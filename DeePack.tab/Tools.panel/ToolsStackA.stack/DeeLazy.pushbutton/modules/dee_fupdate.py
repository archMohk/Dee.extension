# -*- coding: utf-8 -*-
"""
DeeLazy - DeeFUpdate module
Batch-pushes ONE Family into many other Revit files (local and/or ACC
cloud), overwriting the existing version wherever that family is already
present - "update the family everyone's using, everywhere it's used."

The Family can come from either an .rfa file on disk, or one already
loaded in the CURRENT project (extracted to a temp .rfa behind the scenes
via Document.EditFamily + SaveAs - the family in the current project is
never modified). Both source modes converge on the same local .rfa path
before the batch runs, so the batch loop itself doesn't care which mode
was used.

--------------------------------------------------------------------
Batch file processing - reused, not rebuilt
--------------------------------------------------------------------
Every "open a file, do something, save/sync, close" mechanic here is the
SAME lib/deew_*.py engine DeeW.Clean already proves live: deew_document_
manager (open_document_no_detach/synchronize_with_central/save_standalone/
close_document), deew_failure_handler (DeeWFailuresPreprocessor + the
native-dialog auto-dismiss handler, so a batch of many files doesn't hang
on a "family already exists" style prompt), deew_progress_service,
deew_report_generator, acc_file_browser/deew_cloud_service for the ACC
Cloud Models picker. The local+cloud target-file picker UI/logic is
copied directly from DeeWClean.pushbutton/script.py (browse/scan folder,
add files, add cloud models, one combined run loop, per-file try/except
that never aborts the whole batch on one failure).

--------------------------------------------------------------------
The one genuinely new piece: forced family overwrite
--------------------------------------------------------------------
Confirmed via a repo-wide search before writing this: no file anywhere in
this codebase references LoadFamily, IFamilyLoadOptions, FamilySource, or
OnFamilyFound. This module is the first.

NEEDS LIVE-REVIT VERIFICATION, explicitly, before trusting a real batch
run on production files:
- _OverwriteFamilyLoadOptions below implements IFamilyLoadOptions.
  IronPython's documented convention for implementing a .NET interface
  method that has `out` parameters: the out parameter is NOT part of the
  incoming call signature, and the method's return value is a tuple of
  (the real return value, the out value(s), in declaration order) - that
  is what's written here. If this convention is wrong for this specific
  IronPython/Revit API combination, the very first live test (a single
  file, single family) will fail loudly with a clear exception rather
  than silently misbehaving - test that one case first, watched closely,
  before running a real batch.
- overwriteParameterValues is hard-set to True (fully overwrite, including
  any instance parameter values already customized in a target file) -
  matches "accept the update... and override it" read as "the new family
  should fully win." Worth confirming live that this is really what's
  wanted before trusting it against files with hand-tuned values.
- Document.EditFamily + temp-file SaveAs (the "pick from current project"
  source mode) is standard, documented Revit API, but opening a second
  Document mid-tool is new to this codebase.
"""
import os
import time
import datetime
import tempfile

import clr
clr.AddReference("System.Windows.Forms")
from System.Windows.Forms import FolderBrowserDialog, OpenFileDialog, DialogResult, SaveFileDialog, MessageBox

from pyrevit import forms
import dee_branding

from Autodesk.Revit.DB import (
    FilteredElementCollector, Family, Transaction, SaveAsOptions,
    IFamilyLoadOptions, FamilySource,
)

import deew_logger
import deew_model_scanner as scanner
import deew_document_manager as docmgr
import deew_failure_handler as ffh
import deew_progress_service as progsvc
import deew_report_generator as reportgen
import acc_file_browser as afb
import deew_cloud_service as cloudsvc

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "DeeFUpdate.xaml")
_TOOL_NAME = "DeeFUpdate"
_TOOL_TITLE = "DeeFUpdate"
_CACHE_FILE = os.path.join(_THIS_DIR, ".deefupdate_acc_cache.json")


def _revit_version_text(app):
    try:
        return str(app.VersionNumber)
    except Exception:
        return "Unknown"


