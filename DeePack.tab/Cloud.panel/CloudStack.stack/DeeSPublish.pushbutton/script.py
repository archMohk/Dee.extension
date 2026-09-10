# -*- coding: utf-8 -*-
"""
DeeS.Publish
Publish ALL Revit files found in one or more selected ACC/BIM360
projects — no individual file selection required.

Flow:
  Hub  →  Project(s) [multi-select]  →  (cache or fresh scan, per project)
       →  confirm breakdown  →  publish mode  →  publish all  →  coloured results
"""
from pyrevit import forms, script
import json
import os
import time
import datetime
import threading

try:
    import Queue as queue
except ImportError:
    import queue

import acc_auth
import acc_api

output = script.get_output()

# shared cache with DeeOpener / DeePublisher
_CACHE_FILE = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "DeeOpener.pushbutton", ".acc_file_cache.json"
))


def _load_cache(project_id):
    try:
        with open(_CACHE_FILE, "r") as fh:
            cache = json.load(fh)
        entry = cache.get(project_id)
        if entry and isinstance(entry.get("files"), dict):
            return entry["ts"], entry["files"]
    except Exception:
        pass
    return None, None


def _save_cache(project_id, files, published_ids=None):
    try:
        try:
            with open(_CACHE_FILE, "r") as fh:
                cache = json.load(fh)
        except Exception:
            cache = {}
        cache[project_id] = {
            "ts": time.time(),
            "files": files,
            "published_ids": list(published_ids or []),
        }
        with open(_CACHE_FILE, "w") as fh:
            json.dump(cache, fh)
    except Exception:
        pass


# APS's Data Management API documents a 429 "Too Many Requests" response
# (with a Retry-After header) when a project's rate limit is exceeded,
# and Autodesk's own guidance for it is exponential backoff - confirmed
# via aps.autodesk.com/en/docs/data/v2/developers_guide/rate-limiting,
# not guessed. 8 concurrent scan workers hitting the same project is
# exactly the kind of burst that can trip it. acc_api._get() raises a
# plain formatted string rather than a typed exception carrying the
# actual Retry-After value, so this reads the .NET HttpStatusCode's own
# string form out of the message _get() already embeds (`ToString()` on
# HttpStatusCode.TooManyRequests is the word "TooManyRequests", not the
# number "429" - both are checked) and waits longer for that specific
# case than for an ordinary transient failure.
_SCAN_MAX_ATTEMPTS = 5

# Live crash 2026-09-10: a permission-restricted subfolder (routine in a
# large ACC project - whole areas are commonly locked to certain roles)
# or a stale/rejected token fails EVERY attempt, no matter how many are
# made or how long we wait between them. Retrying those as if they were
# transient just replaced the old bug's silent-and-fast folder drop with
# a slow one: 5 attempts x up to 30s backoff, times every worker, times
# every restricted folder, easily adds up to many minutes of the main
# Revit thread sitting blocked in `t.join()` below - long enough that it
# reads as "Revit crashed" even though nothing actually raised. HTTP 401/
# 403 (`HttpStatusCode.ToString()` gives the words "Unauthorized"/
# "Forbidden", not the numbers - same gotcha as the 429 check below) can
# never succeed on retry, so those fail after one attempt instead of
# five.
_PERMANENT_ERROR_MARKERS = ("Unauthorized", "Forbidden", "401", "403")


def _is_permanent_error(error_text):
    return any(marker in error_text for marker in _PERMANENT_ERROR_MARKERS)


def _retry_delay_seconds(attempt, error_text):
    if "TooManyRequests" in error_text or "429" in error_text:
        return min(30, 4 * (2 ** attempt))   # 4s, 8s, 16s, 30s (capped)
    return 2 * (attempt + 1)                 # 2s, 4s, 6s, 8s


