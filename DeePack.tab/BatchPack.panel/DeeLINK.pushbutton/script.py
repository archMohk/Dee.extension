# -*- coding: utf-8 -*-
"""
Batch Link Tool
Browse ACC, pick Revit files, and batch-link them into the active document
with a chosen positioning method.
"""
from pyrevit import forms, script
from Autodesk.Revit.DB import (
    RevitLinkType, RevitLinkOptions, RevitLinkInstance,
    ModelPathUtils, ImportPlacement, Transaction
)
import System
import threading
import json
import os
import base64
import time
import datetime

try:
    import Queue as queue
except ImportError:
    import queue

import acc_auth
import acc_api

output = script.get_output()
uiapp  = __revit__
doc    = uiapp.ActiveUIDocument.Document

# ── shared cache (same file as DeeOpener / DeePublisher) ─────────────────────
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


# ── cloud GUID helpers (mirrors DeeOpener) ────────────────────────────────────
def to_guid(aps_id):
    return System.Guid(aps_id.split(".")[-1])


def _lineage_to_guid_hexstr(item_id):
    encoded = item_id.split(":")[-1]
    padded  = encoded + "=" * (-len(encoded) % 4)
    raw = bytearray(base64.urlsafe_b64decode(padded))
    h = ''.join('{0:02x}'.format(b) for b in raw)
    return System.Guid('{0}-{1}-{2}-{3}-{4}'.format(
        h[0:8], h[8:12], h[12:16], h[16:20], h[20:32]))


def get_cloud_path_guids(browsing_project_id, item_id, token):
    PROJ_FIELDS  = ("projectGuid", "projectId", "project_guid")
    MODEL_FIELDS = ("modelId", "modelGuid", "c4rModelId", "model_id")

    def _extract(ext_data, label):
        mguid = None
        for f in MODEL_FIELDS:
            v = ext_data.get(f)
            if v and isinstance(v, str) and '-' in v:
                try:
                    mguid = System.Guid(v)
                    break
                except Exception:
                    pass
        if mguid is None:
            return None
        for f in PROJ_FIELDS:
            v = ext_data.get(f)
            if v and isinstance(v, str) and '-' in v:
                try:
                    return System.Guid(v), mguid, label + "+" + f
                except Exception:
                    pass
        return to_guid(browsing_project_id), mguid, label + "+browsing-proj"

    try:
        resp = acc_api._get(
            acc_api.BASE_URL + "/data/v1/projects/{0}/items/{1}".format(
                browsing_project_id, item_id), token)
        ext = (resp.get("data", {})
                   .get("attributes", {})
                   .get("extension", {})
                   .get("data", {}))
        result = _extract(ext, "item.ext")
        if result:
            return result
    except Exception:
        pass

    try:
        tip = acc_api._get(
            acc_api.BASE_URL + "/data/v1/projects/{0}/items/{1}/tip".format(
                browsing_project_id, item_id), token)
        tip_d   = tip.get("data", {})
        tip_ext = tip_d.get("attributes", {}).get("extension", {}).get("data", {})
        result  = _extract(tip_ext, "tip.ext")
        if result:
            return result
        tip_id = tip_d.get("id", "")
        if "vf." in tip_id:
            vf  = tip_id.split("vf.")[-1].split("?")[0]
            enc = vf + "=" * (-len(vf) % 4)
            raw = bytearray(base64.urlsafe_b64decode(enc))
            h   = ''.join('{0:02x}'.format(b) for b in raw)
            return (
                to_guid(browsing_project_id),
                System.Guid('{0}-{1}-{2}-{3}-{4}'.format(
                    h[0:8], h[8:12], h[12:16], h[16:20], h[20:32])),
                "vf-urn")
    except Exception:
        pass

    return to_guid(browsing_project_id), _lineage_to_guid_hexstr(item_id), "lineage"


# ── BFS scan ──────────────────────────────────────────────────────────────────
def scan_level(level, project_id, token, max_workers=8):
    next_level  = []
    found_items = []
    lock = threading.Lock()
    q    = queue.Queue()
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


# ── placement options ─────────────────────────────────────────────────────────
PLACEMENT_OPTIONS = [
    ("Auto - Center to Center",                         ImportPlacement.Centered),
    ("Auto - Internal Origin to Internal Origin",       ImportPlacement.Origin),
    ("Auto - By Shared Coordinates",                    ImportPlacement.Shared),
    ("Auto - Project Base Point to Project Base Point", ImportPlacement.Site),
]


