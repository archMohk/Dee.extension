# -*- coding: utf-8 -*-
"""
DeeW.Sharing (DeeW.Cloud)
Converts Revit models into Autodesk Construction Cloud Cloud Models.
Automatically detects each model's type (Standalone / Central / Local
Copy / Cloud / Detached / Corrupted / Read-Only), detaches from
central and enables worksharing where needed, saves to your chosen ACC
Hub/Project/Folder, and generates a detailed report.

--------------------------------------------------------------------
Architecture - IronPython 2, not CPython 3 (history below)
--------------------------------------------------------------------
This package was originally built to run under pyRevit's CPython 3
engine (via a "#! python3" script.py hashbang), per this package's
original spec, which explicitly asked for Python 3 over IronPython 2.
Live testing immediately confirmed that engine is not usable in this
environment: opening this tool crashed with a .NET-level
TypeInitializationException ("the type initializer for 'Delegates'
threw an exception") in Revit 2024, and a DIFFERENT .NET-level
FormatException ("the input string '3.12.3' was not in a correct
format") in Revit 2026 - both failures happen inside pyRevit's own
CPython-engine bootstrapping, before any script code (including this
file) ever runs, so neither was fixable from within this package.
This matches pyRevit's own community-documented caveat that its
CPython engine "is under active development and might be unstable."

Given that, this file (and the rest of DeeW.Cloud) now runs on
pyRevit's default IronPython 2 engine, matching every other tool in
Dee.extension (proven stable across 39+ tools all built the same way).
pyrevit.forms.WPFWindow and the Click="method_name" XAML-binding
pattern used throughout this file are identical to every other
IronPython 2 tool in this extension - no longer a new-ground
assumption.

Business logic is split across reusable lib/ services (per spec,
shared with every other DeeW.Cloud tool, present and future):
  deew_logger            - structured logging (in-memory + file)
  deew_settings           - persistent last-folder/destination/options
  deew_model_scanner      - closed-file model-type classification
  deew_document_manager   - open/detach/worksharing/compact/close
  deew_cloud_service       - ACC discovery + Document.SaveAsCloudModel
  deew_failure_handler     - DialogBoxShowing + IFailuresPreprocessor
  deew_progress_service    - progress/stats reporting
  deew_report_generator    - CSV/TXT/Excel report export
  acc_auth / acc_api / acc_file_browser - this repo's existing APS/ACC
      REST client, reused unchanged (not duplicated)

This file itself is the "single responsibility" of orchestrating those
services into the specific DeeW.Sharing workflow, plus its WPF UI
controller - matching this repo's established one-script.py-per-tool
convention (see DeeDistributor.pushbutton/script.py, DeeReLevel, etc.)
rather than fragmenting the orchestration logic itself across files.

--------------------------------------------------------------------
NEEDS LIVE-REVIT VERIFICATION (flagged explicitly - see also the
per-function docstrings in each lib/deew_*.py module)
--------------------------------------------------------------------
- Document.IsModelInCloud, WorksetConfigurationOption.OpenAllWorksets,
  and the cloud-model-GUID readback in deew_report_generator.py.
- acc_api.search_cloud_models() is documented as project-wide, not
  folder-scoped - so the duplicate-name check here may flag a
  same-named model in a DIFFERENT folder of the same project as a
  collision. Treated as a conservative false-positive-tolerant check
  (better to rename/skip unnecessarily than silently collide).
- "Overwrite Existing Cloud Model" does NOT delete/replace an existing
  cloud item - that would require a verified, destructive Data
  Management API DELETE call this package does not implement. Instead,
  when a name collision is found and Overwrite is selected, the file
  is SKIPPED with a clear reason rather than risking an unverified
  destructive operation or silently doing the wrong thing.
"""
import os
import time
import datetime

import clr
clr.AddReference("System.Windows.Forms")
from System.Windows.Forms import FolderBrowserDialog, SaveFileDialog, DialogResult, MessageBox

from pyrevit import forms, script

import deew_logger
import deew_settings
import deew_model_scanner as scanner
import deew_document_manager as docmgr
import deew_cloud_service as cloudsvc
import deew_failure_handler as ffh
import deew_progress_service as progsvc
import deew_report_generator as reportgen
import acc_api

output = script.get_output()

_TOOL_NAME = "DeeWSharing"
_TOOL_TITLE = "DeeW.Sharing"

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")

_DEFAULT_SETTINGS = {
    "last_source_folder": "",
    "last_destination": None,
    "options": {},
}


def _revit_version_text(app):
    try:
        return str(app.VersionNumber)
    except Exception:
        return "Unknown"


