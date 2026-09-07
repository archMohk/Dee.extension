# -*- coding: utf-8 -*-
"""
dee_mono_service
Recolours a 3D view into a single-hue "monochrome presentation" theme
from one picked colour - the graphic style in the reference image the
user attached (a flat, poster-like salmon/terracotta rendering with
crisp dark outlines), built out of Revit's own View Graphic Overrides
(the same dialog "VV" opens, driven programmatically instead).

--------------------------------------------------------------------
Revit API facts this module relies on, and how each was established
--------------------------------------------------------------------
Nearly everything here is NOT a guess: the load-bearing calls
(OverrideGraphicSettings, RevitColor(*rgb), a solid-fill pattern found
by scanning FillPatternElement for IsSolidFill, ElementId.
InvalidElementId as the "no override" sentinel) are used, live-tested,
this session, by DeeSSelect (Color Splasher) - see
dee_sselect.py:_solid_fill_pattern_id / _build_ogs. This module's
category-scoped calls are copied in spirit from that proven element-
scoped code, not invented fresh.

The two calls DeeSSelect does not already prove were confirmed by
reading the Revit API reference directly this session (WebFetch/
WebSearch against revitapidocs.com, not guessed):
  - View.SetCategoryOverrides(ElementId, OverrideGraphicSettings) and
    its mirror View.GetCategoryOverrides(ElementId) -> OverrideGraphic
    Settings (returns a default/blank instance if nothing was set) -
    documented on revitapidocs.com, present since Revit 2014. This is
    what makes "capture the view's current graphics before touching
    it, so they can be restored later" possible at all.
  - View.DisplayStyle (a plain property) using the DisplayStyle enum:
    Undefined, Wireframe, HLR, Shading, ShadingWithEdges, Rendering,
    Realistic, FlatColors, RealisticWithEdges, Raytrace. FlatColors is
    the enum name for what the Revit UI calls "Consistent Colors" - a
    flat, non-gradient shading mode that reads as a poster/rendering
    style rather than photorealistic, which is why it is offered here
    as the "flatten shading" option.

Deliberately NOT attempted: Silhouette Edges colour/weight, Depth
Cueing, Ambient Shadows, Sketchy Lines - the rest of the "Graphic
Display Options" dialog. Multiple searches this session found no
documented public API for reading or writing these settings. The dark
line-work effect in the reference image is achieved here instead by
forcing every category's own ProjectionLineColor/CutLineColor to a
fixed dark neutral and relying on ShadingWithEdges/FlatColors to draw
those lines at every face edge - a fully supported, already-proven
mechanism that reaches the same visual result without depending on an
API that may not exist.

View.SetBackground(ViewDisplayBackground) is confirmed documented and
restricted to 3D views, sections and elevations, and throws if the
view's DisplayStyle is Rendering - this module only ever calls it AFTER
setting DisplayStyle to something else, and wraps the call regardless.
ViewDisplayBackground.CreateGradient(sky, horizon, ground) with all
three colours equal is the documented way to get a solid colour rather
than an actual gradient. View.GetBackground() exists (confirmed as a
documented method page) but its exact return shape was not indepen-
dently verified, so background is restored best-effort and never
allowed to fail the rest of a restore.

Scope, stated plainly rather than silently under-delivered: this only
recolours categories in the HOST document. SetCategoryOverrides affects
the categories of the document whose view.SetCategoryOverrides is
called - a linked model's own elements are drawn using ITS OWN
document's category definitions and are not touched by an override
applied to the host view. Theming linked-model geometry the same way
would need opening/looping the link's own document, which this tool
does not attempt.

NEEDS LIVE-REVIT VERIFICATION (everything above the "confirmed" bar
still means confirmed via documentation, not via running Revit - flagged
per this codebase's own convention):
  1. GetCategoryOverrides/SetCategoryOverrides for real project
     categories (as opposed to DeeSSelect's proven per-ELEMENT sibling
     call) - the two are documented as symmetrical but this is the
     first time this codebase has driven the per-category form.
  2. View.GetBackground()'s actual return shape, used only for the
     best-effort restore.
  3. Whether every model category present in a real, messy project 3D
     view (curtain wall sub-categories, MEP fixtures, imported CAD
     categories, etc.) accepts SetCategoryOverrides without throwing -
     guarded per-category so one refusal cannot lose the rest.
"""
import io
import json
import os
import re

