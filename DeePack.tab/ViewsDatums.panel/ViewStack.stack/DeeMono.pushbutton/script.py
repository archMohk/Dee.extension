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
from System.Windows.Controls import Border, TextBlock
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

# A representative spread across every tone role this tool defines
# (background/structure/structure_heavy/structure_light/accent/
# accent_cool/glazing/planting) - the preview earns its keep by
# actually showing every kind of variance the style dropdown controls,
# not just one or two categories.
PREVIEW_CATEGORIES = ["Floors", "Walls", "Structural Columns", "Stairs",
                      "Furniture", "Lighting Fixtures", "Windows", "Planting"]


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
        })
        self.base_rgb = tuple(saved.get("base_rgb", _DEFAULT_RGB))
        self.preset_name = saved.get("preset_name", core.DEFAULT_PRESET)
        if self.preset_name not in core.PRESETS:
            self.preset_name = core.DEFAULT_PRESET

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

        self._ready = True
        self._update_colour_display()
        self._refresh_style_description()
        self._refresh_restore_button()
        self._refresh_preview()

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
            self._refresh_preview()
        self._guard(run)

    # ---------------- style ----------------
    def style_cb_changed(self, sender, args):
        if not self._ready:
            return
        def run():
            i = self.style_cb.SelectedIndex
            if 0 <= i < len(core.PRESET_ORDER):
                self.preset_name = core.PRESET_ORDER[i]
            self._refresh_style_description()
            self._refresh_preview()
        self._guard(run)

    def _refresh_style_description(self):
        self.style_desc_tb.Text = self._selected_preset().get("description", "")

    def flatten_cb_click(self, sender, args):
        pass

    # ---------------- preview ----------------
    def _refresh_preview(self):
        """Rebuilt from scratch each time rather than updated in place -
        there are only 8 small swatches, so the cost is trivial, and it
        avoids any risk of a stale swatch left over from a previous
        style/colour combination."""
        preset = self._selected_preset()
        self.preview_panel.Children.Clear()
        for cat_name in PREVIEW_CATEGORIES:
            fill_rgb, line_rgb, _transparency = core.plan_for_category(
                self.base_rgb, cat_name, preset)

            cell = Border()
            cell.Width = 92
            cell.Height = 52
            cell.Margin = Thickness(0, 0, 6, 6)
            cell.CornerRadius = CornerRadius(3)
            cell.Background = SolidColorBrush(_mcolor(fill_rgb))
            cell.BorderBrush = SolidColorBrush(_mcolor(line_rgb))
            cell.BorderThickness = Thickness(3)

            label = TextBlock()
            label.Text = cat_name
            label.TextWrapping = TextWrapping.Wrap
            label.FontSize = 11
            label.Margin = Thickness(5)
            label.Foreground = SolidColorBrush(_mcolor(line_rgb))
            cell.Child = label

            self.preview_panel.Children.Add(cell)

    # ---------------- apply / restore ----------------
    def _save_settings(self):
        try:
            deew_settings.save(_SETTINGS, {
                "base_rgb": list(self.base_rgb),
                "preset_name": self.preset_name,
                "flatten": self.flatten_cb.IsChecked is True,
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
                    flatten_shading=self.flatten_cb.IsChecked is True)

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
