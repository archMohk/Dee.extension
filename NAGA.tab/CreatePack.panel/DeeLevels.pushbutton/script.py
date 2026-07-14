# -*- coding: utf-8 -*-
"""
DeeLevels (Detect Levels)
Scans all Levels in the active model into an editable grid, with a
visual previewer beside it:
  - Level Name, Elevation, and Height Diff from the level below are all
    editable - editing either Elevation or Height Diff recomputes the
    other, and the list re-sorts/recalculates Sequence automatically in
    elevation order ("inserting between levels" just means giving a row
    an elevation that lands between two others). For the lowest level,
    Height Diff is measured from datum (elevation 0).
  - The previewer draws each level as a horizontal line positioned by
    elevation. Selecting a row (grid or previewer) highlights it in
    both places. Dragging a line up/down in the previewer moves that
    level live; the grid/model update once the drag is released.
  - Add Level appends a new, not-yet-created row; Delete marks an
    existing level for deletion (toggle to undo) or drops an unsaved
    new row outright.
  - Structural Levels: select one or more main-level rows (click, or
    Ctrl/Shift-click for several) and "Generate Structural Levels for
    Selected" adds one companion level beneath each, at the Global
    Offset, named from the naming template ({level} is replaced with
    the main level's name). Editing a structural row's Elevation,
    Height Diff, or Offset directly marks it Overridden, which pins its
    offset - unchecking Override snaps it back to following the Global
    Offset. Changing the Global Offset live-updates every non-
    overridden structural level. The previewer draws structural levels
    in a different color with a thin connector line to their main
    level. The main/structural relationship lives only in this tool's
    session - it is not stored on the Level elements themselves, so a
    Refresh (re-scan) forgets which existing levels were structural
    companions of which.
  - Datum / Survey Point columns are read-only: each level's elevation
    minus the Project Base Point's current Z, and minus the Survey
    Point's current Z, respectively (matches Revit's own "Elevation
    Base" concept). A "Fixed" checkbox per point maps directly to
    Revit's real Clipped property on that point (the same padlock
    Revit's own UI uses to stop it being moved by accident) - applied
    at Apply time along with everything else.
  - Nothing touches the Revit model until "Apply Changes to Model" is
    clicked, which creates/renames/moves/deletes the real Level
    elements in one Transaction and reports per-level results.

Elevation/Height Diff/Offset are shown/edited as plain decimal numbers
in the project's current length display unit (shown in the column
headers) - not architectural feet-inches-fraction text.
"""
import os
import csv
import System
from pyrevit import forms, script
from Autodesk.Revit.DB import (
    FilteredElementCollector, Level, Transaction, BuiltInParameter,
    UnitUtils, SpecTypeId, UnitTypeId, BuiltInCategory, ElementId, BasePoint
)
from System.Windows.Shapes import Line, Rectangle
from System.Windows.Controls import Canvas, TextBlock
from System.Windows.Media import Brushes, SolidColorBrush, Color
from System.Windows.Input import Cursors, MouseButtonState

import clr
clr.AddReference("System.Windows.Forms")
from System.Windows.Forms import SaveFileDialog, DialogResult, MessageBox

output = script.get_output()

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")

_DEFAULT_STORY_HEIGHT_METERS = 4.0
_DEFAULT_STRUCTURAL_OFFSET_METERS = -0.15
_PREVIEW_MARGIN = 20.0
_ZERO_EPSILON = 1e-6

_NORMAL_BRUSH = SolidColorBrush(Color.FromRgb(70, 130, 180))
_SELECTED_BRUSH = SolidColorBrush(Color.FromRgb(220, 20, 60))
_STRUCTURAL_BRUSH = SolidColorBrush(Color.FromRgb(230, 126, 34))

_UNIT_ABBR = [
    (UnitTypeId.Millimeters, "mm"),
    (UnitTypeId.Centimeters, "cm"),
    (UnitTypeId.Meters, "m"),
    (UnitTypeId.Feet, "ft"),
    (UnitTypeId.FeetFractionalInches, "ft"),
    (UnitTypeId.Inches, "in"),
    (UnitTypeId.FractionalInches, "in"),
]


def _read_level_name(level):
    """Element.Name has thrown a bare, unhelpful exception on some
    element types in this Revit/IronPython combination before
    (ViewFamilyType, FamilySymbol) - applying the same Parameter
    fallback here defensively in case Level is affected too."""
    try:
        return level.Name
    except Exception:
        pass
    try:
        p = level.get_Parameter(BuiltInParameter.DATUM_TEXT)
        if p is not None:
            val = p.AsString()
            if val:
                return val
    except Exception:
        pass
    return None


