# -*- coding: utf-8 -*-
"""
dee_qs_service
Business logic for DeeQs (Phase 1 of a multi-phase BOQ/QTO tool - see
the project's plan file for the full roadmap). Scans every real model
element in the project, computes a real measured quantity per element
(Area / Volume / Length / Count - picked per-category from
_CATEGORY_QUANTITY_MAP below, never a single generic "auto-detect"
parameter, matching how real-world QTO tools behave), and pivots those
quantities against a second, user-chosen "segregation" parameter's
value (e.g. Level) into a BOQ (Bill of Quantities) structure: one
Section per distinct segregation value, one Item per Category within
it - matching the "Floor 1 has value 1, Floor 2 has value 2" example
from the original request, upgraded to real quantities rather than
counts per the user's explicit choice.

Two-phase scan, mirroring DeeRoomStamp's proven shape: build_element_
universe() does one cheap collector pass at window-open (category
names + parameter-name universe, for the picker UI), and
scan_for_quantities() does the real per-category quantity extraction
on demand (Scan button), reusing the already-cached element list so
the project is never walked more than twice regardless of project size.

Category -> quantity-kind mapping, WebFetch-verified against Autodesk's
own API docs/support articles this session (see plan file):
  - Walls/Floors/Ceilings/Roofs -> Area (HOST_AREA_COMPUTED)
  - Pipes/Ducts/Conduit/CableTray -> Length (CURVE_ELEM_LENGTH, with a
    LocationCurve fallback - a confirmed NullReferenceException case on
    some Pipes)
  - Structural Framing/Columns -> Volume (HOST_VOLUME_COMPUTED) - the
    Length parameter is confirmed-unreliable specifically in Revit 2024
    (Autodesk Support-documented bug, fixed in 2025/2026), so Length is
    NOT used as this category's quantity even though it's tempting
  - Doors/Windows -> Count only (Area is documented-unreliable for
    these categories - a confusing multi-axis overlap sum)
  - Rooms/Spaces/Areas -> excluded entirely (CategoryType.Model filter
    below) - a BOQ lists physical construction elements, not the
    spatial rooms themselves
  - everything else (Furniture, Generic Models, Casework, Specialty
    Equipment...) -> Count (geometry-derived Volume is documented as
    fragile - can read 0 until interactively clicked once in-session)

Every quantity read goes through a 3-tier fallback: the mapped
BuiltInParameter -> (length categories only) LocationCurve.Curve.Length
-> a generic scan of the element's own parameters for one whose
Definition matches the expected spec (Area/Volume/Length) by data
type. This last tier exists specifically because the exact
BuiltInParameter for Structural Framing/Columns Volume has NOT been
verified live in this codebase yet (flagged below) - if the primary
guess is wrong, the generic tier still finds a usable value instead of
silently reading 0.

NEEDS LIVE-REVIT VERIFICATION (flagged per this session's own
convention - see DeeRoomStamp's docstring for the same pattern):
  1. BuiltInParameter.HOST_VOLUME_COMPUTED on Structural Framing/
     Columns specifically - confirmed to exist and work for Walls/
     Floors/Ceilings/Roofs (HostObject-derived), NOT independently
     confirmed for structural framing/column instances. The generic
     spec-match fallback (tier 3 above) exists as a safety net for
     exactly this uncertainty.
  2. CURVE_ELEM_LENGTH on Conduit/CableTray specifically - confirmed
     for Pipes/Ducts, assumed (not independently verified) to extend to
     the other two MEP linear categories given they share the same
     LocationCurve-based placement model.
  3. Segregation-parameter reads check the element's own instance
     parameters first, then its type's parameters - this mirrors real
     BOQ practice (Level is usually instance, but some classification
     parameters live on Type only) but the type-parameter fallback
     path itself hasn't been exercised live in this codebase before.
"""
from Autodesk.Revit.DB import (
    FilteredElementCollector, BuiltInCategory, BuiltInParameter, ElementId,
    CategoryType, StorageType, LocationCurve, SpecTypeId, UnitTypeId, UnitUtils,
    RevitLinkInstance,
)


# ----------------------------------------------------------------------------
# Defensive id / name reads (same style as DeeDistributor/DeeRoomStamp's own
# local copies - Element.Name and similar can throw a bare exception on some
# types in this IronPython build)
# ----------------------------------------------------------------------------
def _element_id_value(eid):
    try:
        return eid.Value
    except Exception:
        pass
    try:
        return eid.IntegerValue
    except Exception:
        return str(eid)


