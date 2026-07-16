# -*- coding: utf-8 -*-
"""
DeeSheet
Paste the Sheet Number column and the Sheet Name column from Excel (one
column per box - copy just that column, paste into the matching box) and
batch-create the corresponding sheets in the currently active document.

Line 1 in both boxes pairs up as one sheet, line 2 as the next, etc. Sheet
numbers that already exist in the project are skipped rather than
overwritten.

After creation, offers to export the results to a themed .xlsx (via the
shared lib/xlsx_writer.py - no openpyxl/Excel automation needed) styled
to match NAGA's LOD Drawings Index register: Arial, light-gray header
band, thin borders, bold title row, status-tinted rows.
"""
import os
from pyrevit import forms, script
from Autodesk.Revit.DB import (
    FilteredElementCollector, BuiltInCategory, ViewSheet, ElementId, Transaction,
    BuiltInParameter
)
import xlsx_writer

import clr
clr.AddReference("System.Windows.Forms")
from System.Windows.Forms import SaveFileDialog, DialogResult, MessageBox

output = script.get_output()

_XAML_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.xaml")


class _SheetPasteWindow(forms.WPFWindow):
    def __init__(self, xaml_file):
        forms.WPFWindow.__init__(self, xaml_file)
        self.confirmed = False

    def create_click(self, sender, args):
        self.confirmed = True
        self.Close()

    def cancel_click(self, sender, args):
        self.confirmed = False
        self.Close()


def _read_name(element):
    """Element.Name can throw a bare, unhelpful "Name" exception on some
    element types in this Revit/IronPython combination - already seen on
    ViewFamilyType and now on FamilySymbol (title block types) too. Almost
    certainly a reflection/property-binding quirk, not a real data
    problem, so this falls back to reading the same value through the
    Parameter system instead, which sidesteps it entirely."""
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


def _get_title_block_id(doc):
    """Returns an ElementId to pass to ViewSheet.Create, or None if the
    user cancelled a picker. ElementId.InvalidElementId means "no title
    block", which Revit fully supports.

    Asks in two steps, matching Revit's own Family > Type hierarchy: first
    which title block FAMILY is loaded (if more than one), then which TYPE
    within that family (if it has more than one)."""
    types = list(FilteredElementCollector(doc)
                 .OfCategory(BuiltInCategory.OST_TitleBlocks)
                 .WhereElementIsElementType())

    none_label = "None (no title block)"

    families = {}  # family_name -> [(type_name, ElementId), ...]
    last_err = None
    for t in types:
        try:
            fam = t.Family
            fam_name = _read_name(fam)
        except Exception as e:
            last_err = "[Family] {0}".format(e)
            continue
        type_name = _read_name(t)
        if fam_name is None or type_name is None:
            last_err = "[Name] could not read Family/Type name via .Name or Parameter fallback"
            continue
        families.setdefault(fam_name, []).append((type_name, t.Id))

    if not families:
        try:
            doc_title = doc.Title
        except Exception:
            doc_title = "(unknown)"
        if types:
            # Title block types WERE found, but every single one failed to
            # read .Family.Name / .Name - a genuine property-read problem,
            # not "nothing loaded". Different message so it's not mistaken
            # for the empty case.
            forms.alert(
                "Found {0} Title Block type(s) in '{1}', but couldn't read "
                "their Family/Type names. Sheets will be created with no "
                "title block.\n\nLast error: {2}".format(
                    len(types), doc_title, last_err)
            )
        else:
            forms.alert(
                "No Title Block family is loaded in '{0}', so there's "
                "nothing to pick from - sheets will be created with no "
                "title block. Load one first (Insert tab > Load Family) "
                "if you want one, then run DeeSheet again.\n\n"
                "If you expected title blocks to show up here, check "
                "that this really is the file you're looking at - "
                "multiple documents may be open at once.".format(doc_title)
            )
        return ElementId.InvalidElementId

    family_options = [none_label] + sorted(families.keys())

    family_choice = forms.SelectFromList.show(
        family_options,
        title="DeeSheet — Select Title Block Family loaded in this file")
    if not family_choice:
        return None
    if family_choice == none_label:
        return ElementId.InvalidElementId

    type_list = families[family_choice]
    if len(type_list) == 1:
        return type_list[0][1]

    type_lookup = dict(type_list)
    type_choice = forms.SelectFromList.show(
        sorted(type_lookup.keys()),
        title="DeeSheet — Select Type within '{0}'".format(family_choice))
    if not type_choice:
        return None
    return type_lookup[type_choice]


