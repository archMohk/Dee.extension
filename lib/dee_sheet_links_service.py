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
- Element.get_BoundingBox(None/view) on the WHOLE RevitLinkInstance is
  documented to return the aggregate bounding box already transformed
  into host coordinates - but this is NOT what resolve_villa_bbox uses
  as its primary route any more, after a live run showed two separate
  problems with it: (a) get_BoundingBox(None) alone returned null for
  every villa (a documented "model box not known" case for this element
  type - see feedback_link_instance_bounding_box memory), and (b) even
  get_BoundingBox(active_view) succeeding folded in the site's Levels/
  Grids/Reference Planes, producing a crop so oversized the actual
  villa geometry was reduced to an invisible speck on the sheet. The
  PRIMARY route now walks the linked document's own elements directly
  (GetLinkDocument() + GetTotalTransform(), filtered to
  Category.HasMaterialQuantities - see _model_element_world_bbox) and
  only falls back to the whole-instance calls (flagged "approximate")
  when the link cannot be traversed at all.
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
- The crop is now a ROTATED rectangle, aligned to the villa's own
  placement rotation, not a larger world-axis-aligned box around a
  rotated building - a live run showed exactly that: a diagonal villa
  sitting inside a much-too-big straight crop ("not adjusted to the
  longest part"). Directly rotating BoundingBoxXYZ.Transform does NOT
  visibly rotate a plan view's rectangular crop - confirmed via
  WebSearch against Jeremy Tammik's own account of testing this exact
  approach and finding it silently does nothing ("I do get the
  transformation, but I am not able to apply it... Something fails and
  the transformation remains unchanged"). Instead this reuses
  DeeViewAdjust's own PROVEN-LIVE mechanism -
  GetCropRegionShapeManager().SetCropShape(CurveLoop) - building a
  simple 4-corner rectangle whose corners are placed directly via the
  villa's own GetTotalTransform() (so the rotation comes for free from
  the corner positions, no separate "rotate the crop element" step
  needed at all). See apply_villa_crop.
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
    Viewport, Transaction, XYZ, Line, CurveLoop, BuiltInCategory,
    UnitUtils, UnitTypeId, ModelPathUtils,
)

import dee_shared_param_service
import deew_settings

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
                 view_template_id=None, view_template_name="(None)",
                 include_view=True, fixed_scale=None):
        self.name = name
        self.level_id = level_id
        self.level_name = level_name
        self.view_template_id = view_template_id
        self.view_template_name = view_template_name
        # include_view=False: this Sheet Type creates a plain Sheet only
        # - no cropped view, no crop math, no view parameters - for
        # cover sheets, schedule-only sheets, or anything meant to be
        # filled in by hand later ("not all sheets had to contain
        # views", explicit request).
        self.include_view = include_view
        # fixed_scale=None means auto-fit (the original behaviour,
        # picking the smallest standard scale the villa's crop fits
        # at); an int pins the view to exactly that scale denominator
        # instead - still measured and reported if it overflows the
        # sheet, never silently overridden.
        self.fixed_scale = fixed_scale


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
# Naming Sets - saved (Sheet Number template, Sheet Name template) pairs,
# named by the user, so a naming scheme built once does not need
# retyping every run. Reuses deew_settings.py's generic per-tool JSON
# store - the SAME persistence layer DeeSheet Renamer's own presets
# already use, rather than a second, parallel implementation.
# ==========================================================================
_NAMING_PRESET_TOOL_NAME = "dee_sheet_links_naming_presets"


def list_naming_presets():
    data = deew_settings.load(_NAMING_PRESET_TOOL_NAME, {})
    return sorted(data.keys())


def load_naming_preset(name):
    """{"number_template": ..., "name_template": ...} or None."""
    data = deew_settings.load(_NAMING_PRESET_TOOL_NAME, {})
    return data.get(name)


