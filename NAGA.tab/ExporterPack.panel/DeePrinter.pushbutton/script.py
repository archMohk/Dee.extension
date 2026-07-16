# -*- coding: utf-8 -*-
"""
DeePrinter
Batch-export selected sheets to PDF and/or DWG, in one combined window:
  - Filterable checkbox list of sheets (Check/Uncheck/Toggle All)
  - File Naming: drag-and-drop (or Add/Remove/Move Up/Down buttons) to
    build the file name out of an ordered token sequence - parameter
    names, literal separators, and date/time keywords - same naming
    scheme as the reference pyTiBa Export PDF/DWG tools under the hood
  - Folder Structure: drag-and-drop (or Add/Remove/Move Up/Down buttons)
    to build an ordered folder hierarchy out of any sheet parameter
    (Discipline, Building, Level, custom shared/project parameters, etc).
    Applies identically to PDF, DWG, and DXF - all three land in the same
    computed hierarchy folder for a given sheet.
  - Save Settings persists everything to JSON (global dlgval.json +
    per-project naming tokens + folder hierarchy), same persistence
    approach as the reference tools

PDF export uses Revit's native PDFExportOptions API (driver-free - no
PDFCreator/Adobe PDF virtual printer needed, unlike the reference). DWG
export uses the project's own DWG Export Setting, same as Revit's own
Export > DWG (same approach as the reference).
"""
import os
import json
import datetime
from pyrevit import forms, script, framework
from pyrevit.framework import Controls
from Autodesk.Revit.DB import (
    FilteredElementCollector, ViewSheet, ExportDWGSettings, PDFExportOptions,
    ElementId, StorageType, DXFExportOptions
)
from System.Collections.Generic import List

import clr
clr.AddReference("System.Windows.Forms")
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
clr.AddReference("WindowsBase")
from System.Windows.Forms import FolderBrowserDialog, DialogResult, MessageBox
from System.Windows import DataObject, DragDropEffects, DragDrop, Point
from System.Windows.Input import MouseButtonState
from System.Windows.Media import VisualTreeHelper
from System.Windows.Controls import ListBoxItem

output = script.get_output()

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")
_DLGVAL_FILE = os.path.join(_THIS_DIR, "dlgval.json")
_PRJ_DLG_DIR = os.path.join(_THIS_DIR, "Project_Dlg_Data")

DEFAULT_PARANAMES = "Sheet Number,-,Sheet Name,_,date,_,time"
_DRAG_THRESHOLD = 4.0

_SEPARATOR_TOKENS = [
    ("_ (underscore)", "_"),
    ("- (dash)", "-"),
    (". (dot)", "."),
    ("(space)", " "),
    ("; (semicolon)", ";"),
    ("(blank)", ""),
]
_KEYWORD_TOKENS = [
    ("date", "date"),
    ("time", "time"),
]


def _prj_dlg_file(doc):
    return os.path.join(_PRJ_DLG_DIR, "{0}_dlgval.json".format(doc.Title))


# ── naming (ported from the reference namefromparalist / lookupparaval) ────

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


def name_from_paralist(view, paralist):
    """paralist items are either: a literal separator (_, ' ', ., -, ;, ''),
    the keyword date/time, a raw %-strftime code, or a parameter name to
    look up on `view`. Matches the reference tool's naming scheme exactly
    (date -> 25-08-19, time -> 10.34)."""
    now = datetime.datetime.now()
    parts = []
    for i in paralist:
        if i in ["_", " ", ".", "-", ";", ""]:
            parts.append(i)
        elif i.lower() in ("date", "time"):
            fmt = "%d-%m-%y" if i.lower() == "date" else "%H.%M"
            parts.append(now.strftime(fmt))
        elif i.startswith("%"):
            try:
                parts.append(now.strftime(i))
            except Exception:
                pass
        else:
            val = _lookup_param_value(view, i)
            parts.append(str(val) if val is not None else "Error")
    return "".join(parts)


def _sanitize_filename(name):
    invalid = '\\/:*?"<>|'
    for ch in invalid:
        name = name.replace(ch, "_")
    return name.strip()


# ── folder hierarchy (parameter-based export folder structure) ─────────────

