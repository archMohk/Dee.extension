# -*- coding: utf-8 -*-
"""
dee_sheet_links_service
DeeSheetLinks (Masterplan): grabs every RevitLinkInstance already placed
in the project (every "villa copy", however many duplicate instances
one link type has) and, for a user-defined list of generic "Sheet
Types" (a name + a Level + an optional View Template - "make it
generic", not hardcoded to Ground/First/Roof), creates one Sheet + one
Floor Plan View per (villa, sheet type) pair - the view's Crop Region
set to that villa's own footprint (+ a user offset), and the SAME
Building Typology/Parcel ID/Developer ID shared parameters DeeLinkDist
already wrote onto the link instance copied onto the created View too
(same parameter NAMES, extended to also cover the Views category via
dee_shared_param_service - see that module's docstring for why a
category is EXTENDED via ReInsert rather than re-bound).

--------------------------------------------------------------------
Revit API facts this module relies on, and how each was confirmed
--------------------------------------------------------------------
- Element.get_BoundingBox(None) on a RevitLinkInstance returns the
  bounding box ALREADY TRANSFORMED into the HOST document's coordinate
  system, and ALWAYS AXIS-ALIGNED to the host's own default axes (no
  rotation applied to the return value even for a rotated link instance)
  - confirmed via WebSearch against revitapidocs.com/Jeremy Tammik's own
  coordinate-transform notes. This is exactly what a rectangular
  "villa's footprint" crop needs: a plain world-space AABB, with no
  extra transform math required regardless of the villa's own rotation.
- ViewPlan.Create(doc, viewFamilyTypeId, levelId) - confirmed via
  WebSearch/Autodesk sample code; the FloorPlan ViewFamilyType is found
  via FilteredElementCollector(doc).OfClass(ViewFamilyType) filtered on
  .ViewFamily == ViewFamily.FloorPlan.
- ViewSheet.Create(doc, titleBlockTypeId) - confirmed via WebSearch;
  requires a real title block FamilySymbol ElementId, always user-picked
  here (never assumed/blank).
- Viewport.Create/CanAddViewToSheet/GetBoxOutline/SetBoxCenter - already
  proven LIVE in this codebase (DeeAssemb, DeeView); the auto-fit-to-
  standard-scale loop here is a simplified, single-viewport version of
  DeeAssemb's own proven _fit_and_place_viewport (one villa's crop per
  sheet, so the whole sheet outline IS the one cell - no grid needed).
- The rectangular-crop-from-world-points idiom (read the view's own
  CropBox to get its Transform, inverse-transform world points into the
  view's local frame, build a NEW BoundingBoxXYZ with the SAME Transform
  and the new Min/Max, reassign) is a LOCAL COPY of DeeViewAdjust's own
  proven-live apply_bbox_crop_from_world_points - mutating the read-back
  box's Min/Max in place does not reliably take effect, per that
  module's own docstring.
- Parameter creation/binding is dee_shared_param_service.
  ensure_shared_parameters(doc, app, names, OST_Views, "Views") - the
  SAME generic engine DeeLinkDist itself now goes through for
  OST_RvtLinks, extracted specifically because both tools need it.

NEEDS LIVE-REVIT VERIFICATION (per this codebase's own convention -
everything above is confirmed via documentation/proven sibling code):
this is the first tool here to create a ViewPlan+ViewSheet+Viewport
BATCH from scratch (DeeAssemb creates views via AssemblyViewUtils, a
different API surface) and the first to EXTEND an already-bound shared
parameter to a second category via ReInsert - see
dee_shared_param_service's own docstring for that specific flag.
"""
import os

from Autodesk.Revit.DB import (
    FilteredElementCollector, RevitLinkInstance, Level,
    View, ViewPlan, ViewFamilyType, ViewFamily, ViewSheet, ViewSchedule,
    Viewport, Transaction, BoundingBoxXYZ, XYZ, BuiltInCategory,
    UnitUtils, UnitTypeId, ModelPathUtils,
)

import dee_shared_param_service

