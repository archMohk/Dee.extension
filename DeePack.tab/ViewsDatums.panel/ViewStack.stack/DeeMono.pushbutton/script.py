# -*- coding: utf-8 -*-
"""
DeeMono
Recolours a 3D view into a single-hue "monochrome presentation" theme
(the style in the reference image the user attached: flat, poster-like
colour with crisp dark line work) by driving the same View Graphic
Overrides "VV" opens - see lib/dee_mono_service.py for the Revit API
facts this relies on.

--------------------------------------------------------------------
One window, with a style dropdown and a live preview
--------------------------------------------------------------------
The first version of this tool deliberately avoided any custom window,
using only sequential native/pyRevit dialogs, specifically to steer
clear of the nested-modal crash this codebase has hit before
(forms.ask_for_string or Selection.PickObject called from inside an
already-open custom WPF window). The user then asked for a real window
with a dropdown and a small previewer.

That is a SAFE thing to build, not a return to the dangerous pattern:
the only "nested dialog" this window ever opens is a native
System.Windows.Forms.ColorDialog, which is a lightweight Win32 common
dialog Windows itself manages - not a second WPF Window.ShowDialog().
This exact nesting (a native ColorDialog opened from a button inside an
already-open custom WPF window) is already proven live in this
codebase: DeeSSelect (Color Splasher) does precisely this. What is
still never done here is opening a SECOND WPF window, or calling
Selection.PickObject, from inside this one - this tool needs neither.

The preview panel calls the exact same lib/dee_mono_service.plan_for_
category() that Apply itself uses - not a separate approximation of
it - so what is shown is provably what Apply will produce, not a
best-effort guess at it.

--------------------------------------------------------------------
Tints and outline override
--------------------------------------------------------------------
Each preview swatch now carries its own Slider, letting the user nudge
that ONE tone role's lightness by hand, on top of whatever the chosen
style already computed for it - "make the furniture tone a bit darker
than Balanced gives it" without switching style. The outline colour
(fixed by default, deliberately not theme-derived - see LINE_RGB in
lib/dee_mono_service.py) can now be overridden the same way the theme
colour can.

Both controls are built to survive being dragged: the swatches and
sliders are constructed ONCE (_build_preview_cells), and every later
change (colour, a slider drag, an outline pick) only repaints the
EXISTING swatch Borders in place (_update_swatch_colours) rather than
tearing down and rebuilding the WrapPanel's children. Rebuilding on
every ValueChanged tick would have replaced the very Slider object the
user's mouse is currently dragging out from under the drag itself - a
real risk with a live slider that a one-shot dialog never has to worry
about. Only a STYLE change rebuilds from scratch, because switching
style redefines the baseline every tint adjustment is measured from,
so old slider positions would otherwise mean something different than
what they showed a moment ago - the sliders reset to 0 in that case,
deliberately, not silently carried over.

--------------------------------------------------------------------
Per-category outline overrides, hide-categories, and event guards
--------------------------------------------------------------------
Every event handler that does not already start with `if not
self._ready: return` (flatten_cb_click, outline_override_click,
outline_click, reset_tints_click) now has that guard too, matching
view_cb_changed/style_cb_changed - cheap insurance against a
programmatic IsChecked/SelectedIndex assignment during __init__ firing
before there is anything valid to react to, even though WPF's own
Click semantics (only user interaction reaches OnClick, not a property
setter) mean it is not certain this was ever the actual cause of a
live "DeeMono hit an error" report against the outline-override
feature.

Each preview cell now carries a third small control below its tint
Slider: a thin clickable bar for that ONE role's own outline colour -
left-click opens the same ColorDialog the global Override button uses,
scoped to just this role; right-click clears it back to "use the
outline above". Stored in self.line_overrides ({role: rgb}), passed
straight through to core.plan_for_category/apply_theme's own
line_overrides parameter, which already resolves per-role before
global before the module default (see plan_for_category's docstring).
The global Override checkbox is untouched in behaviour - "make the
Override as it is" - ticking it alone now also opens the colour picker
immediately the FIRST time (only when the remembered colour still
equals the module's own default), since ticking a box that changes
nothing visible is exactly what a user would read as broken.

Four checkboxes ("Hide in view: Levels / Grids / Section Box / Scope
Boxes") call View.SetCategoryHidden through apply_theme's
hide_categories_list - a real visibility toggle, not a graphic
override, so a monochrome presentation render can drop datum/crop
clutter entirely rather than merely recolour it to match everything
else. Off by default, like every other opt-in in this tool.

--------------------------------------------------------------------
Shadows
--------------------------------------------------------------------
A user request for "shadows and ambient shadows" control prompted a
second research pass. View.ShadowIntensity and View.SunlightIntensity
are real, documented int (0-100) properties - "Adjust shadows for this
view" reveals two sliders that read/write them straight through
apply_theme's shadow_intensity/sunlight_intensity parameters. The
"Show Ambient Shadows" checkbox from the Graphic Display Options dialog
specifically is NOT wired up here: researched directly and confirmed,
via an on-the-record Autodesk dev-team forum response, to have no
public API at all (an open, unresolved feature request) - not
attempted, rather than faked with the wrong property.

Unlike the theme colour, nothing about shadows is persisted across
sessions or carried over between views: the two sliders are seeded from
the SELECTED view's own live current values every time the view
selection changes (_seed_shadow_sliders_from_view), since Revit already
holds the real answer on the view itself - there is nothing to
remember. The checkbox starts unticked and the section starts disabled,
same as every other opt-in control in this window.

--------------------------------------------------------------------
Auto outline, per-category transparency, and custom presets
--------------------------------------------------------------------
"Auto outline" is a THIRD outline source (core.auto_outline_color),
alongside the fixed default and the global Override - each category's
outline becomes a lighter/darker shade of THAT category's OWN fill
instead of one flat colour shared by everything. It is mutually
exclusive with the global Override in this window (ticking one
force-unticks the other, enforced both directions in
auto_outline_cb_click/outline_override_click) purely for UI clarity -
core.plan_for_category's own precedence already resolves the two
correctly either way, a per-category manual pick still wins over both.

Every preview cell gained a second slider for transparency
(core.transparency_adjust), additive on top of that role's own base -
previously only glazing had any transparency at all. The swatch
Border's own Opacity now reflects the resulting percentage, so
transparency is something the preview actually SHOWS, not just a
number Apply would use unseen.

"Save as Preset" captures the FULL current adjustment state - base
style, every tint/transparency/per-category-outline change, the auto
outline and global override settings - as one named, reusable bundle,
stored under a SEPARATE deew_settings key ("dee_mono_custom_presets")
from the window's own small settings blob. Deliberately does NOT
capture the theme colour or the per-view options (hide-categories,
shadows) - those are per-run choices, not part of what most people mean
by "a preset". Saved presets appear in the SAME style_cb dropdown,
below a plain-text divider - selecting one loads the whole bundle back
(_load_custom_preset_into_window) rather than just switching a style
name. The name field is a permanently-visible inline TextBox, not
forms.ask_for_string or a second window - the established reason
neither of those may be opened from inside this already-modal window.
"""
import os
import traceback

