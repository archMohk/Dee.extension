# -*- coding: utf-8 -*-
"""
DeeCoord
For each selected cloud Revit file:
  1. Open it (all worksets open)
  2. Create/reuse 3D view "BIM Coordination View" - RVT links hidden
  3. Create/reuse 3D view "BIM Exported NAVIS"    - RVT links hidden
  4. Configure cloud Publish Settings: set "BIM Coordination Set" with
     "BIM Coordination View" active (via UI Automation - no public API
     exists for this dialog)
  5. Synchronize With Central
  6. Close the file
  7. Publish via the APS Commands API (skipped if step 4 failed)
"""
from pyrevit import forms, script
from Autodesk.Revit.DB import (
    OpenOptions, WorksetConfigurationOption, WorksetConfiguration,
    ModelPathUtils, FilteredElementCollector, ViewFamilyType, ViewFamily,
    View3D, BuiltInCategory, ElementId,
    Transaction, TransactionStatus,
    TransactWithCentralOptions, SynchronizeWithCentralOptions,
    RelinquishOptions, IFailuresPreprocessor, FailureProcessingResult,
    FailureSeverity
)
from Autodesk.Revit.UI import RevitCommandId, PostableCommand, TaskDialogResult
import System
import threading
try:
    import Queue as queue
except ImportError:
    import queue
import json
import os
import base64
import time
import datetime

import acc_auth
import acc_api
import publish_settings_ui

output = script.get_output()

# Defaults - editable per run via the name prompts in main(). The view TYPE
# name (Project Browser grouping) must differ from the view instance names -
# Revit does not allow a View Type and a View to share the exact same name.
DEFAULT_VIEW_TYPE = "BIM Coordination"
DEFAULT_COORD_VIEW = "BIM Coordination View"
DEFAULT_NAVIS_VIEW = "BIM Exported NAVIS"
DEFAULT_COORD_SET = "BIM Coordination Set"

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


def to_guid(aps_id):
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
    """Same cross-project-safe GUID resolution used by DeeOpener."""
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


# ── Revit-side helpers ────────────────────────────────────────────────────

def _find_3d_view(doc, name):
    for v in FilteredElementCollector(doc).OfClass(View3D):
        if not v.IsTemplate and v.Name == name:
            return v
    return None


def _as_element_id(value):
    """Defensive coercion - View3D.CreateIsometric threw "expected
    ElementId, got ViewFamilyType" even though every return path here
    extracts .Id explicitly, which points at IronPython's dynamic method
    binder resolving .Duplicate() to an unexpected overload in this
    environment. Accepts either an ElementId or an Element/ElementType
    object and always returns a plain ElementId, so the exact cause
    doesn't matter."""
    if isinstance(value, ElementId):
        return value
    if hasattr(value, "Id"):
        return value.Id
    return value


def _hide_links_category(doc, view):
    cat = doc.Settings.Categories.get_Item(BuiltInCategory.OST_RvtLinks)
    if cat is not None and view.CanCategoryBeHidden(cat.Id):
        view.SetCategoryHidden(cat.Id, True)


def _ensure_3d_view_type(doc, type_name):
    """Returns (type_id, actual_name). .ViewFamily has never been observed
    to fail on any ViewFamilyType in this file, but .Name has - on some
    entries, and even on Revit's own "{3D}" default view in one test file.
    So this scans every 3D ViewFamilyType using ONLY .ViewFamily to filter
    (safe), and treats each one's .Name read as optional/best-effort: if a
    type is already correctly named, great; if a type's name can't be
    read, it's simply skipped as a name-match candidate but still kept as
    a fallback to duplicate from."""
    fallback_id = None
    for vft in FilteredElementCollector(doc).OfClass(ViewFamilyType):
        try:
            if vft.ViewFamily != ViewFamily.ThreeDimensional:
                continue
        except Exception:
            continue
        if fallback_id is None:
            fallback_id = _as_element_id(vft.Id)
        try:
            if vft.Name == type_name:
                return _as_element_id(vft.Id), type_name
        except Exception:
            continue

    if fallback_id is None:
        raise Exception("No usable 3D ViewFamilyType found in this project")

    base_type = doc.GetElement(fallback_id)
    last_err = None
    for i in range(1, 21):
        candidate = type_name if i == 1 else "{0} {1}".format(type_name, i)
        try:
            new_type_id = _as_element_id(base_type.Duplicate(candidate))
            return new_type_id, candidate
        except Exception as e:
            last_err = e
    raise Exception(
        "[duplicate type to '{0}'] it and 19 numbered variants are all "
        "already taken (original error: {1})".format(type_name, last_err))


