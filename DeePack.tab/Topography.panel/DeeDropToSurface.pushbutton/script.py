# -*- coding: utf-8 -*-
"""
DeeDropToSurface (Topography)
Picks any number of elements, then a single Floor or Toposolid, and moves
each picked element STRAIGHT UP/DOWN so it rests on that surface - X/Y
never changes, only Z. Each element gets its OWN vertical offset, computed
from the surface's elevation directly below (or above) that element's own
XY location, so a sloped Floor or Toposolid is respected: elements spread
across a ramp or hillside each land at the right height for their own
spot, instead of every element moving by one flat, shared amount.

Deliberately sequential native/pyRevit dialogs, NO custom WPF window -
this flow needs PickObject/PickObjects, and this codebase's hard rule is
never to call PickObject (or open a second WPF dialog) from inside an
open WPF window (see DeeCtotopo's own docstring for the same reasoning -
this tool mirrors DeeCtotopo's whole interaction style on purpose, being
the second tool in the same Topography panel).

How the per-element Z is found
-------------------------------
Autodesk.Revit.DB.ReferenceIntersector, filtered (via the ElementId-list
constructor) to ONLY the picked target's own id, fires one ray straight
down from well above the target's bounding box through each element's own
XY. The nearest hit against the target's face gives that column's exact
surface elevation - correct for a flat OR sloped Floor/Toposolid, and
immune to any other geometry in the model since the intersector only ever
tests against the target.

Any element type is accepted (best-effort, by explicit request) - the
reference point used for "this element's XY and current base Z" is:
  - LocationPoint elements (most families: furniture, planting, generic
    models, columns, ...) - the location point itself.
  - LocationCurve elements OTHER than a straight Wall (beams, curved
    walls, ...) - the curve's midpoint; a plain rigid vertical move can
    only match the slope AT that one point along the element's length.
  - anything else (DirectShapes, imports, ...) - its bounding box's
    horizontal center and Min.Z (bottom), the closest generic "base"
    available without knowing the element's own semantics.

Straight walls (Wall with a LocationCurve whose Curve is a Line) get
different, more invasive treatment - see "Wall handling" below - because
a live report showed the plain vertical move leaving a straight wall's
base floating above/buried below an undulating Toposolid, which is a
geometry fact: a single translation can never make a straight, rigid
wall's bottom edge follow a curved surface.

--------------------------------------------------------------------
Wall handling - delete + rebuild with a stepped, sloped bottom edge
--------------------------------------------------------------------
Revit's own "Attach Base" command (the one that reshapes a wall's
bottom edge to hug a Floor/Toposolid/Roof underneath it) has NO public
API equivalent - confirmed via the Revit API forum ("Wall: attach
top/base, no API?") - so DeeDropToSurface cannot call it, at any Revit
version. The only way to make a wall's base actually follow a slope is
to give it a genuinely different SHAPE: delete the original wall and
recreate it via the profile-based Wall.Create(doc, IList<Curve> profile,
wallTypeId, levelId, structural, normal) overload, whose closed boundary
is: a STEPPED bottom (one straight segment per sampled point along the
wall's length - _sample_bottom_points, roughly one sample every 2 feet,
capped 2-40 samples), vertical edges at both ends, and a FLAT top at the
wall's original top elevation (only the base follows the terrain; the
top stays level, matching a typical retaining/site wall on a slope).

This is explicitly DESTRUCTIVE and asked for by name (the alternative -
just fixing the wall's Base Level/Offset without reshaping it - was
turned down as insufficient): the original wall is deleted, so anything
hosted on it (doors, windows, wall-hosted annotation) is lost and would
need re-placing on the new wall afterward. The confirmation dialog
states the wall count AND the total dependent/hosted element count
(Element.GetDependentElements) up front, and the whole run can be
declined - a "No" skips EVERY straight wall entirely (never moved at
all) rather than silently falling back to the flawed plain vertical
move that prompted this feature.

_rebuild_wall_with_profile() is verify-before-delete: it builds and
validates the NEW wall completely before the ORIGINAL is ever deleted,
so a failure partway through (a malformed profile, an API rejection)
leaves the original wall untouched and reports that wall as failed,
never as a silent data loss. Curved walls (LocationCurve whose Curve is
an Arc/Spline, not a Line) are NOT supported by this path - the sampled-
points profile assumes a single straight trace - and are always skipped
and reported separately, regardless of the rebuild confirmation.

Preserved from the original wall: WallType, base Level (LevelId), and
orientation (Wall.Orientation, passed as Wall.Create's `normal` so the
new wall keeps the same inside/outside side). NOT preserved: whether the
wall was Structural (simplified to always non-structural - this tool
targets site/landscape context, where a wall being dropped onto terrain
is architectural in practice) and any wall-hosted elements (see above).

--------------------------------------------------------------------
Floor handling - shape-edit (Add Point) instead of moving
--------------------------------------------------------------------
A picked Floor is never rigidly moved - a live report showed a flat
Floor staying perfectly flat and cutting through a sloped Toposolid,
and asked for the Floor itself to be reshaped so its top surface follows
the terrain, "by adding Point" (Revit's own Shape Editing "Add Point"
tool, not Attach Base - a Floor's PLAN OUTLINE stays exactly where it
is; only its top surface warps).

_shape_edit_floor_to_surface() enables SlabShapeEditor on the picked
Floor (Revit only allows shape editing on a flat, HORIZONTAL floor with
no shape edits yet - SlabShapeEditor is None otherwise, reported as a
failure for that floor) and calls AddPoint(XYZ(x, y, z)) - z from the
same _surface_z_at() raycast every other element uses - across a grid
spanning the floor's own bounding box (spacing starts at 3 feet, widened
so a very large floor never adds more than ~300 points). AddPoint's XYZ
is in the same absolute, Project-Base-Point-relative coordinate space as
every other XYZ this tool already works in - confirmed via the Revit API
forum discussion of how SlabShapeVertices/AddPoint coordinates compare
to the UI's own "offset from Top Plane" display, which is a presentation
convenience layered over the same absolute coordinates.

This ONLY adds interior points - it deliberately does NOT touch the
Floor's existing boundary/corner vertices (Revit's own ModifySubElement,
which WOULD move them, takes an offset-from-original-face value with
more ambiguous semantics than AddPoint's plain absolute Z - not used
here to keep this one behaviour unambiguous). A grid point that lands
exactly on the boundary or just outside the Floor's actual (possibly
non-rectangular) footprint is expected to fail - AddPoint requires a
point strictly inside - and is silently skipped per-point rather than
aborting the whole floor; only a floor where EVERY grid point failed is
reported as a failure. Non-destructive and fully within the one shared
Transaction, so declining is as simple as Undo - no separate "are you
sure" confirmation is asked for this, unlike the Wall rebuild above.

Pinned elements are skipped and reported, not auto-unpinned - unlike
DeeASelect's Move/Mirror (which unpins/re-pins around a single shared
transform), this tool moves each element by a DIFFERENT amount, so
silently unpinning someone's intentionally-fixed furniture to drop it
somewhere they didn't explicitly ask to move it to felt like the wrong
default; skipping and telling the user is safer for a tool whose whole
point is touching many unrelated elements at once.

--------------------------------------------------------------------
NEEDS LIVE-REVIT VERIFICATION (per this codebase's convention)
--------------------------------------------------------------------
1. ReferenceIntersector's ElementId-list constructor is a new technique
   for this codebase (grepped repo-wide, nothing reuses it) - the
   general approach is a standard, well-documented Revit API pattern,
   but its exact include/exclude semantics have not been exercised live
   here yet. _surface_z_at() re-checks every hit's own ElementId against
   the target regardless, so a wrong assumption there fails safe (a
   no-hit skip) rather than silently using some other element's height.
2. Elements whose MoveElement legitimately fails for reasons unrelated
   to pinning (hosted elements tied to their host, elements inside a
   Group, etc.) are caught per-item and reported as failed rather than
   aborting the whole run - not exhaustively tested against every
   element type Revit has.
3. Wall.Create's profile overload is documented (pyRevit forum) to have
   at least one known Revit-version-specific quirk: failing specifically
   when the wall's level is the PROJECT'S LOWEST level, working fine on
   Level 2 and above. _rebuild_wall_with_profile() is verify-before-
   delete specifically because of this - a failure there (including that
   exact case) leaves the original wall untouched and reports it as
   failed, rather than losing it. Not exercised live against a real
   Level-1 wall from this session.
4. SlabShapeEditor.AddPoint's exact behaviour at a grid's worth of points
   in one go (as opposed to the one-or-few-points-by-hand the Revit UI's
   own tool is normally used for) is new territory - the per-point
   try/except means a batch of individual point failures degrades to a
   sparser shape rather than crashing, but this has not been exercised
   live against a real, non-trivial Floor shape yet.
"""
from pyrevit import forms, script
import dee_telemetry
dee_telemetry.check_access("DeeDropToSurface")

