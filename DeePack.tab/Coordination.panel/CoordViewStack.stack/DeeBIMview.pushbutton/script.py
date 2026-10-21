# -*- coding: utf-8 -*-
"""
DeeBIMview
One window with every option on screen - no chain of popup prompts.

Models (pick the source with the radio buttons):
  - ACC project: Connect, choose Hub and Project from the dropdowns, Load
    Files (a cached file list is used unless "Rescan" is ticked), tick any
    number of cloud files. Each is opened in turn and processed.
  - Opened Revit session: the project documents ALREADY open in this Revit
    are listed (linked copies and family documents are skipped); tick any
    number. Nothing is opened or closed.

Steps (tick any combination; only "Create/Update Views" is on by default):
  - Create/Update Views: 3D views "BIM Coordination View" and "BIM Exported
    NAVIS" under a 3D view type (all names editable in the window). The
    Revit Links category is HIDDEN in them by default - these views exist to
    publish / export the model on its own, without linked content - and one
    checkbox PER VIEW (Coordination / NAVIS) turns that off to show the links
    instead. The visibility is set explicitly both ways, so a view reused
    from an earlier run is corrected.
  - Synchronize With Central
  - Close After Sync   (ACC only - open-session files are left open)
  - Publish via the APS Commands API   (ACC only - it needs each model's
    cloud item id, which an open document does not expose)
ACC-only options (open with all worksets closed, publish without links)
are disabled while the source is the open session.

There is deliberately NO "Configure Publish Settings" step any more. It drove
Revit's Publish Settings dialog through UI Automation, starting with a
simulated ribbon click that was never verified to work and failed on every
run ("Target element cannot receive focus"). Revit does have a postable
PublishSettings command, but a posted command only runs after the script
ends, so the dialog could not be driven file by file inside a batch. Publish
therefore uses whatever publish settings each model already has. (The old
automation is still in lib/publish_settings_ui.py for DeeCoord.)

The window only COLLECTS the choices; when Run is pressed it closes and the
batch then runs with a progress bar and the coloured results page - the
same way DeeLazy launches its modules, because opening files and switching
the active document are not safe to do from inside a still-open modal
window.

ACC files are opened with a cross-project-safe GUID lookup shared with every
other Dee cloud tool (lib/acc_file_browser.py).
"""
import os

import clr
clr.AddReference("WindowsBase")
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")

from System import Action
from System.Windows import Visibility
from System.Windows.Threading import Dispatcher, DispatcherFrame, DispatcherPriority

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

import dee_branding
import acc_auth
import acc_api
import acc_file_browser as afb
import dee_telemetry
dee_telemetry.check_access("DeeBIMview")


output = script.get_output()

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")
# Per-tool ACC file-list cache (gitignored), same convention as the other
# Dee cloud tools - NOT shared with DeeOpener any more, since the shared
# lister takes the cache path as an argument.
_CACHE_FILE = os.path.join(_THIS_DIR, ".acc_file_cache.json")

# Defaults - editable in the window. The view TYPE name (Project Browser
# grouping) must differ from the view instance names - Revit does not allow
# a View Type and a View to share the exact same name.
DEFAULT_VIEW_TYPE = "BIM Coordination"
DEFAULT_COORD_VIEW = "BIM Coordination View"
DEFAULT_NAVIS_VIEW = "BIM Exported NAVIS"

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


def _set_links_hidden(doc, view, hidden):
    """Hides or shows the Revit Links category in a view. Setting it
    explicitly both ways matters: a reused view that was hidden on an
    earlier run must be shown again when the option is turned off."""
    cat = doc.Settings.Categories.get_Item(BuiltInCategory.OST_RvtLinks)
    if cat is not None and view.CanCategoryBeHidden(cat.Id):
        view.SetCategoryHidden(cat.Id, hidden)


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
        proj_guid, model_guid, _src = afb.get_cloud_path_guids(project_id, item_id, token)
        cloud_path = ModelPathUtils.ConvertCloudGUIDsToCloudPath(region, proj_guid, model_guid)
        wc_option = (WorksetConfigurationOption.CloseAllWorksets if close_worksets
                     else WorksetConfigurationOption.OpenAllWorksets)
        open_options = OpenOptions()
        open_options.SetOpenWorksetsConfiguration(WorksetConfiguration(wc_option))
        uidoc = uiapp.OpenAndActivateDocument(cloud_path, open_options, False)
        return uidoc.Document, ("Opened", True)
    except Exception as e:
        return None, ("Open FAILED: {0}".format(e), False)


