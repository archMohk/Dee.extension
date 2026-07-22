# -*- coding: utf-8 -*-
"""
DeeReLevel (LazyPack)
Safely change one or more Level elevations while automatically
preserving the physical (real-world) position of everything hosted or
constrained to those Levels - the goal is to move the LEVEL, not the
BUILDING. Unlike Revit's default behavior (which moves every element
that stores an "offset from this level" rigidly along with the level,
since it keeps that offset unchanged), DeeReLevel recomputes each
affected offset-type parameter so the absolute elevation of every wall
base, floor, roof, ceiling, hosted family, and MEP run stays exactly
where it was before the edit.

--------------------------------------------------------------------
Architecture note on "C# MVVM / DI" vs this codebase
--------------------------------------------------------------------
This tool was requested against a full C# Revit add-in spec (WPF
MVVM, ICommand/RelayCommand, a DI container, xUnit tests, a compiled
DLL + .addin manifest). Dee.extension is a pyRevit IronPython 2.7
extension instead - every existing tool in it (DeeDistributor,
DeeHealth, DeeCordiPoint, etc.) uses the same lightweight pattern:
a single `forms.WPFWindow` subclass as a thin code-behind controller,
Click="method_name" handlers in XAML, and plain Python objects as
pseudo-view-models bound to DataGrid columns. True C# MVVM
(INotifyPropertyChanged + ICommand + a DI container) isn't idiomatic
or necessary in IronPython/pyRevit, so this file follows the existing
codebase convention instead of faking C#-style ceremony that wouldn't
actually buy anything here. SOLID/"single responsibility" is instead
expressed as clearly-separated classes/functions below, each named
after the role it plays in the original spec (LevelScanner,
OffsetCalculator, ValidationEngine, etc.) even though they all live in
one file, matching this repo's convention of one script.py per tool
(see DeeDistributor.pushbutton/script.py, ~2800 lines, same style).

Pure, Revit-API-free math (the actual "keep absolute elevation fixed"
formula, slope preservation, duplicate-elevation detection) lives in
lib/relevel_tools.py, which has its own runnable unit-test suite
(`python relevel_tools.py`) - see that file for the core algorithm.

--------------------------------------------------------------------
How the core algorithm works
--------------------------------------------------------------------
For an element whose vertical position is "Level elevation + offset
parameter" (Base Offset, Sill Height, Height Offset From Level, an MEP
run's Offset, etc.), when the Level moves by `delta`:

    new_offset = old_offset - delta

keeps `level_elevation + offset` (the element's absolute elevation)
identical to what it was before. This is applied per matched
parameter, per element, driven by a data table (_CATEGORY_RULES
below) rather than one bespoke function per category - adding a new
category means adding a table row (open/closed principle), not new
branching logic.

For two-ended sloped runs (pipes/ducts), shifting the single Offset
parameter by -delta shifts the whole run's location curve rigidly in
Z, which preserves both endpoint elevations - and therefore the slope
between them - simultaneously. This is flagged as the single highest-
uncertainty assumption in the whole tool (see NEEDS LIVE VERIFICATION
below) since it depends on exactly how Revit's sloped-pipe/duct
placement is internally driven; the UI marks every sloped MEP element
with a "verify after applying" badge rather than asserting silent
certainty.

Elements that don't have an offset-type parameter DeeReLevel can find
(some structural connections, exotic in-place families) fall back to
a direct geometric translation via ElementTransformUtils.MoveElement
by (0, 0, -delta) - mathematically equivalent, just less semantically
tidy in schedules since the offset parameter itself won't reflect the
new position. Group members never get this fallback individually
(that would corrupt the group's internal geometry) - if a fallback
translation is ever needed on a grouped element, the whole Group
instance is translated once instead.

Hosting relationships (wall/floor/ceiling/roof/face/work-plane host)
are NEVER touched - DeeReLevel only ever edits Level and Offset-type
instance parameters, so a hosted family simply stays hosted exactly
as it was; there is no "re-host" step anywhere in this file.

--------------------------------------------------------------------
NEEDS LIVE-REVIT VERIFICATION (flagged explicitly, not silently
assumed correct - consistent with every other tool in this session)
--------------------------------------------------------------------
- Every BuiltInParameter name in _CATEGORY_RULES below - each has a
  string-name LookupParameter fallback, but the primary BuiltInParameter
  enum members should be spot-checked against a real project per
  category before trusting this on production models.
- Whether a single Offset-parameter shift on a SLOPED pipe/duct truly
  translates the whole run rigidly (vs. Revit recomputing one end from
  slope+length, which would silently break the assumption). Flagged in
  the UI on every sloped element found.
- Structural Framing (beams): no reliable single offset parameter was
  assumed here - DeeReLevel always uses the fallback geometric
  translation path for OST_StructuralFraming, since beam vertical
  position is primarily sketch/analytical-model driven. Verify this
  path preserves analytical model alignment before relying on it.
- Whether Parameter.Set() truly succeeds unmodified on Pinned elements
  across Revit 2024-2026 (assumed yes, based on Pinned blocking
  Move/Rotate/Delete but not parameter edits) - if wrong, the
  "Ignore pinned" / "Temporarily unpin" choice in the UI covers it.
- Rebar is treated as "follows its host automatically, no direct edit
  needed" - not independently verified against a live rebar-hosted
  model.
- Annotation categories (Spot Elevations, Dimensions, Detail
  Components, Reference Planes) are report-only (counted, never
  edited) on the assumption that since the underlying geometry they
  reference doesn't move, they don't need to either - verify against a
  project with Level-hosted reference planes specifically.
"""
import os
import math
import json
import time
import datetime

from pyrevit import forms, script
from Autodesk.Revit.DB import (
    FilteredElementCollector, BuiltInCategory, BuiltInParameter, ElementId,
    Level, Transaction, TransactionGroup, TransactionStatus, SubTransaction,
    ElementTransformUtils, XYZ, Group, FailureProcessingResult,
    IFailuresPreprocessor, FailureSeverity,
)

from System.Collections.Generic import List
import clr
clr.AddReference("System.Windows.Forms")
from System.Windows.Forms import SaveFileDialog, DialogResult, MessageBox

import xlsx_writer
import relevel_tools as rt

output = script.get_output()

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")


# ==========================================================================
# Units (same pattern as DeeCordiPoint/DeeGrid - internal feet <-> display)
# ==========================================================================
from Autodesk.Revit.DB import UnitUtils, UnitTypeId, SpecTypeId


def _length_unit_type_id(doc):
    try:
        return doc.GetUnits().GetFormatOptions(SpecTypeId.Length).GetUnitTypeId()
    except Exception:
        return UnitTypeId.Feet