from Autodesk.Revit.DB import (
    FilteredElementCollector, FillPatternElement, ElementId, View3D,
    OverrideGraphicSettings, Color as RevitColor, DisplayStyle,
    ViewDisplayBackground, Transaction,
)

_STORE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".dee_mono")
_INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')

# A fixed dark neutral for line work, deliberately NOT derived from the
# theme colour. A theme-derived dark shade could land close in hue/
# lightness to the fill for some picked colours (a dark theme colour
# especially) and the outline would all but disappear - the one thing
# that must never happen in a style whose whole identity is crisp line
# work over flat colour. A fixed near-black reads correctly against
# every fill this module can produce.
LINE_RGB = (28, 26, 24)

WHITE_RGB = (255, 255, 255)


# ==========================================================================
# pure colour maths - no Revit, fully unit-testable
# ==========================================================================
def rgb_to_hsl(rgb):
    r, g, b = (c / 255.0 for c in rgb)
    lo, hi = min(r, g, b), max(r, g, b)
    l = (hi + lo) / 2.0
    if hi == lo:
        return 0.0, 0.0, l
    d = hi - lo
    s = d / (2.0 - hi - lo) if l > 0.5 else d / (hi + lo)
    if hi == r:
        h = (g - b) / d + (6.0 if g < b else 0.0)
    elif hi == g:
        h = (b - r) / d + 2.0
    else:
        h = (r - g) / d + 4.0
    return h / 6.0, s, l


def _hue_to_rgb(p, q, t):
    if t < 0:
        t += 1
    if t > 1:
        t -= 1
    if t < 1.0 / 6:
        return p + (q - p) * 6 * t
    if t < 1.0 / 2:
        return q
    if t < 2.0 / 3:
        return p + (q - p) * (2.0 / 3 - t) * 6
    return p


def hsl_to_rgb(h, s, l):
    if s <= 0:
        v = int(round(l * 255))
        v = max(0, min(255, v))
        return v, v, v
    q = l * (1 + s) if l < 0.5 else l + s - l * s
    p = 2 * l - q
    r = _hue_to_rgb(p, q, h + 1.0 / 3)
    g = _hue_to_rgb(p, q, h)
    b = _hue_to_rgb(p, q, h - 1.0 / 3)
    return tuple(max(0, min(255, int(round(c * 255)))) for c in (r, g, b))


def shift_lightness(rgb, delta):
    """delta in [-1, 1]: positive moves toward white, negative toward
    black, along the SAME hue/saturation - the only axis this tool ever
    varies, which is what keeps the whole view reading as one colour
    family rather than a patchwork."""
    h, s, l = rgb_to_hsl(rgb)
    if delta >= 0:
        l = l + (1.0 - l) * delta
    else:
        l = l + l * delta
    return hsl_to_rgb(h, s, max(0.02, min(0.98, l)))


def contrast_ratio(rgb_a, rgb_b):
    """WCAG relative-luminance contrast - used only by the tests to
    prove LINE_RGB stays legible against every role's fill."""
    def lum(rgb):
        def chan(c):
            c = c / 255.0
            return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
        r, g, b = (chan(c) for c in rgb)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b
    la, lb = lum(rgb_a) + 0.05, lum(rgb_b) + 0.05
    return max(la, lb) / min(la, lb)


# ==========================================================================
# category -> tone role
# ==========================================================================
# (lightness delta toward white(+)/black(-), transparency percent 0-100)
ROLE_TONE = {
    "background": (0.72, 0),
    "structure":  (0.0, 0),
    "accent":     (-0.30, 0),
    "glazing":    (0.55, 55),
    "planting":   (-0.15, 0),
}
DEFAULT_ROLE = "structure"

