# -*- coding: utf-8 -*-
"""
DeeDistributor
Scans Rooms and/or Spaces (selected, active view, or whole project) and
auto-distributes one or more Family Types across each spatial element's
boundary using a configurable pattern (Grid, Offset Grid/Brick, Centered
Grid, Boundary Offset Grid, Diagonal, Radial, Perimeter, Random), with
spacing/boundary-offset, alignment, rotation, and collision detection
against real model geometry.

All pattern/geometry math (point-in-polygon, polygon inward offset,
grid/diagonal/radial/perimeter/random candidate generation, spacing<->
count solvers) lives in the Revit-API-free lib/distribution_patterns.py
module (imported as plain "import distribution_patterns", matching this
codebase's "import xlsx_writer" convention) and is NOT reimplemented
here. This script is entirely the Revit-API-facing shell: scanning
Rooms/Spaces, resolving Category/Family/Type, hosted placement per
FamilyPlacementType, collision detection against cached model geometry,
the Transaction/ProgressBar/report, presets I/O, and the WPF UI.

Nothing touches the model until Generate: everything is staged on plain
Python row/settings objects, exactly one Transaction wraps the whole
Generate pass, and every per-item attempt is wrapped in try/except and
collected as (label, kind, ok, detail) tuples (ok in True/False/None for
success/fail/skip), rendered afterward as a color-coded HTML report.

Previously-created instances are tagged via the Comments parameter
("DeeDistributor|Room:<id>|Family:<symbolId>|Index:<i>") so re-running
the tool can Skip/Replace/Update/Ask per a Duplicate Handling setting,
mirroring DeeFinisher's tagging scheme.

Highest-risk areas (cannot be fully validated without live-testing
inside Revit - see inline comments at each site for detail):
  1. resolve_ceiling_bottom_face_reference: bbox-overlap + centroid
     heuristic for "which ceiling bounds this room" - no official API.
  2. WorkPlaneBased NewFamilyInstance(Reference, XYZ, XYZ, FamilySymbol)
     overload correctness and Reference staleness across Regenerate().
  3. OneLevelBasedHosted nearest-wall host resolution heuristic.
  4. Collision detection is a bbox-vs-bbox proxy (no exact solid
     intersection), and XY-only (no Z/height range check) - documented
     approximation, not a v1 requirement. _obstacle_categories_for_symbol
     excludes the family's own host category (Walls for
     OneLevelBasedHosted, Ceilings/Floors for WorkPlaneBased) since
     touching the host is the intended behavior, not a collision - a
     wall-hosted family with 100% of candidates flagged as "colliding"
     with the very wall it mounts to was the confirmed symptom before
     this exclusion existed.
  5. FamilyPlacementType branch dispatch - wrong overload throws
     ArgumentException at RUNTIME under IronPython (no static checking).
  6. Post-placement rotation timing for face-hosted instances assumes
     instance.Location.Point is valid after doc.Regenerate().
  7. offset_polygon_inward degeneracy fallback (silently boundary_offset
     -> 0) is surfaced as a warning, not hidden - see _generate_for_pair.
  8. Space vs Room API parity: only .Area (not a BuiltInParameter) is
     used for area, per the research notes' confirmed-safe recommendation.
  9. Category/Family/Type selection (Step 2) uses standalone ComboBoxes
     scoped to the selected grid row (see category_cb_changed/
     family_cb_changed/type_cb_changed) - replaced an earlier in-grid
     cascading DataGridComboBoxColumn design that broke twice on WPF
     cell-editing-lifecycle timing.
 10. TwoLevelsBased (column-like) support: StructuralType.Column vs
     NonStructural determined from the symbol's own Category (structural
     vs architectural columns); FAMILY_BASE_LEVEL_OFFSET_PARAM /
     FAMILY_TOP_LEVEL_PARAM / FAMILY_TOP_LEVEL_OFFSET_PARAM are set
     defensively (some families may not expose all three) - needs
     live-Revit verification against real column families.
 11. CurveBased/CurveBasedDetail support: places a short Line segment
     (length = X Spacing) centered on each candidate point via
     NewFamilyInstance(Curve, FamilySymbol, Level, StructuralType) -
     reasonable for short/linear accessories, not verified against every
     curve-based family's own constraints (e.g. minimum/maximum length).
 12. WorkPlaneBased ceiling-fallback now hosts to a Reference Plane
     (_get_or_create_fallback_reference_plane) instead of the previous,
     confirmed-wrong OneLevelBased overload call. Document.Create.
     NewReferencePlane's exact argument geometry and whether
     ReferencePlane.GetReference() is a valid host Reference for
     arbitrary work-plane-based families - not verified live. One
     Reference Plane is cached per (level, fallback height) per Generate
     run, reused across every room/instance that needs it, per explicit
     request not to create one per instance.

Explicitly NOT supported: ViewBased (detail item) families, since they
live in a specific view/sheet as 2D annotation rather than 3D model
space - placing them would require a view-selection concept this tool
doesn't have, a genuinely different feature. FamilyPlacementType.Invalid
is also excluded (Revit itself cannot place these as instances).
"""
import os
import csv
import json
import math

import distribution_patterns

from pyrevit import forms, script
from Autodesk.Revit.DB import (
    FilteredElementCollector, BuiltInCategory, ElementId, Transaction,
    BuiltInParameter, SpatialElementBoundaryOptions, SpatialElementBoundaryLocation,
    XYZ, Line, UnitUtils, UnitTypeId, SpecTypeId, Level,
    FamilySymbol, Family, FamilyPlacementType, CategoryType,
    ElementTransformUtils, Wall, Ceiling, HostObjectUtils, Outline,
    BoundingBoxIntersectsFilter, ElementMulticategoryFilter
)
from Autodesk.Revit.DB.Structure import StructuralType
from Autodesk.Revit.DB.Architecture import Room

try:
    from Autodesk.Revit.DB.Mechanical import Space
    _HAS_SPACE = True
except Exception:
    Space = None
    _HAS_SPACE = False

from System.Collections.Generic import List
import clr
clr.AddReference("System.Windows.Forms")
from System.Windows.Forms import SaveFileDialog, OpenFileDialog, DialogResult, MessageBox

clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
from System.Windows.Shapes import Ellipse, Polygon, Line as WpfLine, Rectangle
from System.Windows.Controls import Canvas, TextBlock
from System.Windows.Media import Brushes, SolidColorBrush, Color, PointCollection, TranslateTransform
from System.Windows import Point as WpfPoint
from System.Windows.Input import MouseButtonState

output = script.get_output()

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")
_PRESETS_FILE = os.path.join(_THIS_DIR, "presets.json")
_USER_PRESETS_FILE = os.path.join(_THIS_DIR, "user_presets.json")
_TAG_PREFIX = "DeeDistributor|"

_PREVIEW_MARGIN = 30.0

_LEGEND_COLORS = [
    (31, 119, 180), (255, 127, 14), (44, 160, 44), (214, 39, 40),
    (148, 103, 189), (140, 86, 75), (227, 119, 194), (127, 127, 127),
]
_COLLISION_BRUSH = SolidColorBrush(Color.FromRgb(230, 74, 25))
_OUTER_BRUSH = SolidColorBrush(Color.FromRgb(60, 60, 60))
_HOLE_BRUSH = SolidColorBrush(Color.FromRgb(150, 150, 150))


# --------------------------------------------------------------------------
# Units (copied verbatim from DeeFinisher/script.py - identical logic,
# no Room-specific assumptions)
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


def _mm_to_internal(value_mm):
    return UnitUtils.ConvertToInternalUnits(value_mm, UnitTypeId.Millimeters)


# --------------------------------------------------------------------------
# Safety limits - a mis-set (near-zero) spacing, or a room/target-count
# combination that implies tens of thousands of instances, can otherwise
# make the pattern-generation loop run long enough to look like Revit has
# frozen. These are deliberately conservative and are checked BEFORE the
# expensive work starts (in _run_preview) as well as defensively inside
# the generator-consuming loop itself (_generate_candidates_for_pair).
# --------------------------------------------------------------------------
_MIN_SPACING_INTERNAL = _mm_to_internal(1.0)
_MAX_CANDIDATES_WARN = 3000
_MAX_CANDIDATES_HARD_CAP = 20000


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
# Defensive name / value reads (same pattern as DeeFinisher's _read_name -
# Element.Name throws a bare exception on some types in this IronPython
# build - fall back to Parameter reads)
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


def _read_spatial_number(spatial_element):
    try:
        n = spatial_element.Number
        if n:
            return n
    except Exception:
        pass
    try:
        p = spatial_element.get_Parameter(BuiltInParameter.ROOM_NUMBER)
        if p is not None:
            v = p.AsString()
            if v:
                return v
    except Exception:
        pass
    return "?"


def _read_spatial_name(spatial_element):
    try:
        n = spatial_element.Name
        if n:
            return n
    except Exception:
        pass
    try:
        p = spatial_element.get_Parameter(BuiltInParameter.ROOM_NAME)
        if p is not None:
            v = p.AsString()
            if v:
                return v
    except Exception:
        pass
    return "(unnamed)"


def _safe_level(spatial_element):
    try:
        return spatial_element.Level
    except Exception:
        return None


def _safe_area(spatial_element):
    # Prefer the inherited .Area double property over any BuiltInParameter
    # lookup - confirmed-safe for both Room and Space per the research
    # notes, sidestepping any uncertainty about a shared ROOM_AREA enum.
    try:
        a = spatial_element.Area
        if a:
            return a
    except Exception:
        pass
    return 0.0


# --------------------------------------------------------------------------
# Boundary geometry -> plain (x, y) loops for distribution_patterns
# --------------------------------------------------------------------------
def _get_boundary_loops_xy(spatial_element):
    """Returns (outer_loop, hole_loops) as plain (x, y) tuple lists (feet,
    Revit internal units), picking the largest-bbox loop as outer and
    treating the rest as holes. Also returns the element-id list bounding
    each outer-loop segment (parallel list of ElementId) for wall-nearest
    lookups, and the raw z-elevation of the loop's first point."""
    opts = SpatialElementBoundaryOptions()
    try:
        opts.SpatialElementBoundaryLocation = SpatialElementBoundaryLocation.Center
    except Exception:
        pass
    try:
        raw_loops = spatial_element.GetBoundarySegments(opts)
    except Exception:
        return None, [], [], 0.0

    loops_xy = []
    loops_eids = []
    z = 0.0
    for raw_loop in raw_loops:
        loop = []
        eids = []
        for seg in raw_loop:
            try:
                curve = seg.GetCurve()
            except Exception:
                continue
            if curve is None:
                continue
            p0 = curve.GetEndPoint(0)
            loop.append((p0.X, p0.Y))
            z = p0.Z
            try:
                eids.append(seg.ElementId)
            except Exception:
                eids.append(ElementId.InvalidElementId)
        if loop:
            loops_xy.append(loop)
            loops_eids.append(eids)

    if not loops_xy:
        return None, [], [], 0.0

    def _bbox_area(loop):
        minx, miny, maxx, maxy = distribution_patterns.polygon_bounds(loop)
        return (maxx - minx) * (maxy - miny)

    best_i = 0
    best_area = -1.0
    for i, loop in enumerate(loops_xy):
        a = _bbox_area(loop)
        if a > best_area:
            best_area = a
            best_i = i

    outer_loop = loops_xy[best_i]
    outer_eids = loops_eids[best_i]
    hole_loops = [loops_xy[i] for i in range(len(loops_xy)) if i != best_i]
    return outer_loop, hole_loops, outer_eids, z


# --------------------------------------------------------------------------
# Duplicate-safety tagging (Comments parameter) - same pattern as
# DeeFinisher's _tag_element / _find_existing_by_tag
# --------------------------------------------------------------------------
def _tag_prefix_for_lookup(room_id, symbol_id):
    return "{0}Room:{1}|Family:{2}".format(
        _TAG_PREFIX, _element_id_value(room_id), _element_id_value(symbol_id))


def _tag_value(room_id, symbol_id, index):
    return "{0}|Index:{1}".format(_tag_prefix_for_lookup(room_id, symbol_id), index)


def _tag_element(elem, room_id, symbol_id, index):
    try:
        p = elem.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
        if p is not None and not p.IsReadOnly:
            p.Set(_tag_value(room_id, symbol_id, index))
    except Exception:
        pass


def _read_tag(elem):
    try:
        p = elem.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
        if p is not None:
            return p.AsString()
    except Exception:
        pass
    return None


def _find_existing_by_tag(doc, category_id, room_id, symbol_id):
    prefix = _tag_prefix_for_lookup(room_id, symbol_id)
    found = []
    try:
        collector = FilteredElementCollector(doc).OfCategoryId(category_id).WhereElementIsNotElementType()
    except Exception:
        return found
    for e in collector:
        val = _read_tag(e)
        if val and val.startswith(prefix):
            found.append(e)
    return found


# --------------------------------------------------------------------------
# Data classes
# --------------------------------------------------------------------------
class SourceRow(object):
    """One row representing EITHER a Room or a Space."""
    def __init__(self, spatial_element, kind, doc):
        self.spatial_element = spatial_element
        self.kind = kind  # "Room" or "Space"
        self.selected = False
        self.number = _read_spatial_number(spatial_element)
        self.name = _read_spatial_name(spatial_element)
        self.level_name = _read_name(_safe_level(spatial_element)) or "(no level)"
        self.area_internal = _safe_area(spatial_element)
        self.area_text = _format_area(doc, self.area_internal)
        self.instances_planned = 0
        self.instances_created = 0
        self.status_text = "Pending"

    @property
    def label(self):
        return "{0} {1} - {2}".format(self.kind, self.number, self.name)