import math

from System.Collections.Generic import List

from Autodesk.Revit.UI.Selection import ObjectType, ISelectionFilter
from Autodesk.Revit.DB import (
    FilteredElementCollector, View3D, ReferenceIntersector, FindReferenceTarget,
    XYZ, Transaction, Floor, ElementTransformUtils, ElementId,
    LocationPoint, LocationCurve, Wall, Line, Curve,
)

try:
    from Autodesk.Revit.DB import Toposolid
except ImportError:
    Toposolid = None

_TOOL = "DeeDropToSurface"
output = script.get_output()

# Comfortably above any realistic site/building surface's own top face -
# the ray only ever needs to start ABOVE the target, since the
# intersector is filtered to the target's own id alone (see module
# docstring) and so cannot be blocked by any element in between.
_SEARCH_MARGIN = 500.0  # feet

# Floor shape-edit grid: starts at one point every 3 feet, widened so a
# very large floor never asks for more than ~300 AddPoint calls.
_FLOOR_GRID_BASE_SPACING = 3.0  # feet
_FLOOR_GRID_MAX_POINTS = 300


class _TargetFilter(ISelectionFilter):
    """Floor or Toposolid only - picking anything else as the surface to
    drop onto makes no sense for this tool."""
    def AllowElement(self, element):
        if isinstance(element, Floor):
            return True
        if Toposolid is not None and isinstance(element, Toposolid):
            return True
        return False

    def AllowReference(self, reference, position):
        return False


