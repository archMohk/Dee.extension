# -*- coding: utf-8 -*-
"""
DeeHostLevel (HealthPack)
Scans every Model-category element in the project by which Level it is
associated with (Element.LevelId), then:
  - Report tab: a Level-by-Level summary grid (name/elevation/element
    count) plus a full element list (Id/Category/Family/Type/Level).
    Export to CSV from here.
  - Color & 3D View tab: auto-assigns a distinct color per Level
    (click a swatch's "Change Color..." to override it with the
    system color picker), then Generate/Update builds (or reuses) a 3D
    view named exactly "DeeHostLevel" with every scanned element
    tinted by its Level's color via per-element graphic overrides (a
    solid fill pattern + that color).
  - Rehost tab: either consolidates every scanned element onto ONE
    Level you pick, or maps each existing Level to its own target
    Level (select row(s) in the table, pick a target in the standalone
    combo below it, Apply to Selected - deliberately NOT an inline
    per-row combo column; an earlier tool in this extension
    (DeeDistributor) hit real bugs with DataGridComboBoxColumn and
    moved to this same standalone-combo pattern). "Keep elements in
    their current physical position" compensates each element's
    offset/elevation parameter for the difference between the old and
    new Level's elevation; unchecked, the offset is left as-is and the
    element visually shifts by that difference.

Rehosting works differently per category under the hood: most
furniture/equipment/fixture-style families use a Level + a separate
offset/elevation instance parameter, while Walls/Floors/Roofs/Ceilings
use a Base-Constraint-style Level parameter. Since Revit does not
expose one uniform API for "the Level parameter" across every
category, find_level_parameter/find_offset_parameter try a list of
likely BuiltInParameters first (skipping any that don't exist on this
Revit version via a safe getattr, so a wrong guess never crashes),
then fall back to matching a parameter by its displayed name. Elements
where neither approach finds a writable parameter are skipped and
listed as such in the results - never silently dropped, never a hard
crash.

A wall whose Top Constraint references "Up to Level" (rather than
Unconnected Height) may change height as a side effect of moving its
Base Constraint to a different Level - this tool does not attempt to
compensate for that; it is expected, documented behavior.

Nothing touches the model until "Generate / Update 'DeeHostLevel' View"
or "Apply Rehost" is clicked.
"""
import os
import csv

import clr
clr.AddReference("System.Windows.Forms")
clr.AddReference("System.Drawing")
from System.Windows.Forms import SaveFileDialog, DialogResult, MessageBox, ColorDialog
from System.Drawing import Color as DrawingColor
from System.Windows import Thickness, Visibility, VerticalAlignment
from System.Windows.Controls import StackPanel, TextBlock, Button, Orientation
from System.Windows.Shapes import Rectangle
from System.Windows.Media import SolidColorBrush, Color as MediaColor, Brushes

from pyrevit import forms, script
from Autodesk.Revit.DB import (
    FilteredElementCollector, Level, Transaction, BuiltInParameter, CategoryType,
    ElementId, StorageType, View3D, ViewFamilyType, ViewFamily,
    OverrideGraphicSettings, FillPatternElement, Color as RevitColor,
    UnitUtils, SpecTypeId, RevitLinkInstance
)

from host_level_tools import generate_level_colors, compute_rehosted_offset

output = script.get_output()

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")

_VIEW_NAME = "DeeHostLevel"

_LEVEL_PARAM_CANDIDATES = [
    "SCHEDULE_LEVEL_PARAM", "FAMILY_LEVEL_PARAM", "LEVEL_PARAM",
    "WALL_BASE_CONSTRAINT", "ROOF_BASE_LEVEL_PARAM", "ROOF_CONSTRAINT_LEVEL_PARAM",
    "STAIRS_BASE_LEVEL_PARAM", "RBS_START_LEVEL_PARAM",
]
_OFFSET_PARAM_CANDIDATES = [
    "INSTANCE_FREE_HOST_OFFSET_PARAM", "INSTANCE_ELEVATION_PARAM",
    "WALL_BASE_OFFSET", "FLOOR_HEIGHTABOVELEVEL_PARAM",
    "CEILING_HEIGHTABOVELEVEL_PARAM", "ROOF_LEVEL_OFFSET_PARAM",
    "ROOF_CONSTRAINT_OFFSET_PARAM", "STAIRS_BASE_OFFSET",
]
_LEVEL_PARAM_NAME_FALLBACK = ("Level", "Base Constraint", "Reference Level", "Schedule Level")
_OFFSET_PARAM_NAME_FALLBACK = ("Elevation from Level", "Offset", "Base Offset", "Height Offset From Level")


