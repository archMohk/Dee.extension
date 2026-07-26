# -*- coding: utf-8 -*-
"""
DeeLazy.utils
Generic helpers shared by EVERY DeeLazy module (present and future) -
nothing in here is specific to View Cropping. A future module (Sheets,
Dimensions, Levels, Grids, ...) should be able to import this file
unchanged rather than re-deriving unit conversion, safe parameter
access, or view/sheet scanning.

Kept deliberately small: only things more than one module is expected
to need. Module-specific logic belongs in that module's own file under
modules/, not here.
"""
import os

from Autodesk.Revit.DB import (
    FilteredElementCollector, ViewSheet, View, Viewport, ScheduleSheetInstance,
    UnitUtils, UnitTypeId, SpecTypeId,
)


# ==========================================================================
# Units (same pattern already proven in DeeAligner/DeeReLevel)
# ==========================================================================
def length_unit_type_id(doc):
    try:
        return doc.GetUnits().GetFormatOptions(SpecTypeId.Length).GetUnitTypeId()
    except Exception:
        return UnitTypeId.Millimeters


def internal_to_display(doc, value_internal):
    uid = length_unit_type_id(doc)
    try:
        return UnitUtils.ConvertFromInternalUnits(value_internal, uid)
    except Exception:
        return UnitUtils.ConvertFromInternalUnits(value_internal, UnitTypeId.Millimeters)


def display_to_internal(doc, value_display):
    uid = length_unit_type_id(doc)
    try:
        return UnitUtils.ConvertToInternalUnits(value_display, uid)
    except Exception:
        return UnitUtils.ConvertToInternalUnits(value_display, UnitTypeId.Millimeters)


_UNIT_ABBR = [
    (UnitTypeId.Millimeters, "mm"),
    (UnitTypeId.Centimeters, "cm"),
    (UnitTypeId.Meters, "m"),
    (UnitTypeId.Feet, "ft"),
    (UnitTypeId.FeetFractionalInches, "ft"),
    (UnitTypeId.FractionalInches, "in"),
    (UnitTypeId.Inches, "in"),
]


def unit_abbreviation(doc):
    uid = length_unit_type_id(doc)
    for u, abbr in _UNIT_ABBR:
        if u == uid:
            return abbr
    return "mm"


def safe_float(text, default=0.0):
    try:
        return float(text)
    except Exception:
        return default


# ==========================================================================
# Safe element/parameter access - defensive by design: a wrong/renamed
# BuiltInParameter or an element that doesn't support a given parameter
# must degrade (skip, report) rather than crash the whole batch. This is
# the same philosophy already established elsewhere in this extension
# (e.g. DeeReLevel's _bic/_bip, DeeAligner's _find_param_by_name).
# ==========================================================================
def read_name(element):
    """Element.Name, falling back to nothing rather than raising - some
    element/parameter states make the plain .Name getter unreliable."""
    if element is None:
        return None
    try:
        n = element.Name
        if n:
            return n
    except Exception:
        pass
    return None


def find_param_by_name(element, name):
    """Name-based parameter lookup (LookupParameter) - used instead of a
    BuiltInParameter enum member when the exact enum name isn't
    confirmed reliable across Revit versions (documented per-call site,
    not silently assumed)."""
    try:
        p = element.LookupParameter(name)
        if p is not None:
            return p
    except Exception:
        pass
    return None


def get_bool_param_by_name(element, name):
    """Returns True/False, or None if the parameter doesn't exist on
    this element (distinct from False - "not applicable" vs "off")."""
    p = find_param_by_name(element, name)
    if p is None:
        return None
    try:
        return bool(p.AsInteger())
    except Exception:
        return None


def set_bool_param_by_name(element, name, value):
    """Returns True on success, False if the parameter is missing or
    read-only - never raises."""
    p = find_param_by_name(element, name)
    if p is None or p.IsReadOnly:
        return False
    try:
        p.Set(1 if value else 0)
        return True
    except Exception:
        return False


def get_builtin_param(element, bip):
    """Defensive get_Parameter(BuiltInParameter) - returns None instead
    of raising if the parameter doesn't apply to this element."""
    try:
        return element.get_Parameter(bip)
    except Exception:
        return None


# ==========================================================================
# Sheets / placed-view lookup - reusable by any module that needs to
# know "which sheet is this view on" (View Cropping's Sheet filter,
# and any future Sheets/Views module).
# ==========================================================================
def all_sheets(doc):
    return list(FilteredElementCollector(doc).OfClass(ViewSheet))


def build_view_to_sheet_map(doc):
    """{view_id_int: ViewSheet} for every View placed on a Sheet via a
    Viewport, or a Schedule placed via a ScheduleSheetInstance - one
    pass over the whole document rather than a per-view lookup, so
    this stays fast on projects with thousands of views/sheets
    (matches this extension's established "one collector pass, not one
    per item" performance convention)."""
    mapping = {}
    try:
        for vp in FilteredElementCollector(doc).OfClass(Viewport):
            try:
                sheet = doc.GetElement(vp.SheetId)
                if sheet is not None:
                    mapping[vp.ViewId.IntegerValue] = sheet
            except Exception:
                continue
    except Exception:
        pass
    try:
        for ssi in FilteredElementCollector(doc).OfClass(ScheduleSheetInstance):
            try:
                sheet = doc.GetElement(ssi.SheetId)
                if sheet is not None:
                    mapping[ssi.ScheduleId.IntegerValue] = sheet
            except Exception:
                continue
    except Exception:
        pass
    return mapping


def sheet_label(sheet):
    if sheet is None:
        return ""
    try:
        return "{0} - {1}".format(sheet.SheetNumber, read_name(sheet) or "(unnamed)")
    except Exception:
        return ""


# ==========================================================================
# View classification - shared by any module that needs to know what
# kind of view it's looking at (not just View Cropping).
# ==========================================================================
def is_view_template(view):
    try:
        return bool(view.IsTemplate)
    except Exception:
        return False


def get_primary_view_id(view):
    """Returns the primary/parent view's ElementId if `view` is a
    dependent view, or None if it's a primary view (or the check
    failed) - View.GetPrimaryViewId() is documented to return
    ElementId.InvalidElementId for a non-dependent view."""
    try:
        pid = view.GetPrimaryViewId()
        if pid is not None and pid.IntegerValue > 0:
            return pid
    except Exception:
        pass
    return None


def is_dependent_view(view):
    return get_primary_view_id(view) is not None


def module_dir():
    return os.path.dirname(os.path.abspath(__file__))