def _to_display(doc, value_internal):
    uid = _length_unit_type_id(doc)
    try:
        return UnitUtils.ConvertFromInternalUnits(value_internal, uid)
    except Exception:
        return UnitUtils.ConvertFromInternalUnits(value_internal, UnitTypeId.Feet)


def _to_internal(doc, value_display):
    uid = _length_unit_type_id(doc)
    try:
        return UnitUtils.ConvertToInternalUnits(value_display, uid)
    except Exception:
        return UnitUtils.ConvertToInternalUnits(value_display, UnitTypeId.Feet)


_UNIT_ABBR = [
    (UnitTypeId.Millimeters, "mm"), (UnitTypeId.Centimeters, "cm"),
    (UnitTypeId.Meters, "m"), (UnitTypeId.Feet, "ft"),
    (UnitTypeId.FeetFractionalInches, "ft"), (UnitTypeId.FractionalInches, "in"),
    (UnitTypeId.Inches, "in"),
]


def _unit_abbreviation(doc):
    uid = _length_unit_type_id(doc)
    for u, abbr in _UNIT_ABBR:
        if u == uid:
            return abbr
    return "ft"


def _read_name(element):
    if element is None:
        return None
    try:
        n = element.Name
        if n:
            return n
    except Exception:
        pass
    return None


def _get_param(element, bip, fallback_name):
    """BuiltInParameter first, defensive LookupParameter-by-name second -
    the same pattern used throughout this codebase (DeeCordiPoint etc.)
    for parameters whose exact enum name needs live-Revit confirmation."""
    if element is None:
        return None
    if bip is not None:
        try:
            p = element.get_Parameter(bip)
            if p is not None:
                return p
        except Exception:
            pass
    if fallback_name:
        try:
            return element.LookupParameter(fallback_name)
        except Exception:
            return None
    return None


# ==========================================================================
# Category rule table - the data-driven engine.
# One row per category: which parameter names the Level reference and its
# offset, plus an optional independent top constraint (walls/columns).
# Adding a category = adding a row here, not new branch logic elsewhere
# (open/closed principle).
# ==========================================================================
class CategoryRule(object):
    def __init__(self, group, bic, label,
                 level_bip=None, level_name=None,
                 offset_bip=None, offset_name=None,
                 top_level_bip=None, top_level_name=None,
                 top_offset_bip=None, top_offset_name=None,
                 is_mep=False, report_only=False, fallback_translation=False):
        self.group = group                    # spec grouping: Architecture/Structure/MEP/Annotation
        self.bic = bic
        self.label = label
        self.level_bip = level_bip
        self.level_name = level_name
        self.offset_bip = offset_bip
        self.offset_name = offset_name
        self.top_level_bip = top_level_bip
        self.top_level_name = top_level_name
        self.top_offset_bip = top_offset_bip
        self.top_offset_name = top_offset_name
        self.is_mep = is_mep
        self.report_only = report_only
        # True => always use ElementTransformUtils.MoveElement instead of
        # trying to resolve an offset parameter (Structural Framing).
        self.fallback_translation = fallback_translation


def _bic(name):
    """getattr-based BuiltInCategory lookup - returns None instead of
    raising if `name` doesn't exist in this Revit version, so one wrong
    or renamed enum member degrades that single category rule instead
    of crashing the whole module at load time."""
    return getattr(BuiltInCategory, name, None)


def _bip(name):
    """Same defensive lookup for BuiltInParameter."""
    return getattr(BuiltInParameter, name, None)