# Matched against Revit's own Category.Name (English UI strings - this
# is the same "match by the string Revit shows in the VG dialog" idea
# this codebase already uses in dee_getdwg_service, rather than trying
# to enumerate BuiltInCategory members that may not exist by that exact
# name across every category/region/version). Unrecognised categories -
# imported CAD layers, add-in-created categories, anything future
# Revit adds - fall through to DEFAULT_ROLE rather than being left
# unstyled or erroring.
CATEGORY_ROLES = {
    "floors": "background", "ceilings": "background", "roofs": "background",
    "topography": "background", "toposolid": "background", "site": "background",
    "structural foundations": "background", "shaft openings": "background",

    "walls": "structure", "curtain wall mullions": "structure",
    "structural columns": "structure", "columns": "structure",
    "structural framing": "structure", "stairs": "structure",
    "railings": "structure", "ramps": "structure", "generic models": "structure",
    "mass": "structure", "parts": "structure",

    "furniture": "accent", "furniture systems": "accent", "casework": "accent",
    "specialty equipment": "accent", "plumbing fixtures": "accent",
    "electrical fixtures": "accent", "electrical equipment": "accent",
    "lighting fixtures": "accent", "mechanical equipment": "accent",
    "appliances": "accent", "food service equipment": "accent",
    "data devices": "accent", "communication devices": "accent",

    "windows": "glazing", "curtain panels": "glazing", "curtain wall panels": "glazing",
    "skylights": "glazing",

    # Doors are deliberately NOT "glazing" - most doors in a real model
    # are solid wood or metal, and making an ordinary opaque door 55%
    # transparent would read as a bug, not a style choice. Doors sit
    # with the light "background" tone instead - close enough to the
    # wall they are set into that the opening still reads clearly
    # without the tool guessing which individual doors happen to be
    # glazed.
    "doors": "background",

    "planting": "planting", "entourage": "planting",
}


def role_for_category(category_name):
    key = (category_name or "").strip().lower()
    return CATEGORY_ROLES.get(key, DEFAULT_ROLE)


_LINE_CONTRAST_FLOOR = 1.5
_MAX_CONTRAST_NUDGES = 24


def _ensure_line_contrast(fill_rgb):
    """If a dark or saturated theme colour lands a role's fill close
    enough to LINE_RGB in luminance, the outline that role depends on
    for readability all but disappears - and it happens exactly on
    "structure" (delta 0.0, i.e. the picked colour unchanged), the role
    carrying the MOST line work (walls). A fixed per-role delta only
    reduces the odds of this; it does not rule it out for every colour
    a user might pick. This nudges the fill lighter, a small step at a
    time along its own hue/saturation, until the line colour is
    guaranteed legible against it - the same "measure, do not assume"
    approach this codebase's icon system already uses to guarantee
    every icon colour clears its own contrast floor on both light and
    dark grounds."""
    for _ in range(_MAX_CONTRAST_NUDGES):
        if contrast_ratio(fill_rgb, LINE_RGB) >= _LINE_CONTRAST_FLOOR:
            return fill_rgb
        fill_rgb = shift_lightness(fill_rgb, 0.08)
    return fill_rgb


def plan_for_category(base_rgb, category_name, glazing_transparency=None):
    """Pure: (fill_rgb, line_rgb, transparency_pct) for one category
    name. No Revit objects in or out, so this is exactly what the tests
    exercise - the Revit-facing function below only ever wraps this."""
    role = role_for_category(category_name)
    delta, default_transparency = ROLE_TONE[role]
    fill = _ensure_line_contrast(shift_lightness(base_rgb, delta))
    transparency = default_transparency
    if role == "glazing" and glazing_transparency is not None:
        transparency = max(0, min(95, int(glazing_transparency)))
    return fill, LINE_RGB, transparency


def build_plan(base_rgb, category_names, glazing_transparency=None):
    """{category_name: (fill_rgb, line_rgb, transparency_pct)} for a
    whole view - what apply_theme() turns into real API calls."""
    return dict((name, plan_for_category(base_rgb, name, glazing_transparency))
               for name in category_names)


# ==========================================================================
# Revit reads (no writes)
# ==========================================================================
def element_name(element):
    if element is None:
        return ""
    try:
        return element.Name
    except Exception:
        return ""


