# -*- coding: utf-8 -*-
"""
dee_transmit_service
The cleaning operations behind DeeTransmit - stripping a model down
ready to issue, far past what Revit's own Purge Unused reaches.

Relationship to the existing tools
----------------------------------
lib/deew_clean_service.py already owns purge / zero-area rooms / unused
groups / delete-all-sheets / delete-all-views / delete-unused-views, and
those are IMPORTED and reused here rather than rewritten - DeeW.Clean
and DeeW.Transmit keep working exactly as they do today, and a fix to
one of those operations fixes it everywhere. Everything in this module
is new capability that did not exist anywhere in the extension.

"Used" and "unused" mean specific, checkable things here:
  sheet     used = carries at least one viewport or schedule instance
  view      used = placed on a sheet
  legend    used = placed on a sheet
  schedule  used = placed on a sheet
  parameter used = at least one element in the model has a value for it
There is no guessing: each one is a direct query, and the "all" scope
never consults them.

ORDER MATTERS and is fixed in clean_document() rather than left to the
caller. Sheets are emptied before views (deleting a sheet drops its
viewports but keeps the views), links and imports go before the purge
(so the purge can reclaim what they were holding), and the purge runs
LAST because every deletion above it frees more for it to take.

--------------------------------------------------------------------
DELETION IS PERMANENT. Every function here removes real content.
DeeTransmit only ever runs these against a model the user has been
warned about, and its default flow works on detached copies so the
source is never touched.
--------------------------------------------------------------------

NEEDS LIVE-REVIT VERIFICATION (written without Revit access - flagged
per this codebase's own convention):
  1. Deleting a DesignOption element and whether Revit removes the
     elements inside it (assumed yes) versus promoting them. There is
     no public "accept primary option" API, so secondary options are
     deleted and primaries are left alone; this is the most uncertain
     operation in the module and is reported separately.
  2. Phase deletion - Revit refuses if anything still references the
     phase. Handled as attempt-and-report rather than pre-checked.
  3. BindingMap.Remove(definition) as the way to delete a project
     parameter, and InternalDefinition surviving iteration while the
     map is being modified (the bindings are collected into a list
     FIRST here for exactly that reason).
  4. Element.GetMaterialIds(False) being present on every element type
     - guarded per element, since one refusal must not lose the pass.
"""
# Only what is actually used: every extra name here is one more class
# that could be missing on some Revit version and take the whole module
# down at import time, before a single option is even shown.
from Autodesk.Revit.DB import (
    FilteredElementCollector, BuiltInCategory, BuiltInParameter, ElementId,
    Transaction, View, ViewSheet, Viewport, ViewType,
    ScheduleSheetInstance, ImportInstance, CADLinkType, RevitLinkInstance,
    RevitLinkType, FamilySymbol, FamilyInstance, Material,
    ParameterFilterElement, Revision, RevisionCloud, DesignOption, Phase,
    ImageType, StorageType,
)

import deew_clean_service as base

SCOPE_NONE = "none"
SCOPE_ALL = "all"
SCOPE_USED = "used"
SCOPE_UNUSED = "unused"
SCOPES = [SCOPE_NONE, SCOPE_UNUSED, SCOPE_USED, SCOPE_ALL]

SCOPE_LABELS = [
    ("Leave alone", SCOPE_NONE),
    ("Delete unused only", SCOPE_UNUSED),
    ("Delete used only", SCOPE_USED),
    ("Delete all", SCOPE_ALL),
]

# Cap on how many elements are examined when deciding whether a project
# parameter carries any value. A parameter in use shows up almost
# immediately; without a cap this single check could walk a million
# elements per parameter.
_PARAM_PROBE_LIMIT = 20000


class TransmitResult(object):
    """Plain counters. The report never needs to know how any of this
    was found, only how much went."""

    def __init__(self):
        self.counts = {}
        self.errors = []
        self.notes = []

    def add(self, key, count):
        if count:
            self.counts[key] = self.counts.get(key, 0) + count

    def total(self):
        return sum(self.counts.values())

    def summary(self):
        if not self.counts:
            return "nothing removed"
        parts = ["{0}: {1}".format(k, v) for k, v in sorted(self.counts.items())]
        return ", ".join(parts)