_CATEGORY_RULES = [
    # ---- Architecture ----
    CategoryRule("Architecture", _bic("OST_Walls"), "Walls",
                 level_bip=_bip("WALL_BASE_CONSTRAINT"), level_name="Base Constraint",
                 offset_bip=_bip("WALL_BASE_OFFSET"), offset_name="Base Offset",
                 top_level_bip=_bip("WALL_HEIGHT_TYPE"), top_level_name="Top Constraint",
                 top_offset_bip=_bip("WALL_TOP_OFFSET"), top_offset_name="Top Offset"),
    CategoryRule("Architecture", _bic("OST_Floors"), "Floors",
                 level_bip=_bip("LEVEL_PARAM"), level_name="Level",
                 offset_bip=_bip("FLOOR_HEIGHTABOVELEVEL_PARAM"), offset_name="Height Offset From Level"),
    CategoryRule("Architecture", _bic("OST_Roofs"), "Roofs",
                 level_bip=_bip("LEVEL_PARAM"), level_name="Base Level",
                 offset_bip=_bip("ROOF_LEVEL_OFFSET_PARAM"), offset_name="Base Offset From Level"),
    CategoryRule("Architecture", _bic("OST_Ceilings"), "Ceilings",
                 level_bip=_bip("LEVEL_PARAM"), level_name="Level",
                 offset_bip=_bip("CEILING_HEIGHTABOVELEVEL_PARAM"), offset_name="Height Offset From Level"),
    CategoryRule("Architecture", _bic("OST_Doors"), "Doors",
                 level_bip=_bip("FAMILY_LEVEL_PARAM"), level_name="Level",
                 offset_bip=_bip("INSTANCE_SILL_HEIGHT_PARAM"), offset_name="Sill Height"),
    CategoryRule("Architecture", _bic("OST_Windows"), "Windows",
                 level_bip=_bip("FAMILY_LEVEL_PARAM"), level_name="Level",
                 offset_bip=_bip("INSTANCE_SILL_HEIGHT_PARAM"), offset_name="Sill Height"),
    CategoryRule("Architecture", _bic("OST_StairsRailing"), "Railings",
                 level_bip=_bip("FAMILY_LEVEL_PARAM"), level_name="Base Level",
                 offset_bip=_bip("INSTANCE_ELEVATION_PARAM"), offset_name="Base Offset"),
    CategoryRule("Architecture", _bic("OST_Stairs"), "Stairs",
                 level_bip=_bip("STAIRS_BASE_LEVEL_PARAM"), level_name="Base Level",
                 offset_bip=_bip("STAIRS_BASE_OFFSET"), offset_name="Base Offset"),
    CategoryRule("Architecture", _bic("OST_Rooms"), "Rooms",
                 level_bip=_bip("ROOM_LEVEL_ID"), level_name="Level",
                 offset_bip=_bip("ROOM_UPPER_OFFSET"), offset_name="Upper Offset",
                 report_only=True),
    CategoryRule("Architecture", _bic("OST_Areas"), "Areas",
                 level_bip=_bip("ROOM_LEVEL_ID"), level_name="Level",
                 report_only=True),
    CategoryRule("Architecture", _bic("OST_GenericModel"), "Generic Models",
                 level_bip=_bip("FAMILY_LEVEL_PARAM"), level_name="Level",
                 offset_bip=_bip("INSTANCE_ELEVATION_PARAM"), offset_name="Elevation from Level"),
    CategoryRule("Architecture", _bic("OST_Casework"), "Casework",
                 level_bip=_bip("FAMILY_LEVEL_PARAM"), level_name="Level",
                 offset_bip=_bip("INSTANCE_ELEVATION_PARAM"), offset_name="Elevation from Level"),
    CategoryRule("Architecture", _bic("OST_Furniture"), "Furniture",
                 level_bip=_bip("FAMILY_LEVEL_PARAM"), level_name="Level",
                 offset_bip=_bip("INSTANCE_ELEVATION_PARAM"), offset_name="Elevation from Level"),
    CategoryRule("Architecture", _bic("OST_SpecialityEquipment"), "Specialty Equipment",
                 level_bip=_bip("FAMILY_LEVEL_PARAM"), level_name="Level",
                 offset_bip=_bip("INSTANCE_ELEVATION_PARAM"), offset_name="Elevation from Level"),

    # ---- Structure ----
    CategoryRule("Structure", _bic("OST_Columns"), "Architectural Columns",
                 level_bip=_bip("FAMILY_BASE_LEVEL_PARAM"), level_name="Base Level",
                 offset_bip=_bip("FAMILY_BASE_LEVEL_OFFSET_PARAM"), offset_name="Base Offset",
                 top_level_bip=_bip("FAMILY_TOP_LEVEL_PARAM"), top_level_name="Top Level",
                 top_offset_bip=_bip("FAMILY_TOP_LEVEL_OFFSET_PARAM"), top_offset_name="Top Offset"),
    CategoryRule("Structure", _bic("OST_StructuralColumns"), "Structural Columns",
                 level_bip=_bip("FAMILY_BASE_LEVEL_PARAM"), level_name="Base Level",
                 offset_bip=_bip("FAMILY_BASE_LEVEL_OFFSET_PARAM"), offset_name="Base Offset",
                 top_level_bip=_bip("FAMILY_TOP_LEVEL_PARAM"), top_level_name="Top Level",
                 top_offset_bip=_bip("FAMILY_TOP_LEVEL_OFFSET_PARAM"), top_offset_name="Top Offset"),
    CategoryRule("Structure", _bic("OST_StructuralFraming"), "Structural Framing",
                 fallback_translation=True),
    CategoryRule("Structure", _bic("OST_StructuralFoundation"), "Foundations",
                 level_bip=_bip("FAMILY_LEVEL_PARAM"), level_name="Level",
                 offset_bip=_bip("INSTANCE_ELEVATION_PARAM"), offset_name="Elevation"),
    CategoryRule("Structure", _bic("OST_Rebar"), "Rebar", report_only=True),

    # ---- MEP (all treated as "simple rule", one Offset shift proven to
    #      preserve slope for the common single-reference-level case -
    #      see the module docstring's NEEDS LIVE VERIFICATION note) ----
    CategoryRule("MEP", _bic("OST_PipeCurves"), "Pipes",
                 level_bip=_bip("RBS_START_LEVEL_PARAM"), level_name="Reference Level",
                 offset_bip=_bip("RBS_OFFSET_PARAM"), offset_name="Offset", is_mep=True),
    CategoryRule("MEP", _bic("OST_PipeFitting"), "Pipe Fittings",
                 level_bip=_bip("RBS_START_LEVEL_PARAM"), level_name="Reference Level",
                 offset_bip=_bip("INSTANCE_ELEVATION_PARAM"), offset_name="Offset", is_mep=True),
    CategoryRule("MEP", _bic("OST_PipeAccessory"), "Pipe Accessories",
                 level_bip=_bip("FAMILY_LEVEL_PARAM"), level_name="Reference Level",
                 offset_bip=_bip("INSTANCE_ELEVATION_PARAM"), offset_name="Offset", is_mep=True),
    CategoryRule("MEP", _bic("OST_PipeInsulations"), "Pipe Insulation", report_only=True, is_mep=True),
    CategoryRule("MEP", _bic("OST_DuctCurves"), "Ducts",
                 level_bip=_bip("RBS_START_LEVEL_PARAM"), level_name="Reference Level",
                 offset_bip=_bip("RBS_OFFSET_PARAM"), offset_name="Offset", is_mep=True),
    CategoryRule("MEP", _bic("OST_DuctFitting"), "Duct Fittings",
                 level_bip=_bip("RBS_START_LEVEL_PARAM"), level_name="Reference Level",
                 offset_bip=_bip("INSTANCE_ELEVATION_PARAM"), offset_name="Offset", is_mep=True),
    CategoryRule("MEP", _bic("OST_DuctAccessory"), "Duct Accessories",
                 level_bip=_bip("FAMILY_LEVEL_PARAM"), level_name="Reference Level",
                 offset_bip=_bip("INSTANCE_ELEVATION_PARAM"), offset_name="Offset", is_mep=True),
    CategoryRule("MEP", _bic("OST_FlexPipeCurves"), "Flex Pipes",
                 level_bip=_bip("RBS_START_LEVEL_PARAM"), level_name="Reference Level",
                 offset_bip=_bip("RBS_OFFSET_PARAM"), offset_name="Offset", is_mep=True),
    CategoryRule("MEP", _bic("OST_FlexDuctCurves"), "Flex Ducts",
                 level_bip=_bip("RBS_START_LEVEL_PARAM"), level_name="Reference Level",
                 offset_bip=_bip("RBS_OFFSET_PARAM"), offset_name="Offset", is_mep=True),
    CategoryRule("MEP", _bic("OST_CableTray"), "Cable Trays",
                 level_bip=_bip("RBS_START_LEVEL_PARAM"), level_name="Reference Level",
                 offset_bip=_bip("RBS_OFFSET_PARAM"), offset_name="Offset", is_mep=True),
    CategoryRule("MEP", _bic("OST_CableTrayFitting"), "Cable Tray Fittings",
                 level_bip=_bip("RBS_START_LEVEL_PARAM"), level_name="Reference Level",
                 offset_bip=_bip("INSTANCE_ELEVATION_PARAM"), offset_name="Offset", is_mep=True),
    CategoryRule("MEP", _bic("OST_Conduit"), "Conduits",
                 level_bip=_bip("RBS_START_LEVEL_PARAM"), level_name="Reference Level",
                 offset_bip=_bip("RBS_OFFSET_PARAM"), offset_name="Offset", is_mep=True),
    CategoryRule("MEP", _bic("OST_ConduitFitting"), "Conduit Fittings",
                 level_bip=_bip("RBS_START_LEVEL_PARAM"), level_name="Reference Level",
                 offset_bip=_bip("INSTANCE_ELEVATION_PARAM"), offset_name="Offset", is_mep=True),
    CategoryRule("MEP", _bic("OST_MechanicalEquipment"), "Mechanical Equipment",
                 level_bip=_bip("FAMILY_LEVEL_PARAM"), level_name="Level",
                 offset_bip=_bip("INSTANCE_ELEVATION_PARAM"), offset_name="Offset", is_mep=True),
    CategoryRule("MEP", _bic("OST_PlumbingFixtures"), "Plumbing Fixtures",
                 level_bip=_bip("FAMILY_LEVEL_PARAM"), level_name="Level",
                 offset_bip=_bip("INSTANCE_ELEVATION_PARAM"), offset_name="Offset", is_mep=True),
    CategoryRule("MEP", _bic("OST_ElectricalFixtures"), "Electrical Fixtures",
                 level_bip=_bip("FAMILY_LEVEL_PARAM"), level_name="Level",
                 offset_bip=_bip("INSTANCE_ELEVATION_PARAM"), offset_name="Offset", is_mep=True),
    CategoryRule("MEP", _bic("OST_LightingFixtures"), "Lighting Fixtures",
                 level_bip=_bip("FAMILY_LEVEL_PARAM"), level_name="Level",
                 offset_bip=_bip("INSTANCE_ELEVATION_PARAM"), offset_name="Offset", is_mep=True),
    CategoryRule("MEP", _bic("OST_Sprinklers"), "Sprinklers",
                 level_bip=_bip("FAMILY_LEVEL_PARAM"), level_name="Level",
                 offset_bip=_bip("INSTANCE_ELEVATION_PARAM"), offset_name="Offset", is_mep=True),
    CategoryRule("MEP", _bic("OST_FireProtection"), "Fire Protection Equipment",
                 level_bip=_bip("FAMILY_LEVEL_PARAM"), level_name="Level",
                 offset_bip=_bip("INSTANCE_ELEVATION_PARAM"), offset_name="Offset", is_mep=True),

    # ---- Annotation (report-only: geometry they reference doesn't move,
    #      so they shouldn't need touching) ----
    CategoryRule("Annotation", _bic("OST_SpotElevations"), "Spot Elevations", report_only=True),
    CategoryRule("Annotation", _bic("OST_Dimensions"), "Dimensions", report_only=True),
    CategoryRule("Annotation", _bic("OST_DetailComponents"), "Detail Components", report_only=True),
    CategoryRule("Annotation", _bic("OST_CLines"), "Reference Planes", report_only=True),
]
# Drop any rule whose category didn't resolve in this Revit version
# (see _bic/_bip above) - keeps the tool running with reduced coverage
# instead of crashing outright on an unexpected BuiltInCategory name.
_CATEGORY_RULES = [r for r in _CATEGORY_RULES if r.bic is not None]