def _collect_scope_boxes(doc):
    return list(FilteredElementCollector(doc)
                .OfCategory(BuiltInCategory.OST_VolumeOfInterest)
                .WhereElementIsNotElementType())


def _read_scope_box_id(level):
    try:
        p = level.get_Parameter(BuiltInParameter.DATUM_VOLUME_OF_INTEREST)
        if p is not None:
            val = p.AsElementId()
            if val is not None:
                return val
    except Exception:
        pass
    return ElementId.InvalidElementId


def _scope_box_name(doc, scope_box_id):
    if scope_box_id is None or scope_box_id == ElementId.InvalidElementId:
        return "(None)"
    elem = doc.GetElement(scope_box_id)
    if elem is None:
        return "(None)"
    try:
        return elem.Name
    except Exception:
        return "(Unnamed Scope Box)"


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


def _compose_structural_name(template, main_name):
    try:
        return template.format(level=main_name)
    except Exception:
        return "{0} - STR".format(main_name)


def _compose_decorated_name(row, prefix, suffix, include_datum, include_dimension):
    """Rebuilds a row's displayed name from its base_name plus whichever
    of Prefix / Suffix / Datum / Dimension-to-zero are turned on. Applies
    identically to main levels and structural levels - `base_name` for a
    structural row is whatever the structural naming template produced,
    so this just decorates on top of that."""
    parts = [row.base_name]
    if include_datum:
        parts.append("Datum {0}".format(row.datum_text))
    if include_dimension:
        parts.append(row.elevation_text)
    core = " - ".join(parts)
    if prefix:
        core = "{0} {1}".format(prefix, core)
    if suffix:
        core = "{0} {1}".format(core, suffix)
    return core


def _get_base_point(doc):
    try:
        return BasePoint.GetProjectBasePoint(doc)
    except Exception:
        return None


def _get_survey_point(doc):
    try:
        return BasePoint.GetSurveyPoint(doc)
    except Exception:
        return None


def _point_z_internal(point):
    if point is None:
        return 0.0
    try:
        return point.Position.Z
    except Exception:
        return 0.0


