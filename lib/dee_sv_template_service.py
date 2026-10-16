# -*- coding: utf-8 -*-
"""
dee_sv_template_service
Backs DeeSVTemplate.pushbutton - applies a named, per-category visibility/
graphics PRESET (hide / halftone / line weight) to a set of views, lets the
user save their own new presets per Package, and turns a configured view
into a real Revit View Template.

--------------------------------------------------------------------
Preset shape - literal per-category control, not DeeMono's hue engine
--------------------------------------------------------------------
lib/dee_mono_service.py already proves the low-level Revit API calls a
preset needs (view.SetCategoryOverrides/SetCategoryHidden inside one
Transaction) - reused directly here (view_categories, solid_fill_pattern_id,
create_view_template are all imported from there, not reimplemented).
But DeeMono's own PRESETS are abstract hue/intensity/bias knobs tuned for
a nice-looking recolored render, not "Walls: halftone, Grids: hidden" -
the literal, differentiated per-category instruction this tool actually
needs. So presets here are a plain, explicit dict instead:

    {
        "name": "Preset 1 - ...",
        "categories": {
            "Walls": {"halftone": True},
            "Structural Framing": {"line_weight": 1},
            "Grids": {"hidden": True},
        },
    }

Only categories that need a NON-DEFAULT treatment are listed - applying a
preset never touches any category it doesn't mention.

`halftone` uses OverrideGraphicSettings.SetHalftone(bool) - Revit's own
literal per-category Halftone checkbox (the same one in the Visibility/
Graphics dialog) - NEEDS LIVE-REVIT VERIFICATION, since no file in this
repo has called it before (dee_mono_service.py fakes a "halftone-like"
look a different way, via transparency + colour, tuned for its own
recolor effect rather than reproducing Revit's literal Halftone toggle).

--------------------------------------------------------------------
Hand-authored examples vs. the rule-based generator
--------------------------------------------------------------------
The live tool's own user pushed back on the original plan (hand-author
only a few example Packages, leave the other 130+ for the firm to
author themselves via Save New Preset) and asked for every Package to
have presets now. Individually hand-authoring 138 Packages x 3+ presets
each against real BIM standards is still not something this session has
an authoritative basis for - so instead of fabricating 400+ one-off
opinions, EXAMPLE_PRESETS below keeps the 3 original fully-worked,
hand-considered Packages (proving the mechanism), and
generate_default_presets() below provides every OTHER Package with 3
systematically generated presets from one shared, auditable rule set:
which categories "belong" to a Discipline (DISCIPLINE_CATEGORIES),
which to lean on for a Package's own stated focus (_KEYWORD_EMPHASIS,
matched against the Package name). This is a sensible STARTING POINT
applied consistently everywhere, not a claim of correctness against any
specific firm's real standard - exactly what "Save New Preset" (and
simply editing the rule tables below) remains for: tuning the result
against real standards, in one place, not retyping 400+ presets by hand.

Presets are keyed by (discipline, package), not package name alone - a
live check found one real name collision ("Furniture Installation"
exists under both Interior and Landscape) that a package-name-only key
would have silently cross-contaminated.
--------------------------------------------------------------------
"""
import deew_settings
import dee_mono_service as mono

_USER_PRESETS_TOOL_NAME = "dee_sv_template_presets"


def _key(discipline, package):
    return "{0}||{1}".format(discipline, package)