# ==========================================================================
# Data model
# ==========================================================================
class LevelRow(object):
    """Bound to the main DataGrid. `new_elevation_text` is the only
    two-way-editable field (typed by the user); everything else is
    recomputed by Preview/Scan and re-pushed into the grid."""
    def __init__(self, doc, level):
        self.doc = doc
        self.level = level
        self.element_id = level.Id
        self.name = _read_name(level) or "(unnamed level)"
        self.current_elev_internal = level.Elevation
        self.current_elev_text = "{0:.3f}".format(_to_display(doc, self.current_elev_internal))
        self._new_elevation_text = self.current_elev_text
        self.selected = False
        self.hosted_count = 0
        self.status = "Scanned"

    @property
    def new_elevation_text(self):
        return self._new_elevation_text

    @new_elevation_text.setter
    def new_elevation_text(self, value):
        self._new_elevation_text = value

    def new_elev_internal(self):
        try:
            return _to_internal(self.doc, float(self._new_elevation_text))
        except Exception:
            return self.current_elev_internal

    @property
    def diff_text(self):
        try:
            diff_internal = self.new_elev_internal() - self.current_elev_internal
            diff_display = _to_display(self.doc, diff_internal)
            return rt.format_signed(diff_display)
        except Exception:
            return "0.000"

    @property
    def hosted_count_text(self):
        return str(self.hosted_count)


class ElementActionRow(object):
    """One row per affected element, shown in Preview and written to the
    report - the "detailed log" the spec asks for (element id, category,
    level, parameter, old/new value, status)."""
    def __init__(self, level_name, category_label, element, param_name,
                 old_value_text, new_value_text, status, note=""):
        self.level_name = level_name
        self.category_label = category_label
        try:
            self.element_id_text = str(element.Id.IntegerValue if hasattr(element.Id, "IntegerValue") else element.Id)
        except Exception:
            self.element_id_text = "?"
        self.element = element
        self.param_name = param_name or ""
        self.old_value_text = old_value_text
        self.new_value_text = new_value_text
        self.status = status               # "ok" / "skipped" / "warning" / "error"
        self.note = note
        self.timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ==========================================================================
# LevelScanner
# ==========================================================================
def scan_levels(doc):
    """Every Level in the project, sorted by elevation - the low-level
    building block STEP 1 in the spec asks for."""
    levels = list(FilteredElementCollector(doc).OfClass(Level))
    levels.sort(key=lambda lv: lv.Elevation)
    return levels


# ==========================================================================
# ElementRelationshipAnalyzer / HostedElementScanner
# Combined into one pass for performance on large projects: one
# FilteredElementCollector per category (not per level x category),
# each element's level-reference parameter(s) checked once.
# ==========================================================================
def _param_level_id(element, bip, name):
    p = _get_param(element, bip, name)
    if p is None:
        return None
    try:
        val = p.AsElementId()
        if val is not None and val != ElementId.InvalidElementId:
            return val
    except Exception:
        pass
    return None


def hosting_kind(element):
    """Classifies a family instance's hosting relationship for
    reporting only (Wall/Ceiling/Floor/Roof/Face/Work Plane Hosted,
    or Level Hosted / Not hosted) - DeeReLevel never changes this, it
    only reports it, per spec: "Never detach hosted families."""
    host = None
    try:
        host = element.Host
    except Exception:
        host = None
    if host is None:
        try:
            if element.HostFace is not None:
                return "Face Hosted"
        except Exception:
            pass
        return "Level Hosted / Not Hosted"
    try:
        cat_name = host.Category.Name if host.Category is not None else ""
    except Exception:
        cat_name = ""
    for key in ("Wall", "Ceiling", "Floor", "Roof"):
        if key in cat_name:
            return "{0} Hosted".format(key)
    return "Hosted ({0})".format(cat_name or "?")


