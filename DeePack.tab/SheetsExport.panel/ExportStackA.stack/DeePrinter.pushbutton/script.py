# -*- coding: utf-8 -*-
"""
DeePrinter
Batch-export selected sheets to PDF and/or DWG, in one combined window.

Layout: the sheet checklist lives in its own LEFT column that always
keeps its full height, with a live "N of M checked" count above it;
every setting (Sheet Set, File Naming, Folder Structure, format
checkboxes, PDF Engine) lives in a scrollable column on the right, so
adding a new setting can never again crowd the sheet list into a
sliver (live report: "i cant see the Sheets" - a GroupBox added to the
old single, non-scrolling column had done exactly that).

  - Filterable checkbox list of sheets (Check/Uncheck/Toggle All)
  - Sheet Set: a CHECKLIST (not a single-select dropdown) of the
    document's own saved View/Sheet Sets (File > Print > Select Views/
    Sheets to Print > Save As...) - tick one or several, then "Check
    These" adds their sheets to whatever is already checked, "Only
    These" replaces the whole selection with them, "Clear" unticks the
    sets themselves without touching the sheet checklist. Reads
    Revit's own ViewSheetSet elements; DeePrinter never creates or
    edits one itself.
  - File Naming: drag-and-drop (or Add/Remove/Move Up/Down buttons) to
    build the file name out of an ordered token sequence - parameter
    names, literal separators, and date/time keywords - same naming
    scheme as the reference pyTiBa Export PDF/DWG tools under the hood
  - Folder Structure: drag-and-drop (or Add/Remove/Move Up/Down buttons)
    to build an ordered folder hierarchy out of any sheet parameter
    (Discipline, Building, Level, custom shared/project parameters, etc).
    Applies identically to PDF, DWG, and DXF - all three land in the same
    computed hierarchy folder for a given sheet, unless "Split by file
    type" is checked, which puts each format in its own top-level folder
    (PDF/, DWG/, DXF/) with the hierarchy repeated inside each.
  - File Naming's preview box is LIVE - it recomputes automatically
    against the first checked sheet on every token add/remove/reorder
    and every prefix/suffix edit, with no button press needed (the
    "Refresh" button still exists, only for re-checking after a
    different sheet gets checked).
  - Save Settings persists everything to JSON (global dlgval.json +
    per-project naming tokens + folder hierarchy), same persistence
    approach as the reference tools

PDF export uses Revit's native PDFExportOptions API by default (driver-
free - no PDFCreator/Adobe PDF virtual printer needed, unlike the
reference), with an optional Combined PDF mode - all checked sheets in
one multi-page file named <Project>_<date>_<time>.pdf. Export PDF
(separated, one file per sheet) and Combined PDF are independent; with
BOTH ticked the outputs land in "PDF > Separated" and "PDF > Combined"
under the output folder. A per-project Prefix/Suffix pair wraps every
exported file name.

PDF Engine lets this default be swapped for whichever printer/driver is
already INSTALLED on this PC (Adobe PDF, Microsoft Print to PDF, a real
plotter, PDFCreator, ...), driven through PrintManager.SubmitPrint()
(_print_pdf_via_system_printer) - for print setups/paper handling only
the classic Print dialog route reproduces exactly, or to send straight
to a physical plotter. DeePrinter never bundles or installs a PDF
printer of its own - this only reaches drivers already on the machine.
Off by default: a virtual PDF driver not configured for silent output
pops up its own Save dialog per sheet, which this tool cannot see or
answer, so the dialog itself carries this warning in plain text.
DWG
export uses the project's own DWG Export Setting, same as Revit's own
Export > DWG (same approach as the reference) - except views on sheets
are ALWAYS merged into the sheet file (MergedViews=True), never exported
as separate per-view DWGs xref'd into it.
"""
import os
import json
import datetime
from pyrevit import forms, script, framework
import dee_branding
from pyrevit.framework import Controls
from Autodesk.Revit.DB import (
    FilteredElementCollector, ViewSheet, ExportDWGSettings, PDFExportOptions,
    ElementId, StorageType, DXFExportOptions, ViewSheetSet, ViewSet, PrintRange
)
from System.Collections.Generic import List

