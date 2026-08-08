# -*- coding: utf-8 -*-
"""
dee_room_xyd_service
Scans every Room visible in the ACTIVE VIEW and reports, per room, the
length of its longest boundary edge running predominantly along the
model's X axis and the length of its longest boundary edge running
predominantly along the Y axis - a quick way to read off a room's
"effective" width/depth from its real boundary geometry rather than
its bounding box, so an L-shaped or otherwise irregular room still
gets a meaningful X/Y size instead of one inflated by the notch.

Optionally, for checked rows, an actual Dimension can be placed in the
active view for the longest X edge and the longest Y edge - this is
the only part of this module that changes the model; everything else
(scanning, reporting, export) is read-only.

--------------------------------------------------------------------
Revit API facts relied on here (verified before writing, not guessed)
--------------------------------------------------------------------
- Room lives in Autodesk.Revit.DB.Architecture, not the main DB
  namespace like most other element classes - but FilteredElementCollector.
  OfClass(Room) does NOT work: Revit's collector only filters by types
  that exist in Revit's native object model, and Room is a .NET-API-only
  wrapper over that native model (confirmed live: it throws
  "Input type(...Room) is of an element type that exists in the API,
  but not in Revit's native object model"). The correct, idiomatic
  collection is by category - OfCategory(BuiltInCategory.OST_Rooms)
  .WhereElementIsNotElementType() - which returns the same Room
  instances (IronPython's dynamic typing means no explicit Room import/
  cast is even needed to call .Number/.Area/.GetBoundarySegments() on
  what it returns).
- FilteredElementCollector(doc, view.Id) restricts the collector to
  elements visible in that specific view - this is how "active view
  only" scoping is implemented, rather than filtering by Level after
  the fact (which wouldn't respect crop regions, view-specific
  visibility overrides, phase, etc).
- Room.GetBoundarySegments(SpatialElementBoundaryOptions) returns one
  list of BoundarySegment per loop (the outer perimeter, plus one per
  island/inner loop such as a shaft or column). A room with no valid
  boundary (Not Placed / Not Enclosed) returns None or an empty list,
  and Room.Area is 0 for those - both are treated as "no boundary to
  measure" here, and reported rather than silently skipped, matching
  this extension's usual "report every row, flag problems" approach.
- BoundarySegment.GetCurve() gives the segment's real geometry (Line
  or Arc); BoundarySegment.ElementId gives the element (usually a Wall)
  that generated it. curve.Length is the true length either way; for
  classifying a curve as "X axis" or "Y axis" the straight chord
  between its two endpoints is used (exact for a Line, a reasonable
  approximation for an Arc) - comparing |dx| vs |dy| of that chord.
- Classification is against the model's own internal X/Y axes (i.e.
  Project North), not True North or any per-room rotated frame - the
  same coordinate system every other geometry-reading tool in this
  extension already uses.
- Placing a Dimension for one boundary segment needs References to the
  elements bounding its TWO ENDS (e.g. click the wall on the left, then
  the wall on the right, exactly like Revit's own Aligned Dimension
  tool) - not the segment's own bounding element. Those are the
  PREVIOUS and NEXT segments in the same boundary loop (loops are
  ordered), via their own BoundarySegment.ElementId.
- A valid Reference to a wall's face requires (a) Options.
  ComputeReferences = True when calling Element.get_Geometry(options)
  - without it, Face.Reference is null - and (b) picking the correct
  one of the (usually 2) vertical PlanarFaces a wall has, done here by
  Face.Project(point).Distance to the target corner point rather than
  guessing a side by offset direction. Face.Project() finds the
  closest point ON THE FACE'S ACTUAL BOUNDED EXTENT (not just its
  infinite plane), so this distance comparison is meaningful even
  across many candidate walls, not just the two "official" neighbors.
- The immediately-adjacent boundary segment's own bounding element is
  often NOT a usable Wall (a short jog, a Room Separation Line with no
  solid geometry, a curved return, a door/window puncturing the face
  right at the corner) - live testing on a real project showed ~40%
  of edges failing to resolve with only the two adjacent segments as
  candidates. Fixed by widening the candidate pool to every Wall whose
  bounding box intersects an expanded box around the room (see
  _collect_nearby_walls) - the nearest-face-wins selection in
  _get_best_face_reference naturally ignores irrelevant far walls, so
  a bigger candidate pool only helps, it never picks something worse.
- The dimension line is offset a small distance from the wall, toward
  the room's own Location point (Room.Location is a LocationPoint) -
  matching how a person would normally pull a room dimension slightly
  into the room rather than drawing it exactly on the wall face.
"""
import time