# --------------------------------------------------------------------------
# units - a local copy of dee_link_dist_service.UNIT_OPTIONS/resolve_unit
# (same reasoning as the link-name copy below: this module stays fully
# decoupled from dee_link_dist_service's own, much larger, link-PLACEMENT
# import surface, which this tool has no need for at all).
# --------------------------------------------------------------------------
UNIT_OPTIONS = [
    ("Millimeters", UnitTypeId.Millimeters),
    ("Centimeters", UnitTypeId.Centimeters),
    ("Meters", UnitTypeId.Meters),
    ("Feet", UnitTypeId.Feet),
    ("Inches", UnitTypeId.Inches),
]
DEFAULT_UNIT_LABEL = "Meters"


def resolve_unit(label):
    for opt_label, unit_type_id in UNIT_OPTIONS:
        if opt_label == label:
            return unit_type_id
    return UnitTypeId.Meters


_DEFAULT_PARAM_NAMES = {
    "typology": "Building Typology",
    "parcel_id": "Parcel ID",
    "developer_id": "Developer ID",
}


def _safe_name(element):
    try:
        n = element.Name
        if n:
            return n
    except Exception:
        pass
    return "(unnamed)"


def _link_display_name(link_type):
    """Local copy of dee_link_dist_service.link_type_display_name - see
    that module's own docstring for why RevitLinkType.Name alone is not
    trustworthy (a live-reported bug on a real project, already fixed
    there). Duplicated rather than imported: this module only needs the
    pure name-resolution logic, not dee_link_dist_service's much larger
    link-CREATION/placement import surface."""
    try:
        ref = link_type.GetExternalFileReference()
        if ref is not None:
            model_path = ref.GetPath()
            if model_path is not None:
                visible_path = ModelPathUtils.ConvertModelPathToUserVisiblePath(model_path)
                if visible_path:
                    return os.path.basename(visible_path)
    except Exception:
        pass
    try:
        if link_type.Name:
            return link_type.Name
    except Exception:
        pass
    return "(unnamed link)"


# ==========================================================================
# Scanning - link instances
# ==========================================================================
class LinkInstanceRow(object):
    """One placed RevitLinkInstance - one 'villa copy'. Holds the
    ElementId only, never the live element across the window's
    lifetime - this tool opens a window and waits on the user between
    scanning and building, exactly the situation this codebase's own
    never-cache-Revit-elements rule exists for."""

    def __init__(self, instance, link_name, typology, parcel_id, developer_id):
        self.instance_id = instance.Id
        self.selected = True
        self.link_name = link_name
        self.typology = typology or ""
        self.parcel_id = parcel_id or ""
        self.developer_id = developer_id or ""

    @property
    def has_typology(self):
        return bool(self.typology.strip())


def _read_param(element, name):
    if not name:
        return ""
    try:
        p = element.LookupParameter(name)
        if p is None:
            return ""
        val = p.AsString()
        return val or ""
    except Exception:
        return ""


def list_link_instances(doc, param_names=None):
    """Every RevitLinkInstance placed in the document - literally "all
    the links", per DeeSheetLinks' own opening ask - each with its
    Typology/Parcel ID/Developer ID read via whatever parameter names
    are CURRENTLY configured (this tool has no reliable way to know what
    names a given project used when the links were placed, so it reads
    live, every scan, from param_names - defaulting to the same three
    names DeeLinkDist itself defaults to)."""
    names = dict(_DEFAULT_PARAM_NAMES)
    if param_names:
        names.update(dict((k, v) for k, v in param_names.items() if v))

    rows = []
    try:
        instances = list(FilteredElementCollector(doc).OfClass(RevitLinkInstance))
    except Exception:
        instances = []
    for inst in instances:
        try:
            link_type = doc.GetElement(inst.GetTypeId())
            link_name = _link_display_name(link_type) if link_type is not None else "(unknown link)"
        except Exception:
            link_name = "(unknown link)"
        rows.append(LinkInstanceRow(
            inst, link_name,
            _read_param(inst, names["typology"]),
            _read_param(inst, names["parcel_id"]),
            _read_param(inst, names["developer_id"])))
    rows.sort(key=lambda r: (r.link_name.lower(), r.typology.lower()))
    return rows