# ==========================================================================
# Rule-based generator - every Package not in EXAMPLE_PRESETS gets 3
# presets built from these tables.
# ==========================================================================
DISCIPLINE_CATEGORIES = {
    "Architecture": [
        "Walls", "Curtain Walls", "Curtain Systems", "Curtain Wall Mullions",
        "Doors", "Windows", "Floors", "Ceilings", "Roofs", "Stairs",
        "Railings", "Ramps", "Casework", "Furniture", "Furniture Systems",
        "Specialty Equipment", "Rooms", "Room Tags", "Door Tags", "Window Tags",
    ],
    "Interior": [
        "Furniture", "Furniture Systems", "Casework", "Specialty Equipment",
        "Ceilings", "Walls", "Floors", "Room Tags",
    ],
    "Structure": [
        "Structural Columns", "Structural Framing", "Structural Foundations",
        "Structural Connections", "Structural Stiffeners", "Structural Trusses",
        "Structural Rebar", "Structural Area Reinforcement",
        "Structural Path Reinforcement", "Grids",
    ],
    "Mechanical": [
        "Mechanical Equipment", "Duct Systems", "Duct Fittings",
        "Duct Accessories", "Air Terminals", "Flex Ducts",
        "Duct Insulations", "Duct Linings",
    ],
    "Plumbing": [
        "Plumbing Fixtures", "Pipes", "Pipe Fittings", "Pipe Accessories",
        "Pipe Insulations", "Flex Pipes",
    ],
    "Fire Protection": ["Sprinklers", "Fire Protection", "Fire Alarm Devices"],
    "Electrical": [
        "Electrical Equipment", "Electrical Fixtures", "Lighting Fixtures",
        "Lighting Devices", "Conduits", "Conduit Fittings", "Cable Tray",
        "Cable Tray Fittings", "Communication Devices", "Data Devices",
    ],
    "ELV": [
        "Communication Devices", "Data Devices", "Security Devices",
        "Nurse Call Devices", "Telephone Devices", "Fire Alarm Devices",
    ],
    "Landscape": ["Planting", "Site", "Parking", "Topography", "Entourage"],
    # "General" deliberately has no entry - cover sheets/drawing indexes/
    # legends/schedules are administrative, not real model views with
    # categories worth overriding.
}

_ALL_KNOWN_CATEGORIES = sorted(set(
    cat for cats in DISCIPLINE_CATEGORIES.values() for cat in cats))

# (keywords to look for in the Package name, categories to emphasize for
# Preset 3) - first match wins, checked in order.
_KEYWORD_EMPHASIS = [
    (["ceiling"], ["Ceilings", "Lighting Fixtures"]),
    (["furniture", "fit-out", "fit out", "millwork", "joinery", "ffe"],
     ["Furniture", "Furniture Systems", "Casework"]),
    (["finish", "tile", "stone"], ["Walls", "Floors", "Ceilings"]),
    (["elevation", "facade", "cladding"], ["Walls", "Windows", "Doors"]),
    (["section"], ["Walls", "Structural Framing", "Structural Columns"]),
    (["foundation", "pile", "footing"], ["Structural Foundations"]),
    (["column"], ["Structural Columns"]),
    (["framing", "slab", "formwork", "precast", "steel"], ["Structural Framing"]),
    (["reinforcement", "rebar", "post-tensioning", "tendon"], ["Structural Rebar"]),
    (["duct", "ventilation", "air distribution", "chilled"],
     ["Duct Systems", "Air Terminals", "Mechanical Equipment"]),
    (["pipe", "water", "drainage", "plumbing", "waterproofing", "bathroom"],
     ["Pipes", "Plumbing Fixtures"]),
    (["sprinkler", "fire pump", "fire alarm", "smoke", "standpipe", "clean-agent"],
     ["Sprinklers", "Fire Alarm Devices"]),
    (["power", "panel", "switchgear", "cable", "generator", "earthing", "containment"],
     ["Electrical Equipment", "Cable Tray", "Conduits"]),
    (["lighting"], ["Lighting Fixtures", "Lighting Devices"]),
    (["data", "telecom", "rack", "camera", "access control", "audiovisual", "information technology"],
     ["Data Devices", "Communication Devices", "Security Devices"]),
    (["planting", "softscape", "irrigation", "tree"], ["Planting"]),
    (["hardscape", "paving"], ["Site"]),
]


def _owned_categories(discipline):
    return DISCIPLINE_CATEGORIES.get(discipline, [])


def _other_categories(discipline):
    owned = set(_owned_categories(discipline))
    return [c for c in _ALL_KNOWN_CATEGORIES if c not in owned]


def _emphasis_for(discipline, package):
    pkg_lower = package.lower()
    for keywords, cats in _KEYWORD_EMPHASIS:
        if any(k in pkg_lower for k in keywords):
            return cats
    return _owned_categories(discipline)[:3]


def _preset_clean(discipline):
    categories = {}
    for cat in _other_categories(discipline):
        categories[cat] = {"halftone": True}
    for cat in _owned_categories(discipline):
        categories[cat] = {"line_weight": 4}
    return {"name": "Preset 1 - {0} Focus (Clean)".format(discipline), "categories": categories}


