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

from Autodesk.Revit.DB import ImportPlacement, ModelPathUtils, AttachmentType

import acc_auth
import acc_file_browser as afb
import deew_document_manager as docmgr
import deew_failure_handler as ffh
import deew_logger
import deew_settings
import dee_linkmap_service as lms
import dee_maplink_service as dms
import dee_telemetry
dee_telemetry.check_access("DeeMAPLink")


output = script.get_output()

_TOOL_NAME = "DeeMAPLink"
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")
_CACHE_FILE = os.path.join(_THIS_DIR, ".acc_file_cache.json")

# --------------------------------------------------------------------------
# Resumable batch - a HUGE host model can crash Revit itself (a native
# access violation deep in Revit's own file-loading code, 0xc0000005,
# confirmed live via a real crash journal 2026-09-18 - not something any
# Python try/except can catch, since it happens in unmanaged code before
# control ever returns here). That means one bad host can take down the
# whole session mid-batch. What IS fixable: which hosts were ALREADY
# successfully synchronized before that happens is durably recorded to
# disk the moment each one finishes - via lib/deew_settings.py's existing
# generic per-tool JSON store (same mechanism DeeSheet/DeeVSDupl already
# use for presets) - so re-running the same batch after a crash (or just
# reopening the tool later) skips hosts already done instead of redoing
# the whole thing from scratch.
# --------------------------------------------------------------------------
_PROGRESS_TOOL_NAME = "DeeMAPLink_progress"


def _progress_context_key(mode, project_id, local_folder):
    """One bucket per ACC project or per local folder, so completed-host
    history from one project never hides/skips a same-named host in a
    totally different project."""
    if mode == "acc":
        return "acc:{0}".format(project_id or "")
    return "local:{0}".format(local_folder or "")


def _load_done_hosts(context_key):
    data = deew_settings.load(_PROGRESS_TOOL_NAME, {})
    return dict(data.get(context_key, {}))


def _mark_host_done(context_key, target_name):
    """Called immediately after a host's Synchronize succeeds - never
    batched until the end, since the whole point is surviving a crash
    that happens later in the SAME run."""
    data = deew_settings.load(_PROGRESS_TOOL_NAME, {})
    bucket = dict(data.get(context_key, {}))
    bucket[target_name] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data[context_key] = bucket
    deew_settings.save(_PROGRESS_TOOL_NAME, data)


