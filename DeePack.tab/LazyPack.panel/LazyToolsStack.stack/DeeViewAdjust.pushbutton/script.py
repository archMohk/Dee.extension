# -*- coding: utf-8 -*-
"""
DeeViewAdjust
Scans the project's Rooms, lets you check one or more, and reshapes the
ACTIVE VIEW's Crop Region to follow the EXACT combined boundary of the
checked room(s) - not a rectangular box - with an optional uniform
outward offset from the room boundary. PLAN VIEWS ONLY (FloorPlan/
CeilingPlan/AreaPlan/StructuralPlan) - a room boundary is a horizontal-
plane shape, and Revit's crop-shape API requires the crop loop's plane
to be parallel to the view's own plane.

--------------------------------------------------------------------
Revit API facts relied on here (WebFetch-verified against
revitapidocs.com and Jeremy Tammik's Autodesk-affiliated technical
blog before writing, not guessed)
--------------------------------------------------------------------
- view.GetCropRegionShapeManager().SetCropShape(CurveLoop) accepts
  EXACTLY ONE CurveLoop - there is no overload for multiple disjoint
  loops, and Split Region only splits a RECTANGULAR crop into
  rectangular strips (doesn't help combine several room outlines). If
  the checked rooms don't end up forming one connected/overlapping
  outline (even after the offset), there is no way to represent them
  as a single crop shape - see "Disjoint rooms" below for what this
  module does instead of silently producing a wrong result.
- The loop must be planar, non-self-intersecting, made of straight
  Line segments only (arcs are rejected - tessellated to line chords
  here before ever reaching SetCropShape), in a plane parallel to the
  view's own plane. crsm.IsCropRegionShapeValid(loop) validates before
  committing; crsm.RemoveCropRegionShape() resets to rectangular.
- Coordinate system: real MODEL (project) XYZ, no transform needed -
  confirmed via Jeremy Tammik's own CropViewToRoom sample
  (github.com/jeremytammik/CropViewToRoom), which passes raw room-
  boundary curves straight into the crop CurveLoop with zero
  transform. (pyRevit's own bundled "Set Crop Region To Selected
  Shape" tool DOES transform its input - but only because that tool's
  polygon is drawn as detail lines on a SHEET and must be mapped into
  the viewport's model view; not applicable here, since this tool
  reads room geometry directly in the active view's own model space.)

--------------------------------------------------------------------
Why the mitering is hand-rolled instead of DB.CurveLoop.CreateViaOffset
--------------------------------------------------------------------
CreateViaOffset exists (Revit 2015+) but is documented - via Jeremy
Tammik's own team's findings ("CreateViaOffset and Room Outer
Outline") - to throw InvalidOperationException ("Curve loop couldn't
be properly trimmed") on real concave/L-shaped boundaries with small
segments. This tool instead adapts the proven, already-working
mitering approach from DeeFinisher.pushbutton/script.py (used there
for inward Wall Finish offsets) - offset each straight edge
perpendicular to itself, then re-miter each corner by intersecting
adjacent offset edges - just flipped to offset OUTWARD, with every
edge eligible (DeeFinisher only offsets wall-bounded edges; a crop
polygon needs every edge, including room-separation-line-bounded
ones, to stay closed).

--------------------------------------------------------------------
Disjoint rooms - what happens when checked rooms don't form one shape
--------------------------------------------------------------------
Since SetCropShape only accepts one CurveLoop, if the checked rooms
(even after the offset) don't overlap/touch, there is no way to
represent them as a single crop shape. This tool does NOT silently
substitute a rectangular crop in that case - it aborts the shaped-crop
path, alerts naming the situation, then offers an explicit Yes/No:
apply a rectangular bounding-box crop covering all checked rooms +
offset instead (Yes), or cancel with nothing changed (No). A tool
whose whole point is "follow the exact shape" quietly becoming a
rectangle on failure would look plausible but silently mean something
different from what was asked - matches this extension's consistent
stance elsewhere (fill_conversion.py reports every skip individually
rather than guessing).

--------------------------------------------------------------------
NEEDS LIVE-REVIT VERIFICATION (flagged, not silently assumed correct -
same policy as this extension's other geometry-heavy tools)
--------------------------------------------------------------------
- Whether a Boolean union of two fully DISJOINT solids throws vs.
  silently returns a multi-shell Solid - defended both ways (each
  pairwise union in its own try/except, AND a post-union face-count
  check), not yet exercised live.
- Whether Face.GetEdgesAsCurveLoops()[first] reliably stays the OUTER
  loop for a face produced BY a Boolean union specifically (documented
  for a face's own loop list in general, not exercised locally post-
  union) - defended here by always picking the loop with the largest
  true polygon area, not blindly trusting index [0].
- Winding-order sensitivity of GeometryCreationUtilities.
  CreateExtrusionGeometry relative to XYZ.BasisZ.
- Whether outward mitering at reflex/acute corners on real L/T/U-
  shaped rooms can self-intersect at larger offsets - guarded by
  IsCropRegionShapeValid before commit with a clear failure message,
  not fully ruled out without live testing across a range of offsets.
- Explicitly LOWER risk: the single-room path (tessellate -> pick
  outer loop -> offset -> SetCropShape, no extrusion/union at all)
  reuses the same proven-live mitering math as DeeFinisher, just
  outward instead of inward.
"""
import os
import time