def _preset_coordination(discipline):
    structure_cats = set(DISCIPLINE_CATEGORIES.get("Structure", []))
    categories = {}
    for cat in _other_categories(discipline):
        if discipline != "Structure" and cat in structure_cats:
            categories[cat] = {"line_weight": 1}
        else:
            categories[cat] = {"halftone": True}
    for cat in _owned_categories(discipline):
        categories[cat] = {"line_weight": 3}
    return {"name": "Preset 2 - Coordination", "categories": categories}


def _preset_focus(discipline, package):
    emphasis = set(_emphasis_for(discipline, package))
    categories = {}
    for cat in _other_categories(discipline):
        categories[cat] = {"line_weight": 2} if cat in emphasis else {"hidden": True}
    for cat in _owned_categories(discipline):
        categories[cat] = {"line_weight": 5} if cat in emphasis else {"halftone": True}
    return {"name": "Preset 3 - Package Focus", "categories": categories}


def generate_default_presets(discipline, package):
    """3 systematically generated presets for ANY (discipline, package) -
    see this module's own docstring for what these are (and aren't)."""
    if discipline not in DISCIPLINE_CATEGORIES:
        return []
    return [_preset_clean(discipline), _preset_coordination(discipline),
            _preset_focus(discipline, package)]


# {(discipline, package) key: [preset_dict, ...]} - the 3 original,
# hand-considered examples kept as-is; every other Package instead gets
# generate_default_presets()'s output (see presets_for below).
EXAMPLE_PRESETS = {
    _key("Architecture", "General Arrangement Plan"): [
        {
            "name": "Preset 1 - Architectural Plan (Clean)",
            "categories": {
                "Structural Columns": {"halftone": True},
                "Structural Framing": {"halftone": True},
                "Grids": {"halftone": True, "line_weight": 1},
                "Furniture": {"halftone": True},
                "Mechanical Equipment": {"hidden": True},
                "Duct Systems": {"hidden": True},
                "Piping": {"hidden": True},
                "Walls": {"line_weight": 5},
                "Doors": {"line_weight": 3},
                "Windows": {"line_weight": 3},
            },
        },
        {
            "name": "Preset 2 - Coordination (Structure Visible)",
            "categories": {
                "Structural Columns": {"line_weight": 4},
                "Structural Framing": {"line_weight": 2},
                "Grids": {"line_weight": 1},
                "Furniture": {"halftone": True},
                "Walls": {"halftone": True},
                "Doors": {"halftone": True},
                "Windows": {"halftone": True},
            },
        },
        {
            "name": "Preset 3 - Presentation (Furniture Emphasis)",
            "categories": {
                "Furniture": {"line_weight": 3},
                "Furniture Systems": {"line_weight": 3},
                "Structural Columns": {"halftone": True},
                "Structural Framing": {"hidden": True},
                "Grids": {"hidden": True},
                "Mechanical Equipment": {"hidden": True},
                "Duct Systems": {"hidden": True},
                "Piping": {"hidden": True},
                "Walls": {"line_weight": 5},
            },
        },
    ],
    _key("Architecture", "Reflected Ceiling Plan"): [
        {
            "name": "Preset 1 - Ceiling Emphasis",
            "categories": {
                "Furniture": {"halftone": True},
                "Casework": {"halftone": True},
                "Walls": {"halftone": True},
                "Doors": {"halftone": True},
                "Ceilings": {"line_weight": 4},
                "Lighting Fixtures": {"line_weight": 3},
                "Mechanical Equipment": {"line_weight": 2},
            },
        },
        {
            "name": "Preset 2 - Coordination (MEP Visible)",
            "categories": {
                "Furniture": {"hidden": True},
                "Casework": {"hidden": True},
                "Ceilings": {"line_weight": 1},
                "Lighting Fixtures": {"line_weight": 3},
                "Duct Systems": {"line_weight": 2},
                "Mechanical Equipment": {"line_weight": 2},
                "Sprinklers": {"line_weight": 2},
            },
        },
        {
            "name": "Preset 3 - Lighting Only",
            "categories": {
                "Furniture": {"hidden": True},
                "Casework": {"hidden": True},
                "Ceilings": {"halftone": True},
                "Duct Systems": {"hidden": True},
                "Mechanical Equipment": {"hidden": True},
                "Lighting Fixtures": {"line_weight": 4},
            },
        },
    ],
    _key("Structure", "Foundation Layout"): [
        {
            "name": "Preset 1 - Foundation Plan",
            "categories": {
                "Structural Columns": {"line_weight": 3},
                "Structural Foundations": {"line_weight": 5},
                "Walls": {"halftone": True},
                "Grids": {"line_weight": 1},
            },
        },
        {
            "name": "Preset 2 - Coordination (Architecture Halftoned)",
            "categories": {
                "Walls": {"halftone": True},
                "Doors": {"halftone": True},
                "Windows": {"halftone": True},
                "Furniture": {"hidden": True},
                "Structural Foundations": {"line_weight": 5},
                "Structural Columns": {"line_weight": 3},
            },
        },
        {
            "name": "Preset 3 - Pile/Footing Emphasis",
            "categories": {
                "Structural Foundations": {"line_weight": 6},
                "Structural Columns": {"halftone": True},
                "Walls": {"hidden": True},
                "Grids": {"line_weight": 1},
            },
        },
    ],
}