class DistributionSettings(object):
    PATTERNS = ["Grid", "Offset Grid (Brick)", "Centered Grid",
                "Boundary Offset Grid", "Diagonal", "Radial",
                "Perimeter", "Random", "Custom Pattern..."]
    ALIGNMENTS = ["Center", "Top", "Bottom", "Left", "Right",
                  "Center Horizontally", "Center Vertically"]
    ROTATION_MODES = ["Keep Family Orientation", "Rotate to Room Direction",
                       "Rotate to Nearest Wall", "Fixed Rotation Angle",
                       "Custom Rotation"]
    COLLISION_MODES = ["Skip", "Move to Nearest Available Location",
                        "Replace Existing Instance", "Notify User"]
    LOCK_MODES = ["Spacing-Locked", "Count-Locked"]

    def __init__(self):
        self.pattern = "Grid"
        self.x_spacing_internal = _mm_to_internal(600.0)
        self.y_spacing_internal = _mm_to_internal(600.0)
        self.boundary_offset_internal = _mm_to_internal(300.0)
        self.alignment = "Center"
        self.rotation_mode = "Keep Family Orientation"
        self.fixed_rotation_degrees = 0.0
        self.collision_mode = "Skip"
        self.lock_mode = "Spacing-Locked"
        self.lock_target_count = 0
        self.pattern_kwargs = {}
        self.ceiling_fallback_enabled = False
        self.fallback_height_internal = _mm_to_internal(3000.0)
        self.duplicate_mode = "Skip Existing"
        self.column_height_mode = "Attach to Level Above"
        self.column_height_internal = _mm_to_internal(3000.0)
        self.base_offset_internal = 0.0
        self.lighting_mode_enabled = False
        self.lighting_pattern = "Uniform Grid"
        self.lighting_row_position_mode = "Centered"
        self.lighting_row_offset_internal = 0.0

    def copy(self):
        s = DistributionSettings()
        s.__dict__.update(self.__dict__)
        s.pattern_kwargs = dict(self.pattern_kwargs)
        return s

    def to_preset_dict(self, doc):
        return {
            "pattern": self.pattern,
            "x_spacing_mm": UnitUtils.ConvertFromInternalUnits(self.x_spacing_internal, UnitTypeId.Millimeters),
            "y_spacing_mm": UnitUtils.ConvertFromInternalUnits(self.y_spacing_internal, UnitTypeId.Millimeters),
            "boundary_offset_mm": UnitUtils.ConvertFromInternalUnits(
                self.boundary_offset_internal, UnitTypeId.Millimeters),
            "alignment": self.alignment,
            "rotation_mode": self.rotation_mode,
            "fixed_rotation_degrees": self.fixed_rotation_degrees,
            "collision_mode": self.collision_mode,
            "lock_mode": self.lock_mode,
            "lock_target_count": self.lock_target_count,
            "pattern_kwargs": dict(self.pattern_kwargs),
            "column_height_mode": self.column_height_mode,
            "column_height_mm": UnitUtils.ConvertFromInternalUnits(self.column_height_internal, UnitTypeId.Millimeters),
            "base_offset_mm": UnitUtils.ConvertFromInternalUnits(self.base_offset_internal, UnitTypeId.Millimeters),
            "lighting_mode_enabled": self.lighting_mode_enabled,
            "lighting_pattern": self.lighting_pattern,
            "lighting_row_position_mode": self.lighting_row_position_mode,
            "lighting_row_offset_mm": UnitUtils.ConvertFromInternalUnits(
                self.lighting_row_offset_internal, UnitTypeId.Millimeters),
        }

    def apply_preset_dict(self, d):
        self.pattern = d.get("pattern", self.pattern)
        if "x_spacing_mm" in d:
            self.x_spacing_internal = _mm_to_internal(float(d["x_spacing_mm"]))
        if "y_spacing_mm" in d:
            self.y_spacing_internal = _mm_to_internal(float(d["y_spacing_mm"]))
        if "boundary_offset_mm" in d:
            self.boundary_offset_internal = _mm_to_internal(float(d["boundary_offset_mm"]))
        self.alignment = d.get("alignment", self.alignment)
        self.rotation_mode = d.get("rotation_mode", self.rotation_mode)
        self.fixed_rotation_degrees = d.get("fixed_rotation_degrees", self.fixed_rotation_degrees)
        self.collision_mode = d.get("collision_mode", self.collision_mode)
        self.lock_mode = d.get("lock_mode", self.lock_mode)
        self.lock_target_count = d.get("lock_target_count", self.lock_target_count)
        self.pattern_kwargs = dict(d.get("pattern_kwargs", {}))
        self.column_height_mode = d.get("column_height_mode", self.column_height_mode)
        if "column_height_mm" in d:
            self.column_height_internal = _mm_to_internal(float(d["column_height_mm"]))
        if "base_offset_mm" in d:
            self.base_offset_internal = _mm_to_internal(float(d["base_offset_mm"]))
        self.lighting_mode_enabled = d.get("lighting_mode_enabled", self.lighting_mode_enabled)
        self.lighting_pattern = d.get("lighting_pattern", self.lighting_pattern)
        self.lighting_row_position_mode = d.get(
            "lighting_row_position_mode", self.lighting_row_position_mode)
        if "lighting_row_offset_mm" in d:
            self.lighting_row_offset_internal = _mm_to_internal(float(d["lighting_row_offset_mm"]))


def _effective_pattern_name(settings):
    """The pattern actually used for generation/preview/estimate - when
    the Lighting Distribution tab is Active for this family, it overrides
    whatever Step 3's Pattern dropdown shows."""
    if settings.lighting_mode_enabled:
        return settings.lighting_pattern
    return settings.pattern


_PLACEMENT_KIND_LABELS = {
    "OneLevelBased": "Level-Based (Free Standing)",
    "TwoLevelsBased": "Two-Level (Column-like, Base+Top Level)",
    "OneLevelBasedHosted": "Wall-Hosted",
    "WorkPlaneBased": "Face-Based (Ceiling/Floor/Work Plane)",
    "ViewBased": "View-Based (Detail Item) - NOT SUPPORTED (2D, view-specific - not 3D model geometry)",
    "CurveBased": "Line-Based (short segment per point)",
    "CurveBasedDetail": "Line-Based Detail (short segment per point)",
    "Invalid": "Invalid - cannot be placed",
}


class FamilyChoice(object):
    """One row in the Step 2 'families to distribute' list."""
    def __init__(self):
        self.category_name = None
        self.family_name = None
        self.type_name = None
        self.symbol = None
        self.settings = DistributionSettings()
        self.enabled = True

    @property
    def placement_kind_text(self):
        if self.symbol is None:
            return ""
        try:
            pt = self.symbol.Family.FamilyPlacementType
            return _PLACEMENT_KIND_LABELS.get(str(pt), str(pt))
        except Exception:
            return "(unknown)"

    @property
    def label(self):
        return "{0} / {1} / {2}".format(
            self.category_name or "?", self.family_name or "?", self.type_name or "?")


# --------------------------------------------------------------------------
# Room/Space scanning
# --------------------------------------------------------------------------
def _collect_spatial_elements(doc, mode, uidoc, view):
    rows = []
    if mode in ("selected_rooms", "selected_spaces"):
        try:
            ids = uidoc.Selection.GetElementIds()
        except Exception:
            ids = []
        for eid in ids:
            elem = doc.GetElement(eid)
            if mode == "selected_rooms" and isinstance(elem, Room):
                rows.append(SourceRow(elem, "Room", doc))
            elif mode == "selected_spaces" and _HAS_SPACE and Space is not None and isinstance(elem, Space):
                rows.append(SourceRow(elem, "Space", doc))
        return rows

    if mode == "active_view":
        collector_rooms = FilteredElementCollector(doc, view.Id).OfCategory(BuiltInCategory.OST_Rooms)
        for r in collector_rooms:
            if isinstance(r, Room):
                rows.append(SourceRow(r, "Room", doc))
        if _HAS_SPACE:
            try:
                collector_spaces = FilteredElementCollector(doc, view.Id).OfCategory(BuiltInCategory.OST_MEPSpaces)
                for s in collector_spaces:
                    if isinstance(s, Space):
                        rows.append(SourceRow(s, "Space", doc))
            except Exception:
                pass
        return rows

    # entire_project
    collector_rooms = FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_Rooms)
    for r in collector_rooms:
        if isinstance(r, Room):
            rows.append(SourceRow(r, "Room", doc))
    if _HAS_SPACE:
        try:
            collector_spaces = FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_MEPSpaces)
            for s in collector_spaces:
                if isinstance(s, Space):
                    rows.append(SourceRow(s, "Space", doc))
        except Exception:
            pass
    return rows


# --------------------------------------------------------------------------
# Category / Family / Type resolution for Step 2's cascading dropdowns
# --------------------------------------------------------------------------
def _collect_all_family_symbols(doc):
    try:
        return list(FilteredElementCollector(doc).OfClass(FamilySymbol))
    except Exception:
        return []


# --------------------------------------------------------------------------
# Collision detection (bbox proxy against a per-room cached obstacle list)
# --------------------------------------------------------------------------
_OBSTACLE_CATEGORIES = [
    BuiltInCategory.OST_Walls, BuiltInCategory.OST_Columns,
    BuiltInCategory.OST_StructuralColumns, BuiltInCategory.OST_Floors,
    BuiltInCategory.OST_Ceilings, BuiltInCategory.OST_ShaftOpening,
    BuiltInCategory.OST_GenericModel,
]


def _obstacle_categories_for_symbol(symbol):
    """Excludes whichever category this family will be intentionally
    hosted to/touching - a wall-hosted family (e.g. a sidewall sprinkler
    or a wall-mounted detector) is SUPPOSED to touch its host wall, and a
    ceiling/floor-hosted (WorkPlaneBased) family is supposed to touch its
    host ceiling - that's not a collision to avoid, it's the whole point.
    Without this, every wall-hosted candidate near a wall was being
    flagged as colliding with that same wall and skipped, no matter where
    it was placed."""
    cats = list(_OBSTACLE_CATEGORIES)
    try:
        placement_type = str(symbol.Family.FamilyPlacementType) if symbol is not None else ""
    except Exception:
        placement_type = ""
    if placement_type == "OneLevelBasedHosted":
        if BuiltInCategory.OST_Walls in cats:
            cats.remove(BuiltInCategory.OST_Walls)
    elif placement_type == "WorkPlaneBased":
        if BuiltInCategory.OST_Ceilings in cats:
            cats.remove(BuiltInCategory.OST_Ceilings)
        if BuiltInCategory.OST_Floors in cats:
            cats.remove(BuiltInCategory.OST_Floors)
    return cats


def _build_obstacle_cache(doc, outer_loop, categories, extra_category_id=None, pad_internal=6.0):
    """One FilteredElementCollector call per room, reduced to a plain list
    of (minx, miny, maxx, maxy, element) tuples - no further Revit API
    calls happen per-candidate-point after this (see spec section 5.1).
    `categories` should come from _obstacle_categories_for_symbol so the
    family's own host category is excluded where appropriate."""
    minx, miny, maxx, maxy = distribution_patterns.polygon_bounds(outer_loop)
    outline = Outline(XYZ(minx - pad_internal, miny - pad_internal, -pad_internal),
                       XYZ(maxx + pad_internal, maxy + pad_internal, pad_internal * 4))
    bbox_filter = BoundingBoxIntersectsFilter(outline)

    cats = List[BuiltInCategory](categories)
    try:
        cat_filter = ElementMulticategoryFilter(cats)
        collector = FilteredElementCollector(doc).WherePasses(cat_filter).WherePasses(
            bbox_filter).WhereElementIsNotElementType()
        elems = list(collector)
    except Exception:
        elems = []

    if extra_category_id is not None:
        try:
            extra = list(FilteredElementCollector(doc).OfCategoryId(extra_category_id).WherePasses(
                bbox_filter).WhereElementIsNotElementType())
            elems.extend(extra)
        except Exception:
            pass

    cache = []
    for e in elems:
        try:
            bb = e.get_BoundingBox(None)
        except Exception:
            bb = None
        if bb is None:
            continue
        cache.append((bb.Min.X, bb.Min.Y, bb.Max.X, bb.Max.Y, e))
    return cache


def _symbol_bbox_footprint(symbol):
    """Returns (half_x, half_y) footprint half-extents from the symbol's
    own (definition-space, near-origin) bounding box - a cheap, documented
    approximation (see risk area #4): exact instance geometry after
    rotation/hosting is not used."""
    try:
        bb = symbol.get_BoundingBox(None)
        if bb is not None:
            return ((bb.Max.X - bb.Min.X) / 2.0, (bb.Max.Y - bb.Min.Y) / 2.0)
    except Exception:
        pass
    return (_mm_to_internal(150.0), _mm_to_internal(150.0))


def _candidate_bbox(x, y, half_x, half_y, angle_radians):
    """Rotated-rectangle bbox, conservatively expanded to its own
    axis-aligned bounding box after rotation (cheap, no Solid needed)."""
    corners = [(-half_x, -half_y), (half_x, -half_y), (half_x, half_y), (-half_x, half_y)]
    cos_a, sin_a = math.cos(angle_radians), math.sin(angle_radians)
    xs = []
    ys = []
    for cx, cy in corners:
        wx = x + cx * cos_a - cy * sin_a
        wy = y + cx * sin_a + cy * cos_a
        xs.append(wx)
        ys.append(wy)
    return (min(xs), min(ys), max(xs), max(ys))


