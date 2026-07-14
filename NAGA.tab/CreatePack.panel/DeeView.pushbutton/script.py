# -*- coding: utf-8 -*-
"""
DeeView
Batch-creates views (Floor/Ceiling/Structural/Area plans) for chosen
levels, via a 4-step wizard (tabs, not separate pages - far simpler to
build reliably than a true paged wizard while giving the same
step-through feel):

  1. Levels    - searchable, checkbox multi-select list of project
                 levels. This is a convenience pool: checking levels
                 here just seeds the level set for newly-added rows in
                 Step 2 - each row owns its own level list from then on.
  2. Views     - an editable grid of view configs (Enabled, View Type,
                 View Template, Scale, Prefix, Suffix, Place on Sheet,
                 Levels). Each enabled row creates one view per level
                 assigned to THAT row - different rows can target
                 different levels. Select one or more rows and use
                 "Pick Levels for Selected Row(s)" to assign/change
                 which levels a row applies to. A side panel shows a
                 live, multi-line naming preview (one line per planned
                 view name). View Type / View Template are dropdowns
                 populated from this project's actual ViewFamilyTypes/
                 templates - not a hardcoded list.
  3. Sheets    - Create Views Only or Place on Existing Sheets (DeeView
                 never creates sheets), a sheet-assignment rule for
                 matching existing sheets (per level / per view
                 type-discipline / per Building parameter / manual
                 mapping), viewport type, placement position (6
                 anchors or custom coordinates), offsets, and spacing
                 between multiple viewports on one sheet. Includes an
                 illustrative layout preview (real viewport size is
                 only known once a view is actually created, so this
                 is a placeholder-box preview of the layout pattern,
                 not a pixel-exact forecast).
  4. Preview & Run - resolves the full per-row/per-level plan,
                 including duplicate-name resolution, shows a summary
                 and per-view grid, then runs everything in one
                 Transaction with a cancellable progress bar and a
                 detailed HTML report.

Presets (view configs, their assigned levels by name, and all Step 3
settings) can be saved/loaded to a local Presets/ folder, or imported/
exported as a plain JSON file for sharing as a company standard - level
assignments are stored by name so a preset can be reused across
different projects.

Two deliberate simplifications versus the full spec: view-config rows
are reordered with Move Up/Down buttons rather than full drag-and-drop
(row order doesn't affect what gets created, so the lighter mechanism
is enough), and there's no "estimated execution time" in the preview
(not a meaningful number to compute in advance).
"""
import os
import json
import csv
import System
from pyrevit import forms, script
from Autodesk.Revit.DB import (
    FilteredElementCollector, Level, Transaction, BuiltInParameter, BuiltInCategory,
    View, ViewFamily, ViewFamilyType, ViewPlan, ViewType, AreaScheme,
    ViewSheet, Viewport, XYZ, StorageType
)

import clr
clr.AddReference("System.Windows.Forms")
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
from System.Windows.Forms import OpenFileDialog, SaveFileDialog, DialogResult, MessageBox
from System.Windows.Shapes import Rectangle
from System.Windows.Controls import Canvas, TextBlock
from System.Windows.Media import Brushes, SolidColorBrush, Color

output = script.get_output()

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")
_PRESETS_DIR = os.path.join(_THIS_DIR, "Presets")

_PLAN_FAMILIES = [ViewFamily.FloorPlan, ViewFamily.CeilingPlan,
                  ViewFamily.StructuralPlan, ViewFamily.AreaPlan]

_FAMILY_TO_VIEWTYPE = {
    ViewFamily.FloorPlan: ViewType.FloorPlan,
    ViewFamily.CeilingPlan: ViewType.CeilingPlan,
    ViewFamily.StructuralPlan: ViewType.EngineeringPlan,
    ViewFamily.AreaPlan: ViewType.AreaPlan,
}

_POSITION_OPTIONS = ["Center", "Top-Left", "Top-Right", "Bottom-Left", "Bottom-Right", "Custom"]
_DUPLICATE_MODES = ["Skip", "Replace", "Rename Automatically", "Ask Each Time"]

_NORMAL_BRUSH = SolidColorBrush(Color.FromRgb(70, 130, 180))
_SELECTED_BRUSH = SolidColorBrush(Color.FromRgb(220, 20, 60))


# ── name-fallback helper (established pattern for this Revit/IronPython combo) ──

def _read_name(element):
    try:
        return element.Name
    except Exception:
        pass
    for bip in (BuiltInParameter.SYMBOL_NAME_PARAM, BuiltInParameter.ALL_MODEL_TYPE_NAME,
                BuiltInParameter.DATUM_TEXT, BuiltInParameter.VIEW_NAME,
                BuiltInParameter.SHEET_NAME):
        try:
            p = element.get_Parameter(bip)
            if p is not None:
                val = p.AsString()
                if val:
                    return val
        except Exception:
            continue
    return None


def _lookup_param_value(element, paraname):
    try:
        p = element.LookupParameter(paraname)
    except Exception:
        p = None
    if not p:
        return None
    if p.StorageType == StorageType.String:
        return p.AsString()
    elif p.StorageType == StorageType.Integer:
        return p.AsInteger()
    elif p.StorageType == StorageType.Double:
        return p.AsDouble()
    return None


# ── project discovery ────────────────────────────────────────────────────────

