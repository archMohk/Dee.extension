# -*- coding: utf-8 -*-
"""
DeeLazy - URS Files: "Levels & Grids only" cleaner.

strip_to_levels_and_grids(doc, options) removes everything from an
already-open (headless, detached) document except its Levels and Grids,
and can optionally reset the coordinates to the internal origin.

Built on lib/dee_transmit_service.py (DeeTransmit's cleaner) rather than
new deletion code: its delete_ids() deletes in bulk and falls back to one
element at a time, and its transactions carry SilentDeletionFailures so a
"dimension lost its reference" style error is resolved instead of
stopping the batch on a modal.

Order (fixed here, because every step frees more for the next):
  1. revisions, schedules, legends, sheets
  2. views - all of them, except floor plans + elevations when
     "keep views" is ticked; one floor plan is ALWAYS kept so the model is
     never left without a view
  3. links / CAD / images / point clouds / secondary design options /
     scope boxes / guide grids
  4. groups, then every remaining model / annotation / analytical
     element instance whose category is not Levels or Grids
  5. unused families, materials, view templates, filters, phases, then
     Revit's own Purge Unused (2024+)
  6. optional move of grids + levels (+ kept scope boxes and elevation
     markers) so the grid layout's anchor point lands ON the internal
     origin - well inside the 3 m x 3 m zone around it
  7. optional coordinate reset

Scope boxes: the Revit API has NO way to create a scope box (there is no
ScopeBox.Create, and its extents cannot be set either), so this tool can
only KEEP the scope boxes already in the model and move them with the
grids. "keep_scope_boxes" turns that on.

NEEDS LIVE-REVIT VERIFICATION (written without Revit access):
- Coordinate reset: moves the Project Base Point and Survey Point to the
  internal origin with ElementTransformUtils.MoveElement, then sets the
  active ProjectLocation's position to (0,0,0, angle 0) so shared
  coordinates match internal coordinates. Each step is tried and reported
  on its own.
"""
from Autodesk.Revit.DB import (
    FilteredElementCollector, BuiltInCategory, CategoryType, ElementId,
    View, ViewType, Group, ElevationMarker, BasePoint, XYZ, ProjectPosition,
    ElementTransformUtils, Grid, Level, Line,
)
from System.Collections.Generic import List

import dee_transmit_service as ts
import deew_clean_service as base

_CHUNK = 2000

# Categories that are never deleted. Looked up by name so a category
# missing on an older Revit version is simply skipped.
_KEEP_CATEGORY_NAMES = (
    "OST_Levels", "OST_Grids", "OST_GridChains",
    "OST_ProjectBasePoint", "OST_SharedBasePoint", "OST_IOS_GeoSite",
    "OST_ProjectInformation", "OST_Viewers", "OST_Cameras", "OST_SectionBox",
    "OST_Sun", "OST_SketchLines",
)

# .NET class names that are never deleted directly - views are handled in
# their own step, sketches go with the element that owns them.
_KEEP_CLASS_NAMES = set([
    "Level", "Grid", "MultiSegmentGrid", "BasePoint", "InternalOrigin",
    "ProjectInfo", "Sketch", "SketchPlane", "ProjectLocation", "SiteLocation",
])

_KEPT_VIEW_TYPES = (ViewType.FloorPlan, ViewType.Elevation)


ANCHOR_INTERSECTION = "intersection"
ANCHOR_CENTRE = "centre"
_ZONE_FT = 3.0 / 0.3048 / 2.0   # half of 3 m, in Revit feet


def _keep_category_ids(keep_scope_boxes=False):
    ids = set()
    names = list(_KEEP_CATEGORY_NAMES)
    if keep_scope_boxes:
        names.append("OST_VolumeOfInterest")
    for name in names:
        bic = getattr(BuiltInCategory, name, None)
        if bic is not None:
            try:
                ids.add(int(bic))
            except Exception:
                pass
    return ids


def _chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _delete_in_chunks(doc, ids, name):
    total = 0
    for chunk in _chunks(ids, _CHUNK):
        total += ts.delete_ids(doc, chunk, name)
    return total