def link_file(doc, name, cloud_path, placement):
    t = Transaction(doc, "Link: {0}".format(name))
    t.Start()
    try:
        options = RevitLinkOptions(False)
        result  = RevitLinkType.Create(doc, cloud_path, options)
        if result.ElementId.Value < 0:
            t.RollBack()
            return False, "link type could not be created"
        RevitLinkInstance.Create(doc, result.ElementId, placement)
        t.Commit()
        return True, "linked"
    except Exception as e:
        if t.HasStarted() and not t.HasEnded():
            t.RollBack()
        return False, str(e)


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    if doc is None or doc.IsFamilyDocument:
        forms.alert("Please open a project document first.")
        return

    try:
        token = acc_auth.get_access_token()
    except Exception as e:
        forms.alert("Authentication failed:\n{0}".format(e))
        return

    # hub → project
    hubs = acc_api.list_hubs(token)
    if not hubs:
        forms.alert("No ACC/BIM360 hubs found.")
        return
    hub_lookup = {name: (hub_id, region) for hub_id, name, region in hubs}
    hub_name   = forms.SelectFromList.show(sorted(hub_lookup.keys()), title="Select Hub")
    if not hub_name:
        return
    hub_id, region = hub_lookup[hub_name]

    projects = acc_api.list_projects(hub_id, token)
    if not projects:
        forms.alert("No projects found in that hub.")
        return
    project_lookup = {name: pid for pid, name in projects}
    project_name   = forms.SelectFromList.show(
        sorted(project_lookup.keys()), title="Select Project")
    if not project_name:
        return
    project_id = project_lookup[project_name]

    # cache or scan
    cache_ts, cached_files = _load_cache(project_id)
    if cached_files is not None:
        age_str = datetime.datetime.fromtimestamp(cache_ts).strftime("%Y-%m-%d %H:%M")
        choice  = forms.CommandSwitchWindow.show(
            ["Use Cached List ({0} files, saved {1})".format(len(cached_files), age_str),
             "Rescan Project (may take several minutes)"],
            message="A cached file list exists for this project.")
        if not choice:
            return
        if "Use Cached" in choice:
            all_items = {str(k): str(v) for k, v in cached_files.items()}
        else:
            cached_files = None

    if cached_files is None:
        all_items  = {}
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
            done  = 0
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
        _save_cache(project_id, all_items, set(all_items.values()) - native_ids)

    # select files
    selected_names = forms.SelectFromList.show(
        sorted(all_items.keys()),
        title="Batch Link – Select files to link  ({0} available)".format(len(all_items)),
        multiselect=True,
        button_name="Link Selected")
    if not selected_names:
        return

    # pick placement
    chosen_label = forms.CommandSwitchWindow.show(
        [p[0] for p in PLACEMENT_OPTIONS],
        message="Select link positioning method:")
    if not chosen_label:
        return
    placement = dict(PLACEMENT_OPTIONS)[chosen_label]

    # link with progress
    results = []
    total   = len(selected_names)

    with forms.ProgressBar(
            title="Linking 0 / {0}...".format(total),
            cancellable=True) as pb:
        pb.reset()
        for i, name in enumerate(selected_names):
            if pb.cancelled:
                results.append((None, "Cancelled", "after {0} of {1} files".format(i, total)))
                break
            pb.title = "Linking {0} / {1}  —  {2}".format(i + 1, total, name)
            pb.update_progress(i + 1, total)
            item_id = all_items[name]
            try:
                proj_guid, model_guid, guid_src = get_cloud_path_guids(
                    project_id, item_id, token)
                cloud_path = ModelPathUtils.ConvertCloudGUIDsToCloudPath(
                    region, proj_guid, model_guid)
                ok, detail = link_file(doc, name, cloud_path, placement)
                if ok:
                    results.append((True, name, "Linked ({0})".format(chosen_label)))
                else:
                    results.append((False, name, "FAILED: {0}".format(detail)))
            except Exception as e:
                results.append((False, name, "FAILED: {0}".format(str(e))))

    html = '<h2 style="font-family:sans-serif;color:#ddd;">Batch Link Results</h2>'
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