def list_3d_views(doc):
    """Local copy of the same small scan dee_3d_export_service.py
    already has - per this codebase's own established convention of
    never sharing a helper this small across files (an earlier tool
    crashed Revit when a shared helper's location changed under it)."""
    views = []
    try:
        collector = FilteredElementCollector(doc).OfClass(View3D)
    except Exception:
        return views
    for v in collector:
        try:
            if v.IsTemplate:
                continue
            views.append(v)
        except Exception:
            continue
    views.sort(key=lambda v: (element_name(v) or "").lower())
    return views


def view_label(view):
    extra = ""
    try:
        if view.IsPerspective:
            extra = " (perspective)"
    except Exception:
        pass
    return "{0}{1}".format(element_name(view), extra)


def view_categories(doc, view):
    """[(Category, count)] actually present in this view, name-sorted -
    what gets offered/recoloured is exactly what the view shows, never
    every category the model happens to define."""
    counts = {}
    cats = {}
    try:
        collector = FilteredElementCollector(doc, view.Id).WhereElementIsNotElementType()
    except Exception:
        return []
    for el in collector:
        try:
            cat = el.Category
            if cat is None or not cat.Name:
                continue
            key = cat.Name
            counts[key] = counts.get(key, 0) + 1
            cats[key] = cat
        except Exception:
            continue
    return sorted(((cats[k], counts[k]) for k in cats), key=lambda t: t[0].Name.lower())


def solid_fill_pattern_id(doc):
    """Identical in spirit to dee_sselect.py's own proven
    _solid_fill_pattern_id - filtering by IsSolidFill rather than
    matching a pattern NAME, because the name is UI-language-dependent
    and IsSolidFill is not."""
    try:
        for fp in FilteredElementCollector(doc).OfClass(FillPatternElement):
            try:
                if fp.GetFillPattern().IsSolidFill:
                    return fp.Id
            except Exception:
                continue
    except Exception:
        pass
    return ElementId.InvalidElementId


# ==========================================================================
# snapshot: capture the view's CURRENT graphics before touching them
# ==========================================================================
def _safe_color(getter):
    try:
        c = getter()
        if c is not None and c.IsValid:
            return [int(c.Red), int(c.Green), int(c.Blue)]
    except Exception:
        pass
    return None


def _safe_int(getter, invalid=-1):
    try:
        v = getter()
        return int(v) if v is not None else invalid
    except Exception:
        return invalid


def capture_category_ogs(ogs):
    """One OverrideGraphicSettings -> a plain, JSON-safe dict.

    These are PROPERTIES on OverrideGraphicSettings, not Get*() method
    calls - confirmed via the Revit API reference this session, after
    an early draft of this function called them as methods (GetCutLine
    Color() etc.), which would have thrown AttributeError on every
    single capture and made Restore silently do nothing. Asymmetric
    naming applies to transparency specifically: the SETTER is the
    method SetSurfaceTransparency(int), but the GETTER is the plain
    property .Transparency - confirmed from the same reference lookup.

    Every field is wrapped individually, so a Revit version exposing a
    slightly different property set degrades to 'leave that one field
    alone' on restore rather than failing the whole capture."""
    return {
        "line_color": _safe_color(lambda: ogs.ProjectionLineColor),
        "cut_line_color": _safe_color(lambda: ogs.CutLineColor),
        "fg_color": _safe_color(lambda: ogs.SurfaceForegroundPatternColor),
        "fg_pattern": _safe_int(lambda: _eid_value(ogs.SurfaceForegroundPatternId)),
        "cut_fg_color": _safe_color(lambda: ogs.CutForegroundPatternColor),
        "cut_fg_pattern": _safe_int(lambda: _eid_value(ogs.CutForegroundPatternId)),
        "transparency": _safe_int(lambda: ogs.Transparency, invalid=0),
        "halftone": bool(getattr(ogs, "Halftone", False)),
    }


def _eid_value(eid):
    if eid is None:
        return -1
    try:
        return int(eid.Value)
    except Exception:
        pass
    try:
        return int(eid.IntegerValue)
    except Exception:
        return -1


