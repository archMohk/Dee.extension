# -*- coding: utf-8 -*-
"""
DeeGrid
Same UI approach as DeeLevels, applied to Revit Grids instead of Levels
- no "structural" companion feature here, just detecting and managing
the current Grids in two independent tables plus a live 2D previewer:

  - Grids are split by orientation: a straight Grid line running mostly
    along Y (i.e. drawn "vertical" on a plan) is a Vertical grid,
    positioned by its X coordinate; one running mostly along X is a
    Horizontal grid, positioned by its Y coordinate - the familiar
    numbered/lettered grid convention. Only straight (Line-based) grids
    are supported; any arc grids found are skipped with a warning
    (no clean single "position" for these).
  - Each table (Vertical Grids, Horizontal Grids) works exactly like
    DeeLevels' single table: Grid Name, Position, and Spacing (from the
    previous grid in the SAME direction, measured from 0 for the
    first one) are all editable - editing either Position or Spacing
    recomputes the other, and each table independently re-sorts/
    recalculates in position order.
  - The previewer draws an actual top-down grid pattern to scale:
    Vertical grids as vertical lines, Horizontal grids as horizontal
    lines. Selecting a row highlights its line. Dragging a line moves
    that grid live (left/right for Vertical, up/down for Horizontal);
    the tables update once the drag is released.
  - Add Grid appends a new, not-yet-created row (spanning the same
    extent as the existing grids, or a default span if there are
    none yet). Delete marks an existing grid for deletion (toggle to
    undo) or drops an unsaved new row outright.
  - Nothing touches the Revit model until "Apply Changes to Model" is
    clicked, which creates/renames/moves/deletes the real Grid
    elements in one Transaction and reports per-grid results. Moving
    an existing grid uses ElementTransformUtils.MoveElement (Grid has
    no simple "position" parameter the way Level has LEVEL_ELEV).

Position/Spacing are shown/edited as plain decimal numbers in the
project's current length display unit (shown in the column headers) -
not architectural feet-inches-fraction text.
"""
import os
import csv
import System
from pyrevit import forms, script
from Autodesk.Revit.DB import (
    FilteredElementCollector, Grid, Line, Transaction, BuiltInParameter,
    ElementTransformUtils, UnitUtils, SpecTypeId, UnitTypeId, XYZ
)
from System.Windows.Shapes import Line as WpfLine, Rectangle
from System.Windows.Controls import Canvas, TextBlock
from System.Windows.Media import Brushes, SolidColorBrush, Color
from System.Windows.Input import Cursors, MouseButtonState
from System.Windows import FontWeights

import clr
clr.AddReference("System.Windows.Forms")
from System.Windows.Forms import SaveFileDialog, DialogResult, MessageBox

output = script.get_output()

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")

_DEFAULT_GRID_SPACING_METERS = 6.0
_DEFAULT_EXTENT_MARGIN_METERS = 2.0
_DEFAULT_EXTENT_HALF_METERS = 10.0
_PREVIEW_MARGIN = 30.0

_NORMAL_BRUSH = SolidColorBrush(Color.FromRgb(70, 130, 180))
_SELECTED_BRUSH = SolidColorBrush(Color.FromRgb(220, 20, 60))
_DIM_BRUSH = SolidColorBrush(Color.FromRgb(0, 128, 128))

_UNIT_ABBR = [
    (UnitTypeId.Millimeters, "mm"),
    (UnitTypeId.Centimeters, "cm"),
    (UnitTypeId.Meters, "m"),
    (UnitTypeId.Feet, "ft"),
    (UnitTypeId.FeetFractionalInches, "ft"),
    (UnitTypeId.Inches, "in"),
    (UnitTypeId.FractionalInches, "in"),
]


def _read_grid_name(grid):
    """Element.Name has thrown a bare, unhelpful exception on some
    element types in this Revit/IronPython combination before (Level,
    ViewFamilyType, FamilySymbol) - applying the same Parameter
    fallback here defensively in case Grid is affected too."""
    try:
        return grid.Name
    except Exception:
        pass
    try:
        p = grid.get_Parameter(BuiltInParameter.DATUM_TEXT)
        if p is not None:
            val = p.AsString()
            if val:
                return val
    except Exception:
        pass
    return None


def _length_unit_type_id(doc):
    return doc.GetUnits().GetFormatOptions(SpecTypeId.Length).GetUnitTypeId()


