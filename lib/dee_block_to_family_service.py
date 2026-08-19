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
    FilteredElementCollector, ImportInstance, BuiltInParameter, BuiltInCategory,
    CategoryType, ElementId, Transaction, Options, ViewDetailLevel,
    GeometryInstance, GeometryElement, Line, Arc, Curve, Solid, PolyLine, Mesh,
    XYZ, Transform, ElementTransformUtils, Level, HostObjectUtils, ReferencePlane,
    FamilySymbol,
    UnitUtils, UnitTypeId, SpecTypeId,
    IFailuresPreprocessor, FailureProcessingResult, FailureSeverity,
)
from Autodesk.Revit.DB.Structure import StructuralType

from pyrevit import script

output = script.get_output()

_ROUND_NDP = 5  # decimal places, internal (feet) units - ~1e-5 ft ~ 0.003mm
_BBOX_PAD = 0.05  # feet (~15mm) - containment/proximity tolerance for click resolution

# Supported FamilyPlacementType kinds, each with its own placement
# branch in place_matches:
#   OneLevelBased   - plain point + Level (furniture, generic models)
#   TwoLevelsBased  - column-like; same creation call, Revit resolves
#                     the top constraint from the type's own defaults
#   WorkPlaneBased  - face/ceiling-hosted (ceiling spotlights,
#                     sprinklers, diffusers, smoke detectors). CANNOT be
#                     placed with the plain XYZ+Level overload at all -
#                     it needs a host Reference, so this branch finds
#                     the ceiling above each block point and hosts to
#                     its bottom face, falling back to a shared
#                     horizontal Reference Plane when there is no
#                     ceiling there.
_PLACEMENT_ONE_LEVEL = "OneLevelBased"
_PLACEMENT_TWO_LEVELS = "TwoLevelsBased"
_PLACEMENT_WORK_PLANE = "WorkPlaneBased"
_SUPPORTED_PLACEMENTS = (_PLACEMENT_ONE_LEVEL, _PLACEMENT_TWO_LEVELS, _PLACEMENT_WORK_PLANE)

# Human-readable explanation per unsupported kind, so the UI can say
# WHY rather than just "not supported"
_UNSUPPORTED_REASONS = {
    "OneLevelBasedHosted": (
        "wall-hosted - it must be cut into a specific wall, and a CAD block point alone "
        "doesn't identify which wall to host it to"),
    "CurveBased": "line-based - it needs a line to sit on, not a single point",
    "CurveBasedDetail": "line-based detail - it needs a line to sit on, not a single point",
    "ViewBased": "a 2D detail/annotation family - it lives in one view, not in 3D model space",
    "Invalid": "not placeable as an instance by Revit itself",
}


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


def display_to_internal(doc, value_display):
    uid = _length_unit_type_id(doc)
    try:
        return UnitUtils.ConvertToInternalUnits(value_display, uid)
    except Exception:
        return value_display


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
    """Deliberately holds NO live Revit objects - only plain Python
    numbers/tuples. The placement position/rotation/scale are extracted
    from the world Transform immediately during the walk and stored as
    floats, and the GeometryInstance itself is never retained.

    This matters: a BlockOccurrence list survives across a
    Selection.PickObject call, a full CAD geometry walk, and an
    open-ended amount of user UI interaction on the wizard's later
    tabs. Holding Revit API objects (GeometryInstance/Transform, which
    are managed wrappers over native geometry owned by a
    GeometryElement that is itself long out of scope) across that span
    risks a native access violation - a hard "unrecoverable error"
    Revit crash that no try/except can catch - rather than a clean
    Python exception. Storing plain floats removes that entire class
    of failure, and costs nothing since placement only ever needed the
    numbers anyway."""

    def __init__(self, local_signature, origin, rotation_radians, scale, world_bbox):
        self.local_signature = local_signature
        self.origin = origin              # plain (x, y, z) float tuple, world space
        self.rotation_radians = rotation_radians
        self.scale = scale
        self.world_bbox = world_bbox      # (min_tuple, max_tuple) of plain floats, or None


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
            # Extract placement numbers NOW, while world_transform is
            # known-live, so nothing Revit-owned outlives this call -
            # see BlockOccurrence's docstring.
            origin, angle, scale = extract_position_and_rotation(world_transform)
            results.append(BlockOccurrence(
                local_sig, (origin.X, origin.Y, origin.Z), angle, scale, world_bbox))
            _walk_geometry_element(symbol_geom, world_transform, results, progress_cb)
        elif isinstance(obj, GeometryElement):
            _walk_geometry_element(obj, accumulated_transform, results, progress_cb)
        # else: a loose Curve/Solid/PolyLine/Mesh not part of any block -
        # not relevant to block matching, skipped


