# -*- coding: utf-8 -*-
"""
DeeSheet
Two tabs:

1. Normal Creation - paste the Sheet Number column and the Sheet Name
   column from Excel (one column per box) and batch-create the
   corresponding sheets, using the Title Block picked from the dropdown.
   Line 1 in both boxes pairs up as one sheet, line 2 as the next, etc.
   Sheet numbers that already exist in the project are skipped rather
   than overwritten. Export Last Results to Excel writes a themed
   .xlsx (via lib/xlsx_writer.py) styled to match NAGA's LOD Drawings
   Index register.

2. Super Sheet - scans every Sheet in the project into one editable
   grid. Edit Number/Name directly in a cell (the grid is sortable, so
   a new sheet numbered between two existing ones will naturally land
   between them once sorted by Number); Move Up/Move Down let you
   manually reorder the highlighted row in this view (independent of
   sort order - purely a workspace-organization aid, doesn't touch
   Revit's own sheet numbering/browser order); Add Row adds a new,
   not-yet-created sheet row; the Delete checkbox marks an existing
   sheet for deletion, or just drops a not-yet-created row since it
   was never made. Apply Changes creates/renames/deletes everything in
   one Transaction. The Title Block dropdown + Apply to All Sheets
   button swaps every existing sheet's title block instance to the
   selected type (via ChangeTypeId, preserving position/overrides), or
   places one if a sheet currently has none.

Needs live-Revit verification: ChangeTypeId on a placed title block
instance, and Document.Create.NewFamilyInstance(XYZ.Zero, symbol,
sheet) for placing a title block on a sheet that doesn't have one.
"""
import os
import System
from pyrevit import forms, script
import dee_branding
from Autodesk.Revit.DB import (
    FilteredElementCollector, BuiltInCategory, ViewSheet, ElementId, Transaction,
    BuiltInParameter, XYZ,
)
import xlsx_writer
import dee_sheet_renamer_service as renamer

import clr
clr.AddReference("System.Windows.Forms")
from System.Windows.Forms import SaveFileDialog, DialogResult, MessageBox

output = script.get_output()

_XAML_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.xaml")
_NONE_TITLEBLOCK_LABEL = "(None - no title block)"

_RN_TOKEN_CHOICES = [
    "{SheetNumber}", "{SheetName}", "{Serial}", "{Serial|PAD3}", "{Alpha}",
    "{Date}", "{Date:yyyyMMdd}",
]
_RN_TARGET_CHOICES = ["Number", "Name", "Both"]
_RN_POSITION_CHOICES = ["Prefix", "Suffix"]
_RN_NO_RESET_LABEL = "(No reset)"


def _read_name(element):
    """Element.Name can throw a bare, unhelpful "Name" exception on some
    element types in this Revit/IronPython combination - seen on
    ViewFamilyType and FamilySymbol (title block types). Almost certainly
    a reflection/property-binding quirk, not a real data problem, so this
    falls back to reading the same value through the Parameter system."""
    try:
        return element.Name
    except Exception:
        pass
    try:
        p = element.get_Parameter(BuiltInParameter.SYMBOL_NAME_PARAM)
        if p is not None:
            val = p.AsString()
            if val:
                return val
    except Exception:
        pass
    try:
        p = element.get_Parameter(BuiltInParameter.ALL_MODEL_TYPE_NAME)
        if p is not None:
            val = p.AsString()
            if val:
                return val
    except Exception:
        pass
    return None


def _collect_titleblock_types(doc):
    """Returns {"Family - Type": ElementId} for every Title Block type
    loaded in the project."""
    result = {}
    for t in (FilteredElementCollector(doc)
              .OfCategory(BuiltInCategory.OST_TitleBlocks)
              .WhereElementIsElementType()):
        try:
            fam_name = _read_name(t.Family)
            type_name = _read_name(t)
            if fam_name and type_name:
                result["{0} - {1}".format(fam_name, type_name)] = t.Id
        except Exception:
            continue
    return result


