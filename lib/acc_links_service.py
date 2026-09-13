# -*- coding: utf-8 -*-
"""Read a cloud-workshared Revit model's own Revit links straight out of
ACC - WITHOUT opening the model in Revit and WITHOUT Design Automation.

WHY THIS EXISTS
---------------
DeeLinkMAP originally answered "what does each model link?" the only way
that was known to work: open every model headlessly in the running Revit,
read RevitLinkType, close it. That is minutes per file, and a long batch
of open/close cycles is exactly the pattern that kept taking Revit down.
This module replaces that with plain HTTPS calls, so a whole project can
be mapped in seconds with nothing opened at all.

WHAT WAS ACTUALLY VERIFIED (live, 2026-09-13, against a real NAGA ACC
project - every response shape below was OBSERVED, not read off a docs
page; the APS reference pages for these endpoints render as an empty
single-page-app shell and could not be fetched at all)
------------------------------------------------------------------------
1. GET /data/v1/projects/{projectId}/items/{itemId}
   Returns the item AND, in `included`, its TIP VERSION - so one request
   per file yields versionId, versionNumber and the whole C4R extension
   block. That block is where the unique cloud identifiers live:

       "modelGuid":   "4d979840-1fc2-425e-ae10-b8db5992f158",
       "projectGuid": "33201399-425b-4dbc-b554-2efa678b0312",
       "hasLinks":     true/false,
       "publishType": "NoZipFile" | "WithoutLinks",
       "revitProjectVersion": 2026,
       "processState": "PROCESSING_COMPLETE"

   modelGuid + projectGuid are exactly the two GUIDs
   ModelPathUtils.ConvertCloudGUIDsToCloudPath(region, projectGuid,
   modelGuid) wants. Nothing here is invented or inferred from a file
   name - ACC hands them over directly.

2. GET /construction/rcm/v1/projects/{BARE-projectId}
       /published-versions/{versionId}/linked-files?includeHost=true
   THE key endpoint. Returns the host plus every Revit model it links:

       {"hostFile": {"modelName", "itemId", "versionId", "size",
                     "publishStatus", "signedUrl"},
        "linkedFiles": {"pagination": {"limit","offset","totalResults"},
                        "results": [{"modelName", "itemId", "size",
                                     "publishStatus", "signedUrl"}]}}

   - The projectId must be the BARE GUID (the "b." prefix stripped).
   - The caller's ordinary 3-legged PKCE token works as-is. No
     `code:all` scope, no Design Automation, no AppBundle/Activity to
     publish, no cloud credits.
   - Each linked entry carries its OWN itemId. That is a real ACC
     identifier, so links resolve BY ID - never by matching file names,
     which is the whole point of the requirement this implements.
   - Linked entries carry NO versionId (they come back as
     publishStatus "NotPublished"). resolve_link() below closes that gap
     with one follow-up item lookup per distinct linked itemId.
   - Cross-checked: the linked entry's signedUrl path embeds
     .../models/<modelGuid>/version_N.rvt, and that modelGuid matched
     the modelGuid returned by looking its itemId up independently. Two
     unrelated identifiers agreeing is good evidence the itemId is the
     right model and not a same-named different one.

3. THE REAL LIMIT - not every version is eligible.
   A version published the older way answers 404 with:
       "The requested version was published before the release of this
        feature."
   Observed split in the project tested: publishType "NoZipFile" -> the
   endpoint works; publishType "WithoutLinks" -> 404. In that project
   that was 5 eligible models out of 155. So this route is a FAST PATH
   that must always degrade gracefully, never the only path - callers
   get UNSUPPORTED_PUBLISH back and can fall back to opening that one
   model, or ask the user to re-publish it.
   Re-publishing a model normally (Publish WITH links - what
   acc_api.publish_item(without_links=False) triggers) is the obvious
   candidate for making an old model eligible, but that was NOT tested
   here: testing it would have published someone else's live model.
   Treat it as a lead, not a fact.

4. NOT a route: GET /data/v1/.../versions/{id}/relationships/refs
   returns "data": [] for C4R models. Version refs are populated by the
   Docs "upload linked files" workflow, not by cloud worksharing. Tried
   and rejected - recorded so nobody spends the afternoon on it twice.

WHAT THIS ROUTE CANNOT GIVE YOU
-------------------------------
The endpoint reports WHICH models are linked, not how they sit inside
the host. Link instance counts, transforms/positioning, per-instance
load status, attachment vs overlay, and workset of the link all live in
the Revit document itself - reading those still requires opening the
model (locally, or in Design Automation). Callers that need them must
say so rather than reporting a blank as a zero.
"""
import threading
import time