def _create_views(doc, coord_view_name, navis_view_name, view_type_name,
                  hide_links_coord=True, hide_links_navis=True):
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
            _set_links_hidden(doc, coord_view, hide_links_coord)
        except Exception as e:
            raise Exception("[links visibility on coord view] {0}".format(e))

        try:
            navis_view, actual_navis = _ensure_3d_view(doc, navis_view_name, type_id)
        except Exception as e:
            raise Exception("[navis view] {0}".format(e))

        try:
            _set_links_hidden(doc, navis_view, hide_links_navis)
        except Exception as e:
            raise Exception("[links visibility on navis view] {0}".format(e))

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
        links_note = "coordination links {0}, NAVIS links {1}".format(
            "hidden" if hide_links_coord else "shown", "hidden" if hide_links_navis else "shown")
        return ("Views created/updated (type: '{0}', {1}){2}".format(actual_type, links_note, note), True, actual_coord)
    except Exception as e:
        if t.GetStatus() == TransactionStatus.Started:
            t.RollBack()
        return ("Views FAILED: {0}".format(e), False, None)


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


SCRIPT_VERSION = "2026-10-22-v3-window"


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



# ══════════════════════════════════════════════════════════════════════════
# The window - collects every choice, then closes and the batch runs
# ══════════════════════════════════════════════════════════════════════════
_ACC = "acc"
_SESSION = "session"


class FileRow(object):
    """One tickable model: ref is the ACC item id (ACC source) or the open
    Document (session source)."""

    def __init__(self, name, kind, note, ref):
        self.name = name
        self.kind = kind
        self.note = note
        self.ref = ref
        self.selected = False


class DeeBIMviewWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, uiapp):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.uiapp = uiapp
        self.request = None
        self._ready = False
        self._suppress = False
        self._token = None
        self._hubs = []
        self._hub = None
        self._projects = []
        self._project = None
        self._acc_rows = []
        self._session_rows = []

        self.viewtype_tb.Text = DEFAULT_VIEW_TYPE
        self.coord_tb.Text = DEFAULT_COORD_VIEW
        self.navis_tb.Text = DEFAULT_NAVIS_VIEW

        # Radio buttons are set in code, not XAML: both have a wired Checked
        # handler, and XAML-declared IsChecked fires it before this window
        # is ready (a known hazard in this extension).
        has_open_docs = bool(_open_project_documents(uiapp))
        self.source_session_rb.IsChecked = has_open_docs
        self.source_acc_rb.IsChecked = not has_open_docs
        self._ready = True
        self._apply_source()

    # ---------------- small helpers ----------------
    def _status(self, message):
        self.status_tb.Text = message

    def _pump(self):
        """Let WPF repaint before a long blocking call (status text etc.)."""
        try:
            frame = DispatcherFrame()

            def _stop():
                frame.Continue = False

            self.Dispatcher.BeginInvoke(DispatcherPriority.Background, Action(_stop))
            Dispatcher.PushFrame(frame)
        except Exception:
            pass

    def _guard(self, fn):
        try:
            fn()
        except Exception as e:
            self._status("ERROR: {0}".format(e))
            forms.alert("DeeBIMview hit an error:\n\n{0}".format(e), title="DeeBIMview")

    def _source(self):
        return _SESSION if bool(self.source_session_rb.IsChecked) else _ACC

    def _rows(self):
        return self._session_rows if self._source() == _SESSION else self._acc_rows

    # ---------------- source / enabling ----------------
    def _apply_source(self):
        is_acc = (self._source() == _ACC)
        self.acc_panel.Visibility = Visibility.Visible if is_acc else Visibility.Collapsed
        self.session_panel.Visibility = Visibility.Collapsed if is_acc else Visibility.Visible
        for ctl in (self.step_close_cb, self.step_publish_cb, self.open_regular_rb, self.open_closed_rb):
            ctl.IsEnabled = is_acc
        if not is_acc:
            self.step_close_cb.IsChecked = False
            self.step_publish_cb.IsChecked = False
            if not self._session_rows:
                self._load_session()
        self._update_enables()
        self._refresh_grid()

    def _update_enables(self):
        is_acc = (self._source() == _ACC)
        publish_on = is_acc and bool(self.step_publish_cb.IsChecked)
        self.publish_normal_rb.IsEnabled = publish_on
        self.publish_nolinks_rb.IsEnabled = publish_on

    def source_changed(self, sender, args):
        if not self._ready:
            return
        self._apply_source()

    def step_changed(self, sender, args):
        if not self._ready:
            return
        self._update_enables()

    # ---------------- session documents ----------------
    def _load_session(self):
        try:
            active = self.uiapp.ActiveUIDocument.Document
        except Exception:
            active = None
        rows = []
        for d in _open_project_documents(self.uiapp):
            is_active = active is not None and _same_document(d, active)
            try:
                if d.IsModelInCloud:
                    kind = "Cloud"
                elif d.IsWorkshared:
                    kind = "Workshared"
                else:
                    kind = "Local"
            except Exception:
                kind = "?"
            row = FileRow(d.Title, kind, "(active)" if is_active else "", d)
            row.selected = is_active
            rows.append(row)
        self._session_rows = rows

    def refresh_docs_click(self, sender, args):
        self._load_session()
        self._refresh_grid()

    # ---------------- ACC: hubs / projects / files ----------------
    def connect_click(self, sender, args):
        self._guard(self._connect)

    def _connect(self):
        self._status("Signing in / loading hubs...")
        self._pump()
        try:
            self._token = acc_auth.get_access_token()
        except Exception as e:
            self._status("Authentication failed.")
            forms.alert("Authentication failed:\n{0}".format(e), title="DeeBIMview")
            return
        hubs = acc_api.list_hubs(self._token)
        if not hubs:
            self._status("No ACC/BIM360 hubs found.")
            forms.alert("No ACC/BIM360 hubs found for this account.", title="DeeBIMview")
            return
        self._hubs = sorted(hubs, key=lambda h: (h[1] or "").lower())
        self._suppress = True
        try:
            self.hub_cb.ItemsSource = ["{0}  [{1}]".format(name, region or "?") for _id, name, region in self._hubs]
            self.project_cb.ItemsSource = None
        finally:
            self._suppress = False
        self._hub = None
        self._project = None
        self._projects = []
        self._acc_rows = []
        self._refresh_grid()
        self._status("{0} hub(s) loaded - choose a hub.".format(len(self._hubs)))
        if len(self._hubs) == 1:
            self.hub_cb.SelectedIndex = 0

    def hub_changed(self, sender, args):
        if not self._ready or self._suppress:
            return
        idx = self.hub_cb.SelectedIndex
        if idx < 0 or idx >= len(self._hubs):
            return
        self._guard(lambda: self._load_projects(self._hubs[idx]))

    def _load_projects(self, hub):
        self._hub = hub
        self._status("Loading projects in '{0}'...".format(hub[1]))
        self._pump()
        projects = acc_api.list_projects(hub[0], self._token)
        self._project = None
        self._acc_rows = []
        self.cache_tb.Text = ""
        if not projects:
            self._projects = []
            self._suppress = True
            try:
                self.project_cb.ItemsSource = None
            finally:
                self._suppress = False
            self._refresh_grid()
            self._status("No projects found in that hub.")
            return
        self._projects = sorted(projects, key=lambda p: (p[1] or "").lower())
        self._suppress = True
        try:
            self.project_cb.ItemsSource = [name for _pid, name in self._projects]
        finally:
            self._suppress = False
        self._refresh_grid()
        self._status("{0} project(s) - choose a project.".format(len(self._projects)))

    def project_changed(self, sender, args):
        if not self._ready or self._suppress:
            return
        idx = self.project_cb.SelectedIndex
        if idx < 0 or idx >= len(self._projects):
            return
        self._project = self._projects[idx]
        self._acc_rows = []
        self._refresh_grid()
        self._show_cache_info()
        self._status("Project '{0}' selected - press Load Files.".format(self._project[1]))

    def _show_cache_info(self):
        if self._project is None:
            self.cache_tb.Text = ""
            return
        info = afb.cache_info(_CACHE_FILE, self._project[0])
        if info:
            self.cache_tb.Text = "Cached list: {0} files, saved {1}.".format(info[1], info[0])
        else:
            self.cache_tb.Text = "No cached list yet - the first load scans the whole project (can take minutes)."

    def load_files_click(self, sender, args):
        self._guard(self._load_files)

    def _load_files(self):
        if self._token is None or self._hub is None or self._project is None:
            forms.alert("Connect to ACC and choose a hub and a project first.", title="DeeBIMview")
            return
        cache_mode = "rescan" if bool(self.rescan_cb.IsChecked) else "cache"
        self._status("Loading Revit files from '{0}'...".format(self._project[1]))
        self._pump()
        items = afb.list_project_files(self._hub[0], self._project[0], self._token, _CACHE_FILE, cache_mode)
        if not items:
            self._status("No Revit files were loaded (cancelled, or none found).")
            return
        self._acc_rows = [FileRow(n, "ACC cloud model", "", items[n]) for n in sorted(items, key=lambda n: n.lower())]
        self.rescan_cb.IsChecked = False
        self._show_cache_info()
        self._refresh_grid()
        warnings = getattr(afb, "last_scan_warnings", None)
        extra = "  ({0} folder(s) could not be read)".format(len(warnings)) if warnings else ""
        self._status("{0} file(s) loaded - tick the ones to process.{1}".format(len(self._acc_rows), extra))

    # ---------------- file grid ----------------
    def _visible_rows(self):
        rows = self._rows()
        text = (self.search_tb.Text or "").strip().lower()
        if not text:
            return rows
        words = text.split()
        return [r for r in rows if all(w in r.name.lower() for w in words)]

    def _refresh_grid(self):
        self.files_grid.ItemsSource = None
        self.files_grid.ItemsSource = self._visible_rows()

    def search_changed(self, sender, args):
        if not self._ready:
            return
        self._refresh_grid()

    def select_all_click(self, sender, args):
        for r in self._visible_rows():
            r.selected = True
        self.files_grid.Items.Refresh()

    def select_none_click(self, sender, args):
        for r in self._visible_rows():
            r.selected = False
        self.files_grid.Items.Refresh()

    # ---------------- run / close ----------------
    def run_click(self, sender, args):
        source = self._source()
        rows = [r for r in self._rows() if r.selected]
        if not rows:
            forms.alert("Tick at least one model to process.", title="DeeBIMview")
            return
        do_views = bool(self.step_views_cb.IsChecked)
        do_sync = bool(self.step_sync_cb.IsChecked)
        do_close = bool(self.step_close_cb.IsChecked) and source == _ACC
        do_publish = bool(self.step_publish_cb.IsChecked) and source == _ACC
        if not (do_views or do_sync or do_close or do_publish):
            forms.alert("Tick at least one step to run.", title="DeeBIMview")
            return

        view_type_name = (self.viewtype_tb.Text or "").strip()
        coord_view_name = (self.coord_tb.Text or "").strip()
        navis_view_name = (self.navis_tb.Text or "").strip()
        if do_views and not coord_view_name:
            forms.alert("Enter the coordination view name.", title="DeeBIMview")
            return
        if do_views and (not view_type_name or not navis_view_name):
            forms.alert("Enter the 3D view type name and the NAVIS view name.", title="DeeBIMview")
            return
        if source == _ACC and (self._token is None or self._project is None or self._hub is None):
            forms.alert("Connect to ACC and choose a hub and a project first.", title="DeeBIMview")
            return

        self.request = {
            "source": source,
            "targets": [(r.name, r.ref) for r in rows],
            "region": self._hub[2] if self._hub else None,
            "project_id": self._project[0] if self._project else None,
            "token": self._token,
            "close_worksets": bool(self.open_closed_rb.IsChecked),
            "do_views": do_views,
            "hide_links_coord": bool(self.hide_links_coord_cb.IsChecked),
            "hide_links_navis": bool(self.hide_links_navis_cb.IsChecked),
            "do_sync": do_sync,
            "do_close": do_close,
            "do_publish": do_publish,
            "view_type_name": view_type_name,
            "coord_view_name": coord_view_name,
            "navis_view_name": navis_view_name,
            "without_links": bool(self.publish_nolinks_rb.IsChecked),
        }
        self.Close()

    def close_click(self, sender, args):
        self.Close()