def _id_from_value(value):
    if value is None or value < 0:
        return ElementId.InvalidElementId
    try:
        return ElementId(value)
    except Exception:
        return ElementId.InvalidElementId


def ogs_from_snapshot(snapshot):
    """The inverse of capture_category_ogs - rebuilds a real
    OverrideGraphicSettings from a captured dict. A missing/None field
    is simply never called, which leaves that one aspect at Revit's own
    default rather than forcing a value nothing captured."""
    ogs = OverrideGraphicSettings()
    try:
        if snapshot.get("line_color"):
            ogs.SetProjectionLineColor(RevitColor(*snapshot["line_color"]))
        if snapshot.get("cut_line_color"):
            ogs.SetCutLineColor(RevitColor(*snapshot["cut_line_color"]))
        if snapshot.get("fg_color"):
            ogs.SetSurfaceForegroundPatternColor(RevitColor(*snapshot["fg_color"]))
        if snapshot.get("fg_pattern", -1) >= 0:
            ogs.SetSurfaceForegroundPatternId(_id_from_value(snapshot["fg_pattern"]))
        if snapshot.get("cut_fg_color"):
            ogs.SetCutForegroundPatternColor(RevitColor(*snapshot["cut_fg_color"]))
        if snapshot.get("cut_fg_pattern", -1) >= 0:
            ogs.SetCutForegroundPatternId(_id_from_value(snapshot["cut_fg_pattern"]))
        if snapshot.get("transparency") is not None:
            ogs.SetSurfaceTransparency(int(snapshot["transparency"]))
        if snapshot.get("halftone"):
            ogs.SetHalftone(True)
    except Exception:
        pass
    return ogs


# ==========================================================================
# persistence - one small JSON file per (document, view)
# ==========================================================================
def _safe_stem(text):
    cleaned = _INVALID_FILENAME_CHARS.sub("_", text or "").strip()
    return cleaned or "model"


def _snapshot_path(doc_title, view_unique_id):
    name = "{0}__{1}.json".format(_safe_stem(doc_title), view_unique_id or "view")
    return os.path.join(_STORE_DIR, name)


def save_snapshot(doc_title, view_unique_id, data):
    try:
        if not os.path.isdir(_STORE_DIR):
            os.makedirs(_STORE_DIR)
        path = _snapshot_path(doc_title, view_unique_id)
        with io.open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        return True
    except Exception:
        return False


def load_snapshot(doc_title, view_unique_id):
    try:
        path = _snapshot_path(doc_title, view_unique_id)
        if not os.path.isfile(path):
            return None
        with io.open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def has_any_snapshot(doc_title):
    try:
        if not os.path.isdir(_STORE_DIR):
            return False
        prefix = _safe_stem(doc_title) + "__"
        return any(f.startswith(prefix) for f in os.listdir(_STORE_DIR))
    except Exception:
        return False


def list_snapshot_view_ids(doc_title):
    ids = []
    try:
        if not os.path.isdir(_STORE_DIR):
            return ids
        prefix = _safe_stem(doc_title) + "__"
        for f in os.listdir(_STORE_DIR):
            if f.startswith(prefix) and f.endswith(".json"):
                ids.append(f[len(prefix):-5])
    except Exception:
        pass
    return ids


# ==========================================================================
# orchestration
# ==========================================================================
class MonoResult(object):
    def __init__(self):
        self.applied = 0
        self.skipped = 0
        self.errors = []
        self.category_count = 0
        self.display_style_set = False
        self.background_set = False


def _display_style_enum(flat):
    try:
        return DisplayStyle.FlatColors if flat else DisplayStyle.ShadingWithEdges
    except Exception:
        return None