class _SafeProgress(object):
    """forms.ProgressBar tries to set Window.TaskbarItemInfo on its host
    window - throws NotImplementedException under Remote Desktop/no
    taskbar (live-confirmed in DeeSheetLinks). Falls back to no progress
    UI at all rather than crashing."""
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


class CloudFUpdateItem(object):
    """One row per cloud model added via "Add Cloud Models..." - same
    shape as DeeWClean's CloudCleanItem, matching acc_file_browser's
    pick_hub/pick_project/list_project_files return values directly."""

    def __init__(self, hub_id, hub_name, region, project_id, project_name, item_id, display_name, token):
        self.selected = False
        self.hub_id = hub_id
        self.hub_name = hub_name
        self.region = region
        self.project_id = project_id
        self.project_name = project_name
        self.item_id = item_id
        self.display_name = display_name
        self.token = token
        self.status = "Ready"

    @property
    def location_text(self):
        return "{0} / {1}".format(self.hub_name, self.project_name)


class _OverwriteFamilyLoadOptions(IFamilyLoadOptions):
    """Always answers "overwrite the existing family, including its
    parameter values" - see the module docstring for the IronPython
    out-parameter convention this relies on, flagged for live
    verification."""

    def OnFamilyFound(self, familyInUse):
        return True, True

    def OnSharedFamilyFound(self, sharedFamily, familyInUse):
        return True, FamilySource.Family, True


def _load_family_into(document, rfa_path, logger=None):
    """Loads/overwrites `rfa_path` into `document` inside its own
    Transaction, with the same DeeWFailuresPreprocessor every other
    DeeW.Cloud batch operation uses so a duplicate-type-style warning
    during the load doesn't pop a modal. Returns (ok, detail)."""
    t = Transaction(document, "DeeFUpdate - Load Family")
    t.Start()
    try:
        ffh.apply_to_transaction(t, logger)
        result, _loaded_family = document.LoadFamily(rfa_path, _OverwriteFamilyLoadOptions())
        t.Commit()
        if result:
            return True, "Family loaded"
        return False, "Revit refused to load the family (LoadFamily returned False)"
    except Exception as e:
        t.RollBack()
        return False, str(e)


class FUpdatePipeline(object):
    """Owns the actual per-file Open -> Load Family -> Save/Sync -> Close
    pipeline, for both local and cloud sources - same shape as DeeWClean's
    CleanPipeline, swapping the clean step for _load_family_into."""

    def __init__(self, application, rfa_path, family_display_name, options, logger):
        self.application = application
        self.rfa_path = rfa_path
        self.family_display_name = family_display_name
        self.options = options
        self.logger = logger

    def _add_error(self, row, detail):
        row.errors = (row.errors + "; " + detail) if row.errors else detail

    def process_local(self, scanned_model):
        row = reportgen.FUpdateReportRow(
            scanned_model.file_name, scanned_model.file_path, "Local",
            self.family_display_name, _revit_version_text(self.application))
        start = time.time()
        document = None
        try:
            if scanned_model.model_type == scanner.MODEL_TYPE_CLOUD:
                row.save_status = "Skipped - already a cloud model (use Add Cloud Models... instead)"
                return row
            if scanned_model.model_type in (scanner.MODEL_TYPE_CORRUPTED, scanner.MODEL_TYPE_READ_ONLY):
                row.save_status = "Skipped - {0}".format(scanned_model.status)
                row.errors = scanned_model.error
                return row

            document, err = docmgr.open_document_no_detach(
                self.application, scanned_model.file_path,
                audit=self.options.get("audit", False), open_all_worksets=True, logger=self.logger)
            if document is None:
                row.save_status = "Failed - could not open"
                row.errors = err
                return row

            ok, detail = _load_family_into(document, self.rfa_path, self.logger)
            if not ok:
                row.save_status = "Failed - load error"
                self._add_error(row, detail)
                return row

            if docmgr.is_workshared(document):
                ok, detail = docmgr.synchronize_with_central(
                    document, comment=self.options.get("sync_comment", ""), compact=False, logger=self.logger)
                row.save_status = "Synchronized with Central" if ok else "Failed - sync error"
                if not ok:
                    self._add_error(row, detail)
            else:
                ok, detail = docmgr.save_standalone(document, logger=self.logger)
                row.save_status = "Saved" if ok else "Failed - save error"
                if not ok:
                    self._add_error(row, detail)

            return row
        except Exception as e:
            row.save_status = "Failed - unexpected error"
            row.errors = str(e)
            self.logger.exception("Unexpected error updating family in file", e, file=scanned_model.file_name)
            return row
        finally:
            if document is not None:
                docmgr.close_document(document, save_modified=False, logger=self.logger)
            row.processing_time_seconds = time.time() - start

    def process_cloud(self, item, uiapp):
        row = reportgen.FUpdateReportRow(
            item.display_name, item.location_text, "Cloud",
            self.family_display_name, _revit_version_text(self.application))
        start = time.time()
        ui_doc = None
        try:
            ui_doc, detail = afb.open_cloud_file(
                uiapp, item.region, item.project_id, item.item_id, item.token, close_worksets=False)
            if ui_doc is None:
                row.save_status = "Failed - could not open"
                row.errors = detail
                return row
            document = ui_doc.Document

            ok, detail = _load_family_into(document, self.rfa_path, self.logger)
            if not ok:
                row.save_status = "Failed - load error"
                self._add_error(row, detail)
                return row

            ok, sync_detail = docmgr.synchronize_with_central(
                document, comment=self.options.get("sync_comment", ""), compact=False, logger=self.logger)
            row.save_status = "Synchronized with Central" if ok else "Failed - sync error"
            if not ok:
                self._add_error(row, sync_detail)

            return row
        except Exception as e:
            row.save_status = "Failed - unexpected error"
            row.errors = str(e)
            self.logger.exception("Unexpected error updating family in cloud file", e, file=item.display_name)
            return row
        finally:
            if ui_doc is not None:
                try:
                    docmgr.close_document(ui_doc.Document, save_modified=False, logger=self.logger)
                except Exception:
                    pass
            row.processing_time_seconds = time.time() - start


