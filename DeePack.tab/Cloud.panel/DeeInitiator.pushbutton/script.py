# -*- coding: utf-8 -*-
"""
DeeInitiator
Batch-creates new Revit project files from a template across any number
of user-defined "Delivery Parties" (e.g. Architecture, Structure, MEP),
uploads every one of them to Autodesk Construction Cloud (ACC) as a new
worksharing-enabled Cloud Model, then lets the user wire up real Revit
Links between the newly-created files, in-app - no second tool needed.

Each Delivery Party pairs a name (picked from a preset list or typed -
"Save as Preset" adds it for next time) with ONE template/RVT file, a
bulk list of new file names (one per line), and its OWN ACC destination
(Hub/Project/Folder - ACC has no raw filesystem path, so a browsed
destination IS "the ACC path", exactly like every other ACC tool in
this repo). Delivery Parties are added one at a time by clicking "+ Add
Delivery Party" - unbounded, not a fixed pre-built count - per explicit
user request ("I prefer clicking + [to add zones] rather than a tool
pre-built with 8 zones toggled by checkboxes").

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
- The dynamic "+ Add Delivery Party" WPF UI (building a GroupBox of
  controls in code and appending it to a StackPanel at runtime, per
  click) has no precedent in this repo for a USER-TRIGGERED repeat-
  and-append interaction - the closest analogs (DeeLazy.pushbutton/
  controller.py's _build_cards, DeeRelink.pushbutton/script.py's
  _build_tabs) both build a FIXED, pre-known set once at window-open.
  The underlying technique (constructing WPF controls via direct .NET
  constructors and wiring per-instance closures via Click +=) is the
  same proven mechanism those two already use, just applied to a
  user-clicked "+" instead of a startup loop.

--------------------------------------------------------------------
In-app linking - Link Management tab, by explicit user request
(replacing an earlier CSV handoff to a separate tool)
--------------------------------------------------------------------
An earlier version of this tool exported a CSV for DeeMAPLink
(DeePack.tab/Coordination.panel/CoordViewStack.stack/DeeMAPLink.
pushbutton) to import. The user asked for that workflow brought INSIDE
DeeInitiator instead - one more tab, reusing DeeMAPLink's own proven
UI/pipeline rather than reinventing it: "Link Management" ports
DeeMAPLink's "Wire Map" tab - two columns of files, click one then the
other, a Canvas-drawn line (via TranslatePoint, the same technique
DeeMAPLink already proves live) connects them. (DeeMAPLink's OTHER tab,
"Two Lists" - tick-both-sides, cross-multiply into matches - was also
ported initially but removed again by explicit user request, keeping
only the wire-map style interaction.)
self._matches feeds the actual link-creation work in _execute_link_run,
shared by TWO callers, by explicit user request ("I need it one RUN"):
the main "Run" button calls it automatically right after a successful
upload (interactive=False - no second confirmation, folded into Run's
own one summary/report), and the standalone "Run Links" button calls it
on its own (interactive=True - its own confirmation/alerts), kept
around as a manual way to retry just the linking step later (e.g. a
sync failure needs retrying, or matches were added/changed after the
last Run) without re-creating/re-uploading every file. Either way, a
match pointing at a file that isn't uploaded yet is reported and
skipped, never guessed at - see _execute_link_run's own docstring.
Reused UNCHANGED: lib/dee_maplink_service.py (matching/
grouping/estimate/link_into), lib/dee_link_create_service.py (the
RevitLinkType.Create/RevitLinkInstance.Create primitive, via
link_into), lib/acc_file_browser.py (open_cloud_document_attached,
cloud_model_path), lib/deew_document_manager.py (synchronize_with_
central, close_document).

One real structural difference from DeeMAPLink: DeeMAPLink assumes ONE
ACC project for its whole run (a live scan of one project). DeeInitiator's
Delivery Parties can each upload to a DIFFERENT ACC project, so each
linkable file (LinkFileRef) carries its OWN project_id/region rather
than reading one window-wide field - _open_link_target/
_link_model_path_for read it from the file reference, not from self.

The file lists are never a live ACC scan here, and - by explicit user
request ("make the links before the Run") - they do NOT require a
successful Run first either. Two tiers:
  - PLANNED (self._build_planned_link_files/_refresh_planned_link_files):
    a cheap, local-only list built straight from every Delivery Party's
    own typed bulk names + its ACC destination, with item_id=None. Runs
    on every names/destination edit and on add/remove Delivery Party, so
    Link Management always shows what's currently planned, even before
    anything has been created. No ACC API call.
  - RESOLVED (self._refresh_link_files, the "Refresh File List" button
    and the automatic call at the end of a successful Run): starts from
    the same planned list, then looks up the real ACC item id for
    whichever names have actually been uploaded this session
    (self._report_rows where Uploaded) via acc_api.search_cloud_models
    (the same function DeeWSharing's own _existing_cloud_names already
    uses for name-collision checking, just read here for its id instead
    of its name) - Document.SaveAsCloudModel never hands that id back
    directly. A short, documented best-effort retry, not a guarantee.
Matches (self._matches) can therefore be built against PLANNED names at
any time; run_links_click only ever acts on a match whose LinkFileRef
has a resolved item_id - a planned name with no real file yet is
reported and skipped, never opened by guessing at an id.

Scope limit, not silently dropped: DeeMAPLink's Wire Map tab also has
per-column discipline-segment filter dropdowns (AR/ST/ME/...) for
narrowing hundreds of pre-existing project files. DeeInitiator's own
file lists are just THIS run's own small batch of newly-created files,
so that filtering layer is not ported here - a plain search box per
column is enough at this scale.

--------------------------------------------------------------------
Delivery Party name presets
--------------------------------------------------------------------
A separate, DeeInitiator-only preset list (lib/deew_settings.py, the
same load/save idiom DeeVSDupl's own selection presets and DeeMAPLink's
own resumable-batch tracking already use) - NOT dee_linkmap_service's
shared .dee_linkmap_disciplines.json, which is specifically for
DeeLinkMAP's own file-name discipline-DETECTION logic (regex segment
matching + hash-based coloring); reusing that file would couple this
tool's simple name presets to unrelated detection semantics and let an
edit in one tool's config silently change the other's. Seeded with
common AEC disciplines; "Save as Preset" on any Delivery Party's name
field adds whatever is typed, instantly visible in every other open
Delivery Party's own name dropdown.

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
- Duplicate name LINES typed into the same Delivery Party's bulk box
  are silently de-duplicated (DeliveryParty.names()), not flagged as
  an error.
- "Validate ACC" confirms the destination is populated (hub/project/
  folder ids all present) and that a token can currently be fetched -
  it does NOT re-walk the Hub/Project/Folder tree to confirm the
  folder still exists on Autodesk's side (no cheap existing API call
  does this). A folder deleted between Validate and Run only surfaces
  as a failed upload in the report, never pre-emptively in green/red.
- Only one Revit document is open at a time, processed start-to-finish
  before the next starts - no parallelism across parties, files, or
  link hosts, matching deew_document_manager's own stated memory-
  release discipline.
- No retry/resume for the CREATE step - a cancelled or partially-
  failed Run must be re-triggered manually; already-uploaded files are
  not detected or skipped on a second Run. The LINK step (Run Links)
  DOES have resumable-batch tracking (same crash-mid-batch safety net
  DeeMAPLink itself has, since a huge host model can crash Revit
  natively mid-batch) - already-synchronized hosts are skipped on a
  re-run unless history is cleared.
- The Output Root Folder is ONE shared location for the whole run, with
  one auto-created subfolder per Delivery Party ("<root>/01 -
  Architecture/", "<root>/02 - Structure/", ...) - not a separate
  picker per party.
- "Delete local copy after successful upload" is ONE global checkbox
  for the whole run, not per-party - and only ever deletes AFTER a
  CONFIRMED successful upload; a failed upload always leaves the local
  duplicate in place so nothing is silently lost.
- Linking across Delivery Parties that uploaded to DIFFERENT ACC
  projects is supported structurally (per-file project/region), but is
  only as reliable as Revit's own cross-project cloud-link support -
  not something verifiable without a live multi-project test.
"""
import os
import time
import shutil
import datetime