from Autodesk.Revit.DB import (
    FilteredElementCollector, SpatialElementBoundaryOptions,
    UnitUtils, UnitTypeId, SpecTypeId, BuiltInParameter, BuiltInCategory,
    ViewType, Options, ViewDetailLevel, Solid, PlanarFace, Line, ReferenceArray,
    Transaction, XYZ, Outline, BoundingBoxIntersectsFilter,
)

from pyrevit import script

import xlsx_writer

output = script.get_output()

_REPORT_HEADERS = ["Number", "Name", "Level", "Area", "Longest X Edge", "Longest Y Edge", "Status"]
_REPORT_COL_WIDTHS = [12, 30, 20, 14, 16, 16, 20]

SUPPORTED_VIEW_TYPES = set([ViewType.FloorPlan, ViewType.CeilingPlan, ViewType.AreaPlan])


def is_view_supported(view):
    try:
        return view.ViewType in SUPPORTED_VIEW_TYPES
    except Exception:
        return False


# ==========================================================================
# Units (same small pattern already proven in DeeAligner/DeeReLevel/DeeLazy)
# ==========================================================================
def _length_unit_type_id(doc):
    try:
        return doc.GetUnits().GetFormatOptions(SpecTypeId.Length).GetUnitTypeId()
    except Exception:
        return UnitTypeId.Feet


def internal_to_display(doc, value_internal):
    uid = _length_unit_type_id(doc)
    try:
        return UnitUtils.ConvertFromInternalUnits(value_internal, uid)
    except Exception:
        return value_internal


_UNIT_ABBR = [
    (UnitTypeId.Millimeters, "mm"),
    (UnitTypeId.Centimeters, "cm"),
    (UnitTypeId.Meters, "m"),
    (UnitTypeId.Feet, "ft"),
    (UnitTypeId.FeetFractionalInches, "ft"),
    (UnitTypeId.FractionalInches, "in"),
    (UnitTypeId.Inches, "in"),
]


def unit_abbreviation(doc):
    uid = _length_unit_type_id(doc)
    for u, abbr in _UNIT_ABBR:
        if u == uid:
            return abbr
    return "ft"


# ==========================================================================
# Name / number reading - defensive fallback chain, same philosophy
# already established elsewhere in this extension (e.g. DeeRelink)
# ==========================================================================
def _read_number(room):
    try:
        n = room.Number
        if n:
            return n
    except Exception:
        pass
    return "?"


def _read_name(room):
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
    return "(unnamed room)"


def _read_level_name(doc, room):
    try:
        level = doc.GetElement(room.LevelId)
        if level is not None:
            return level.Name
    except Exception:
        pass
    return ""


# ==========================================================================
# Boundary scanning - keeps the actual segment geometry (not just
# lengths) around, since the "place dimension" action needs to walk
# to the adjacent segments and reference their bounding elements.
# ==========================================================================
def _classify_axis(curve):
    """Returns "x", "y", or None (degenerate/zero-length chord)."""
    try:
        p0 = curve.GetEndPoint(0)
        p1 = curve.GetEndPoint(1)
    except Exception:
        return None
    dx = abs(p1.X - p0.X)
    dy = abs(p1.Y - p0.Y)
    if dx == 0.0 and dy == 0.0:
        return None
    return "x" if dx >= dy else "y"


