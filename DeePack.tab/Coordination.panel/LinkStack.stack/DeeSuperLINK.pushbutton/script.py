# -*- coding: utf-8 -*-
"""
DeeSuperLINK
Links ACC/BIM360 cloud models into each other with NO Revit file open.

Two ways to build the batch, both driving "Create Links" (fixed mode) /
"Run Matches" (arbitrary mode) independently of each other:

Fixed Mode (original, untouched):
  ONE -> MANY : pick one model, link it into many host models.
  MANY -> ONE : pick one host, link many models into it.

Wire Map / Matrix / Selected Links (new, by explicit user request - "add
to all the Linking tools that I have"): an arbitrary many-to-many match
set, ported from DeeMAPLink/DeeInitiator's own identical tabs, now that
all three live in shared lib/dee_matchset_ui.py (Matrix grid, filters,
separators, shift-select) and lib/dee_selected_links_ui.py (per-match
Link Type/Attachment override grid). The two fixed-mode and arbitrary-
mode paths are kept SEPARATE on purpose, per explicit user request, not
merged into one - "Run Matches" only ever touches self._matches, "Create
Links" only ever touches the original single_cb/files_grid ticked set.
Save Set/Load Set (lib/dee_matchset_io.py) round-trips the ACC project,
link options, matches, per-match overrides, and Matrix separators to one
JSON file - this tool had no such persistence before.

This tool's own locally-duplicated PLACEMENT_OPTIONS/matches_search/
format_duration/existing_link_names/already_linked/link_into/
issues_from_log (near-identical twins of lib/dee_maplink_service.py's own
versions, built LATER against this tool as its model) are now imported as
dms.* instead - a straight cleanup, not a behavior change for Fixed Mode.

--------------------------------------------------------------------
How it differs from DeeLINK (the button below it)
--------------------------------------------------------------------
DeeLINK links cloud files into the document you already have open. It
is interactive and single-host by nature.

DeeSuperLINK never touches an open document - it is `context: zero-doc`
and opens each HOST model itself, headless and ATTACHED, creates the
link, then Synchronizes With Central so the link reaches the real
shared model. That is the whole point: setting up a federated model
without opening anything by hand.

Because it opens hosts ATTACHED and syncs, this writes to real cloud
models. That is deliberate and is what the tool is for, but it is a
categorically bigger action than DeeLINK's, so the confirmation names
exactly how many models will be modified.

--------------------------------------------------------------------
Reused rather than reinvented
--------------------------------------------------------------------
- acc_auth / acc_api / acc_file_browser - hub/project/file browsing,
  already proven by DeeLINK, DeeOpener and the DeeW.Cloud tools.
- acc_file_browser.open_cloud_document_attached() - added for this
  tool; the headless ATTACHED sibling of the detached opener written
  for DeeW.Transmit. Neither uses OpenAndActivateDocument, which
  activates documents into Revit's UI and proved unusable for batch
  work (see that function's docstring for the history).
- acc_file_browser.cloud_model_path() - the ModelPath a link needs.
- deew_document_manager.synchronize_with_central / close_document.
- The link call itself mirrors DeeLINK's proven link_file():
  RevitLinkType.Create(doc, cloud_path, RevitLinkOptions(False)) then
  RevitLinkInstance.Create(doc, type_id, placement).

--------------------------------------------------------------------
Placement - all four Revit options, verified not assumed
--------------------------------------------------------------------
ImportPlacement has exactly four members (checked against the API docs
before writing, because "Base Point" is NOT its own member and the
naming is easy to get wrong):
  Origin   -> Internal Origin to Internal Origin
  Site     -> Project Base Point to Project Base Point. The docs read
              "Placement at Base Point. Useful for Revit links only."
              This is the Base Point option, despite the name.
  Shared   -> By Shared Coordinates
  Centered -> Center to Center
DeeLINK already maps these the same way, so both tools agree.

--------------------------------------------------------------------
NEEDS LIVE-REVIT VERIFICATION (flagged, not assumed)
--------------------------------------------------------------------
- Whether RevitLinkType.Create accepts a cloud ModelPath from inside a
  headlessly-opened host. DeeLINK proves it works against the ACTIVE
  document; doing it in a background document is the new part.
- Whether a host opened attached-and-headless can Synchronize With
  Central without a UI document. DeeW.Clean does the same thing and is
  itself still unverified live, so this shares that risk rather than
  resting on a proven path.
- A model cannot link itself; that case is filtered out here, but a
  circular A->B->A link is NOT detected and Revit's own behaviour for
  it is untested.
"""
import os
import time
import datetime

from pyrevit import forms, script
import dee_branding

import clr
clr.AddReference("PresentationFramework")
clr.AddReference("WindowsBase")
from System import Action
from System.Windows import Visibility, Thickness, HorizontalAlignment, VerticalAlignment, TextTrimming, Point
from System.Windows.Controls import Border, Canvas, TextBlock
from System.Windows.Media import Brushes, Color, SolidColorBrush, PointCollection
from System.Windows.Shapes import Line, Polygon
from System.Windows.Input import Cursors
from System.Windows.Forms import SaveFileDialog, OpenFileDialog, DialogResult, MessageBox
from System.Windows.Threading import Dispatcher, DispatcherFrame, DispatcherPriority

from Autodesk.Revit.DB import (
    RevitLinkType, ImportPlacement, AttachmentType, Transaction, FilteredElementCollector,
)

import acc_auth
import acc_api
import acc_file_browser as afb
import deew_document_manager as docmgr
import deew_logger
import dee_link_create_service as lcs
import deew_failure_handler as ffh
import dee_maplink_service as dms
import dee_matchset_ui as mxui
import dee_selected_links_ui as slui
import dee_matchset_io as msio
import dee_telemetry
dee_telemetry.check_access("DeeSuperLINK")


output = script.get_output()