def _bbox_overlaps(a, b):
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return not (ax1 < bx0 or bx1 < ax0 or ay1 < by0 or by1 < ay0)


def _check_collision(candidate_bbox, obstacle_cache):
    for ox0, oy0, ox1, oy1, elem in obstacle_cache:
        if _bbox_overlaps(candidate_bbox, (ox0, oy0, ox1, oy1)):
            return elem
    return None


# --------------------------------------------------------------------------
# Alignment: translate the outer loop so the pattern's own anchor point
# (bbox min-corner / center / etc.) lines up with the requested alignment,
# WITHOUT modifying distribution_patterns.py (which is already
# unit-tested and frozen). Grid-family patterns key their stepping off
# the inset bbox's min-corner (grid/offset_grid) or center (centered_grid/
# boundary_offset_grid) - "Top"/"Bottom"/"Left"/"Right" reflect the loop
# about its own bbox center along one axis before generation and reflect
# candidate points back afterward, which is equivalent to starting the
# grid stepping from the opposite corner.
# --------------------------------------------------------------------------
def _apply_alignment(outer_loop, alignment):
    """Returns (transformed_loop, inverse_fn) - inverse_fn maps a
    generated (x, y) back to true world space."""
    if alignment in ("Center", "Center Horizontally", "Center Vertically"):
        return outer_loop, (lambda x, y: (x, y))

    minx, miny, maxx, maxy = distribution_patterns.polygon_bounds(outer_loop)
    if alignment == "Top":
        # reflect Y about bbox center so grid()'s bottom-up stepping
        # effectively starts from the top edge
        cy = (miny + maxy) / 2.0
        new_loop = [(x, 2 * cy - y) for (x, y) in outer_loop]
        return new_loop, (lambda x, y, cy=cy: (x, 2 * cy - y))
    if alignment == "Bottom":
        return outer_loop, (lambda x, y: (x, y))
    if alignment == "Left":
        return outer_loop, (lambda x, y: (x, y))
    if alignment == "Right":
        cx = (minx + maxx) / 2.0
        new_loop = [(2 * cx - x, y) for (x, y) in outer_loop]
        return new_loop, (lambda x, y, cx=cx: (2 * cx - x, y))
    return outer_loop, (lambda x, y: (x, y))


def _transform_holes_for_alignment(hole_loops, alignment, outer_loop):
    if alignment in ("Center", "Center Horizontally", "Center Vertically", "Bottom", "Left"):
        return hole_loops
    minx, miny, maxx, maxy = distribution_patterns.polygon_bounds(outer_loop)
    if alignment == "Top":
        cy = (miny + maxy) / 2.0
        return [[(x, 2 * cy - y) for (x, y) in h] for h in hole_loops]
    if alignment == "Right":
        cx = (minx + maxx) / 2.0
        return [[(2 * cx - x, y) for (x, y) in h] for h in hole_loops]
    return hole_loops


# --------------------------------------------------------------------------
# Rotation resolution
# --------------------------------------------------------------------------
def _dominant_edge_angle(loop):
    """Angle (radians) of the longest straight edge in `loop`, relative
    to World X - used for 'Rotate to Room Direction'."""
    best_len = -1.0
    best_angle = 0.0
    n = len(loop)
    for i in range(n):
        x0, y0 = loop[i]
        x1, y1 = loop[(i + 1) % n]
        dx, dy = x1 - x0, y1 - y0
        length = (dx * dx + dy * dy) ** 0.5
        if length > best_len:
            best_len = length
            best_angle = math.atan2(dy, dx)
    return best_angle


def _nearest_wall_angle(doc, x, y, outer_eids):
    """Angle (radians) of the nearest wall-bounded boundary segment to
    (x, y) - reuses the room's own boundary segment ElementIds (§6, risk
    area #3: nearest-wall resolution is a heuristic, not a guaranteed-
    correct host/orientation match)."""
    best_dist = None
    best_angle = 0.0
    for eid in outer_eids:
        try:
            wall = doc.GetElement(eid)
        except Exception:
            wall = None
        if not isinstance(wall, Wall):
            continue
        try:
            loc = wall.Location
            curve = loc.Curve
        except Exception:
            continue
        try:
            proj = curve.Project(XYZ(x, y, curve.GetEndPoint(0).Z))
            dist = proj.Distance
        except Exception:
            continue
        if best_dist is None or dist < best_dist:
            best_dist = dist
            p0 = curve.GetEndPoint(0)
            p1 = curve.GetEndPoint(1)
            best_angle = math.atan2(p1.Y - p0.Y, p1.X - p0.X)
    return best_angle


def _resolve_rotation_radians(settings, doc, x, y, outer_loop, outer_eids):
    mode = settings.rotation_mode
    if mode == "Keep Family Orientation":
        return 0.0
    if mode == "Rotate to Room Direction":
        return _dominant_edge_angle(outer_loop)
    if mode == "Rotate to Nearest Wall":
        return _nearest_wall_angle(doc, x, y, outer_eids)
    if mode in ("Fixed Rotation Angle", "Custom Rotation"):
        return math.radians(settings.fixed_rotation_degrees)
    return 0.0


# --------------------------------------------------------------------------
# resolve_ceiling_bottom_face_reference - highest-risk helper (§6.1).
# bbox-overlap + centroid heuristic, no official Room/Space -> Ceiling
# API. Unverified against sloped/coffered ceilings or multi-ceiling rooms.
# --------------------------------------------------------------------------
def _level_above(doc, level):
    if level is None:
        return None
    levels = sorted(FilteredElementCollector(doc).OfClass(Level), key=lambda l: l.Elevation)
    for l in levels:
        if l.Elevation > level.Elevation + 1e-6:
            return l
    return None


def resolve_ceiling_bottom_face_reference(doc, spatial_element, outer_loop, warnings_out):
    level = _safe_level(spatial_element)
    level_ids = set()
    if level is not None:
        level_ids.add(_element_id_value(level.Id))
        above = _level_above(doc, level)
        if above is not None:
            level_ids.add(_element_id_value(above.Id))

    minx, miny, maxx, maxy = distribution_patterns.polygon_bounds(outer_loop)
    cx, cy = (minx + maxx) / 2.0, (miny + maxy) / 2.0

    candidates = []
    try:
        ceilings = list(FilteredElementCollector(doc).OfClass(Ceiling))
    except Exception:
        ceilings = []
    for c in ceilings:
        try:
            clevel_id = c.LevelId
            if level_ids and _element_id_value(clevel_id) not in level_ids:
                continue
        except Exception:
            pass
        try:
            bb = c.get_BoundingBox(None)
        except Exception:
            bb = None
        if bb is None:
            continue
        if bb.Max.X < minx or bb.Min.X > maxx or bb.Max.Y < miny or bb.Min.Y > maxy:
            continue
        ox0 = max(bb.Min.X, minx)
        oy0 = max(bb.Min.Y, miny)
        ox1 = min(bb.Max.X, maxx)
        oy1 = min(bb.Max.Y, maxy)
        overlap_area = max(0.0, ox1 - ox0) * max(0.0, oy1 - oy0)
        candidates.append((overlap_area, c, bb))

    if not candidates:
        return None

    candidates.sort(key=lambda t: t[0], reverse=True)
    if len(candidates) > 1 and candidates[1][0] > 1e-6:
        warnings_out.append(
            "Multiple ceiling candidates found for {0} - using the one with the largest "
            "bbox overlap; review placement height/host manually.".format(
                _read_spatial_name(spatial_element)))

    best_ceiling = candidates[0][1]
    try:
        refs = HostObjectUtils.GetBottomFaces(best_ceiling)
    except Exception:
        refs = []
    for ref in refs:
        try:
            face = best_ceiling.GetGeometryObjectFromReference(ref)
            normal = face.ComputeNormal(face.GetBoundingBox().Min)
            if normal.Z < 0:
                return ref
        except Exception:
            continue
    if refs:
        return refs[0]
    return None


def _get_or_create_fallback_reference_plane(doc, level, z, ref_plane_cache):
    """WorkPlaneBased families with no hosting ceiling found need SOME
    Reference to host to (NewFamilyInstance(Reference, XYZ, XYZ, Symbol) -
    the same overload used for real ceiling faces - there is no separate
    'just place it floating at a height' overload for a work-plane-based
    symbol). Creates a horizontal ReferencePlane at height z as that host.

    Cached per (level, height) in ref_plane_cache (a plain dict the
    caller creates fresh per Generate run) so multiple rooms on the same
    level using the same fallback height share ONE reference plane
    instead of getting a new one per placed instance - keeping the model
    from filling up with hundreds of near-duplicate reference planes.
    Highest-risk / needs-live-verification area (see module docstring
    risk #12): NewReferencePlane's exact argument geometry and
    ReferencePlane.GetReference()'s suitability as a hosting Reference
    for arbitrary work-plane-based families hasn't been tested live."""
    key = (_element_id_value(level.Id) if level is not None else None, round(z, 4))
    cached = ref_plane_cache.get(key)
    if cached is not None:
        try:
            return cached.GetReference()
        except Exception:
            del ref_plane_cache[key]

    try:
        bubble_end = XYZ(-100.0, 0.0, z)
        free_end = XYZ(100.0, 0.0, z)
        third_pt = XYZ(0.0, 100.0, z)
        ref_plane = doc.Create.NewReferencePlane(bubble_end, free_end, third_pt, doc.ActiveView)
        try:
            ref_plane.Name = "DeeDistributor Fallback Plane - {0}".format(
                _read_name(level) or "no level")
        except Exception:
            pass
        ref_plane_cache[key] = ref_plane
        return ref_plane.GetReference()
    except Exception:
        return None


# --------------------------------------------------------------------------
# Placement (hosted-placement decision tree, §6)
# --------------------------------------------------------------------------
def _ensure_symbol_active(doc, symbol):
    try:
        if not symbol.IsActive:
            symbol.Activate()
            doc.Regenerate()
    except Exception:
        pass


def _structural_type_for_symbol(symbol):
    """Structural columns (OST_StructuralColumns) need StructuralType.Column;
    architectural columns (OST_Columns) and everything else use
    NonStructural even though both placement-type-wise are TwoLevelsBased."""
    try:
        cat = symbol.Category
        if cat is not None and cat.Id == ElementId(BuiltInCategory.OST_StructuralColumns):
            return StructuralType.Column
    except Exception:
        pass
    return StructuralType.NonStructural


def _apply_two_level_extent(doc, inst, base_level, settings):
    """Sets Base Offset / Top Level / Top Offset on a freshly-created
    TwoLevelsBased instance (column-like). All three parameters are set
    defensively (missing/read-only on some families) - a failure here
    still leaves the instance placed at the base level with whatever
    default top constraint its type carries."""
    try:
        p = inst.get_Parameter(BuiltInParameter.FAMILY_BASE_LEVEL_OFFSET_PARAM)
        if p is not None and not p.IsReadOnly:
            p.Set(settings.base_offset_internal)
    except Exception:
        pass

    top_level = base_level
    top_offset = settings.column_height_internal
    if settings.column_height_mode == "Attach to Level Above":
        above = _level_above(doc, base_level)
        if above is not None:
            top_level = above
            top_offset = 0.0

    try:
        p = inst.get_Parameter(BuiltInParameter.FAMILY_TOP_LEVEL_PARAM)
        if p is not None and not p.IsReadOnly:
            p.Set(top_level.Id)
    except Exception:
        pass
    try:
        p = inst.get_Parameter(BuiltInParameter.FAMILY_TOP_LEVEL_OFFSET_PARAM)
        if p is not None and not p.IsReadOnly:
            p.Set(top_offset)
    except Exception:
        pass


def _nearest_wall_host(doc, x, y, outer_eids):
    best_dist = None
    best_wall = None
    for eid in outer_eids:
        try:
            wall = doc.GetElement(eid)
        except Exception:
            wall = None
        if not isinstance(wall, Wall):
            continue
        try:
            curve = wall.Location.Curve
            proj = curve.Project(XYZ(x, y, curve.GetEndPoint(0).Z))
            dist = proj.Distance
        except Exception:
            continue
        if best_dist is None or dist < best_dist:
            best_dist = dist
            best_wall = wall
    return best_wall