def save_naming_preset(name, number_template, name_template):
    data = deew_settings.load(_NAMING_PRESET_TOOL_NAME, {})
    data[name] = {"number_template": number_template, "name_template": name_template}
    return deew_settings.save(_NAMING_PRESET_TOOL_NAME, data)


def delete_naming_preset(name):
    data = deew_settings.load(_NAMING_PRESET_TOOL_NAME, {})
    if name in data:
        del data[name]
        return deew_settings.save(_NAMING_PRESET_TOOL_NAME, data)
    return True


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


def revalidate_plan(plan_rows, existing_numbers=None):
    """Re-runs the same duplicate/blank/invalid checks build_plan uses,
    against whatever Sheet Number/Name each row CURRENTLY holds - since
    the window lets the user hand-edit those values directly in the
    preview grid after Preview, a row's status can go stale the moment
    it is edited. Called right before Create filters to Ready rows, so
    a manually-typed duplicate is caught the same way a template-
    generated one always was, never silently created."""
    _compute_statuses(plan_rows, set(existing_numbers or []))
    return plan_rows


# ==========================================================================
# Crop - a ROTATED rectangle aligned to the villa's OWN placement
# rotation via DeeViewAdjust's proven-live SetCropShape mechanism (see
# module docstring for why directly rotating BoundingBoxXYZ.Transform
# does NOT work).
# ==========================================================================
def _identity_transform():
    """Transform.Identity - imported locally (not at module top level)
    so this module's own import surface stays exactly what the rest of
    it already declares; Transform is only ever needed for this one
    fallback case (no rotation info available)."""
    from Autodesk.Revit.DB import Transform
    return Transform.Identity


def _is_physical_model_element(el):
    """True for a real, buildable piece of the villa - walls, floors,
    roofs, doors, windows, columns, generic models, MEP equipment/
    fixtures, and so on. Category.HasMaterialQuantities is the
    documented Revit API property meant for exactly this distinction
    (it is true only for categories with compound structure or
    assignable materials - walls/roofs/floors/ceilings/stairs/3D
    families) and, critically, is FALSE for every datum category
    (Levels, Grids, Reference Planes, Scope Boxes) - those are not
    "buildable" and carry no material. Filtering on it is what keeps a
    site's Levels/Grids (which routinely span far beyond the actual
    building footprint) out of the crop."""
    try:
        cat = el.Category
        return cat is not None and bool(cat.HasMaterialQuantities)
    except Exception:
        return False


def _model_element_local_bbox(link_doc):
    """Unions the bounding box of every PHYSICAL model element in the
    linked document (see _is_physical_model_element), in the LINK's OWN
    local coordinate frame - deliberately NOT transformed into host
    coordinates here (unlike an earlier version of this function): a
    box measured along the BUILDING's own axes stays minimal-area even
    when the villa is placed at an angle, which is exactly what makes a
    ROTATED crop possible - resolve_villa_bbox pairs this local box with
    the villa's own GetTotalTransform() so the caller can place it
    (still rotated) directly in the model."""
    min_pt = max_pt = None
    try:
        elements = FilteredElementCollector(link_doc).WhereElementIsNotElementType()
    except Exception:
        return None
    for el in elements:
        if not _is_physical_model_element(el):
            continue
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
    return min_pt, max_pt


