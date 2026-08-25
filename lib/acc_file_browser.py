# -*- coding: utf-8 -*-
"""
acc_file_browser
Shared ACC/BIM360 hub -> project -> cloud-file browsing/opening helpers,
extracted from DeeOpener.pushbutton/script.py (that tool is left completely
untouched - this module is a copy of its proven logic, factored out so a
second tool, DeeNWCs, can reuse the same hub/project/file picking and
cloud-model opening flow instead of re-deriving the fragile GUID-resolution
logic independently).

Depends on the existing shared lib/acc_auth.py and lib/acc_api.py modules
(same ones DeeOpener already uses).

Typical usage (mirrors DeeOpener's main()):
    token = acc_auth.get_access_token()
    hub_id, region, hub_name = pick_hub(token)
    project_id, project_name = pick_project(hub_id, token)
    all_items = list_project_files(hub_id, project_id, token, cache_file_path)
    selected_names = pick_files_to_open(all_items)
    for name in selected_names:
        ui_doc, detail = open_cloud_file(uiapp, region, project_id, all_items[name],
                                          token, close_worksets=False)
"""
import base64
import json
import os
import time
import datetime
import threading
try:
    import Queue as queue  # IronPython 2.7 / Python 2
except ImportError:
    import queue

import System
from pyrevit import forms
from Autodesk.Revit.DB import (
    OpenOptions, WorksetConfigurationOption, WorksetConfiguration, ModelPathUtils,
    DetachFromCentralOption,
)
from Autodesk.Revit.UI import TaskDialogResult