from pyrevit import forms
from pyrevit import script as pyrevit_script
import dee_branding
import utils

from Autodesk.Revit.DB import (
    FilteredElementCollector, BuiltInCategory, BuiltInParameter, ElementId,
    Transaction, SpatialElementBoundaryOptions, SpatialElementBoundaryLocation,
    CurveLoop, Line, XYZ, ViewPlan, BoundingBoxXYZ,
    GeometryCreationUtilities, BooleanOperationsUtils, BooleanOperationsType, PlanarFace,
)
from Autodesk.Revit.DB.Architecture import Room
from System.Collections.Generic import List

output = pyrevit_script.get_output()

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")

_EXTRUSION_HEIGHT = 1.0  # feet - arbitrary, only used as scratch geometry for the union


def _read_name(element):
    if element is None:
        return None
    try:
        n = element.Name
        return n if n else None
    except Exception:
        return None


def _read_room_name(room):
    """Room.Name is unreliable in IronPython for some rooms (returns
    empty/throws rather than the real name - not specific to any one
    language, but confirmed live to affect Arabic-named rooms in this
    project) - falls back to the ROOM_NAME parameter directly, same
    proven pattern already established in DeeFinisher.pushbutton and
    DeeCleaner.pushbutton's own _read_room_name helpers."""
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


# ==========================================================================
# Room scanning
# ==========================================================================
class RoomRow(object):
    def __init__(self, room, number, name, level_name):
        self.room = room
        self.id = room.Id
        self.checked = False
        self.number = number
        self.name = name
        self.level_name = level_name

    @property
    def area_text(self):
        try:
            p = self.room.get_Parameter(BuiltInParameter.ROOM_AREA)
            if p is not None:
                val = p.AsValueString()
                if val:
                    return val
        except Exception:
            pass
        return "(unplaced)"


def scan_rooms(doc):
    rows = []
    collector = FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_Rooms).WhereElementIsNotElementType()
    for el in collector:
        if not isinstance(el, Room):
            continue
        try:
            num_param = el.get_Parameter(BuiltInParameter.ROOM_NUMBER)
            number_text = num_param.AsString() if num_param is not None else ""
        except Exception:
            number_text = ""
        name = _read_room_name(el)
        level_name = ""
        try:
            lvl = doc.GetElement(el.LevelId)
            level_name = _read_name(lvl) or ""
        except Exception:
            pass
        rows.append(RoomRow(el, number_text or "", name, level_name))
    rows.sort(key=lambda r: (r.level_name, r.number))
    return rows


# ==========================================================================
# Boundary extraction, arc tessellation, true-area outer-loop selection
# ==========================================================================
def _get_boundary_curve_arrays(room):
    """Returns a list of loops; each loop is a list of Curves straight
    from GetBoundarySegments. Local copy of the proven
    DeeFinisher.pushbutton/script.py helper of the same name (this
    version drops the per-segment ElementId DeeFinisher keeps, since
    DeeViewAdjust has no wall-only gating to apply)."""
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
            if curve is not None:
                loop.append(curve)
        if loop:
            loops.append(loop)
    return loops