# ══════════════════════════════════════════════════════════════════════════
# Running the batch (after the window has closed)
# ══════════════════════════════════════════════════════════════════════════
def _process_acc(uiapp, req):
    targets = req["targets"]
    region = req["region"]
    project_id = req["project_id"]
    token = req["token"]
    do_views = req["do_views"]
    do_sync = req["do_sync"]
    do_close = req["do_close"]
    do_publish = req["do_publish"]

    all_results = []
    total = len(targets)
    prev_doc = None
    prev_steps = None

    dismissed_log = []
    dialog_handler = _make_dialog_handler(dismissed_log)
    uiapp.DialogBoxShowing += dialog_handler

    try:
        with _SafeProgress(title="DeeBIMview — starting...", cancellable=True) as pb:
            for i, (name, item_id) in enumerate(targets):
                if pb.cancelled:
                    all_results.append((name, [("Cancelled", None)]))
                    break

                steps = []
                all_results.append((name, steps))
                pb.update_progress(i, total)

                pb.title = "File {0}/{1}: {2} — Opening".format(i + 1, total, name)
                output.print_md("**File {0}/{1}: {2} — Opening**".format(i + 1, total, name))
                before_count = len(dismissed_log)
                doc, step = _open_file(uiapp, region, project_id, name, item_id, token, req["close_worksets"])
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

                if do_views:
                    pb.title = "File {0}/{1}: {2} — Creating views".format(i + 1, total, name)
                    output.print_md("Creating views...")
                    before_count = len(dismissed_log)
                    label, ok, _created_coord_name = _create_views(
                        doc, req["coord_view_name"], req["navis_view_name"], req["view_type_name"],
                        req["hide_links_coord"], req["hide_links_navis"])
                    steps.append((label, ok))
                    steps.extend(dismissed_log[before_count:])

                if do_sync:
                    pb.title = "File {0}/{1}: {2} — Synchronizing".format(i + 1, total, name)
                    output.print_md("Synchronizing with central...")
                    before_count = len(dismissed_log)
                    steps.append(_sync(doc))
                    steps.extend(dismissed_log[before_count:])

                if do_publish:
                    pb.title = "File {0}/{1}: {2} — Publishing".format(i + 1, total, name)
                    output.print_md("Publishing...")
                    steps.append(_publish(project_id, item_id, token, req["without_links"]))

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


def _process_session(uiapp, req):
    targets = req["targets"]
    do_views = req["do_views"]
    do_sync = req["do_sync"]

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

                if do_views:
                    pb.title = "Document {0}/{1}: {2} — Creating views".format(i + 1, total, doc.Title)
                    before_count = len(dismissed_log)
                    view_label_text, ok, _created_coord_name = _create_views(
                        doc, req["coord_view_name"], req["navis_view_name"], req["view_type_name"],
                        req["hide_links_coord"], req["hide_links_navis"])
                    steps.append((view_label_text, ok))
                    steps.extend(dismissed_log[before_count:])

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

    _print_results(all_results)


# ══════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════
def main():
    uiapp = __revit__
    window = DeeBIMviewWindow(_XAML_FILE, uiapp)
    window.ShowDialog()
    req = window.request
    if req is None:
        return
    if req["source"] == _SESSION:
        _process_session(uiapp, req)
    else:
        _process_acc(uiapp, req)


main()