# Live crash 2026-09-10, second incident, right after the fix above:
# "its Crash the Revit when i Start scaning" with no Windows Event Log
# entry for it at all (checked directly - the only recent unhandled-.NET-
# exception log entries are from unrelated plugins, Enscape and an
# Autodesk SxS issue, neither touching this code). No WER entry means
# Revit's OWN internal handler caught something and self-terminated
# BEFORE it became a normal logged unhandled exception - consistent with
# a known IronPython/DLR fragility around sustained real-OS-thread churn.
# The old design (`_scan_level`, walked one BFS level at a time) spawned
# a FRESH batch of up to 8 real `System.Threading.Thread`s - each
# executing interpreted IronPython bytecode - and joined/discarded them
# for EVERY SINGLE level; a deep/large project can have dozens of levels,
# so a slow scan (exactly what "took too long" describes, and exactly
# what yesterday's retry/backoff fix could make more likely for a
# genuinely rate-limited project) meant dozens of thread create/destroy
# cycles sustained over several minutes. That is precisely the pattern
# IronPython's runtime is fragile around. This is a structural risk
# reduction, not a confirmed single root cause - there was no stack trace
# to point at - but it removes a real, known-risky pattern regardless.
_SCAN_TIME_BUDGET_SECONDS = 480   # 8 min/project hard ceiling - a scan
                                  # can no longer run indefinitely no
                                  # matter how many folders keep failing


def _scan_all_folders(top_folders, project_id, token, pb, project_name, max_workers=4):
    """Walks the WHOLE folder tree with ONE small pool of worker threads
    created ONCE (not re-created per BFS level) - a worker that discovers
    subfolders pushes them back onto its OWN shared queue instead of
    handing them to the caller to spin up a new batch of threads for.
    Returns (found_items, failed_folders, was_cancelled).
    `top_folders`/queue entries are (folder_id, folder_name) tuples -
    names are carried through so a folder that fails every retry can be
    reported to the user BY NAME, not as an opaque id nobody can act on."""
    q = queue.Queue()
    for entry in top_folders:
        q.put(entry)
    lock = threading.Lock()
    found_items = []
    failed_folders = []
    start_time = time.time()
    cancelled = [False]

    def worker():
        while True:
            try:
                fid, fname = q.get(timeout=0.5)
            except queue.Empty:
                if q.unfinished_tasks == 0:
                    return
                continue
            try:
                if cancelled[0] or (time.time() - start_time) > _SCAN_TIME_BUDGET_SECONDS:
                    with lock:
                        failed_folders.append((fname, "not scanned - cancelled or scan time budget exceeded"))
                    continue
                last_err = None
                ok = False
                for attempt in range(_SCAN_MAX_ATTEMPTS):
                    try:
                        subfolders, items = acc_api.list_folder_contents(
                            project_id, fid, token)
                        with lock:
                            for iid, iname in items:
                                found_items.append((iid, iname))
                        for sfid, sname in subfolders:
                            q.put((sfid, sname))
                        ok = True
                        break
                    except Exception as e:
                        last_err = str(e)
                        if _is_permanent_error(last_err):
                            break
                        if attempt < _SCAN_MAX_ATTEMPTS - 1:
                            time.sleep(_retry_delay_seconds(attempt, last_err))
                if not ok:
                    with lock:
                        failed_folders.append((fname, last_err))
            except Exception as thread_exc:
                # An exception escaping worker() would be UNHANDLED on a
                # real .NET thread - by default that terminates the
                # WHOLE PROCESS (Revit itself), not just this call. Pure
                # insurance so a bug here can never do that.
                with lock:
                    failed_folders.append((fname, str(thread_exc)))
            finally:
                q.task_done()

    workers = []
    for _ in range(max_workers):
        t = threading.Thread(target=worker)
        t.daemon = True
        t.start()
        workers.append(t)

    while any(t.is_alive() for t in workers):
        if pb and pb.cancelled:
            cancelled[0] = True
        if pb:
            with lock:
                pb.title = "{0} — Step 2/2 — Scanning ({1} found)...".format(
                    project_name, len(found_items))
        for t in workers:
            t.join(0.5)

    return found_items, failed_folders, cancelled[0]