def _collect_levels(doc):
    levels = list(FilteredElementCollector(doc).OfClass(Level))
    return sorted(levels, key=lambda l: l.Elevation)


def _collect_plan_view_family_types(doc):
    types = list(FilteredElementCollector(doc).OfClass(ViewFamilyType))
    result = []
    for t in types:
        try:
            if t.ViewFamily in _PLAN_FAMILIES:
                nm = _read_name(t)
                if nm:
                    result.append((nm, t))
        except Exception:
            continue
    return result


def _collect_templates(doc):
    views = list(FilteredElementCollector(doc).OfClass(View))
    result = []
    for v in views:
        try:
            if v.IsTemplate:
                nm = _read_name(v)
                if nm:
                    result.append((nm, v))
        except Exception:
            continue
    return result


def _collect_area_schemes(doc):
    return list(FilteredElementCollector(doc).OfClass(AreaScheme))


def _collect_viewport_types(doc):
    types = list(FilteredElementCollector(doc)
                 .OfCategory(BuiltInCategory.OST_Viewports)
                 .WhereElementIsElementType())
    result = []
    for t in types:
        nm = _read_name(t)
        if nm:
            result.append((nm, t))
    return result


def _collect_sheets(doc):
    return list(FilteredElementCollector(doc).OfClass(ViewSheet))


def _sheet_label(sheet):
    try:
        return "{0} - {1}".format(sheet.SheetNumber, sheet.Name)
    except Exception:
        return str(sheet.Id)


# ── naming / duplicate resolution ───────────────────────────────────────────

def _compose_view_name(prefix, level_name, vft_name, suffix):
    parts = [p for p in [prefix, level_name, vft_name] if p]
    name = " - ".join(parts)
    if suffix:
        name = "{0} {1}".format(name, suffix)
    return name


def _resolve_duplicate_name(base_name, taken_names, mode):
    """Returns (display_name, conflict_label). display_name is what the
    plan/report shows; actual skip/replace/ask decisions happen at run
    time against the live model (names can change between preview and
    run if the user keeps the window open and edits things)."""
    if base_name not in taken_names:
        return base_name, ""
    if mode == "Skip":
        return base_name, "Skipped (exists)"
    if mode == "Replace":
        return base_name, "Will replace existing"
    if mode == "Ask Each Time":
        return base_name, "Will ask (exists)"
    # Rename Automatically
    n = 2
    while True:
        candidate = "{0} ({1})".format(base_name, n)
        if candidate not in taken_names:
            return candidate, "Renamed (exists)"
        n += 1


def _existing_view_names(doc):
    names = set()
    for v in FilteredElementCollector(doc).OfClass(View):
        try:
            if not v.IsTemplate:
                names.add(v.Name)
        except Exception:
            continue
    return names


def _template_matches_vft(template, vft):
    """Validates a View Template against a View Type's expected ViewType,
    since Revit itself will reject applying an incompatible template
    (e.g. a Ceiling Plan template on a Floor Plan view)."""
    expected = _FAMILY_TO_VIEWTYPE.get(vft.ViewFamily)
    if expected is None:
        return True
    try:
        return template.ViewType == expected
    except Exception:
        return True


def _find_view_by_name(doc, name):
    for v in FilteredElementCollector(doc).OfClass(View):
        try:
            if not v.IsTemplate and v.Name == name:
                return v
        except Exception:
            continue
    return None


# ── sheet-assignment grouping ────────────────────────────────────────────────

def _sheet_group_key(rule, level, row, building_param_name):
    if rule == "One Sheet per View Type (Discipline)":
        return ("viewtype", row.vft_name)
    if rule == "One Sheet per Building Parameter":
        val = _lookup_param_value(level, building_param_name)
        return ("building", str(val) if val is not None else "Unspecified")
    return ("level", str(level.Id))


def _sheet_group_label(rule, level, row, building_param_name):
    if rule == "One Sheet per View Type (Discipline)":
        return row.vft_name
    if rule == "One Sheet per Building Parameter":
        val = _lookup_param_value(level, building_param_name)
        return str(val) if val is not None else "Unspecified"
    return _read_name(level) or "Level"


# ── viewport layout (simple left-to-right, top-to-bottom flow from the
# chosen anchor - a deliberate simplification over a direction-aware
# per-corner packing algorithm, which would be much more fragile) ──────────

def _sheet_anchor(position, outline, custom_x, custom_y, offset_x, offset_y, margin=0.5):
    min_u, min_v = outline.Min.U, outline.Min.V
    max_u, max_v = outline.Max.U, outline.Max.V
    if position == "Custom":
        x, y = custom_x, custom_y
    elif position == "Top-Left":
        x, y = min_u + margin, max_v - margin
    elif position == "Top-Right":
        x, y = max_u - margin, max_v - margin
    elif position == "Bottom-Left":
        x, y = min_u + margin, min_v + margin
    elif position == "Bottom-Right":
        x, y = max_u - margin, min_v + margin
    else:
        x, y = (min_u + max_u) / 2.0, (min_v + max_v) / 2.0
    return x + offset_x, y + offset_y