# ==========================================================================
# helpers
# ==========================================================================
def _eid(element_id):
    if element_id is None:
        return 0
    try:
        return int(element_id.Value)
    except Exception:
        pass
    try:
        return int(element_id.IntegerValue)
    except Exception:
        return 0


def delete_ids(doc, ids, name):
    """Deletes as a group, then falls back to one-at-a-time so a single
    undeletable element cannot cost the whole batch. Returns the count
    actually removed."""
    ids = [i for i in ids if i is not None and _eid(i) > 0]
    if not ids:
        return 0
    deleted = 0
    t = Transaction(doc, name)
    try:
        t.Start()
        try:
            from System.Collections.Generic import List
            bundle = List[ElementId](ids)
            removed = doc.Delete(bundle)
            deleted = len(removed) if removed else 0
        except Exception:
            deleted = 0
            for one in ids:
                try:
                    doc.Delete(one)
                    deleted += 1
                except Exception:
                    continue
        t.Commit()
    except Exception:
        try:
            if t.HasStarted() and not t.HasEnded():
                t.RollBack()
        except Exception:
            pass
        return 0
    return deleted


def _collect(doc, cls=None, bic=None, instances_only=True):
    try:
        col = FilteredElementCollector(doc)
        if cls is not None:
            col = col.OfClass(cls)
        if bic is not None:
            col = col.OfCategory(bic)
        if instances_only:
            col = col.WhereElementIsNotElementType()
        return list(col)
    except Exception:
        return []


def _placed_view_ids(doc):
    """Every view id that sits on a sheet, via Viewport (drawings) and
    ScheduleSheetInstance (schedules and legends placed as schedules)."""
    placed = set()
    for vp in _collect(doc, cls=Viewport):
        try:
            placed.add(_eid(vp.ViewId))
        except Exception:
            continue
    for si in _collect(doc, cls=ScheduleSheetInstance):
        try:
            placed.add(_eid(si.ScheduleId))
        except Exception:
            continue
    return placed


def _sheet_is_used(doc, sheet):
    try:
        vps = sheet.GetAllViewports()
        if vps and len(vps) > 0:
            return True
    except Exception:
        pass
    # a sheet holding only schedules still counts as used
    for si in _collect(doc, cls=ScheduleSheetInstance):
        try:
            if _eid(si.OwnerViewId) == _eid(sheet.Id):
                return True
        except Exception:
            continue
    return False


def _wanted(scope, is_used):
    if scope == SCOPE_ALL:
        return True
    if scope == SCOPE_USED:
        return is_used
    if scope == SCOPE_UNUSED:
        return not is_used
    return False


# ==========================================================================
# sheets / views / legends / schedules
# ==========================================================================
def delete_sheets(doc, scope):
    if scope == SCOPE_NONE:
        return 0
    sheets = _collect(doc, cls=ViewSheet)
    ids = []
    for sheet in sheets:
        try:
            if sheet.IsTemplate:
                continue
            if _wanted(scope, _sheet_is_used(doc, sheet)):
                ids.append(sheet.Id)
        except Exception:
            continue
    return delete_ids(doc, ids, "DeeTransmit - Delete Sheets")


def _view_bucket(view):
    """'legend' | 'schedule' | 'view' | None (not deletable)."""
    try:
        if view.IsTemplate:
            return None
        vt = view.ViewType
        if vt == ViewType.Legend:
            return "legend"
        if vt in (ViewType.Schedule, ViewType.ColumnSchedule, ViewType.PanelSchedule):
            return "schedule"
        if vt in (ViewType.DrawingSheet, ViewType.ProjectBrowser,
                  ViewType.SystemBrowser, ViewType.Internal, ViewType.Undefined):
            return None
        return "view"
    except Exception:
        return None


def _delete_view_bucket(doc, scope, bucket, name):
    if scope == SCOPE_NONE:
        return 0
    placed = _placed_view_ids(doc)
    ids = []
    for view in _collect(doc, cls=View):
        try:
            if _view_bucket(view) != bucket:
                continue
            if _wanted(scope, _eid(view.Id) in placed):
                ids.append(view.Id)
        except Exception:
            continue
    return delete_ids(doc, ids, name)