import clr
clr.AddReference("System.Windows.Forms")
clr.AddReference("System.Drawing")
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
clr.AddReference("WindowsBase")
from System.Windows.Forms import FolderBrowserDialog, DialogResult, MessageBox
from System.Drawing.Printing import PrinterSettings
from System.Windows import DataObject, DragDropEffects, DragDrop, Point, Visibility
from System.Windows.Input import MouseButtonState
from System.Windows.Media import VisualTreeHelper
from System.Windows.Controls import ListBoxItem
import dee_telemetry
dee_telemetry.check_access("DeePrinter")


output = script.get_output()

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")
_DLGVAL_FILE = os.path.join(_THIS_DIR, "dlgval.json")
_PRJ_DLG_DIR = os.path.join(_THIS_DIR, "Project_Dlg_Data")

DEFAULT_PARANAMES = "Sheet Number,-,Sheet Name,_,date,_,time"
_DRAG_THRESHOLD = 4.0

# PDF Engine: which mechanism actually produces the PDF. Native (the
# long-standing default - see _export_pdf's docstring for why it
# replaced a virtual-printer approach) vs whatever printer/driver is
# already INSTALLED on this PC, driven through PrintManager - DeePrinter
# never bundles or installs a PDF printer of its own; "advanced" in the
# old label read as if it had, so the wording was dropped (live
# feedback: "did you generate a PDF printer engine inside the tool?").
_ENGINE_NATIVE = u"Native (Revit's own PDF export) - recommended"
_ENGINE_PRINTER = u"An installed printer on this PC (Windows Print)"

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


def _eid(element_id):
    """ElementId.Value (Revit 2024+, 64-bit) with pre-2024 IntegerValue
    as the fallback - same convention used across this extension's other
    tools for comparing ElementIds as plain ints rather than relying on
    IronPython hashing .NET ElementId objects consistently."""
    try:
        return int(element_id.Value)
    except Exception:
        pass
    try:
        return int(element_id.IntegerValue)
    except Exception:
        return -1


class SheetSetOption(object):
    """Wraps a Revit ViewSheetSet - one of the project's own saved sheet
    sets (File > Print > Select Views/Sheets to Print > Save As...)."""
    def __init__(self, vss):
        self.vss = vss
        try:
            self.name = vss.Name or u"(unnamed set)"
        except Exception:
            self.name = u"(unnamed set)"

    def sheet_ids(self):
        """Every member VIEWSHEET's id, as plain ints (_eid) - a saved
        set can also contain plain Views (schedules, 3D views, ...),
        which DeePrinter's sheet list has no row for anyway."""
        ids = set()
        try:
            for v in self.vss.Views:
                if isinstance(v, ViewSheet):
                    ids.add(_eid(v.Id))
        except Exception:
            pass
        return ids


class SheetSetRow(object):
    """One checkable row in the Sheet Set checklist - plain check/uncheck
    per saved set (not a single-select dropdown), so several sets can be
    combined in one Check These / Only These action. `name` is the
    property the checklist's DataTemplate binds to directly (never rely
    on __str__/ToString() for a WPF display - see the git history of
    this exact control for why: it showed raw IronPython type names
    before this fix)."""
    def __init__(self, opt):
        self.opt = opt
        self.name = opt.name
        self.state = False


# ── combined sheet-picker + settings window ─────────────────────────────────

class DeePrinterWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, context, doc):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self._context = context
        self.doc = doc
        self.response = None

        self.list_lb.SelectionMode = Controls.SelectionMode.Extended
        self.list_lb.ItemsSource = self._context
        self._update_sheet_count()

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
        self._load_pdf_engine_ui()
        self._load_settings()
        self._load_sheet_sets()

    def _load_pdf_engine_ui(self):
        self.pdfengine_cb.Items.Clear()
        self.pdfengine_cb.Items.Add(_ENGINE_NATIVE)
        self.pdfengine_cb.Items.Add(_ENGINE_PRINTER)
        self._refresh_printer_list()

    def _refresh_printer_list(self):
        self.printer_cb.ItemsSource = None
        self.printer_cb.ItemsSource = list_installed_printers()

    def _sync_printer_row_visibility(self):
        show = (self.pdfengine_cb.SelectedItem == _ENGINE_PRINTER)
        self.printer_row.Visibility = Visibility.Visible if show else Visibility.Collapsed

    def pdfengine_changed(self, sender, args):
        self._sync_printer_row_visibility()

    def refresh_printers_click(self, sender, args):
        current = self.printer_cb.SelectedItem
        self._refresh_printer_list()
        printers = list(self.printer_cb.ItemsSource or [])
        if current in printers:
            self.printer_cb.SelectedItem = current

    def _load_sheet_sets(self):
        """Populates the Sheet Set checklist from every ViewSheetSet
        already saved in this document (File > Print > Select Views/
        Sheets to Print > Save As...) - Revit's own named sheet sets,
        not anything DeePrinter invents or stores itself. A checklist,
        not a single-select dropdown, so several sets can be combined in
        one action (e.g. tick both 'SCH 100%' and 'BIM Coordination
        Views' and Check These once)."""
        try:
            sets = list(FilteredElementCollector(self.doc).OfClass(ViewSheetSet))
        except Exception:
            sets = []
        options = sorted(
            (SheetSetOption(vss) for vss in sets), key=lambda s: s.name.lower())
        self._sheet_set_rows = [SheetSetRow(o) for o in options]
        self.sheetset_list_ic.ItemsSource = self._sheet_set_rows
        has_sets = bool(self._sheet_set_rows)
        self.checkset_b.IsEnabled = has_sets
        self.onlyset_b.IsEnabled = has_sets
        self.clearset_b.IsEnabled = has_sets
        self.sheetset_status_tb.Text = (
            u"" if has_sets else
            u"This document has no saved sheet sets yet - create one via "
            u"File > Print > Select Views/Sheets to Print > Save As...")

    def _apply_sheet_set(self, additive):
        ticked = [r.opt for r in self._sheet_set_rows if r.state]
        if not ticked:
            self.sheetset_status_tb.Text = u"Tick at least one sheet set first."
            return
        member_ids = set()
        for opt in ticked:
            member_ids |= opt.sheet_ids()
        if not member_ids:
            self.sheetset_status_tb.Text = (
                u"The ticked set(s) have no sheets in them (only "
                u"views/schedules, or empty).")
            return
        matched = 0
        for c in self._context:
            in_set = _eid(c.item.Id) in member_ids
            if in_set:
                matched += 1
            if additive:
                if in_set:
                    c.state = True
            else:
                c.state = in_set
        # Membership is checked against EVERY sheet regardless of the
        # search box, but the visible rows must still respect whatever
        # filter is currently typed - same refresh path search_txt_changed
        # itself uses.
        self._list_options(checkbox_filter=self.search_tb.Text)
        self._update_sheet_count()
        self._refresh_naming_preview()
        verb = u"Added" if additive else u"Checked only"
        names = u", ".join(o.name for o in ticked)
        self.sheetset_status_tb.Text = u"{0} {1} sheet(s) from: {2}".format(
            verb, matched, names)

    def check_set_click(self, sender, args):
        self._apply_sheet_set(additive=True)

    def only_set_click(self, sender, args):
        self._apply_sheet_set(additive=False)

    def clear_set_click(self, sender, args):
        """Unticks every set in the checklist - never touches which
        sheets are checked in the main list, only resets the sets
        themselves so a stale selection can't be mistaken for active."""
        for r in self._sheet_set_rows:
            r.state = False
        self.sheetset_list_ic.ItemsSource = None
        self.sheetset_list_ic.ItemsSource = self._sheet_set_rows
        self.sheetset_status_tb.Text = u""

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
        self.tb_prefix.Text = self.dicprj.get("prefix", "")
        self.tb_suffix.Text = self.dicprj.get("suffix", "")

        self.lb_printfilepath.Content = self.dicdlg.get("printfilepath", "")
        self.chbox_pdfexport.IsChecked = self.dicdlg.get("pdfexport", True)
        self.chbox_combinedpdf.IsChecked = self.dicdlg.get("combinedpdf", False)
        self.chbox_dwgexport.IsChecked = self.dicdlg.get("dwgexport", False)
        self.chbox_dxfexport.IsChecked = self.dicdlg.get("dxfexport", False)
        self.chbox_splitbyext.IsChecked = self.dicdlg.get("splitbyext", False)
        self.chbox_output.IsChecked = self.dicdlg.get("output", True)
        self.chbox_messageboxes.IsChecked = self.dicdlg.get("messageboxes", False)

        saved_engine = self.dicdlg.get("pdfengine", _ENGINE_NATIVE)
        self.pdfengine_cb.SelectedItem = (
            saved_engine if saved_engine in (_ENGINE_NATIVE, _ENGINE_PRINTER)
            else _ENGINE_NATIVE)
        saved_printer = self.dicdlg.get("printername", "")
        printers = list(self.printer_cb.ItemsSource or [])
        if saved_printer and saved_printer in printers:
            self.printer_cb.SelectedItem = saved_printer
        self._sync_printer_row_visibility()

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
        self.dicprj["prefix"] = self.tb_prefix.Text or ""
        self.dicprj["suffix"] = self.tb_suffix.Text or ""
        self.dicprj["folder_hierarchy"] = [
            {"name": h.name, "enabled": bool(h.enabled)}
            for h in self._hierarchy_items]
        self.dicdlg["printfilepath"] = self.lb_printfilepath.Content
        self.dicdlg["pdfexport"] = bool(self.chbox_pdfexport.IsChecked)
        self.dicdlg["combinedpdf"] = bool(self.chbox_combinedpdf.IsChecked)
        self.dicdlg["dwgexport"] = bool(self.chbox_dwgexport.IsChecked)
        self.dicdlg["dxfexport"] = bool(self.chbox_dxfexport.IsChecked)
        self.dicdlg["splitbyext"] = bool(self.chbox_splitbyext.IsChecked)
        self.dicdlg["output"] = bool(self.chbox_output.IsChecked)
        self.dicdlg["messageboxes"] = bool(self.chbox_messageboxes.IsChecked)
        self.dicdlg["pdfengine"] = self.pdfengine_cb.SelectedItem or _ENGINE_NATIVE
        self.dicdlg["printername"] = self.printer_cb.SelectedItem or ""

    # -- list filter/check helpers (ported) ----------------------------------
    def _update_sheet_count(self):
        """Keeps a one-line 'N of M checked' summary visible above the
        sheet list at all times - added because narrowing the list into
        a side column (to make room for the settings panel) means a
        long list scrolls out of view, and this is the at-a-glance
        confirmation that the right sheets are actually checked without
        having to scroll to find out."""
        total = len(self._context)
        checked = sum(1 for c in self._context if c.state)
        shown = len(list(self.list_lb.ItemsSource or []))
        if shown == total:
            self.sheetcount_tb.Text = u"{0} of {1} sheets checked".format(checked, total)
        else:
            self.sheetcount_tb.Text = u"{0} of {1} sheets checked ({2} shown by filter)".format(
                checked, total, shown)

    def _list_options(self, checkbox_filter=None):
        if checkbox_filter:
            filt = checkbox_filter.lower()
            self.list_lb.ItemsSource = [
                c for c in self._context if filt in c.name.lower()]
        else:
            self.list_lb.ItemsSource = self._context
        self._update_sheet_count()

    def _set_states(self, state=True, flip=False, selected=False):
        all_items = self.list_lb.ItemsSource
        current_list = self.list_lb.SelectedItems if selected else self.list_lb.ItemsSource
        for cb in current_list:
            cb.state = (not cb.state) if flip else state
        self.list_lb.ItemsSource = None
        self.list_lb.ItemsSource = all_items
        self._update_sheet_count()
        self._refresh_naming_preview()

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
        self._refresh_naming_preview()

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
    def _preview_name(self, sheetobj):
        str2list = [t.value for t in self._naming_tokens]
        return u"{0}{1}{2}".format(self.tb_prefix.Text or "",
                                   name_from_paralist(sheetobj, str2list),
                                   self.tb_suffix.Text or "")

    def _refresh_naming_preview(self):
        """Recomputes the live preview against the first CHECKED sheet
        (list order, i.e. lowest sheet number), falling back to any
        sheet in the document if none is checked yet. Called from every
        naming mutation (token add/remove/reorder/drop, prefix/suffix
        edits) and every sheet-check change, so the box always reflects
        the exact file name the next export would use - no separate
        button press required (live feedback: naming used to need a
        manual 'Preview' click to ever update)."""
        checked = [c.item for c in self._context if c.state]
        sheetobj = checked[0] if checked else \
            FilteredElementCollector(self.doc).OfClass(ViewSheet).FirstElement()
        self.lb_txtbox_preview.Text = (
            self._preview_name(sheetobj) if sheetobj else "No sheet selected")

    def preview_click(self, sender, args):
        self._refresh_naming_preview()

    def naming_text_changed(self, sender, args):
        self._refresh_naming_preview()

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
        filename = _sanitize_filename(self._preview_name(sheetobj))
        base = self.lb_printfilepath.Content or "<Output Folder>"
        if self.chbox_pdfexport.IsChecked and self.chbox_combinedpdf.IsChecked:
            # both PDF modes -> the per-sheet files live in PDF\Separated
            base = os.path.join(base, "PDF", "Separated")
        elif self.chbox_splitbyext.IsChecked:
            # preview with the first enabled format's folder
            fmt = ("PDF" if self.chbox_pdfexport.IsChecked else
                   "DWG" if self.chbox_dwgexport.IsChecked else
                   "DXF" if self.chbox_dxfexport.IsChecked else "PDF")
            base = os.path.join(base, fmt)
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

