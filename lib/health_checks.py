# -*- coding: utf-8 -*-
"""
health_checks
Revit-side check functions for DeeHealth, one per check_id referenced in
health_rubric.CHECK_ID_MAP. Every check function has the same signature:
    check_fn(doc, ctx) -> (value, detail)
`ctx` is a small dict of shared/optional settings (currently just
"naming_pattern", the regex used by the three naming-convention checks).
`value` is the raw number to score against the rubric's E/F/G
thresholds (None if the check produced nothing meaningful to score -
still reported, just excluded from the weighted score). `detail` is a
short, human-readable summary for the results report. Any exception
raised is caught by the caller (script.py) per-check, so one bad check
never blocks the rest of the run.

Four checks are explicitly approximate given real Revit API limits -
each says so plainly in its own detail string, not just in this
docstring:
  - purgeable_elements: counts unused Family Types (zero placed
    instances) as a proxy. Revit's own Purge Unused also removes unused
    materials, line patterns, fill patterns, filters, and view
    templates, which this does not replicate - there is no public API
    that exposes Purge Unused's own dependency analysis.
  - duplicate_elements: groups model elements by
    (Category, Type, rounded Location) - an approximation of "same
    place", not a full geometry comparison.
  - overlapping_rooms / overlapping_spaces: flags rooms/spaces whose
    center point falls inside another one's boundary, matching the
    rubric's own written description ("in the same location as another
    room") rather than a full boundary-overlap solve.
  - largest_family: ranks loaded families by type count + placed
    instance count, NOT actual family file size - measuring real file
    size would mean exporting/saving every family individually, which
    is impractically slow for a project with many families.
"""
import os
import re

from Autodesk.Revit.DB import (
    FilteredElementCollector, FilteredWorksetCollector, WorksetKind,
    Family, FamilyInstance, Level, Grid, View, ViewSheet, Viewport,
    CurveElement, CurveElementType, ImportInstance, RevitLinkInstance,
    RevitLinkType, ImageInstance, DesignOption, BasePoint,
    CategoryType, BuiltInCategory, BuiltInParameter, ElementId, StorageType,
)

_DEFAULT_NAMING_PATTERN = r"^[A-Za-z0-9_\-\.\s]+$"


# -- shared defensive helpers (same patterns already proven elsewhere) -----
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


def _eid_value(eid):
    try:
        return eid.Value
    except Exception:
        pass
    try:
        return eid.IntegerValue
    except Exception:
        return str(eid)


def _naming_pattern(ctx):
    return (ctx or {}).get("naming_pattern") or _DEFAULT_NAMING_PATTERN


# -- Level/Offset parameter resolution (self-contained copy of the same
# defensive candidate-list approach used in DeeRehoster, kept separate
# rather than sharing code across two independently-working tools) --------
_LEVEL_PARAM_CANDIDATES = [
    "SCHEDULE_LEVEL_PARAM", "FAMILY_LEVEL_PARAM", "LEVEL_PARAM",
    "WALL_BASE_CONSTRAINT", "ROOF_BASE_LEVEL_PARAM", "ROOF_CONSTRAINT_LEVEL_PARAM",
    "STAIRS_BASE_LEVEL_PARAM", "RBS_START_LEVEL_PARAM",
]
_OFFSET_PARAM_CANDIDATES = [
    "INSTANCE_FREE_HOST_OFFSET_PARAM", "INSTANCE_ELEVATION_PARAM",
    "WALL_BASE_OFFSET", "FLOOR_HEIGHTABOVELEVEL_PARAM",
    "CEILING_HEIGHTABOVELEVEL_PARAM", "ROOF_LEVEL_OFFSET_PARAM",
    "ROOF_CONSTRAINT_OFFSET_PARAM", "STAIRS_BASE_OFFSET",
]
_OFFSET_PARAM_NAME_FALLBACK = ("Elevation from Level", "Offset", "Base Offset", "Height Offset From Level")


def _resolve_bip(name):
    return getattr(BuiltInParameter, name, None)