def _scan_boundary(room):
    """Returns None if there's no usable boundary, otherwise a dict:
        "loops": [[(curve, bounding_element_id), ...], ...]  (one list per loop)
        "best_x": (loop_idx, seg_idx, curve, length) or None
        "best_y": (loop_idx, seg_idx, curve, length) or None
    """
    try:
        raw_loops = room.GetBoundarySegments(SpatialElementBoundaryOptions())
    except Exception:
        raw_loops = None
    if not raw_loops:
        return None

    loops = []
    for raw_loop in raw_loops:
        loop = []
        for segment in raw_loop:
            try:
                curve = segment.GetCurve()
                bounding_id = segment.ElementId
            except Exception:
                continue
            if curve is None:
                continue
            loop.append((curve, bounding_id))
        loops.append(loop)

    best_x = None
    best_y = None
    for loop_idx, loop in enumerate(loops):
        for seg_idx, (curve, _bounding_id) in enumerate(loop):
            try:
                length = curve.Length
            except Exception:
                continue
            axis = _classify_axis(curve)
            if axis == "x" and (best_x is None or length > best_x[3]):
                best_x = (loop_idx, seg_idx, curve, length)
            elif axis == "y" and (best_y is None or length > best_y[3]):
                best_y = (loop_idx, seg_idx, curve, length)

    return {"loops": loops, "best_x": best_x, "best_y": best_y}


class RoomRow(object):
    def __init__(self, doc, room):
        self.doc = doc
        self.room = room
        self.id = room.Id
        self.selected = False
        self.number = _read_number(room)
        self.name = _read_name(room)
        self.level_name = _read_level_name(doc, room)
        try:
            self.area_internal = float(room.Area)
        except Exception:
            self.area_internal = 0.0

        self.boundary = None
        if self.area_internal > 0.0:
            self.boundary = _scan_boundary(room)
            has_x = self.boundary is not None and self.boundary["best_x"] is not None
            has_y = self.boundary is not None and self.boundary["best_y"] is not None
            self.longest_x_internal = self.boundary["best_x"][3] if has_x else 0.0
            self.longest_y_internal = self.boundary["best_y"][3] if has_y else 0.0
            self.status = "OK" if (has_x or has_y) else "No usable boundary segments"
        else:
            self.longest_x_internal = 0.0
            self.longest_y_internal = 0.0
            self.status = "Not Placed / Not Enclosed (zero area)"

    @property
    def area_text(self):
        # Area unit conversion (sq ft/sq m) is a different SpecTypeId than
        # length - kept simple/internal (sq ft) here since the grid's main
        # purpose is the X/Y edge lengths, not area precision.
        return "{0:,.1f} sf".format(self.area_internal)

    @property
    def longest_x_text(self):
        if self.status != "OK":
            return "-"
        val = internal_to_display(self.doc, self.longest_x_internal)
        return "{0:,.2f} {1}".format(val, unit_abbreviation(self.doc))

    @property
    def longest_y_text(self):
        if self.status != "OK":
            return "-"
        val = internal_to_display(self.doc, self.longest_y_internal)
        return "{0:,.2f} {1}".format(val, unit_abbreviation(self.doc))

    def to_list(self):
        return [self.number, self.name, self.level_name, self.area_text,
                self.longest_x_text, self.longest_y_text, self.status]

    def status_tag(self):
        return "ok" if self.status == "OK" else "skip"


def scan(doc, view):
    """view: the active view - only Rooms visible in it are returned
    (FilteredElementCollector(doc, view.Id)), not every Room in the
    document."""
    rows = []
    collector = (FilteredElementCollector(doc, view.Id)
                 .OfCategory(BuiltInCategory.OST_Rooms)
                 .WhereElementIsNotElementType())
    for room in collector:
        try:
            rows.append(RoomRow(doc, room))
        except Exception:
            continue
    rows.sort(key=lambda r: r.number)
    return rows


def export_report(path, rows):
    xlsx_rows = [(r.to_list(), r.status_tag()) for r in rows]
    xlsx_writer.write_themed_xlsx(path, "DeeRoomXYD - Longest X/Y Room Edges",
                                   _REPORT_HEADERS, _REPORT_COL_WIDTHS, xlsx_rows)


# ==========================================================================
# Place Dimensions in Active View - the only model-modifying part of
# this module. Best-effort per room/axis: a resolvable pair of wall-
# face References is required, or that one dimension is skipped and
# reported - never guessed at, never crashes the batch.
# ==========================================================================
_NEARBY_WALLS_MARGIN_INTERNAL = 15.0  # feet - "beyond the room", not just touching it
_DIMENSION_OFFSET_INTERNAL = 0.5      # feet (~150mm) - small pull-off from the wall face