def _ensure_3d_view(doc, name, type_id):
    """Returns (view, actual_name_used). Reuses an existing view with this
    exact name as-is. Otherwise creates a fresh 3D view directly - the
    earlier "duplicate an existing view" detour turned out unnecessary:
    the real problem was always reading .Name during a full scan, not
    CreateIsometric itself, and it also hit Revit's prohibited-characters
    rule when trying to name a helper view "{3D}"."""
    view = _find_3d_view(doc, name)
    if view is not None:
        return view, name

    try:
        view = View3D.CreateIsometric(doc, _as_element_id(type_id))
    except Exception as e:
        raise Exception("[CreateIsometric] {0}".format(e))

    last_err = None
    for i in range(1, 21):
        candidate = name if i == 1 else "{0} {1}".format(name, i)
        try:
            view.Name = candidate
            return view, candidate
        except Exception as e:
            last_err = e
    raise Exception(
        "[rename to '{0}'] it and 19 numbered variants are all already "
        "taken (original error: {1})".format(name, last_err))


# "Error - cannot be ignored" (element checked out by another user) is a
# Revit Failure Processing dialog raised during transaction commit, not a
# DialogBoxShowing-style TaskDialog - it has to be intercepted here, at the
# transaction level, instead.
class _CancelOnErrorPreprocessor(IFailuresPreprocessor):
    def PreprocessFailures(self, failures_accessor):
        messages = failures_accessor.GetFailureMessages()
        has_unresolvable_error = False
        for f in messages:
            if f.GetSeverity() == FailureSeverity.Error:
                has_unresolvable_error = True
            else:
                try:
                    failures_accessor.DeleteWarning(f)
                except Exception:
                    pass
        if has_unresolvable_error:
            return FailureProcessingResult.ProceedWithRollBack
        return FailureProcessingResult.Continue


def _sync_with_central(doc):
    relinquish = RelinquishOptions(True)
    swc_options = SynchronizeWithCentralOptions()
    swc_options.SetRelinquishOptions(relinquish)
    swc_options.Comment = "Auto-sync by DeeCoord"
    swc_options.SaveLocalBefore = True
    transact_options = TransactWithCentralOptions()
    try:
        transact_options.SetFailuresPreprocessor(_CancelOnErrorPreprocessor())
    except Exception:
        pass  # not supported on this API surface - falls back to old behavior
    doc.SynchronizeWithCentral(transact_options, swc_options)


# ── auto-handled dialogs ──────────────────────────────────────────────────
# Revit fires DialogBoxShowing for every modal dialog it's about to display,
# which lets us auto-answer known ones instead of the batch hanging on a
# popup no one is watching. Only dialogs matched below are touched -
# anything unrecognized is left alone and will still block/pause the run,
# same as before.
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
                # Another user owns the element (worksharing checkout) - there
                # is no safe auto-fix, just unblock the hang so the calling
                # transaction fails normally and gets reported as a step
                # failure instead of freezing the whole batch.
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


def _open_file(uiapp, region, project_id, name, item_id, token, close_worksets):
    """Returns (doc, step). doc is None on failure."""
    try:
        proj_guid, model_guid, _src = get_cloud_path_guids(project_id, item_id, token)
        cloud_path = ModelPathUtils.ConvertCloudGUIDsToCloudPath(region, proj_guid, model_guid)
        wc_option = (WorksetConfigurationOption.CloseAllWorksets if close_worksets
                     else WorksetConfigurationOption.OpenAllWorksets)
        open_options = OpenOptions()
        open_options.SetOpenWorksetsConfiguration(WorksetConfiguration(wc_option))
        uidoc = uiapp.OpenAndActivateDocument(cloud_path, open_options, False)
        return uidoc.Document, ("Opened", True)
    except Exception as e:
        return None, ("Open FAILED: {0}".format(e), False)