def _force_merged_views(options):
    """MergedViews=True is the API side of UNchecking "Export views on
    sheets and links as external references" in Revit's DWG/DXF export
    setup. With it False, every viewport on a sheet exports as its own
    DWG that the sheet file merely xrefs - a folder full of fragment
    files instead of one drawing. DeePrinter always wants one flat,
    self-contained file per sheet regardless of what the project's
    export setup says, so this is forced, not optional."""
    try:
        options.MergedViews = True
    except Exception:
        pass
    return options


def _get_dwg_options(doc):
    first_setting = FilteredElementCollector(doc).OfClass(ExportDWGSettings).FirstElement()
    if not first_setting:
        return None
    try:
        active = first_setting.GetActivePredefinedSettings(doc)
    except Exception:
        active = None
    options = active.GetDWGExportOptions() if active else first_setting.GetDWGExportOptions()
    return _force_merged_views(options)


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
            return _force_merged_views(source.GetDXFExportOptions())
        except Exception:
            pass
    return _force_merged_views(DXFExportOptions())


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


def _export_pdf_combined(doc, sheets, filename, folder):
    """One multi-page PDF of all given sheets. Page order follows the
    order of `sheets` (the checked list is already sorted by sheet
    number). The per-sheet naming tokens are parameter-driven and don't
    apply to a multi-sheet file, so the combined file gets its own
    <Project>_<date>_<time> name built by the caller."""
    options = PDFExportOptions()
    options.FileName = filename
    options.Combine = True
    ids = List[ElementId]()
    for sheet in sheets:
        ids.Add(sheet.Id)
    if not doc.Export(folder, ids, options):
        raise Exception("Document.Export (combined PDF) returned False")


