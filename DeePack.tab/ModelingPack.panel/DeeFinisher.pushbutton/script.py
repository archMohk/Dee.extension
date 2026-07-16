# -*- coding: utf-8 -*-
"""
DeeFinisher
Scans Rooms (active view or whole project), lets you assign a Floor Finish
Type / Wall Finish Type / Ceiling Finish Type per room (or in bulk via the
Global Assignment Panel), previews what will be created, then generates the
actual Floor / Wall / Ceiling elements from each Room's boundary in one
Transaction.

Wall Finish is generated in "full" mode per explicit request: each straight,
wall-bounded boundary segment is offset inward (toward the room) by half the
finish wall's thickness, corners are mitered by intersecting adjacent offset
edges, segments are created via Wall.Create, and adjacent segments are joined
via JoinGeometryUtils after creation. This is the most geometry-sensitive
part of the tool (curved boundary segments fall back to their own unmitered
offset, and separation-line-bounded segments are skipped since there's no
physical wall to line the inside of) - review results in the model and
re-run (Skip/Replace/Update Existing) if a corner needs manual cleanup.

Previously-created elements are tagged via the Comments parameter
("DeeFinisher|Room:<id>|Type:<kind>[|Segment:<i>]") so re-runs can Skip /
Replace / Update / Ask per the Duplicate Detection setting instead of piling
up duplicates.
"""
import os
import csv
from pyrevit import forms, script
from Autodesk.Revit.DB import (
    FilteredElementCollector, BuiltInCategory, ElementId, Transaction,
    BuiltInParameter, SpatialElementBoundaryOptions, SpatialElementBoundaryLocation,
    CurveLoop, Line, XYZ, Floor, FloorType, Ceiling, CeilingType,
    Wall, WallType, JoinGeometryUtils, UnitUtils, UnitTypeId, SpecTypeId, Level
)
from Autodesk.Revit.DB.Architecture import Room

from System.Collections.Generic import List
import clr
clr.AddReference("System.Windows.Forms")
from System.Windows.Forms import SaveFileDialog, DialogResult, MessageBox

output = script.get_output()

_XAML_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.xaml")
_TAG_PREFIX = "DeeFinisher|"


# --------------------------------------------------------------------------
# Units
# --------------------------------------------------------------------------
def _length_unit_type_id(doc):
    try:
        return doc.GetUnits().GetFormatOptions(SpecTypeId.Length).GetUnitTypeId()
    except Exception:
        return UnitTypeId.Millimeters


_UNIT_ABBR = [
    (UnitTypeId.Millimeters, "mm"),
    (UnitTypeId.Centimeters, "cm"),
    (UnitTypeId.Meters, "m"),
    (UnitTypeId.Feet, "ft"),
    (UnitTypeId.FeetFractionalInches, "ft"),
    (UnitTypeId.FractionalInches, "in"),
    (UnitTypeId.Inches, "in"),
]


def _unit_abbreviation(doc):
    uid = _length_unit_type_id(doc)
    for u, abbr in _UNIT_ABBR:
        if u == uid:
            return abbr
    return "mm"


def _internal_to_display(doc, value_internal):
    uid = _length_unit_type_id(doc)
    try:
        return UnitUtils.ConvertFromInternalUnits(value_internal, uid)
    except Exception:
        return UnitUtils.ConvertFromInternalUnits(value_internal, UnitTypeId.Millimeters)


def _display_to_internal(doc, value_display):
    uid = _length_unit_type_id(doc)
    try:
        return UnitUtils.ConvertToInternalUnits(value_display, uid)
    except Exception:
        return UnitUtils.ConvertToInternalUnits(value_display, UnitTypeId.Millimeters)


_AREA_UNIT_ABBR = [
    (UnitTypeId.SquareMeters, "m2"),
    (UnitTypeId.SquareFeet, "ft2"),
]


def _format_area(doc, area_internal):
    try:
        uid = doc.GetUnits().GetFormatOptions(SpecTypeId.Area).GetUnitTypeId()
        val = UnitUtils.ConvertFromInternalUnits(area_internal, uid)
    except Exception:
        uid = UnitTypeId.SquareMeters
        val = UnitUtils.ConvertFromInternalUnits(area_internal, uid)
    abbr = "m2"
    for u, a in _AREA_UNIT_ABBR:
        if u == uid:
            abbr = a
            break
    return "{0:.2f} {1}".format(val, abbr)


# --------------------------------------------------------------------------
# Defensive name / value reads (Element.Name throws a bare exception on some
# types in this Revit/IronPython combination - fall back to Parameter reads)
# --------------------------------------------------------------------------
def _read_name(element):
    if element is None:
        return None
    try:
        n = element.Name
        if n:
            return n
    except Exception:
        pass
    for bip in (BuiltInParameter.SYMBOL_NAME_PARAM, BuiltInParameter.ALL_MODEL_TYPE_NAME,
                BuiltInParameter.DATUM_TEXT):
        try:
            p = element.get_Parameter(bip)
            if p is not None:
                val = p.AsString()
                if val:
                    return val
        except Exception:
            continue
    return None


