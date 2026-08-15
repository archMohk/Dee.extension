# -*- coding: utf-8 -*-
"""
DeeLazy - DeeSSelect module
Groups elements in the ACTIVE VIEW by a chosen category + parameter's
unique values - exactly like pyRevit's own built-in "Color Splasher"
tool (Analysis.panel, pyRevitTools.extension) - and lets you Apply
Colors, Create View Filters, or Create a Legend from the grouping.
Additionally, each value-group row has its own checkbox, and a
dedicated "Select Checked" action selects the UNION of elements across
every checked group at once in Revit - Color Splasher only supports
selecting one group at a time (shift-click a row).

--------------------------------------------------------------------
Porting source of truth
--------------------------------------------------------------------
Ported from pyRevit's real Color Splasher source, read in full before
writing this module:
C:\\Program Files\\pyRevit-Master\\extensions\\pyRevitTools.extension\\
pyRevit.tab\\Analysis.panel\\ColorSplasher.pushbutton\\script.py
All business-logic functions below (value extraction, grouping,
random/gradient color generation, OGS application, View Filter
creation, Legend creation, .cschn save/load) mirror that source's
logic closely. Confirmed by the user: active-view-only scope, full
feature parity with Color Splasher plus the new checkbox-select
capability.

--------------------------------------------------------------------
Architectural difference from Color Splasher: modal, not modeless
--------------------------------------------------------------------
Color Splasher's window is modeless (wndw.show(), Topmost=True) -
because a modeless WPF window can't call the Revit API directly, it's
built around 5 IExternalEvent/IExternalEventHandler pairs plus a
ViewActivated subscription to handle the user switching views/
documents while it's open. Every other DeeLazy module opens modal
(window.ShowDialog()) and calls the Revit API directly from click
handlers - going modal here eliminates the entire problem class that
machinery exists to solve (the model/view can't change out from under
a modal dialog), so none of it is ported. All actions below are plain
click handlers with a Transaction opened directly inside.

--------------------------------------------------------------------
Version pruning (this extension targets Revit 2024/2026 only)
--------------------------------------------------------------------
The real Color Splasher supports Revit versions back to ~2018 and
carries several version-gated branches for that range. Since
Dee.extension targets 2024/2026 only, the following branches are
pruned entirely (only the "newer" side of each gate could ever run
here):
- ColorFillScheme auto-assignment's pre-2021 "else" warning branch.
- Background-pattern-override's pre-2019 disabled-checkbox branch.
- The pre-2023 3-arg case-sensitive String filter-rule overload.
- get_integer_value's legacy ParameterType-based YesNo branch.

--------------------------------------------------------------------
A real Color Splasher bug, fixed here rather than ported
--------------------------------------------------------------------
Color Splasher's own Reset Colors searches for View Filters to delete
using the prefix "{category}/" (sel_cat.name + "/"), but Create View
Filters actually names filters "{Category} {Param} - {Value}" (space-
separated, no slash) - the prefix never matches, so Reset silently
leaves every filter it ever created behind. reset_colors() below
matches the REAL naming scheme (category name + a space) instead.

--------------------------------------------------------------------
NEEDS LIVE-REVIT VERIFICATION (flagged, not silently assumed correct -
same policy as this module's siblings, fill_conversion.py/
view_cropping.py)
--------------------------------------------------------------------
- create_view_filters() is the first use of ParameterFilterElement/
  ElementParameterFilter/ParameterFilterRuleFactory anywhere in
  Dee.extension. The StorageType branching is ported closely from
  Color Splasher's proven implementation, but needs a live smoke test
  on both Revit 2024 and 2026 specifically before being trusted blind
  - don't assume identical behavior across both just because both
  satisfy the same "> 2023" version gate the pruning above relies on.
- create_legend()'s geometry/layout math (text note placement, filled
  region swatch sizing) is ported closely but has not been run live.
"""
import os
import time
from random import randint
from unicodedata import normalize
from unicodedata import category as _unicode_category

import clr
clr.AddReference("System.Windows.Forms")
clr.AddReference("System.Drawing")
from System import Enum as _SystemEnum
from System.Windows.Forms import OpenFileDialog, SaveFileDialog, DialogResult, ColorDialog
from System.Drawing import Color as DrawingColor
from System.Windows.Media import SolidColorBrush, Color as MediaColor
from System.Collections.Generic import List

from pyrevit import forms, script
import dee_branding

from Autodesk.Revit.DB import (
    FilteredElementCollector, BuiltInCategory, BuiltInParameter, StorageType,
    Transaction, OverrideGraphicSettings, Color as RevitColor, ElementId, Element,
    ParameterFilterElement, ParameterFilterRuleFactory, ElementParameterFilter,
    FillPatternElement, ColorFillScheme, View, ViewType, ViewDuplicateOption,
    TextNote, TextNoteType, FilledRegion, FilledRegionType, FillPattern,
    FillPatternTarget, FillPatternHostOrientation, XYZ, Line, CurveLoop, SpecTypeId,
)

output = script.get_output()

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "DeeSSelect.xaml")


def strip_accents(text):
    try:
        return "".join(ch for ch in normalize("NFKD", text) if _unicode_category(ch) != "Mn")
    except Exception:
        return text


