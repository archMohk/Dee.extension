# -*- coding: utf-8 -*-
"""
DeeTransmit
Strip models down ready to issue - from three sources: the models you
already have open, local/network files, or an ACC/BIM 360 project.

--------------------------------------------------------------------
The shape of the flow, and why
--------------------------------------------------------------------
Every prompt happens with NO window open. The options window collects
the choices, closes, and only then does any work start. That is not
tidiness: opening a second modal (CommandSwitchWindow, ask_for_string)
from inside an already-modal window is a documented way to kill Revit
in this codebase, and DeeBlocktoFamily was rewritten around exactly
that crash.

  1. Where are the models?          (no window)
  2. Pick them                      (no window)
  3. Models you already have open get an explicit warning here, and
     files get DeeOpener's four open modes.
  4. Options window                 -> closes before anything runs
  5. Confirm what was ticked        (no window)
  6. Clean, with a progress bar
  7. Save / Synchronize / copy / nothing - asked at the END, so a bad
     result can simply be thrown away by choosing nothing.

Nothing is written until step 7. A model opened detached cannot reach
its central at all; a model opened attached, or one already open in the
session, is only ever written if the user picks Save or Synchronize.

The cleaning itself is lib/dee_transmit_service.py, which reuses
lib/deew_clean_service.py for the operations that already existed.
"""
import os
import traceback

import clr
clr.AddReference("System.Windows.Forms")
from System.Windows.Forms import (
    FolderBrowserDialog, OpenFileDialog, DialogResult, MessageBox
)
from Autodesk.Revit.UI import TaskDialogResult

from pyrevit import forms, script

import dee_branding
import deew_settings
import deew_document_manager as dm
import deew_model_scanner as scanner
import dee_transmit_service as core

output = script.get_output()

_TOOL = "DeeTransmit"
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")
_SETTINGS = "dee_transmit"
_CACHE_FILE = os.path.join(_THIS_DIR, ".acc_file_cache.json")

SOURCE_SESSION = "Models I already have open"
SOURCE_LOCAL = "Local or network files"
SOURCE_ACC = "ACC / BIM 360 cloud project"

# (label, detach_kind, open_all_worksets) - the same four DeeOpener offers.
OPEN_MODES = [
    ("Detached - discard worksets (recommended for issuing)", "discard", True),
    ("Detached - preserve worksets", "preserve", True),
    ("Regular Open", None, True),
    ("Open with All Worksets Closed", None, False),
]

# The finish modes and which of them are possible live in the service,
# so the rule can be tested without a Revit session.
FINISH_COPY = core.FINISH_COPY
FINISH_COPY_OPEN = core.FINISH_COPY_OPEN
FINISH_SAVE = core.FINISH_SAVE
FINISH_SYNC = core.FINISH_SYNC
FINISH_DISCARD = core.FINISH_DISCARD
FINISH_BACKGROUND = core.FINISH_BACKGROUND
FINISH_LEAVE = core.FINISH_LEAVE
_KEEP_OPEN = core.KEEP_OPEN


# Revit dialogs this tool answers on its own, matched against the dialog
# id and the message text together. Deleting content in bulk raises a lot
# of these, and a batch that stops on every one is unusable.
#
# Anything NOT listed here is answered Cancel - the non-destructive
# default - and then reported by name afterwards, so an unknown dialog
# surfaces as something to add to this list rather than as a mystery.
_DIALOG_ANSWERS = [
    ("serious error", int(TaskDialogResult.CommandLink2)),   # Continue Without Save
    ("unresolved", int(TaskDialogResult.CommandLink2)),
    ("could not find or read", int(TaskDialogResult.CommandLink2)),
    ("duplicate", int(TaskDialogResult.Ok)),
    ("group instance", int(TaskDialogResult.Ok)),
    ("extents greater than", int(TaskDialogResult.Ok)),
    ("truncated", int(TaskDialogResult.Ok)),
    ("have been deleted", int(TaskDialogResult.Ok)),
    ("will be upgraded", int(TaskDialogResult.Ok)),
    ("missing", int(TaskDialogResult.Close)),
    ("save", int(TaskDialogResult.Ok)),
]