def _find_offset_parameter(el):
    for name in _OFFSET_PARAM_CANDIDATES:
        bip = _resolve_bip(name)
        if bip is None:
            continue
        try:
            p = el.get_Parameter(bip)
        except Exception:
            p = None
        if p is not None and not p.IsReadOnly and p.StorageType == StorageType.Double:
            return p
    try:
        for p in el.Parameters:
            try:
                if p.IsReadOnly or p.StorageType != StorageType.Double:
                    continue
                if p.Definition.Name in _OFFSET_PARAM_NAME_FALLBACK:
                    return p
            except Exception:
                continue
    except Exception:
        pass
    return None


# -- Families Naming Convention ---------------------------------------------
def families_naming(doc, ctx):
    pattern = _naming_pattern(ctx)
    total = 0
    matched = 0
    bad_names = []
    for fam in FilteredElementCollector(doc).OfClass(Family):
        total += 1
        name = _read_name(fam) or ""
        try:
            ok = bool(re.match(pattern, name))
        except Exception:
            ok = True
        if ok:
            matched += 1
        else:
            bad_names.append(name)
    if total == 0:
        return None, "No families found"
    pct = matched / float(total)
    detail = "{0}/{1} families match the naming pattern".format(matched, total)
    if bad_names:
        shown = bad_names[:15]
        detail += " - non-matching: {0}{1}".format(
            ", ".join(shown), " ..." if len(bad_names) > 15 else "")
    return pct, detail


def worksets_naming(doc, ctx):
    pattern = _naming_pattern(ctx)
    try:
        worksets = list(FilteredWorksetCollector(doc).OfKind(WorksetKind.UserWorkset))
    except Exception:
        return None, "Worksharing not enabled"
    total = len(worksets)
    if total == 0:
        return None, "No user worksets found (worksharing not enabled)"
    matched = 0
    bad_names = []
    for w in worksets:
        try:
            name = w.Name
            ok = bool(re.match(pattern, name))
        except Exception:
            name = ""
            ok = True
        if ok:
            matched += 1
        else:
            bad_names.append(name)
    pct = matched / float(total)
    detail = "{0}/{1} worksets match the naming pattern".format(matched, total)
    if bad_names:
        detail += " - non-matching: {0}".format(", ".join(bad_names))
    return pct, detail


def line_style_naming(doc, ctx):
    pattern = _naming_pattern(ctx)
    try:
        lines_cat = doc.Settings.Categories.get_Item(BuiltInCategory.OST_Lines)
        subcats = list(lines_cat.SubCategories)
    except Exception as e:
        return None, "Could not read Line Styles: {0}".format(e)
    total = len(subcats)
    if total == 0:
        return None, "No line styles found"
    matched = 0
    bad_names = []
    for sc in subcats:
        name = _read_name(sc) or ""
        try:
            ok = bool(re.match(pattern, name))
        except Exception:
            ok = True
        if ok:
            matched += 1
        else:
            bad_names.append(name)
    pct = matched / float(total)
    detail = "{0}/{1} line styles match the naming pattern".format(matched, total)
    if bad_names:
        detail += " - non-matching: {0}".format(", ".join(bad_names[:15]))
    return pct, detail


# -- Model Performance --------------------------------------------------------
def warnings_count(doc, ctx):
    try:
        warnings = doc.GetWarnings()
    except Exception as e:
        return None, "Could not read warnings: {0}".format(e)
    count = len(list(warnings))
    return count, "{0} warning(s) in the model".format(count)


def file_size(doc, ctx):
    try:
        path = doc.PathName
    except Exception:
        path = None
    if not path or not os.path.exists(path):
        return None, "Model has no local file path (cloud-only or unsaved) - cannot measure size"
    size_mb = os.path.getsize(path) / (1024.0 * 1024.0)
    return round(size_mb, 1), "{0:.1f} MB".format(size_mb)


