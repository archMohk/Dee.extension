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
Built-in vs. user-saved presets - see the DeeSVTemplate plan's own
scope note: BUILTIN_PRESETS below covers a handful of fully-worked
example Packages to prove the mechanism end-to-end. Every other Package
has no built-in presets - only the firm can correctly say which
categories should be halftoned/hidden/outlined for 130+ distinct
deliverable types, against their own real standards. "Save New Preset"
is how they fill the rest in over time, through the tool's own Preview
tab, not invented here.
--------------------------------------------------------------------
"""
import deew_settings
import dee_mono_service as mono

_USER_PRESETS_TOOL_NAME = "dee_sv_template_presets"

# {package_name: [preset_dict, ...]} - a handful of fully-worked examples,
# not an attempt at covering every Package in dee_sv_template_data.
BUILTIN_PRESETS = {
    "General Arrangement Plan": [
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
    "Reflected Ceiling Plan": [
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
    "Foundation Layout": [
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


def builtin_presets_for(package):
    return list(BUILTIN_PRESETS.get(package, []))


def _user_presets_all():
    return deew_settings.load(_USER_PRESETS_TOOL_NAME, {})


def user_presets_for(package):
    return list(_user_presets_all().get(package, []))


def save_user_preset(package, preset):
    """preset: {"name":.., "categories": {...}} - appended, or replaces an
    existing user preset of the same name for this Package."""
    data = _user_presets_all()
    bucket = list(data.get(package, []))
    bucket = [p for p in bucket if p.get("name") != preset.get("name")]
    bucket.append(preset)
    data[package] = bucket
    deew_settings.save(_USER_PRESETS_TOOL_NAME, data)


def delete_user_preset(package, preset_name):
    data = _user_presets_all()
    bucket = [p for p in data.get(package, []) if p.get("name") != preset_name]
    data[package] = bucket
    deew_settings.save(_USER_PRESETS_TOOL_NAME, data)


def presets_for(package):
    """Built-ins first, then user-saved - the shape the Preset dropdown
    actually lists."""
    return builtin_presets_for(package) + user_presets_for(package)


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
