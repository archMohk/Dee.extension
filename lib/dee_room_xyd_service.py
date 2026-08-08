# -*- coding: utf-8 -*-
"""
dee_room_xyd_service
Scans every Room in the active document and reports, per room, the
length of its longest boundary edge running predominantly along the
model's X axis and the length of its longest boundary edge running
predominantly along the Y axis - a quick way to read off a room's
"effective" width/depth from its real boundary geometry rather than
its bounding box, so an L-shaped or otherwise irregular room still
gets a meaningful X/Y size instead of one inflated by the notch.

Read-only - this module never touches the model, so no Transaction is
used anywhere here.

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
- Room.GetBoundarySegments(SpatialElementBoundaryOptions) returns one
  list of BoundarySegment per loop (the outer perimeter, plus one per
  island/inner loop such as a shaft or column). A room with no valid
  boundary (Not Placed / Not Enclosed) returns None or an empty list,
  and Room.Area is 0 for those - both are treated as "no boundary to
  measure" here, and reported rather than silently skipped, matching
  this extension's usual "report every row, flag problems" approach.
- BoundarySegment.GetCurve() gives the segment's real geometry (Line
  or Arc). curve.Length is the true length either way; for
  classifying a curve as "X axis" or "Y axis" the straight chord
  between its two endpoints is used (exact for a Line, a reasonable
  approximation for an Arc) - comparing |dx| vs |dy| of that chord.
- Classification is against the model's own internal X/Y axes (i.e.
  Project North), not True North or any per-room rotated frame - the
  same coordinate system every other geometry-reading tool in this
  extension already uses.
"""
from Autodesk.Revit.DB import (
    FilteredElementCollector, SpatialElementBoundaryOptions,
    UnitUtils, UnitTypeId, SpecTypeId, BuiltInParameter, BuiltInCategory,
)

import xlsx_writer

_REPORT_HEADERS = ["Number", "Name", "Level", "Area", "Longest X Edge", "Longest Y Edge", "Status"]
_REPORT_COL_WIDTHS = [12, 30, 20, 14, 16, 16, 20]


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
# Boundary scanning
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


def _scan_longest_edges(room):
    """Returns (longest_x_internal, longest_y_internal, segment_count)."""
    longest_x = 0.0
    longest_y = 0.0
    segment_count = 0
    try:
        loops = room.GetBoundarySegments(SpatialElementBoundaryOptions())
    except Exception:
        loops = None
    if not loops:
        return longest_x, longest_y, segment_count

    for loop in loops:
        for segment in loop:
            try:
                curve = segment.GetCurve()
            except Exception:
                continue
            if curve is None:
                continue
            try:
                length = curve.Length
            except Exception:
                continue
            axis = _classify_axis(curve)
            if axis is None:
                continue
            segment_count += 1
            if axis == "x" and length > longest_x:
                longest_x = length
            elif axis == "y" and length > longest_y:
                longest_y = length
    return longest_x, longest_y, segment_count


class RoomRow(object):
    def __init__(self, doc, room):
        self.doc = doc
        self.room = room
        self.id = room.Id
        self.number = _read_number(room)
        self.name = _read_name(room)
        self.level_name = _read_level_name(doc, room)
        try:
            self.area_internal = float(room.Area)
        except Exception:
            self.area_internal = 0.0

        if self.area_internal > 0.0:
            longest_x, longest_y, seg_count = _scan_longest_edges(room)
            self.longest_x_internal = longest_x
            self.longest_y_internal = longest_y
            self.status = "OK" if seg_count > 0 else "No usable boundary segments"
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


def scan(doc):
    rows = []
    collector = FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_Rooms).WhereElementIsNotElementType()
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