def _make_dialog_handler(log):
    """uiapp.DialogBoxShowing fires just before Revit puts a native
    dialog on screen, which is the only chance to answer it without a
    human. The failure preprocessor in dee_transmit_service handles the
    other kind - failures raised inside a transaction - and between them
    they cover everything a cleaning run produces."""
    def handler(sender, args):
        try:
            try:
                dialog_id = args.DialogId or ""
            except Exception:
                dialog_id = ""
            try:
                message = args.Message or ""
            except Exception:
                message = ""
            text = (dialog_id + " " + message).lower()

            for needle, answer in _DIALOG_ANSWERS:
                if needle in text:
                    args.OverrideResult(answer)
                    log.append((True, dialog_id, message))
                    return
            args.OverrideResult(int(TaskDialogResult.Cancel))
            log.append((False, dialog_id, message))
        except Exception:
            pass
    return handler


class Target(object):
    """One model to clean. `opened_by_us` decides whether this tool is
    allowed to close it afterwards - a document the user already had
    open is never closed."""

    def __init__(self, label, doc, opened_by_us, detached=False):
        self.label = label
        self.doc = doc
        self.opened_by_us = opened_by_us
        self.detached = detached
        self.result = None
        self.saved_to = ""
        self.error = ""


# ==========================================================================
# the options window - collects choices, then gets out of the way
# ==========================================================================
class OptionsWindow(dee_branding.DeeBrandedWindow):
    _ready = False

    def __init__(self, xaml_file, header):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.confirmed = False
        self.options = {}
        self.source_tb.Text = header

        self._scopes = [self.sheets_cb, self.views_cb, self.legends_cb,
                        self.schedules_cb, self.params_cb]
        for combo in self._scopes:
            for label, _value in core.SCOPE_LABELS:
                combo.Items.Add(label)
            combo.SelectedIndex = 0

        self._checks = [
            self.revisions_cb, self.images_cb, self.cad_cb, self.links_cb,
            self.clouds_cb, self.design_cb, self.phases_cb, self.rooms_cb,
            self.groups_cb, self.templates_cb, self.filters_cb, self.scope_cb,
            self.grids_cb, self.families_cb, self.materials_cb, self.purge_cb,
        ]
        saved = deew_settings.load(_SETTINGS, {})
        self._apply(saved)
        self._ready = True

    def _guard(self, fn, *args):
        try:
            return fn(*args)
        except Exception as e:
            self.status_tb.Text = "ERROR: {0}".format(e)
            forms.alert("DeeTransmit hit an error:\n\n{0}\n\n{1}".format(
                e, traceback.format_exc()[-900:]), title=_TOOL)

    def _scope_value(self, combo):
        return core.SCOPE_LABELS[max(0, combo.SelectedIndex)][1]

    def _set_scope(self, combo, value):
        for i, (_label, v) in enumerate(core.SCOPE_LABELS):
            if v == value:
                combo.SelectedIndex = i
                return
        combo.SelectedIndex = 0

    def _apply(self, saved):
        keys = ["sheets", "views", "legends", "schedules", "parameters"]
        for combo, key in zip(self._scopes, keys):
            self._set_scope(combo, saved.get(key, core.SCOPE_NONE))
        names = ["revisions", "images", "cad", "revit_links", "point_clouds",
                 "design_options", "phases", "unplaced_rooms", "unused_groups",
                 "view_templates", "filters", "scope_boxes", "guide_grids",
                 "unused_families", "unused_materials", "purge_unused"]
        for box, key in zip(self._checks, names):
            box.IsChecked = bool(saved.get(key))
        self.geometry_cb.IsChecked = bool(saved.get("geometry_only"))
        self._sync_geometry()

    def _sync_geometry(self):
        """Geometry-only already removes every parameter, so the
        parameter scope would be a contradiction sitting next to it."""
        stripping = self.geometry_cb.IsChecked is True
        self.params_cb.IsEnabled = not stripping
        if stripping:
            self.status_tb.Text = ("Geometry only: every project and shared parameter "
                                   "goes, and identity data is cleared.")

    def geometry_click(self, sender, args):
        if not self._ready:
            return
        self._guard(self._sync_geometry)

    def _collect(self):
        keys = ["sheets", "views", "legends", "schedules", "parameters"]
        options = {}
        for combo, key in zip(self._scopes, keys):
            options[key] = self._scope_value(combo)
        names = ["revisions", "images", "cad", "revit_links", "point_clouds",
                 "design_options", "phases", "unplaced_rooms", "unused_groups",
                 "view_templates", "filters", "scope_boxes", "guide_grids",
                 "unused_families", "unused_materials", "purge_unused"]
        for box, key in zip(self._checks, names):
            options[key] = box.IsChecked is True
        options["geometry_only"] = self.geometry_cb.IsChecked is True
        if options["geometry_only"]:
            options["parameters"] = core.SCOPE_NONE
        return options

    def _set_all(self, value):
        for box in self._checks:
            box.IsChecked = value
        for combo in self._scopes:
            self._set_scope(combo, core.SCOPE_ALL if value else core.SCOPE_NONE)
        if not value:
            self.geometry_cb.IsChecked = False
        self._sync_geometry()

    def all_click(self, sender, args):
        self._guard(self._set_all, True)

    def none_click(self, sender, args):
        self._guard(self._set_all, False)

    def preset_click(self, sender, args):
        """What you usually want when sending a model out: drawings and
        borrowed content go, the model and its data stay."""
        def run():
            self._set_all(False)
            self._set_scope(self.sheets_cb, core.SCOPE_ALL)
            self._set_scope(self.views_cb, core.SCOPE_UNUSED)
            self._set_scope(self.legends_cb, core.SCOPE_ALL)
            self._set_scope(self.schedules_cb, core.SCOPE_ALL)
            for box in (self.revisions_cb, self.images_cb, self.cad_cb,
                        self.links_cb, self.clouds_cb, self.templates_cb,
                        self.filters_cb, self.scope_cb, self.grids_cb,
                        self.groups_cb, self.families_cb, self.purge_cb):
                box.IsChecked = True
            self.status_tb.Text = "Issue preset loaded - adjust anything you like."
        self._guard(run)

    def run_click(self, sender, args):
        def run():
            self.options = self._collect()
            if core.describe_options(self.options) == "nothing selected":
                forms.alert("Nothing is ticked, so there is nothing to do.", title=_TOOL)
                return
            try:
                deew_settings.save(_SETTINGS, self.options)
            except Exception:
                pass
            self.confirmed = True
            self.Close()
        self._guard(run)

    def cancel_click(self, sender, args):
        self.confirmed = False
        self.Close()