def list_installed_printers():
    """Every printer Windows currently knows about - real hardware,
    network printers, and virtual "print to file" drivers alike. Plain
    .NET enumeration, no admin rights needed."""
    try:
        return [str(name) for name in PrinterSettings.InstalledPrinters]
    except Exception:
        return []


def _print_pdf_via_system_printer(doc, sheets, filename, folder, printer_name):
    """One PDF - a single sheet, or several combined into one file -
    produced through a real Windows printer DRIVER via
    PrintManager.SubmitPrint(), instead of the native Export API
    _export_pdf/_export_pdf_combined use. Exists for print setups or
    paper handling that only the classic Print dialog route reproduces
    exactly, or to send straight to a physical plotter.

    Always drives CombinedFile=True with an explicit ViewSet, even for
    ONE sheet - that is what keeps DeePrinter's own file name (tokens,
    prefix/suffix) in full control of the output path; CombinedFile=
    False makes Revit invent its own per-view file names instead,
    which would silently break the naming feature for this engine.

    IMPORTANT (also shown in the dialog itself): many VIRTUAL PDF
    printer drivers (Adobe PDF, Microsoft Print to PDF) pop up their
    own Save-As dialog for every print job, which the Revit API cannot
    see or answer - a batch run through such a driver hangs on the
    very first sheet, waiting on a dialog nothing is watching. Only a
    driver already configured for silent, path-specified output (most
    real plotters are inherently silent; PDFCreator/Bluebeam can be set
    up with an Auto-Save profile) is safe here. This exact failure mode
    is why DeePrinter's DEFAULT engine bypasses printer drivers
    entirely - see _export_pdf's own docstring.

    --------------------------------------------------------------
    NEEDS LIVE-REVIT VERIFICATION (per this codebase's convention)
    --------------------------------------------------------------
    SelectNewPrintDriver's exact reset behaviour on properties set
    before it is called (they are set AFTER here, which the API docs
    say is required); and whether PrintManager.Apply()/SubmitPrint()
    genuinely need no open Transaction (documented as not modifying
    the model, but never exercised live in this codebase). SubmitPrint
    itself returns nothing, so the file-existence check below is the
    only confirmation available from the API side - a misconfigured
    driver can "succeed" here with no file ever written."""
    pm = doc.PrintManager
    pm.SelectNewPrintDriver(printer_name)
    pm.PrintRange = PrintRange.Select
    viewset = ViewSet()
    for s in sheets:
        viewset.Insert(s)
    pm.ViewSheetSetting.CurrentViewSheetSet.Views = viewset
    pm.CombinedFile = True
    pm.PrintToFile = True
    full_path = os.path.join(folder, filename + ".pdf")
    pm.PrintToFileName = full_path
    pm.Apply()
    pm.SubmitPrint()
    if not os.path.isfile(full_path):
        raise Exception(
            "No file appeared after printing - '{0}' may be waiting on its "
            "own Save dialog (turn off 'prompt for filename' in the "
            "printer's own properties, or pick a silent driver)".format(
                printer_name))