def purgeable_elements(doc, ctx):
    instances = ctx.get("all_family_instances") if ctx else None
    if instances is None:
        instances = list(FilteredElementCollector(doc).OfClass(FamilyInstance).WhereElementIsNotElementType())
    instance_type_ids = set()
    for inst in instances:
        try:
            instance_type_ids.add(inst.GetTypeId())
        except Exception:
            continue
    unused_types = 0
    total_types = 0
    for fam in FilteredElementCollector(doc).OfClass(Family):
        try:
            for type_id in fam.GetFamilySymbolIds():
                total_types += 1
                if type_id not in instance_type_ids:
                    unused_types += 1
        except Exception:
            continue
    detail = (
        "{0} unused Family Type(s) out of {1} (APPROXIMATE - counts unused Family "
        "Types with zero placed instances only; Revit's own Purge Unused also "
        "removes unused materials, line patterns, fill patterns, filters, and view "
        "templates, which this does not check - there is no public API for Purge "
        "Unused's own dependency analysis)".format(unused_types, total_types))
    return unused_types, detail


def duplicate_elements(doc, ctx):
    elements = ctx.get("all_elements") if ctx else None
    if elements is None:
        elements = list(FilteredElementCollector(doc).WhereElementIsNotElementType())
    groups = {}
    for el in elements:
        try:
            cat = el.Category
            if cat is None or cat.CategoryType != CategoryType.Model:
                continue
            loc = el.Location
            pt = None
            if loc is not None:
                if hasattr(loc, "Point") and loc.Point is not None:
                    pt = loc.Point
                elif hasattr(loc, "Curve") and loc.Curve is not None:
                    pt = loc.Curve.GetEndPoint(0)
            if pt is None:
                continue
            key = (_eid_value(cat.Id), _eid_value(el.GetTypeId()),
                   round(pt.X, 2), round(pt.Y, 2), round(pt.Z, 2))
            groups.setdefault(key, []).append(el.Id)
        except Exception:
            continue
    dup_groups = [ids for ids in groups.values() if len(ids) > 1]
    dup_count = sum(len(ids) - 1 for ids in dup_groups)
    detail = (
        "{0} duplicate element(s) found across {1} location(s) with more than one "
        "element (APPROXIMATE - matches by rounded Location Point/Curve-start "
        "position + Category + Type, not a full geometry comparison)"
        .format(dup_count, len(dup_groups)))
    return dup_count, detail


def total_elements(doc, ctx):
    count = len(list(FilteredElementCollector(doc).WhereElementIsNotElementType()))
    return count, "{0} placed element(s) total".format(count)


def model_groups(doc, ctx):
    count = len(list(FilteredElementCollector(doc)
                      .OfCategory(BuiltInCategory.OST_IOSModelGroups)
                      .WhereElementIsNotElementType()))
    return count, "{0} model group instance(s)".format(count)


def detail_groups(doc, ctx):
    count = len(list(FilteredElementCollector(doc)
                      .OfCategory(BuiltInCategory.OST_IOSDetailGroups)
                      .WhereElementIsNotElementType()))
    return count, "{0} detail group instance(s)".format(count)


def model_lines(doc, ctx):
    count = 0
    for cl in FilteredElementCollector(doc).OfClass(CurveElement):
        try:
            if cl.CurveElementType == CurveElementType.ModelCurve:
                count += 1
        except Exception:
            continue
    return count, "{0} model line(s)".format(count)


def generic_models(doc, ctx):
    count = len(list(FilteredElementCollector(doc)
                      .OfCategory(BuiltInCategory.OST_GenericModel)
                      .WhereElementIsNotElementType()))
    return count, "{0} generic model instance(s)".format(count)


def masses(doc, ctx):
    count = len(list(FilteredElementCollector(doc)
                      .OfCategory(BuiltInCategory.OST_Mass)
                      .WhereElementIsNotElementType()))
    return count, "{0} mass instance(s)".format(count)