def _unit_abbreviation(doc):
    unit_type_id = _length_unit_type_id(doc)
    for k, v in _UNIT_ABBR:
        if unit_type_id == k:
            return v
    return ""


def _internal_to_display(doc, value_internal):
    return UnitUtils.ConvertFromInternalUnits(value_internal, _length_unit_type_id(doc))


def _display_to_internal(doc, value_display):
    return UnitUtils.ConvertToInternalUnits(value_display, _length_unit_type_id(doc))


_NAMING_PATTERNS = [
    "Numeric (1, 2, 3...)",
    "Zero-Padded Numeric (01, 02...)",
    "Alphabetic (A, B, C...)",
    "Double Letter (AA, BB, CC...)",
]


def _excel_letters(i):
    """0-based index -> Excel-style column letters: 0->A, 25->Z, 26->AA..."""
    result = ""
    i += 1
    while i > 0:
        i, rem = divmod(i - 1, 26)
        result = chr(ord('A') + rem) + result
    return result


def _naming_pattern_label(i, pattern, total_count):
    if pattern.startswith("Numeric"):
        return str(i + 1)
    if pattern.startswith("Zero-Padded"):
        width = max(2, len(str(total_count)))
        return str(i + 1).zfill(width)
    if pattern.startswith("Alphabetic"):
        return _excel_letters(i)
    if pattern.startswith("Double Letter"):
        if i < 26:
            letter = chr(ord('A') + i)
            return letter + letter
        return "{0}{1}".format(_excel_letters(i), i + 1)
    return str(i + 1)


def _classify_grid(grid):
    """Returns (direction, position, other_min, other_max) for a
    straight Grid, or (None, None, None, None) if it isn't a straight
    (Line-based) grid. `position` is the coordinate that matters for
    this grid's direction (X for Vertical, Y for Horizontal);
    `other_min`/`other_max` is its extent along the other axis."""
    try:
        curve = grid.Curve
    except Exception:
        return None, None, None, None
    if not isinstance(curve, Line):
        return None, None, None, None
    p0 = curve.GetEndPoint(0)
    p1 = curve.GetEndPoint(1)
    dx = p1.X - p0.X
    dy = p1.Y - p0.Y
    if abs(dy) >= abs(dx):
        return "Vertical", (p0.X + p1.X) / 2.0, min(p0.Y, p1.Y), max(p0.Y, p1.Y)
    else:
        return "Horizontal", (p0.Y + p1.Y) / 2.0, min(p0.X, p1.X), max(p0.X, p1.X)


class GridRow(object):
    """One row in a table - either a real Grid (existing_id set) or a
    not-yet-created one (existing_id is None). `other_min`/`other_max`
    is this grid's extent along the axis perpendicular to `direction`,
    needed to build/redraw its actual line."""

    def __init__(self, doc, name, position_internal, direction, existing_id=None,
                 other_min=None, other_max=None):
        self.doc = doc
        self.name = name
        self.position_internal = position_internal
        self.direction = direction
        self.existing_id = existing_id
        self.other_min = other_min
        self.other_max = other_max
        self.pending_delete = False
        self.sequence = 0
        self.spacing_internal = 0.0
        self._prev_position_internal = 0.0
        self._original_name = name
        self._original_position_internal = position_internal

    @property
    def sequence_text(self):
        return str(self.sequence)

    @property
    def position_text(self):
        return "{0:.3f}".format(_internal_to_display(self.doc, self.position_internal))

    @position_text.setter
    def position_text(self, value):
        try:
            display_val = float(value)
        except (ValueError, TypeError):
            return
        self.position_internal = _display_to_internal(self.doc, display_val)

    @property
    def spacing_text(self):
        return "{0:.3f}".format(_internal_to_display(self.doc, self.spacing_internal))

    @spacing_text.setter
    def spacing_text(self, value):
        try:
            display_val = float(value)
        except (ValueError, TypeError):
            return
        self.position_internal = self._prev_position_internal + _display_to_internal(
            self.doc, display_val)

    @property
    def status_text(self):
        if self.pending_delete:
            return "Pending Delete"
        if self.existing_id is None:
            return "New"
        if (self.name != self._original_name or
                abs(self.position_internal - self._original_position_internal) > 1e-9):
            return "Modified"
        return "Existing"


