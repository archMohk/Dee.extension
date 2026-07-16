# -*- coding: utf-8 -*-
from pyrevit import forms, script
from Autodesk.Revit.DB import OpenOptions, WorksetConfigurationOption, WorksetConfiguration, ModelPathUtils
from Autodesk.Revit.UI import TaskDialogResult
import System
import threading
try:
    import Queue as queue  # IronPython 2.7 / Python 2
except ImportError:
    import queue
import json
import os
import base64
import time
import datetime

import acc_auth
import acc_api

output = script.get_output()

# ── file-list cache ───────────────────────────────────────────────────────────
# Stores {name: item_id} per project so the expensive BFS scan only runs once.
# Cache has no automatic expiry – the user picks "Rescan" in the project dialog
# when they need fresh data.  Format on disk:
#   { "<project_id>": { "ts": <unix-time>, "files": {<name>: <item_id>} } }
_CACHE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".acc_file_cache.json"
)


def _load_cache(project_id):
    try:
        with open(_CACHE_FILE, "r") as fh:
            cache = json.load(fh)
        entry = cache.get(project_id)
        if entry and isinstance(entry.get("files"), dict):
            return entry["ts"], entry["files"], set(entry.get("published_ids", []))
    except Exception:
        pass
    return None, None, set()


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


def to_guid(aps_id):
    # hub/project ids look like "b.xxxxxxxx-xxxx-..." or "a.xxxxxxxx-..." - strip the prefix
    return System.Guid(aps_id.split(".")[-1])


def _lineage_to_guid_hexstr(item_id):
    encoded = item_id.split(":")[-1]
    padded = encoded + "=" * (-len(encoded) % 4)
    raw = bytearray(base64.urlsafe_b64decode(padded))
    h = ''.join('{0:02x}'.format(b) for b in raw)
    return System.Guid('{0}-{1}-{2}-{3}-{4}'.format(
        h[0:8], h[8:12], h[12:16], h[16:20], h[20:32]
    ))


def get_cloud_path_guids(browsing_project_id, item_id, token):
    """
    Returns (project_guid, model_guid, source_label) for ConvertCloudGUIDsToCloudPath.

    Files in ACC can be cross-project published references: they appear in the
    folder structure of the project you browse, but the underlying C4R model
    actually lives in a DIFFERENT source project.  Revit always uses the SOURCE
    project GUID - not the browsing project GUID - when building the cloud path.

    The source project GUID and the C4R model GUID are stored in the item's
    (or tip version's) extension.data by Autodesk.  We try to read them here
    and fall back to lineage-URN decoding only when they are absent.
    """
    PROJ_FIELDS  = ("projectGuid", "projectId", "project_guid")
    MODEL_FIELDS = ("modelId", "modelGuid", "c4rModelId", "model_id")

    def _extract(ext_data, label):
        """Return (project_guid, model_guid, label) if both are found, else None."""
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
        # Try to get a project GUID from the same extension block
        for f in PROJ_FIELDS:
            v = ext_data.get(f)
            if v and isinstance(v, str) and '-' in v:
                try:
                    return System.Guid(v), mguid, label + "+" + f
                except Exception:
                    pass
        # Model GUID found but no project GUID → fall back to browsing project
        return to_guid(browsing_project_id), mguid, label + "+browsing-proj"

    # 1. item extension.data
    try:
        resp = acc_api._get(
            acc_api.BASE_URL + "/data/v1/projects/{0}/items/{1}".format(
                browsing_project_id, item_id),
            token
        )
        ext = (resp.get("data", {})
                   .get("attributes", {})
                   .get("extension", {})
                   .get("data", {}))
        result = _extract(ext, "item.ext")
        if result:
            return result
    except Exception:
        pass

    # 2. tip version extension.data, then version-URN decode
    try:
        tip  = acc_api._get(
            acc_api.BASE_URL + "/data/v1/projects/{0}/items/{1}/tip".format(
                browsing_project_id, item_id),
            token
        )
        tip_d   = tip.get("data", {})
        tip_ext = tip_d.get("attributes", {}).get("extension", {}).get("data", {})
        result  = _extract(tip_ext, "tip.ext")
        if result:
            return result

        # Last resort before full fallback: decode the version-file URN
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
                "vf-urn"
            )
    except Exception:
        pass

    # 3. Pure fallback: browsing project + lineage decode
    return to_guid(browsing_project_id), _lineage_to_guid_hexstr(item_id), "lineage"