def _next_viewport_center(cursor, half_w, half_h, outline, h_spacing, v_spacing, margin=0.5):
    if cursor.get("is_first", True):
        cx, cy = cursor["x"], cursor["y"]
        cursor["is_first"] = False
        cursor["row_start_x"] = cx
        cursor["row_max_half_h"] = half_h
        cursor["next_x"] = cx + half_w + h_spacing
        cursor["y"] = cy
        return cx, cy

    cx = cursor["next_x"] + half_w
    cy = cursor["y"]
    if cx + half_w > outline.Max.U - margin:
        cx = cursor["row_start_x"] + half_w
        cy = cursor["y"] - (cursor["row_max_half_h"] * 2 + v_spacing)
        cursor["row_max_half_h"] = half_h
    else:
        cursor["row_max_half_h"] = max(cursor["row_max_half_h"], half_h)
    cursor["next_x"] = cx + half_w + h_spacing
    cursor["y"] = cy
    return cx, cy


# ── data model classes ───────────────────────────────────────────────────────

class LevelOption(object):
    def __init__(self, level):
        self.level = level
        self.state = False

    def __nonzero__(self):
        return self.state
    __bool__ = __nonzero__

    @property
    def name(self):
        return _read_name(self.level) or "(unnamed)"


class _LevelPickItem(forms.TemplateListItem):
    """SelectFromList silently re-wraps (and un-checks) any context item
    that isn't already a TemplateListItem, so a pre-seeded checked state
    would otherwise be lost - subclassing directly, per pyRevit's own
    documented pattern, keeps the seeded state intact."""

    @property
    def name(self):
        return _read_name(self.item) or "(unnamed)"


class ViewConfigRow(object):
    def __init__(self, vft_name="", template_name="(None)", scale=100,
                 prefix="", suffix="", enabled=True, place_on_sheet=True, levels=None):
        self.enabled = enabled
        self.vft_name = vft_name
        self.template_name = template_name
        self._scale = scale
        self.prefix = prefix
        self.suffix = suffix
        self.place_on_sheet = place_on_sheet
        self.levels = list(levels) if levels else []

    @property
    def scale(self):
        return self._scale

    @property
    def scale_text(self):
        return str(self._scale)

    @scale_text.setter
    def scale_text(self, value):
        try:
            self._scale = int(float(value))
        except (ValueError, TypeError):
            return

    @property
    def levels_text(self):
        if not self.levels:
            return "(none - pick levels)"
        names = sorted(_read_name(l) or "?" for l in self.levels)
        if len(names) <= 3:
            return ", ".join(names)
        return "{0}, {1}, ... (+{2} more)".format(names[0], names[1], len(names) - 2)

    def to_dict(self):
        return {
            "enabled": bool(self.enabled), "vft_name": self.vft_name,
            "template_name": self.template_name, "scale": self._scale,
            "prefix": self.prefix, "suffix": self.suffix,
            "place_on_sheet": bool(self.place_on_sheet),
            "level_names": [(_read_name(l) or "") for l in self.levels],
        }

    @classmethod
    def from_dict(cls, d, levels_by_name=None):
        levels_by_name = levels_by_name or {}
        levels = [levels_by_name[n] for n in d.get("level_names", []) if n in levels_by_name]
        return cls(
            vft_name=d.get("vft_name", ""), template_name=d.get("template_name", "(None)"),
            scale=d.get("scale", 100), prefix=d.get("prefix", ""), suffix=d.get("suffix", ""),
            enabled=d.get("enabled", True), place_on_sheet=d.get("place_on_sheet", True),
            levels=levels)


class ManualMapRow(object):
    def __init__(self, level, sheet_label="(None)"):
        self.level = level
        self.sheet_label = sheet_label

    @property
    def level_name(self):
        return _read_name(self.level) or "(unnamed)"


class PlannedView(object):
    def __init__(self, level, row, view_name, sheet_label, conflict_label):
        self.level = level
        self.row = row
        self.view_name = view_name
        self.level_name = _read_name(level) or "(unnamed)"
        self.vft_name = row.vft_name
        self.template_name = row.template_name
        self.scale_label = "1:{0}".format(row.scale)
        self.sheet_label = sheet_label
        self.conflict_label = conflict_label


# ── main window ──────────────────────────────────────────────────────────────