def _element_id_value(eid):
    """Dee.extension's own ElementId-across-Revit-versions compat
    helper (established convention - see DeeRehoster.pushbutton), used
    in place of pyrevit.compat.get_elementid_value_func()."""
    try:
        return eid.Value
    except Exception:
        pass
    try:
        return eid.IntegerValue
    except Exception:
        return -1


# ==========================================================================
# Category exclusion list (ported verbatim from Color Splasher's
# CAT_EXCLUDED, built defensively - a BuiltInCategory member missing on
# some Revit version is skipped, not a hard crash at import time)
# ==========================================================================
_CAT_EXCLUDED_NAMES = (
    "OST_RoomSeparationLines", "OST_Cameras", "OST_CurtainGrids", "OST_Elev",
    "OST_Grids", "OST_IOSModelGroups", "OST_Views", "OST_SitePropertyLineSegment",
    "OST_SectionBox", "OST_ShaftOpening", "OST_BeamAnalytical",
    "OST_StructuralFramingOpening", "OST_MEPSpaceSeparationLines", "OST_DuctSystem",
    "OST_Lines", "OST_PipingSystem", "OST_Matchline", "OST_CenterLines",
    "OST_CurtainGridsRoof", "OST_SWallRectOpening",
)


def _build_cat_excluded():
    ids = []
    for name in _CAT_EXCLUDED_NAMES:
        try:
            ids.append(int(getattr(BuiltInCategory, name)))
        except Exception:
            continue
    ids.append(-2000278)
    ids.append(-1)
    return tuple(ids)


CAT_EXCLUDED = _build_cat_excluded()

_EXCLUDED_PARAMS = (BuiltInParameter.ELEM_CATEGORY_PARAM, BuiltInParameter.ELEM_CATEGORY_PARAM_MT)


# ==========================================================================
# Active view validation (ported from Color Splasher's get_active_view)
# ==========================================================================
def get_splashable_view(doc, uidoc):
    view = doc.ActiveView
    if view.ViewType in (ViewType.ProjectBrowser, ViewType.SystemBrowser):
        open_views = list(uidoc.GetOpenUIViews())
        if not open_views:
            return None
        view = doc.GetElement(open_views[0].ViewId)
    if not view.CanUseTemporaryVisibilityModes():
        return None
    return view


# ==========================================================================
# Category / parameter discovery
# ==========================================================================
class ParameterInfo(object):
    def __init__(self, param_type, definition, rl_par):
        self.param_type = param_type  # 0 = instance, 1 = type
        self.definition = definition
        self.rl_par = rl_par
        self.name = strip_accents(definition.Name)


class CategoryInfo(object):
    def __init__(self, category):
        self.cat = category
        self.name = strip_accents(category.Name)
        self.int_id = _element_id_value(category.Id)
        self.parameters = []


def scan_categories(doc, view):
    collector = (
        FilteredElementCollector(doc, view.Id)
        .WhereElementIsNotElementType()
        .WhereElementIsViewIndependent()
        .ToElements()
    )
    cats_by_id = {}
    for ele in collector:
        try:
            cat = ele.Category
            if cat is None:
                continue
            cat_id_val = _element_id_value(cat.Id)
            if cat_id_val in CAT_EXCLUDED or cat_id_val >= -1 or cat_id_val in cats_by_id:
                continue

            parameters = []
            for par in ele.Parameters:
                try:
                    if par.Definition.BuiltInParameter not in _EXCLUDED_PARAMS:
                        parameters.append(ParameterInfo(0, par.Definition, par))
                except Exception:
                    continue
            typ = doc.GetElement(ele.GetTypeId())
            if typ is not None:
                for par in typ.Parameters:
                    try:
                        if par.Definition.BuiltInParameter not in _EXCLUDED_PARAMS:
                            parameters.append(ParameterInfo(1, par.Definition, par))
                    except Exception:
                        continue
            parameters.sort(key=lambda p: p.name.upper())

            info = CategoryInfo(cat)
            info.parameters = parameters
            cats_by_id[cat_id_val] = info
        except Exception:
            continue
    result = list(cats_by_id.values())
    result.sort(key=lambda c: c.name)
    return result


# ==========================================================================
# Value extraction (ported from get_parameter_value/get_double_value/
# get_elementid_value/get_integer_value - pruned to the always-true
# side of each version gate for a 2024/2026-only extension)
# ==========================================================================
def get_parameter_display_value(doc, param):
    if not param.HasValue:
        return "None"
    st = param.StorageType
    if st == StorageType.Double:
        return param.AsValueString()
    if st == StorageType.ElementId:
        id_val = param.AsElementId()
        if _element_id_value(id_val) >= 0:
            el = doc.GetElement(id_val)
            return Element.Name.GetValue(el) if el is not None else "None"
        return "None"
    if st == StorageType.Integer:
        try:
            param_type = param.Definition.GetDataType()
            if SpecTypeId.Boolean.YesNo == param_type:
                return "True" if param.AsInteger() == 1 else "False"
        except Exception:
            pass
        return param.AsValueString()
    if st == StorageType.String:
        return param.AsString() or "None"
    return "None"


def random_color(used_colors):
    while True:
        r = randint(0, 230)
        g = randint(0, 230)
        b = randint(0, 230)
        if (r, g, b) not in used_colors:
            return (r, g, b)