def _export_results_to_excel(numbers, names, results):
    dlg = SaveFileDialog()
    dlg.Filter = "Excel Workbook (*.xlsx)|*.xlsx"
    dlg.FileName = "DeeSheet_Results.xlsx"
    if dlg.ShowDialog() != DialogResult.OK:
        return
    status_style = {True: "ok", False: "fail", None: "skip"}
    status_label = {True: "Created", False: "Failed", None: "Skipped"}
    rows = []
    for (number, name), (ok, _label, detail) in zip(zip(numbers, names), results):
        rows.append(([number, name, status_label.get(ok, "Failed"), detail],
                      status_style.get(ok, "fail")))
    try:
        xlsx_writer.write_themed_xlsx(
            dlg.FileName, "DeeSheet - Sheet Creation Results",
            ["Sheet Number", "Sheet Name", "Status", "Detail"],
            [16, 36, 14, 60], rows)
    except Exception as e:
        forms.alert("Could not export: {0}".format(e))
        return
    MessageBox.Show("Exported {0} row(s) to:\n{1}".format(len(rows), dlg.FileName), "DeeSheet")


def main():
    doc = __revit__.ActiveUIDocument.Document

    titleblock_id = _get_title_block_id(doc)
    if titleblock_id is None:
        return

    window = _SheetPasteWindow(_XAML_FILE)
    window.show(modal=True)
    if not window.confirmed:
        return

    numbers = [n.strip() for n in window.numbers_box.Text.splitlines()]
    names = [n.strip() for n in window.names_box.Text.splitlines()]
    while numbers and numbers[-1] == "":
        numbers.pop()
    while names and names[-1] == "":
        names.pop()

    if not numbers or not names:
        forms.alert("Paste at least one Sheet Number and one Sheet Name before creating.")
        return

    if len(numbers) != len(names):
        forms.alert(
            "Sheet Number ({0} lines) and Sheet Name ({1} lines) don't "
            "match. Fix the paste so both boxes have the same number of "
            "lines, one sheet per line.".format(len(numbers), len(names)))
        return

    existing_numbers = set()
    for vs in FilteredElementCollector(doc).OfClass(ViewSheet):
        try:
            existing_numbers.add(vs.SheetNumber)
        except Exception:
            continue

    results = []
    t = Transaction(doc, "DeeSheet - Create Sheets")
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
                sheet = ViewSheet.Create(doc, titleblock_id)
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

    html = '<h2 style="font-family:sans-serif;color:#ddd;">DeeSheet Results</h2>'
    for ok, number, detail in results:
        bg = "#2e7d32" if ok else ("#455a64" if ok is None else "#c62828")
        icon = "&#10003;" if ok else ("&#9888;" if ok is None else "&#10007;")
        html += (
            '<div style="padding:8px 14px;margin:3px 0;background:{0};color:#fff;'
            'border-radius:4px;font-family:monospace;font-size:13px;">'
            '<b>{1}</b>&nbsp; {2} &mdash; {3}'
            '</div>'.format(bg, icon, number, detail)
        )
    ok_count = sum(1 for r in results if r[0])
    html += ('<hr><b style="font-family:sans-serif;">{0} / {1} sheets '
            'created.</b>').format(ok_count, len(results))
    output.print_html(html)

    if results and forms.alert(
            "Export these results to Excel?", title="DeeSheet", yes=True, no=True):
        _export_results_to_excel(numbers, names, results)


main()
