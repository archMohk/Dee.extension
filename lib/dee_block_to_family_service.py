# -*- coding: utf-8 -*-
"""
dee_block_to_family_service
Scans a linked or imported CAD (DWG) file already in the project for
AutoCAD block references, lets the user identify one block TYPE by
clicking a single visible occurrence of it in an open Revit view, then
places a chosen Revit Family instance at every OTHER occurrence of
that same block type found in the file - matching each occurrence's
position and rotation. The CAD/DWG geometry itself is never touched,
deleted, or modified (explicit user requirement).

--------------------------------------------------------------------
Revit API facts relied on here (WebFetch/WebSearch-researched against
Autodesk's Revit API forum, the pyRevit forum, and Jeremy Tammik's
Building Coder blog this session - not guessed)
--------------------------------------------------------------------
- Both "Import CAD" and "Link CAD" produce an ImportInstance element,
  same as lib/dee_getdwg_service.py already established - scan_cad_
  instances() below is a local copy of that same collection pattern
  (per this codebase's own convention of never sharing that kind of
  small helper across files - an earlier tool crashed Revit when a
  shared helper module's location changed under it).
- CONFIRMED: there is NO clean, documented, non-interactive Revit API
  call that returns the real AutoCAD block-definition name (e.g.
  "CHAIR-01") for a block reference inside an ImportInstance's
  geometry. GeometryInstance (what a block insertion becomes when
  walking an ImportInstance's GeometryElement) exposes a .Transform
  but no name and no stable ID - confirmed via multiple Autodesk Revit
  API forum threads and a pyRevit forum thread where practitioners
  ultimately gave up on API introspection and exported a coordinate
  table from AutoCAD itself instead. This is WHY this tool identifies
  a block by having the user click one visible occurrence in the view
  (Selection.PickObject) rather than offering a named list to browse.
- GeometryInstance.GetSymbolGeometry() returns the block's geometry in
  its own LOCAL (symbol-definition) space, unaffected by any one
  instance's position/rotation/scale (those live only in .Transform).
  Two occurrences of the true same AutoCAD block therefore share
  IDENTICAL local geometry by construction - this is what makes a
  rounded-equality signature over that local geometry a correct
  matching model, not an approximation of a fuzzier one.
- Nested blocks (a block containing another block) are a real
  possibility - each level's own Transform must be composed with its
  parent's (Transform.Multiply) while walking, and a GeometryInstance's
  own local signature already folds in any of ITS OWN nested
  GeometryInstances recursively, so an outer block containing an inner
  block still compares equal across occurrences of the outer block.
- Reference.GlobalPoint is a documented, populated property for a
  Reference resulting from an ObjectType.PointOnElement pick - used
  here to resolve a user's click back to a specific walked occurrence
  by bounding-box containment, rather than parsing
  Reference.ConvertToStableRepresentation()'s string encoding (research
  found this string-parsing approach is what practitioners fall back
  to, but it is fiddlier and more version-sensitive; GlobalPoint +
  proximity is simpler and was chosen as the primary mechanism here).
- Placement reuses DeeDistributor's already-proven pattern verbatim in
  spirit: doc.Create.NewFamilyInstance(XYZ, symbol, level,
  StructuralType.NonStructural) for the OneLevelBased case (confirmed
  at DeePack.tab/Rooms.panel/RoomStack.stack/DeeDistributor.pushbutton/
  script.py, _place_instance's OneLevelBased branch and _apply_rotation),
  v1 is scoped to ONLY this placement type - other FamilyPlacementType
  kinds (hosted, curve-based, work-plane-based...) are explicitly
  reported as unsupported rather than guessed at, matching that same
  file's own precedent for ViewBased families.
- Revit's public placement API has no supported way to apply a CAD
  block's own instance SCALE to a placed FamilyInstance the way
  position and rotation are supported - a non-1:1 scaled occurrence is
  placed at native family size and reported as a warning, not silently
  dropped or guessed at.

NEEDS LIVE-REVIT VERIFICATION (no Revit access available while writing
this - flagged per this codebase's own established convention):
  1. ImportInstance.get_Geometry(options)'s top-level coordinate space
     - treated as already world/document space here (by analogy with
       every other Element.get_Geometry() caller in this codebase and
       with DeeGetDWG's confirmed ImportInstance.GetTransform() fact),
       but this specific call has no direct precedent in this codebase.
  2. GeometryInstance.Transform composition semantics specifically for
     TRUE nested blocks-within-blocks (vs. the far more common flat
     case) - designed defensively (always Transform.Identity ->
     Multiply before recursing, the standard documented technique) but
     unverified against a real nested-block DWG.
  3. Reference.GlobalPoint reliability against ImportInstance
     sub-geometry specifically (vs. ordinary model elements, the more
     commonly documented case).
  4. Transform.Scale availability/behavior on a CAD-import-sourced
     transform specifically (vs. family-instance transforms).
  5. The window Hide()/PickObject()/Show() sequence in script.py,
     combined with dee_branding.DeeBrandedWindow's footer bar and DWM
     corner-rounding hook - see that file's own docstring for detail.
"""
import math