def get_index_units(str_value):
    """Returns how many trailing non-digit chars a value string ends
    with (e.g. "10 mm" -> 3, for " mm"), 0 if it ends in a digit, -1
    if it has no digit at all - used to numeric-sort values like
    "10 mm"/"2 mm" correctly instead of alphabetically."""
    reversed_value = str_value[::-1]
    for ch in reversed_value:
        if ch.isdigit():
            return reversed_value.index(ch)
    return -1


def safe_float(value):
    try:
        return float(value)
    except ValueError:
        return float("inf")


# ==========================================================================
# Grouping (ported from get_range_values)
# ==========================================================================
class ValueGroupRow(object):
    def __init__(self, value_text, sample_param):
        self.value_text = value_text
        self.element_ids = List[ElementId]()
        self.sample_param = sample_param
        self.raw_doubles = []
        self.color = (200, 200, 200)
        self.checked = False

    @property
    def count(self):
        return self.element_ids.Count

    @property
    def count_text(self):
        return str(self.count)

    @property
    def color_brush(self):
        r, g, b = self.color
        return SolidColorBrush(MediaColor.FromRgb(r, g, b))


def scan_values(doc, view, category_info, param_info):
    bic = None
    for sample_bic in _SystemEnum.GetValues(BuiltInCategory):
        if category_info.int_id == int(sample_bic):
            bic = sample_bic
            break
    if bic is None:
        return []

    collector = (
        FilteredElementCollector(doc, view.Id)
        .OfCategory(bic)
        .WhereElementIsNotElementType()
        .WhereElementIsViewIndependent()
        .ToElements()
    )

    rows_by_value = {}
    order = []
    used_colors = set()
    for ele in collector:
        try:
            ele_par = ele if param_info.param_type != 1 else doc.GetElement(ele.GetTypeId())
            if ele_par is None:
                continue
            matched = None
            for pr in ele_par.Parameters:
                if pr.Definition.Name == param_info.definition.Name:
                    matched = pr
                    break
            if matched is None:
                continue
            value = get_parameter_display_value(doc, matched) or "None"
            row = rows_by_value.get(value)
            if row is None:
                row = ValueGroupRow(value, matched)
                row.color = random_color(used_colors)
                used_colors.add(row.color)
                rows_by_value[value] = row
                order.append(value)
            row.element_ids.Add(ele.Id)
            if matched.StorageType == StorageType.Double:
                row.raw_doubles.append(matched.AsDouble())
        except Exception:
            continue

    none_row = rows_by_value.pop("None", None)
    rows = [rows_by_value[v] for v in order if v in rows_by_value]
    rows.sort(key=lambda r: r.value_text)
    if len(rows) > 1:
        try:
            first_value = rows[0].value_text
            idx_del = get_index_units(first_value)
            if idx_del == 0:
                rows.sort(key=lambda r: safe_float(r.value_text))
            elif 0 < idx_del < len(first_value):
                rows.sort(key=lambda r: safe_float(r.value_text[:-idx_del]))
        except Exception:
            pass
    if none_row is not None and none_row.count > 0:
        rows.append(none_row)
    return rows


# ==========================================================================
# Color generation
# ==========================================================================
def assign_random_colors(rows):
    used = set()
    for row in rows:
        c = random_color(used)
        used.add(c)
        row.color = c


def compute_gradient_colors(start_rgb, end_rgb, steps):
    if steps <= 0:
        return []
    r0, g0, b0 = start_rgb
    r1, g1, b1 = end_rgb
    r_step = float(r1 - r0) / steps
    g_step = float(g1 - g0) / steps
    b_step = float(b1 - b0) / steps
    colors = []
    for i in range(steps):
        r = max(0, min(255, int(r0 + r_step * i)))
        g = max(0, min(255, int(g0 + g_step * i)))
        b = max(0, min(255, int(b0 + b_step * i)))
        colors.append((r, g, b))
    return colors


def apply_gradient(rows):
    if len(rows) < 2:
        return
    colors = compute_gradient_colors(rows[0].color, rows[-1].color, len(rows))
    for row, c in zip(rows, colors):
        row.color = c


def get_color_shades(base_color, apply_line, apply_foreground, apply_background):
    """Returns (line_color, foreground_color, background_color) RGB
    tuples - foreground/background use the full base color; line
    color is faded/desaturated when combined with a pattern color, so
    the two don't visually compete (ported verbatim from Color
    Splasher's get_color_shades)."""
    r, g, b = base_color
    if apply_line and (apply_foreground or apply_background):
        line_r = max(0, min(255, int(r + (255 - r) * 0.6)))
        line_g = max(0, min(255, int(g + (255 - g) * 0.6)))
        line_b = max(0, min(255, int(b + (255 - b) * 0.6)))
        gray = (line_r + line_g + line_b) / 3.0
        line_color = (
            int(line_r * 0.7 + gray * 0.3),
            int(line_g * 0.7 + gray * 0.3),
            int(line_b * 0.7 + gray * 0.3),
        )
    else:
        line_color = base_color
    return line_color, base_color, base_color


# ==========================================================================
# OGS application
# ==========================================================================
def _solid_fill_pattern_id(doc):
    for fp in FilteredElementCollector(doc).OfClass(FillPatternElement):
        try:
            if fp.GetFillPattern().IsSolidFill:
                return fp.Id
        except Exception:
            continue
    return ElementId.InvalidElementId