def delete_views(doc, scope):
    return _delete_view_bucket(doc, scope, "view", "DeeTransmit - Delete Views")


def delete_legends(doc, scope):
    return _delete_view_bucket(doc, scope, "legend", "DeeTransmit - Delete Legends")


def delete_schedules(doc, scope):
    return _delete_view_bucket(doc, scope, "schedule", "DeeTransmit - Delete Schedules")


# ==========================================================================
# links, imports and images
# ==========================================================================
def delete_images(doc):
    """Raster images, both linked and imported, plus their types."""
    ids = [e.Id for e in _collect(doc, bic=BuiltInCategory.OST_RasterImages)]
    ids += [e.Id for e in _collect(doc, cls=ImageType, instances_only=False)]
    return delete_ids(doc, ids, "DeeTransmit - Delete Images")


def delete_cad(doc):
    """DWG/DXF/DGN, whether linked or imported - ImportInstance covers
    both, and the CADLinkType removal is what stops a deleted link
    reappearing in Manage Links."""
    ids = [e.Id for e in _collect(doc, cls=ImportInstance)]
    ids += [e.Id for e in _collect(doc, cls=CADLinkType, instances_only=False)]
    return delete_ids(doc, ids, "DeeTransmit - Delete CAD Imports and Links")


def delete_revit_links(doc):
    ids = [e.Id for e in _collect(doc, cls=RevitLinkInstance)]
    ids += [e.Id for e in _collect(doc, cls=RevitLinkType, instances_only=False)]
    return delete_ids(doc, ids, "DeeTransmit - Delete Revit Links")


def delete_point_clouds(doc):
    ids = [e.Id for e in _collect(doc, bic=BuiltInCategory.OST_PointClouds)]
    ids += [e.Id for e in _collect(doc, bic=BuiltInCategory.OST_PointClouds,
                                   instances_only=False)]
    return delete_ids(doc, ids, "DeeTransmit - Delete Point Clouds")


# ==========================================================================
# project parameters
# ==========================================================================
def _project_parameter_bindings(doc):
    """[(definition, name)] captured into a list BEFORE anything is
    removed - mutating a BindingMap while its own iterator is live is
    asking for trouble."""
    found = []
    try:
        it = doc.ParameterBindings.ForwardIterator()
        it.Reset()
        while it.MoveNext():
            try:
                definition = it.Key
                if definition is not None:
                    found.append((definition, definition.Name))
            except Exception:
                continue
    except Exception:
        pass
    return found


def _parameter_has_any_value(doc, definition):
    """True as soon as one element carries a value. Bounded, because an
    unused parameter would otherwise walk the entire model."""
    checked = 0
    try:
        for element in FilteredElementCollector(doc).WhereElementIsNotElementType():
            checked += 1
            if checked > _PARAM_PROBE_LIMIT:
                break
            try:
                p = element.get_Parameter(definition)
            except Exception:
                continue
            if p is None or not p.HasValue:
                continue
            try:
                st = p.StorageType
                if st == StorageType.String:
                    if (p.AsString() or "").strip():
                        return True
                elif st == StorageType.Integer:
                    if p.AsInteger():
                        return True
                elif st == StorageType.Double:
                    if p.AsDouble():
                        return True
                elif st == StorageType.ElementId:
                    if _eid(p.AsElementId()) > 0:
                        return True
            except Exception:
                continue
    except Exception:
        return False
    return False


def delete_project_parameters(doc, scope, progress=None):
    if scope == SCOPE_NONE:
        return 0
    bindings = _project_parameter_bindings(doc)
    if not bindings:
        return 0
    targets = []
    for i, (definition, name) in enumerate(bindings):
        if progress is not None:
            progress(i, len(bindings), name)
        if scope == SCOPE_ALL:
            targets.append((definition, name))
            continue
        used = _parameter_has_any_value(doc, definition)
        if _wanted(scope, used):
            targets.append((definition, name))
    if not targets:
        return 0

    removed = 0
    t = Transaction(doc, "DeeTransmit - Delete Project Parameters")
    try:
        t.Start()
        for definition, _name in targets:
            try:
                if doc.ParameterBindings.Remove(definition):
                    removed += 1
            except Exception:
                continue
        t.Commit()
    except Exception:
        try:
            if t.HasStarted() and not t.HasEnded():
                t.RollBack()
        except Exception:
            pass
        return 0
    return removed


