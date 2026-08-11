# -*- coding: utf-8 -*-
"""
DeeW.Clean (DeeW.Cloud)
Batch-cleans local and cloud Revit models: Purge Unused Elements,
delete Zero-Area Rooms, delete Unused Groups, and flag In-Place
Families for manual review - then saves (Standalone) or Synchronizes
With Central (workshared local files AND ACC cloud models) so the
cleanup actually reaches the shared model, not a disconnected copy.

--------------------------------------------------------------------
Architecture - IronPython 2, matching the rest of DeeW.Cloud
--------------------------------------------------------------------
Built directly on IronPython 2 from the start (unlike DeeW.Sharing/
Batch Save to Cloud, which were originally built for pyRevit's CPython
3 engine and rewritten after live-test crashes - see those tools'
module docstrings for that history). Shares every relevant lib/deew_*.py
service already proven by Sharing/Batch:
  deew_logger / deew_settings / deew_model_scanner / deew_failure_handler
  deew_progress_service / deew_report_generator (CleanReportRow, added
      here rather than repurposing the upload-oriented ReportRow, since
      this tool never uploads anything - it purges/deletes elements in
      place and saves/syncs the SAME file)
  acc_file_browser / deew_cloud_service - for the Cloud Models picker
      and get_token(), reused unchanged

New shared logic added for this tool:
  deew_clean_service.py - the actual purge/delete/count operations,
      adapted from DeeCleaner's own proven interactive scans (see that
      module's docstring for the exact detection rules reused).
  deew_document_manager.open_document_no_detach() / synchronize_with_central()
      / save_standalone() - DeeW.Clean deliberately does NOT detach
      before cleaning (unlike Sharing/Batch, whose whole point is an
      independent NEW cloud copy) - it opens the REAL central/cloud
      model directly so the cleanup reaches the actual shared file,
      then pushes changes back via Synchronize With Central. Property
      names for SynchronizeWithCentralOptions were verified against
      revitapidocs.com before writing, not guessed.

--------------------------------------------------------------------
DELIBERATELY CONSERVATIVE: In-Place Families are REPORT-ONLY
--------------------------------------------------------------------
Matches DeeCleaner's own restraint - an in-place family is real
modeled geometry, not an "unused" element by any Revit definition.
This tool counts and reports them but NEVER deletes them automatically,
even though every other check here does delete unattended - that
asymmetry is intentional, not an oversight.

--------------------------------------------------------------------
NEEDS LIVE-REVIT VERIFICATION (flagged, not silently assumed correct)
--------------------------------------------------------------------
- Document.GetUnusedElements is Revit 2024+ only (verified against
  revitapidocs.com) - guarded via hasattr(), degrades to "Purge
  skipped" on older Revit rather than crashing, but the actual
  behavior needs a live check on a real 2024+ session.
- Opening a file the scanner classified as "Central" (vs "Local Copy
  of Central") directly with DoNotDetach and then Synchronizing With
  Central from it is unusual - per deew_model_scanner.py's own
  documented limitation, a closed file's header can't reliably
  distinguish Central from Local Copy, so both are treated the same
  way here. Best practice is always working from a local copy; this
  is flagged rather than silently assumed safe.
- Cloud items opened via acc_file_browser.open_cloud_file() use
  OpenAndActivateDocument, which switches Revit's visible active tab
  to each file in turn while the batch runs - expected/proven behavior
  (same mechanism DeeOpener/DeeNWCs already use), not a bug, but worth
  knowing if you're watching Revit while a Cloud Models batch runs.
"""
import os
import time
import datetime

import clr
clr.AddReference("System.Windows.Forms")
from System.Windows.Forms import FolderBrowserDialog, OpenFileDialog, SaveFileDialog, DialogResult, MessageBox

from pyrevit import forms, script
import dee_branding

import deew_logger
import deew_settings
import deew_model_scanner as scanner
import deew_document_manager as docmgr
import deew_clean_service as cleansvc
import deew_failure_handler as ffh
import deew_progress_service as progsvc
import deew_report_generator as reportgen
import acc_file_browser as afb
import deew_cloud_service as cloudsvc

output = script.get_output()

_TOOL_NAME = "DeeWClean"
_TOOL_TITLE = "DeeW.Clean"

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")
_CACHE_FILE = os.path.join(_THIS_DIR, ".acc_file_cache.json")

_DEFAULT_SETTINGS = {
    "last_source_folder": "",
    "options": {},
}


def _revit_version_text(app):
    try:
        return str(app.VersionNumber)
    except Exception:
        return "Unknown"


class CloudCleanItem(object):
    """One row per cloud model added via "Add Cloud Models..." - plain
    fields matching acc_file_browser's existing pick_hub/pick_project/
    list_project_files return shapes, so no new ACC-browsing logic is
    needed here beyond calling those already-proven functions."""

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