def _place_instance(doc, symbol, x, y, z, level, outer_eids, settings, warnings_out, spatial_element,
                     ref_plane_cache):
    """Returns (instance_or_None, detail_string)."""
    try:
        placement_type = symbol.Family.FamilyPlacementType
    except Exception:
        return None, "Could not determine FamilyPlacementType"
    pt_name = str(placement_type)

    if pt_name == "OneLevelBased":
        if level is None:
            return None, "Room/Space has no Level"
        try:
            inst = doc.Create.NewFamilyInstance(
                XYZ(x, y, z), symbol, level, StructuralType.NonStructural)
            return inst, "Placed (Level-Based)"
        except Exception as e:
            return None, "FAILED (Level-Based): {0}".format(e)

    if pt_name == "TwoLevelsBased":
        if level is None:
            return None, "Room/Space has no Level"
        try:
            struct_type = _structural_type_for_symbol(symbol)
            inst = doc.Create.NewFamilyInstance(XYZ(x, y, z), symbol, level, struct_type)
        except Exception as e:
            return None, "FAILED (Two-Level/Column): {0}".format(e)
        try:
            doc.Regenerate()
        except Exception:
            pass
        _apply_two_level_extent(doc, inst, level, settings)
        return inst, "Placed (Two-Level/Column, base='{0}', mode='{1}')".format(
            _read_name(level) or "?", settings.column_height_mode)

    if pt_name == "OneLevelBasedHosted":
        wall = _nearest_wall_host(doc, x, y, outer_eids)
        if wall is None:
            return None, "No qualifying host wall found near this candidate - skipped"
        if level is None:
            return None, "Room/Space has no Level"
        try:
            inst = doc.Create.NewFamilyInstance(
                XYZ(x, y, z), symbol, wall, level, StructuralType.NonStructural)
            return inst, "Placed (Wall-Hosted)"
        except Exception as e:
            return None, "FAILED (Wall-Hosted): {0}".format(e)

    if pt_name == "WorkPlaneBased":
        # highest-risk path - see module docstring risk #1/#2
        outer_loop_local = None
        try:
            outer_loop_local, _holes, _eids, _z = _get_boundary_loops_xy(spatial_element)
        except Exception:
            pass
        host_ref = None
        try:
            host_ref = resolve_ceiling_bottom_face_reference(
                doc, spatial_element, outer_loop_local or [(x, y)], warnings_out)
        except Exception:
            host_ref = None
        if host_ref is None:
            if settings.ceiling_fallback_enabled:
                if level is None:
                    return None, "No hosting ceiling found and Room/Space has no Level for fallback"
                fallback_z = level.Elevation + settings.fallback_height_internal
                # A WorkPlaneBased symbol can only be placed via the
                # Reference-based overload (same one used for a real
                # ceiling face below) - there is no "just float it at a
                # height" overload for a work-plane-based family, unlike
                # OneLevelBased. Previously this incorrectly called the
                # OneLevelBased overload, which is why WorkPlaneBased
                # families with no ceiling found always failed. Host to a
                # cached (one per level+height, not one per instance)
                # horizontal Reference Plane instead.
                fallback_ref = _get_or_create_fallback_reference_plane(
                    doc, level, fallback_z, ref_plane_cache)
                if fallback_ref is None:
                    return None, "FAILED: could not create a fallback Reference Plane"
                try:
                    inst = doc.Create.NewFamilyInstance(fallback_ref, XYZ(x, y, fallback_z),
                                                         XYZ.BasisX, symbol)
                    return inst, "Placed (Reference Plane fallback, no ceiling found)"
                except Exception as e:
                    return None, "FAILED (fallback placement): {0}".format(e)
            return None, ("No hosting ceiling found for this Room/Space - enable 'fixed height "
                          "fallback' in Step 3 or skip")
        try:
            # z as passed in is the room/space's Level elevation (floor
            # height), not the ceiling's - use the resolved ceiling's own
            # bounding box to place at its actual bottom-face height
            # instead, otherwise ceiling-hosted fixtures (lights,
            # sprinklers, diffusers, smoke detectors) would be placed at
            # floor level.
            place_z = z
            try:
                host_elem = doc.GetElement(host_ref.ElementId)
                bb = host_elem.get_BoundingBox(None) if host_elem is not None else None
                if bb is not None:
                    place_z = bb.Min.Z
            except Exception:
                pass
            inst = doc.Create.NewFamilyInstance(host_ref, XYZ(x, y, place_z), XYZ.BasisX, symbol)
            return inst, "Placed (Work Plane/Ceiling-Hosted)"
        except Exception as e:
            return None, "FAILED (Work Plane-Hosted): {0}".format(e)

    if pt_name == "ViewBased":
        return None, ("View-based (detail item) families live in a specific view/sheet, not "
                       "3D model space - not supported by DeeDistributor - skipped")

    if pt_name in ("CurveBased", "CurveBasedDetail"):
        if level is None:
            return None, "Room/Space has no Level"
        length = settings.x_spacing_internal if settings.x_spacing_internal > 1e-6 else _mm_to_internal(600.0)
        half = length / 2.0
        try:
            p0 = XYZ(x - half, y, z)
            p1 = XYZ(x + half, y, z)
            curve = Line.CreateBound(p0, p1)
            inst = doc.Create.NewFamilyInstance(curve, symbol, level, StructuralType.NonStructural)
            detail = "Placed (Line-Based, segment length={0:.0f}mm along X - rotated per Rotation setting)".format(
                UnitUtils.ConvertFromInternalUnits(length, UnitTypeId.Millimeters))
            return inst, detail
        except Exception as e:
            return None, "FAILED (Line-Based): {0}".format(e)

    return None, "This family/type cannot be placed as an instance (Invalid placement type)"