def diagnose_occurrences(doc, import_instance, occurrences):
    """Cross-checks the walked block positions against the
    ImportInstance's OWN bounding box, which Revit reports in internal
    units (feet) and is therefore authoritative.

    Every block in a CAD file must, by definition, sit inside that
    file's extents. So if the computed origins fall outside it - or
    span a wildly different size - the transform composition is wrong,
    and the ratio between the two spans is the scale factor being
    missed (e.g. ~304.8 for a millimetre DWG whose unit conversion
    wasn't applied). This turns "the positions look wrong" into a
    measured number instead of a guess.

    Returns (ok, message). Never raises."""
    try:
        if not occurrences:
            return True, "no occurrences to check"
        bb = import_instance.get_BoundingBox(None)
        if bb is None:
            return True, "import has no bounding box - cannot cross-check"

        ix0, iy0, iz0 = bb.Min.X, bb.Min.Y, bb.Min.Z
        ix1, iy1, iz1 = bb.Max.X, bb.Max.Y, bb.Max.Z
        xs = [o.origin[0] for o in occurrences]
        ys = [o.origin[1] for o in occurrences]
        ox0, ox1 = min(xs), max(xs)
        oy0, oy1 = min(ys), max(ys)

        import_span = max(ix1 - ix0, iy1 - iy0)
        origin_span = max(ox1 - ox0, oy1 - oy0)

        pad = max(1.0, import_span * 0.05)
        inside = (ox0 >= ix0 - pad and ox1 <= ix1 + pad and
                  oy0 >= iy0 - pad and oy1 <= iy1 + pad)

        detail = ("import bbox X[{0:.3f}..{1:.3f}] Y[{2:.3f}..{3:.3f}] Z[{4:.3f}..{5:.3f}] ft"
                  " | block origins X[{6:.3f}..{7:.3f}] Y[{8:.3f}..{9:.3f}] ft"
                  " | spans import={10:.3f} blocks={11:.3f}").format(
                      ix0, ix1, iy0, iy1, iz0, iz1, ox0, ox1, oy0, oy1,
                      import_span, origin_span)

        if inside:
            return True, "positions inside the import extents - OK. " + detail

        ratio = (origin_span / import_span) if import_span > 1e-9 else float("inf")
        # Deliberately NOT presented as an exact unit factor: the blocks
        # only cover part of the drawing, so this ratio is diluted by
        # however much of the sheet they actually occupy. It is an
        # order-of-magnitude signal, and saying more than that would be
        # inventing precision the number does not carry.
        hint = ""
        for factor, name in ((304.8, "millimetres"), (30.48, "centimetres"), (3.2808, "metres")):
            if 0.5 * factor <= ratio <= 2.0 * factor:
                hint = (" The magnitude is consistent with DWG {0} not being converted to "
                        "Revit's internal feet.".format(name))
                break
        return False, (
            "BLOCK POSITIONS FALL OUTSIDE THE CAD IMPORT'S OWN EXTENTS - a scale factor is being "
            "lost somewhere in the transform composition. Blocks span roughly {0:.1f}x the "
            "import's own span (approximate - the blocks cover only part of the drawing).{1} "
            "{2}".format(ratio, hint, detail))
    except Exception as e:
        return True, "cross-check failed: {0}".format(e)


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