def _create_views(doc, coord_view_name, navis_view_name, view_type_name):
    t = Transaction(doc, "DeeCoord - Create Coordination Views")
    fho = t.GetFailureHandlingOptions()
    fho.SetFailuresPreprocessor(_CancelOnErrorPreprocessor())
    t.SetFailureHandlingOptions(fho)
    t.Start()
    try:
        try:
            type_id, actual_type = _ensure_3d_view_type(doc, view_type_name)
        except Exception as e:
            raise Exception("[find/create view type] {0}".format(e))

        try:
            coord_view, actual_coord = _ensure_3d_view(doc, coord_view_name, type_id)
        except Exception as e:
            raise Exception("[coord view] {0}".format(e))

        try:
            _hide_links_category(doc, coord_view)
        except Exception as e:
            raise Exception("[hide links on coord view] {0}".format(e))

        try:
            navis_view, actual_navis = _ensure_3d_view(doc, navis_view_name, type_id)
        except Exception as e:
            raise Exception("[navis view] {0}".format(e))

        try:
            _hide_links_category(doc, navis_view)
        except Exception as e:
            raise Exception("[hide links on navis view] {0}".format(e))

        status = t.Commit()
        if status == TransactionStatus.RolledBack:
            return ("Views FAILED: element locked by another user "
                    "(auto-cancelled, no dialog shown)", False, None)

        notes = []
        if actual_type != view_type_name:
            notes.append("type '{0}'->'{1}'".format(view_type_name, actual_type))
        if actual_coord != coord_view_name:
            notes.append("coord view '{0}'->'{1}'".format(coord_view_name, actual_coord))
        if actual_navis != navis_view_name:
            notes.append("navis view '{0}'->'{1}'".format(navis_view_name, actual_navis))
        note = " [{0}]".format("; ".join(notes)) if notes else ""
        return ("Views created/updated (type: '{0}'){1}".format(actual_type, note), True, actual_coord)
    except Exception as e:
        if t.GetStatus() == TransactionStatus.Started:
            t.RollBack()
        return ("Views FAILED: {0}".format(e), False, None)


def _configure_publish_settings(uiapp, set_name, coord_view_name):
    try:
        publish_settings_ui.configure_publish_settings(uiapp, set_name, coord_view_name)
        return ("Publish Settings configured", True)
    except Exception as e:
        return ("Publish Settings FAILED: {0} (configure manually)".format(e), None)


def _sync(doc):
    try:
        _sync_with_central(doc)
        return ("Synchronized", True)
    except Exception as e:
        return ("Sync FAILED: {0}".format(e), False)


def _publish(project_id, item_id, token, without_links):
    ok, detail = acc_api.publish_item(project_id, item_id, without_links, token)
    label = "Published ({0})".format(detail) if ok else "Publish FAILED: {0}".format(detail)
    return (label, ok)


SCRIPT_VERSION = "2026-07-03-v11-elementid-coercion"


