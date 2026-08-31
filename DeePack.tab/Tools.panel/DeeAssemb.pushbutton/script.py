# -*- coding: utf-8 -*-
"""
DeeAssemb
Batch-creates a full set of assembly views plus its own sheet for every
selected assembly, arranging the views on the sheet using a layout the
user designs visually. All scan / layout / create logic lives in
lib/dee_assembly_service.py; this file only wires the WPF window to it
(the same split used by DeeRoomStamp -> dee_room_stamp_service).

The layout designer works in title-block millimetres so the preview and
the real sheet agree: margins and spacing are entered in mm and applied
to the ACTUAL ViewSheet.Outline at build time, so an unusual title block
still lays out correctly even if its "Sheet Width"/"Sheet Height"
parameters are missing (those are only used to draw the preview at the
right aspect ratio).

Two habits carried over from this extension's own crash history:
  - `_ready` is a CLASS attribute, false until __init__ finishes. WPF
    fires TextChanged/SelectionChanged/SizeChanged while the XAML is
    still being loaded by the base class - i.e. before any instance
    attribute exists - so every handler returns early until the window
    is actually built.
  - AssemblyRow holds ElementIds and plain values only, never live
    Element wrappers. This window waits on the user between the scan and
    the run, which is exactly when a cached Element goes stale and takes
    Revit down with an uncatchable native error.
"""
import os
import time

from pyrevit import forms, script
import dee_branding

import clr
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")
from System import Action
from System.Windows import Visibility, Thickness
from System.Windows.Controls import Canvas, TextBlock
from System.Windows.Shapes import Rectangle
from System.Windows.Media import SolidColorBrush, Color, Brushes
from System.Windows.Threading import Dispatcher, DispatcherFrame, DispatcherPriority

import dee_assembly_service as core

output = script.get_output()

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")

# Fallback preview aspect: ISO A1 landscape. Only ever used to draw the
# preview when a title block does not expose Sheet Width/Height - the real
# build always measures the actual sheet.
_FALLBACK_SHEET_MM = (841.0, 594.0)

_PREVIEW_PAD = 12.0
_SHEET_STROKE = SolidColorBrush(Color.FromRgb(150, 150, 150))
_CELL_STROKE = SolidColorBrush(Color.FromRgb(80, 80, 80))
_VIEW_FILL = SolidColorBrush(Color.FromArgb(80, 33, 150, 243))
_VIEW_STROKE = SolidColorBrush(Color.FromRgb(33, 150, 243))
_SEL_FILL = SolidColorBrush(Color.FromArgb(110, 255, 152, 0))
_SEL_STROKE = SolidColorBrush(Color.FromRgb(255, 152, 0))
_LABEL_BRUSH = SolidColorBrush(Color.FromRgb(235, 235, 235))