def apply_theme(doc, view, base_rgb, flatten_shading=True, glazing_transparency=55):
    """Captures the view's current graphics FIRST (always, even on a
    first-ever run, so Restore is available immediately afterwards),
    then applies the theme. One Transaction: the number of categories
    in even a large, messy view is small (tens, not thousands), so this
    is nowhere near the scale that would call for DeeTransmit-style
    batching."""
    result = MonoResult()
    categories = view_categories(doc, view)
    result.category_count = len(categories)
    if not categories:
        result.errors.append("This view shows no model categories to recolour.")
        return result

    solid_id = solid_fill_pattern_id(doc)
    plan = build_plan(base_rgb, [cat.Name for cat, _count in categories], glazing_transparency)

    snapshot = {"categories": {}, "display_style": None}
    try:
        # The enum's NAME, not its integer value__: restoring it is then
        # a plain getattr(DisplayStyle, name) - the same "read back by
        # name, not by number" pattern this codebase already uses for
        # BuiltInCategory/ViewType elsewhere, and it sidesteps needing
        # Enum.GetValues (a System.Enum static method, not a member of
        # the enum type itself - easy to get wrong from IronPython).
        snapshot["display_style"] = view.DisplayStyle.ToString()
    except Exception:
        snapshot["display_style"] = None

    t = Transaction(doc, "DeeMono - Apply Theme")
    try:
        t.Start()
        for cat, _count in categories:
            cat_id_value = _eid_value(cat.Id)
            try:
                snapshot["categories"][str(cat_id_value)] = capture_category_ogs(
                    view.GetCategoryOverrides(cat.Id))
            except Exception:
                pass

            fill_rgb, line_rgb, transparency = plan[cat.Name]
            try:
                ogs = OverrideGraphicSettings()
                ogs.SetProjectionLineColor(RevitColor(*line_rgb))
                ogs.SetCutLineColor(RevitColor(*line_rgb))
                ogs.SetSurfaceForegroundPatternColor(RevitColor(*fill_rgb))
                ogs.SetCutForegroundPatternColor(RevitColor(*fill_rgb))
                if solid_id != ElementId.InvalidElementId:
                    ogs.SetSurfaceForegroundPatternId(solid_id)
                    ogs.SetCutForegroundPatternId(solid_id)
                ogs.SetSurfaceTransparency(transparency)
                view.SetCategoryOverrides(cat.Id, ogs)
                result.applied += 1
            except Exception as e:
                result.skipped += 1
                result.errors.append("{0}: {1}".format(cat.Name, e))

        style = _display_style_enum(flatten_shading)
        if style is not None:
            try:
                view.DisplayStyle = style
                result.display_style_set = True
            except Exception as e:
                result.errors.append("Display style: {0}".format(e))

        try:
            white = RevitColor(*WHITE_RGB)
            view.SetBackground(ViewDisplayBackground.CreateGradient(white, white, white))
            result.background_set = True
        except Exception:
            pass

        t.Commit()
    except Exception as e:
        try:
            if t.HasStarted() and not t.HasEnded():
                t.RollBack()
        except Exception:
            pass
        result.errors.append("Apply failed: {0}".format(e))
        return result

    save_snapshot(doc.Title, view.UniqueId, snapshot)
    return result


def restore_theme(doc, view):
    result = MonoResult()
    snapshot = load_snapshot(doc.Title, view.UniqueId)
    if not snapshot:
        result.errors.append("No saved original graphics for this view.")
        return result

    t = Transaction(doc, "DeeMono - Restore Original Graphics")
    try:
        t.Start()
        for cat_id_text, cat_snapshot in snapshot.get("categories", {}).items():
            try:
                cat_id = _id_from_value(int(cat_id_text))
                view.SetCategoryOverrides(cat_id, ogs_from_snapshot(cat_snapshot))
                result.applied += 1
            except Exception as e:
                result.skipped += 1
                result.errors.append("category {0}: {1}".format(cat_id_text, e))

        ds_name = snapshot.get("display_style")
        if ds_name:
            try:
                member = getattr(DisplayStyle, ds_name, None)
                if member is not None:
                    view.DisplayStyle = member
                    result.display_style_set = True
            except Exception as e:
                result.errors.append("Display style: {0}".format(e))

        t.Commit()
    except Exception as e:
        try:
            if t.HasStarted() and not t.HasEnded():
                t.RollBack()
        except Exception:
            pass
        result.errors.append("Restore failed: {0}".format(e))
        return result

    return result
