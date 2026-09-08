# -*- coding: utf-8 -*-
from pyrevit import forms, script
from Autodesk.Revit.DB import (
    OpenOptions, WorksetConfigurationOption, WorksetConfiguration, ModelPathUtils,
    WorksharingUtils, DetachFromCentralOption,
)
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
import deew_model_scanner as scanner

import clr
clr.AddReference("System.Windows.Forms")
from System.Windows.Forms import OpenFileDialog, FolderBrowserDialog, DialogResult

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


SOURCE_ACC = "ACC / BIM 360 cloud project"
SOURCE_LOCAL = "Local or network files"

# (label, detach_kind, close_worksets). detach_kind None = stay attached.
#
# Detach IS available through UIApplication.OpenAndActivateDocument - it is
# OpenOptions.DetachFromCentralOption that decides, and the documented overload
# is OpenAndActivateDocument(modelPath, openOptions, bDetachAndPrompt). An
# earlier note in this repo claimed that call "cannot detach"; what actually
# happened was that acc_file_browser.open_cloud_file never SET the option, only
# the workset configuration.
#
# Detach and worksets are offered as four flat modes rather than a matrix
# because that is the real decision being made per batch, and one click beats
# two prompts. Both detached modes open all worksets.
OPEN_MODES = [
    ("Regular Open", None, False),
    ("Open with All Worksets Closed", None, True),
    ("Detached - preserve worksets", "preserve", False),
    ("Detached - discard worksets (model becomes non-workshared)", "discard", False),
]


class OpenTarget(object):
    """One model to open. `workshared` matters because Revit REJECTS a detach
    option outright on a non-workshared model - ArgumentException "Detach
    option is not valid" - rather than ignoring it. The local branch knows each
    file's type from its header, so it can simply not ask for detach on a
    Standalone; the open loop still falls back if the classification was wrong.
    """

    def __init__(self, label, factory, workshared=True):
        self.label = label
        self.factory = factory
        self.workshared = workshared


def main():
    """DeeOpener opens models from EITHER an ACC project or the local/network
    file system. The two halves share the open loop and the report - only the
    way a model is located differs, so _open_and_report() takes a lazy path
    factory per model rather than a ready ModelPath."""
    source = forms.CommandSwitchWindow.show(
        [SOURCE_ACC, SOURCE_LOCAL],
        message="Where are the models you want to open?")
    if not source:
        return
    if source == SOURCE_LOCAL:
        _open_from_local()
    else:
        _open_from_acc()


# ==========================================================================
# Local / network files
# ==========================================================================
def _pick_local_paths():
    """Returns a list of .rvt paths, or [] if the user backed out. Two ways in
    because both are normal: a handful of known files, or a whole job folder."""
    how = forms.CommandSwitchWindow.show(
        ["Pick files...", "Scan a folder...", "Scan a folder and its subfolders..."],
        message="How do you want to choose the local models?")
    if not how:
        return []

    if how == "Pick files...":
        dlg = OpenFileDialog()
        dlg.Title = "Select Revit models to open"
        dlg.Filter = "Revit models (*.rvt)|*.rvt"
        dlg.Multiselect = True
        if dlg.ShowDialog() != DialogResult.OK:
            return []
        return list(dlg.FileNames)

    dlg = FolderBrowserDialog()
    dlg.Description = "Select a folder containing Revit models"
    if dlg.ShowDialog() != DialogResult.OK:
        return []
    recursive = how.endswith("subfolders...")
    folder = dlg.SelectedPath
    found = []
    try:
        if recursive:
            for root, _dirs, files in os.walk(folder):
                for f in files:
                    if f.lower().endswith(".rvt"):
                        found.append(os.path.join(root, f))
        else:
            found = [os.path.join(folder, f) for f in os.listdir(folder)
                     if f.lower().endswith(".rvt")]
    except Exception as e:
        forms.alert("Could not read that folder:\n{0}".format(e))
        return []
    return sorted(found)


def _local_label(model):
    """One line per file, carrying the facts that decide how it should be
    opened - a Central in particular should normally NOT be opened directly.
    Revit's own backup folders are full of files whose name tells you nothing,
    so the type matters more than the name here."""
    bits = [model.model_type]
    if model.version and model.version != "Unknown":
        bits.append(model.version)
    bits.append(model.size_mb_text)
    return "{0}   [{1}]".format(model.file_name, "  |  ".join(bits))