def builtin_presets_for(discipline, package):
    """The 3 hand-authored EXAMPLE_PRESETS if this exact (discipline,
    package) has one, else the rule-based generator's output - every
    Package gets real presets either way, never an empty "built-in"
    list, without 138 Packages needing individual hand-authored entries."""
    key = _key(discipline, package)
    if key in EXAMPLE_PRESETS:
        return list(EXAMPLE_PRESETS[key])
    return generate_default_presets(discipline, package)


def _user_presets_all():
    return deew_settings.load(_USER_PRESETS_TOOL_NAME, {})


def user_presets_for(discipline, package):
    return list(_user_presets_all().get(_key(discipline, package), []))


def save_user_preset(discipline, package, preset):
    """preset: {"name":.., "categories": {...}} - appended, or replaces an
    existing user preset of the same name for this (discipline, package).
    Keyed by BOTH, not package name alone - "Furniture Installation"
    exists under both Interior and Landscape, and a package-name-only key
    would let one discipline's saved preset silently leak into the
    other's dropdown."""
    key = _key(discipline, package)
    data = _user_presets_all()
    bucket = list(data.get(key, []))
    bucket = [p for p in bucket if p.get("name") != preset.get("name")]
    bucket.append(preset)
    data[key] = bucket
    deew_settings.save(_USER_PRESETS_TOOL_NAME, data)


def delete_user_preset(discipline, package, preset_name):
    key = _key(discipline, package)
    data = _user_presets_all()
    bucket = [p for p in data.get(key, []) if p.get("name") != preset_name]
    data[key] = bucket
    deew_settings.save(_USER_PRESETS_TOOL_NAME, data)


def presets_for(discipline, package):
    """Built-ins/generated first, then user-saved - the shape the Preset
    dropdown actually lists."""
    return builtin_presets_for(discipline, package) + user_presets_for(discipline, package)


def all_category_names(doc, view):
    """Every category actually present in `view`, name-sorted - reuses
    DeeMono's own proven view_categories() rather than re-scanning."""
    return [cat.Name for cat, _count in mono.view_categories(doc, view)]


def _category_by_name(doc, view, name):
    for cat, _count in mono.view_categories(doc, view):
        if cat.Name == name:
            return cat
    try:
        for cat in doc.Settings.Categories:
            if cat.Name == name:
                return cat
    except Exception:
        pass
    return None


def _build_ogs(doc, entry):
    from Autodesk.Revit.DB import OverrideGraphicSettings
    ogs = OverrideGraphicSettings()
    if entry.get("halftone"):
        try:
            ogs.SetHalftone(True)
        except Exception:
            pass
    weight = entry.get("line_weight")
    if weight is not None:
        try:
            w = max(1, min(16, int(weight)))
            ogs.SetProjectionLineWeight(w)
            ogs.SetCutLineWeight(w)
        except Exception:
            pass
    return ogs


class ApplyResult(object):
    def __init__(self):
        self.views_touched = 0
        self.categories_applied = 0
        self.categories_skipped = 0
        self.errors = []