def _discover_sheet_parameters(sheets):
    """Union of parameter names found across the given sheets - built-in,
    shared, and project parameters alike - used to populate the "Available
    Parameters" list in the Folder Structure UI. Whatever custom shared/
    project parameters this project actually has (Discipline, Building,
    Zone, Sheet Set, ...) will show up automatically, with no hardcoded
    parameter list to maintain."""
    names = set()
    for s in sheets:
        try:
            for p in s.Parameters:
                try:
                    nm = p.Definition.Name
                    if nm:
                        names.add(nm)
                except Exception:
                    continue
        except Exception:
            continue
    return sorted(names)


def _sanitize_foldername(name):
    invalid = '\\/:*?"<>|'
    for ch in invalid:
        name = name.replace(ch, "_")
    name = name.strip()
    return name if name else "Unspecified"


def folder_path_from_hierarchy(sheet, hierarchy_items):
    """Builds the relative sub-folder path for one sheet out of the
    enabled, ordered hierarchy levels - e.g. Discipline -> Building ->
    Level gives 'Architecture\\Building A\\Level 01'. A missing/blank
    parameter value on a given sheet falls back to 'Unspecified' rather
    than breaking the whole export."""
    parts = []
    for item in hierarchy_items:
        if not item.enabled:
            continue
        val = _lookup_param_value(sheet, item.name)
        folder_name = str(val) if val not in (None, "") else "Unspecified"
        parts.append(_sanitize_foldername(folder_name))
    return os.path.join(*parts) if parts else ""


class HierarchyLevelItem(object):
    """One level of the export folder hierarchy: a parameter name plus
    whether it's currently enabled. Order within the owning list is the
    folder nesting order (index 0 = outermost)."""

    def __init__(self, name, enabled=True):
        self.name = name
        self.enabled = enabled

    def __str__(self):
        return self.name


class NamingToken(object):
    """One token in the file name sequence - a parameter name, a literal
    separator, or a date/time keyword. `label` is what's shown in the UI
    (e.g. "(space)"), `value` is what's fed into name_from_paralist (e.g.
    " "). For parameter tokens label == value."""

    def __init__(self, label, value):
        self.label = label
        self.value = value

    def __str__(self):
        return self.label


def _drop_target_index(listbox, position):
    """Hit-tests `position` (in `listbox`'s own coordinate space) against
    the ListBox's realized item containers and returns the index to
    insert a dropped item at - end-of-list if the drop lands below the
    last item or on empty space."""
    element = listbox.InputHitTest(position)
    while element is not None and not isinstance(element, ListBoxItem):
        try:
            element = VisualTreeHelper.GetParent(element)
        except Exception:
            element = None
    if element is None:
        return listbox.Items.Count
    index = listbox.ItemContainerGenerator.IndexFromContainer(element)
    if index < 0:
        return listbox.Items.Count
    top_left = element.TranslatePoint(Point(0, 0), listbox)
    if position.Y > top_left.Y + element.ActualHeight / 2.0:
        index += 1
    return index


# ── sheet checkbox list items (ported from the reference tool) ─────────────

class BaseCheckBoxItem(object):
    """Base class for checkbox option wrapping another object."""

    def __init__(self, orig_item):
        self.item = orig_item
        self.state = False

    def __nonzero__(self):
        return self.state

    __bool__ = __nonzero__

    def __str__(self):
        return self.name or str(self.item)

    @property
    def name(self):
        return getattr(self.item, "name", "")

    def unwrap(self):
        return self.item


class SheetOption(BaseCheckBoxItem):
    def __init__(self, sheet_element):
        super(SheetOption, self).__init__(sheet_element)

    @property
    def name(self):
        return "{0} - {1}{2}".format(
            self.item.SheetNumber, self.item.Name,
            " (placeholder)" if self.item.IsPlaceholder else "")

    @property
    def number(self):
        return self.item.SheetNumber


# ── combined sheet-picker + settings window ─────────────────────────────────

