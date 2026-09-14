# -*- coding: utf-8 -*-
"""
DeeMAPLink
Scans an ACC project OR a local folder for worksharing Revit files, pools
them into two lists, and lets you build a many-to-many link map between
them: tick files in List 1 (sources), tick files in List 2 (targets),
press Add Match. Every target host is then opened headless, every matched
source linked into it, and the host synchronized back - no file ever
opened by hand.

--------------------------------------------------------------------
How it differs from DeeSuperLINK (the tool this one is modeled on)
--------------------------------------------------------------------
DeeSuperLINK is ACC-only and has exactly two fixed shapes: one model into
many hosts, or many models into one host. DeeMAPLink adds a Local-folder
source (deew_model_scanner's closed-file worksharing scan, the same
mechanism DeeW.Sharing uses) and replaces the fixed shape with an
arbitrary match set the user builds by hand between two lists of the same
scanned files - so a single run can link some sources into some hosts and
different sources into others, all at once.

It also adds three things DeeSuperLINK does not have: deew_failure_handler
wired up for the whole run (native-dialog auto-resolution + transaction
failure preprocessing - DeeSuperLINK predates that module), a per-host
"Issues" line in the final report built from a log-index slice around
each host's open/close, and a pre-run Analysis panel with a rough time
estimate, kept in sync with the match list at all times.

--------------------------------------------------------------------
Reused rather than reinvented - see lib/dee_maplink_service.py's own
docstring for the full list and exactly what each piece is proven to do.
The embedded, ETA-aware progress bar (_pump/_progress_begin/_progress_
render/_progress_step/_progress_done_one/_progress_end) is copied
directly from DeeSuperLINK, extended here with an indeterminate mode +
_progress_set_total so the same bar can also cover the scan step (ACC
project listing or local folder scan), not just the run - DeeSuperLINK's
own scan step still relies on a separate _SafeProgress/forms.ProgressBar
fallback that shows nothing at all in this user's Remote Desktop
environment (see feedback_progressbar_before_window_shown.md).
--------------------------------------------------------------------

NEEDS LIVE-REVIT VERIFICATION - see lib/dee_maplink_service.py's module
docstring for the full list (local ModelPath linking, local-central open
+ sync, and the time-estimate constants are all unverified against a
real project).
"""
import os
import time
import datetime

from pyrevit import forms, script
import dee_branding

import clr
clr.AddReference("PresentationFramework")
clr.AddReference("WindowsBase")
clr.AddReference("System.Windows.Forms")
from System import Action
# Namespaces matter here and are easy to get wrong: Cursors looks like
# it belongs with Thickness and CornerRadius, but it lives in
# System.Windows.Input alongside Keyboard - importing it from
# System.Windows fails at load with "Cannot import name Cursors".
from System.Windows import (CornerRadius, Point, TextTrimming, Thickness,
                            VerticalAlignment, Visibility)
from System.Windows.Controls import (Border, Canvas, ComboBox, Panel,
                                     TextBlock, WrapPanel)
from System.Windows.Input import Cursors, Keyboard, ModifierKeys
from System.Windows.Media import (Brushes, Color, DoubleCollection,
                                  PointCollection, SolidColorBrush,
                                  VisualTreeHelper)
from System.Windows.Shapes import Line, Polygon
from System.Windows.Threading import Dispatcher, DispatcherFrame, DispatcherPriority
from System.Windows.Forms import FolderBrowserDialog, DialogResult

from Autodesk.Revit.DB import ImportPlacement, ModelPathUtils

import acc_auth
import acc_file_browser as afb
import deew_document_manager as docmgr
import deew_failure_handler as ffh
import deew_logger
import dee_linkmap_service as lms
import dee_maplink_service as dms
import dee_telemetry
dee_telemetry.check_access("DeeMAPLink")


output = script.get_output()

_TOOL_NAME = "DeeMAPLink"
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")
_CACHE_FILE = os.path.join(_THIS_DIR, ".acc_file_cache.json")


class FileRow(object):
    """One scanned Revit file. `ref` is an ACC item_id or a local file
    path, depending on which source mode found it - script.py is the
    only place that needs to know which, via self._source_mode()."""
    def __init__(self, name, ref):
        self.selected = False
        self.name = name
        self.ref = ref


