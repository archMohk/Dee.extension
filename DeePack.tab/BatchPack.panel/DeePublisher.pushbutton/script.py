# -*- coding: utf-8 -*-
"""
Batch Published Tool
Browse ACC, pick Revit files, and publish them via the APS API —
exactly what the ACC Docs "Publish" button does, but in batch.

No Revit files are opened.  Two modes:
  • Normal Publish       — publishes with all linked models
  • Publish without Links — publishes the model file only
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

# ── shared cache (same file as DeeB.Opener) ───────────────────────────────────
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


# ── BFS scan (same as DeeB.Opener) ────────────────────────────────────────────
def scan_level(level, project_id, token, max_workers=8):
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


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    try:
        token = acc_auth.get_access_token()
    except Exception as e:
        forms.alert("Authentication failed:\n{0}".format(e))
        return

    # ── hub → project ─────────────────────────────────────────────────────────
    hubs = acc_api.list_hubs(token)
    if not hubs:
        forms.alert("No ACC/BIM360 hubs found.")
        return
    hub_lookup = {name: (hub_id, region) for hub_id, name, region in hubs}
    hub_name = forms.SelectFromList.show(
        sorted(hub_lookup.keys()), title="Select Hub")
    if not hub_name:
        return
    hub_id, _region = hub_lookup[hub_name]

    projects = acc_api.list_projects(hub_id, token)
    if not projects:
        forms.alert("No projects found in that hub.")
        return
    project_lookup = {name: proj_id for proj_id, name in projects}
    project_name = forms.SelectFromList.show(
        sorted(project_lookup.keys()), title="Select Project")
    if not project_name:
        return
    project_id = project_lookup[project_name]

    # ── load or build file list ───────────────────────────────────────────────
    cache_ts, cached_files = _load_cache(project_id)
    if cached_files is not None:
        age_str = datetime.datetime.fromtimestamp(cache_ts).strftime(
            "%Y-%m-%d %H:%M")
        choice = forms.CommandSwitchWindow.show(
            ["Use Cached List ({0} files, saved {1})".format(
                len(cached_files), age_str),
             "Rescan Project (may take several minutes)"],
            message="A cached file list exists for this project."
        )
        if not choice:
            return
        if "Use Cached" in choice:
            all_items = {str(k): str(v) for k, v in cached_files.items()}
        else:
            cached_files = None

    if cached_files is None:
        all_items = {}
        native_ids = set()

        def _add(iid, iname):
            key = iname
            if key in all_items and all_items[key] != iid:
                key = "{0}  [{1}]".format(iname, iid.split(":")[-1][:8])
            all_items[key] = iid

        with forms.ProgressBar(title="Scanning project for Revit files...",
                               cancellable=True, indeterminate=True) as pb:
            pb.title = "Step 1/2 – Searching native cloud models..."
            for iid, iname in acc_api.search_cloud_models(project_id, token):
                native_ids.add(iid)
                _add(iid, iname)

            pb.title = "Step 2/2 – BFS folder scan..."
            top_folders = acc_api.get_top_folders(hub_id, project_id, token)
            level = [fid for fid, _ in top_folders]
            done = 0
            while level:
                if pb.cancelled:
                    forms.alert("Scan cancelled.")
                    return
                next_level, found_items = scan_level(level, project_id, token)
                for iid, iname in found_items:
                    _add(iid, iname)
                done += len(level)
                level = next_level
                pb.title = "Step 2/2 – Scanning ({0} done, {1} found)...".format(
                    done, len(all_items))

        if not all_items:
            forms.alert("No Revit files found in that project.")
            return
        _save_cache(project_id, all_items,
                    set(all_items.values()) - native_ids)

    # ── select files ──────────────────────────────────────────────────────────
    selected_names = forms.SelectFromList.show(
        sorted(all_items.keys()),
        title="Batch Published Tool – Select files to publish  "
              "({0} available)".format(len(all_items)),
        multiselect=True,
        button_name="Publish Selected"
    )
    if not selected_names:
        return

    # ── pick publish mode ─────────────────────────────────────────────────────
    pub_mode = forms.CommandSwitchWindow.show(
        ["Normal Publish", "Publish without Links"],
        message="Select publish mode:"
    )
    if not pub_mode:
        return
    without_links = (pub_mode == "Publish without Links")

    # ── publish via APS API ───────────────────────────────────────────────────
    results = []
    total = len(selected_names)

    with forms.ProgressBar(
            title="Publishing 0 / {0}...".format(total),
            cancellable=True) as pb:
        pb.reset()
        for i, name in enumerate(selected_names):
            if pb.cancelled:
                results.append((None, "Cancelled", "after {0} of {1} files".format(i, total)))
                break
            pb.title = "Publishing {0} / {1}  —  {2}".format(
                i + 1, total, name)
            pb.update_progress(i + 1, total)
            item_id = all_items[name]
            ok, detail = acc_api.publish_item(
                project_id, item_id, without_links, token)
            if ok:
                results.append((True, name, "Published ({0})".format(detail)))
            else:
                results.append((False, name, "FAILED: {0}".format(detail)))

    html = '<h2 style="font-family:sans-serif;color:#ddd;">Batch Publish Results</h2>'
    for ok, name, detail in results:
        bg   = "#1b5e20" if ok else ("#37474f" if ok is None else "#b71c1c")
        icon = "&#10003;" if ok else ("&#9888;" if ok is None else "&#10007;")
        html += (
            '<div style="padding:8px 14px;margin:3px 0;background:{0};color:#fff;'
            'border-radius:4px;font-family:monospace;font-size:13px;">'
            '<b>{1}</b>&nbsp; {2} &mdash; {3}'
            '</div>'.format(bg, icon, name, detail)
        )
    output.print_html(html)


main()