try:
    import Queue as queue          # IronPython 2.7 / Python 2
except ImportError:
    import queue                   # Python 3 (unit tests run here)

try:
    from urllib import quote as _quote        # Python 2
except ImportError:
    from urllib.parse import quote as _quote  # Python 3

import acc_api

BASE_URL = acc_api.BASE_URL

# How a linked model was tied back to an ACC item. Reported per link so a
# map never presents a guess as if it were a verified match.
RESOLVED_BY_ID = "RESOLVED_BY_ID"
RESOLVED_BY_PATH = "RESOLVED_BY_PATH"
RESOLVED_BY_FILENAME = "RESOLVED_BY_FILENAME"
UNRESOLVED = "UNRESOLVED"

# Why a host could not be read through the API - each maps to a distinct
# thing the user can actually do about it.
UNSUPPORTED_PUBLISH = "UNSUPPORTED_PUBLISH"   # published before the feature existed
NOT_FOUND = "NOT_FOUND"
NO_ACCESS = "NO_ACCESS"                       # 401/403 - permissions or stale token
API_ERROR = "API_ERROR"

# The publish workflow whose versions the linked-files endpoint can
# serve. Observed live: versions carrying this publishType answered
# normally; versions carrying "WithoutLinks" answered 404 "published
# before the release of this feature".
NEW_WORKFLOW_PUBLISH_TYPE = "NoZipFile"

_PERMANENT_MARKERS = ("Unauthorized", "Forbidden", "401", "403")
_MAX_ATTEMPTS = 4
_SCAN_TIME_BUDGET_SECONDS = 480


def _enc(value):
    """URL-encodes a whole URN as ONE path segment. safe="" matters: a
    version id ends in "?version=3", and leaving that "?" unencoded turns
    the rest of the id into a query string and the request 404s."""
    return _quote(value, safe="")


def bare_project_id(project_id):
    """ACC project ids come back from Data Management as "b.<guid>". The
    /construction/* APIs want the bare guid; Data Management accepts
    either. Verified live - the prefixed form 404s nothing extra, but the
    bare form is what the RCM endpoint documents."""
    if project_id and project_id.startswith("b."):
        return project_id[2:]
    return project_id


def _is_permanent(error_text):
    return any(marker in error_text for marker in _PERMANENT_MARKERS)


def _retry_delay(attempt, error_text):
    if "TooManyRequests" in error_text or "429" in error_text:
        return min(30, 4 * (2 ** attempt))
    return 2 * (attempt + 1)


def _get_with_retry(url, token):
    """GET with the same retry policy the folder scanner learned the hard
    way: back off on transient failures, but give up immediately on
    401/403, which can never succeed on retry and whose retries once
    added up to minutes of a frozen-looking Revit."""
    last = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            return acc_api.api_get(url, token)
        except Exception as e:
            last = str(e)
            if _is_permanent(last):
                break
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(_retry_delay(attempt, last))
    raise Exception(last or "request failed")


def classify_error(error_text):
    """Maps a raw request failure onto one of the reason constants."""
    text = error_text or ""
    if "published before the release of this feature" in text:
        return UNSUPPORTED_PUBLISH
    if _is_permanent(text):
        return NO_ACCESS
    if "404" in text or "NotFound" in text:
        return NOT_FOUND
    return API_ERROR


# --------------------------------------------------------------------
# Items and versions
# --------------------------------------------------------------------

def _version_info(version_entry):
    attrs = version_entry.get("attributes", {}) or {}
    ext = (attrs.get("extension", {}) or {}).get("data", {}) or {}
    return {
        "version_id": version_entry.get("id"),
        "version_number": attrs.get("versionNumber"),
        "version_name": attrs.get("displayName") or attrs.get("name"),
        "created_time": attrs.get("createTime"),
        "created_by": attrs.get("createUserName"),
        "file_type": attrs.get("fileType"),
        "storage_size": attrs.get("storageSize"),
        # C4R-specific block - absent on a plain uploaded .rvt
        "model_guid": ext.get("modelGuid"),
        "project_guid": ext.get("projectGuid"),
        "has_links": ext.get("hasLinks"),
        "publish_type": ext.get("publishType"),
        "process_state": ext.get("processState"),
        "revit_version": ext.get("revitProjectVersion"),
        "is_composite": ext.get("isCompositeDesign"),
        "is_cloud_workshared": ext.get("modelType") == "multiuser",
    }