import acc_api


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
    actually lives in a DIFFERENT source project. Revit always uses the SOURCE
    project GUID - not the browsing project GUID - when building the cloud path.

    The source project GUID and the C4R model GUID are stored in the item's
    (or tip version's) extension.data by Autodesk. We try to read them here
    and fall back to lineage-URN decoding only when they are absent.
    """
    PROJ_FIELDS = ("projectGuid", "projectId", "project_guid")
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

    try:
        tip = acc_api._get(
            acc_api.BASE_URL + "/data/v1/projects/{0}/items/{1}/tip".format(
                browsing_project_id, item_id),
            token
        )
        tip_d = tip.get("data", {})
        tip_ext = tip_d.get("attributes", {}).get("extension", {}).get("data", {})
        result = _extract(tip_ext, "tip.ext")
        if result:
            return result

        tip_id = tip_d.get("id", "")
        if "vf." in tip_id:
            vf = tip_id.split("vf.")[-1].split("?")[0]
            enc = vf + "=" * (-len(vf) % 4)
            raw = bytearray(base64.urlsafe_b64decode(enc))
            h = ''.join('{0:02x}'.format(b) for b in raw)
            return (
                to_guid(browsing_project_id),
                System.Guid('{0}-{1}-{2}-{3}-{4}'.format(
                    h[0:8], h[8:12], h[12:16], h[16:20], h[20:32])),
                "vf-urn"
            )
    except Exception:
        pass

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


def _load_cache(cache_file, project_id):
    try:
        with open(cache_file, "r") as fh:
            cache = json.load(fh)
        entry = cache.get(project_id)
        if entry and isinstance(entry.get("files"), dict):
            return entry["ts"], entry["files"], set(entry.get("published_ids", []))
    except Exception:
        pass
    return None, None, set()


def _save_cache(cache_file, project_id, files, published_ids=None):
    try:
        try:
            with open(cache_file, "r") as fh:
                cache = json.load(fh)
        except Exception:
            cache = {}
        cache[project_id] = {
            "ts": time.time(),
            "files": files,
            "published_ids": list(published_ids or []),
        }
        with open(cache_file, "w") as fh:
            json.dump(cache, fh)
    except Exception:
        pass


def make_dialog_handler(log_list):
    """Revit fires DialogBoxShowing for every modal dialog it's about to
    display, which lets us auto-answer known ones instead of the batch
    hanging on a popup no one is watching."""
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
                if hasattr(args, "OverrideResult"):
                    args.OverrideResult(int(TaskDialogResult.Ok))
                    log_list.append((
                        "Auto-dismissed 'geometry extents/truncated' CAD warning (OK)", True))
                return

            if "numerical data" in text and "truncated" in text:
                if hasattr(args, "OverrideResult"):
                    args.OverrideResult(int(TaskDialogResult.Close))
                    log_list.append((
                        "Auto-dismissed 'numerical data truncated' CAD warning (Close)", True))
        except Exception:
            pass
    return handler


def pick_hub(token):
    """Returns (hub_id, region, hub_name), or None if the user cancelled.

    The hub list itself is never region-filtered - acc_api.list_hubs()
    calls the single global APS "/project/v1/hubs" endpoint, which
    returns every hub (US, EMEA, or otherwise) the SIGNED-IN Autodesk
    account has membership in, all in one call. If an expected region's
    hub is missing here, that's an Autodesk-account/admin-permissions
    matter (the signed-in account isn't a member of that hub), not a
    region restriction in this code - the region tag shown in each list
    entry below makes that directly checkable."""
    hubs = acc_api.list_hubs(token)
    if not hubs:
        forms.alert("No ACC/BIM360 hubs found for this account.")
        return None
    display_lookup = {}
    for hub_id, name, region in hubs:
        display = "{0}  [{1}]".format(name, region or "?")
        display_lookup[display] = (hub_id, region, name)
    picked = forms.SelectFromList.show(sorted(display_lookup.keys()), title="Select Hub")
    if not picked:
        return None
    return display_lookup[picked]


def pick_project(hub_id, token):
    """Returns (project_id, project_name), or None if the user cancelled."""
    projects = acc_api.list_projects(hub_id, token)
    if not projects:
        forms.alert("No projects found in that hub.")
        return None
    project_lookup = {name: proj_id for proj_id, name in projects}
    project_name = forms.SelectFromList.show(sorted(project_lookup.keys()), title="Select Project")
    if not project_name:
        return None
    return project_lookup[project_name], project_name


def pick_folder(hub_id, project_id, token):
    """Interactive folder-TREE drill-down picker - unlike pick_hub/
    pick_project (single flat list), this lets the user descend
    through nested subfolders one level at a time so a Cloud Model can
    be saved to its correct real path in the project (not only ever
    one of the project's top-level folders). At every level past the
    first, a "[Use This Folder]" option is offered so the user can stop
    at any depth rather than being forced to a leaf.

    Returns (folder_id, "breadcrumb / path / string"), or None if the
    user cancelled at any step. A folder with no subfolders is
    auto-finalized (returned immediately) since there's nothing further
    to drill into."""
    breadcrumb = []
    folder_id = None
    try:
        subfolders = acc_api.get_top_folders(hub_id, project_id, token)
    except Exception:
        subfolders = []
    if not subfolders:
        forms.alert("No folders found in that project.")
        return None

    while True:
        options = sorted(name for _fid, name in subfolders)
        if breadcrumb:
            options = ["[Use This Folder: {0}]".format(" / ".join(breadcrumb))] + options

        title = "Select ACC Folder"
        if breadcrumb:
            title = "Select ACC Folder  -  {0}".format(" / ".join(breadcrumb))
        picked = forms.SelectFromList.show(options, title=title, button_name="Open / Select")
        if not picked:
            return None

        if picked.startswith("[Use This Folder"):
            return folder_id, " / ".join(breadcrumb)

        picked_id = dict((name, fid) for fid, name in subfolders)[picked]
        breadcrumb.append(picked)
        folder_id = picked_id
        try:
            subfolders, _items = acc_api.list_folder_contents(project_id, folder_id, token)
        except Exception:
            subfolders = []
        if not subfolders:
            # Leaf folder - nothing further to drill into, so use it.
            return folder_id, " / ".join(breadcrumb)


def browse_and_pick_cloud_file(hub_id, project_id, token, cache_file):
    """Folder-by-folder navigation (reusing the exact same drill-down
    primitives as pick_folder() above - acc_api.get_top_folders/
    list_folder_contents) instead of list_project_files()'s full
    recursive whole-project BFS scan, which is slow/heavy on large
    projects and scans far more than the user usually needs. The user
    navigates into subfolders one level at a time; at ANY level they
    can pick "[Scan This Folder for Revit Files]" to explicitly list
    just that folder's own Revit files (optionally including its
    subfolders), rather than the tool scanning everything up front.

    Each folder's scan result is cached (reusing _load_cache/
    _save_cache, keyed by "project_id::folder_id[:r]" so a recursive
    and non-recursive scan of the same folder don't collide) -
    revisiting the same folder later offers "Use Cached" instead of
    re-hitting the API, mirroring list_project_files()'s own cache-or-
    rescan prompt.

    Returns (item_id, display_name), or None if the user cancelled at
    any point."""
    breadcrumb = []
    folder_id = None
    try:
        subfolders = acc_api.get_top_folders(hub_id, project_id, token)
    except Exception:
        subfolders = []
    if not subfolders:
        forms.alert("No folders found in that project.")
        return None

    scan_option = "[Scan This Folder for Revit Files]"
    while True:
        options = [scan_option] + sorted(name for _fid, name in subfolders)
        title = "Browse ACC Folders"
        if breadcrumb:
            title = "Browse ACC Folders  -  {0}".format(" / ".join(breadcrumb))
        picked = forms.SelectFromList.show(options, title=title, button_name="Open / Scan")
        if not picked:
            return None

        if picked == scan_option:
            choice = forms.CommandSwitchWindow.show(
                ["This Folder Only", "Include Subfolders"],
                message="Scan '{0}' for Revit files:".format(" / ".join(breadcrumb) or "(project root)")
            )
            if not choice:
                continue
            recursive = choice == "Include Subfolders"
            items = _scan_one_folder(project_id, folder_id, token, recursive, cache_file)
            if not items:
                forms.alert("No Revit (.rvt) files found in that folder{0}.".format(
                    " or its subfolders" if recursive else ""))
                continue
            picked_name = forms.SelectFromList.show(
                sorted(items.keys()), title="Select Revit File", button_name="Select")
            if not picked_name:
                continue
            return items[picked_name], picked_name

        picked_id = dict((name, fid) for fid, name in subfolders)[picked]
        breadcrumb.append(picked)
        folder_id = picked_id
        try:
            subfolders, _items = acc_api.list_folder_contents(project_id, folder_id, token)
        except Exception:
            subfolders = []


def _scan_one_folder(project_id, folder_id, token, recursive, cache_file):
    """Returns {display_name: item_id} for one folder (optionally its
    subfolders too), using a cached result if one exists for this exact
    folder+recursive-flag combination."""
    cache_key = "{0}::{1}{2}".format(project_id, folder_id, ":r" if recursive else "")
    cache_ts, cached_files, _pub = _load_cache(cache_file, cache_key)
    if cached_files is not None:
        age_str = datetime.datetime.fromtimestamp(cache_ts).strftime("%Y-%m-%d %H:%M")
        choice = forms.CommandSwitchWindow.show(
            ["Use Cached List ({0} file(s), saved {1})".format(len(cached_files), age_str),
             "Rescan This Folder"],
            message="A cached file list exists for this folder."
        )
        if not choice:
            return None
        if "Use Cached" in choice:
            return {str(k): str(v) for k, v in cached_files.items()}

    items = {}

    def _add(iid, iname):
        key = iname
        if key in items and items[key] != iid:
            key = "{0}  [{1}]".format(iname, iid.split(":")[-1][:8])
        items[key] = iid

    if recursive:
        level = [folder_id]
        while level:
            next_level, found_items = scan_level(level, project_id, token)
            for iid, iname in found_items:
                _add(iid, iname)
            level = next_level
    else:
        try:
            _subfolders, found_items = acc_api.list_folder_contents(project_id, folder_id, token)
        except Exception:
            found_items = []
        for iid, iname in found_items:
            _add(iid, iname)

    _save_cache(cache_file, cache_key, items)
    return items


def list_project_files(hub_id, project_id, token, cache_file):
    """Full 'use cache or rescan' flow (same prompts/behavior as DeeOpener).
    Returns a dict {display_name: item_id}, or None if the user cancelled or
    nothing was found."""
    cache_ts, cached_files, _cached_pub_ids = _load_cache(cache_file, project_id)
    all_items = None
    if cached_files is not None:
        age_str = datetime.datetime.fromtimestamp(cache_ts).strftime("%Y-%m-%d %H:%M")
        choice = forms.CommandSwitchWindow.show(
            ["Use Cached List ({0} files, saved {1})".format(len(cached_files), age_str),
             "Rescan Project (may take several minutes)"],
            message="A cached file list exists for this project."
        )
        if not choice:
            return None
        if "Use Cached" in choice:
            all_items = {str(k): str(v) for k, v in cached_files.items()}
        else:
            cached_files = None

    if all_items is None:
        all_items = {}
        native_ids = set()

        def _add(iid, iname):
            key = iname
            if key in all_items and all_items[key] != iid:
                key = "{0}  [{1}]".format(iname, iid.split(":")[-1][:8])
            all_items[key] = iid

        with forms.ProgressBar(title="Finding all cloud models...",
                                cancellable=True, indeterminate=True) as pb:
            pb.title = "Step 1/2 - Searching for native cloud models..."
            for iid, iname in acc_api.search_cloud_models(project_id, token):
                native_ids.add(iid)
                _add(iid, iname)

            pb.title = "Step 2/2 - Scanning folders (0 done, {0} found)...".format(len(all_items))
            top_folders = acc_api.get_top_folders(hub_id, project_id, token)
            level = [fid for fid, _ in top_folders]
            done = 0
            while level:
                if pb.cancelled:
                    forms.alert("Scan cancelled.")
                    return None
                next_level, found_items = scan_level(level, project_id, token)
                for iid, iname in found_items:
                    _add(iid, iname)
                done += len(level)
                level = next_level
                pb.title = "Step 2/2 - Scanning folders ({0} done, {1} found)...".format(
                    done, len(all_items))

        if not all_items:
            forms.alert("No Revit (.rvt) files found in that project.")
            return None

        published_ids = set(all_items.values()) - native_ids
        _save_cache(cache_file, project_id, all_items, published_ids)

    if not all_items:
        forms.alert("No Revit (.rvt) files found in that project.")
        return None
    return all_items


def pick_files_to_open(all_items, title="Select Files to Open", button_name="Open Selected"):
    """Thin wrapper over forms.SelectFromList - returns a list of selected
    display names, or None if the user cancelled."""
    return forms.SelectFromList.show(
        sorted(all_items.keys()),
        title=title,
        multiselect=True,
        button_name=button_name
    )


def open_cloud_document_detached(application, region, project_id, item_id, token,
                                  audit=False, discard_worksets=False):
    """Opens a cloud model HEADLESS and DETACHED via
    Application.OpenDocumentFile - deliberately NOT the
    UIApplication.OpenAndActivateDocument path used by open_cloud_file
    below.

    Two things make this the right call for any tool that wants to save
    a COPY of a cloud model (DeeW.Transmit):

    1. OpenAndActivateDocument cannot detach - the document stays bound
       to its ACC central. Document.SaveAs to a local path on a still-
       attached workshared/cloud central is rejected by Revit, which is
       exactly why DeeW.Transmit's first live run reported "Failed -
       could not save copy" after successfully opening and cleaning the
       model. Detaching first is what makes SaveAs legal.
    2. It activates each document into Revit's UI, swapping the visible
       tab per file and closing the active document between iterations.
       On that same live run every model after the first failed to open
       at all, one second apart - consistent with driving document
       activation repeatedly from inside a modal window. The headless
       OpenDocumentFile never touches the UI, so the batch does not
       depend on Revit's window/active-document state at all.

    Returns (document_or_None, detail_string). Never raises."""
    try:
        proj_guid, model_guid, _guid_src = get_cloud_path_guids(project_id, item_id, token)
    except Exception as e:
        return None, "Could not resolve cloud GUIDs: {0}".format(e)
    try:
        cloud_path = ModelPathUtils.ConvertCloudGUIDsToCloudPath(region, proj_guid, model_guid)
        open_options = OpenOptions()
        open_options.DetachFromCentralOption = (
            DetachFromCentralOption.DetachAndDiscardWorksets if discard_worksets
            else DetachFromCentralOption.DetachAndPreserveWorksets)
        open_options.Audit = bool(audit)
        try:
            open_options.SetOpenWorksetsConfiguration(
                WorksetConfiguration(WorksetConfigurationOption.OpenAllWorksets))
        except Exception:
            pass
        document = application.OpenDocumentFile(cloud_path, open_options)
        return document, "Opened (detached)"
    except Exception as e:
        return None, str(e)


def open_cloud_file(uiapp, region, project_id, item_id, token, close_worksets=False):
    """Resolves the cloud path for `item_id` and opens+activates it. Returns
    (ui_document_or_None, detail_string)."""
    proj_guid, model_guid, _guid_src = get_cloud_path_guids(project_id, item_id, token)
    try:
        cloud_path = ModelPathUtils.ConvertCloudGUIDsToCloudPath(region, proj_guid, model_guid)
        open_options = OpenOptions()
        wc_option = (WorksetConfigurationOption.CloseAllWorksets if close_worksets
                     else WorksetConfigurationOption.OpenAllWorksets)
        open_options.SetOpenWorksetsConfiguration(WorksetConfiguration(wc_option))
        ui_doc = uiapp.OpenAndActivateDocument(cloud_path, open_options, False)
        return ui_doc, "Opened"
    except Exception as e:
        return None, str(e)