def resolve_villa_bbox(instance, active_view=None):
    """((local_min, local_max, transform), detail).

    local_min/local_max are the villa's TIGHT bounding box in its OWN
    local coordinate frame (before rotation/translation) - measured
    along the BUILDING's own axes, so it stays minimal-area even for a
    rotated villa. transform carries that local frame into host/world
    coordinates: instance.GetTotalTransform() for the PRIMARY route
    (which also gives apply_villa_crop the villa's own rotation, for
    free, from the corner positions it produces), or an IDENTITY
    Transform for the FALLBACK route below - local_min/max are then
    simply the world-space box (no rotation info available), which
    degrades correctly to a plain axis-aligned crop, exactly the
    original behaviour.

    PRIMARY route: walk the linked document's own physical model
    elements (see _model_element_local_bbox) - requires the link to be
    genuinely loaded (GetLinkDocument() returns a real Document).

    FALLBACK (only when the link cannot be traversed directly - an
    unloaded/broken link): the whole-instance get_BoundingBox(None),
    then get_BoundingBox(active_view) - both APPROXIMATE, since Revit's
    own aggregate box for a link instance is not filterable by category
    and may include datums, but still better than nothing. Flagged as
    "approximate" in the returned detail so a caller can tell which
    villas got the imprecise treatment."""
    try:
        link_doc = instance.GetLinkDocument()
    except Exception:
        link_doc = None

    if link_doc is not None:
        try:
            transform = instance.GetTotalTransform()
        except Exception:
            transform = None
        if transform is not None:
            result = _model_element_local_bbox(link_doc)
            if result is not None:
                return (result[0], result[1], transform), ""
            return None, ("the linked model has no physical (wall/floor/roof/door/...) "
                          "elements to crop to")

    identity = _identity_transform()
    try:
        bbox = instance.get_BoundingBox(None)
        if bbox is not None:
            return (bbox.Min, bbox.Max, identity), (
                "approximate - could not read the linked model's own geometry directly, "
                "used the link's overall extents (not rotation-aligned)")
    except Exception:
        pass

    if active_view is not None:
        try:
            bbox = instance.get_BoundingBox(active_view)
            if bbox is not None:
                return (bbox.Min, bbox.Max, identity), (
                    "approximate - could not read the linked model's own geometry directly, "
                    "used the link's overall extents (not rotation-aligned)")
        except Exception:
            pass

    return None, "the link is not loaded - load/reload it first"


def apply_villa_crop(view, local_min, local_max, transform, offset_internal):
    """Sets the view's Crop Region to a rectangle tightly hugging the
    villa, ALIGNED TO THE VILLA'S OWN ROTATION - not a larger world-
    axis-aligned box around a rotated building (a live run showed
    exactly that: a diagonal villa sitting inside a much-too-big
    straight crop). offset_internal pads local_min/local_max along the
    villa's OWN local X/Y axes (so the margin looks even all the way
    around the building regardless of its rotation) before the 4
    corners are placed via `transform` - the rotation comes for free
    from the corner positions, no separate "rotate the crop" step.

    Uses GetCropRegionShapeManager().SetCropShape(CurveLoop) - the SAME
    mechanism DeeViewAdjust already proved live for room-boundary crops
    - rather than trying to rotate BoundingBoxXYZ.Transform directly,
    which does not visibly rotate a plan view's crop (see module
    docstring)."""
    x0 = local_min.X - offset_internal
    x1 = local_max.X + offset_internal
    y0 = local_min.Y - offset_internal
    y1 = local_max.Y + offset_internal
    z = local_min.Z

    local_corners = [XYZ(x0, y0, z), XYZ(x1, y0, z), XYZ(x1, y1, z), XYZ(x0, y1, z)]
    try:
        world_corners = [transform.OfPoint(c) for c in local_corners]
    except Exception as e:
        return False, "Could not place the crop corners: {0}".format(e)

    try:
        if not view.CropBoxActive:
            view.CropBoxActive = True
    except Exception:
        pass

    try:
        crsm = view.GetCropRegionShapeManager()
    except Exception as e:
        return False, "Could not access this view's crop region shape manager: {0}".format(e)
    try:
        if not crsm.CanHaveShape:
            return False, "This view does not support a shaped crop region"
    except Exception:
        pass

    try:
        loop = CurveLoop()
        for i in range(4):
            loop.Append(Line.CreateBound(world_corners[i], world_corners[(i + 1) % 4]))
    except Exception as e:
        return False, "Could not build the crop rectangle: {0}".format(e)

    try:
        valid = crsm.IsCropRegionShapeValid(loop)
    except Exception as e:
        return False, "Could not validate the crop shape ({0}) - not applied".format(e)
    if not valid:
        return False, "Revit rejected the villa's crop rectangle as a crop shape"

    try:
        crsm.SetCropShape(loop)
        return True, "Crop set to the villa's footprint"
    except Exception as e:
        return False, "Could not set the crop shape: {0}".format(e)


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