def get_item_tip(project_id, item_id, token):
    """ONE request that returns both the item and its tip version.

    Returns a dict with item_name, folder_id and every field
    _version_info produces. This is the cheap per-file metadata call -
    155 of them ran in 16 seconds live at 8 threads."""
    url = "{0}/data/v1/projects/{1}/items/{2}".format(
        BASE_URL, project_id, _enc(item_id))
    data = _get_with_retry(url, token)

    entry = data.get("data", {}) or {}
    attrs = entry.get("attributes", {}) or {}
    rels = entry.get("relationships", {}) or {}

    tip = None
    for included in data.get("included", []) or []:
        if included.get("type") == "versions":
            tip = included
            break
    # Fall back to the tip pointer if `included` was not inlined - the
    # id alone is still useful even without the version's attributes.
    info = _version_info(tip) if tip is not None else {
        "version_id": ((rels.get("tip", {}) or {}).get("data", {}) or {}).get("id")
    }

    info["item_id"] = entry.get("id") or item_id
    info["item_name"] = attrs.get("displayName")
    info["folder_id"] = ((rels.get("parent", {}) or {}).get("data", {}) or {}).get("id")
    info["file_extension"] = _extension_of(info.get("item_name"))
    return info


def _extension_of(name):
    if not name or "." not in name:
        return ""
    return name.rsplit(".", 1)[-1].lower()


def list_versions(project_id, item_id, token):
    """Every version of one item, newest first, as plain dicts.

    The scanner uses the tip by default; this exists so a caller can
    offer "map an older version instead"."""
    url = "{0}/data/v1/projects/{1}/items/{2}/versions".format(
        BASE_URL, project_id, _enc(item_id))
    data = _get_with_retry(url, token)
    out = []
    for entry in data.get("data", []) or []:
        info = _version_info(entry)
        info["item_id"] = item_id
        out.append(info)
    out.sort(key=lambda v: (v.get("version_number") or 0), reverse=True)
    return out


def folder_path(project_id, folder_id, token, cache=None):
    """Walks parent folders up to the project root, returning
    "Project Files/Architecture/Zone 1". Cached per folder id because a
    project's models cluster into a handful of folders and re-walking
    the same chain per file would triple the request count."""
    if not folder_id:
        return ""
    if cache is None:
        cache = {}
    if folder_id in cache:
        return cache[folder_id]

    names = []
    current = folder_id
    seen = set()
    while current and current not in seen:
        seen.add(current)
        if current in cache:
            # Splice the already-known prefix on and stop walking.
            known = cache[current]
            names.append(known)
            break
        try:
            url = "{0}/data/v1/projects/{1}/folders/{2}".format(
                BASE_URL, project_id, _enc(current))
            data = _get_with_retry(url, token)
        except Exception:
            break
        entry = data.get("data", {}) or {}
        name = (entry.get("attributes", {}) or {}).get("displayName")
        if not name:
            break
        names.append(name)
        parent = (((entry.get("relationships", {}) or {}).get("parent", {}) or {})
                  .get("data", {}) or {}).get("id")
        current = parent

    ordered = list(reversed(names))
    # The walk terminates at the project's synthetic root, whose display
    # name is "<project-guid>-root-folder" - noise in every single path.
    if ordered and ordered[0].endswith("-root-folder"):
        ordered = ordered[1:]
    path = "/".join(ordered)
    cache[folder_id] = path
    return path


# --------------------------------------------------------------------
# The linked-files endpoint
# --------------------------------------------------------------------

class LinkedFilesUnavailable(Exception):
    """Raised when the RCM endpoint cannot serve this version. `reason`
    is one of the reason constants so a caller can tell "this model was
    published the old way" (fixable - re-publish, or fall back to
    opening it) apart from "you have no access to it" (not fixable by
    re-running)."""

    def __init__(self, reason, detail):
        Exception.__init__(self, detail)
        self.reason = reason
        self.detail = detail