# ==========================================================================
# gathering the models
# ==========================================================================
def _session_targets(app):
    docs = []
    try:
        for doc in app.Documents:
            try:
                if doc.IsLinked or doc.IsFamilyDocument:
                    continue
                docs.append(doc)
            except Exception:
                continue
    except Exception:
        return None
    if not docs:
        forms.alert("No project models are open in this session.", title=_TOOL)
        return None

    by_name = {}
    for doc in docs:
        try:
            by_name[doc.Title] = doc
        except Exception:
            continue
    picked = forms.SelectFromList.show(
        sorted(by_name.keys()), title="Models open in this session",
        button_name="Use these", multiselect=True)
    if not picked:
        return None

    names = "\n".join("   - " + n for n in picked)
    if not forms.alert(
            "These models are OPEN and being worked in:\n\n{0}\n\n"
            "Cleaning happens in the live document, not a copy. Nothing is saved "
            "unless you choose Save or Synchronize at the end - if you do not like "
            "the result you can close without saving and lose nothing.\n\n"
            "Carry on?".format(names),
            title=_TOOL, yes=True, no=True):
        return None
    return [Target(n, by_name[n], opened_by_us=False) for n in picked]


def _ask_open_mode():
    chosen = forms.CommandSwitchWindow.show(
        [m[0] for m in OPEN_MODES],
        message="How should these models be opened?")
    if not chosen:
        return None
    for label, detach, worksets in OPEN_MODES:
        if label == chosen:
            return detach, worksets
    return None


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


def _pick_local_files():
    how = forms.CommandSwitchWindow.show(
        ["Pick files", "Scan a folder", "Scan a folder and its subfolders"],
        message="Which local models?")
    if not how:
        return []
    if how == "Pick files":
        dlg = OpenFileDialog()
        dlg.Filter = "Revit models (*.rvt)|*.rvt"
        dlg.Multiselect = True
        if dlg.ShowDialog() != DialogResult.OK:
            return []
        return list(dlg.FileNames)

    dlg = FolderBrowserDialog()
    dlg.Description = "Choose the folder holding the models"
    if dlg.ShowDialog() != DialogResult.OK:
        return []
    recursive = how.endswith("subfolders")
    found = []
    with _SafeProgress(title="DeeTransmit - scanning for models...",
                           indeterminate=True):
        try:
            found = scanner.scan_folder(dlg.SelectedPath, recursive=recursive)
        except Exception as e:
            forms.alert("Could not scan that folder:\n{0}".format(e), title=_TOOL)
            return []
    if not found:
        forms.alert("No .rvt files found there.", title=_TOOL)
        return []
    by_name = {}
    for model in found:
        by_name["{0}   [{1}]".format(
            os.path.basename(model.file_path), model.model_type)] = model.file_path
    picked = forms.SelectFromList.show(
        sorted(by_name.keys()), title="Models found", button_name="Use these",
        multiselect=True)
    return [by_name[p] for p in (picked or [])]