def _pick_elements(uidoc, doc):
    try:
        sel_ids = list(uidoc.Selection.GetElementIds())
    except Exception:
        sel_ids = []
    if sel_ids:
        use_selection = forms.alert(
            u"{0:,} element(s) are already selected in Revit - use those?"
            .format(len(sel_ids)), title=_TOOL, yes=True, no=True)
        if use_selection:
            return [doc.GetElement(eid) for eid in sel_ids
                    if doc.GetElement(eid) is not None]
    try:
        refs = uidoc.Selection.PickObjects(
            ObjectType.Element,
            "Select the elements to drop onto a Floor or Toposolid, "
            "then press Finish")
    except Exception:
        return None  # user pressed Esc
    return [doc.GetElement(r.ElementId) for r in refs
            if doc.GetElement(r.ElementId) is not None]


def _pick_target(uidoc):
    try:
        ref = uidoc.Selection.PickObject(
            ObjectType.Element, _TargetFilter(),
            "Pick the Floor or Toposolid to drop the elements onto")
    except Exception:
        return None  # user pressed Esc
    return uidoc.Document.GetElement(ref.ElementId)


def _first_3d_view(doc):
    for v in FilteredElementCollector(doc).OfClass(View3D):
        if not v.IsTemplate:
            return v
    return None


def _reference_point(el):
    """(x, y, current base z) to use for this element, or None if nothing
    usable could be found at all - see the module docstring's own
    breakdown of the three cases."""
    try:
        loc = el.Location
        if isinstance(loc, LocationPoint):
            return loc.Point
        if isinstance(loc, LocationCurve):
            return loc.Curve.Evaluate(0.5, True)
    except Exception:
        pass
    try:
        bbox = el.get_BoundingBox(None)
        if bbox is not None:
            return XYZ((bbox.Min.X + bbox.Max.X) / 2.0,
                       (bbox.Min.Y + bbox.Max.Y) / 2.0,
                       bbox.Min.Z)
    except Exception:
        pass
    return None


