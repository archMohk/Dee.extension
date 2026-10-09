# -*- coding: utf-8 -*-
"""
DeeInitiator
Batch-creates new Revit project files from a template across any number
of user-defined "Zones", then uploads every one of them to Autodesk
Construction Cloud (ACC) as a new worksharing-enabled Cloud Model.

Each Zone pairs ONE template/RVT file with a bulk list of new file
names (one per line) and its OWN ACC destination (Hub/Project/Folder -
ACC has no raw filesystem path, so a browsed destination IS "the ACC
path", exactly like every other ACC tool in this repo). Zones are added
one at a time by clicking "+ Add Zone" - unbounded, not a fixed pre-
built count - per explicit user request ("I prefer clicking + [to add
zones] rather than a tool pre-built with 8 zones toggled by checkboxes").

--------------------------------------------------------------------
Reused building blocks - this is NOT new ground for the upload half
--------------------------------------------------------------------
The "open a local file -> enable worksharing -> upload to ACC as a new
Cloud Model" pipeline already exists, proven, in DeeW.Sharing
(DeePack.tab/Cloud.panel/CloudStack.stack/DeeWCloud.pulldown/
DeeWSharing.pushbutton/script.py's SharingPipeline) - InitiatorPipeline
below mirrors its process_one() shape directly:
  deew_document_manager.open_document_best_detach - opens the local
      duplicate, trying preserve/discard/none detach in order.
  deew_document_manager.enable_worksharing - no-ops if already
      workshared, else Document.EnableWorksharing("Shared Levels and
      Grids", "Workset1"), matching Revit's own UI command exactly.
  deew_document_manager.unique_target_path - non-colliding on-disk name.
  deew_document_manager.close_document.
  deew_cloud_service.pick_destination - the SAME Hub->Project->Folder
      picker every ACC tool here uses; returns hub/project/folder ids
      plus their human-readable names.
  deew_cloud_service.save_to_cloud - wraps the verified
      Document.SaveAsCloudModel(accountGuid, projectGuid, folderId,
      modelName) Revit API call. Never raises.
  deew_model_scanner.scan_file - classifies a closed file (corrupted/
      read-only/etc) via BasicFileInfo.Extract() - confirmed by reading
      its source that it does NOT filter by extension (only
      scan_folder's own glob does), so it is reused UNMODIFIED here for
      .rte validation, not just .rvt.
  deew_progress_service.DeeWProgressService - already computes elapsed
      time, ETA, and per-file average, rendered into the progress bar's
      title - this alone satisfies "Progress Bar + Estimated Time", no
      new progress code needed.
  deew_report_generator.export / lib/xlsx_writer - CSV/TXT/Excel export
      with green/red/gray row tinting, given a custom header/row schema
      (InitiatorReportRow below).
  deew_failure_handler.make_dialog_handler - auto-dismisses known
      open-time dialogs during a batch run.

--------------------------------------------------------------------
Genuinely new ground - flagged explicitly, not silently assumed
--------------------------------------------------------------------
NEEDS LIVE-REVIT VERIFICATION:
- Duplicating a template under a batch of names is a plain OS-level
  file copy (shutil.copy2), always written with a .rvt extension
  regardless of whether the source was .rvt or .rte, rather than
  Application.NewProjectDocument(templatePath). Application.
  NewProjectDocument has ZERO precedent anywhere in this codebase (a
  repo-wide search found only a string inside an AI system-prompt,
  never actually invoked by real code) - a plain file copy is the
  lower-risk choice because everything AFTER it (open/enable-
  worksharing/upload) is already proven, unmodified code. This relies
  on a .rte file being binary-identical in on-disk format to a .rvt
  (Revit's own "new project from template" behavior is triggered by
  the file-open UI command, not by file contents) - believed correct
  but never exercised against a live .rte file in this extension. If
  it opens in "template edit" mode instead of a plain project, or
  raises on open, the fallback is Application.NewProjectDocument(
  templatePath) followed by Document.SaveAs to the target .rvt path
  (itself untested here too).
- The dynamic "+ Add Zone" WPF UI (building a GroupBox of controls in
  code and appending it to a StackPanel at runtime, per click) has no
  precedent in this repo for a USER-TRIGGERED repeat-and-append
  interaction - the closest analogs (DeeLazy.pushbutton/controller.py's
  _build_cards, DeeRelink.pushbutton/script.py's _build_tabs) both
  build a FIXED, pre-known set once at window-open. The underlying
  technique (constructing WPF controls via direct .NET constructors
  and wiring per-instance closures via Click +=) is the same proven
  mechanism those two already use, just applied to a user-clicked "+"
  instead of a startup loop.

--------------------------------------------------------------------
Second export - handoff to DeeMAPLink, by explicit user request
--------------------------------------------------------------------
"Export Link-Setup File" (export_linkmap_click) writes a second, simpler
CSV naming exactly the files THIS run uploaded successfully - meant to
be opened in DeeMAPLink's (DeePack.tab/Coordination.panel/CoordViewStack.
stack/DeeMAPLink.pushbutton) new "Import file" source mode, so the user
can see those exact new files as DeeMAPLink's own two lists and wire up
real Revit Links between them, without first re-scanning the whole ACC
project (which may contain many unrelated files). DeeMAPLink already
implements the two-list/click-to-wire/Canvas-line/link-creation UI this
needed - this export is the only new piece, closing one real gap:
Document.SaveAsCloudModel never hands back the new cloud item's id, so
_resolve_item_ids looks it up afterward via acc_api.search_cloud_models
(the same function DeeWSharing's own _existing_cloud_names already uses
for name-collision checking, just read here for its id instead of its
name) - a short, documented best-effort retry, not a guarantee; a file
whose id can't be resolved yet is marked UNRESOLVED and DeeMAPLink's
import skips it rather than risk opening the wrong item.

--------------------------------------------------------------------
Scope limits for this first pass - not silently incomplete
--------------------------------------------------------------------
- Duplicate-name collisions ON DISK are resolved via unique_target_
  path's existing "(2)", "(3)" suffixing. Collisions AGAINST AN
  EXISTING CLOUD MODEL at the same ACC destination are NOT pre-checked
  (unlike DeeW.Sharing's _resolve_model_name) - SaveAsCloudModel is
  asked to use the typed name as-is; a real ACC-side collision surfaces
  as a failed upload with Autodesk's own error text in the report,
  rather than this tool guessing at a rename policy nobody asked for.
- Duplicate name LINES typed into the same Zone's bulk box are silently
  de-duplicated (Zone.names()), not flagged as an error.
- "Validate ACC" confirms the destination is populated (hub/project/
  folder ids all present) and that a token can currently be fetched -
  it does NOT re-walk the Hub/Project/Folder tree to confirm the
  folder still exists on Autodesk's side (no cheap existing API call
  does this). A folder deleted between Validate and Run only surfaces
  as a failed upload in the report, never pre-emptively in green/red.
- Only one Revit document is open at a time, processed start-to-finish
  before the next starts - no parallelism across zones or files,
  matching deew_document_manager's own stated memory-release
  discipline.
- No retry/resume - a cancelled or partially-failed run must be
  re-triggered manually; already-uploaded files are not detected or
  skipped on a second run.
- The Output Root Folder is ONE shared location for the whole run, with
  one auto-created subfolder per Zone ("<root>/Zone 1/", "<root>/
  Zone 2/", ...) - not a separate picker per Zone.
- "Delete local copy after successful upload" is ONE global checkbox
  for the whole run, not per-zone - and only ever deletes AFTER a
  CONFIRMED successful upload; a failed upload always leaves the local
  duplicate in place so nothing is silently lost.
"""
import os
import time
import shutil
import datetime