def _local_targets(app):
    paths = _pick_local_files()
    if not paths:
        return None
    mode = _ask_open_mode()
    if mode is None:
        return None
    detach, worksets = mode

    targets = []
    with _SafeProgress(title="DeeTransmit - opening models...",
                           cancellable=True) as pb:
        for i, path in enumerate(paths):
            if pb.cancelled:
                break
            pb.update_progress(i, len(paths))
            name = os.path.basename(path)
            # These helpers never raise - they hand back (document, error).
            if detach:
                doc, err = dm.open_document(app, path, detach_option=detach,
                                            open_all_worksets=worksets)
            else:
                doc, err = dm.open_document_no_detach(app, path,
                                                      open_all_worksets=worksets)
            if doc is None:
                t = Target(name, None, opened_by_us=False)
                t.error = "Could not open: {0}".format(err or "unknown reason")
                targets.append(t)
            else:
                targets.append(Target(name, doc, opened_by_us=True,
                                      detached=bool(detach)))
    return targets


def _acc_targets(app):
    try:
        import acc_auth
        import acc_file_browser as afb
    except Exception as e:
        forms.alert("The ACC modules could not be loaded:\n{0}".format(e), title=_TOOL)
        return None
    try:
        token = acc_auth.get_access_token()
    except Exception as e:
        forms.alert("Authentication failed:\n{0}".format(e), title=_TOOL)
        return None

    hub = afb.pick_hub(token)
    if hub is None:
        return None
    hub_id, region, _hub_name = hub
    project = afb.pick_project(hub_id, token)
    if project is None:
        return None
    project_id, _project_name = project
    all_items = afb.list_project_files(hub_id, project_id, token, _CACHE_FILE)
    if not all_items:
        return None
    picked = afb.pick_files_to_open(all_items, title="Select cloud models",
                                    button_name="Use these")
    if not picked:
        return None
    mode = _ask_open_mode()
    if mode is None:
        return None
    detach, _worksets = mode

    targets = []
    with _SafeProgress(title="DeeTransmit - opening cloud models...",
                           cancellable=True) as pb:
        for i, name in enumerate(picked):
            if pb.cancelled:
                break
            pb.update_progress(i, len(picked))
            if detach:
                doc, detail = afb.open_cloud_document_detached(
                    app, region, project_id, all_items[name], token)
            else:
                doc, detail = afb.open_cloud_document_attached(
                    app, region, project_id, all_items[name], token)
            if doc is None:
                t = Target(name, None, opened_by_us=False)
                t.error = "Could not open: {0}".format(detail or "unknown reason")
                targets.append(t)
            else:
                targets.append(Target(name, doc, opened_by_us=True,
                                      detached=bool(detach)))
    return targets


# ==========================================================================
# finishing
# ==========================================================================
def _finish_choices(live_targets):
    opened = [t for t in live_targets if t.opened_by_us]
    return core.finish_choices(
        len(opened),
        len([t for t in live_targets if not t.opened_by_us]),
        len([t for t in opened if t.detached]))


def _finish(targets, live_targets):
    """Asked at the END on purpose: by this point the result is known,
    and keeping nothing costs a single click."""
    chosen = forms.CommandSwitchWindow.show(
        _finish_choices(live_targets), message="The models are cleaned. What now?")
    if not chosen:
        return FINISH_BACKGROUND, ""
    if chosen in _KEEP_OPEN:
        return chosen, ""

    if chosen in (FINISH_COPY, FINISH_COPY_OPEN):
        dlg = FolderBrowserDialog()
        dlg.Description = "Choose a folder for the cleaned copies"
        if dlg.ShowDialog() != DialogResult.OK:
            return FINISH_BACKGROUND, ""
        return chosen, dlg.SelectedPath

    if chosen == FINISH_DISCARD:
        return chosen, ""

    if any(not t.opened_by_us for t in live_targets) and not forms.alert(
            "This writes over the models you had open before running DeeTransmit.\n\n"
            "Are you sure?", title=_TOOL, yes=True, no=True):
        return FINISH_BACKGROUND, ""
    return chosen, ""