def _build_ogs(color_rgb, apply_line, apply_fg, apply_bg, solid_fill_id):
    ogs = OverrideGraphicSettings()
    line_color, fg_color, bg_color = get_color_shades(color_rgb, apply_line, apply_fg, apply_bg)
    if apply_line:
        rc = RevitColor(*line_color)
        ogs.SetProjectionLineColor(rc)
        ogs.SetCutLineColor(rc)
    if apply_fg:
        rc = RevitColor(*fg_color)
        ogs.SetSurfaceForegroundPatternColor(rc)
        ogs.SetCutForegroundPatternColor(rc)
        if solid_fill_id != ElementId.InvalidElementId:
            ogs.SetSurfaceForegroundPatternId(solid_fill_id)
            ogs.SetCutForegroundPatternId(solid_fill_id)
    if apply_bg:
        rc = RevitColor(*bg_color)
        ogs.SetSurfaceBackgroundPatternColor(rc)
        ogs.SetCutBackgroundPatternColor(rc)
        if solid_fill_id != ElementId.InvalidElementId:
            ogs.SetSurfaceBackgroundPatternId(solid_fill_id)
            ogs.SetCutBackgroundPatternId(solid_fill_id)
    return ogs


class ActionResult(object):
    def __init__(self):
        self.ok_count = 0
        self.skipped = []  # list of (label, reason)
        self.elapsed_seconds = 0.0
        self.legend_name = ""

    def add_skip(self, label, reason):
        self.skipped.append((label, reason))


def apply_colors(doc, view, category_info, rows, apply_line, apply_fg, apply_bg):
    start = time.time()
    result = ActionResult()
    solid_fill_id = _solid_fill_pattern_id(doc)

    t = Transaction(doc, "DeeSSelect - Apply Colors")
    t.Start()
    try:
        cat_id_val = _element_id_value(category_info.cat.Id)
        if cat_id_val in (int(BuiltInCategory.OST_Rooms), int(BuiltInCategory.OST_MEPSpaces),
                           int(BuiltInCategory.OST_Areas)):
            try:
                if view.GetColorFillSchemeId(category_info.cat.Id).ToString() == "-1":
                    for sch in FilteredElementCollector(doc).OfClass(ColorFillScheme):
                        if sch.CategoryId == category_info.cat.Id and len(list(sch.GetEntries())) > 0:
                            view.SetColorFillSchemeId(category_info.cat.Id, sch.Id)
                            break
            except Exception:
                pass

        for row in rows:
            ogs = _build_ogs(row.color, apply_line, apply_fg, apply_bg, solid_fill_id)
            for eid in row.element_ids:
                try:
                    view.SetElementOverrides(eid, ogs)
                    result.ok_count += 1
                except Exception as e:
                    result.add_skip(row.value_text, "Override failed: {0}".format(e))
        t.Commit()
    except Exception:
        t.RollBack()
        raise

    result.elapsed_seconds = time.time() - start
    return result


def reset_colors(doc, view, category_info):
    start = time.time()
    result = ActionResult()

    t = Transaction(doc, "DeeSSelect - Reset Colors")
    t.Start()
    try:
        ogs = OverrideGraphicSettings()
        collector = (
            FilteredElementCollector(doc, view.Id)
            .WhereElementIsNotElementType()
            .WhereElementIsViewIndependent()
            .ToElementIds()
        )
        for eid in collector:
            try:
                view.SetElementOverrides(eid, ogs)
                result.ok_count += 1
            except Exception:
                continue

        # Fix for a real Color Splasher bug (see module docstring):
        # match filters by the naming scheme create_view_filters()
        # actually uses ("{Category} {Param} - {Value}"), not the
        # mismatched "{category}/" prefix the original tool searches
        # for (which never matches anything and orphans filters).
        filter_prefix = category_info.name + " "
        for filt_id in list(view.GetFilters()):
            try:
                filt_ele = doc.GetElement(filt_id)
                if filt_ele is not None and filt_ele.Name.startswith(filter_prefix):
                    view.RemoveFilter(filt_id)
                    try:
                        doc.Delete(filt_id)
                    except Exception:
                        pass
            except Exception:
                continue
        t.Commit()
    except Exception:
        t.RollBack()
        raise

    result.elapsed_seconds = time.time() - start
    return result