def apply_preset(doc, views, preset):
    """Applies `preset`'s per-category overrides to every view in
    `views`, inside ONE Transaction covering all of them. Never raises -
    a failure on one category/view is recorded and skipped, same
    one-refusal-never-loses-the-rest convention as dee_mono_service."""
    from Autodesk.Revit.DB import Transaction

    result = ApplyResult()
    categories = (preset or {}).get("categories") or {}
    if not categories or not views:
        return result

    t = Transaction(doc, "DeeSVTemplate - Apply Preset")
    t.Start()
    try:
        for view in views:
            touched_this_view = False
            for cat_name, entry in categories.items():
                cat = _category_by_name(doc, view, cat_name)
                if cat is None:
                    result.categories_skipped += 1
                    continue
                try:
                    if entry.get("hidden"):
                        view.SetCategoryHidden(cat.Id, True)
                    else:
                        view.SetCategoryOverrides(cat.Id, _build_ogs(doc, entry))
                    result.categories_applied += 1
                    touched_this_view = True
                except Exception as e:
                    result.categories_skipped += 1
                    result.errors.append("{0} / {1}: {2}".format(view.Name, cat_name, e))
            if touched_this_view:
                result.views_touched += 1
        t.Commit()
    except Exception as e:
        try:
            if t.HasStarted() and not t.HasEnded():
                t.RollBack()
        except Exception:
            pass
        result.errors.append("Apply failed: {0}".format(e))
    return result


def create_view_template(doc, view, name=None):
    """Thin pass-through to dee_mono_service's own proven
    create_view_template() (view.IsViewValidForTemplateCreation() +
    view.CreateViewTemplate()) - not reimplemented here, see that
    module's own docstring for the full mechanism."""
    return mono.create_view_template(doc, view, name=name)


def _resolve_exported_file(file_path_no_ext):
    """Document.ExportImage's FilePath is a *base* path - some Revit
    versions write exactly "<base>.png", others append a view-name
    suffix first. Same defensive resolution lib/dee_icon_service.py
    already proved out for this exact uncertainty - not imported from
    there since it's that module's own private helper, but the
    technique is identical: look for whatever ExportImage actually
    produced, picking the most recently modified match."""
    import os
    exact = file_path_no_ext + ".png"
    if os.path.isfile(exact):
        return exact
    folder = os.path.dirname(file_path_no_ext) or "."
    prefix = os.path.basename(file_path_no_ext).lower()
    candidates = []
    try:
        for name in os.listdir(folder):
            if name.lower().startswith(prefix) and name.lower().endswith(".png"):
                candidates.append(os.path.join(folder, name))
    except Exception:
        pass
    if not candidates:
        return None
    candidates.sort(key=os.path.getmtime, reverse=True)
    return candidates[0]


def export_preview_image(doc, view):
    """Exports `view` (expected to already have the draft/preset's
    overrides applied - this never applies anything itself) as a PNG
    into the system temp folder, for the Preview tab's "Generate
    Preview Image" button. Returns (path, None) or (None, error_text) -
    never raises.

    NEEDS LIVE-REVIT VERIFICATION - same ImageExportOptions/
    Document.ExportImage call already flagged unverified in
    lib/dee_icon_service.py, reused here for the same reason: no other
    way to turn a view into a picture exists in this codebase."""
    import os
    import tempfile
    try:
        from Autodesk.Revit.DB import (
            ImageExportOptions, ExportRange, ZoomFitType, ImageResolution, ImageFileType)
        folder = tempfile.gettempdir()
        base = os.path.join(folder, "DeeSVTemplate_Preview_{0}".format(view.Id.IntegerValue))
        options = ImageExportOptions()
        options.ZoomType = ZoomFitType.FitToPage
        options.PixelSize = 900
        options.ImageResolution = ImageResolution.DPI_150
        options.ExportRange = ExportRange.CurrentView
        options.HLRandWFViewsFileType = ImageFileType.PNG
        options.FilePath = base
        doc.ExportImage(options)
        exported = _resolve_exported_file(base)
        if not exported:
            return None, "Revit did not produce an image file for this view."
        return exported, None
    except Exception as e:
        return None, str(e)


def views_on_sheets(doc, sheets):
    """Every view actually placed on any of `sheets`, de-duplicated,
    via ViewSheet.GetAllPlacedViews() - the "target by sheet instead of
    by view" option."""
    seen_ids = set()
    result = []
    for sheet in sheets or []:
        try:
            for view_id in sheet.GetAllPlacedViews():
                key = str(view_id.IntegerValue) if hasattr(view_id, "IntegerValue") else str(view_id)
                if key in seen_ids:
                    continue
                seen_ids.add(key)
                view = doc.GetElement(view_id)
                if view is not None:
                    result.append(view)
        except Exception:
            continue
    return result