from Autodesk.Revit.DB import (
    FilteredElementCollector, ImportInstance, BuiltInParameter,
    CategoryType, ElementId, Transaction, Options, ViewDetailLevel,
    GeometryInstance, GeometryElement, Line, Arc, Curve, Solid, PolyLine, Mesh,
    XYZ, Transform, ElementTransformUtils, Level,
    FamilySymbol, StructuralType,
    UnitUtils, UnitTypeId, SpecTypeId,
)

from pyrevit import script

output = script.get_output()

_ROUND_NDP = 5  # decimal places, internal (feet) units - ~1e-5 ft ~ 0.003mm
_BBOX_PAD = 0.05  # feet (~15mm) - containment/proximity tolerance for click resolution


# ==========================================================================
# Units (local copy of the same small pattern already proven in
# dee_getdwg_service.py/DeeAligner/DeeReLevel - not a shared import,
# matching this codebase's established per-file-copy convention)
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
    (UnitTypeId.Millimeters, "mm"), (UnitTypeId.Centimeters, "cm"), (UnitTypeId.Meters, "m"),
    (UnitTypeId.Feet, "ft"), (UnitTypeId.FeetFractionalInches, "ft"),
    (UnitTypeId.FractionalInches, "in"), (UnitTypeId.Inches, "in"),
]


def unit_abbreviation(doc):
    uid = _length_unit_type_id(doc)
    for u, abbr in _UNIT_ABBR:
        if u == uid:
            return abbr
    return "ft"


# ==========================================================================
# Defensive name reads (local copy, same pattern used throughout this
# codebase - Element.Name can throw a bare exception on some types)
# ==========================================================================
def _read_name(element):
    if element is None:
        return None
    try:
        n = element.Name
        if n:
            return n
    except Exception:
        pass
    for bip in (BuiltInParameter.SYMBOL_NAME_PARAM, BuiltInParameter.ALL_MODEL_TYPE_NAME):
        try:
            p = element.get_Parameter(bip)
            if p is not None:
                val = p.AsString()
                if val:
                    return val
        except Exception:
            continue
    return None


# ==========================================================================
# CAD file discovery (local copy of dee_getdwg_service.py's scan/CadRow
# shape, trimmed to just what this tool needs)
# ==========================================================================
def _read_cad_file_name(doc, element):
    try:
        el_type = doc.GetElement(element.GetTypeId())
        if el_type is not None:
            n = _read_name(el_type)
            if n:
                return n
    except Exception:
        pass
    return _read_name(element) or "(unnamed CAD instance)"


class CadFileRow(object):
    def __init__(self, doc, element):
        self.element = element
        self.id = element.Id
        try:
            self.is_linked = bool(element.IsLinked)
        except Exception:
            self.is_linked = False
        self.kind_text = "Linked" if self.is_linked else "Imported"
        self.file_name = _read_cad_file_name(doc, element)

    @property
    def label(self):
        return "{0}  ({1})".format(self.file_name, self.kind_text)


def scan_cad_instances(doc):
    rows = []
    for el in FilteredElementCollector(doc).OfClass(ImportInstance):
        try:
            rows.append(CadFileRow(doc, el))
        except Exception:
            continue
    rows.sort(key=lambda r: r.file_name.lower())
    return rows


# ==========================================================================
# Geometry signature (the block "fingerprint") - see module docstring
# for why local-space rounded-equality is the correct model here
# ==========================================================================
def _round_pt(xyz):
    return (round(xyz.X, _ROUND_NDP), round(xyz.Y, _ROUND_NDP), round(xyz.Z, _ROUND_NDP))