def _surface_z_at(intersector, x, y, search_top, target_id):
    """target_id is re-checked against the actual hit's ElementId even
    though the intersector is already constructed to test only that one
    element - the exact include/exclude semantics of ReferenceIntersector's
    ICollection<ElementId> constructor are the one part of this tool not
    exercised live yet (see module docstring). This makes a wrong
    assumption there fail SAFE (reported as no-hit) instead of silently
    returning some other element's elevation."""
    try:
        result = intersector.FindNearest(XYZ(x, y, search_top), XYZ(0.0, 0.0, -1.0))
    except Exception:
        result = None
    if result is None:
        return None
    try:
        reference = result.GetReference()
        if reference.ElementId != target_id:
            return None
        return reference.GlobalPoint.Z
    except Exception:
        return None


# ==========================================================================
# wall handling - delete + rebuild with a sloped/stepped bottom edge
# ==========================================================================
def _is_straight_wall(el):
    if not isinstance(el, Wall):
        return False
    try:
        loc = el.Location
        return isinstance(loc, LocationCurve) and isinstance(loc.Curve, Line)
    except Exception:
        return False


def _dependent_count(doc, el):
    """How many OTHER elements would be deleted along with `el` (hosted
    doors/windows, wall-hosted annotation, ...) - GetDependentElements
    includes `el` itself in the returned set, hence the -1."""
    try:
        ids = list(el.GetDependentElements(None))
        return max(0, len(ids) - 1)
    except Exception:
        return 0


def _sample_bottom_points(intersector, p1, p2, search_top, target_id, fallback_z):
    """One point every ~2 feet along the wall's own straight trace (p1 to
    p2), each with the target surface's Z directly below/above it - a
    hole in the surface at one sample (no_hit) falls back to `fallback_z`
    (the wall's own original base) rather than leaving a gap in the
    profile. Capped 2-40 points so a very long wall doesn't build an
    unreasonably dense profile."""
    length = p1.DistanceTo(p2)
    n = max(2, min(40, int(length / 2.0) + 1))
    points = []
    for i in range(n):
        t = float(i) / float(n - 1)
        x = p1.X + (p2.X - p1.X) * t
        y = p1.Y + (p2.Y - p1.Y) * t
        z = _surface_z_at(intersector, x, y, search_top, target_id)
        if z is None:
            z = fallback_z
        points.append(XYZ(x, y, z))
    return points


def _rebuild_wall_with_profile(doc, wall, intersector, search_top, target_id):
    """Deletes nothing itself - builds and returns a NEW wall whose
    bottom edge steps along the sampled surface points, a flat top at
    the original top elevation, and the original's type/level/
    orientation. Raises on any failure; the caller only deletes the
    ORIGINAL wall after this returns successfully (verify-before-delete -
    see the module docstring's Wall handling section for why)."""
    curve = wall.Location.Curve
    p1, p2 = curve.GetEndPoint(0), curve.GetEndPoint(1)
    bbox = wall.get_BoundingBox(None)
    top_z = bbox.Max.Z
    fallback_z = bbox.Min.Z

    bottom = _sample_bottom_points(intersector, p1, p2, search_top, target_id, fallback_z)

    profile = List[Curve]()
    for i in range(len(bottom) - 1):
        profile.Add(Line.CreateBound(bottom[i], bottom[i + 1]))
    last, first = bottom[-1], bottom[0]
    top_last = XYZ(last.X, last.Y, top_z)
    top_first = XYZ(first.X, first.Y, top_z)
    profile.Add(Line.CreateBound(last, top_last))
    profile.Add(Line.CreateBound(top_last, top_first))
    profile.Add(Line.CreateBound(top_first, first))

    wall_type_id = wall.GetTypeId()
    try:
        level_id = wall.LevelId
    except Exception:
        level_id = ElementId.InvalidElementId
    try:
        normal = wall.Orientation
    except Exception:
        normal = None

    # structural is deliberately not preserved - see module docstring's
    # Wall handling section.
    if normal is not None:
        return Wall.Create(doc, profile, wall_type_id, level_id, False, normal)
    return Wall.Create(doc, profile, wall_type_id, level_id, False)


