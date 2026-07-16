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


def _scan_level(level, project_id, token, max_workers=8):
    next_level = []
    found_items = []
    lock = threading.Lock()
    q = queue.Queue()
    for fid in level:
        q.put(fid)

    def worker():
        while True:
            try:
                fid = q.get_nowait()
            except queue.Empty:
                return
            try:
                for attempt in range(3):
                    try:
                        subfolders, items = acc_api.list_folder_contents(
                            project_id, fid, token)
                        with lock:
                            for sfid, _n in subfolders:
                                next_level.append(sfid)
                            for iid, iname in items:
                                found_items.append((iid, iname))
                        break
                    except Exception:
                        if attempt < 2:
                            time.sleep(2)
            finally:
                q.task_done()

    workers = []
    for _ in range(min(max_workers, max(1, len(level)))):
        t = threading.Thread(target=worker)
        t.daemon = True
        t.start()
        workers.append(t)
    for t in workers:
        t.join()
    return next_level, found_items


def _get_project_files(hub_id, project_id, project_name, token, pb=None):
    """Returns {display_name: item_id} for one project, using cache when
    the user opts in, otherwise doing a fresh BFS scan."""
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
            return None
        if "Use Cached" in choice:
            return {str(k): str(v) for k, v in cached_files.items()}

    all_items = {}
    native_ids = set()

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

    top_folders = acc_api.get_top_folders(hub_id, project_id, token)
    level = [fid for fid, _ in top_folders]
    done = 0
    while level:
        if pb and pb.cancelled:
            return None
        next_level, found_items = _scan_level(level, project_id, token)
        for iid, iname in found_items:
            _add(iid, iname)
        done += len(level)
        level = next_level
        if pb:
            pb.title = "{0} — Step 2/2 — Scanning ({1} done, {2} found)...".format(
                project_name, done, len(all_items))

    if all_items:
        _save_cache(project_id, all_items, set(all_items.values()) - native_ids)
    return all_items


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

    with forms.ProgressBar(title="Scanning projects...",
                           cancellable=True, indeterminate=True) as pb:
        for project_name in project_names:
            project_id = project_lookup[project_name]
            files = _get_project_files(hub_id, project_id, project_name, token, pb)
            if files is None:
                forms.alert("Scan cancelled.")
                return
            for name, item_id in files.items():
                queue_items.append((project_id, project_name, name, item_id))
            per_project_counts.append((project_name, len(files)))

    if not queue_items:
        forms.alert("No Revit files found in the selected project(s).")
        return

    # ── confirm: publish all ──────────────────────────────────────────────────
    breakdown = "\n".join(
        "    {0} — {1} file(s)".format(name, count)
        for name, count in per_project_counts
    )
    total = len(queue_items)
    confirm = forms.alert(
        "Found {0} Revit file(s) across {1} project(s):\n\n{2}\n\n"
        "This will publish ALL of them. Continue?".format(
            total, len(project_names), breakdown),
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

    with forms.ProgressBar(
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