def scan_level(level, project_id, token, max_workers=8):
    """Scans every folder in `level` concurrently, returns (next_level, items).
    Each folder gets up to 3 attempts so transient API failures don't silently
    drop entire folders from the results."""
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
                        subfolders, items = acc_api.list_folder_contents(project_id, fid, token)
                        with lock:
                            for sfid, _sname in subfolders:
                                next_level.append(sfid)
                            for iid, iname in items:
                                found_items.append((iid, iname))
                        break  # success
                    except Exception:
                        if attempt < 2:
                            time.sleep(2)  # wait before retry
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


# ── auto-handled dialogs ──────────────────────────────────────────────────
# Revit fires DialogBoxShowing for every modal dialog it's about to display,
# which lets us auto-answer known ones instead of the batch hanging on a
# popup no one is watching.
def _make_dialog_handler(log_list):
    def handler(sender, args):
        try:
            dialog_id = (args.DialogId or "") if hasattr(args, "DialogId") else ""
            message = ""
            try:
                message = args.Message or ""
            except Exception:
                pass
            text = (dialog_id + " " + message).lower()

            if "unresolved" in text or "could not find or read" in text:
                if hasattr(args, "OverrideResult"):
                    args.OverrideResult(int(TaskDialogResult.CommandLink2))
                    log_list.append((
                        "Auto-dismissed 'Unresolved References' dialog "
                        "(Ignore and continue opening the project)", True))
                return

            if "cannot be ignored" in text or "resaves the element to central" in text:
                if hasattr(args, "OverrideResult"):
                    args.OverrideResult(int(TaskDialogResult.Cancel))
                    log_list.append((
                        "Auto-dismissed 'element owned by another user' error (Cancel)", None))
                return

            if "extents greater than" in text or "will be truncated" in text:
                # CAD-link geometry-extents warning ("Click OK to continue,
                # Cancel to exit import") - a plain OK/Cancel TaskDialog, not
                # a command-link one, so it needs TaskDialogResult.Ok instead.
                if hasattr(args, "OverrideResult"):
                    args.OverrideResult(int(TaskDialogResult.Ok))
                    log_list.append((
                        "Auto-dismissed 'geometry extents/truncated' CAD warning (OK)", True))
                return

            if "numerical data" in text and "truncated" in text:
                # Informational, single-button ("Close") variant of the same
                # CAD numeric-range warning - distinct wording ("has been
                # truncated") from the OK/Cancel one above so it needs its
                # own match and its own result value.
                if hasattr(args, "OverrideResult"):
                    args.OverrideResult(int(TaskDialogResult.Close))
                    log_list.append((
                        "Auto-dismissed 'numerical data truncated' CAD warning (Close)", True))
        except Exception:
            pass
    return handler