def _commit(target, mode, folder):
    if mode in _KEEP_OPEN:
        return ("loaded in the background, nothing saved"
                if target.opened_by_us else "left as it was, nothing saved")
    if mode == FINISH_DISCARD:
        return "discarded without saving"
    if target.detached and mode in (FINISH_SAVE, FINISH_SYNC):
        return "detached copy - cannot save in place or synchronize"
    try:
        if mode in (FINISH_COPY, FINISH_COPY_OPEN):
            stem = os.path.splitext(os.path.basename(target.label))[0]
            path = dm.unique_target_path(folder, "{0}_CLEANED.rvt".format(stem))
            ok, detail = dm.save_copy_as(target.doc, path)
            if not ok:
                return "SAVE FAILED: {0}".format(detail)
            target.saved_to = detail or path
            return "copy saved to {0}".format(target.saved_to)
        if mode == FINISH_SYNC:
            if not dm.is_workshared(target.doc):
                return "not workshared - nothing to synchronize"
            ok, detail = dm.synchronize_with_central(
                target.doc, comment="DeeTransmit cleanup")
            return detail if ok else "SYNC FAILED: {0}".format(detail)
        if mode == FINISH_SAVE:
            if dm.is_workshared(target.doc):
                return ("saved (local file)" if dm.compact_and_save_local(target.doc)
                        else "SAVE FAILED")
            ok, detail = dm.save_standalone(target.doc)
            return detail if ok else "SAVE FAILED: {0}".format(detail)
    except Exception as e:
        return "SAVE FAILED: {0}".format(e)
    return "nothing done"


# ==========================================================================
def _report(targets, options, finish_mode, dialogs=None):
    html = '<h2 style="font-family:sans-serif;">DeeTransmit</h2>'
    html += ('<div style="font-family:sans-serif;font-size:12px;color:#888;'
             'margin-bottom:8px;">Removed: {0}<br>Finished with: {1}</div>'.format(
                 core.describe_options(options), finish_mode))
    ok = fail = 0
    for t in targets:
        if t.error:
            fail += 1
            bg, detail = "#c62828", t.error
        else:
            ok += 1
            bg = "#2e7d32"
            detail = t.result.summary() if t.result else "no changes"
            if t.result and t.result.notes:
                detail += "  |  " + "; ".join(t.result.notes)
            if t.result and t.result.errors:
                bg = "#8d6e19"
                detail += "  |  " + "; ".join(t.result.errors[:3])
            if t.saved_to:
                detail += "  |  saved: {0}".format(t.saved_to)
        html += ('<div style="padding:6px 12px;margin:2px 0;background:{0};color:#fff;'
                 'border-radius:4px;font-family:monospace;font-size:12px;">'
                 '<b>{1}</b> &nbsp; {2}</div>'.format(bg, t.label, detail))
    html += ('<hr><b style="font-family:sans-serif;">{0} model(s) cleaned, {1} '
             'failed.</b>'.format(ok, fail))

    if dialogs:
        answered = [d for d in dialogs if d[0]]
        unknown = [d for d in dialogs if not d[0]]
        html += ('<div style="margin-top:8px;padding:7px 11px;background:#37474f;'
                 'color:#fff;border-radius:4px;font-family:sans-serif;font-size:12px;">'
                 '<b>{0} Revit dialog(s) answered automatically</b>'.format(len(dialogs)))
        if unknown:
            # Named on purpose: an unrecognised dialog was answered
            # Cancel, which is safe but may not be what it needed. This
            # is the list to grow _DIALOG_ANSWERS from.
            seen = []
            for _ok, did, msg in unknown:
                text = (did or msg or "?")[:90]
                if text not in seen:
                    seen.append(text)
            html += ('<br>{0} were not recognised and were cancelled: {1}'.format(
                len(unknown), "; ".join(seen[:5])))
        html += "</div>"
    output.print_html(html)