def _default_local_target(central_path):
    """Where a new local copy goes - <name>_<username>.rvt in the user's
    Documents folder, which is Revit's own default convention. Falls back to
    the central's own folder if Documents is not there."""
    base = os.path.splitext(os.path.basename(central_path))[0]
    user = os.environ.get("USERNAME") or "local"
    docs = os.path.join(os.path.expanduser("~"), "Documents")
    if not os.path.isdir(docs):
        docs = os.path.dirname(central_path)
    return os.path.join(docs, "{0}_{1}.rvt".format(base, user))


def _unique_path(path):
    """Never overwrite an existing local copy - that could throw away work
    somebody has not synchronised yet."""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 2
    while os.path.exists("{0} ({1}){2}".format(base, i, ext)):
        i += 1
    return "{0} ({1}){2}".format(base, i, ext)


def _local_path_factory(model, make_local):
    """Returns a callable producing (ModelPath, note) for one model. Deferred
    so CreateNewLocal - which writes a file and is slow - happens inside the
    progress loop rather than all up front."""
    def factory():
        source = ModelPathUtils.ConvertUserVisiblePathToModelPath(model.file_path)
        if make_local and model.model_type == scanner.MODEL_TYPE_CENTRAL:
            target_path = _unique_path(_default_local_target(model.file_path))
            target = ModelPathUtils.ConvertUserVisiblePathToModelPath(target_path)
            WorksharingUtils.CreateNewLocal(source, target)
            return target, "new local: {0}".format(os.path.basename(target_path))
        return source, ""
    return factory


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


def _open_from_local():
    paths = _pick_local_paths()
    if not paths:
        return

    with _SafeProgress(title="DeeOpener - reading model headers...",
                           cancellable=True) as pb:
        models = []
        for i, path in enumerate(paths):
            if pb.cancelled:
                return
            pb.update_progress(i, len(paths))
            models.append(scanner.scan_file(path))

    unreadable = [m for m in models if m.model_type == scanner.MODEL_TYPE_CORRUPTED]
    usable = [m for m in models if m.model_type != scanner.MODEL_TYPE_CORRUPTED]
    if not usable:
        forms.alert("None of those {0} file(s) could be read.\n\n"
                    "They may be corrupted, locked by another Revit session, "
                    "or saved by a newer Revit than this one.".format(len(models)))
        return

    label_to_model = {}
    for m in usable:
        label_to_model[_local_label(m)] = m
    chosen = forms.SelectFromList.show(
        sorted(label_to_model.keys()),
        title="Select Local Models to Open  ({0} readable)".format(len(usable)),
        multiselect=True, button_name="Open Selected")
    if not chosen:
        return
    picked = [label_to_model[c] for c in chosen]

    mode = _ask_open_mode()
    if mode is None:
        return
    detach, close_worksets = mode

    # A workshared CENTRAL should not normally be opened directly - that is
    # exactly what Revit's own "Create New Local" exists to avoid. Only asked
    # when a central is actually in the selection, and never assumed - and NOT
    # asked at all when detaching, because a detached open never touches the
    # central in the first place, which is the whole point of it.
    centrals = [m for m in picked if m.model_type == scanner.MODEL_TYPE_CENTRAL]
    make_local = False
    if centrals and not detach:
        answer = forms.CommandSwitchWindow.show(
            ["Create a new local copy first (recommended)",
             "Open the central file directly"],
            message="{0} of the selected model(s) are workshared CENTRAL "
                    "files.".format(len(centrals)))
        if not answer:
            return
        make_local = answer.startswith("Create")

    workshared_types = (scanner.MODEL_TYPE_CENTRAL, scanner.MODEL_TYPE_LOCAL)
    targets = [OpenTarget(m.file_name, _local_path_factory(m, make_local),
                          workshared=m.model_type in workshared_types)
               for m in picked]
    extra = [(False, m.file_name, "Skipped - could not read header: {0}".format(
        m.error or m.model_type)) for m in unreadable]
    _open_and_report(targets, close_worksets, detach, extra)


# ==========================================================================
# Shared open loop
# ==========================================================================
def _ask_open_mode():
    """(detach_kind, close_worksets), or None if the user backed out. None as
    the whole result is deliberately distinct from a detach_kind of None, which
    is a real choice meaning "stay attached"."""
    labels = [m[0] for m in OPEN_MODES]
    chosen = forms.CommandSwitchWindow.show(
        labels, message="How should the selected files be opened?")
    if not chosen:
        return None
    for label, detach, close_worksets in OPEN_MODES:
        if label == chosen:
            return detach, close_worksets
    return None, False