def _read_room_number(room):
    try:
        n = room.Number
        if n:
            return n
    except Exception:
        pass
    try:
        p = room.get_Parameter(BuiltInParameter.ROOM_NUMBER)
        if p is not None:
            v = p.AsString()
            if v:
                return v
    except Exception:
        pass
    return "?"


def _read_room_name(room):
    try:
        n = room.Name
        if n:
            return n
    except Exception:
        pass
    try:
        p = room.get_Parameter(BuiltInParameter.ROOM_NAME)
        if p is not None:
            v = p.AsString()
            if v:
                return v
    except Exception:
        pass
    return "(unnamed)"


def _room_dept(room):
    try:
        p = room.get_Parameter(BuiltInParameter.ROOM_DEPARTMENT)
        if p is not None:
            v = p.AsString()
            if v:
                return v
    except Exception:
        pass
    return "(none)"


def _room_phase(room, doc):
    try:
        p = room.get_Parameter(BuiltInParameter.ROOM_PHASE)
        if p is not None:
            pid = p.AsElementId()
            if pid and pid != ElementId.InvalidElementId:
                phase = doc.GetElement(pid)
                n = _read_name(phase)
                if n:
                    return n
    except Exception:
        pass
    return "(none)"


def _room_param_value(room, param_name):
    try:
        p = room.LookupParameter(param_name)
        if p is not None:
            v = p.AsValueString()
            if v:
                return v
            v = p.AsString()
            if v:
                return v
    except Exception:
        pass
    return ""


def _element_id_value(eid):
    try:
        return eid.Value
    except Exception:
        pass
    try:
        return eid.IntegerValue
    except Exception:
        return str(eid)


# --------------------------------------------------------------------------
# Room / type collection
# --------------------------------------------------------------------------
def _collect_rooms(doc, active_view_only, view):
    if active_view_only and view is not None:
        collector = FilteredElementCollector(doc, view.Id).OfCategory(BuiltInCategory.OST_Rooms)
    else:
        collector = FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_Rooms)
    rooms = []
    for r in collector:
        if isinstance(r, Room):
            rooms.append(r)
    return rooms


def _collect_types(doc, cls):
    result = {}
    for t in FilteredElementCollector(doc).OfClass(cls):
        n = _read_name(t)
        if n:
            result[n] = t
    return result


def _collect_floor_types(doc):
    return _collect_types(doc, FloorType)


def _collect_wall_types(doc):
    return _collect_types(doc, WallType)


def _collect_ceiling_types(doc):
    return _collect_types(doc, CeilingType)


# --------------------------------------------------------------------------
# Boundary geometry
# --------------------------------------------------------------------------
def _get_boundary_curve_arrays(room):
    """Returns a list of loops; each loop is a list of (Curve, ElementId)
    tuples straight from GetBoundarySegments (ElementId is whatever bounds
    that segment - a Wall, a room separation line, or another room)."""
    opts = SpatialElementBoundaryOptions()
    try:
        opts.SpatialElementBoundaryLocation = SpatialElementBoundaryLocation.Finish
    except Exception:
        pass
    try:
        raw_loops = room.GetBoundarySegments(opts)
    except Exception:
        return []
    loops = []
    for raw_loop in raw_loops:
        loop = []
        for seg in raw_loop:
            try:
                curve = seg.GetCurve()
            except Exception:
                continue
            if curve is None:
                continue
            try:
                eid = seg.ElementId
            except Exception:
                eid = ElementId.InvalidElementId
            loop.append((curve, eid))
        if loop:
            loops.append(loop)
    return loops


def _build_curve_loops(loops_raw):
    curve_loops = []
    for loop in loops_raw:
        cl = CurveLoop()
        for curve, _eid in loop:
            cl.Append(curve)
        curve_loops.append(cl)
    return curve_loops


def _bbox_xy(loops):
    xs = []
    ys = []
    for loop in loops:
        for curve, _eid in loop:
            p0 = curve.GetEndPoint(0)
            xs.append(p0.X)
            ys.append(p0.Y)
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _pick_outer_loop(loops):
    best = None
    best_area = -1.0
    for loop in loops:
        bbox = _bbox_xy([loop])
        if bbox is None:
            continue
        area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
        if area > best_area:
            best_area = area
            best = loop
    return best


def _polygon_centroid(loop):
    pts = [curve.GetEndPoint(0) for curve, _eid in loop]
    if not pts:
        return None
    cx = sum(p.X for p in pts) / len(pts)
    cy = sum(p.Y for p in pts) / len(pts)
    return XYZ(cx, cy, pts[0].Z)


def _bbox_overlap_fraction(a, b):
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ox0 = max(ax0, bx0)
    oy0 = max(ay0, by0)
    ox1 = min(ax1, bx1)
    oy1 = min(ay1, by1)
    if ox1 <= ox0 or oy1 <= oy0:
        return 0.0
    overlap_area = (ox1 - ox0) * (oy1 - oy0)
    a_area = (ax1 - ax0) * (ay1 - ay0)
    b_area = (bx1 - bx0) * (by1 - by0)
    smaller = min(a_area, b_area)
    if smaller <= 0:
        return 0.0
    return overlap_area / smaller