class LevelRow(object):
    """One row in the grid - either a real Level (existing_id set) or a
    not-yet-created one (existing_id is None). `_original_*` track the
    values last known to match the model, so status_text can tell
    Existing / Modified / New / Pending Delete apart. `_prev_elevation_
    internal` is cached at each recompute so editing Height Diff can
    work out the new Elevation on its own (0.0 / datum for the lowest
    level, which has no level below it).

    Structural-level fields (only meaningful when is_structural=True):
    `structural_of` is the main-level LevelRow this one was generated
    from, `structural_offset_internal` is this row's own offset below
    that main level, and `structural_override` marks whether this row
    is pinned to its own offset (True) or should keep following the
    window's Global Offset (False).

    `base_name` is the "undecorated" name (whatever it was last typed
    directly, in the grid or elsewhere) - Prefix/Suffix/Datum/Dimension
    naming always rebuilds `name` FROM `base_name`, so re-applying it is
    idempotent instead of compounding onto an already-decorated name.
    Editing the Level Name cell directly resets both, via the `name`
    setter."""

    def __init__(self, doc, name, elevation_internal, existing_id=None,
                 scope_box_id=None, is_structural=False, structural_of=None,
                 base_point_z_internal=0.0, survey_point_z_internal=0.0):
        self.doc = doc
        self._name = name
        self.base_name = name
        self.elevation_internal = elevation_internal
        self.existing_id = existing_id
        self.scope_box_id = scope_box_id if scope_box_id is not None else ElementId.InvalidElementId
        self.pending_delete = False
        self.sequence = 0
        self.height_diff_internal = 0.0
        self._prev_elevation_internal = 0.0
        self._original_name = name
        self._original_elevation_internal = elevation_internal
        self._original_scope_box_id = self.scope_box_id

        self.is_structural = is_structural
        self.structural_of = structural_of
        self.structural_offset_internal = 0.0
        self.structural_override = False

        self.base_point_z_internal = base_point_z_internal
        self.survey_point_z_internal = survey_point_z_internal

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, value):
        # A direct edit (e.g. typing in the grid) redefines what
        # "undecorated" means for this row too.
        self._name = value
        self.base_name = value

    def _set_decorated_name(self, value):
        """Used only by the Naming section's Apply buttons - updates the
        displayed name WITHOUT touching base_name, so repeated applies
        stay idempotent instead of stacking prefixes/suffixes."""
        self._name = value

    @property
    def sequence_text(self):
        return str(self.sequence)

    @property
    def elevation_text(self):
        return "{0:.3f}".format(_internal_to_display(self.doc, self.elevation_internal))

    @elevation_text.setter
    def elevation_text(self, value):
        try:
            display_val = float(value)
        except (ValueError, TypeError):
            return
        self.elevation_internal = _display_to_internal(self.doc, display_val)
        self._mark_structural_override_from_elevation()

    @property
    def height_diff_text(self):
        return "{0:.3f}".format(_internal_to_display(self.doc, self.height_diff_internal))

    @height_diff_text.setter
    def height_diff_text(self, value):
        try:
            display_val = float(value)
        except (ValueError, TypeError):
            return
        self.elevation_internal = self._prev_elevation_internal + _display_to_internal(
            self.doc, display_val)
        self._mark_structural_override_from_elevation()

    @property
    def offset_text(self):
        if not self.is_structural:
            return ""
        return "{0:.3f}".format(_internal_to_display(self.doc, self.structural_offset_internal))

    @offset_text.setter
    def offset_text(self, value):
        if not self.is_structural:
            return
        try:
            display_val = float(value)
        except (ValueError, TypeError):
            return
        self.structural_offset_internal = _display_to_internal(self.doc, display_val)
        self.structural_override = True
        if self.structural_of is not None:
            self.elevation_internal = self.structural_of.elevation_internal + self.structural_offset_internal

    def _mark_structural_override_from_elevation(self):
        """A direct Elevation/Height Diff edit on a structural row means
        the user wants THIS level pinned there - flips on Override and
        keeps the row's own offset consistent with the new elevation."""
        if not self.is_structural:
            return
        self.structural_override = True
        if self.structural_of is not None:
            self.structural_offset_internal = self.elevation_internal - self.structural_of.elevation_internal

    @property
    def structural_of_text(self):
        if self.is_structural and self.structural_of is not None:
            return self.structural_of.name
        return ""

    @property
    def datum_text(self):
        return "{0:.3f}".format(
            _internal_to_display(self.doc, self.elevation_internal - self.base_point_z_internal))

    @property
    def survey_text(self):
        return "{0:.3f}".format(
            _internal_to_display(self.doc, self.elevation_internal - self.survey_point_z_internal))

    @property
    def scope_box_name_text(self):
        return _scope_box_name(self.doc, self.scope_box_id)

    @property
    def status_text(self):
        suffix = " (Structural)" if self.is_structural else ""
        if self.pending_delete:
            return "Pending Delete" + suffix
        if self.existing_id is None:
            return "New" + suffix
        if (self.name != self._original_name or
                abs(self.elevation_internal - self._original_elevation_internal) > 1e-9 or
                self.scope_box_id != self._original_scope_box_id):
            return "Modified" + suffix
        return "Existing" + suffix