def _collect_nearby_walls(doc, room):
    """Returns the ElementIds of every Wall whose bounding box
    intersects an expanded box around the room - a much broader
    candidate pool than just the two boundary segments officially
    adjacent to a given edge, so a usable face can still be found even
    when that "official" neighbor isn't a plain wall (a short jog, a
    Room Separation Line, a door/window right at the corner, etc)."""
    try:
        bbox = room.get_BoundingBox(None)
    except Exception:
        bbox = None
    if bbox is None:
        return []

    m = _NEARBY_WALLS_MARGIN_INTERNAL
    try:
        outline = Outline(
            XYZ(bbox.Min.X - m, bbox.Min.Y - m, bbox.Min.Z - m),
            XYZ(bbox.Max.X + m, bbox.Max.Y + m, bbox.Max.Z + m),
        )
        wall_filter = BoundingBoxIntersectsFilter(outline)
        walls = (FilteredElementCollector(doc)
                 .OfCategory(BuiltInCategory.OST_Walls)
                 .WhereElementIsNotElementType()
                 .WherePasses(wall_filter))
        return [w.Id for w in walls]
    except Exception:
        return []


def _get_element_vertical_faces(doc, element_id, cache):
    """Returns a list of the element's vertical PlanarFaces that have a
    valid Reference - cached per element_id since the same wall can
    bound more than one room's edge in a single batch."""
    key = element_id.IntegerValue
    if key in cache:
        return cache[key]

    faces = []
    el = doc.GetElement(element_id)
    if el is not None:
        try:
            options = Options()
            options.ComputeReferences = True
            options.DetailLevel = ViewDetailLevel.Fine
            geom = el.get_Geometry(options)
        except Exception:
            geom = None
        if geom is not None:
            for geom_obj in geom:
                if not isinstance(geom_obj, Solid):
                    continue
                try:
                    face_array = geom_obj.Faces
                except Exception:
                    continue
                if face_array is None:
                    continue
                for face in face_array:
                    try:
                        if not isinstance(face, PlanarFace):
                            continue
                        if abs(face.FaceNormal.Z) > 0.1:
                            continue  # only vertical faces
                        if face.Reference is None:
                            continue
                    except Exception:
                        continue
                    faces.append(face)

    cache[key] = faces
    return faces


def _get_best_face_reference(doc, candidate_ids, near_point, cache):
    """Best-effort Reference to the vertical face NEAREST near_point,
    searched across every element in candidate_ids - the same face a
    person would click with Revit's own Aligned Dimension tool at that
    corner. Searching many candidates (not just one "official"
    neighbor) is safe: Face.Project() measures distance to the face's
    actual bounded extent, so an irrelevant far wall never wins over a
    genuinely close one just for being coplanar. Returns None if
    nothing usable is found among any candidate."""
    best_face = None
    best_dist = None
    for element_id in candidate_ids:
        for face in _get_element_vertical_faces(doc, element_id, cache):
            try:
                result = face.Project(near_point)
                if result is None:
                    continue
                dist = result.Distance
            except Exception:
                continue
            if best_dist is None or dist < best_dist:
                best_dist = dist
                best_face = face
    if best_face is None:
        return None
    return best_face.Reference


class DimensionResult(object):
    def __init__(self):
        self.placed_count = 0
        self.skipped = []  # list of (label, reason)
        self.elapsed_seconds = 0.0

    def add_skip(self, label, reason):
        self.skipped.append((label, reason))


def _offset_toward_room(p0, p1, room_point, offset):
    """Returns (p0', p1') - p0/p1 shifted perpendicular to the p0->p1
    direction, by `offset`, toward room_point - so the dimension line
    is pulled a little off the wall and into the room, instead of
    sitting exactly on the boundary edge."""
    direction = p1 - p0
    length = direction.GetLength()
    if length < 1e-9:
        return p0, p1
    direction = direction.Normalize()
    perp = XYZ(-direction.Y, direction.X, 0.0)
    midpoint = XYZ((p0.X + p1.X) / 2.0, (p0.Y + p1.Y) / 2.0, (p0.Z + p1.Z) / 2.0)
    to_room = room_point - midpoint
    if perp.DotProduct(to_room) < 0:
        perp = perp.Negate()
    shift = perp.Multiply(offset)
    return p0 + shift, p1 + shift