_TOOL_NAME = "DeeSuperLINK"
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")
_CACHE_FILE = os.path.join(_THIS_DIR, ".acc_file_cache.json")

MODE_ONE_TO_MANY = "one_to_many"
MODE_MANY_TO_ONE = "many_to_one"


class FileRow(object):
    """One ACC Revit file. Plain values only - no Revit objects are held
    across the window's lifetime."""

    def __init__(self, name, item_id):
        self.selected = False
        self.name = name
        self.item_id = item_id
        self.status = ""


class MatchRow(object):
    """One row in the matches grid - plain source/target strings, the
    same shape dee_maplink_service.build_matches() works with. New to
    this tool - backs the arbitrary many-to-many Wire Map/Matrix/Selected
    Links flow alongside the original fixed one-to-many/many-to-one mode,
    which is untouched."""
    def __init__(self, source, target):
        self.source = source
        self.target = target


# PLACEMENT_OPTIONS, matches_search, format_duration, existing_link_names,
# already_linked, link_into, issues_from_log all used to be hand-
# duplicated here (near-identical twins of lib/dee_maplink_service.py's
# own versions, built later against this tool as its model) - now
# imported as dms.* instead, so a future fix to any of them lands once,
# not twice. See dee_maplink_service.py's own module docstring for the
# full reasoning on each.


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