def _read_name(element_or_category):
    if element_or_category is None:
        return None
    try:
        n = element_or_category.Name
        if n:
            return n
    except Exception:
        pass
    for bip in (BuiltInParameter.SYMBOL_NAME_PARAM, BuiltInParameter.ALL_MODEL_TYPE_NAME):
        try:
            p = element_or_category.get_Parameter(bip)
            if p is not None:
                val = p.AsString()
                if val:
                    return val
        except Exception:
            continue
    return None


# ----------------------------------------------------------------------------
# Category -> quantity-kind mapping (built defensively - a BuiltInCategory or
# BuiltInParameter enum member that doesn't exist on this Revit version is
# just skipped, never a hard failure)
# ----------------------------------------------------------------------------
_CATEGORY_QUANTITY_SPEC = [
    ("OST_Walls", "area", ["HOST_AREA_COMPUTED"]),
    ("OST_Floors", "area", ["HOST_AREA_COMPUTED"]),
    ("OST_Ceilings", "area", ["HOST_AREA_COMPUTED"]),
    ("OST_Roofs", "area", ["HOST_AREA_COMPUTED"]),
    ("OST_PipeCurves", "length", ["CURVE_ELEM_LENGTH"]),
    ("OST_DuctCurves", "length", ["CURVE_ELEM_LENGTH"]),
    ("OST_Conduit", "length", ["CURVE_ELEM_LENGTH"]),
    ("OST_CableTray", "length", ["CURVE_ELEM_LENGTH"]),
    ("OST_StructuralFraming", "volume", ["HOST_VOLUME_COMPUTED"]),
    ("OST_StructuralColumns", "volume", ["HOST_VOLUME_COMPUTED"]),
    ("OST_Columns", "volume", ["HOST_VOLUME_COMPUTED"]),
]


def _build_category_quantity_map():
    m = {}
    for bic_name, kind, bip_names in _CATEGORY_QUANTITY_SPEC:
        bic = getattr(BuiltInCategory, bic_name, None)
        if bic is None:
            continue
        try:
            cat_id_val = _element_id_value(ElementId(bic))
        except Exception:
            continue
        m[cat_id_val] = (kind, bip_names)
    return m


_CATEGORY_QUANTITY_MAP = _build_category_quantity_map()

_SPEC_BY_KIND = {}
try:
    _SPEC_BY_KIND = {"area": SpecTypeId.Area, "volume": SpecTypeId.Volume, "length": SpecTypeId.Length}
except Exception:
    _SPEC_BY_KIND = {}


# ----------------------------------------------------------------------------
# Quantity extraction (3-tier fallback - see module docstring)
# ----------------------------------------------------------------------------
def _safe_param_double(element, bip_name):
    bip = getattr(BuiltInParameter, bip_name, None)
    if bip is None:
        return None
    try:
        p = element.get_Parameter(bip)
        if p is not None and p.HasValue:
            v = p.AsDouble()
            if v:
                return v
    except Exception:
        pass
    return None


def _curve_length_fallback(element):
    try:
        loc = element.Location
        if isinstance(loc, LocationCurve):
            length = loc.Curve.Length
            if length:
                return length
    except Exception:
        pass
    return None


def _spec_match_fallback(element, kind):
    spec_id = _SPEC_BY_KIND.get(kind)
    if spec_id is None:
        return None
    try:
        for p in element.GetOrderedParameters():
            try:
                if not p.HasValue:
                    continue
                if p.Definition.GetDataType() == spec_id:
                    v = p.AsDouble()
                    if v:
                        return v
            except Exception:
                continue
    except Exception:
        pass
    return None


def get_element_quantity(element, category_id_value):
    """Returns (quantity_internal_units, kind) where kind is one of
    "area"/"volume"/"length"/"count". Count-kind categories always
    return (1.0, "count") - every element counts as one, summed later
    by build_pivot. Never raises - an element this can't measure at all
    still contributes its Count."""
    spec = _CATEGORY_QUANTITY_MAP.get(category_id_value)
    if spec is None:
        return 1.0, "count"
    kind, bip_names = spec

    value = None
    for bip_name in bip_names:
        value = _safe_param_double(element, bip_name)
        if value:
            break
    if not value and kind == "length":
        value = _curve_length_fallback(element)
    if not value:
        value = _spec_match_fallback(element, kind)
    if not value:
        return 0.0, kind
    return value, kind