import clr
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("System.Windows.Forms")
from System.Windows import Thickness, FontWeights, HorizontalAlignment, VerticalAlignment, TextWrapping
from System.Windows.Controls import GroupBox, StackPanel, Orientation, TextBox, TextBlock, Button, ScrollBarVisibility
from System.Windows.Media import SolidColorBrush, Color
from System.Windows.Forms import FolderBrowserDialog, OpenFileDialog, SaveFileDialog, DialogResult, MessageBox

from pyrevit import forms, script
import dee_branding

import deew_logger
import deew_model_scanner as scanner
import deew_document_manager as docmgr
import deew_cloud_service as cloudsvc
import deew_failure_handler as ffh
import deew_progress_service as progsvc
import deew_report_generator as reportgen
import acc_api

import dee_telemetry
dee_telemetry.check_access("DeeInitiator")


output = script.get_output()

_TOOL_NAME = "DeeInitiator"
_TOOL_TITLE = "DeeInitiator"

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")

_VALID_EXTENSIONS = (".rvt", ".rte")

_GREEN = "#2e7d32"
_RED = "#c62828"

HEADERS = ["Zone", "Template Used", "Local Output Folder", "New File Name",
           "File Validation", "ACC Hub / Project / Folder", "Upload Status",
           "Worksharing Enabled", "Local Copy", "Duration", "Errors",
           "Date", "Revit Version", "User"]
COL_WIDTHS = [8, 34, 34, 26, 16, 34, 18, 16, 14, 12, 30, 18, 12, 16]


def _brush(hex_color):
    h = hex_color.lstrip("#")
    return SolidColorBrush(Color.FromRgb(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)))


def _revit_version_text(app):
    try:
        return str(app.VersionNumber)
    except Exception:
        return "Unknown"