def main():
    uiapp = __revit__
    app = uiapp.Application

    source = forms.CommandSwitchWindow.show(
        [SOURCE_SESSION, SOURCE_LOCAL, SOURCE_ACC],
        message="Where are the models you want to clean?")
    if not source:
        return

    if source == SOURCE_SESSION:
        targets = _session_targets(app)
    elif source == SOURCE_LOCAL:
        targets = _local_targets(app)
    else:
        targets = _acc_targets(app)
    if not targets:
        return

    live = [t for t in targets if t.doc is not None]
    if not live:
        _report(targets, {}, "nothing opened")
        return

    header = "{0} model(s) from: {1}".format(len(live), source)
    window = OptionsWindow(_XAML_FILE, header)
    window.ShowDialog()
    if not window.confirmed:
        return
    options = window.options

    if not forms.alert(
            "About to remove from {0} model(s):\n\n{1}\n\nThis cannot be undone inside "
            "the cleaned model. Nothing is saved until you choose at the end.\n\n"
            "Run it?".format(len(live), core.describe_options(options)),
            title=_TOOL, yes=True, no=True):
        return

    # Two different mechanisms, both needed. The preprocessor inside
    # dee_transmit_service answers failures raised INSIDE a transaction
    # (invalid dimension references, "Can't cut joined element",
    # duplicate Type Marks). This handler answers Revit's own native
    # dialogs, which never reach a preprocessor at all.
    dialogs = []
    handler = _make_dialog_handler(dialogs)
    uiapp.DialogBoxShowing += handler
    try:
        with _SafeProgress(title="DeeTransmit - cleaning...",
                               cancellable=True) as pb:
            for i, target in enumerate(live):
                if pb.cancelled:
                    target.error = "Cancelled before this model"
                    continue
                pb.update_progress(i, len(live))
                try:
                    target.result = core.clean_document(target.doc, options)
                except Exception as e:
                    target.error = "Cleaning failed: {0}".format(e)
    finally:
        uiapp.DialogBoxShowing -= handler

    finish_mode, folder = _finish(targets, live)
    if finish_mode not in _KEEP_OPEN:
        # The saving pass raises the same dialogs the cleaning pass did,
        # so it needs the same handler over it.
        uiapp.DialogBoxShowing += handler
        try:
            with _SafeProgress(title="DeeTransmit - saving...",
                                   cancellable=False) as pb:
                for i, target in enumerate(live):
                    pb.update_progress(i, len(live))
                    if target.error:
                        continue
                    note = _commit(target, finish_mode, folder)
                    if target.result:
                        target.result.notes.append(note)
        finally:
            uiapp.DialogBoxShowing -= handler

    # Only ever close what this tool opened, and only when the user did
    # NOT ask to keep them. A document the user already had open before
    # running DeeTransmit is never closed.
    closed = 0
    if finish_mode not in _KEEP_OPEN:
        for target in live:
            if not target.opened_by_us:
                continue
            if dm.close_document(target.doc, save_modified=False):
                closed += 1

    # "Let me look at it" is only deliverable by opening the saved copy:
    # the cleaned document itself is headless and Revit cannot promote a
    # background document into the UI.
    opened_copy = ""
    if finish_mode == FINISH_COPY_OPEN:
        for target in live:
            if target.saved_to and os.path.isfile(target.saved_to):
                try:
                    uiapp.OpenAndActivateDocument(target.saved_to)
                    opened_copy = target.saved_to
                    break
                except Exception as e:
                    opened_copy = "could not open the copy: {0}".format(e)
                    break

    _report(targets, options, finish_mode, dialogs)

    if finish_mode in _KEEP_OPEN:
        tail = ("The cleaned models are still loaded, but they were opened in the "
                "background so Revit shows no window for them - that is what let "
                "every view be deleted. To look at a result, run again and choose "
                "the option that saves a copy and opens it."
                if any(t.opened_by_us for t in live)
                else "Your models are exactly as the cleaning left them. Nothing was saved.")
    elif opened_copy:
        tail = "Opened for review: {0}".format(opened_copy)
    else:
        tail = ("{0} model(s) closed.".format(closed) if closed
                else "Nothing was closed.")

    MessageBox.Show(
        "DeeTransmit finished.\n\n{0} model(s) processed.\n"
        "{1} Revit dialog(s) answered automatically.\n\n{2}\n\n"
        "Full details are in the pyRevit output window.".format(
            len(live), len(dialogs), tail),
        _TOOL)


main()