import clr
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")
clr.AddReference("System.Windows.Forms")
from System import Action
from System.Windows import (Thickness, FontWeights, HorizontalAlignment, VerticalAlignment,
                            TextWrapping, Point, TextTrimming)
from System.Windows.Controls import (GroupBox, StackPanel, Orientation, TextBox, TextBlock,
                                     Button, ScrollBarVisibility, Border, Canvas, ComboBox)
from System.Windows.Media import SolidColorBrush, Color, Brushes, PointCollection
from System.Windows.Shapes import Line, Polygon
from System.Windows.Input import Cursors
from System.Windows.Threading import Dispatcher, DispatcherFrame, DispatcherPriority
from System.Windows.Forms import FolderBrowserDialog, OpenFileDialog, SaveFileDialog, DialogResult, MessageBox

from Autodesk.Revit.DB import ImportPlacement, AttachmentType

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
import acc_file_browser as afb
import dee_maplink_service as dms
import acc_api

import dee_telemetry
dee_telemetry.check_access("DeeInitiator")


output = script.get_output()

_TOOL_NAME = "DeeInitiator"
_TOOL_TITLE = "DeeInitiator"

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")

_VALID_EXTENSIONS = (".rvt", ".rte")
_INVALID_FOLDER_CHARS = set('\\/:*?"<>|')

_GREEN = "#2e7d32"
_RED = "#c62828"

HEADERS = ["Delivery Party", "Template Used", "Local Output Folder", "New File Name",
           "File Validation", "ACC Hub / Project / Folder", "Upload Status",
           "Worksharing Enabled", "Local Copy", "Duration", "Errors",
           "Date", "Revit Version", "User"]
COL_WIDTHS = [14, 34, 34, 26, 16, 34, 18, 16, 14, 12, 30, 18, 12, 16]

_PARTY_PRESET_TOOL_NAME = "dee_initiator_party_presets"
_DEFAULT_PARTY_PRESETS = ["Architecture", "Structure", "MEP", "Electrical",
                          "Plumbing", "Civil", "Landscape", "Interior Design"]

_LINK_PROGRESS_TOOL_NAME = "DeeInitiator_link_progress"


def _brush(hex_color):
    h = hex_color.lstrip("#")
    return SolidColorBrush(Color.FromRgb(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)))


def _revit_version_text(app):
    try:
        return str(app.VersionNumber)
    except Exception:
        return "Unknown"


def _safe_folder_name(text):
    cleaned = "".join(c if c not in _INVALID_FOLDER_CHARS else "_" for c in (text or "").strip())
    return cleaned or "Party"


def _load_party_presets():
    data = deew_settings.load(_PARTY_PRESET_TOOL_NAME, {"presets": _DEFAULT_PARTY_PRESETS})
    presets = data.get("presets")
    return list(presets) if presets else list(_DEFAULT_PARTY_PRESETS)


def _save_party_presets(presets):
    deew_settings.save(_PARTY_PRESET_TOOL_NAME, {"presets": presets})


# ==========================================================================
# DeliveryParty - plain data holder, one per Delivery Party, living
# alongside that party's own WPF controls (same "data object next to
# its own UI" pattern this codebase already uses for DataGrid rows,
# just applied to a bigger per-party block instead of a grid row).
# ==========================================================================
class DeliveryParty(object):
    _next_id = [1]

    def __init__(self):
        self.id = DeliveryParty._next_id[0]
        DeliveryParty._next_id[0] += 1
        self.name = ""
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


def validate_party_file(party):
    """Returns (ok, detail). Checks the template path exists, has a
    .rvt/.rte extension, passes scanner.scan_file's closed-file
    corrupted/read-only check, and that at least one new-file name was
    typed. Never raises."""
    path = (party.template_path or "").strip()
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
    if not party.names():
        return False, "Type at least one new file name (one per line)"
    return True, "Validated"


def validate_party_acc(party):
    """Returns (ok, detail). ACC has no literal path - "validated" means
    a destination was actually picked (Hub/Project/Folder ids all
    present) and a token can still be fetched right now. Does NOT
    re-confirm the folder still exists on Autodesk's side - see module
    docstring's Scope Limits."""
    dest = party.destination
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
    def __init__(self, party_id, template_used, output_folder, new_file_name, revit_version):
        self.party_id = party_id
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
            self.party_id, self.template_used, self.output_folder, self.new_file_name,
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


def _resolve_item_ids(project_id, token, expected_names, logger, attempts=8, delay_seconds=4.0, on_attempt=None):
    """Returns {expected_display_name: item_id}, best-effort. Document.
    SaveAsCloudModel (deew_cloud_service.save_to_cloud) never hands back
    the new cloud item's id, so it has to be looked up afterward via
    acc_api.search_cloud_models(project_id, token) - the SAME function
    DeeWSharing's own _existing_cloud_names already uses for name-
    collision checking, just used here to read an id instead.

    The search hits Autodesk's own index, which can lag noticeably
    behind the upload transaction that just completed - live-observed
    to take longer than a few seconds for a BRAND NEW model (unlike
    DeeWSharing's own use of this same function, which only ever checks
    EXISTING names before upload, never a just-created one) - so this
    retries up to `attempts` times with a real pause between them rather
    than assumed to be immediately consistent. on_attempt(attempt,
    attempts, found_count, remaining_count), if given, is called after
    every round so the caller can show live progress instead of the UI
    looking hung during the wait. A name still not found after every
    attempt is simply absent from the returned dict - the caller leaves
    it out of the linkable file list rather than guessing at an id.

    If acc_api.search_cloud_models itself raises on EVERY attempt (e.g.
    an expired token), `found` stays empty for every name - logged via
    logger.exception each time, not silently swallowed."""
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
        if on_attempt is not None:
            try:
                on_attempt(attempt + 1, attempts, len(found), len(remaining))
            except Exception:
                pass
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

    def process_one(self, party, new_name, party_output_folder, revit_version_text):
        row = InitiatorReportRow(party.id, party.template_path, party_output_folder, new_name, revit_version_text)
        row.acc_destination_text = "{0} / {1} / {2}".format(
            party.destination.get("hub_name", "?"), party.destination.get("project_name", "?"),
            party.destination.get("folder_name", "?"))
        start = time.time()
        document = None
        target_path = None
        try:
            target_path = docmgr.unique_target_path(party_output_folder, new_name + ".rvt")
            try:
                shutil.copy2(party.template_path, target_path)
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

            success, detail = cloudsvc.save_to_cloud(document, party.destination, new_name)
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
# Link-setup data model - populated from THIS run's own uploaded files,
# never a live ACC scan (see module docstring).
# ==========================================================================
class LinkFileRef(object):
    """One file available to link. Carries its OWN project/region -
    unlike DeeMAPLink (one ACC project per run), different Delivery
    Parties here can upload to different ACC projects."""
    def __init__(self, name, item_id, project_id, region, hub_id):
        self.name = name
        self.item_id = item_id
        self.project_id = project_id
        self.region = region
        self.hub_id = hub_id