# ==========================================================================
# View Filter creation (ported from CreateFilters - first use of
# ParameterFilterElement anywhere in Dee.extension, see module
# docstring's NEEDS LIVE-REVIT VERIFICATION note)
# ==========================================================================
def create_view_filters(doc, view, category_info, param_info, rows, apply_line, apply_fg, apply_bg):
    start = time.time()
    result = ActionResult()
    solid_fill_id = _solid_fill_pattern_id(doc)

    dict_filters = {}
    for filt_id in view.GetFilters():
        filt_ele = doc.GetElement(filt_id)
        dict_filters[filt_ele.Name] = filt_id
    dict_rules = {}
    for pfe in FilteredElementCollector(doc).OfClass(ParameterFilterElement):
        dict_rules[pfe.Name] = pfe.Id

    parameter_id = param_info.rl_par.Id
    storage_type = param_info.rl_par.StorageType
    categories = List[ElementId]()
    categories.Add(category_info.cat.Id)

    t = Transaction(doc, "DeeSSelect - Create View Filters")
    t.Start()
    try:
        for row in rows:
            ogs = _build_ogs(row.color, apply_line, apply_fg, apply_bg, solid_fill_id)
            filter_name = "{0} {1} - {2}".format(category_info.name, param_info.name, row.value_text)
            for ch in "{}[]:\\|?/<>*":
                filter_name = filter_name.replace(ch, "")

            if filter_name in dict_filters:
                view.SetFilterOverrides(dict_filters[filter_name], ogs)
                result.ok_count += 1
                continue
            if filter_name in dict_rules:
                view.AddFilter(dict_rules[filter_name])
                view.SetFilterOverrides(dict_rules[filter_name], ogs)
                result.ok_count += 1
                continue

            try:
                if storage_type == StorageType.Double:
                    if row.value_text == "None" or not row.raw_doubles:
                        equals_rule = ParameterFilterRuleFactory.CreateEqualsRule(parameter_id, "", 0.001)
                    else:
                        lo = min(row.raw_doubles)
                        hi = max(row.raw_doubles)
                        avg = (hi + lo) / 2.0
                        equals_rule = ParameterFilterRuleFactory.CreateEqualsRule(
                            parameter_id, avg, abs(avg - lo) + 0.001)
                elif storage_type == StorageType.ElementId:
                    prevalue = ElementId.InvalidElementId if row.value_text == "None" else row.sample_param.AsElementId()
                    equals_rule = ParameterFilterRuleFactory.CreateEqualsRule(parameter_id, prevalue)
                elif storage_type == StorageType.Integer:
                    prevalue = 0 if row.value_text == "None" else row.sample_param.AsInteger()
                    equals_rule = ParameterFilterRuleFactory.CreateEqualsRule(parameter_id, prevalue)
                elif storage_type == StorageType.String:
                    prevalue = "" if row.value_text == "None" else row.value_text
                    equals_rule = ParameterFilterRuleFactory.CreateEqualsRule(parameter_id, prevalue)
                else:
                    result.add_skip(row.value_text, "Filter rules aren't supported for this parameter's storage type")
                    continue

                elem_filter = ElementParameterFilter(equals_rule)
                pfe = ParameterFilterElement.Create(doc, filter_name, categories, elem_filter)
                view.AddFilter(pfe.Id)
                view.SetFilterOverrides(pfe.Id, ogs)
                result.ok_count += 1
            except Exception as e:
                result.add_skip(row.value_text, "Filter creation failed: {0}".format(e))
        t.Commit()
    except Exception:
        t.RollBack()
        raise

    result.elapsed_seconds = time.time() - start
    return result