def largest_family(doc, ctx):
    instances = ctx.get("all_family_instances") if ctx else None
    if instances is None:
        instances = list(FilteredElementCollector(doc).OfClass(FamilyInstance).WhereElementIsNotElementType())
    instance_counts = {}
    for inst in instances:
        try:
            sym = inst.Symbol
            fam = sym.Family if sym is not None else None
            if fam is None:
                continue
            instance_counts[fam.Id] = instance_counts.get(fam.Id, 0) + 1
        except Exception:
            continue

    ranked = []
    for fam in FilteredElementCollector(doc).OfClass(Family):
        try:
            name = _read_name(fam) or "(unnamed)"
            type_count = len(list(fam.GetFamilySymbolIds()))
            ranked.append((name, type_count, instance_counts.get(fam.Id, 0)))
        except Exception:
            continue
    ranked.sort(key=lambda r: (r[1] + r[2]), reverse=True)
    top = ranked[:10]
    detail = (
        "Top families by type+instance count (APPROXIMATE proxy for size - true "
        "family file size would need exporting/saving each family individually, "
        "impractically slow for a project with many families): "
        + "; ".join("{0} ({1} types, {2} instances)".format(n, tc, ic) for n, tc, ic in top))
    return len(ranked), detail


def project_organization(doc, ctx):
    names = []
    try:
        from Autodesk.Revit.DB import BrowserOrganization
        try:
            for bo in BrowserOrganization.GetAllBrowserOrganizationsForViews(doc):
                try:
                    names.append("{0} (Views)".format(bo.Name))
                except Exception:
                    continue
        except Exception:
            pass
        try:
            for bo in BrowserOrganization.GetAllBrowserOrganizationsForSheets(doc):
                try:
                    names.append("{0} (Sheets)".format(bo.Name))
                except Exception:
                    continue
        except Exception:
            pass
    except Exception:
        pass
    if not names:
        return None, "Could not read browser organization schemes"
    return len(names), "Browser organization scheme(s): {0}".format(", ".join(names))


def in_place_families(doc, ctx):
    instances = ctx.get("all_family_instances") if ctx else None
    if instances is None:
        instances = list(FilteredElementCollector(doc).OfClass(FamilyInstance).WhereElementIsNotElementType())
    count = 0
    for inst in instances:
        try:
            sym = inst.Symbol
            fam = sym.Family if sym is not None else None
            if fam is not None and fam.IsInPlace:
                count += 1
        except Exception:
            continue
    return count, "{0} in-place family instance(s)".format(count)


# -- External Files -----------------------------------------------------------
def _import_instances(doc):
    return list(FilteredElementCollector(doc).OfClass(ImportInstance))


def imported_skp(doc, ctx):
    count = 0
    for ii in _import_instances(doc):
        try:
            if ii.IsLinked:
                continue
            type_elem = doc.GetElement(ii.GetTypeId())
            name = (_read_name(type_elem) or "").lower()
            if name.endswith(".skp"):
                count += 1
        except Exception:
            continue
    return count, "{0} imported SKP file(s)".format(count)


def imported_cad(doc, ctx):
    count = 0
    for ii in _import_instances(doc):
        try:
            if ii.IsLinked:
                continue
            type_elem = doc.GetElement(ii.GetTypeId())
            name = (_read_name(type_elem) or "").lower()
            if name.endswith(".skp"):
                continue
            count += 1
        except Exception:
            continue
    return count, "{0} imported CAD file(s) (excluding SKP)".format(count)


def linked_cad(doc, ctx):
    count = 0
    for ii in _import_instances(doc):
        try:
            if ii.IsLinked:
                count += 1
        except Exception:
            continue
    return count, "{0} linked CAD file instance(s)".format(count)


def linked_cad_visible_all_views(doc, ctx):
    count = 0
    for ii in _import_instances(doc):
        try:
            if not ii.IsLinked:
                continue
            if ii.OwnerViewId == ElementId.InvalidElementId:
                count += 1
        except Exception:
            continue
    return count, "{0} linked CAD file(s) visible in all views (not Current View Only)".format(count)


def linked_revit_method(doc, ctx):
    details = []
    for lt in FilteredElementCollector(doc).OfClass(RevitLinkType):
        try:
            name = _read_name(lt) or "(unnamed)"
            attach = lt.AttachmentType
            details.append("{0}: {1}".format(name, attach))
        except Exception:
            continue
    return len(details), ("; ".join(details) if details else "No Revit links found")


