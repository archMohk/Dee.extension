# -*- coding: utf-8 -*-
"""
acc_upload
Saves local files into an ACC/BIM360 Docs folder, and creates folders
there. No Revit API and no UI, so it can be tested outside Revit
(tools/checks/test_acc_upload.py).

One file = the Data Management upload sequence:
  1. POST storage              a place for the bytes
  2. signed S3 upload          PUT the parts, then POST the completion
  3. POST items / versions     a new file, or a NEW VERSION of the file
                               with that name already in the folder

Needs a token with acc_auth.SCOPE_CREATE. ACC itself still decides: the
signed-in user needs Create (upload) permission on the folder.

    up = Uploader(dest, base_folder)
    ok, detail = up.upload(path)

`dest` is plain JSON data (remembered in settings, stored in DeeSchedule
tasks): account, hub_id, project_id, project_name, folder_id, folder_path.
"""
import os
import re
import time

import clr
clr.AddReference("System")
clr.AddReference("System.Net.Http")
import System
from System.IO import File, FileMode, FileAccess, FileShare
from System.Net.Http import HttpClient, HttpRequestMessage, HttpMethod, ByteArrayContent

import acc_api
import acc_auth

# S3 wants at least 5 MB per part (the last may be smaller).
PART_SIZE = 20 * 1024 * 1024
_URLS_PER_REQUEST = 25
_MAX_NAME = 255
_TRIES = 3
_WAIT_SECONDS = 2
_INVALID_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_JSONAPI = "application/vnd.api+json"


class UploadCancelled(Exception):
    pass


def clean_name(name):
    """A file or folder name ACC accepts."""
    name = _INVALID_NAME.sub("_", name or "").strip().rstrip(".").strip()
    return name[:_MAX_NAME]


def dest_label(dest):
    return u"{0} / {1}".format(dest.get("project_name") or "?",
                               dest.get("folder_path") or "?")


def explain(error):
    """A readable reason for an APS failure."""
    text = u"{0}".format(error)
    if "Forbidden" in text:
        return (u"ACC refused it (no permission). You need Create permission "
                u"(upload) on that folder")
    if "Unauthorized" in text:
        return u"the Autodesk sign-in is no longer valid - sign in again"
    if "NotFound" in text:
        return u"ACC can't find that folder any more (deleted, or no access)"
    return text if len(text) < 400 else text[:400] + u"..."


def _permanent(error):
    """A 4xx other than a timeout or 429: trying again cannot help."""
    text = u"{0}".format(error)
    return any("({0})".format(code) in text for code in (
        "Unauthorized", "Forbidden", "NotFound", "BadRequest", "Conflict"))


def _retry(fn):
    for attempt in range(_TRIES):
        try:
            return fn()
        except Exception as e:
            if attempt == _TRIES - 1 or _permanent(e):
                raise
            time.sleep(_WAIT_SECONDS * (attempt + 1))


# --------------------------------------------------------------------------
# folders
# --------------------------------------------------------------------------
def _contents(project_id, folder_id, kind, token):
    """Every (id, displayName) of one kind ("folders" / "items")."""
    found = []
    url = "{0}/data/v1/projects/{1}/folders/{2}/contents?filter[type]={3}&page[limit]=200".format(
        acc_api.BASE_URL, project_id, folder_id, kind)
    while url:
        page = url
        data = _retry(lambda: acc_api.api_get(page, token))
        for entry in data.get("data", []):
            attrs = entry.get("attributes", {})
            if entry.get("type") == kind and not attrs.get("hidden"):
                name = attrs.get("displayName") or attrs.get("name") or ""
                if name:
                    found.append((entry["id"], name))
        link = (data.get("links") or {}).get("next")
        url = link.get("href") if isinstance(link, dict) else link
    return found


def list_subfolders(project_id, folder_id, token):
    return _contents(project_id, folder_id, "folders", token)


def _find(entries, name):
    want = name.lower()
    for entry_id, entry_name in entries:
        if entry_name.lower() == want:
            return entry_id
    return None


def create_folder(project_id, parent_id, name, token):
    """The id of the subfolder `name`: the existing one if there is one,
    else a new one. Raises on failure."""
    name = clean_name(name)
    if not name:
        raise Exception("that is not a valid folder name")
    existing = _find(list_subfolders(project_id, parent_id, token), name)
    if existing:
        return existing
    body = {
        "jsonapi": {"version": "1.0"},
        "data": {
            "type": "folders",
            "attributes": {
                "name": name,
                "extension": {"type": "folders:autodesk.bim360:Folder", "version": "1.0"},
            },
            "relationships": {"parent": {"data": {"type": "folders", "id": parent_id}}},
        },
    }
    url = "{0}/data/v1/projects/{1}/folders".format(acc_api.BASE_URL, project_id)
    try:
        return acc_api.api_post(url, token, body, _JSONAPI)["data"]["id"]
    except Exception as e:
        if "(Conflict)" in u"{0}".format(e):
            # somebody else made it in the meantime
            existing = _find(list_subfolders(project_id, parent_id, token), name)
            if existing:
                return existing
        raise