# ==========================================================================
# Scanning - levels / view templates / title blocks (what a Sheet Type
# is built from)
# ==========================================================================
def list_levels(doc):
    """[(ElementId, name)] every Level, sorted by elevation - the order
    a person would expect (Ground below First below Roof)."""
    try:
        levels = list(FilteredElementCollector(doc).OfClass(Level))
    except Exception:
        levels = []
    levels.sort(key=lambda l: getattr(l, "Elevation", 0.0))
    return [(l.Id, _safe_name(l)) for l in levels]


def list_view_templates(doc):
    """[(ElementId, name)] every view template that can apply to a Floor
    Plan - i.e. every template that is NOT a schedule template (Revit
    rejects a schedule template on a model view outright). Same split
    dee_assembly_service.collect_view_templates already uses, duplicated
    here rather than imported for the same import-surface-isolation
    reason as _link_display_name above."""
    out = []
    for v in FilteredElementCollector(doc).OfClass(View):
        try:
            if not v.IsTemplate or isinstance(v, ViewSchedule):
                continue
        except Exception:
            continue
        out.append((v.Id, _safe_name(v)))
    out.sort(key=lambda t: t[1].lower())
    return out


def list_title_blocks(doc):
    """[(ElementId, label)] every Title Block TYPE in the project."""
    out = []
    for sym in (FilteredElementCollector(doc)
                .OfCategory(BuiltInCategory.OST_TitleBlocks)
                .WhereElementIsElementType()):
        try:
            fam = ""
            try:
                fam = sym.Family.Name
            except Exception:
                pass
            label = "{0} : {1}".format(fam, _safe_name(sym)) if fam else _safe_name(sym)
            out.append((sym.Id, label))
        except Exception:
            continue
    out.sort(key=lambda t: t[1].lower())
    return out


def existing_sheet_numbers(doc):
    """Every Sheet Number already in the project - a freshly-generated
    number must avoid these too, not just avoid colliding WITHIN the new
    batch."""
    numbers = set()
    for sheet in FilteredElementCollector(doc).OfClass(ViewSheet):
        try:
            if getattr(sheet, "IsPlaceholder", False):
                continue
            if sheet.SheetNumber:
                numbers.add(sheet.SheetNumber)
        except Exception:
            continue
    return numbers


def find_floor_plan_view_family_type(doc):
    """The ElementId of the project's FloorPlan ViewFamilyType - every
    project has at least one (Revit ships a default) - or None if
    somehow missing, checked and reported plainly rather than assumed."""
    try:
        for vft in FilteredElementCollector(doc).OfClass(ViewFamilyType):
            try:
                if vft.ViewFamily == ViewFamily.FloorPlan:
                    return vft.Id
            except Exception:
                continue
    except Exception:
        pass
    return None


# ==========================================================================
# Sheet Types - the generic, user-managed list ("Number of Sheets" is
# just len(sheet_types) - open-ended, not a hardcoded spinner, per the
# explicit "make it generic" request).
# ==========================================================================
class SheetType(object):
    def __init__(self, name="", level_id=None, level_name="",
                 view_template_id=None, view_template_name="(None)"):
        self.name = name
        self.level_id = level_id
        self.level_name = level_name
        self.view_template_id = view_template_id
        self.view_template_name = view_template_name


# ==========================================================================
# Naming - token engine, same {KIND}/{KIND:ARG}/|MODIFIER syntax this
# codebase's own DeeSheet Renamer already established as its "main
# Naming System" (dee_sheet_renamer_service.py) - the generic string
# utilities below (_TOKEN_RE/_apply_modifier(s)/_clean_modifier/_to_alpha/
# _split_pick/render_template) are a LOCAL COPY of that module's own
# (Revit-free, pure-string) engine, verbatim, so PAD/UPPER/FIRST/etc all
# work identically and feel familiar - only _resolve_token's KIND list
# differs, since this tool's tokens describe a (villa, sheet type) pair,
# not a Revit Sheet.
# ==========================================================================
import re

_TOKEN_RE = re.compile(r"\{([^{}]+)\}")

DEFAULT_NUMBER_TEMPLATE = "{Serial|PAD4}"
DEFAULT_NAME_TEMPLATE = "{Typology} - {SheetType}"