class CleanPipeline(object):
    """Owns the actual per-file Open -> Clean -> Save/Sync -> Close
    pipeline, for both local and cloud sources - kept separate from
    the Window class per this repo's established one-tool-file
    convention (UI wiring vs. the real work)."""

    def __init__(self, application, options, logger):
        self.application = application
        self.options = options
        self.logger = logger

    def _apply_clean_result(self, row, clean_result):
        row.purged_count = clean_result.purged_count
        row.zero_area_rooms_deleted = clean_result.zero_area_rooms_deleted
        row.unused_groups_deleted = clean_result.unused_groups_deleted
        row.inplace_families_found = clean_result.inplace_families_found
        if clean_result.errors:
            row.errors = "; ".join(clean_result.errors)
        if not clean_result.purge_supported and self.options.get("purge_unused"):
            note = "Purge Unused needs Revit 2024+ (this session is {0})".format(
                _revit_version_text(self.application))
            row.warnings = (row.warnings + "; " + note) if row.warnings else note

    def _add_error(self, row, detail):
        row.errors = (row.errors + "; " + detail) if row.errors else detail

    def process_local(self, scanned_model):
        row = reportgen.CleanReportRow(
            scanned_model.file_name, scanned_model.file_path, "Local",
            scanned_model.model_type, _revit_version_text(self.application))
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

            clean_result = cleansvc.clean_document(document, self.options)
            self._apply_clean_result(row, clean_result)

            if docmgr.is_workshared(document):
                ok, detail = docmgr.synchronize_with_central(
                    document, comment=self.options.get("sync_comment", ""),
                    compact=self.options.get("compact_on_sync", False), logger=self.logger)
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
            self.logger.exception("Unexpected error cleaning file", e, file=scanned_model.file_name)
            return row
        finally:
            if document is not None and self.options.get("close_after", True):
                docmgr.close_document(document, save_modified=False, logger=self.logger)
            row.processing_time_seconds = time.time() - start

    def process_cloud(self, item, uiapp):
        row = reportgen.CleanReportRow(
            item.display_name, item.location_text, "Cloud", "Cloud Model",
            _revit_version_text(self.application))
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

            clean_result = cleansvc.clean_document(document, self.options)
            self._apply_clean_result(row, clean_result)

            ok, sync_detail = docmgr.synchronize_with_central(
                document, comment=self.options.get("sync_comment", ""),
                compact=self.options.get("compact_on_sync", False), logger=self.logger)
            row.save_status = "Synchronized with Central" if ok else "Failed - sync error"
            if not ok:
                self._add_error(row, sync_detail)

            return row
        except Exception as e:
            row.save_status = "Failed - unexpected error"
            row.errors = str(e)
            self.logger.exception("Unexpected error cleaning cloud file", e, file=item.display_name)
            return row
        finally:
            if ui_doc is not None and self.options.get("close_after", True):
                try:
                    docmgr.close_document(ui_doc.Document, save_modified=False, logger=self.logger)
                except Exception:
                    pass
            row.processing_time_seconds = time.time() - start


class DeeWCleanWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, uiapp):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.uiapp = uiapp
        self.application = uiapp.Application
        self.logger = deew_logger.DeeWLogger(_TOOL_NAME)
        self._models = []
        self._cloud_items = []
        self._report_rows = []
        self._status_lines = []
        self._dialog_handler = None

        saved = deew_settings.load(_TOOL_NAME, _DEFAULT_SETTINGS)
        if saved.get("last_source_folder") and os.path.isdir(saved["last_source_folder"]):
            self.source_folder_tb.Text = saved["last_source_folder"]
        self._apply_saved_options(saved.get("options", {}))

        self._log("Ready. Browse to a source folder and click Scan Folder, or Add Cloud Models.")

    # ---------------- persistence ----------------
    def _current_options(self):
        return {
            "purge_unused": bool(self.purge_unused_cb.IsChecked),
            "delete_zero_area_rooms": bool(self.delete_rooms_cb.IsChecked),
            "delete_unused_groups": bool(self.delete_groups_cb.IsChecked),
            "flag_inplace_families": bool(self.flag_inplace_cb.IsChecked),
            "sync_comment": self.sync_comment_tb.Text,
            "compact_on_sync": bool(self.compact_on_sync_cb.IsChecked),
            "audit": bool(self.audit_cb.IsChecked),
            "auto_resolve_dialogs": bool(self.auto_resolve_dialogs_cb.IsChecked),
            "generate_report": bool(self.generate_report_cb.IsChecked),
            "close_after": bool(self.close_after_cb.IsChecked),
        }

    def _apply_saved_options(self, options):
        try:
            self.purge_unused_cb.IsChecked = options.get("purge_unused", True)
            self.delete_rooms_cb.IsChecked = options.get("delete_zero_area_rooms", True)
            self.delete_groups_cb.IsChecked = options.get("delete_unused_groups", True)
            self.flag_inplace_cb.IsChecked = options.get("flag_inplace_families", True)
            self.sync_comment_tb.Text = options.get("sync_comment", "DeeW.Clean - automated batch cleanup")
            self.compact_on_sync_cb.IsChecked = options.get("compact_on_sync", False)
            self.audit_cb.IsChecked = options.get("audit", False)
            self.auto_resolve_dialogs_cb.IsChecked = options.get("auto_resolve_dialogs", True)
            self.generate_report_cb.IsChecked = options.get("generate_report", True)
            self.close_after_cb.IsChecked = options.get("close_after", True)
        except Exception:
            pass

    def _save_settings(self):
        deew_settings.save(_TOOL_NAME, {
            "last_source_folder": self.source_folder_tb.Text,
            "options": self._current_options(),
        })

    # ---------------- logging ----------------
    def _log(self, message):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self._status_lines.append("[{0}] {1}".format(ts, message))
        try:
            self.status_tb.Text = "\n".join(self._status_lines[-500:])
            self.status_tb.ScrollToEnd()
        except Exception:
            pass

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
        for path in dlg.FileNames:
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
            all_items = afb.list_project_files(hub_id, project_id, token, _CACHE_FILE)
            if not all_items:
                return
            picked_names = afb.pick_files_to_open(
                all_items, title="Select Cloud Models to Clean", button_name="Add Selected")
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
                CloudCleanItem(hub_id, hub_name, region, project_id, project_name, item_id, name, token))
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
        selected_local = [m for m in self._models if m.selected]
        selected_cloud = [i for i in self._cloud_items if i.selected]
        if not selected_local and not selected_cloud:
            forms.alert("Select at least one local file or cloud model first.")
            return

        options = self._current_options()
        if not (options["purge_unused"] or options["delete_zero_area_rooms"]
                or options["delete_unused_groups"] or options["flag_inplace_families"]):
            forms.alert("Check at least one cleaning operation in the Options tab first.")
            return
        self._save_settings()

        if options["auto_resolve_dialogs"]:
            self._dialog_handler = ffh.make_dialog_handler(self.logger)
            try:
                self.uiapp.DialogBoxShowing += self._dialog_handler
            except Exception as e:
                self.logger.exception("Could not attach dialog handler", e)

        pipeline = CleanPipeline(self.application, options, self.logger)
        total = len(selected_local) + len(selected_cloud)

        report_rows = []
        try:
            with progsvc.DeeWProgressService(_TOOL_TITLE, total) as prog:
                for model in selected_local:
                    if prog.cancelled:
                        self._log("Cancelled by user.")
                        break
                    prog.step(model.file_name, "Cleaning (local)")
                    self._log("Cleaning '{0}'...".format(model.file_name))
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
                        prog.step(item.display_name, "Cleaning (cloud)")
                        self._log("Cleaning cloud model '{0}'...".format(item.display_name))
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

        self._report_rows = report_rows
        self.report_grid.ItemsSource = None
        self.report_grid.ItemsSource = report_rows
        self._refresh_models_grid()
        self._refresh_cloud_grid()

        succeeded = sum(1 for r in report_rows if r.save_status in ("Saved", "Synchronized with Central"))
        failed = sum(1 for r in report_rows if "fail" in r.save_status.lower())
        skipped = sum(1 for r in report_rows if "skip" in r.save_status.lower())
        self.summary_tb.Text = (
            "{0} processed: {1} cleaned & saved, {2} failed, {3} skipped.".format(
                len(report_rows), succeeded, failed, skipped))
        self._log(self.summary_tb.Text)
        forms.alert(self.summary_tb.Text, title=_TOOL_TITLE)

    # ---------------- Export / Cancel ----------------
    def export_report_click(self, sender, args):
        if not self._report_rows:
            forms.alert("Run the tool first - nothing to export yet.")
            return
        dlg = SaveFileDialog()
        dlg.Filter = "Excel Workbook (*.xlsx)|*.xlsx|CSV (*.csv)|*.csv|Text (*.txt)|*.txt"
        dlg.FileName = "DeeWClean_Report.xlsx"
        if dlg.ShowDialog() != DialogResult.OK:
            return
        try:
            reportgen.export(
                dlg.FileName, "DeeW.Clean - Batch Cleanup Report", self._report_rows,
                headers=reportgen.CLEAN_REPORT_HEADERS, col_widths=reportgen.CLEAN_EXCEL_COL_WIDTHS)
        except Exception as e:
            forms.alert("Could not export report: {0}".format(e))
            return
        MessageBox.Show("Report exported to:\n{0}".format(dlg.FileName), _TOOL_TITLE)

    def cancel_click(self, sender, args):
        self._save_settings()
        self.Close()


def main():
    uiapp = __revit__
    window = DeeWCleanWindow(_XAML_FILE, uiapp)
    window.ShowDialog()


main()