import clr
clr.AddReference("System.Windows.Forms")
clr.AddReference("System.Drawing")
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
from System.Windows.Forms import ColorDialog, DialogResult
from System.Drawing import Color as DrawingColor
from System.Windows import Thickness, CornerRadius, TextWrapping
from System.Windows.Controls import Border, TextBlock, Slider, StackPanel, Orientation
from System.Windows.Media import SolidColorBrush, Color as MediaColor
from System.Windows.Input import Cursors

from pyrevit import forms, script

import dee_branding
import deew_settings
import dee_mono_service as core

output = script.get_output()

_TOOL = "DeeMono"
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")
_SETTINGS = "dee_mono"
_DEFAULT_RGB = (200, 120, 90)
_TINT_SLIDER_RANGE = 40  # +/- percentage points of extra lightness shift

# One representative category per tone role this tool defines
# (background/structure/structure_heavy/structure_light/accent/
# accent_cool/glazing/planting), with a short label naming the REAL
# categories that role actually covers - a tint slider adjusts the
# whole role, not just the one sample category shown, so the label
# has to say so or "Furniture" would look like it only affects
# furniture when it also retunes casework, appliances and more.
PREVIEW_ROLES = [
    ("background", "Floors", "Floors / Ceilings / Roofs"),
    ("structure", "Walls", "Walls"),
    ("structure_heavy", "Structural Columns", "Columns / Framing"),
    ("structure_light", "Stairs", "Stairs / Railings"),
    ("accent", "Furniture", "Furniture / Casework"),
    ("accent_cool", "Lighting Fixtures", "Fixtures / Equipment"),
    ("glazing", "Windows", "Windows / Glazing"),
    ("planting", "Planting", "Planting"),
]

# core.HIDE_CATEGORY_OPTIONS label -> the x:Name of that option's
# CheckBox in ui.xaml. A plain dict, not a class, since the only thing
# ever done with it is looking up the right control by the label the
# service module already defines - keeping ONE list (the service
# module's) as the source of truth for which four categories exist,
# rather than a second, easy-to-drift copy here.
_HIDE_CB_NAMES = {
    "Levels": "hide_levels_cb",
    "Grids": "hide_grids_cb",
    "Section Box": "hide_sectionbox_cb",
    "Scope Boxes": "hide_scopebox_cb",
}

# Custom presets live under a SEPARATE deew_settings key from the
# window's own small settings blob (_SETTINGS) - a different kind of
# data (a growing, named collection the user curates) than "the last
# few choices this window remembers", worth keeping in its own file.
_CUSTOM_PRESET_SETTINGS = "dee_mono_custom_presets"
# Plain-text divider shown in style_cb between the 6 built-in presets
# and any saved custom ones - not a real choice; selecting it is caught
# in style_cb_changed and reverts to whatever was selected before.
_CUSTOM_PRESET_DIVIDER = "---- Custom Presets ----"


def _load_custom_presets():
    data = deew_settings.load(_CUSTOM_PRESET_SETTINGS, {"presets": {}})
    presets = data.get("presets")
    return presets if isinstance(presets, dict) else {}


def _save_custom_presets(presets):
    deew_settings.save(_CUSTOM_PRESET_SETTINGS, {"presets": presets})


def _mcolor(rgb):
    return MediaColor.FromRgb(rgb[0], rgb[1], rgb[2])