def _collect_families(doc):
    """{category_name: {family_name: Family}} - Family, not FamilySymbol
    (unlike dee_block_to_family_service.py's type-level index): loading a
    family loads all of its types at once, so only the Family itself
    needs identifying here."""
    index = {}
    for fam in FilteredElementCollector(doc).OfClass(Family):
        try:
            cat = fam.FamilyCategory
            cat_name = cat.Name if cat is not None else "(uncategorized)"
            name = fam.Name
            if not name:
                continue
            index.setdefault(cat_name, {})[name] = fam
        except Exception:
            continue
    return index


class DeeFUpdateWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, uiapp):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.uiapp = uiapp
        self.application = uiapp.Application
        self.doc = uiapp.ActiveUIDocument.Document
        self.logger = deew_logger.DeeWLogger(_TOOL_NAME)
        self._models = []
        self._cloud_items = []
        self._report_rows = []
        self._status_lines = []
        self._dialog_handler = None
        self._family_index = _collect_families(self.doc)
        self._resolved_rfa_path = None
        self._temp_rfa_path = None

        self.category_cb.ItemsSource = sorted(self._family_index.keys())
        self.from_file_rb.IsChecked = True

        self._log("Ready. Pick a Family source, add target files, then Run.")

    # ---------------- logging ----------------
    def _log(self, message):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self._status_lines.append("[{0}] {1}".format(ts, message))
        try:
            self.status_tb.Text = "\n".join(self._status_lines[-500:])
            self.status_tb.ScrollToEnd()
        except Exception:
            pass

    # ---------------- Family source ----------------
    def family_source_changed(self, sender, args):
        try:
            from_file = bool(self.from_file_rb.IsChecked)
            self.file_path_tb.IsEnabled = from_file
            self.browse_family_b.IsEnabled = from_file
            self.category_cb.IsEnabled = not from_file
            self.family_cb.IsEnabled = not from_file
        except Exception:
            pass

    def category_changed(self, sender, args):
        cat_name = self.category_cb.SelectedItem
        names = sorted(self._family_index.get(cat_name, {}).keys()) if cat_name else []
        self.family_cb.ItemsSource = names

    def browse_family_click(self, sender, args):
        dlg = OpenFileDialog()
        dlg.Filter = "Revit Family (*.rfa)|*.rfa"
        dlg.Title = "Pick a Family"
        if dlg.ShowDialog() != DialogResult.OK:
            return
        self.file_path_tb.Text = dlg.FileName
        self.family_status_tb.Text = "Selected: {0}".format(os.path.basename(dlg.FileName))

    def _export_family_to_temp(self, family):
        """Opens `family` for editing (a NEW, separate Document) and
        SaveAs's it to a temp .rfa - the family in the current project is
        never modified. Returns (path_or_None, detail)."""
        family_doc = None
        try:
            family_doc = self.doc.EditFamily(family)
        except Exception as e:
            return None, str(e)
        try:
            safe_name = "".join(c for c in family.Name if c.isalnum() or c in (" ", "_", "-")).strip()
            safe_name = safe_name or "DeeFUpdate_Family"
            temp_path = os.path.join(tempfile.gettempdir(), "{0}_DeeFUpdate.rfa".format(safe_name))
            options = SaveAsOptions()
            options.OverwriteExistingFile = True
            family_doc.SaveAs(temp_path, options)
            return temp_path, ""
        except Exception as e:
            return None, str(e)
        finally:
            try:
                family_doc.Close(False)
            except Exception:
                pass

    def _resolve_family_source(self):
        """Returns (rfa_path_or_None, display_name_or_None, error_or_None)."""
        if bool(self.from_file_rb.IsChecked):
            path = self.file_path_tb.Text
            if not path or not os.path.isfile(path):
                return None, None, "Pick a valid .rfa file first."
            return path, os.path.basename(path), None

        cat_name = self.category_cb.SelectedItem
        fam_name = self.family_cb.SelectedItem
        if not cat_name or not fam_name:
            return None, None, "Pick a Category and Family first."
        family = self._family_index.get(cat_name, {}).get(fam_name)
        if family is None:
            return None, None, "That family could not be found - rescan by reopening this window."
        self.family_status_tb.Text = "Exporting '{0}' to a temporary file...".format(fam_name)
        temp_path, err = self._export_family_to_temp(family)
        if temp_path is None:
            return None, None, "Could not export the selected family: {0}".format(err)
        self._temp_rfa_path = temp_path
        return temp_path, fam_name, None

    # ---------------- Local files ----------------
    def browse_click(self, sender, args):
        dlg = FolderBrowserDialog()
        if self.source_folder_tb.Text and os.path.isdir(self.source_folder_tb.Text):
            dlg.SelectedPath = self.source_folder_tb.Text
        if dlg.ShowDialog() == DialogResult.OK:
            self.source_folder_tb.Text = dlg.SelectedPath

    def scan_click(self, sender, args):
        folder = self.source_folder_tb.Text
        if not folder or not os.path.isdir(folder):
            forms.alert("Pick a valid source folder first.")
            return
        self._log("Scanning '{0}' for RVT files...".format(folder))
        recursive = bool(self.recursive_cb.IsChecked)
        with progsvc.DeeWProgressService(_TOOL_TITLE, 1) as prog:
            def progress_cb(i, total, name):
                prog.total_files = max(total, 1)
                prog.step(name, "Scanning", index=i)
            models = scanner.scan_folder(folder, recursive=recursive, progress_cb=progress_cb)
        scanner.annotate_version_mismatch(models, _revit_version_text(self.application))
        self._models = models
        self._refresh_models_grid()
        self.scan_status_tb.Text = "{0} local RVT file(s) found.".format(len(models))
        self._log("Scan complete: {0} local file(s).".format(len(models)))

    def add_files_click(self, sender, args):
        dlg = OpenFileDialog()
        dlg.Filter = "Revit Files (*.rvt)|*.rvt"
        dlg.Multiselect = True
        dlg.Title = "Add individual Revit files"
        if dlg.ShowDialog() != DialogResult.OK:
            return
        existing_paths = set(m.file_path for m in self._models)
        added = 0
        with _SafeProgress(title="DeeFUpdate - scanning {value} of {max_value}...") as pb:
            for i, path in enumerate(dlg.FileNames):
                pb.update_progress(i, len(dlg.FileNames))
                if path in existing_paths:
                    continue
                self._models.append(scanner.scan_file(path))
                existing_paths.add(path)
                added += 1
        scanner.annotate_version_mismatch(self._models, _revit_version_text(self.application))
        self._refresh_models_grid()
        self.scan_status_tb.Text = "{0} local RVT file(s) in list.".format(len(self._models))
        self._log("Added {0} local file(s) individually.".format(added))

    def select_all_click(self, sender, args):
        for m in self._models:
            m.selected = True
        self._refresh_models_grid()

    def select_none_click(self, sender, args):
        for m in self._models:
            m.selected = False
        self._refresh_models_grid()

    def _refresh_models_grid(self):
        self.models_grid.ItemsSource = None
        self.models_grid.ItemsSource = self._models

    # ---------------- Cloud models ----------------
    def add_cloud_click(self, sender, args):
        try:
            token = cloudsvc.get_token()
            hub = afb.pick_hub(token)
            if not hub:
                return
            hub_id, region, hub_name = hub
            project = afb.pick_project(hub_id, token)
            if not project:
                return
            project_id, project_name = project
            self._log("Loading cloud model list for '{0}'...".format(project_name))
            with _SafeProgress(title="DeeFUpdate - loading cloud model list...", indeterminate=True):
                all_items = afb.list_project_files(hub_id, project_id, token, _CACHE_FILE)
            if not all_items:
                return
            picked_names = afb.pick_files_to_open(
                all_items, title="Select Cloud Models to Update", button_name="Add Selected")
            if not picked_names:
                return
        except Exception as e:
            forms.alert("Could not load ACC cloud models: {0}".format(e))
            return

        existing_ids = set(i.item_id for i in self._cloud_items)
        added = 0
        for name in picked_names:
            item_id = all_items[name]
            if item_id in existing_ids:
                continue
            self._cloud_items.append(
                CloudFUpdateItem(hub_id, hub_name, region, project_id, project_name, item_id, name, token))
            existing_ids.add(item_id)
            added += 1
        self._refresh_cloud_grid()
        self._log("Added {0} cloud model(s) ({1} already in list).".format(added, len(picked_names) - added))

    def select_all_cloud_click(self, sender, args):
        for i in self._cloud_items:
            i.selected = True
        self._refresh_cloud_grid()

    def select_none_cloud_click(self, sender, args):
        for i in self._cloud_items:
            i.selected = False
        self._refresh_cloud_grid()

    def _refresh_cloud_grid(self):
        self.cloud_grid.ItemsSource = None
        self.cloud_grid.ItemsSource = self._cloud_items

    # ---------------- Run ----------------
    def run_click(self, sender, args):
        rfa_path, family_display_name, err = self._resolve_family_source()
        if rfa_path is None:
            forms.alert(err)
            return
        self.family_status_tb.Text = "Using: {0}".format(family_display_name)

        selected_local = [m for m in self._models if m.selected]
        selected_cloud = [i for i in self._cloud_items if i.selected]
        if not selected_local and not selected_cloud:
            forms.alert("Select at least one local file or cloud model on the Target Files tab first.")
            return

        total = len(selected_local) + len(selected_cloud)
        if not forms.alert(
                "Load '{0}' into {1} file(s), overwriting the existing version (including its "
                "parameter values) wherever that family is already present?\n\nThis is SAVED/"
                "SYNCHRONIZED back to the real files - it cannot be undone from here.".format(
                    family_display_name, total),
                title=_TOOL_TITLE + " - confirm", yes=True, no=True):
            return

        options = {
            "audit": bool(self.audit_cb.IsChecked),
            "sync_comment": self.sync_comment_tb.Text,
            "auto_resolve_dialogs": bool(self.auto_resolve_dialogs_cb.IsChecked),
        }

        if options["auto_resolve_dialogs"]:
            self._dialog_handler = ffh.make_dialog_handler(self.logger)
            try:
                self.uiapp.DialogBoxShowing += self._dialog_handler
            except Exception as e:
                self.logger.exception("Could not attach dialog handler", e)

        pipeline = FUpdatePipeline(self.application, rfa_path, family_display_name, options, self.logger)

        report_rows = []
        try:
            with progsvc.DeeWProgressService(_TOOL_TITLE, total) as prog:
                for model in selected_local:
                    if prog.cancelled:
                        self._log("Cancelled by user.")
                        break
                    prog.step(model.file_name, "Updating family (local)")
                    self._log("Updating '{0}'...".format(model.file_name))
                    row = pipeline.process_local(model)
                    report_rows.append(row)
                    model.status = row.save_status
                    outcome = ("success" if row.save_status in ("Saved", "Synchronized with Central")
                               else ("skipped" if "skip" in row.save_status.lower() else "failed"))
                    prog.finish_file(outcome)
                    self._log("'{0}': {1}".format(model.file_name, row.save_status))

                if not prog.cancelled:
                    for item in selected_cloud:
                        if prog.cancelled:
                            self._log("Cancelled by user.")
                            break
                        prog.step(item.display_name, "Updating family (cloud)")
                        self._log("Updating cloud model '{0}'...".format(item.display_name))
                        row = pipeline.process_cloud(item, self.uiapp)
                        report_rows.append(row)
                        item.status = row.save_status
                        outcome = ("success" if row.save_status == "Synchronized with Central"
                                   else ("skipped" if "skip" in row.save_status.lower() else "failed"))
                        prog.finish_file(outcome)
                        self._log("'{0}': {1}".format(item.display_name, row.save_status))
        finally:
            if self._dialog_handler is not None:
                try:
                    self.uiapp.DialogBoxShowing -= self._dialog_handler
                except Exception:
                    pass
                self._dialog_handler = None
            if self._temp_rfa_path and os.path.isfile(self._temp_rfa_path):
                try:
                    os.remove(self._temp_rfa_path)
                except Exception:
                    pass
                self._temp_rfa_path = None

        self._report_rows = report_rows
        self.report_grid.ItemsSource = None
        self.report_grid.ItemsSource = report_rows
        self._refresh_models_grid()
        self._refresh_cloud_grid()

        succeeded = sum(1 for r in report_rows if r.save_status in ("Saved", "Synchronized with Central"))
        failed = sum(1 for r in report_rows if "fail" in r.save_status.lower())
        skipped = sum(1 for r in report_rows if "skip" in r.save_status.lower())
        self.summary_tb.Text = "{0} processed: {1} updated, {2} failed, {3} skipped.".format(
            len(report_rows), succeeded, failed, skipped)
        self._log(self.summary_tb.Text)
        forms.alert(self.summary_tb.Text, title=_TOOL_TITLE)

    # ---------------- Export / Close ----------------
    def export_report_click(self, sender, args):
        if not self._report_rows:
            forms.alert("Run the tool first - nothing to export yet.")
            return
        dlg = SaveFileDialog()
        dlg.Filter = "Excel Workbook (*.xlsx)|*.xlsx|CSV (*.csv)|*.csv|Text (*.txt)|*.txt"
        dlg.FileName = "DeeFUpdate_Report.xlsx"
        if dlg.ShowDialog() != DialogResult.OK:
            return
        try:
            reportgen.export(
                dlg.FileName, "DeeFUpdate - Batch Family Update Report", self._report_rows,
                headers=reportgen.FUPDATE_REPORT_HEADERS, col_widths=reportgen.FUPDATE_EXCEL_COL_WIDTHS)
        except Exception as e:
            forms.alert("Could not export report: {0}".format(e))
            return
        MessageBox.Show("Report exported to:\n{0}".format(dlg.FileName), _TOOL_TITLE)

    def close_click(self, sender, args):
        self.Close()


# ==========================================================================
# Launch entry point (called by the DeeLazy home window)
# ==========================================================================
def launch(uiapp):
    if uiapp.ActiveUIDocument is None:
        forms.alert("Open a Revit project first.")
        return
    window = DeeFUpdateWindow(_XAML_FILE, uiapp)
    window.ShowDialog()


TOOL_INFO = {
    "id": "dee_fupdate",
    "title": "DeeFUpdate",
    "description": "Batch-push a Family into many other Revit files (local and/or ACC cloud), overwriting the existing version wherever it's already present.",
    "launch": launch,
}