# -- defensive reads (same patterns already proven elsewhere in this extension) --
def _read_name(element):
    if element is None:
        return None
    try:
        n = element.Name
        if n:
            return n
    except Exception:
        pass
    for bip in (BuiltInParameter.SYMBOL_NAME_PARAM, BuiltInParameter.ALL_MODEL_TYPE_NAME,
                BuiltInParameter.DATUM_TEXT):
        try:
            p = element.get_Parameter(bip)
            if p is not None:
                val = p.AsString()
                if val:
                    return val
        except Exception:
            continue
    return None


def _element_id_value(eid):
    try:
        return eid.Value
    except Exception:
        pass
    try:
        return eid.IntegerValue
    except Exception:
        return str(eid)


def _family_and_type_name(doc, el):
    try:
        type_id = el.GetTypeId()
    except Exception:
        return "", ""
    if type_id is None or type_id == ElementId.InvalidElementId:
        return "", ""
    elem_type = doc.GetElement(type_id)
    if elem_type is None:
        return "", ""
    type_name = _read_name(elem_type) or ""
    try:
        family_name = elem_type.FamilyName
    except Exception:
        family_name = ""
    if not family_name:
        family_name = "(System Family)"
    return family_name, type_name


def _get_element_level_id(el):
    try:
        lid = el.LevelId
        if lid is not None and lid != ElementId.InvalidElementId:
            return lid
    except Exception:
        pass
    return ElementId.InvalidElementId


# -- rehost parameter lookup: candidate BuiltInParameters first (a wrong guess
# just doesn't resolve via getattr, never crashes), then name-based fallback --
def _resolve_bip(name):
    return getattr(BuiltInParameter, name, None)


def _find_param_by_candidates(el, candidate_names, storage_type):
    for name in candidate_names:
        bip = _resolve_bip(name)
        if bip is None:
            continue
        try:
            p = el.get_Parameter(bip)
        except Exception:
            p = None
        if p is not None and not p.IsReadOnly and p.StorageType == storage_type:
            return p
    return None


def _find_param_by_display_name(el, name_options, storage_type):
    try:
        for p in el.Parameters:
            try:
                if p.IsReadOnly or p.StorageType != storage_type:
                    continue
                if p.Definition.Name in name_options:
                    return p
            except Exception:
                continue
    except Exception:
        pass
    return None


def find_level_parameter(el):
    p = _find_param_by_candidates(el, _LEVEL_PARAM_CANDIDATES, StorageType.ElementId)
    if p is not None:
        return p
    return _find_param_by_display_name(el, _LEVEL_PARAM_NAME_FALLBACK, StorageType.ElementId)


def find_offset_parameter(el):
    p = _find_param_by_candidates(el, _OFFSET_PARAM_CANDIDATES, StorageType.Double)
    if p is not None:
        return p
    return _find_param_by_display_name(el, _OFFSET_PARAM_NAME_FALLBACK, StorageType.Double)


# -- units (same pattern as DeeLevels) --
def _length_unit_type_id(doc):
    return doc.GetUnits().GetFormatOptions(SpecTypeId.Length).GetUnitTypeId()


def _internal_to_display(doc, value_internal):
    return UnitUtils.ConvertFromInternalUnits(value_internal, _length_unit_type_id(doc))


# -- 3D view helpers (same defensive pattern as DeeBIMview's _ensure_3d_view*,
# including the ElementId coercion workaround for an IronPython dynamic
# method-binder quirk seen in this environment) --
def _as_element_id(value):
    if isinstance(value, ElementId):
        return value
    if hasattr(value, "Id"):
        return value.Id
    return value