class FamilyTypeEntry(object):
    """Everything the UI needs about one FamilySymbol, captured as plain
    Python values at index-build time - deliberately does NOT hold the
    live FamilySymbol object.

    Holding Revit Element objects (rather than ElementIds) across other
    API operations is a well-known crash risk: the managed wrapper can
    outlive or desync from the underlying native object, and in this
    tool the gap is unusually wide and unusually eventful - the symbols
    are collected once at window-open, then the user runs
    Selection.PickObject and a full CAD geometry walk over potentially
    thousands of geometry objects, and only THEN picks a Category/
    Family/Type. Reading symbol.Family.FamilyPlacementType on a
    long-held symbol object at that point crashed Revit outright
    ("unrecoverable error") in live testing - three times, with the
    crash point moving further down the Category -> Family -> Type
    chain as intermediate mitigations were applied, which is exactly
    the signature of stale/unstable element references rather than a
    logic bug.

    So: the placement kind is read ONCE here, while the symbol is
    freshly collected, and stored as a plain string; only the
    ElementId is kept, and the real symbol is re-resolved via
    doc.GetElement(id) at the single moment it's actually needed
    (placement). The entire interactive Category/Family/Type selection
    path then makes ZERO Revit API calls."""
    def __init__(self, element_id, type_name, placement_kind):
        self.element_id = element_id
        self.type_name = type_name
        self.placement_kind = placement_kind  # plain string, e.g. "OneLevelBased"

    @property
    def is_supported(self):
        return self.placement_kind in _SUPPORTED_PLACEMENTS

    @property
    def needs_host_face(self):
        """WorkPlaneBased families can't be placed at a bare point - the
        UI uses this to show/enable the ceiling-fallback height field."""
        return self.placement_kind == _PLACEMENT_WORK_PLANE

    @property
    def unsupported_reason(self):
        return _UNSUPPORTED_REASONS.get(
            self.placement_kind, "an unsupported placement type ({0})".format(self.placement_kind))


def build_family_index(doc, progress_cb=None):
    """Returns (type_index, family_names_index):
    type_index[category_name][family_name][type_name] = FamilyTypeEntry
    family_names_index[category_name] = set/dict of family names
    Model categories only (matches DeeDistributor's own filter - a CAD
    block gets replaced by real 3D content, so annotation/2D families
    don't belong in this list).

    Note this stores FamilyTypeEntry (ElementId + pre-read placement
    kind), never the live FamilySymbol/Family objects - see
    FamilyTypeEntry's docstring for why that distinction is
    load-bearing here, not incidental."""
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
            # Read the placement kind NOW, while this symbol is freshly
            # collected and its Family reference is known-good.
            try:
                placement_kind = str(fam.FamilyPlacementType)
            except Exception:
                placement_kind = "(unknown)"
            entry = FamilyTypeEntry(fs.Id, type_name, placement_kind)
            type_index.setdefault(cat_name, {}).setdefault(fam_name, {})[type_name] = entry
            family_index.setdefault(cat_name, {})[fam_name] = True
        except Exception:
            continue
    return type_index, family_index


def resolve_symbol(doc, entry):
    """Re-resolves a FamilyTypeEntry's ElementId to a live FamilySymbol
    at the moment of use - never held across other API operations."""
    if entry is None:
        return None
    try:
        return doc.GetElement(entry.element_id)
    except Exception:
        return None


class LevelEntry(object):
    """Same ElementId-not-Element discipline as FamilyTypeEntry - the
    Level dropdown is populated once at window-open but only used much
    later, after PickObject and the geometry walk."""
    def __init__(self, element_id, name, elevation):
        self.element_id = element_id
        self.name = name
        self.elevation = elevation


def list_level_entries(doc):
    entries = []
    try:
        levels = sorted(FilteredElementCollector(doc).OfClass(Level), key=lambda l: l.Elevation)
    except Exception:
        return entries
    for lvl in levels:
        try:
            name = _read_name(lvl)
            if name:
                entries.append(LevelEntry(lvl.Id, name, lvl.Elevation))
        except Exception:
            continue
    return entries


def nearest_level_entry(level_entries, z_internal):
    if not level_entries:
        return None
    return min(level_entries, key=lambda e: abs(e.elevation - z_internal))