class _SafeProgress(object):
    """forms.ProgressBar tries to set Window.TaskbarItemInfo on its host
    window - a genuine WPF-level bug (not this extension's code):
    Window.TaskbarItemInfo throws NotImplementedException whenever the
    underlying ITaskbarList::HrInit COM call fails, which is documented
    to happen specifically under Remote Desktop/Terminal Services or a
    custom shell without a taskbar (live-confirmed in DeeSheetLinks).

    Wraps the real forms.ProgressBar and falls back to running with NO
    progress UI at all if entering it fails, so the tool degrades
    gracefully under RDP instead of crashing - everyone else still gets
    the real progress bar exactly as before. `pb.update_progress(...)`/
    `pb.cancelled` are safe no-ops in the fallback case, so callers never
    need an extra branch."""
    def __init__(self, **kwargs):
        self._kwargs = kwargs
        self._real = None

    def __enter__(self):
        try:
            self._real = forms.ProgressBar(**self._kwargs)
            return self._real.__enter__()
        except Exception:
            self._real = None
            return self

    def __exit__(self, exc_type, exc_value, tb):
        if self._real is not None:
            return self._real.__exit__(exc_type, exc_value, tb)
        return False

    @property
    def cancelled(self):
        return False

    def update_progress(self, i, total):
        pass


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
    combined_pdf = bool(dicdlg.get("combinedpdf"))
    do_dwg = bool(dicdlg.get("dwgexport"))
    do_dxf = bool(dicdlg.get("dxfexport"))
    split_by_ext = bool(dicdlg.get("splitbyext"))
    pdf_engine = dicdlg.get("pdfengine", _ENGINE_NATIVE)
    printer_name = dicdlg.get("printername", "")
    show_output = bool(dicdlg.get("output"))
    show_messageboxes = bool(dicdlg.get("messageboxes"))
    base_folder = dicdlg.get("printfilepath")
    raw_paranames = dicprj.get("paranames", DEFAULT_PARANAMES)
    paranames = raw_paranames if isinstance(raw_paranames, list) else raw_paranames.split(",")
    name_prefix = dicprj.get("prefix", "") or ""
    name_suffix = dicprj.get("suffix", "") or ""

    def _final_name(core_name):
        return _sanitize_filename(u"{0}{1}{2}".format(name_prefix, core_name, name_suffix))

    if not do_pdf and not combined_pdf and not do_dwg and not do_dxf:
        forms.alert("Nothing to export - check Export PDF, Combined PDF, DWG, and/or DXF.")
        return
    if not sheets_to_export:
        forms.alert("No sheets were checked.")
        return
    if not base_folder or not os.path.isdir(base_folder):
        forms.alert("Pick a valid output folder first.")
        return
    if (do_pdf or combined_pdf) and pdf_engine == _ENGINE_PRINTER and not printer_name:
        forms.alert("Pick a printer in the PDF Engine section first, or "
                    "switch back to the Native engine.")
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

    # Export PDF (one file per sheet) and Combined PDF (one multi-page
    # file) are independent. With BOTH ticked, the two outputs get their
    # own subfolders under one PDF folder - PDF\Separated\... and
    # PDF\Combined\ - so they never mix.
    per_sheet_pdf = do_pdf
    both_pdf = do_pdf and combined_pdf
    pdf_sep_root = os.path.join(base_folder, "PDF", "Separated") if both_pdf else None

    with _SafeProgress(title="DeePrinter — exporting...", cancellable=True) as pb:
        if combined_pdf:
            steps = []
            all_results.append(("Combined PDF ({0} sheets)".format(total), steps))
            pb.title = "Exporting combined PDF ({0} sheets)...".format(total)
            if show_output:
                output.print_md("**Exporting combined PDF ({0} sheets)...**".format(total))
            if both_pdf:
                combined_folder = os.path.join(base_folder, "PDF", "Combined")
            else:
                combined_folder = os.path.join(base_folder, "PDF") if split_by_ext else base_folder
            combined_name = _final_name("{0}_{1}".format(
                doc.Title, datetime.datetime.now().strftime("%d-%m-%y_%H.%M")))
            try:
                if not os.path.exists(combined_folder):
                    os.makedirs(combined_folder)
                if pdf_engine == _ENGINE_PRINTER:
                    _print_pdf_via_system_printer(
                        doc, sheets_to_export, combined_name, combined_folder, printer_name)
                else:
                    _export_pdf_combined(doc, sheets_to_export, combined_name, combined_folder)
                steps.append(("Combined PDF exported as '{0}.pdf'".format(combined_name), True))
            except Exception as e:
                steps.append(("Combined PDF FAILED: {0}".format(e), False))

        if not (per_sheet_pdf or do_dwg or do_dxf):
            sheets_to_export = []

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
            filename = _final_name(name_from_paralist(sheet, paranames))

            folder_part = folder_path_from_hierarchy(sheet, hierarchy_items)

            def _target_folder(fmt, _steps=steps, _part=folder_part, _root=None):
                """Resolves (and creates) the destination folder for one
                format of this sheet. With Split by file type on, the format
                folder (PDF/DWG/DXF) sits directly under the output folder
                and the parameter hierarchy is repeated inside it; otherwise
                all formats share the same hierarchy folder. `_root`
                overrides the base entirely (used for PDF\\Separated when
                both PDF modes run together). Returns None (with the
                failure recorded) if the folder can't be created."""
                if _root is not None:
                    parts = [_root]
                else:
                    parts = [base_folder]
                    if split_by_ext:
                        parts.append(fmt)
                if _part:
                    parts.append(_part)
                folder = os.path.join(*parts)
                try:
                    if not os.path.exists(folder):
                        os.makedirs(folder)
                    return folder
                except Exception as e:
                    _steps.append(("FAILED to create folder '{0}': {1}".format(folder, e), False))
                    return None

            if per_sheet_pdf:
                pdf_folder = _target_folder("PDF", _root=pdf_sep_root)
                if pdf_folder:
                    try:
                        if pdf_engine == _ENGINE_PRINTER:
                            _print_pdf_via_system_printer(
                                doc, [sheet], filename, pdf_folder, printer_name)
                        else:
                            _export_pdf(doc, sheet, filename, pdf_folder)
                        steps.append(("PDF exported as '{0}.pdf'".format(filename), True))
                    except Exception as e:
                        steps.append(("PDF FAILED: {0}".format(e), False))

            if do_dwg:
                dwg_folder = _target_folder("DWG")
                if dwg_folder:
                    try:
                        _export_dwg(doc, sheet, filename, dwg_folder, dwg_options)
                        steps.append(("DWG exported as '{0}.dwg'".format(filename), True))
                    except Exception as e:
                        steps.append(("DWG FAILED: {0}".format(e), False))

            if do_dxf:
                dxf_folder = _target_folder("DXF")
                if dxf_folder:
                    try:
                        _export_dxf(doc, sheet, filename, dxf_folder, dxf_options)
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