# ==========================================================================
# Legend creation (ported from CreateLegend)
# ==========================================================================
def create_legend(doc, category_info, param_info, rows, apply_line, apply_fg, apply_bg):
    if not rows:
        raise Exception("No values to add to the legend.")
    legend = None
    for vw in FilteredElementCollector(doc).OfClass(View):
        if vw.ViewType == ViewType.Legend:
            legend = vw
            break
    if legend is None:
        raise Exception("No Legend view exists in this model - create one first.")

    start = time.time()
    result = ActionResult()

    t = Transaction(doc, "DeeSSelect - Create Legend")
    t.Start()
    try:
        new_legend_id = legend.Duplicate(ViewDuplicateOption.Duplicate)
        new_legend = doc.GetElement(new_legend_id)

        cat_name = category_info.name
        par_name = param_info.name
        base_name = "DeeSSelect - {0} - {1}".format(cat_name, par_name)
        renamed = False
        try:
            new_legend.Name = base_name
            renamed = True
        except Exception:
            pass
        if not renamed:
            for i in range(1000):
                try:
                    new_legend.Name = "{0} - {1}".format(base_name, i)
                    break
                except Exception:
                    if i == 999:
                        raise Exception("Could not rename the new legend view")

        text_note_type_id = None
        for ele in FilteredElementCollector(doc, legend.Id).ToElements():
            if ele.Id != new_legend.Id and isinstance(ele, TextNote):
                text_note_type_id = ele.GetTypeId()
                break
        if text_note_type_id is None:
            for tnt in FilteredElementCollector(doc).OfClass(TextNoteType):
                text_note_type_id = tnt.Id
                break
        if text_note_type_id is None:
            raise Exception("No text note type found in the model")

        filled_type = None
        filled_region_types = list(FilteredElementCollector(doc).OfClass(FilledRegionType))
        for frt in filled_region_types:
            pattern_el = doc.GetElement(frt.ForegroundPatternId)
            if (pattern_el is not None and pattern_el.GetFillPattern().IsSolidFill
                    and frt.ForegroundPatternColor.IsValid):
                filled_type = frt
                break
        if filled_type is None and filled_region_types:
            new_type = None
            for idx in range(100):
                try:
                    new_type = filled_region_types[0].Duplicate("DeeSSelect Fill Region {0}".format(idx))
                    break
                except Exception:
                    if idx == 99:
                        raise Exception("Could not create a fill region type")
            new_pattern_el = None
            for idx in range(100):
                try:
                    new_pattern = FillPattern(
                        "DeeSSelect Fill Pattern {0}".format(idx),
                        FillPatternTarget.Drafting,
                        FillPatternHostOrientation.ToView,
                        0.0, 0.00001)
                    new_pattern_el = FillPatternElement.Create(doc, new_pattern)
                    break
                except Exception:
                    if idx == 99:
                        raise Exception("Could not create a fill pattern")
            new_type.ForegroundPatternId = new_pattern_el.Id
            filled_type = new_type
        if filled_type is None:
            raise Exception("Could not find or create a fill region type")

        list_max_x = []
        list_y = []
        list_heights = []
        y_pos = 0.0
        spacing = 0.0
        for row in rows:
            point = XYZ(0, y_pos, 0)
            text_line = "{0} / {1} - {2}".format(cat_name, par_name, row.value_text)
            new_text = TextNote.Create(doc, new_legend.Id, point, text_line, text_note_type_id)
            doc.Regenerate()
            bbox = new_text.get_BoundingBox(new_legend)
            height = bbox.Max.Y - bbox.Min.Y
            spacing = height * 0.25
            list_max_x.append(bbox.Max.X)
            list_y.append(bbox.Min.Y)
            list_heights.append(height)
            y_pos = bbox.Min.Y - (height + spacing)
        ini_x = (max(list_max_x) + spacing) if list_max_x else spacing
        solid_fill_id = _solid_fill_pattern_id(doc) if apply_fg else ElementId.InvalidElementId

        for i, row in enumerate(rows):
            try:
                height = list_heights[i]
                y = list_y[i]
                rect_width = height * 2
                p0 = XYZ(ini_x, y, 0)
                p1 = XYZ(ini_x, y + height, 0)
                p2 = XYZ(ini_x + rect_width, y + height, 0)
                p3 = XYZ(ini_x + rect_width, y, 0)
                loop = CurveLoop()
                loop.Append(Line.CreateBound(p0, p1))
                loop.Append(Line.CreateBound(p1, p2))
                loop.Append(Line.CreateBound(p2, p3))
                loop.Append(Line.CreateBound(p3, p0))
                loops = List[CurveLoop]()
                loops.Add(loop)
                region = FilledRegion.Create(doc, filled_type.Id, new_legend.Id, loops)

                ogs = OverrideGraphicSettings()
                line_color, fg_color, bg_color = get_color_shades(row.color, apply_line, apply_fg, apply_bg)
                if apply_line:
                    rc = RevitColor(*line_color)
                    ogs.SetProjectionLineColor(rc)
                    ogs.SetCutLineColor(rc)
                if apply_fg:
                    rc = RevitColor(*fg_color)
                    ogs.SetSurfaceForegroundPatternColor(rc)
                    ogs.SetCutForegroundPatternColor(rc)
                    if solid_fill_id != ElementId.InvalidElementId:
                        ogs.SetSurfaceForegroundPatternId(solid_fill_id)
                        ogs.SetCutForegroundPatternId(solid_fill_id)
                elif apply_bg:
                    # A FilledRegion's own visible fill in a Legend view
                    # is governed by its foreground pattern slot - if
                    # only the background channel is checked, paint the
                    # swatch's foreground with the background color
                    # anyway so the swatch isn't left blank (ported
                    # from Color Splasher's own CreateLegend fallback).
                    rc = RevitColor(*bg_color)
                    ogs.SetSurfaceForegroundPatternColor(rc)
                    ogs.SetCutForegroundPatternColor(rc)
                    if solid_fill_id != ElementId.InvalidElementId:
                        ogs.SetSurfaceForegroundPatternId(solid_fill_id)
                        ogs.SetCutForegroundPatternId(solid_fill_id)
                new_legend.SetElementOverrides(region.Id, ogs)
                result.ok_count += 1
            except Exception as e:
                result.add_skip(row.value_text, "Swatch failed: {0}".format(e))
        t.Commit()
        result.legend_name = new_legend.Name
    except Exception:
        t.RollBack()
        raise

    result.elapsed_seconds = time.time() - start
    return result


# ==========================================================================
# Save/Load color scheme (.cschn - plain text, cross-compatible with
# real Color Splasher scheme files)
# ==========================================================================
def save_scheme(path, rows):
    with open(path, "w") as f:
        for row in rows:
            r, g, b = row.color
            f.write("{0}::R{1}G{2}B{3}\n".format(row.value_text, r, g, b))


def _parse_scheme_line(line):
    key, rgb_part = line.strip().split("::R", 1)
    g_idx = rgb_part.index("G")
    b_idx = rgb_part.index("B")
    r = int(rgb_part[:g_idx])
    g = int(rgb_part[g_idx + 1:b_idx])
    b = int(rgb_part[b_idx + 1:])
    return key, (r, g, b)


def load_scheme(path, rows, by_value=True):
    with open(path, "r") as f:
        lines = f.readlines()
    if by_value:
        by_val = {row.value_text: row for row in rows}
        for line in lines:
            try:
                key, rgb = _parse_scheme_line(line)
                row = by_val.get(key)
                if row is not None:
                    row.color = rgb
            except Exception:
                continue
    else:
        for i, line in enumerate(lines):
            if i >= len(rows):
                break
            try:
                _key, rgb = _parse_scheme_line(line)
                rows[i].color = rgb
            except Exception:
                continue