def _get_project_files(hub_id, project_id, project_name, token, pb=None):
    """Returns (all_items, failed_folders) for one project - all_items is
    {display_name: item_id}; failed_folders is [(folder_name, error), ...],
    non-empty only when a fresh scan happened AND at least one folder
    failed every one of its retry attempts (previously dropped silently -
    see _scan_all_folders' docstring). Returns (None, []) if the user
    cancels. Using the cache always returns an empty failed_folders
    list - the cache's own save timestamp, already shown in the choice
    dialog, is the honesty check for that path."""
    cache_ts, cached_files = _load_cache(project_id)
    if cached_files is not None:
        age_str = datetime.datetime.fromtimestamp(cache_ts).strftime("%Y-%m-%d %H:%M")
        choice = forms.CommandSwitchWindow.show(
            ["Use Cached List ({0} files, saved {1})".format(
                len(cached_files), age_str),
             "Rescan Project (may take several minutes)"],
            message="A cached file list exists for '{0}'.".format(project_name)
        )
        if not choice:
            return None, []
        if "Use Cached" in choice:
            return {str(k): str(v) for k, v in cached_files.items()}, []

    all_items = {}
    native_ids = set()
    all_failed = []

    def _add(iid, iname):
        key = iname
        if key in all_items and all_items[key] != iid:
            key = "{0}  [{1}]".format(iname, iid.split(":")[-1][:8])
        all_items[key] = iid

    if pb:
        pb.title = "{0} — Step 1/2 — Searching native cloud models...".format(project_name)
    for iid, iname in acc_api.search_cloud_models(project_id, token):
        native_ids.add(iid)
        _add(iid, iname)

    if pb and pb.cancelled:
        return None, []
    top_folders = list(acc_api.get_top_folders(hub_id, project_id, token))
    if pb:
        pb.title = "{0} — Step 2/2 — Scanning (0 found)...".format(project_name)
    found_items, failed_folders, was_cancelled = _scan_all_folders(
        top_folders, project_id, token, pb, project_name)
    if was_cancelled:
        return None, []
    for iid, iname in found_items:
        _add(iid, iname)
    all_failed.extend(failed_folders)

    if all_items:
        _save_cache(project_id, all_items, set(all_items.values()) - native_ids)
    return all_items, all_failed


class _SafeProgress(object):
    """forms.ProgressBar tries to set Window.TaskbarItemInfo on its host
    window - a genuine WPF-level bug (not this extension's code):
    Window.TaskbarItemInfo throws NotImplementedException whenever the
    underlying ITaskbarList::HrInit COM call fails, which is documented
    to happen specifically under Remote Desktop/Terminal Services or a
    custom shell without a taskbar (live-confirmed in DeeSheetLinks).

    Wraps the real forms.ProgressBar and falls back to running with NO
    progress UI at all if entering it fails, so the tool degrades
    gracefully under RDP instead of crashing - everyone else still gets
    the real progress bar exactly as before. `pb.update_progress(...)`/
    `pb.cancelled`/`pb.reset()` are safe no-ops in the fallback case, so
    callers never need an extra branch. (Live crash fixed 2026-09-10:
    the fallback originally had no `reset()` at all, so this file's own
    `pb.reset()` call - present before the fallback was ever added -
    raised AttributeError whenever forms.ProgressBar failed to construct,
    i.e. every run in this environment. DeePublisher and DeeLINK had the
    identical gap, fixed alongside this one.)"""
    def __init__(self, **kwargs):
        self._kwargs = kwargs
        self._real = None

    def __enter__(self):
        try:
            self._real = forms.ProgressBar(**self._kwargs)
            return self._real.__enter__()
        except Exception:
            self._real = None
            return self

    def __exit__(self, exc_type, exc_value, tb):
        if self._real is not None:
            return self._real.__exit__(exc_type, exc_value, tb)
        return False

    @property
    def cancelled(self):
        return False

    def update_progress(self, i, total):
        pass

    def reset(self):
        pass