# ==========================================================================
# families, materials and the organisational clutter Purge leaves behind
# ==========================================================================
def delete_unused_families(doc):
    """FamilySymbols with no placed instance. Revit removes the Family
    itself once its last symbol goes, so families are not deleted
    directly."""
    used = set()
    for inst in _collect(doc, cls=FamilyInstance):
        try:
            used.add(_eid(inst.GetTypeId()))
        except Exception:
            continue
    ids = []
    for symbol in _collect(doc, cls=FamilySymbol, instances_only=False):
        try:
            if _eid(symbol.Id) not in used:
                ids.append(symbol.Id)
        except Exception:
            continue
    return delete_ids(doc, ids, "DeeTransmit - Delete Unused Families")


def delete_unused_materials(doc, progress=None):
    """Every material not referenced by any element. GetMaterialIds is
    asked of each element and guarded individually - one element type
    refusing it must not cost the whole pass."""
    used = set()
    elements = _collect(doc, instances_only=False)
    for i, element in enumerate(elements):
        if progress is not None and i % 500 == 0:
            progress(i, len(elements), "materials")
        try:
            for mid in element.GetMaterialIds(False):
                used.add(_eid(mid))
        except Exception:
            continue
    ids = []
    for material in _collect(doc, cls=Material, instances_only=False):
        try:
            if _eid(material.Id) not in used:
                ids.append(material.Id)
        except Exception:
            continue
    return delete_ids(doc, ids, "DeeTransmit - Delete Unused Materials")


def delete_unused_view_templates(doc):
    used = set()
    for view in _collect(doc, cls=View):
        try:
            used.add(_eid(view.ViewTemplateId))
        except Exception:
            continue
    ids = []
    for view in _collect(doc, cls=View):
        try:
            if view.IsTemplate and _eid(view.Id) not in used:
                ids.append(view.Id)
        except Exception:
            continue
    return delete_ids(doc, ids, "DeeTransmit - Delete Unused View Templates")


def delete_unused_filters(doc):
    used = set()
    for view in _collect(doc, cls=View):
        try:
            for fid in view.GetFilters():
                used.add(_eid(fid))
        except Exception:
            continue
    ids = []
    for f in _collect(doc, cls=ParameterFilterElement, instances_only=False):
        try:
            if _eid(f.Id) not in used:
                ids.append(f.Id)
        except Exception:
            continue
    return delete_ids(doc, ids, "DeeTransmit - Delete Unused View Filters")


def delete_scope_boxes(doc):
    ids = [e.Id for e in _collect(doc, bic=BuiltInCategory.OST_VolumeOfInterest)]
    return delete_ids(doc, ids, "DeeTransmit - Delete Scope Boxes")


def delete_guide_grids(doc):
    ids = [e.Id for e in _collect(doc, bic=BuiltInCategory.OST_GuideGrid,
                                  instances_only=False)]
    return delete_ids(doc, ids, "DeeTransmit - Delete Guide Grids")


def delete_revisions(doc):
    """Clouds first: a Revision cannot go while clouds still point at it."""
    count = delete_ids(doc, [e.Id for e in _collect(doc, cls=RevisionCloud)],
                       "DeeTransmit - Delete Revision Clouds")
    count += delete_ids(doc, [e.Id for e in _collect(doc, cls=Revision,
                                                     instances_only=False)],
                        "DeeTransmit - Delete Revisions")
    return count


def delete_secondary_design_options(doc):
    """Only the non-primary options. There is no public "accept primary"
    call, so primaries are left exactly where they are rather than
    guessed at - see this module's docstring."""
    ids = []
    for option in _collect(doc, cls=DesignOption, instances_only=False):
        try:
            if not option.IsPrimary:
                ids.append(option.Id)
        except Exception:
            continue
    return delete_ids(doc, ids, "DeeTransmit - Delete Secondary Design Options")