def main():
    try:
        token = acc_auth.get_access_token()
    except Exception as e:
        forms.alert("Authentication failed:\n{0}".format(e))
        return

    hubs = acc_api.list_hubs(token)
    if not hubs:
        forms.alert("No ACC/BIM360 hubs found for this account.")
        return
    hub_lookup = {name: (hub_id, region) for hub_id, name, region in hubs}
    hub_name = forms.SelectFromList.show(sorted(hub_lookup.keys()), title="Select Hub")
    if not hub_name:
        return
    hub_id, region = hub_lookup[hub_name]

    projects = acc_api.list_projects(hub_id, token)
    if not projects:
        forms.alert("No projects found in that hub.")
        return
    project_lookup = {name: proj_id for proj_id, name in projects}
    project_name = forms.SelectFromList.show(sorted(project_lookup.keys()), title="Select Project")
    if not project_name:
        return
    project_id = project_lookup[project_name]

    # ── check cache ───────────────────────────────────────────────────────────
    cache_ts, cached_files, _cached_pub_ids = _load_cache(project_id)
    if cached_files is not None:
        age_str = datetime.datetime.fromtimestamp(cache_ts).strftime("%Y-%m-%d %H:%M")
        choice = forms.CommandSwitchWindow.show(
            ["Use Cached List ({0} files, saved {1})".format(len(cached_files), age_str),
             "Rescan Project (may take several minutes)"],
            message="A cached file list exists for this project."
        )
        if not choice:
            return
        if "Use Cached" in choice:
            all_items = {str(k): str(v) for k, v in cached_files.items()}
        else:
            cached_files = None  # fall through to rescan

    if cached_files is None:
        all_items = {}
        native_ids = set()

        def _add(iid, iname):
            key = iname
            if key in all_items and all_items[key] != iid:
                key = "{0}  [{1}]".format(iname, iid.split(":")[-1][:8])
            all_items[key] = iid

        with forms.ProgressBar(title="Finding all cloud models...",
                               cancellable=True, indeterminate=True) as pb:

            # Step 1 – v2 search: native C4RModel items (belong to this project)
            pb.title = "Step 1/2 - Searching for native cloud models..."
            for iid, iname in acc_api.search_cloud_models(project_id, token):
                native_ids.add(iid)
                _add(iid, iname)

            # Step 2 – BFS folder scan: also finds cross-project published refs
            pb.title = "Step 2/2 - Scanning folders (0 done, {0} found)...".format(
                len(all_items))
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
                pb.title = "Step 2/2 - Scanning folders ({0} done, {1} found)...".format(
                    done, len(all_items))

        if not all_items:
            forms.alert("No Revit (.rvt) files found in that project.")
            return

        # published_ids = items found by BFS but NOT by native search
        published_ids = set(all_items.values()) - native_ids
        _save_cache(project_id, all_items, published_ids)

    if not all_items:
        forms.alert("No Revit (.rvt) files found in that project.")
        return

    selected_names = forms.SelectFromList.show(
        sorted(all_items.keys()),
        title="Select Files to Open",
        multiselect=True,
        button_name="Open Selected"
    )
    if not selected_names:
        return

    open_mode = forms.CommandSwitchWindow.show(
        ["Regular Open", "Open with All Worksets Closed"],
        message="How should the selected files be opened?"
    )
    if not open_mode:
        return
    close_worksets = open_mode == "Open with All Worksets Closed"

    uiapp = __revit__

    dismissed_log = []
    dialog_handler = _make_dialog_handler(dismissed_log)
    uiapp.DialogBoxShowing += dialog_handler

    results = []
    try:
        for name in selected_names:
            item_id = all_items[name]
            proj_guid, model_guid, guid_src = get_cloud_path_guids(project_id, item_id, token)
            before_count = len(dismissed_log)
            try:
                cloud_path = ModelPathUtils.ConvertCloudGUIDsToCloudPath(
                    region, proj_guid, model_guid
                )
                open_options = OpenOptions()
                wc_option = (WorksetConfigurationOption.CloseAllWorksets if close_worksets
                             else WorksetConfigurationOption.OpenAllWorksets)
                open_options.SetOpenWorksetsConfiguration(WorksetConfiguration(wc_option))
                uiapp.OpenAndActivateDocument(cloud_path, open_options, False)
                results.append((True, name, "Opened"))
            except Exception as e:
                results.append((False, name, str(e)))
            for msg, _sev in dismissed_log[before_count:]:
                results.append((True, name, msg))
    finally:
        uiapp.DialogBoxShowing -= dialog_handler

    html = '<h2 style="font-family:sans-serif;color:#ddd;">Batch Open Results</h2>'
    for ok, name, detail in results:
        bg   = "#1b5e20" if ok else "#b71c1c"
        icon = "&#10003;" if ok else "&#10007;"
        html += (
            '<div style="padding:8px 14px;margin:3px 0;background:{0};color:#fff;'
            'border-radius:4px;font-family:monospace;font-size:13px;">'
            '<b>{1}</b>&nbsp; {2} &mdash; {3}'
            '</div>'.format(bg, icon, name, detail)
        )
    output.print_html(html)


main()