class MatchRow(object):
    """One row in the matches grid - plain source/target strings, the
    same shape dee_maplink_service.build_matches() works with."""
    def __init__(self, source, target):
        self.source = source
        self.target = target


class DeeMAPLinkWindow(dee_branding.DeeBrandedWindow):
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
        self._local_folder = None
        self._all_items = {}          # {display name: ref (item_id or file_path)}
        self._rows1 = []
        self._rows2 = []
        self._matches = []            # [(source_name, target_name), ...]
        self._log_lines = []
        self._prog_total = 1
        self._prog_done = 0
        self._prog_start = time.time()

        self.placement_cb.ItemsSource = [label for label, _v in dms.PLACEMENT_OPTIONS]
        self.placement_cb.SelectedIndex = 0
        self._refresh_matches()
        self._log("Ready. Pick an ACC project or a local folder to begin.")

        # The canvas handlers live on the CANVAS, not the boxes:
        # a move or a release that ends on empty space still has
        # to finish the gesture cleanly.
        self._map_pos = {}
        self._map_boxes = {}
        self._map_wire_shapes = []
        self._map_filter_cbs = []
        self._map_drag = None
        self._map_preview = None
        self.map_canvas.MouseMove += self._map_canvas_move
        self.map_canvas.MouseLeftButtonUp += self._map_canvas_up
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

    def _safe_text(self, textbox):
        try:
            return textbox.Text or ""
        except Exception:
            return ""

    def _source_mode(self):
        return "acc" if self.source_acc_rb.IsChecked is True else "local"

    def _placement(self):
        idx = self.placement_cb.SelectedIndex
        if idx is None or idx < 0:
            idx = 0
        return dms.PLACEMENT_OPTIONS[idx][1]

    # ---------------- in-window progress bar ----------------
    # Copied from DeeSuperLINK (same reasoning: a modal WPF window does
    # not repaint during a long synchronous loop, so _pump() drains
    # pending render work at Background priority after each update),
    # extended with an indeterminate mode + _progress_set_total so the
    # SAME bar also covers the scan step, not just the run.
    def _pump(self):
        try:
            frame = DispatcherFrame()

            def _stop():
                frame.Continue = False

            self.Dispatcher.BeginInvoke(DispatcherPriority.Background, Action(_stop))
            Dispatcher.PushFrame(frame)
        except Exception:
            pass

    def _progress_begin(self, total_steps, indeterminate=False):
        self._prog_total = max(1, total_steps)
        self._prog_done = 0
        self._prog_start = time.time()
        try:
            self.progress_bar.IsIndeterminate = indeterminate
            self.progress_bar.Minimum = 0
            self.progress_bar.Maximum = self._prog_total
            self.progress_bar.Value = 0
            self.progress_host.Visibility = Visibility.Visible
            self.run_b.IsEnabled = False
        except Exception:
            pass
        self._progress_render("Starting...")

    def _progress_set_total(self, total_steps):
        """Switches an indeterminate bar to determinate once a real
        total becomes known mid-scan (e.g. after the first folder-scan
        callback reveals the file count)."""
        self._prog_total = max(1, total_steps)
        try:
            self.progress_bar.IsIndeterminate = False
            self.progress_bar.Maximum = self._prog_total
        except Exception:
            pass

    def _progress_render(self, label):
        try:
            elapsed = time.time() - self._prog_start
            done = self._prog_done
            if done > 0:
                remaining = (elapsed / float(done)) * (self._prog_total - done)
                eta = "ETA {0}".format(dms.format_duration(remaining))
            else:
                eta = "ETA --"
            self.progress_bar.Value = min(done, self._prog_total)
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
            self.progress_bar.Value = min(self._prog_done, self._prog_total)
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

    # ---------------- source mode ----------------
    def source_mode_changed(self, sender, args):
        # Fires during XAML load (source_acc_rb starts IsChecked="True"),
        # before the other fields exist yet.
        try:
            is_acc = self._source_mode() == "acc"
            self.acc_row.Visibility = Visibility.Visible if is_acc else Visibility.Collapsed
            self.local_row.Visibility = Visibility.Collapsed if is_acc else Visibility.Visible
        except Exception:
            return

    # ---------------- ACC ----------------
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
        self._hub_id = hub_id
        self._region = region
        self._project_id = project_id
        self._project_name = project_name
        self.project_tb.Text = "{0} / {1}".format(hub_name, project_name)
        self._log("Loading Revit files in '{0}'...".format(project_name))
        self._scan_acc()

    def _scan_acc(self):
        if not self._project_id:
            forms.alert("Pick a hub and project first.")
            return
        self._progress_begin(1, indeterminate=True)
        self._progress_step("Loading project files from ACC (large projects can take a while)...")
        try:
            self._all_items = afb.list_project_files(
                self._hub_id, self._project_id, self._token, _CACHE_FILE) or {}
        except Exception as e:
            self._progress_end()
            forms.alert("Could not list project files:\n{0}".format(e))
            return
        self._progress_done_one()
        self._progress_end()
        self._after_scan()

    # ---------------- Local ----------------
    def pick_folder_click(self, sender, args):
        dlg = FolderBrowserDialog()
        dlg.Description = "Pick a folder to scan for worksharing Revit files"
        if dlg.ShowDialog() != DialogResult.OK:
            return
        self._local_folder = dlg.SelectedPath
        self.folder_tb.Text = self._local_folder

    def scan_folder_click(self, sender, args):
        if not self._local_folder:
            forms.alert("Pick a folder first.")
            return
        self._scan_local()

    def _scan_local(self):
        recursive = bool(self.recursive_cb.IsChecked)
        self._progress_begin(1, indeterminate=True)
        self._progress_step("Scanning folder...")
        state = {"known": False}

        def cb(i, total, name):
            if not state["known"]:
                self._progress_set_total(total)
                state["known"] = True
            self._prog_done = i
            self._progress_render("Scanning: {0}".format(name))

        self._all_items = dms.scan_local_folder(self._local_folder, recursive, progress_cb=cb)
        self._progress_done_one()
        self._progress_end()
        self._after_scan()

    # ---------------- after either scan ----------------
    def _after_scan(self):
        names = sorted(self._all_items.keys())
        self._rows1 = [FileRow(n, self._all_items[n]) for n in names]
        self._rows2 = [FileRow(n, self._all_items[n]) for n in names]
        self._refresh_list1()
        self._refresh_list2()
        if not names:
            self.status_tb.Text = "No worksharing Revit files found."
            forms.alert(self.status_tb.Text)
        else:
            self.status_tb.Text = "{0} worksharing file(s) found. Tick sources and targets, then Add Match.".format(
                len(names))
        self._log(self.status_tb.Text)

    # ---------------- lists ----------------
        if getattr(self, "_map_ready", False):
            try:
                self._map_build_filters()
                self._map_build(keep_positions=False)
            except Exception as e:
                self.logger.exception("Could not build the wire map", e)

    def _visible(self, rows, query):
        return [r for r in rows if dms.matches_search(r.name, query)]

    def _refresh_list1(self):
        visible = self._visible(self._rows1, self._safe_text(self.search1_tb))
        self.list1_grid.ItemsSource = None
        self.list1_grid.ItemsSource = visible

    def _refresh_list2(self):
        visible = self._visible(self._rows2, self._safe_text(self.search2_tb))
        self.list2_grid.ItemsSource = None
        self.list2_grid.ItemsSource = visible

    def search1_changed(self, sender, args):
        try:
            self._refresh_list1()
        except Exception:
            pass

    def search2_changed(self, sender, args):
        try:
            self._refresh_list2()
        except Exception:
            pass

    def clear_search1_click(self, sender, args):
        self.search1_tb.Text = ""

    def clear_search2_click(self, sender, args):
        self.search2_tb.Text = ""

    def select_all1_click(self, sender, args):
        query = self._safe_text(self.search1_tb)
        for r in self._rows1:
            if dms.matches_search(r.name, query):
                r.selected = True
        self._refresh_list1()

    def select_none1_click(self, sender, args):
        query = self._safe_text(self.search1_tb)
        for r in self._rows1:
            if dms.matches_search(r.name, query):
                r.selected = False
        self._refresh_list1()

    def select_all2_click(self, sender, args):
        query = self._safe_text(self.search2_tb)
        for r in self._rows2:
            if dms.matches_search(r.name, query):
                r.selected = True
        self._refresh_list2()

    def select_none2_click(self, sender, args):
        query = self._safe_text(self.search2_tb)
        for r in self._rows2:
            if dms.matches_search(r.name, query):
                r.selected = False
        self._refresh_list2()

    # ---------------- matches ----------------
    # ---------------- error guard ----------------
    def _guard(self, fn):
        """Runs fn and turns any escaping exception into a readable
        dialog plus a log entry. An unhandled exception in a WPF click
        handler surfaces as a bare pyRevit traceback window, which tells
        the user nothing about what they were doing."""
        try:
            fn()
        except Exception as e:
            import traceback
            self.logger.exception("Unhandled error", e)
            forms.alert("DeeMAPLink hit an error:\n\n{0}\n\n{1}".format(
                e, traceback.format_exc()[-900:]), title="DeeMAPLink")

    # ---------------- wire map ----------------
    # A second way to build the same match list: drag a wire from one
    # file to another and that IS the match. Everything here edits
    # self._matches, the same list the two-list tab builds, so the two
    # tabs are two views of one plan rather than two plans.
    #
    # Drawn with plain WPF shapes on a Canvas. Hit-testing on mouse-up
    # goes through Canvas.InputHitTest rather than mouse capture: capture
    # would send the release to the box the drag STARTED on, which is
    # exactly the box we do not want.

    # DeePack's orange, the same accent the maps and the branding
    # bar use, so a wire reads as 'this tool drew that'.
    _WIRE_BRUSH = SolidColorBrush(Color.FromRgb(0xF2, 0x99, 0x4D))

    _BOX_W = 250
    _BOX_H = 34
    _COL_GAP = 26
    _ROW_GAP = 14

    def _guard_map(self, fn):
        """Canvas handlers are called by WPF, so an exception escaping
        one surfaces as a bare pyRevit traceback with no clue which
        gesture caused it."""
        try:
            fn()
        except Exception as e:
            import traceback
            self.logger.exception("Wire map error", e)
            forms.alert("The wire map hit an error:\n\n{0}\n\n{1}".format(
                e, traceback.format_exc()[-700:]), title="DeeMAPLink")

    def _map_visible_names(self):
        query = self._safe_text(self.map_search_tb)
        names = [n for n in sorted(self._all_items.keys())
                 if dms.matches_search(n, query)]
        # Search AND every chosen part - narrowing, the way anyone
        # expects stacked filters to behave.
        for index, combo in getattr(self, "_map_filter_cbs", []):
            want = combo.SelectedItem
            if not want or want == self._MAP_ALL:
                continue
            names = [n for n in names if self._map_segment(n, index) == want]
        return names

    def _map_build(self, keep_positions=False):
        """Lays the file boxes out and redraws every wire.

        keep_positions keeps whatever the user has dragged boxes to, so
        a search or a new wire does not throw their arrangement away."""
        if not hasattr(self, "_map_pos") or not keep_positions:
            self._map_pos = {}
        self._map_boxes = {}
        canvas = self.map_canvas
        canvas.Children.Clear()

        names = self._map_visible_names()
        self.map_count_tb.Text = "{0} of {1} file(s)".format(
            len(names), len(self._all_items))
        if not names:
            return

        # Grid placement, widest-first columns. Only used for boxes that
        # have no position yet, so dragged boxes stay where they were put.
        area_w = max(900, int(canvas.Width))
        per_row = max(1, int((area_w - self._COL_GAP) /
                             (self._BOX_W + self._COL_GAP)))
        index = 0
        for name in names:
            if name not in self._map_pos:
                col = index % per_row
                row = index // per_row
                self._map_pos[name] = (
                    self._COL_GAP + col * (self._BOX_W + self._COL_GAP),
                    self._ROW_GAP + row * (self._BOX_H + self._ROW_GAP))
            index += 1

        # Grow the canvas to whatever the layout actually needs, so the
        # scrollbars can reach the last row.
        max_y = max(self._map_pos[n][1] for n in names) + self._BOX_H + 40
        if max_y > canvas.Height:
            canvas.Height = max_y

        for name in names:
            box = self._map_make_box(name)
            x, y = self._map_pos[name]
            Canvas.SetLeft(box, x)
            Canvas.SetTop(box, y)
            Panel.SetZIndex(box, 10)
            canvas.Children.Add(box)
            self._map_boxes[name] = box

        self._map_draw_wires()

    def _map_make_box(self, name):
        colour = self._map_colour_for(name)
        text = TextBlock()
        text.Text = name
        text.Foreground = Brushes.WhiteSmoke
        text.FontSize = 10
        text.TextTrimming = TextTrimming.CharacterEllipsis
        text.VerticalAlignment = VerticalAlignment.Center
        text.Margin = Thickness(6, 0, 6, 0)
        text.IsHitTestVisible = False      # clicks belong to the box

        box = Border()
        box.Width = self._BOX_W
        box.Height = self._BOX_H
        box.CornerRadius = CornerRadius(4)
        box.BorderThickness = Thickness(2)
        box.BorderBrush = colour
        box.Background = SolidColorBrush(Color.FromRgb(0x2B, 0x2B, 0x2B))
        box.Child = text
        box.Cursor = Cursors.Cross
        box.ToolTip = name
        box.Tag = name
        box.MouseLeftButtonDown += self._map_box_down
        return box

    def _map_colour_for(self, name):
        """Discipline colour, read from the same configured code list
        DeeLinkMAP's map uses - so a file is the same colour in both."""
        try:
            _code, label = lms.detect_discipline(name, lms.load_disciplines())
            if not hasattr(self, "_map_colours"):
                self._map_colours = {}
            if label not in self._map_colours:
                palette = lms._PALETTE
                used = len(self._map_colours)
                hexed = (lms._UNKNOWN_COLOR if label == lms.UNKNOWN_LABEL
                         else palette[used % len(palette)])
                self._map_colours[label] = SolidColorBrush(Color.FromRgb(
                    int(hexed[1:3], 16), int(hexed[3:5], 16), int(hexed[5:7], 16)))
            return self._map_colours[label]
        except Exception:
            return SolidColorBrush(Color.FromRgb(0x88, 0x88, 0x88))

    # ---- part filters -------------------------------------------
    # The naming convention is positional, so part 3 is always the zone
    # and part 7 always the discipline. Every part that VARIES across the
    # scanned files becomes a dropdown; a part that is the same in every
    # name (the project code, "MOD", the trailing zeros) is a constant,
    # not a filter, and is left out.

    _MAP_ALL = "(all)"

    def _map_segment(self, name, index):
        parts = lms.name_segments(name)
        return parts[index] if 0 <= index < len(parts) else ""

    def _map_build_filters(self):
        """Rebuilds the dropdown row for whatever was just scanned."""
        panel = self.map_filters_panel
        panel.Children.Clear()
        self._map_filter_cbs = []
        names = sorted(self._all_items.keys())
        if not names:
            return

        longest = 0
        for name in names:
            longest = max(longest, len(lms.name_segments(name)))

        for index in range(longest):
            values = sorted(set(self._map_segment(n, index) for n in names))
            values = [v for v in values if v]
            if len(values) < 2:
                continue

            label = TextBlock()
            label.Text = "Part {0}:".format(index + 1)
            label.Foreground = Brushes.Gray
            label.VerticalAlignment = VerticalAlignment.Center
            label.Margin = Thickness(8, 0, 4, 0)
            panel.Children.Add(label)

            combo = ComboBox()
            combo.Width = 92
            combo.Height = 22
            combo.ToolTip = "{0} value(s): {1}".format(
                len(values), ", ".join(values[:12]))
            combo.Items.Add(self._MAP_ALL)
            for value in values:
                combo.Items.Add(value)
            # Selected BEFORE the handler is attached: assigning it after
            # fires SelectionChanged during setup, which rebuilds the map
            # while it is still being built.
            combo.SelectedIndex = 0
            combo.SelectionChanged += self._map_filter_changed
            combo.Tag = index
            panel.Children.Add(combo)
            self._map_filter_cbs.append((index, combo))

    def _map_filter_changed(self, sender, args):
        if getattr(self, "_map_ready", False):
            self._guard_map(lambda: self._map_build(keep_positions=True))

    def _map_clear_filters(self):
        for _index, combo in getattr(self, "_map_filter_cbs", []):
            combo.SelectedIndex = 0

    # ---- drawing wires ----
    def _map_centre(self, name):
        x, y = self._map_pos.get(name, (0, 0))
        return x + self._BOX_W / 2.0, y + self._BOX_H / 2.0

    def _map_draw_wires(self):
        canvas = self.map_canvas
        for shape in list(getattr(self, "_map_wire_shapes", [])):
            try:
                canvas.Children.Remove(shape)
            except Exception:
                pass
        self._map_wire_shapes = []

        for index, pair in enumerate(self._matches):
            source, target = pair
            if source not in self._map_boxes or target not in self._map_boxes:
                continue       # one end filtered out of view
            x1, y1 = self._map_centre(source)
            x2, y2 = self._map_centre(target)

            line = Line()
            line.X1, line.Y1, line.X2, line.Y2 = x1, y1, x2, y2
            line.Stroke = self._WIRE_BRUSH
            line.StrokeThickness = 2.0
            line.ToolTip = "{0}\nlinked into\n{1}\n\n(click to remove)".format(
                source, target)
            line.Cursor = Cursors.Hand
            # A 2px line is nearly impossible to click, so an invisible
            # fat line sits under it and takes the clicks.
            hit = Line()
            hit.X1, hit.Y1, hit.X2, hit.Y2 = x1, y1, x2, y2
            hit.Stroke = Brushes.Transparent
            hit.StrokeThickness = 12.0
            hit.Cursor = Cursors.Hand
            hit.Tag = index
            line.Tag = index
            hit.MouseLeftButtonDown += self._map_wire_down
            line.MouseLeftButtonDown += self._map_wire_down

            head = self._map_arrow_head(x1, y1, x2, y2)
            for shape in (hit, line, head):
                Panel.SetZIndex(shape, 5)
                canvas.Children.Add(shape)
                self._map_wire_shapes.append(shape)

    def _map_arrow_head(self, x1, y1, x2, y2):
        """A filled triangle sitting ON the target box's edge, pointing
        at it - an arrow buried under the box shows no direction."""
        import math
        dx, dy = x2 - x1, y2 - y1
        length = math.sqrt(dx * dx + dy * dy) or 1.0
        ux, uy = dx / length, dy / length
        # step back to the box edge rather than its centre
        half_w, half_h = self._BOX_W / 2.0 + 2, self._BOX_H / 2.0 + 2
        scale = min(half_w / (abs(ux) or 0.0001), half_h / (abs(uy) or 0.0001))
        tipx, tipy = x2 - ux * scale, y2 - uy * scale
        size = 9.0
        left = (tipx - ux * size - uy * size * 0.55,
                tipy - uy * size + ux * size * 0.55)
        right = (tipx - ux * size + uy * size * 0.55,
                 tipy - uy * size - ux * size * 0.55)
        head = Polygon()
        head.Fill = self._WIRE_BRUSH
        head.IsHitTestVisible = False
        head.Points = PointCollection()
        head.Points.Add(Point(tipx, tipy))
        head.Points.Add(Point(left[0], left[1]))
        head.Points.Add(Point(right[0], right[1]))
        return head

    # ---- gestures ----
    def _map_box_down(self, sender, args):
        def run():
            name = sender.Tag
            if args.ClickCount == 2:
                return
            # SHIFT starts a move, anything else starts a wire - drawing
            # is what this surface is for, so it gets the plain drag.
            shift = (Keyboard.Modifiers & ModifierKeys.Shift) == ModifierKeys.Shift
            point = args.GetPosition(self.map_canvas)
            if shift:
                x, y = self._map_pos[name]
                self._map_drag = {"mode": "move", "name": name,
                                  "dx": point.X - x, "dy": point.Y - y}
            else:
                self._map_drag = {"mode": "wire", "name": name}
                preview = Line()
                preview.X1, preview.Y1 = self._map_centre(name)
                preview.X2, preview.Y2 = point.X, point.Y
                preview.Stroke = self._WIRE_BRUSH
                preview.StrokeThickness = 2.0
                preview.StrokeDashArray = DoubleCollection()
                preview.StrokeDashArray.Add(4.0)
                preview.StrokeDashArray.Add(3.0)
                preview.IsHitTestVisible = False
                Panel.SetZIndex(preview, 20)
                self.map_canvas.Children.Add(preview)
                self._map_preview = preview
                sender.BorderThickness = Thickness(3)
            args.Handled = True
        self._guard_map(run)

    def _map_canvas_move(self, sender, args):
        def run():
            drag = getattr(self, "_map_drag", None)
            if not drag:
                return
            point = args.GetPosition(self.map_canvas)
            if drag["mode"] == "wire":
                preview = getattr(self, "_map_preview", None)
                if preview is not None:
                    preview.X2, preview.Y2 = point.X, point.Y
            else:
                name = drag["name"]
                x = max(0, point.X - drag["dx"])
                y = max(0, point.Y - drag["dy"])
                self._map_pos[name] = (x, y)
                box = self._map_boxes.get(name)
                if box is not None:
                    Canvas.SetLeft(box, x)
                    Canvas.SetTop(box, y)
                self._map_draw_wires()
        self._guard_map(run)

    def _map_canvas_up(self, sender, args):
        def run():
            drag = getattr(self, "_map_drag", None)
            self._map_drag = None
            preview = getattr(self, "_map_preview", None)
            if preview is not None:
                try:
                    self.map_canvas.Children.Remove(preview)
                except Exception:
                    pass
                self._map_preview = None
            if not drag:
                return
            source = drag["name"]
            box = self._map_boxes.get(source)
            if box is not None:
                box.BorderThickness = Thickness(2)
            if drag["mode"] != "wire":
                return

            target = self._map_hit_name(args.GetPosition(self.map_canvas))
            if not target or target == source:
                return
            pair = (source, target)
            if pair in self._matches:
                self._log("Already matched: {0} -> {1}".format(source, target))
                return
            self._matches.append(pair)
            self._refresh_matches()
            self._log("Wired {0} into {1}".format(source, target))
        self._guard_map(run)

    def _map_hit_name(self, point):
        """Which box is under the point. Walks up from whatever was hit,
        because the visual under the cursor may be the TextBlock or the
        Border's own chrome rather than the Border itself."""
        try:
            hit = self.map_canvas.InputHitTest(point)
        except Exception:
            return None
        node = hit
        depth = 0
        while node is not None and depth < 8:
            tag = getattr(node, "Tag", None)
            if isinstance(tag, str) and tag in self._map_boxes:
                return tag
            try:
                node = VisualTreeHelper.GetParent(node)
            except Exception:
                return None
            depth += 1
        return None

    def _map_wire_down(self, sender, args):
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

    # ---- toolbar ----
    def map_search_changed(self, sender, args):
        if getattr(self, "_map_ready", False):
            self._guard_map(lambda: self._map_build(keep_positions=True))

    def map_clear_search_click(self, sender, args):
        # Clears the dropdowns as well - a "Clear" that leaves three
        # filters silently applied is a trap.
        self._map_clear_filters()
        self.map_search_tb.Text = ""
        if getattr(self, "_map_ready", False):
            self._guard_map(lambda: self._map_build(keep_positions=True))

    def map_refresh_click(self, sender, args):
        self._guard_map(lambda: self._map_build(keep_positions=False))

    def map_clear_wires_click(self, sender, args):
        def run():
            if not self._matches:
                return
            if not forms.alert("Remove all {0} wire(s)?".format(len(self._matches)),
                               yes=True, no=True):
                return
            self._matches = []
            self._refresh_matches()
        self._guard_map(run)

    def add_match_click(self, sender, args):
        sources = [r.name for r in self._rows1 if r.selected]
        targets = [r.name for r in self._rows2 if r.selected]
        if not sources or not targets:
            forms.alert("Tick at least one file in each list first.")
            return
        self._matches, added, skipped_self = dms.build_matches(self._matches, sources, targets)
        self._refresh_matches()
        msg = "Added {0} new match(es).".format(added)
        if skipped_self:
            msg += " ({0} self-match(es) skipped - a file can't be linked into itself.)".format(skipped_self)
        self._log(msg)
        self.status_tb.Text = msg

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

    # ---------------- opening / model paths (source-mode dispatch) ----------------
        # The two tabs are two views of ONE plan, so a match added
        # or removed on the lists tab redraws the wires too.
        if getattr(self, "_map_ready", False):
            try:
                self._map_draw_wires()
            except Exception:
                pass

    def _open_attached(self, target_name):
        if self._source_mode() == "acc":
            item_id = self._all_items.get(target_name)
            return afb.open_cloud_document_attached(
                self.application, self._region, self._project_id, item_id, self._token)
        file_path = self._all_items.get(target_name)
        return docmgr.open_document_no_detach(self.application, file_path, logger=self.logger)

    def _model_path_for(self, source_name):
        if self._source_mode() == "acc":
            item_id = self._all_items.get(source_name)
            return afb.cloud_model_path(self._region, self._project_id, item_id, self._token)
        file_path = self._all_items.get(source_name)
        try:
            return ModelPathUtils.ConvertUserVisiblePathToModelPath(file_path), "ok"
        except Exception as e:
            return None, str(e)

    # ---------------- run ----------------
    def run_click(self, sender, args):
        if not self._matches:
            forms.alert("Build at least one match first - tick files in both lists, press Add Match.")
            return
        mode = self._source_mode()
        if mode == "acc" and not self._project_id:
            forms.alert("Pick a hub and project first.")
            return
        if mode == "local" and not self._local_folder:
            forms.alert("Pick a folder first.")
            return

        groups = dms.group_by_target(self._matches)
        est = dms.estimate_seconds(groups)
        analysis = dms.analysis_text(groups, est)

        if not forms.alert(
                "{0}\n\nEach host is opened, linked, and SYNCHRONIZED back - this modifies {1} real "
                "shared model(s).\n\nContinue?".format(analysis, len(groups)),
                title="DeeMAPLink - Confirm", yes=True, no=True):
            return

        placement = self._placement()
        # Shared coordinates only mean something when the two models
        # actually share them - fall back to origin-to-origin otherwise.
        fallback = ImportPlacement.Origin if placement == ImportPlacement.Shared else None
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
                doc, detail = self._open_attached(target_name)
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

                        model_path, path_detail = self._model_path_for(source_name)
                        if model_path is None:
                            results.append((False, "{0} -> {1}".format(source_name, target_name), path_detail))
                            self._log("  '{0}' path failed - {1}".format(source_name, path_detail))
                            self._progress_done_one()
                            continue

                        self._progress_step("Linking {0} -> {1}".format(source_name, target_name))
                        ok, link_detail = dms.link_into(
                            doc, source_name, model_path, placement, fallback, logger=self.logger)
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
                "A host was linked but could NOT be synchronized, so the batch was stopped before "
                "touching any more models.\n\nThat model now has the link but has not been sent back. "
                "Open it, synchronize manually if you want to keep the change, and relinquish - then "
                "re-run.\n\nThe reason is in the live status log and the report.",
                title="DeeMAPLink - stopped after a sync failure")
        self._report(results)
        ok_count = sum(1 for r in results if r[0] is True)
        fail = sum(1 for r in results if r[0] is False)
        self.status_tb.Text = "{0} succeeded, {1} failed. See the pyRevit output window.".format(ok_count, fail)
        self._log(self.status_tb.Text)

    def _report(self, results):
        html = ['<h2 style="font-family:sans-serif;color:#ddd;">DeeMAPLink - Results</h2>']
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
    window = DeeMAPLinkWindow(_XAML_FILE, __revit__)
    window.ShowDialog()


main()