# ==========================================================================
# floor handling - shape-edit (Add Point) instead of moving
# ==========================================================================
def _shape_edit_floor_to_surface(doc, floor, intersector, search_top, target_id):
    """Adds interior AddPoint shape points across `floor`'s own bounding
    box, each snapped to the target surface's elevation directly below
    it - see the module docstring's Floor handling section for the full
    rationale (absolute-Z confirmation, boundary vertices untouched,
    why failures are per-point). Returns the number of points actually
    added; raises only if the floor cannot be shape-edited AT ALL
    (SlabShapeEditor is None - not flat/horizontal, or already has shape
    edits)."""
    editor = floor.SlabShapeEditor
    if editor is None:
        raise Exception(
            "This floor can't be shape-edited - Revit only allows it on a "
            "flat, horizontal floor with no existing shape edits.")
    if not editor.IsEnabled:
        editor.Enable()

    bbox = floor.get_BoundingBox(None)
    width = max(bbox.Max.X - bbox.Min.X, 0.001)
    depth = max(bbox.Max.Y - bbox.Min.Y, 0.001)
    spacing = max(_FLOOR_GRID_BASE_SPACING,
                  math.sqrt((width * depth) / float(_FLOOR_GRID_MAX_POINTS)))

    added = 0
    y = bbox.Min.Y + spacing
    while y < bbox.Max.Y:
        x = bbox.Min.X + spacing
        while x < bbox.Max.X:
            z = _surface_z_at(intersector, x, y, search_top, target_id)
            if z is not None:
                try:
                    editor.AddPoint(XYZ(x, y, z))
                    added += 1
                except Exception:
                    # Off the floor's actual (possibly non-rectangular)
                    # footprint, or on its boundary - expected, per-point.
                    pass
            x += spacing
        y += spacing
    return added