def _detect_possible_overlaps(rows):
    """Approximate (bounding-box) overlap check, same level - a cheap warning
    signal, not an exact boundary-vs-boundary boolean test."""
    warnings = []
    boxes = []
    for r in rows:
        loops = _get_boundary_curve_arrays(r.room)
        outer = _pick_outer_loop(loops)
        bbox = _bbox_xy([outer]) if outer else None
        if bbox is not None:
            boxes.append((r, bbox))
    n = len(boxes)
    for i in range(n):
        for j in range(i + 1, n):
            r1, b1 = boxes[i]
            r2, b2 = boxes[j]
            if r1.level_name != r2.level_name:
                continue
            frac = _bbox_overlap_fraction(b1, b2)
            if frac > 0.3:
                warnings.append(
                    "Possible boundary overlap: Room {0} ({1}) and Room {2} ({3}) on {4}".format(
                        r1.number, r1.name, r2.number, r2.name, r1.level_name))
    return warnings


# --------------------------------------------------------------------------
# Wall Finish mitering (offset each wall-bounded straight edge inward,
# intersect adjacent offset edges to get a mitered corner point)
# --------------------------------------------------------------------------
def _offset_line_toward_point(line, distance, toward_point):
    p0 = line.GetEndPoint(0)
    p1 = line.GetEndPoint(1)
    direction = (p1 - p0).Normalize()
    perp = XYZ(-direction.Y, direction.X, 0.0)
    midpoint = (p0 + p1) * 0.5
    to_target = toward_point - midpoint
    if perp.DotProduct(to_target) < 0:
        perp = perp.Negate()
    offset_vec = perp * distance
    return Line.CreateBound(p0 + offset_vec, p1 + offset_vec)


def _line_intersection_xy(p1, d1, p2, d2):
    denom = d1.X * d2.Y - d1.Y * d2.X
    if abs(denom) < 1e-9:
        return None
    diff = p2 - p1
    t = (diff.X * d2.Y - diff.Y * d2.X) / denom
    return p1 + d1 * t


def _build_wall_finish_curves(loop, doc, centroid, half_thickness):
    """Returns a list of (start_xyz, end_xyz, original_segment_index) - one
    per wall-bounded straight boundary segment, with corners mitered against
    eligible neighbors (both must be straight and wall-bounded)."""
    n = len(loop)
    eligible = [False] * n
    offset_lines = [None] * n
    for i, (curve, eid) in enumerate(loop):
        is_line = isinstance(curve, Line)
        wall = None
        if eid is not None and eid != ElementId.InvalidElementId:
            try:
                elem = doc.GetElement(eid)
                if isinstance(elem, Wall):
                    wall = elem
            except Exception:
                wall = None
        if is_line and wall is not None:
            eligible[i] = True
            offset_lines[i] = _offset_line_toward_point(curve, half_thickness, centroid)

    results = []
    for i in range(n):
        if not eligible[i]:
            continue
        line = offset_lines[i]
        p0 = line.GetEndPoint(0)
        p1 = line.GetEndPoint(1)
        d = (p1 - p0).Normalize()

        prev_i = (i - 1) % n
        if n > 1 and eligible[prev_i]:
            prev_line = offset_lines[prev_i]
            pp0 = prev_line.GetEndPoint(0)
            pd = (prev_line.GetEndPoint(1) - pp0).Normalize()
            inter = _line_intersection_xy(p0, d, pp0, pd)
            if inter is not None:
                p0 = inter

        next_i = (i + 1) % n
        if n > 1 and eligible[next_i]:
            next_line = offset_lines[next_i]
            np0 = next_line.GetEndPoint(0)
            nd = (next_line.GetEndPoint(1) - np0).Normalize()
            inter = _line_intersection_xy(p0, d, np0, nd)
            if inter is not None:
                p1 = inter

        if p0.DistanceTo(p1) < 0.01:
            continue
        results.append((p0, p1, i))
    return results


def _room_height_internal(room, default_height):
    try:
        p = room.get_Parameter(BuiltInParameter.ROOM_HEIGHT)
        if p is not None:
            v = p.AsDouble()
            if v > 0:
                return v
    except Exception:
        pass
    return default_height


def _level_above(doc, level):
    levels = sorted(FilteredElementCollector(doc).OfClass(Level), key=lambda l: l.Elevation)
    for l in levels:
        if l.Elevation > level.Elevation + 1e-6:
            return l
    return None


# --------------------------------------------------------------------------
# Duplicate tagging (Comments parameter)
# --------------------------------------------------------------------------
def _tag_value(room_id, kind, segment_index=None):
    if segment_index is None:
        return "{0}Room:{1}|Type:{2}".format(_TAG_PREFIX, _element_id_value(room_id), kind)
    return "{0}Room:{1}|Type:{2}|Segment:{3}".format(
        _TAG_PREFIX, _element_id_value(room_id), kind, segment_index)


def _tag_element(doc, elem, room_id, kind, segment_index=None):
    try:
        p = elem.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
        if p is not None and not p.IsReadOnly:
            p.Set(_tag_value(room_id, kind, segment_index))
    except Exception:
        pass