class DeeViewWindow(forms.WPFWindow):
    def __init__(self, xaml_file, doc):
        forms.WPFWindow.__init__(self, xaml_file)
        self.doc = doc

        self._levels = _collect_levels(doc)
        self._level_options = [LevelOption(l) for l in self._levels]
        self.levels_lb.ItemsSource = self._level_options

        self._vft_list = _collect_plan_view_family_types(doc)
        self._vft_by_name = dict(self._vft_list)

        self._template_list = _collect_templates(doc)
        self._template_by_name = dict(self._template_list)

        self._area_schemes = _collect_area_schemes(doc)

        self._viewport_types = _collect_viewport_types(doc)
        self._viewport_type_by_name = dict(self._viewport_types)

        self._sheets = _collect_sheets(doc)
        self._sheet_by_label = {}
        sheet_labels = []
        for s in self._sheets:
            label = _sheet_label(s)
            self._sheet_by_label[label] = s
            sheet_labels.append(label)

        self.view_type_col.ItemsSource = sorted(self._vft_by_name.keys())
        self.template_col.ItemsSource = ["(None)"] + sorted(self._template_by_name.keys())
        self.manual_sheet_col.ItemsSource = ["(None)"] + sorted(sheet_labels)

        self.viewport_type_cb.ItemsSource = ["(Default)"] + sorted(self._viewport_type_by_name.keys())
        self.viewport_type_cb.SelectedIndex = 0

        self.position_cb.ItemsSource = _POSITION_OPTIONS
        self.position_cb.SelectedIndex = 0

        self.duplicate_mode_cb.ItemsSource = _DUPLICATE_MODES
        self.duplicate_mode_cb.SelectedIndex = 2

        self._rows = []
        self._manual_map_rows = []
        self._plan = []

        if not os.path.exists(_PRESETS_DIR):
            try:
                os.makedirs(_PRESETS_DIR)
            except Exception:
                pass
        self._refresh_preset_list()

        self.wizard_tabs.SelectedIndex = 0

    # -- Step 1: Levels -------------------------------------------------------
    def level_search_changed(self, sender, args):
        filt = self.level_search_tb.Text.lower()
        if filt:
            self.levels_lb.ItemsSource = [o for o in self._level_options if filt in o.name.lower()]
        else:
            self.levels_lb.ItemsSource = self._level_options

    def check_all_levels_click(self, sender, args):
        for o in self.levels_lb.ItemsSource:
            o.state = True
        self._refresh_levels_list()

    def uncheck_all_levels_click(self, sender, args):
        for o in self.levels_lb.ItemsSource:
            o.state = False
        self._refresh_levels_list()

    def _refresh_levels_list(self):
        items = self.levels_lb.ItemsSource
        self.levels_lb.ItemsSource = None
        self.levels_lb.ItemsSource = items

    # -- Step 2: Views ----------------------------------------------------------
    def _refresh_views_grid(self):
        self.views_grid.ItemsSource = None
        self.views_grid.ItemsSource = list(self._rows)

    def add_row_click(self, sender, args):
        default_vft = sorted(self._vft_by_name.keys())[0] if self._vft_by_name else ""
        seed_levels = [o.level for o in self._level_options if o.state]
        self._rows.append(ViewConfigRow(vft_name=default_vft, levels=seed_levels))
        self._refresh_views_grid()

    def remove_row_click(self, sender, args):
        selected = list(self.views_grid.SelectedItems)
        if not selected:
            forms.alert("Select one or more rows first.")
            return
        for row in selected:
            self._rows.remove(row)
        self._refresh_views_grid()

    def pick_levels_for_rows_click(self, sender, args):
        selected = list(self.views_grid.SelectedItems)
        if not selected:
            forms.alert("Select one or more rows first.")
            return
        seed_levels = selected[0].levels
        level_items = [_LevelPickItem(l, checked=(l in seed_levels)) for l in self._levels]
        result = forms.SelectFromList.show(
            level_items, multiselect=True,
            button_name="Apply", title="DeeView - Pick Levels for Selected Row(s)")
        # SelectFromList already unwraps TemplateListItem back to the plain
        # Level objects and filters to only the checked ones - result IS
        # the chosen level list already, nothing further to unwrap/filter.
        if result is None:
            return
        for row in selected:
            row.levels = list(result)
        self._refresh_views_grid()

    def move_row_up_click(self, sender, args):
        row = self.views_grid.SelectedItem
        if not row:
            return
        idx = self._rows.index(row)
        if idx > 0:
            self._rows[idx - 1], self._rows[idx] = self._rows[idx], self._rows[idx - 1]
            self._refresh_views_grid()
            self.views_grid.SelectedItem = row

    def move_row_down_click(self, sender, args):
        row = self.views_grid.SelectedItem
        if not row:
            return
        idx = self._rows.index(row)
        if idx < len(self._rows) - 1:
            self._rows[idx + 1], self._rows[idx] = self._rows[idx], self._rows[idx + 1]
            self._refresh_views_grid()
            self.views_grid.SelectedItem = row

    def views_row_edit_ending(self, sender, args):
        try:
            self.Dispatcher.BeginInvoke(System.Action(self._refresh_views_grid))
        except Exception:
            self._refresh_views_grid()

    def preview_naming_click(self, sender, args):
        enabled_rows = [r for r in self._rows if r.enabled]
        lines = []
        for row in enabled_rows:
            for level in row.levels:
                level_name = _read_name(level) or "(unnamed)"
                lines.append(_compose_view_name(row.prefix, level_name, row.vft_name, row.suffix))
        if not lines:
            self.naming_preview_lb.ItemsSource = [
                "Enable at least one row and assign it levels (Pick Levels for Selected Row(s))."]
            return
        cap = 300
        display_lines = lines[:cap]
        if len(lines) > cap:
            display_lines.append("... (+{0} more)".format(len(lines) - cap))
        self.naming_preview_lb.ItemsSource = display_lines

    # -- Step 3: Sheets -----------------------------------------------------
    def _sheet_mode(self):
        if self.mode_existing_rb.IsChecked:
            return "Place on Existing Sheets"
        return "Create Views Only"

    def _sheet_rule(self):
        if self.rule_per_viewtype_rb.IsChecked:
            return "One Sheet per View Type (Discipline)"
        if self.rule_per_building_rb.IsChecked:
            return "One Sheet per Building Parameter"
        if self.rule_manual_rb.IsChecked:
            return "Manual Mapping"
        return "One Sheet per Level"

    def position_changed(self, sender, args):
        is_custom = (self.position_cb.SelectedItem == "Custom")
        self.custom_x_tb.IsEnabled = is_custom
        self.custom_y_tb.IsEnabled = is_custom

    def load_manual_map_click(self, sender, args):
        checked_levels = [o.level for o in self._level_options if o.state]
        if not checked_levels:
            forms.alert("Select at least one level in Step 1 first.")
            return
        self._manual_map_rows = [ManualMapRow(l) for l in checked_levels]
        self.manual_map_grid.ItemsSource = None
        self.manual_map_grid.ItemsSource = list(self._manual_map_rows)

    def preview_layout_click(self, sender, args):
        self._draw_layout_preview()

    def _draw_layout_preview(self):
        canvas = self.layout_canvas
        canvas.Children.Clear()
        height = canvas.ActualHeight if canvas.ActualHeight > 1 else 220.0
        width = canvas.ActualWidth if canvas.ActualWidth > 1 else 600.0

        margin = 10.0
        sheet_rect = Rectangle()
        sheet_w = width - 2 * margin
        sheet_h = height - 2 * margin
        sheet_rect.Width = sheet_w
        sheet_rect.Height = sheet_h
        sheet_rect.Stroke = _NORMAL_BRUSH
        sheet_rect.StrokeThickness = 2
        Canvas.SetLeft(sheet_rect, margin)
        Canvas.SetTop(sheet_rect, margin)
        canvas.Children.Add(sheet_rect)

        enabled_rows = [r for r in self._rows if r.enabled and r.place_on_sheet]
        if not enabled_rows:
            return

        position = self.position_cb.SelectedItem or "Center"
        try:
            h_spacing_px = float(self.h_spacing_tb.Text) * 20.0
        except Exception:
            h_spacing_px = 10.0
        try:
            v_spacing_px = float(self.v_spacing_tb.Text) * 20.0
        except Exception:
            v_spacing_px = 10.0

        box_w, box_h = 60.0, 45.0
        cols = max(1, int(sheet_w // (box_w + h_spacing_px)))

        start_x = margin + 8
        start_y = margin + 8
        if position == "Top-Right":
            start_x = margin + sheet_w - box_w - 8
        if position == "Bottom-Left":
            start_y = margin + sheet_h - box_h - 8
        if position == "Bottom-Right":
            start_x = margin + sheet_w - box_w - 8
            start_y = margin + sheet_h - box_h - 8
        if position == "Center":
            row_count = -(-len(enabled_rows) // cols)
            total_h = row_count * box_h + max(0, row_count - 1) * v_spacing_px
            start_x = margin + (sheet_w - min(cols, len(enabled_rows)) * (box_w + h_spacing_px)) / 2.0
            start_y = margin + max(8.0, (sheet_h - total_h) / 2.0)

        x, y, col_i = start_x, start_y, 0
        for r in enabled_rows:
            box = Rectangle()
            box.Width = box_w
            box.Height = box_h
            box.Stroke = _SELECTED_BRUSH
            box.StrokeThickness = 1.5
            box.Fill = Brushes.Transparent
            Canvas.SetLeft(box, x)
            Canvas.SetTop(box, y)
            canvas.Children.Add(box)

            label = TextBlock()
            label.Text = r.vft_name
            label.FontSize = 9
            Canvas.SetLeft(label, x + 2)
            Canvas.SetTop(label, y + 2)
            canvas.Children.Add(label)

            col_i += 1
            if col_i >= cols:
                col_i = 0
                x = start_x
                y += box_h + v_spacing_px
            else:
                x += box_w + h_spacing_px

    # -- Step 4: Preview & Run ------------------------------------------------
    def _build_plan(self):
        enabled_rows = [r for r in self._rows if r.enabled]
        mode = self._sheet_mode()
        rule = self._sheet_rule()
        building_param = self.building_param_tb.Text or "Building"
        dup_mode = self.duplicate_mode_cb.SelectedItem or "Rename Automatically"

        taken_names = _existing_view_names(self.doc)
        plan = []
        sheet_labels_by_key = {}

        manual_map = {}
        if rule == "Manual Mapping":
            for mr in self._manual_map_rows:
                manual_map[str(mr.level.Id)] = mr.sheet_label

        for row in enabled_rows:
            for level in row.levels:
                level_name = _read_name(level) or "(unnamed)"
                base_name = _compose_view_name(row.prefix, level_name, row.vft_name, row.suffix)
                display_name, conflict_label = _resolve_duplicate_name(base_name, taken_names, dup_mode)
                taken_names.add(display_name)

                if row.template_name and row.template_name != "(None)":
                    vft = self._vft_by_name.get(row.vft_name)
                    template = self._template_by_name.get(row.template_name)
                    if vft is not None and template is not None and not _template_matches_vft(template, vft):
                        mismatch = "Template/View Type mismatch!"
                        conflict_label = "{0} {1}".format(conflict_label, mismatch).strip()

                sheet_label = "(none)"
                if mode == "Place on Existing Sheets" and row.place_on_sheet:
                    if rule == "Manual Mapping":
                        sheet_label = manual_map.get(str(level.Id), "(None)")
                    else:
                        key = _sheet_group_key(rule, level, row, building_param)
                        if key not in sheet_labels_by_key:
                            sheet_labels_by_key[key] = _sheet_group_label(rule, level, row, building_param)
                        sheet_label = sheet_labels_by_key[key]

                plan.append(PlannedView(level, row, display_name, sheet_label, conflict_label))
        return plan

    def export_csv_click(self, sender, args):
        if not self._plan:
            forms.alert("Nothing planned yet - click Refresh Preview first.")
            return
        dlg = SaveFileDialog()
        dlg.Filter = "CSV files (*.csv)|*.csv"
        dlg.FileName = "DeeView_Plan.csv"
        if dlg.ShowDialog() != DialogResult.OK:
            return
        try:
            with open(dlg.FileName, "w") as f:
                writer = csv.writer(f)
                writer.writerow(["View Name", "Level", "View Type", "Template", "Scale",
                                  "Target Sheet", "Conflict"])
                for p in self._plan:
                    writer.writerow([p.view_name, p.level_name, p.vft_name, p.template_name,
                                      p.scale_label, p.sheet_label, p.conflict_label])
        except Exception as e:
            forms.alert("Could not export: {0}".format(e))
            return
        MessageBox.Show("Exported {0} planned view(s) to:\n{1}".format(len(self._plan), dlg.FileName),
                         "DeeView")

    def refresh_preview_click(self, sender, args):
        self._plan = self._build_plan()
        self.plan_grid.ItemsSource = None
        self.plan_grid.ItemsSource = self._plan

        skip_count = sum(1 for p in self._plan if p.conflict_label.startswith("Skipped"))
        rename_count = sum(1 for p in self._plan if p.conflict_label.startswith("Renamed"))
        replace_count = sum(1 for p in self._plan if "replace" in p.conflict_label.lower())
        sheet_labels = set(p.sheet_label for p in self._plan if p.sheet_label != "(none)")
        distinct_levels = set(str(p.level.Id) for p in self._plan)

        summary = (
            "Distinct Levels Used: {0}\n"
            "View Config Rows (enabled): {1}\n"
            "Total Planned Views: {2}\n"
            "Sheets Involved: {3}\n"
            "Existing Views to Skip: {4}\n"
            "Existing Views to Rename: {5}\n"
            "Existing Views to Replace: {6}\n"
            "Duplicate Handling Mode: {7}\n"
            "Sheet Mode: {8}   |   Sheet Rule: {9}"
        ).format(
            len(distinct_levels),
            len([r for r in self._rows if r.enabled]),
            len(self._plan), len(sheet_labels), skip_count, rename_count, replace_count,
            self.duplicate_mode_cb.SelectedItem, self._sheet_mode(), self._sheet_rule())
        self.summary_tb.Text = summary

    @staticmethod
    def _safe_float(text, default=0.0):
        try:
            return float(text)
        except (ValueError, TypeError):
            return default

    def run_click(self, sender, args):
        if not self._plan:
            self._plan = self._build_plan()
            self.plan_grid.ItemsSource = None
            self.plan_grid.ItemsSource = self._plan
        if not self._plan:
            forms.alert("Nothing planned - enable at least one view row and assign it levels (Step 2).")
            return

        if not forms.alert(
                "About to create/update {0} view(s). Continue?".format(len(self._plan)),
                title="DeeView - Confirm", yes=True, no=True):
            return

        mode = self._sheet_mode()
        rule = self._sheet_rule()
        building_param = self.building_param_tb.Text or "Building"
        dup_mode = self.duplicate_mode_cb.SelectedItem or "Rename Automatically"
        position = self.position_cb.SelectedItem or "Center"
        offset_x = self._safe_float(self.offset_x_tb.Text)
        offset_y = self._safe_float(self.offset_y_tb.Text)
        custom_x = self._safe_float(self.custom_x_tb.Text)
        custom_y = self._safe_float(self.custom_y_tb.Text)
        h_spacing = self._safe_float(self.h_spacing_tb.Text)
        v_spacing = self._safe_float(self.v_spacing_tb.Text)

        viewport_type_name = self.viewport_type_cb.SelectedItem
        viewport_type = (self._viewport_type_by_name.get(viewport_type_name)
                          if viewport_type_name != "(Default)" else None)

        manual_map = {}
        if rule == "Manual Mapping":
            for mr in self._manual_map_rows:
                manual_map[str(mr.level.Id)] = mr.sheet_label

        results = []
        created_sheets_by_key = {}
        sheet_cursor = {}

        total = len(self._plan)
        with forms.ProgressBar(title="DeeView — creating views...", cancellable=True) as pb:
            t = Transaction(self.doc, "DeeView - Batch Create Views")
            t.Start()

            for i, planned in enumerate(self._plan):
                if pb.cancelled:
                    results.append(("(cancelled)", "Cancelled", None))
                    break
                pb.update_progress(i, total)

                level = planned.level
                row = planned.row
                base_name = _compose_view_name(row.prefix, planned.level_name, row.vft_name, row.suffix)

                vft = self._vft_by_name.get(row.vft_name)
                if vft is None:
                    results.append((base_name, "View Type '{0}' not found".format(row.vft_name), False))
                    continue

                existing_view = _find_view_by_name(self.doc, base_name)
                final_name = base_name
                do_replace = False

                if existing_view is not None:
                    if dup_mode == "Skip":
                        results.append((base_name, "Skipped - already exists", None))
                        continue
                    elif dup_mode == "Replace":
                        do_replace = True
                    elif dup_mode == "Rename Automatically":
                        n = 2
                        while True:
                            candidate = "{0} ({1})".format(base_name, n)
                            if _find_view_by_name(self.doc, candidate) is None:
                                final_name = candidate
                                break
                            n += 1
                    elif dup_mode == "Ask Each Time":
                        choice = forms.SelectFromList.show(
                            ["Skip", "Replace", "Rename"], multiselect=False,
                            title="DeeView - '{0}' already exists".format(base_name))
                        if not choice or choice == "Skip":
                            results.append((base_name, "Skipped by user", None))
                            continue
                        elif choice == "Replace":
                            do_replace = True
                        else:
                            n = 2
                            while True:
                                candidate = "{0} ({1})".format(base_name, n)
                                if _find_view_by_name(self.doc, candidate) is None:
                                    final_name = candidate
                                    break
                                n += 1

                try:
                    if do_replace and existing_view is not None:
                        self.doc.Delete(existing_view.Id)

                    if vft.ViewFamily == ViewFamily.AreaPlan:
                        if not self._area_schemes:
                            results.append((base_name, "No Area Scheme found in project", False))
                            continue
                        new_view = ViewPlan.CreateAreaPlan(self.doc, self._area_schemes[0].Id, level.Id)
                    else:
                        new_view = ViewPlan.Create(self.doc, vft.Id, level.Id)

                    new_view.Name = final_name

                    template_note = ""
                    if row.template_name and row.template_name != "(None)":
                        template = self._template_by_name.get(row.template_name)
                        if template is not None:
                            if _template_matches_vft(template, vft):
                                new_view.ViewTemplateId = template.Id
                            else:
                                template_note = " (template '{0}' skipped - doesn't match View Type)".format(
                                    row.template_name)

                    try:
                        new_view.Scale = row.scale
                    except Exception:
                        pass

                    results.append((final_name, "View created{0}".format(template_note), True))

                    if mode == "Place on Existing Sheets" and row.place_on_sheet:
                        sheet = None
                        if rule == "Manual Mapping":
                            label = manual_map.get(str(level.Id), "(None)")
                            sheet = self._sheet_by_label.get(label)
                            if sheet is None:
                                results.append((final_name, "No target sheet mapped - view created but not placed", None))
                        else:
                            key = _sheet_group_key(rule, level, row, building_param)
                            if key in created_sheets_by_key:
                                sheet = created_sheets_by_key[key]
                            else:
                                label = _sheet_group_label(rule, level, row, building_param)
                                match = None
                                for s in self._sheets:
                                    if s.Name == label or _sheet_label(s) == label:
                                        match = s
                                        break
                                sheet = match
                                created_sheets_by_key[key] = sheet
                            if sheet is None:
                                results.append((final_name, "No matching existing sheet found - view created but not placed", None))

                        if sheet is not None:
                            try:
                                outline = sheet.Outline
                                sheet_key = str(sheet.Id)
                                if sheet_key not in sheet_cursor:
                                    ax, ay = _sheet_anchor(position, outline, custom_x, custom_y, offset_x, offset_y)
                                    sheet_cursor[sheet_key] = {"x": ax, "y": ay, "is_first": True}

                                provisional_pt = XYZ(sheet_cursor[sheet_key]["x"], sheet_cursor[sheet_key]["y"], 0)
                                vp = Viewport.Create(self.doc, sheet.Id, new_view.Id, provisional_pt)
                                if viewport_type is not None:
                                    try:
                                        vp.ChangeTypeId(viewport_type.Id)
                                    except Exception:
                                        pass

                                vp_outline = vp.GetBoxOutline()
                                half_w = (vp_outline.MaximumPoint.X - vp_outline.MinimumPoint.X) / 2.0
                                half_h = (vp_outline.MaximumPoint.Y - vp_outline.MinimumPoint.Y) / 2.0

                                cx, cy = _next_viewport_center(
                                    sheet_cursor[sheet_key], half_w, half_h, outline, h_spacing, v_spacing)
                                vp.SetBoxCenter(XYZ(cx, cy, 0))

                                results.append((final_name, "Placed on sheet '{0}'".format(sheet.Name), True))
                            except Exception as e:
                                results.append((final_name, "Viewport placement FAILED: {0}".format(e), False))

                except Exception as e:
                    results.append((base_name, "View creation FAILED: {0}".format(e), False))

            t.Commit()

        html = '<h2 style="font-family:sans-serif;color:#ddd;">DeeView Results</h2>'
        for name, detail, ok in results:
            bg = "#2e7d32" if ok else ("#455a64" if ok is None else "#c62828")
            icon = "&#10003;" if ok else ("&#9888;" if ok is None else "&#10007;")
            html += (
                '<div style="padding:6px 12px;margin:3px 0;background:{0};color:#fff;'
                'border-radius:4px;font-family:monospace;font-size:13px;">'
                '{1}&nbsp; <b>{2}</b> &mdash; {3}</div>'.format(bg, icon, name, detail))
        output.print_html(html)

    # -- wizard nav -----------------------------------------------------------
    def back_click(self, sender, args):
        if self.wizard_tabs.SelectedIndex > 0:
            self.wizard_tabs.SelectedIndex -= 1

    def next_click(self, sender, args):
        if self.wizard_tabs.SelectedIndex < self.wizard_tabs.Items.Count - 1:
            self.wizard_tabs.SelectedIndex += 1

    def close_click(self, sender, args):
        self.Close()

    # -- presets --------------------------------------------------------------
    def _current_preset_dict(self):
        return {
            "view_configs": [r.to_dict() for r in self._rows],
            "sheet_mode": self._sheet_mode(),
            "sheet_rule": self._sheet_rule(),
            "building_param_name": self.building_param_tb.Text,
            "viewport_type_name": self.viewport_type_cb.SelectedItem,
            "position": self.position_cb.SelectedItem,
            "offset_x": self._safe_float(self.offset_x_tb.Text),
            "offset_y": self._safe_float(self.offset_y_tb.Text),
            "custom_x": self._safe_float(self.custom_x_tb.Text),
            "custom_y": self._safe_float(self.custom_y_tb.Text),
            "h_spacing": self._safe_float(self.h_spacing_tb.Text, 0.5),
            "v_spacing": self._safe_float(self.v_spacing_tb.Text, 0.5),
            "duplicate_mode": self.duplicate_mode_cb.SelectedItem,
        }

    def _apply_preset_dict(self, d):
        levels_by_name = {}
        for l in self._levels:
            nm = _read_name(l)
            if nm:
                levels_by_name[nm] = l
        self._rows = [ViewConfigRow.from_dict(rd, levels_by_name) for rd in d.get("view_configs", [])]
        self._refresh_views_grid()

        mode = d.get("sheet_mode", "Create Views Only")
        self.mode_views_only_rb.IsChecked = (mode == "Create Views Only")
        self.mode_existing_rb.IsChecked = (mode == "Place on Existing Sheets")

        rule = d.get("sheet_rule", "One Sheet per Level")
        self.rule_per_level_rb.IsChecked = (rule == "One Sheet per Level")
        self.rule_per_viewtype_rb.IsChecked = (rule == "One Sheet per View Type (Discipline)")
        self.rule_per_building_rb.IsChecked = (rule == "One Sheet per Building Parameter")
        self.rule_manual_rb.IsChecked = (rule == "Manual Mapping")

        self.building_param_tb.Text = d.get("building_param_name", "Building")
        vpt_name = d.get("viewport_type_name")
        if vpt_name in list(self.viewport_type_cb.ItemsSource):
            self.viewport_type_cb.SelectedItem = vpt_name
        pos = d.get("position")
        if pos in _POSITION_OPTIONS:
            self.position_cb.SelectedItem = pos
        self.offset_x_tb.Text = str(d.get("offset_x", 0.0))
        self.offset_y_tb.Text = str(d.get("offset_y", 0.0))
        self.custom_x_tb.Text = str(d.get("custom_x", 0.0))
        self.custom_y_tb.Text = str(d.get("custom_y", 0.0))
        self.h_spacing_tb.Text = str(d.get("h_spacing", 0.5))
        self.v_spacing_tb.Text = str(d.get("v_spacing", 0.5))
        dup = d.get("duplicate_mode")
        if dup in _DUPLICATE_MODES:
            self.duplicate_mode_cb.SelectedItem = dup

    def _refresh_preset_list(self):
        names = []
        try:
            for fn in os.listdir(_PRESETS_DIR):
                if fn.lower().endswith(".json"):
                    names.append(fn[:-5])
        except Exception:
            pass
        self.preset_cb.ItemsSource = sorted(names)

    def save_preset_click(self, sender, args):
        name = forms.ask_for_string(default="", prompt="Preset name:", title="DeeView - Save Preset")
        if not name:
            return
        try:
            if not os.path.exists(_PRESETS_DIR):
                os.makedirs(_PRESETS_DIR)
            path = os.path.join(_PRESETS_DIR, "{0}.json".format(name))
            with open(path, "w") as f:
                json.dump(self._current_preset_dict(), f, indent=4)
        except Exception as e:
            forms.alert("Could not save preset: {0}".format(e))
            return
        self._refresh_preset_list()
        self.preset_cb.SelectedItem = name

    def load_preset_click(self, sender, args):
        name = self.preset_cb.SelectedItem
        if not name:
            forms.alert("Pick a preset first.")
            return
        path = os.path.join(_PRESETS_DIR, "{0}.json".format(name))
        try:
            with open(path, "r") as f:
                d = json.load(f)
        except Exception as e:
            forms.alert("Could not load preset: {0}".format(e))
            return
        self._apply_preset_dict(d)

    def import_preset_click(self, sender, args):
        dlg = OpenFileDialog()
        dlg.Filter = "JSON files (*.json)|*.json"
        if dlg.ShowDialog() != DialogResult.OK:
            return
        try:
            with open(dlg.FileName, "r") as f:
                d = json.load(f)
        except Exception as e:
            forms.alert("Could not import preset: {0}".format(e))
            return
        self._apply_preset_dict(d)

    def export_preset_click(self, sender, args):
        dlg = SaveFileDialog()
        dlg.Filter = "JSON files (*.json)|*.json"
        dlg.FileName = "DeeView_Preset.json"
        if dlg.ShowDialog() != DialogResult.OK:
            return
        try:
            with open(dlg.FileName, "w") as f:
                json.dump(self._current_preset_dict(), f, indent=4)
        except Exception as e:
            forms.alert("Could not export preset: {0}".format(e))
            return
        MessageBox.Show("Preset exported.", "DeeView")


def main():
    doc = __revit__.ActiveUIDocument.Document
    window = DeeViewWindow(_XAML_FILE, doc)
    window.ShowDialog()


main()