_INVALID_NAME_CHARS = set("\\:{}[]|;<>?`~")


def _has_invalid_chars(text):
    return any(c in _INVALID_NAME_CHARS for c in (text or ""))


def _to_alpha(n):
    n = max(1, int(n))
    letters = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _split_pick(value, delim, index):
    try:
        bits = value.split(delim)
        if index > 0:
            return bits[index - 1] if index <= len(bits) else value
        if index < 0:
            return bits[index] if abs(index) <= len(bits) else value
        return value
    except Exception:
        return value


def _apply_modifier(value, mod):
    if not mod:
        return value
    upper_mod = mod.upper()
    try:
        if upper_mod == "UPPER":
            return value.upper()
        if upper_mod == "LOWER":
            return value.lower()
        if upper_mod == "TITLE":
            return value.title()
        if upper_mod == "TRIM":
            return value.strip()
        if upper_mod.startswith("PAD"):
            n = int(upper_mod[3:])
            return value.rjust(n, "0")
        if upper_mod.startswith("FIRST"):
            n = int(upper_mod[5:])
            return value[:n]
        if upper_mod.startswith("LAST"):
            n = int(upper_mod[4:])
            return value[-n:] if n > 0 else value
        if upper_mod.startswith("REPLACE:"):
            _, old, new = mod.split(":", 2)
            return value.replace(old, new)
        if upper_mod.startswith("SPLIT:"):
            _, delim, idx = mod.split(":", 2)
            return _split_pick(value, delim, int(idx))
        if upper_mod.startswith("DEFAULT:"):
            fallback = mod.split(":", 1)[1]
            return value if value.strip() != "" else fallback
    except Exception:
        return value
    return value


def _apply_modifiers(value, mods):
    for m in mods:
        value = _apply_modifier(value, m)
    return value


def _clean_modifier(mod):
    mod = mod.lstrip()
    if ":" not in mod:
        mod = mod.rstrip()
    return mod


def _resolve_token(kind, arg, mods, ctx):
    k = (kind or "").strip().upper()
    if k == "TYPOLOGY":
        value = ctx["typology"]
    elif k in ("PARCELID", "PARCEL"):
        value = ctx["parcel_id"]
    elif k in ("DEVELOPERID", "DEVELOPER"):
        value = ctx["developer_id"]
    elif k == "LINKNAME":
        value = ctx["link_name"]
    elif k == "SHEETTYPE":
        value = ctx["sheet_type"]
    elif k == "VILLAINDEX":
        value = str(ctx["villa_index"])
    elif k == "SERIAL":
        value = str(ctx["serial_value"])
    elif k == "ALPHA":
        value = _to_alpha(ctx["serial_value"])
    else:
        return "{" + kind + (":" + arg if arg else "") + "}"
    return _apply_modifiers(value, mods)


def render_template(template, ctx):
    if not template:
        return ""

    def _repl(m):
        body = m.group(1)
        parts = body.split("|")
        head = parts[0].strip()
        mods = [_clean_modifier(p) for p in parts[1:]]
        if ":" in head:
            kind, arg = head.split(":", 1)
        else:
            kind, arg = head, None
        return _resolve_token(kind, arg, mods, ctx)

    try:
        return _TOKEN_RE.sub(_repl, template)
    except Exception:
        return template


# ==========================================================================
# Plan - the (villa x sheet type) cartesian product, rendered and
# validated BEFORE any Revit call - "report before action, always".
# ==========================================================================
STATUS_READY = "Ready"
STATUS_EMPTY = "Empty"
STATUS_DUPLICATE = "Duplicate"
STATUS_INVALID = "Invalid"


class SheetPlanRow(object):
    def __init__(self, link_row, sheet_type, villa_index, serial_value):
        self.link_row = link_row
        self.sheet_type = sheet_type
        self.villa_index = villa_index
        self.serial_value = serial_value
        self.sheet_number = ""
        self.sheet_name = ""
        self.status = ""

    @property
    def link_name(self):
        return self.link_row.link_name

    @property
    def typology(self):
        return self.link_row.typology

    @property
    def sheet_type_name(self):
        return self.sheet_type.name