def _bbox_union(a, b):
    if a is None:
        return b
    if b is None:
        return a
    amin, amax = a
    bmin, bmax = b
    return (
        (min(amin[0], bmin[0]), min(amin[1], bmin[1]), min(amin[2], bmin[2])),
        (max(amax[0], bmax[0]), max(amax[1], bmax[1]), max(amax[2], bmax[2])),
    )


def _point_bbox(points):
    if not points:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    zs = [p[2] for p in points]
    return ((min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs)))


def _curve_signature_and_bbox(curve):
    """Returns (signature_tuple, bbox) for one Curve. Line/Arc get a
    tight, exact signature from their defining points; anything else
    (Ellipse, NurbSpline, ...) falls back to a tessellated point list -
    coarser, but never crashes on a curve type not explicitly handled."""
    try:
        if isinstance(curve, Line):
            p0, p1 = _round_pt(curve.GetEndPoint(0)), _round_pt(curve.GetEndPoint(1))
            pts = [p0, p1]
            return ("LINE",) + tuple(sorted([p0, p1])), _point_bbox(pts)
        if isinstance(curve, Arc):
            center = _round_pt(curve.Center)
            p0, p1 = _round_pt(curve.GetEndPoint(0)), _round_pt(curve.GetEndPoint(1))
            radius = round(curve.Radius, _ROUND_NDP)
            pts = [curve.Center, curve.GetEndPoint(0), curve.GetEndPoint(1)]
            return ("ARC", center, radius) + tuple(sorted([p0, p1])), _point_bbox(
                [_round_pt(p) for p in pts])
    except Exception:
        pass
    try:
        tess = list(curve.Tessellate())
        pts = [_round_pt(p) for p in tess]
        return ("CURVE", tuple(pts)), _point_bbox(pts)
    except Exception:
        return ("CURVE", "?"), None


def _solid_signature_and_bbox(solid):
    try:
        bb = solid.GetBoundingBox()
        if bb is None:
            return ("SOLID", "?"), None
        # bb.Min/Max are already in the Solid's own local space here
        mn, mx = _round_pt(bb.Min), _round_pt(bb.Max)
        vol = round(solid.Volume, 3)
        return ("SOLID", mn, mx, vol), (mn, mx)
    except Exception:
        return ("SOLID", "?"), None


def _polyline_signature_and_bbox(polyline):
    try:
        pts = [_round_pt(p) for p in polyline.GetCoordinates()]
        return ("POLY", tuple(pts)), _point_bbox(pts)
    except Exception:
        return ("POLY", "?"), None


def _mesh_signature_and_bbox(mesh):
    try:
        pts = [_round_pt(p) for p in mesh.Vertices]
        sig = ("MESH", len(pts), mesh.NumTriangles) + tuple(sorted(pts))
        return sig, _point_bbox(pts)
    except Exception:
        return ("MESH", "?"), None


def _geometry_object_signature_and_bbox(obj):
    """One GeometryObject (never a bare GeometryInstance - those are
    handled by the caller so nested-block recursion can happen) ->
    (signature_tuple, local_bbox_or_None). Unrecognized types get a
    stable-but-uninformative signature rather than being skipped
    silently, so they still affect equality (two blocks that differ
    only by an unhandled geometry type won't be wrongly matched)."""
    if isinstance(obj, Curve):
        return _curve_signature_and_bbox(obj)
    if isinstance(obj, Solid):
        try:
            if obj.Volume <= 0 and obj.Faces.Size == 0:
                return None, None  # empty/void solid, contributes nothing
        except Exception:
            pass
        return _solid_signature_and_bbox(obj)
    if isinstance(obj, PolyLine):
        return _polyline_signature_and_bbox(obj)
    if isinstance(obj, Mesh):
        return _mesh_signature_and_bbox(obj)
    return ("OTHER", type(obj).__name__), None


