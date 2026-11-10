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
dee_telemetry.check_access("DeeWSharing")


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


from dee_ui import SafeProgress as _SafeProgress
import deew_background_ui
from deew_sharing_pipeline import SharingPipeline


class DeeWSharingWindow(dee_branding.DeeBrandedWindow):
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
            _TOOL_TITLE, "DeeWSharing_Settings.json", self._collect_settings_dict)

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
        with _SafeProgress(title="DeeWSharing - scanning {value} of {max_value}...") as pb:
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
        token = self._destination.get("token") or cloudsvc.get_token()
        pipeline = SharingPipeline(self.application, self._destination, options, self.logger)
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
            self, "DeeWSharing", "deew_sharing_pipeline", "worker", items, options, [_THIS_DIR],
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