def _tessellate_loop_to_lines(loop):
    """Every curve in `loop` becomes one or more straight Lines - a
    plain Line passes through unchanged; anything else (Arc, etc.) is
    expanded via curve.Tessellate() into a chain of Line segments.
    SetCropShape rejects any non-Line curve, and a crop polygon can't
    have a gap, so (unlike DeeFinisher's wall-finish code, which just
    drops curved segments) every segment must survive here."""
    lines = []
    for curve in loop:
        if isinstance(curve, Line):
            lines.append(Line.CreateBound(curve.GetEndPoint(0), curve.GetEndPoint(1)))
            continue
        try:
            pts = list(curve.Tessellate())
        except Exception:
            continue
        for i in range(len(pts) - 1):
            p0, p1 = pts[i], pts[i + 1]
            if p0.DistanceTo(p1) < 1e-6:
                continue
            lines.append(Line.CreateBound(p0, p1))
    return lines


def _signed_area_xy(curves):
    """Shoelace formula over the ordered curve chain's start points -
    a TRUE polygon area (not a bounding-box proxy), needed because
    getting outer-vs-island wrong here would visibly crop the view to
    the wrong shape."""
    n = len(curves)
    if n < 3:
        return 0.0
    area = 0.0
    for i in range(n):
        p0 = curves[i].GetEndPoint(0)
        p1 = curves[(i + 1) % n].GetEndPoint(0)
        area += p0.X * p1.Y - p1.X * p0.Y
    return area / 2.0


def _pick_outer_loop(tessellated_loops):
    best = None
    best_area = -1.0
    for loop in tessellated_loops:
        if len(loop) < 3:
            continue
        area = abs(_signed_area_xy(loop))
        if area > best_area:
            best_area = area
            best = loop
    return best


def _flatten_to_z(lines, z):
    """Rewrites every Line's Z to a single shared constant. Needed
    because rooms are scanned project-wide, so checked rooms can come
    from different Levels - extruding each room's loop from its own
    (possibly very different) source Z would break vertical overlap
    for the solid union. The crop shape only needs a planar XY
    footprint parallel to the view's plane, not any specific Z (see
    module docstring), so flattening early removes a whole class of
    Z-drift bugs."""
    flat = []
    for line in lines:
        p0 = line.GetEndPoint(0)
        p1 = line.GetEndPoint(1)
        np0 = XYZ(p0.X, p0.Y, z)
        np1 = XYZ(p1.X, p1.Y, z)
        if np0.DistanceTo(np1) < 1e-6:
            continue
        flat.append(Line.CreateBound(np0, np1))
    return flat


# ==========================================================================
# Outward offset (adapted from DeeFinisher's proven mitering functions)
# ==========================================================================
def _polygon_centroid(lines):
    pts = [line.GetEndPoint(0) for line in lines]
    if not pts:
        return None
    cx = sum(p.X for p in pts) / len(pts)
    cy = sum(p.Y for p in pts) / len(pts)
    return XYZ(cx, cy, pts[0].Z)


def _line_intersection_xy(p1, d1, p2, d2):
    denom = d1.X * d2.Y - d1.Y * d2.X
    if abs(denom) < 1e-9:
        return None
    diff = p2 - p1
    t = (diff.X * d2.Y - diff.Y * d2.X) / denom
    return p1 + d1 * t


def _offset_line_outward(line, distance, centroid):
    p0 = line.GetEndPoint(0)
    p1 = line.GetEndPoint(1)
    direction = (p1 - p0).Normalize()
    perp = XYZ(-direction.Y, direction.X, 0.0)
    midpoint = (p0 + p1) * 0.5
    to_centroid = centroid - midpoint
    if perp.DotProduct(to_centroid) > 0:
        # perp currently points TOWARD the centroid - flip it outward
        perp = perp.Negate()
    offset_vec = perp * distance
    return Line.CreateBound(p0 + offset_vec, p1 + offset_vec)