def _signature_and_bbox_for_geometry_element(geom_element):
    """Walks one GeometryElement's DIRECT children (does NOT recurse
    into nested GeometryInstance objects - the caller does that
    separately, once, via _walk_geometry_element, so a block's own
    signature still folds in its nested blocks' geometry without this
    function doing its own recursion too). Returns (signature_tuple,
    local_bbox_or_None) - a coarse prefilter (counts + rounded bbox
    dims) is placed FIRST in the tuple so tuple `==`/`<` short-circuits
    on the cheap part before ever comparing the full point lists."""
    parts = []
    bbox = None
    counts = {"LINE": 0, "ARC": 0, "CURVE": 0, "SOLID": 0, "POLY": 0, "MESH": 0, "BLOCK": 0, "OTHER": 0}
    try:
        for obj in geom_element:
            if isinstance(obj, GeometryInstance):
                child_local_sig, child_local_bbox = _signature_and_bbox_for_geometry_element(
                    obj.GetSymbolGeometry())
                fp = _transform_fingerprint(obj.Transform)
                parts.append(("BLOCK", fp, child_local_sig))
                counts["BLOCK"] += 1
                if child_local_bbox is not None:
                    world_bbox = _apply_bbox_transform(child_local_bbox, obj.Transform)
                    bbox = _bbox_union(bbox, world_bbox)
                continue
            sig, obj_bbox = _geometry_object_signature_and_bbox(obj)
            if sig is None:
                continue
            parts.append(sig)
            kind = sig[0] if sig[0] in counts else "OTHER"
            counts[kind] += 1
            if obj_bbox is not None:
                bbox = _bbox_union(bbox, obj_bbox)
    except Exception:
        pass

    parts.sort(key=lambda t: repr(t))
    coarse = tuple(sorted(counts.items()))
    bbox_dims = None
    if bbox is not None:
        mn, mx = bbox
        bbox_dims = (round(mx[0] - mn[0], _ROUND_NDP), round(mx[1] - mn[1], _ROUND_NDP),
                     round(mx[2] - mn[2], _ROUND_NDP))
    signature = (coarse, bbox_dims) + tuple(parts)
    return signature, bbox


def _transform_fingerprint(t):
    """A rounded summary of a Transform, used only INSIDE a nested
    block's own signature (so a block containing two differently-
    positioned copies of the same inner block is distinguished from
    one containing only one) - not used for matching top-level
    occurrences against each other (their transforms are expected to
    differ; only their LOCAL signature should match)."""
    try:
        return (_round_pt(t.Origin), _round_pt(t.BasisX), _round_pt(t.BasisY), _round_pt(t.BasisZ))
    except Exception:
        return None


def _apply_bbox_transform(local_bbox, transform):
    """Transforms all 8 corners of a local-space bbox and returns the
    componentwise min/max - correct for a rotated/scaled transform,
    unlike naively transforming just Min and Max."""
    mn, mx = local_bbox
    corners = []
    for x in (mn[0], mx[0]):
        for y in (mn[1], mx[1]):
            for z in (mn[2], mx[2]):
                corners.append(_round_pt(transform.OfPoint(XYZ(x, y, z))))
    return _point_bbox(corners)


# ==========================================================================
# The one recursive walk - produces every block occurrence's local
# signature + world transform + world bbox in a single pass
# ==========================================================================
class BlockOccurrence(object):
    def __init__(self, geometry_instance, local_signature, world_transform, world_bbox):
        self.geometry_instance = geometry_instance
        self.local_signature = local_signature
        self.world_transform = world_transform
        self.world_bbox = world_bbox  # (min_tuple, max_tuple) or None


def _walk_geometry_element(geom_element, accumulated_transform, results, progress_cb):
    try:
        objs = list(geom_element)
    except Exception:
        return
    for obj in objs:
        if progress_cb is not None:
            if progress_cb(len(results)):
                return
        if isinstance(obj, GeometryInstance):
            # world_transform maps a point in obj's own LOCAL/symbol space
            # directly to true world space (that's what Transform.Multiply
            # composition means) - local_bbox below is computed by
            # _signature_and_bbox_for_geometry_element(obj.GetSymbolGeometry()),
            # which is exactly that same local/symbol space, so a single
            # _apply_bbox_transform(local_bbox, world_transform) is the
            # correct, direct way to get the final world-space bbox - no
            # separate obj.Transform-then-accumulated_transform double step
            # needed (that would require accumulated_transform to be
            # associative with bbox-corner transformation in exactly the
            # same way, which is true but needlessly roundabout to read).
            world_transform = accumulated_transform.Multiply(obj.Transform)
            try:
                symbol_geom = obj.GetSymbolGeometry()
            except Exception:
                symbol_geom = None
            if symbol_geom is None:
                continue
            local_sig, local_bbox = _signature_and_bbox_for_geometry_element(symbol_geom)
            world_bbox = _apply_bbox_transform(local_bbox, world_transform) if local_bbox else None
            results.append(BlockOccurrence(obj, local_sig, world_transform, world_bbox))
            _walk_geometry_element(symbol_geom, world_transform, results, progress_cb)
        elif isinstance(obj, GeometryElement):
            _walk_geometry_element(obj, accumulated_transform, results, progress_cb)
        # else: a loose Curve/Solid/PolyLine/Mesh not part of any block -
        # not relevant to block matching, skipped