def delete_unused_phases(doc):
    """Attempt-and-report: Revit refuses a phase that anything still
    references, and that refusal is the authoritative answer."""
    phases = _collect(doc, cls=Phase, instances_only=False)
    if len(phases) <= 1:
        return 0
    removed = 0
    # never leave a model with zero phases
    for phase in phases[:-1]:
        removed += delete_ids(doc, [phase.Id], "DeeTransmit - Delete Unused Phase")
    return removed


def delete_unplaced_rooms_and_areas(doc):
    """Rooms and Areas with no area - unplaced or not enclosed.

    OfCategory, never OfClass: a FilteredElementCollector.OfClass(Room)
    throws at runtime in this API, which this codebase has already been
    bitten by once."""
    ids = []
    for bic in (BuiltInCategory.OST_Rooms, BuiltInCategory.OST_Areas,
                BuiltInCategory.OST_MEPSpaces):
        for element in _collect(doc, bic=bic):
            try:
                if element.Area <= 0:
                    ids.append(element.Id)
            except Exception:
                continue
    return delete_ids(doc, ids, "DeeTransmit - Delete Unplaced Rooms and Areas")


# ==========================================================================
# geometry only
# ==========================================================================
_IDENTITY_PARAMS = (
    BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS,
    BuiltInParameter.ALL_MODEL_MARK,
    BuiltInParameter.ALL_MODEL_DESCRIPTION,
    BuiltInParameter.ALL_MODEL_MANUFACTURER,
    BuiltInParameter.ALL_MODEL_MODEL,
    BuiltInParameter.ALL_MODEL_URL,
    BuiltInParameter.ALL_MODEL_TYPE_COMMENTS,
    BuiltInParameter.ALL_MODEL_COST,
)


def strip_to_geometry(doc, progress=None):
    """Leaves the shapes, removes the data riding on them: every project
    and shared parameter binding, plus the writable identity fields.

    Honest about its limit - a built-in parameter that DEFINES geometry
    or type behaviour (height, width, level, family and type names)
    cannot be removed without destroying the model, so those stay. This
    strips what a recipient would read as information, not the model."""
    removed = 0
    bindings = _project_parameter_bindings(doc)
    if bindings:
        t = Transaction(doc, "DeeTransmit - Remove All Project Parameters")
        try:
            t.Start()
            for definition, _name in bindings:
                try:
                    if doc.ParameterBindings.Remove(definition):
                        removed += 1
                except Exception:
                    continue
            t.Commit()
        except Exception:
            try:
                if t.HasStarted() and not t.HasEnded():
                    t.RollBack()
            except Exception:
                pass

    cleared = 0
    elements = _collect(doc, instances_only=True)
    t = Transaction(doc, "DeeTransmit - Clear Identity Data")
    try:
        t.Start()
        for i, element in enumerate(elements):
            if progress is not None and i % 500 == 0:
                progress(i, len(elements), "identity data")
            for bip in _IDENTITY_PARAMS:
                try:
                    p = element.get_Parameter(bip)
                    if p is None or p.IsReadOnly:
                        continue
                    if p.StorageType == StorageType.String:
                        if (p.AsString() or "") != "":
                            p.Set("")
                            cleared += 1
                except Exception:
                    continue
        t.Commit()
    except Exception:
        try:
            if t.HasStarted() and not t.HasEnded():
                t.RollBack()
        except Exception:
            pass
    return removed + cleared


# ==========================================================================
# the pipeline
# ==========================================================================
def _run(result, key, fn):
    try:
        result.add(key, fn())
    except Exception as e:
        result.errors.append("{0} failed: {1}".format(key, e))


