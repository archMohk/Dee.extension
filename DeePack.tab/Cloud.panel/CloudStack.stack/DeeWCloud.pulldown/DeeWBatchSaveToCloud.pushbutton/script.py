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
import datetime

import clr
clr.AddReference("System.Windows.Forms")
from System.Windows.Forms import FolderBrowserDialog, OpenFileDialog, SaveFileDialog, DialogResult, MessageBox

from pyrevit import forms, script
import dee_branding

import deew_logger
import deew_settings
import dee_settings_file
import deew_model_scanner as scanner
import deew_cloud_service as cloudsvc
import deew_failure_handler as ffh
import dee_known_dialogs
import deew_progress_service as progsvc
import deew_report_generator as reportgen
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

from dee_ui import SafeProgress as _SafeProgress
from deew_batch_pipeline import _revit_version_text
import deew_background_ui
from deew_batch_pipeline import BatchPipeline


class DeeWBatchWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, uiapp):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        # Shared "On Revit errors: delete ... and continue" tick box
        dee_known_dialogs.add_error_checkbox(self)
        deew_background_ui.add_run_controls(self)
        self.uiapp = uiapp
        self.application = uiapp.Application
        self.logger = deew_logger.DeeWLogger(_TOOL_NAME)
        self._models = []
        self._destination = None
        self._report_rows = []
        self._status_lines = []
        self._dialog_handler = None

        saved = deew_settings.load(_TOOL_NAME, _DEFAULT_SETTINGS)
        self._apply_settings_dict(saved)

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

    def _collect_settings_dict(self):
        return {
            "last_source_folder": self.source_folder_tb.Text,
            "last_destination": self._destination,
            "options": self._current_options(),
        }

    def _apply_settings_dict(self, settings):
        if settings.get("last_source_folder") and os.path.isdir(settings["last_source_folder"]):
            self.source_folder_tb.Text = settings["last_source_folder"]
        if settings.get("last_destination"):
            self._destination = settings["last_destination"]
            self._refresh_destination_text()
        self._apply_saved_options(settings.get("options", {}))

    def _save_settings(self):
        deew_settings.save(_TOOL_NAME, self._collect_settings_dict())

    def save_settings_click(self, sender, args):
        dee_settings_file.save_settings_file(
            _TOOL_TITLE, "DeeWBatchSaveToCloud_Settings.json", self._collect_settings_dict)

    def load_settings_click(self, sender, args):
        if dee_settings_file.load_settings_file(_TOOL_TITLE, self._apply_settings_dict):
            self._save_settings()

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
                if pb.cancelled:
                    break   # Cancel: stop after the current item, keep what is done
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

    def add_open_click(self, sender, args):
        """Adds every currently open, non-linked document in this same
        Revit session to the Source Models list, using each Document's
        own authoritative IsWorkshared/IsDetached/IsModelInCloud state
        (scanner.scan_open_document) rather than a closed-file guess.
        A document that has never been saved (no PathName) has no file
        to point at and is skipped with a note - save it first."""
        try:
            open_docs = [d for d in self.uiapp.Application.Documents if not d.IsLinked]
        except Exception as e:
            forms.alert("Could not read open documents: {0}".format(e))
            return
        if not open_docs:
            forms.alert("No open documents found in this session.")
            return

        existing_paths = set(m.file_path for m in self._models if not m.live_document)
        added = 0
        skipped_unsaved = 0
        skipped_duplicate = 0
        for doc in open_docs:
            try:
                path = doc.PathName or ""
            except Exception:
                path = ""
            if not path:
                skipped_unsaved += 1
                continue
            if path in existing_paths or any(
                    m.live_document is doc for m in self._models):
                skipped_duplicate += 1
                continue
            self._models.append(scanner.scan_open_document(doc))
            existing_paths.add(path)
            added += 1

        self._refresh_models_grid()
        self.scan_status_tb.Text = "{0} RVT file(s) in list.".format(len(self._models))
        note = "Added {0} open document(s)".format(added)
        if skipped_unsaved:
            note += "; {0} skipped (never saved - save first)".format(skipped_unsaved)
        if skipped_duplicate:
            note += "; {0} already in list".format(skipped_duplicate)
        self._log(note + ".")

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

        if deew_background_ui.is_background(self):
            self._run_background(selected, options)
            return

        # Always attached: with "Automatically Resolve Dialogs" off, it still
        # answers the shared must-handle dialogs (Copied Central Model, ...
        # see lib/dee_known_dialogs.py) and leaves every other dialog to the
        # user - otherwise a single-button notice stalls the whole batch.
        if options["auto_resolve_dialogs"]:
            self._dialog_handler = ffh.make_dialog_handler(self.logger)
        else:
            self._dialog_handler = ffh.make_known_dialogs_handler(self.logger)
        try:
            self.uiapp.DialogBoxShowing += self._dialog_handler
            dee_known_dialogs.attach_failures(self.uiapp)
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
                    dee_known_dialogs.detach_failures(self.uiapp)
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

    # ---------------- Background Revit ----------------
    def _run_background(self, selected, options):
        stay = [m for m in selected if m.live_document is not None]
        if stay:
            self._log("{0} model(s) open in this session cannot go to a background Revit - untick them or "
                      "run them by this session: {1}".format(len(stay), ", ".join(m.file_name for m in stay)))
            selected = [m for m in selected if m.live_document is None]
        token = self._destination.get("token") or cloudsvc.get_token()
        pipeline = BatchPipeline(self.application, self._destination, options, self.logger)
        self._log("Reserving cloud model names...")
        options["resolved_names"] = pipeline.reserve_names(selected, token)
        destination = dict(self._destination)
        destination.pop("token", None)
        options["destination"] = destination
        options["token"] = token
        items = [deew_background_ui.local_item(m) for m in selected]
        for m in selected:
            m.status = "Queued (background)"
        self._refresh_models_grid()
        self._bg_lookup = dict((m.file_path, m) for m in selected)
        deew_background_ui.launch(
            self, "DeeWBatchSaveToCloud", "deew_batch_pipeline", "worker", items, options, [_THIS_DIR],
            reportgen.ReportRow, "upload_status", ("Uploaded",), self._background_done, title=_TOOL_TITLE)

    def _background_done(self, rows, results, how):
        for key, m in self._bg_lookup.items():
            res = results.get(key)
            m.status = (res or {}).get("status") or "Failed - not processed"
        self._refresh_models_grid()
        self._report_rows = rows
        uploaded, failed, skipped = deew_background_ui.count(rows, "upload_status", ("Uploaded",))
        self.summary_tb.Text = "{0} processed in background Revit(s): {1} uploaded, {2} failed, {3} skipped.".format(
            len(rows), uploaded, failed, skipped)
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