# ==========================================================================
# The new capability - checkbox union select
# ==========================================================================
def elements_for_checked(rows):
    seen = set()
    ids = List[ElementId]()
    for row in rows:
        if not row.checked:
            continue
        for eid in row.element_ids:
            key = _element_id_value(eid)
            if key in seen:
                continue
            seen.add(key)
            ids.Add(eid)
    return ids


def all_elements(rows):
    ids = List[ElementId]()
    for row in rows:
        for eid in row.element_ids:
            ids.Add(eid)
    return ids


def select_ids(uidoc, element_ids):
    uidoc.Selection.SetElementIds(element_ids)
    uidoc.RefreshActiveView()


def clear_selection(uidoc):
    uidoc.Selection.SetElementIds(List[ElementId]())
    uidoc.RefreshActiveView()


# ==========================================================================
# Reporting
# ==========================================================================
def print_report(action_title, result):
    html = ['<h2 style="font-family:sans-serif;color:#ddd;">DeeSSelect - {0} Results</h2>'.format(action_title),
            '<p style="color:#ddd;">{0} succeeded, {1} skipped - {2:.2f}s.</p>'.format(
                result.ok_count, len(result.skipped), result.elapsed_seconds)]
    if result.legend_name:
        html.append('<p style="color:#ddd;">Legend view created: <b>{0}</b></p>'.format(result.legend_name))
    for label, reason in result.skipped:
        html.append(
            '<div style="padding:4px 10px;margin:2px 0;background:#c62828;color:#fff;'
            'border-radius:4px;font-family:monospace;font-size:12px;">'
            '&#10007;&nbsp; <b>{0}</b> &mdash; {1}</div>'.format(label, reason))
    output.print_html("".join(html))