def main():
    uidoc = __revit__.ActiveUIDocument
    if uidoc is None:
        forms.alert("Open a Revit project first.", title=_TOOL)
        return
    doc = uidoc.Document

    elements = _pick_elements(uidoc, doc)
    if not elements:
        return

    target = _pick_target(uidoc)
    if target is None:
        return

    view3d = _first_3d_view(doc)
    if view3d is None:
        forms.alert(
            "This project has no 3D view - DeeDropToSurface needs one "
            "(even closed/not-current) to find the surface's elevation "
            "under each element. Create any 3D view and try again.",
            title=_TOOL)
        return

    target_bbox = target.get_BoundingBox(None)
    if target_bbox is None:
        forms.alert("Could not read the picked surface's geometry.", title=_TOOL)
        return
    search_top = target_bbox.Max.Z + _SEARCH_MARGIN

    target_ids = List[ElementId]()
    target_ids.Add(target.Id)
    intersector = ReferenceIntersector(target_ids, FindReferenceTarget.Face, view3d)
    intersector.FindReferencesInRevitLinks = False

    # Straight walls get the destructive rebuild path (see module
    # docstring's Wall handling section); curved walls can't use it at
    # all. Floors get shape-edited (Add Point) instead of moved (see the
    # Floor handling section). Everything else keeps the plain vertical
    # move.
    straight_walls, curved_wall_ids, floors_to_shape, others = [], [], [], []
    for el in elements:
        if el.Id == target.Id:
            continue
        if isinstance(el, Wall):
            if _is_straight_wall(el):
                straight_walls.append(el)
            else:
                curved_wall_ids.append(el.Id)
        elif isinstance(el, Floor):
            floors_to_shape.append(el)
        else:
            others.append(el)

    if straight_walls:
        dep_total = sum(_dependent_count(doc, w) for w in straight_walls)
        dep_note = (
            u"\n\n{0:,} hosted/dependent element(s) on those walls (doors, "
            u"windows, wall-hosted annotation, ...) will be LOST and need "
            u"re-placing afterward.".format(dep_total)
            if dep_total else u"")
        proceed = forms.alert(
            u"{0:,} straight wall(s) will be DELETED and REBUILT with a "
            u"stepped bottom edge that follows the picked surface's "
            u"slope - Revit's own Attach Base has no public API, so this "
            u"is the only way to actually make a wall's base hug a "
            u"slope.{1}\n\nChoose No to skip these walls entirely instead "
            u"(they will not be moved at all).\n\nRebuild these walls?"
            .format(len(straight_walls), dep_note),
            title=_TOOL + " - Wall Rebuild", yes=True, no=True)
        if not proceed:
            skipped_wall_ids = curved_wall_ids + [w.Id for w in straight_walls]
            straight_walls = []
        else:
            skipped_wall_ids = curved_wall_ids
    else:
        skipped_wall_ids = curved_wall_ids

    moved, no_reference, no_hit, pinned_skipped, failed = [], [], [], [], []
    rebuilt, rebuild_failed = [], []
    shaped, shape_failed = [], []

    t = Transaction(doc, "DeeDropToSurface - drop elements onto surface")
    t.Start()
    for el in others:
        try:
            if el.Pinned:
                pinned_skipped.append(el.Id)
                continue
        except Exception:
            pass
        ref_pt = _reference_point(el)
        if ref_pt is None:
            no_reference.append(el.Id)
            continue
        hit_z = _surface_z_at(intersector, ref_pt.X, ref_pt.Y, search_top, target.Id)
        if hit_z is None:
            no_hit.append(el.Id)
            continue
        delta_z = hit_z - ref_pt.Z
        if abs(delta_z) < 1e-6:
            moved.append(el.Id)
            continue
        try:
            ElementTransformUtils.MoveElement(doc, el.Id, XYZ(0.0, 0.0, delta_z))
            moved.append(el.Id)
        except Exception as e:
            failed.append((el.Id, str(e)))

    for wall in straight_walls:
        try:
            if wall.Pinned:
                pinned_skipped.append(wall.Id)
                continue
        except Exception:
            pass
        try:
            new_wall = _rebuild_wall_with_profile(doc, wall, intersector, search_top, target.Id)
            doc.Delete(wall.Id)
            rebuilt.append(new_wall.Id)
        except Exception as e:
            rebuild_failed.append((wall.Id, str(e)))

    for floor in floors_to_shape:
        try:
            if floor.Pinned:
                pinned_skipped.append(floor.Id)
                continue
        except Exception:
            pass
        try:
            n = _shape_edit_floor_to_surface(doc, floor, intersector, search_top, target.Id)
            if n > 0:
                shaped.append((floor.Id, n))
            else:
                shape_failed.append((floor.Id,
                    "No interior points landed on the surface's extent."))
        except Exception as e:
            shape_failed.append((floor.Id, str(e)))

    if moved or rebuilt or shaped:
        t.Commit()
    else:
        t.RollBack()

    lines = [u"**DeeDropToSurface — done**", u"",
             u"- Target surface: id {0}".format(target.Id),
             u"- Moved onto surface: {0:,}".format(len(moved))]
    if rebuilt:
        lines.append(u"- Walls rebuilt with a sloped bottom edge: {0:,}".format(len(rebuilt)))
    if rebuild_failed:
        lines.append(u"- Walls FAILED to rebuild (original kept, untouched): {0:,}, "
                     u"first error: {1}".format(len(rebuild_failed), rebuild_failed[0][1]))
    if skipped_wall_ids:
        lines.append(u"- Walls skipped (curved, or rebuild declined - not moved at all): {0:,}"
                     .format(len(skipped_wall_ids)))
    if shaped:
        total_pts = sum(n for _id, n in shaped)
        lines.append(u"- Floors shape-edited to follow the surface: {0:,} "
                     u"({1:,} point(s) added total)".format(len(shaped), total_pts))
    if shape_failed:
        lines.append(u"- Floors FAILED to shape-edit (untouched): {0:,}, "
                     u"first reason: {1}".format(len(shape_failed), shape_failed[0][1]))
    if pinned_skipped:
        lines.append(u"- Skipped (PINNED - untouched): {0:,}".format(len(pinned_skipped)))
    if no_reference:
        lines.append(u"- Skipped (no usable location/geometry): {0:,}".format(len(no_reference)))
    if no_hit:
        lines.append(u"- Skipped (its X/Y falls outside the surface's extent): {0:,}"
                     .format(len(no_hit)))
    if failed:
        lines.append(u"- FAILED to move: {0:,}, first error: {1}"
                     .format(len(failed), failed[0][1]))
    output.print_md(u"\n".join(lines))

    skipped_total = (len(pinned_skipped) + len(no_reference) + len(no_hit)
                     + len(failed) + len(skipped_wall_ids) + len(rebuild_failed)
                     + len(shape_failed))
    forms.alert(
        u"Moved {0:,} element(s), rebuilt {1:,} wall(s), and shape-edited "
        u"{2:,} floor(s) onto the picked surface.{3}".format(
            len(moved), len(rebuilt), len(shaped),
            u"\n\n{0:,} element(s)/wall(s)/floor(s) were skipped or failed - "
            u"see the output window for why.".format(skipped_total)
            if skipped_total else u""),
        title=_TOOL)


main()
