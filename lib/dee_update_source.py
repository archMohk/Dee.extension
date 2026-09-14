# -*- coding: utf-8 -*-
"""Install and update the extension from Supabase, file by file.

WHY THIS EXISTS
---------------
GitHub is the normal route: pyRevit clones the repo and `pyrevit
extensions update --all` git-pulls it. On networks that block github.com
that route does not exist at all, so this provides a second one carrying
exactly the same files.

Deliberately NOT a zip. A zip means every update re-downloads everything
and a half-extracted archive leaves a broken extension. This mirrors the
individual files and syncs them the way git does: compare, then fetch
only what actually differs. A typical update moves a handful of KB.

THE MANIFEST IS THE INDEX
-------------------------
Alongside the files, the bucket holds manifest.json:

    {"commit": "5470c44", "built": "2026-09-14T17:40:00Z",
     "files": {"lib/dee_telemetry.py": {"sha256": "ab12...", "size": 8421},
               ...}}

The client downloads that one small file, hashes what it has locally, and
downloads only the differences. No directory listing, no guessing from
timestamps - the hash is the answer.

WHAT THE MIRROR CONTAINS, AND WHY THAT MATTERS
----------------------------------------------
The publisher builds the manifest from `git ls-files`, so the mirror
carries exactly the file set GitHub carries - "the same as GitHub" by
construction rather than by intention. It also means .gitignore protects
the mirror: the ACC token cache, the per-project file caches and the
user's own acc_config.json are untracked, so they can never be uploaded,
and (see below) are never deleted either.

DELETION NEEDS A RECORD, NOT A GUESS
------------------------------------
A file that is in the folder but not in the manifest is NOT automatically
rubbish - it might be the user's acc_config.json, their logs, or a file
from a newer version. Deleting by absence would throw those away. So the
last applied manifest is kept on disk, and only files THIS mirror put
there and the new manifest no longer lists are removed.
"""
import hashlib
import json
import os

try:                                  # IronPython 2.7 inside Revit
    import clr
    clr.AddReference("System")
    clr.AddReference("System.Net.Http")
    from System.Net.Http import HttpClient, HttpRequestMessage, HttpMethod
    _NET = True
except Exception:                     # CPython, for the tests and tooling
    _NET = False
    try:
        import urllib.request as _urlreq
        from urllib.error import HTTPError as _HTTPError
    except ImportError:
        import urllib2 as _urlreq
        from urllib2 import HTTPError as _HTTPError


SOURCE_GITHUB = "github"
SOURCE_SUPABASE = "supabase"

MANIFEST_NAME = "manifest.json"

# Where the applied manifest is remembered, relative to the extension
# root. Dot-prefixed and gitignored, like the other local state files.
APPLIED_MANIFEST = os.path.join("lib", ".dee_mirror_manifest.json")

# Never touched by an update, whatever the manifest says. These are the
# user's, not ours: their ACC credentials and config, their logs, and the
# caches that make the ACC tools fast. Matched on the path as it appears
# in the manifest (forward slashes).
PROTECTED = (
    "acc_config.json",
    "lib/.acc_token_cache.json",
    "lib/.dee_identity.json",
    "lib/.dee_access_status.json",
    "lib/.dee_favorites.json",
    "lib/.dee_force_config.json",
    "lib/.dee_ribbon_mode.json",
    "lib/.dee_linkmap_disciplines.json",
    "lib/.deew_settings/",
    "lib/.dee_mono/",
    "logs/",
)

_CHUNK = 65536


def is_protected(rel_path):
    """True for anything an update must leave alone."""
    path = rel_path.replace("\\", "/")
    for entry in PROTECTED:
        if entry.endswith("/"):
            if path.startswith(entry):
                return True
        elif path == entry or path.endswith("/" + entry):
            return True
    # Any dot-prefixed file directly inside lib/ is local state by this
    # codebase's own convention - listing every one of them above would
    # go stale the moment another is added.
    if path.startswith("lib/.") and "/" not in path[4:].lstrip("."):
        return True
    return False


def file_digest(path):
    """sha256 of a file, or None if it cannot be read. Streamed, because
    a 550 MB model file has no business being loaded into memory - and
    nothing stops someone dropping one into the folder."""
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            while True:
                block = handle.read(_CHUNK)
                if not block:
                    break
                digest.update(block)
        return digest.hexdigest()
    except Exception:
        return None


def public_url(supabase_url, bucket, rel_path):
    """The public download URL for one object.

    Public-bucket reads need no key at all, which is the point on a
    locked-down network: one plain HTTPS GET, no auth dance."""
    quoted = "/".join(_quote(part) for part in
                      rel_path.replace("\\", "/").split("/"))
    return "{0}/storage/v1/object/public/{1}/{2}".format(
        supabase_url.rstrip("/"), bucket, quoted)