def build_relationship_index(doc, active_design_option_id, progress=None):
    """The key performance optimization for large projects (spec target:
    300,000+ elements): ONE FilteredElementCollector pass per category
    total, not one pass per (level x category) combination. Returns
    {level_id_int: [(element, rule, base_hit, top_hit), ...]}, built
    once per Scan/Apply and reused for every level's lookup."""
    index = {}
    total = len(_CATEGORY_RULES)
    for i, rule in enumerate(_CATEGORY_RULES):
        if progress is not None:
            progress.update(i, total)
        try:
            collector = FilteredElementCollector(doc).OfCategory(rule.bic).WhereElementIsNotElementType()
        except Exception:
            continue
        for el in collector:
            try:
                do = el.DesignOption
                if do is not None and active_design_option_id is not None and do.Id != active_design_option_id:
                    continue
            except Exception:
                pass

            if rule.fallback_translation:
                base_lvl_id = _param_level_id(el, None, "Reference Level")
                top_lvl_id = None
            else:
                base_lvl_id = _param_level_id(el, rule.level_bip, rule.level_name)
                top_lvl_id = None
                if rule.top_level_bip is not None or rule.top_level_name:
                    top_lvl_id = _param_level_id(el, rule.top_level_bip, rule.top_level_name)

            same_level = (base_lvl_id is not None and top_lvl_id is not None
                          and base_lvl_id.IntegerValue == top_lvl_id.IntegerValue)
            if base_lvl_id is not None:
                index.setdefault(base_lvl_id.IntegerValue, []).append(
                    (el, rule, True, bool(same_level)))
            if top_lvl_id is not None and not same_level:
                index.setdefault(top_lvl_id.IntegerValue, []).append((el, rule, False, True))
    return index


def scan_related_elements(level, index):
    return index.get(level.Id.IntegerValue, [])


def count_hosted_elements(level, index):
    return len(scan_related_elements(level, index))


# ==========================================================================
# OffsetCalculator / ConstraintManager / HostedFamilyManager (merged: all
# three amount to "adjust the matched parameter(s), never touch Host,
# redirect any fallback move to the whole Group")
# ==========================================================================
class OffsetCalculator(object):
    """Applies new_offset = old_offset - delta (see lib/relevel_tools.py)
    to a single element's matched parameter(s). Never raises out of
    `apply()` - every outcome becomes an ElementActionRow so one bad
    element can't abort an entire level's batch."""

    def __init__(self, doc, level_name, delta_internal, pinned_mode="unpin"):
        self.doc = doc
        self.level_name = level_name
        self.delta = delta_internal
        self.pinned_mode = pinned_mode   # "unpin" or "ignore"

    def apply(self, element, rule, base_hit, top_hit):
        if rule.report_only:
            return [ElementActionRow(self.level_name, rule.label, element, "",
                                      "", "", "skipped", "Report-only category - not modified")]
        if rule.fallback_translation:
            return [self._apply_fallback_translation(element, rule)]

        rows = []
        if base_hit and (rule.offset_bip is not None or rule.offset_name):
            rows.append(self._apply_param(element, rule.offset_bip, rule.offset_name, rule.label))
        if top_hit and (rule.top_offset_bip is not None or rule.top_offset_name):
            rows.append(self._apply_param(element, rule.top_offset_bip, rule.top_offset_name,
                                           rule.label + " (top)"))
        if not rows:
            rows.append(ElementActionRow(self.level_name, rule.label, element, "",
                                          "", "", "skipped", "No matching offset parameter found on this element"))
        return rows

    def _apply_param(self, element, bip, name, label):
        p = _get_param(element, bip, name)
        if p is None or p.IsReadOnly:
            return ElementActionRow(self.level_name, label, element, name or "?",
                                     "", "", "warning", "Parameter not found or read-only")
        try:
            old_val = p.AsDouble()
        except Exception:
            return ElementActionRow(self.level_name, label, element, name or "?",
                                     "", "", "warning", "Parameter is not a numeric offset")
        new_val = rt.adjust_offset(old_val, self.delta)
        try:
            p.Set(new_val)
        except Exception as e:
            return ElementActionRow(
                self.level_name, label, element, name or "?",
                "{0:.3f}".format(_to_display(self.doc, old_val)), "", "error", str(e))
        return ElementActionRow(
            self.level_name, label, element, name or "?",
            "{0:.3f}".format(_to_display(self.doc, old_val)),
            "{0:.3f}".format(_to_display(self.doc, new_val)), "ok")

    def _apply_fallback_translation(self, element, rule):
        """No offset parameter mapped for this category (Structural
        Framing) - counteract the level's move with a direct geometric
        translation instead. Group members are redirected to move the
        whole Group instance once, never the member alone (moving a
        single member out of formation corrupts the group)."""
        try:
            group_id = element.GroupId
        except Exception:
            group_id = ElementId.InvalidElementId
        target = element
        note = "Moved directly (no offset parameter mapped for this category)"
        if group_id is not None and group_id != ElementId.InvalidElementId:
            try:
                target = self.doc.GetElement(group_id)
                note = "Moved the whole Group instance (this element is a group member) instead of the element alone"
            except Exception:
                target = element

        try:
            was_pinned = target.Pinned
        except Exception:
            was_pinned = False
        if was_pinned and self.pinned_mode == "ignore":
            return ElementActionRow(self.level_name, rule.label, element, "(location)",
                                     "", "", "skipped", "Pinned - ignored per user setting")
        try:
            if was_pinned:
                target.Pinned = False
            ElementTransformUtils.MoveElement(self.doc, target.Id, XYZ(0, 0, -self.delta))
            if was_pinned:
                target.Pinned = True
        except Exception as e:
            return ElementActionRow(self.level_name, rule.label, element, "(location)",
                                     "", "", "error", "{0} - {1}".format(note, e))
        return ElementActionRow(self.level_name, rule.label, element, "(location)",
                                 "", "{0:.3f}".format(_to_display(self.doc, -self.delta)), "ok", note)