class MatchRow(object):
    """One row in the shared matches grid - plain source/target strings,
    the same shape dee_maplink_service.build_matches() works with."""
    def __init__(self, source, target):
        self.source = source
        self.target = target


# ==========================================================================
# Window
# ==========================================================================
class DeeInitiatorWindow(dee_branding.DeeBrandedWindow):
    _WIRE_BRUSH = SolidColorBrush(Color.FromRgb(0xF2, 0x99, 0x4D))
    _ROW_BG = SolidColorBrush(Color.FromRgb(0x2B, 0x2B, 0x2B))
    _ROW_ARMED = SolidColorBrush(Color.FromRgb(0x4A, 0x3A, 0x22))

    def __init__(self, xaml_file, uiapp):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.uiapp = uiapp
        self.application = uiapp.Application
        self.logger = deew_logger.DeeWLogger(_TOOL_NAME)
        self._parties = []
        self._report_rows = []
        self._status_lines = []

        # Link-setup state, driving the Link Management tab's wire map.
        self._link_files = {}      # {display_name: LinkFileRef}
        self._matches = []         # [(source_name, target_name), ...]
        self._map_left_rows = {}
        self._map_right_rows = {}
        self._map_pending = None
        self._map_ready = False

        self.link_placement_cb.ItemsSource = [label for label, _v in dms.PLACEMENT_OPTIONS]
        self.link_placement_cb.SelectedIndex = 0
        self._refresh_link_matches()

        self._log("Ready. Click '+ Add Delivery Party' to define a name, template + bulk name "
                  "list, and an ACC destination, pick an Output Root Folder, Validate All "
                  "Parties, then Run.")

        # Canvas handlers live on the scroll viewers/canvas itself, not
        # per-row - a scroll or resize has to redraw every wire, not just
        # the one under the cursor.
        self.link_map_left_scroll.ScrollChanged += self._map_scrolled
        self.link_map_right_scroll.ScrollChanged += self._map_scrolled
        self.link_map_canvas.SizeChanged += self._map_scrolled
        self._map_ready = True

    # ---------------- logging ----------------
    def _log(self, message):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self._status_lines.append("[{0}] {1}".format(ts, message))
        try:
            self.status_tb.Text = "\n".join(self._status_lines[-500:])
            self.status_tb.ScrollToEnd()
        except Exception:
            pass

    def _safe_text(self, textbox):
        try:
            return textbox.Text or ""
        except Exception:
            return ""

    # ---------------- Output root folder ----------------
    def browse_root_click(self, sender, args):
        dlg = FolderBrowserDialog()
        if self.root_folder_tb.Text and os.path.isdir(self.root_folder_tb.Text):
            dlg.SelectedPath = self.root_folder_tb.Text
        if dlg.ShowDialog() == DialogResult.OK:
            self.root_folder_tb.Text = dlg.SelectedPath

    # ---------------- Delivery Parties: add / remove / build UI ----------------
    def add_party_click(self, sender, args):
        party = DeliveryParty()
        self._parties.append(party)
        self.zones_panel.Children.Add(self._build_party_ui(party))
        self._update_run_enabled()
        self._log("Added Delivery Party {0}.".format(party.id))

    def _remove_party(self, party):
        if not forms.alert("Remove Delivery Party {0}?".format(party.id), title=_TOOL_TITLE, yes=True, no=True):
            return
        try:
            self.zones_panel.Children.Remove(party.controls.get("root"))
        except Exception:
            pass
        if party in self._parties:
            self._parties.remove(party)
        self._update_run_enabled()
        self._log("Removed Delivery Party {0}.".format(party.id))
        self._refresh_planned_link_files()

    def _build_party_ui(self, party):
        """Builds one Delivery Party's GroupBox of controls in code (no
        static XAML for this - the party count is unbounded, set by
        "+ Add Delivery Party" clicks, same "build it in code because
        the count varies" reason DeeLazy.pushbutton/controller.py's
        _build_cards already states). Per-control closures are built via
        self._make_*_handler(party) so each one is bound to ITS OWN
        party, not whichever party happened to be built last - same
        reasoning DeeLazy's own _wire_card_interaction states for its
        per-card closures."""
        root = GroupBox()
        root.Header = "Delivery Party {0}".format(party.id)
        root.Margin = Thickness(0, 0, 0, 10)
        root.Padding = Thickness(6)

        panel = StackPanel()

        # Row 0: delivery party name (preset combo)
        row0 = StackPanel()
        row0.Orientation = Orientation.Horizontal
        row0.Margin = Thickness(0, 0, 0, 6)
        lbl0 = TextBlock()
        lbl0.Text = "Delivery Party Name:"
        lbl0.Width = 140
        lbl0.VerticalAlignment = VerticalAlignment.Center
        row0.Children.Add(lbl0)
        name_cb = ComboBox()
        name_cb.Width = 220
        name_cb.Height = 24
        name_cb.IsEditable = True
        name_cb.ItemsSource = _load_party_presets()
        name_cb.Text = party.name
        row0.Children.Add(name_cb)
        name_cb.LostFocus += self._make_party_name_handler(party, name_cb)
        name_cb.SelectionChanged += self._make_party_name_handler(party, name_cb)
        save_preset_b = Button()
        save_preset_b.Content = "Save as Preset"
        save_preset_b.Width = 120
        save_preset_b.Height = 24
        save_preset_b.Margin = Thickness(8, 0, 0, 0)
        save_preset_b.Click += self._make_save_preset_handler(name_cb)
        row0.Children.Add(save_preset_b)
        panel.Children.Add(row0)

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
        browse_b.Click += self._make_browse_template_handler(party)
        row1.Children.Add(browse_b)
        validate_file_b = Button()
        validate_file_b.Content = "Validate File"
        validate_file_b.Width = 100
        validate_file_b.Height = 24
        validate_file_b.Margin = Thickness(8, 0, 0, 0)
        validate_file_b.Click += self._make_validate_file_handler(party)
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
        names_tb.TextChanged += self._make_names_changed_handler(party)
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
        pick_acc_b.Click += self._make_pick_acc_handler(party)
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
        validate_acc_b.Click += self._make_validate_acc_handler(party)
        row3.Children.Add(validate_acc_b)
        acc_status_tb = TextBlock()
        acc_status_tb.Margin = Thickness(10, 0, 0, 0)
        acc_status_tb.VerticalAlignment = VerticalAlignment.Center
        acc_status_tb.FontWeight = FontWeights.Bold
        row3.Children.Add(acc_status_tb)
        panel.Children.Add(row3)

        # Row 4: remove this party
        row4 = StackPanel()
        row4.Orientation = Orientation.Horizontal
        row4.HorizontalAlignment = HorizontalAlignment.Right
        remove_b = Button()
        remove_b.Content = "Remove Delivery Party"
        remove_b.Width = 140
        remove_b.Height = 22
        remove_b.Click += self._make_remove_party_handler(party)
        row4.Children.Add(remove_b)
        panel.Children.Add(row4)

        root.Content = panel

        party.controls["root"] = root
        party.controls["name_cb"] = name_cb
        party.controls["template_tb"] = template_tb
        party.controls["file_status_tb"] = file_status_tb
        party.controls["names_tb"] = names_tb
        party.controls["names_count_tb"] = names_count_tb
        party.controls["acc_dest_tb"] = acc_dest_tb
        party.controls["acc_status_tb"] = acc_status_tb
        return root

    # ---------------- per-party closures ----------------
    def _make_party_name_handler(self, party, name_cb):
        def handler(sender, args):
            party.name = (name_cb.Text or "").strip()
        return handler

    def _make_save_preset_handler(self, name_cb):
        def handler(sender, args):
            text = (name_cb.Text or "").strip()
            if not text:
                return
            presets = _load_party_presets()
            if text in presets:
                self._log("'{0}' is already a preset.".format(text))
                return
            presets.append(text)
            _save_party_presets(presets)
            self._refresh_all_party_name_presets()
            self._log("Added '{0}' to Delivery Party presets.".format(text))
        return handler

    def _refresh_all_party_name_presets(self):
        presets = _load_party_presets()
        for party in self._parties:
            cb = party.controls.get("name_cb")
            if cb is None:
                continue
            current = cb.Text
            cb.ItemsSource = presets
            cb.Text = current

    def _make_browse_template_handler(self, party):
        def handler(sender, args):
            dlg = OpenFileDialog()
            dlg.Filter = "Revit Files (*.rvt;*.rte)|*.rvt;*.rte|All Files (*.*)|*.*"
            dlg.Title = "Pick a Template or RVT File"
            if dlg.ShowDialog() != DialogResult.OK:
                return
            party.template_path = dlg.FileName
            party.controls["template_tb"].Text = dlg.FileName
            party.file_valid = None
            party.controls["file_status_tb"].Text = ""
            self._update_run_enabled()
        return handler

    def _make_validate_file_handler(self, party):
        def handler(sender, args):
            self._apply_file_validation(party)
        return handler

    def _make_names_changed_handler(self, party):
        def handler(sender, args):
            party.bulk_names_text = party.controls["names_tb"].Text
            party.controls["names_count_tb"].Text = "{0} name(s)".format(len(party.names()))
            if party.file_valid is not None:
                party.file_valid = None
                party.controls["file_status_tb"].Text = ""
            self._update_run_enabled()
            self._refresh_planned_link_files()
        return handler

    def _make_pick_acc_handler(self, party):
        def handler(sender, args):
            self._log("Delivery Party {0}: loading ACC Hubs/Projects/Folders...".format(party.id))
            try:
                destination = cloudsvc.pick_destination()
            except Exception as e:
                forms.alert("Could not load ACC destinations: {0}".format(e))
                return
            if not destination:
                return
            party.destination = destination
            party.controls["acc_dest_tb"].Text = "{0} / {1} / {2}".format(
                destination.get("hub_name", "?"), destination.get("project_name", "?"),
                destination.get("folder_name", "?"))
            party.acc_valid = None
            party.controls["acc_status_tb"].Text = ""
            self._update_run_enabled()
            self._log("Delivery Party {0} destination set: {1}".format(party.id, party.controls["acc_dest_tb"].Text))
            self._refresh_planned_link_files()
        return handler

    def _make_validate_acc_handler(self, party):
        def handler(sender, args):
            self._apply_acc_validation(party)
        return handler

    def _make_remove_party_handler(self, party):
        def handler(sender, args):
            self._remove_party(party)
        return handler

    # ---------------- validation ----------------
    def _apply_file_validation(self, party):
        ok, detail = validate_party_file(party)
        party.file_valid = ok
        tb = party.controls.get("file_status_tb")
        if tb is not None:
            tb.Text = "Validated" if ok else "Error: {0}".format(detail)
            tb.Foreground = _brush(_GREEN if ok else _RED)
        self._update_run_enabled()

    def _apply_acc_validation(self, party):
        ok, detail = validate_party_acc(party)
        party.acc_valid = ok
        tb = party.controls.get("acc_status_tb")
        if tb is not None:
            tb.Text = "Validated" if ok else "Error: {0}".format(detail)
            tb.Foreground = _brush(_GREEN if ok else _RED)
        self._update_run_enabled()

    def validate_all_click(self, sender, args):
        if not self._parties:
            forms.alert("Add at least one Delivery Party first.")
            return
        for party in self._parties:
            self._apply_file_validation(party)
            self._apply_acc_validation(party)
        all_ok = all(p.file_valid and p.acc_valid for p in self._parties)
        self._log("Validate All Delivery Parties: {0}".format(
            "all passed" if all_ok else "one or more need attention - see red status text above"))

    def _update_run_enabled(self):
        try:
            self.run_b.IsEnabled = bool(self._parties) and all(
                p.file_valid and p.acc_valid for p in self._parties)
        except Exception:
            pass

    # ---------------- Run (create + upload) ----------------
    def run_click(self, sender, args):
        if not self._parties:
            forms.alert("Add at least one Delivery Party first.")
            return
        root_folder = (self.root_folder_tb.Text or "").strip()
        if not root_folder or not os.path.isdir(root_folder):
            forms.alert("Pick a valid Output Root Folder first.")
            return
        # Defensive re-check - never trust only the Run button's own
        # IsEnabled state, since editing a party's path/names/destination
        # after validating resets that party's own status to None.
        if not all(p.file_valid and p.acc_valid for p in self._parties):
            forms.alert("Validate every Delivery Party (file + ACC) before running - editing a "
                         "party after validating it clears that party's status.")
            return

        total = sum(len(p.names()) for p in self._parties)
        if total < 1:
            forms.alert("No file names to process - type at least one name into a Delivery Party.")
            return

        match_note = ""
        if self._matches:
            match_note = (" Afterward, the {0} link match(es) already set up on Link Management "
                           "will be created too, for whichever ones are ready.".format(len(self._matches)))
        if not forms.alert("Create and upload {0} file(s) across {1} Delivery Part{2}?{3}".format(
                total, len(self._parties), "y" if len(self._parties) == 1 else "ies", match_note),
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
                for party in self._parties:
                    if prog.cancelled:
                        self._log("Cancelled by user.")
                        break
                    party_folder = os.path.join(
                        root_folder, "{0:02d} - {1}".format(party.id, _safe_folder_name(party.name)))
                    try:
                        if not os.path.isdir(party_folder):
                            os.makedirs(party_folder)
                    except Exception as e:
                        self._log("Delivery Party {0}: could not create output folder - {1}".format(party.id, e))
                        continue
                    for name in party.names():
                        if prog.cancelled:
                            self._log("Cancelled by user.")
                            break
                        prog.step(name, "Duplicating + uploading")
                        row = pipeline.process_one(party, name, party_folder, revit_version_text)
                        report_rows.append(row)
                        outcome = "success" if row.upload_status == "Uploaded" else "failed"
                        prog.finish_file(outcome)
                        self._log("Delivery Party {0} - '{1}': {2}".format(party.id, name, row.upload_status))
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

        if uploaded:
            self._refresh_link_files()

        # One Run, one result - chain straight into the already-set-up
        # link matches (if any) rather than making the user click a
        # second "Run Links" button, per explicit user request. A match
        # pointing at a file that failed to upload is simply not ready
        # yet (see _execute_link_run) and is reported, not silently lost.
        if self._matches:
            link_results, link_groups = self._execute_link_run(interactive=False)
            if link_groups:
                link_ok = sum(1 for r in link_results if r[0] is True)
                link_fail = sum(1 for r in link_results if r[0] is False)
                self.link_run_status_tb.Text = (
                    "{0} succeeded, {1} failed. See the Log tab / pyRevit output.".format(link_ok, link_fail))
                self._log(self.link_run_status_tb.Text)
                self._print_link_report(link_results)
                self.summary_tb.Text += "  Links: {0} succeeded, {1} failed.".format(link_ok, link_fail)

        forms.alert(self.summary_tb.Text, title=_TOOL_TITLE)

        try:
            self.main_tabs.SelectedIndex = 2
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

    def cancel_click(self, sender, args):
        self.Close()

    # ======================================================================
    # Link setup - drives the Link Management tab
    # ======================================================================

    # ---------------- populating the linkable file list ----------------
    # Two tiers, by explicit user request ("make the links before the
    # Run"): a cheap, local-only PLANNED list (every Delivery Party's own
    # typed names, no network call) that updates live as the user types
    # or picks an ACC destination - so matches can be built on Link
    # Management before anything is ever created - and a fuller
    # RESOLVED pass (the Refresh File List button, and the automatic call
    # at the end of a successful Run) that looks up the real ACC item id
    # for whichever of those names have actually been uploaded. A
    # LinkFileRef's item_id stays None until resolved; run_links_click
    # only ever acts on a match whose item_id is actually set - a planned
    # name with no real file yet is reported and skipped, never guessed at.
    def _build_planned_link_files(self):
        """Returns {display_name: LinkFileRef} from every Delivery
        Party's OWN planned bulk names + its ACC destination - available
        the moment a party has a destination and at least one name
        typed, independent of whether Run has happened yet."""
        planned = {}
        for party in self._parties:
            if not party.destination:
                continue
            project_id = party.destination.get("project_id", "")
            region = party.destination.get("region", "")
            hub_id = party.destination.get("hub_id", "")
            for name in party.names():
                display_name = name if name.lower().endswith(".rvt") else name + ".rvt"
                planned[display_name] = LinkFileRef(display_name, None, project_id, region, hub_id)
        return planned

    def _refresh_planned_link_files(self):
        """Cheap, local-only rebuild - no ACC API calls - called on every
        names/destination edit and on add/remove Delivery Party, so the
        Link tabs always reflect what's currently typed. Preserves any
        item_id already resolved for a name that's still planned, so
        editing one party's names doesn't forget another's already-
        uploaded files."""
        planned = self._build_planned_link_files()
        for name, ref in planned.items():
            existing = self._link_files.get(name)
            if existing is not None and existing.item_id:
                ref.item_id = existing.item_id
        self._link_files = planned
        self._update_link_files_count_text()
        self._after_link_scan()

    def _update_link_files_count_text(self):
        resolved_count = sum(1 for f in self._link_files.values() if f.item_id)
        self.link_files_count_tb.Text = "{0} file(s) planned, {1} already uploaded and ready to link".format(
            len(self._link_files), resolved_count)

    def refresh_link_files_click(self, sender, args):
        self._refresh_link_files()

    def _refresh_link_files(self):
        """Starts from the same PLANNED list as _refresh_planned_link_files,
        then resolves real ACC item ids for whichever names have actually
        been uploaded this session (self._report_rows where Uploaded) -
        never a live ACC scan (see module docstring). Resolves per
        distinct ACC project (one search per project, not one per file).
        Called by the Refresh File List button and automatically after a
        successful Run."""
        planned = self._build_planned_link_files()

        uploaded_rows = [r for r in self._report_rows if r.upload_status == "Uploaded"]
        party_by_id = dict((p.id, p) for p in self._parties)
        by_project = {}
        for row in uploaded_rows:
            party = party_by_id.get(row.party_id)
            if party is None or not party.destination:
                continue
            by_project.setdefault(party.destination["project_id"], []).append((row, party))

        if by_project:
            self._log("Resolving cloud file ids for already-uploaded files (can take a while - "
                      "ACC's own search index needs a moment to pick up a brand new file)...")
            for project_id, pairs in by_project.items():
                # Always a FRESH token here, never the one cached on
                # party.destination - that was fetched back when the ACC
                # destination was first picked, which can be long enough
                # before Run finishes (several file duplicate/open/
                # upload cycles) that it's no longer valid; a stale token
                # makes search_cloud_models fail silently (caught,
                # logged, returns nothing) which looks identical to "the
                # file just isn't indexed yet" unless you check the log.
                token = cloudsvc.get_token()
                expected_names = set()
                for row, _party in pairs:
                    name = row.new_file_name
                    expected_names.add(name if name.lower().endswith(".rvt") else name + ".rvt")

                def _on_attempt(attempt, attempts, found_count, remaining_count, _project_id=project_id):
                    if remaining_count:
                        self._log("  [{0}] attempt {1}/{2}: {3} found, {4} still not indexed yet...".format(
                            _project_id, attempt, attempts, found_count, remaining_count))

                found = _resolve_item_ids(project_id, token, expected_names, self.logger, on_attempt=_on_attempt)
                for display_name, item_id in found.items():
                    if display_name in planned:
                        planned[display_name].item_id = item_id
                    else:
                        # Uploaded under a name no longer present in any
                        # party's CURRENT bulk box (e.g. edited after
                        # Run) - still linkable, just not "planned" any
                        # more. first_party is whichever row's party
                        # this name came from - good enough for region/
                        # hub display purposes.
                        first_party = party_by_id.get(pairs[0][0].party_id)
                        planned[display_name] = LinkFileRef(
                            display_name, item_id, project_id,
                            first_party.destination.get("region", "") if first_party else "",
                            first_party.destination.get("hub_id", "") if first_party else "")

        self._link_files = planned
        self._update_link_files_count_text()
        self._log(self.link_files_count_tb.Text)
        self._after_link_scan()

    def _after_link_scan(self):
        if self._map_ready:
            try:
                self._map_build()
            except Exception as e:
                self.logger.exception("Could not build the wire map", e)
                self._log("Wire map could not be built: {0}".format(e))

    # ---------------- matches - driven by the Link Management wire map ----------------
    def link_remove_match_click(self, sender, args):
        selected_rows = list(self.link_matches_grid.SelectedItems)
        if not selected_rows:
            forms.alert("Select one or more rows in the matches list first.")
            return
        remove_set = set((r.source, r.target) for r in selected_rows)
        self._matches = [m for m in self._matches if m not in remove_set]
        self._refresh_link_matches()
        self._log("Removed {0} match(es).".format(len(remove_set)))

    def link_clear_matches_click(self, sender, args):
        if not self._matches:
            return
        if not forms.alert("Clear all {0} match(es)?".format(len(self._matches)), yes=True, no=True):
            return
        self._matches = []
        self._refresh_link_matches()
        self._log("All matches cleared.")

    def _refresh_link_matches(self):
        self.link_matches_grid.ItemsSource = None
        self.link_matches_grid.ItemsSource = [MatchRow(s, t) for s, t in self._matches]
        groups = dms.group_by_target(self._matches)
        est = dms.estimate_seconds(groups)
        self.link_analysis_tb.Text = dms.analysis_text(groups, est)
        if self._map_ready:
            try:
                self._map_draw_wires()
            except Exception:
                pass

    # ---------------- Link Management tab: wire map (patchbay) ----------------
    # Sources down the left, targets down the right, wires across the
    # middle - ported from DeeMAPLink.pushbutton/script.py's own
    # _map_* methods (see module docstring), minus its per-column
    # discipline-filter dropdowns (not needed at this tool's scale).
    def _guard_map(self, fn):
        try:
            fn()
        except Exception as e:
            import traceback
            self.logger.exception("Wire map error", e)
            forms.alert("The wire map hit an error:\n\n{0}\n\n{1}".format(
                e, traceback.format_exc()[-700:]), title=_TOOL_TITLE)

    def _pump(self):
        """Forces one Dispatcher pass before reading row geometry -
        ported verbatim from DeeMAPLink, see its own comment: rows have
        no ActualHeight until WPF lays them out, and TranslatePoint
        silently returns (0,0) rather than erroring if read too soon."""
        try:
            frame = DispatcherFrame()

            def _stop():
                frame.Continue = False

            self.Dispatcher.BeginInvoke(DispatcherPriority.Background, Action(_stop))
            Dispatcher.PushFrame(frame)
        except Exception:
            pass

    def _map_names_for(self, side):
        if side == "source":
            query = self._safe_text(self.link_map_left_search_tb)
        else:
            query = self._safe_text(self.link_map_right_search_tb)
        return [n for n in sorted(self._link_files.keys()) if dms.matches_search(n, query)]

    def _map_build(self):
        left_names = self._map_names_for("source")
        right_names = self._map_names_for("target")
        total = len(self._link_files)
        self.link_map_left_count_tb.Text = "{0}/{1}".format(len(left_names), total)
        self.link_map_right_count_tb.Text = "{0}/{1}".format(len(right_names), total)
        self.link_map_count_tb.Text = "{0} file(s) available".format(total)

        self.link_map_left_panel.Children.Clear()
        self.link_map_right_panel.Children.Clear()
        self._map_left_rows = {}
        self._map_right_rows = {}

        for name in left_names:
            left = self._map_make_row(name, "source")
            self.link_map_left_panel.Children.Add(left)
            self._map_left_rows[name] = left

        for name in right_names:
            right = self._map_make_row(name, "target")
            self.link_map_right_panel.Children.Add(right)
            self._map_right_rows[name] = right

        if self._map_pending and self._map_pending not in self._map_left_rows:
            self._map_pending = None
        self._map_update_hint()
        self._pump()
        self._map_draw_wires()

    def _map_make_row(self, name, side):
        text = TextBlock()
        text.Text = name
        text.Foreground = Brushes.WhiteSmoke
        text.FontSize = 10
        text.TextTrimming = TextTrimming.CharacterEllipsis
        text.VerticalAlignment = VerticalAlignment.Center
        text.Margin = Thickness(6, 0, 6, 0)
        text.IsHitTestVisible = False

        row = Border()
        row.Height = 20
        row.Margin = Thickness(2, 1, 2, 1)
        row.Background = self._ROW_BG
        row.BorderThickness = Thickness(0, 0, 4, 0) if side == "source" else Thickness(4, 0, 0, 0)
        row.BorderBrush = self._WIRE_BRUSH
        row.Child = text
        row.Cursor = Cursors.Hand
        row.ToolTip = name
        row.Tag = "{0}|{1}".format(side, name)
        row.MouseLeftButtonDown += self._map_row_click
        return row

    def _map_row_click(self, sender, args):
        def run():
            side, name = str(sender.Tag).split("|", 1)
            if side == "source":
                # Clicking another source just moves the arming, rather
                # than refusing - changing your mind is not an error.
                self._map_pending = None if self._map_pending == name else name
            else:
                if not self._map_pending:
                    self._map_update_hint("Pick a SOURCE on the left first, then this target.")
                    return
                source = self._map_pending
                if source == name:
                    self._map_update_hint("A file cannot be linked into itself.")
                    return
                pair = (source, name)
                if pair in self._matches:
                    self._map_update_hint("Already linked: {0} into {1}".format(source, name))
                    self._map_pending = None
                    self._map_refresh_row_states()
                    return
                self._matches.append(pair)
                self._map_pending = None
                self._refresh_link_matches()
                self._log("Wired {0} into {1}".format(source, name))
            self._map_refresh_row_states()
            self._map_update_hint()
            args.Handled = True
        self._guard_map(run)

    def _map_refresh_row_states(self):
        for name, row in self._map_left_rows.items():
            row.Background = (self._ROW_ARMED if name == self._map_pending else self._ROW_BG)

    def _map_update_hint(self, message=None):
        if message:
            self.link_map_hint_tb.Text = message
            return
        if self._map_pending:
            self.link_map_hint_tb.Text = (
                "Linking FROM  {0}   —  now click a TARGET on the right. "
                "Click it again to cancel.".format(self._map_pending))
        else:
            self.link_map_hint_tb.Text = (
                "Click a file on the left, then a file on the right, to link "
                "the first into the second.  Click a wire to remove it.")

    def _map_row_y(self, row):
        """TranslatePoint accounts for scroll offset automatically - see
        DeeMAPLink's own comment on the same technique."""
        try:
            point = row.TranslatePoint(Point(0, row.ActualHeight / 2.0), self.link_map_canvas)
            return point.Y
        except Exception:
            return None

    def _map_draw_wires(self):
        canvas = self.link_map_canvas
        canvas.Children.Clear()

        width = canvas.ActualWidth or 200.0
        height = canvas.ActualHeight or 0.0
        if height <= 0:
            return

        for index, pair in enumerate(self._matches):
            source, target = pair
            left_row = self._map_left_rows.get(source)
            right_row = self._map_right_rows.get(target)
            if left_row is None or right_row is None:
                continue
            y1 = self._map_row_y(left_row)
            y2 = self._map_row_y(right_row)
            if y1 is None or y2 is None:
                continue
            if (y1 < 0 and y2 < 0) or (y1 > height and y2 > height):
                continue

            hit = Line()
            hit.X1, hit.Y1, hit.X2, hit.Y2 = 0.0, y1, width, y2
            hit.Stroke = Brushes.Transparent
            hit.StrokeThickness = 12.0
            hit.Cursor = Cursors.Hand
            hit.Tag = index
            hit.MouseLeftButtonDown += self._map_wire_click

            line = Line()
            line.X1, line.Y1, line.X2, line.Y2 = 0.0, y1, width, y2
            line.Stroke = self._WIRE_BRUSH
            line.StrokeThickness = 2.0
            line.Cursor = Cursors.Hand
            line.Tag = index
            line.ToolTip = "{0}\nlinked into\n{1}\n\n(click to remove)".format(source, target)
            line.MouseLeftButtonDown += self._map_wire_click

            head = Polygon()
            head.Fill = self._WIRE_BRUSH
            head.IsHitTestVisible = False
            head.Points = PointCollection()
            head.Points.Add(Point(width, y2))
            head.Points.Add(Point(width - 9.0, y2 - 4.5))
            head.Points.Add(Point(width - 9.0, y2 + 4.5))

            for shape in (hit, line, head):
                canvas.Children.Add(shape)

    def _map_wire_click(self, sender, args):
        def run():
            index = sender.Tag
            if not isinstance(index, int) or index >= len(self._matches):
                return
            source, target = self._matches[index]
            del self._matches[index]
            self._refresh_link_matches()
            self._log("Removed wire {0} -> {1}".format(source, target))
            args.Handled = True
        self._guard_map(run)

    def _map_scrolled(self, sender, args):
        self._guard_map(self._map_draw_wires)

    def link_map_left_search_changed(self, sender, args):
        if self._map_ready:
            self._guard_map(self._map_build)

    def link_map_right_search_changed(self, sender, args):
        if self._map_ready:
            self._guard_map(self._map_build)

    def link_map_left_clear_click(self, sender, args):
        self.link_map_left_search_tb.Text = ""
        if self._map_ready:
            self._guard_map(self._map_build)

    def link_map_right_clear_click(self, sender, args):
        self.link_map_right_search_tb.Text = ""
        if self._map_ready:
            self._guard_map(self._map_build)

    def link_map_refresh_click(self, sender, args):
        self._guard_map(self._map_build)

    def link_map_clear_wires_click(self, sender, args):
        def run():
            if not self._matches:
                return
            if not forms.alert("Remove all {0} wire(s)?".format(len(self._matches)), yes=True, no=True):
                return
            self._matches = []
            self._map_pending = None
            self._refresh_link_matches()
            self._map_refresh_row_states()
            self._map_update_hint()
        self._guard_map(run)

    # ---------------- Run Links ----------------
    def _link_progress_context_key(self):
        """One bucket per distinct SET of ACC projects involved, so
        completed-host history from one combination never bleeds into a
        different one - the multi-project equivalent of DeeMAPLink's own
        per-project bucketing."""
        project_ids = sorted(set(f.project_id for f in self._link_files.values()))
        return "projects:{0}".format(",".join(project_ids))

    def _load_done_link_hosts(self, context_key):
        data = deew_settings.load(_LINK_PROGRESS_TOOL_NAME, {})
        return dict(data.get(context_key, {}))

    def _mark_link_host_done(self, context_key, target_name):
        data = deew_settings.load(_LINK_PROGRESS_TOOL_NAME, {})
        bucket = dict(data.get(context_key, {}))
        bucket[target_name] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        data[context_key] = bucket
        deew_settings.save(_LINK_PROGRESS_TOOL_NAME, data)

    def clear_link_progress_click(self, sender, args):
        context_key = self._link_progress_context_key()
        data = deew_settings.load(_LINK_PROGRESS_TOOL_NAME, {})
        if context_key in data:
            del data[context_key]
            deew_settings.save(_LINK_PROGRESS_TOOL_NAME, data)
        forms.alert("Cleared completed-link history for the current file set - the next Run "
                     "Links will process every matched host again.")

    def _link_placement(self):
        idx = self.link_placement_cb.SelectedIndex
        if idx is None or idx < 0:
            idx = 0
        return dms.PLACEMENT_OPTIONS[idx][1]

    def _link_attachment(self):
        if self.link_attachment_attachment_rb.IsChecked is True:
            return AttachmentType.Attachment
        return AttachmentType.Overlay

    def _open_link_target(self, target_ref, token):
        return afb.open_cloud_document_attached(
            self.application, target_ref.region, target_ref.project_id, target_ref.item_id, token)

    def _link_model_path_for(self, source_ref, token):
        return afb.cloud_model_path(source_ref.region, source_ref.project_id, source_ref.item_id, token)

    def run_links_click(self, sender, args):
        results, groups = self._execute_link_run(interactive=True)
        if not groups:
            return
        ok_count = sum(1 for r in results if r[0] is True)
        fail = sum(1 for r in results if r[0] is False)
        self.link_run_status_tb.Text = "{0} succeeded, {1} failed. See the Log tab / pyRevit output.".format(
            ok_count, fail)
        self._log(self.link_run_status_tb.Text)
        self._print_link_report(results)

    def _execute_link_run(self, interactive):
        """Core link-creation loop, shared by the standalone "Run Links"
        button (interactive=True - its own alerts/confirmation dialog)
        and the single combined "Run" action (interactive=False - skips
        straight past anything that would otherwise need a click,
        silently doing nothing if there's nothing ready yet, so the
        caller can fold the result into Run's own one summary instead of
        popping up a second round of dialogs). Returns (results, groups) -
        groups is empty (falsy) whenever nothing actually ran, which the
        caller uses to decide whether there's anything to report."""
        if not self._matches:
            if interactive:
                forms.alert("Build at least one match first - on Link Management, click a file on "
                             "the left then one on the right to wire them together. Matches can be "
                             "set up any time, even before Run.")
            return [], {}

        # A match can be built from a PLANNED name before that file is
        # ever created - only a name whose LinkFileRef has a resolved
        # item_id actually exists on ACC yet and can be opened/linked.
        # Anything else is reported and skipped here, never guessed at.
        def _ready(name):
            ref = self._link_files.get(name)
            return ref is not None and ref.item_id

        groups = dms.group_by_target(self._matches)
        not_ready = set()
        for target, sources in groups.items():
            if not _ready(target):
                not_ready.add(target)
            for source in sources:
                if not _ready(source):
                    not_ready.add(source)
        groups = dict((t, [s for s in sources if _ready(s)])
                      for t, sources in groups.items() if _ready(t))
        groups = dict((t, s) for t, s in groups.items() if s)

        if not groups:
            if interactive:
                forms.alert("None of the current matches point at files that have been uploaded yet "
                             "- Run first (or Refresh File List if you've already Run), then Run "
                             "Links again.")
            return [], {}
        if not_ready and interactive:
            forms.alert("{0} file(s) in your matches haven't been uploaded yet, so they'll be "
                         "skipped for now - Run first, then Run Links again to pick them up:\n\n{1}"
                         .format(len(not_ready), ", ".join(sorted(not_ready))))

        context_key = self._link_progress_context_key()
        skip_completed = bool(self.link_skip_completed_cb.IsChecked)
        done_hosts = self._load_done_link_hosts(context_key) if skip_completed else {}
        already_done_count = sum(1 for t in groups.keys() if t in done_hosts)
        if skip_completed and done_hosts:
            groups = dict((t, s) for t, s in groups.items() if t not in done_hosts)
        if not groups:
            if interactive:
                forms.alert("Every matched host was already synchronized in a previous Run Links. "
                             "Uncheck 'Skip hosts already synchronized' or Clear History to redo them.")
            return [], {}

        if interactive:
            est = dms.estimate_seconds(groups)
            analysis = dms.analysis_text(groups, est)
            resume_note = ("\n\n{0} host(s) already synchronized in a previous run are being skipped."
                            .format(already_done_count)) if already_done_count else ""
            if not forms.alert(
                    "{0}{1}\n\nEach host is opened, linked, and SYNCHRONIZED back - this modifies {2} "
                    "real shared cloud model(s). If Revit crashes partway through (a huge host model "
                    "can do this), hosts already synchronized before the crash are safely saved; just "
                    "Refresh File List and Run Links again to pick up where it left off.\n\nContinue?"
                    .format(analysis, resume_note, len(groups)),
                    title=_TOOL_TITLE + " - confirm", yes=True, no=True):
                return [], {}

        placement = self._link_placement()
        attachment = self._link_attachment()
        fallback = ImportPlacement.Origin if placement == ImportPlacement.Shared else None
        comment = self.link_sync_comment_tb.Text or ""
        skip_existing = bool(self.link_skip_existing_cb.IsChecked)

        total_steps = sum(len(sources) + 1 for sources in groups.values())
        sync_failed = False
        results = []

        dialog_handler = ffh.make_dialog_handler(self.logger)
        try:
            self.uiapp.DialogBoxShowing += dialog_handler
        except Exception as e:
            self.logger.exception("Could not attach dialog handler", e)

        try:
            with progsvc.DeeWProgressService(_TOOL_TITLE, total_steps) as prog:
                for target_name, source_names in groups.items():
                    if sync_failed or prog.cancelled:
                        results.append((None, target_name, "skipped - batch stopped"))
                        for _ in range(len(source_names) + 1):
                            prog.finish_file("skipped")
                        continue

                    target_ref = self._link_files[target_name]
                    token = cloudsvc.get_token()
                    log_start = len(self.logger.entries)
                    prog.step(target_name, "Opening")
                    self._log("Opening host '{0}'...".format(target_name))
                    doc, detail = self._open_link_target(target_ref, token)
                    if doc is None:
                        results.append((False, target_name, "could not open: {0}".format(detail)))
                        self._log("  FAILED to open - {0}".format(detail))
                        for _ in range(len(source_names) + 1):
                            prog.finish_file("failed")
                        continue

                    try:
                        existing = dms.existing_link_names(doc) if skip_existing else set()
                        linked_here = 0
                        for source_name in source_names:
                            if skip_existing and dms.already_linked(existing, source_name):
                                results.append((None, "{0} -> {1}".format(source_name, target_name),
                                                "already linked - skipped"))
                                self._log("  '{0}' already linked - skipped".format(source_name))
                                prog.finish_file("skipped")
                                continue

                            source_ref = self._link_files[source_name]
                            model_path, path_detail = self._link_model_path_for(source_ref, token)
                            if model_path is None:
                                results.append((False, "{0} -> {1}".format(source_name, target_name), path_detail))
                                self._log("  '{0}' path failed - {1}".format(source_name, path_detail))
                                prog.finish_file("failed")
                                continue

                            prog.step("{0} -> {1}".format(source_name, target_name), "Linking")
                            ok, link_detail = dms.link_into(
                                doc, source_name, model_path, placement, fallback,
                                attachment=attachment, logger=self.logger)
                            results.append((ok, "{0} -> {1}".format(source_name, target_name), link_detail))
                            self._log("  '{0}': {1}".format(source_name, link_detail))
                            if ok:
                                linked_here += 1
                            prog.finish_file("success" if ok else "failed")

                        if linked_here:
                            prog.step(target_name, "Synchronizing")
                            ok_sync, sync_detail = docmgr.synchronize_with_central(
                                doc, comment=comment, compact=False, logger=self.logger)
                            results.append((ok_sync, target_name,
                                            "synchronized" if ok_sync else "sync failed: {0}".format(sync_detail)))
                            self._log("  host {0}".format("Synchronized" if ok_sync else "Linked but sync FAILED"))
                            prog.finish_file("success" if ok_sync else "failed")
                            if ok_sync:
                                self._mark_link_host_done(context_key, target_name)
                            else:
                                self._log("  SYNC ERROR: {0}".format(sync_detail))
                                sync_failed = True
                        else:
                            results.append((None, target_name, "opened - nothing new to link"))
                            prog.finish_file("skipped")
                            self._mark_link_host_done(context_key, target_name)
                    except Exception as e:
                        results.append((False, target_name, "unexpected error: {0}".format(e)))
                        self.logger.exception("Unexpected error linking", e, file=target_name)
                    finally:
                        log_end = len(self.logger.entries)
                        issues = dms.issues_from_log(self.logger, log_start, log_end)
                        if issues:
                            results.append((None, target_name + " - issues seen", issues))
                        try:
                            docmgr.close_document(doc, save_modified=False, logger=self.logger)
                        except Exception:
                            pass
        finally:
            try:
                self.uiapp.DialogBoxShowing -= dialog_handler
            except Exception:
                pass

        if sync_failed:
            forms.alert(
                "A host was linked but could NOT be synchronized, so the batch was stopped before "
                "touching any more models. Open it, synchronize manually if you want to keep the "
                "change, and relinquish - then re-run Run Links.",
                title=_TOOL_TITLE + " - stopped after a sync failure")

        return results, groups

    def _print_link_report(self, results):
        html = ['<h2 style="font-family:sans-serif;color:#ddd;">DeeInitiator - Link Results</h2>']
        for ok, label, detail in results:
            bg = "#2e7d32" if ok else ("#8d6e00" if ok is None else "#c62828")
            icon = "&#10003;" if ok else ("&#9888;" if ok is None else "&#10007;")
            html.append(
                '<div style="padding:6px 12px;margin:3px 0;background:{0};color:#fff;'
                'border-radius:4px;font-family:monospace;font-size:12px;">'
                '{1}&nbsp; <b>{2}</b> &mdash; {3}</div>'.format(bg, icon, label, detail))
        ok_count = sum(1 for r in results if r[0] is True)
        html.append('<hr><b style="font-family:sans-serif;">{0} / {1} succeeded.</b>'.format(
            ok_count, len(results)))
        output.print_html("".join(html))


def main():
    uiapp = __revit__
    window = DeeInitiatorWindow(_XAML_FILE, uiapp)
    window.ShowDialog()


main()