def resolve_level(doc, entry):
    if entry is None:
        return None
    try:
        return doc.GetElement(entry.element_id)
    except Exception:
        return None


# ==========================================================================
# Placement - v1 scoped to OneLevelBased only (point-placed generic
# model/furniture/planting-style families), matching DeeDistributor's
# own _place_instance(OneLevelBased) branch and _apply_rotation
# ==========================================================================
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


# --------------------------------------------------------------------------
# Ceiling hosting for WorkPlaneBased families (ceiling spotlights,
# sprinklers, diffusers, smoke detectors - the common case for a CAD
# reflected-ceiling-plan block).
#
# Note on the element cache below vs. the "never cache Revit Elements"
# rule this tool was just fixed for: that rule is about holding elements
# across UNRELATED API operations and open-ended UI waits. This cache is
# built and fully consumed inside a single place_matches call, within
# one Transaction, with no user interaction in between - the normal,
# correct way to avoid re-querying ceilings 300+ times. It is never
# stored on the window.
# --------------------------------------------------------------------------
def _build_ceiling_cache(doc):
    """[(minx, miny, minz, maxx, maxy, maxz, ceiling_element), ...]"""
    cache = []
    try:
        collector = (FilteredElementCollector(doc)
                     .OfCategory(BuiltInCategory.OST_Ceilings)
                     .WhereElementIsNotElementType())
    except Exception:
        return cache
    for c in collector:
        try:
            bb = c.get_BoundingBox(None)
            if bb is None:
                continue
            cache.append((bb.Min.X, bb.Min.Y, bb.Min.Z, bb.Max.X, bb.Max.Y, bb.Max.Z, c))
        except Exception:
            continue
    return cache


def _ceiling_face_at_point(doc, ceiling_cache, x, y, ref_z):
    """Returns (face_reference, face_z) for the best ceiling above the
    point (x, y). (None, None) when no ceiling covers that point at all.

    ref_z is the BLOCK's own Z, not the chosen Level's elevation. That
    distinction is load-bearing: the first version anchored the search
    to the Level, and against a real project where the picked Level was
    "TOS" (top of slab, above the ceilings) every single ceiling was
    filtered out as "below the level" - 317 of 317 fixtures fell through
    to the fallback Reference Plane even though the model had ceilings.
    The Level a user picks is the family's level association, which says
    nothing about where the ceilings sit relative to it.

    Preference order:
      1. the LOWEST ceiling whose underside is at/above the block (the
         ceiling the fixture belongs in, not one a storey up), else
      2. the ceiling nearest in Z, so a block sitting slightly above its
         own ceiling plane (very common - CAD blocks carry whatever
         elevation the drafter left them at) still hosts correctly
         instead of silently falling back.

    Point-in-bbox is a deliberate simplification over exact face
    containment: a ceiling's bbox is its real plan extent for the
    rectangular/simple ceilings this targets, and an over-match just
    means the fixture hosts to a ceiling whose edge is nearby rather
    than failing outright."""
    above = None       # (bottom_z, ceiling) - lowest underside at/above ref_z
    nearest = None     # (abs_dz, bottom_z, ceiling)
    for (x0, y0, z0, x1, y1, _z1, ceiling) in ceiling_cache:
        if not (x0 <= x <= x1 and y0 <= y <= y1):
            continue
        # z0 is the ceiling's UNDERSIDE - that's the face a fixture
        # hosts to, and what should be compared against the block.
        if z0 >= ref_z - 1e-6:
            if above is None or z0 < above[0]:
                above = (z0, ceiling)
        dz = abs(z0 - ref_z)
        if nearest is None or dz < nearest[0]:
            nearest = (dz, z0, ceiling)

    if above is not None:
        ceiling = above[1]
    elif nearest is not None:
        ceiling = nearest[2]
    else:
        return None, None
    try:
        refs = HostObjectUtils.GetBottomFaces(ceiling)
    except Exception:
        refs = None
    if not refs:
        return None, None
    face_z = None
    try:
        bb = ceiling.get_BoundingBox(None)
        if bb is not None:
            face_z = bb.Min.Z
    except Exception:
        face_z = None
    for r in refs:
        return r, face_z
    return None, None