class DeeLevelsWindow(forms.WPFWindow):
    def __init__(self, xaml_file, doc):
        forms.WPFWindow.__init__(self, xaml_file)
        self.doc = doc
        self._initializing = True
        self._rows = []
        self._preview_elements = {}
        self._drag_row = None
        self._preview_min_e = 0.0
        self._preview_max_e = 0.0
        self._preview_height = 500.0

        unit_abbr = _unit_abbreviation(doc)
        for g in (self.above_grid, self.zero_grid, self.below_grid):
            g.Columns[2].Header = "Elevation ({0})".format(unit_abbr)
            g.Columns[3].Header = "Height Diff ({0})".format(unit_abbr)
            g.Columns[4].Header = "Datum ({0})".format(unit_abbr)
            g.Columns[5].Header = "Survey Point ({0})".format(unit_abbr)
            g.Columns[8].Header = "Offset ({0})".format(unit_abbr)
        self.struct_offset_unit_tb.Text = unit_abbr

        scope_boxes = _collect_scope_boxes(doc)
        self._scope_box_by_name = {sb.Name: sb.Id for sb in scope_boxes}
        self.scope_box_cb.ItemsSource = ["(None)"] + sorted(self._scope_box_by_name.keys())
        self.scope_box_cb.SelectedIndex = 0

        self._base_point = _get_base_point(doc)
        self._survey_point = _get_survey_point(doc)
        self._base_point_z_internal = _point_z_internal(self._base_point)
        self._survey_point_z_internal = _point_z_internal(self._survey_point)
        self._base_point_original_clipped = bool(self._base_point.Clipped) if self._base_point is not None else False
        self._survey_point_original_clipped = bool(self._survey_point.Clipped) if self._survey_point is not None else False
        self.base_point_fixed_cb.IsChecked = self._base_point_original_clipped
        self.survey_point_fixed_cb.IsChecked = self._survey_point_original_clipped
        if self._base_point is None:
            self.base_point_fixed_cb.IsEnabled = False
        if self._survey_point is None:
            self.survey_point_fixed_cb.IsEnabled = False

        default_offset_internal = UnitUtils.ConvertToInternalUnits(
            _DEFAULT_STRUCTURAL_OFFSET_METERS, UnitTypeId.Meters)
        self.struct_offset_tb.Text = "{0:.3f}".format(
            _internal_to_display(doc, default_offset_internal))

        self._load_from_model(confirm=False)
        self._initializing = False

    # -- scan / recompute / redraw -------------------------------------------
    def _fixed_checkboxes_changed(self):
        base_changed = (self._base_point is not None and
                        bool(self.base_point_fixed_cb.IsChecked) != self._base_point_original_clipped)
        survey_changed = (self._survey_point is not None and
                          bool(self.survey_point_fixed_cb.IsChecked) != self._survey_point_original_clipped)
        return base_changed or survey_changed

    def _has_pending_changes(self):
        return self._fixed_checkboxes_changed() or any(
            r.pending_delete or r.existing_id is None or
            r.name != r._original_name or
            abs(r.elevation_internal - r._original_elevation_internal) > 1e-9 or
            r.scope_box_id != r._original_scope_box_id
            for r in self._rows)

    def _load_from_model(self, confirm=True):
        if confirm and self._has_pending_changes():
            if not forms.alert(
                    "Discard unsaved changes and re-scan levels from the model?",
                    title="DeeLevels", yes=True, no=True):
                return
        levels = list(FilteredElementCollector(self.doc).OfClass(Level))
        self._rows = [
            LevelRow(self.doc, _read_level_name(lvl) or "(unnamed)", lvl.Elevation, lvl.Id,
                      scope_box_id=_read_scope_box_id(lvl),
                      base_point_z_internal=self._base_point_z_internal,
                      survey_point_z_internal=self._survey_point_z_internal)
            for lvl in levels
        ]
        self._recompute_refresh_and_redraw()

    def _global_offset_internal(self):
        try:
            display_val = float(self.struct_offset_tb.Text)
        except (ValueError, TypeError):
            display_val = _DEFAULT_STRUCTURAL_OFFSET_METERS * 1000.0
        return _display_to_internal(self.doc, display_val)

    def _sync_structural_offsets(self):
        global_offset = self._global_offset_internal()
        for row in self._rows:
            if row.is_structural and row.structural_of is not None:
                if not row.structural_override:
                    row.structural_offset_internal = global_offset
                row.elevation_internal = row.structural_of.elevation_internal + row.structural_offset_internal

    def _recompute(self):
        self._sync_structural_offsets()
        self._rows.sort(key=lambda r: r.elevation_internal)
        prev = None
        for i, row in enumerate(self._rows):
            row.sequence = i + 1
            row._prev_elevation_internal = prev if prev is not None else 0.0
            row.height_diff_internal = row.elevation_internal - row._prev_elevation_internal
            prev = row.elevation_internal

    def _section_for_row(self, row):
        """Which of the three stacked grids a row belongs in, purely by
        the sign of its (raw) elevation - Upground / Zero / Underground."""
        if row.elevation_internal > _ZERO_EPSILON:
            return self.above_grid
        if row.elevation_internal < -_ZERO_EPSILON:
            return self.below_grid
        return self.zero_grid

    def _all_grids(self):
        return (self.above_grid, self.zero_grid, self.below_grid)

    def _get_selected_rows(self):
        rows = []
        for g in self._all_grids():
            rows.extend(list(g.SelectedItems))
        return rows

    def _get_selected_row(self):
        rows = self._get_selected_rows()
        return rows[0] if rows else None

    def _refresh_grid(self):
        above = [r for r in self._rows if r.elevation_internal > _ZERO_EPSILON]
        zero = [r for r in self._rows if abs(r.elevation_internal) <= _ZERO_EPSILON]
        below = [r for r in self._rows if r.elevation_internal < -_ZERO_EPSILON]

        for grid, rows in ((self.above_grid, above), (self.zero_grid, zero), (self.below_grid, below)):
            selected = grid.SelectedItem
            grid.ItemsSource = None
            grid.ItemsSource = rows
            if selected in rows:
                grid.SelectedItem = selected

    def _recompute_refresh_and_redraw(self):
        self._recompute()
        self._refresh_grid()
        self._rebuild_previewer()

    # -- previewer: elevation <-> pixel mapping -------------------------------
    def _elev_to_y(self, elevation_internal, min_e, max_e, height):
        usable = height - 2 * _PREVIEW_MARGIN
        if usable <= 0 or max_e - min_e < 1e-9:
            return height / 2.0
        frac = (elevation_internal - min_e) / (max_e - min_e)
        return _PREVIEW_MARGIN + (1.0 - frac) * usable

    def _y_to_elev(self, y, min_e, max_e, height):
        usable = height - 2 * _PREVIEW_MARGIN
        if usable <= 0:
            return min_e
        frac = 1.0 - (y - _PREVIEW_MARGIN) / usable
        return min_e + frac * (max_e - min_e)

    # -- previewer: full rebuild (safe between gestures) ---------------------
    def _rebuild_previewer(self):
        canvas = self.preview_canvas
        canvas.Children.Clear()
        self._preview_elements = {}
        self._drag_row = None

        if not self._rows:
            return

        height = canvas.ActualHeight if canvas.ActualHeight > 1 else 500.0
        width = canvas.ActualWidth if canvas.ActualWidth > 1 else 200.0

        elevations = [r.elevation_internal for r in self._rows]
        min_e = min(elevations)
        max_e = max(elevations)
        if max_e - min_e < 1e-6:
            pad = UnitUtils.ConvertToInternalUnits(1.0, UnitTypeId.Meters)
        else:
            pad = (max_e - min_e) * 0.15
        min_e -= pad
        max_e += pad

        self._preview_min_e = min_e
        self._preview_max_e = max_e
        self._preview_height = height

        selected_rows = self._get_selected_rows()

        for row in self._rows:
            y = self._elev_to_y(row.elevation_internal, min_e, max_e, height)
            is_sel = row in selected_rows
            if is_sel:
                brush = _SELECTED_BRUSH
            elif row.is_structural:
                brush = _STRUCTURAL_BRUSH
            else:
                brush = _NORMAL_BRUSH

            line = Line()
            line.X1 = 0
            line.Y1 = y
            line.X2 = width
            line.Y2 = y
            line.Stroke = brush
            line.StrokeThickness = 3 if is_sel else 1.5
            if row.is_structural:
                line.StrokeThickness = 3 if is_sel else 2.0
            canvas.Children.Add(line)

            label = TextBlock()
            label.Text = "{0}  {1}".format(row.name, row.elevation_text)
            label.Foreground = brush
            label.FontSize = 11
            Canvas.SetLeft(label, 4)
            Canvas.SetTop(label, y - 15)
            canvas.Children.Add(label)

            hit = Rectangle()
            hit.Width = width
            hit.Height = 14
            hit.Fill = Brushes.Transparent
            hit.Cursor = Cursors.SizeNS
            hit.Tag = row
            Canvas.SetLeft(hit, 0)
            Canvas.SetTop(hit, y - 7)
            hit.MouseLeftButtonDown += self._preview_mouse_down
            hit.MouseMove += self._preview_mouse_move
            hit.MouseLeftButtonUp += self._preview_mouse_up
            canvas.Children.Add(hit)

            self._preview_elements[row] = (line, label, hit)

        # Thin connector between each structural level and its main level,
        # so the relationship is visible at a glance.
        for row in self._rows:
            if row.is_structural and row.structural_of in self._preview_elements:
                main_line, _, _ = self._preview_elements[row.structural_of]
                struct_line, _, _ = self._preview_elements[row]
                connector = Line()
                connector.X1 = width - 20
                connector.Y1 = main_line.Y1
                connector.X2 = width - 20
                connector.Y2 = struct_line.Y1
                connector.Stroke = _STRUCTURAL_BRUSH
                connector.StrokeThickness = 1
                canvas.Children.Add(connector)

    def _update_previewer_highlight(self):
        selected_rows = self._get_selected_rows()
        for row, (line, label, hit) in self._preview_elements.items():
            is_sel = row in selected_rows
            if is_sel:
                brush = _SELECTED_BRUSH
            elif row.is_structural:
                brush = _STRUCTURAL_BRUSH
            else:
                brush = _NORMAL_BRUSH
            line.Stroke = brush
            line.StrokeThickness = 3 if is_sel else (2.0 if row.is_structural else 1.5)
            label.Foreground = brush

    # -- previewer: drag (never touches Canvas.Children mid-gesture, so the
    # captured element is never destroyed) -----------------------------------
    def _preview_mouse_down(self, sender, args):
        row = sender.Tag
        sender.CaptureMouse()
        self._drag_row = row
        self._section_for_row(row).SelectedItem = row
        args.Handled = True

    def _preview_mouse_move(self, sender, args):
        if self._drag_row is None or args.LeftButton != MouseButtonState.Pressed:
            return
        pos = args.GetPosition(self.preview_canvas)
        y = pos.Y
        if y < 0:
            y = 0.0
        if y > self._preview_height:
            y = self._preview_height
        new_elev = self._y_to_elev(y, self._preview_min_e, self._preview_max_e, self._preview_height)
        self._drag_row.elevation_internal = new_elev
        self._drag_row._mark_structural_override_from_elevation()

        line, label, hit = self._preview_elements[self._drag_row]
        line.Y1 = y
        line.Y2 = y
        Canvas.SetTop(label, y - 15)
        Canvas.SetTop(hit, y - 7)
        label.Text = "{0}  {1}".format(self._drag_row.name, self._drag_row.elevation_text)
        args.Handled = True

    def _preview_mouse_up(self, sender, args):
        if self._drag_row is not None:
            sender.ReleaseMouseCapture()
            self._drag_row = None
            self._recompute_refresh_and_redraw()
        args.Handled = True

    def preview_canvas_size_changed(self, sender, args):
        self._rebuild_previewer()

    def levels_grid_selection_changed(self, sender, args):
        self._update_previewer_highlight()

    # -- buttons --------------------------------------------------------------
    def refresh_click(self, sender, args):
        self._load_from_model(confirm=True)

    def add_level_click(self, sender, args):
        base_elev = self._rows[-1].elevation_internal if self._rows else 0.0
        new_elev = base_elev + UnitUtils.ConvertToInternalUnits(
            _DEFAULT_STORY_HEIGHT_METERS, UnitTypeId.Meters)
        n = len(self._rows) + 1
        self._rows.append(LevelRow(
            self.doc, "New Level {0}".format(n), new_elev, None,
            base_point_z_internal=self._base_point_z_internal,
            survey_point_z_internal=self._survey_point_z_internal))
        self._recompute_refresh_and_redraw()

    def delete_level_click(self, sender, args):
        row = self._get_selected_row()
        if not row:
            forms.alert("Select a level row first.")
            return
        if row.existing_id is None:
            self._rows.remove(row)
        else:
            row.pending_delete = not row.pending_delete
        self._recompute_refresh_and_redraw()

    def _apply_scope_box_to_rows(self, rows):
        name = self.scope_box_cb.SelectedItem
        if name is None:
            return
        scope_id = (self._scope_box_by_name[name] if name != "(None)"
                    else ElementId.InvalidElementId)
        for row in rows:
            row.scope_box_id = scope_id
        self._recompute_refresh_and_redraw()

    def apply_scope_selected_click(self, sender, args):
        row = self._get_selected_row()
        if not row:
            forms.alert("Select a level row first.")
            return
        self._apply_scope_box_to_rows([row])

    def apply_scope_all_click(self, sender, args):
        if not self._rows:
            return
        self._apply_scope_box_to_rows(list(self._rows))

    # -- level naming (Prefix/Suffix/Datum/Dimension - main + structural alike) --
    def _apply_naming_to_rows(self, rows):
        if not rows:
            forms.alert("Select one or more level rows first.")
            return
        prefix = self.naming_prefix_tb.Text.strip()
        suffix = self.naming_suffix_tb.Text.strip()
        include_datum = bool(self.naming_datum_cb.IsChecked)
        include_dimension = bool(self.naming_dimension_cb.IsChecked)
        for row in rows:
            row._set_decorated_name(_compose_decorated_name(
                row, prefix, suffix, include_datum, include_dimension))
        self._recompute_refresh_and_redraw()

    def apply_naming_selected_click(self, sender, args):
        self._apply_naming_to_rows(self._get_selected_rows())

    def apply_naming_all_click(self, sender, args):
        self._apply_naming_to_rows(list(self._rows))

    # -- structural levels ------------------------------------------------------
    def struct_offset_changed(self, sender, args):
        if self._initializing:
            return
        self._recompute_refresh_and_redraw()

    def generate_structural_click(self, sender, args):
        if not self.create_struct_cb.IsChecked:
            forms.alert("Check 'Create Structural Levels' first.")
            return
        selected = [r for r in self._get_selected_rows() if not r.is_structural]
        if not selected:
            forms.alert("Select one or more main level rows first (not structural rows).")
            return

        template = self.struct_naming_tb.Text or "{level} - STR"
        global_offset = self._global_offset_internal()
        existing_names = set(r.name for r in self._rows)

        created = 0
        skipped = []
        for main_row in selected:
            struct_name = _compose_structural_name(template, main_row.name)
            if struct_name in existing_names:
                skipped.append("{0} (name already exists)".format(struct_name))
                continue
            struct_elev = main_row.elevation_internal + global_offset
            conflict = next((r for r in self._rows if abs(r.elevation_internal - struct_elev) < 1e-6), None)
            if conflict is not None:
                skipped.append("{0} (elevation conflicts with '{1}')".format(struct_name, conflict.name))
                continue

            new_row = LevelRow(
                self.doc, struct_name, struct_elev, None,
                is_structural=True, structural_of=main_row,
                base_point_z_internal=self._base_point_z_internal,
                survey_point_z_internal=self._survey_point_z_internal)
            new_row.structural_offset_internal = global_offset
            self._rows.append(new_row)
            existing_names.add(struct_name)
            created += 1

        self._recompute_refresh_and_redraw()

        msg = "{0} structural level(s) created.".format(created)
        if skipped:
            msg += "\n\nSkipped:\n" + "\n".join(skipped)
        forms.alert(msg, title="DeeLevels - Structural Levels")

    def rename_structural_click(self, sender, args):
        struct_rows = [r for r in self._rows if r.is_structural and r.structural_of is not None]
        if not struct_rows:
            forms.alert("No structural levels to rename.")
            return
        template = self.struct_naming_tb.Text or "{level} - STR"
        existing_names = set(r.name for r in self._rows)
        renamed = 0
        skipped = 0
        for row in struct_rows:
            existing_names.discard(row.name)
            new_name = _compose_structural_name(template, row.structural_of.name)
            if new_name in existing_names:
                existing_names.add(row.name)
                skipped += 1
                continue
            row.name = new_name
            existing_names.add(new_name)
            renamed += 1
        self._recompute_refresh_and_redraw()
        msg = "{0} structural level name(s) updated.".format(renamed)
        if skipped:
            msg += " {0} skipped (would collide with another name).".format(skipped)
        forms.alert(msg, title="DeeLevels - Structural Levels")

    def levels_row_edit_ending(self, sender, args):
        # Deferred via Dispatcher: reassigning ItemsSource synchronously
        # inside the DataGrid's own RowEditEnding (re-entering the same
        # control's edit-transition machinery) risks an invalid-operation
        # error, so the recompute+redraw runs right after this event
        # finishes instead.
        try:
            self.Dispatcher.BeginInvoke(System.Action(self._recompute_refresh_and_redraw))
        except Exception:
            self._recompute_refresh_and_redraw()

    def close_click(self, sender, args):
        self.Close()

    # -- export -----------------------------------------------------------------
    def export_csv_click(self, sender, args):
        if not self._rows:
            forms.alert("No levels to export.")
            return
        dlg = SaveFileDialog()
        dlg.Filter = "CSV files (*.csv)|*.csv"
        dlg.FileName = "DeeLevels_Export.csv"
        if dlg.ShowDialog() != DialogResult.OK:
            return
        try:
            with open(dlg.FileName, "w") as f:
                writer = csv.writer(f)
                writer.writerow(["#", "Level Name", "Elevation", "Height Diff", "Datum",
                                  "Survey Point", "Scope Box", "Structural Of", "Offset",
                                  "Override", "Status"])
                for row in self._rows:
                    writer.writerow([
                        row.sequence_text, row.name, row.elevation_text, row.height_diff_text,
                        row.datum_text, row.survey_text, row.scope_box_name_text,
                        row.structural_of_text, row.offset_text,
                        "Yes" if row.structural_override else "No", row.status_text])
        except Exception as e:
            forms.alert("Could not export: {0}".format(e))
            return
        MessageBox.Show("Exported {0} level(s) to:\n{1}".format(len(self._rows), dlg.FileName), "DeeLevels")

    # -- apply to model ---------------------------------------------------------
    def apply_click(self, sender, args):
        new_rows = [r for r in self._rows if r.existing_id is None]
        delete_rows = [r for r in self._rows if r.pending_delete and r.existing_id is not None]
        modified_rows = [
            r for r in self._rows
            if r.existing_id is not None and not r.pending_delete and
            (r.name != r._original_name or
             abs(r.elevation_internal - r._original_elevation_internal) > 1e-9 or
             r.scope_box_id != r._original_scope_box_id)]

        base_point_changed = (self._base_point is not None and
                              bool(self.base_point_fixed_cb.IsChecked) != self._base_point_original_clipped)
        survey_point_changed = (self._survey_point is not None and
                                bool(self.survey_point_fixed_cb.IsChecked) != self._survey_point_original_clipped)

        if not new_rows and not delete_rows and not modified_rows and not base_point_changed and not survey_point_changed:
            forms.alert("No pending changes to apply.")
            return

        struct_new_count = sum(1 for r in new_rows if r.is_structural)
        fixed_note = ""
        if base_point_changed or survey_point_changed:
            fixed_note = "\n  Base Point / Survey Point Fixed state will also change\n"
        summary = (
            "About to apply to the model:\n"
            "  {0} new level(s) to create ({1} structural)\n"
            "  {2} level(s) to rename/move\n"
            "  {3} level(s) to delete\n"
            "{4}\n"
            "Continue?".format(len(new_rows), struct_new_count, len(modified_rows), len(delete_rows), fixed_note))
        if not forms.alert(summary, title="DeeLevels - Confirm", yes=True, no=True):
            return

        results = []
        deleted_ids = set()

        t = Transaction(self.doc, "DeeLevels - Apply Level Changes")
        t.Start()

        if base_point_changed:
            try:
                self._base_point.Clipped = bool(self.base_point_fixed_cb.IsChecked)
                self._base_point_original_clipped = bool(self.base_point_fixed_cb.IsChecked)
                results.append((True, "Project Base Point",
                                "Fixed" if self._base_point_original_clipped else "Unfixed"))
            except Exception as e:
                results.append((False, "Project Base Point", "Could not change Fixed state: {0}".format(e)))

        if survey_point_changed:
            try:
                self._survey_point.Clipped = bool(self.survey_point_fixed_cb.IsChecked)
                self._survey_point_original_clipped = bool(self.survey_point_fixed_cb.IsChecked)
                results.append((True, "Survey Point",
                                "Fixed" if self._survey_point_original_clipped else "Unfixed"))
            except Exception as e:
                results.append((False, "Survey Point", "Could not change Fixed state: {0}".format(e)))

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
                if abs(row.elevation_internal - row._original_elevation_internal) > 1e-9:
                    elem.get_Parameter(BuiltInParameter.LEVEL_ELEV).Set(row.elevation_internal)
                if row.name != row._original_name:
                    elem.Name = row.name
                if row.scope_box_id != row._original_scope_box_id:
                    elem.get_Parameter(BuiltInParameter.DATUM_VOLUME_OF_INTEREST).Set(row.scope_box_id)
                row._original_name = row.name
                row._original_elevation_internal = row.elevation_internal
                row._original_scope_box_id = row.scope_box_id
                results.append((True, row.name, "Updated"))
            except Exception as e:
                results.append((False, row.name, "Update FAILED: {0}".format(e)))

        # Main (non-structural) levels first, so structural levels below
        # them can rely on an already-created main level existing if ever
        # needed - not currently required (structural rows only read the
        # main row's in-memory elevation), but keeps creation order sane.
        for row in sorted(new_rows, key=lambda r: r.is_structural):
            try:
                new_level = Level.Create(self.doc, row.elevation_internal)
                new_level.Name = row.name
                if row.scope_box_id != ElementId.InvalidElementId:
                    new_level.get_Parameter(BuiltInParameter.DATUM_VOLUME_OF_INTEREST).Set(row.scope_box_id)
                row.existing_id = new_level.Id
                row._original_name = row.name
                row._original_elevation_internal = row.elevation_internal
                row._original_scope_box_id = row.scope_box_id
                results.append((True, row.name, "Created"))
            except Exception as e:
                results.append((False, row.name, "Create FAILED: {0}".format(e)))

        t.Commit()

        self._rows = [r for r in self._rows if r.existing_id not in deleted_ids]
        self._recompute_refresh_and_redraw()

        html = '<h2 style="font-family:sans-serif;color:#ddd;">DeeLevels Results</h2>'
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
    window = DeeLevelsWindow(_XAML_FILE, doc)
    window.ShowDialog()


main()