def _apply_rotation(doc, instance, angle_radians, fallback_xyz):
    if abs(angle_radians) < 1e-9:
        return
    try:
        doc.Regenerate()
    except Exception:
        pass
    origin = None
    try:
        loc = instance.Location
        if loc is not None:
            origin = loc.Point
    except Exception:
        origin = None
    if origin is None:
        origin = fallback_xyz
    try:
        axis = Line.CreateBound(origin, origin + XYZ.BasisZ)
        ElementTransformUtils.RotateElement(doc, instance.Id, axis, angle_radians)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Preview / staged placement computation (shared by Preview and Generate -
# Generate consumes exactly what Preview last staged, per §7.3)
# --------------------------------------------------------------------------
class StagedPlacement(object):
    def __init__(self, family_choice, source_row, x, y, collision, moved_from=None):
        self.family_choice = family_choice
        self.source_row = source_row
        self.x = x
        self.y = y
        self.collision = collision  # True/False - whether a collision was detected
        self.moved_from = moved_from  # (orig_x, orig_y) if Move-mode relocated this point


def _resolve_effective_spacing(outer_loop, settings):
    """Returns (x_spacing_internal, y_spacing_internal) actually used for
    generation - if Count-Locked, this is CALCULATED from the room's own
    area and shape (bounding width/height) for a target fixture count,
    via distribution_patterns.solve_grid_spacing_for_count (grid-family
    patterns: N points across N-1 gaps) or
    solve_uniform_grid_spacing_for_count / solve_single_row_spacing_for_count
    (Lighting Distribution's Uniform Grid / Single Row: N equal CELLS,
    fixture at each center - a different formula, since dividing by
    cols-1/rows-1 there would silently break the
    margin-equals-half-actual-spacing guarantee) - rather than the raw
    settings fields. Factored out so both the real generator
    (_generate_candidates_for_pair) and the Preview pre-check
    (_run_preview) validate the exact same effective values."""
    x_spacing = settings.x_spacing_internal
    y_spacing = settings.y_spacing_internal
    if settings.lock_mode == "Count-Locked" and settings.lock_target_count > 0:
        minx, miny, maxx, maxy = distribution_patterns.polygon_bounds(outer_loop)
        width = max(1e-6, maxx - minx)
        height = max(1e-6, maxy - miny)
        pattern_name = _effective_pattern_name(settings)
        try:
            if pattern_name == "Single Row":
                _cols, x_spacing = distribution_patterns.solve_single_row_spacing_for_count(
                    width, settings.lock_target_count)
            elif pattern_name == "Uniform Grid":
                _cols, _rows, x_spacing, y_spacing = distribution_patterns.solve_uniform_grid_spacing_for_count(
                    width, height, settings.lock_target_count)
            else:
                _cols, _rows, x_spacing, y_spacing = distribution_patterns.solve_grid_spacing_for_count(
                    width, height, settings.lock_target_count)
            if x_spacing <= 0:
                x_spacing = settings.x_spacing_internal
            if y_spacing <= 0:
                y_spacing = settings.y_spacing_internal
        except Exception:
            pass
    return x_spacing, y_spacing


def _estimate_candidate_count(outer_loop, settings, x_spacing, y_spacing):
    """Cheap, deliberately-conservative pre-estimate of how many points a
    pattern will generate - used purely as a safety-net signal (not for
    exact planning) before running the real, potentially slow generator.
    x_spacing/y_spacing must already be the EFFECTIVE, resolved values
    (see _resolve_effective_spacing - Count-Locked mode solves these from
    a target count rather than using the raw settings fields directly).
    Random uses its own target_count directly; Perimeter scales with
    boundary length; everything else (the grid-family patterns, and
    Radial as a reasonable approximation) scales with area / spacing^2."""
    minx, miny, maxx, maxy = distribution_patterns.polygon_bounds(outer_loop)
    width = max(1e-6, maxx - minx)
    height = max(1e-6, maxy - miny)
    area = width * height
    perimeter = 2.0 * (width + height)
    pattern = _effective_pattern_name(settings)

    if pattern == "Random":
        return int(settings.pattern_kwargs.get("target_count", 50) or 50)

    # Floor at _MIN_SPACING_INTERNAL (1mm), not a near-zero epsilon - a
    # tinier floor turns "spacing is basically zero" into an
    # astronomical, meaningless-looking number (e.g. 3e21) instead of a
    # clearly-too-large-but-readable one. The real "is this degenerate"
    # decision belongs to the caller (_run_preview checks the same
    # resolved spacing against _MIN_SPACING_INTERNAL and stops with a
    # clear message before ever computing an estimate) - this floor is
    # just a last-resort guard against this function itself misbehaving.
    x_sp = max(x_spacing, _MIN_SPACING_INTERNAL)
    y_sp = max(y_spacing, _MIN_SPACING_INTERNAL)

    if pattern == "Perimeter":
        return int(perimeter / x_sp) + 4

    if pattern == "Single Row":
        return int(width / x_sp) + 2

    if pattern == "Radial":
        ring_sp = settings.pattern_kwargs.get("ring_spacing") or min(x_sp, y_sp)
        ring_sp = max(ring_sp, _MIN_SPACING_INTERNAL)
        return int(area / (ring_sp * ring_sp)) + 4

    return int(area / (x_sp * y_sp)) + 4


def _generate_candidates_for_pair(doc, family_choice, source_row, warnings_out):
    settings = family_choice.settings
    outer_loop, hole_loops, outer_eids, z = _get_boundary_loops_xy(source_row.spatial_element)
    if outer_loop is None:
        warnings_out.append("{0}: no valid boundary - skipped".format(source_row.label))
        return []

    pattern_name = _effective_pattern_name(settings)

    # Uniform Grid / Single Row (Lighting Distribution) intentionally
    # ignore Boundary Offset entirely - their margin IS half the actual
    # spacing by definition, not a separately configurable value - so the
    # usual "boundary offset too large" pre-check doesn't apply to them.
    if pattern_name not in ("Uniform Grid", "Single Row"):
        degeneracy_check = distribution_patterns.offset_polygon_inward(
            outer_loop, settings.boundary_offset_internal)
        if degeneracy_check is None and settings.boundary_offset_internal > 1e-9:
            warnings_out.append(
                "Boundary offset too large for {0}'s geometry - placed with reduced/no "
                "offset".format(source_row.label))

    aligned_outer, inverse_fn = _apply_alignment(outer_loop, settings.alignment)
    aligned_holes = _transform_holes_for_alignment(hole_loops, settings.alignment, outer_loop)

    if pattern_name == "Custom Pattern...":
        forms.alert(
            "No custom pattern is registered yet - add one to "
            "distribution_patterns.PATTERN_REGISTRY.", title="DeeDistributor")
        return []

    pattern_fn = distribution_patterns.PATTERN_REGISTRY.get(pattern_name)
    if pattern_fn is None:
        warnings_out.append("Unknown pattern '{0}' - skipped".format(pattern_name))
        return []

    kwargs = dict(settings.pattern_kwargs)
    if pattern_name == "Single Row":
        kwargs["row_position_mode"] = settings.lighting_row_position_mode
        kwargs["row_offset"] = settings.lighting_row_offset_internal
    x_spacing, y_spacing = _resolve_effective_spacing(aligned_outer, settings)

    # Safety check on the ACTUAL resolved spacing (post Count-Locked
    # solve, if any) - a near-zero spacing here would otherwise try to
    # generate an unreasonable/unbounded number of points and can freeze
    # Revit. Random doesn't step by x/y spacing the same way, so it's
    # exempt (its own target_count is checked upstream in _run_preview).
    if pattern_name != "Random" and (x_spacing < _MIN_SPACING_INTERNAL or y_spacing < _MIN_SPACING_INTERNAL):
        warnings_out.append(
            "{0}: resolved X/Y Spacing is below 1mm for pattern '{1}' - this would try to create "
            "an unreasonable number of instances and can freeze Revit, so this room/family pair "
            "was skipped. Increase spacing (or the Count-Locked target count's implied spacing) "
            "in Step 3.".format(source_row.label, pattern_name))
        return []

    candidates_local = []
    hard_cap_hit = False
    try:
        for i, (lx, ly) in enumerate(pattern_fn(aligned_outer, aligned_holes, x_spacing, y_spacing,
                                    settings.boundary_offset_internal, **kwargs)):
            if i >= _MAX_CANDIDATES_HARD_CAP:
                hard_cap_hit = True
                break
            wx, wy = inverse_fn(lx, ly)
            candidates_local.append((wx, wy))
    except Exception as e:
        warnings_out.append("{0}: pattern generation failed ({1})".format(source_row.label, e))
        return []
    if hard_cap_hit:
        warnings_out.append(
            "{0}: stopped after {1} candidate points (safety cap) - spacing may be too small for "
            "this pattern/boundary. Increase spacing or reduce the target area.".format(
                source_row.label, _MAX_CANDIDATES_HARD_CAP))

    # sort by (y, x) for stable index assignment across re-runs (§8)
    candidates_local.sort(key=lambda p: (round(p[1], 6), round(p[0], 6)))

    symbol = family_choice.symbol
    obstacle_cache = _build_obstacle_cache(doc, outer_loop, _obstacle_categories_for_symbol(symbol))
    half_x, half_y = _symbol_bbox_footprint(symbol) if symbol is not None else (0.5, 0.5)

    staged = []
    level = _safe_level(source_row.spatial_element)
    for (x, y) in candidates_local:
        angle = _resolve_rotation_radians(settings, doc, x, y, outer_loop, outer_eids)
        cbbox = _candidate_bbox(x, y, half_x, half_y, angle)
        collided = _check_collision(cbbox, obstacle_cache) is not None

        if not collided:
            staged.append(StagedPlacement(family_choice, source_row, x, y, False))
            continue

        mode = settings.collision_mode
        if mode == "Move to Nearest Available Location":
            # outer_loop (not aligned_outer) - (x, y) here are already
            # world-space, mapped back via inverse_fn above.
            moved = _try_move_candidate(x, y, half_x, half_y, angle, obstacle_cache,
                                          outer_loop, hole_loops,
                                          x_spacing, y_spacing)
            if moved is not None:
                staged.append(StagedPlacement(family_choice, source_row, moved[0], moved[1],
                                               False, moved_from=(x, y)))
                continue
        # Skip / Replace / Notify User all fall through to a flagged
        # collision marker in preview; Generate-time logic decides the
        # actual per-mode behavior (§5.4)
        staged.append(StagedPlacement(family_choice, source_row, x, y, True))

    source_row.instances_planned = len(staged)
    return staged


def _try_move_candidate(x, y, half_x, half_y, angle, obstacle_cache, outer_loop, hole_loops,
                          x_spacing, y_spacing):
    step = max(min(x_spacing, y_spacing) / 4.0, 1e-6)
    offsets = []
    for radius in range(1, 9):
        r = step * radius
        for k in range(8):
            a = (2 * math.pi / 8.0) * k
            offsets.append((r * math.cos(a), r * math.sin(a)))
    for dx, dy in offsets:
        nx, ny = x + dx, y + dy
        if not distribution_patterns.point_in_boundary(nx, ny, outer_loop, hole_loops):
            continue
        cbbox = _candidate_bbox(nx, ny, half_x, half_y, angle)
        if _check_collision(cbbox, obstacle_cache) is None:
            return (nx, ny)
    return None


# --------------------------------------------------------------------------
# Presets I/O
# --------------------------------------------------------------------------
def _load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _load_builtin_presets():
    return _load_json(_PRESETS_FILE, {})


def _load_user_presets():
    return _load_json(_USER_PRESETS_FILE, {})


def _save_user_presets(data):
    _save_json(_USER_PRESETS_FILE, data)


# --------------------------------------------------------------------------
# Window
# --------------------------------------------------------------------------
class DeeDistributorWindow(forms.WPFWindow):
    def __init__(self, xaml_file, doc, uidoc):
        forms.WPFWindow.__init__(self, xaml_file)
        self.doc = doc
        self.uidoc = uidoc
        self._rows = []
        self._families = []
        self._type_index = {}
        self._family_index = {}
        self._staged_placements = []
        self._preview_dirty = True
        self._preview_scale = 1.0
        self._preview_min_x = 0.0
        self._preview_max_y = 0.0
        self._preview_origin_px = 0.0
        self._preview_origin_py = 0.0
        self._preview_width = 400.0
        self._preview_height = 400.0
        self._panning = False
        self._pan_last = None
        self._pan_transform = None
        self._loading_family_editor = False
        self._loading_lighting_tab = False

        self.dist_pattern_cb.ItemsSource = DistributionSettings.PATTERNS
        self.dist_alignment_cb.ItemsSource = DistributionSettings.ALIGNMENTS
        self.dist_rotation_cb.ItemsSource = DistributionSettings.ROTATION_MODES
        self.dist_collision_cb.ItemsSource = DistributionSettings.COLLISION_MODES
        self.dist_duplicate_mode_cb.ItemsSource = ["Skip Existing", "Replace Existing",
                                                    "Update Existing", "Ask Every Time"]
        self.dist_duplicate_mode_cb.SelectedIndex = 0

        unit_abbr = _unit_abbreviation(doc)
        self.dist_xspacing_unit_tb.Text = unit_abbr
        self.dist_yspacing_unit_tb.Text = unit_abbr
        self.dist_boundary_offset_unit_tb.Text = unit_abbr
        self.dist_fallback_height_unit_tb.Text = unit_abbr
        self.dist_column_height_unit_tb.Text = unit_abbr
        self.dist_base_offset_unit_tb.Text = unit_abbr

        self._rebuild_family_index()

        self._refresh_preset_dropdown()

        self.wizard_tabs.SelectedIndex = 0

    def _rebuild_family_index(self):
        """One single scan over every FamilySymbol in the project, building
        a nested category -> family -> type index (self._type_index) plus
        a parallel category -> family -> Family lookup (self._family_index).
        Every FamilySymbol already exposes both its own Family and
        Category, so this replaces what used to be three separate,
        repeated whole-project scans (one for categories, one for
        families-in-a-category, one for types-in-a-family) that were
        re-run from scratch on every single cascading-dropdown interaction
        in Step 2. Built once (Window init / Refresh Families), reused for
        the rest of the session.

        Each symbol is wrapped in its OWN try/except so one problematic
        FamilySymbol only skips itself - a single shared try/except around
        the whole loop was silently aborting the ENTIRE scan partway
        through on some projects, which is why some categories (e.g.
        Sprinklers) were missing entirely, or the Category list came up
        empty if it happened on one of the first few symbols.

        Only Model categories (cat.CategoryType == CategoryType.Model)
        are included - Annotation categories (tags, symbols, generic
        annotation families, etc.) are 2D, view-specific, and can't be
        distributed across a room boundary as real 3D content, so they're
        filtered out entirely rather than just cluttering the list."""
        symbols = _collect_all_family_symbols(self.doc)
        total = len(symbols)
        type_index = {}
        family_index = {}
        with forms.ProgressBar(title="DeeDistributor — scanning loaded families...",
                                cancellable=True) as pb:
            for i, fs in enumerate(symbols):
                if pb.cancelled:
                    break
                if i % 20 == 0 or i == total - 1:
                    pb.update_progress(i, total)
                try:
                    cat = fs.Category
                    fam = fs.Family
                    if cat is None or fam is None:
                        continue
                    try:
                        if cat.CategoryType != CategoryType.Model:
                            continue
                    except Exception:
                        continue
                    cat_name = _read_name(cat)
                    fam_name = _read_name(fam)
                    type_name = _read_name(fs)
                    if not (cat_name and fam_name and type_name):
                        continue
                    type_index.setdefault(cat_name, {}).setdefault(fam_name, {})[type_name] = fs
                    family_index.setdefault(cat_name, {})[fam_name] = fam
                except Exception:
                    continue

        self._type_index = type_index
        self._family_index = family_index
        self.fam_category_cb.ItemsSource = sorted(self._family_index.keys())
        self._load_family_editor()

    def refresh_families_click(self, sender, args):
        self._rebuild_family_index()
        total_types = sum(len(types) for fams in self._type_index.values() for types in fams.values())
        n_cats = len(self._family_index)
        forms.alert(
            "Scanned the project: found {0} Model categor{1} (Annotation categories are "
            "excluded), {2} loadable family type(s).\n\n"
            "If a category you expected (e.g. Sprinklers) isn't listed, no family of that "
            "category is loaded into this project yet - load one first (Insert > Load Family), "
            "the same as Revit's own Place a Component command needs.".format(
                n_cats, "y" if n_cats == 1 else "ies", total_types),
            title="DeeDistributor")

    # ---- Step 1 ----
    def scan_click(self, sender, args):
        self._scan()

    def _current_source_mode(self):
        if bool(self.src_selected_rooms_rb.IsChecked):
            return "selected_rooms"
        if bool(self.src_selected_spaces_rb.IsChecked):
            return "selected_spaces"
        if bool(self.src_active_view_rb.IsChecked):
            return "active_view"
        return "entire_project"

    def _scan(self):
        mode = self._current_source_mode()
        view = self.doc.ActiveView
        try:
            rows = _collect_spatial_elements(self.doc, mode, self.uidoc, view)
        except Exception as e:
            forms.alert("Could not scan rooms/spaces: {0}".format(e))
            return
        self._rows = rows
        self._refresh_grid_view()
        self.src_count_tb.Text = "{0} room(s)/space(s) scanned".format(len(self._rows))
        self._preview_dirty = True

    def _refresh_grid_view(self):
        self.rooms_grid.ItemsSource = None
        self.rooms_grid.ItemsSource = list(self._rows)

    def select_all_click(self, sender, args):
        for r in self._rows:
            r.selected = True
        self._refresh_grid_view()

    def deselect_all_click(self, sender, args):
        for r in self._rows:
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

    def _get_selected_rows(self):
        return [r for r in self._rows if r.selected]

    # ---- Step 2: Category / Family / Type editor for the selected row ----
    # Redesigned away from an in-grid cascading DataGridComboBoxColumn
    # (broke twice across two fix attempts on WPF cell-editing-lifecycle
    # timing) to the same proven pattern Step 3 already uses: the grid is
    # a plain read-only list, and standalone ComboBoxes below it edit
    # whichever row is currently selected. No cell-edit-commit timing to
    # get right at all - just ordinary SelectionChanged handlers.
    def add_family_click(self, sender, args):
        fc = FamilyChoice()
        user_presets = _load_user_presets()
        default_name = user_presets.get("default_preset")
        if default_name:
            builtins = _load_builtin_presets()
            preset_dict = builtins.get(default_name) or user_presets.get(default_name)
            if preset_dict:
                fc.settings.apply_preset_dict(preset_dict)
        self._families.append(fc)
        self._refresh_families_view()
        self.families_grid.SelectedItem = fc

    def remove_family_click(self, sender, args):
        row = self.families_grid.SelectedItem
        if row is None:
            forms.alert("Select a family row to remove.")
            return
        self._families.remove(row)
        self._refresh_families_view()

    def _refresh_families_view(self):
        # Reassigning ItemsSource (needed so the grid re-renders edited
        # values, since FamilyChoice isn't INotifyPropertyChanged) clears
        # the grid's SelectedItem as a side effect - explicitly restore it
        # afterward, otherwise the very next Category/Family/Type pick
        # (which calls this same method) silently finds no selected row
        # and does nothing, and worse, navigating straight to Step 3
        # afterward finds no family to apply spacing/pattern edits to
        # either - exactly the "editing Step 3 does nothing" symptom.
        selected = self.families_grid.SelectedItem
        self.families_grid.ItemsSource = None
        self.families_grid.ItemsSource = list(self._families)
        if selected in self._families:
            self.families_grid.SelectedItem = selected
        self._preview_dirty = True

    def families_grid_selection_changed(self, sender, args):
        self._load_settings_into_step3()
        self._load_family_editor()
        self._load_lighting_tab()

    def _load_family_editor(self):
        fc = self.families_grid.SelectedItem
        if fc is None:
            self.fam_editing_row_tb.Text = "No family row selected - click a row above, or Add Family Row first."
            self.fam_category_cb.SelectedItem = None
            self.fam_family_cb.ItemsSource = []
            self.fam_type_cb.ItemsSource = []
            return
        self.fam_editing_row_tb.Text = "Editing: {0}".format(fc.label)
        self._loading_family_editor = True
        try:
            self.fam_category_cb.SelectedItem = fc.category_name
            families = self._family_index.get(fc.category_name, {}) if fc.category_name else {}
            self.fam_family_cb.ItemsSource = sorted(families.keys())
            self.fam_family_cb.SelectedItem = fc.family_name
            types = (self._type_index.get(fc.category_name, {}).get(fc.family_name, {})
                     if fc.category_name and fc.family_name else {})
            self.fam_type_cb.ItemsSource = sorted(types.keys())
            self.fam_type_cb.SelectedItem = fc.type_name
        finally:
            self._loading_family_editor = False

    def category_cb_changed(self, sender, args):
        if self._loading_family_editor:
            return
        fc = self.families_grid.SelectedItem
        if fc is None:
            return
        new_cat = self.fam_category_cb.SelectedItem
        if new_cat == fc.category_name:
            return
        fc.category_name = new_cat
        fc.family_name = None
        fc.type_name = None
        fc.symbol = None
        self._loading_family_editor = True
        try:
            families = self._family_index.get(new_cat, {}) if new_cat else {}
            self.fam_family_cb.ItemsSource = sorted(families.keys())
            self.fam_family_cb.SelectedItem = None
            self.fam_type_cb.ItemsSource = []
            self.fam_type_cb.SelectedItem = None
        finally:
            self._loading_family_editor = False
        self._refresh_families_view()

    def family_cb_changed(self, sender, args):
        if self._loading_family_editor:
            return
        fc = self.families_grid.SelectedItem
        if fc is None:
            return
        new_fam = self.fam_family_cb.SelectedItem
        if new_fam == fc.family_name:
            return
        fc.family_name = new_fam
        fc.type_name = None
        fc.symbol = None
        self._loading_family_editor = True
        try:
            types = (self._type_index.get(fc.category_name, {}).get(new_fam, {})
                     if fc.category_name and new_fam else {})
            self.fam_type_cb.ItemsSource = sorted(types.keys())
            self.fam_type_cb.SelectedItem = None
        finally:
            self._loading_family_editor = False
        self._refresh_families_view()

    def type_cb_changed(self, sender, args):
        if self._loading_family_editor:
            return
        fc = self.families_grid.SelectedItem
        if fc is None:
            return
        new_type = self.fam_type_cb.SelectedItem
        if new_type == fc.type_name:
            return
        fc.type_name = new_type
        self._resolve_symbol_for_row(fc)
        self._refresh_families_view()

    def _resolve_symbol_for_row(self, fc):
        if not (fc.category_name and fc.family_name and fc.type_name):
            fc.symbol = None
            return
        fc.symbol = self._type_index.get(fc.category_name, {}).get(fc.family_name, {}).get(fc.type_name)

    # ---- Step 3: distribution settings, scoped to selected family row ----
    def _current_family_choice(self):
        return self.families_grid.SelectedItem

    def _load_settings_into_step3(self):
        fc = self._current_family_choice()
        if fc is None:
            self.dist_editing_family_tb.Text = "No family selected — go back to Step 2"
            return
        self._resolve_symbol_for_row(fc)
        self.dist_editing_family_tb.Text = "Editing settings for: {0}".format(fc.label)
        s = fc.settings
        self.dist_pattern_cb.SelectedItem = s.pattern
        self.dist_alignment_cb.SelectedItem = s.alignment
        self.dist_rotation_cb.SelectedItem = s.rotation_mode
        self.dist_collision_cb.SelectedItem = s.collision_mode
        self.dist_xspacing_tb.Text = "{0:.1f}".format(_internal_to_display(self.doc, s.x_spacing_internal))
        self.dist_yspacing_tb.Text = "{0:.1f}".format(_internal_to_display(self.doc, s.y_spacing_internal))
        self.dist_boundary_offset_tb.Text = "{0:.1f}".format(
            _internal_to_display(self.doc, s.boundary_offset_internal))
        self.dist_fixed_angle_tb.Text = "{0:.1f}".format(s.fixed_rotation_degrees)
        self.dist_lock_count_tb.Text = str(s.lock_target_count)
        if s.lock_mode == "Count-Locked":
            self.dist_lock_count_rb.IsChecked = True
        else:
            self.dist_lock_spacing_rb.IsChecked = True
        self.dist_ceiling_fallback_cb.IsChecked = s.ceiling_fallback_enabled
        self.dist_fallback_height_tb.Text = "{0:.1f}".format(
            _internal_to_display(self.doc, s.fallback_height_internal))
        self.dist_duplicate_mode_cb.SelectedItem = s.duplicate_mode
        if s.column_height_mode == "Fixed Height":
            self.dist_column_fixed_height_rb.IsChecked = True
        else:
            self.dist_column_level_above_rb.IsChecked = True
        self.dist_column_height_tb.Text = "{0:.1f}".format(
            _internal_to_display(self.doc, s.column_height_internal))
        self.dist_base_offset_tb.Text = "{0:.1f}".format(
            _internal_to_display(self.doc, s.base_offset_internal))
        self.dist_diagonal_angle_tb.Text = str(s.pattern_kwargs.get("angle_degrees", 45.0))
        self.dist_radial_ring_spacing_tb.Text = str(s.pattern_kwargs.get("ring_spacing_display", ""))
        self.dist_random_count_tb.Text = str(s.pattern_kwargs.get("target_count", 50))
        self._update_pattern_note()
        self._preview_dirty = True

    # ---- Step 5: Lighting Distribution (Uniform Grid / Single Row) ----
    def _load_lighting_tab(self):
        fc = self._current_family_choice()
        if fc is None:
            self.light_editing_family_tb.Text = "No family row selected - go back to Step 2."
            return
        self.light_editing_family_tb.Text = "Editing: {0}".format(fc.label)
        s = fc.settings
        self._loading_lighting_tab = True
        try:
            # Which sub-tab is selected IS the Normal/Lighting toggle for
            # this family row - mutually exclusive by construction, since
            # a TabControl can only ever have one selected child.
            self.distribution_subtabs.SelectedIndex = 1 if s.lighting_mode_enabled else 0
            if s.lighting_pattern == "Single Row":
                self.light_pattern_row_rb.IsChecked = True
            else:
                self.light_pattern_uniform_rb.IsChecked = True
            if s.lighting_row_position_mode == "Custom Offset":
                self.light_row_custom_rb.IsChecked = True
            else:
                self.light_row_centered_rb.IsChecked = True
            self.light_row_offset_tb.Text = "{0:.1f}".format(
                _internal_to_display(self.doc, s.lighting_row_offset_internal))
            self.light_row_offset_unit_tb.Text = _unit_abbreviation(self.doc)
        finally:
            self._loading_lighting_tab = False
        self._update_distribution_subtab_enabled_state()

    def _update_distribution_subtab_enabled_state(self):
        """Only one of Normal Distribution / Lighting Distribution is ever
        in effect for the selected family row - gray out the inactive
        one's controls entirely so it's visually unambiguous which
        settings actually apply, rather than leaving both editable at
        once."""
        lighting_active = self.distribution_subtabs.SelectedIndex == 1
        self.normal_distribution_panel.IsEnabled = not lighting_active
        self.lighting_distribution_panel.IsEnabled = lighting_active

    def distribution_subtabs_selection_changed(self, sender, args):
        self._update_distribution_subtab_enabled_state()
        if self._loading_lighting_tab:
            return
        fc = self._current_family_choice()
        if fc is None:
            return
        fc.settings.lighting_mode_enabled = (self.distribution_subtabs.SelectedIndex == 1)
        self._preview_dirty = True

    def light_pattern_changed(self, sender, args):
        if self._loading_lighting_tab:
            return
        fc = self._current_family_choice()
        if fc is None:
            return
        fc.settings.lighting_pattern = "Single Row" if bool(self.light_pattern_row_rb.IsChecked) else "Uniform Grid"
        self._preview_dirty = True

    def light_row_position_changed(self, sender, args):
        if self._loading_lighting_tab:
            return
        fc = self._current_family_choice()
        if fc is None:
            return
        s = fc.settings
        s.lighting_row_position_mode = "Custom Offset" if bool(self.light_row_custom_rb.IsChecked) else "Centered"
        s.lighting_row_offset_internal = _display_to_internal(
            self.doc, self._safe_float(self.light_row_offset_tb.Text, 0.0))
        self._preview_dirty = True

    def _safe_float(self, text, default):
        try:
            return float(text)
        except Exception:
            return default

    def _safe_int(self, text, default):
        try:
            return int(float(text))
        except Exception:
            return default

    def pattern_changed(self, sender, args):
        fc = self._current_family_choice()
        if fc is None:
            return
        pattern = self.dist_pattern_cb.SelectedItem
        if pattern == "Custom Pattern...":
            forms.alert(
                "No custom pattern is registered yet - add one to "
                "distribution_patterns.PATTERN_REGISTRY.", title="DeeDistributor")
        fc.settings.pattern = pattern
        self._update_pattern_note()
        self._preview_dirty = True

    def _update_pattern_note(self):
        notes = {
            "Grid": "Grid: regular rows/columns starting at the boundary's min-corner.",
            "Offset Grid (Brick)": "Offset Grid (Brick): running-bond, odd rows shifted by half X Spacing.",
            "Centered Grid": "Centered Grid: grid expands symmetrically outward from the boundary's center.",
            "Boundary Offset Grid": "Boundary Offset Grid: maintains equal margins around the boundary.",
            "Diagonal": "Diagonal: rows follow a rotated grid at the given angle.",
            "Radial": "Radial: concentric rings of points around a center point.",
            "Perimeter": "Perimeter: points placed only along the boundary, not the interior.",
            "Random": "Random: rejection-sampled random placement with a minimum spacing.",
            "Custom Pattern...": "Custom Pattern: not yet registered - choose a different pattern.",
        }
        self.dist_pattern_note_tb.Text = notes.get(self.dist_pattern_cb.SelectedItem, "")

    def pattern_kwarg_changed(self, sender, args):
        fc = self._current_family_choice()
        if fc is None:
            return
        fc.settings.pattern_kwargs["angle_degrees"] = self._safe_float(self.dist_diagonal_angle_tb.Text, 45.0)
        ring_txt = (self.dist_radial_ring_spacing_tb.Text or "").strip()
        if ring_txt:
            fc.settings.pattern_kwargs["ring_spacing_display"] = ring_txt
            fc.settings.pattern_kwargs["ring_spacing"] = _display_to_internal(
                self.doc, self._safe_float(ring_txt, 0.0))
        else:
            fc.settings.pattern_kwargs.pop("ring_spacing", None)
            fc.settings.pattern_kwargs.pop("ring_spacing_display", None)
        fc.settings.pattern_kwargs["target_count"] = self._safe_int(self.dist_random_count_tb.Text, 50)
        self._preview_dirty = True

    def settings_field_changed(self, sender, args):
        fc = self._current_family_choice()
        if fc is None:
            return
        s = fc.settings
        # Fall back to the EXISTING spacing (not 0.0) when the textbox is
        # momentarily empty/unparseable - TextChanged fires on every
        # keystroke, including the instant between clearing old digits
        # and typing new ones, and a stray 0 spacing that survives
        # (e.g. if the user tabs away mid-edit) is exactly what caused
        # the pattern-generation math to blow up into a nonsensical
        # instance-count estimate.
        prev_x_mm = _internal_to_display(self.doc, s.x_spacing_internal)
        prev_y_mm = _internal_to_display(self.doc, s.y_spacing_internal)
        s.x_spacing_internal = _display_to_internal(self.doc, self._safe_float(self.dist_xspacing_tb.Text, prev_x_mm))
        s.y_spacing_internal = _display_to_internal(self.doc, self._safe_float(self.dist_yspacing_tb.Text, prev_y_mm))
        # Same fallback-to-existing-value reasoning as spacing above - an
        # unintended 0 boundary offset (from a momentarily empty textbox)
        # is indistinguishable from a deliberate one otherwise, and
        # silently removes the margin from walls the user actually set.
        prev_offset_mm = _internal_to_display(self.doc, s.boundary_offset_internal)
        s.boundary_offset_internal = _display_to_internal(
            self.doc, self._safe_float(self.dist_boundary_offset_tb.Text, prev_offset_mm))
        s.alignment = self.dist_alignment_cb.SelectedItem or s.alignment
        s.collision_mode = self.dist_collision_cb.SelectedItem or s.collision_mode
        s.fixed_rotation_degrees = self._safe_float(self.dist_fixed_angle_tb.Text, 0.0)
        s.lock_target_count = self._safe_int(self.dist_lock_count_tb.Text, 0)
        s.ceiling_fallback_enabled = bool(self.dist_ceiling_fallback_cb.IsChecked)
        s.fallback_height_internal = _display_to_internal(
            self.doc, self._safe_float(self.dist_fallback_height_tb.Text, 3000.0))
        s.duplicate_mode = self.dist_duplicate_mode_cb.SelectedItem or s.duplicate_mode
        s.column_height_mode = ("Fixed Height" if bool(self.dist_column_fixed_height_rb.IsChecked)
                                 else "Attach to Level Above")
        s.column_height_internal = _display_to_internal(
            self.doc, self._safe_float(self.dist_column_height_tb.Text, 3000.0))
        s.base_offset_internal = _display_to_internal(
            self.doc, self._safe_float(self.dist_base_offset_tb.Text, 0.0))
        self._preview_dirty = True

    def rotation_mode_changed(self, sender, args):
        fc = self._current_family_choice()
        mode = self.dist_rotation_cb.SelectedItem
        self.dist_fixed_angle_tb.IsEnabled = mode in ("Fixed Rotation Angle", "Custom Rotation")
        if fc is not None:
            fc.settings.rotation_mode = mode
        self._preview_dirty = True

    def lock_mode_changed(self, sender, args):
        fc = self._current_family_choice()
        is_count_locked = bool(self.dist_lock_count_rb.IsChecked)
        self.dist_lock_count_tb.IsEnabled = is_count_locked
        if fc is not None:
            fc.settings.lock_mode = "Count-Locked" if is_count_locked else "Spacing-Locked"
        self._preview_dirty = True

    def apply_settings_to_all_click(self, sender, args):
        fc = self._current_family_choice()
        if fc is None:
            forms.alert("Select a family row in Step 2 first.")
            return
        for other in self._families:
            if other is fc:
                continue
            other.settings = fc.settings.copy()
        forms.alert("Applied current settings to all {0} family row(s).".format(len(self._families)))
        self._preview_dirty = True

    # ---- Step 4: presets ----
    def _refresh_preset_dropdown(self):
        builtins = sorted(_load_builtin_presets().keys())
        user_presets = _load_user_presets()
        user_names = sorted(k for k in user_presets.keys() if k != "default_preset")
        items = list(builtins)
        if user_names:
            items.append("--- User Presets ---")
            items.extend(user_names)
        self.preset_cb.ItemsSource = items
        if items:
            self.preset_cb.SelectedIndex = 0

    def preset_selected_changed(self, sender, args):
        pass

    def _is_builtin_preset(self, name):
        return name in _load_builtin_presets()

    def preset_apply_click(self, sender, args):
        fc = self._current_family_choice()
        if fc is None:
            forms.alert("Select a family row in Step 2 first.")
            return
        name = self.preset_cb.SelectedItem
        if not name or name == "--- User Presets ---":
            forms.alert("Select a preset first.")
            return
        builtins = _load_builtin_presets()
        user_presets = _load_user_presets()
        preset_dict = builtins.get(name) or user_presets.get(name)
        if preset_dict is None:
            forms.alert("Preset '{0}' not found.".format(name))
            return
        fc.settings.apply_preset_dict(preset_dict)
        self._load_settings_into_step3()
        self._refresh_families_view()

    def preset_save_click(self, sender, args):
        fc = self._current_family_choice()
        if fc is None:
            forms.alert("Select a family row in Step 2 first (its current settings will be saved).")
            return
        name = forms.ask_for_string(default="My Preset", prompt="Preset name:", title="DeeDistributor - Save Preset")
        if not name:
            return
        if self._is_builtin_preset(name):
            forms.alert("'{0}' is a built-in preset name and cannot be overwritten.".format(name))
            return
        user_presets = _load_user_presets()
        user_presets[name] = fc.settings.to_preset_dict(self.doc)
        _save_user_presets(user_presets)
        self._refresh_preset_dropdown()

    def preset_rename_click(self, sender, args):
        name = self.preset_cb.SelectedItem
        if not name or name == "--- User Presets ---":
            forms.alert("Select a user preset first.")
            return
        if self._is_builtin_preset(name):
            forms.alert("Built-in presets are read-only and cannot be renamed.")
            return
        user_presets = _load_user_presets()
        if name not in user_presets:
            forms.alert("Preset '{0}' not found among user presets.".format(name))
            return
        new_name = forms.ask_for_string(default=name, prompt="New name:", title="DeeDistributor - Rename Preset")
        if not new_name or new_name == name:
            return
        user_presets[new_name] = user_presets.pop(name)
        _save_user_presets(user_presets)
        self._refresh_preset_dropdown()

    def preset_delete_click(self, sender, args):
        name = self.preset_cb.SelectedItem
        if not name or name == "--- User Presets ---":
            forms.alert("Select a user preset first.")
            return
        if self._is_builtin_preset(name):
            forms.alert("Built-in presets are read-only and cannot be deleted.")
            return
        user_presets = _load_user_presets()
        if name in user_presets:
            del user_presets[name]
            _save_user_presets(user_presets)
        self._refresh_preset_dropdown()

    def preset_export_click(self, sender, args):
        name = self.preset_cb.SelectedItem
        if not name or name == "--- User Presets ---":
            forms.alert("Select a preset first.")
            return
        builtins = _load_builtin_presets()
        user_presets = _load_user_presets()
        preset_dict = builtins.get(name) or user_presets.get(name)
        if preset_dict is None:
            forms.alert("Preset not found.")
            return
        dlg = SaveFileDialog()
        dlg.Filter = "JSON (*.json)|*.json"
        dlg.FileName = "{0}.json".format(name)
        if dlg.ShowDialog() != DialogResult.OK:
            return
        try:
            with open(dlg.FileName, "w") as f:
                json.dump({name: preset_dict}, f, indent=2)
        except Exception as e:
            forms.alert("Could not export: {0}".format(e))
            return
        MessageBox.Show("Exported preset '{0}' to:\n{1}".format(name, dlg.FileName), "DeeDistributor")

    def preset_import_click(self, sender, args):
        dlg = OpenFileDialog()
        dlg.Filter = "JSON (*.json)|*.json"
        if dlg.ShowDialog() != DialogResult.OK:
            return
        try:
            with open(dlg.FileName, "r") as f:
                imported = json.load(f)
        except Exception as e:
            forms.alert("Could not import: {0}".format(e))
            return
        user_presets = _load_user_presets()
        count = 0
        for k, v in imported.items():
            if k == "default_preset":
                continue
            user_presets[k] = v
            count += 1
        _save_user_presets(user_presets)
        self._refresh_preset_dropdown()
        MessageBox.Show("Imported {0} preset(s).".format(count), "DeeDistributor")

    def preset_set_default_changed(self, sender, args):
        name = self.preset_cb.SelectedItem
        user_presets = _load_user_presets()
        if bool(self.preset_set_default_cb.IsChecked) and name and name != "--- User Presets ---":
            user_presets["default_preset"] = name
        else:
            user_presets.pop("default_preset", None)
        _save_user_presets(user_presets)

    # ---- Step 5: preview & generate ----
    def preview_click(self, sender, args):
        self._run_preview()

    def refresh_preview_click(self, sender, args):
        self._run_preview()

    def cancel_preview_click(self, sender, args):
        self._staged_placements = []
        self.preview_canvas.Children.Clear()
        self.prev_summary_tb.Text = ""
        self.prev_warnings_lb.ItemsSource = []

    def _run_preview(self):
        rows = self._get_selected_rows()
        if not rows:
            forms.alert("No rooms/spaces selected. Go back to Step 1 and select at least one.")
            return
        enabled_families = [f for f in self._families if f.enabled]
        for f in enabled_families:
            self._resolve_symbol_for_row(f)
        enabled_families = [f for f in enabled_families if f.symbol is not None]
        if not enabled_families:
            forms.alert("No enabled family rows with a resolved Category/Family/Type. Check Step 2.")
            return

        # Informational check: a family whose own footprint is bigger than
        # its configured spacing WILL overlap its own neighbors - collision
        # detection only checks candidates against real, already-existing
        # model elements (walls, columns, etc), never against other
        # candidate points from the same batch, so this kind of overlap
        # would otherwise pass through silently with zero collision
        # warnings shown.
        footprint_warnings = []
        for fc in enabled_families:
            half_x, half_y = _symbol_bbox_footprint(fc.symbol)
            fam_w = half_x * 2.0
            fam_h = half_y * 2.0
            s = fc.settings
            if _effective_pattern_name(s) != "Random" and (s.x_spacing_internal < fam_w or s.y_spacing_internal < fam_h):
                footprint_warnings.append(
                    "{0}: footprint is {1:.0f} x {2:.0f} {5}, but spacing is {3:.0f} x {4:.0f} {5} - "
                    "instances will very likely overlap EACH OTHER (not just the room boundary), "
                    "which DeeDistributor's collision detection does not catch (it only checks "
                    "against existing model elements). Increase spacing in Step 3 if that's not "
                    "intended.".format(
                        fc.label,
                        _internal_to_display(self.doc, fam_w), _internal_to_display(self.doc, fam_h),
                        _internal_to_display(self.doc, s.x_spacing_internal),
                        _internal_to_display(self.doc, s.y_spacing_internal),
                        _unit_abbreviation(self.doc)))

        for f in enabled_families:
            _ensure_symbol_active(self.doc, f.symbol)

        # Safety pre-check: cheaply ESTIMATE the total candidate count
        # across every selected room/space x enabled-family pair BEFORE
        # running the real (potentially slow) pattern generation. A
        # mis-set spacing or lock-count otherwise wouldn't surface as a
        # problem until Revit is already busy computing tens of thousands
        # of points and looks frozen.
        total_estimate = 0
        low_spacing_labels = []
        for row in rows:
            outer_loop, _holes, _eids, _z = _get_boundary_loops_xy(row.spatial_element)
            if outer_loop is None:
                continue
            for fc in enabled_families:
                s = fc.settings
                x_sp, y_sp = _resolve_effective_spacing(outer_loop, s)
                if _effective_pattern_name(s) != "Random" and (x_sp < _MIN_SPACING_INTERNAL or y_sp < _MIN_SPACING_INTERNAL):
                    low_spacing_labels.append("{0} / {1}".format(row.label, fc.label))
                    continue
                total_estimate += _estimate_candidate_count(outer_loop, s, x_sp, y_sp)

        if low_spacing_labels:
            forms.alert(
                "X/Y Spacing resolves to below 1mm for {0} room/family pair(s), e.g.:\n\n{1}\n\n"
                "This would try to create an unreasonable number of instances and can freeze "
                "Revit, so Preview was stopped before doing any work. Go to Step 3 and fix the "
                "spacing (or the Count-Locked target count) for the affected family/families, "
                "then try Preview again.".format(
                    len(low_spacing_labels), "\n".join(low_spacing_labels[:10])),
                title="DeeDistributor - Spacing Too Small")
            return

        if total_estimate > _MAX_CANDIDATES_WARN:
            if not forms.alert(
                    "This would attempt to create roughly {0} family instances across the "
                    "selected room(s)/space(s). That's a lot - computing and placing this many "
                    "can take a long time and may make Revit unresponsive while it runs.\n\n"
                    "Check your X/Y Spacing (or Count-Locked target count) in Step 3 if this "
                    "number looks too high.\n\nContinue anyway?".format(total_estimate),
                    title="DeeDistributor - Large Instance Count", yes=True, no=True):
                return

        warnings = list(footprint_warnings)
        staged = []
        total_pairs = len(rows) * len(enabled_families)
        with forms.ProgressBar(title="DeeDistributor — computing preview...", cancellable=True) as pb:
            i = 0
            for row in rows:
                for fc in enabled_families:
                    if pb.cancelled:
                        break
                    pb.update_progress(i, total_pairs)
                    i += 1
                    staged.extend(_generate_candidates_for_pair(self.doc, fc, row, warnings))
                if pb.cancelled:
                    break

        self._staged_placements = staged
        self._preview_dirty = False

        collisions = sum(1 for s in staged if s.collision)
        pattern_names = sorted(set(_effective_pattern_name(f.settings) for f in enabled_families))
        avg_x = sum(f.settings.x_spacing_internal for f in enabled_families) / max(1, len(enabled_families))
        avg_y = sum(f.settings.y_spacing_internal for f in enabled_families) / max(1, len(enabled_families))
        avg_offset = sum(f.settings.boundary_offset_internal for f in enabled_families) / max(1, len(enabled_families))

        lines = [
            "Selected Rooms/Spaces: {0}".format(len(rows)),
            "Total Families to Create: {0}".format(len(staged)),
            "Pattern Name(s): {0}".format(", ".join(pattern_names)),
            "Average Spacing: {0:.1f} x {1:.1f} {2}".format(
                _internal_to_display(self.doc, avg_x), _internal_to_display(self.doc, avg_y),
                _unit_abbreviation(self.doc)),
            "Boundary Offset: {0:.1f} {1}".format(
                _internal_to_display(self.doc, avg_offset), _unit_abbreviation(self.doc)),
            "Collision Warnings: {0}".format(collisions),
        ]
        self.prev_summary_tb.Text = "\n".join(lines)
        self.prev_warnings_lb.ItemsSource = warnings
        self._refresh_grid_view()
        self._rebuild_previewer(fit=True)

    # -- 2D preview canvas: world <-> pixel mapping, adapted from DeeGrid --
    def _prev_x_to_px(self, x):
        return self._preview_origin_px + (x - self._preview_min_x) * self._preview_scale

    def _prev_y_to_py(self, y):
        return self._preview_origin_py + (self._preview_max_y - y) * self._preview_scale

    def _prev_px_to_x(self, px):
        return self._preview_min_x + (px - self._preview_origin_px) / self._preview_scale

    def _prev_py_to_y(self, py):
        return self._preview_max_y - (py - self._preview_origin_py) / self._preview_scale

    def preview_canvas_size_changed(self, sender, args):
        self._rebuild_previewer(fit=False)

    def zoom_fit_click(self, sender, args):
        self._rebuild_previewer(fit=True)

    def zoom_in_click(self, sender, args):
        self._preview_scale *= 1.25
        self._rebuild_previewer(fit=False, keep_scale=True)

    def zoom_out_click(self, sender, args):
        self._preview_scale *= 0.8
        self._rebuild_previewer(fit=False, keep_scale=True)

    def _rebuild_previewer(self, fit=True, keep_scale=False):
        canvas = self.preview_canvas
        canvas.Children.Clear()

        rows = self._get_selected_rows()
        width = canvas.ActualWidth if canvas.ActualWidth > 1 else 400.0
        height = canvas.ActualHeight if canvas.ActualHeight > 1 else 400.0
        self._preview_width = width
        self._preview_height = height

        if not rows:
            return

        all_pts = []
        for row in rows:
            outer_loop, hole_loops, _eids, _z = _get_boundary_loops_xy(row.spatial_element)
            if outer_loop:
                all_pts.extend(outer_loop)

        if not all_pts and not self._staged_placements:
            return

        if fit or not keep_scale:
            xs = [p[0] for p in all_pts] + [s.x for s in self._staged_placements]
            ys = [p[1] for p in all_pts] + [s.y for s in self._staged_placements]
            if not xs:
                return
            margin = _mm_to_internal(500.0)
            min_x, max_x = min(xs) - margin, max(xs) + margin
            min_y, max_y = min(ys) - margin, max(ys) + margin
            range_x = max(max_x - min_x, 1e-6)
            range_y = max(max_y - min_y, 1e-6)
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

        for row in rows:
            outer_loop, hole_loops, _eids, _z = _get_boundary_loops_xy(row.spatial_element)
            if outer_loop:
                self._draw_loop(canvas, outer_loop, _OUTER_BRUSH, 1.5, False)
            for h in hole_loops:
                self._draw_loop(canvas, h, _HOLE_BRUSH, 1.0, True)

        family_colors = {}
        for i, fc in enumerate(self._families):
            family_colors[id(fc)] = SolidColorBrush(Color.FromRgb(*_LEGEND_COLORS[i % len(_LEGEND_COLORS)]))

        for s in self._staged_placements:
            brush = _COLLISION_BRUSH if s.collision else family_colors.get(
                id(s.family_choice), _OUTER_BRUSH)
            if s.moved_from is not None:
                self._draw_marker(canvas, s.moved_from[0], s.moved_from[1], _OUTER_BRUSH, small=True)
            self._draw_marker(canvas, s.x, s.y, brush, small=False)

        self._draw_legend(canvas)

        bg_hit = Rectangle()
        bg_hit.Fill = Brushes.Transparent
        bg_hit.Width = width
        bg_hit.Height = height
        Canvas.SetLeft(bg_hit, 0)
        Canvas.SetTop(bg_hit, 0)
        bg_hit.MouseLeftButtonDown += self._pan_mouse_down
        bg_hit.MouseMove += self._pan_mouse_move
        bg_hit.MouseLeftButtonUp += self._pan_mouse_up
        canvas.Children.Insert(0, bg_hit)

    def _draw_loop(self, canvas, loop, brush, thickness, dashed):
        poly = Polygon()
        poly.Stroke = brush
        poly.StrokeThickness = thickness
        poly.Fill = Brushes.Transparent
        if dashed:
            from System.Windows.Media import DoubleCollection
            dashes = DoubleCollection()
            dashes.Add(4)
            dashes.Add(2)
            poly.StrokeDashArray = dashes
        pts = PointCollection()
        for x, y in loop:
            pts.Add(WpfPoint(self._prev_x_to_px(x), self._prev_y_to_py(y)))
        poly.Points = pts
        canvas.Children.Add(poly)

    def _draw_marker(self, canvas, x, y, brush, small=False):
        r = 3 if small else 4
        e = Ellipse()
        e.Width = r * 2
        e.Height = r * 2
        e.Fill = brush
        px = self._prev_x_to_px(x)
        py = self._prev_y_to_py(y)
        Canvas.SetLeft(e, px - r)
        Canvas.SetTop(e, py - r)
        canvas.Children.Add(e)

    def _draw_legend(self, canvas):
        y_off = 6.0
        for i, fc in enumerate(self._families):
            if not fc.enabled:
                continue
            brush = SolidColorBrush(Color.FromRgb(*_LEGEND_COLORS[i % len(_LEGEND_COLORS)]))
            swatch = Ellipse()
            swatch.Width = 8
            swatch.Height = 8
            swatch.Fill = brush
            Canvas.SetLeft(swatch, 6)
            Canvas.SetTop(swatch, y_off)
            canvas.Children.Add(swatch)

            label = TextBlock()
            label.Text = fc.label
            label.FontSize = 10
            Canvas.SetLeft(label, 18)
            Canvas.SetTop(label, y_off - 6)
            canvas.Children.Add(label)
            y_off += 16.0

        collision_swatch = Ellipse()
        collision_swatch.Width = 8
        collision_swatch.Height = 8
        collision_swatch.Fill = _COLLISION_BRUSH
        Canvas.SetLeft(collision_swatch, 6)
        Canvas.SetTop(collision_swatch, y_off)
        canvas.Children.Add(collision_swatch)
        collision_label = TextBlock()
        collision_label.Text = "Collision detected"
        collision_label.FontSize = 10
        Canvas.SetLeft(collision_label, 18)
        Canvas.SetTop(collision_label, y_off - 6)
        canvas.Children.Add(collision_label)

    def _pan_mouse_down(self, sender, args):
        self._panning = True
        self._pan_last = args.GetPosition(self.preview_canvas)
        self._pan_transform = TranslateTransform(0, 0)
        self.preview_canvas.RenderTransform = self._pan_transform
        try:
            sender.CaptureMouse()
        except Exception:
            pass
        args.Handled = True

    def _pan_mouse_move(self, sender, args):
        if not self._panning or args.LeftButton != MouseButtonState.Pressed:
            return
        pos = args.GetPosition(self.preview_canvas)
        dx = pos.X - self._pan_last.X
        dy = pos.Y - self._pan_last.Y
        # In-place move during the active gesture: shift the whole canvas
        # via RenderTransform instead of touching Children. Calling
        # _rebuild_previewer() here would Clear() and recreate bg_hit -
        # the element that owns CaptureMouse() - killing the drag after
        # the first tick (the same mid-drag-rebuild pitfall already found
        # and fixed in this codebase's DeeGrid/DeeLevels 2D previewers).
        # The full rebuild is deferred to _pan_mouse_up, once the gesture
        # has ended.
        self._pan_transform.X += dx
        self._pan_transform.Y += dy
        self._pan_last = pos
        args.Handled = True

    def _pan_mouse_up(self, sender, args):
        self._panning = False
        if self._pan_transform is not None:
            self._preview_origin_px += self._pan_transform.X
            self._preview_origin_py += self._pan_transform.Y
            self._pan_transform = None
        self.preview_canvas.RenderTransform = None
        try:
            sender.ReleaseMouseCapture()
        except Exception:
            pass
        self._rebuild_previewer(fit=False, keep_scale=True)
        args.Handled = True

    # ---- Generate ----
    def generate_click(self, sender, args):
        if self._preview_dirty:
            forms.alert("Settings changed since the last Preview - click Preview/Refresh Preview first.")
            return
        if not self._staged_placements:
            forms.alert("Nothing staged to generate - click Preview first.")
            return
        if not forms.alert(
                "Generate {0} staged family instance(s)? This creates model elements in one "
                "Transaction.".format(len(self._staged_placements)),
                title="DeeDistributor", yes=True, no=True):
            return

        results = []
        total = len(self._staged_placements)
        with forms.ProgressBar(title="DeeDistributor — generating instances...", cancellable=True) as pb:
            t = Transaction(self.doc, "DeeDistributor - Generate Family Instances")
            t.Start()
            try:
                # FamilySymbol.Activate() modifies the document (loads its
                # geometry), so it requires an open Transaction - it was
                # previously only attempted during Preview, which
                # deliberately has none open ("nothing touches the model
                # until Generate"), so it silently failed there and every
                # placement below hit Revit's own "symbol is not active"
                # error. Activate everything actually used here, now that
                # a Transaction is genuinely open.
                seen_symbol_ids = set()
                for sp in self._staged_placements:
                    sym = sp.family_choice.symbol
                    if sym is None:
                        continue
                    sid = _element_id_value(sym.Id)
                    if sid in seen_symbol_ids:
                        continue
                    seen_symbol_ids.add(sid)
                    _ensure_symbol_active(self.doc, sym)

                by_room_family = {}
                for sp in self._staged_placements:
                    key = (id(sp.source_row), id(sp.family_choice))
                    by_room_family.setdefault(key, []).append(sp)

                # One Reference Plane per (level, fallback height), reused
                # across every room/instance that needs it this run - not
                # one per placed instance (see
                # _get_or_create_fallback_reference_plane).
                ref_plane_cache = {}
                index_counters = {}
                i = 0
                for sp in self._staged_placements:
                    if pb.cancelled:
                        results.append((sp.source_row.label, "-", None, "Cancelled"))
                        break
                    pb.update_progress(i, total)
                    i += 1
                    self._generate_one(sp, index_counters, results, ref_plane_cache)
                t.Commit()
            except Exception as e:
                t.RollBack()
                forms.alert("Generation aborted: {0}".format(e))
                return

        self._render_report(results)
        self._refresh_grid_view()

    def _generate_one(self, sp, index_counters, results, ref_plane_cache):
        fc = sp.family_choice
        row = sp.source_row
        settings = fc.settings
        symbol = fc.symbol
        label = "{0} / {1}".format(row.label, fc.label)

        if symbol is None:
            results.append((label, "-", False, "No resolved Family Symbol"))
            return

        room_id = row.spatial_element.Id
        symbol_id = symbol.Id
        key = (_element_id_value(room_id), _element_id_value(symbol_id))
        idx = index_counters.get(key, 0)
        index_counters[key] = idx + 1

        category_id = None
        try:
            category_id = symbol.Category.Id
        except Exception:
            category_id = None

        if idx == 0 and category_id is not None:
            existing = _find_existing_by_tag(self.doc, category_id, room_id, symbol_id)
            if existing:
                action, ok, detail = self._handle_existing_for_room_family(
                    existing, settings.duplicate_mode, label)
                if action == "done":
                    results.append((label, "Duplicate", ok, detail))
                    if action == "done" and ok is None and detail and "Skipped" in detail:
                        return

        x, y = sp.x, sp.y
        outer_loop, hole_loops, outer_eids, z = _get_boundary_loops_xy(row.spatial_element)
        if outer_loop is None:
            results.append((label, "Place", False, "No valid boundary at generate time"))
            return
        level = _safe_level(row.spatial_element)
        level_elev = level.Elevation if level is not None else 0.0
        place_z = level_elev

        warnings_out = []
        collision = sp.collision
        if collision:
            mode = settings.collision_mode
            if mode == "Skip":
                results.append((label, "Place", None, "Collision at ({0:.2f},{1:.2f}) - skipped".format(x, y)))
                return
            if mode == "Notify User":
                results.append((label, "Place", None,
                                 "Collision at ({0:.2f},{1:.2f}) - flagged for user review".format(x, y)))
                return
            if mode == "Replace Existing Instance":
                obstacle_cache = _build_obstacle_cache(
                    self.doc, outer_loop, _obstacle_categories_for_symbol(symbol))
                half_x, half_y = _symbol_bbox_footprint(symbol)
                angle = _resolve_rotation_radians(settings, self.doc, x, y, outer_loop, outer_eids)
                cbbox = _candidate_bbox(x, y, half_x, half_y, angle)
                obstacle = _check_collision(cbbox, obstacle_cache)
                tag = _read_tag(obstacle) if obstacle is not None else None
                prefix = _tag_prefix_for_lookup(room_id, symbol_id)
                if obstacle is not None and tag and tag.startswith(prefix):
                    try:
                        self.doc.Delete(obstacle.Id)
                    except Exception:
                        pass
                else:
                    results.append((label, "Place", None,
                                     "Collision with non-DeeDistributor element - skipped (Replace only "
                                     "replaces DeeDistributor's own prior instances)"))
                    return
            # "Move to Nearest Available Location" is already resolved at
            # preview time (sp.collision would be False if a move
            # succeeded) - if we get here with collision True, no free
            # spot was found, so fall back to Skip.
            elif mode == "Move to Nearest Available Location":
                results.append((label, "Place", None,
                                 "Collision - no nearby free spot found - skipped"))
                return

        angle = _resolve_rotation_radians(settings, self.doc, x, y, outer_loop, outer_eids)
        instance, detail = _place_instance(
            self.doc, symbol, x, y, place_z, level, outer_eids, settings, warnings_out,
            row.spatial_element, ref_plane_cache)

        if instance is None:
            results.append((label, "Place", False, detail))
            return

        _tag_element(instance, room_id, symbol_id, idx)
        _apply_rotation(self.doc, instance, angle, XYZ(x, y, place_z))
        row.instances_created += 1
        results.append((label, "Place", True, detail))
        for w in warnings_out:
            results.append((label, "Warning", None, w))

    def _handle_existing_for_room_family(self, existing, dup_mode, label):
        mode = dup_mode
        if mode == "Ask Every Time":
            choice = forms.SelectFromList.show(
                ["Skip", "Replace", "Update"], multiselect=False,
                title="DeeDistributor - Existing instances found for {0}".format(label))
            if not choice or choice == "Skip":
                return ("done", None, "Skipped - existing instances found ({0})".format(label))
            mode = choice + " Existing"

        if mode == "Skip Existing":
            return ("done", None, "Skipped - existing DeeDistributor instances already present")
        if mode == "Update Existing":
            return ("done", True, "Existing instances left in place (Update mode - per-point "
                                   "collision/replace logic still applies for new points)")
        # Replace Existing
        for e in existing:
            try:
                self.doc.Delete(e.Id)
            except Exception:
                pass
        return ("done", True, "Deleted {0} existing DeeDistributor instance(s) before regenerating".format(
            len(existing)))

    def _render_report(self, results):
        html = '<h2 style="font-family:sans-serif;color:#ddd;">DeeDistributor Results</h2>'
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
            '<hr><b style="font-family:sans-serif;">{0} succeeded, {1} failed, {2} skipped/flagged '
            '(out of {3} operation(s)).</b>'.format(ok_count, fail_count, skip_count, len(results)))
        output.print_html(html)

    def export_csv_click(self, sender, args):
        if not self._rows:
            forms.alert("Nothing to export - scan rooms/spaces first.")
            return
        dlg = SaveFileDialog()
        dlg.Filter = "CSV (*.csv)|*.csv"
        dlg.FileName = "DeeDistributor_Rooms.csv"
        if dlg.ShowDialog() != DialogResult.OK:
            return
        try:
            with open(dlg.FileName, "wb") as f:
                writer = csv.writer(f)
                writer.writerow(["Kind", "Number", "Name", "Level", "Area",
                                  "Planned Instances", "Created Instances", "Status"])
                for r in self._rows:
                    writer.writerow([r.kind, r.number, r.name, r.level_name, r.area_text,
                                      r.instances_planned, r.instances_created, r.status_text])
        except Exception as e:
            forms.alert("Could not export: {0}".format(e))
            return
        MessageBox.Show("Exported {0} row(s) to:\n{1}".format(len(self._rows), dlg.FileName), "DeeDistributor")

    # ---- nav ----
    def back_click(self, sender, args):
        if self.wizard_tabs.SelectedIndex > 0:
            self.wizard_tabs.SelectedIndex -= 1

    def next_click(self, sender, args):
        if self.wizard_tabs.SelectedIndex < self.wizard_tabs.Items.Count - 1:
            self.wizard_tabs.SelectedIndex += 1
            if self.wizard_tabs.SelectedIndex == 2:
                self._load_settings_into_step3()
                self._load_lighting_tab()

    def close_click(self, sender, args):
        self.Close()


def main():
    doc = __revit__.ActiveUIDocument.Document
    uidoc = __revit__.ActiveUIDocument
    window = DeeDistributorWindow(_XAML_FILE, doc, uidoc)
    window.show(modal=True)


main()