def build_plan(link_rows, sheet_types, number_template, name_template,
               existing_numbers=None):
    """Cartesian product of link_rows x sheet_types, VILLA-MAJOR (every
    sheet type for one villa is contiguous - how a person expects a
    printed set to read), with Sheet Number/Name rendered from the token
    templates and duplicate/blank/invalid Sheet Numbers flagged before
    anything is created. existing_numbers: Sheet Numbers already used
    elsewhere in the project - a generated number colliding with one of
    those is ALSO a Duplicate, not just a within-batch collision."""
    existing_numbers = set(existing_numbers or [])
    plan = []
    serial = 1
    for villa_index, link_row in enumerate(link_rows, start=1):
        for sheet_type in sheet_types:
            ctx = {
                "typology": link_row.typology, "parcel_id": link_row.parcel_id,
                "developer_id": link_row.developer_id, "link_name": link_row.link_name,
                "sheet_type": sheet_type.name, "villa_index": villa_index,
                "serial_value": serial,
            }
            row = SheetPlanRow(link_row, sheet_type, villa_index, serial)
            row.sheet_number = render_template(number_template, ctx)
            row.sheet_name = render_template(name_template, ctx)
            plan.append(row)
            serial += 1
    _compute_statuses(plan, existing_numbers)
    return plan


def _compute_statuses(plan, existing_numbers):
    seen = {}
    for row in plan:
        seen.setdefault(row.sheet_number, []).append(row)
    for row in plan:
        if not row.sheet_number.strip() or not row.sheet_name.strip():
            row.status = STATUS_EMPTY
        elif _has_invalid_chars(row.sheet_number) or _has_invalid_chars(row.sheet_name):
            row.status = STATUS_INVALID
        elif len(seen.get(row.sheet_number, [])) > 1 or row.sheet_number in existing_numbers:
            row.status = STATUS_DUPLICATE
        else:
            row.status = STATUS_READY


# ==========================================================================
# Crop - local copy of DeeViewAdjust's proven
# apply_bbox_crop_from_world_points idiom, generalised from "an existing
# view's crop" to "a brand new view's crop from a villa's world bbox".
# ==========================================================================
def _bbox_via_linked_elements(link_doc, transform):
    """Fallback tier 3 - unions every model element's OWN bounding box
    (in the LINKED document's local coordinates), then transforms the 8
    corners of that union into host coordinates via the instance's own
    GetTotalTransform(). Heavier than the other two tiers (walks the
    whole linked model), so it is only reached - and CACHED per villa,
    see create_sheets_and_views' bbox_cache - when both cheaper tiers
    give up."""
    min_pt = max_pt = None
    try:
        elements = FilteredElementCollector(link_doc).WhereElementIsNotElementType()
    except Exception:
        return None
    for el in elements:
        try:
            b = el.get_BoundingBox(None)
        except Exception:
            b = None
        if b is None:
            continue
        if min_pt is None:
            min_pt, max_pt = b.Min, b.Max
        else:
            min_pt = XYZ(min(min_pt.X, b.Min.X), min(min_pt.Y, b.Min.Y), min(min_pt.Z, b.Min.Z))
            max_pt = XYZ(max(max_pt.X, b.Max.X), max(max_pt.Y, b.Max.Y), max(max_pt.Z, b.Max.Z))
    if min_pt is None:
        return None
    corners = [XYZ(x, y, z)
              for x in (min_pt.X, max_pt.X)
              for y in (min_pt.Y, max_pt.Y)
              for z in (min_pt.Z, max_pt.Z)]
    world_corners = [transform.OfPoint(c) for c in corners]
    xs = [p.X for p in world_corners]
    ys = [p.Y for p in world_corners]
    zs = [p.Z for p in world_corners]
    return XYZ(min(xs), min(ys), min(zs)), XYZ(max(xs), max(ys), max(zs))


