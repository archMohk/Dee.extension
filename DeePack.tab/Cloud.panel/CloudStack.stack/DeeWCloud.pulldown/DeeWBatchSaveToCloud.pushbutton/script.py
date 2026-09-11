# -*- coding: utf-8 -*-
"""
DeeW.Batch Save to Cloud (DeeW.Cloud)
Bulk-converts hundreds of local Revit models into Autodesk
Construction Cloud Cloud Models, auto-detecting the correct upload
mode per file:
  Mode 1: Local RVT -> ACC Cloud Model
  Mode 2: Central RVT -> Detach -> ACC Cloud Model
  Mode 3: Local Copy of Central -> Detach -> ACC Cloud Model

Optimized for large batches: only one Revit Document is ever open at
a time, closed and released before the next file starts (spec:
"Never keep multiple Revit documents open simultaneously... Release
memory after each document").

--------------------------------------------------------------------
Architecture - IronPython 2, not CPython 3 (history below)
--------------------------------------------------------------------
This package was originally built to run under pyRevit's CPython 3
engine. Live testing confirmed that engine crashes at the .NET level
before any script code runs (two different failures, in Revit 2024
and Revit 2026 respectively) - see DeeWSharing.pushbutton/script.py's
module docstring for the full explanation. This file (and the rest of
DeeW.Cloud) now runs on pyRevit's default IronPython 2 engine instead,
matching every other tool in Dee.extension.

Shares every lib/deew_*.py service with DeeW.Sharing (logging,
settings, model scanning, document lifecycle, cloud upload, dialog/
failure suppression, progress, reporting) - this file's own
responsibility is the batch-specific orchestration (duplicate
handling with 3 modes instead of 2, local backup cleanup) and its WPF
UI controller, not a copy of DeeW.Sharing's pipeline.

--------------------------------------------------------------------
"Delete Local Backup" - a deliberately CONSERVATIVE interpretation
--------------------------------------------------------------------
The spec lists this option without further detail. Implemented here as
deleting Revit's OWN auto-generated backup files (the well-known
"<name>.####.rvt" numbered backups Revit creates next to a workshared
file) after a successful upload - NEVER the user's original source
file itself. Deleting the user's actual source model would be a
categorically more dangerous action than cleaning up disposable
backup copies, and the spec's wording ("Local Backup", not "Local
File" or "Source File") supports this reading.
"""
import os
import re
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
import deew_cloud_service as cloudsvc
import deew_failure_handler as ffh
import deew_progress_service as progsvc
import deew_report_generator as reportgen
import acc_api
import dee_telemetry
dee_telemetry.check_access("DeeWBatchSaveToCloud")


output = script.get_output()

_TOOL_NAME = "DeeWBatchSaveToCloud"
_TOOL_TITLE = "DeeW.Batch Save to Cloud"

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")

_DEFAULT_SETTINGS = {
    "last_source_folder": "",
    "last_destination": None,
    "options": {},
}

_BACKUP_PATTERN = re.compile(r"^(.+)\.\d{4}\.rvt$", re.IGNORECASE)


def _revit_version_text(app):
    try:
        return str(app.VersionNumber)
    except Exception:
        return "Unknown"


def _delete_local_backups(file_path, logger):
    """Deletes only files matching Revit's own numbered-backup naming
    pattern in the SAME folder as `file_path` - never the file itself.
    Best-effort: a locked/permission-denied backup file is skipped,
    not raised, since cleanup failing must never fail the whole batch
    entry for that model (the upload already succeeded by the time
    this runs)."""
    try:
        folder = os.path.dirname(file_path)
        base_name = os.path.splitext(os.path.basename(file_path))[0]
        removed = 0
        for entry in os.listdir(folder):
            match = _BACKUP_PATTERN.match(entry)
            if match and match.group(1).lower() == base_name.lower():
                try:
                    os.remove(os.path.join(folder, entry))
                    removed += 1
                except Exception:
                    pass
        if logger is not None and removed:
            logger.info("Deleted local backup file(s)", file=file_path, count=removed)
        return removed
    except Exception as e:
        if logger is not None:
            logger.exception("Backup cleanup failed", e, file=file_path)
        return 0


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


