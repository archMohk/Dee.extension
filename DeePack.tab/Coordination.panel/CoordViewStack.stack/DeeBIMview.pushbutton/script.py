# -*- coding: utf-8 -*-
"""
DeeBIMview
Two ways to choose the models (asked first when the button is clicked):
  - ACC project: browse hub -> project, tick any number of cloud files,
    each is opened in turn and processed (the original flow, below).
  - Opened Revit session: tick any of the project documents ALREADY open
    in this Revit - nothing is opened or closed. Create/Update Views,
    Configure Publish Settings and Synchronize With Central are offered;
    Open, Close and Publish are not (the files are already open and stay
    open, and Publish needs each model's cloud item id, which an open
    document does not expose). Views and Sync are done through the API on
    each document directly; Configure Publish Settings drives Revit's own
    dialog, which only exists for the ACTIVE document, so each document is
    switched to first (best effort - NEEDS LIVE-REVIT VERIFICATION that
    OpenAndActivateDocument just activates an already-open model) and the
    document that was active at the start is restored at the end.

For each selected cloud Revit file, runs up to 5 independently-toggled
steps (only "Create/Update Views" is checked by default - everything
else is opt-in):
  1. Open it (all worksets open)
  2. Create/reuse 3D view "BIM Coordination View" - RVT links hidden
  3. Create/reuse 3D view "BIM Exported NAVIS"    - RVT links hidden
  4. Configure cloud Publish Settings: set "BIM Coordination Set" with
     "BIM Coordination View" active (via UI Automation - no public API
     exists for this dialog)
  5. Synchronize With Central
  6. Close the file
  7. Publish via the APS Commands API (skipped if step 4 failed)
With every optional step left unchecked, this behaves exactly like the
old view-only DeeBIMview: files are left open afterward so the views
can be reviewed before you decide to sync/publish yourself. Checking
the extra steps gives the old DeeCoord full-pipeline behavior - the two
tools were merged into this one, with the pipeline steps exposed as
options instead of being a separate always-on tool.
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
import dee_telemetry
dee_telemetry.check_access("DeeBIMview")


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


# 2026-09-10: a folder that failed all its retry attempts was silently
# dropped (and everything nested under it with it) - live-reported
# against DeeS.Publish's own copy of this exact logic as "it dosnt grap
# all the revit files", root-caused to APS's documented 429 rate limit
# (aps.autodesk.com/en/docs/data/v2/developers_guide/rate-limiting) under
# 8 concurrent scan workers. More attempts + exponential backoff
# specifically for a 429 response, matching the fix already made to
# DeeS.Publish/acc_file_browser.py/acc_api.py. The return shape here is
# left unchanged (still (next_level, found_items)) - only the retry
# robustness is improved, not the failure-reporting UI those two also
# got, so no caller-side change is needed.
_SCAN_MAX_ATTEMPTS = 5


def _retry_delay_seconds(attempt, error_text):
    if "TooManyRequests" in error_text or "429" in error_text:
        return min(30, 4 * (2 ** attempt))
    return 2 * (attempt + 1)


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
                for attempt in range(_SCAN_MAX_ATTEMPTS):
                    try:
                        subfolders, items = acc_api.list_folder_contents(project_id, fid, token)
                        with lock:
                            for sfid, _n in subfolders:
                                next_level.append(sfid)
                            for iid, iname in items:
                                found_items.append((iid, iname))
                        break
                    except Exception as e:
                        if attempt < _SCAN_MAX_ATTEMPTS - 1:
                            time.sleep(_retry_delay_seconds(attempt, str(e)))
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
    swc_options.Comment = "Auto-sync by DeeBIMview"
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

            if "docwarn" in text:
                # The warnings-review list Revit shows while OPENING a
                # model that already has warnings in it. Message text
                # varies per model, but the dialog id
                # (dialog_revit_docwarndialog) does not - matches
                # lib/deew_failure_handler.py's "docwarn" mapping.
                if hasattr(args, "OverrideResult"):
                    args.OverrideResult(int(TaskDialogResult.Ok))
                    log_list.append((
                        "Auto-dismissed open-time warnings review dialog (OK)", True))
                return

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
                return

            if "analytical" in text and ("upgrade" in text or "modified or lost" in text):
                # "Structural Analytical Model Upgrade" notice, shown while
                # OPENING a model with structural analytical elements.
                # Single-button ("Close") dialog.
                if hasattr(args, "OverrideResult"):
                    args.OverrideResult(int(TaskDialogResult.Close))
                    log_list.append((
                        "Auto-dismissed 'Structural Analytical Model Upgrade' notice (Close)", True))
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
    t = Transaction(doc, "DeeBIMview - Create Coordination Views")
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


SCRIPT_VERSION = "2026-10-22-v2-open-session"


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
    `pb.cancelled` are safe no-ops in the fallback case, so callers never
    need an extra branch."""
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