def _find_existing_horizontal_plane(doc, z, tol=1e-4):
    """Reuse a horizontal Reference Plane already sitting at this height
    rather than adding another one on every run."""
    try:
        planes = FilteredElementCollector(doc).OfClass(ReferencePlane)
    except Exception:
        return None
    for rp in planes:
        try:
            normal = rp.Normal
            if abs(abs(normal.Z) - 1.0) > 1e-6:
                continue  # not horizontal
            if abs(rp.BubbleEnd.Z - z) <= tol:
                return rp
        except Exception:
            continue
    return None


def _get_or_create_reference_plane(doc, z, cache):
    """One shared horizontal Reference Plane per height - the fallback
    host for WorkPlaneBased families where no ceiling was found.
    Ported from DeeDistributor's
    _get_or_create_fallback_reference_plane (whose own docstring flags
    NewReferencePlane's argument geometry and ReferencePlane.
    GetReference()'s suitability as a host as NOT live-verified - the
    same caveat applies here).

    Deliberately does NOT set a Name. An earlier version named each
    plane after its height, which blew up on the SECOND run against the
    same model: the plane from the first run already owned that name,
    and Revit reports the duplicate as a transaction-level ERROR ("The
    name entered is already in use") that blocks the whole commit -
    taking every family placement down with it. Wrapping the assignment
    in try/except was not enough, because the failure is raised by
    Revit's failure-handling machinery at commit time, not by the
    property setter. An unnamed plane hosts families exactly as well,
    so the name bought nothing and cost a hard blocker. Existing
    planes at the same height are reused instead of piling up."""
    key = round(z, 4)
    cached = cache.get(key)
    if cached is not None:
        try:
            return cached.GetReference()
        except Exception:
            del cache[key]

    existing = _find_existing_horizontal_plane(doc, z)
    if existing is not None:
        try:
            cache[key] = existing
            return existing.GetReference()
        except Exception:
            pass

    try:
        rp = doc.Create.NewReferencePlane(
            XYZ(-100.0, 0.0, z), XYZ(100.0, 0.0, z), XYZ(0.0, 100.0, z), doc.ActiveView)
        cache[key] = rp
        return rp.GetReference()
    except Exception:
        return None


class _WarningSwallower(IFailuresPreprocessor):
    """Placing hundreds of fixtures legitimately raises hundreds of
    Revit WARNINGS - most commonly "There are identical instances in
    the same place", which fires whenever a new instance lands on top
    of one that already exists (very likely here: the CAD block layout
    is often already modelled). Left unhandled, Revit stacks them into
    a modal dialog listing 300+ entries that the user has to dismiss,
    and which can itself block an otherwise-fine commit.

    This swallows WARNINGS only. Errors are deliberately left alone -
    they still surface, still block, and still get reported, because an
    error means Revit could not do what was asked and hiding that would
    be lying about the result. The count is kept so the report can say
    honestly how many were suppressed."""

    def __init__(self):
        self.swallowed = 0

    def PreprocessFailures(self, failures_accessor):
        try:
            for fm in failures_accessor.GetFailureMessages():
                try:
                    if fm.GetSeverity() == FailureSeverity.Warning:
                        failures_accessor.DeleteWarning(fm)
                        self.swallowed += 1
                except Exception:
                    continue
        except Exception:
            pass
        return FailureProcessingResult.Continue


class PlacementResult(object):
    def __init__(self):
        self.placed_count = 0
        self.skipped = []  # list of (label, reason)
        self.scale_warnings = 0
        self.ceiling_hosted_count = 0
        self.fallback_plane_count = 0
        self.rotation_applied = True
        self.revit_warnings_suppressed = 0
        self.ceilings_in_model = 0


