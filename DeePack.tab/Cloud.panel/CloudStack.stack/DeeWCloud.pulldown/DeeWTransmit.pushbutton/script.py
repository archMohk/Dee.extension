# -*- coding: utf-8 -*-
"""
DeeW.Transmit (DeeW.Cloud)
Issues cleaned COPIES of local and cloud Revit models into a folder you
choose - an eTransmit-style "prepare a model to send out" workflow.

--------------------------------------------------------------------
The one thing that makes this different from DeeW.Clean
--------------------------------------------------------------------
DeeW.Clean opens the REAL central/cloud model (DoNotDetach) and pushes
its changes back via Synchronize With Central - the cleanup is meant to
reach the shared model.

DeeW.Transmit does the opposite, deliberately, and this was confirmed
with the user before it was built: every model is opened DETACHED, the
same cleaning operations are applied to that detached copy, and the
result is written to a NEW file in the destination folder. The source
local file is never saved and the source cloud model is never
synchronized. Closing the detached document without saving is what
guarantees that - there is no code path here that writes to the origin.

Because the copies are detached, deleting all views/sheets (the
destructive options this tool shares with DeeW.Clean) is a normal thing
to do here rather than an alarming one: it strips a model down for
issuing without touching the drawings the team is still working in.

--------------------------------------------------------------------
Shared with DeeW.Clean - no duplicated logic
--------------------------------------------------------------------
  deew_clean_service.clean_document() - the identical option dict, so
      a checkbox added to one tool's Options tab means the same thing
      in the other by construction.
  deew_document_manager.open_document / save_copy_as / unique_target_path
  deew_model_scanner / deew_logger / deew_settings / deew_failure_handler
  deew_progress_service / deew_report_generator (CleanReportRow reused
      as-is: its "location" column carries the saved copy's path here
      instead of the source location, which is the more useful thing to
      report for this tool)
  acc_file_browser / deew_cloud_service - Cloud Models picker, unchanged

--------------------------------------------------------------------
NEEDS LIVE-REVIT VERIFICATION (flagged, not silently assumed correct)
--------------------------------------------------------------------
- Document.SaveAs(ModelPath, SaveAsOptions) on a detached workshared
  document: the detached doc is saved as a NEW standalone/central file.
  Whether the result is standalone or a new central depends on the
  detach option used at open time (DetachAndPreserveWorksets keeps
  worksets, so the copy is a new central). That is the intended
  behaviour for issuing a model, but confirm it matches expectations on
  a real workshared project before relying on it.
- Cloud models are opened via acc_file_browser.open_cloud_document_detached()
  (Application.OpenDocumentFile on a cloud ModelPath, detached, headless).
  FIRST LIVE RUN FAILED with the earlier approach and this is the fix:
  it originally used open_cloud_file()/OpenAndActivateDocument, which
  cannot detach, so SaveAs to a local path was rejected on the still-
  attached ACC central ("Failed - could not save copy"), and every
  model after the first then failed to open one second apart -
  consistent with repeatedly activating/closing documents from inside a
  modal window. Headless detached opening removes both problems, but
  needs confirming on a real ACC project.
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
import dee_telemetry
dee_telemetry.check_access("DeeWTransmit")


output = script.get_output()

_TOOL_NAME = "DeeWTransmit"
_TOOL_TITLE = "DeeW.Transmit"

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")
_CACHE_FILE = os.path.join(_THIS_DIR, ".acc_file_cache.json")

_DEFAULT_SETTINGS = {
    "last_source_folder": "",
    "last_dest_folder": "",
    "options": {},
}


def _revit_version_text(app):
    try:
        return str(app.VersionNumber)
    except Exception:
        return "Unknown"


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


class CloudTransmitItem(object):
    """One row per cloud model added via "Add Cloud Models..." - same
    plain shape acc_file_browser's pick_hub/pick_project/
    list_project_files already return."""

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


class TransmitPipeline(object):
    """Per-file Open (detached) -> Clean -> Save Copy -> Close. Kept
    separate from the Window class per this repo's UI-vs-work
    convention, and structured to mirror DeeW.Clean's CleanPipeline so
    the two stay easy to compare."""

    def __init__(self, application, options, logger):
        self.application = application
        self.options = options
        self.logger = logger

    def _target_name(self, source_file_name):
        base, ext = os.path.splitext(source_file_name)
        if not ext:
            ext = ".rvt"
        suffix = self.options.get("name_suffix", "") or ""
        return base + suffix + ext

    def _apply_clean_result(self, row, clean_result):
        row.purged_count = clean_result.purged_count
        row.zero_area_rooms_deleted = clean_result.zero_area_rooms_deleted
        row.unused_groups_deleted = clean_result.unused_groups_deleted
        row.inplace_families_found = clean_result.inplace_families_found
        row.sheets_deleted = clean_result.sheets_deleted
        row.views_deleted = clean_result.views_deleted
        row.unused_views_deleted = clean_result.unused_views_deleted
        if clean_result.errors:
            row.errors = "; ".join(clean_result.errors)
        if not clean_result.purge_supported and self.options.get("purge_unused"):
            note = "Purge Unused needs Revit 2024+ (this session is {0})".format(
                _revit_version_text(self.application))
            row.warnings = (row.warnings + "; " + note) if row.warnings else note

    def _add_error(self, row, detail):
        row.errors = (row.errors + "; " + detail) if row.errors else detail

    def _save_copy(self, row, document, source_file_name):
        dest_folder = self.options.get("dest_folder", "")
        target = docmgr.unique_target_path(dest_folder, self._target_name(source_file_name))
        ok, detail = docmgr.save_copy_as(
            document, target, compact=self.options.get("compact_on_save", False),
            overwrite=False, logger=self.logger)
        if ok:
            row.save_status = "Copy saved"
            row.location = detail          # the saved copy's real path
        else:
            row.save_status = "Failed - could not save copy"
            self._add_error(row, detail)
        return ok

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

            # Detach where the model supports it, fall back where it
            # does not - a standalone .rvt has nothing to detach from
            # and Revit rejects the option outright.
            document, open_detail = docmgr.open_document_best_detach(
                self.application, scanned_model.file_path,
                audit=self.options.get("audit", False), logger=self.logger)
            if document is None:
                row.save_status = "Failed - could not open"
                row.errors = open_detail
                return row
            row.warnings = (row.warnings + "; " + open_detail) if row.warnings else open_detail

            clean_result = cleansvc.clean_document(document, self.options)
            self._apply_clean_result(row, clean_result)
            self._save_copy(row, document, scanned_model.file_name)
            return row
        except Exception as e:
            row.save_status = "Failed - unexpected error"
            row.errors = str(e)
            self.logger.exception("Unexpected error transmitting file", e, file=scanned_model.file_name)
            return row
        finally:
            if document is not None:
                # save_modified=False always: the detached document has
                # already been written to its new path, and there is
                # nothing we ever want flushed back toward the origin.
                docmgr.close_document(document, save_modified=False, logger=self.logger)
            row.processing_time_seconds = time.time() - start

    def process_cloud(self, item, uiapp):
        """Cloud models are opened HEADLESS + DETACHED (see
        acc_file_browser.open_cloud_document_detached for why). The
        earlier version used the UI-activating, non-detaching
        open_cloud_file, which made SaveAs illegal on the still-attached
        central and destabilised every subsequent open in the batch."""
        row = reportgen.CleanReportRow(
            item.display_name, item.location_text, "Cloud", "Cloud Model",
            _revit_version_text(self.application))
        start = time.time()
        document = None
        try:
            document, open_detail = afb.open_cloud_document_detached(
                self.application, item.region, item.project_id, item.item_id, item.token,
                audit=self.options.get("audit", False))
            if document is None:
                row.save_status = "Failed - could not open"
                row.errors = open_detail
                return row
            row.warnings = (row.warnings + "; " + open_detail) if row.warnings else open_detail

            clean_result = cleansvc.clean_document(document, self.options)
            self._apply_clean_result(row, clean_result)
            # The document is detached, so this writes only to the new
            # path - the ACC model is never synchronized or altered.
            self._save_copy(row, document, item.display_name)
            return row
        except Exception as e:
            row.save_status = "Failed - unexpected error"
            row.errors = str(e)
            self.logger.exception("Unexpected error transmitting cloud file", e, file=item.display_name)
            return row
        finally:
            if document is not None:
                try:
                    docmgr.close_document(document, save_modified=False, logger=self.logger)
                except Exception:
                    pass
            row.processing_time_seconds = time.time() - start


class DeeWTransmitWindow(dee_branding.DeeBrandedWindow):
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
        if saved.get("last_dest_folder") and os.path.isdir(saved["last_dest_folder"]):
            self.dest_folder_tb.Text = saved["last_dest_folder"]
        self._apply_saved_options(saved.get("options", {}))

        self._log("Ready. Pick source models, set a destination folder, then Run.")

    # ---------------- persistence ----------------
    def _current_options(self):
        return {
            "purge_unused": bool(self.purge_unused_cb.IsChecked),
            "delete_zero_area_rooms": bool(self.delete_rooms_cb.IsChecked),
            "delete_unused_groups": bool(self.delete_groups_cb.IsChecked),
            "flag_inplace_families": bool(self.flag_inplace_cb.IsChecked),
            "delete_all_sheets": bool(self.delete_all_sheets_cb.IsChecked),
            "delete_all_views": bool(self.delete_all_views_cb.IsChecked),
            "delete_unused_views": bool(self.delete_unused_views_cb.IsChecked),
            "dest_folder": self.dest_folder_tb.Text,
            "name_suffix": self.name_suffix_tb.Text,
            "compact_on_save": bool(self.compact_on_save_cb.IsChecked),
            "audit": bool(self.audit_cb.IsChecked),
            "auto_resolve_dialogs": bool(self.auto_resolve_dialogs_cb.IsChecked),
            "generate_report": bool(self.generate_report_cb.IsChecked),
            "close_after": True,  # always - a detached copy is never left open
        }

    def _apply_saved_options(self, options):
        try:
            self.purge_unused_cb.IsChecked = options.get("purge_unused", True)
            self.delete_rooms_cb.IsChecked = options.get("delete_zero_area_rooms", True)
            self.delete_groups_cb.IsChecked = options.get("delete_unused_groups", True)
            self.flag_inplace_cb.IsChecked = options.get("flag_inplace_families", True)
            self.delete_all_sheets_cb.IsChecked = options.get("delete_all_sheets", False)
            self.delete_all_views_cb.IsChecked = options.get("delete_all_views", False)
            self.delete_unused_views_cb.IsChecked = options.get("delete_unused_views", False)
            self.name_suffix_tb.Text = options.get("name_suffix", "_CLEANED")
            self.compact_on_save_cb.IsChecked = options.get("compact_on_save", False)
            self.audit_cb.IsChecked = options.get("audit", False)
            self.auto_resolve_dialogs_cb.IsChecked = options.get("auto_resolve_dialogs", True)
            self.generate_report_cb.IsChecked = options.get("generate_report", True)
        except Exception:
            pass

    def _save_settings(self):
        deew_settings.save(_TOOL_NAME, {
            "last_source_folder": self.source_folder_tb.Text,
            "last_dest_folder": self.dest_folder_tb.Text,
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

    def browse_dest_click(self, sender, args):
        dlg = FolderBrowserDialog()
        dlg.Description = "Pick the folder the cleaned copies will be saved into"
        if self.dest_folder_tb.Text and os.path.isdir(self.dest_folder_tb.Text):
            dlg.SelectedPath = self.dest_folder_tb.Text
        if dlg.ShowDialog() == DialogResult.OK:
            self.dest_folder_tb.Text = dlg.SelectedPath

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
        with _SafeProgress(title="DeeWTransmit - scanning {value} of {max_value}...") as pb:
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
            with _SafeProgress(title="DeeWTransmit - loading cloud model list...", indeterminate=True):
                all_items = afb.list_project_files(hub_id, project_id, token, _CACHE_FILE)
            if not all_items:
                return
            picked_names = afb.pick_files_to_open(
                all_items, title="Select Cloud Models to Transmit", button_name="Add Selected")
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
                CloudTransmitItem(hub_id, hub_name, region, project_id, project_name, item_id, name, token))
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
        dest = options["dest_folder"]
        if not dest or not os.path.isdir(dest):
            forms.alert("Pick a valid destination folder on the Options tab first.")
            return
        # Writing the copies back into the folder being scanned would
        # make a second run pick up its own output as a source.
        src = self.source_folder_tb.Text
        if src and os.path.isdir(src) and os.path.normcase(os.path.abspath(src)) == \
                os.path.normcase(os.path.abspath(dest)):
            if not forms.alert(
                    "The destination folder is the same as the source folder. The cleaned copies "
                    "will be picked up as sources next time you scan.\n\nUse it anyway?",
                    title=_TOOL_TITLE, yes=True, no=True):
                return

        self._save_settings()

        if options["auto_resolve_dialogs"]:
            self._dialog_handler = ffh.make_dialog_handler(self.logger)
            try:
                self.uiapp.DialogBoxShowing += self._dialog_handler
            except Exception as e:
                self.logger.exception("Could not attach dialog handler", e)

        pipeline = TransmitPipeline(self.application, options, self.logger)
        total = len(selected_local) + len(selected_cloud)

        report_rows = []
        try:
            with progsvc.DeeWProgressService(_TOOL_TITLE, total) as prog:
                for model in selected_local:
                    if prog.cancelled:
                        self._log("Cancelled by user.")
                        break
                    prog.step(model.file_name, "Transmitting (local)")
                    self._log("Transmitting '{0}'...".format(model.file_name))
                    row = pipeline.process_local(model)
                    report_rows.append(row)
                    model.status = row.save_status
                    outcome = ("success" if row.save_status == "Copy saved"
                               else ("skipped" if "skip" in row.save_status.lower() else "failed"))
                    prog.finish_file(outcome)
                    self._log("'{0}': {1}{2}".format(
                        model.file_name, row.save_status,
                        (" - " + row.errors) if row.errors else ""))

                if not prog.cancelled:
                    for item in selected_cloud:
                        if prog.cancelled:
                            self._log("Cancelled by user.")
                            break
                        prog.step(item.display_name, "Transmitting (cloud)")
                        self._log("Transmitting cloud model '{0}'...".format(item.display_name))
                        row = pipeline.process_cloud(item, self.uiapp)
                        report_rows.append(row)
                        item.status = row.save_status
                        outcome = ("success" if row.save_status == "Copy saved"
                                   else ("skipped" if "skip" in row.save_status.lower() else "failed"))
                        prog.finish_file(outcome)
                        self._log("'{0}': {1}{2}".format(
                            item.display_name, row.save_status,
                            (" - " + row.errors) if row.errors else ""))
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

        succeeded = sum(1 for r in report_rows if r.save_status == "Copy saved")
        failed = sum(1 for r in report_rows if "fail" in r.save_status.lower())
        skipped = sum(1 for r in report_rows if "skip" in r.save_status.lower())
        self.summary_tb.Text = (
            "{0} processed: {1} copies saved to '{2}', {3} failed, {4} skipped.".format(
                len(report_rows), succeeded, dest, failed, skipped))
        self._log(self.summary_tb.Text)
        forms.alert(self.summary_tb.Text, title=_TOOL_TITLE)

    # ---------------- Export / Cancel ----------------
    def export_report_click(self, sender, args):
        if not self._report_rows:
            forms.alert("Run the tool first - nothing to export yet.")
            return
        dlg = SaveFileDialog()
        dlg.Filter = "Excel Workbook (*.xlsx)|*.xlsx|CSV (*.csv)|*.csv|Text (*.txt)|*.txt"
        dlg.FileName = "DeeWTransmit_Report.xlsx"
        if dlg.ShowDialog() != DialogResult.OK:
            return
        try:
            reportgen.export(
                dlg.FileName, "DeeW.Transmit - Issued Copies Report", self._report_rows,
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
    window = DeeWTransmitWindow(_XAML_FILE, uiapp)
    window.ShowDialog()


main()