# ==========================================================================
# ValidationEngine
# ==========================================================================
class ValidationEngine(object):
    """Conservative, best-effort pre-apply checks (not an exhaustive
    Revit-internal validator). A non-empty `errors` list means Apply
    must refuse to proceed until the user fixes the input."""

    @staticmethod
    def validate(all_level_rows, selected_rows):
        errors = []
        warnings = []

        for row in selected_rows:
            try:
                float(row.new_elevation_text)
            except Exception:
                errors.append("'{0}': '{1}' is not a valid number.".format(row.name, row.new_elevation_text))

        selected_ids = set(r.element_id.IntegerValue for r in selected_rows)
        proposed = []
        for row in all_level_rows:
            if row.element_id.IntegerValue in selected_ids:
                proposed.append((row.name, row.new_elev_internal()))
            else:
                proposed.append((row.name, row.current_elev_internal))
        for name_a, name_b, _elev in rt.duplicate_elevation_conflicts(proposed):
            warnings.append("'{0}' and '{1}' would end up at the same elevation.".format(name_a, name_b))

        for row in selected_rows:
            delta = rt.compute_delta(row.current_elev_internal, row.new_elev_internal())
            if abs(delta) < 1e-9:
                continue
            if row.hosted_count > 0 and abs(delta) > 1e6:
                # Sanity guard against an obviously-wrong pasted value
                # (huge internal-unit delta), not a real design case.
                errors.append("'{0}': the new elevation looks implausibly large - please double-check it.".format(row.name))

        return errors, warnings


# ==========================================================================
# FailureProcessor - suppresses known-safe warnings so a large batch
# doesn't pop a modal dialog per element; requests rollback the moment
# a real Error-severity failure appears (never lets a level partially
# apply with silent corruption).
# ==========================================================================
class FailureProcessor(IFailuresPreprocessor):
    def __init__(self, error_log):
        self._log = error_log

    def PreprocessFailures(self, failuresAccessor):
        try:
            failures = list(failuresAccessor.GetFailureMessages())
        except Exception:
            return FailureProcessingResult.Continue
        had_error = False
        for f in failures:
            try:
                severity = f.GetSeverity()
            except Exception:
                continue
            if severity == FailureSeverity.Error:
                had_error = True
                try:
                    self._log.append(f.GetDescriptionText())
                except Exception:
                    pass
            else:
                try:
                    failuresAccessor.DeleteWarning(f)
                except Exception:
                    pass
        if had_error:
            return FailureProcessingResult.ProceedWithRollBack
        return FailureProcessingResult.Continue


# ==========================================================================
# ProgressService - thin wrapper around pyrevit.forms.ProgressBar, kept
# as its own class (matching the spec's suggested architecture) so
# callers don't need to know pyRevit's specific progress-bar API.
# ==========================================================================
class ProgressService(object):
    def __init__(self, title):
        self.title = title
        self._pb = None

    def __enter__(self):
        self._pb = forms.ProgressBar(title=self.title, cancellable=True)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            self._pb.close()
        except Exception:
            pass
        return False

    def update(self, current, total):
        try:
            self._pb.update_progress(current, total)
        except Exception:
            pass

    @property
    def cancelled(self):
        try:
            return self._pb.cancelled
        except Exception:
            return False


# ==========================================================================
# LevelUpdater / TransactionManager (merged: the spec's TransactionManager
# only exists to serve LevelUpdater's apply workflow, so they're one
# class here) - one TransactionGroup for the whole run, one Transaction
# per level so a failure on one level doesn't force rolling back edits
# already committed for a different, unrelated level.
# ==========================================================================
class LevelUpdater(object):
    def __init__(self, doc, active_design_option_id, pinned_mode="unpin"):
        self.doc = doc
        self.active_design_option_id = active_design_option_id
        self.pinned_mode = pinned_mode
        self.action_rows = []
        self.errors = []

    def apply(self, level_rows_selected, progress=None):
        tg = TransactionGroup(self.doc, "DeeReLevel - Update Levels")
        tg.Start()
        total = len(level_rows_selected)
        try:
            index = build_relationship_index(self.doc, self.active_design_option_id)
            for i, row in enumerate(level_rows_selected):
                if progress is not None:
                    progress.update(i, total)
                    if progress.cancelled:
                        tg.RollBack()
                        return False
                self._apply_one_level(row, index)
            tg.Assimilate()
            return True
        except Exception as e:
            self.errors.append(str(e))
            try:
                tg.RollBack()
            except Exception:
                pass
            return False

    def _apply_one_level(self, row, index):
        level_name = row.name
        new_elev = row.new_elev_internal()
        delta = rt.compute_delta(row.current_elev_internal, new_elev)
        row.old_elev_text = row.current_elev_text
        if abs(delta) < 1e-9:
            row.status = "No change"
            row.applied_new_elev_text = row.current_elev_text
            return

        related = scan_related_elements(row.level, index)
        calc = OffsetCalculator(self.doc, level_name, delta, self.pinned_mode)

        t = Transaction(self.doc, "DeeReLevel - Update Level '{0}'".format(level_name))
        options = t.GetFailureHandlingOptions()
        options.SetFailuresPreprocessor(FailureProcessor(self.errors))
        t.SetFailureHandlingOptions(options)
        t.Start()
        try:
            row.level.Elevation = new_elev
            self.doc.Regenerate()
            for element, rule, base_hit, top_hit in related:
                self.action_rows.extend(calc.apply(element, rule, base_hit, top_hit))
            status = t.Commit()
            if status != TransactionStatus.Committed:
                row.status = "Failed ({0})".format(status)
            else:
                row.status = "Applied ({0} elements)".format(len(related))
                row.current_elev_internal = new_elev
                row.current_elev_text = "{0:.3f}".format(_to_display(self.doc, new_elev))
                row.applied_new_elev_text = row.current_elev_text
        except Exception as e:
            try:
                if not t.HasEnded():
                    t.RollBack()
            except Exception:
                pass
            row.status = "Error: {0}".format(e)
            self.errors.append("{0}: {1}".format(level_name, e))