def walk_import_instance(doc, import_instance, progress_cb=None):
    """progress_cb(occurrences_found_so_far) -> True to cancel, False/None
    to continue - there is no reliable total count to report progress
    against up front (the tree depth/breadth isn't known until walked),
    so this reports a running count rather than a percentage, matching
    forms.ProgressBar's indeterminate mode."""
    options = Options()
    try:
        options.ComputeReferences = False
        options.DetailLevel = ViewDetailLevel.Fine
    except Exception:
        pass
    try:
        geom = import_instance.get_Geometry(options)
    except Exception:
        geom = None
    if geom is None:
        return []
    results = []
    _walk_geometry_element(geom, Transform.Identity, results, progress_cb)
    return results


# ==========================================================================
# Click resolution + match finding - both pure filters over one
# already-walked occurrence list, no further Revit API calls
# ==========================================================================
def _bbox_contains(bbox, point, pad=_BBOX_PAD):
    if bbox is None:
        return False
    mn, mx = bbox
    return (mn[0] - pad <= point.X <= mx[0] + pad and
            mn[1] - pad <= point.Y <= mx[1] + pad and
            mn[2] - pad <= point.Z <= mx[2] + pad)


def _bbox_volume(bbox):
    """Used only to rank "most specific" among several bboxes that all
    contain a click - a pure 3D volume degenerates to exactly 0 for any
    flat/planar bbox (Z-extent 0), which is the COMMON case for 2D CAD
    block geometry, making every flat block tie at 0 and silently fall
    back to list order instead of real size. Flooring each dimension at
    a small epsilon keeps flat blocks ranked by their real XY (or XZ/YZ)
    footprint instead of collapsing to a false tie."""
    if bbox is None:
        return float("inf")
    mn, mx = bbox
    eps = 1e-4  # feet, ~0.03mm - well below any real block dimension
    dx = max(eps, mx[0] - mn[0])
    dy = max(eps, mx[1] - mn[1])
    dz = max(eps, mx[2] - mn[2])
    return dx * dy * dz


def _bbox_center_distance(bbox, point):
    if bbox is None:
        return float("inf")
    mn, mx = bbox
    cx, cy, cz = (mn[0] + mx[0]) / 2.0, (mn[1] + mx[1]) / 2.0, (mn[2] + mx[2]) / 2.0
    return ((cx - point.X) ** 2 + (cy - point.Y) ** 2 + (cz - point.Z) ** 2) ** 0.5


def resolve_clicked_occurrence(occurrences, world_point):
    """world_point: an XYZ (e.g. Reference.GlobalPoint). Returns the
    best-matching BlockOccurrence or None. Prefers the smallest-bbox
    containing candidate (most specific, so a small block nested inside
    a larger one's bbox is still picked correctly); falls back to
    nearest-bbox-center if nothing actually contains the point (e.g.
    the click landed exactly on a curve with zero-volume bbox)."""
    containing = [occ for occ in occurrences if _bbox_contains(occ.world_bbox, world_point)]
    if containing:
        containing.sort(key=lambda occ: _bbox_volume(occ.world_bbox))
        return containing[0]
    if not occurrences:
        return None
    return min(occurrences, key=lambda occ: _bbox_center_distance(occ.world_bbox, world_point))


def find_matching_occurrences(occurrences, clicked):
    if clicked is None:
        return []
    return [occ for occ in occurrences if occ.local_signature == clicked.local_signature]


# ==========================================================================
# Family index (category -> family -> type), same shape as
# DeeDistributor's _rebuild_family_index, ported here since DeeDistributor
# lives in its own pushbutton folder and this codebase's convention is
# per-file copies, not shared imports, for exactly this kind of helper
# ==========================================================================
def collect_all_family_symbols(doc):
    try:
        return list(FilteredElementCollector(doc).OfClass(FamilySymbol))
    except Exception:
        return []