class SharingPipeline(object):
    """Owns the actual per-file Validate -> Determine -> Open ->
    Detach/Worksharing -> Save-to-Cloud -> Verify -> Close pipeline.
    Kept separate from the Window class (single responsibility: this
    class does the Revit/cloud work, the Window class only handles
    UI wiring) even though both live in this one script.py file, per
    this repo's established per-tool-file convention."""

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

        if self.options.get("overwrite_cloud"):
            # See module docstring - true overwrite (deleting the
            # existing cloud item) is not implemented; skip rather
            # than risk an unverified destructive operation.
            return None, "Skipped - a cloud model named '{0}' already exists (overwrite not performed)".format(base_name)

        # Rename Duplicate (default): find the next free "(n)" suffix.
        n = 2
        candidate = "{0} ({1})".format(base_name, n)
        while candidate in existing:
            n += 1
            candidate = "{0} ({1})".format(base_name, n)
        return candidate, None

    def _detach_option(self, scanned_model):
        if scanned_model.model_type in (scanner.MODEL_TYPE_CENTRAL, scanner.MODEL_TYPE_LOCAL):
            if not self.options.get("detach"):
                return "none"
            return "discard" if self.options.get("discard_worksets") else "preserve"
        return "none"

    def process_one(self, scanned_model, token, dialog_handler_registered):
        row = reportgen.ReportRow(
            scanned_model.file_name, scanned_model.file_path,
            _revit_version_text(self.application), scanned_model.model_type)
        start = time.time()
        document = None
        try:
            if scanned_model.model_type == scanner.MODEL_TYPE_CLOUD:
                row.upload_status = "Skipped - already a cloud model"
                self.logger.info("Skipped (already cloud)", file=scanned_model.file_name)
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
                self.logger.info("Uploaded", file=scanned_model.file_name, detail=detail)
            else:
                row.upload_status = "Failed - upload error"
                row.errors = detail
                self.logger.error("Upload failed", file=scanned_model.file_name, detail=detail)

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


class DeeWSharingWindow(forms.WPFWindow):
    def __init__(self, xaml_file, uiapp):
        forms.WPFWindow.__init__(self, xaml_file)
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
            "discard_worksets": bool(self.discard_worksets_rb.IsChecked),
            "audit": bool(self.audit_cb.IsChecked),
            "compact": bool(self.compact_cb.IsChecked),
            "open_all_worksets": bool(self.open_all_worksets_cb.IsChecked),
            "ignore_missing_links": bool(self.ignore_missing_links_cb.IsChecked),
            "ignore_missing_families": bool(self.ignore_missing_families_cb.IsChecked),
            "auto_resolve_dialogs": bool(self.auto_resolve_dialogs_cb.IsChecked),
            "overwrite_cloud": bool(self.overwrite_cloud_rb.IsChecked),
            "generate_report": bool(self.generate_report_cb.IsChecked),
            "close_after": bool(self.close_after_cb.IsChecked),
        }

    def _apply_saved_options(self, options):
        try:
            self.enable_worksharing_cb.IsChecked = options.get("enable_worksharing", True)
            self.detach_cb.IsChecked = options.get("detach", True)
            self.discard_worksets_rb.IsChecked = options.get("discard_worksets", False)
            self.preserve_worksets_rb.IsChecked = not options.get("discard_worksets", False)
            self.audit_cb.IsChecked = options.get("audit", False)
            self.compact_cb.IsChecked = options.get("compact", False)
            self.open_all_worksets_cb.IsChecked = options.get("open_all_worksets", True)
            self.ignore_missing_links_cb.IsChecked = options.get("ignore_missing_links", True)
            self.ignore_missing_families_cb.IsChecked = options.get("ignore_missing_families", True)
            self.auto_resolve_dialogs_cb.IsChecked = options.get("auto_resolve_dialogs", True)
            self.overwrite_cloud_rb.IsChecked = options.get("overwrite_cloud", False)
            self.rename_cloud_rb.IsChecked = not options.get("overwrite_cloud", False)
            self.generate_report_cb.IsChecked = options.get("generate_report", True)
            self.close_after_cb.IsChecked = options.get("close_after", True)
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
                prog.step(name, "Scanning")
            models = scanner.scan_folder(folder, recursive=recursive, progress_cb=progress_cb)
        self._models = models
        self.models_grid.ItemsSource = None
        self.models_grid.ItemsSource = models
        self.scan_status_tb.Text = "{0} RVT file(s) found.".format(len(models))
        self._log("Scan complete: {0} file(s).".format(len(models)))

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

    # ---------------- ACC Destination tab ----------------
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

        pipeline = SharingPipeline(self.application, self._destination, options, self.logger)
        token = self._destination.get("token") or cloudsvc.get_token()

        report_rows = []
        try:
            with progsvc.DeeWProgressService(_TOOL_TITLE, len(selected)) as prog:
                for model in selected:
                    if prog.cancelled:
                        self._log("Cancelled by user.")
                        break
                    prog.step(model.file_name, "Processing")
                    self._log("Processing '{0}'...".format(model.file_name))
                    row = pipeline.process_one(model, token, self._dialog_handler is not None)
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
            forms.alert("Run the tool first - nothing to export yet.")
            return
        dlg = SaveFileDialog()
        dlg.Filter = "Excel Workbook (*.xlsx)|*.xlsx|CSV (*.csv)|*.csv|Text (*.txt)|*.txt"
        dlg.FileName = "DeeWSharing_Report.xlsx"
        if dlg.ShowDialog() != DialogResult.OK:
            return
        try:
            reportgen.export(dlg.FileName, "DeeW.Sharing - Cloud Conversion Report", self._report_rows)
        except Exception as e:
            forms.alert("Could not export report: {0}".format(e))
            return
        MessageBox.Show("Report exported to:\n{0}".format(dlg.FileName), _TOOL_TITLE)

    def cancel_click(self, sender, args):
        self._save_settings()
        self.Close()


def main():
    uiapp = __revit__
    window = DeeWSharingWindow(_XAML_FILE, uiapp)
    window.ShowDialog()


main()