def _build_open_options(close_worksets, detach):
    options = OpenOptions()
    wc_option = (WorksetConfigurationOption.CloseAllWorksets if close_worksets
                 else WorksetConfigurationOption.OpenAllWorksets)
    options.SetOpenWorksetsConfiguration(WorksetConfiguration(wc_option))
    if detach == "preserve":
        options.DetachFromCentralOption = \
            DetachFromCentralOption.DetachAndPreserveWorksets
    elif detach == "discard":
        options.DetachFromCentralOption = \
            DetachFromCentralOption.DetachAndDiscardWorksets
    return options


def _open_and_report(targets, close_worksets, detach=None, extra_results=None):
    """targets: [OpenTarget]. target.factory() -> (ModelPath, note).

    Shared by the ACC and local paths so the dialog-suppression handler, the
    progress bar and the report exist once. The factory is called INSIDE the
    loop so a per-model failure (an unresolvable cloud GUID, a CreateNewLocal
    that cannot write) is reported against that model instead of aborting the
    whole batch before it starts.

    A detach request on a model that turns out not to be workshared is retried
    once without it, because Revit rejects the option rather than ignoring it -
    the same attempt-and-fall-back this repo already needed in
    deew_document_manager.open_document_best_detach()."""
    uiapp = __revit__
    dismissed_log = []
    dialog_handler = _make_dialog_handler(dismissed_log)
    uiapp.DialogBoxShowing += dialog_handler

    results = list(extra_results or [])
    try:
        with _SafeProgress(title="DeeOpener - opening {value} of {max_value}...",
                               cancellable=False) as pb:
            for i, target in enumerate(targets):
                label = target.label
                pb.update_progress(i, len(targets))
                before_count = len(dismissed_log)
                notes = []
                want_detach = detach if target.workshared else None
                if detach and not target.workshared:
                    notes.append("not workshared, opened attached")
                try:
                    model_path, note = target.factory()
                    if note:
                        notes.insert(0, note)
                    try:
                        uiapp.OpenAndActivateDocument(
                            model_path, _build_open_options(close_worksets, want_detach),
                            False)
                    except Exception as detach_err:
                        if not want_detach:
                            raise
                        notes.append("detach refused ({0}), opened attached".format(
                            str(detach_err).split("\n")[0][:80]))
                        uiapp.OpenAndActivateDocument(
                            model_path, _build_open_options(close_worksets, None), False)
                    detail = "Opened"
                    if detach and want_detach:
                        detail = "Opened DETACHED ({0} worksets)".format(want_detach)
                    if notes:
                        detail += "  (" + "; ".join(notes) + ")"
                    results.append((True, label, detail))
                except Exception as e:
                    results.append((False, label, str(e)))
                for msg, _sev in dismissed_log[before_count:]:
                    results.append((True, label, msg))
    finally:
        uiapp.DialogBoxShowing -= dialog_handler

    html = '<h2 style="font-family:sans-serif;color:#ddd;">Batch Open Results</h2>'
    for ok, name, detail in results:
        bg = "#1b5e20" if ok else "#b71c1c"
        icon = "&#10003;" if ok else "&#10007;"
        html += (
            '<div style="padding:8px 14px;margin:3px 0;background:{0};color:#fff;'
            'border-radius:4px;font-family:monospace;font-size:13px;">'
            '<b>{1}</b>&nbsp; {2} &mdash; {3}'
            '</div>'.format(bg, icon, name, detail)
        )
    output.print_html(html)


# ==========================================================================
# ACC / BIM 360
# ==========================================================================
def _open_from_acc():
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

        with _SafeProgress(title="Finding all cloud models...",
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

    mode = _ask_open_mode()
    if mode is None:
        return
    detach, close_worksets = mode

    # Cloud models are workshared by definition, so detach always applies here.
    targets = [OpenTarget(name,
                          _acc_path_factory(project_id, all_items[name], region, token),
                          workshared=True)
               for name in selected_names]
    _open_and_report(targets, close_worksets, detach)


def _acc_path_factory(project_id, item_id, region, token):
    """Cloud GUID resolution is deferred into the open loop: it is a network
    call that can fail per-model, and doing it lazily means one unresolvable
    item is reported against its own row instead of killing the batch."""
    def factory():
        proj_guid, model_guid, _src = get_cloud_path_guids(project_id, item_id, token)
        return ModelPathUtils.ConvertCloudGUIDsToCloudPath(
            region, proj_guid, model_guid), ""
    return factory


main()