# ----------------------------------------------------------------------------
# Segregation-parameter value reads
# ----------------------------------------------------------------------------
def _param_display_value(doc, param):
    if param is None or not param.HasValue:
        return None
    try:
        st = param.StorageType
    except Exception:
        return None
    try:
        if st == StorageType.String:
            v = param.AsString()
            return v if v else None
        if st == StorageType.ElementId:
            eid = param.AsElementId()
            if eid is None or eid == ElementId.InvalidElementId:
                return None
            elem = doc.GetElement(eid)
            return _read_name(elem) if elem is not None else None
        # Integer / Double - AsValueString gives the formatted display
        # text (units, Yes/No, etc.) when available
        try:
            vs = param.AsValueString()
            if vs:
                return vs
        except Exception:
            pass
        if st == StorageType.Integer:
            return str(param.AsInteger())
        if st == StorageType.Double:
            return str(param.AsDouble())
    except Exception:
        return None
    return None


def _read_segregation_value(doc, element, param_name):
    if not param_name:
        return None
    try:
        p = element.LookupParameter(param_name)
        val = _param_display_value(doc, p)
        if val:
            return val
    except Exception:
        pass
    try:
        type_id = element.GetTypeId()
        if type_id and type_id != ElementId.InvalidElementId:
            type_elem = doc.GetElement(type_id)
            if type_elem is not None:
                p2 = type_elem.LookupParameter(param_name)
                val = _param_display_value(doc, p2)
                if val:
                    return val
    except Exception:
        pass
    return None


# ----------------------------------------------------------------------------
# Two-phase scan
# ----------------------------------------------------------------------------
_UNASSIGNED_LABEL = "(Unassigned)"


class ElementUniverse(object):
    def __init__(self):
        self.category_names = []
        self.param_names = []
        self.cached_elements = []  # list of (element, category_name, category_id_value)


def _is_model_element(el):
    try:
        if isinstance(el, RevitLinkInstance):
            return False
    except Exception:
        pass
    try:
        cat = el.Category
    except Exception:
        cat = None
    if cat is None:
        return False, None
    try:
        if cat.CategoryType != CategoryType.Model:
            return False, None
    except Exception:
        return False, None
    return True, cat


def build_element_universe(doc, progress_cb=None):
    """One collector pass over every non-type element in the project.
    Narrows to CategoryType.Model (excludes Rooms/Spaces/Areas and
    every annotation/analytical category - real physical construction
    elements only, matching BOQ convention) and skips RevitLinkInstance
    so linked-model geometry never gets double-counted."""
    universe = ElementUniverse()
    elements = list(FilteredElementCollector(doc).WhereElementIsNotElementType())
    total = len(elements)
    cat_names = set()
    param_names = set()
    cached = []
    for i, el in enumerate(elements):
        if progress_cb is not None:
            if progress_cb(i, total):
                break
        is_model, cat = _is_model_element(el)
        if not is_model:
            continue
        cat_name = _read_name(cat) or "(unnamed category)"
        cat_names.add(cat_name)
        try:
            cat_id_val = _element_id_value(cat.Id)
        except Exception:
            cat_id_val = None
        cached.append((el, cat_name, cat_id_val))
        try:
            for p in el.Parameters:
                try:
                    nm = p.Definition.Name
                    if nm:
                        param_names.add(nm)
                except Exception:
                    continue
        except Exception:
            pass
    universe.category_names = sorted(cat_names)
    universe.param_names = sorted(param_names)
    universe.cached_elements = cached
    return universe


class ScanRow(object):
    def __init__(self, element, category_name, quantity, quantity_kind, segregation_value):
        self.element = element
        self.element_id = element.Id
        self.category_name = category_name
        self.quantity = quantity
        self.quantity_kind = quantity_kind
        self.segregation_value = segregation_value if segregation_value else _UNASSIGNED_LABEL