def _delete_views(doc, keep_views):
    """Every non-template view except (optionally) floor plans and
    elevations. One floor plan always survives."""
    views = ts._collect(doc, cls=View)
    keep_one = None
    for v in views:
        try:
            if not v.IsTemplate and v.ViewType == ViewType.FloorPlan:
                keep_one = ts._eid(v.Id)
                break
        except Exception:
            continue
    ids = []
    for v in views:
        try:
            if ts._view_bucket(v) != "view":
                continue
            if ts._eid(v.Id) == keep_one:
                continue
            if keep_views and v.ViewType in _KEPT_VIEW_TYPES:
                continue
            ids.append(v.Id)
        except Exception:
            continue
    return _delete_in_chunks(doc, ids, "URS Files - Delete Views")


def _model_element_ids(doc, keep_views, keep_scope_boxes=False):
    keep_cats = _keep_category_ids(keep_scope_boxes)
    wanted_types = set([CategoryType.Model, CategoryType.Annotation])
    analytical = getattr(CategoryType, "AnalyticalModel", None)
    if analytical is not None:
        wanted_types.add(analytical)
    ids = []
    for e in ts._collect(doc, instances_only=True):
        try:
            if isinstance(e, View):
                continue
            if keep_views and isinstance(e, ElevationMarker):
                continue
            if e.GetType().Name in _KEEP_CLASS_NAMES:
                continue
            cat = e.Category
            if cat is None:
                continue
            if ts._eid(cat.Id) in keep_cats:
                continue
            if cat.CategoryType not in wanted_types:
                continue
            # members of a group go when the group goes
            try:
                if e.GroupId is not None and ts._eid(e.GroupId) > 0:
                    continue
            except Exception:
                pass
            ids.append(e.Id)
        except Exception:
            continue
    return ids


def delete_everything_but_levels_and_grids(doc, keep_views, keep_scope_boxes=False):
    groups = [g.Id for g in ts._collect(doc, cls=Group)]
    removed = _delete_in_chunks(doc, groups, "URS Files - Delete Groups")
    # two passes: deleting hosts cascades, and an element refused in the
    # first pass because of what it was attached to often goes in the second
    for _ in range(2):
        ids = _model_element_ids(doc, keep_views, keep_scope_boxes)
        if not ids:
            break
        removed += _delete_in_chunks(doc, ids, "URS Files - Delete Elements")
    return removed


def reset_coordinates(doc):
    """Returns a list of short notes, one per step."""
    notes = []
    t = ts.start_transaction(doc, "URS Files - Reset Coordinates")
    try:
        for label, getter in (("Project Base Point", BasePoint.GetProjectBasePoint),
                              ("Survey Point", BasePoint.GetSurveyPoint)):
            try:
                bp = getter(doc)
                try:
                    bp.Pinned = False
                except Exception:
                    pass
                pos = bp.Position
                if pos.IsAlmostEqualTo(XYZ.Zero):
                    notes.append("{0} already at origin".format(label))
                else:
                    ElementTransformUtils.MoveElement(doc, bp.Id, pos.Negate())
                    notes.append("{0} moved to origin".format(label))
            except Exception as e:
                notes.append("{0} not moved: {1}".format(label, e))
        try:
            doc.ActiveProjectLocation.SetProjectPosition(
                XYZ.Zero, ProjectPosition(0.0, 0.0, 0.0, 0.0))
            notes.append("shared coordinates reset (0,0,0, angle 0)")
        except Exception as e:
            notes.append("shared coordinates not reset: {0}".format(e))
        t.Commit()
    except Exception as e:
        try:
            t.RollBack()
        except Exception:
            pass
        notes.append("coordinate reset rolled back: {0}".format(e))
    return notes


def _grids(doc):
    out = []
    for g in ts._collect(doc, bic=BuiltInCategory.OST_Grids):
        if isinstance(g, Grid):
            out.append(g)
    return out