def _build_offset_loop(lines, centroid, distance):
    """Offsets every edge of a closed Line loop outward by `distance`,
    then re-miters each corner by intersecting adjacent offset edges -
    same technique as DeeFinisher._build_wall_finish_curves, but every
    edge is eligible (not just wall-bounded ones) and the result is
    one continuous closed loop, not a filtered/disconnected list."""
    if distance <= 1e-9:
        return lines
    n = len(lines)
    offset_lines = [_offset_line_outward(line, distance, centroid) for line in lines]

    result = []
    for i in range(n):
        line = offset_lines[i]
        p0 = line.GetEndPoint(0)
        p1 = line.GetEndPoint(1)
        d = (p1 - p0).Normalize()

        prev_line = offset_lines[(i - 1) % n]
        pp0 = prev_line.GetEndPoint(0)
        pd = (prev_line.GetEndPoint(1) - pp0).Normalize()
        inter = _line_intersection_xy(p0, d, pp0, pd)
        if inter is not None:
            p0 = inter

        next_line = offset_lines[(i + 1) % n]
        np0 = next_line.GetEndPoint(0)
        nd = (next_line.GetEndPoint(1) - np0).Normalize()
        inter = _line_intersection_xy(p0, d, np0, nd)
        if inter is not None:
            p1 = inter

        if p0.DistanceTo(p1) < 1e-6:
            continue
        result.append(Line.CreateBound(p0, p1))
    return result


def _curve_loop_from_lines(lines):
    if len(lines) < 3:
        return None
    loop = CurveLoop()
    for line in lines:
        loop.Append(line)
    return loop


# ==========================================================================
# Combining multiple rooms via a Boolean solid union (Revit's own
# geometry engine correctly handles arbitrary concave-polygon unions
# this way, avoiding a hand-rolled 2D boolean algorithm)
# ==========================================================================
def _build_extrusion_solid(lines, height=_EXTRUSION_HEIGHT):
    loop = _curve_loop_from_lines(lines)
    if loop is None:
        return None
    loops = List[CurveLoop]()
    loops.Add(loop)
    try:
        return GeometryCreationUtilities.CreateExtrusionGeometry(loops, XYZ.BasisZ, height)
    except Exception:
        return None


def _union_solids(solids):
    """Pairwise-unions every solid in `solids` via Revit's own Boolean
    engine - each pair in its own try/except; any exception is treated
    as proof that pair is disjoint (see module docstring)."""
    if not solids:
        return None
    acc = solids[0]
    for nxt in solids[1:]:
        try:
            acc = BooleanOperationsUtils.ExecuteBooleanOperation(acc, nxt, BooleanOperationsType.Union)
        except Exception:
            return None
    return acc


def _extract_top_outer_loops(solid):
    """Reads back every TOP planar face of `solid` (FaceNormal.Z near
    +1, so bottom/side faces are excluded) and its largest-area edge
    loop - one result = the checked rooms fused into one connected
    shape; more than one = disjoint pieces even though the union call
    itself didn't raise."""
    if solid is None:
        return []
    results = []
    for face in solid.Faces:
        if not isinstance(face, PlanarFace):
            continue
        try:
            if face.FaceNormal.Z <= 0.99:
                continue
            loops = face.GetEdgesAsCurveLoops()
        except Exception:
            continue
        if not loops:
            continue
        best_loop = None
        best_area = -1.0
        for cl in loops:
            curves = list(cl)
            area = abs(_signed_area_xy(curves))
            if area > best_area:
                best_area = area
                best_loop = curves
        if best_loop is not None:
            results.append(best_loop)
    return results


# ==========================================================================
# Orchestrator: build ONE combined crop CurveLoop from checked rooms
# ==========================================================================
class CropBuildResult(object):
    def __init__(self):
        self.ok = False
        self.disjoint = False  # True: per-room shapes were fine, but couldn't combine into one loop
        self.curve_loop = None
        self.detail = ""
        self.all_points = []  # every offset-loop vertex - used for the bbox fallback when disjoint