def _place_one_dimension(doc, view, room_row, axis, face_cache, nearby_wall_ids):
    """axis: "x" or "y". Returns (ok, detail)."""
    if room_row.boundary is None:
        return False, "No boundary data"

    best = room_row.boundary["best_x"] if axis == "x" else room_row.boundary["best_y"]
    if best is None:
        return False, "No usable {0}-axis edge".format(axis.upper())

    loop_idx, seg_idx, curve, length = best
    loop = room_row.boundary["loops"][loop_idx]
    n = len(loop)
    if n < 2:
        return False, "Not enough boundary segments to reference"

    _, prev_id = loop[(seg_idx - 1) % n]
    _, next_id = loop[(seg_idx + 1) % n]

    p0 = curve.GetEndPoint(0)
    p1 = curve.GetEndPoint(1)

    # Search the immediate neighbors first, but fall back to every
    # nearby wall - see _collect_nearby_walls/_get_best_face_reference.
    candidates_p0 = [prev_id] + [eid for eid in nearby_wall_ids if eid != prev_id]
    candidates_p1 = [next_id] + [eid for eid in nearby_wall_ids if eid != next_id]

    ref0 = _get_best_face_reference(doc, candidates_p0, p0, face_cache)
    ref1 = _get_best_face_reference(doc, candidates_p1, p1, face_cache)
    if ref0 is None or ref1 is None:
        return False, "Could not resolve wall face references for this edge"

    ref_array = ReferenceArray()
    ref_array.Append(ref0)
    ref_array.Append(ref1)

    try:
        room_point = room_row.room.Location.Point
    except Exception:
        room_point = XYZ((p0.X + p1.X) / 2.0, (p0.Y + p1.Y) / 2.0, (p0.Z + p1.Z) / 2.0)

    line_p0, line_p1 = _offset_toward_room(p0, p1, room_point, _DIMENSION_OFFSET_INTERNAL)

    try:
        line = Line.CreateBound(line_p0, line_p1)
        doc.Create.NewDimension(view, line, ref_array)
        return True, "{0}-axis dimension placed".format(axis.upper())
    except Exception as e:
        return False, "Dimension creation failed: {0}".format(e)


def place_dimensions(doc, view, rows):
    """rows: the checked RoomRow list. Places both the longest-X and
    longest-Y dimension (when resolvable) for each, all inside ONE
    Transaction, matching this extension's "single transaction per
    command" convention. Never raises for one bad row/axis - failures
    are collected and reported, the rest still run."""
    start = time.time()
    result = DimensionResult()
    face_cache = {}

    t = Transaction(doc, "DeeRoomXYD - Place Dimensions")
    t.Start()
    try:
        for row in rows:
            nearby_wall_ids = _collect_nearby_walls(doc, row.room)
            for axis in ("x", "y"):
                ok, detail = _place_one_dimension(doc, view, row, axis, face_cache, nearby_wall_ids)
                label = "{0} - {1} ({2}-axis)".format(row.number, row.name, axis.upper())
                if ok:
                    result.placed_count += 1
                else:
                    result.add_skip(label, detail)
        t.Commit()
    except Exception:
        t.RollBack()
        raise

    result.elapsed_seconds = time.time() - start
    return result


def print_dimension_report(result):
    """Logs to the pyRevit output console - no popup dialog on
    success, matching the same preference already applied to
    DeeAlign/DeeGetDWG."""
    html = [
        '<h2 style="font-family:sans-serif;color:#ddd;">DeeRoomXYD - Place Dimensions</h2>',
        '<p style="color:#ddd;">Placed {0} - Skipped {1} - {2:.2f}s.</p>'.format(
            result.placed_count, len(result.skipped), result.elapsed_seconds),
    ]
    for label, reason in result.skipped:
        html.append(
            '<div style="padding:4px 10px;margin:2px 0;background:#c62828;color:#fff;'
            'border-radius:4px;font-family:monospace;font-size:12px;">'
            '&#10007;&nbsp; <b>{0}</b> &mdash; {1}</div>'.format(label, reason))
    output.print_html("".join(html))