def _grid_anchor(doc, mode):
    """(x, y, label) of the point that should land on the internal
    origin, or None when there are no grids.

    intersection: the intersection of two straight grids nearest the
                  lower-left corner of the layout (normally A/1);
                  falls back to the centre when no two grids cross.
    centre:       centre of the box around every grid end point."""
    grids = _grids(doc)
    pts = []
    lines = []
    for g in grids:
        try:
            c = g.Curve
            pts.append(c.GetEndPoint(0))
            pts.append(c.GetEndPoint(1))
            if isinstance(c, Line):
                lines.append((g.Name, c))
        except Exception:
            continue
    if not pts:
        return None
    min_x = min(p.X for p in pts)
    min_y = min(p.Y for p in pts)
    centre = ((min_x + max(p.X for p in pts)) / 2.0,
              (min_y + max(p.Y for p in pts)) / 2.0, "centre of grids")
    if mode != ANCHOR_INTERSECTION:
        return centre
    best = None
    for i in range(len(lines)):
        n1, c1 = lines[i]
        d1 = c1.Direction
        for j in range(i + 1, len(lines)):
            n2, c2 = lines[j]
            d2 = c2.Direction
            cross = d1.X * d2.Y - d1.Y * d2.X
            if abs(cross) < 1e-6:
                continue   # parallel
            p1, p2 = c1.GetEndPoint(0), c2.GetEndPoint(0)
            t = ((p2.X - p1.X) * d2.Y - (p2.Y - p1.Y) * d2.X) / cross
            x, y = p1.X + d1.X * t, p1.Y + d1.Y * t
            score = (x - min_x) + (y - min_y)
            if best is None or score < best[0]:
                best = (score, x, y, "grid {0}/{1}".format(n1, n2))
    if best is None:
        return centre
    return best[1], best[2], best[3]


def _move_ids(doc, ids, vector):
    """Unpins, moves, re-pins. Returns (moved_count, error_or_None)."""
    if not ids:
        return 0, None
    pinned = []
    for eid in ids:
        e = doc.GetElement(eid)
        try:
            if e is not None and e.Pinned:
                e.Pinned = False
                pinned.append(e)
        except Exception:
            pass
    err = None
    moved = 0
    try:
        ElementTransformUtils.MoveElements(doc, List[ElementId](ids), vector)
        moved = len(ids)
    except Exception as e:
        err = str(e)
        for eid in ids:
            try:
                ElementTransformUtils.MoveElement(doc, eid, vector)
                moved += 1
            except Exception:
                continue
    for e in pinned:
        try:
            e.Pinned = True
        except Exception:
            pass
    return moved, err


def move_datums_to_origin(doc, mode=ANCHOR_INTERSECTION, keep_scope_boxes=False):
    """Moves grids (+ scope boxes, elevation markers) and levels
    horizontally so the anchor point sits on the internal origin.
    Returns a list of short notes."""
    anchor = _grid_anchor(doc, mode)
    if anchor is None:
        return ["no grids found - nothing moved"]
    ax, ay, label = anchor
    dist_m = (ax * ax + ay * ay) ** 0.5 * 0.3048
    if abs(ax) <= _ZONE_FT and abs(ay) <= _ZONE_FT:
        return ["{0} is already inside the 3 m x 3 m zone at the origin ({1:.2f} m away) - not moved"
                .format(label, dist_m)]
    vector = XYZ(-ax, -ay, 0.0)
    notes = []
    t = ts.start_transaction(doc, "URS Files - Move Grids and Levels to Origin")
    try:
        ids = [g.Id for g in _grids(doc)]
        if keep_scope_boxes:
            ids += [e.Id for e in ts._collect(doc, bic=BuiltInCategory.OST_VolumeOfInterest)]
        ids += [e.Id for e in ts._collect(doc, cls=ElevationMarker)]
        moved, err = _move_ids(doc, ids, vector)
        notes.append("{0} moved {1:.2f} m to the internal origin ({2} grid/scope box/elevation element(s))"
                     .format(label, dist_m, moved))
        if err:
            notes.append("bulk move refused, moved one by one: {0}".format(err))
        level_ids = [lv.Id for lv in ts._collect(doc, cls=Level)]
        lmoved, lerr = _move_ids(doc, level_ids, vector)
        if lmoved:
            notes.append("{0} level(s) moved with the grids".format(lmoved))
        elif level_ids:
            notes.append("levels not moved sideways ({0}) - their heights are unchanged"
                         .format(lerr or "Revit refused"))
        t.Commit()
    except Exception as e:
        try:
            t.RollBack()
        except Exception:
            pass
        notes = ["move to origin rolled back: {0}".format(e)]
    return notes