def _quote(text):
    safe = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.~"
    out = []
    for ch in text:
        if ch in safe:
            out.append(ch)
        else:
            for byte in ch.encode("utf-8"):
                out.append("%{0:02X}".format(byte if isinstance(byte, int)
                                             else ord(byte)))
    return "".join(out)


def _get_bytes(url, timeout=60):
    """Returns (data_or_None, detail). Never raises."""
    if _NET:
        try:
            client = HttpClient()
            request = HttpRequestMessage(HttpMethod.Get, url)
            response = client.SendAsync(request).Result
            if not response.IsSuccessStatusCode:
                return None, "HTTP {0}".format(response.StatusCode)
            return bytes(response.Content.ReadAsByteArrayAsync().Result), "ok"
        except Exception as e:
            return None, str(e)
    try:
        return _urlreq.urlopen(url, timeout=timeout).read(), "ok"
    except _HTTPError as e:
        return None, "HTTP {0}".format(e.code)
    except Exception as e:
        return None, str(e)


def fetch_manifest(supabase_url, bucket):
    """The remote index. Returns (manifest_or_None, detail)."""
    url = public_url(supabase_url, bucket, MANIFEST_NAME)
    data, detail = _get_bytes(url)
    if data is None:
        return None, "could not reach the mirror ({0})".format(detail)
    try:
        manifest = json.loads(data.decode("utf-8"))
    except Exception as e:
        return None, "mirror manifest is not readable ({0})".format(e)
    if not isinstance(manifest, dict) or "files" not in manifest:
        return None, "mirror manifest has no file list"
    return manifest, "ok"


def load_applied_manifest(root):
    """What this mirror last put on disk, so deletions are a record
    rather than a guess."""
    path = os.path.join(root, APPLIED_MANIFEST)
    try:
        with open(path, "r") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_applied_manifest(root, manifest):
    path = os.path.join(root, APPLIED_MANIFEST)
    folder = os.path.dirname(path)
    try:
        if folder and not os.path.isdir(folder):
            os.makedirs(folder)
        with open(path, "w") as handle:
            json.dump(manifest, handle)
        return True
    except Exception:
        return False


def plan_update(root, manifest, applied=None):
    """What an update would actually do, without doing any of it.

    Returns a dict with `download`, `delete`, `unchanged` and `bytes`, so
    the user can be told "12 files, 84 KB" before anything is written -
    and so the same plan can be shown and then executed rather than
    computed twice from different rules."""
    if applied is None:
        applied = load_applied_manifest(root)

    remote_files = manifest.get("files", {})
    download, unchanged, total = [], [], 0

    for rel_path, info in remote_files.items():
        if is_protected(rel_path):
            continue
        local = os.path.join(root, rel_path.replace("/", os.sep))
        if not os.path.exists(local):
            download.append(rel_path)
            total += int(info.get("size") or 0)
            continue
        if file_digest(local) == info.get("sha256"):
            unchanged.append(rel_path)
        else:
            download.append(rel_path)
            total += int(info.get("size") or 0)

    # Only files this mirror placed AND the new manifest has dropped.
    # Anything else in the folder belongs to somebody else.
    previous = (applied.get("files") or {}) if isinstance(applied, dict) else {}
    delete = [p for p in previous
              if p not in remote_files
              and not is_protected(p)
              and os.path.exists(os.path.join(root, p.replace("/", os.sep)))]

    return {
        "download": sorted(download),
        "delete": sorted(delete),
        "unchanged": sorted(unchanged),
        "bytes": total,
        "commit": manifest.get("commit"),
        "built": manifest.get("built"),
    }