def get_linked_files(project_id, version_id, token, include_host=True):
    """The model's Revit links, read from ACC with nothing opened.

    Returns {"host": {...}, "links": [{...}], "total": n, "truncated":
    bool}. Raises LinkedFilesUnavailable when this version is not
    eligible (see the module docstring - the common case is a version
    published before Autodesk shipped this endpoint)."""
    url = ("{0}/construction/rcm/v1/projects/{1}/published-versions/{2}"
           "/linked-files{3}").format(
        BASE_URL, bare_project_id(project_id), _enc(version_id),
        "?includeHost=true" if include_host else "")
    try:
        data = _get_with_retry(url, token)
    except Exception as e:
        text = str(e)
        raise LinkedFilesUnavailable(classify_error(text), text)

    host_raw = data.get("hostFile") or {}
    linked = data.get("linkedFiles") or {}
    pagination = linked.get("pagination") or {}
    results = linked.get("results") or []

    host = {
        "name": host_raw.get("modelName"),
        "item_id": host_raw.get("itemId"),
        "version_id": host_raw.get("versionId"),
        "size": host_raw.get("size"),
        "publish_status": host_raw.get("publishStatus"),
    }
    links = []
    for entry in results:
        links.append({
            "name": entry.get("modelName"),
            "item_id": entry.get("itemId"),
            "size": entry.get("size"),
            "publish_status": entry.get("publishStatus"),
            # The signed S3 URL is deliberately NOT carried forward. It
            # expires in an hour, it is 1.8KB of credentials-bearing
            # text per link, and nothing in a link MAP needs to download
            # the model. A future "download the links" feature should
            # re-request it at the moment of use rather than store it.
            "model_guid": _model_guid_from_signed_url(entry.get("signedUrl")),
        })

    total = pagination.get("totalResults", len(links))
    return {
        "host": host,
        "links": links,
        "total": total,
        # The endpoint pages at 600; a model with more links than that
        # would silently look smaller than it is if this went unreported.
        "truncated": bool(total) and len(links) < total,
    }


def _model_guid_from_signed_url(signed_url):
    """Pulls the cloud modelGuid out of the signed S3 path, which looks
    like .../projects/<projectGuid>/models/<modelGuid>/version_N.rvt.

    A second, independent identifier for the same model: it was checked
    live against the modelGuid returned by resolving the link's itemId
    on its own, and they matched. Used only to corroborate an id-based
    match - never as the match itself."""
    if not signed_url or "/models/" not in signed_url:
        return None
    try:
        tail = signed_url.split("/models/", 1)[1]
        guid = tail.split("/", 1)[0]
        return guid if len(guid) == 36 else None
    except Exception:
        return None


# --------------------------------------------------------------------
# Resolving a link back to its ACC item / version / folder
# --------------------------------------------------------------------

def resolve_link(project_id, link, token, item_cache=None, folder_cache=None,
                 name_index=None):
    """Fills in the linked model's ACC version and folder.

    `name_index` is an optional {lower-case file name: item_id} built
    from a project scan, used ONLY when the endpoint gave no itemId - and
    when it is used the match is reported as RESOLVED_BY_FILENAME, never
    dressed up as a verified one, because several projects legitimately
    contain several files with the same name."""
    if item_cache is None:
        item_cache = {}
    if folder_cache is None:
        folder_cache = {}

    out = dict(link)
    item_id = link.get("item_id")
    method = RESOLVED_BY_ID

    if not item_id and name_index:
        candidate = name_index.get((link.get("name") or "").lower())
        if candidate:
            item_id = candidate
            method = RESOLVED_BY_FILENAME

    if not item_id:
        out["match_method"] = UNRESOLVED
        out["project_id"] = project_id
        return out

    if item_id in item_cache:
        info = item_cache[item_id]
    else:
        try:
            info = get_item_tip(project_id, item_id, token)
        except Exception as e:
            out["match_method"] = UNRESOLVED
            out["error"] = str(e)
            out["project_id"] = project_id
            return out
        item_cache[item_id] = info

    out["item_id"] = item_id
    out["match_method"] = method
    out["project_id"] = project_id
    out["version_id"] = info.get("version_id")
    out["version_number"] = info.get("version_number")
    out["folder_id"] = info.get("folder_id")
    out["folder_path"] = folder_path(project_id, info.get("folder_id"), token, folder_cache)
    # Corroboration, not identification: if the guid embedded in the
    # signed URL disagrees with the guid of the item we resolved, say so
    # loudly rather than quietly presenting a wrong link.
    url_guid = link.get("model_guid")
    item_guid = info.get("model_guid")
    if url_guid and item_guid and url_guid != item_guid:
        out["match_method"] = UNRESOLVED
        out["error"] = ("model guid mismatch - signed url says {0}, "
                        "resolved item says {1}".format(url_guid, item_guid))
    elif item_guid:
        out["model_guid"] = item_guid
    return out


