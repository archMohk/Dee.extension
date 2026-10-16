# -*- coding: utf-8 -*-
"""
DeeS.V.Template
Three cascading pickers - Discipline, Originator (Consultant/Contractor),
Package (a specific deliverable sheet type, e.g. "General Arrangement
Plan") - each Package backed by named visibility/graphics PRESETS (hide /
halftone / line-weight per category). Apply a preset to picked views, or
to whatever views are already placed on picked sheets; create a real
Revit View Template from a configured view; author and save new presets
visually via the Preview tab.

See lib/dee_sv_template_data.py and lib/dee_sv_template_service.py for
the reference dataset, the preset data model, and why only a handful of
Packages ship with real built-in presets (the other 130+ are a BIM-
standards content task for the firm to fill in via Save As New Preset,
not something to invent here - see that module's own docstring).

Reuses lib/dee_mono_service.py's proven Revit API primitives
(view.SetCategoryOverrides/SetCategoryHidden inside one Transaction,
view.CreateViewTemplate()) rather than reimplementing them - this tool's
own preset shape is a simpler, explicit per-category dict instead of
DeeMono's abstract hue-engine, since the need here is literal,
differentiated control ("halftone THIS, thick-outline THAT"), not a
recolored render.

NEEDS LIVE-REVIT VERIFICATION: OverrideGraphicSettings.SetHalftone(bool)
has no precedent elsewhere in this repo (DeeMono fakes a halftone LOOK a
different way); view.CreateViewTemplate() does have a precedent
(dee_mono_service.py), but that call itself is still flagged there as
unverified beyond existing in shipped code.
"""
import os
import datetime

import clr
clr.AddReference("WindowsBase")
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
from System.Windows import (CornerRadius, HorizontalAlignment, Thickness,
                            VerticalAlignment, TextWrapping)
from System.Windows.Controls import (Border, CheckBox, Orientation, StackPanel,
                                     TextBlock, WrapPanel)
from System.Windows.Media import Brushes, Color, SolidColorBrush
from System.Windows.Media.Imaging import BitmapImage, BitmapCacheOption
from System.Windows.Input import Cursors
from System.IO import File, MemoryStream

from Autodesk.Revit.DB import FilteredElementCollector, View, ViewSheet

from pyrevit import forms, script
import dee_branding

import dee_sv_template_data as data
import dee_sv_template_service as svc
import dee_maplink_service as dms
import dee_telemetry
dee_telemetry.check_access("DeeSVTemplate")

output = script.get_output()

_TOOL = "DeeS.V.Template"
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")

_SWATCH_DEFAULT = SolidColorBrush(Color.FromRgb(0x3A, 0x3A, 0x3A))
_SWATCH_HALFTONE = SolidColorBrush(Color.FromRgb(0x77, 0x77, 0x77))
_SWATCH_HIDDEN = SolidColorBrush(Color.FromRgb(0x5C, 0x2A, 0x2A))
_ROW_BG = SolidColorBrush(Color.FromRgb(0x2B, 0x2B, 0x2B))


class DeeSVTemplateWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, doc, uidoc):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.doc = doc
        self.uidoc = uidoc
        self._ready = False
        self._status_lines = []
        self._presets_cache = []
        self._current_discipline = None
        self._current_package = None
        self._target_checks = []  # [(element, CheckBox)]
        self._preview_cells = {}  # {category_name: Border}
        self._draft_categories = {}

        self.discipline_cb.ItemsSource = data.disciplines()
        self._ready = True
        if self.discipline_cb.Items.Count:
            self.discipline_cb.SelectedIndex = 0  # cascades originator -> package -> presets

        self._refresh_targets()
        self._preview_refresh()
        self._log("Ready. Pick a Discipline, Originator and Package to begin.")

    # ---------------- helpers ----------------
    def _log(self, message):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self._status_lines.append("[{0}] {1}".format(ts, message))
        try:
            self.status_tb.Text = self._status_lines[-1]
        except Exception:
            pass

    def _safe_text(self, textbox):
        try:
            return textbox.Text or ""
        except Exception:
            return ""

    def _current_selection(self):
        return (self.discipline_cb.SelectedItem, self.originator_cb.SelectedItem,
                self.package_cb.SelectedItem)

    def _entry_label(self, entry):
        if entry.get("hidden"):
            return "hidden"
        parts = []
        if entry.get("halftone"):
            parts.append("halftone")
        if entry.get("line_weight") is not None:
            parts.append("weight {0}".format(entry["line_weight"]))
        return ", ".join(parts) or "default"

    # ---------------- 1. Discipline / Originator / Package ----------------
    def discipline_changed(self, sender, args):
        if not self._ready:
            return
        self._rebuild_originators()

    def originator_changed(self, sender, args):
        if not self._ready:
            return
        self._rebuild_packages()

    def package_changed(self, sender, args):
        if not self._ready:
            return
        self._refresh_reference_text()
        self._refresh_presets()

    def _rebuild_originators(self):
        # Called directly (never relying on SelectionChanged alone) -
        # WPF does NOT raise SelectionChanged when SelectedIndex is set
        # to the value it already has, which a naive "set ItemsSource
        # then SelectedIndex = 0" cascade silently breaks on: e.g.
        # switching Discipline from "General" to "Architecture" leaves
        # Originator's own SelectedIndex at 0 both times (Consultant is
        # first for both), so originator_changed never fires and the
        # Package list stays stuck on the PREVIOUS discipline's rows -
        # live-reported bug. Calling _rebuild_packages() directly here,
        # every time, regardless of whether the event fires, is what
        # actually fixes it.
        disc = self.discipline_cb.SelectedItem
        origs = data.originators_for(disc) if disc else []
        self.originator_cb.ItemsSource = origs
        self.originator_cb.SelectedIndex = 0 if origs else -1
        self._rebuild_packages()

    def _rebuild_packages(self):
        disc, orig, _pkg = self._current_selection()
        pkgs = data.packages_for(disc, orig) if (disc and orig) else []
        self.package_cb.ItemsSource = pkgs
        self.package_cb.SelectedIndex = 0 if pkgs else -1
        self._refresh_reference_text()
        self._refresh_presets()

    def _refresh_reference_text(self):
        disc, orig, pkg = self._current_selection()
        if not (disc and orig and pkg):
            self.ref_views_tb.Text = ""
            self.ref_templates_tb.Text = ""
            self.package_info_tb.Text = ""
            return
        self.ref_views_tb.Text = "; ".join(data.views_for(disc, orig, pkg))
        self.ref_templates_tb.Text = "; ".join(data.templates_for(disc, orig, pkg))
        self.package_info_tb.Text = "{0} / {1} / {2}".format(disc, orig, pkg)

    # ---------------- 2. Presets ----------------
    def _refresh_presets(self):
        disc, _orig, pkg = self._current_selection()
        self._current_discipline = disc
        self._current_package = pkg
        presets = svc.presets_for(disc, pkg) if (disc and pkg) else []
        self._presets_cache = presets
        self.preset_cb.ItemsSource = [p["name"] for p in presets]
        if presets:
            self.preset_cb.SelectedIndex = 0
        else:
            self.preset_detail_tb.Text = (
                "No presets yet for this Package - configure categories on the "
                "Preview tab, then Save As New Preset.")
            self.delete_preset_b.IsEnabled = False

    def preset_changed(self, sender, args):
        if not self._ready:
            return
        idx = self.preset_cb.SelectedIndex
        if not (0 <= idx < len(self._presets_cache)):
            self.preset_detail_tb.Text = ""
            self.delete_preset_b.IsEnabled = False
            return
        preset = self._presets_cache[idx]
        cats = preset.get("categories", {})
        self.preset_detail_tb.Text = (", ".join(
            "{0} ({1})".format(k, self._entry_label(v)) for k, v in cats.items())
            or "No category overrides.")
        builtin_count = len(svc.builtin_presets_for(self._current_discipline, self._current_package))
        self.delete_preset_b.IsEnabled = idx >= builtin_count

    def delete_preset_click(self, sender, args):
        idx = self.preset_cb.SelectedIndex
        if not (0 <= idx < len(self._presets_cache)):
            return
        preset = self._presets_cache[idx]
        if not forms.alert("Delete preset '{0}'?".format(preset["name"]), yes=True, no=True):
            return
        svc.delete_user_preset(self._current_discipline, self._current_package, preset["name"])
        self._log("Deleted preset '{0}'.".format(preset["name"]))
        self._refresh_presets()

    # ---------------- 3. Targets ----------------
    def target_mode_changed(self, sender, args):
        if self._ready:
            self._refresh_targets()

    def target_search_changed(self, sender, args):
        if self._ready:
            self._refresh_targets()

    def _target_source_items(self):
        if self.target_sheets_rb.IsChecked is True:
            items = list(FilteredElementCollector(self.doc).OfClass(ViewSheet))
            return sorted(items, key=lambda s: "{0} - {1}".format(s.SheetNumber, s.Name))
        items = [v for v in FilteredElementCollector(self.doc).OfClass(View)
                 if not v.IsTemplate and not isinstance(v, ViewSheet)]
        return sorted(items, key=lambda v: v.Name)

    def _target_label(self, element):
        if isinstance(element, ViewSheet):
            return "{0} - {1}".format(element.SheetNumber, element.Name)
        return element.Name

    def _make_target_row(self, element, label):
        cb = CheckBox()
        cb.Content = label
        cb.Foreground = Brushes.WhiteSmoke
        cb.Margin = Thickness(6, 3, 6, 3)
        cb.Tag = element
        row = Border()
        row.Background = _ROW_BG
        row.Margin = Thickness(2, 1, 2, 1)
        row.Child = cb
        self._target_checks.append((element, cb))
        return row

    def _refresh_targets(self):
        query = self._safe_text(self.target_search_tb)
        items = self._target_source_items()
        self.target_panel.Children.Clear()
        self._target_checks = []
        shown = 0
        for element in items:
            label = self._target_label(element)
            if query and not dms.matches_search(label, query):
                continue
            shown += 1
            self.target_panel.Children.Add(self._make_target_row(element, label))
        self.target_count_tb.Text = "{0}/{1}".format(shown, len(items))

    def target_all_click(self, sender, args):
        for _el, cb in self._target_checks:
            cb.IsChecked = True

    def target_none_click(self, sender, args):
        for _el, cb in self._target_checks:
            cb.IsChecked = False

    def _selected_targets(self):
        return [el for el, cb in self._target_checks if cb.IsChecked is True]

    # ---------------- apply / create template ----------------
    def apply_click(self, sender, args):
        idx = self.preset_cb.SelectedIndex
        if not (0 <= idx < len(self._presets_cache)):
            forms.alert("Pick a Preset first.", title=_TOOL)
            return
        preset = self._presets_cache[idx]
        targets = self._selected_targets()
        if not targets:
            forms.alert("Tick at least one view or sheet first.", title=_TOOL)
            return
        if self.target_sheets_rb.IsChecked is True:
            views = svc.views_on_sheets(self.doc, targets)
            if not views:
                forms.alert("The ticked sheet(s) have no views placed on them yet.", title=_TOOL)
                return
        else:
            views = targets

        result = svc.apply_preset(self.doc, views, preset)
        msg = "Applied '{0}' to {1} view(s) - {2} categor(y/ies) overridden, {3} skipped.".format(
            preset["name"], result.views_touched, result.categories_applied, result.categories_skipped)
        self._log(msg)
        if result.errors:
            self._log("  " + "; ".join(result.errors[:5]))
        forms.alert(msg, title=_TOOL)

    def create_template_click(self, sender, args):
        view = self.doc.ActiveView
        if view is None:
            forms.alert("No active view.", title=_TOOL)
            return
        disc, orig, pkg = self._current_selection()
        templates = data.templates_for(disc, orig, pkg) if pkg else []
        default_name = templates[0] if templates else (view.Name + " Template")
        name = forms.ask_for_string(
            default=default_name, prompt="Name for the new View Template:", title=_TOOL)
        if not name:
            return
        template, error = svc.create_view_template(self.doc, view, name=name)
        if template is None:
            forms.alert("Could not create the view template:\n{0}".format(error), title=_TOOL)
            self._log("Create View Template failed: {0}".format(error))
            return
        self._log("Created view template '{0}' from '{1}'.".format(template.Name, view.Name))
        forms.alert("Created view template '{0}'.".format(template.Name), title=_TOOL)

    # ---------------- Preview tab ----------------
    def preview_refresh_click(self, sender, args):
        self._preview_refresh()

    def _preview_refresh(self):
        view = self.doc.ActiveView
        self.preview_panel.Children.Clear()
        self._preview_cells = {}
        if view is None:
            return
        for name in svc.all_category_names(self.doc, view):
            self.preview_panel.Children.Add(self._make_preview_cell(name))

    def _make_preview_cell(self, name):
        outer = StackPanel()
        outer.Orientation = Orientation.Vertical
        outer.Width = 130
        outer.Margin = Thickness(0, 0, 10, 10)

        label = TextBlock()
        label.Text = name
        label.TextWrapping = TextWrapping.Wrap
        label.FontSize = 10
        label.Foreground = Brushes.WhiteSmoke
        label.HorizontalAlignment = HorizontalAlignment.Center
        label.VerticalAlignment = VerticalAlignment.Center
        label.Margin = Thickness(3)

        swatch = Border()
        swatch.Height = 50
        swatch.CornerRadius = CornerRadius(3)
        swatch.BorderBrush = Brushes.Gray
        swatch.BorderThickness = Thickness(1)
        swatch.Cursor = Cursors.Hand
        swatch.Tag = name
        swatch.Child = label
        swatch.MouseLeftButtonDown += self._preview_swatch_click

        outer.Children.Add(swatch)
        self._preview_cells[name] = swatch
        self._paint_preview_cell(name)
        return outer

    def _paint_preview_cell(self, name):
        swatch = self._preview_cells.get(name)
        if swatch is None:
            return
        entry = self._draft_categories.get(name)
        if entry is None:
            swatch.Background = _SWATCH_DEFAULT
            swatch.Child.Text = name
        elif entry.get("hidden"):
            swatch.Background = _SWATCH_HIDDEN
            swatch.Child.Text = name + "\n(hidden)"
        elif entry.get("halftone"):
            swatch.Background = _SWATCH_HALFTONE
            swatch.Child.Text = name + "\n(halftone)"
        else:
            swatch.Background = _SWATCH_DEFAULT
            swatch.Child.Text = name

    def _preview_swatch_click(self, sender, args):
        name = sender.Tag
        entry = self._draft_categories.get(name)
        if entry is None:
            self._draft_categories[name] = {"halftone": True}
        elif entry.get("halftone"):
            self._draft_categories[name] = {"hidden": True}
        else:
            self._draft_categories.pop(name, None)
        self._paint_preview_cell(name)

    def _effective_preview_categories(self):
        """The in-progress draft (if the user has clicked any swatches)
        takes priority - that's a work-in-progress new preset being
        authored. Otherwise falls back to whichever Preset is picked on
        the Setup tab, so "Preview on Active View" and "Generate Preview
        Image" are also useful for checking an EXISTING preset, not only
        a brand-new one being drafted."""
        if self._draft_categories:
            return self._draft_categories
        idx = self.preset_cb.SelectedIndex
        if 0 <= idx < len(self._presets_cache):
            return self._presets_cache[idx].get("categories", {})
        return {}

    def preview_apply_now_click(self, sender, args):
        view = self.doc.ActiveView
        if view is None:
            forms.alert("No active view.", title=_TOOL)
            return
        categories = self._effective_preview_categories()
        if not categories:
            forms.alert("Pick a Preset, or click some swatches first.", title=_TOOL)
            return
        result = svc.apply_preset(self.doc, [view], {"categories": categories})
        self._log("Previewed on '{0}' - {1} categor(y/ies) applied.".format(
            view.Name, result.categories_applied))

    def preview_image_click(self, sender, args):
        view = self.doc.ActiveView
        if view is None:
            forms.alert("No active view.", title=_TOOL)
            return
        categories = self._effective_preview_categories()
        if not categories:
            forms.alert("Pick a Preset, or click some swatches first.", title=_TOOL)
            return
        svc.apply_preset(self.doc, [view], {"categories": categories})
        path, error = svc.export_preview_image(self.doc, view)
        if path is None:
            forms.alert("Could not generate a preview image:\n{0}".format(error), title=_TOOL)
            self._log("Preview image failed: {0}".format(error))
            return
        try:
            bmp = BitmapImage()
            bmp.BeginInit()
            bmp.CacheOption = BitmapCacheOption.OnLoad
            bmp.StreamSource = MemoryStream(File.ReadAllBytes(path))
            bmp.EndInit()
            bmp.Freeze()
            self.preview_image_img.Source = bmp
            self._log("Generated preview image from '{0}'.".format(view.Name))
        except Exception as e:
            forms.alert("Generated the image but could not display it:\n{0}".format(e), title=_TOOL)

    def preview_reset_click(self, sender, args):
        self._draft_categories = {}
        for name in list(self._preview_cells.keys()):
            self._paint_preview_cell(name)

    def save_preset_click(self, sender, args):
        disc, _orig, pkg = self._current_selection()
        if not (disc and pkg):
            forms.alert("Pick a Discipline/Originator/Package first.", title=_TOOL)
            return
        if not self._draft_categories:
            forms.alert("Click some swatches on the Preview tab first - nothing to save yet.",
                        title=_TOOL)
            return
        name = forms.ask_for_string(default="New Preset", prompt="Name for this new preset:",
                                    title=_TOOL)
        if not name:
            return
        svc.save_user_preset(disc, pkg, {"name": name, "categories": dict(self._draft_categories)})
        self._log("Saved new preset '{0}' for '{1}'.".format(name, pkg))
        self._refresh_presets()

    def close_click(self, sender, args):
        self.Close()


def main():
    uidoc = __revit__.ActiveUIDocument
    if uidoc is None:
        forms.alert("Open a Revit project first.", title=_TOOL)
        return
    doc = uidoc.Document
    window = DeeSVTemplateWindow(_XAML_FILE, doc, uidoc)
    window.ShowDialog()


main()