def scan_for_quantities(doc, cached_elements, segregation_param_name, selected_categories=None,
                         segregation_param_name_2=None, progress_cb=None):
    """cached_elements: the (element, category_name, category_id_value)
    list from build_element_universe - re-collecting is never needed
    for the real scan, only re-filtering/re-measuring.

    segregation_param_name_2 (optional, Phase 2): when given, each
    element's two parameter values are combined into one compound
    segregation_value string ("Level 1 | Concrete") rather than
    restructuring BoqSection/BoqItem into a true 3-tier hierarchy -
    build_pivot/seed_boq_from_pivot/renumber_all are completely
    unchanged by this, they just see a richer value string, keeping
    this an additive change on top of the already-live-tested Phase 1
    data model rather than a rework of it."""
    rows = []
    total = len(cached_elements)
    for i, (el, cat_name, cat_id_val) in enumerate(cached_elements):
        if progress_cb is not None:
            if progress_cb(i, total):
                break
        if selected_categories is not None and cat_name not in selected_categories:
            continue
        quantity, kind = get_element_quantity(el, cat_id_val)
        seg_value = _read_segregation_value(doc, el, segregation_param_name)
        if segregation_param_name_2:
            seg_value_2 = _read_segregation_value(doc, el, segregation_param_name_2)
            seg_value = "{0} | {1}".format(seg_value or _UNASSIGNED_LABEL, seg_value_2 or _UNASSIGNED_LABEL)
        rows.append(ScanRow(el, cat_name, quantity, kind, seg_value))
    return rows


def raw_rows_for_export(doc, rows):
    """Flattens ScanRow objects (one per scanned element, NOT summed)
    into plain (category_name, segregation_value, quantity, unit)
    tuples for xlsx_writer.write_boq_advanced_xlsx's hidden Data sheet
    - every individual element in display units, so the Advanced
    export stays traceable/auditable back to the raw scan, unlike the
    Summary/Detailed sheets which are aggregated per (value, category)."""
    out = []
    for r in rows:
        display_qty, unit_label = convert_and_label(doc, r.quantity, r.quantity_kind)
        out.append((r.category_name, r.segregation_value, display_qty, unit_label))
    return out


# ----------------------------------------------------------------------------
# Pivot (rows = segregation values, columns = categories)
# ----------------------------------------------------------------------------
def build_pivot(rows):
    """Returns (categories, values, grid) where grid[(value, category)] =
    [quantity_sum_internal, kind]. "(Unassigned)" is always sorted last,
    matching the reference app's own grouping convention."""
    categories = sorted(set(r.category_name for r in rows))
    values = sorted(set(r.segregation_value for r in rows if r.segregation_value != _UNASSIGNED_LABEL))
    if any(r.segregation_value == _UNASSIGNED_LABEL for r in rows):
        values.append(_UNASSIGNED_LABEL)

    grid = {}
    for r in rows:
        key = (r.segregation_value, r.category_name)
        cell = grid.get(key)
        if cell is None:
            grid[key] = [r.quantity, r.quantity_kind]
        else:
            cell[0] += r.quantity
    return categories, values, grid


# ----------------------------------------------------------------------------
# Unit conversion for display (local copy, matching DeeDistributor/
# DeeFinisher's own per-file convention rather than a shared utils import -
# see the DeeViewAdjust "import utils" crash postmortem for why)
# ----------------------------------------------------------------------------
_UNIT_LABELS = {}
try:
    _UNIT_LABELS = {
        UnitTypeId.Millimeters: "mm", UnitTypeId.Centimeters: "cm", UnitTypeId.Meters: "m",
        UnitTypeId.Feet: "ft", UnitTypeId.Inches: "in",
        UnitTypeId.SquareMillimeters: "mm2", UnitTypeId.SquareMeters: "m2", UnitTypeId.SquareFeet: "ft2",
        UnitTypeId.CubicMeters: "m3", UnitTypeId.CubicFeet: "ft3",
    }
except Exception:
    _UNIT_LABELS = {}


def _unit_type_id_for_kind(doc, kind):
    try:
        spec_id = _SPEC_BY_KIND.get(kind)
        if spec_id is not None:
            return doc.GetUnits().GetFormatOptions(spec_id).GetUnitTypeId()
    except Exception:
        pass
    defaults = {"area": UnitTypeId.SquareMeters, "volume": UnitTypeId.CubicMeters, "length": UnitTypeId.Meters}
    return defaults.get(kind, UnitTypeId.Meters)


def convert_and_label(doc, value_internal, kind):
    if kind == "count":
        return value_internal, "nr"
    uid = _unit_type_id_for_kind(doc, kind)
    try:
        val = UnitUtils.ConvertFromInternalUnits(value_internal, uid)
    except Exception:
        val = value_internal
    return val, _UNIT_LABELS.get(uid, "")