# --------------------------------------------------------------------
# Batch scan
# --------------------------------------------------------------------

def scan_host(project_id, item_id, token, item_cache=None, folder_cache=None,
              name_index=None, version_id=None):
    """Everything about one host model: its own identifiers, and its
    Revit links resolved back to ACC items/versions.

    Never raises for an ordinary failure - an unreadable host comes back
    with status "failed" and a reason, so one bad model in a batch of two
    hundred can never take the batch down."""
    if item_cache is None:
        item_cache = {}
    if folder_cache is None:
        folder_cache = {}

    try:
        if item_id in item_cache:
            info = item_cache[item_id]
        else:
            info = get_item_tip(project_id, item_id, token)
            item_cache[item_id] = info
    except Exception as e:
        return {
            "item_id": item_id, "status": "failed",
            "reason": classify_error(str(e)), "detail": str(e), "links": [],
        }

    result = {
        "item_id": item_id,
        "name": info.get("item_name"),
        "version_id": version_id or info.get("version_id"),
        "version_number": info.get("version_number"),
        "model_guid": info.get("model_guid"),
        "project_guid": info.get("project_guid"),
        "project_id": project_id,
        "folder_id": info.get("folder_id"),
        "revit_version": info.get("revit_version"),
        "publish_type": info.get("publish_type"),
        "has_links": info.get("has_links"),
        "links": [],
        "status": "ok",
    }

    if not result["version_id"]:
        result["status"] = "failed"
        result["reason"] = NOT_FOUND
        result["detail"] = "no tip version"
        return result

    new_workflow = info.get("publish_type") == NEW_WORKFLOW_PUBLISH_TYPE

    # "hasLinks: false" is only a real ANSWER on a version published the
    # new way. On an older "WithoutLinks" publish the same false means
    # "this published version carries no link data" - which is also what
    # it says for a model that is full of links but was published with
    # them stripped. Reporting that as "no links" would be a silent false
    # negative, and a link map that quietly omits links is worse than one
    # that admits it does not know. So: say we cannot tell, and name the
    # reason, which is the same thing the endpoint itself would answer
    # (404, "published before the release of this feature") if asked.
    if not new_workflow:
        result["status"] = "unavailable"
        result["reason"] = UNSUPPORTED_PUBLISH
        result["detail"] = (
            "published as '{0}' - ACC cannot report this version's links. "
            "Re-publish the model (a normal publish, with links) or read it "
            "by opening the model.".format(info.get("publish_type")))
        return result

    # Published the new way AND ACC says there are no links: that is a
    # trustworthy no, so skip a request that would only confirm it.
    if info.get("has_links") is False:
        result["status"] = "ok"
        result["links_source"] = "acc-haslinks-false"
        return result

    try:
        payload = get_linked_files(project_id, result["version_id"], token)
    except LinkedFilesUnavailable as e:
        result["status"] = "unavailable"
        result["reason"] = e.reason
        result["detail"] = e.detail
        return result

    result["links_source"] = "acc-linked-files"
    result["truncated"] = payload.get("truncated", False)
    result["links"] = [
        resolve_link(project_id, link, token, item_cache, folder_cache, name_index)
        for link in payload.get("links", [])
    ]
    return result


