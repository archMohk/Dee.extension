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
  - LocationCurve elements (walls, beams, ...) - the curve's midpoint;
    a wall's own location curve already lies at its base elevation, so
    this reads as "move the wall's base to the surface" for a straight
    wall (a wall's rigid MoveElement can't tilt to match slope along its
    own length, so a long wall crossing a slope will only match the
    slope AT its midpoint - a plain limitation of a rigid translation).
  - anything else (DirectShapes, imports, ...) - its bounding box's
    horizontal center and Min.Z (bottom), the closest generic "base"
    available without knowing the element's own semantics.

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
"""
from pyrevit import forms, script
import dee_telemetry
dee_telemetry.check_access("DeeDropToSurface")

from System.Collections.Generic import List

from Autodesk.Revit.UI.Selection import ObjectType, ISelectionFilter
from Autodesk.Revit.DB import (
    FilteredElementCollector, View3D, ReferenceIntersector, FindReferenceTarget,
    XYZ, Transaction, Floor, ElementTransformUtils, ElementId,
    LocationPoint, LocationCurve,
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

    moved, no_reference, no_hit, pinned_skipped, failed = [], [], [], [], []

    t = Transaction(doc, "DeeDropToSurface - drop elements onto surface")
    t.Start()
    for el in elements:
        if el.Id == target.Id:
            continue
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
    if moved:
        t.Commit()
    else:
        t.RollBack()

    lines = [u"**DeeDropToSurface — done**", u"",
             u"- Target surface: id {0}".format(target.Id),
             u"- Moved onto surface: {0:,}".format(len(moved))]
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

    skipped_total = len(pinned_skipped) + len(no_reference) + len(no_hit) + len(failed)
    forms.alert(
        u"Moved {0:,} element(s) onto the picked surface.{1}".format(
            len(moved),
            u"\n\n{0:,} element(s) were skipped - see the output window for "
            u"why.".format(skipped_total) if skipped_total else u""),
        title=_TOOL)


main()