# ==========================================================================
# ReportGenerator - Excel (2 sheets: Levels, Affected Elements) + JSON,
# matching the spec's reporting requirements (old/new elevation, updated
# parameters, affected/skipped elements, errors, warnings, execution time).
# ==========================================================================
class ReportGenerator(object):
    def __init__(self, level_rows, action_rows, errors, warnings, execution_seconds):
        self.level_rows = level_rows
        self.action_rows = action_rows
        self.errors = errors
        self.warnings = warnings
        self.execution_seconds = execution_seconds

    def _level_sheet_rows(self):
        rows = []
        for r in self.level_rows:
            old_text = getattr(r, "old_elev_text", None) or r.current_elev_text
            new_text = getattr(r, "applied_new_elev_text", None) or r.new_elevation_text
            rows.append([r.name, old_text, new_text, r.diff_text, r.hosted_count_text, r.status])
        return rows

    def _action_sheet_rows(self):
        rows = []
        for a in self.action_rows:
            rows.append([a.level_name, a.category_label, a.element_id_text, a.param_name,
                         a.old_value_text, a.new_value_text, a.status, a.note, a.timestamp])
        return rows

    def export_excel(self, path):
        sheets = [
            {
                "name": "Levels",
                "fingerprint": "DeeReLevel-Levels-v1",
                "headers": ["Level", "Old Elevation", "New Elevation", "Difference", "Hosted Elements", "Status"],
                "col_widths": [24, 16, 16, 14, 16, 24],
                "rows": self._level_sheet_rows(),
            },
            {
                "name": "Affected Elements",
                "fingerprint": "DeeReLevel-Elements-v1",
                "headers": ["Level", "Category", "Element Id", "Parameter", "Old Value", "New Value",
                            "Status", "Note", "Timestamp"],
                "col_widths": [20, 20, 12, 22, 12, 12, 10, 40, 18],
                "rows": self._action_sheet_rows(),
            },
        ]
        if self.errors or self.warnings:
            err_rows = [["ERROR", e] for e in self.errors] + [["WARNING", w] for w in self.warnings]
            sheets.append({
                "name": "Errors and Warnings",
                "fingerprint": "DeeReLevel-Errors-v1",
                "headers": ["Type", "Message"],
                "col_widths": [12, 90],
                "rows": err_rows,
            })
        xlsx_writer.write_multisheet_xlsx(path, sheets)

    def export_json(self, path):
        data = {
            "tool": "DeeReLevel",
            "generated": datetime.datetime.now().isoformat(),
            "execution_seconds": self.execution_seconds,
            "levels": [
                {
                    "name": r.name,
                    "old_elevation": getattr(r, "old_elev_text", None) or r.current_elev_text,
                    "new_elevation": getattr(r, "applied_new_elev_text", None) or r.new_elevation_text,
                    "difference": r.diff_text,
                    "hosted_elements": r.hosted_count,
                    "status": r.status,
                }
                for r in self.level_rows
            ],
            "affected_elements": [
                {
                    "level": a.level_name, "category": a.category_label, "element_id": a.element_id_text,
                    "parameter": a.param_name, "old_value": a.old_value_text, "new_value": a.new_value_text,
                    "status": a.status, "note": a.note, "timestamp": a.timestamp,
                }
                for a in self.action_rows
            ],
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)


class LogEntryRow(object):
    """One row in the error/warning log grid."""
    def __init__(self, kind, message):
        self.kind = kind
        self.message = message