class DeeSuperLinkWindow(dee_branding.DeeBrandedWindow):
    # Shared with DeeInitiator/DeeMAPLink's own Wire Map/Matrix - same
    # colors, so a file selected/wired looks identical across every
    # linking tool in this repo.
    _WIRE_BRUSH = SolidColorBrush(Color.FromRgb(0xF2, 0x99, 0x4D))
    _ROW_BG = SolidColorBrush(Color.FromRgb(0x2B, 0x2B, 0x2B))
    _ROW_ARMED = SolidColorBrush(Color.FromRgb(0x4A, 0x3A, 0x22))

    def __init__(self, xaml_file, uiapp):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.uiapp = uiapp
        self.application = uiapp.Application
        self.logger = deew_logger.DeeWLogger(_TOOL_NAME)

        self._token = None
        self._hub_id = None
        self._region = None
        self._project_id = None
        self._project_name = ""
        self._all_items = {}          # {display name: item_id}
        self._rows = []
        self._log_lines = []
        self._prog_total = 1
        self._prog_done = 0
        self._prog_start = time.time()

        # Arbitrary many-to-many matching (Wire Map/Matrix/Selected Links)
        # - new to this tool, alongside the original fixed one-to-many/
        # many-to-one mode above, which is untouched and still has its
        # own "Create Links" button. "Run Matches" runs THIS instead.
        self._matches = []            # [(source_name, target_name), ...]
        self._match_options = {}      # {(source,target): {"placement_label":.., "attachment_label":..}}
        self._matrix = mxui.MatrixState()
        self._matrix_ready = True
        self._map_left_rows = {}
        self._map_right_rows = {}
        self._map_pending = None
        self._map_ready = False

        placement_labels = [label for label, _v in dms.PLACEMENT_OPTIONS]
        attachment_labels = ["Overlay", "Attachment"]
        self.placement_cb.ItemsSource = placement_labels
        self.placement_cb.SelectedIndex = 0
        self.selected_links_placement_col.ItemsSource = placement_labels
        self.selected_links_attachment_col.ItemsSource = attachment_labels
        self.bulk_placement_cb.ItemsSource = placement_labels
        self.bulk_attachment_cb.ItemsSource = attachment_labels
        self._refresh_matches()
        self._apply_mode_labels()
        self._log("Ready. Pick a hub and project to begin.")

        # Canvas handlers live on the scroll viewers/canvas itself, not
        # per-row - a scroll or resize has to redraw every wire, not just
        # the one under the cursor. Mirrors DeeInitiator/DeeMAPLink's own
        # Wire Map wiring exactly.
        self.wiremap_left_scroll.ScrollChanged += self._map_scrolled
        self.wiremap_right_scroll.ScrollChanged += self._map_scrolled
        self.wiremap_canvas.SizeChanged += self._map_scrolled
        self._map_ready = True

    # ---------------- helpers ----------------
    def _log(self, message):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self._log_lines.append("[{0}] {1}".format(ts, message))
        try:
            self.log_tb.Text = "\n".join(self._log_lines[-400:])
            self.log_tb.ScrollToEnd()
        except Exception:
            pass

    # ---------------- in-window progress bar ----------------
    #
    # A modal WPF window does not repaint while a long synchronous loop
    # runs on the UI thread, so the bar would only appear once the work
    # had already finished. _pump() drains pending render work at
    # Background priority (the standard WPF "DoEvents") after each
    # update, which is what makes the bar actually animate.
    #
    # The step text is centred ON the bar by stacking a TextBlock over
    # the ProgressBar in a Grid, rather than sitting beside it.
    def _pump(self):
        try:
            frame = DispatcherFrame()

            def _stop():
                frame.Continue = False

            self.Dispatcher.BeginInvoke(DispatcherPriority.Background, Action(_stop))
            Dispatcher.PushFrame(frame)
        except Exception:
            pass

    def _progress_begin(self, total_steps):
        self._prog_total = max(1, total_steps)
        self._prog_done = 0
        self._prog_start = time.time()
        try:
            self.progress_bar.Maximum = self._prog_total
            self.progress_bar.Value = 0
            self.progress_host.Visibility = Visibility.Visible
            self.run_b.IsEnabled = False
        except Exception:
            pass
        self._progress_render("Starting...")

    def _progress_render(self, label):
        try:
            elapsed = time.time() - self._prog_start
            done = self._prog_done
            if done > 0:
                remaining = (elapsed / float(done)) * (self._prog_total - done)
                eta = "ETA {0}".format(dms.format_duration(remaining))
            else:
                eta = "ETA --"
            self.progress_bar.Value = done
            self.progress_text_tb.Text = "{0}   |   {1} of {2}   |   elapsed {3}   |   {4}".format(
                label, min(done + 1, self._prog_total), self._prog_total,
                dms.format_duration(elapsed), eta)
        except Exception:
            pass
        self._pump()

    def _progress_step(self, label):
        """Call BEFORE doing the work the label describes."""
        self._progress_render(label)

    def _progress_done_one(self):
        self._prog_done += 1
        try:
            self.progress_bar.Value = self._prog_done
        except Exception:
            pass
        self._pump()

    def _progress_end(self):
        try:
            self.progress_host.Visibility = Visibility.Collapsed
            self.run_b.IsEnabled = True
        except Exception:
            pass
        self._pump()

    def _mode(self):
        return MODE_ONE_TO_MANY if self.mode_one_to_many_rb.IsChecked is True else MODE_MANY_TO_ONE

    def _placement(self):
        idx = self.placement_cb.SelectedIndex
        if idx is None or idx < 0:
            idx = 0
        return dms.PLACEMENT_OPTIONS[idx][1]

    def _attachment(self):
        if self.attachment_attachment_rb.IsChecked is True:
            return AttachmentType.Attachment
        return AttachmentType.Overlay

    def _apply_mode_labels(self):
        if self._mode() == MODE_ONE_TO_MANY:
            self.single_label_tb.Text = "Model to link:"
            self.many_label_tb.Text = "3. Host models to link INTO (tick them):"
        else:
            self.single_label_tb.Text = "Host model:"
            self.many_label_tb.Text = "3. Models to link INTO that host (tick them):"

    def mode_changed(self, sender, args):
        # Fires during XAML load before the fields exist.
        try:
            self._apply_mode_labels()
        except Exception:
            return
        self._refresh_rows()

    def single_changed(self, sender, args):
        try:
            self._refresh_rows()
        except Exception:
            pass

    # ---------------- project ----------------
    def pick_project_click(self, sender, args):
        try:
            self._token = acc_auth.get_access_token()
        except Exception as e:
            forms.alert("Authentication failed:\n{0}".format(e))
            return
        hub = afb.pick_hub(self._token)
        if not hub:
            return
        hub_id, region, hub_name = hub
        project = afb.pick_project(hub_id, self._token)
        if not project:
            return
        project_id, project_name = project
        # hub_id is required by list_project_files' fresh-scan path
        # (acc_api.get_top_folders) - only the cached path works without it.
        self._hub_id = hub_id
        self._region = region
        self._project_id = project_id
        self._project_name = project_name
        self.project_tb.Text = "{0} / {1}".format(hub_name, project_name)
        self._log("Loading Revit files in '{0}'...".format(project_name))
        self.refresh_click(None, None)

    def refresh_click(self, sender, args):
        if not self._project_id:
            forms.alert("Pick a hub and project first.")
            return
        try:
            with _SafeProgress(title="DeeSuperLINK - loading project files...", indeterminate=True):
                self._all_items = afb.list_project_files(
                    self._hub_id, self._project_id, self._token, _CACHE_FILE) or {}
        except Exception as e:
            forms.alert("Could not list project files:\n{0}".format(e))
            return
        names = sorted(self._all_items.keys())
        self.single_cb.ItemsSource = names
        if names:
            self.single_cb.SelectedIndex = 0
        self._rows = [FileRow(n, self._all_items[n]) for n in names]
        self._refresh_rows()
        self._log("{0} Revit file(s) found.".format(len(names)))

        # Wire Map/Matrix/Selected Links are the other three tabs' own
        # views of this SAME scanned pool - a fresh scan makes their
        # state stale too, same reasoning DeeMAPLink's own _after_scan
        # already applies.
        if getattr(self, "_map_ready", False):
            try:
                self._map_build()
            except Exception as e:
                self.logger.exception("Could not build the wire map", e)
                self._log("Wire map could not be built: {0}".format(e))
        if getattr(self, "_matrix_ready", False):
            self._refresh_matrix_filters()
            self._matrix.built = False
            if self.mode_tabs.SelectedIndex == 2:
                self._guard_map(self._matrix_build)

    def _eligible_rows(self):
        """Everything that could be used - the single-side file is
        excluded because a model cannot link into itself. Search does
        NOT apply here: this is the set Run actually works from."""
        single = self.single_cb.SelectedItem
        return [r for r in self._rows if r.name != single]

    def _visible_rows(self):
        query = ""
        try:
            query = self.search_tb.Text or ""
        except Exception:
            query = ""
        return [r for r in self._eligible_rows() if dms.matches_search(r.name, query)]

    def _refresh_rows(self):
        visible = self._visible_rows()
        self.files_grid.ItemsSource = None
        self.files_grid.ItemsSource = visible

        eligible = self._eligible_rows()
        ticked_total = sum(1 for r in eligible if r.selected)
        visible_names = set(r.name for r in visible)
        ticked_hidden = sum(1 for r in eligible if r.selected and r.name not in visible_names)

        # A ticked file that the current search hides would still be
        # processed by Run. Saying so out loud is the difference between
        # a filter and a trap.
        try:
            self.hidden_warning_tb.Text = (
                "{0} ticked file(s) hidden by the search - they WILL still be used".format(ticked_hidden)
                if ticked_hidden else "")
        except Exception:
            pass

        self.status_tb.Text = "Showing {0} of {1} file(s). {2} ticked.".format(
            len(visible), len(eligible), ticked_total)

    def search_changed(self, sender, args):
        # Fires while the XAML is still loading, before the grid exists.
        try:
            self._refresh_rows()
        except Exception:
            pass

    def clear_search_click(self, sender, args):
        try:
            self.search_tb.Text = ""
        except Exception:
            pass

    def select_all_click(self, sender, args):
        """Applies to what the search is currently showing, not the whole
        project - ticking files you cannot see would be worse."""
        for r in self._visible_rows():
            r.selected = True
        self._refresh_rows()

    def select_none_click(self, sender, args):
        for r in self._visible_rows():
            r.selected = False
        self._refresh_rows()

    # ======================================================================
    # Arbitrary many-to-many matching (Wire Map / Matrix / Selected Links)
    # - new to this tool, alongside the original fixed one-to-many/many-
    # to-one mode above (untouched). All three read/write the SAME
    # self._matches list, ported from DeeInitiator's own identical tabs
    # (DeeInitiator's own docstring: "minus its per-column discipline-
    # filter dropdowns, not needed at this tool's scale" - same choice
    # made here). Matrix itself is the shared lib/dee_matchset_ui.py,
    # also used by DeeInitiator/DeeMAPLink.
    # ======================================================================
    def mode_tabs_selection_changed(self, sender, args):
        if not getattr(self, "_matrix_ready", False):
            return
        try:
            idx = self.mode_tabs.SelectedIndex
            if idx == 2 and not self._matrix.built:
                self._matrix_build()
            elif idx == 3:
                self._selected_links_build()
        except Exception:
            pass

    def _guard_map(self, fn):
        try:
            fn()
        except Exception as e:
            import traceback
            self.logger.exception("Wire map / matrix error", e)
            forms.alert("That hit an error:\n\n{0}\n\n{1}".format(
                e, traceback.format_exc()[-700:]), title=_TOOL_NAME)

    # ---- Wire Map ----
    def _map_names_for(self, side):
        if side == "source":
            query = self._safe_text(self.wiremap_left_search_tb)
        else:
            query = self._safe_text(self.wiremap_right_search_tb)
        return [n for n in sorted(self._all_items.keys()) if dms.matches_search(n, query)]

    def _safe_text(self, textbox):
        try:
            return textbox.Text or ""
        except Exception:
            return ""

    def _map_build(self):
        left_names = self._map_names_for("source")
        right_names = self._map_names_for("target")
        total = len(self._all_items)
        self.wiremap_left_count_tb.Text = "{0}/{1}".format(len(left_names), total)
        self.wiremap_right_count_tb.Text = "{0}/{1}".format(len(right_names), total)
        self.wiremap_count_tb.Text = "{0} file(s) available".format(total)

        self.wiremap_left_panel.Children.Clear()
        self.wiremap_right_panel.Children.Clear()
        self._map_left_rows = {}
        self._map_right_rows = {}

        for name in left_names:
            left = self._map_make_row(name, "source")
            self.wiremap_left_panel.Children.Add(left)
            self._map_left_rows[name] = left

        for name in right_names:
            right = self._map_make_row(name, "target")
            self.wiremap_right_panel.Children.Add(right)
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
                self._refresh_matches()
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
            self.wiremap_hint_tb.Text = message
            return
        if self._map_pending:
            self.wiremap_hint_tb.Text = (
                "Linking FROM  {0}   —  now click a TARGET on the right. "
                "Click it again to cancel.".format(self._map_pending))
        else:
            self.wiremap_hint_tb.Text = (
                "Click a file on the left, then a file on the right, to link "
                "the first into the second.  Click a wire to remove it.")

    def _map_row_y(self, row):
        try:
            point = row.TranslatePoint(Point(0, row.ActualHeight / 2.0), self.wiremap_canvas)
            return point.Y
        except Exception:
            return None

    def _map_draw_wires(self):
        canvas = self.wiremap_canvas
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
            self._refresh_matches()
            self._log("Removed wire {0} -> {1}".format(source, target))
            args.Handled = True
        self._guard_map(run)

    def _map_scrolled(self, sender, args):
        self._guard_map(self._map_draw_wires)

    def wiremap_left_search_changed(self, sender, args):
        if self._map_ready:
            self._guard_map(self._map_build)

    def wiremap_right_search_changed(self, sender, args):
        if self._map_ready:
            self._guard_map(self._map_build)

    def wiremap_left_clear_click(self, sender, args):
        self.wiremap_left_search_tb.Text = ""
        if self._map_ready:
            self._guard_map(self._map_build)

    def wiremap_right_clear_click(self, sender, args):
        self.wiremap_right_search_tb.Text = ""
        if self._map_ready:
            self._guard_map(self._map_build)

    def wiremap_refresh_click(self, sender, args):
        self._guard_map(self._map_build)

    def wiremap_clear_wires_click(self, sender, args):
        def run():
            if not self._matches:
                return
            if not forms.alert("Remove all {0} wire(s)?".format(len(self._matches)),
                               yes=True, no=True):
                return
            self._matches = []
            self._map_pending = None
            self._refresh_matches()
            self._map_refresh_row_states()
            self._map_update_hint()
        self._guard_map(run)

    # ---- Matrix ----
    def _refresh_matrix_filters(self):
        """No party/discipline concept to filter by here (this tool's
        pool is a flat ACC project listing) - search-only is enough at
        this scale, same scope limit DeeInitiator's own Matrix states
        explicitly. Kept as a method (not inlined) so both axes still
        get independently reset the same way DeeInitiator/DeeMAPLink do,
        if a filter concept is ever added later."""
        for combo in (self.matrix_row_filter_cb, self.matrix_col_filter_cb):
            try:
                combo.ItemsSource = ["(all)"]
                combo.SelectedIndex = 0
            except Exception:
                pass

    def _matrix_filter_value(self, combo):
        try:
            selected = combo.SelectedItem
            if selected and selected != "(all)":
                return selected
        except Exception:
            pass
        return None

    def _matrix_is_matched(self, row_name, col_name):
        return (row_name, col_name) in self._matches

    def _matrix_toggle_range(self, pairs, new_value):
        match_set = set(self._matches)
        changed = 0
        for pair in pairs:
            if new_value and pair not in match_set:
                self._matches.append(pair)
                match_set.add(pair)
                changed += 1
            elif not new_value and pair in match_set:
                self._matches.remove(pair)
                match_set.discard(pair)
                changed += 1
        if not changed:
            return
        verb = "Wired" if new_value else "Removed"
        if len(pairs) == 1:
            self._log("{0} {1} -> {2}".format(verb, pairs[0][0], pairs[0][1]))
        else:
            self._log("{0} {1} match(es) (shift-select).".format(verb, changed))
        self._refresh_matches()

    def _matrix_build(self):
        query = self._safe_text(self.matrix_search_tb)
        row_filter = self._matrix_filter_value(self.matrix_row_filter_cb)
        col_filter = self._matrix_filter_value(self.matrix_col_filter_cb)

        names_rows = mxui.filtered_names(self._all_items.keys(), query, row_filter, None)
        names_cols = mxui.filtered_names(self._all_items.keys(), query, col_filter, None)

        mxui.build(self.matrix_grid, self._matrix, names_rows, names_cols,
                   self._matrix_is_matched, self._matrix_toggle_range)
        self.matrix_count_tb.Text = "{0} row(s) x {1} column(s) / {2} file(s) total".format(
            len(names_rows), len(names_cols), len(self._all_items))

    def matrix_search_changed(self, sender, args):
        if getattr(self, "_matrix_ready", False) and self._matrix.built:
            self._guard_map(self._matrix_build)

    def matrix_clear_search_click(self, sender, args):
        self.matrix_search_tb.Text = ""
        if getattr(self, "_matrix_ready", False) and self._matrix.built:
            self._guard_map(self._matrix_build)

    def matrix_filter_changed(self, sender, args):
        if getattr(self, "_matrix_ready", False) and self._matrix.built:
            self._guard_map(self._matrix_build)

    def matrix_clear_click(self, sender, args):
        def run():
            if not self._matches:
                return
            if not forms.alert("Remove all {0} match(es)?".format(len(self._matches)),
                               yes=True, no=True):
                return
            self._matches = []
            self._refresh_matches()
            if self._matrix.built:
                self._matrix_build()
            self._log("All matches cleared (from Matrix).")
        self._guard_map(run)

    # ---- Selected Links ----
    def _selected_links_default_labels(self):
        placement_labels = [label for label, _v in dms.PLACEMENT_OPTIONS]
        idx = self.placement_cb.SelectedIndex
        default_placement_label = placement_labels[idx] if 0 <= idx < len(placement_labels) else placement_labels[0]
        default_attachment_label = "Attachment" if self.attachment_attachment_rb.IsChecked is True else "Overlay"
        return default_placement_label, default_attachment_label

    def _selected_links_build(self):
        default_placement_label, default_attachment_label = self._selected_links_default_labels()
        slui.refresh_grid(self.selected_links_grid, self._matches, self._match_options,
                           default_placement_label, default_attachment_label)

    def selected_links_row_edit_ending(self, sender, args):
        def run():
            slui.row_edit_committed(self.selected_links_grid, args, self._match_options,
                                     self._refresh_matches)
        try:
            self.Dispatcher.BeginInvoke(Action(run))
        except Exception:
            run()

    def bulk_placement_apply_click(self, sender, args):
        label = self.bulk_placement_cb.SelectedItem
        if not label:
            forms.alert("Pick a Link Type first.")
            return
        count = slui.apply_to_selection(self.selected_links_grid, self._match_options,
                                         placement_label=label, on_changed=self._selected_links_build)
        if not count:
            forms.alert("Select one or more rows in the grid above first.")
        else:
            self._log("Set Link Type '{0}' on {1} selected match(es).".format(label, count))

    def bulk_attachment_apply_click(self, sender, args):
        label = self.bulk_attachment_cb.SelectedItem
        if not label:
            forms.alert("Pick an Attachment value first.")
            return
        count = slui.apply_to_selection(self.selected_links_grid, self._match_options,
                                         attachment_label=label, on_changed=self._selected_links_build)
        if not count:
            forms.alert("Select one or more rows in the grid above first.")
        else:
            self._log("Set Attachment '{0}' on {1} selected match(es).".format(label, count))

    def _effective_link_options(self, source_name, target_name):
        override = self._match_options.get((source_name, target_name), {})

        placement_label = override.get("placement_label")
        if placement_label:
            by_label = dict(dms.PLACEMENT_OPTIONS)
            placement = by_label.get(placement_label, self._placement())
        else:
            placement = self._placement()

        attachment_label = override.get("attachment_label")
        if attachment_label == "Attachment":
            attachment = AttachmentType.Attachment
        elif attachment_label == "Overlay":
            attachment = AttachmentType.Overlay
        else:
            attachment = self._attachment()

        fallback = ImportPlacement.Origin if placement == ImportPlacement.Shared else None
        return placement, attachment, fallback

    # ---- shared matches grid / analysis (right-docked panel) ----
    def remove_match_click(self, sender, args):
        selected_rows = list(self.matches_grid.SelectedItems)
        if not selected_rows:
            forms.alert("Select one or more rows in the matches list first.")
            return
        remove_set = set((r.source, r.target) for r in selected_rows)
        self._matches = [m for m in self._matches if m not in remove_set]
        self._refresh_matches()
        self._log("Removed {0} match(es).".format(len(remove_set)))

    def clear_matches_click(self, sender, args):
        if not self._matches:
            return
        if not forms.alert("Clear all {0} match(es)?".format(len(self._matches)), yes=True, no=True):
            return
        self._matches = []
        self._refresh_matches()
        self._log("All matches cleared.")

    def _refresh_matches(self):
        self.matches_grid.ItemsSource = None
        self.matches_grid.ItemsSource = [MatchRow(s, t) for s, t in self._matches]
        groups = dms.group_by_target(self._matches)
        est = dms.estimate_seconds(groups)
        self.analysis_tb.Text = dms.analysis_text(groups, est)
        if getattr(self, "_map_ready", False):
            try:
                self._map_draw_wires()
            except Exception:
                pass
        if getattr(self, "_matrix_ready", False) and self._matrix.built:
            try:
                mxui.refresh_ticks(self._matrix, self._matrix_is_matched)
            except Exception:
                pass
        if getattr(self, "_matrix_ready", False) and self.mode_tabs.SelectedIndex == 3:
            try:
                self._selected_links_build()
            except Exception:
                pass

    # ---------------- run (matches-driven, independent of Fixed Mode) ----------------
    def run_matches_click(self, sender, args):
        if not self._project_id:
            forms.alert("Pick a hub and project first.")
            return
        if not self._matches:
            forms.alert("Build at least one match first - on Wire Map, click a file on the left "
                         "then one on the right, or tick cells on Matrix.")
            return

        groups = dms.group_by_target(self._matches)
        est = dms.estimate_seconds(groups)
        analysis = dms.analysis_text(groups, est)
        if not forms.alert(
                "{0}\n\nEach host is opened, linked, and SYNCHRONIZED back - this modifies {1} "
                "real cloud model(s).\n\nContinue?".format(analysis, len(groups)),
                title="DeeSuperLINK - Confirm", yes=True, no=True):
            return

        comment = self.sync_comment_tb.Text or ""
        skip_existing = (self.skip_existing_cb.IsChecked is True)
        total_steps = sum(len(sources) + 1 for sources in groups.values())
        self._progress_begin(total_steps)
        sync_failed = False
        results = []

        dialog_handler = ffh.make_dialog_handler(self.logger)
        try:
            self.uiapp.DialogBoxShowing += dialog_handler
        except Exception as e:
            self.logger.exception("Could not attach dialog handler", e)

        try:
            for target_name, source_names in groups.items():
                if sync_failed:
                    results.append((None, target_name, "skipped - batch stopped after a sync failure"))
                    for _ in range(len(source_names) + 1):
                        self._progress_done_one()
                    continue

                log_start = len(self.logger.entries)
                self._progress_step("Opening {0}".format(target_name))
                self._log("Opening host '{0}'...".format(target_name))
                target_item_id = self._all_items.get(target_name)
                doc, detail = afb.open_cloud_document_attached(
                    self.application, self._region, self._project_id, target_item_id, self._token)
                if doc is None:
                    results.append((False, target_name, "could not open: {0}".format(detail)))
                    self._log("  FAILED to open - {0}".format(detail))
                    for _ in range(len(source_names) + 1):
                        self._progress_done_one()
                    continue

                try:
                    existing = dms.existing_link_names(doc) if skip_existing else set()
                    linked_here = 0
                    for source_name in source_names:
                        if skip_existing and dms.already_linked(existing, source_name):
                            results.append((None, "{0} -> {1}".format(source_name, target_name),
                                            "already linked - skipped"))
                            self._log("  '{0}' already linked - skipped".format(source_name))
                            self._progress_done_one()
                            continue

                        source_item_id = self._all_items.get(source_name)
                        cloud_path, path_detail = afb.cloud_model_path(
                            self._region, self._project_id, source_item_id, self._token)
                        if cloud_path is None:
                            results.append((False, "{0} -> {1}".format(source_name, target_name), path_detail))
                            self._log("  '{0}' path failed - {1}".format(source_name, path_detail))
                            self._progress_done_one()
                            continue

                        placement, attachment, fallback = self._effective_link_options(source_name, target_name)
                        self._progress_step("Linking {0} -> {1}".format(source_name, target_name))
                        ok, link_detail = dms.link_into(
                            doc, source_name, cloud_path, placement, fallback,
                            attachment=attachment, logger=self.logger)
                        results.append((ok, "{0} -> {1}".format(source_name, target_name), link_detail))
                        self._log("  '{0}': {1}".format(source_name, link_detail))
                        if ok:
                            linked_here += 1
                        self._progress_done_one()

                    if linked_here:
                        self._progress_step("Synchronizing {0}".format(target_name))
                        ok_sync, sync_detail = docmgr.synchronize_with_central(
                            doc, comment=comment, compact=False, logger=self.logger)
                        results.append((ok_sync, target_name,
                                        "synchronized" if ok_sync else "sync failed: {0}".format(sync_detail)))
                        self._log("  host {0}".format("Synchronized" if ok_sync else "Linked but sync FAILED"))
                        self._progress_done_one()
                        if not ok_sync:
                            self._log("  SYNC ERROR: {0}".format(sync_detail))
                            sync_failed = True
                    else:
                        results.append((None, target_name, "opened - nothing new to link"))
                        self._progress_done_one()
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

        self._progress_end()
        if sync_failed:
            forms.alert(
                "A host was linked but could NOT be synchronized, so the batch was stopped "
                "before touching any more models.\n\nThat model now has the link but has not "
                "been sent to ACC. Open it in Revit, synchronize manually if you want to keep "
                "the change, and relinquish - then re-run.\n\nThe reason is in the live status "
                "log and the report.",
                title="DeeSuperLINK - stopped after a sync failure")
        self._report(results)
        ok_count = sum(1 for r in results if r[0] is True)
        fail = sum(1 for r in results if r[0] is False)
        self.status_tb.Text = "{0} succeeded, {1} failed. See the pyRevit output window.".format(
            ok_count, fail)
        self._log(self.status_tb.Text)

    # ---------------- Save / Load Set (ACC project + options + matches) ----------------
    def save_set_click(self, sender, args):
        if not self._all_items and not self._matches:
            forms.alert("Scan a project first - nothing to save yet.")
            return
        dlg = SaveFileDialog()
        dlg.Filter = "DeeSuperLINK Set (*.json)|*.json"
        dlg.FileName = "DeeSuperLINK_Set.json"
        if dlg.ShowDialog() != DialogResult.OK:
            return

        data = {
            "format": 1,
            "acc_context": {
                "hub_id": self._hub_id or "",
                "region": self._region or "",
                "project_id": self._project_id or "",
                "project_name": self._project_name or "",
            },
            "link_settings": {
                "placement_index": self.placement_cb.SelectedIndex,
                "sync_comment": self.sync_comment_tb.Text or "",
                "attachment": "Attachment" if self.attachment_attachment_rb.IsChecked else "Overlay",
                "skip_existing": bool(self.skip_existing_cb.IsChecked),
            },
            "matches": msio.matches_to_json(self._matches),
            "match_options": msio.match_options_to_json(self._match_options),
            "matrix_separators": msio.separators_to_json(self._matrix),
        }
        try:
            msio.write_set(dlg.FileName, data)
        except Exception as e:
            forms.alert("Could not save the set: {0}".format(e))
            return
        self._log("Saved set ({0} match(es)) to {1}".format(len(self._matches), dlg.FileName))
        MessageBox.Show("Set saved to:\n{0}".format(dlg.FileName), _TOOL_NAME)

    def load_set_click(self, sender, args):
        dlg = OpenFileDialog()
        dlg.Filter = "DeeSuperLINK Set (*.json)|*.json"
        dlg.Title = "Load a saved DeeSuperLINK Set"
        if dlg.ShowDialog() != DialogResult.OK:
            return

        data, error = msio.read_set(dlg.FileName)
        if error:
            forms.alert(error)
            return

        if self._matches or self._all_items:
            if not forms.alert("Loading a set rescans its ACC project and replaces the current "
                                 "matches. Continue?", title=_TOOL_NAME + " - confirm", yes=True, no=True):
                return

        link_settings = data.get("link_settings") or {}
        try:
            idx = link_settings.get("placement_index", 0)
            self.placement_cb.SelectedIndex = (
                idx if isinstance(idx, int) and 0 <= idx < len(dms.PLACEMENT_OPTIONS) else 0)
        except Exception:
            pass
        self.sync_comment_tb.Text = link_settings.get("sync_comment") or "Synchronize"
        if link_settings.get("attachment") == "Attachment":
            self.attachment_attachment_rb.IsChecked = True
        else:
            self.attachment_overlay_rb.IsChecked = True
        self.skip_existing_cb.IsChecked = bool(link_settings.get("skip_existing", True))

        acc_context = data.get("acc_context") or {}
        self._hub_id = acc_context.get("hub_id") or None
        self._region = acc_context.get("region") or None
        self._project_id = acc_context.get("project_id") or None
        self._project_name = acc_context.get("project_name") or ""
        if not self._project_id:
            forms.alert("This set has no ACC project reference - pick a project and scan again.")
            return
        try:
            self._token = acc_auth.get_access_token()
        except Exception as e:
            forms.alert("Authentication failed:\n{0}".format(e))
            return
        self.project_tb.Text = self._project_name or self._project_id

        pending_matches = msio.matches_from_json(data.get("matches"))
        pending_options = msio.match_options_from_json(data.get("match_options"))

        self.refresh_click(None, None)
        if not self._project_id:
            return

        # refresh_click rebuilds self._all_items/Wire Map/Matrix from the
        # FRESH scan - matches/options loaded above are applied against
        # that fresh pool now, never guessed at against a stale one. A
        # pair naming a file no longer present in the rescanned project
        # is dropped, not silently kept pointing at nothing.
        known = set(self._all_items.keys())
        kept_matches = [(s, t) for s, t in pending_matches if s in known and t in known]
        dropped = len(pending_matches) - len(kept_matches)
        self._matches = kept_matches
        kept_set = set(kept_matches)
        self._match_options = dict((pair, opts) for pair, opts in pending_options.items() if pair in kept_set)
        msio.separators_from_json(data.get("matrix_separators"), self._matrix)
        self._matrix.built = False
        if getattr(self, "_matrix_ready", False) and self.mode_tabs.SelectedIndex == 2:
            self._guard_map(self._matrix_build)

        self._refresh_matches()
        summary = "Loaded {0} match(es) from {1}.".format(len(self._matches), dlg.FileName)
        if dropped:
            summary += " {0} match(es) dropped - their file(s) are no longer in this project.".format(dropped)
        self._log(summary)
        forms.alert(summary, title=_TOOL_NAME)

    # ---------------- run ----------------
    def run_click(self, sender, args):
        if not self._project_id:
            forms.alert("Pick a hub and project first.")
            return
        single = self.single_cb.SelectedItem
        if not single:
            forms.alert("Pick the single model in the dropdown first.")
            return
        # Deliberately the ELIGIBLE set, not the visible one: files
        # ticked before a search was typed still count. _refresh_rows
        # warns on screen when the search is hiding any of them.
        ticked = [r for r in self._eligible_rows() if r.selected]
        if not ticked:
            forms.alert("Tick at least one file in the list.")
            return

        mode = self._mode()
        placement_label = self.placement_cb.SelectedItem
        if mode == MODE_ONE_TO_MANY:
            hosts = ticked
            links_for = lambda host: [single]
            summary = "Link '{0}' into {1} host model(s)".format(single, len(hosts))
        else:
            hosts = [r for r in self._rows if r.name == single] or [FileRow(single, self._all_items[single])]
            links_for = lambda host: [r.name for r in ticked]
            summary = "Link {0} model(s) into '{1}'".format(len(ticked), single)

        if not forms.alert(
                "{0}, placed {1}?\n\nEach host is opened, linked, and SYNCHRONIZED back to ACC - "
                "this modifies {2} real cloud model(s).".format(
                    summary, placement_label, len(hosts)),
                title="DeeSuperLINK - Confirm", yes=True, no=True):
            return

        placement = self._placement()
        attachment = self._attachment()
        # Shared coordinates only mean something when the two models
        # actually share them - fall back to origin-to-origin otherwise,
        # per the user's explicit request.
        fallback = ImportPlacement.Origin if placement == ImportPlacement.Shared else None
        comment = self.sync_comment_tb.Text or ""
        skip_existing = (self.skip_existing_cb.IsChecked is True)
        results = []

        total_steps = sum(len(links_for(h)) + 1 for h in hosts)   # +1 = sync per host
        self._progress_begin(total_steps)
        sync_failed = False

        # Wired for the whole batch (not per-host) so a native dialog
        # fired by ANY host is auto-resolved instead of hanging Revit
        # indefinitely - the missing piece behind the live "crashing"
        # report. DeeMAPLink (built later, reusing this tool as its
        # model) already had this; DeeSuperLINK did not. Fixed to match,
        # same wiring pattern as DeeMAPLink's own run_click.
        dialog_handler = ffh.make_dialog_handler(self.logger)
        try:
            self.uiapp.DialogBoxShowing += dialog_handler
        except Exception as e:
            self.logger.exception("Could not attach dialog handler", e)

        try:
            for host in hosts:
                if sync_failed:
                    # Stop rather than keep opening and modifying more cloud
                    # models. A sync failure means this host is already left
                    # changed-but-unsynced; carrying on would multiply that
                    # across the batch, and each additional open model adds
                    # memory pressure - the first live run crashed Revit
                    # while opening host 5 of 8 after two failed syncs.
                    host.status = "Not attempted - batch stopped"
                    results.append((None, host.name, "skipped - batch stopped after a sync failure"))
                    for _ in range(len(links_for(host)) + 1):
                        self._progress_done_one()
                    continue
                log_start = len(self.logger.entries)
                self._progress_step("Opening {0}".format(host.name))
                self._log("Opening host '{0}'...".format(host.name))
                doc, detail = afb.open_cloud_document_attached(
                    self.application, self._region, self._project_id, host.item_id, self._token)
                if doc is None:
                    host.status = "Failed to open"
                    results.append((False, host.name, "could not open: {0}".format(detail)))
                    self._log("  FAILED to open - {0}".format(detail))
                    for _ in range(len(links_for(host)) + 1):
                        self._progress_done_one()
                    continue
                try:
                    existing = dms.existing_link_names(doc) if skip_existing else set()
                    linked_here = 0
                    for link_name in links_for(host):
                        if skip_existing and dms.already_linked(existing, link_name):
                            results.append((None, "{0} -> {1}".format(link_name, host.name),
                                            "already linked - skipped"))
                            self._log("  '{0}' already linked - skipped".format(link_name))
                            self._progress_done_one()
                            continue
                        item_id = self._all_items.get(link_name)
                        cloud_path, path_detail = afb.cloud_model_path(
                            self._region, self._project_id, item_id, self._token)
                        if cloud_path is None:
                            results.append((False, "{0} -> {1}".format(link_name, host.name), path_detail))
                            self._log("  '{0}' path failed - {1}".format(link_name, path_detail))
                            self._progress_done_one()
                            continue
                        self._progress_step("Linking {0} -> {1}".format(link_name, host.name))
                        ok, link_detail = dms.link_into(doc, link_name, cloud_path, placement, fallback,
                                                     attachment=attachment, logger=self.logger)
                        results.append((ok, "{0} -> {1}".format(link_name, host.name), link_detail))
                        self._log("  '{0}': {1}".format(link_name, link_detail))
                        if ok:
                            linked_here += 1
                        self._progress_done_one()

                    if linked_here:
                        self._progress_step("Synchronizing {0}".format(host.name))
                        ok_sync, sync_detail = docmgr.synchronize_with_central(
                            doc, comment=comment, compact=False, logger=self.logger)
                        host.status = "Synchronized" if ok_sync else "Linked but sync FAILED"
                        results.append((ok_sync, host.name,
                                        "synchronized" if ok_sync else "sync failed: {0}".format(sync_detail)))
                        self._log("  host {0}".format(host.status))
                        self._progress_done_one()
                        if not ok_sync:
                            # Surface WHY immediately. This detail previously
                            # only reached the end-of-run report, so when the
                            # first live run crashed mid-batch the user never
                            # saw the reason at all.
                            self._log("  SYNC ERROR: {0}".format(sync_detail))
                            sync_failed = True
                    else:
                        host.status = "Nothing to link"
                        self._progress_done_one()
                except Exception as e:
                    host.status = "Failed"
                    results.append((False, host.name, "unexpected error: {0}".format(e)))
                    self.logger.exception("Unexpected error linking", e, file=host.name)
                finally:
                    log_end = len(self.logger.entries)
                    issues = dms.issues_from_log(self.logger, log_start, log_end)
                    if issues:
                        results.append((None, host.name + " - issues seen", issues))
                    try:
                        docmgr.close_document(doc, save_modified=False, logger=self.logger)
                    except Exception:
                        pass
        finally:
            try:
                self.uiapp.DialogBoxShowing -= dialog_handler
            except Exception:
                pass

        self._progress_end()
        self._refresh_rows()
        if sync_failed:
            forms.alert(
                "A host was linked but could NOT be synchronized, so the batch was stopped "
                "before touching any more models.\n\n"
                "That model now has the link but has not been sent to ACC. Open it in Revit, "
                "synchronize manually if you want to keep the change, and relinquish - then "
                "re-run.\n\n"
                "The reason is in the live status log and the report.",
                title="DeeSuperLINK - stopped after a sync failure")
        self._report(results)
        ok_count = sum(1 for r in results if r[0] is True)
        fail = sum(1 for r in results if r[0] is False)
        self.status_tb.Text = "{0} succeeded, {1} failed. See the pyRevit output window.".format(
            ok_count, fail)
        self._log(self.status_tb.Text)

    def _report(self, results):
        html = ['<h2 style="font-family:sans-serif;color:#ddd;">DeeSuperLINK - Results</h2>']
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

    def close_click(self, sender, args):
        self.Close()


def main():
    window = DeeSuperLinkWindow(_XAML_FILE, __revit__)
    window.ShowDialog()


main()