def _find_3d_view(doc, name):
    for v in FilteredElementCollector(doc).OfClass(View3D):
        if not v.IsTemplate and v.Name == name:
            return v
    return None


def _ensure_3d_view_type(doc):
    fallback_id = None
    for vft in FilteredElementCollector(doc).OfClass(ViewFamilyType):
        try:
            if vft.ViewFamily != ViewFamily.ThreeDimensional:
                continue
        except Exception:
            continue
        fallback_id = _as_element_id(vft.Id)
        break
    if fallback_id is None:
        raise Exception("No usable 3D ViewFamilyType found in this project")
    return fallback_id


def _ensure_view(doc, name):
    view = _find_3d_view(doc, name)
    if view is not None:
        return view
    type_id = _ensure_3d_view_type(doc)
    try:
        view = View3D.CreateIsometric(doc, _as_element_id(type_id))
    except Exception as e:
        raise Exception("[CreateIsometric] {0}".format(e))
    view.Name = name
    return view


def _get_solid_fill_pattern_id(doc):
    for fp in FilteredElementCollector(doc).OfClass(FillPatternElement):
        try:
            pat = fp.GetFillPattern()
            if pat.IsSolidFill:
                return fp.Id
        except Exception:
            continue
    return ElementId.InvalidElementId


# -- data model --------------------------------------------------------------
class LevelInfo(object):
    def __init__(self, doc, level, name, elevation_internal):
        self.doc = doc
        self.level = level
        self.id = level.Id
        self.name = name
        self.elevation_internal = elevation_internal
        self.color = (200, 200, 200)
        self.element_count = 0
        self.target_level = None  # None => "(No Change)" in the mapping table

    @property
    def elevation_text(self):
        return "{0:.3f}".format(_internal_to_display(self.doc, self.elevation_internal))

    @property
    def element_count_text(self):
        return str(self.element_count)

    @property
    def target_name(self):
        return self.target_level.name if self.target_level is not None else "(No Change)"


class ElementRow(object):
    def __init__(self, element, category_name, family_name, type_name, level_id, level_name):
        self.element = element
        self.id = element.Id
        self.id_text = str(_element_id_value(element.Id))
        self.category_name = category_name or ""
        self.family_name = family_name or ""
        self.type_name = type_name or ""
        self.level_id = level_id
        self.level_name = level_name or ""