def build_combined_crop_loop(rows, offset_internal):
    result = CropBuildResult()
    offset_lines_per_room = []
    for row in rows:
        loops = _get_boundary_curve_arrays(row.room)
        tessellated = [_tessellate_loop_to_lines(loop) for loop in loops]
        tessellated = [lp for lp in tessellated if len(lp) >= 3]
        outer = _pick_outer_loop(tessellated)
        if outer is None:
            result.detail = "Room {0} ({1}) has no usable boundary".format(row.number, row.name)
            return result
        flat = _flatten_to_z(outer, 0.0)
        centroid = _polygon_centroid(flat)
        offset_loop = _build_offset_loop(flat, centroid, offset_internal)
        if len(offset_loop) < 3:
            result.detail = "Room {0} ({1})'s offset boundary collapsed - try a smaller offset".format(
                row.number, row.name)
            return result
        offset_lines_per_room.append(offset_loop)

    for lines in offset_lines_per_room:
        for line in lines:
            result.all_points.append(line.GetEndPoint(0))
            result.all_points.append(line.GetEndPoint(1))

    if len(offset_lines_per_room) == 1:
        result.curve_loop = _curve_loop_from_lines(offset_lines_per_room[0])
        result.ok = result.curve_loop is not None
        if not result.ok:
            result.detail = "Could not build a closed crop shape from this room's boundary"
        return result

    solids = [s for s in (_build_extrusion_solid(lines) for lines in offset_lines_per_room) if s is not None]
    if len(solids) != len(offset_lines_per_room):
        result.disjoint = True
        result.detail = "Could not build solid geometry for one or more selected rooms"
        return result

    union = _union_solids(solids)
    if union is None:
        result.disjoint = True
        result.detail = "Selected rooms could not be combined into one connected shape"
        return result

    top_loops = _extract_top_outer_loops(union)
    if len(top_loops) != 1:
        result.disjoint = True
        result.detail = "Selected rooms do not form one connected shape even with the current offset"
        return result

    final_lines = _tessellate_loop_to_lines(top_loops[0])
    result.curve_loop = _curve_loop_from_lines(final_lines)
    result.ok = result.curve_loop is not None
    if not result.ok:
        result.detail = "Could not build a closed crop shape from the combined room outline"
    return result


# ==========================================================================
# Applying / resetting the view's crop
# ==========================================================================
def apply_view_crop(doc, view, curve_loop):
    try:
        crsm = view.GetCropRegionShapeManager()
    except Exception as e:
        return False, "Could not access this view's crop region shape manager: {0}".format(e)
    try:
        if not crsm.CanHaveShape:
            return False, "This view does not support a non-rectangular crop shape"
    except Exception:
        pass
    try:
        if not view.CropBoxActive:
            view.CropBoxActive = True
    except Exception:
        pass
    try:
        if not crsm.IsCropRegionShapeValid(curve_loop):
            return False, "The combined room boundary is not a valid crop shape (self-intersecting or too complex)"
    except Exception:
        pass
    try:
        crsm.SetCropShape(curve_loop)
        return True, "Crop region set"
    except Exception as e:
        return False, "Could not set the crop shape: {0}".format(e)


def reset_view_crop(doc, view):
    try:
        crsm = view.GetCropRegionShapeManager()
        crsm.RemoveCropRegionShape()
        return True, "Crop region reset to rectangular"
    except Exception as e:
        return False, str(e)


def apply_bbox_crop_from_world_points(view, world_points):
    """The opt-in fallback offered when checked rooms don't form one
    connected shape: an absolute rectangle from world-space room
    points, matching view_cropping.apply_crop_offset's proven
    BoundingBoxXYZ()/.Transform=bbox.Transform/rebuild-then-reassign
    idiom - but building an ABSOLUTE box from world points (inverse-
    transformed into the view's local frame) rather than nudging the
    existing box by a relative amount."""
    try:
        crsm = view.GetCropRegionShapeManager()
        crsm.RemoveCropRegionShape()
    except Exception:
        pass
    try:
        bbox = view.CropBox
        if bbox is None:
            return False, "View has no crop box"
        transform = bbox.Transform
        inv = transform.Inverse
        local_pts = [inv.OfPoint(p) for p in world_points]
        if not local_pts:
            return False, "No room geometry to build a bounding box from"
        xs = [p.X for p in local_pts]
        ys = [p.Y for p in local_pts]
        new_box = BoundingBoxXYZ()
        new_box.Transform = transform
        new_box.Min = XYZ(min(xs), min(ys), bbox.Min.Z)
        new_box.Max = XYZ(max(xs), max(ys), bbox.Max.Z)
        view.CropBox = new_box
        return True, "Rectangular crop applied (combined bounding box)"
    except Exception as e:
        return False, str(e)