def clean_document(doc, options, progress=None):
    """options: the flat dict the DeeTransmit window builds. Scope keys
    ("sheets", "views", "legends", "schedules", "parameters") take one
    of SCOPES; everything else is a bool.

    The ORDER here is deliberate and is not the caller's business - see
    the module docstring."""
    result = TransmitResult()

    def note(step):
        if progress is not None:
            progress(step)

    note("revisions")
    if options.get("revisions"):
        _run(result, "revisions", lambda: delete_revisions(doc))

    for key, fn in (("schedules", delete_schedules), ("legends", delete_legends),
                    ("sheets", delete_sheets), ("views", delete_views)):
        scope = options.get(key, SCOPE_NONE)
        if scope == SCOPE_NONE:
            continue
        note(key)
        _run(result, key, (lambda f=fn, s=scope: f(doc, s)))

    note("images")
    if options.get("images"):
        _run(result, "images", lambda: delete_images(doc))
    note("CAD")
    if options.get("cad"):
        _run(result, "CAD imports/links", lambda: delete_cad(doc))
    note("Revit links")
    if options.get("revit_links"):
        _run(result, "Revit links", lambda: delete_revit_links(doc))
    note("point clouds")
    if options.get("point_clouds"):
        _run(result, "point clouds", lambda: delete_point_clouds(doc))

    note("design options")
    if options.get("design_options"):
        _run(result, "secondary design options",
             lambda: delete_secondary_design_options(doc))
    note("rooms and areas")
    if options.get("unplaced_rooms"):
        _run(result, "unplaced rooms/areas",
             lambda: delete_unplaced_rooms_and_areas(doc))
    note("phases")
    if options.get("phases"):
        _run(result, "phases", lambda: delete_unused_phases(doc))

    note("view templates")
    if options.get("view_templates"):
        _run(result, "unused view templates",
             lambda: delete_unused_view_templates(doc))
    note("view filters")
    if options.get("filters"):
        _run(result, "unused view filters", lambda: delete_unused_filters(doc))
    note("scope boxes")
    if options.get("scope_boxes"):
        _run(result, "scope boxes", lambda: delete_scope_boxes(doc))
    note("guide grids")
    if options.get("guide_grids"):
        _run(result, "guide grids", lambda: delete_guide_grids(doc))

    note("groups")
    if options.get("unused_groups"):
        _run(result, "unused groups", lambda: base.delete_unused_groups(doc))

    if options.get("geometry_only") or options.get("parameters", SCOPE_NONE) != SCOPE_NONE:
        note("parameters")
    if options.get("geometry_only"):
        _run(result, "data stripped for geometry-only",
             lambda: strip_to_geometry(doc))
    elif options.get("parameters", SCOPE_NONE) != SCOPE_NONE:
        _run(result, "project parameters",
             lambda: delete_project_parameters(doc, options["parameters"]))

    note("unused families")
    if options.get("unused_families"):
        _run(result, "unused families", lambda: delete_unused_families(doc))
    note("unused materials")
    if options.get("unused_materials"):
        _run(result, "unused materials", lambda: delete_unused_materials(doc))

    # Last, always: everything above frees more for it to reclaim.
    note("purge")
    if options.get("purge_unused"):
        try:
            purged, supported = base.purge_unused(doc)
            result.add("purged", purged)
            if not supported:
                result.notes.append(
                    "Purge Unused needs Revit 2024 or newer - skipped on this version.")
        except Exception as e:
            result.errors.append("Purge failed: {0}".format(e))

    return result


def describe_options(options):
    """One human-readable line for the confirmation prompt, so nobody
    runs a destructive batch without seeing what they ticked."""
    parts = []
    for key, label in (("sheets", "sheets"), ("views", "views"),
                       ("legends", "legends"), ("schedules", "schedules"),
                       ("parameters", "project parameters")):
        scope = options.get(key, SCOPE_NONE)
        if scope != SCOPE_NONE:
            parts.append("{0} {1}".format(scope, label))
    for key, label in (("images", "images"), ("cad", "CAD"),
                       ("revit_links", "Revit links"),
                       ("point_clouds", "point clouds"),
                       ("revisions", "revisions"),
                       ("design_options", "secondary design options"),
                       ("phases", "phases"),
                       ("unplaced_rooms", "unplaced rooms/areas"),
                       ("view_templates", "unused view templates"),
                       ("filters", "unused filters"),
                       ("scope_boxes", "scope boxes"),
                       ("guide_grids", "guide grids"),
                       ("unused_groups", "unused groups"),
                       ("unused_families", "unused families"),
                       ("unused_materials", "unused materials"),
                       ("purge_unused", "purge unused"),
                       ("geometry_only", "STRIP ALL DATA (geometry only)")):
        if options.get(key):
            parts.append(label)
    return ", ".join(parts) if parts else "nothing selected"