def main():
    output.print_md("*DeeCoord script version: {0}*".format(SCRIPT_VERSION))
    try:
        token = acc_auth.get_access_token()
    except Exception as e:
        forms.alert("Authentication failed:\n{0}".format(e))
        return

    hubs = acc_api.list_hubs(token)
    if not hubs:
        forms.alert("No ACC/BIM360 hubs found.")
        return
    hub_lookup = {name: (hub_id, region) for hub_id, name, region in hubs}
    hub_name = forms.SelectFromList.show(
        sorted(hub_lookup.keys()), title="DeeCoord — Select Hub")
    if not hub_name:
        return
    hub_id, region = hub_lookup[hub_name]

    projects = acc_api.list_projects(hub_id, token)
    if not projects:
        forms.alert("No projects found in that hub.")
        return
    project_lookup = {name: proj_id for proj_id, name in projects}
    project_name = forms.SelectFromList.show(
        sorted(project_lookup.keys()), title="DeeCoord — Select Project")
    if not project_name:
        return
    project_id = project_lookup[project_name]

    # ── load or build file list ───────────────────────────────────────────────
    cache_ts, cached_files = _load_cache(project_id)
    all_items = None
    if cached_files is not None:
        age_str = datetime.datetime.fromtimestamp(cache_ts).strftime("%Y-%m-%d %H:%M")
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

    if all_items is None:
        all_items = {}
        native_ids = set()

        def _add(iid, iname):
            key = iname
            if key in all_items and all_items[key] != iid:
                key = "{0}  [{1}]".format(iname, iid.split(":")[-1][:8])
            all_items[key] = iid

        with forms.ProgressBar(title="Scanning project for Revit files...",
                               cancellable=True, indeterminate=True) as pb:
            pb.title = "Step 1/2 — Searching native cloud models..."
            for iid, iname in acc_api.search_cloud_models(project_id, token):
                native_ids.add(iid)
                _add(iid, iname)

            pb.title = "Step 2/2 — BFS folder scan..."
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
                pb.title = "Step 2/2 — Scanning ({0} done, {1} found)...".format(
                    done, len(all_items))

        if not all_items:
            forms.alert("No Revit files found in that project.")
            return
        _save_cache(project_id, all_items, set(all_items.values()) - native_ids)

    # ── select files ──────────────────────────────────────────────────────────
    selected_names = forms.SelectFromList.show(
        sorted(all_items.keys()),
        title="DeeCoord — Select files to process ({0} available)".format(len(all_items)),
        multiselect=True,
        button_name="Process Selected"
    )
    if not selected_names:
        return

    # ── open mode ─────────────────────────────────────────────────────────────
    open_mode = forms.CommandSwitchWindow.show(
        ["Regular Open", "Open with All Worksets Closed"],
        message="How should each selected file be opened?"
    )
    if not open_mode:
        return
    close_worksets = (open_mode == "Open with All Worksets Closed")

    # ── editable names (current defaults pre-filled) ─────────────────────────
    view_type_name = forms.ask_for_string(
        default=DEFAULT_VIEW_TYPE,
        prompt="3D View Type name (the group both views appear under in "
               "the Project Browser, e.g. '3D Views ({0})'). Created by "
               "duplicating an existing 3D View Type if it doesn't already "
               "exist:".format(DEFAULT_VIEW_TYPE),
        title="DeeCoord — 3D View Type Name"
    )
    if not view_type_name:
        return

    coord_view_name = forms.ask_for_string(
        default=DEFAULT_COORD_VIEW,
        prompt="3D view name for the coordination view (no links):",
        title="DeeCoord — Coordination View Name"
    )
    if not coord_view_name:
        return

    navis_view_name = forms.ask_for_string(
        default=DEFAULT_NAVIS_VIEW,
        prompt="3D view name for the NAVIS export view (no links):",
        title="DeeCoord — NAVIS View Name"
    )
    if not navis_view_name:
        return

    coord_set_name = forms.ask_for_string(
        default=DEFAULT_COORD_SET,
        prompt="Publish Settings set name (its active view will be '{0}'):".format(
            coord_view_name),
        title="DeeCoord — Publish Set Name"
    )
    if not coord_set_name:
        return

    # ── per-step on/off toggles ──────────────────────────────────────────────
    step_choices = forms.SelectFromList.show(
        [
            forms.TemplateListItem("Create/Update Views", checked=True),
            forms.TemplateListItem("Configure Publish Settings", checked=True),
            forms.TemplateListItem("Synchronize With Central", checked=True),
            forms.TemplateListItem("Close After Sync", checked=True),
            forms.TemplateListItem("Publish", checked=True),
        ],
        title="DeeCoord — Steps to run ({0} file(s))".format(len(selected_names)),
        multiselect=True,
        button_name="Continue"
    )
    if not step_choices:
        return
    do_views = "Create/Update Views" in step_choices
    do_pubset = "Configure Publish Settings" in step_choices
    do_sync = "Synchronize With Central" in step_choices
    do_close = "Close After Sync" in step_choices
    do_publish = "Publish" in step_choices

    without_links = False
    if do_publish:
        pub_mode = forms.CommandSwitchWindow.show(
            ["Normal Publish", "Publish without Links"],
            message="Select publish mode for successfully-configured files:"
        )
        if not pub_mode:
            return
        without_links = (pub_mode == "Publish without Links")

    uiapp = __revit__
    all_results = []
    total = len(selected_names)
    prev_doc = None
    prev_steps = None

    dismissed_log = []
    dialog_handler = _make_dialog_handler(dismissed_log)
    uiapp.DialogBoxShowing += dialog_handler

    try:
        with forms.ProgressBar(title="DeeCoord — starting...", cancellable=True) as pb:
            for i, name in enumerate(selected_names):
                if pb.cancelled:
                    all_results.append((name, [("Cancelled", None)]))
                    break

                steps = []
                all_results.append((name, steps))
                item_id = all_items[name]
                pb.update_progress(i, total)

                pb.title = "File {0}/{1}: {2} — Opening".format(i + 1, total, name)
                output.print_md("**File {0}/{1}: {2} — Opening**".format(i + 1, total, name))
                before_count = len(dismissed_log)
                doc, step = _open_file(uiapp, region, project_id, name, item_id, token, close_worksets)
                steps.append(step)
                steps.extend(dismissed_log[before_count:])

                # The document that was active before this one can only be
                # closed now that it is no longer the active document (Revit
                # forbids Document.Close() on the active document).
                if prev_doc is not None:
                    if do_close:
                        pb.title = "File {0}/{1}: closing previous file".format(i + 1, total)
                        try:
                            prev_doc.Close(False)
                            prev_steps.append(("Closed", True))
                        except Exception as e:
                            prev_steps.append(("Close FAILED: {0}".format(e), False))
                    else:
                        prev_steps.append(("Left open (Close After Sync unchecked)", None))
                    prev_doc = None

                if doc is None:
                    continue

                publish_settings_ok = True
                actual_coord_view_name = coord_view_name
                if do_views:
                    pb.title = "File {0}/{1}: {2} — Creating views".format(i + 1, total, name)
                    output.print_md("Creating views...")
                    before_count = len(dismissed_log)
                    label, ok, created_coord_name = _create_views(
                        doc, coord_view_name, navis_view_name, view_type_name)
                    steps.append((label, ok))
                    steps.extend(dismissed_log[before_count:])
                    if created_coord_name:
                        actual_coord_view_name = created_coord_name

                if do_pubset:
                    pb.title = "File {0}/{1}: {2} — Publish Settings".format(i + 1, total, name)
                    output.print_md("Configuring Publish Settings...")
                    label, ok = _configure_publish_settings(
                        uiapp, coord_set_name, actual_coord_view_name)
                    steps.append((label, ok))
                    if ok is None:
                        publish_settings_ok = False
                else:
                    steps.append(("Publish Settings skipped (unchecked)", None))

                if do_sync:
                    pb.title = "File {0}/{1}: {2} — Synchronizing".format(i + 1, total, name)
                    output.print_md("Synchronizing with central...")
                    before_count = len(dismissed_log)
                    steps.append(_sync(doc))
                    steps.extend(dismissed_log[before_count:])

                if do_publish:
                    if publish_settings_ok:
                        pb.title = "File {0}/{1}: {2} — Publishing".format(i + 1, total, name)
                        output.print_md("Publishing...")
                        steps.append(_publish(project_id, item_id, token, without_links))
                    else:
                        steps.append(("Publish skipped (Publish Settings not confirmed)", None))

                prev_doc = doc
                prev_steps = steps

            # The very last file has no "next" document to activate, so it
            # can't be closed synchronously - post the Close command instead,
            # which Revit runs right after this script finishes.
            if prev_doc is not None:
                if do_close:
                    pb.title = "Closing last file..."
                    try:
                        cmd_id = RevitCommandId.LookupPostableCommandId(PostableCommand.Close)
                        if uiapp.CanPostCommand(cmd_id):
                            uiapp.PostCommand(cmd_id)
                            prev_steps.append(("Close requested (completes after this tool exits)", None))
                        else:
                            prev_steps.append(("Close FAILED: command not postable right now", False))
                    except Exception as e:
                        prev_steps.append(("Close FAILED: {0}".format(e), False))
                else:
                    prev_steps.append(("Left open (Close After Sync unchecked)", None))
    finally:
        uiapp.DialogBoxShowing -= dialog_handler

    # ── coloured HTML results ──────────────────────────────────────────────────
    html = '<h2 style="font-family:sans-serif;color:#ddd;">DeeCoord Results</h2>'
    for name, steps in all_results:
        oks = [s[1] for s in steps]
        if any(o is False for o in oks):
            file_bg = "#b71c1c"
        elif any(o is None for o in oks):
            file_bg = "#37474f"
        else:
            file_bg = "#1b5e20"

        html += ('<div style="margin:10px 0 2px 0;padding:6px 12px;background:{0};'
                 'color:#fff;border-radius:4px;font-family:sans-serif;'
                 'font-weight:bold;">{1}</div>').format(file_bg, name)
        for label, ok in steps:
            bg = "#2e7d32" if ok else ("#455a64" if ok is None else "#c62828")
            icon = "&#10003;" if ok else ("&#9888;" if ok is None else "&#10007;")
            html += (
                '<div style="padding:5px 14px 5px 24px;margin:2px 0;background:{0};'
                'color:#fff;border-radius:3px;font-family:monospace;font-size:12px;">'
                '{1}&nbsp; {2}'
                '</div>'.format(bg, icon, label)
            )
    output.print_html(html)


main()
