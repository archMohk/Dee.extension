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
        })
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
        self._swatch_cells = {}
        self._swatch_labels = {}
        self._tint_sliders = {}

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
        self.style_cb.SelectedIndex = core.PRESET_ORDER.index(self.preset_name)

        self.flatten_cb.IsChecked = bool(saved.get("flatten", True))
        self.outline_override_cb.IsChecked = bool(saved.get("outline_enabled", False))
        self.outline_b.IsEnabled = self.outline_override_cb.IsChecked is True

        self._ready = True
        self._update_colour_display()
        self._update_outline_display()
        self._refresh_style_description()
        self._refresh_restore_button()
        self._build_preview_cells()

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

    # ---------------- view ----------------
    def view_cb_changed(self, sender, args):
        if not self._ready:
            return
        self._guard(self._refresh_restore_button)

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
            if 0 <= i < len(core.PRESET_ORDER):
                self.preset_name = core.PRESET_ORDER[i]
            self.tint_adjust = {}
            self._refresh_style_description()
            self._build_preview_cells()
            self.status_tb.Text = ("Switched style - tint sliders reset to this "
                                   "style's own defaults.")
        self._guard(run)

    def _refresh_style_description(self):
        self.style_desc_tb.Text = self._selected_preset().get("description", "")

    def flatten_cb_click(self, sender, args):
        pass

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

    def outline_override_click(self, sender, args):
        def run():
            self.outline_b.IsEnabled = self.outline_override_cb.IsChecked is True
            self._update_swatch_colours()
        self._guard(run)

    def outline_click(self, sender, args):
        def run():
            dlg = ColorDialog()
            dlg.Color = DrawingColor.FromArgb(*self._outline_rgb_value)
            dlg.FullOpen = True
            dlg.AnyColor = True
            if dlg.ShowDialog() != DialogResult.OK:
                return
            c = dlg.Color
            self._outline_rgb_value = (int(c.R), int(c.G), int(c.B))
            self._update_outline_display()
            self._update_swatch_colours()
        self._guard(run)

    # ---------------- tints ----------------
    def reset_tints_click(self, sender, args):
        def run():
            self.tint_adjust = {}
            self._build_preview_cells()
            self.status_tb.Text = "Tints reset to this style's own defaults."
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

            self._swatch_cells[role] = cell
            self._swatch_labels[role] = label
            self._tint_sliders[role] = slider
            self.preview_panel.Children.Add(outer)

        self._update_swatch_colours()

    def _update_swatch_colours(self):
        """Repaints the EXISTING swatch Borders in place - never touches
        the Slider objects, so a slider mid-drag is never disrupted by
        this being called from its own ValueChanged handler."""
        preset = self._selected_preset()
        outline = self._effective_outline_rgb()
        for role, cat_name, _group_label in PREVIEW_ROLES:
            cell = self._swatch_cells.get(role)
            if cell is None:
                continue
            fill_rgb, line_rgb, _transparency = core.plan_for_category(
                self.base_rgb, cat_name, preset, tint_adjust=self.tint_adjust,
                line_rgb=outline)
            cell.Background = SolidColorBrush(_mcolor(fill_rgb))
            cell.BorderBrush = SolidColorBrush(_mcolor(line_rgb))
            self._swatch_labels[role].Foreground = SolidColorBrush(_mcolor(line_rgb))

    # ---------------- apply / restore ----------------
    def _save_settings(self):
        try:
            deew_settings.save(_SETTINGS, {
                "base_rgb": list(self.base_rgb),
                "preset_name": self.preset_name,
                "flatten": self.flatten_cb.IsChecked is True,
                "outline_enabled": self.outline_override_cb.IsChecked is True,
                "outline_rgb": list(self._outline_rgb_value),
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
                    tint_adjust=self.tint_adjust, line_rgb=self._effective_outline_rgb())

            self._refresh_restore_button()
            self._report(view, result, "Apply")

            try:
                uidoc = __revit__.ActiveUIDocument
                if uidoc is not None and self.active_view is not None \
                        and view.Id == self.active_view.Id:
                    uidoc.RefreshActiveView()
            except Exception:
                pass

            self.status_tb.Text = "Applied '{0}' to '{1}': {2} categor(y/ies) recoloured.".format(
                self.preset_name, core.view_label(view), result.applied)
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

    def _report(self, view, result, mode):
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
            if self.tint_adjust:
                parts = ["{0} {1:+d}%".format(role, int(round(v * 100)))
                        for role, v in sorted(self.tint_adjust.items())]
                html += "<br><b>Manual tints:</b> " + ", ".join(parts)
        html += "</div>"
        bg = "#2e7d32" if not result.errors else "#8d6e19"
        html += ('<div style="margin-top:8px;padding:7px 11px;background:{0};color:#fff;'
                 'border-radius:4px;font-family:monospace;font-size:12px;">'
                 '{1} categor(y/ies) recoloured, {2} skipped, out of {3} in the view. '
                 'Display style {4}. Background {5}.</div>'.format(
                     bg, result.applied, result.skipped, result.category_count,
                     "changed" if result.display_style_set else "unchanged",
                     "set to white" if result.background_set else "left as it was"))
        if result.errors:
            html += '<div style="font-family:sans-serif;font-size:11px;color:#a55;margin-top:6px;">'
            for e in result.errors[:12]:
                html += "&bull; {0}<br>".format(e)
            html += "</div>"
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