def _clear_done_hosts(context_key):
    data = deew_settings.load(_PROGRESS_TOOL_NAME, {})
    if context_key in data:
        del data[context_key]
        deew_settings.save(_PROGRESS_TOOL_NAME, data)


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
        self._map_left_rows = {}
        self._map_right_rows = {}
        self._map_wire_shapes = []
        self._map_left_cbs = []
        self._map_right_cbs = []
        self._map_pending = None
        # Wires are positioned from where the rows currently ARE, so they
        # have to be redrawn whenever a column scrolls or the middle
        # strip changes size.
        self.map_left_scroll.ScrollChanged += self._map_scrolled
        self.map_right_scroll.ScrollChanged += self._map_scrolled
        self.map_canvas.SizeChanged += self._map_scrolled
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

    def _attachment(self):
        if self.attachment_attachment_rb.IsChecked is True:
            return AttachmentType.Attachment
        return AttachmentType.Overlay

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
                self._map_build()
            except Exception as e:
                # Logged AND surfaced. This swallowed a TypeError once -
                # the call still passed keep_positions after the patchbay
                # rewrite dropped it - and the only symptom was a Wire Map
                # that stayed empty with no explanation anywhere.
                self.logger.exception("Could not build the wire map", e)
                self._log("Wire map could not be built: {0}".format(e))

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

    # ---------------- wire map (patchbay) ----------------
    # Sources down the left, targets down the right, wires across the
    # middle. Each column scrolls on its own, so either end of a link can
    # always be brought into view - which the free canvas could not do
    # once there were more files than fitted on screen.
    #
    # Connecting is click-then-click, not drag. Between the two clicks
    # you can scroll, search and filter freely, which is the whole point:
    # a drag cannot survive a scroll, so a drag can only ever link two
    # files that are already visible together.

    _WIRE_BRUSH = SolidColorBrush(Color.FromRgb(0xF2, 0x99, 0x4D))
    _ROW_BG = SolidColorBrush(Color.FromRgb(0x2B, 0x2B, 0x2B))
    _ROW_ARMED = SolidColorBrush(Color.FromRgb(0x4A, 0x3A, 0x22))
    _MAP_ALL = "(all)"

    def _guard_map(self, fn):
        """WPF calls these handlers, so an escaping exception surfaces as
        a bare pyRevit traceback with no clue which click caused it."""
        try:
            fn()
        except Exception as e:
            import traceback
            self.logger.exception("Wire map error", e)
            forms.alert("The wire map hit an error:\n\n{0}\n\n{1}".format(
                e, traceback.format_exc()[-700:]), title="DeeMAPLink")

    # ---- part filters, one set per column ------------------------
    # One shared filter could only narrow both sides at once, which is
    # the wrong shape: the useful question is nearly always "these
    # sources into those targets" - every AR model into the coordination
    # file - and that needs the two sides filtered differently.

    def _map_segment(self, name, index):
        parts = lms.name_segments(name)
        return parts[index] if 0 <= index < len(parts) else ""

    def _map_build_filters(self):
        """Builds both filter rows for whatever was just scanned."""
        self._map_left_cbs = self._map_fill_filter_panel(
            self.map_left_filters_panel, self._map_left_filter_changed)
        self._map_right_cbs = self._map_fill_filter_panel(
            self.map_right_filters_panel, self._map_right_filter_changed)

    def _map_fill_filter_panel(self, panel, handler):
        """One dropdown per name part that VARIES across the scan. A part
        identical in every name is a constant, not a filter."""
        panel.Children.Clear()
        combos = []
        names = sorted(self._all_items.keys())
        if not names:
            return combos

        longest = 0
        for name in names:
            longest = max(longest, len(lms.name_segments(name)))

        for index in range(longest):
            values = sorted(set(self._map_segment(n, index) for n in names))
            values = [v for v in values if v]
            if len(values) < 2:
                continue

            label = TextBlock()
            label.Text = "P{0}".format(index + 1)
            label.Foreground = Brushes.Gray
            label.FontSize = 10
            label.VerticalAlignment = VerticalAlignment.Center
            label.Margin = Thickness(6, 0, 3, 0)
            panel.Children.Add(label)

            combo = ComboBox()
            combo.Width = 74
            combo.Height = 21
            combo.FontSize = 10
            combo.ToolTip = "Part {0} - {1} value(s): {2}".format(
                index + 1, len(values), ", ".join(values[:12]))
            combo.Items.Add(self._MAP_ALL)
            for value in values:
                combo.Items.Add(value)
            # Selected BEFORE the handler is attached: setting it after
            # fires SelectionChanged during setup and rebuilds the map
            # while it is still being built.
            combo.SelectedIndex = 0
            combo.SelectionChanged += handler
            combo.Tag = index
            panel.Children.Add(combo)
            combos.append((index, combo))
        return combos

    def _map_left_filter_changed(self, sender, args):
        if getattr(self, "_map_ready", False):
            self._guard_map(self._map_build)

    def _map_right_filter_changed(self, sender, args):
        if getattr(self, "_map_ready", False):
            self._guard_map(self._map_build)

    def _map_clear_filters(self, combos):
        for _index, combo in combos:
            combo.SelectedIndex = 0

    def _map_names_for(self, side):
        """The files visible in ONE column, after that column's own
        search box and its own dropdowns."""
        if side == "source":
            query = self._safe_text(self.map_left_search_tb)
            combos = getattr(self, "_map_left_cbs", [])
        else:
            query = self._safe_text(self.map_right_search_tb)
            combos = getattr(self, "_map_right_cbs", [])

        names = [n for n in sorted(self._all_items.keys())
                 if dms.matches_search(n, query)]
        for index, combo in combos:
            want = combo.SelectedItem
            if not want or want == self._MAP_ALL:
                continue
            names = [n for n in names if self._map_segment(n, index) == want]
        return names

    # ---- the two columns ----
    def _map_build(self):
        left_names = self._map_names_for("source")
        right_names = self._map_names_for("target")
        total = len(self._all_items)
        self.map_left_count_tb.Text = "{0}/{1}".format(len(left_names), total)
        self.map_right_count_tb.Text = "{0}/{1}".format(len(right_names), total)
        self.map_count_tb.Text = "{0} file(s) scanned".format(total)

        self.map_left_panel.Children.Clear()
        self.map_right_panel.Children.Clear()
        self._map_left_rows = {}
        self._map_right_rows = {}

        for name in left_names:
            left = self._map_make_row(name, "source")
            self.map_left_panel.Children.Add(left)
            self._map_left_rows[name] = left

        for name in right_names:
            right = self._map_make_row(name, "target")
            self.map_right_panel.Children.Add(right)
            self._map_right_rows[name] = right

        # An armed source that has just been filtered out would leave the
        # tool waiting for a second click that can never come.
        if self._map_pending and self._map_pending not in self._map_left_rows:
            self._map_pending = None
        self._map_update_hint()
        # Rows have no size until WPF lays them out, and the wires are
        # positioned FROM that size - so let layout run first.
        self._pump()
        self._map_draw_wires()

    def _map_make_row(self, name, side):
        colour = self._map_colour_for(name)

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
        row.BorderThickness = Thickness(0, 0, 4, 0) if side == "source" \
            else Thickness(4, 0, 0, 0)
        row.BorderBrush = colour
        row.Child = text
        row.Cursor = Cursors.Hand
        row.ToolTip = name
        row.Tag = "{0}|{1}".format(side, name)
        row.MouseLeftButtonDown += self._map_row_click
        return row

    def _map_colour_for(self, name):
        """Discipline colour from the same configured code list the
        DeeLinkMAP map uses, so a file looks the same in both."""
        try:
            _code, label = lms.detect_discipline(name, lms.load_disciplines())
            if not hasattr(self, "_map_colours"):
                self._map_colours = {}
            if label not in self._map_colours:
                used = len(self._map_colours)
                hexed = (lms._UNKNOWN_COLOR if label == lms.UNKNOWN_LABEL
                         else lms._PALETTE[used % len(lms._PALETTE)])
                self._map_colours[label] = SolidColorBrush(Color.FromRgb(
                    int(hexed[1:3], 16), int(hexed[3:5], 16), int(hexed[5:7], 16)))
            return self._map_colours[label]
        except Exception:
            return SolidColorBrush(Color.FromRgb(0x88, 0x88, 0x88))

    # ---- click, then click ----
    def _map_row_click(self, sender, args):
        def run():
            side, name = str(sender.Tag).split("|", 1)
            if side == "source":
                # Clicking another source just moves the arming, rather
                # than refusing - changing your mind is not an error.
                self._map_pending = None if self._map_pending == name else name
            else:
                if not self._map_pending:
                    self._map_update_hint(
                        "Pick a SOURCE on the left first, then this target.")
                    return
                source = self._map_pending
                if source == name:
                    self._map_update_hint(
                        "A file cannot be linked into itself.")
                    return
                pair = (source, name)
                if pair in self._matches:
                    self._map_update_hint(
                        "Already linked: {0} into {1}".format(source, name))
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
        for name, row in getattr(self, "_map_left_rows", {}).items():
            row.Background = (self._ROW_ARMED if name == self._map_pending
                              else self._ROW_BG)

    def _map_update_hint(self, message=None):
        if message:
            self.map_hint_tb.Text = message
            return
        if self._map_pending:
            self.map_hint_tb.Text = (
                "Linking FROM  {0}   —  now click a TARGET on the right. "
                "Click it again to cancel.".format(self._map_pending))
        else:
            self.map_hint_tb.Text = (
                "Click a file on the left, then a file on the right, to link "
                "the first into the second.  Click a wire to remove it.")

    # ---- wires ----
    def _map_row_y(self, row):
        """Where this row sits in the middle canvas's own coordinates.

        TranslatePoint does the work: it accounts for how far the column
        has been scrolled, so a wire follows its row instead of needing
        the scroll offset tracked by hand."""
        try:
            point = row.TranslatePoint(Point(0, row.ActualHeight / 2.0),
                                       self.map_canvas)
            return point.Y
        except Exception:
            return None

    def _map_draw_wires(self):
        canvas = self.map_canvas
        canvas.Children.Clear()
        self._map_wire_shapes = []

        width = canvas.ActualWidth or 200.0
        height = canvas.ActualHeight or 0.0
        if height <= 0:
            return

        for index, pair in enumerate(self._matches):
            source, target = pair
            left_row = getattr(self, "_map_left_rows", {}).get(source)
            right_row = getattr(self, "_map_right_rows", {}).get(target)
            if left_row is None or right_row is None:
                continue          # one end filtered out of view
            y1 = self._map_row_y(left_row)
            y2 = self._map_row_y(right_row)
            if y1 is None or y2 is None:
                continue
            # Both ends off the same edge means the wire is nowhere near
            # the visible strip; drawing it just adds clutter. The canvas
            # clips the rest.
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
            line.ToolTip = "{0}\nlinked into\n{1}\n\n(click to remove)".format(
                source, target)
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
                self._map_wire_shapes.append(shape)

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
        # Cheap: only the wires move, the rows are where WPF put them.
        self._guard_map(self._map_draw_wires)

    # ---- toolbar ----
    def map_left_search_changed(self, sender, args):
        if getattr(self, "_map_ready", False):
            self._guard_map(self._map_build)

    def map_right_search_changed(self, sender, args):
        if getattr(self, "_map_ready", False):
            self._guard_map(self._map_build)

    def map_left_clear_click(self, sender, args):
        # Clears that column's dropdowns too - a "Clear" that leaves
        # filters silently applied is a trap.
        self._map_clear_filters(getattr(self, "_map_left_cbs", []))
        self.map_left_search_tb.Text = ""
        if getattr(self, "_map_ready", False):
            self._guard_map(self._map_build)

    def map_right_clear_click(self, sender, args):
        self._map_clear_filters(getattr(self, "_map_right_cbs", []))
        self.map_right_search_tb.Text = ""
        if getattr(self, "_map_ready", False):
            self._guard_map(self._map_build)

    def map_refresh_click(self, sender, args):
        self._guard_map(self._map_build)

    def map_clear_wires_click(self, sender, args):
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
    def clear_progress_click(self, sender, args):
        context_key = _progress_context_key(self._source_mode(), self._project_id, self._local_folder)
        _clear_done_hosts(context_key)
        forms.alert("Cleared completed-run history for the current project/folder - the next run "
                     "will process every matched host again, regardless of past runs.")

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

        context_key = _progress_context_key(mode, self._project_id, self._local_folder)
        skip_completed = (self.skip_completed_cb.IsChecked is True)
        done_hosts = _load_done_hosts(context_key) if skip_completed else {}
        already_done_count = sum(1 for t in groups.keys() if t in done_hosts)
        if skip_completed and done_hosts:
            groups = dict((t, s) for t, s in groups.items() if t not in done_hosts)
        if not groups:
            forms.alert("Every matched host was already synchronized in a previous run of this "
                         "batch. Uncheck 'Skip hosts already synchronized' or Clear History to "
                         "redo them.")
            return

        est = dms.estimate_seconds(groups)
        analysis = dms.analysis_text(groups, est)
        resume_note = ("\n\n{0} host(s) already synchronized in a previous run are being skipped."
                        .format(already_done_count)) if already_done_count else ""

        if not forms.alert(
                "{0}{1}\n\nEach host is opened, linked, and SYNCHRONIZED back - this modifies {2} real "
                "shared model(s). If Revit crashes partway through (a huge host model can do this - "
                "see the Log tab), hosts already synchronized before the crash are safely saved; just "
                "reopen this tool and Run again to pick up where it left off.\n\nContinue?".format(
                    analysis, resume_note, len(groups)),
                title="DeeMAPLink - Confirm", yes=True, no=True):
            return

        placement = self._placement()
        attachment = self._attachment()
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
                            doc, source_name, model_path, placement, fallback,
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
                        if ok_sync:
                            # Written to disk immediately, not batched until
                            # the end of the run - see this file's own
                            # resumable-batch note above. A host that crashes
                            # Revit LATER in this same run must not erase the
                            # fact that THIS host's work is already safely
                            # synchronized.
                            _mark_host_done(context_key, target_name)
                        else:
                            self._log("  SYNC ERROR: {0}".format(sync_detail))
                            sync_failed = True
                    else:
                        results.append((None, target_name, "opened - nothing new to link"))
                        self._progress_done_one()
                        _mark_host_done(context_key, target_name)
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