# ==========================================================================
# Zone - plain data holder, one per Zone, living alongside that Zone's own
# WPF controls (same "data object next to its own UI" pattern this
# codebase already uses for DataGrid rows, just applied to a bigger
# per-zone block instead of a grid row).
# ==========================================================================
class Zone(object):
    _next_id = [1]

    def __init__(self):
        self.id = Zone._next_id[0]
        Zone._next_id[0] += 1
        self.template_path = ""
        self.bulk_names_text = ""
        self.destination = None    # dict from cloudsvc.pick_destination(), or None
        self.file_valid = None     # None = not yet validated, True/False after
        self.acc_valid = None
        self.controls = {}         # WPF control references, keyed by name

    def names(self):
        """One name per non-blank line, trimmed, de-duplicated preserving
        order - see module docstring's Scope Limits re: duplicate lines."""
        seen = set()
        out = []
        for line in (self.bulk_names_text or "").splitlines():
            name = line.strip()
            if name and name not in seen:
                seen.add(name)
                out.append(name)
        return out


def validate_zone_file(zone):
    """Returns (ok, detail). Checks the template path exists, has a
    .rvt/.rte extension, passes scanner.scan_file's closed-file
    corrupted/read-only check, and that at least one new-file name was
    typed. Never raises."""
    path = (zone.template_path or "").strip()
    if not path:
        return False, "No template/RVT file selected"
    if not os.path.isfile(path):
        return False, "File does not exist: {0}".format(path)
    ext = os.path.splitext(path)[1].lower()
    if ext not in _VALID_EXTENSIONS:
        return False, "Must be a .rvt or .rte file (found '{0}')".format(ext or "no extension")
    try:
        model = scanner.scan_file(path)
        if model.model_type in (scanner.MODEL_TYPE_CORRUPTED, scanner.MODEL_TYPE_READ_ONLY):
            return False, model.status
    except Exception as e:
        return False, "Could not read file: {0}".format(e)
    if not zone.names():
        return False, "Type at least one new file name (one per line)"
    return True, "Validated"


def validate_zone_acc(zone):
    """Returns (ok, detail). ACC has no literal path - "validated" means
    a destination was actually picked (Hub/Project/Folder ids all
    present) and a token can still be fetched right now. Does NOT
    re-confirm the folder still exists on Autodesk's side - see module
    docstring's Scope Limits."""
    dest = zone.destination
    if not dest:
        return False, "No ACC destination selected"
    if not (dest.get("hub_id") and dest.get("project_id") and dest.get("folder_id")):
        return False, "Destination is incomplete - pick it again"
    try:
        cloudsvc.get_token()
    except Exception as e:
        return False, "Could not verify ACC sign-in: {0}".format(e)
    return True, "Validated"


# ==========================================================================
# Report row
# ==========================================================================
class InitiatorReportRow(object):
    def __init__(self, zone_id, template_used, output_folder, new_file_name, revit_version):
        self.zone_id = zone_id
        self.template_used = template_used
        self.output_folder = output_folder
        self.new_file_name = new_file_name
        self.file_validation = "Validated"
        self.acc_destination_text = ""
        self.upload_status = "Pending"
        self.worksharing_enabled = "No"
        self.local_copy = ""
        self.processing_time_seconds = 0.0
        self.errors = ""
        self.date_text = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.revit_version = revit_version
        try:
            self.user = os.environ.get("USERNAME", "Unknown")
        except Exception:
            self.user = "Unknown"

    def to_list(self):
        return [
            self.zone_id, self.template_used, self.output_folder, self.new_file_name,
            self.file_validation, self.acc_destination_text, self.upload_status,
            self.worksharing_enabled, self.local_copy, "{0:.1f}s".format(self.processing_time_seconds),
            self.errors, self.date_text, self.revit_version, self.user,
        ]

    def status_tag(self):
        status = (self.upload_status or "").lower()
        if "fail" in status:
            return "fail"
        if "uploaded" in status:
            return "ok"
        return None


# ==========================================================================
# Link-setup handoff file - a second, simpler export naming exactly the
# files this run uploaded, meant to be imported into DeeMAPLink
# (DeePack.tab/Coordination.panel/CoordViewStack.stack/DeeMAPLink.pushbutton)
# so the user can wire up Revit Links between the newly-created files
# without re-scanning the whole ACC project. See module docstring.
# ==========================================================================
_LINKMAP_HEADERS = ["Zone", "New File Name", "Item Id", "Region", "Project Id",
                    "Project Name", "Hub Id", "Hub Name", "Folder Id", "Folder Name",
                    "Date", "User"]
_LINKMAP_COL_WIDTHS = [8, 30, 36, 10, 36, 24, 36, 20, 36, 24, 18, 16]

_UNRESOLVED_ITEM_ID = "UNRESOLVED"