# ==========================================================================
# Reporting
# ==========================================================================
class ActionResult(object):
    def __init__(self):
        self.ok = False
        self.detail = ""
        self.elapsed_seconds = 0.0


def print_report(action_title, room_labels, offset_display, unit_abbr, action_result):
    bg = "#2e7d32" if action_result.ok else "#c62828"
    icon = "&#10003;" if action_result.ok else "&#10007;"
    html = ['<h2 style="font-family:sans-serif;color:#ddd;">DeeViewAdjust - {0} Results</h2>'.format(action_title)]
    if room_labels:
        html.append('<p style="color:#ddd;">Room(s): {0}</p>'.format(", ".join(room_labels)))
        html.append('<p style="color:#ddd;">Outward offset: {0:.3f} {1}</p>'.format(offset_display, unit_abbr))
    html.append(
        '<div style="padding:6px 12px;margin:3px 0;background:{0};color:#fff;'
        'border-radius:4px;font-family:monospace;font-size:13px;">'
        '{1}&nbsp; {2}</div>'.format(bg, icon, action_result.detail))
    html.append('<p style="color:#888;font-size:11px;">{0:.2f}s.</p>'.format(action_result.elapsed_seconds))
    output.print_html("".join(html))


# ==========================================================================
# Window - UI wiring only; all real work happens in the plain functions
# above (same separation as this extension's other geometry-heavy tools)
# ==========================================================================
class DeeViewAdjustWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, uiapp, doc, uidoc, view):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.uiapp = uiapp
        self.doc = doc
        self.uidoc = uidoc
        self.view = view
        self._rows = []

        with forms.ProgressBar(title="DeeViewAdjust - scanning rooms...", indeterminate=True):
            self._rows = scan_rooms(self.doc)
        self.rooms_grid.ItemsSource = self._rows
        self.offset_unit_tb.Text = utils.unit_abbreviation(self.doc)
        self.offset_tb.Text = "0"
        self._update_status()

    def _update_status(self):
        checked_count = sum(1 for r in self._rows if r.checked)
        self.status_tb.Text = "{0} room(s), {1} checked.".format(len(self._rows), checked_count)

    # ---------------- selection ----------------
    def check_all_click(self, sender, args):
        for r in self._rows:
            r.checked = True
        self.rooms_grid.Items.Refresh()
        self._update_status()

    def check_none_click(self, sender, args):
        for r in self._rows:
            r.checked = False
        self.rooms_grid.Items.Refresh()
        self._update_status()

    def invert_selection_click(self, sender, args):
        for r in self._rows:
            r.checked = not r.checked
        self.rooms_grid.Items.Refresh()
        self._update_status()

    def _selected_rows(self):
        return [r for r in self._rows if r.checked]

    # ---------------- see the room(s) in the open view ----------------
    def show_in_view_click(self, sender, args):
        selected = self._selected_rows()
        if not selected:
            forms.alert("Check at least one room first.")
            return
        ids = List[ElementId]([r.id for r in selected])
        with forms.ProgressBar(title="DeeViewAdjust - showing room(s) in view...", indeterminate=True):
            self.uidoc.Selection.SetElementIds(ids)
            try:
                self.uidoc.ShowElements(ids)
            except Exception:
                pass
            self.uidoc.RefreshActiveView()
        self.status_tb.Text = "Showing {0} checked room(s) in the active view.".format(len(selected))

    # ---------------- main actions ----------------
    def apply_click(self, sender, args):
        selected = self._selected_rows()
        if not selected:
            forms.alert("Check at least one room first.")
            return

        offset_display = utils.safe_float(self.offset_tb.Text, 0.0)
        if offset_display < 0:
            forms.alert("Offset must be zero or a positive value.")
            return
        offset_internal = utils.display_to_internal(self.doc, offset_display)
        unit_abbr = utils.unit_abbreviation(self.doc)

        if not forms.alert(
                "Reshape this view's Crop Region to follow {0} room(s), offset outward by {1:.3f} {2}?".format(
                    len(selected), offset_display, unit_abbr),
                title="DeeViewAdjust - Confirm", yes=True, no=True):
            return

        room_labels = ["{0} - {1}".format(r.number, r.name) for r in selected]
        start = time.time()

        with forms.ProgressBar(title="DeeViewAdjust - building crop shape...", indeterminate=True):
            build_result = build_combined_crop_loop(selected, offset_internal)

        curve_loop = None
        bbox_points = None
        if build_result.ok:
            curve_loop = build_result.curve_loop
        elif build_result.disjoint:
            use_bbox = forms.alert(
                "These rooms don't form a single connected shape after the {0:.3f} {1} offset, so Revit "
                "can't accept them as one Crop Region Shape.\n\n{2}\n\n"
                "Apply a rectangular crop encompassing all selected rooms + offset instead?".format(
                    offset_display, unit_abbr, build_result.detail),
                title="DeeViewAdjust - Rooms Not Connected", yes=True, no=True)
            if not use_bbox:
                action_result = ActionResult()
                action_result.detail = "Cancelled - rooms are not connected, no rectangular fallback applied."
                action_result.elapsed_seconds = time.time() - start
                print_report("Apply Crop", room_labels, offset_display, unit_abbr, action_result)
                return
            bbox_points = build_result.all_points
        else:
            action_result = ActionResult()
            action_result.detail = build_result.detail
            action_result.elapsed_seconds = time.time() - start
            print_report("Apply Crop", room_labels, offset_display, unit_abbr, action_result)
            return

        action_result = ActionResult()
        with forms.ProgressBar(title="DeeViewAdjust - applying crop...", indeterminate=True):
            t = Transaction(self.doc, "DeeViewAdjust - Apply Crop")
            t.Start()
            try:
                if curve_loop is not None:
                    ok, detail = apply_view_crop(self.doc, self.view, curve_loop)
                else:
                    ok, detail = apply_bbox_crop_from_world_points(self.view, bbox_points)
                if ok:
                    t.Commit()
                else:
                    t.RollBack()
            except Exception as e:
                t.RollBack()
                ok, detail = False, str(e)
            action_result.ok = ok
            action_result.detail = detail

        action_result.elapsed_seconds = time.time() - start
        print_report("Apply Crop", room_labels, offset_display, unit_abbr, action_result)
        self.status_tb.Text = "Apply Crop: {0}".format(action_result.detail)

    def reset_crop_click(self, sender, args):
        if not forms.alert(
                "Reset this view's Crop Region back to a rectangular shape?",
                title="DeeViewAdjust - Confirm", yes=True, no=True):
            return
        start = time.time()
        with forms.ProgressBar(title="DeeViewAdjust - resetting crop...", indeterminate=True):
            t = Transaction(self.doc, "DeeViewAdjust - Reset Crop")
            t.Start()
            try:
                ok, detail = reset_view_crop(self.doc, self.view)
                if ok:
                    t.Commit()
                else:
                    t.RollBack()
            except Exception as e:
                t.RollBack()
                ok, detail = False, str(e)

        action_result = ActionResult()
        action_result.ok = ok
        action_result.detail = detail
        action_result.elapsed_seconds = time.time() - start
        print_report("Reset Crop", [], 0.0, "", action_result)
        self.status_tb.Text = "Reset Crop: {0}".format(detail)

    def close_click(self, sender, args):
        self.Close()


# ==========================================================================
# View-type validation and launch
# ==========================================================================
def _is_plan_view(view):
    try:
        return isinstance(view, ViewPlan)
    except Exception:
        return False


uiapp = __revit__
if uiapp.ActiveUIDocument is None:
    forms.alert("Open a Revit project first.")
else:
    _uidoc = uiapp.ActiveUIDocument
    _doc = _uidoc.Document
    _view = _doc.ActiveView
    if not _is_plan_view(_view):
        forms.alert(
            "DeeViewAdjust only works on Plan-type views (Floor Plan, Ceiling Plan, "
            "Area Plan, Structural Plan) - a room boundary is a horizontal-plane shape, "
            "which can only become a Crop Region Shape in a view whose own plane is also "
            "horizontal. Switch to a plan view and run DeeViewAdjust again.",
            title="DeeViewAdjust")
    else:
        window = DeeViewAdjustWindow(_XAML_FILE, uiapp, _doc, _uidoc, _view)
        window.ShowDialog()