def apply_update(root, supabase_url, bucket, manifest, plan=None,
                 on_progress=None, logger=None):
    """Downloads and writes the planned files. Returns a result dict.

    Every file is written to a temporary name and then moved into place,
    so a download that dies half way cannot leave a truncated .py behind
    for pyRevit to choke on. A file Revit currently has locked fails that
    move, and is reported rather than silently skipped."""
    if plan is None:
        plan = plan_update(root, manifest)

    written, failed, removed = [], [], []
    files = manifest.get("files", {})
    total = len(plan["download"])

    for index, rel_path in enumerate(plan["download"]):
        if on_progress is not None:
            try:
                on_progress(index, total, rel_path)
            except Exception:
                pass

        url = public_url(supabase_url, bucket, rel_path)
        data, detail = _get_bytes(url)
        if data is None:
            failed.append((rel_path, detail))
            continue

        expected = (files.get(rel_path) or {}).get("sha256")
        if expected:
            got = hashlib.sha256(data).hexdigest()
            if got != expected:
                # Corrupted in transit, or the mirror is mid-publish and
                # the manifest no longer matches the object. Either way
                # writing it would be worse than not.
                failed.append((rel_path, "checksum mismatch"))
                continue

        local = os.path.join(root, rel_path.replace("/", os.sep))
        folder = os.path.dirname(local)
        try:
            if folder and not os.path.isdir(folder):
                os.makedirs(folder)
            temp = local + ".dee_new"
            with open(temp, "wb") as handle:
                handle.write(data)
            if os.path.exists(local):
                os.remove(local)
            os.rename(temp, local)
            written.append(rel_path)
        except Exception as e:
            failed.append((rel_path, str(e)))
            try:
                if os.path.exists(local + ".dee_new"):
                    os.remove(local + ".dee_new")
            except Exception:
                pass

    for rel_path in plan["delete"]:
        local = os.path.join(root, rel_path.replace("/", os.sep))
        try:
            os.remove(local)
            removed.append(rel_path)
        except Exception as e:
            failed.append((rel_path, "could not remove: {0}".format(e)))

    if on_progress is not None:
        try:
            on_progress(total, total, "")
        except Exception:
            pass

    # Only record success if everything landed. A partial apply that
    # claimed completion would make the NEXT update think those files
    # are current and skip them forever.
    if not failed:
        save_applied_manifest(root, manifest)

    if logger is not None:
        try:
            logger.info("Mirror update applied", written=len(written),
                        removed=len(removed), failed=len(failed),
                        commit=manifest.get("commit"))
        except Exception:
            pass

    return {
        "written": written,
        "removed": removed,
        "failed": failed,
        "complete": not failed,
        "commit": manifest.get("commit"),
    }


def describe_plan(plan):
    """One human sentence for a confirm dialog."""
    if not plan["download"] and not plan["delete"]:
        return "Already up to date - nothing to download."
    parts = []
    if plan["download"]:
        parts.append("{0} file(s) to update ({1:.0f} KB)".format(
            len(plan["download"]), plan["bytes"] / 1024.0))
    if plan["delete"]:
        parts.append("{0} file(s) no longer part of the extension".format(
            len(plan["delete"])))
    text = ", ".join(parts)
    if plan.get("commit"):
        text += "\nMirror is at commit {0}".format(plan["commit"])
    return text

# --------------------------------------------------------------------
# Where the mirror is, and which route this PC installed by
# --------------------------------------------------------------------
# Not secret. A public bucket read needs no key at all, which is the
# whole point on a network that blocks github.com: one plain HTTPS GET.
# The SECRET key lives only in tools/.supabase_publish.json on the
# developer's machine and never ships.
DEFAULT_SUPABASE_URL = "https://gmuvmeolvkgqkmwvfbjj.supabase.co"
DEFAULT_BUCKET = "dee-extension"

# Optional override, for anyone self-hosting their own mirror.
MIRROR_CONFIG = os.path.join("lib", ".dee_mirror_config.json")


def extension_root(start=None):
    """The extension folder, found by walking up from this file."""
    here = os.path.dirname(os.path.abspath(start or __file__))
    return os.path.dirname(here)          # lib/ -> extension root


def load_mirror_config(root=None):
    """(supabase_url, bucket). Defaults unless a config file overrides."""
    if root is None:
        root = extension_root()
    url, bucket = DEFAULT_SUPABASE_URL, DEFAULT_BUCKET
    try:
        path = os.path.join(root, MIRROR_CONFIG)
        if os.path.exists(path):
            with open(path, "r") as handle:
                cfg = json.load(handle)
            url = cfg.get("url") or url
            bucket = cfg.get("bucket") or bucket
    except Exception:
        pass
    return url, bucket


def detect_install_source(root=None):
    """How this copy got here: SOURCE_GITHUB, SOURCE_SUPABASE, or None.

    A .git folder means pyRevit cloned it, so `pyrevit extensions update`
    can pull. An applied-manifest file means the mirror put it here, and
    there is no git to pull from. Both can be true on a developer's
    machine; git wins there because it is the real repository.

    None means neither - a hand-copied or OneDrive-synced folder, where
    the GitHub route will find nothing to update and the mirror route is
    the only one that can do anything."""
    if root is None:
        root = extension_root()
    if os.path.isdir(os.path.join(root, ".git")):
        return SOURCE_GITHUB
    if os.path.exists(os.path.join(root, APPLIED_MANIFEST)):
        return SOURCE_SUPABASE
    return None


def installed_commit(root=None):
    """The commit the mirror last delivered, if it was the mirror."""
    if root is None:
        root = extension_root()
    applied = load_applied_manifest(root)
    return applied.get("commit") if isinstance(applied, dict) else None