def linked_revit_unpinned(doc, ctx):
    count = 0
    for li in FilteredElementCollector(doc).OfClass(RevitLinkInstance):
        try:
            if not li.Pinned:
                count += 1
        except Exception:
            continue
    return count, "{0} linked Revit file instance(s) not pinned in place".format(count)


def linked_cad_unpinned(doc, ctx):
    count = 0
    for ii in _import_instances(doc):
        try:
            if ii.IsLinked and not ii.Pinned:
                count += 1
        except Exception:
            continue
    return count, "{0} linked CAD file(s) not pinned in place".format(count)


def raster_images(doc, ctx):
    count = len(list(FilteredElementCollector(doc).OfClass(ImageInstance)))
    return count, "{0} raster image instance(s)".format(count)


# -- Project Settings (informational - 0% weight section) --------------------
def project_information(doc, ctx):
    try:
        info = doc.ProjectInformation
        params = list(info.Parameters)
    except Exception as e:
        return None, "Could not read Project Information: {0}".format(e)
    sample = []
    for p in params[:12]:
        try:
            name = p.Definition.Name
            val = p.AsValueString()
            if val is None:
                val = p.AsString() or ""
            sample.append("{0}={1}".format(name, val))
        except Exception:
            continue
    return len(params), "{0} Project Information parameter(s): {1}".format(len(params), "; ".join(sample))


def design_options(doc, ctx):
    try:
        options = list(FilteredElementCollector(doc).OfClass(DesignOption))
    except Exception as e:
        return None, "Could not read Design Options: {0}".format(e)
    elements = ctx.get("all_elements") if ctx else None
    if elements is None:
        elements = list(FilteredElementCollector(doc).WhereElementIsNotElementType())
    counts = {}
    for el in elements:
        try:
            do = el.DesignOption
            if do is not None:
                counts[do.Id] = counts.get(do.Id, 0) + 1
        except Exception:
            continue
    details = []
    for opt in options:
        try:
            name = _read_name(opt) or "(unnamed)"
            details.append("{0}: {1} element(s)".format(name, counts.get(opt.Id, 0)))
        except Exception:
            continue
    return len(options), ("; ".join(details) if details else "No Design Options defined")


def project_coordinates(doc, ctx):
    parts = []
    try:
        base_point = BasePoint.GetProjectBasePoint(doc)
        if base_point is not None:
            pos = base_point.Position
            parts.append("Project Base Point: ({0:.2f}, {1:.2f}, {2:.2f})".format(pos.X, pos.Y, pos.Z))
    except Exception:
        pass
    try:
        survey_point = BasePoint.GetSurveyPoint(doc)
        if survey_point is not None:
            pos = survey_point.Position
            parts.append("Survey Point: ({0:.2f}, {1:.2f}, {2:.2f})".format(pos.X, pos.Y, pos.Z))
    except Exception:
        pass
    if not parts:
        return None, "Could not read Project Base Point / Survey Point"
    return len(parts), "; ".join(parts)


def worksets_list(doc, ctx):
    try:
        worksets = list(FilteredWorksetCollector(doc).OfKind(WorksetKind.UserWorkset))
    except Exception as e:
        return None, "Worksharing not enabled or could not read worksets: {0}".format(e)
    names = []
    for w in worksets:
        try:
            names.append(w.Name)
        except Exception:
            continue
    return len(names), "{0} user workset(s): {1}".format(len(names), ", ".join(names))


def phase_elements(doc, ctx):
    try:
        phases = list(doc.Phases)
    except Exception as e:
        return None, "Could not read Phases: {0}".format(e)
    elements = ctx.get("all_elements") if ctx else None
    if elements is None:
        elements = list(FilteredElementCollector(doc).WhereElementIsNotElementType())
    counts = {}
    for el in elements:
        try:
            p = el.get_Parameter(BuiltInParameter.PHASE_CREATED)
            if p is not None:
                key = p.AsElementId()
                counts[key] = counts.get(key, 0) + 1
        except Exception:
            continue
    details = []
    for ph in phases:
        try:
            details.append("{0}: {1} element(s)".format(_read_name(ph) or "(unnamed)", counts.get(ph.Id, 0)))
        except Exception:
            continue
    return len(phases), ("; ".join(details) if details else "No phases found")