class LinkSetupRow(object):
    def __init__(self, zone_id, new_file_name, item_id, destination):
        self.zone_id = zone_id
        self.new_file_name = new_file_name
        self.item_id = item_id
        self.region = destination.get("region", "")
        self.project_id = destination.get("project_id", "")
        self.project_name = destination.get("project_name", "")
        self.hub_id = destination.get("hub_id", "")
        self.hub_name = destination.get("hub_name", "")
        self.folder_id = destination.get("folder_id", "")
        self.folder_name = destination.get("folder_name", "")
        self.date_text = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            self.user = os.environ.get("USERNAME", "Unknown")
        except Exception:
            self.user = "Unknown"

    def to_list(self):
        return [
            self.zone_id, self.new_file_name, self.item_id, self.region, self.project_id,
            self.project_name, self.hub_id, self.hub_name, self.folder_id, self.folder_name,
            self.date_text, self.user,
        ]


def _resolve_item_ids(project_id, token, expected_names, logger, attempts=3, delay_seconds=2.0):
    """Returns {expected_display_name: item_id}, best-effort. Document.
    SaveAsCloudModel (deew_cloud_service.save_to_cloud) never hands back
    the new cloud item's id, so it has to be looked up afterward via
    acc_api.search_cloud_models(project_id, token) - the SAME function
    DeeWSharing's own _existing_cloud_names already uses for name-
    collision checking, just used here to read an id instead.

    The search hits Autodesk's own index, which can lag moments behind
    the upload transaction that just completed - retried up to
    `attempts` times with a short pause rather than assumed to be
    immediately consistent. A name still not found after every attempt
    is simply absent from the returned dict; the caller marks it
    UNRESOLVED rather than guessing - never silently invents an id."""
    remaining = set(expected_names)
    found = {}
    for attempt in range(attempts):
        if not remaining:
            break
        try:
            items = acc_api.search_cloud_models(project_id, token)
        except Exception as e:
            logger.exception("search_cloud_models failed while resolving item ids", e)
            items = []
        by_name = {}
        for item_id, display_name in items:
            by_name.setdefault(display_name, item_id)
        for name in list(remaining):
            if name in by_name:
                found[name] = by_name[name]
                remaining.discard(name)
        if remaining and attempt < attempts - 1:
            time.sleep(delay_seconds)
    return found


# ==========================================================================
# Pipeline - duplicate -> open -> enable worksharing -> upload -> keep/delete
# Mirrors DeeWSharing.pushbutton/script.py's SharingPipeline.process_one
# shape directly: never raises out, always returns a report row, closes
# the document in a finally block.
# ==========================================================================
class InitiatorPipeline(object):
    def __init__(self, application, delete_after_upload, logger):
        self.application = application
        self.delete_after_upload = delete_after_upload
        self.logger = logger

    def process_one(self, zone, new_name, zone_output_folder, revit_version_text):
        row = InitiatorReportRow(zone.id, zone.template_path, zone_output_folder, new_name, revit_version_text)
        row.acc_destination_text = "{0} / {1} / {2}".format(
            zone.destination.get("hub_name", "?"), zone.destination.get("project_name", "?"),
            zone.destination.get("folder_name", "?"))
        start = time.time()
        document = None
        target_path = None
        try:
            target_path = docmgr.unique_target_path(zone_output_folder, new_name + ".rvt")
            try:
                shutil.copy2(zone.template_path, target_path)
            except Exception as e:
                row.upload_status = "Failed - could not duplicate"
                row.errors = str(e)
                return row

            document, detail = docmgr.open_document_best_detach(self.application, target_path, logger=self.logger)
            if document is None:
                row.upload_status = "Failed - could not open duplicate"
                row.errors = detail
                return row

            ok = docmgr.enable_worksharing(document, logger=self.logger)
            row.worksharing_enabled = "Yes" if ok else "Failed"

            success, detail = cloudsvc.save_to_cloud(document, zone.destination, new_name)
            if success:
                row.upload_status = "Uploaded"
            else:
                row.upload_status = "Failed - upload error"
                row.errors = detail
            return row
        except Exception as e:
            row.upload_status = "Failed - unexpected error"
            row.errors = str(e)
            self.logger.exception("Unexpected error processing file", e, file=new_name)
            return row
        finally:
            if document is not None:
                docmgr.close_document(document, save_modified=False, logger=self.logger)
            # Delete only ever happens here, after close, and ONLY on a
            # CONFIRMED successful upload - a failed upload always leaves
            # the local duplicate in place so nothing is silently lost.
            if target_path and row.upload_status == "Uploaded":
                if self.delete_after_upload:
                    try:
                        os.remove(target_path)
                        row.local_copy = "Deleted"
                    except Exception:
                        row.local_copy = "Kept (delete failed)"
                else:
                    row.local_copy = "Kept"
            row.processing_time_seconds = time.time() - start