class DeePrinterWindow(forms.WPFWindow):
    def __init__(self, xaml_file, context, doc):
        forms.WPFWindow.__init__(self, xaml_file)
        self._context = context
        self.doc = doc
        self.response = None

        self.list_lb.SelectionMode = Controls.SelectionMode.Extended
        self.list_lb.ItemsSource = self._context

        self._avail_drag_start = None
        self._hier_drag_start = None
        self._avail_token_drag_start = None
        self._naming_seq_drag_start = None

        all_sheets = [c.item for c in self._context]
        self._all_param_names = _discover_sheet_parameters(all_sheets)
        self._available_params = list(self._all_param_names)
        self._hierarchy_items = []

        self._available_tokens = self._build_available_tokens()
        self._naming_tokens = []
        self.available_tokens_lb.ItemsSource = list(self._available_tokens)

        self.dicprj = {}
        self.dicdlg = {}
        self._load_settings()

    def _load_settings(self):
        try:
            with open(_prj_dlg_file(self.doc), "r") as f:
                self.dicprj = json.load(f)
        except Exception:
            self.dicprj = {}
        try:
            with open(_DLGVAL_FILE, "r") as f:
                self.dicdlg = json.load(f)
        except Exception:
            self.dicdlg = {}

        raw_paranames = self.dicprj.get("paranames", DEFAULT_PARANAMES)
        value_list = raw_paranames if isinstance(raw_paranames, list) else raw_paranames.split(",")
        self._naming_tokens = [self._token_for_value(v) for v in value_list]
        self._refresh_naming_list()

        self.lb_printfilepath.Content = self.dicdlg.get("printfilepath", "")
        self.chbox_pdfexport.IsChecked = self.dicdlg.get("pdfexport", True)
        self.chbox_dwgexport.IsChecked = self.dicdlg.get("dwgexport", False)
        self.chbox_dxfexport.IsChecked = self.dicdlg.get("dxfexport", False)
        self.chbox_output.IsChecked = self.dicdlg.get("output", True)
        self.chbox_messageboxes.IsChecked = self.dicdlg.get("messageboxes", False)

        saved_hierarchy = self.dicprj.get("folder_hierarchy", [])
        saved_names = set()
        for h in saved_hierarchy:
            nm = h.get("name")
            if nm in self._all_param_names:
                self._hierarchy_items.append(
                    HierarchyLevelItem(nm, h.get("enabled", True)))
                saved_names.add(nm)
        self._available_params = [
            n for n in self._all_param_names if n not in saved_names]
        self._refresh_lists()

    def _collect_settings(self):
        self.dicprj["paranames"] = [t.value for t in self._naming_tokens]
        self.dicprj["folder_hierarchy"] = [
            {"name": h.name, "enabled": bool(h.enabled)}
            for h in self._hierarchy_items]
        self.dicdlg["printfilepath"] = self.lb_printfilepath.Content
        self.dicdlg["pdfexport"] = bool(self.chbox_pdfexport.IsChecked)
        self.dicdlg["dwgexport"] = bool(self.chbox_dwgexport.IsChecked)
        self.dicdlg["dxfexport"] = bool(self.chbox_dxfexport.IsChecked)
        self.dicdlg["output"] = bool(self.chbox_output.IsChecked)
        self.dicdlg["messageboxes"] = bool(self.chbox_messageboxes.IsChecked)

    # -- list filter/check helpers (ported) ----------------------------------
    def _list_options(self, checkbox_filter=None):
        if checkbox_filter:
            filt = checkbox_filter.lower()
            self.list_lb.ItemsSource = [
                c for c in self._context if filt in c.name.lower()]
        else:
            self.list_lb.ItemsSource = self._context

    def _set_states(self, state=True, flip=False, selected=False):
        all_items = self.list_lb.ItemsSource
        current_list = self.list_lb.SelectedItems if selected else self.list_lb.ItemsSource
        for cb in current_list:
            cb.state = (not cb.state) if flip else state
        self.list_lb.ItemsSource = None
        self.list_lb.ItemsSource = all_items

    def toggle_all(self, sender, args):
        self._set_states(flip=True)

    def check_all(self, sender, args):
        self._set_states(state=True)

    def uncheck_all(self, sender, args):
        self._set_states(state=False)

    def check_selected(self, sender, args):
        self._set_states(state=True, selected=True)

    def uncheck_selected(self, sender, args):
        self._set_states(state=False, selected=True)

    def check_highlighted_click(self, sender, args):
        if not list(self.list_lb.SelectedItems):
            forms.alert("Click a row (Shift-click or Ctrl-click for more) to highlight rows first.")
            return
        self._set_states(state=True, selected=True)

    def uncheck_highlighted_click(self, sender, args):
        if not list(self.list_lb.SelectedItems):
            forms.alert("Click a row (Shift-click or Ctrl-click for more) to highlight rows first.")
            return
        self._set_states(state=False, selected=True)

    def search_txt_changed(self, sender, args):
        if self.search_tb.Text == "":
            self.clrsearch_b.Visibility = framework.Windows.Visibility.Collapsed
        else:
            self.clrsearch_b.Visibility = framework.Windows.Visibility.Visible
        self._list_options(checkbox_filter=self.search_tb.Text)

    def clear_search(self, sender, args):
        self.search_tb.Text = ""
        self.search_tb.Focus()

    # -- naming: token palette + ordered sequence (mirrors Folder Structure) --
    def _build_available_tokens(self):
        tokens = []
        for lbl, val in _SEPARATOR_TOKENS:
            tokens.append(NamingToken(lbl, val))
        for lbl, val in _KEYWORD_TOKENS:
            tokens.append(NamingToken(lbl, val))
        for nm in self._all_param_names:
            tokens.append(NamingToken(nm, nm))
        return tokens

    def _token_for_value(self, value):
        for lbl, val in _SEPARATOR_TOKENS + _KEYWORD_TOKENS:
            if val == value:
                return NamingToken(lbl, val)
        return NamingToken(value, value)

    def _refresh_naming_list(self):
        self.naming_seq_lb.ItemsSource = None
        self.naming_seq_lb.ItemsSource = list(self._naming_tokens)

    def add_token_click(self, sender, args):
        token = self.available_tokens_lb.SelectedItem
        if not token:
            return
        self._naming_tokens.append(NamingToken(token.label, token.value))
        self._refresh_naming_list()

    def remove_token_click(self, sender, args):
        item = self.naming_seq_lb.SelectedItem
        if not item:
            return
        self._naming_tokens.remove(item)
        self._refresh_naming_list()

    def move_token_up_click(self, sender, args):
        item = self.naming_seq_lb.SelectedItem
        if not item:
            return
        idx = self._naming_tokens.index(item)
        if idx > 0:
            self._naming_tokens[idx - 1], self._naming_tokens[idx] = \
                self._naming_tokens[idx], self._naming_tokens[idx - 1]
            self._refresh_naming_list()
            self.naming_seq_lb.SelectedItem = item

    def move_token_down_click(self, sender, args):
        item = self.naming_seq_lb.SelectedItem
        if not item:
            return
        idx = self._naming_tokens.index(item)
        if idx < len(self._naming_tokens) - 1:
            self._naming_tokens[idx + 1], self._naming_tokens[idx] = \
                self._naming_tokens[idx], self._naming_tokens[idx + 1]
            self._refresh_naming_list()
            self.naming_seq_lb.SelectedItem = item

    # -- naming: drag-and-drop ------------------------------------------------
    def avail_token_mouse_down(self, sender, args):
        self._avail_token_drag_start = args.GetPosition(None)

    def avail_token_mouse_move(self, sender, args):
        if self._avail_token_drag_start is None or args.LeftButton != MouseButtonState.Pressed:
            return
        pos = args.GetPosition(None)
        if (abs(pos.X - self._avail_token_drag_start.X) < _DRAG_THRESHOLD and
                abs(pos.Y - self._avail_token_drag_start.Y) < _DRAG_THRESHOLD):
            return
        self._avail_token_drag_start = None
        token = self.available_tokens_lb.SelectedItem
        if not token:
            return
        data = DataObject()
        data.SetData("namingToken", token)
        try:
            DragDrop.DoDragDrop(self.available_tokens_lb, data, DragDropEffects.Copy)
        except Exception:
            pass

    def naming_seq_mouse_down(self, sender, args):
        self._naming_seq_drag_start = args.GetPosition(None)

    def naming_seq_mouse_move(self, sender, args):
        if self._naming_seq_drag_start is None or args.LeftButton != MouseButtonState.Pressed:
            return
        pos = args.GetPosition(None)
        if (abs(pos.X - self._naming_seq_drag_start.X) < _DRAG_THRESHOLD and
                abs(pos.Y - self._naming_seq_drag_start.Y) < _DRAG_THRESHOLD):
            return
        self._naming_seq_drag_start = None
        item = self.naming_seq_lb.SelectedItem
        if not item:
            return
        data = DataObject()
        data.SetData("namingSeqIndex", self._naming_tokens.index(item))
        try:
            DragDrop.DoDragDrop(self.naming_seq_lb, data, DragDropEffects.Move)
        except Exception:
            pass

    def naming_seq_drag_over(self, sender, args):
        if args.Data.GetDataPresent("namingToken") or args.Data.GetDataPresent("namingSeqIndex"):
            args.Effects = DragDropEffects.Move
            args.Handled = True

    def naming_seq_drop(self, sender, args):
        position = args.GetPosition(self.naming_seq_lb)
        target_index = _drop_target_index(self.naming_seq_lb, position)

        if args.Data.GetDataPresent("namingToken"):
            token = args.Data.GetData("namingToken")
            if target_index > len(self._naming_tokens):
                target_index = len(self._naming_tokens)
            self._naming_tokens.insert(target_index, NamingToken(token.label, token.value))
            self._refresh_naming_list()
        elif args.Data.GetDataPresent("namingSeqIndex"):
            src_index = args.Data.GetData("namingSeqIndex")
            if not (0 <= src_index < len(self._naming_tokens)):
                return
            item = self._naming_tokens.pop(src_index)
            if target_index > src_index:
                target_index -= 1
            if target_index > len(self._naming_tokens):
                target_index = len(self._naming_tokens)
            elif target_index < 0:
                target_index = 0
            self._naming_tokens.insert(target_index, item)
            self._refresh_naming_list()
        args.Handled = True

    # -- naming preview -------------------------------------------------------
    def preview_click(self, sender, args):
        str2list = [t.value for t in self._naming_tokens]
        checked = [c.item for c in self._context if c.state]
        sheetobj = checked[0] if checked else \
            FilteredElementCollector(self.doc).OfClass(ViewSheet).FirstElement()
        self.lb_txtbox_preview.Text = (
            name_from_paralist(sheetobj, str2list) if sheetobj else "No sheet selected")

    def selectprintfilepath_click(self, sender, args):
        dlg = FolderBrowserDialog()
        dlg.Description = "Select output folder for DeePrinter exports"
        if dlg.ShowDialog() == DialogResult.OK:
            self.lb_printfilepath.Content = dlg.SelectedPath

    # -- folder structure: list refresh + Add/Remove/Move buttons -----------
    def _refresh_lists(self):
        self.available_params_lb.ItemsSource = None
        self.available_params_lb.ItemsSource = list(self._available_params)
        self.hierarchy_lb.ItemsSource = None
        self.hierarchy_lb.ItemsSource = list(self._hierarchy_items)

    def add_level_click(self, sender, args):
        name = self.available_params_lb.SelectedItem
        if not name:
            return
        self._available_params.remove(name)
        self._hierarchy_items.append(HierarchyLevelItem(name, True))
        self._refresh_lists()

    def remove_level_click(self, sender, args):
        item = self.hierarchy_lb.SelectedItem
        if not item:
            return
        self._hierarchy_items.remove(item)
        self._available_params.append(item.name)
        self._available_params.sort()
        self._refresh_lists()

    def move_up_click(self, sender, args):
        item = self.hierarchy_lb.SelectedItem
        if not item:
            return
        idx = self._hierarchy_items.index(item)
        if idx > 0:
            self._hierarchy_items[idx - 1], self._hierarchy_items[idx] = \
                self._hierarchy_items[idx], self._hierarchy_items[idx - 1]
            self._refresh_lists()
            self.hierarchy_lb.SelectedItem = item

    def move_down_click(self, sender, args):
        item = self.hierarchy_lb.SelectedItem
        if not item:
            return
        idx = self._hierarchy_items.index(item)
        if idx < len(self._hierarchy_items) - 1:
            self._hierarchy_items[idx + 1], self._hierarchy_items[idx] = \
                self._hierarchy_items[idx], self._hierarchy_items[idx + 1]
            self._refresh_lists()
            self.hierarchy_lb.SelectedItem = item

    def hierarchy_level_toggled(self, sender, args):
        # IsChecked="{Binding enabled}" is TwoWay by default, so the
        # HierarchyLevelItem is already updated - nothing else to do.
        pass

    # -- folder structure: drag-and-drop -------------------------------------
    def available_mouse_down(self, sender, args):
        self._avail_drag_start = args.GetPosition(None)

    def available_mouse_move(self, sender, args):
        if self._avail_drag_start is None or args.LeftButton != MouseButtonState.Pressed:
            return
        pos = args.GetPosition(None)
        if (abs(pos.X - self._avail_drag_start.X) < _DRAG_THRESHOLD and
                abs(pos.Y - self._avail_drag_start.Y) < _DRAG_THRESHOLD):
            return
        self._avail_drag_start = None
        name = self.available_params_lb.SelectedItem
        if not name:
            return
        data = DataObject()
        data.SetData("paramName", name)
        try:
            DragDrop.DoDragDrop(self.available_params_lb, data, DragDropEffects.Move)
        except Exception:
            pass

    def hierarchy_mouse_down(self, sender, args):
        self._hier_drag_start = args.GetPosition(None)

    def hierarchy_mouse_move(self, sender, args):
        if self._hier_drag_start is None or args.LeftButton != MouseButtonState.Pressed:
            return
        pos = args.GetPosition(None)
        if (abs(pos.X - self._hier_drag_start.X) < _DRAG_THRESHOLD and
                abs(pos.Y - self._hier_drag_start.Y) < _DRAG_THRESHOLD):
            return
        self._hier_drag_start = None
        item = self.hierarchy_lb.SelectedItem
        if not item:
            return
        data = DataObject()
        data.SetData("hierarchyIndex", self._hierarchy_items.index(item))
        try:
            DragDrop.DoDragDrop(self.hierarchy_lb, data, DragDropEffects.Move)
        except Exception:
            pass

    def hierarchy_drag_over(self, sender, args):
        if args.Data.GetDataPresent("paramName") or args.Data.GetDataPresent("hierarchyIndex"):
            args.Effects = DragDropEffects.Move
            args.Handled = True

    def hierarchy_drop(self, sender, args):
        position = args.GetPosition(self.hierarchy_lb)
        target_index = _drop_target_index(self.hierarchy_lb, position)

        if args.Data.GetDataPresent("paramName"):
            name = args.Data.GetData("paramName")
            if name not in self._available_params:
                return
            self._available_params.remove(name)
            if target_index > len(self._hierarchy_items):
                target_index = len(self._hierarchy_items)
            self._hierarchy_items.insert(target_index, HierarchyLevelItem(name, True))
            self._refresh_lists()
        elif args.Data.GetDataPresent("hierarchyIndex"):
            src_index = args.Data.GetData("hierarchyIndex")
            if not (0 <= src_index < len(self._hierarchy_items)):
                return
            item = self._hierarchy_items.pop(src_index)
            if target_index > src_index:
                target_index -= 1
            if target_index > len(self._hierarchy_items):
                target_index = len(self._hierarchy_items)
            elif target_index < 0:
                target_index = 0
            self._hierarchy_items.insert(target_index, item)
            self._refresh_lists()
        args.Handled = True

    def preview_folders_click(self, sender, args):
        checked = [c.item for c in self._context if c.state]
        sheetobj = checked[0] if checked else \
            FilteredElementCollector(self.doc).OfClass(ViewSheet).FirstElement()
        if not sheetobj:
            self.lb_txtbox_folderpreview.Text = "No sheet selected"
            return
        folder_part = folder_path_from_hierarchy(sheetobj, self._hierarchy_items)
        str2list = [t.value for t in self._naming_tokens]
        filename = _sanitize_filename(name_from_paralist(sheetobj, str2list))
        base = self.lb_printfilepath.Content or "<Output Folder>"
        full = (os.path.join(base, folder_part, filename) if folder_part
                else os.path.join(base, filename))
        self.lb_txtbox_folderpreview.Text = full

    # -- persistence ------------------------------------------------------------
    def savesettings_click(self, sender, args):
        self._collect_settings()
        try:
            if not os.path.exists(_PRJ_DLG_DIR):
                os.makedirs(_PRJ_DLG_DIR)
            with open(_prj_dlg_file(self.doc), "w") as f:
                json.dump(self.dicprj, f, indent=4)
            with open(_DLGVAL_FILE, "w") as f:
                json.dump(self.dicdlg, f, indent=4)
        except Exception as e:
            MessageBox.Show("Could not save settings: {0}".format(e))
            return
        if self.chbox_messageboxes.IsChecked:
            MessageBox.Show("Dialog settings saved!", "DeePrinter")

    def button_select(self, sender, args):
        self._collect_settings()
        self.response = [c.item for c in self._context if c.state]
        self.Close()