class DeeGridWindow(forms.WPFWindow):
    def __init__(self, xaml_file, doc):
        forms.WPFWindow.__init__(self, xaml_file)
        self.doc = doc
        self._v_rows = []
        self._h_rows = []
        self._preview_elements = {}
        self._drag_row = None
        self._preview_scale = 1.0
        self._preview_min_x = 0.0
        self._preview_max_y = 0.0
        self._preview_origin_px = 0.0
        self._preview_origin_py = 0.0
        self._preview_width = 400.0
        self._preview_height = 400.0

        unit_abbr = _unit_abbreviation(doc)
        self.v_grid.Columns[2].Header = "Position ({0})".format(unit_abbr)
        self.v_grid.Columns[3].Header = "Spacing ({0})".format(unit_abbr)
        self.h_grid.Columns[2].Header = "Position ({0})".format(unit_abbr)
        self.h_grid.Columns[3].Header = "Spacing ({0})".format(unit_abbr)

        self.rename_pattern_cb.ItemsSource = _NAMING_PATTERNS
        self.rename_pattern_cb.SelectedIndex = 0

        self._load_from_model(confirm=False)

    # -- scan / recompute / redraw -------------------------------------------
    def _has_pending_changes(self):
        rows = self._v_rows + self._h_rows
        return any(
            r.pending_delete or r.existing_id is None or
            r.name != r._original_name or
            abs(r.position_internal - r._original_position_internal) > 1e-9
            for r in rows)

    def _load_from_model(self, confirm=True):
        if confirm and self._has_pending_changes():
            if not forms.alert(
                    "Discard unsaved changes and re-scan grids from the model?",
                    title="DeeGrid", yes=True, no=True):
                return
        grids = list(FilteredElementCollector(self.doc).OfClass(Grid))
        self._v_rows = []
        self._h_rows = []
        skipped = 0
        for g in grids:
            direction, position, other_min, other_max = _classify_grid(g)
            if direction is None:
                skipped += 1
                continue
            row = GridRow(self.doc, _read_grid_name(g) or "(unnamed)", position, direction,
                          existing_id=g.Id, other_min=other_min, other_max=other_max)
            if direction == "Vertical":
                self._v_rows.append(row)
            else:
                self._h_rows.append(row)
        if skipped:
            forms.alert(
                "{0} grid(s) are not straight (Line-based) and were skipped - "
                "arc grids aren't supported by this tool.".format(skipped),
                title="DeeGrid")
        self._recompute_refresh_and_redraw()

    def _recompute_one(self, rows):
        rows.sort(key=lambda r: r.position_internal)
        prev = None
        for i, row in enumerate(rows):
            row.sequence = i + 1
            row._prev_position_internal = prev if prev is not None else 0.0
            row.spacing_internal = row.position_internal - row._prev_position_internal
            prev = row.position_internal

    def _recompute(self):
        self._recompute_one(self._v_rows)
        self._recompute_one(self._h_rows)

    def _refresh_grids_ui(self):
        v_selected = self.v_grid.SelectedItem
        self.v_grid.ItemsSource = None
        self.v_grid.ItemsSource = list(self._v_rows)
        if v_selected in self._v_rows:
            self.v_grid.SelectedItem = v_selected

        h_selected = self.h_grid.SelectedItem
        self.h_grid.ItemsSource = None
        self.h_grid.ItemsSource = list(self._h_rows)
        if h_selected in self._h_rows:
            self.h_grid.SelectedItem = h_selected

    def _recompute_refresh_and_redraw(self):
        self._recompute()
        self._refresh_grids_ui()
        self._rebuild_previewer()

    # -- previewer: 2D position <-> pixel mapping (uniform scale, so the
    # grid pattern isn't stretched out of proportion) ------------------------
    def _rebuild_previewer(self):
        canvas = self.preview_canvas
        canvas.Children.Clear()
        self._preview_elements = {}
        self._drag_row = None

        all_rows = self._v_rows + self._h_rows
        if not all_rows:
            return

        width = canvas.ActualWidth if canvas.ActualWidth > 1 else 400.0
        height = canvas.ActualHeight if canvas.ActualHeight > 1 else 400.0
        self._preview_width = width
        self._preview_height = height

        margin_internal = UnitUtils.ConvertToInternalUnits(
            _DEFAULT_EXTENT_MARGIN_METERS, UnitTypeId.Meters)
        half_default = UnitUtils.ConvertToInternalUnits(
            _DEFAULT_EXTENT_HALF_METERS, UnitTypeId.Meters)

        v_positions = [r.position_internal for r in self._v_rows]
        h_positions = [r.position_internal for r in self._h_rows]
        min_x = min(v_positions) if v_positions else -half_default
        max_x = max(v_positions) if v_positions else half_default
        min_y = min(h_positions) if h_positions else -half_default
        max_y = max(h_positions) if h_positions else half_default
        if max_x - min_x < 1e-6:
            min_x -= half_default
            max_x += half_default
        if max_y - min_y < 1e-6:
            min_y -= half_default
            max_y += half_default
        min_x -= margin_internal
        max_x += margin_internal
        min_y -= margin_internal
        max_y += margin_internal

        range_x = max_x - min_x
        range_y = max_y - min_y
        usable_w = width - 2 * _PREVIEW_MARGIN
        usable_h = height - 2 * _PREVIEW_MARGIN
        scale = min(usable_w / range_x, usable_h / range_y) if range_x > 0 and range_y > 0 else 1.0
        if scale <= 0:
            scale = 1.0

        drawn_w = range_x * scale
        drawn_h = range_y * scale

        self._preview_scale = scale
        self._preview_min_x = min_x
        self._preview_max_y = max_y
        self._preview_origin_px = _PREVIEW_MARGIN + (usable_w - drawn_w) / 2.0
        self._preview_origin_py = _PREVIEW_MARGIN + (usable_h - drawn_h) / 2.0

        selected = self.v_grid.SelectedItem or self.h_grid.SelectedItem

        for row in all_rows:
            is_sel = row is selected
            brush = _SELECTED_BRUSH if is_sel else _NORMAL_BRUSH
            self._draw_grid_row(canvas, row, brush, is_sel)

        # Spacing dimensions between adjacent grids (added last, so they
        # sit on top of the grid-line hit-rectangles and get click
        # priority) - click one to type a new spacing directly.
        self._draw_spacing_dimensions(canvas, self._v_rows)
        self._draw_spacing_dimensions(canvas, self._h_rows)

    def _draw_grid_row(self, canvas, row, brush, is_sel):
        line = WpfLine()
        line.Stroke = brush
        line.StrokeThickness = 3 if is_sel else 1.5

        label = TextBlock()
        label.Foreground = brush
        label.FontSize = 11
        label.Text = "{0}  {1}".format(row.name, row.position_text)

        hit = Rectangle()
        hit.Fill = Brushes.Transparent
        hit.Tag = row

        if row.direction == "Vertical":
            px = self._x_to_px(row.position_internal)
            line.X1 = px
            line.Y1 = 0
            line.X2 = px
            line.Y2 = self._preview_height
            Canvas.SetLeft(label, px + 2)
            Canvas.SetTop(label, 4)
            hit.Width = 12
            hit.Height = self._preview_height
            hit.Cursor = Cursors.SizeWE
            Canvas.SetLeft(hit, px - 6)
            Canvas.SetTop(hit, 0)
        else:
            py = self._y_to_py(row.position_internal)
            line.X1 = 0
            line.Y1 = py
            line.X2 = self._preview_width
            line.Y2 = py
            Canvas.SetLeft(label, 4)
            Canvas.SetTop(label, py - 15)
            hit.Width = self._preview_width
            hit.Height = 12
            hit.Cursor = Cursors.SizeNS
            Canvas.SetLeft(hit, 0)
            Canvas.SetTop(hit, py - 6)

        canvas.Children.Add(line)
        canvas.Children.Add(label)
        hit.MouseLeftButtonDown += self._preview_mouse_down
        hit.MouseMove += self._preview_mouse_move
        hit.MouseLeftButtonUp += self._preview_mouse_up
        canvas.Children.Add(hit)

        self._preview_elements[row] = (line, label, hit)

    def _draw_spacing_dimensions(self, canvas, rows):
        """One clickable label per adjacent pair, showing the spacing
        between them - Vertical spacings sit in a band near the top,
        Horizontal spacings in a band near the left, so they don't
        collide with the grid-name labels on the lines themselves."""
        for i in range(1, len(rows)):
            prev_row = rows[i - 1]
            row = rows[i]
            if row.direction == "Vertical":
                x0 = self._x_to_px(prev_row.position_internal)
                x1 = self._x_to_px(row.position_internal)
                cx, cy = (x0 + x1) / 2.0, 20.0
            else:
                y0 = self._y_to_py(prev_row.position_internal)
                y1 = self._y_to_py(row.position_internal)
                cx, cy = 55.0, (y0 + y1) / 2.0

            label = TextBlock()
            label.Text = row.spacing_text
            label.FontSize = 10
            label.Foreground = _DIM_BRUSH
            label.FontWeight = FontWeights.Bold
            Canvas.SetLeft(label, cx - 20)
            Canvas.SetTop(label, cy - 8)
            canvas.Children.Add(label)

            hit = Rectangle()
            hit.Fill = Brushes.Transparent
            hit.Width = 46
            hit.Height = 16
            hit.Cursor = Cursors.Hand
            hit.Tag = row
            Canvas.SetLeft(hit, cx - 23)
            Canvas.SetTop(hit, cy - 8)
            hit.MouseLeftButtonDown += self._dim_label_click
            canvas.Children.Add(hit)

    def _dim_label_click(self, sender, args):
        row = sender.Tag
        rows = self._v_rows if row.direction == "Vertical" else self._h_rows
        try:
            idx = rows.index(row)
        except ValueError:
            return
        if idx == 0:
            return
        prev_row = rows[idx - 1]
        new_value = forms.ask_for_string(
            default=row.spacing_text,
            prompt="New spacing for {0} (measured from {1}):".format(row.name, prev_row.name),
            title="DeeGrid - Edit Spacing")
        if new_value is None:
            return
        try:
            display_val = float(new_value)
        except (ValueError, TypeError):
            forms.alert("Enter a valid number.")
            return
        row.position_internal = prev_row.position_internal + _display_to_internal(self.doc, display_val)
        self._recompute_refresh_and_redraw()
        args.Handled = True

    def _x_to_px(self, x):
        return self._preview_origin_px + (x - self._preview_min_x) * self._preview_scale

    def _px_to_x(self, px):
        return self._preview_min_x + (px - self._preview_origin_px) / self._preview_scale

    def _y_to_py(self, y):
        return self._preview_origin_py + (self._preview_max_y - y) * self._preview_scale

    def _py_to_y(self, py):
        return self._preview_max_y - (py - self._preview_origin_py) / self._preview_scale

    def _update_previewer_highlight(self):
        selected = self.v_grid.SelectedItem or self.h_grid.SelectedItem
        for row, (line, label, hit) in self._preview_elements.items():
            is_sel = row is selected
            brush = _SELECTED_BRUSH if is_sel else _NORMAL_BRUSH
            line.Stroke = brush
            line.StrokeThickness = 3 if is_sel else 1.5
            label.Foreground = brush

    # -- previewer: drag (never touches Canvas.Children mid-gesture, so the
    # captured element is never destroyed) -----------------------------------
    def _preview_mouse_down(self, sender, args):
        row = sender.Tag
        sender.CaptureMouse()
        self._drag_row = row
        if row.direction == "Vertical":
            self.v_grid.SelectedItem = row
        else:
            self.h_grid.SelectedItem = row
        args.Handled = True

    def _preview_mouse_move(self, sender, args):
        if self._drag_row is None or args.LeftButton != MouseButtonState.Pressed:
            return
        row = self._drag_row
        pos = args.GetPosition(self.preview_canvas)

        line, label, hit = self._preview_elements[row]
        if row.direction == "Vertical":
            px = pos.X
            if px < 0:
                px = 0.0
            if px > self._preview_width:
                px = self._preview_width
            row.position_internal = self._px_to_x(px)
            line.X1 = px
            line.X2 = px
            Canvas.SetLeft(hit, px - 6)
            Canvas.SetLeft(label, px + 2)
        else:
            py = pos.Y
            if py < 0:
                py = 0.0
            if py > self._preview_height:
                py = self._preview_height
            row.position_internal = self._py_to_y(py)
            line.Y1 = py
            line.Y2 = py
            Canvas.SetTop(hit, py - 6)
            Canvas.SetTop(label, py - 15)

        label.Text = "{0}  {1}".format(row.name, row.position_text)
        args.Handled = True

    def _preview_mouse_up(self, sender, args):
        if self._drag_row is not None:
            sender.ReleaseMouseCapture()
            self._drag_row = None
            self._recompute_refresh_and_redraw()
        args.Handled = True

    def preview_canvas_size_changed(self, sender, args):
        self._rebuild_previewer()

    def v_selection_changed(self, sender, args):
        self._update_previewer_highlight()

    def h_selection_changed(self, sender, args):
        self._update_previewer_highlight()

    # -- default extent for new grids ------------------------------------------
    def _default_extent(self, direction):
        """Extent along the axis PERPENDICULAR to `direction`, so a new
        grid visually crosses the existing grids in the other direction."""
        other_rows = self._h_rows if direction == "Vertical" else self._v_rows
        margin = UnitUtils.ConvertToInternalUnits(_DEFAULT_EXTENT_MARGIN_METERS, UnitTypeId.Meters)
        if other_rows:
            positions = [r.position_internal for r in other_rows]
            return min(positions) - margin, max(positions) + margin
        half = UnitUtils.ConvertToInternalUnits(_DEFAULT_EXTENT_HALF_METERS, UnitTypeId.Meters)
        return -half, half

    # -- buttons --------------------------------------------------------------
    def refresh_click(self, sender, args):
        self._load_from_model(confirm=True)

    def _add_grid(self, direction, rows):
        base_pos = rows[-1].position_internal if rows else 0.0
        new_pos = base_pos + UnitUtils.ConvertToInternalUnits(
            _DEFAULT_GRID_SPACING_METERS, UnitTypeId.Meters)
        other_min, other_max = self._default_extent(direction)
        n = len(rows) + 1
        prefix = "V" if direction == "Vertical" else "H"
        rows.append(GridRow(self.doc, "New {0} Grid {1}".format(prefix, n), new_pos, direction,
                             other_min=other_min, other_max=other_max))
        self._recompute_refresh_and_redraw()

    def add_vertical_click(self, sender, args):
        self._add_grid("Vertical", self._v_rows)

    def add_horizontal_click(self, sender, args):
        self._add_grid("Horizontal", self._h_rows)

    def _delete_grid(self, rows, selected_grid):
        row = selected_grid.SelectedItem
        if not row:
            forms.alert("Select a grid row first.")
            return
        if row.existing_id is None:
            rows.remove(row)
        else:
            row.pending_delete = not row.pending_delete
        self._recompute_refresh_and_redraw()

    def delete_vertical_click(self, sender, args):
        self._delete_grid(self._v_rows, self.v_grid)

    def delete_horizontal_click(self, sender, args):
        self._delete_grid(self._h_rows, self.h_grid)

    # -- pattern-based batch renaming -----------------------------------------
    def _rename_direction(self, rows):
        if not rows:
            forms.alert("No grids in this direction to rename.")
            return
        pattern = self.rename_pattern_cb.SelectedItem or _NAMING_PATTERNS[0]
        ordered = sorted(rows, key=lambda r: r.position_internal)
        total = len(ordered)
        for i, row in enumerate(ordered):
            row.name = _naming_pattern_label(i, pattern, total)
        self._recompute_refresh_and_redraw()

    def rename_vertical_click(self, sender, args):
        self._rename_direction(self._v_rows)

    def rename_horizontal_click(self, sender, args):
        self._rename_direction(self._h_rows)

    def v_row_edit_ending(self, sender, args):
        try:
            self.Dispatcher.BeginInvoke(System.Action(self._recompute_refresh_and_redraw))
        except Exception:
            self._recompute_refresh_and_redraw()

    def h_row_edit_ending(self, sender, args):
        try:
            self.Dispatcher.BeginInvoke(System.Action(self._recompute_refresh_and_redraw))
        except Exception:
            self._recompute_refresh_and_redraw()

    def close_click(self, sender, args):
        self.Close()

    # -- export -----------------------------------------------------------------
    def export_csv_click(self, sender, args):
        if not self._v_rows and not self._h_rows:
            forms.alert("No grids to export.")
            return
        dlg = SaveFileDialog()
        dlg.Filter = "CSV files (*.csv)|*.csv"
        dlg.FileName = "DeeGrid_Export.csv"
        if dlg.ShowDialog() != DialogResult.OK:
            return
        try:
            with open(dlg.FileName, "w") as f:
                writer = csv.writer(f)
                writer.writerow(["#", "Grid Name", "Direction", "Position", "Spacing", "Status"])
                ordered = (sorted(self._v_rows, key=lambda r: r.position_internal) +
                           sorted(self._h_rows, key=lambda r: r.position_internal))
                for row in ordered:
                    writer.writerow([row.sequence_text, row.name, row.direction,
                                      row.position_text, row.spacing_text, row.status_text])
        except Exception as e:
            forms.alert("Could not export: {0}".format(e))
            return
        MessageBox.Show("Exported {0} grid(s) to:\n{1}".format(len(self._v_rows) + len(self._h_rows), dlg.FileName),
                         "DeeGrid")

    # -- apply to model ---------------------------------------------------------
    def apply_click(self, sender, args):
        all_rows = self._v_rows + self._h_rows
        new_rows = [r for r in all_rows if r.existing_id is None]
        delete_rows = [r for r in all_rows if r.pending_delete and r.existing_id is not None]
        modified_rows = [
            r for r in all_rows
            if r.existing_id is not None and not r.pending_delete and
            (r.name != r._original_name or
             abs(r.position_internal - r._original_position_internal) > 1e-9)]

        if not new_rows and not delete_rows and not modified_rows:
            forms.alert("No pending changes to apply.")
            return

        summary = (
            "About to apply to the model:\n"
            "  {0} new grid(s) to create\n"
            "  {1} grid(s) to rename/move\n"
            "  {2} grid(s) to delete\n\n"
            "Continue?".format(len(new_rows), len(modified_rows), len(delete_rows)))
        if not forms.alert(summary, title="DeeGrid - Confirm", yes=True, no=True):
            return

        results = []
        deleted_ids = set()

        t = Transaction(self.doc, "DeeGrid - Apply Grid Changes")
        t.Start()

        for row in delete_rows:
            try:
                self.doc.Delete(row.existing_id)
                deleted_ids.add(row.existing_id)
                results.append((True, row.name, "Deleted"))
            except Exception as e:
                results.append((False, row.name, "Delete FAILED: {0}".format(e)))

        for row in modified_rows:
            elem = self.doc.GetElement(row.existing_id)
            if elem is None:
                results.append((False, row.name, "Element no longer exists"))
                continue
            try:
                delta = row.position_internal - row._original_position_internal
                if abs(delta) > 1e-9:
                    translation = (XYZ(delta, 0, 0) if row.direction == "Vertical"
                                   else XYZ(0, delta, 0))
                    ElementTransformUtils.MoveElement(self.doc, row.existing_id, translation)
                if row.name != row._original_name:
                    elem.Name = row.name
                row._original_name = row.name
                row._original_position_internal = row.position_internal
                results.append((True, row.name, "Updated"))
            except Exception as e:
                results.append((False, row.name, "Update FAILED: {0}".format(e)))

        for row in new_rows:
            try:
                other_min = row.other_min
                other_max = row.other_max
                if other_min is None or other_max is None:
                    other_min, other_max = self._default_extent(row.direction)
                if row.direction == "Vertical":
                    line = Line.CreateBound(
                        XYZ(row.position_internal, other_min, 0),
                        XYZ(row.position_internal, other_max, 0))
                else:
                    line = Line.CreateBound(
                        XYZ(other_min, row.position_internal, 0),
                        XYZ(other_max, row.position_internal, 0))
                new_grid = Grid.Create(self.doc, line)
                new_grid.Name = row.name
                row.existing_id = new_grid.Id
                row._original_name = row.name
                row._original_position_internal = row.position_internal
                results.append((True, row.name, "Created"))
            except Exception as e:
                results.append((False, row.name, "Create FAILED: {0}".format(e)))

        t.Commit()

        self._v_rows = [r for r in self._v_rows if r.existing_id not in deleted_ids]
        self._h_rows = [r for r in self._h_rows if r.existing_id not in deleted_ids]
        self._recompute_refresh_and_redraw()

        html = '<h2 style="font-family:sans-serif;color:#ddd;">DeeGrid Results</h2>'
        for ok, name, detail in results:
            bg = "#2e7d32" if ok else "#c62828"
            icon = "&#10003;" if ok else "&#10007;"
            html += (
                '<div style="padding:6px 12px;margin:3px 0;background:{0};color:#fff;'
                'border-radius:4px;font-family:monospace;font-size:13px;">'
                '{1}&nbsp; <b>{2}</b> &mdash; {3}</div>'.format(bg, icon, name, detail))
        output.print_html(html)


def main():
    doc = __revit__.ActiveUIDocument.Document
    window = DeeGridWindow(_XAML_FILE, doc)
    window.ShowDialog()


main()