def build_family_index(doc, progress_cb=None):
    """Returns (type_index, family_index):
    type_index[category_name][family_name][type_name] = FamilySymbol
    family_index[category_name][family_name] = Family
    Model categories only (matches DeeDistributor's own filter - a CAD
    block gets replaced by real 3D content, so annotation/2D families
    don't belong in this list)."""
    symbols = collect_all_family_symbols(doc)
    total = len(symbols)
    type_index = {}
    family_index = {}
    for i, fs in enumerate(symbols):
        if progress_cb is not None:
            if progress_cb(i, total):
                break
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
    return type_index, family_index


def list_levels(doc):
    try:
        return sorted(FilteredElementCollector(doc).OfClass(Level), key=lambda l: l.Elevation)
    except Exception:
        return []


def nearest_level(levels, z_internal):
    if not levels:
        return None
    return min(levels, key=lambda l: abs(l.Elevation - z_internal))


# ==========================================================================
# Placement - v1 scoped to OneLevelBased only (point-placed generic
# model/furniture/planting-style families), matching DeeDistributor's
# own _place_instance(OneLevelBased) branch and _apply_rotation
# ==========================================================================
_SUPPORTED_PLACEMENT = "OneLevelBased"


def placement_kind_text(symbol):
    if symbol is None:
        return ""
    try:
        return str(symbol.Family.FamilyPlacementType)
    except Exception:
        return "(unknown)"


def is_placement_supported(symbol):
    return placement_kind_text(symbol) == _SUPPORTED_PLACEMENT


def ensure_symbol_active(doc, symbol):
    try:
        if not symbol.IsActive:
            symbol.Activate()
            doc.Regenerate()
    except Exception:
        pass


def extract_position_and_rotation(world_transform):
    origin = world_transform.Origin
    try:
        angle = math.atan2(world_transform.BasisX.Y, world_transform.BasisX.X)
    except Exception:
        angle = 0.0
    scale = 1.0
    try:
        scale = world_transform.Scale
    except Exception:
        scale = 1.0
    return origin, angle, scale


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


class PlacementResult(object):
    def __init__(self):
        self.placed_count = 0
        self.skipped = []  # list of (label, reason)
        self.scale_warnings = 0


def place_matches(doc, symbol, occurrences, level):
    """One Transaction; each occurrence wrapped in its own try/except so
    one failure never aborts the rest. Never touches the CAD/DWG
    geometry - only ever creates new FamilyInstance elements."""
    result = PlacementResult()
    t = Transaction(doc, "DeeBlocktoFamily - Place Family at Matched Blocks")
    t.Start()
    try:
        ensure_symbol_active(doc, symbol)
        for i, occ in enumerate(occurrences):
            label = "Occurrence {0}".format(i + 1)
            try:
                origin, angle, scale = extract_position_and_rotation(occ.world_transform)
                if abs(scale - 1.0) > 1e-4:
                    result.scale_warnings += 1
                inst = doc.Create.NewFamilyInstance(origin, symbol, level, StructuralType.NonStructural)
                _apply_rotation(doc, inst, angle, origin)
                result.placed_count += 1
            except Exception as e:
                result.skipped.append((label, "FAILED: {0}".format(e)))
        t.Commit()
    except Exception:
        t.RollBack()
        raise
    return result


def print_report(result, block_count, family_label):
    html = [
        '<h2 style="font-family:sans-serif;color:#ddd;">DeeBlocktoFamily - Placement Results</h2>',
        '<p style="color:#ddd;">Matched {0} block occurrence(s). Placed {1} instance(s) of "{2}". '
        '{3} skipped.</p>'.format(block_count, result.placed_count, family_label, len(result.skipped)),
    ]
    if result.scale_warnings:
        html.append(
            '<div style="padding:6px 12px;margin:4px 0;background:#8d6e00;color:#fff;'
            'border-radius:4px;font-family:monospace;font-size:12px;">'
            '&#9888;&nbsp; {0} occurrence(s) had a non-1:1 CAD scale - Revit\'s placement API has no '
            'supported way to apply instance scale to a Family, so these were placed at native family '
            'size (position/rotation still matched).</div>'.format(result.scale_warnings))
    for label, reason in result.skipped:
        html.append(
            '<div style="padding:4px 10px;margin:2px 0;background:#c62828;color:#fff;'
            'border-radius:4px;font-family:monospace;font-size:12px;">'
            '&#10007;&nbsp; <b>{0}</b> &mdash; {1}</div>'.format(label, reason))
    output.print_html("".join(html))