def _fit_and_center_viewport(doc, sheet, view, probe_scale=100, fixed_scale=None):
    """Returns (viewport_or_None, warning_or_None).

    fixed_scale=None -> auto-fit: measure the real viewport box at a
    probe scale, then pick the largest standard scale that still fits
    the sheet, re-measuring (the original, unconditional behaviour).
    fixed_scale=<int> -> use exactly that scale - still measured, and
    an overflow is reported as a warning rather than silently allowed,
    but the scale the user asked for on this Sheet Type is never
    overridden. Mirrors DeeAssemb's own proven _fit_and_place_viewport,
    simplified to one full-sheet cell instead of a grid."""
    outline = sheet.Outline
    cell = (outline.Min.U, outline.Max.U, outline.Min.V, outline.Max.V)
    cx = (cell[0] + cell[1]) / 2.0
    cy = (cell[2] + cell[3]) / 2.0
    cell_w = cell[1] - cell[0]
    cell_h = cell[3] - cell[2]

    wanted = int(fixed_scale) if fixed_scale else probe_scale
    scale_locked = False
    try:
        view.Scale = wanted
    except Exception:
        scale_locked = True

    if not Viewport.CanAddViewToSheet(doc, sheet.Id, view.Id):
        return None, None

    vp = Viewport.Create(doc, sheet.Id, view.Id, XYZ(cx, cy, 0))
    doc.Regenerate()

    def measured():
        o = vp.GetBoxOutline()
        return (o.MaximumPoint.X - o.MinimumPoint.X, o.MaximumPoint.Y - o.MinimumPoint.Y)

    w, h = measured()
    warning = None

    if fixed_scale and scale_locked:
        warning = ("could not set the requested 1:{0} - the view's scale is locked "
                   "(a View Template is probably assigned)".format(int(fixed_scale)))
    elif not fixed_scale and w > 1e-9 and h > 1e-9 and cell_w > 0 and cell_h > 0:
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

    if not scale_locked and (w > cell_w + 1e-6 or h > cell_h + 1e-6):
        if fixed_scale:
            warning = ("the view does not fit its sheet at the 1:{0} you chose - placed "
                       "anyway and allowed to overflow".format(int(fixed_scale)))
        else:
            warning = "still larger than its sheet even at the coarsest standard scale"

    vp.SetBoxCenter(XYZ(cx, cy, 0))
    return vp, warning


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
    - fed into resolve_villa_bbox's approximate fallback (only used when
    the link's own geometry cannot be read directly).
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

                if not row.sheet_type.include_view:
                    # "not all sheet had to contain views" - a plain
                    # Sheet only, no crop/view/parameters at all.
                    result.row_results.append(SheetBuildRowResult(row, True, "Created (sheet only)"))
                    result.applied += 1
                    continue

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

                ok, detail = apply_villa_crop(view, bbox[0], bbox[1], bbox[2], offset_internal)
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
                bbox_note = " (crop is {0})".format(bbox_detail) if bbox_detail else ""

                vp, scale_warning = _fit_and_center_viewport(
                    doc, sheet, view, fixed_scale=row.sheet_type.fixed_scale)
                scale_note = " - {0}".format(scale_warning) if scale_warning else ""
                if vp is None:
                    result.row_results.append(SheetBuildRowResult(
                        row, True, "Created" + param_note + bbox_note +
                        " - view could not be placed on the sheet"))
                else:
                    result.row_results.append(SheetBuildRowResult(
                        row, True, "Created" + param_note + bbox_note + scale_note))
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
