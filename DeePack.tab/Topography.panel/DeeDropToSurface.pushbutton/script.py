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
"""
from pyrevit import forms, script
import dee_telemetry
dee_telemetry.check_access("DeeDropToSurface")

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
    # all; everything else keeps the plain vertical move.
    straight_walls, curved_wall_ids, others = [], [], []
    for el in elements:
        if el.Id == target.Id:
            continue
        if isinstance(el, Wall):
            if _is_straight_wall(el):
                straight_walls.append(el)
            else:
                curved_wall_ids.append(el.Id)
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

    if moved or rebuilt:
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
                     + len(failed) + len(skipped_wall_ids) + len(rebuild_failed))
    forms.alert(
        u"Moved {0:,} element(s) and rebuilt {1:,} wall(s) onto the picked "
        u"surface.{2}".format(
            len(moved), len(rebuilt),
            u"\n\n{0:,} element(s)/wall(s) were skipped or failed - see "
            u"the output window for why.".format(skipped_total)
            if skipped_total else u""),
        title=_TOOL)


main()