def format_duration(seconds):
    seconds = int(max(0, seconds))
    if seconds < 60:
        return "{0}s".format(seconds)
    return "{0}m {1:02d}s".format(seconds // 60, seconds % 60)


def matches_search(row, query):
    """AND-of-terms over the assembly name and its type name - same rule
    DeeSuperLINK's file filter uses, so the two behave the same way."""
    if not query:
        return True
    haystack = "{0} {1}".format(row.name, row.type_name).lower()
    return all(term in haystack for term in query.lower().split())


class DeeAssembWindow(dee_branding.DeeBrandedWindow):
    # See the module docstring - must exist BEFORE the base class loads the
    # XAML, because loading it fires the handlers below.
    _ready = False

    def __init__(self, xaml_file, doc, uidoc):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.doc = doc
        self.uidoc = uidoc

        self._rows = []
        self._slots = {}            # key -> (row, col, row_span, col_span)
        self._selected_key = None
        self._rect_map = []         # (key, x, y, w, h) in canvas pixels
        self._drag_key = None
        self._sheet_mm = _FALLBACK_SHEET_MM
        self._prog_total = 1
        self._prog_done = 0
        self._prog_start = time.time()

        self._titleblocks = []
        self._model_templates = []
        self._schedule_templates = []
        self._categories = []

        with forms.ProgressBar(title="DeeAssemb - scanning assemblies...", cancellable=True) as pb:
            self._rows = core.collect_assemblies(self.doc, self._progress_cb(pb))
        with forms.ProgressBar(title="DeeAssemb - reading title blocks and templates...",
                               indeterminate=True):
            self._titleblocks = core.collect_titleblocks(self.doc)
            self._model_templates, self._schedule_templates = core.collect_view_templates(self.doc)
            self._categories = core.collect_schedulable_categories(self.doc)

        self._fill_combos()
        self._refresh_grid()

        # Sensible starting set: the three views most assembly sheets open
        # with. Checked here rather than in the XAML so the Click handler
        # never runs against a half-built window.
        for name in ("kind_3d_cb", "kind_elev_front_cb", "kind_part_list_cb"):
            try:
                getattr(self, name).IsChecked = True
            except Exception:
                pass

        self._ready = True
        self._slots = core.auto_arrange(self._checked_keys(), self._grid_rows(), self._grid_cols())
        self._update_status()
        self._redraw()

    # ---------------- per-view scale ----------------
    def _scale_for(self, key):
        """None = auto-fit, otherwise the chosen standard denominator."""
        cb = getattr(self, "scale_{0}_cb".format(key), None)
        if cb is None:
            return None
        idx = cb.SelectedIndex
        if idx <= 0 or idx > len(core.STANDARD_SCALES):
            return None
        return core.STANDARD_SCALES[idx - 1]

    def scale_all_auto_click(self, sender, args):
        for key in self._drawing_keys():
            cb = getattr(self, "scale_{0}_cb".format(key), None)
            if cb is not None:
                cb.SelectedIndex = 0

    # ---------------- sheet naming ----------------
    def _naming_rule(self, which):
        """which is "num" or "name" - the two rows of the naming grid."""
        mode_cb = getattr(self, "{0}_mode_cb".format(which))
        idx = mode_cb.SelectedIndex
        mode = (core.NAMING_MODE_LABELS[idx][1]
                if 0 <= idx < len(core.NAMING_MODE_LABELS) else core.NAMING_NONE)
        return core.NamingRule(
            prefix=(getattr(self, "{0}_prefix_tb".format(which)).Text or ""),
            suffix=(getattr(self, "{0}_suffix_tb".format(which)).Text or ""),
            mode=mode,
            start=(getattr(self, "{0}_start_tb".format(which)).Text or "1").strip(),
            step=self._int_of(getattr(self, "{0}_step_tb".format(which)), 1, -999, 999),
            pad=self._int_of(getattr(self, "{0}_pad_tb".format(which)), 0, 0, 12))

    def _preview_samples(self):
        """The first few assemblies that will actually be built, so the preview
        shows real names rather than invented ones."""
        rows = self._selected_rows() or [r for r in self._rows if r.is_ready] or self._rows
        return [(r.name, r.type_name) for r in rows[:3]]

    def _refresh_naming_preview(self):
        samples = self._preview_samples()
        if not samples:
            self.naming_preview_tb.Text = "No assemblies to preview."
            return
        numbers = self._naming_rule("num").preview(samples)
        names = self._naming_rule("name").preview(samples)
        lines = ["First {0} sheet(s) would be:".format(len(samples))]
        for i in range(len(samples)):
            lines.append("   {0}   |   {1}".format(numbers[i], names[i]))
        self.naming_preview_tb.Text = "\n".join(lines)

    def naming_changed(self, sender, args):
        if not self._ready:
            return
        self._refresh_naming_preview()

    def naming_mode_changed(self, sender, args):
        if not self._ready:
            return
        self._refresh_naming_preview()

    # ---------------- scanning helpers ----------------
    def _progress_cb(self, pb):
        def cb(i, total):
            if i % 25 == 0 or i == total - 1:
                try:
                    pb.update_progress(i, total)
                except Exception:
                    pass
            return pb.cancelled
        return cb

    def _drawing_keys(self):
        """The view kinds that have a scale. Schedules do not - Revit has no
        concept of scale for one, so they get no combo at all."""
        return [k for k, _lbl, kind in core.VIEW_KINDS if kind != "schedule"]

    def _fill_scale_combos(self):
        for key in self._drawing_keys():
            cb = getattr(self, "scale_{0}_cb".format(key), None)
            if cb is None:
                continue
            cb.Items.Clear()
            cb.Items.Add("Auto-fit")
            for s in core.STANDARD_SCALES:
                cb.Items.Add("1:{0}".format(s))
            cb.SelectedIndex = 0

    def _fill_naming_combos(self):
        for name in ("num_mode_cb", "name_mode_cb"):
            cb = getattr(self, name)
            cb.Items.Clear()
            for label, _mode in core.NAMING_MODE_LABELS:
                cb.Items.Add(label)
            cb.SelectedIndex = 0

    def _fill_combos(self):
        self._fill_scale_combos()
        self._fill_naming_combos()
        self.titleblock_cb.Items.Clear()
        for _tid, label, _w, _h in self._titleblocks:
            self.titleblock_cb.Items.Add(label)
        if self._titleblocks:
            self.titleblock_cb.SelectedIndex = 0

        self.model_template_cb.Items.Clear()
        self.model_template_cb.Items.Add("(None)")
        for _tid, label in self._model_templates:
            self.model_template_cb.Items.Add(label)
        self.model_template_cb.SelectedIndex = 0

        self.schedule_template_cb.Items.Clear()
        self.schedule_template_cb.Items.Add("(None)")
        for _tid, label in self._schedule_templates:
            self.schedule_template_cb.Items.Add(label)
        self.schedule_template_cb.SelectedIndex = 0

        self.schedule_category_cb.Items.Clear()
        for _cid, label in self._categories:
            self.schedule_category_cb.Items.Add(label)
        if self._categories:
            self.schedule_category_cb.SelectedIndex = 0

    def _refresh_grid(self):
        query = ""
        try:
            query = self.search_tb.Text or ""
        except Exception:
            pass
        shown = [r for r in self._rows if matches_search(r, query)]
        self.assemblies_grid.ItemsSource = None
        self.assemblies_grid.ItemsSource = shown
        return shown

    def _update_status(self):
        ready = len([r for r in self._rows if r.is_ready])
        skipped = len(self._rows) - ready
        picked = len(self._selected_rows())
        self.status_tb.Text = (
            "{0} assembly(ies) found - {1} ready, {2} already have views/sheets. "
            "{3} ticked to build.".format(len(self._rows), ready, skipped, picked))
        self.run_summary_tb.Text = "{0} assembly(ies) x {1} view(s) selected.".format(
            picked, len(self._checked_keys()))
        # The preview shows the assemblies actually queued, so it has to follow
        # every change to the selection.
        self._refresh_naming_preview()

    def _selected_rows(self):
        return [r for r in self._rows if r.selected and r.is_ready]

    # ---------------- tab 1 handlers ----------------
    def search_changed(self, sender, args):
        if not self._ready:
            return
        self._refresh_grid()

    def select_all_click(self, sender, args):
        for r in self._rows:
            r.selected = r.is_ready
        self._refresh_grid()
        self._update_status()

    def select_none_click(self, sender, args):
        for r in self._rows:
            r.selected = False
        self._refresh_grid()
        self._update_status()

    def select_shown_click(self, sender, args):
        query = self.search_tb.Text or ""
        for r in self._rows:
            if matches_search(r, query) and r.is_ready:
                r.selected = True
        self._refresh_grid()
        self._update_status()

    def rescan_click(self, sender, args):
        with forms.ProgressBar(title="DeeAssemb - re-scanning assemblies...", cancellable=True) as pb:
            self._rows = core.collect_assemblies(self.doc, self._progress_cb(pb))
        self._refresh_grid()
        self._update_status()

    # ---------------- tab 2 handlers ----------------
    def titleblock_changed(self, sender, args):
        if not self._ready:
            return
        self._sheet_mm = self._titleblock_mm()
        w, h = self._sheet_mm
        known = self._titleblock_size_known()
        self.titleblock_size_tb.Text = (
            "{0:.0f} x {1:.0f} mm".format(w, h) if known else
            "size not published by this title block - preview assumes {0:.0f} x {1:.0f} mm "
            "(the real sheet is measured at build time)".format(w, h))
        self._redraw()

    def _titleblock_size_known(self):
        idx = self.titleblock_cb.SelectedIndex
        if idx < 0 or idx >= len(self._titleblocks):
            return False
        _tid, _label, w, h = self._titleblocks[idx]
        return bool(w and h and w > 1 and h > 1)

    def _titleblock_mm(self):
        idx = self.titleblock_cb.SelectedIndex
        if idx < 0 or idx >= len(self._titleblocks):
            return _FALLBACK_SHEET_MM
        _tid, _label, w, h = self._titleblocks[idx]
        if w and h and w > 1 and h > 1:
            return (float(w), float(h))
        return _FALLBACK_SHEET_MM

    def view_kind_changed(self, sender, args):
        if not self._ready:
            return
        keys = self._checked_keys()
        self._slots = core.normalize_slots(self._slots, keys,
                                           self._grid_rows(), self._grid_cols())
        if self._selected_key not in keys:
            self._selected_key = None
        self._update_status()
        self._redraw()

    def _checked_keys(self):
        keys = []
        for key, _label, _kind in core.VIEW_KINDS:
            cb = getattr(self, "kind_{0}_cb".format(key), None)
            if cb is not None and cb.IsChecked is True:
                keys.append(key)
        return keys

    # ---------------- tab 3: layout ----------------
    def _int_of(self, textbox, default, minimum=1, maximum=20):
        try:
            v = int(str(textbox.Text).strip())
        except Exception:
            return default
        return max(minimum, min(maximum, v))

    def _float_of(self, textbox, default):
        try:
            return max(0.0, float(str(textbox.Text).strip()))
        except Exception:
            return default

    def _grid_rows(self):
        return self._int_of(self.rows_tb, 2)

    def _grid_cols(self):
        return self._int_of(self.cols_tb, 2)

    def layout_param_changed(self, sender, args):
        if not self._ready:
            return
        self._slots = core.normalize_slots(self._slots, self._checked_keys(),
                                           self._grid_rows(), self._grid_cols())
        self._redraw()

    def auto_arrange_click(self, sender, args):
        self._slots = core.auto_arrange(self._checked_keys(),
                                        self._grid_rows(), self._grid_cols())
        self._slots = core.normalize_slots(self._slots, self._checked_keys(),
                                           self._grid_rows(), self._grid_cols())
        self._redraw()

    def span_changed(self, sender, args):
        if not self._ready or self._selected_key is None:
            return
        slot = self._slots.get(self._selected_key)
        if slot is None:
            return
        rs = self._int_of(self.row_span_tb, 1, 1, 20)
        cs = self._int_of(self.col_span_tb, 1, 1, 20)
        self._slots[self._selected_key] = (slot[0], slot[1], rs, cs)
        self._slots = core.normalize_slots(self._slots, self._checked_keys(),
                                           self._grid_rows(), self._grid_cols())
        self._redraw()

    def _nudge(self, d_row, d_col):
        if self._selected_key is None:
            return
        slot = self._slots.get(self._selected_key)
        if slot is None:
            return
        r = max(0, min(self._grid_rows() - 1, slot[0] + d_row))
        c = max(0, min(self._grid_cols() - 1, slot[1] + d_col))
        self._move_to(self._selected_key, r, c)

    def move_up_click(self, sender, args):
        self._nudge(-1, 0)

    def move_down_click(self, sender, args):
        self._nudge(1, 0)

    def move_left_click(self, sender, args):
        self._nudge(0, -1)

    def move_right_click(self, sender, args):
        self._nudge(0, 1)

    def _move_to(self, key, row, col):
        """Moves `key` to (row, col). If another view already starts there the
        two swap, so a drop never silently stacks two views on one cell."""
        slot = self._slots.get(key)
        if slot is None:
            return
        other = None
        for k, s in self._slots.items():
            if k != key and s[0] == row and s[1] == col:
                other = k
                break
        self._slots[key] = (row, col, slot[2], slot[3])
        if other is not None:
            o = self._slots[other]
            self._slots[other] = (slot[0], slot[1], o[2], o[3])
        self._slots = core.normalize_slots(self._slots, self._checked_keys(),
                                           self._grid_rows(), self._grid_cols())
        self._redraw()

    # -- canvas <-> sheet-mm mapping --
    def _preview_geometry(self):
        """Returns (origin_x_px, origin_y_px, px_per_mm, sheet_w_mm, sheet_h_mm)
        for the sheet rectangle centred in the canvas."""
        cw = self.layout_canvas.ActualWidth
        ch = self.layout_canvas.ActualHeight
        if cw < 20 or ch < 20:
            cw, ch = 600.0, 400.0
        sw, sh = self._sheet_mm
        scale = min((cw - 2 * _PREVIEW_PAD) / sw, (ch - 2 * _PREVIEW_PAD) / sh)
        scale = max(scale, 0.01)
        ox = (cw - sw * scale) / 2.0
        oy = (ch - sh * scale) / 2.0
        return ox, oy, scale, sw, sh

    def _sheet_rect_to_canvas(self, bounds, geom):
        """bounds is (min_x, max_x, min_y, max_y) in sheet mm, Y up.
        Canvas Y grows downward, so the top edge comes from max_y."""
        ox, oy, scale, _sw, sh = geom
        x0, x1, y0, y1 = bounds
        px = ox + x0 * scale
        py = oy + (sh - y1) * scale
        return px, py, max((x1 - x0) * scale, 1.0), max((y1 - y0) * scale, 1.0)

    def _canvas_to_sheet(self, px, py, geom):
        ox, oy, scale, _sw, sh = geom
        return ((px - ox) / scale, sh - (py - oy) / scale)

    def _current_cells(self):
        """Cells in sheet-mm space. compute_cells is unit-agnostic (bounds and
        margins just have to agree), so the preview can use mm directly while
        the build uses feet."""
        sw, sh = self._sheet_mm
        return core.compute_cells(
            (0.0, sw, 0.0, sh), self._grid_rows(), self._grid_cols(),
            self._float_of(self.margin_tb, 10.0),
            self._float_of(self.spacing_h_tb, 5.0),
            self._float_of(self.spacing_v_tb, 5.0))

    def _redraw(self):
        if not self._ready:
            return
        canvas = self.layout_canvas
        canvas.Children.Clear()
        self._rect_map = []

        geom = self._preview_geometry()
        ox, oy, scale, sw, sh = geom

        sheet = Rectangle()
        sheet.Width = max(sw * scale, 1.0)
        sheet.Height = max(sh * scale, 1.0)
        sheet.Stroke = _SHEET_STROKE
        sheet.StrokeThickness = 1.5
        sheet.Fill = Brushes.Transparent
        Canvas.SetLeft(sheet, ox)
        Canvas.SetTop(sheet, oy)
        canvas.Children.Add(sheet)

        cells = self._current_cells()
        if not cells:
            self.layout_warning_tb.Text = (
                "The margin and spacing leave no room for a {0} x {1} grid on a "
                "{2:.0f} x {3:.0f} mm sheet. Reduce them, or use fewer rows/columns.".format(
                    self._grid_rows(), self._grid_cols(), sw, sh))
            return
        self.layout_warning_tb.Text = ""

        for row in cells:
            for cell in row:
                x, y, w, h = self._sheet_rect_to_canvas(cell, geom)
                r = Rectangle()
                r.Width = w
                r.Height = h
                r.Stroke = _CELL_STROKE
                r.StrokeThickness = 1.0
                r.StrokeDashArray.Add(3.0)
                r.StrokeDashArray.Add(3.0)
                r.Fill = Brushes.Transparent
                Canvas.SetLeft(r, x)
                Canvas.SetTop(r, y)
                canvas.Children.Add(r)

        for key in self._checked_keys():
            slot = self._slots.get(key)
            if slot is None:
                continue
            bounds = core.slot_bounds(cells, slot[0], slot[1], slot[2], slot[3])
            if bounds is None:
                continue
            x, y, w, h = self._sheet_rect_to_canvas(bounds, geom)
            selected = (key == self._selected_key)
            r = Rectangle()
            r.Width = w
            r.Height = h
            r.Fill = _SEL_FILL if selected else _VIEW_FILL
            r.Stroke = _SEL_STROKE if selected else _VIEW_STROKE
            r.StrokeThickness = 2.5 if selected else 1.5
            Canvas.SetLeft(r, x)
            Canvas.SetTop(r, y)
            canvas.Children.Add(r)

            label = TextBlock()
            label.Text = core.VIEW_KIND_LABELS.get(key, key)
            label.Foreground = _LABEL_BRUSH
            label.FontSize = 11
            label.Margin = Thickness(4, 2, 0, 0)
            label.MaxWidth = max(w - 6, 10)
            Canvas.SetLeft(label, x + 3)
            Canvas.SetTop(label, y + 3)
            canvas.Children.Add(label)

            self._rect_map.append((key, x, y, w, h))

        self._refresh_selected_panel()

    def _refresh_selected_panel(self):
        if self._selected_key is None:
            self.selected_view_tb.Text = "Nothing selected. Click a view in the preview."
            return
        slot = self._slots.get(self._selected_key)
        label = core.VIEW_KIND_LABELS.get(self._selected_key, self._selected_key)
        if slot is None:
            self.selected_view_tb.Text = label
            return
        self.selected_view_tb.Text = "{0}\nrow {1}, column {2}".format(
            label, slot[0] + 1, slot[1] + 1)
        self._ready = False          # do not let these writes re-enter span_changed
        try:
            self.row_span_tb.Text = str(slot[2])
            self.col_span_tb.Text = str(slot[3])
        finally:
            self._ready = True

    def _hit_test(self, px, py):
        # Reversed so the most recently drawn (topmost) rectangle wins.
        for key, x, y, w, h in reversed(self._rect_map):
            if x <= px <= x + w and y <= py <= y + h:
                return key
        return None

    def canvas_mouse_down(self, sender, args):
        if not self._ready:
            return
        pt = args.GetPosition(self.layout_canvas)
        key = self._hit_test(pt.X, pt.Y)
        self._selected_key = key
        self._drag_key = key
        if key is not None:
            try:
                self.layout_canvas.CaptureMouse()
            except Exception:
                pass
        self._redraw()

    def canvas_mouse_move(self, sender, args):
        # Position is resolved on mouse-up; this exists so the drag reads as a
        # drag to WPF and the cursor stays captured over the canvas.
        return

    def canvas_mouse_up(self, sender, args):
        if not self._ready or self._drag_key is None:
            self._drag_key = None
            return
        try:
            self.layout_canvas.ReleaseMouseCapture()
        except Exception:
            pass
        pt = args.GetPosition(self.layout_canvas)
        geom = self._preview_geometry()
        sx, sy = self._canvas_to_sheet(pt.X, pt.Y, geom)
        cells = self._current_cells()
        target = core.cell_at_point(cells, sx, sy) if cells else None
        key = self._drag_key
        self._drag_key = None
        if target is not None:
            self._move_to(key, target[0], target[1])
        else:
            self._redraw()

    def canvas_size_changed(self, sender, args):
        if not self._ready:
            return
        self._redraw()

    # ---------------- in-window progress bar ----------------
    #
    # A modal WPF window does not repaint while a long synchronous loop runs
    # on the UI thread, so the bar would only appear once the work had already
    # finished. _pump() drains pending render work at Background priority (the
    # standard WPF "DoEvents") after each update, which is what makes the bar
    # animate. Same implementation as DeeSuperLINK's, which is confirmed
    # working live.
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
                eta = "ETA {0}".format(format_duration(remaining))
            else:
                eta = "ETA --"
            self.progress_bar.Value = done
            self.progress_text_tb.Text = "{0}   |   {1} of {2}   |   elapsed {3}   |   {4}".format(
                label, min(done + 1, self._prog_total), self._prog_total,
                format_duration(elapsed), eta)
        except Exception:
            pass
        self._pump()

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

    def _log(self, line):
        try:
            self.log_tb.AppendText(line + "\r\n")
            self.log_tb.ScrollToEnd()
        except Exception:
            pass
        self._pump()

    # ---------------- run ----------------
    def _build_options(self):
        opts = core.BuildOptions()
        opts.view_keys = self._checked_keys()
        opts.rows = self._grid_rows()
        opts.cols = self._grid_cols()
        opts.slots = core.normalize_slots(self._slots, opts.view_keys, opts.rows, opts.cols)
        opts.margin_mm = self._float_of(self.margin_tb, 10.0)
        opts.spacing_h_mm = self._float_of(self.spacing_h_tb, 5.0)
        opts.spacing_v_mm = self._float_of(self.spacing_v_tb, 5.0)

        idx = self.titleblock_cb.SelectedIndex
        opts.titleblock_id = self._titleblocks[idx][0] if 0 <= idx < len(self._titleblocks) else None

        idx = self.model_template_cb.SelectedIndex
        opts.model_template_id = (self._model_templates[idx - 1][0]
                                  if 0 < idx <= len(self._model_templates) else None)
        idx = self.schedule_template_cb.SelectedIndex
        opts.schedule_template_id = (self._schedule_templates[idx - 1][0]
                                     if 0 < idx <= len(self._schedule_templates) else None)
        opts.assign_template = self.assign_template_cb.IsChecked is True

        idx = self.schedule_category_cb.SelectedIndex
        opts.schedule_category_id = (self._categories[idx][0]
                                     if 0 <= idx < len(self._categories) else None)

        opts.sheet_number_rule = self._naming_rule("num")
        opts.sheet_name_rule = self._naming_rule("name")
        opts.scales = dict((key, self._scale_for(key)) for key in opts.view_keys)
        return opts

    def run_click(self, sender, args):
        targets = self._selected_rows()
        if not targets:
            forms.alert("Tick at least one assembly on the Assemblies tab.\n\n"
                        "Assemblies marked Skip already have views or a sheet and "
                        "cannot be ticked.", title="DeeAssemb")
            return
        keys = self._checked_keys()
        if not keys:
            forms.alert("Tick at least one view type on the Views to Create tab.",
                        title="DeeAssemb")
            return
        opts = self._build_options()
        if opts.titleblock_id is None:
            forms.alert("Pick a title block on the Views to Create tab - every "
                        "assembly sheet needs one.", title="DeeAssemb")
            return
        if "cat_schedule" in keys and opts.schedule_category_id is None:
            forms.alert("Single-Category Schedule is ticked but no category is "
                        "chosen for it.", title="DeeAssemb")
            return
        if not self._current_cells():
            forms.alert("The margin and spacing leave no room for the grid on this "
                        "sheet. Fix the layout on the Sheet Layout tab first.",
                        title="DeeAssemb")
            return

        proceed = forms.alert(
            "Create {0} view(s) and one sheet for each of {1} assembly(ies)?\n\n"
            "That is {2} new view(s) and {1} new sheet(s) in this model.\n\n"
            "Assemblies that already have views or a sheet are not touched.".format(
                len(keys), len(targets), len(keys) * len(targets)),
            title="DeeAssemb", yes=True, no=True)
        if not proceed:
            return

        self.main_tabs.SelectedIndex = 3
        self._log("=" * 70)
        self._log("DeeAssemb run - {0} assembly(ies), {1} view(s) each".format(
            len(targets), len(keys)))
        self._log("=" * 70)

        results = []
        self._progress_begin(len(targets))
        try:
            for i, row in enumerate(targets, start=1):
                self._progress_render(row.name)
                result = core.build_for_assembly(self.doc, row, opts, i)
                results.append(result)
                if result.ok:
                    self._log("[{0}/{1}] {2}  ->  sheet '{3}', {4} view(s) placed".format(
                        i, len(targets), row.name, result.sheet_label, len(result.created)))
                    for label, detail in result.created:
                        self._log("           {0}{1}".format(
                            label, "  ({0})".format(detail) if detail else ""))
                else:
                    self._log("[{0}/{1}] {2}  ->  FAILED: {3}".format(
                        i, len(targets), row.name, result.error))
                for w in result.warnings:
                    self._log("           ! {0}".format(w))
                self._progress_done_one()
        finally:
            self._progress_end()

        ok = len([r for r in results if r.ok])
        failed = len(results) - ok
        self._log("-" * 70)
        self._log("Done. {0} built, {1} failed.".format(ok, failed))

        core.print_report(results, output)

        with forms.ProgressBar(title="DeeAssemb - refreshing assembly list...",
                               indeterminate=True):
            self._rows = core.collect_assemblies(self.doc)
        self._refresh_grid()
        self._update_status()
        self.status_tb.Text = ("Run finished: {0} assembly(ies) built, {1} failed. "
                               "Full report is in the pyRevit output window.".format(ok, failed))

    def close_click(self, sender, args):
        self.Close()


uiapp = __revit__
if uiapp.ActiveUIDocument is None:
    forms.alert("Open a Revit project first.", title="DeeAssemb")
else:
    _doc = uiapp.ActiveUIDocument.Document
    window = DeeAssembWindow(_XAML_FILE, _doc, uiapp.ActiveUIDocument)
    if not window._rows:
        forms.alert("This model has no assemblies.\n\n"
                    "Create an assembly in Revit first (select elements, then "
                    "Modify > Create Assembly), then run DeeAssemb.",
                    title="DeeAssemb")
    else:
        window.ShowDialog()