# ── export helpers ──────────────────────────────────────────────────────────

def _get_dwg_options(doc):
    first_setting = FilteredElementCollector(doc).OfClass(ExportDWGSettings).FirstElement()
    if not first_setting:
        return None
    try:
        active = first_setting.GetActivePredefinedSettings(doc)
    except Exception:
        active = None
    return active.GetDWGExportOptions() if active else first_setting.GetDWGExportOptions()


def _export_dwg(doc, sheet, filename, folder, dwg_options):
    ids = List[ElementId]()
    ids.Add(sheet.Id)
    if not doc.Export(folder, filename, ids, dwg_options):
        raise Exception("Document.Export (DWG) returned False")


def _get_dxf_options(doc):
    """Revit's UI groups DWG and DXF setups together under one dialog
    (Manage > Additional Settings > Export Setups DWG/DXF), so the same
    ExportDWGSettings element is expected to expose GetDXFExportOptions()
    alongside GetDWGExportOptions() - unverified against a live project,
    first run will confirm. Falls back to a bare DXFExportOptions() if
    that method isn't there."""
    first_setting = FilteredElementCollector(doc).OfClass(ExportDWGSettings).FirstElement()
    if first_setting is not None:
        try:
            active = first_setting.GetActivePredefinedSettings(doc)
        except Exception:
            active = None
        source = active if active else first_setting
        try:
            return source.GetDXFExportOptions()
        except Exception:
            pass
    return DXFExportOptions()