class DeeMonoWindow(dee_branding.DeeBrandedWindow):
    # Exists before the base class loads the XAML, which fires
    # SelectionChanged on the combo boxes during InitializeComponent -
    # before this __init__ has populated anything to react to.
    _ready = False

    def __init__(self, xaml_file, doc, views, active_view):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.doc = doc
        self.views = views
        self.active_view = active_view

        saved = deew_settings.load(_SETTINGS, {
            "base_rgb": list(_DEFAULT_RGB),
            "preset_name": core.DEFAULT_PRESET,
            "flatten": True,
            "outline_enabled": False,
            "outline_rgb": None,
            "hide_categories": [],
            "auto_outline_enabled": False,
            "auto_outline_style": core.DEFAULT_AUTO_OUTLINE_PRESET,
            "auto_outline_delta": None,
        })
        self._custom_presets = _load_custom_presets()
        self.base_rgb = tuple(saved.get("base_rgb", _DEFAULT_RGB))
        self.preset_name = saved.get("preset_name", core.DEFAULT_PRESET)
        if self.preset_name not in core.PRESETS:
            self.preset_name = core.DEFAULT_PRESET

        # tint_adjust is deliberately NEVER persisted across sessions -
        # it is a live fine-tune on top of the CURRENT colour/style, and
        # restoring stale slider offsets against a different colour
        # picked later could look broken with nothing to explain why.
        # The outline override, by contrast, is a stable preference like
        # the theme colour itself, so it IS remembered.
        self.tint_adjust = {}
        outline_rgb = saved.get("outline_rgb")
        self._outline_rgb_value = tuple(outline_rgb) if outline_rgb else core.LINE_RGB
        # {role: rgb} per-category outline overrides - same "live
        # fine-tune, never persisted" treatment as tint_adjust rather
        # than the global outline's "stable preference" treatment: a
        # per-role colour is a much more specific choice, easy to end up
        # stale/confusing against a colour or style picked in a later
        # session, so each run starts clean and the user re-applies it
        # if still wanted.
        self.line_overrides = {}
        # {role: extra_pct}, additive on top of that role's own base
        # transparency - same "live fine-tune, never persisted" shape as
        # tint_adjust/line_overrides, extended to cover every category
        # rather than only glazing (the only role with any base
        # transparency at all before this).
        self.transparency_adjust = {}
        self._swatch_cells = {}
        self._swatch_labels = {}
        self._tint_sliders = {}
        self._transparency_sliders = {}
        self._outline_bars = {}

        active_label = None
        try:
            if active_view is not None:
                active_label = core.view_label(active_view)
        except Exception:
            active_label = None

        default_index = 0
        for i, v in enumerate(views):
            label = core.view_label(v)
            self.view_cb.Items.Add(label + ("  (active view)" if label == active_label else ""))
            if label == active_label:
                default_index = i
        self.view_cb.SelectedIndex = default_index

        for name in core.PRESET_ORDER:
            self.style_cb.Items.Add(name)
        if self._custom_presets:
            self.style_cb.Items.Add(_CUSTOM_PRESET_DIVIDER)
            for name in sorted(self._custom_presets.keys(), key=lambda s: s.lower()):
                self.style_cb.Items.Add(name)
        self.style_cb.SelectedIndex = core.PRESET_ORDER.index(self.preset_name)
        self._last_style_index = self.style_cb.SelectedIndex

        for name in core.AUTO_OUTLINE_PRESET_ORDER:
            self.auto_outline_style_cb.Items.Add(name)
        saved_auto_style = saved.get("auto_outline_style", core.DEFAULT_AUTO_OUTLINE_PRESET)
        if saved_auto_style not in core.AUTO_OUTLINE_PRESETS:
            saved_auto_style = core.DEFAULT_AUTO_OUTLINE_PRESET
        self.auto_outline_style_cb.SelectedIndex = core.AUTO_OUTLINE_PRESET_ORDER.index(
            saved_auto_style)
        saved_auto_delta = saved.get("auto_outline_delta")
        self._auto_outline_delta_init = (
            float(saved_auto_delta) if saved_auto_delta is not None
            else core.AUTO_OUTLINE_PRESETS[saved_auto_style])

        self.flatten_cb.IsChecked = bool(saved.get("flatten", True))
        self.outline_override_cb.IsChecked = bool(saved.get("outline_enabled", False))
        self.outline_b.IsEnabled = self.outline_override_cb.IsChecked is True

        self.auto_outline_cb.IsChecked = bool(saved.get("auto_outline_enabled", False))
        self.auto_outline_style_cb.IsEnabled = self.auto_outline_cb.IsChecked is True
        self.auto_outline_slider.IsEnabled = self.auto_outline_cb.IsChecked is True
        # Mutual exclusivity is enforced going forward by the click
        # handlers, but a settings file predating one of these two
        # features (or a manually edited one) could in principle have
        # both saved True - guard the load itself too, auto outline
        # loses the tiebreak since it is the newer, more specific
        # feature and the global Override is the one this session was
        # told explicitly to "make... as it is".
        if self.auto_outline_cb.IsChecked is True and self.outline_override_cb.IsChecked is True:
            self.auto_outline_cb.IsChecked = False
            self.auto_outline_style_cb.IsEnabled = False
            self.auto_outline_slider.IsEnabled = False

        # Defaults to nothing selected - this codebase's own "nothing
        # happens unless the user asks for it" convention, same as
        # outline override starting off. A stable preference, unlike
        # tint_adjust/line_overrides, so it IS remembered like the
        # theme colour and global outline are.
        saved_hide = set(saved.get("hide_categories", []) or [])
        for label, bic_name in core.HIDE_CATEGORY_OPTIONS:
            cb = getattr(self, _HIDE_CB_NAMES[label])
            cb.IsChecked = bic_name in saved_hide

        # Shadows: NEVER persisted and NEVER defaults to "on" - unlike
        # the theme colour, Revit already holds the real current value
        # on the view itself, so there is nothing to remember here; the
        # sliders are seeded from the SELECTED view's own live
        # ShadowIntensity/SunlightIntensity (see _seed_shadow_sliders_
        # from_view), not a saved preference from a different view.
        self.shadows_cb.IsChecked = False
        self.shadows_grid.IsEnabled = False

        self._ready = True
        self._update_colour_display()
        self._update_outline_display()
        self._refresh_style_description()
        self._refresh_restore_button()
        self._build_preview_cells()
        self._seed_shadow_sliders_from_view()
        # Fires auto_outline_slider_changed now that _ready is True, so
        # the value label ends up correct - same reasoning as the
        # shadow sliders just above.
        self.auto_outline_slider.Value = self._auto_outline_delta_init * 100.0

    def _guard(self, fn, *args):
        """WPF swallows exceptions raised inside an event handler, which
        would make a real bug look exactly like 'the button does
        nothing'. Every handler goes through here so a failure is
        visible instead."""
        try:
            return fn(*args)
        except Exception as e:
            self.status_tb.Text = "ERROR: {0}".format(e)
            forms.alert("DeeMono hit an error:\n\n{0}\n\n{1}".format(
                e, traceback.format_exc()[-900:]), title=_TOOL)

    # ---------------- selection helpers ----------------
    def _selected_view(self):
        i = self.view_cb.SelectedIndex
        if i is None or i < 0 or i >= len(self.views):
            return None
        return self.views[i]

    def _selected_preset(self):
        return core.PRESETS.get(self.preset_name, core.PRESETS[core.DEFAULT_PRESET])

    def _selected_hide_categories(self):
        """BuiltInCategory member names for every ticked 'Hide in view'
        checkbox - what apply_theme's hide_categories_list expects."""
        names = []
        for label, bic_name in core.HIDE_CATEGORY_OPTIONS:
            cb = getattr(self, _HIDE_CB_NAMES[label])
            if cb.IsChecked is True:
                names.append(bic_name)
        return names

    # ---------------- shadows ----------------
    def _seed_shadow_sliders_from_view(self):
        """Shows the SELECTED view's own live shadow values, not a
        remembered preference - Revit already holds the real current
        state, so reading it fresh is more honest than a stale saved
        number from a different view. Setting .Value on an XAML-wired
        Slider DOES fire its own ValueChanged (unlike a Button's Click,
        which only fires from real user interaction) - harmless here
        since that handler only repaints a text label, never writes
        into apply_theme's own parameters (those are read straight off
        the sliders at Apply time, not cached into a side dict)."""
        view = self._selected_view()
        if view is None:
            return
        try:
            self.shadow_slider.Value = core._clamp_pct(view.ShadowIntensity)
        except Exception:
            pass
        try:
            self.sunlight_slider.Value = core._clamp_pct(view.SunlightIntensity)
        except Exception:
            pass

    def shadows_cb_click(self, sender, args):
        if not self._ready:
            return
        def run():
            self.shadows_grid.IsEnabled = self.shadows_cb.IsChecked is True
        self._guard(run)

    def shadow_slider_changed(self, sender, args):
        if not self._ready:
            return
        self.shadow_value_tb.Text = str(int(round(sender.Value)))

    def sunlight_slider_changed(self, sender, args):
        if not self._ready:
            return
        self.sunlight_value_tb.Text = str(int(round(sender.Value)))

    def _effective_shadow_intensity(self):
        """None means 'leave it as it is' - same tri-state shape as
        _effective_outline_rgb, for the same reason: the checkbox is the
        real on/off switch, the slider position underneath it is only
        meaningful once that switch is on."""
        if self.shadows_cb.IsChecked is True:
            return int(round(self.shadow_slider.Value))
        return None

    def _effective_sunlight_intensity(self):
        if self.shadows_cb.IsChecked is True:
            return int(round(self.sunlight_slider.Value))
        return None

    # ---------------- view ----------------
    def view_cb_changed(self, sender, args):
        if not self._ready:
            return
        self._guard(self._refresh_restore_button)
        self._guard(self._seed_shadow_sliders_from_view)

    def _refresh_restore_button(self):
        view = self._selected_view()
        has_snapshot = False
        if view is not None:
            try:
                has_snapshot = view.UniqueId in set(
                    core.list_snapshot_view_ids(self.doc.Title))
            except Exception:
                has_snapshot = False
        self.restore_b.IsEnabled = has_snapshot
        if view is not None:
            self.status_tb.Text = ("A saved original exists for this view."
                                   if has_snapshot else
                                   "No saved original for this view yet - Apply will save one.")

    # ---------------- colour ----------------
    def _update_colour_display(self):
        self.colour_swatch.Background = SolidColorBrush(_mcolor(self.base_rgb))
        self.colour_hex_tb.Text = "#{0:02X}{1:02X}{2:02X}".format(*self.base_rgb)

    def colour_click(self, sender, args):
        def run():
            dlg = ColorDialog()
            dlg.Color = DrawingColor.FromArgb(*self.base_rgb)
            dlg.FullOpen = True
            dlg.AnyColor = True
            if dlg.ShowDialog() != DialogResult.OK:
                return
            c = dlg.Color
            self.base_rgb = (int(c.R), int(c.G), int(c.B))
            self._update_colour_display()
            self._update_swatch_colours()
        self._guard(run)

    # ---------------- style ----------------
    def style_cb_changed(self, sender, args):
        if not self._ready:
            return
        def run():
            i = self.style_cb.SelectedIndex
            if i < 0 or i >= self.style_cb.Items.Count:
                return
            name = self.style_cb.Items[i]
            if name == _CUSTOM_PRESET_DIVIDER:
                # Not a real choice - a plain-text separator. Revert to
                # whatever was actually selected before, without letting
                # THIS revert itself re-trigger the handler.
                self._ready = False
                try:
                    self.style_cb.SelectedIndex = self._last_style_index
                finally:
                    self._ready = True
                return
            self._last_style_index = i
            if name in self._custom_presets:
                self._load_custom_preset_into_window(name)
                return
            if 0 <= i < len(core.PRESET_ORDER):
                self.preset_name = core.PRESET_ORDER[i]
            self.tint_adjust = {}
            self.transparency_adjust = {}
            self._refresh_style_description()
            self._build_preview_cells()
            self.status_tb.Text = ("Switched style - tint/transparency sliders reset to "
                                   "this style's own defaults.")
        self._guard(run)

    def _current_style_name(self):
        i = self.style_cb.SelectedIndex
        if 0 <= i < self.style_cb.Items.Count:
            return self.style_cb.Items[i]
        return None

    def _refresh_style_description(self):
        name = self._current_style_name()
        if name in self._custom_presets:
            self.style_desc_tb.Text = "Custom preset, based on '{0}'.".format(self.preset_name)
        else:
            self.style_desc_tb.Text = self._selected_preset().get("description", "")

    def _load_custom_preset_into_window(self, name):
        """Loads a FULL saved bundle back into the window's own state -
        the custom-preset counterpart of picking a built-in style, just
        restoring more than one axis at once. Rebuilds the preview from
        scratch afterwards (same as any other style change), so every
        slider/swatch reflects the loaded values, not stale ones."""
        bundle = self._custom_presets.get(name)
        if not bundle:
            return
        base_preset = bundle.get("base_preset", core.DEFAULT_PRESET)
        if base_preset not in core.PRESETS:
            base_preset = core.DEFAULT_PRESET
        self.preset_name = base_preset
        self.tint_adjust = dict(bundle.get("tint_adjust") or {})
        self.transparency_adjust = dict(bundle.get("transparency_adjust") or {})
        self.line_overrides = dict(
            (k, tuple(v)) for k, v in (bundle.get("line_overrides") or {}).items())
        self.flatten_cb.IsChecked = bool(bundle.get("flatten", True))

        self.outline_override_cb.IsChecked = bool(bundle.get("outline_enabled", False))
        outline_rgb = bundle.get("outline_rgb")
        if outline_rgb:
            self._outline_rgb_value = tuple(outline_rgb)
        self.outline_b.IsEnabled = self.outline_override_cb.IsChecked is True

        auto_on = bool(bundle.get("auto_outline_enabled", False))
        self.auto_outline_cb.IsChecked = auto_on
        self.auto_outline_style_cb.IsEnabled = auto_on
        self.auto_outline_slider.IsEnabled = auto_on
        delta = bundle.get("auto_outline_delta")
        if delta is not None:
            self.auto_outline_slider.Value = float(delta) * 100.0
        if auto_on and self.outline_override_cb.IsChecked is True:
            self.outline_override_cb.IsChecked = False
            self.outline_b.IsEnabled = False

        self._update_outline_display()
        self._refresh_style_description()
        self._build_preview_cells()
        self.status_tb.Text = "Loaded custom preset '{0}'.".format(name)

    def _rebuild_style_items(self, select_name=None):
        """Rebuilds style_cb's full item list (6 built-ins, then the
        divider and every custom preset name, alphabetical) - used after
        Save adds or overwrites one. Suppresses _ready around the
        rebuild since Items.Clear()/Add() on a ComboBox with something
        selected can itself raise SelectionChanged partway through."""
        self._ready = False
        try:
            self.style_cb.Items.Clear()
            for name in core.PRESET_ORDER:
                self.style_cb.Items.Add(name)
            if self._custom_presets:
                self.style_cb.Items.Add(_CUSTOM_PRESET_DIVIDER)
                for name in sorted(self._custom_presets.keys(), key=lambda s: s.lower()):
                    self.style_cb.Items.Add(name)
            target = select_name if select_name in self._custom_presets else self.preset_name
            idx = 0
            for i in range(self.style_cb.Items.Count):
                if self.style_cb.Items[i] == target:
                    idx = i
                    break
            self.style_cb.SelectedIndex = idx
            self._last_style_index = idx
        finally:
            self._ready = True

    def save_preset_click(self, sender, args):
        def run():
            name = (self.custom_preset_name_tb.Text or "").strip()
            if not name:
                forms.alert("Type a name for the preset first.", title=_TOOL)
                return
            if name == _CUSTOM_PRESET_DIVIDER:
                forms.alert("That name is reserved - pick a different one.", title=_TOOL)
                return
            bundle = {
                "base_preset": self.preset_name,
                "tint_adjust": dict(self.tint_adjust),
                "transparency_adjust": dict(self.transparency_adjust),
                "line_overrides": dict((k, list(v)) for k, v in self.line_overrides.items()),
                "flatten": self.flatten_cb.IsChecked is True,
                "outline_enabled": self.outline_override_cb.IsChecked is True,
                "outline_rgb": list(self._outline_rgb_value),
                "auto_outline_enabled": self.auto_outline_cb.IsChecked is True,
                "auto_outline_delta": float(self.auto_outline_slider.Value) / 100.0,
            }
            was_new = name not in self._custom_presets
            self._custom_presets[name] = bundle
            _save_custom_presets(self._custom_presets)
            self._rebuild_style_items(select_name=name)
            self.status_tb.Text = (
                "Saved new preset '{0}'.".format(name) if was_new else
                "Updated preset '{0}'.".format(name))
        self._guard(run)

    # ---------------- view template ----------------
    def _default_template_name(self):
        view = self._selected_view()
        label = core.view_label(view) if view is not None else "View"
        return "DeeMono - {0} - {1}".format(self.preset_name, label)

    def save_template_cb_click(self, sender, args):
        if not self._ready:
            return
        def run():
            is_on = self.save_template_cb.IsChecked is True
            self.template_name_tb.IsEnabled = is_on
            if is_on and not (self.template_name_tb.Text or "").strip():
                self.template_name_tb.Text = self._default_template_name()
        self._guard(run)

    def flatten_cb_click(self, sender, args):
        if not self._ready:
            return

    # ---------------- outline override ----------------
    def _update_outline_display(self):
        self.outline_swatch.Background = SolidColorBrush(_mcolor(self._outline_rgb_value))
        self.outline_hex_tb.Text = "#{0:02X}{1:02X}{2:02X}".format(*self._outline_rgb_value)

    def _effective_outline_rgb(self):
        """None means 'use the module's own safe default' - kept as a
        real tri-state (off / on-with-a-remembered-colour) rather than
        collapsing to a plain colour, so unticking Override and
        reticking it later brings back the colour the user actually
        chose instead of resetting to the default."""
        if self.outline_override_cb.IsChecked is True:
            return self._outline_rgb_value
        return None

    def _pick_outline_colour(self):
        """The actual ColorDialog flow, factored out of outline_click so
        outline_override_click can trigger the exact same picker the
        moment the box is first ticked - see outline_override_click for
        why. Returns True only if the user actually picked a colour
        (pressed OK), so callers can tell 'ticked but cancelled the
        picker' apart from 'picked a colour'."""
        dlg = ColorDialog()
        dlg.Color = DrawingColor.FromArgb(*self._outline_rgb_value)
        dlg.FullOpen = True
        dlg.AnyColor = True
        if dlg.ShowDialog() != DialogResult.OK:
            return False
        c = dlg.Color
        self._outline_rgb_value = (int(c.R), int(c.G), int(c.B))
        self._update_outline_display()
        return True

    def outline_override_click(self, sender, args):
        if not self._ready:
            return
        def run():
            is_on = self.outline_override_cb.IsChecked is True
            self.outline_b.IsEnabled = is_on
            # Only one outline SOURCE applies to the whole view at once -
            # this global override and Auto outline below are
            # alternatives, not layers, so turning one on turns the
            # other off. A per-category manual pick still wins over
            # either regardless.
            if is_on and self.auto_outline_cb.IsChecked is True:
                self.auto_outline_cb.IsChecked = False
                self.auto_outline_style_cb.IsEnabled = False
                self.auto_outline_slider.IsEnabled = False
            # Ticking the box alone changes nothing VISIBLE the first
            # time: the remembered colour still equals the module's own
            # default outline, so the preview would look identical to
            # "off" and read as broken rather than "already on, at the
            # default colour". Open the picker immediately in that one
            # case only - once the user has actually chosen a colour,
            # re-ticking later never force-reopens it again.
            if is_on and self._outline_rgb_value == core.LINE_RGB:
                self._pick_outline_colour()
            self._update_swatch_colours()
        self._guard(run)

    def outline_click(self, sender, args):
        def run():
            if self._pick_outline_colour():
                self._update_swatch_colours()
        self._guard(run)

    # ---------------- auto outline ----------------
    def auto_outline_cb_click(self, sender, args):
        if not self._ready:
            return
        def run():
            is_on = self.auto_outline_cb.IsChecked is True
            self.auto_outline_style_cb.IsEnabled = is_on
            self.auto_outline_slider.IsEnabled = is_on
            if is_on and self.outline_override_cb.IsChecked is True:
                self.outline_override_cb.IsChecked = False
                self.outline_b.IsEnabled = False
            self._update_swatch_colours()
        self._guard(run)

    def auto_outline_style_cb_changed(self, sender, args):
        if not self._ready:
            return
        def run():
            i = self.auto_outline_style_cb.SelectedIndex
            if 0 <= i < len(core.AUTO_OUTLINE_PRESET_ORDER):
                name = core.AUTO_OUTLINE_PRESET_ORDER[i]
                # Setting .Value fires auto_outline_slider_changed, which
                # updates the label and repaints - same "preset seeds a
                # control, then the slider takes over" pattern the style
                # presets already use with the tint sliders.
                self.auto_outline_slider.Value = core.AUTO_OUTLINE_PRESETS[name] * 100.0
        self._guard(run)

    def auto_outline_slider_changed(self, sender, args):
        if not self._ready:
            return
        self.auto_outline_value_tb.Text = "{0:+d}%".format(int(round(sender.Value)))
        self._update_swatch_colours()

    def _effective_auto_outline_delta(self):
        """None means 'not in effect' - same tri-state shape as
        _effective_outline_rgb/_effective_shadow_intensity."""
        if self.auto_outline_cb.IsChecked is True:
            return float(self.auto_outline_slider.Value) / 100.0
        return None

    # ---------------- per-category outline overrides ----------------
    def _make_role_outline_handler(self, role):
        def handler(sender, args):
            self._guard(self._pick_role_outline, role)
        return handler

    def _pick_role_outline(self, role):
        current = self.line_overrides.get(role) or self._outline_rgb_value
        dlg = ColorDialog()
        dlg.Color = DrawingColor.FromArgb(*current)
        dlg.FullOpen = True
        dlg.AnyColor = True
        if dlg.ShowDialog() != DialogResult.OK:
            return
        c = dlg.Color
        self.line_overrides[role] = (int(c.R), int(c.G), int(c.B))
        self._update_swatch_colours()

    def _make_role_outline_clear_handler(self, role):
        def handler(sender, args):
            self._guard(self._clear_role_outline, role)
        return handler

    def _clear_role_outline(self, role):
        if role in self.line_overrides:
            del self.line_overrides[role]
            self._update_swatch_colours()

    # ---------------- tints ----------------
    def reset_tints_click(self, sender, args):
        if not self._ready:
            return
        def run():
            self.tint_adjust = {}
            self.line_overrides = {}
            self.transparency_adjust = {}
            self._build_preview_cells()
            self.status_tb.Text = ("Tints, transparency and per-category outlines reset "
                                   "to this style's own defaults.")
        self._guard(run)

    def _make_tint_handler(self, role):
        def handler(sender, args):
            self._guard(self._on_tint_changed, role, sender.Value)
        return handler

    def _on_tint_changed(self, role, slider_value):
        if not self._ready:
            return
        self.tint_adjust[role] = float(slider_value) / 100.0
        self._update_swatch_colours()

    def _make_transparency_handler(self, role):
        def handler(sender, args):
            self._guard(self._on_transparency_changed, role, sender.Value)
        return handler

    def _on_transparency_changed(self, role, slider_value):
        if not self._ready:
            return
        self.transparency_adjust[role] = float(slider_value)
        self._update_swatch_colours()

    # ---------------- preview ----------------
    def _build_preview_cells(self):
        """Constructs every swatch + slider FROM SCRATCH - only called
        on initial load and on a style change, both deliberate,
        infrequent actions where resetting the sliders to 0 is the
        correct behaviour, not an in-progress-drag risk. Every other
        change (colour, a slider move, an outline pick) goes through
        _update_swatch_colours instead, which touches only colours on
        the ALREADY-EXISTING Border/Slider objects."""
        self.preview_panel.Children.Clear()
        self._swatch_cells = {}
        self._swatch_labels = {}
        self._tint_sliders = {}
        self._transparency_sliders = {}
        self._outline_bars = {}

        for role, _cat_name, group_label in PREVIEW_ROLES:
            outer = StackPanel()
            outer.Orientation = Orientation.Vertical
            outer.Width = 108
            outer.Margin = Thickness(0, 0, 10, 10)

            cell = Border()
            cell.Height = 46
            cell.CornerRadius = CornerRadius(3)
            cell.BorderThickness = Thickness(3)

            label = TextBlock()
            label.Text = group_label
            label.TextWrapping = TextWrapping.Wrap
            label.FontSize = 10.5
            label.Margin = Thickness(5)
            cell.Child = label
            outer.Children.Add(cell)

            slider = Slider()
            slider.Minimum = -_TINT_SLIDER_RANGE
            slider.Maximum = _TINT_SLIDER_RANGE
            slider.Width = 108
            slider.Margin = Thickness(0, 3, 0, 0)
            slider.ToolTip = "Nudge '{0}' lighter or darker than this style's default".format(
                group_label)
            slider.Value = self.tint_adjust.get(role, 0.0) * 100.0
            # Wired AFTER Value is set, so the initial seed never fires
            # a spurious change - each slider's own handler only exists
            # once its starting position is already correct.
            slider.ValueChanged += self._make_tint_handler(role)
            outer.Children.Add(slider)

            transparency_slider = Slider()
            transparency_slider.Minimum = -100
            transparency_slider.Maximum = 100
            transparency_slider.Width = 108
            transparency_slider.Margin = Thickness(0, 3, 0, 0)
            transparency_slider.ToolTip = (
                "Nudge '{0}' more (right) or less (left) see-through, on top of "
                "this style's own default (0 for every category except "
                "Windows/Glazing)".format(group_label))
            transparency_slider.Value = self.transparency_adjust.get(role, 0.0)
            transparency_slider.ValueChanged += self._make_transparency_handler(role)
            outer.Children.Add(transparency_slider)

            # A thin clickable bar for this ONE role's own outline
            # colour - left-click sets it, right-click clears it back to
            # "use the outline above" (global override, or the module's
            # own default if that is off too). Built here rather than in
            # ui.xaml for the same reason the Slider above is: one entry
            # per PREVIEW_ROLES role, not one hand-written XAML row per
            # role.
            outline_bar = Border()
            outline_bar.Height = 9
            outline_bar.CornerRadius = CornerRadius(2)
            outline_bar.BorderBrush = SolidColorBrush(_mcolor((150, 150, 150)))
            outline_bar.BorderThickness = Thickness(1)
            outline_bar.Margin = Thickness(0, 4, 0, 0)
            outline_bar.Cursor = Cursors.Hand
            outline_bar.MouseLeftButtonDown += self._make_role_outline_handler(role)
            outline_bar.MouseRightButtonDown += self._make_role_outline_clear_handler(role)
            outer.Children.Add(outline_bar)

            self._swatch_cells[role] = cell
            self._swatch_labels[role] = label
            self._tint_sliders[role] = slider
            self._transparency_sliders[role] = transparency_slider
            self._outline_bars[role] = outline_bar
            self.preview_panel.Children.Add(outer)

        self._update_swatch_colours()

    def _update_swatch_colours(self):
        """Repaints the EXISTING swatch Borders in place - never touches
        the Slider objects, so a slider mid-drag is never disrupted by
        this being called from its own ValueChanged handler."""
        preset = self._selected_preset()
        outline = self._effective_outline_rgb()
        auto_delta = self._effective_auto_outline_delta()
        for role, cat_name, _group_label in PREVIEW_ROLES:
            cell = self._swatch_cells.get(role)
            if cell is None:
                continue
            fill_rgb, line_rgb, transparency = core.plan_for_category(
                self.base_rgb, cat_name, preset, tint_adjust=self.tint_adjust,
                line_rgb=outline, line_overrides=self.line_overrides,
                auto_outline_delta=auto_delta, transparency_adjust=self.transparency_adjust)
            cell.Background = SolidColorBrush(_mcolor(fill_rgb))
            cell.BorderBrush = SolidColorBrush(_mcolor(line_rgb))
            # A floor, not 0 - a fully-transparent swatch would just be
            # blank, which reads as "this cell broke", not "this
            # category is very see-through". The label stays legible at
            # every setting.
            cell.Opacity = max(0.15, 1.0 - transparency / 100.0)
            self._swatch_labels[role].Foreground = SolidColorBrush(_mcolor(line_rgb))

            bar = self._outline_bars.get(role)
            if bar is not None:
                # Always shows the EFFECTIVE outline colour, whichever
                # source produced it - not just "blank unless manually
                # overridden" - so Auto mode's whole point (a different
                # computed colour per category) is something the preview
                # actually demonstrates, not just a number Apply uses
                # unseen.
                bar.Background = SolidColorBrush(_mcolor(line_rgb))
                if role in self.line_overrides:
                    bar.ToolTip = (
                        "This category's own outline: #{0:02X}{1:02X}{2:02X}. "
                        "Right-click to clear it (falls back to the outline "
                        "above).".format(*line_rgb))
                else:
                    source = "Auto" if auto_delta is not None else "the outline above"
                    bar.ToolTip = (
                        "Current outline for this category: #{0:02X}{1:02X}{2:02X} "
                        "(from {3}). Left-click to set your own just for this "
                        "category.".format(line_rgb[0], line_rgb[1], line_rgb[2], source))

    # ---------------- apply / restore ----------------
    def _save_settings(self):
        try:
            auto_style_i = self.auto_outline_style_cb.SelectedIndex
            auto_style_name = (core.AUTO_OUTLINE_PRESET_ORDER[auto_style_i]
                               if 0 <= auto_style_i < len(core.AUTO_OUTLINE_PRESET_ORDER)
                               else core.DEFAULT_AUTO_OUTLINE_PRESET)
            deew_settings.save(_SETTINGS, {
                "base_rgb": list(self.base_rgb),
                "preset_name": self.preset_name,
                "flatten": self.flatten_cb.IsChecked is True,
                "outline_enabled": self.outline_override_cb.IsChecked is True,
                "outline_rgb": list(self._outline_rgb_value),
                "hide_categories": self._selected_hide_categories(),
                "auto_outline_enabled": self.auto_outline_cb.IsChecked is True,
                "auto_outline_style": auto_style_name,
                "auto_outline_delta": float(self.auto_outline_slider.Value) / 100.0,
            })
        except Exception:
            pass

    def apply_click(self, sender, args):
        def run():
            view = self._selected_view()
            if view is None:
                forms.alert("Pick a 3D view first.", title=_TOOL)
                return
            self._save_settings()

            with forms.ProgressBar(title="DeeMono - applying theme to '{0}'...".format(
                    core.view_label(view)), indeterminate=True):
                result = core.apply_theme(
                    self.doc, view, self.base_rgb, preset_name=self.preset_name,
                    flatten_shading=self.flatten_cb.IsChecked is True,
                    tint_adjust=self.tint_adjust, line_rgb=self._effective_outline_rgb(),
                    line_overrides=self.line_overrides,
                    hide_categories_list=self._selected_hide_categories(),
                    shadow_intensity=self._effective_shadow_intensity(),
                    sunlight_intensity=self._effective_sunlight_intensity(),
                    auto_outline_delta=self._effective_auto_outline_delta(),
                    transparency_adjust=self.transparency_adjust)

            template_result = None
            if self.save_template_cb.IsChecked is True and result.applied > 0:
                template_name = (self.template_name_tb.Text or "").strip() \
                    or self._default_template_name()
                with forms.ProgressBar(title="DeeMono - creating view template...",
                                       indeterminate=True):
                    template_view, template_error = core.create_view_template(
                        self.doc, view, name=template_name)
                template_result = (template_view, template_error)

            self._refresh_restore_button()
            self._report(view, result, "Apply", template_result=template_result)

            try:
                uidoc = __revit__.ActiveUIDocument
                if uidoc is not None and self.active_view is not None \
                        and view.Id == self.active_view.Id:
                    uidoc.RefreshActiveView()
            except Exception:
                pass

            status = "Applied '{0}' to '{1}': {2} categor(y/ies) recoloured.".format(
                self.preset_name, core.view_label(view), result.applied)
            if template_result is not None:
                template_view, template_error = template_result
                status += (" View Template '{0}' created.".format(template_view.Name)
                          if template_view is not None else
                          " View Template NOT created: {0}".format(template_error))
            self.status_tb.Text = status
        self._guard(run)

    def restore_click(self, sender, args):
        def run():
            view = self._selected_view()
            if view is None:
                return
            with forms.ProgressBar(title="DeeMono - restoring original graphics...",
                                   indeterminate=True):
                result = core.restore_theme(self.doc, view)
            self._report(view, result, "Restore")

            try:
                uidoc = __revit__.ActiveUIDocument
                if uidoc is not None and self.active_view is not None \
                        and view.Id == self.active_view.Id:
                    uidoc.RefreshActiveView()
            except Exception:
                pass

            self.status_tb.Text = "Restored '{0}'.".format(core.view_label(view))
        self._guard(run)

    def _report(self, view, result, mode, template_result=None):
        html = '<h2 style="font-family:sans-serif;">DeeMono</h2>'
        html += ('<div style="font-family:sans-serif;font-size:12px;">'
                 '<b>View:</b> {0}<br><b>Mode:</b> {1}'.format(core.view_label(view), mode))
        if mode == "Apply":
            hexcode = "#{0:02X}{1:02X}{2:02X}".format(*self.base_rgb)
            html += "<br><b>Style:</b> {0}<br><b>Theme colour:</b> {1}".format(
                self.preset_name, hexcode)
            outline = self._effective_outline_rgb()
            if outline:
                html += "<br><b>Outline colour:</b> #{0:02X}{1:02X}{2:02X} (overridden)".format(
                    *outline)
            auto_delta = self._effective_auto_outline_delta()
            if auto_delta is not None:
                html += "<br><b>Auto outline:</b> {0:+d}% (per category, from its own fill)".format(
                    int(round(auto_delta * 100)))
            if self.tint_adjust:
                parts = ["{0} {1:+d}%".format(role, int(round(v * 100)))
                        for role, v in sorted(self.tint_adjust.items())]
                html += "<br><b>Manual tints:</b> " + ", ".join(parts)
            if self.transparency_adjust:
                parts = ["{0} {1:+d}%".format(role, int(round(v)))
                        for role, v in sorted(self.transparency_adjust.items())]
                html += "<br><b>Manual transparency:</b> " + ", ".join(parts)
            if self.line_overrides:
                parts = ["{0} #{1:02X}{2:02X}{3:02X}".format(role, *rgb)
                        for role, rgb in sorted(self.line_overrides.items())]
                html += "<br><b>Per-category outlines:</b> " + ", ".join(parts)
            hide_list = self._selected_hide_categories()
            if hide_list:
                labels = [label for label, bic in core.HIDE_CATEGORY_OPTIONS
                         if bic in hide_list]
                html += "<br><b>Hidden categories:</b> " + ", ".join(labels)
            shadow_i = self._effective_shadow_intensity()
            sun_i = self._effective_sunlight_intensity()
            if shadow_i is not None or sun_i is not None:
                parts = []
                if shadow_i is not None:
                    parts.append("shadow intensity {0}%".format(shadow_i))
                if sun_i is not None:
                    parts.append("sunlight intensity {0}%".format(sun_i))
                html += "<br><b>Shadows:</b> " + ", ".join(parts)
        html += "</div>"
        bg = "#2e7d32" if not result.errors else "#8d6e19"
        hidden_note = ""
        if result.hidden_applied or result.hidden_skipped:
            hidden_note = " Visibility changed for {0} categor(y/ies).".format(
                result.hidden_applied)
        html += ('<div style="margin-top:8px;padding:7px 11px;background:{0};color:#fff;'
                 'border-radius:4px;font-family:monospace;font-size:12px;">'
                 '{1} categor(y/ies) recoloured, {2} skipped, out of {3} in the view. '
                 'Display style {4}. Background {5}.{6}</div>'.format(
                     bg, result.applied, result.skipped, result.category_count,
                     "changed" if result.display_style_set else "unchanged",
                     "set to white" if result.background_set else "left as it was",
                     hidden_note))
        if result.errors:
            html += '<div style="font-family:sans-serif;font-size:11px;color:#a55;margin-top:6px;">'
            for e in result.errors[:12]:
                html += "&bull; {0}<br>".format(e)
            html += "</div>"
        if template_result is not None:
            template_view, template_error = template_result
            if template_view is not None:
                html += ('<div style="margin-top:8px;padding:7px 11px;background:#2e7d32;'
                         'color:#fff;border-radius:4px;font-family:monospace;font-size:12px;">'
                         'View Template created: {0}</div>'.format(template_view.Name))
            else:
                html += ('<div style="margin-top:8px;padding:7px 11px;background:#8d6e19;'
                         'color:#fff;border-radius:4px;font-family:monospace;font-size:12px;">'
                         'View Template NOT created: {0}</div>'.format(template_error))
        output.print_html(html)

    def close_click(self, sender, args):
        self._save_settings()
        self.Close()


def main():
    uiapp = __revit__
    uidoc = uiapp.ActiveUIDocument
    if uidoc is None:
        forms.alert("Open a Revit project first.", title=_TOOL)
        return
    doc = uidoc.Document
    if doc.IsFamilyDocument:
        forms.alert("DeeMono works on a project's 3D view. It cannot be run inside the "
                    "Family Editor.", title=_TOOL)
        return

    views = core.list_3d_views(doc)
    if not views:
        forms.alert("This model has no 3D views. Create one and run DeeMono again.",
                    title=_TOOL)
        return

    active_view = None
    try:
        active_view = doc.ActiveView
    except Exception:
        active_view = None

    window = DeeMonoWindow(_XAML_FILE, doc, views, active_view)
    window.ShowDialog()


main()