class BatchPipeline(object):
    """Per-file Validate -> Determine -> Open -> Detach/Worksharing ->
    Save-to-Cloud -> Verify -> Close pipeline for batch runs. Deliberately
    a SEPARATE class from DeeWSharing's SharingPipeline (each tool's
    own script.py owns its own orchestration, per this repo's
    established one-file-per-tool convention) even though both call
    into the exact same shared lib/deew_*.py services underneath -
    only the duplicate-handling policy (3 modes here vs 2 in
    DeeW.Sharing) and the backup-cleanup step differ."""

    def __init__(self, application, destination, options, logger):
        self.application = application
        self.destination = destination
        self.options = options
        self.logger = logger
        self._cloud_model_names_cache = None

    def _existing_cloud_names(self, token):
        if self._cloud_model_names_cache is None:
            try:
                items = acc_api.search_cloud_models(self.destination["project_id"], token)
                self._cloud_model_names_cache = set(name for _id, name in items)
            except Exception as e:
                self.logger.exception("Could not list existing cloud models", e)
                self._cloud_model_names_cache = set()
        return self._cloud_model_names_cache

    def _resolve_model_name(self, scanned_model, token):
        base_name = os.path.splitext(scanned_model.file_name)[0]
        existing = self._existing_cloud_names(token)
        if base_name not in existing:
            return base_name, None

        if self.options.get("skip_existing"):
            return None, "Skipped - a cloud model named '{0}' already exists".format(base_name)
        if self.options.get("overwrite_existing"):
            # See module docstring's counterpart in DeeW.Sharing - true
            # overwrite (deleting the existing cloud item) requires an
            # unverified destructive Data Management API DELETE call
            # this package does not implement; skip rather than risk it.
            return None, "Skipped - a cloud model named '{0}' already exists (overwrite not performed)".format(base_name)

        n = 2
        candidate = "{0} ({1})".format(base_name, n)
        while candidate in existing:
            n += 1
            candidate = "{0} ({1})".format(base_name, n)
        return candidate, None

    def _detach_option(self, scanned_model):
        if scanned_model.model_type in (scanner.MODEL_TYPE_CENTRAL, scanner.MODEL_TYPE_LOCAL):
            return "preserve" if self.options.get("detach") else "none"
        return "none"

    def process_one(self, scanned_model, token):
        row = reportgen.ReportRow(
            scanned_model.file_name, scanned_model.file_path,
            _revit_version_text(self.application), scanned_model.model_type)
        row.warnings = scanned_model.version_warning
        start = time.time()
        document = None
        try:
            if scanned_model.model_type == scanner.MODEL_TYPE_CLOUD:
                row.upload_status = "Skipped - already a cloud model"
                return row
            if scanned_model.model_type in (scanner.MODEL_TYPE_CORRUPTED, scanner.MODEL_TYPE_READ_ONLY):
                row.upload_status = "Skipped - {0}".format(scanned_model.status)
                row.errors = scanned_model.error
                return row

            model_name, skip_reason = self._resolve_model_name(scanned_model, token)
            if skip_reason:
                row.upload_status = skip_reason
                return row
            row.cloud_model_name = model_name + ".rvt"

            detach_option = self._detach_option(scanned_model)
            document, err = docmgr.open_document(
                self.application, scanned_model.file_path,
                detach_option=detach_option, audit=self.options.get("audit", False),
                open_all_worksets=self.options.get("open_all_worksets", False),
                logger=self.logger)
            if document is None:
                row.upload_status = "Failed - could not open"
                row.errors = err
                return row

            if docmgr.is_cloud_model(document):
                row.upload_status = "Skipped - already a cloud model"
                return row

            if self.options.get("enable_worksharing"):
                ok = docmgr.enable_worksharing(document, logger=self.logger)
                row.worksharing_enabled = "Yes" if ok else "Failed"
            else:
                row.worksharing_enabled = "Yes" if docmgr.is_workshared(document) else "No"

            if self.options.get("compact"):
                docmgr.compact_and_save_local(document, logger=self.logger)

            row.acc_hub = self.destination.get("hub_name", "")
            row.acc_project = self.destination.get("project_name", "")
            row.acc_folder = self.destination.get("folder_name", "")

            success, detail = cloudsvc.save_to_cloud(document, self.destination, model_name)
            if success:
                row.upload_status = "Uploaded"
                row.cloud_guid = reportgen.get_cloud_model_guid(document)
                if self.options.get("delete_local_backup"):
                    removed = _delete_local_backups(scanned_model.file_path, self.logger)
                    if removed:
                        note = "Removed {0} local backup file(s)".format(removed)
                        row.warnings = "{0}; {1}".format(row.warnings, note) if row.warnings else note
            else:
                row.upload_status = "Failed - upload error"
                row.errors = detail

            return row
        except Exception as e:
            row.upload_status = "Failed - unexpected error"
            row.errors = str(e)
            self.logger.exception("Unexpected error processing file", e, file=scanned_model.file_name)
            return row
        finally:
            if document is not None and self.options.get("close_after", True):
                docmgr.close_document(document, save_modified=False, logger=self.logger)
            row.processing_time_seconds = time.time() - start


class DeeWBatchWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, uiapp):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.uiapp = uiapp
        self.application = uiapp.Application
        self.logger = deew_logger.DeeWLogger(_TOOL_NAME)
        self._models = []
        self._destination = None
        self._report_rows = []
        self._status_lines = []
        self._dialog_handler = None

        saved = deew_settings.load(_TOOL_NAME, _DEFAULT_SETTINGS)
        if saved.get("last_source_folder") and os.path.isdir(saved["last_source_folder"]):
            self.source_folder_tb.Text = saved["last_source_folder"]
        if saved.get("last_destination"):
            self._destination = saved["last_destination"]
            self._refresh_destination_text()
        self._apply_saved_options(saved.get("options", {}))

        self._log("Ready. Browse to a source folder and click Scan Folder.")

    # ---------------- persistence ----------------
    def _current_options(self):
        return {
            "enable_worksharing": bool(self.enable_worksharing_cb.IsChecked),
            "detach": bool(self.detach_cb.IsChecked),
            "audit": bool(self.audit_cb.IsChecked),
            "compact": bool(self.compact_cb.IsChecked),
            "open_all_worksets": bool(self.open_all_worksets_cb.IsChecked),
            "skip_existing": bool(self.skip_existing_rb.IsChecked),
            "overwrite_existing": bool(self.overwrite_existing_rb.IsChecked),
            "delete_local_backup": bool(self.delete_local_backup_cb.IsChecked),
            "generate_report": bool(self.generate_report_cb.IsChecked),
            "close_after": bool(self.close_after_cb.IsChecked),
            "auto_resolve_dialogs": bool(self.auto_resolve_dialogs_cb.IsChecked),
        }

    def _apply_saved_options(self, options):
        try:
            self.enable_worksharing_cb.IsChecked = options.get("enable_worksharing", True)
            self.detach_cb.IsChecked = options.get("detach", True)
            self.audit_cb.IsChecked = options.get("audit", False)
            self.compact_cb.IsChecked = options.get("compact", False)
            self.open_all_worksets_cb.IsChecked = options.get("open_all_worksets", True)
            self.skip_existing_rb.IsChecked = options.get("skip_existing", True)
            self.overwrite_existing_rb.IsChecked = options.get("overwrite_existing", False)
            self.rename_duplicates_rb.IsChecked = not (
                options.get("skip_existing", True) or options.get("overwrite_existing", False))
            self.delete_local_backup_cb.IsChecked = options.get("delete_local_backup", False)
            self.generate_report_cb.IsChecked = options.get("generate_report", True)
            self.close_after_cb.IsChecked = options.get("close_after", True)
            self.auto_resolve_dialogs_cb.IsChecked = options.get("auto_resolve_dialogs", True)
        except Exception:
            pass

    def _save_settings(self):
        deew_settings.save(_TOOL_NAME, {
            "last_source_folder": self.source_folder_tb.Text,
            "last_destination": self._destination,
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

    # ---------------- Source Models tab ----------------
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
        self.models_grid.ItemsSource = None
        self.models_grid.ItemsSource = models
        self.scan_status_tb.Text = "{0} RVT file(s) found.".format(len(models))
        self._log("Scan complete: {0} file(s).".format(len(models)))

    def add_files_click(self, sender, args):
        dlg = OpenFileDialog()
        dlg.Filter = "Revit Files (*.rvt)|*.rvt"
        dlg.Multiselect = True
        dlg.Title = "Add individual Revit files"
        if dlg.ShowDialog() != DialogResult.OK:
            return
        existing_paths = set(m.file_path for m in self._models)
        added = 0
        with _SafeProgress(title="DeeWBatchSaveToCloud - scanning {value} of {max_value}...") as pb:
            for i, path in enumerate(dlg.FileNames):
                pb.update_progress(i, len(dlg.FileNames))
                if path in existing_paths:
                    continue
                self._models.append(scanner.scan_file(path))
                existing_paths.add(path)
                added += 1
        scanner.annotate_version_mismatch(self._models, _revit_version_text(self.application))
        self._refresh_models_grid()
        self.scan_status_tb.Text = "{0} RVT file(s) in list.".format(len(self._models))
        self._log("Added {0} file(s) individually ({1} already in list).".format(added, len(dlg.FileNames) - added))

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

    # ---------------- Destination tab ----------------
    def _refresh_destination_text(self):
        if not self._destination:
            self.destination_tb.Text = "No destination selected yet."
            return
        self.destination_tb.Text = "{0} / {1} / {2}".format(
            self._destination.get("hub_name", "?"),
            self._destination.get("project_name", "?"),
            self._destination.get("folder_name", "?"))

    def pick_destination_click(self, sender, args):
        self._log("Loading ACC Hubs/Projects/Folders...")
        try:
            destination = cloudsvc.pick_destination()
        except Exception as e:
            forms.alert("Could not load ACC destinations: {0}".format(e))
            return
        if not destination:
            return
        self._destination = destination
        self._refresh_destination_text()
        self._log("Destination set: {0}".format(self.destination_tb.Text))

    # ---------------- Run ----------------
    def run_click(self, sender, args):
        selected = [m for m in self._models if m.selected]
        if not selected:
            forms.alert("Scan a folder and select at least one model first.")
            return
        if not self._destination:
            forms.alert("Pick an ACC destination first.")
            return

        options = self._current_options()
        self._save_settings()

        if options["auto_resolve_dialogs"]:
            self._dialog_handler = ffh.make_dialog_handler(self.logger)
            try:
                self.uiapp.DialogBoxShowing += self._dialog_handler
            except Exception as e:
                self.logger.exception("Could not attach dialog handler", e)

        pipeline = BatchPipeline(self.application, self._destination, options, self.logger)
        token = self._destination.get("token") or cloudsvc.get_token()

        report_rows = []
        try:
            with progsvc.DeeWProgressService(_TOOL_TITLE, len(selected)) as prog:
                for model in selected:
                    if prog.cancelled:
                        self._log("Cancelled by user.")
                        break
                    prog.step(model.file_name, "Processing")
                    self._log("Processing '{0}' ({1})...".format(model.file_name, model.upload_mode))
                    row = pipeline.process_one(model, token)
                    report_rows.append(row)
                    model.status = row.upload_status
                    outcome = "success" if "upload" in row.upload_status.lower() else (
                        "skipped" if "skip" in row.upload_status.lower() else "failed")
                    prog.finish_file(outcome)
                    self._log("'{0}': {1}".format(model.file_name, row.upload_status))
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

        uploaded = sum(1 for r in report_rows if "upload" in r.upload_status.lower())
        failed = sum(1 for r in report_rows if "fail" in r.upload_status.lower())
        skipped = sum(1 for r in report_rows if "skip" in r.upload_status.lower())
        self.summary_tb.Text = (
            "{0} processed: {1} uploaded, {2} failed, {3} skipped.".format(
                len(report_rows), uploaded, failed, skipped))
        self._log(self.summary_tb.Text)
        forms.alert(self.summary_tb.Text, title=_TOOL_TITLE)

    # ---------------- Export / Cancel ----------------
    def export_report_click(self, sender, args):
        if not self._report_rows:
            forms.alert("Run the batch first - nothing to export yet.")
            return
        dlg = SaveFileDialog()
        dlg.Filter = "Excel Workbook (*.xlsx)|*.xlsx|CSV (*.csv)|*.csv|Text (*.txt)|*.txt"
        dlg.FileName = "DeeWBatchSaveToCloud_Report.xlsx"
        if dlg.ShowDialog() != DialogResult.OK:
            return
        try:
            reportgen.export(dlg.FileName, "DeeW.Batch Save to Cloud - Report", self._report_rows)
        except Exception as e:
            forms.alert("Could not export report: {0}".format(e))
            return
        MessageBox.Show("Report exported to:\n{0}".format(dlg.FileName), _TOOL_TITLE)

    def cancel_click(self, sender, args):
        self._save_settings()
        self.Close()


def main():
    uiapp = __revit__
    window = DeeWBatchWindow(_XAML_FILE, uiapp)
    window.ShowDialog()


main()