# -- Views ---------------------------------------------------------------------
def _real_views(doc):
    result = []
    for v in FilteredElementCollector(doc).OfClass(View):
        try:
            if v.IsTemplate or isinstance(v, ViewSheet):
                continue
            result.append(v)
        except Exception:
            continue
    return result


def _views_on_sheets(doc):
    on_sheet_ids = set()
    for vp in FilteredElementCollector(doc).OfClass(Viewport):
        try:
            on_sheet_ids.add(vp.ViewId)
        except Exception:
            continue
    for sheet in FilteredElementCollector(doc).OfClass(ViewSheet):
        try:
            for vid in sheet.GetAllPlacedViews():
                on_sheet_ids.add(vid)
        except Exception:
            continue
    return on_sheet_ids


def _ctx_real_views(doc, ctx):
    views = ctx.get("real_views") if ctx else None
    return views if views is not None else _real_views(doc)


def _ctx_views_on_sheets(doc, ctx):
    ids = ctx.get("views_on_sheet_ids") if ctx else None
    return ids if ids is not None else _views_on_sheets(doc)


def views_count(doc, ctx):
    views = _ctx_real_views(doc, ctx)
    return len(views), "{0} view(s) (excluding templates and sheets)".format(len(views))


def sheets_count(doc, ctx):
    count = len(list(FilteredElementCollector(doc).OfClass(ViewSheet)))
    return count, "{0} sheet(s)".format(count)


def views_hidden_elements(doc, ctx):
    count = 0
    checked = 0
    for v in _ctx_real_views(doc, ctx):
        try:
            checked += 1
            found_hidden = False
            for el in FilteredElementCollector(doc, v.Id).WhereElementIsNotElementType():
                try:
                    if el.IsHidden(v):
                        found_hidden = True
                        break
                except Exception:
                    continue
            if found_hidden:
                count += 1
        except Exception:
            continue
    return count, "{0} view(s) with at least one hidden element (out of {1} checked)".format(count, checked)


def views_not_on_sheets(doc, ctx):
    on_sheet_ids = _ctx_views_on_sheets(doc, ctx)
    views = _ctx_real_views(doc, ctx)
    count = 0
    for v in views:
        try:
            if not v.CanBePrinted:
                continue
            if v.Id not in on_sheet_ids:
                count += 1
        except Exception:
            continue
    return count, "{0} view(s) not placed on any sheet (out of {1} checked)".format(count, len(views))


def views_no_template(doc, ctx):
    on_sheet_ids = _ctx_views_on_sheets(doc, ctx)
    count = 0
    checked = 0
    for v in _ctx_real_views(doc, ctx):
        try:
            if v.Id not in on_sheet_ids:
                continue
            checked += 1
            if v.ViewTemplateId == ElementId.InvalidElementId:
                count += 1
        except Exception:
            continue
    return count, "{0} view(s) on sheets with no View Template assigned (out of {1} on sheets)".format(count, checked)


# -- Datum Elements ------------------------------------------------------------
def levels_count(doc, ctx):
    count = len(list(FilteredElementCollector(doc).OfClass(Level)))
    return count, "{0} level(s)".format(count)


def grids_count(doc, ctx):
    count = len(list(FilteredElementCollector(doc).OfClass(Grid)))
    return count, "{0} grid(s)".format(count)


def unplaced_rooms(doc, ctx):
    count = 0
    total = 0
    for r in FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_Rooms).WhereElementIsNotElementType():
        try:
            total += 1
            if r.Area <= 0 or r.Location is None:
                count += 1
        except Exception:
            continue
    return count, "{0} unplaced room(s) out of {1}".format(count, total)