# --------------------------------------------------------------------------
# one file
# --------------------------------------------------------------------------
def _new_storage(project_id, folder_id, name, token):
    body = {
        "jsonapi": {"version": "1.0"},
        "data": {
            "type": "objects",
            "attributes": {"name": name},
            "relationships": {"target": {"data": {"type": "folders", "id": folder_id}}},
        },
    }
    url = "{0}/data/v1/projects/{1}/storage".format(acc_api.BASE_URL, project_id)
    return _retry(lambda: acc_api.api_post(url, token, body, _JSONAPI))["data"]["id"]


def _read_part(stream, size):
    """The next `size` bytes as a .NET byte[] of exactly that length."""
    buf = System.Array.CreateInstance(System.Byte, size)
    done = 0
    while done < size:
        got = stream.Read(buf, done, size - done)
        if got <= 0:
            raise Exception("the file became shorter while it was uploading")
        done += got
    return buf


def _put(client, url, data):
    # No Authorization header: the URL itself is signed.
    request = HttpRequestMessage(HttpMethod.Put, url)
    request.Content = ByteArrayContent(data)
    response = client.SendAsync(request).Result
    try:
        if not response.IsSuccessStatusCode:
            # Not "(Forbidden)": that would read as a missing ACC permission.
            raise Exception("the upload was rejected, status {0}".format(
                int(response.StatusCode)))
    finally:
        response.Dispose()


def _send_bytes(storage_id, path, token, progress):
    """storage_id: "urn:adsk.objects:os.object:<bucket>/<object key>"."""
    if "os.object:" not in storage_id or "/" not in storage_id:
        raise Exception("ACC gave an unexpected storage id '{0}'".format(storage_id))
    bucket, key = storage_id.split("os.object:", 1)[1].split("/", 1)
    base = "{0}/oss/v2/buckets/{1}/objects/{2}/signeds3upload".format(
        acc_api.BASE_URL, bucket, System.Uri.EscapeDataString(key))

    client = HttpClient()
    client.Timeout = System.TimeSpan.FromMinutes(30)
    stream = File.Open(path, FileMode.Open, FileAccess.Read, FileShare.Read)
    try:
        total = int(stream.Length)
        if total == 0:
            raise Exception("the exported file is empty")
        parts = (total + PART_SIZE - 1) // PART_SIZE
        urls = {}                       # part number -> signed URL
        state = {"key": ""}             # names this upload to OSS

        def signed_url(number, fresh):
            """URLs come in batches and live 60 minutes; `fresh` asks again
            (a retry may be about a URL that expired)."""
            if fresh or number not in urls:
                query = "?parts={0}&firstPart={1}&minutesExpiration=60".format(
                    min(_URLS_PER_REQUEST, parts - number + 1), number)
                if state["key"]:
                    query += "&uploadKey=" + System.Uri.EscapeDataString(state["key"])
                data = _retry(lambda: acc_api.api_get(base + query, token))
                state["key"] = data["uploadKey"]
                for i, url in enumerate(data["urls"]):
                    urls[number + i] = url
            return urls[number]

        sent = 0
        for number in range(1, parts + 1):
            if progress is not None and progress(sent, total) is False:
                raise UploadCancelled("cancelled")
            data = _read_part(stream, min(PART_SIZE, total - sent))
            for attempt in range(_TRIES):
                try:
                    _put(client, signed_url(number, attempt > 0), data)
                    break
                except Exception:
                    if attempt == _TRIES - 1:
                        raise
                    time.sleep(_WAIT_SECONDS * (attempt + 1))
            sent += data.Length
        if progress is not None and progress(sent, total) is False:
            raise UploadCancelled("cancelled")
        _retry(lambda: acc_api.api_post(base, token, {"uploadKey": state["key"]},
                                        "application/json"))
    finally:
        stream.Dispose()
        client.Dispose()


def _version_body(name, storage_id):
    return {
        "type": "versions",
        "attributes": {
            "name": name,
            "extension": {"type": "versions:autodesk.bim360:File", "version": "1.0"},
        },
        "relationships": {"storage": {"data": {"type": "objects", "id": storage_id}}},
    }