# ==========================================================================
# Window - UI wiring only; all real work happens in the plain functions
# above (same separation as fill_conversion.py/view_cropping.py)
# ==========================================================================
class DeeSSelectWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, uiapp, doc, uidoc, view):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.uiapp = uiapp
        self.doc = doc
        self.uidoc = uidoc
        self.view = view
        self._rows = []

        with forms.ProgressBar(title="DeeSSelect - scanning categories...", indeterminate=True):
            self._categories = scan_categories(self.doc, self.view)
        self.category_cb.ItemsSource = self._categories
        self.param_cb.ItemsSource = []
        self.values_grid.ItemsSource = self._rows
        self._update_status()

    def _update_status(self):
        checked_count = sum(1 for r in self._rows if r.checked)
        self.status_tb.Text = "{0} value(s), {1} checked.".format(len(self._rows), checked_count)

    # ---------------- category / parameter ----------------
    def category_changed(self, sender, args):
        cat = self.category_cb.SelectedItem
        self._rows = []
        self.values_grid.ItemsSource = None
        self.values_grid.ItemsSource = self._rows
        self.param_cb.ItemsSource = cat.parameters if cat is not None else []
        self._update_status()

    def param_changed(self, sender, args):
        cat = self.category_cb.SelectedItem
        param = self.param_cb.SelectedItem
        if cat is None or param is None:
            return
        with forms.ProgressBar(title="DeeSSelect - scanning values...", indeterminate=True):
            self._rows = scan_values(self.doc, self.view, cat, param)
        self.values_grid.ItemsSource = None
        self.values_grid.ItemsSource = self._rows
        self._update_status()

    # ---------------- color scheme tools ----------------
    def random_colors_click(self, sender, args):
        if not self._rows:
            return
        assign_random_colors(self._rows)
        self.values_grid.Items.Refresh()

    def gradient_colors_click(self, sender, args):
        if len(self._rows) < 2:
            forms.alert("Need at least 2 values to create a gradient.")
            return
        apply_gradient(self._rows)
        self.values_grid.Items.Refresh()

    def save_scheme_click(self, sender, args):
        if not self._rows:
            forms.alert("Nothing to save - scan a category/parameter first.")
            return
        dlg = SaveFileDialog()
        dlg.Filter = "Color Scheme (*.cschn)|*.cschn"
        dlg.FileName = "Color Scheme.cschn"
        if dlg.ShowDialog() != DialogResult.OK:
            return
        try:
            with forms.ProgressBar(title="DeeSSelect - saving color scheme...", indeterminate=True):
                save_scheme(dlg.FileName, self._rows)
        except Exception as e:
            forms.alert("Could not save the color scheme: {0}".format(e))

    def load_scheme_click(self, sender, args):
        if not self._rows:
            forms.alert("Nothing to load onto - scan a category/parameter first.")
            return
        dlg = OpenFileDialog()
        dlg.Filter = "Color Scheme (*.cschn)|*.cschn"
        if dlg.ShowDialog() != DialogResult.OK:
            return
        try:
            with forms.ProgressBar(title="DeeSSelect - loading color scheme...", indeterminate=True):
                load_scheme(dlg.FileName, self._rows, by_value=True)
        except Exception as e:
            forms.alert("Could not load the color scheme: {0}".format(e))
            return
        self.values_grid.Items.Refresh()

    # ---------------- per-row recolor ----------------
    def swatch_click(self, sender, args):
        row = sender.Tag
        if row is None:
            return
        dlg = ColorDialog()
        r, g, b = row.color
        dlg.Color = DrawingColor.FromArgb(r, g, b)
        dlg.FullOpen = True
        if dlg.ShowDialog() == DialogResult.OK:
            c = dlg.Color
            row.color = (c.R, c.G, c.B)
            self.values_grid.Items.Refresh()

    # ---------------- grid checkbox select all/none (UI state only,
    # separate from the Revit-selection buttons below) ----------------
    def grid_check_all_click(self, sender, args):
        for r in self._rows:
            r.checked = True
        self.values_grid.Items.Refresh()
        self._update_status()

    def grid_check_none_click(self, sender, args):
        for r in self._rows:
            r.checked = False
        self.values_grid.Items.Refresh()
        self._update_status()

    # ---------------- Revit selection ----------------
    def select_checked_click(self, sender, args):
        checked_rows = [r for r in self._rows if r.checked]
        if not checked_rows:
            forms.alert("Check at least one value first.")
            return
        with forms.ProgressBar(title="DeeSSelect - selecting elements...", indeterminate=True):
            ids = elements_for_checked(self._rows)
            select_ids(self.uidoc, ids)
        self.status_tb.Text = "Selected {0} element(s) across {1} checked value(s).".format(
            ids.Count, len(checked_rows))

    def revit_select_all_click(self, sender, args):
        if not self._rows:
            return
        with forms.ProgressBar(title="DeeSSelect - selecting elements...", indeterminate=True):
            select_ids(self.uidoc, all_elements(self._rows))

    def revit_select_none_click(self, sender, args):
        with forms.ProgressBar(title="DeeSSelect - clearing selection...", indeterminate=True):
            clear_selection(self.uidoc)

    # ---------------- main actions ----------------
    def _channel_flags(self):
        apply_line = bool(self.line_color_cb.IsChecked)
        apply_fg = bool(self.fg_pattern_cb.IsChecked)
        apply_bg = bool(self.bg_pattern_cb.IsChecked)
        if not apply_line and not apply_fg and not apply_bg:
            apply_fg = True
        return apply_line, apply_fg, apply_bg

    def apply_colors_click(self, sender, args):
        cat = self.category_cb.SelectedItem
        if cat is None or not self._rows:
            forms.alert("Pick a category and parameter first.")
            return
        apply_line, apply_fg, apply_bg = self._channel_flags()
        with forms.ProgressBar(title="DeeSSelect - applying colors...", indeterminate=True):
            result = apply_colors(self.doc, self.view, cat, self._rows, apply_line, apply_fg, apply_bg)
        print_report("Apply Colors", result)

    def reset_colors_click(self, sender, args):
        cat = self.category_cb.SelectedItem
        if cat is None:
            forms.alert("Pick a category first.")
            return
        if not forms.alert(
                "Clear all color overrides in this view, and delete any View Filters "
                "DeeSSelect created for '{0}'? This cannot be undone from this dialog.".format(cat.name),
                title="DeeSSelect - Confirm", yes=True, no=True):
            return
        with forms.ProgressBar(title="DeeSSelect - resetting colors...", indeterminate=True):
            result = reset_colors(self.doc, self.view, cat)
        print_report("Reset Colors", result)

    def create_filters_click(self, sender, args):
        cat = self.category_cb.SelectedItem
        param = self.param_cb.SelectedItem
        if cat is None or param is None or not self._rows:
            forms.alert("Pick a category and parameter first.")
            return
        apply_line, apply_fg, apply_bg = self._channel_flags()
        with forms.ProgressBar(title="DeeSSelect - creating view filters...", indeterminate=True):
            result = create_view_filters(
                self.doc, self.view, cat, param, self._rows, apply_line, apply_fg, apply_bg)
        print_report("Create View Filters", result)

    def create_legend_click(self, sender, args):
        cat = self.category_cb.SelectedItem
        param = self.param_cb.SelectedItem
        if cat is None or param is None or not self._rows:
            forms.alert("Pick a category and parameter first.")
            return
        apply_line, apply_fg, apply_bg = self._channel_flags()
        try:
            with forms.ProgressBar(title="DeeSSelect - creating legend...", indeterminate=True):
                result = create_legend(self.doc, cat, param, self._rows, apply_line, apply_fg, apply_bg)
        except Exception as e:
            forms.alert("Could not create the legend: {0}".format(e), title="DeeSSelect")
            return
        print_report("Create Legend", result)

    def close_click(self, sender, args):
        self.Close()


# ==========================================================================
# Launch entry point (called by the DeeLazy home window)
# ==========================================================================
def launch(uiapp):
    if uiapp.ActiveUIDocument is None:
        forms.alert("Open a Revit project first.")
        return
    uidoc = uiapp.ActiveUIDocument
    doc = uidoc.Document
    view = get_splashable_view(doc, uidoc)
    if view is None:
        forms.alert(
            "DeeSSelect works on the active view only, and this view type doesn't "
            "support element color overrides (e.g. sheets, schedules, legends, or the "
            "Project/System Browser). Switch to a graphical view (plan, section, 3D, "
            "etc.) and run DeeSSelect again.",
            title="DeeSSelect")
        return
    window = DeeSSelectWindow(_XAML_FILE, uiapp, doc, uidoc, view)
    window.ShowDialog()


TOOL_INFO = {
    "id": "dee_sselect",
    "title": "DeeSSelect",
    "description": "Color elements in the active view by parameter value (like pyRevit's Color Splasher), plus select elements by checked value-groups.",
    "launch": launch,
}