def place_matches(doc, entry, occurrences, level_entry, fallback_height_internal=0.0,
                   apply_rotation=True):
    """entry/level_entry: FamilyTypeEntry and LevelEntry (ElementId +
    pre-read plain values) - the live FamilySymbol and Level are
    re-resolved from their ids HERE, at the single moment they're
    actually needed, rather than being held by the UI across the
    PickObject/geometry-walk steps (see FamilyTypeEntry's docstring).

    fallback_height_internal: for WorkPlaneBased families only - height
    ABOVE the chosen Level for the fallback Reference Plane used where
    no ceiling is found over a block point.

    apply_rotation: True to rotate each placed family to match its CAD
    block's own rotation; False to leave every instance at the family's
    default orientation. Worth turning off when the source blocks were
    inserted at arbitrary angles that shouldn't carry into the model -
    common for symmetrical fixtures like ceiling spots, where the CAD
    rotation is meaningless noise.

    One Transaction; each occurrence wrapped in its own try/except so
    one failure never aborts the rest. Never touches the CAD/DWG
    geometry - only ever creates new FamilyInstance elements."""
    result = PlacementResult()
    result.rotation_applied = bool(apply_rotation)
    symbol = resolve_symbol(doc, entry)
    if symbol is None:
        result.skipped.append(("Family type", "Could not resolve the selected Family Type - re-pick it."))
        return result
    level = resolve_level(doc, level_entry)
    if level is None:
        result.skipped.append(("Level", "Could not resolve the selected Level - re-pick it."))
        return result

    kind = entry.placement_kind
    needs_face = (kind == _PLACEMENT_WORK_PLANE)
    ceiling_cache = _build_ceiling_cache(doc) if needs_face else []
    result.ceilings_in_model = len(ceiling_cache)

    # A CAD import's GeometryInstance transforms carry the DWG's own
    # unit-conversion factor, so their raw Scale is almost never 1.0 -
    # comparing against 1.0 flagged all 317 occurrences as "non-1:1",
    # which is noise, not information. What actually matters is whether
    # a block is scaled differently from the OTHERS in the same file, so
    # the baseline is the most common scale among the matches and only
    # deviations from it are reported.
    baseline_scale = 1.0
    try:
        counts = {}
        for o in occurrences:
            k = round(o.scale, 4)
            counts[k] = counts.get(k, 0) + 1
        if counts:
            baseline_scale = max(counts.items(), key=lambda kv: kv[1])[0]
    except Exception:
        baseline_scale = 1.0
    if not baseline_scale:
        baseline_scale = 1.0
    ref_plane_cache = {}
    try:
        level_elevation = level.Elevation
    except Exception:
        level_elevation = 0.0
    fallback_z = level_elevation + (fallback_height_internal or 0.0)

    t = Transaction(doc, "DeeBlocktoFamily - Place Family at Matched Blocks")
    t.Start()
    swallower = _WarningSwallower()
    try:
        opts = t.GetFailureHandlingOptions()
        opts.SetFailuresPreprocessor(swallower)
        opts.SetClearAfterRollback(True)
        t.SetFailureHandlingOptions(opts)
    except Exception:
        pass  # worst case the user sees Revit's own warning dialog
    try:
        ensure_symbol_active(doc, symbol)
        for i, occ in enumerate(occurrences):
            label = "Occurrence {0}".format(i + 1)
            try:
                # occ carries plain floats, not a live Transform - a
                # fresh XYZ is built here at placement time.
                x, y, z = occ.origin
                angle = occ.rotation_radians if apply_rotation else 0.0
                if abs(occ.scale - baseline_scale) > 1e-4 * max(1.0, abs(baseline_scale)):
                    result.scale_warnings += 1

                if needs_face:
                    # A WorkPlaneBased symbol CANNOT use the plain
                    # XYZ+Level overload at all - it needs a host
                    # Reference. Prefer the real ceiling above this
                    # block point; fall back to a shared Reference
                    # Plane at the requested height.
                    face_ref, face_z = _ceiling_face_at_point(
                        doc, ceiling_cache, x, y, z)
                    if face_ref is not None:
                        place_z = face_z if face_z is not None else fallback_z
                        origin = XYZ(x, y, place_z)
                        inst = doc.Create.NewFamilyInstance(face_ref, origin, XYZ.BasisX, symbol)
                        result.ceiling_hosted_count += 1
                    else:
                        plane_ref = _get_or_create_reference_plane(doc, fallback_z, ref_plane_cache)
                        if plane_ref is None:
                            result.skipped.append(
                                (label, "No ceiling above this point and the fallback Reference "
                                        "Plane could not be created"))
                            continue
                        origin = XYZ(x, y, fallback_z)
                        inst = doc.Create.NewFamilyInstance(plane_ref, origin, XYZ.BasisX, symbol)
                        result.fallback_plane_count += 1
                else:
                    origin = XYZ(x, y, z)
                    inst = doc.Create.NewFamilyInstance(
                        origin, symbol, level, StructuralType.NonStructural)

                _apply_rotation(doc, inst, angle, origin)
                result.placed_count += 1
            except Exception as e:
                result.skipped.append((label, "FAILED: {0}".format(e)))
        t.Commit()
    except Exception:
        t.RollBack()
        raise
    result.revit_warnings_suppressed = swallower.swallowed
    return result