def main():
    # ── auth ──────────────────────────────────────────────────────────────────
    try:
        token = acc_auth.get_access_token()
    except Exception as e:
        forms.alert("Authentication failed:\n{0}".format(e))
        return

    # ── hub ───────────────────────────────────────────────────────────────────
    hubs = acc_api.list_hubs(token)
    if not hubs:
        forms.alert("No ACC/BIM360 hubs found.")
        return
    hub_lookup = {name: (hub_id, region) for hub_id, name, region in hubs}
    hub_name = forms.SelectFromList.show(
        sorted(hub_lookup.keys()), title="DeeS.Publish — Select Hub")
    if not hub_name:
        return
    hub_id, _region = hub_lookup[hub_name]

    # ── project(s) — multi-select ────────────────────────────────────────────
    projects = acc_api.list_projects(hub_id, token)
    if not projects:
        forms.alert("No projects found in that hub.")
        return
    project_lookup = {name: proj_id for proj_id, name in projects}
    project_names = forms.SelectFromList.show(
        sorted(project_lookup.keys()),
        title="DeeS.Publish — Select Project(s)",
        multiselect=True,
        button_name="Use Selected Projects"
    )
    if not project_names:
        return

    # ── gather files per project ─────────────────────────────────────────────
    # queue_items: list of (project_id, project_name, display_name, item_id)
    queue_items = []
    per_project_counts = []
    all_failed_folders = []   # [(project_name, folder_name, error), ...]

    with _SafeProgress(title="Scanning projects...",
                           cancellable=True, indeterminate=True) as pb:
        for project_name in project_names:
            project_id = project_lookup[project_name]
            files, failed_folders = _get_project_files(hub_id, project_id, project_name, token, pb)
            if files is None:
                forms.alert("Scan cancelled.")
                return
            for name, item_id in files.items():
                queue_items.append((project_id, project_name, name, item_id))
            per_project_counts.append((project_name, len(files)))
            for folder_name, err in failed_folders:
                all_failed_folders.append((project_name, folder_name, err))

    if not queue_items:
        forms.alert("No Revit files found in the selected project(s).")
        return

    # ── confirm: publish all ──────────────────────────────────────────────────
    breakdown = "\n".join(
        "    {0} — {1} file(s)".format(name, count)
        for name, count in per_project_counts
    )
    total = len(queue_items)
    warning = ""
    if all_failed_folders:
        # This is the direct answer to "it dosnt grap all the revit
        # files": these folders (and anything nested under them) could
        # not be listed after 3 attempts each, so the count above is
        # KNOWN to be incomplete, not just possibly so - previously this
        # was silently dropped with nothing shown anywhere.
        folder_lines = "\n".join(
            "    [{0}] {1} — {2}".format(proj, folder, err)
            for proj, folder, err in all_failed_folders[:12]
        )
        more = ""
        if len(all_failed_folders) > 12:
            more = "\n    ...and {0} more.".format(len(all_failed_folders) - 12)
        warning = (
            "\n\n⚠ {0} folder(s) could NOT be scanned after 3 attempts each - "
            "the count above is missing whatever they (and anything nested under "
            "them) contain:\n{1}{2}".format(len(all_failed_folders), folder_lines, more))
    confirm = forms.alert(
        "Found {0} Revit file(s) across {1} project(s):\n\n{2}{3}\n\n"
        "This will publish ALL of them. Continue?".format(
            total, len(project_names), breakdown, warning),
        title="DeeS.Publish — Confirm",
        options=["Publish All", "Cancel"]
    )
    if confirm != "Publish All":
        return

    # ── publish mode ──────────────────────────────────────────────────────────
    pub_mode = forms.CommandSwitchWindow.show(
        ["Normal Publish", "Publish without Links"],
        message="Select publish mode:"
    )
    if not pub_mode:
        return
    without_links = (pub_mode == "Publish without Links")

    # ── publish all ───────────────────────────────────────────────────────────
    queue_items.sort(key=lambda t: (t[1], t[2]))  # by project, then file name
    results = []

    with _SafeProgress(
            title="Publishing 0 / {0}...".format(total),
            cancellable=True) as pb:
        pb.reset()
        for i, (project_id, project_name, name, item_id) in enumerate(queue_items):
            if pb.cancelled:
                results.append((None, project_name, "Cancelled",
                                "after {0} of {1} files".format(i, total)))
                break
            pb.title = "Publishing {0} / {1}  —  [{2}] {3}".format(
                i + 1, total, project_name, name)
            pb.update_progress(i + 1, total)
            ok, detail = acc_api.publish_item(
                project_id, item_id, without_links, token)
            results.append((ok, project_name, name,
                            "Published ({0})".format(detail) if ok
                            else "FAILED: {0}".format(detail)))

    # ── coloured HTML results ──────────────────────────────────────────────────
    html = ('<h2 style="font-family:sans-serif;color:#ddd;">'
            'DeeS.Publish — {0} project(s)</h2>').format(len(project_names))
    for ok, project_name, name, detail in results:
        bg   = "#1b5e20" if ok else ("#37474f" if ok is None else "#b71c1c")
        icon = "&#10003;" if ok else ("&#9888;" if ok is None else "&#10007;")
        html += (
            '<div style="padding:8px 14px;margin:3px 0;background:{0};color:#fff;'
            'border-radius:4px;font-family:monospace;font-size:13px;">'
            '<b>{1}</b>&nbsp; [{2}] {3} &mdash; {4}'
            '</div>'.format(bg, icon, project_name, name, detail)
        )
    ok_count = sum(1 for r in results if r[0])
    html += ('<hr><b style="font-family:sans-serif;">'
             '{0} / {1} published successfully.</b>').format(ok_count, len(results))
    output.print_html(html)


main()