def overlapping_rooms(doc, ctx):
    rooms = list(FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_Rooms).WhereElementIsNotElementType())
    overlap_count = 0
    for i, r1 in enumerate(rooms):
        try:
            loc1 = r1.Location
            if loc1 is None or not hasattr(loc1, "Point") or loc1.Point is None:
                continue
            pt1 = loc1.Point
        except Exception:
            continue
        for r2 in rooms[i + 1:]:
            try:
                if r2.IsPointInRoom(pt1):
                    overlap_count += 1
            except Exception:
                continue
    return overlap_count, (
        "{0} room(s) whose center point falls inside another room "
        "(APPROXIMATE - matches the rubric's own description, not a full "
        "boundary-overlap solve)".format(overlap_count))


def duplicate_room_numbers(doc, ctx):
    numbers = {}
    for r in FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_Rooms).WhereElementIsNotElementType():
        try:
            numbers.setdefault(r.Number, []).append(r.Id)
        except Exception:
            continue
    dups = dict((k, v) for k, v in numbers.items() if len(v) > 1)
    dup_count = sum(len(v) for v in dups.values())
    return dup_count, "{0} room(s) sharing a duplicate Number ({1} duplicate number(s))".format(
        dup_count, len(dups))


def unplaced_spaces(doc, ctx):
    count = 0
    total = 0
    for s in FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_MEPSpaces).WhereElementIsNotElementType():
        try:
            total += 1
            if s.Area <= 0 or s.Location is None:
                count += 1
        except Exception:
            continue
    return count, "{0} unplaced space(s) out of {1}".format(count, total)


def overlapping_spaces(doc, ctx):
    spaces = list(FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_MEPSpaces).WhereElementIsNotElementType())
    overlap_count = 0
    for i, s1 in enumerate(spaces):
        try:
            loc1 = s1.Location
            if loc1 is None or not hasattr(loc1, "Point") or loc1.Point is None:
                continue
            pt1 = loc1.Point
        except Exception:
            continue
        for s2 in spaces[i + 1:]:
            try:
                if s2.IsPointInSpace(pt1):
                    overlap_count += 1
            except Exception:
                continue
    return overlap_count, (
        "{0} space(s) whose center point falls inside another space "
        "(APPROXIMATE, same method as the Rooms check)".format(overlap_count))


def duplicate_space_numbers(doc, ctx):
    numbers = {}
    for s in FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_MEPSpaces).WhereElementIsNotElementType():
        try:
            numbers.setdefault(s.Number, []).append(s.Id)
        except Exception:
            continue
    dups = dict((k, v) for k, v in numbers.items() if len(v) > 1)
    dup_count = sum(len(v) for v in dups.values())
    return dup_count, "{0} space(s) sharing a duplicate Number".format(dup_count)


def areas_not_placed(doc, ctx):
    count = 0
    total = 0
    for a in FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_Areas).WhereElementIsNotElementType():
        try:
            total += 1
            if a.Area <= 0:
                count += 1
        except Exception:
            continue
    return count, "{0} unplaced area(s) out of {1}".format(count, total)


_HOST_DEPENDENT_CATEGORIES = ["OST_Windows", "OST_Doors"]


def unhosted_elements(doc, ctx):
    count = 0
    total = 0
    for cat_name in _HOST_DEPENDENT_CATEGORIES:
        bic = getattr(BuiltInCategory, cat_name, None)
        if bic is None:
            continue
        for inst in FilteredElementCollector(doc).OfCategory(bic).WhereElementIsNotElementType():
            try:
                total += 1
                if inst.Host is None:
                    count += 1
            except Exception:
                continue
    return count, "{0} Door/Window element(s) with no Host, out of {1} checked".format(count, total)