def scan_project(project_id, items, token, max_workers=6, on_progress=None,
                 should_cancel=None, time_budget=_SCAN_TIME_BUDGET_SECONDS):
    """Scans many host models concurrently and returns a list of
    scan_host results in the order `items` was given.

    `items` is [(item_id, name), ...]. Concurrency follows the pattern
    acc_file_browser.scan_level had to learn live: ONE pool of workers
    created once (repeatedly spawning and discarding real .NET threads
    from IronPython is the pattern that was taking Revit down), a hard
    time budget so a scan can never run forever, and every worker body
    wrapped so an escaping exception can never kill the process."""
    total = len(items)
    results = [None] * total
    q = queue.Queue()
    for index, entry in enumerate(items):
        q.put((index, entry))

    lock = threading.Lock()
    done = [0]
    cancelled = [False]
    started = time.time()
    # Shared across workers so a linked model referenced by thirty hosts
    # is looked up once. Plain dicts: CPython and IronPython both make
    # single key get/set atomic, and a duplicate lookup on a race is
    # harmless anyway.
    item_cache = {}
    folder_cache = {}
    name_index = dict((name.lower(), iid) for iid, name in items if name)

    def worker():
        while True:
            try:
                index, (item_id, _name) = q.get(timeout=0.5)
            except queue.Empty:
                if q.unfinished_tasks == 0:
                    return
                continue
            try:
                if cancelled[0] or (time.time() - started) > time_budget:
                    results[index] = {
                        "item_id": item_id, "name": _name, "status": "skipped",
                        "reason": "time budget reached" if not cancelled[0] else "cancelled",
                        "links": [],
                    }
                else:
                    results[index] = scan_host(
                        project_id, item_id, token,
                        item_cache, folder_cache, name_index)
            except Exception as thread_exc:
                # An exception escaping a real .NET thread terminates the
                # whole process - Revit included. Pure insurance.
                results[index] = {
                    "item_id": item_id, "name": _name, "status": "failed",
                    "reason": API_ERROR, "detail": str(thread_exc), "links": [],
                }
            finally:
                with lock:
                    done[0] += 1
                q.task_done()

    workers = []
    for _ in range(max(1, min(max_workers, total or 1))):
        t = threading.Thread(target=worker)
        t.daemon = True
        t.start()
        workers.append(t)

    while any(t.is_alive() for t in workers):
        if should_cancel is not None and should_cancel():
            cancelled[0] = True
        if on_progress is not None:
            try:
                on_progress(done[0], total)
            except Exception:
                pass
        for t in workers:
            t.join(0.3)

    if on_progress is not None:
        try:
            on_progress(done[0], total)
        except Exception:
            pass

    return [r for r in results if r is not None]


# --------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------

def summarize(results):
    """Counts by outcome, for a one-line "what happened" banner."""
    summary = {"ok": 0, "no_links": 0, "unavailable": 0, "failed": 0,
               "skipped": 0, "links_total": 0, "unresolved_links": 0}
    for r in results:
        status = r.get("status")
        if status == "ok":
            if r.get("links"):
                summary["ok"] += 1
            else:
                summary["no_links"] += 1
        elif status in summary:
            summary[status] += 1
        for link in r.get("links", []):
            summary["links_total"] += 1
            if link.get("match_method") != RESOLVED_BY_ID:
                summary["unresolved_links"] += 1
    return summary


def to_json_payload(results):
    """The results in the documented interchange shape: one entry per
    host, each with its links and how each link was matched."""
    payload = []
    for r in results:
        entry = {
            "host": {
                "fileName": r.get("name"),
                "itemId": r.get("item_id"),
                "versionId": r.get("version_id"),
                "versionNumber": r.get("version_number"),
                "projectId": r.get("project_id"),
                "folderId": r.get("folder_id"),
                "modelGuid": r.get("model_guid"),
                "projectGuid": r.get("project_guid"),
                "revitVersion": r.get("revit_version"),
            },
            "status": r.get("status"),
            "links": [],
        }
        if r.get("reason"):
            entry["reason"] = r.get("reason")
            entry["detail"] = r.get("detail")
        for link in r.get("links", []):
            entry["links"].append({
                "name": link.get("name"),
                "status": link.get("publish_status"),
                "cloud": True,
                "matchMethod": link.get("match_method"),
                "linkedItemId": link.get("item_id"),
                "linkedVersionId": link.get("version_id"),
                "linkedVersionNumber": link.get("version_number"),
                "linkedProjectId": link.get("project_id"),
                "linkedFolderId": link.get("folder_id"),
                "linkedFolderPath": link.get("folder_path"),
                "modelGuid": link.get("model_guid"),
                "error": link.get("error"),
            })
        payload.append(entry)
    return payload