class PivotDisplayRow(object):
    """One flattened (value, category) cell of the pivot, for the Scan
    tab's raw preview grid - shown as a long/tidy table (Segregation
    Value | Category | Quantity | Unit) rather than a wide cross-tab,
    since a dynamic-column DataGrid is real added WPF complexity Phase 1
    doesn't need: the actual cross-tab structure is what BOQ Structure
    tab turns this into (one Section per value, one Item per category)."""
    def __init__(self, segregation_value, category_name, quantity, unit):
        self.segregation_value = segregation_value
        self.category_name = category_name
        self.quantity = quantity
        self.unit = unit

    @property
    def quantity_text(self):
        return "{0:.2f}".format(self.quantity)


def pivot_display_rows(doc, categories, values, grid):
    rows = []
    for value in values:
        for category_name in categories:
            cell = grid.get((value, category_name))
            if cell is None:
                continue
            qty_internal, kind = cell
            if not qty_internal:
                continue
            display_qty, unit_label = convert_and_label(doc, qty_internal, kind)
            rows.append(PivotDisplayRow(value, category_name, display_qty, unit_label))
    return rows


# ----------------------------------------------------------------------------
# BOQ data model
# ----------------------------------------------------------------------------
class BoqItem(object):
    """quantity/rate are always plain Python floats (read by .amount, by
    seed_boq_from_pivot, and by xlsx_writer.write_boq_xlsx directly).
    quantity_edit/rate_edit are separate string-facing properties for
    the BOQ Structure tab's editable DataGrid columns to bind to -
    IronPython's dynamic attributes get NO automatic string<->double
    conversion from WPF's binding engine (that only happens for
    statically-typed CLR properties), so a TwoWay-bound DataGridTextColumn
    on a raw float attribute would silently replace it with whatever
    string the user typed, breaking every downstream multiplication.
    Routing edits through a real property's setter (which IronPython
    exposes to WPF as a proper CLR property, unlike a plain attribute)
    lets it parse defensively instead."""
    def __init__(self, description, category_name="", quantity=0.0, unit="", rate=0.0, quantity_kind="count"):
        self.item_no = ""
        self.description = description
        self.category_name = category_name
        self.quantity = float(quantity)
        self.unit = unit
        self.rate = float(rate)
        self.quantity_kind = quantity_kind

    @property
    def amount(self):
        return self.quantity * self.rate

    @property
    def amount_text(self):
        return "{0:.2f}".format(self.amount)

    def _get_quantity_edit(self):
        return "{0:.2f}".format(self.quantity)

    def _set_quantity_edit(self, value):
        try:
            self.quantity = float(value)
        except (TypeError, ValueError):
            pass  # invalid input left untouched - getter re-renders the last valid value

    quantity_edit = property(_get_quantity_edit, _set_quantity_edit)

    def _get_rate_edit(self):
        return "{0:.2f}".format(self.rate)

    def _set_rate_edit(self, value):
        try:
            self.rate = float(value)
        except (TypeError, ValueError):
            pass

    rate_edit = property(_get_rate_edit, _set_rate_edit)


class BoqSection(object):
    def __init__(self, title):
        self.section_no = ""
        self.title = title
        self.items = []

    @property
    def subtotal(self):
        return sum(i.amount for i in self.items)

    @property
    def subtotal_text(self):
        return "{0:.2f}".format(self.subtotal)

    @property
    def label(self):
        return "{0}  {1}   (Subtotal: {2})".format(self.section_no, self.title, self.subtotal_text)


def renumber_all(sections):
    """Section-hierarchical numbering only (Phase 1's one supported mode -
    e.g. "2.03"). Sequential/code modes are a later-phase addition."""
    for si, section in enumerate(sections):
        section.section_no = str(si + 1)
        for ii, item in enumerate(section.items):
            item.item_no = "{0}.{1:02d}".format(si + 1, ii + 1)


def seed_boq_from_pivot(doc, categories, values, grid):
    """Turns a build_pivot() result into an editable BOQ structure: one
    Section per distinct segregation value, one Item per Category that
    actually has a nonzero quantity under it. Rate is left at 0.0 for
    the user to fill in - Amount is always Quantity x Rate, computed
    live, never stored."""
    sections = []
    for value in values:
        section = BoqSection(value)
        for category_name in categories:
            cell = grid.get((value, category_name))
            if cell is None:
                continue
            qty_internal, kind = cell
            if not qty_internal:
                continue
            display_qty, unit_label = convert_and_label(doc, qty_internal, kind)
            section.items.append(BoqItem(
                description=category_name, category_name=category_name,
                quantity=round(display_qty, 2), unit=unit_label, rate=0.0, quantity_kind=kind))
        if section.items:
            sections.append(section)
    renumber_all(sections)
    return sections