def resolve_villa_bbox(instance, active_view=None):
    """(bbox_or_None, detail) - three tiers, cheapest first, since a
    live-observed run showed tier 1 alone returning None for EVERY
    villa link instance (Revit's own get_BoundingBox(None) is
    documented to return None when "the model box is not known" for
    certain element types, and this was confirmed live for
    RevitLinkInstance specifically, not just a hypothetical):

    1. get_BoundingBox(None) - cheapest, works for most elements.
    2. get_BoundingBox(active_view) - the commonly-documented working
       alternative when a real view is available (the view active when
       DeeSheetLinks was opened, threaded in by the caller).
    3. Union every element in the LINKED document's own bounding box,
       transformed into host coordinates via GetTotalTransform() - the
       most expensive but most reliable tier, independent of which view
       happens to be active. Only reached if the link is genuinely
       loaded (GetLinkDocument() returns a real Document) - an unloaded
       link is reported plainly rather than silently producing a wrong
       (all-zero) crop."""
    try:
        bbox = instance.get_BoundingBox(None)
        if bbox is not None:
            return (bbox.Min, bbox.Max), ""
    except Exception:
        pass

    if active_view is not None:
        try:
            bbox = instance.get_BoundingBox(active_view)
            if bbox is not None:
                return (bbox.Min, bbox.Max), ""
        except Exception:
            pass

    try:
        link_doc = instance.GetLinkDocument()
    except Exception:
        link_doc = None
    if link_doc is None:
        return None, "the link is not loaded - load/reload it first"
    try:
        transform = instance.GetTotalTransform()
    except Exception:
        return None, "could not read the link's placement transform"
    result = _bbox_via_linked_elements(link_doc, transform)
    if result is None:
        return None, "the linked model has no elements with computable geometry"
    return result, ""


def _expanded_world_points(min_pt, max_pt, offset_internal):
    """8 corners of the villa's bbox expanded outward by offset_internal
    on X/Y only - Z is left alone, since the crop is a plan footprint,
    not a 3D box."""
    x0, x1 = min_pt.X - offset_internal, max_pt.X + offset_internal
    y0, y1 = min_pt.Y - offset_internal, max_pt.Y + offset_internal
    z0, z1 = min_pt.Z, max_pt.Z
    return [XYZ(x0, y0, z0), XYZ(x1, y0, z0), XYZ(x1, y1, z0), XYZ(x0, y1, z0),
            XYZ(x0, y0, z1), XYZ(x1, y0, z1), XYZ(x1, y1, z1), XYZ(x0, y1, z1)]


def apply_villa_crop(view, min_pt, max_pt, offset_internal):
    """Sets view.CropBox to an absolute rectangle covering the villa's
    (expanded) bbox - local copy of DeeViewAdjust's own
    apply_bbox_crop_from_world_points: read the view's OWN CropBox first
    to get its Transform, inverse-transform the world points into the
    view's local frame, rebuild a NEW BoundingBoxXYZ with the SAME
    Transform and the new Min/Max, then reassign - mutating the read-
    back box's Min/Max in place does not reliably take effect."""
    world_points = _expanded_world_points(min_pt, max_pt, offset_internal)
    try:
        if not view.CropBoxActive:
            view.CropBoxActive = True
    except Exception:
        pass
    try:
        bbox = view.CropBox
        if bbox is None:
            return False, "New view has no crop box"
        transform = bbox.Transform
        inv = transform.Inverse
        local_pts = [inv.OfPoint(p) for p in world_points]
        xs = [p.X for p in local_pts]
        ys = [p.Y for p in local_pts]
        new_box = BoundingBoxXYZ()
        new_box.Transform = transform
        new_box.Min = XYZ(min(xs), min(ys), bbox.Min.Z)
        new_box.Max = XYZ(max(xs), max(ys), bbox.Max.Z)
        view.CropBox = new_box
        return True, "Crop set to the villa's footprint"
    except Exception as e:
        return False, str(e)


# ==========================================================================
# Sheet-fit placement - a simplified, single-viewport version of
# DeeAssemb's own proven _fit_and_place_viewport (that one manages a
# multi-cell grid; here the whole sheet outline IS the one cell, since
# every sheet DeeSheetLinks makes carries exactly one view).
# ==========================================================================
STANDARD_SCALES = [1, 2, 5, 10, 20, 25, 50, 75, 100, 125, 150, 200, 250,
                   500, 1000, 2000, 5000]