def _new_item(project_id, folder_id, name, storage_id, token):
    version = _version_body(name, storage_id)
    version["id"] = "1"
    body = {
        "jsonapi": {"version": "1.0"},
        "data": {
            "type": "items",
            "attributes": {
                "displayName": name,
                "extension": {"type": "items:autodesk.bim360:File", "version": "1.0"},
            },
            "relationships": {
                "tip": {"data": {"type": "versions", "id": "1"}},
                "parent": {"data": {"type": "folders", "id": folder_id}},
            },
        },
        "included": [version],
    }
    acc_api.api_post("{0}/data/v1/projects/{1}/items".format(acc_api.BASE_URL, project_id),
                     token, body, _JSONAPI)


def _new_version(project_id, item_id, name, storage_id, token):
    version = _version_body(name, storage_id)
    version["relationships"]["item"] = {"data": {"type": "items", "id": item_id}}
    data = acc_api.api_post(
        "{0}/data/v1/projects/{1}/versions".format(acc_api.BASE_URL, project_id),
        token, {"jsonapi": {"version": "1.0"}, "data": version}, _JSONAPI)
    return (data.get("data", {}).get("attributes", {}) or {}).get("versionNumber")


def upload_file(project_id, folder_id, path, token, progress=None):
    """Uploads `path` into the folder. Returns "new file" or "version N".
    progress(sent, total) may return False to stop. Raises on failure."""
    name = clean_name(os.path.basename(path))
    storage_id = _new_storage(project_id, folder_id, name, token)
    _send_bytes(storage_id, path, token, progress)
    item_id = _find(_contents(project_id, folder_id, "items", token), name)
    if item_id is None:
        _new_item(project_id, folder_id, name, storage_id, token)
        return "new file"
    number = _new_version(project_id, item_id, name, storage_id, token)
    return "version {0}".format(number) if number else "new version"


# --------------------------------------------------------------------------
# a batch
# --------------------------------------------------------------------------
class Uploader(object):
    """Uploads files found under base_folder, recreating their subfolders
    under the ACC destination. upload() never raises and never opens a
    browser - a failure is returned, and kept in .failed for retry().

    delete_after: base_folder is only a staging area; a file is removed
    once it is safely on ACC."""

    def __init__(self, dest, base_folder, delete_after=False):
        self.dest = dest
        self.base_folder = base_folder
        self.delete_after = delete_after
        self.progress_bar = None        # anything with .title / .cancelled
        self.failed = []                # [(path, detail)]
        self._folders = {}              # "ar/l02/" -> folder id

    def _token(self):
        return acc_auth.get_access_token(
            account=self.dest.get("account") or None,
            scope=acc_auth.SCOPE_CREATE, interactive=False)

    def _sub_parts(self, path):
        rel = os.path.relpath(os.path.dirname(path), self.base_folder)
        return [p for p in rel.replace("\\", "/").split("/") if p and p != "."]

    def _folder_id(self, parts, token):
        folder_id, key = self.dest["folder_id"], ""
        for part in parts:
            key += part.lower() + "/"
            if key not in self._folders:
                self._folders[key] = create_folder(
                    self.dest["project_id"], folder_id, part, token)
            folder_id = self._folders[key]
        return folder_id

    def _progress(self, name):
        def report(sent, total):
            bar = self.progress_bar
            if bar is None:
                return True
            try:
                bar.title = u"Saving {0} on ACC ({1:.0f} of {2:.0f} MB)".format(
                    name, sent / 1048576.0, total / 1048576.0)
                return not bar.cancelled
            except Exception:
                return True
        return report

    def upload(self, path):
        """(ok, detail) for one file."""
        name = os.path.basename(path)
        title = getattr(self.progress_bar, "title", None)
        try:
            parts = self._sub_parts(path)
            token = self._token()
            folder_id = self._folder_id(parts, token)
            what = upload_file(self.dest["project_id"], folder_id, path, token,
                               progress=self._progress(name))
        except Exception as e:
            detail = u"Exported, but NOT saved on ACC: {0}. Local file: {1}".format(
                explain(e), path)
            self.failed.append((path, detail))
            return False, detail
        finally:
            try:
                if self.progress_bar is not None and title is not None:
                    self.progress_bar.title = title
            except Exception:
                pass
        if self.delete_after:
            try:
                os.remove(path)
            except Exception:
                pass
        return True, u"Saved '{0}' on ACC in {1} ({2})".format(
            name, u" / ".join([dest_label(self.dest)] + parts), what)

    def retry(self):
        """Uploads the failed files again: [(old detail, ok, new detail)]."""
        again, self.failed = self.failed, []
        self._folders = {}
        done, out = {}, []
        for path, old_detail in again:
            if path not in done:        # two views can share a file name
                done[path] = self.upload(path)
            out.append((old_detail,) + done[path])
        return out