# ==========================================================================
# Window
# ==========================================================================
class DeeInitiatorWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, uiapp):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.uiapp = uiapp
        self.application = uiapp.Application
        self.logger = deew_logger.DeeWLogger(_TOOL_NAME)
        self._zones = []
        self._report_rows = []
        self._status_lines = []

        self._log("Ready. Click '+ Add Zone' to define a template + bulk name list and an ACC "
                  "destination, pick an Output Root Folder, Validate All Zones, then Run.")

    # ---------------- logging ----------------
    def _log(self, message):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self._status_lines.append("[{0}] {1}".format(ts, message))
        try:
            self.status_tb.Text = "\n".join(self._status_lines[-500:])
            self.status_tb.ScrollToEnd()
        except Exception:
            pass

    # ---------------- Output root folder ----------------
    def browse_root_click(self, sender, args):
        dlg = FolderBrowserDialog()
        if self.root_folder_tb.Text and os.path.isdir(self.root_folder_tb.Text):
            dlg.SelectedPath = self.root_folder_tb.Text
        if dlg.ShowDialog() == DialogResult.OK:
            self.root_folder_tb.Text = dlg.SelectedPath

    # ---------------- Zones: add / remove / build UI ----------------
    def add_zone_click(self, sender, args):
        zone = Zone()
        self._zones.append(zone)
        self.zones_panel.Children.Add(self._build_zone_ui(zone))
        self._update_run_enabled()
        self._log("Added Zone {0}.".format(zone.id))

    def _remove_zone(self, zone):
        if not forms.alert("Remove Zone {0}?".format(zone.id), title=_TOOL_TITLE, yes=True, no=True):
            return
        try:
            self.zones_panel.Children.Remove(zone.controls.get("root"))
        except Exception:
            pass
        if zone in self._zones:
            self._zones.remove(zone)
        self._update_run_enabled()
        self._log("Removed Zone {0}.".format(zone.id))

    def _build_zone_ui(self, zone):
        """Builds one Zone's GroupBox of controls in code (no static XAML
        for this - the zone count is unbounded, set by "+ Add Zone"
        clicks, same "build it in code because the count varies" reason
        DeeLazy.pushbutton/controller.py's _build_cards already states).
        Per-control closures are built via self._make_*_handler(zone) so
        each one is bound to ITS OWN zone, not whichever zone happened
        to be built last - same reasoning DeeLazy's own
        _wire_card_interaction states for its per-card closures."""
        root = GroupBox()
        root.Header = "Zone {0}".format(zone.id)
        root.Margin = Thickness(0, 0, 0, 10)
        root.Padding = Thickness(6)

        panel = StackPanel()

        # Row 1: template/RVT file
        row1 = StackPanel()
        row1.Orientation = Orientation.Horizontal
        row1.Margin = Thickness(0, 0, 0, 6)
        lbl1 = TextBlock()
        lbl1.Text = "Template / RVT File:"
        lbl1.Width = 140
        lbl1.VerticalAlignment = VerticalAlignment.Center
        row1.Children.Add(lbl1)
        template_tb = TextBox()
        template_tb.Width = 320
        template_tb.Height = 24
        template_tb.IsReadOnly = True
        template_tb.VerticalContentAlignment = VerticalAlignment.Center
        row1.Children.Add(template_tb)
        browse_b = Button()
        browse_b.Content = "Browse..."
        browse_b.Width = 90
        browse_b.Height = 24
        browse_b.Margin = Thickness(8, 0, 0, 0)
        browse_b.Click += self._make_browse_template_handler(zone)
        row1.Children.Add(browse_b)
        validate_file_b = Button()
        validate_file_b.Content = "Validate File"
        validate_file_b.Width = 100
        validate_file_b.Height = 24
        validate_file_b.Margin = Thickness(8, 0, 0, 0)
        validate_file_b.Click += self._make_validate_file_handler(zone)
        row1.Children.Add(validate_file_b)
        file_status_tb = TextBlock()
        file_status_tb.Margin = Thickness(10, 0, 0, 0)
        file_status_tb.VerticalAlignment = VerticalAlignment.Center
        file_status_tb.FontWeight = FontWeights.Bold
        row1.Children.Add(file_status_tb)
        panel.Children.Add(row1)

        # Row 2: bulk new-file names
        row2 = StackPanel()
        row2.Orientation = Orientation.Horizontal
        row2.Margin = Thickness(0, 0, 0, 6)
        lbl2 = TextBlock()
        lbl2.Text = "New File Names\n(one per line):"
        lbl2.Width = 140
        row2.Children.Add(lbl2)
        names_tb = TextBox()
        names_tb.Width = 320
        names_tb.Height = 90
        names_tb.AcceptsReturn = True
        names_tb.TextWrapping = TextWrapping.NoWrap
        names_tb.VerticalScrollBarVisibility = ScrollBarVisibility.Auto
        names_tb.HorizontalScrollBarVisibility = ScrollBarVisibility.Auto
        names_tb.TextChanged += self._make_names_changed_handler(zone)
        row2.Children.Add(names_tb)
        names_count_tb = TextBlock()
        names_count_tb.Text = "0 name(s)"
        names_count_tb.Margin = Thickness(10, 0, 0, 0)
        names_count_tb.VerticalAlignment = VerticalAlignment.Top
        row2.Children.Add(names_count_tb)
        panel.Children.Add(row2)

        # Row 3: ACC destination
        row3 = StackPanel()
        row3.Orientation = Orientation.Horizontal
        row3.Margin = Thickness(0, 0, 0, 6)
        lbl3 = TextBlock()
        lbl3.Text = "ACC Destination:"
        lbl3.Width = 140
        lbl3.VerticalAlignment = VerticalAlignment.Center
        row3.Children.Add(lbl3)
        pick_acc_b = Button()
        pick_acc_b.Content = "Pick ACC Destination..."
        pick_acc_b.Width = 160
        pick_acc_b.Height = 24
        pick_acc_b.Click += self._make_pick_acc_handler(zone)
        row3.Children.Add(pick_acc_b)
        acc_dest_tb = TextBlock()
        acc_dest_tb.Text = "No destination selected yet."
        acc_dest_tb.Width = 250
        acc_dest_tb.TextWrapping = TextWrapping.Wrap
        acc_dest_tb.VerticalAlignment = VerticalAlignment.Center
        acc_dest_tb.Margin = Thickness(8, 0, 0, 0)
        row3.Children.Add(acc_dest_tb)
        validate_acc_b = Button()
        validate_acc_b.Content = "Validate ACC"
        validate_acc_b.Width = 100
        validate_acc_b.Height = 24
        validate_acc_b.Margin = Thickness(8, 0, 0, 0)
        validate_acc_b.Click += self._make_validate_acc_handler(zone)
        row3.Children.Add(validate_acc_b)
        acc_status_tb = TextBlock()
        acc_status_tb.Margin = Thickness(10, 0, 0, 0)
        acc_status_tb.VerticalAlignment = VerticalAlignment.Center
        acc_status_tb.FontWeight = FontWeights.Bold
        row3.Children.Add(acc_status_tb)
        panel.Children.Add(row3)

        # Row 4: remove this zone
        row4 = StackPanel()
        row4.Orientation = Orientation.Horizontal
        row4.HorizontalAlignment = HorizontalAlignment.Right
        remove_b = Button()
        remove_b.Content = "Remove Zone"
        remove_b.Width = 100
        remove_b.Height = 22
        remove_b.Click += self._make_remove_zone_handler(zone)
        row4.Children.Add(remove_b)
        panel.Children.Add(row4)

        root.Content = panel

        zone.controls["root"] = root
        zone.controls["template_tb"] = template_tb
        zone.controls["file_status_tb"] = file_status_tb
        zone.controls["names_tb"] = names_tb
        zone.controls["names_count_tb"] = names_count_tb
        zone.controls["acc_dest_tb"] = acc_dest_tb
        zone.controls["acc_status_tb"] = acc_status_tb
        return root

    # ---------------- per-zone closures ----------------
    def _make_browse_template_handler(self, zone):
        def handler(sender, args):
            dlg = OpenFileDialog()
            dlg.Filter = "Revit Files (*.rvt;*.rte)|*.rvt;*.rte|All Files (*.*)|*.*"
            dlg.Title = "Pick a Template or RVT File"
            if dlg.ShowDialog() != DialogResult.OK:
                return
            zone.template_path = dlg.FileName
            zone.controls["template_tb"].Text = dlg.FileName
            zone.file_valid = None
            zone.controls["file_status_tb"].Text = ""
            self._update_run_enabled()
        return handler

    def _make_validate_file_handler(self, zone):
        def handler(sender, args):
            self._apply_file_validation(zone)
        return handler

    def _make_names_changed_handler(self, zone):
        def handler(sender, args):
            zone.bulk_names_text = zone.controls["names_tb"].Text
            zone.controls["names_count_tb"].Text = "{0} name(s)".format(len(zone.names()))
            if zone.file_valid is not None:
                zone.file_valid = None
                zone.controls["file_status_tb"].Text = ""
            self._update_run_enabled()
        return handler

    def _make_pick_acc_handler(self, zone):
        def handler(sender, args):
            self._log("Zone {0}: loading ACC Hubs/Projects/Folders...".format(zone.id))
            try:
                destination = cloudsvc.pick_destination()
            except Exception as e:
                forms.alert("Could not load ACC destinations: {0}".format(e))
                return
            if not destination:
                return
            zone.destination = destination
            zone.controls["acc_dest_tb"].Text = "{0} / {1} / {2}".format(
                destination.get("hub_name", "?"), destination.get("project_name", "?"),
                destination.get("folder_name", "?"))
            zone.acc_valid = None
            zone.controls["acc_status_tb"].Text = ""
            self._update_run_enabled()
            self._log("Zone {0} destination set: {1}".format(zone.id, zone.controls["acc_dest_tb"].Text))
        return handler

    def _make_validate_acc_handler(self, zone):
        def handler(sender, args):
            self._apply_acc_validation(zone)
        return handler

    def _make_remove_zone_handler(self, zone):
        def handler(sender, args):
            self._remove_zone(zone)
        return handler

    # ---------------- validation ----------------
    def _apply_file_validation(self, zone):
        ok, detail = validate_zone_file(zone)
        zone.file_valid = ok
        tb = zone.controls.get("file_status_tb")
        if tb is not None:
            tb.Text = "Validated" if ok else "Error: {0}".format(detail)
            tb.Foreground = _brush(_GREEN if ok else _RED)
        self._update_run_enabled()

    def _apply_acc_validation(self, zone):
        ok, detail = validate_zone_acc(zone)
        zone.acc_valid = ok
        tb = zone.controls.get("acc_status_tb")
        if tb is not None:
            tb.Text = "Validated" if ok else "Error: {0}".format(detail)
            tb.Foreground = _brush(_GREEN if ok else _RED)
        self._update_run_enabled()

    def validate_all_click(self, sender, args):
        if not self._zones:
            forms.alert("Add at least one Zone first.")
            return
        for zone in self._zones:
            self._apply_file_validation(zone)
            self._apply_acc_validation(zone)
        all_ok = all(z.file_valid and z.acc_valid for z in self._zones)
        self._log("Validate All Zones: {0}".format(
            "all zones passed" if all_ok else "one or more zones need attention - see red status text above"))

    def _update_run_enabled(self):
        try:
            self.run_b.IsEnabled = bool(self._zones) and all(
                z.file_valid and z.acc_valid for z in self._zones)
        except Exception:
            pass

    # ---------------- Run ----------------
    def run_click(self, sender, args):
        if not self._zones:
            forms.alert("Add at least one Zone first.")
            return
        root_folder = (self.root_folder_tb.Text or "").strip()
        if not root_folder or not os.path.isdir(root_folder):
            forms.alert("Pick a valid Output Root Folder first.")
            return
        # Defensive re-check - never trust only the Run button's own
        # IsEnabled state, since editing a zone's path/names/destination
        # after validating resets that zone's own status to None.
        if not all(z.file_valid and z.acc_valid for z in self._zones):
            forms.alert("Validate every Zone (file + ACC) before running - editing a zone after "
                         "validating it clears that zone's status.")
            return

        total = sum(len(z.names()) for z in self._zones)
        if total < 1:
            forms.alert("No file names to process - type at least one name into a Zone.")
            return

        if not forms.alert("Create and upload {0} file(s) across {1} zone(s)?".format(total, len(self._zones)),
                            title=_TOOL_TITLE + " - confirm", yes=True, no=True):
            return

        delete_after_upload = bool(self.delete_after_upload_cb.IsChecked)
        pipeline = InitiatorPipeline(self.application, delete_after_upload, self.logger)
        revit_version_text = _revit_version_text(self.application)

        dialog_handler = ffh.make_dialog_handler(self.logger)
        try:
            self.uiapp.DialogBoxShowing += dialog_handler
        except Exception as e:
            self.logger.exception("Could not attach dialog handler", e)

        report_rows = []
        try:
            with progsvc.DeeWProgressService(_TOOL_TITLE, total) as prog:
                for zone in self._zones:
                    if prog.cancelled:
                        self._log("Cancelled by user.")
                        break
                    zone_folder = os.path.join(root_folder, "Zone {0}".format(zone.id))
                    try:
                        if not os.path.isdir(zone_folder):
                            os.makedirs(zone_folder)
                    except Exception as e:
                        self._log("Zone {0}: could not create output folder - {1}".format(zone.id, e))
                        continue
                    for name in zone.names():
                        if prog.cancelled:
                            self._log("Cancelled by user.")
                            break
                        prog.step(name, "Duplicating + uploading")
                        row = pipeline.process_one(zone, name, zone_folder, revit_version_text)
                        report_rows.append(row)
                        outcome = "success" if row.upload_status == "Uploaded" else "failed"
                        prog.finish_file(outcome)
                        self._log("Zone {0} - '{1}': {2}".format(zone.id, name, row.upload_status))
        finally:
            try:
                self.uiapp.DialogBoxShowing -= dialog_handler
            except Exception:
                pass

        self._report_rows = report_rows
        self.report_grid.ItemsSource = None
        self.report_grid.ItemsSource = report_rows

        uploaded = sum(1 for r in report_rows if r.upload_status == "Uploaded")
        failed = sum(1 for r in report_rows if "fail" in r.upload_status.lower())
        self.summary_tb.Text = "{0} processed: {1} uploaded, {2} failed.".format(
            len(report_rows), uploaded, failed)
        self._log(self.summary_tb.Text)
        forms.alert(self.summary_tb.Text, title=_TOOL_TITLE)
        try:
            self.main_tabs.SelectedIndex = 1
        except Exception:
            pass

    # ---------------- Export / Close ----------------
    def export_report_click(self, sender, args):
        if not self._report_rows:
            forms.alert("Run the tool first - nothing to export yet.")
            return
        dlg = SaveFileDialog()
        dlg.Filter = "Excel Workbook (*.xlsx)|*.xlsx|CSV (*.csv)|*.csv|Text (*.txt)|*.txt"
        dlg.FileName = "DeeInitiator_Report.xlsx"
        if dlg.ShowDialog() != DialogResult.OK:
            return
        try:
            reportgen.export(dlg.FileName, "DeeInitiator - Batch Create & Publish Report",
                              self._report_rows, headers=HEADERS, col_widths=COL_WIDTHS)
        except Exception as e:
            forms.alert("Could not export report: {0}".format(e))
            return
        MessageBox.Show("Report exported to:\n{0}".format(dlg.FileName), _TOOL_TITLE)

    def export_linkmap_click(self, sender, args):
        """Second export: a simple handoff file naming exactly the files
        this run uploaded, for DeeMAPLink's "Import file" source mode
        (see module docstring) - so the user can wire up Revit Links
        between the new files without re-scanning the whole ACC project."""
        uploaded_rows = [r for r in self._report_rows if r.upload_status == "Uploaded"]
        if not uploaded_rows:
            forms.alert("Run the tool first and have at least one file upload successfully - "
                         "nothing to export yet.")
            return

        zone_by_id = dict((z.id, z) for z in self._zones)

        # Resolve item ids project-by-project (one search per distinct
        # project, not one per file) - mirrors DeeWSharing's own
        # _existing_cloud_names caching shape.
        self._log("Resolving cloud file ids for the link-setup export...")
        by_project = {}
        for row in uploaded_rows:
            zone = zone_by_id.get(row.zone_id)
            if zone is None or not zone.destination:
                continue
            by_project.setdefault(zone.destination["project_id"], []).append((row, zone))

        resolved = {}
        for project_id, pairs in by_project.items():
            token = pairs[0][1].destination.get("token") or cloudsvc.get_token()
            expected_names = set()
            for row, _zone in pairs:
                name = row.new_file_name
                expected_names.add(name if name.lower().endswith(".rvt") else name + ".rvt")
            found = _resolve_item_ids(project_id, token, expected_names, self.logger)
            resolved[project_id] = found

        link_rows = []
        unresolved_count = 0
        for row in uploaded_rows:
            zone = zone_by_id.get(row.zone_id)
            if zone is None or not zone.destination:
                self._log("Zone {0} - '{1}': no longer available, skipped from link-setup export.".format(
                    row.zone_id, row.new_file_name))
                continue
            name = row.new_file_name
            display_name = name if name.lower().endswith(".rvt") else name + ".rvt"
            item_id = resolved.get(zone.destination["project_id"], {}).get(display_name, "")
            if not item_id:
                item_id = _UNRESOLVED_ITEM_ID
                unresolved_count += 1
            link_rows.append(LinkSetupRow(row.zone_id, display_name, item_id, zone.destination))

        dlg = SaveFileDialog()
        dlg.Filter = "CSV (*.csv)|*.csv"
        dlg.FileName = "DeeInitiator_LinkSetup.csv"
        if dlg.ShowDialog() != DialogResult.OK:
            return
        try:
            reportgen.export_csv(dlg.FileName, link_rows, headers=_LINKMAP_HEADERS)
        except Exception as e:
            forms.alert("Could not export the link-setup file: {0}".format(e))
            return

        note = ""
        if unresolved_count:
            note = ("\n\n{0} file(s) could not have their cloud id resolved yet (Autodesk's search "
                     "index can lag a little after upload) - marked UNRESOLVED and will be skipped "
                     "by DeeMAPLink's import. Re-export in a minute if you need them too.").format(
                unresolved_count)
        MessageBox.Show("Link-setup file exported to:\n{0}\n\nOpen DeeMAPLink, choose "
                         "'Import file', and pick this CSV to wire up Revit Links between these "
                         "new files.{1}".format(dlg.FileName, note), _TOOL_TITLE)
        self._log("Exported link-setup file ({0} file(s), {1} unresolved) to {2}".format(
            len(link_rows), unresolved_count, dlg.FileName))

    def cancel_click(self, sender, args):
        self.Close()


def main():
    uiapp = __revit__
    window = DeeInitiatorWindow(_XAML_FILE, uiapp)
    window.ShowDialog()


main()