def next_standard_scale(value):
    if value is None or value <= 0:
        return STANDARD_SCALES[0]
    for s in STANDARD_SCALES:
        if s >= value - 1e-9:
            return s
    return STANDARD_SCALES[-1]


def _fit_and_center_viewport(doc, sheet, view, probe_scale=100):
    outline = sheet.Outline
    cell = (outline.Min.U, outline.Max.U, outline.Min.V, outline.Max.V)
    cx = (cell[0] + cell[1]) / 2.0
    cy = (cell[2] + cell[3]) / 2.0
    cell_w = cell[1] - cell[0]
    cell_h = cell[3] - cell[2]

    try:
        view.Scale = probe_scale
    except Exception:
        pass

    if not Viewport.CanAddViewToSheet(doc, sheet.Id, view.Id):
        return None

    vp = Viewport.Create(doc, sheet.Id, view.Id, XYZ(cx, cy, 0))
    doc.Regenerate()

    def measured():
        o = vp.GetBoxOutline()
        return (o.MaximumPoint.X - o.MinimumPoint.X, o.MaximumPoint.Y - o.MinimumPoint.Y)

    w, h = measured()
    if w > 1e-9 and h > 1e-9 and cell_w > 0 and cell_h > 0:
        try:
            current = int(view.Scale)
        except Exception:
            current = probe_scale
        ratio = max(w / cell_w, h / cell_h)
        target = next_standard_scale(current * ratio)
        if target != current:
            try:
                view.Scale = target
                doc.Regenerate()
                w, h = measured()
            except Exception:
                pass
        guard = 0
        while (w > cell_w + 1e-9 or h > cell_h + 1e-9) and guard < 4:
            try:
                idx = STANDARD_SCALES.index(int(view.Scale))
            except Exception:
                break
            if idx >= len(STANDARD_SCALES) - 1:
                break
            try:
                view.Scale = STANDARD_SCALES[idx + 1]
                doc.Regenerate()
                w, h = measured()
            except Exception:
                break
            guard += 1

    vp.SetBoxCenter(XYZ(cx, cy, 0))
    return vp


# ==========================================================================
# Build - creates the Sheets + Views for every Ready row of the plan.
# ==========================================================================
class SheetBuildRowResult(object):
    def __init__(self, plan_row, ok, message):
        self.plan_row = plan_row
        self.ok = ok
        self.message = message

    @property
    def villa_index(self):
        return self.plan_row.villa_index

    @property
    def sheet_type_name(self):
        return self.plan_row.sheet_type_name

    @property
    def sheet_number(self):
        return self.plan_row.sheet_number

    @property
    def sheet_name(self):
        return self.plan_row.sheet_name


class BuildResult(object):
    def __init__(self):
        self.applied = 0
        self.skipped = 0
        self.row_results = []
        self.errors = []
        self.parameter_setup = {}


def _write_view_parameters(view, param_names, link_row):
    values = {}
    for key, value in (("typology", link_row.typology), ("parcel_id", link_row.parcel_id),
                       ("developer_id", link_row.developer_id)):
        name = param_names.get(key)
        if name:
            values[name] = value
    written = 0
    for name, value in values.items():
        try:
            p = view.LookupParameter(name)
            if p is not None and not p.IsReadOnly:
                p.Set(value or "")
                written += 1
        except Exception:
            pass
    if values and written == len(values):
        return ""
    return " ({0}/{1} parameters written)".format(written, len(values))