def _find_existing_by_tag(doc, category, room_id, kind):
    tag_prefix = "{0}Room:{1}|Type:{2}".format(_TAG_PREFIX, _element_id_value(room_id), kind)
    found = []
    for e in FilteredElementCollector(doc).OfCategory(category).WhereElementIsNotElementType():
        try:
            p = e.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
            if p is not None:
                val = p.AsString()
                if val and val.startswith(tag_prefix):
                    found.append(e)
        except Exception:
            continue
    return found


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------
class RoomRow(object):
    def __init__(self, room, doc):
        self.room = room
        self.selected = False
        self.number = _read_room_number(room)
        self.name = _read_room_name(room)
        level = None
        try:
            level = room.Level
        except Exception:
            level = None
        self.level = level
        self.level_name = _read_name(level) or "(no level)"
        try:
            area_internal = room.Area
        except Exception:
            area_internal = 0.0
        self.area_internal = area_internal
        self.area_text = _format_area(doc, area_internal)
        self.floor_type_name = "(None)"
        self.wall_type_name = "(None)"
        self.ceiling_type_name = "(None)"
        self.status_text = "Pending"


# --------------------------------------------------------------------------
# Window
# --------------------------------------------------------------------------
class DeeFinisherWindow(forms.WPFWindow):
    def __init__(self, xaml_file, doc):
        forms.WPFWindow.__init__(self, xaml_file)
        self.doc = doc
        self._rows = []
        self._floor_types = {}
        self._wall_types = {}
        self._ceiling_types = {}
        self._custom_group_param = ""
        self._copy_from_lookup = {}

        self.group_by_cb.ItemsSource = ["(None)", "Level", "Department", "Phase", "Room Parameter..."]
        self.group_by_cb.SelectedIndex = 0
        self.duplicate_mode_cb.ItemsSource = ["Skip Existing", "Replace Existing", "Update Existing", "Ask Every Time"]
        self.duplicate_mode_cb.SelectedIndex = 0

        self._load_types()
        self._refresh_type_dropdowns()

        default_height_internal = UnitUtils.ConvertToInternalUnits(3.0, UnitTypeId.Meters)
        self.fixed_height_tb.Text = "{0:.0f}".format(_internal_to_display(self.doc, default_height_internal))
        self.fixed_height_unit_tb.Text = _unit_abbreviation(self.doc)
        self._update_wall_height_controls()

        self.wizard_tabs.SelectedIndex = 0

    # ---- setup ----
    def _load_types(self):
        self._floor_types = _collect_floor_types(self.doc)
        self._wall_types = _collect_wall_types(self.doc)
        self._ceiling_types = _collect_ceiling_types(self.doc)

    def _refresh_type_dropdowns(self):
        floor_names = ["(None)"] + sorted(self._floor_types.keys())
        wall_names = ["(None)"] + sorted(self._wall_types.keys())
        ceiling_names = ["(None)"] + sorted(self._ceiling_types.keys())
        self.floor_type_col.ItemsSource = floor_names
        self.wall_type_col.ItemsSource = wall_names
        self.ceiling_type_col.ItemsSource = ceiling_names
        self.global_floor_cb.ItemsSource = floor_names
        self.global_wall_cb.ItemsSource = wall_names
        self.global_ceiling_cb.ItemsSource = ceiling_names
        self.global_floor_cb.SelectedIndex = 0
        self.global_wall_cb.SelectedIndex = 0
        self.global_ceiling_cb.SelectedIndex = 0

    # ---- Step 1: scan / search / filter / group ----
    def scan_click(self, sender, args):
        self._scan()

    def refresh_scan_click(self, sender, args):
        self._scan()

    def _scan(self):
        active_view_only = bool(self.scan_active_rb.IsChecked)
        view = self.doc.ActiveView if active_view_only else None
        try:
            rooms = _collect_rooms(self.doc, active_view_only, view)
        except Exception as e:
            forms.alert("Could not scan rooms: {0}".format(e))
            return

        neglect_zero_area = bool(self.neglect_zero_area_cb.IsChecked)
        neglected_count = 0
        if neglect_zero_area:
            kept = []
            for r in rooms:
                try:
                    area = r.Area
                except Exception:
                    area = 0.0
                if area > 0:
                    kept.append(r)
                else:
                    neglected_count += 1
            rooms = kept

        self._rows = [RoomRow(r, self.doc) for r in rooms]
        self._refresh_level_filter()
        self._refresh_copy_from()
        self._apply_filters()
        count_text = "{0} room(s) scanned".format(len(self._rows))
        if neglected_count:
            count_text += " ({0} zero-area room(s) neglected)".format(neglected_count)
        self.room_count_tb.Text = count_text

    def _refresh_level_filter(self):
        names = sorted(set(r.level_name for r in self._rows))
        self.level_filter_cb.ItemsSource = ["(All Levels)"] + names
        self.level_filter_cb.SelectedIndex = 0

    def _refresh_copy_from(self):
        labels = ["{0} - {1}".format(r.number, r.name) for r in self._rows]
        self.copy_from_cb.ItemsSource = labels
        self._copy_from_lookup = dict(zip(labels, self._rows))

    def search_changed(self, sender, args):
        self._apply_filters()

    def level_filter_changed(self, sender, args):
        self._apply_filters()

    def group_by_changed(self, sender, args):
        if self.group_by_cb.SelectedItem == "Room Parameter...":
            name = forms.ask_for_string(
                default=self._custom_group_param,
                prompt="Room parameter name to group by:",
                title="DeeFinisher - Group By Parameter")
            if not name:
                self.group_by_cb.SelectedIndex = 0
                return
            self._custom_group_param = name
        self._apply_filters()

    def _apply_filters(self):
        text = (self.search_tb.Text or "").strip().lower()
        level_filter = self.level_filter_cb.SelectedItem
        rows = list(self._rows)
        if text:
            rows = [r for r in rows if text in (r.number or "").lower() or text in (r.name or "").lower()]
        if level_filter and level_filter != "(All Levels)":
            rows = [r for r in rows if r.level_name == level_filter]

        group_by = self.group_by_cb.SelectedItem
        if group_by == "Level":
            rows.sort(key=lambda r: (r.level_name, r.number))
        elif group_by == "Department":
            rows.sort(key=lambda r: (_room_dept(r.room), r.number))
        elif group_by == "Phase":
            rows.sort(key=lambda r: (_room_phase(r.room, self.doc), r.number))
        elif group_by == "Room Parameter..." and self._custom_group_param:
            rows.sort(key=lambda r: (_room_param_value(r.room, self._custom_group_param), r.number))

        self.rooms_grid.ItemsSource = None
        self.rooms_grid.ItemsSource = rows

    def rooms_row_edit_ending(self, sender, args):
        pass

    def _get_selected_rows(self):
        return [r for r in self._rows if r.selected]

    def _refresh_grid_view(self):
        items = self.rooms_grid.ItemsSource
        self.rooms_grid.ItemsSource = None
        self.rooms_grid.ItemsSource = items

    def select_all_click(self, sender, args):
        for r in self.rooms_grid.ItemsSource:
            r.selected = True
        self._refresh_grid_view()

    def deselect_all_click(self, sender, args):
        for r in self.rooms_grid.ItemsSource:
            r.selected = False
        self._refresh_grid_view()

    def select_highlighted_click(self, sender, args):
        highlighted = list(self.rooms_grid.SelectedItems)
        if not highlighted:
            forms.alert("Click a row (Shift-click or Ctrl-click for more) to highlight rows first.")
            return
        for r in highlighted:
            r.selected = True
        self._refresh_grid_view()

    def deselect_highlighted_click(self, sender, args):
        highlighted = list(self.rooms_grid.SelectedItems)
        if not highlighted:
            forms.alert("Click a row (Shift-click or Ctrl-click for more) to highlight rows first.")
            return
        for r in highlighted:
            r.selected = False
        self._refresh_grid_view()

    # ---- Step 2: global assignment / options ----
    def _apply_finish_to_rows(self, rows, kind, type_name):
        if not rows:
            forms.alert("No rooms selected (check the Select column in Step 1 first).")
            return
        type_name = type_name or "(None)"
        for r in rows:
            if kind == "floor":
                r.floor_type_name = type_name
            elif kind == "wall":
                r.wall_type_name = type_name
            else:
                r.ceiling_type_name = type_name
        self._refresh_grid_view()

    def apply_floor_selected_click(self, sender, args):
        self._apply_finish_to_rows(self._get_selected_rows(), "floor", self.global_floor_cb.SelectedItem)

    def apply_floor_all_click(self, sender, args):
        self._apply_finish_to_rows(self._rows, "floor", self.global_floor_cb.SelectedItem)

    def apply_wall_selected_click(self, sender, args):
        self._apply_finish_to_rows(self._get_selected_rows(), "wall", self.global_wall_cb.SelectedItem)

    def apply_wall_all_click(self, sender, args):
        self._apply_finish_to_rows(self._rows, "wall", self.global_wall_cb.SelectedItem)

    def apply_ceiling_selected_click(self, sender, args):
        self._apply_finish_to_rows(self._get_selected_rows(), "ceiling", self.global_ceiling_cb.SelectedItem)

    def apply_ceiling_all_click(self, sender, args):
        self._apply_finish_to_rows(self._rows, "ceiling", self.global_ceiling_cb.SelectedItem)

    def clear_selected_click(self, sender, args):
        rows = self._get_selected_rows()
        if not rows:
            forms.alert("No rooms selected.")
            return
        for r in rows:
            r.floor_type_name = "(None)"
            r.wall_type_name = "(None)"
            r.ceiling_type_name = "(None)"
        self._refresh_grid_view()

    def copy_to_selected_click(self, sender, args):
        label = self.copy_from_cb.SelectedItem
        if not label:
            forms.alert("Pick a room to copy assignments from.")
            return
        src = self._copy_from_lookup.get(label)
        targets = self._get_selected_rows()
        if src is None or not targets:
            forms.alert("Pick a source room and select at least one target room.")
            return
        for r in targets:
            r.floor_type_name = src.floor_type_name
            r.wall_type_name = src.wall_type_name
            r.ceiling_type_name = src.ceiling_type_name
        self._refresh_grid_view()

    def wall_height_mode_changed(self, sender, args):
        self._update_wall_height_controls()

    def _update_wall_height_controls(self):
        fixed_mode = bool(self.height_fixed_rb.IsChecked)
        self.fixed_height_tb.IsEnabled = fixed_mode

    def _safe_float(self, text, default):
        try:
            return float(text)
        except Exception:
            return default

    # ---- Step 3: preview / generate / export ----
    def refresh_preview_click(self, sender, args):
        self._refresh_preview()

    def _refresh_preview(self):
        rows = self._get_selected_rows()
        total_rooms = len(rows)
        floors_to_create = sum(1 for r in rows if r.floor_type_name != "(None)")
        walls_to_create = sum(1 for r in rows if r.wall_type_name != "(None)")
        ceilings_to_create = sum(1 for r in rows if r.ceiling_type_name != "(None)")

        existing_count = 0
        warnings = []
        for r in rows:
            loops = _get_boundary_curve_arrays(r.room)
            if r.room.Area <= 0 or not loops:
                warnings.append("Room {0} ({1}): no valid boundary - will be skipped".format(r.number, r.name))
                continue
            if r.floor_type_name != "(None)" and _find_existing_by_tag(
                    self.doc, BuiltInCategory.OST_Floors, r.room.Id, "Floor"):
                existing_count += 1
            if r.ceiling_type_name != "(None)" and _find_existing_by_tag(
                    self.doc, BuiltInCategory.OST_Ceilings, r.room.Id, "Ceiling"):
                existing_count += 1
            if r.wall_type_name != "(None)" and _find_existing_by_tag(
                    self.doc, BuiltInCategory.OST_Walls, r.room.Id, "WallFinish"):
                existing_count += 1

        warnings.extend(_detect_possible_overlaps(rows))

        missing_types = []
        if any(r.floor_type_name not in ("(None)",) and r.floor_type_name not in self._floor_types for r in rows):
            missing_types.append("Floor")
        if any(r.wall_type_name not in ("(None)",) and r.wall_type_name not in self._wall_types for r in rows):
            missing_types.append("Wall")
        if any(r.ceiling_type_name not in ("(None)",) and r.ceiling_type_name not in self._ceiling_types for r in rows):
            missing_types.append("Ceiling")

        lines = [
            "Total Rooms Selected: {0}".format(total_rooms),
            "Floors to Create: {0}".format(floors_to_create),
            "Walls to Create (rooms with a Wall Finish assigned): {0}".format(walls_to_create),
            "Ceilings to Create: {0}".format(ceilings_to_create),
            "Existing DeeFinisher Elements Found: {0} (mode: {1})".format(
                existing_count, self.duplicate_mode_cb.SelectedItem),
            "",
            "Missing Finish Types: {0}".format(", ".join(missing_types) if missing_types else "None"),
            "Warnings / Conflicts: {0}".format(len(warnings)),
        ]
        self.summary_tb.Text = "\n".join(lines)
        self.warnings_lb.ItemsSource = warnings

    def _room_label(self, row):
        return "{0} - {1}".format(row.number, row.name)

    def generate_click(self, sender, args):
        rows = self._get_selected_rows()
        if not rows:
            forms.alert("No rooms selected. Go back to Step 1 and select at least one room.")
            return
        if not forms.alert(
                "Generate finishes for {0} selected room(s)? This creates/modifies "
                "model elements in one Transaction.".format(len(rows)),
                title="DeeFinisher", yes=True, no=True):
            return

        dup_mode = self.duplicate_mode_cb.SelectedItem or "Skip Existing"
        if bool(self.height_fixed_rb.IsChecked):
            height_mode = "fixed"
        elif bool(self.height_room_rb.IsChecked):
            height_mode = "room"
        else:
            height_mode = "level_above"
        fixed_height_internal = _display_to_internal(
            self.doc, self._safe_float(self.fixed_height_tb.Text, 3000.0))

        results = []
        total = len(rows)
        with forms.ProgressBar(title="DeeFinisher — generating finishes...", cancellable=True) as pb:
            t = Transaction(self.doc, "DeeFinisher - Generate Room Finishes")
            t.Start()
            try:
                for i, row in enumerate(rows):
                    if pb.cancelled:
                        results.append((self._room_label(row), "-", None, "Cancelled"))
                        break
                    pb.update_progress(i, total)
                    self._generate_for_room(row, dup_mode, height_mode, fixed_height_internal, results)
                t.Commit()
            except Exception as e:
                t.RollBack()
                forms.alert("Generation aborted: {0}".format(e))
                return

        self._render_report(results)
        self._refresh_grid_view()

    def _generate_for_room(self, row, dup_mode, height_mode, fixed_height_internal, results):
        room = row.room
        label = self._room_label(row)

        try:
            area = room.Area
        except Exception:
            area = 0.0
        loops = _get_boundary_curve_arrays(room)
        if area <= 0 or not loops:
            row.status_text = "Skipped - no valid boundary"
            results.append((label, "-", None, "No valid room boundary"))
            return

        level = row.level
        if level is None:
            row.status_text = "Skipped - no level"
            results.append((label, "-", None, "Room has no Level"))
            return

        centroid = None
        try:
            loc = room.Location
            if loc is not None:
                centroid = loc.Point
        except Exception:
            centroid = None
        outer_loop = _pick_outer_loop(loops)
        if centroid is None and outer_loop:
            centroid = _polygon_centroid(outer_loop)

        status_parts = []

        if row.floor_type_name != "(None)":
            ok, detail = self._generate_floor(room, row, level, loops, dup_mode)
            results.append((label, "Floor", ok, detail))
            status_parts.append("Floor: " + detail)

        if row.ceiling_type_name != "(None)":
            ok, detail = self._generate_ceiling(room, row, level, loops, dup_mode)
            results.append((label, "Ceiling", ok, detail))
            status_parts.append("Ceiling: " + detail)

        if row.wall_type_name != "(None)":
            ok, detail = self._generate_wall_finish(
                room, row, level, outer_loop, centroid, dup_mode, height_mode, fixed_height_internal)
            results.append((label, "WallFinish", ok, detail))
            status_parts.append("Wall: " + detail)

        row.status_text = "; ".join(status_parts) if status_parts else "No finishes assigned"

    def _handle_existing(self, existing, dup_mode, kind_label, room_label, new_type_id):
        """Common Skip/Replace/Update/Ask handling shared by Floor/Ceiling/Wall.
        Returns ('done', ok, detail) if fully handled (skip or update), or
        ('replace', None, None) if callers should delete `existing` and fall
        through to normal creation."""
        mode = dup_mode
        if mode == "Ask Every Time":
            choice = forms.SelectFromList.show(
                ["Skip", "Replace", "Update"], multiselect=False,
                title="DeeFinisher - {0} already exists for Room {1}".format(kind_label, room_label))
            if not choice or choice == "Skip":
                return ("done", None, "Skipped by user")
            mode = choice + " Existing"

        if mode == "Skip Existing":
            return ("done", None, "Skipped - {0} already exists".format(kind_label))
        if mode == "Update Existing":
            try:
                for e in existing:
                    e.ChangeTypeId(new_type_id)
                return ("done", True, "Updated {0} existing {1} element(s) type".format(len(existing), kind_label))
            except Exception as e:
                return ("done", False, "FAILED updating existing {0}: {1}".format(kind_label, e))
        # Replace Existing
        for e in existing:
            try:
                self.doc.Delete(e.Id)
            except Exception:
                pass
        return ("replace", None, None)

    def _generate_floor(self, room, row, level, loops, dup_mode):
        floor_type = self._floor_types.get(row.floor_type_name)
        if floor_type is None:
            return False, "Floor Type '{0}' not found".format(row.floor_type_name)

        existing = _find_existing_by_tag(self.doc, BuiltInCategory.OST_Floors, room.Id, "Floor")
        if existing:
            action, ok, detail = self._handle_existing(existing, dup_mode, "Floor", row.number, floor_type.Id)
            if action == "done":
                return ok, detail

        try:
            curve_loops = _build_curve_loops(loops)
            new_floor = Floor.Create(self.doc, List[CurveLoop](curve_loops), floor_type.Id, level.Id)
            _tag_element(self.doc, new_floor, room.Id, "Floor")
            return True, "Floor created"
        except Exception as e:
            return False, "FAILED: {0}".format(e)

    def _generate_ceiling(self, room, row, level, loops, dup_mode):
        ceiling_type = self._ceiling_types.get(row.ceiling_type_name)
        if ceiling_type is None:
            return False, "Ceiling Type '{0}' not found".format(row.ceiling_type_name)

        existing = _find_existing_by_tag(self.doc, BuiltInCategory.OST_Ceilings, room.Id, "Ceiling")
        if existing:
            action, ok, detail = self._handle_existing(existing, dup_mode, "Ceiling", row.number, ceiling_type.Id)
            if action == "done":
                return ok, detail

        try:
            curve_loops = _build_curve_loops(loops)
            new_ceiling = Ceiling.Create(self.doc, List[CurveLoop](curve_loops), ceiling_type.Id, level.Id)
            _tag_element(self.doc, new_ceiling, room.Id, "Ceiling")
            return True, "Ceiling created"
        except Exception as e:
            return False, "FAILED: {0}".format(e)

    def _generate_wall_finish(self, room, row, level, outer_loop, centroid, dup_mode, height_mode, fixed_height_internal):
        wall_type = self._wall_types.get(row.wall_type_name)
        if wall_type is None:
            return False, "Wall Type '{0}' not found".format(row.wall_type_name)

        existing = _find_existing_by_tag(self.doc, BuiltInCategory.OST_Walls, room.Id, "WallFinish")
        if existing:
            action, ok, detail = self._handle_existing(existing, dup_mode, "Wall Finish", row.number, wall_type.Id)
            if action == "done":
                return ok, detail

        if not outer_loop:
            return False, "No outer boundary loop found"
        if centroid is None:
            centroid = _polygon_centroid(outer_loop)
        if centroid is None:
            return False, "Could not determine an inside-the-room reference point"

        try:
            half_thickness = wall_type.Width / 2.0
        except Exception:
            half_thickness = _display_to_internal(self.doc, 50.0) / 2.0

        try:
            curves_with_index = _build_wall_finish_curves(outer_loop, self.doc, centroid, half_thickness)
        except Exception as e:
            return False, "FAILED computing wall offsets: {0}".format(e)

        if not curves_with_index:
            return False, ("No wall-bounded straight boundary segments found (room may be "
                            "bounded only by separation lines or curved walls)")

        if height_mode == "room":
            height = _room_height_internal(room, fixed_height_internal)
        elif height_mode == "level_above":
            above = _level_above(self.doc, level)
            height = (above.Elevation - level.Elevation) if above is not None else fixed_height_internal
        else:
            height = fixed_height_internal
        if height <= 0.01:
            height = fixed_height_internal

        created = []
        errors = []
        for p0, p1, idx in curves_with_index:
            try:
                line = Line.CreateBound(p0, p1)
                wall = Wall.Create(self.doc, line, wall_type.Id, level.Id, height, 0.0, False, False)
                _tag_element(self.doc, wall, room.Id, "WallFinish", idx)
                created.append(wall)
            except Exception as e:
                errors.append("segment {0}: {1}".format(idx, e))

        if created:
            try:
                self.doc.Regenerate()
            except Exception:
                pass
            n = len(created)
            for i in range(n):
                j = (i + 1) % n
                if i == j:
                    continue
                try:
                    if not JoinGeometryUtils.AreElementsJoined(self.doc, created[i], created[j]):
                        JoinGeometryUtils.JoinGeometry(self.doc, created[i], created[j])
                except Exception:
                    pass

        if not created:
            return False, "FAILED: no wall finish segments created ({0})".format("; ".join(errors))

        detail = "Created {0} Wall Finish segment(s), mitered corners, auto-joined".format(len(created))
        if errors:
            detail += " ({0} segment(s) failed: {1})".format(len(errors), "; ".join(errors))
        return True, detail

    def _render_report(self, results):
        html = '<h2 style="font-family:sans-serif;color:#ddd;">DeeFinisher Results</h2>'
        for label, kind, ok, detail in results:
            bg = "#2e7d32" if ok else ("#455a64" if ok is None else "#c62828")
            icon = "&#10003;" if ok else ("&#9888;" if ok is None else "&#10007;")
            html += (
                '<div style="padding:6px 12px;margin:2px 0;background:{0};color:#fff;'
                'border-radius:4px;font-family:monospace;font-size:12px;">'
                '<b>{1}</b> [{2}] &nbsp;{3}&nbsp; {4}'
                '</div>'.format(bg, label, kind, icon, detail))
        ok_count = sum(1 for r in results if r[2] is True)
        fail_count = sum(1 for r in results if r[2] is False)
        skip_count = sum(1 for r in results if r[2] is None)
        html += (
            '<hr><b style="font-family:sans-serif;">{0} succeeded, {1} failed, {2} skipped '
            '(out of {3} finish operation(s)).</b>'.format(ok_count, fail_count, skip_count, len(results)))
        output.print_html(html)

    def export_csv_click(self, sender, args):
        if not self._rows:
            forms.alert("Nothing to export - scan rooms first.")
            return
        dlg = SaveFileDialog()
        dlg.Filter = "CSV (*.csv)|*.csv"
        dlg.FileName = "DeeFinisher_Rooms.csv"
        if dlg.ShowDialog() != DialogResult.OK:
            return
        try:
            with open(dlg.FileName, "wb") as f:
                writer = csv.writer(f)
                writer.writerow(["Room Number", "Room Name", "Level", "Area",
                                  "Floor Finish", "Wall Finish", "Ceiling Finish", "Status"])
                for r in self._rows:
                    writer.writerow([r.number, r.name, r.level_name, r.area_text,
                                      r.floor_type_name, r.wall_type_name, r.ceiling_type_name, r.status_text])
        except Exception as e:
            forms.alert("Could not export: {0}".format(e))
            return
        MessageBox.Show("Exported {0} row(s) to:\n{1}".format(len(self._rows), dlg.FileName), "DeeFinisher")

    # ---- nav ----
    def back_click(self, sender, args):
        if self.wizard_tabs.SelectedIndex > 0:
            self.wizard_tabs.SelectedIndex -= 1

    def next_click(self, sender, args):
        if self.wizard_tabs.SelectedIndex < self.wizard_tabs.Items.Count - 1:
            self.wizard_tabs.SelectedIndex += 1
            if self.wizard_tabs.SelectedIndex == 2:
                self._refresh_preview()

    def close_click(self, sender, args):
        self.Close()


def main():
    doc = __revit__.ActiveUIDocument.Document
    window = DeeFinisherWindow(_XAML_FILE, doc)
    window.show(modal=True)


main()