def print_report(result, block_count, family_label):
    html = [
        '<h2 style="font-family:sans-serif;color:#ddd;">DeeBlocktoFamily - Placement Results</h2>',
        '<p style="color:#ddd;">Matched {0} block occurrence(s). Placed {1} instance(s) of "{2}". '
        '{3} skipped.</p>'.format(block_count, result.placed_count, family_label, len(result.skipped)),
    ]
    html.append(
        '<div style="padding:4px 10px;margin:2px 0;color:#bbb;font-family:monospace;font-size:12px;">'
        'Rotation: {0}</div>'.format(
            "matched to each CAD block" if result.rotation_applied
            else "IGNORED - all placed at the family's default orientation"))
    if result.revit_warnings_suppressed:
        html.append(
            '<div style="padding:6px 12px;margin:4px 0;background:#8d6e00;color:#fff;'
            'border-radius:4px;font-family:monospace;font-size:12px;">'
            '&#9888;&nbsp; {0} Revit warning(s) were auto-dismissed so the placement could finish '
            'without a 300-entry dialog. These are usually "There are identical instances in the '
            'same place", which means a fixture was placed on top of one that was ALREADY in the '
            'model - if these blocks were already modelled, you now have duplicates. Undo (Ctrl+Z) '
            'reverses the whole placement in one step if that is the case.</div>'.format(
                result.revit_warnings_suppressed))
    if result.ceiling_hosted_count or result.fallback_plane_count:
        colour = "#2e7d32" if result.fallback_plane_count == 0 else "#8d6e00"
        detail = ""
        if result.fallback_plane_count:
            if result.ceilings_in_model == 0:
                detail = (" There are NO ceilings in this model at all, so a reference plane was the "
                          "only option - model the ceilings first if you want the fixtures hosted "
                          "to them.")
            else:
                detail = (" The model has {0} ceiling(s), but none cover those block points in plan. "
                          "Check that the ceilings actually sit above the CAD blocks.".format(
                              result.ceilings_in_model))
        html.append(
            '<div style="padding:6px 12px;margin:4px 0;background:{0};color:#fff;'
            'border-radius:4px;font-family:monospace;font-size:12px;">'
            'Face-hosted placement: {1} hosted to a real ceiling, {2} hosted to a fallback '
            'Reference Plane.{3}</div>'.format(
                colour, result.ceiling_hosted_count, result.fallback_plane_count, detail))
    if result.scale_warnings:
        html.append(
            '<div style="padding:6px 12px;margin:4px 0;background:#8d6e00;color:#fff;'
            'border-radius:4px;font-family:monospace;font-size:12px;">'
            '&#9888;&nbsp; {0} occurrence(s) are scaled differently from the other blocks in this '
            'file. Revit\'s placement API has no supported way to apply instance scale to a Family, '
            'so these were placed at native family size (position/rotation still matched).</div>'.format(
                result.scale_warnings))
    for label, reason in result.skipped:
        html.append(
            '<div style="padding:4px 10px;margin:2px 0;background:#c62828;color:#fff;'
            'border-radius:4px;font-family:monospace;font-size:12px;">'
            '&#10007;&nbsp; <b>{0}</b> &mdash; {1}</div>'.format(label, reason))
    output.print_html("".join(html))