def _export_dxf(doc, sheet, filename, folder, dxf_options):
    ids = List[ElementId]()
    ids.Add(sheet.Id)
    if not doc.Export(folder, filename, ids, dxf_options):
        raise Exception("Document.Export (DXF) returned False")


def _export_pdf(doc, sheet, filename, folder):
    options = PDFExportOptions()
    options.FileName = filename
    options.Combine = True
    ids = List[ElementId]()
    ids.Add(sheet.Id)
    if not doc.Export(folder, ids, options):
        raise Exception("Document.Export (PDF) returned False")


def main():
    doc = __revit__.ActiveUIDocument.Document

    sheets = [s for s in FilteredElementCollector(doc).OfClass(ViewSheet) if not s.IsTemplate]
    if not sheets:
        forms.alert("No sheets found in this document.")
        return
    sortlist = sorted([SheetOption(s) for s in sheets], key=lambda x: x.number)

    window = DeePrinterWindow(_XAML_FILE, sortlist, doc)
    window.ShowDialog()

    if not window.response:
        return
    sheets_to_export = window.response
    dicprj = window.dicprj
    dicdlg = window.dicdlg
    hierarchy_items = window._hierarchy_items

    do_pdf = bool(dicdlg.get("pdfexport"))
    do_dwg = bool(dicdlg.get("dwgexport"))
    do_dxf = bool(dicdlg.get("dxfexport"))
    show_output = bool(dicdlg.get("output"))
    show_messageboxes = bool(dicdlg.get("messageboxes"))
    base_folder = dicdlg.get("printfilepath")
    raw_paranames = dicprj.get("paranames", DEFAULT_PARANAMES)
    paranames = raw_paranames if isinstance(raw_paranames, list) else raw_paranames.split(",")

    if not do_pdf and not do_dwg and not do_dxf:
        forms.alert("Nothing to export - check Export PDF, DWG, and/or DXF.")
        return
    if not sheets_to_export:
        forms.alert("No sheets were checked.")
        return
    if not base_folder or not os.path.isdir(base_folder):
        forms.alert("Pick a valid output folder first.")
        return

    dwg_options = None
    if do_dwg:
        dwg_options = _get_dwg_options(doc)
        if dwg_options is None:
            forms.alert(
                "No DWG Export Setting found in this project - DWG "
                "export will be skipped for all sheets.")
            do_dwg = False

    # _get_dxf_options always returns a usable DXFExportOptions - falling
    # back to bare defaults if this project has no explicit DXF export
    # setup, rather than blocking the export entirely.
    dxf_options = _get_dxf_options(doc) if do_dxf else None

    all_results = []
    total = len(sheets_to_export)

    with forms.ProgressBar(title="DeePrinter — exporting...", cancellable=True) as pb:
        for i, sheet in enumerate(sheets_to_export):
            if pb.cancelled:
                all_results.append(("(cancelled)", [("Cancelled", None)]))
                break
            label = "{0} - {1}".format(sheet.SheetNumber, sheet.Name)
            pb.update_progress(i, total)
            pb.title = "Exporting {0}/{1}: {2}".format(i + 1, total, label)
            if show_output:
                output.print_md("**Exporting {0}/{1}: {2}**".format(i + 1, total, label))

            steps = []
            all_results.append((label, steps))
            filename = _sanitize_filename(name_from_paralist(sheet, paranames))

            folder_part = folder_path_from_hierarchy(sheet, hierarchy_items)
            sheet_folder = os.path.join(base_folder, folder_part) if folder_part else base_folder
            try:
                if not os.path.exists(sheet_folder):
                    os.makedirs(sheet_folder)
            except Exception as e:
                steps.append(("FAILED to create folder '{0}': {1}".format(sheet_folder, e), False))
                continue

            if do_pdf:
                try:
                    _export_pdf(doc, sheet, filename, sheet_folder)
                    steps.append(("PDF exported as '{0}.pdf'".format(filename), True))
                except Exception as e:
                    steps.append(("PDF FAILED: {0}".format(e), False))

            if do_dwg:
                try:
                    _export_dwg(doc, sheet, filename, sheet_folder, dwg_options)
                    steps.append(("DWG exported as '{0}.dwg'".format(filename), True))
                except Exception as e:
                    steps.append(("DWG FAILED: {0}".format(e), False))

            if do_dxf:
                try:
                    _export_dxf(doc, sheet, filename, sheet_folder, dxf_options)
                    steps.append(("DXF exported as '{0}.dxf'".format(filename), True))
                except Exception as e:
                    steps.append(("DXF FAILED: {0}".format(e), False))

    if show_messageboxes:
        ok_count = sum(1 for _label, steps in all_results for _d, ok in steps if ok)
        MessageBox.Show("{0} file(s) exported.".format(ok_count), "DeePrinter")

    if show_output:
        html = '<h2 style="font-family:sans-serif;color:#ddd;">DeePrinter Results</h2>'
        for label, steps in all_results:
            oks = [s[1] for s in steps]
            if any(o is False for o in oks):
                file_bg = "#b71c1c"
            elif any(o is None for o in oks):
                file_bg = "#37474f"
            else:
                file_bg = "#1b5e20"

            html += ('<div style="margin:10px 0 2px 0;padding:6px 12px;background:{0};'
                     'color:#fff;border-radius:4px;font-family:sans-serif;'
                     'font-weight:bold;">{1}</div>').format(file_bg, label)
            for detail, ok in steps:
                bg = "#2e7d32" if ok else ("#455a64" if ok is None else "#c62828")
                icon = "&#10003;" if ok else ("&#9888;" if ok is None else "&#10007;")
                html += (
                    '<div style="padding:5px 14px 5px 24px;margin:2px 0;background:{0};'
                    'color:#fff;border-radius:3px;font-family:monospace;font-size:12px;">'
                    '{1}&nbsp; {2}'
                    '</div>'.format(bg, icon, detail)
                )
        output.print_html(html)


main()