class DeeHostLevelWindow(forms.WPFWindow):
    def __init__(self, xaml_file, doc):
        forms.WPFWindow.__init__(self, xaml_file)
        self.doc = doc
        self._levels = []
        self._elements = []
        self._elements_by_level = {}
        self.mapping_panel.Visibility = Visibility.Collapsed
        self._scan()

    # -- scan ------------------------------------------------------------
    def _scan(self):
        doc = self.doc
        old_colors = {lvl.name: lvl.color for lvl in self._levels}
        old_targets = {lvl.name: (lvl.target_level.name if lvl.target_level else None)
                       for lvl in self._levels}

        levels = list(FilteredElementCollector(doc).OfClass(Level))
        level_infos = []
        for lvl in sorted(levels, key=lambda l: l.Elevation):
            name = _read_name(lvl) or "(unnamed)"
            level_infos.append(LevelInfo(doc, lvl, name, lvl.Elevation))
        level_by_id = {info.id: info for info in level_infos}

        elements = []
        elements_by_level = {}
        collector = list(FilteredElementCollector(doc).WhereElementIsNotElementType())
        total = len(collector)
        with forms.ProgressBar(title="DeeHostLevel — scanning model...", cancellable=True) as pb:
            for i, el in enumerate(collector):
                if pb.cancelled:
                    break
                if i % 200 == 0 or i == total - 1:
                    pb.update_progress(i, total)
                try:
                    if isinstance(el, RevitLinkInstance):
                        continue
                    cat = el.Category
                    if cat is None:
                        continue
                    try:
                        if cat.CategoryType != CategoryType.Model:
                            continue
                    except Exception:
                        continue
                    level_id = _get_element_level_id(el)
                    level_info = level_by_id.get(level_id)
                    if level_info is None:
                        continue
                    cat_name = _read_name(cat) or ""
                    fam_name, type_name = _family_and_type_name(doc, el)
                    row = ElementRow(el, cat_name, fam_name, type_name, level_id, level_info.name)
                    elements.append(row)
                    elements_by_level.setdefault(level_id, []).append(row)
                    level_info.element_count += 1
                except Exception:
                    continue

        palette = generate_level_colors(len(level_infos))
        for i, info in enumerate(level_infos):
            info.color = old_colors.get(info.name, palette[i])

        name_to_info = {info.name: info for info in level_infos}
        for info in level_infos:
            prev_target_name = old_targets.get(info.name)
            if prev_target_name and prev_target_name in name_to_info:
                info.target_level = name_to_info[prev_target_name]

        self._levels = level_infos
        self._elements = elements
        self._elements_by_level = elements_by_level
        self._refresh_all_ui()

    def _refresh_all_ui(self):
        self.summary_tb.Text = "{0} level(s), {1} element(s) scanned.".format(
            len(self._levels), len(self._elements))

        self.levels_grid.ItemsSource = None
        self.levels_grid.ItemsSource = self._levels
        self.elements_grid.ItemsSource = None
        self.elements_grid.ItemsSource = self._elements
        self._refresh_mapping_grid()
        self._rebuild_color_list()

        names = [info.name for info in self._levels]
        self.consolidate_target_cb.ItemsSource = None
        self.consolidate_target_cb.ItemsSource = names
        if names:
            self.consolidate_target_cb.SelectedIndex = 0

        self.mapping_target_cb.ItemsSource = None
        self.mapping_target_cb.ItemsSource = ["(No Change)"] + names
        self.mapping_target_cb.SelectedIndex = 0

    def _refresh_mapping_grid(self):
        selected = list(self.mapping_grid.SelectedItems)
        self.mapping_grid.ItemsSource = None
        self.mapping_grid.ItemsSource = self._levels
        for row in selected:
            if row in self._levels:
                self.mapping_grid.SelectedItems.Add(row)

    def _name_to_level_info(self):
        return {info.name: info for info in self._levels}

    # -- color list (built in code, not XAML - avoids needing a WPF value
    # converter just to bind a Rectangle.Fill to a color) -----------------
    def _rebuild_color_list(self):
        panel = self.color_list_panel
        panel.Children.Clear()
        for lvl in self._levels:
            row = StackPanel()
            row.Orientation = Orientation.Horizontal
            row.Margin = Thickness(0, 0, 0, 6)

            swatch = Rectangle()
            swatch.Width = 24
            swatch.Height = 24
            swatch.Stroke = Brushes.Black
            swatch.StrokeThickness = 1
            swatch.Fill = SolidColorBrush(MediaColor.FromRgb(*lvl.color))
            row.Children.Add(swatch)

            label = TextBlock()
            label.Text = "{0}   (Elevation {1}, {2} element(s))".format(
                lvl.name, lvl.elevation_text, lvl.element_count_text)
            label.VerticalAlignment = VerticalAlignment.Center
            label.Margin = Thickness(10, 0, 10, 0)
            label.Width = 380
            row.Children.Add(label)

            btn = Button()
            btn.Content = "Change Color..."
            btn.Width = 130
            btn.Height = 22
            btn.Tag = lvl
            btn.Click += self._change_color_click
            row.Children.Add(btn)

            panel.Children.Add(row)

    def _change_color_click(self, sender, args):
        lvl = sender.Tag
        dlg = ColorDialog()
        dlg.Color = DrawingColor.FromArgb(lvl.color[0], lvl.color[1], lvl.color[2])
        dlg.FullOpen = True
        if dlg.ShowDialog() == DialogResult.OK:
            c = dlg.Color
            lvl.color = (c.R, c.G, c.B)
            self._rebuild_color_list()

    def autocolor_click(self, sender, args):
        if not self._levels:
            forms.alert("Scan the model first.")
            return
        palette = generate_level_colors(len(self._levels))
        for i, lvl in enumerate(self._levels):
            lvl.color = palette[i]
        self._rebuild_color_list()

    # -- generate/update the color-coded 3D view --------------------------
    def generate_view_click(self, sender, args):
        if not self._levels:
            forms.alert("Scan the model first.")
            return
        doc = self.doc
        t = Transaction(doc, "DeeHostLevel - Generate Color View")
        t.Start()
        try:
            view = _ensure_view(doc, _VIEW_NAME)
            solid_fill_id = _get_solid_fill_pattern_id(doc)
            total = len(self._elements)
            with forms.ProgressBar(title="DeeHostLevel — coloring elements...", cancellable=True) as pb:
                done = 0
                for lvl in self._levels:
                    r, g, b = lvl.color
                    revit_color = RevitColor(r, g, b)
                    ogs = OverrideGraphicSettings()
                    if solid_fill_id != ElementId.InvalidElementId:
                        ogs.SetSurfaceForegroundPatternColor(revit_color)
                        ogs.SetSurfaceForegroundPatternId(solid_fill_id)
                        ogs.SetSurfaceForegroundPatternVisible(True)
                        ogs.SetCutForegroundPatternColor(revit_color)
                        ogs.SetCutForegroundPatternId(solid_fill_id)
                        ogs.SetCutForegroundPatternVisible(True)
                    ogs.SetProjectionLineColor(revit_color)
                    for row in self._elements_by_level.get(lvl.id, []):
                        if pb.cancelled:
                            break
                        done += 1
                        if done % 200 == 0 or done == total:
                            pb.update_progress(done, total)
                        try:
                            view.SetElementOverrides(row.id, ogs)
                        except Exception:
                            continue
                    if pb.cancelled:
                        break
            t.Commit()
        except Exception as e:
            t.RollBack()
            forms.alert("Could not generate view: {0}".format(e))
            return
        forms.alert("View '{0}' generated/updated.".format(_VIEW_NAME), title="DeeHostLevel")

    # -- rehost ------------------------------------------------------------
    def rehost_mode_changed(self, sender, args):
        if not hasattr(self, "consolidate_panel"):
            return
        if self.mode_consolidate_rb.IsChecked:
            self.consolidate_panel.Visibility = Visibility.Visible
            self.mapping_panel.Visibility = Visibility.Collapsed
        else:
            self.consolidate_panel.Visibility = Visibility.Collapsed
            self.mapping_panel.Visibility = Visibility.Visible

    def set_mapping_target_click(self, sender, args):
        selected_rows = list(self.mapping_grid.SelectedItems)
        if not selected_rows:
            forms.alert("Select one or more Source Level rows first.")
            return
        choice = self.mapping_target_cb.SelectedItem
        if choice is None:
            return
        target = None if choice == "(No Change)" else self._name_to_level_info().get(choice)
        for row in selected_rows:
            row.target_level = target
        self._refresh_mapping_grid()

    def apply_rehost_click(self, sender, args):
        if not self._levels or not self._elements:
            forms.alert("Scan the model first.")
            return

        keep_in_place = bool(self.keep_in_place_cb.IsChecked)
        name_to_info = self._name_to_level_info()

        if self.mode_consolidate_rb.IsChecked:
            target_name = self.consolidate_target_cb.SelectedItem
            target_lvl = name_to_info.get(target_name)
            if target_lvl is None:
                forms.alert("Pick a target Level first.")
                return
            mapping = {lvl.id: target_lvl for lvl in self._levels if lvl.id != target_lvl.id}
            mode_desc = "Consolidate all elements onto '{0}'".format(target_lvl.name)
        else:
            mapping = {}
            for lvl in self._levels:
                if lvl.target_level is not None and lvl.target_level.id != lvl.id:
                    mapping[lvl.id] = lvl.target_level
            if not mapping:
                forms.alert("No target Level changes set in the mapping table.")
                return
            mode_desc = "Level-to-Level mapping"

        total_affected = sum(len(self._elements_by_level.get(src_id, [])) for src_id in mapping)
        if total_affected == 0:
            forms.alert("No elements to rehost with the current settings.")
            return
        if not forms.alert(
                "{0}\n\n{1} element(s) will be rehosted.\nKeep in current physical position: {2}\n\n"
                "Continue?".format(mode_desc, total_affected, "Yes" if keep_in_place else "No"),
                title="DeeHostLevel - Confirm Rehost", yes=True, no=True):
            return

        results = []
        t = Transaction(self.doc, "DeeHostLevel - Rehost Elements")
        t.Start()
        with forms.ProgressBar(title="DeeHostLevel — rehosting...", cancellable=True) as pb:
            done = 0
            for src_id, target_lvl in mapping.items():
                src_lvl = next((l for l in self._levels if l.id == src_id), None)
                if src_lvl is None:
                    continue
                rows = self._elements_by_level.get(src_id, [])
                for row in rows:
                    if pb.cancelled:
                        break
                    done += 1
                    if done % 100 == 0 or done == total_affected:
                        pb.update_progress(done, total_affected)
                    el = row.element
                    try:
                        level_param = find_level_parameter(el)
                        if level_param is None:
                            results.append((False, row.id_text,
                                            "No writable Level parameter found - skipped"))
                            continue
                        offset_param = find_offset_parameter(el)
                        old_offset = offset_param.AsDouble() if offset_param is not None else 0.0
                        level_param.Set(target_lvl.id)
                        if offset_param is not None:
                            new_offset = compute_rehosted_offset(
                                src_lvl.elevation_internal, target_lvl.elevation_internal,
                                old_offset, keep_in_place)
                            offset_param.Set(new_offset)
                            results.append((True, row.id_text,
                                            "Rehosted to '{0}'".format(target_lvl.name)))
                        elif keep_in_place:
                            results.append((True, row.id_text,
                                            "Rehosted to '{0}' - no offset parameter found, "
                                            "could not compensate position".format(target_lvl.name)))
                        else:
                            results.append((True, row.id_text,
                                            "Rehosted to '{0}'".format(target_lvl.name)))
                    except Exception as e:
                        results.append((False, row.id_text, "FAILED: {0}".format(e)))
        t.Commit()

        ok_count = sum(1 for ok, _, _ in results if ok)
        fail_count = len(results) - ok_count
        html = '<h2 style="font-family:sans-serif;color:#ddd;">DeeHostLevel Rehost Results</h2>'
        html += '<p style="color:#ddd;">{0} succeeded, {1} failed/skipped.</p>'.format(ok_count, fail_count)
        for ok, id_text, detail in results:
            bg = "#2e7d32" if ok else "#c62828"
            icon = "&#10003;" if ok else "&#10007;"
            html += (
                '<div style="padding:4px 10px;margin:2px 0;background:{0};color:#fff;'
                'border-radius:4px;font-family:monospace;font-size:12px;">'
                '{1}&nbsp; <b>{2}</b> &mdash; {3}</div>'.format(bg, icon, id_text, detail))
        output.print_html(html)

        self._scan()

    # -- report / export ----------------------------------------------------
    def scan_click(self, sender, args):
        self._scan()

    def export_csv_click(self, sender, args):
        if not self._elements:
            forms.alert("No elements to export - scan first.")
            return
        dlg = SaveFileDialog()
        dlg.Filter = "CSV files (*.csv)|*.csv"
        dlg.FileName = "DeeHostLevel_Export.csv"
        if dlg.ShowDialog() != DialogResult.OK:
            return
        try:
            with open(dlg.FileName, "w") as f:
                writer = csv.writer(f)
                writer.writerow(["Id", "Category", "Family", "Type", "Level"])
                for row in self._elements:
                    writer.writerow([row.id_text, row.category_name, row.family_name,
                                      row.type_name, row.level_name])
        except Exception as e:
            forms.alert("Could not export: {0}".format(e))
            return
        MessageBox.Show("Exported {0} element(s) to:\n{1}".format(len(self._elements), dlg.FileName),
                         "DeeHostLevel")

    def close_click(self, sender, args):
        self.Close()


def main():
    doc = __revit__.ActiveUIDocument.Document
    window = DeeHostLevelWindow(_XAML_FILE, doc)
    window.ShowDialog()


main()