def mishosted_elements(doc, ctx):
    levels = (ctx.get("levels_sorted") if ctx else None)
    if levels is None:
        levels = sorted(FilteredElementCollector(doc).OfClass(Level), key=lambda l: l.Elevation)
    if not levels:
        return None, "No Levels found"
    level_by_id = dict((l.Id, l) for l in levels)
    elements = ctx.get("all_elements") if ctx else None
    if elements is None:
        elements = list(FilteredElementCollector(doc).WhereElementIsNotElementType())
    mismatch_count = 0
    checked = 0
    for el in elements:
        try:
            cat = el.Category
            if cat is None or cat.CategoryType != CategoryType.Model:
                continue
            current_level_id = el.LevelId
            current_level = level_by_id.get(current_level_id)
            if current_level is None:
                continue
            checked += 1
            offset_param = _find_offset_parameter(el)
            offset = offset_param.AsDouble() if offset_param is not None else 0.0
            absolute_elevation = current_level.Elevation + offset
            correct_level = levels[0]
            for lvl in levels:
                if lvl.Elevation <= absolute_elevation:
                    correct_level = lvl
                else:
                    break
            if correct_level.Id != current_level_id:
                mismatch_count += 1
        except Exception:
            continue
    return mismatch_count, (
        "{0} element(s) whose Level doesn't match their actual physical elevation, "
        "out of {1} checked".format(mismatch_count, checked))


def prepare_context(doc, base_ctx=None):
    """Pre-collects the handful of large, expensive collections that
    several checks each used to gather independently (a full-document
    element scan, all FamilyInstances, sorted Levels, real Views, and
    which Views are on sheets) - the main source of a slow overall run
    on a big project. Call this ONCE before running any checks and pass
    the result as `ctx` to every check_fn; each check falls back to
    collecting its own data if the relevant ctx key is missing, so
    checks remain independently callable/testable without this."""
    ctx = dict(base_ctx or {})
    all_elements = list(FilteredElementCollector(doc).WhereElementIsNotElementType())
    ctx["all_elements"] = all_elements
    ctx["all_family_instances"] = [el for el in all_elements if isinstance(el, FamilyInstance)]
    ctx["levels_sorted"] = sorted(FilteredElementCollector(doc).OfClass(Level), key=lambda l: l.Elevation)
    ctx["real_views"] = _real_views(doc)
    ctx["views_on_sheet_ids"] = _views_on_sheets(doc)
    return ctx


# -- dispatch table -----------------------------------------------------------
CHECKS = {
    "families_naming": families_naming,
    "worksets_naming": worksets_naming,
    "line_style_naming": line_style_naming,
    "warnings_count": warnings_count,
    "file_size": file_size,
    "purgeable_elements": purgeable_elements,
    "duplicate_elements": duplicate_elements,
    "total_elements": total_elements,
    "model_groups": model_groups,
    "detail_groups": detail_groups,
    "model_lines": model_lines,
    "generic_models": generic_models,
    "masses": masses,
    "largest_family": largest_family,
    "project_organization": project_organization,
    "in_place_families": in_place_families,
    "imported_skp": imported_skp,
    "imported_cad": imported_cad,
    "linked_cad": linked_cad,
    "linked_cad_visible_all_views": linked_cad_visible_all_views,
    "linked_revit_method": linked_revit_method,
    "linked_revit_unpinned": linked_revit_unpinned,
    "linked_cad_unpinned": linked_cad_unpinned,
    "raster_images": raster_images,
    "project_information": project_information,
    "design_options": design_options,
    "project_coordinates": project_coordinates,
    "worksets_list": worksets_list,
    "phase_elements": phase_elements,
    "views_count": views_count,
    "sheets_count": sheets_count,
    "views_hidden_elements": views_hidden_elements,
    "views_not_on_sheets": views_not_on_sheets,
    "views_no_template": views_no_template,
    "levels_count": levels_count,
    "grids_count": grids_count,
    "unplaced_rooms": unplaced_rooms,
    "overlapping_rooms": overlapping_rooms,
    "duplicate_room_numbers": duplicate_room_numbers,
    "unplaced_spaces": unplaced_spaces,
    "overlapping_spaces": overlapping_spaces,
    "duplicate_space_numbers": duplicate_space_numbers,
    "areas_not_placed": areas_not_placed,
    "unhosted_elements": unhosted_elements,
    "mishosted_elements": mishosted_elements,
}