def _run_acc():
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
        sorted(hub_lookup.keys()), title="DeeBIMview — Select Hub")
    if not hub_name:
        return
    hub_id, region = hub_lookup[hub_name]

    projects = acc_api.list_projects(hub_id, token)
    if not projects:
        forms.alert("No projects found in that hub.")
        return
    project_lookup = {name: proj_id for proj_id, name in projects}
    project_name = forms.SelectFromList.show(
        sorted(project_lookup.keys()), title="DeeBIMview — Select Project")
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

        with _SafeProgress(title="Scanning project for Revit files...",
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
        title="DeeBIMview — Select files to process ({0} available)".format(len(all_items)),
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

    # ── per-step on/off toggles (only Create/Update Views is on by default -
    # this is the difference between the old DeeBIMview and DeeCoord, now
    # exposed as options on one tool instead of two separate buttons) ───────
    step_choices = forms.SelectFromList.show(
        [
            forms.TemplateListItem("Create/Update Views", checked=True),
            forms.TemplateListItem("Configure Publish Settings", checked=False),
            forms.TemplateListItem("Synchronize With Central", checked=False),
            forms.TemplateListItem("Close After Sync", checked=False),
            forms.TemplateListItem("Publish", checked=False),
        ],
        title="DeeBIMview — Steps to run ({0} file(s))".format(len(selected_names)),
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

    # ── editable names (current defaults pre-filled) ─────────────────────────
    view_type_name = forms.ask_for_string(
        default=DEFAULT_VIEW_TYPE,
        prompt="3D View Type name (the group both views appear under in "
               "the Project Browser, e.g. '3D Views ({0})'). Created by "
               "duplicating an existing 3D View Type if it doesn't already "
               "exist:".format(DEFAULT_VIEW_TYPE),
        title="DeeBIMview — 3D View Type Name"
    )
    if not view_type_name:
        return

    coord_view_name = forms.ask_for_string(
        default=DEFAULT_COORD_VIEW,
        prompt="3D view name for the coordination view (no links):",
        title="DeeBIMview — Coordination View Name"
    )
    if not coord_view_name:
        return

    navis_view_name = forms.ask_for_string(
        default=DEFAULT_NAVIS_VIEW,
        prompt="3D view name for the NAVIS export view (no links):",
        title="DeeBIMview — NAVIS View Name"
    )
    if not navis_view_name:
        return

    coord_set_name = DEFAULT_COORD_SET
    if do_pubset:
        coord_set_name = forms.ask_for_string(
            default=DEFAULT_COORD_SET,
            prompt="Publish Settings set name (its active view will be '{0}'):".format(
                coord_view_name),
            title="DeeBIMview — Publish Set Name"
        )
        if not coord_set_name:
            return

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
        with _SafeProgress(title="DeeBIMview — starting...", cancellable=True) as pb:
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

    _print_results(all_results)


# ══════════════════════════════════════════════════════════════════════════
# Opened Revit session mode
# ══════════════════════════════════════════════════════════════════════════
def _print_results(all_results):
    """Coloured HTML results, shared by the ACC and the open-session paths.
    The version banner only shows up alongside the real results, once the
    run has actually finished, not before."""
    html = ('<h2 style="font-family:sans-serif;color:#ddd;">DeeBIMview Results</h2>'
            '<p style="font-family:sans-serif;color:#888;font-size:11px;">Script version: {0}</p>').format(
        SCRIPT_VERSION)
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


def _same_document(a, b):
    """Two Document wrappers for the same open model - compared by title
    and path rather than object identity, which is not reliable across
    separately-obtained wrappers."""
    try:
        return a.Title == b.Title and a.PathName == b.PathName
    except Exception:
        return False


def _open_project_documents(uiapp):
    """Every document currently open in this Revit session that is a
    real project: linked-model copies and family documents are skipped."""
    docs = []
    for d in uiapp.Application.Documents:
        try:
            if d.IsLinked or d.IsFamilyDocument:
                continue
        except Exception:
            continue
        docs.append(d)
    return docs


def _document_label(doc, active_doc):
    try:
        if doc.IsModelInCloud:
            kind = "Cloud"
        elif doc.IsWorkshared:
            kind = "Workshared"
        else:
            kind = "Local"
    except Exception:
        kind = "?"
    label = "{0}  [{1}]".format(doc.Title, kind)
    if active_doc is not None and _same_document(doc, active_doc):
        label += "  (active)"
    return label


def _activate_document(uiapp, doc):
    """Best effort: make doc the active document. Needed only for the
    Publish Settings step, which drives Revit's own dialog for the ACTIVE
    document (no public API exists for it). Returns True if doc is active
    afterwards. NEEDS LIVE-REVIT VERIFICATION: relies on
    OpenAndActivateDocument simply activating a model that is already
    open rather than reopening it."""
    try:
        active = uiapp.ActiveUIDocument
        if active is not None and _same_document(active.Document, doc):
            return True
    except Exception:
        pass
    try:
        if doc.IsModelInCloud:
            path = doc.GetCloudModelPath()
        else:
            if not doc.PathName:
                return False
            path = ModelPathUtils.ConvertUserVisiblePathToModelPath(doc.PathName)
        uiapp.OpenAndActivateDocument(path, OpenOptions(), False)
    except Exception:
        return False
    try:
        return _same_document(uiapp.ActiveUIDocument.Document, doc)
    except Exception:
        return False


def _run_session(uiapp):
    docs = _open_project_documents(uiapp)
    if not docs:
        forms.alert("No project documents are open in this Revit session.")
        return
    try:
        active_doc = uiapp.ActiveUIDocument.Document
    except Exception:
        active_doc = None

    # ── pick the open documents ───────────────────────────────────────────────
    label_to_doc = {}
    items = []
    for d in docs:
        label = _document_label(d, active_doc)
        if label in label_to_doc:
            label = "{0} #{1}".format(label, len(label_to_doc) + 1)
        label_to_doc[label] = d
        items.append(forms.TemplateListItem(
            label, checked=(active_doc is not None and _same_document(d, active_doc))))
    picked = forms.SelectFromList.show(
        items,
        title="DeeBIMview — Open documents to process ({0} open)".format(len(docs)),
        multiselect=True,
        button_name="Process Selected"
    )
    if not picked:
        return
    targets = [(label, label_to_doc[label]) for label in picked if label in label_to_doc]
    if not targets:
        return

    # ── steps - Open, Close and Publish are not offered here: the files are
    # already open and are left open, and Publish needs the cloud item id
    # of each model, which an open document does not expose. ───────────────
    step_choices = forms.SelectFromList.show(
        [
            forms.TemplateListItem("Create/Update Views", checked=True),
            forms.TemplateListItem("Configure Publish Settings", checked=False),
            forms.TemplateListItem("Synchronize With Central", checked=False),
        ],
        title="DeeBIMview — Steps to run ({0} open document(s))".format(len(targets)),
        multiselect=True,
        button_name="Continue"
    )
    if not step_choices:
        return
    do_views = "Create/Update Views" in step_choices
    do_pubset = "Configure Publish Settings" in step_choices
    do_sync = "Synchronize With Central" in step_choices

    view_type_name = forms.ask_for_string(
        default=DEFAULT_VIEW_TYPE,
        prompt="3D View Type name (the group both views appear under in "
               "the Project Browser, e.g. '3D Views ({0})'). Created by "
               "duplicating an existing 3D View Type if it doesn't already "
               "exist:".format(DEFAULT_VIEW_TYPE),
        title="DeeBIMview — 3D View Type Name"
    )
    if not view_type_name:
        return
    coord_view_name = forms.ask_for_string(
        default=DEFAULT_COORD_VIEW,
        prompt="3D view name for the coordination view (no links):",
        title="DeeBIMview — Coordination View Name"
    )
    if not coord_view_name:
        return
    navis_view_name = forms.ask_for_string(
        default=DEFAULT_NAVIS_VIEW,
        prompt="3D view name for the NAVIS export view (no links):",
        title="DeeBIMview — NAVIS View Name"
    )
    if not navis_view_name:
        return
    coord_set_name = DEFAULT_COORD_SET
    if do_pubset:
        coord_set_name = forms.ask_for_string(
            default=DEFAULT_COORD_SET,
            prompt="Publish Settings set name (its active view will be '{0}'):".format(coord_view_name),
            title="DeeBIMview — Publish Set Name"
        )
        if not coord_set_name:
            return

    all_results = []
    total = len(targets)
    dismissed_log = []
    dialog_handler = _make_dialog_handler(dismissed_log)
    uiapp.DialogBoxShowing += dialog_handler
    try:
        with _SafeProgress(title="DeeBIMview — starting...", cancellable=True) as pb:
            for i, (label, doc) in enumerate(targets):
                if pb.cancelled:
                    all_results.append((label, [("Cancelled", None)]))
                    break
                steps = []
                all_results.append((label, steps))
                pb.update_progress(i, total)
                output.print_md("**Document {0}/{1}: {2}**".format(i + 1, total, label))

                actual_coord_view_name = coord_view_name
                if do_views:
                    pb.title = "Document {0}/{1}: {2} — Creating views".format(i + 1, total, doc.Title)
                    before_count = len(dismissed_log)
                    view_label_text, ok, created_coord_name = _create_views(
                        doc, coord_view_name, navis_view_name, view_type_name)
                    steps.append((view_label_text, ok))
                    steps.extend(dismissed_log[before_count:])
                    if created_coord_name:
                        actual_coord_view_name = created_coord_name

                if do_pubset:
                    pb.title = "Document {0}/{1}: {2} — Publish Settings".format(i + 1, total, doc.Title)
                    if _activate_document(uiapp, doc):
                        steps.append(_configure_publish_settings(uiapp, coord_set_name, actual_coord_view_name))
                    else:
                        steps.append(("Publish Settings skipped - could not switch to this document "
                                      "(activate it and configure manually)", None))
                else:
                    steps.append(("Publish Settings skipped (unchecked)", None))

                if do_sync:
                    try:
                        workshared = doc.IsWorkshared
                    except Exception:
                        workshared = False
                    if not workshared:
                        steps.append(("Sync skipped - not a workshared model", None))
                    else:
                        pb.title = "Document {0}/{1}: {2} — Synchronizing".format(i + 1, total, doc.Title)
                        before_count = len(dismissed_log)
                        steps.append(_sync(doc))
                        steps.extend(dismissed_log[before_count:])

                steps.append(("Left open (already open in this session)", None))
    finally:
        uiapp.DialogBoxShowing -= dialog_handler
        # The Publish Settings step may have switched the active document -
        # put the user back where they started (best effort).
        if active_doc is not None:
            _activate_document(uiapp, active_doc)

    _print_results(all_results)


# ══════════════════════════════════════════════════════════════════════════
# Entry point - where are the models?
# ══════════════════════════════════════════════════════════════════════════
SOURCE_ACC = "ACC project - open cloud files in batch"
SOURCE_SESSION = "Opened Revit session - documents already open"


def main():
    source = forms.CommandSwitchWindow.show(
        [SOURCE_ACC, SOURCE_SESSION],
        message="DeeBIMview — where are the models?"
    )
    if not source:
        return
    if source == SOURCE_SESSION:
        _run_session(__revit__)
    else:
        _run_acc()


main()