def create_sheets_and_views(doc, plan_rows, floorplan_vft_id, titleblock_id,
                            offset_display=0.0, unit_label=DEFAULT_UNIT_LABEL,
                            param_names=None, show_crop_boundary=False,
                            active_view=None, progress_cb=None):
    """plan_rows: only rows with status Ready are actually built - a
    caller handing in non-Ready rows is a programming error, not
    silently tolerated here (the window itself filters before calling).
    offset_display/unit_label: the crop offset in WHATEVER unit the
    window's own picker is set to (same UNIT_OPTIONS/resolve_unit
    convention as dee_link_dist_service.place_links) - converted to
    Revit's internal feet here, not by the caller, so this module stays
    the one place that knows how a raw number becomes Revit geometry.
    active_view: the view that was active when DeeSheetLinks was opened
    - fed into resolve_villa_bbox's tier 2 fallback.
    Creates/extends the 3 shared parameters onto the Views category
    FIRST, in its own Transaction (see dee_shared_param_service), then
    ONE Transaction for the whole batch - every row wrapped in its own
    try/except so one failure never aborts the rest, matching
    place_links' own convention."""
    result = BuildResult()
    offset_internal = UnitUtils.ConvertToInternalUnits(offset_display or 0.0, resolve_unit(unit_label))
    ready_rows = [r for r in plan_rows if r.status == STATUS_READY]
    if not ready_rows:
        result.errors.append("Nothing to create - no Ready rows in the plan.")
        return result
    if floorplan_vft_id is None:
        result.errors.append("This project has no Floor Plan view type - cannot create views.")
        return result
    if titleblock_id is None:
        result.errors.append("No Title Block selected.")
        return result

    names = dict(_DEFAULT_PARAM_NAMES)
    if param_names:
        names.update(dict((k, v) for k, v in param_names.items() if v))

    result.parameter_setup = dee_shared_param_service.ensure_shared_parameters(
        doc, doc.Application,
        [names["typology"], names["parcel_id"], names["developer_id"]],
        BuiltInCategory.OST_Views, "Views",
        transaction_name="DeeSheetLinks - Create/Extend View Parameters")

    bbox_cache = {}

    t = Transaction(doc, "DeeSheetLinks - Create Sheets and Views")
    try:
        t.Start()
        total = len(ready_rows)
        for i, row in enumerate(ready_rows):
            if progress_cb is not None and progress_cb(i, total, row):
                break
            try:
                instance = doc.GetElement(row.link_row.instance_id)
                if instance is None:
                    raise Exception("the villa link instance no longer exists")
                cache_key = row.link_row.instance_id
                if cache_key in bbox_cache:
                    bbox, bbox_detail = bbox_cache[cache_key]
                else:
                    bbox, bbox_detail = resolve_villa_bbox(instance, active_view)
                    bbox_cache[cache_key] = (bbox, bbox_detail)
                if bbox is None:
                    raise Exception("could not read the villa's bounding box" +
                                    (" - " + bbox_detail if bbox_detail else ""))

                view = ViewPlan.Create(doc, floorplan_vft_id, row.sheet_type.level_id)
                doc.Regenerate()

                ok, detail = apply_villa_crop(view, bbox[0], bbox[1], offset_internal)
                if not ok:
                    result.row_results.append(SheetBuildRowResult(row, False, detail))
                    result.skipped += 1
                    continue
                try:
                    view.CropBoxVisible = bool(show_crop_boundary)
                except Exception:
                    pass

                if row.sheet_type.view_template_id is not None:
                    try:
                        view.ViewTemplateId = row.sheet_type.view_template_id
                    except Exception:
                        pass

                param_note = _write_view_parameters(view, names, row.link_row)

                sheet = ViewSheet.Create(doc, titleblock_id)
                try:
                    sheet.SheetNumber = row.sheet_number
                except Exception as e:
                    result.row_results.append(SheetBuildRowResult(
                        row, False, "sheet number '{0}' rejected: {1}".format(row.sheet_number, e)))
                    result.skipped += 1
                    continue
                try:
                    sheet.Name = row.sheet_name
                except Exception:
                    pass
                doc.Regenerate()

                vp = _fit_and_center_viewport(doc, sheet, view)
                if vp is None:
                    result.row_results.append(SheetBuildRowResult(
                        row, True, "Created" + param_note +
                        " - view could not be placed on the sheet"))
                else:
                    result.row_results.append(SheetBuildRowResult(row, True, "Created" + param_note))
                result.applied += 1
            except Exception as e:
                result.row_results.append(SheetBuildRowResult(row, False, "{0}".format(e)))
                result.skipped += 1
        t.Commit()
    except Exception as e:
        try:
            if t.HasStarted() and not t.HasEnded():
                t.RollBack()
        except Exception:
            pass
        result.errors.append("Batch failed: {0}".format(e))
        return result

    return result