# ==========================================================================
# Window (the UI controller - see the module docstring's note on why this
# is a forms.WPFWindow code-behind rather than literal C# MVVM/DI)
# ==========================================================================
class DeeReLevelWindow(forms.WPFWindow):
    def __init__(self, xaml_file, doc, uidoc):
        forms.WPFWindow.__init__(self, xaml_file)
        self.doc = doc
        self.uidoc = uidoc
        # Design Option filtering is wired end-to-end (build_relationship_index
        # honors it) but no picker UI is exposed yet - None means "don't
        # filter by design option", matching the simplest safe default.
        self.active_design_option_id = None
        self._all_levels = []
        self._action_rows = []
        self._errors = []
        self._warnings = []
        self._status_lines = []
        self._dark = False
        self._last_execution_seconds = 0.0
        # Set AFTER all the above, not via XAML's SelectedIndex="0": WPF
        # fires SelectionChanged the moment a ComboBox's initial selection
        # is established, and doing that from XAML happens mid-way through
        # forms.WPFWindow.__init__() itself (while load_xaml() is still
        # running) - before this method has assigned self._all_levels etc.,
        # which crashed with "object has no attribute '_all_levels'".
        # Setting it here, after those assignments, fires the same event
        # safely.
        self.status_filter_cb.SelectedIndex = 0
        self._log("Ready. Click Scan Project to begin.")

    # ---------------- logging ----------------
    def _log(self, message):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self._status_lines.append("[{0}] {1}".format(ts, message))
        try:
            self.status_tb.Text = "\n".join(self._status_lines[-500:])
            self.status_tb.ScrollToEnd()
        except Exception:
            pass

    def _refresh_error_grid(self):
        rows = ([LogEntryRow("Error", e) for e in self._errors] +
                [LogEntryRow("Warning", w) for w in self._warnings])
        self.error_grid.ItemsSource = None
        self.error_grid.ItemsSource = rows

    # ---------------- levels grid: search / filter / select ----------------
    def _status_filter_value(self):
        try:
            item = self.status_filter_cb.SelectedItem
            if item is None:
                return "All"
            return str(getattr(item, "Content", item))
        except Exception:
            return "All"

    def _visible_levels(self):
        term = ""
        try:
            term = (self.search_tb.Text or "").strip().lower()
        except Exception:
            pass
        status_filter = self._status_filter_value()

        # Defensive: a XAML-driven control event (ComboBox/DataGrid initial
        # selection, etc.) can in principle fire before __init__ finishes
        # assigning instance attributes - see the SelectedIndex note in
        # __init__. Fall back to an empty list rather than crashing.
        rows = getattr(self, "_all_levels", None) or []
        if term:
            rows = [r for r in rows if term in r.name.lower()]
        if status_filter == "Changed":
            rows = [r for r in rows if abs(r.new_elev_internal() - r.current_elev_internal) > 1e-9]
        elif status_filter == "Applied":
            rows = [r for r in rows if r.status.startswith("Applied")]
        elif status_filter == "Errors":
            rows = [r for r in rows if r.status.startswith("Error") or r.status.startswith("Failed")]
        return rows

    def _refresh_grid(self):
        rows = self._visible_levels()
        self.levels_grid.ItemsSource = None
        self.levels_grid.ItemsSource = rows

    def search_changed(self, sender, args):
        self._refresh_grid()

    def filter_changed(self, sender, args):
        self._refresh_grid()

    def select_all_click(self, sender, args):
        for r in self._visible_levels():
            r.selected = True
        self._refresh_grid()

    def select_none_click(self, sender, args):
        for r in self._all_levels:
            r.selected = False
        self._refresh_grid()

    def _pinned_mode(self):
        try:
            item = self.pinned_mode_cb.SelectedItem
            tag = getattr(item, "Tag", None)
            if tag:
                return str(tag)
        except Exception:
            pass
        return "ignore"

    # ---------------- Scan Project ----------------
    def scan_click(self, sender, args):
        self._log("Scanning levels and indexing related elements...")
        levels = scan_levels(self.doc)
        with ProgressService("DeeReLevel - Scanning Project") as prog:
            index = build_relationship_index(self.doc, self.active_design_option_id, prog)
            rows = []
            for lvl in levels:
                row = LevelRow(self.doc, lvl)
                row.hosted_count = count_hosted_elements(lvl, index)
                rows.append(row)
        self._all_levels = rows
        self._refresh_grid()
        self._log("Scan complete: {0} level(s), {1} categories indexed.".format(
            len(rows), len(_CATEGORY_RULES)))

    # ---------------- Preview Changes ----------------
    def preview_click(self, sender, args):
        selected = [r for r in self._all_levels if r.selected]
        if not selected:
            forms.alert("Select at least one Level first (tick its Select checkbox).")
            return
        errors, warnings = ValidationEngine.validate(self._all_levels, selected)
        self._errors = list(errors)
        self._warnings = list(warnings)
        self._refresh_error_grid()
        if errors:
            self._log("Preview blocked - {0} validation error(s). See the Log & Errors tab.".format(len(errors)))
            forms.alert("Fix these before previewing/applying:\n\n" + "\n".join(errors))
            return

        self._log("Building preview for {0} level(s)...".format(len(selected)))
        preview_rows = []
        with ProgressService("DeeReLevel - Building Preview") as prog:
            index = build_relationship_index(self.doc, self.active_design_option_id, prog)
            for row in selected:
                delta = rt.compute_delta(row.current_elev_internal, row.new_elev_internal())
                related = scan_related_elements(row.level, index)
                row.hosted_count = len(related)
                for element, rule, base_hit, top_hit in related:
                    if rule.report_only:
                        status, note, param_name = "skipped", "Report-only category - not modified", ""
                        new_txt = ""
                    elif rule.fallback_translation:
                        status = "warning"
                        note = "Will be moved directly (no offset parameter mapped) - verify after applying"
                        param_name = "(location)"
                        new_txt = rt.format_signed(_to_display(self.doc, -delta))
                    else:
                        status, note = "ok", ""
                        if rule.is_mep:
                            note = "MEP run - slope preserved by shifting the whole run; verify after applying"
                        param_name = rule.offset_name or ""
                        new_txt = ""
                    preview_rows.append(ElementActionRow(
                        row.name, rule.label, element, param_name, "", new_txt, status, note))
        self._action_rows = preview_rows
        self.preview_grid.ItemsSource = None
        self.preview_grid.ItemsSource = preview_rows
        ok_count = sum(1 for r in preview_rows if r.status == "ok")
        warn_count = sum(1 for r in preview_rows if r.status == "warning")
        skip_count = sum(1 for r in preview_rows if r.status == "skipped")
        self.preview_summary_tb.Text = (
            "{0} element(s) across {1} level(s): {2} will be adjusted automatically, "
            "{3} need manual verification after applying, {4} report-only/skipped.".format(
                len(preview_rows), len(selected), ok_count, warn_count, skip_count))
        if warnings:
            self._log("{0} warning(s) found - see the Log & Errors tab.".format(len(warnings)))
        self._refresh_grid()
        self._log("Preview ready - review the Preview tab, then click Apply Changes.")

    # ---------------- Apply Changes ----------------
    def apply_click(self, sender, args):
        selected = [r for r in self._all_levels if r.selected]
        if not selected:
            forms.alert("Select at least one Level first (tick its Select checkbox).")
            return
        errors, warnings = ValidationEngine.validate(self._all_levels, selected)
        if errors:
            forms.alert("Fix these before applying:\n\n" + "\n".join(errors))
            return

        names = ", ".join(r.name for r in selected)
        if not forms.alert(
                "Apply new elevations to {0} level(s): {1}?\n\n"
                "DeeReLevel will adjust every matched offset parameter so hosted "
                "elements, MEP runs, and constraints keep their current real-world "
                "position. Review the Preview tab first if you haven't already.".format(
                    len(selected), names),
                title="DeeReLevel - Confirm Apply", yes=True, no=True):
            return

        start = time.time()
        updater = LevelUpdater(self.doc, self.active_design_option_id, self._pinned_mode())
        with ProgressService("DeeReLevel - Applying Changes") as prog:
            ok = updater.apply(selected, prog)
        self._last_execution_seconds = time.time() - start

        self._action_rows = updater.action_rows
        self._errors.extend(updater.errors)
        self._refresh_error_grid()
        self.preview_grid.ItemsSource = None
        self.preview_grid.ItemsSource = self._action_rows
        self._refresh_grid()

        if ok:
            self._log("Apply finished in {0:.1f}s - {1} element action(s) logged.".format(
                self._last_execution_seconds, len(self._action_rows)))
            forms.alert("Applied changes to {0} level(s) in {1:.1f}s. Use Export Report for full details.".format(
                len(selected), self._last_execution_seconds), title="DeeReLevel")
        else:
            self._log("Apply rolled back due to an error - see the Log & Errors tab.")
            forms.alert("Apply failed and was rolled back - nothing was changed. See the Log & Errors tab.",
                         title="DeeReLevel")

    # ---------------- Export Report ----------------
    def export_click(self, sender, args):
        if not self._all_levels:
            forms.alert("Scan Project first - nothing to export yet.")
            return
        dlg = SaveFileDialog()
        dlg.Filter = "Excel Workbook (*.xlsx)|*.xlsx|JSON (*.json)|*.json"
        dlg.FileName = "DeeReLevel_Report.xlsx"
        if dlg.ShowDialog() != DialogResult.OK:
            return
        report = ReportGenerator(self._all_levels, self._action_rows, self._errors, self._warnings,
                                  self._last_execution_seconds)
        try:
            if dlg.FileName.lower().endswith(".json"):
                report.export_json(dlg.FileName)
            else:
                report.export_excel(dlg.FileName)
        except Exception as e:
            forms.alert("Could not export: {0}".format(e))
            return
        MessageBox.Show("Report exported to:\n{0}".format(dlg.FileName), "DeeReLevel")

    # ---------------- Dark mode / Cancel ----------------
    def toggle_dark_mode_click(self, sender, args):
        from System.Windows.Media import SolidColorBrush, Color
        self._dark = not self._dark
        if self._dark:
            bg, fg = Color.FromRgb(32, 34, 38), Color.FromRgb(228, 228, 228)
        else:
            bg, fg = Color.FromRgb(255, 255, 255), Color.FromRgb(0, 0, 0)
        try:
            self.Background = SolidColorBrush(bg)
            self.root_panel.Background = SolidColorBrush(bg)
            self.status_tb.Background = SolidColorBrush(bg)
            self.status_tb.Foreground = SolidColorBrush(fg)
        except Exception:
            pass

    def cancel_click(self, sender, args):
        self.Close()

    def close_click(self, sender, args):
        self.Close()


def main():
    uidoc = __revit__.ActiveUIDocument
    doc = uidoc.Document
    window = DeeReLevelWindow(_XAML_FILE, doc, uidoc)
    window.ShowDialog()


main()