def _export_results_to_excel(rows, results, default_name):
    dlg = SaveFileDialog()
    dlg.Filter = "Excel Workbook (*.xlsx)|*.xlsx"
    dlg.FileName = default_name
    if dlg.ShowDialog() != DialogResult.OK:
        return
    status_style = {True: "ok", False: "fail", None: "skip"}
    status_label = {True: "Created", False: "Failed", None: "Skipped"}
    out_rows = []
    for (number, name), (ok, _label, detail) in zip(rows, results):
        out_rows.append(([number, name, status_label.get(ok, "Failed"), detail],
                          status_style.get(ok, "fail")))
    try:
        xlsx_writer.write_themed_xlsx(
            dlg.FileName, "DeeSheet - Sheet Creation Results",
            ["Sheet Number", "Sheet Name", "Status", "Detail"],
            [16, 36, 14, 60], out_rows)
    except Exception as e:
        forms.alert("Could not export: {0}".format(e))
        return
    MessageBox.Show("Exported {0} row(s) to:\n{1}".format(len(out_rows), dlg.FileName), "DeeSheet")


# --------------------------------------------------------------------------
# Super Sheet data model
# --------------------------------------------------------------------------
_SUPER_STATUS_LABELS = {
    "new": "New",
    "modified": "Modified",
    "delete": "To Delete",
    "discarded": "Discarded",
    "existing": "Existing",
}


class SuperSheetRow(object):
    def __init__(self, sheet, number, name):
        self.sheet = sheet
        self.is_new = sheet is None
        self.original_number = number
        self.original_name = name
        self.number = number
        self.name = name
        self.marked_delete = False

    @property
    def status_key(self):
        if self.marked_delete:
            return "discarded" if self.is_new else "delete"
        if self.is_new:
            return "new"
        if self.number != self.original_number or self.name != self.original_name:
            return "modified"
        return "existing"

    @property
    def status_text(self):
        return _SUPER_STATUS_LABELS.get(self.status_key, "Existing")


def _scan_all_sheets(doc):
    rows = []
    for sheet in FilteredElementCollector(doc).OfClass(ViewSheet):
        try:
            rows.append(SuperSheetRow(sheet, sheet.SheetNumber, _read_name(sheet) or ""))
        except Exception:
            continue
    rows.sort(key=lambda r: r.number)
    return rows


class DeeSheetWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, doc):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.doc = doc
        self._titleblock_types = _collect_titleblock_types(doc)
        self._super_rows = []
        self._last_results = []
        self._last_rows = []
        self._rename_rows = []
        self._rename_view_rows = []

        tb_names = [_NONE_TITLEBLOCK_LABEL] + sorted(self._titleblock_types.keys())
        self.titleblock_cb.ItemsSource = tb_names
        self.titleblock_cb.SelectedIndex = 0
        self.super_titleblock_cb.ItemsSource = tb_names
        self.super_titleblock_cb.SelectedIndex = 0

        self._scan_super_sheets()
        self._init_renamer_tab()
        self._scan_renamer()

    def _selected_titleblock_id(self, combo):
        label = combo.SelectedItem
        if not label or label == _NONE_TITLEBLOCK_LABEL:
            return ElementId.InvalidElementId
        return self._titleblock_types.get(label, ElementId.InvalidElementId)

    def _report(self, title, results):
        html = ['<h2 style="font-family:sans-serif;color:#ddd;">{0}</h2>'.format(title)]
        for ok, label, detail in results:
            bg = "#2e7d32" if ok else ("#455a64" if ok is None else "#c62828")
            icon = "&#10003;" if ok else ("&#9888;" if ok is None else "&#10007;")
            html.append(
                '<div style="padding:6px 12px;margin:3px 0;background:{0};color:#fff;'
                'border-radius:4px;font-family:monospace;font-size:12px;">'
                '{1}&nbsp; <b>{2}</b> &mdash; {3}</div>'.format(bg, icon, label, detail))
        ok_count = sum(1 for r in results if r[0] is True)
        html.append('<hr><b style="font-family:sans-serif;">{0} / {1} succeeded.</b>'.format(
            ok_count, len(results)))
        output.print_html("".join(html))

    # ======================================================================
    # Tab 1: Normal Creation
    # ======================================================================
    def create_click(self, sender, args):
        numbers = [n.strip() for n in self.numbers_box.Text.splitlines()]
        names = [n.strip() for n in self.names_box.Text.splitlines()]
        while numbers and numbers[-1] == "":
            numbers.pop()
        while names and names[-1] == "":
            names.pop()

        if not numbers or not names:
            forms.alert("Paste at least one Sheet Number and one Sheet Name before creating.")
            return
        if len(numbers) != len(names):
            forms.alert(
                "Sheet Number ({0} lines) and Sheet Name ({1} lines) don't match. Fix the paste so "
                "both boxes have the same number of lines, one sheet per line.".format(
                    len(numbers), len(names)))
            return

        titleblock_id = self._selected_titleblock_id(self.titleblock_cb)

        existing_numbers = set()
        for vs in FilteredElementCollector(self.doc).OfClass(ViewSheet):
            try:
                existing_numbers.add(vs.SheetNumber)
            except Exception:
                continue

        results = []
        t = Transaction(self.doc, "DeeSheet - Create Sheets")
        t.Start()
        try:
            for number, name in zip(numbers, names):
                if not number or not name:
                    results.append((False, "{0} / {1}".format(number, name),
                                     "Skipped - empty number or name"))
                    continue
                if number in existing_numbers:
                    results.append((None, number, "Already exists - skipped"))
                    continue
                try:
                    sheet = ViewSheet.Create(self.doc, titleblock_id)
                    sheet.SheetNumber = number
                    sheet.Name = name
                    existing_numbers.add(number)
                    results.append((True, number, "Created '{0}'".format(name)))
                except Exception as e:
                    results.append((False, number, "FAILED: {0}".format(e)))
            t.Commit()
        except Exception as e:
            t.RollBack()
            forms.alert("Sheet creation aborted: {0}".format(e))
            return

        self._last_results = results
        self._last_rows = list(zip(numbers, names))
        self._report("DeeSheet - Create Sheets Results", results)
        self._scan_super_sheets()

    def export_results_click(self, sender, args):
        if not self._last_results:
            forms.alert("Create some sheets first - nothing to export yet.")
            return
        _export_results_to_excel(self._last_rows, self._last_results, "DeeSheet_Results.xlsx")

    # ======================================================================
    # Tab 2: Super Sheet
    # ======================================================================
    def _scan_super_sheets(self):
        self._super_rows = _scan_all_sheets(self.doc)
        self._refresh_super_grid()

    def super_scan_click(self, sender, args):
        self._scan_super_sheets()

    def _refresh_super_grid(self):
        self.super_grid.ItemsSource = None
        self.super_grid.ItemsSource = self._super_rows
        new_count = sum(1 for r in self._super_rows if r.is_new)
        del_count = sum(1 for r in self._super_rows if r.marked_delete)
        self.super_status_tb.Text = "{0} sheet(s) ({1} new, {2} marked for deletion).".format(
            len(self._super_rows), new_count, del_count)

    def super_grid_row_edit_ending(self, sender, args):
        # WPF forbids CollectionView.Refresh() while a row edit is still
        # committing (throws "'Refresh' is not allowed during an AddNew or
        # EditItem transaction"). Deferring it to the next dispatcher cycle
        # (same fix already proven in DeeView's views_row_edit_ending) lets
        # the edit finish committing first, so the Status column still
        # repaints right away.
        try:
            self.Dispatcher.BeginInvoke(System.Action(self._refresh_super_grid))
        except Exception:
            pass

    def super_add_row_click(self, sender, args):
        self._super_rows.append(SuperSheetRow(None, "", ""))
        self._refresh_super_grid()

    def super_move_up_click(self, sender, args):
        row = self.super_grid.SelectedItem
        if not row:
            return
        idx = self._super_rows.index(row)
        if idx > 0:
            self._super_rows[idx - 1], self._super_rows[idx] = self._super_rows[idx], self._super_rows[idx - 1]
            self._refresh_super_grid()
            self.super_grid.SelectedItem = row

    def super_move_down_click(self, sender, args):
        row = self.super_grid.SelectedItem
        if not row:
            return
        idx = self._super_rows.index(row)
        if idx < len(self._super_rows) - 1:
            self._super_rows[idx + 1], self._super_rows[idx] = self._super_rows[idx], self._super_rows[idx + 1]
            self._refresh_super_grid()
            self.super_grid.SelectedItem = row

    def super_remove_selected_click(self, sender, args):
        highlighted = list(self.super_grid.SelectedItems)
        if not highlighted:
            forms.alert("Click a row (Shift-click or Ctrl-click for more) to highlight rows first.")
            return
        remaining = []
        for r in self._super_rows:
            if r in highlighted and r.is_new:
                continue  # never existed - just drop it
            if r in highlighted:
                r.marked_delete = True
            remaining.append(r)
        self._super_rows = remaining
        self._refresh_super_grid()

    def super_apply_titleblock_click(self, sender, args):
        type_id = self._selected_titleblock_id(self.super_titleblock_cb)
        if type_id is None or type_id == ElementId.InvalidElementId:
            forms.alert("Pick a Title Block type from the dropdown first.")
            return
        existing_rows = [r for r in self._super_rows if not r.is_new and not r.marked_delete]
        if not existing_rows:
            forms.alert("No existing sheets to update.")
            return
        if not forms.alert(
                "Change the Title Block on {0} sheet(s) to the selected type?".format(len(existing_rows)),
                title="DeeSheet - Confirm", yes=True, no=True):
            return

        tb_symbol = self.doc.GetElement(type_id)
        results = []
        t = Transaction(self.doc, "DeeSheet - Change Title Block on All Sheets")
        t.Start()
        try:
            if tb_symbol is not None and not tb_symbol.IsActive:
                tb_symbol.Activate()
                self.doc.Regenerate()
        except Exception:
            pass
        for r in existing_rows:
            try:
                existing_tbs = list(FilteredElementCollector(self.doc, r.sheet.Id)
                                     .OfCategory(BuiltInCategory.OST_TitleBlocks)
                                     .WhereElementIsNotElementType())
                if existing_tbs:
                    for tb in existing_tbs:
                        tb.ChangeTypeId(type_id)
                    results.append((True, r.number, "Title Block changed"))
                else:
                    self.doc.Create.NewFamilyInstance(XYZ.Zero, tb_symbol, r.sheet)
                    results.append((True, r.number, "Title Block placed (sheet had none)"))
            except Exception as e:
                results.append((False, r.number, "FAILED: {0}".format(e)))
        t.Commit()

        self._report("DeeSheet - Change Title Block Results", results)

    def super_apply_click(self, sender, args):
        to_create = [r for r in self._super_rows if r.is_new and not r.marked_delete]
        to_delete = [r for r in self._super_rows if r.marked_delete and not r.is_new]
        to_update = [r for r in self._super_rows if not r.is_new and not r.marked_delete and
                     (r.number != r.original_number or r.name != r.original_name)]

        if not to_create and not to_delete and not to_update:
            forms.alert("No changes to apply.")
            return

        if not forms.alert(
                "Apply changes?\n\n{0} sheet(s) to create\n{1} sheet(s) to update\n"
                "{2} sheet(s) to delete".format(len(to_create), len(to_update), len(to_delete)),
                title="DeeSheet - Confirm", yes=True, no=True):
            return

        titleblock_id = self._selected_titleblock_id(self.super_titleblock_cb)
        existing_numbers = set(r.number for r in self._super_rows if not r.is_new and not r.marked_delete)

        results = []
        t = Transaction(self.doc, "DeeSheet - Apply Super Sheet Changes")
        t.Start()
        try:
            for r in to_create:
                if not r.number or not r.name:
                    results.append((False, "{0} / {1}".format(r.number, r.name),
                                     "Skipped - empty number or name"))
                    continue
                if r.number in existing_numbers:
                    results.append((False, r.number, "Skipped - duplicate Sheet Number"))
                    continue
                try:
                    sheet = ViewSheet.Create(self.doc, titleblock_id)
                    sheet.SheetNumber = r.number
                    sheet.Name = r.name
                    r.sheet = sheet
                    r.is_new = False
                    r.original_number = r.number
                    r.original_name = r.name
                    existing_numbers.add(r.number)
                    results.append((True, r.number, "Created '{0}'".format(r.name)))
                except Exception as e:
                    results.append((False, r.number, "FAILED: {0}".format(e)))

            for r in to_update:
                try:
                    if r.number != r.original_number:
                        r.sheet.SheetNumber = r.number
                    if r.name != r.original_name:
                        r.sheet.Name = r.name
                    r.original_number = r.number
                    r.original_name = r.name
                    results.append((True, r.number, "Updated"))
                except Exception as e:
                    results.append((False, r.original_number, "FAILED: {0}".format(e)))

            for r in to_delete:
                try:
                    self.doc.Delete(r.sheet.Id)
                    results.append((True, r.original_number, "Deleted"))
                except Exception as e:
                    results.append((False, r.original_number, "FAILED: {0}".format(e)))

            t.Commit()
        except Exception as e:
            t.RollBack()
            forms.alert("Apply aborted: {0}".format(e))
            return

        deleted_set = set(to_delete)
        self._super_rows = [r for r in self._super_rows if r not in deleted_set]
        self._refresh_super_grid()
        self._report("DeeSheet - Apply Super Sheet Changes Results", results)

    # ======================================================================
    # Tab 3: Sheet Renamer
    # ======================================================================
    def _init_renamer_tab(self):
        self.token_cb.ItemsSource = _RN_TOKEN_CHOICES
        self.token_cb.SelectedIndex = 0
        self.fr_target_cb.ItemsSource = _RN_TARGET_CHOICES
        self.fr_target_cb.SelectedIndex = 2
        self.add_target_cb.ItemsSource = _RN_TARGET_CHOICES
        self.add_target_cb.SelectedIndex = 0
        self.add_position_cb.ItemsSource = _RN_POSITION_CHOICES
        self.add_position_cb.SelectedIndex = 0
        self.seg_direction_cb.ItemsSource = ["Left", "Right"]
        self.seg_direction_cb.SelectedIndex = 0
        self.seg_target_cb.ItemsSource = _RN_TARGET_CHOICES
        self.seg_target_cb.SelectedIndex = 2
        self.case_target_cb.ItemsSource = _RN_TARGET_CHOICES
        self.case_target_cb.SelectedIndex = 2
        self._refresh_preset_list()

    def _scan_renamer(self):
        with forms.ProgressBar(title="DeeSheet - scanning sheets...", indeterminate=True):
            self._rename_rows = renamer.scan(self.doc)
            param_names = renamer.list_common_parameter_names(self._rename_rows)
        self.param_token_cb.ItemsSource = param_names
        if param_names:
            self.param_token_cb.SelectedIndex = 0
        self.sort_by_cb.ItemsSource = ["Sheet Number", "Sheet Name"] + param_names
        self.sort_by_cb.SelectedIndex = 0
        self.reset_by_cb.ItemsSource = [_RN_NO_RESET_LABEL] + param_names
        self.reset_by_cb.SelectedIndex = 0
        self._rename_view_rows = list(self._rename_rows)
        self._refresh_rename_grid()

    def rn_refresh_click(self, sender, args):
        self._scan_renamer()

    def _refresh_rename_grid(self):
        self.rn_grid.ItemsSource = None
        self.rn_grid.ItemsSource = self._rename_view_rows
        self._update_rn_status()

    def _update_rn_status(self):
        sel_count = sum(1 for r in self._rename_rows if r.selected)
        self.rn_status_tb.Text = "{0} sheet(s) total, {1} selected, {2} shown.".format(
            len(self._rename_rows), sel_count, len(self._rename_view_rows))

    def rn_filter_click(self, sender, args):
        q = (self.rn_search_tb.Text or "").strip().lower()
        if not q:
            self._rename_view_rows = list(self._rename_rows)
        else:
            self._rename_view_rows = [
                r for r in self._rename_rows
                if q in r.original_number.lower() or q in r.original_name.lower()
                or q in (r.new_number or "").lower() or q in (r.new_name or "").lower()]
        self._refresh_rename_grid()

    def rn_clear_filter_click(self, sender, args):
        self.rn_search_tb.Text = ""
        self._rename_view_rows = list(self._rename_rows)
        self._refresh_rename_grid()

    def rn_grid_row_edit_ending(self, sender, args):
        # Same WPF constraint as super_grid_row_edit_ending above -
        # CollectionView.Refresh() can't run while a row edit is still
        # committing, so the status recompute is deferred one dispatcher
        # cycle.
        try:
            self.Dispatcher.BeginInvoke(System.Action(self._recompute_rn_statuses))
        except Exception:
            pass

    def _recompute_rn_statuses(self):
        renamer.compute_statuses(self._rename_rows)
        self._refresh_rename_grid()

    # ---------------- selection ----------------
    def rn_select_all_click(self, sender, args):
        for r in self._rename_view_rows:
            r.selected = True
        self._refresh_rename_grid()

    def rn_select_none_click(self, sender, args):
        for r in self._rename_view_rows:
            r.selected = False
        self._refresh_rename_grid()

    def rn_select_highlighted_click(self, sender, args):
        highlighted = list(self.rn_grid.SelectedItems)
        if not highlighted:
            forms.alert("Click a row (Shift-click or Ctrl-click for more) to highlight rows first.")
            return
        for r in highlighted:
            r.selected = True
        self._refresh_rename_grid()

    def rn_deselect_highlighted_click(self, sender, args):
        highlighted = list(self.rn_grid.SelectedItems)
        if not highlighted:
            forms.alert("Click a row (Shift-click or Ctrl-click for more) to highlight rows first.")
            return
        for r in highlighted:
            r.selected = False
        self._refresh_rename_grid()

    # ---------------- rule builder ----------------
    def _insert_into(self, textbox, token_text):
        if not token_text:
            return
        textbox.Text = (textbox.Text or "") + token_text
        try:
            textbox.CaretIndex = len(textbox.Text)
            textbox.Focus()
        except Exception:
            pass

    def insert_number_token_click(self, sender, args):
        self._insert_into(self.number_rule_tb, self.token_cb.SelectedItem)

    def insert_name_token_click(self, sender, args):
        self._insert_into(self.name_rule_tb, self.token_cb.SelectedItem)

    def insert_number_param_click(self, sender, args):
        name = self.param_token_cb.SelectedItem
        if name:
            self._insert_into(self.number_rule_tb, "{Parameter:%s}" % name)

    def insert_name_param_click(self, sender, args):
        name = self.param_token_cb.SelectedItem
        if name:
            self._insert_into(self.name_rule_tb, "{Parameter:%s}" % name)

    def generate_preview_click(self, sender, args):
        if not any(r.selected for r in self._rename_rows):
            forms.alert("Check at least one sheet first.")
            return
        try:
            start = int(self.seq_start_tb.Text or "1")
        except Exception:
            start = 1
        try:
            step = int(self.seq_step_tb.Text or "1")
        except Exception:
            step = 1
        sort_key = self.sort_by_cb.SelectedItem or "Sheet Number"
        reset_key = self.reset_by_cb.SelectedItem
        if reset_key == _RN_NO_RESET_LABEL:
            reset_key = None
        # Reading a parameter (sort/reset field) touches the Revit API
        # once per selected sheet - on a large project this can take a
        # few seconds, during which the window would otherwise show no
        # feedback at all and look hung. Same pattern as _scan_renamer.
        with forms.ProgressBar(title="DeeSheet - generating preview...", indeterminate=True):
            renamer.generate_preview(self._rename_rows, self.number_rule_tb.Text, self.name_rule_tb.Text,
                                      start, step, reset_key, sort_key)
            renamer.compute_statuses(self._rename_rows)
        self._refresh_rename_grid()

    def reset_preview_click(self, sender, args):
        renamer.reset_preview(self._rename_rows)
        renamer.compute_statuses(self._rename_rows)
        self._refresh_rename_grid()

    def _rn_target_value(self, combo):
        label = combo.SelectedItem or "Both"
        return label.lower()

    def find_replace_click(self, sender, args):
        if not any(r.selected for r in self._rename_rows):
            forms.alert("Check at least one sheet first.")
            return
        if not self.find_tb.Text:
            forms.alert("Enter text to find first.")
            return
        renamer.find_replace(self._rename_rows, self.find_tb.Text, self.replace_tb.Text or "",
                              self._rn_target_value(self.fr_target_cb),
                              bool(self.fr_case_cb.IsChecked), bool(self.fr_word_cb.IsChecked))
        renamer.compute_statuses(self._rename_rows)
        self._refresh_rename_grid()

    def add_text_click(self, sender, args):
        if not any(r.selected for r in self._rename_rows):
            forms.alert("Check at least one sheet first.")
            return
        if not self.add_text_tb.Text:
            forms.alert("Enter text to add first.")
            return
        position = "prefix" if (self.add_position_cb.SelectedItem or "Prefix") == "Prefix" else "suffix"
        renamer.add_text(self._rename_rows, self.add_text_tb.Text, self._rn_target_value(self.add_target_cb), position)
        renamer.compute_statuses(self._rename_rows)
        self._refresh_rename_grid()

    def extract_segment_click(self, sender, args):
        if not any(r.selected for r in self._rename_rows):
            forms.alert("Check at least one sheet first.")
            return
        if not self.seg_delim_tb.Text:
            forms.alert("Enter a delimiter to split on first.")
            return
        direction = "right" if (self.seg_direction_cb.SelectedItem or "Left") == "Right" else "left"
        renamer.extract_segment(self._rename_rows, self.seg_delim_tb.Text, direction,
                                 self.seg_number_tb.Text or "1", self._rn_target_value(self.seg_target_cb))
        renamer.compute_statuses(self._rename_rows)
        self._refresh_rename_grid()

    def _apply_case(self, mode):
        if not any(r.selected for r in self._rename_rows):
            forms.alert("Check at least one sheet first.")
            return
        renamer.convert_case(self._rename_rows, mode, self._rn_target_value(self.case_target_cb))
        renamer.compute_statuses(self._rename_rows)
        self._refresh_rename_grid()

    def case_upper_click(self, sender, args):
        self._apply_case("upper")

    def case_lower_click(self, sender, args):
        self._apply_case("lower")

    def case_title_click(self, sender, args):
        self._apply_case("title")

    # ---------------- presets ----------------
    def _current_rule_settings(self):
        return {
            "number_rule": self.number_rule_tb.Text or "",
            "name_rule": self.name_rule_tb.Text or "",
            "seq_start": self.seq_start_tb.Text or "1",
            "seq_step": self.seq_step_tb.Text or "1",
            "sort_by": self.sort_by_cb.SelectedItem or "Sheet Number",
            "reset_by": self.reset_by_cb.SelectedItem or _RN_NO_RESET_LABEL,
        }

    def _refresh_preset_list(self, select_name=None):
        names = renamer.list_presets()
        self.preset_cb.ItemsSource = names
        if select_name and select_name in names:
            self.preset_cb.SelectedItem = select_name
        elif names:
            self.preset_cb.SelectedIndex = 0

    def preset_save_click(self, sender, args):
        name = forms.ask_for_string(default="", prompt="Preset name:", title="DeeSheet - Save Preset")
        if not name:
            return
        renamer.save_preset(name, self._current_rule_settings())
        self._refresh_preset_list(select_name=name)

    def preset_load_click(self, sender, args):
        name = self.preset_cb.SelectedItem
        if not name:
            forms.alert("Pick a preset first.")
            return
        data = renamer.load_preset(name)
        if not data:
            forms.alert("Could not load preset '{0}'.".format(name))
            return
        self.number_rule_tb.Text = data.get("number_rule", "")
        self.name_rule_tb.Text = data.get("name_rule", "")
        self.seq_start_tb.Text = data.get("seq_start", "1")
        self.seq_step_tb.Text = data.get("seq_step", "1")
        if data.get("sort_by") in list(self.sort_by_cb.ItemsSource or []):
            self.sort_by_cb.SelectedItem = data.get("sort_by")
        if data.get("reset_by") in list(self.reset_by_cb.ItemsSource or []):
            self.reset_by_cb.SelectedItem = data.get("reset_by")

    def preset_delete_click(self, sender, args):
        name = self.preset_cb.SelectedItem
        if not name:
            forms.alert("Pick a preset first.")
            return
        if not forms.alert("Delete preset '{0}'?".format(name), title="DeeSheet - Confirm", yes=True, no=True):
            return
        renamer.delete_preset(name)
        self._refresh_preset_list()

    # ---------------- apply ----------------
    def rn_apply_click(self, sender, args):
        renamer.compute_statuses(self._rename_rows)
        self._refresh_rename_grid()
        ready = [r for r in self._rename_rows if r.selected and r.status == renamer.STATUS_READY]
        blocked = [r for r in self._rename_rows if r.selected and
                   r.status in (renamer.STATUS_DUPLICATE, renamer.STATUS_EMPTY)]
        if not ready:
            forms.alert("Nothing Ready to apply. Generate a Preview first (or edit New Number/New Name "
                         "directly), and check the Status column - Duplicate/Empty rows must be fixed "
                         "before they can be applied.")
            return
        msg = "Apply {0} sheet rename(s)?".format(len(ready))
        if blocked:
            msg += ("\n\n{0} checked sheet(s) will be SKIPPED (Duplicate or Empty Status) - fix the rule "
                    "or edit those rows directly if you want them included.").format(len(blocked))
        if not forms.alert(msg, title="DeeSheet - Confirm", yes=True, no=True):
            return
        result = renamer.apply_renames(self.doc, self._rename_rows)
        renamer.print_report(result)
        renamer.compute_statuses(self._rename_rows)
        self._refresh_rename_grid()
        self._scan_super_sheets()

    def close_click(self, sender, args):
        self.Close()


def main():
    doc = __revit__.ActiveUIDocument.Document
    window = DeeSheetWindow(_XAML_FILE, doc)
    window.ShowDialog()


main()