def strip_to_levels_and_grids(doc, keep_views=True, purge=True, reset_coords=False, note=None,
                              move_to_origin=False, anchor_mode=ANCHOR_INTERSECTION,
                              keep_scope_boxes=False):
    """Returns (result: ts.TransmitResult, notes: list) - notes cover the
    move to origin and the coordinate reset."""
    result = ts.TransmitResult()
    ts._STATS = ts.new_failure_stats()

    def step(label, key, fn):
        if note is not None:
            try:
                note(label)
            except Exception:
                pass
        ts._run(result, key, fn)

    step("revisions", "revisions", lambda: ts.delete_revisions(doc))
    step("schedules", "schedules", lambda: ts.delete_schedules(doc, ts.SCOPE_ALL))
    step("legends", "legends", lambda: ts.delete_legends(doc, ts.SCOPE_ALL))
    step("sheets", "sheets", lambda: ts.delete_sheets(doc, ts.SCOPE_ALL))
    step("views", "views", lambda: _delete_views(doc, keep_views))
    step("Revit links", "Revit links", lambda: ts.delete_revit_links(doc))
    step("CAD", "CAD imports/links", lambda: ts.delete_cad(doc))
    step("images", "images", lambda: ts.delete_images(doc))
    step("point clouds", "point clouds", lambda: ts.delete_point_clouds(doc))
    step("design options", "design options", lambda: ts.delete_secondary_design_options(doc))
    if not keep_scope_boxes:
        step("scope boxes", "scope boxes", lambda: ts.delete_scope_boxes(doc))
    step("guide grids", "guide grids", lambda: ts.delete_guide_grids(doc))
    step("model elements", "elements",
         lambda: delete_everything_but_levels_and_grids(doc, keep_views, keep_scope_boxes))

    if purge:
        step("unused groups", "unused groups", lambda: base.delete_unused_groups(doc))
        step("unused families", "unused families", lambda: ts.delete_unused_families(doc))
        step("unused view templates", "unused view templates",
             lambda: ts.delete_unused_view_templates(doc))
        step("unused filters", "unused filters", lambda: ts.delete_unused_filters(doc))
        step("unused phases", "phases", lambda: ts.delete_unused_phases(doc))
        step("unused materials", "unused materials", lambda: ts.delete_unused_materials(doc))
        if note is not None:
            note("purge unused")
        try:
            purged, supported = base.purge_unused(doc)
            result.add("purged", purged)
            if not supported:
                result.notes.append("Purge Unused needs Revit 2024+ - skipped.")
        except Exception as e:
            result.errors.append("Purge failed: {0}".format(e))

    coord_notes = []
    if move_to_origin:
        if note is not None:
            note("moving grids and levels to origin")
        coord_notes += ["origin: " + n for n in move_datums_to_origin(doc, anchor_mode, keep_scope_boxes)]
    if reset_coords:
        if note is not None:
            note("coordinates")
        coord_notes += reset_coordinates(doc)

    result.failures = ts._STATS
    return result, coord_notes


def count_levels_and_grids(doc):
    levels = len(ts._collect(doc, bic=BuiltInCategory.OST_Levels))
    grids = len(ts._collect(doc, bic=BuiltInCategory.OST_Grids))
    return levels, grids


def count_scope_boxes(doc):
    return len(ts._collect(doc, bic=BuiltInCategory.OST_VolumeOfInterest))
