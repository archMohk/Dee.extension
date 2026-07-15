# -*- coding: utf-8 -*-
"""
DeeScheduleXL (ExporterPack)
Schedule <-> Excel sync, similar in spirit to BIMOne's Export/ImportExcel:

Export tab: check which Schedules to export, pick a mode, Export to Excel.
  - Bidirectional: for ITEMIZED schedules (one row per element), adds a
    hidden Element Id column and a hidden per-sheet fingerprint (the
    schedule's UniqueId + its exact header list) in row 1, and locks
    (real Excel cell protection, not just a visual cue) any column whose
    field could not be resolved to a writable Parameter - calculated or
    combined-parameter fields, mainly. This is what makes re-import
    possible. Non-itemized schedules are still exported (for reference)
    but without the hidden column/locking/fingerprint, since "one row =
    one element" does not hold for them.
  - Unidirectional: a clean copy with no hidden column or locking -
    matches the reference tool's "keeps formatting but import
    impossible" mode.

Import tab: browse to a previously Bidirectional-exported file. Each
sheet's fingerprint is checked against a live Schedule in the CURRENT
model (same UniqueId, same header list) and split into Compatible / Not
compatible, mirroring the reference tool's UI. Import writes edited
(unlocked) cell values back onto the matching elements (via the hidden
Element Id), inside one Transaction with a per-row/per-field try/except
and a results report - nothing changes in the model until Import is
clicked.

Known scope limits (v1): "Standards" exports (Line Styles, Object
Styles, Family Listing, Parameter Settings, Project Information) from
the reference tool are not included here. ElementId-storage parameters
(fields that reference another element) are exported as their
referenced element's name but are not writable via Import in this
version.
"""
import os

import clr
clr.AddReference("System.Windows.Forms")
clr.AddReference("WindowsBase")
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
clr.AddReference("System.Data")
from System.Windows.Forms import SaveFileDialog, OpenFileDialog, DialogResult, MessageBox
from System.Windows import Clipboard
from System.Windows.Input import Key, Keyboard, ModifierKeys
from System.Data import DataTable

from pyrevit import forms, script
from Autodesk.Revit.DB import (
    FilteredElementCollector, ViewSchedule, Transaction, ElementId, StorageType,
    Category, UnitFormatUtils, BuiltInParameter
)

import xlsx_writer
import xlsx_reader

output = script.get_output()

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")


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


def _schedule_category_name(doc, schedule):
    try:
        cat_id = schedule.Definition.CategoryId
    except Exception:
        return "(Unknown)"
    if cat_id is None or cat_id == ElementId.InvalidElementId:
        return "(Multi-Category / Other)"
    try:
        cat = Category.GetCategory(doc, cat_id)
        if cat is not None:
            return _read_name(cat) or "(Unknown)"
    except Exception:
        pass
    return "(Unknown)"


def _read_field_name(f):
    try:
        n = f.GetName()
        if n:
            return n
    except Exception:
        pass
    return "(field)"


def _schedule_fields(definition):
    fields = []
    try:
        field_ids = list(definition.GetFieldOrder())
    except Exception:
        field_ids = []
    for fid in field_ids:
        try:
            f = definition.GetField(fid)
        except Exception:
            continue
        try:
            if f.IsHidden():
                continue
        except Exception:
            pass
        fields.append(f)
    return fields


def _roundtrip_headers(definition):
    fields = _schedule_fields(definition)
    return ["Element Id"] + [_read_field_name(f) for f in fields], fields


# -- per-element/field parameter resolution -----------------------------
# field.ParameterId is tried first, but is not trusted alone - matching an
# element's own parameters by DISPLAY NAME against the field's column
# header is the robust fallback (and in practice the one that reliably
# works), since a schedule column is essentially always named after the
# real parameter it displays.
def _element_param_maps(element):
    """Returns (by_id, by_name) dicts for one element's parameters."""
    by_id = {}
    by_name = {}
    if element is None:
        return by_id, by_name
    try:
        params = list(element.Parameters)
    except Exception:
        params = []
    for p in params:
        try:
            by_id[p.Id] = p
        except Exception:
            pass
        try:
            nm = p.Definition.Name
            if nm and nm not in by_name:
                by_name[nm] = p
        except Exception:
            pass
    return by_id, by_name


def _element_type_or_none(doc, element):
    try:
        type_id = element.GetTypeId()
    except Exception:
        return None
    if type_id is None or type_id == ElementId.InvalidElementId:
        return None
    return doc.GetElement(type_id)


def _resolve_field_parameter(field, field_name, elem_maps, type_maps):
    elem_by_id, elem_by_name = elem_maps
    type_by_id, type_by_name = type_maps
    try:
        param_id = field.ParameterId
    except Exception:
        param_id = None
    if param_id is not None and param_id != ElementId.InvalidElementId:
        p = elem_by_id.get(param_id) or type_by_id.get(param_id)
        if p is not None:
            return p
    return elem_by_name.get(field_name) or type_by_name.get(field_name)


def _read_param_display_value(param):
    """Returns (text, locked). locked=True means this column should be
    read-only in the export (no resolvable/writable parameter)."""
    if param is None:
        return "", True
    try:
        locked = bool(param.IsReadOnly)
    except Exception:
        locked = True
    try:
        val = param.AsValueString()
        if val is not None:
            return val, locked
    except Exception:
        pass
    try:
        st = param.StorageType
        if st == StorageType.String:
            return param.AsString() or "", locked
        if st == StorageType.Integer:
            return str(param.AsInteger()), locked
        if st == StorageType.Double:
            return str(param.AsDouble()), locked
        if st == StorageType.ElementId:
            eid = param.AsElementId()
            if eid is not None and eid != ElementId.InvalidElementId:
                ref_elem = param.Element.Document.GetElement(eid)
                return (_read_name(ref_elem) or ""), True
            return "", True
    except Exception:
        pass
    return "", True


def _write_param_from_text(doc, param, text):
    st = param.StorageType
    if st == StorageType.String:
        param.Set(text or "")
        return
    if st == StorageType.Integer:
        t = (text or "").strip().lower()
        if t in ("yes", "true"):
            param.Set(1)
            return
        if t in ("no", "false"):
            param.Set(0)
            return
        param.Set(int(float(text)))
        return
    if st == StorageType.Double:
        try:
            spec_id = param.Definition.GetDataType()
            success, value = UnitFormatUtils.TryParse(doc.GetUnits(), spec_id, text)
            if success:
                param.Set(value)
                return
        except Exception:
            pass
        param.Set(float(text))
        return
    raise Exception("Unsupported parameter type for import (ElementId-referencing fields "
                    "are not writable in this version)")


# -- data model -----------------------------------------------------------
class ScheduleRow(object):
    def __init__(self, schedule, name, category_name, is_itemized, field_count):
        self.schedule = schedule
        self.id = schedule.Id
        self.name = name
        self.category_name = category_name
        self.is_itemized = is_itemized
        self.field_count = field_count
        self.is_selected = False

    @property
    def itemized_text(self):
        return "Yes" if self.is_itemized else "No"

    @property
    def field_count_text(self):
        return str(self.field_count)


class ImportSheetRow(object):
    def __init__(self, sheet_name, grid, compatible, reason=""):
        self.sheet_name = sheet_name
        self.grid = grid
        self.compatible = compatible
        self.reason = reason
        self.is_selected = compatible
        self.schedule = None
        self.row_count = max(0, len(grid) - 2) if compatible else 0

    @property
    def row_count_text(self):
        return str(self.row_count)


# -- export build -----------------------------------------------------------
def _build_sheet_spec(doc, row, bidirectional):
    schedule = row.schedule
    definition = schedule.Definition
    headers_full, fields = _roundtrip_headers(definition)

    can_roundtrip = bidirectional and row.is_itemized
    warn = None
    if bidirectional and not row.is_itemized:
        warn = "'{0}': not itemized - exported for reference only, not import-compatible".format(row.name)

    if can_roundtrip:
        headers = headers_full
        hidden_cols = {0}
        locked_cols = {0}
        id_offset = 1
    else:
        headers = headers_full[1:]
        hidden_cols = set()
        locked_cols = set()
        id_offset = 0

    field_names = headers_full[1:]
    elements = list(FilteredElementCollector(doc, schedule.Id).WhereElementIsNotElementType())
    rows_out = []
    for el in elements:
        try:
            elem_maps = _element_param_maps(el)
            type_maps = _element_param_maps(_element_type_or_none(doc, el))

            row_values = [str(_element_id_value(el.Id))] if can_roundtrip else []
            for ci, f in enumerate(fields):
                param = _resolve_field_parameter(f, field_names[ci], elem_maps, type_maps)
                text, locked = _read_param_display_value(param)
                row_values.append(text)
                if can_roundtrip and locked:
                    locked_cols.add(ci + id_offset)
            rows_out.append(row_values)
        except Exception:
            continue

    fingerprint = "{0}::{1}".format(schedule.UniqueId, "|".join(headers_full)) if can_roundtrip else ""

    return {
        "name": row.name,
        "fingerprint": fingerprint,
        "headers": headers,
        "col_widths": [14] * len(headers) if can_roundtrip else [20] * len(headers),
        "hidden_cols": hidden_cols,
        "locked_cols": locked_cols,
        "rows": rows_out,
    }, warn


# -- live editor build (System.Data.DataTable backs the dynamic-column grid,
# since each Schedule has a different field set - .NET's DataRowView is what
# WPF's DataGrid can bind dynamic "[ColumnName]" indexer paths against; a
# plain IronPython object's __getitem__ is not guaranteed to be recognized
# the same way by the binding engine) --------------------------------------
def _build_editor_table(doc, schedule_row):
    schedule = schedule_row.schedule
    fields = _schedule_fields(schedule.Definition)

    dt = DataTable()
    dt.Columns.Add("ElementId", str)
    field_headers = []
    for f in fields:
        header = _read_field_name(f)
        if dt.Columns.Contains(header):
            header = "{0} ({1})".format(header, len(field_headers))
        field_headers.append(header)
        dt.Columns.Add(header, str)

    real_field_names = [_read_field_name(f) for f in fields]
    elements = list(FilteredElementCollector(doc, schedule.Id).WhereElementIsNotElementType())
    locked_indices = set()
    diag_lines = []
    for ei, el in enumerate(elements):
        try:
            elem_maps = _element_param_maps(el)
            type_maps = _element_param_maps(_element_type_or_none(doc, el))
            if ei == 0:
                diag_lines.append("Element Id: {0}".format(_element_id_value(el.Id)))
                diag_lines.append("Parameters found on element: {0}".format(len(elem_maps[1])))
                diag_lines.append("Parameters found on type: {0}".format(len(type_maps[1])))
            row_values = [str(_element_id_value(el.Id))]
            for fi, f in enumerate(fields):
                try:
                    field_pid = f.ParameterId
                except Exception as e:
                    field_pid = "EXC: {0}".format(e)
                param = _resolve_field_parameter(f, real_field_names[fi], elem_maps, type_maps)
                text, locked = _read_param_display_value(param)
                row_values.append(text)
                if locked:
                    locked_indices.add(fi)
                if ei == 0:
                    diag_lines.append(
                        "  Field '{0}': ParameterId={1}, resolved={2}, value='{3}', locked={4}".format(
                            real_field_names[fi], field_pid, param is not None, text, locked))
            dt.Rows.Add(row_values)
        except Exception as e:
            if ei == 0:
                diag_lines.append("FIRST ELEMENT FAILED: {0}".format(e))
            continue

    return dt, fields, field_headers, locked_indices, "\n".join(diag_lines)


class DeeScheduleXLWindow(forms.WPFWindow):
    def __init__(self, xaml_file, doc):
        forms.WPFWindow.__init__(self, xaml_file)
        self.doc = doc
        self._schedule_rows = []
        self._import_compatible = []
        self._import_incompatible = []
        self._editor_table = None
        self._editor_fields = None
        self._editor_field_headers = None
        self._editor_locked_headers = set()
        self._scan_schedules()

    # -- export tab ---------------------------------------------------------
    def _scan_schedules(self):
        rows = []
        for sched in FilteredElementCollector(self.doc).OfClass(ViewSchedule):
            try:
                if sched.IsTemplate:
                    continue
                name = _read_name(sched) or "(unnamed)"
                definition = sched.Definition
                try:
                    is_itemized = bool(definition.IsItemized)
                except Exception:
                    is_itemized = False
                fields = _schedule_fields(definition)
                cat_name = _schedule_category_name(self.doc, sched)
                rows.append(ScheduleRow(sched, name, cat_name, is_itemized, len(fields)))
            except Exception:
                continue
        self._schedule_rows = rows
        self.schedules_grid.ItemsSource = None
        self.schedules_grid.ItemsSource = rows
        self.export_summary_tb.Text = "{0} schedule(s) found.".format(len(rows))

        self.editor_schedule_cb.ItemsSource = None
        self.editor_schedule_cb.ItemsSource = [r.name for r in rows if r.is_itemized]

    def scan_schedules_click(self, sender, args):
        self._scan_schedules()

    def export_click(self, sender, args):
        selected = [r for r in self._schedule_rows if r.is_selected]
        if not selected:
            forms.alert("Check at least one Schedule to export.")
            return
        bidirectional = bool(self.mode_bidirectional_rb.IsChecked)

        dlg = SaveFileDialog()
        dlg.Filter = "Excel files (*.xlsx)|*.xlsx"
        dlg.FileName = "DeeScheduleXL_Export.xlsx"
        if dlg.ShowDialog() != DialogResult.OK:
            return

        sheets = []
        warnings = []
        with forms.ProgressBar(title="DeeScheduleXL — exporting...", cancellable=True) as pb:
            for i, row in enumerate(selected):
                if pb.cancelled:
                    break
                pb.update_progress(i, len(selected))
                try:
                    sheet_spec, warn = _build_sheet_spec(self.doc, row, bidirectional)
                    sheets.append(sheet_spec)
                    if warn:
                        warnings.append(warn)
                except Exception as e:
                    warnings.append("'{0}': FAILED to export - {1}".format(row.name, e))

        if not sheets:
            forms.alert("Nothing was exported.")
            return

        try:
            xlsx_writer.write_multisheet_xlsx(dlg.FileName, sheets)
        except Exception as e:
            forms.alert("Could not write Excel file: {0}".format(e))
            return

        msg = "Exported {0} schedule(s) to:\n{1}".format(len(sheets), dlg.FileName)
        if warnings:
            msg += "\n\nNotes:\n" + "\n".join(warnings)
        forms.alert(msg, title="DeeScheduleXL")

    # -- import tab ---------------------------------------------------------
    def browse_import_click(self, sender, args):
        dlg = OpenFileDialog()
        dlg.Filter = "Excel files (*.xlsx)|*.xlsx"
        if dlg.ShowDialog() != DialogResult.OK:
            return
        self.import_file_tb.Text = dlg.FileName
        self._analyze_import_file(dlg.FileName)

    def _analyze_import_file(self, path):
        try:
            sheets = xlsx_reader.read_xlsx_sheets(path)
        except Exception as e:
            forms.alert("Could not read file: {0}".format(e))
            return

        compatible = []
        incompatible = []
        for sheet_name, grid in sheets.items():
            if len(grid) < 2 or not grid[0]:
                incompatible.append(ImportSheetRow(sheet_name, grid, False, "Empty or malformed sheet"))
                continue
            fingerprint = grid[0][0] if grid[0] else ""
            if not fingerprint or "::" not in fingerprint:
                incompatible.append(ImportSheetRow(
                    sheet_name, grid, False, "Not a Bidirectional export (no fingerprint)"))
                continue
            unique_id, headers_joined = fingerprint.split("::", 1)
            expected_headers = headers_joined.split("|")
            schedule = None
            try:
                schedule = self.doc.GetElement(unique_id)
            except Exception:
                schedule = None
            if schedule is None or not isinstance(schedule, ViewSchedule):
                incompatible.append(ImportSheetRow(
                    sheet_name, grid, False, "Matching Schedule not found in this project"))
                continue
            current_headers = _roundtrip_headers(schedule.Definition)[0]
            if current_headers != expected_headers:
                incompatible.append(ImportSheetRow(
                    sheet_name, grid, False, "Schedule structure has changed since export"))
                continue
            row = ImportSheetRow(sheet_name, grid, True)
            row.schedule = schedule
            compatible.append(row)

        self._import_compatible = compatible
        self._import_incompatible = incompatible
        self.compatible_grid.ItemsSource = None
        self.compatible_grid.ItemsSource = compatible
        self.incompatible_grid.ItemsSource = None
        self.incompatible_grid.ItemsSource = incompatible

    def import_click(self, sender, args):
        selected = [r for r in self._import_compatible if r.is_selected]
        if not selected:
            forms.alert("Check at least one compatible sheet to import.")
            return
        if not forms.alert(
                "Import {0} sheet(s) and write edited values back onto matching elements?\n\n"
                "Continue?".format(len(selected)),
                title="DeeScheduleXL - Confirm Import", yes=True, no=True):
            return

        results = []
        t = Transaction(self.doc, "DeeScheduleXL - Import from Excel")
        t.Start()
        with forms.ProgressBar(title="DeeScheduleXL — importing...", cancellable=True) as pb:
            done = 0
            total = sum(max(0, len(r.grid) - 2) for r in selected)
            for sheet_row in selected:
                schedule = sheet_row.schedule
                _, fields = _roundtrip_headers(schedule.Definition)
                field_names = [_read_field_name(f) for f in fields]
                grid = sheet_row.grid
                for r in range(2, len(grid)):
                    if pb.cancelled:
                        break
                    done += 1
                    if done % 50 == 0 or done == total:
                        pb.update_progress(done, total)
                    data_row = grid[r]
                    if not data_row:
                        continue
                    id_text = data_row[0]
                    try:
                        eid_int = int(id_text)
                    except (ValueError, TypeError):
                        results.append((False, sheet_row.sheet_name, id_text, "Invalid Element Id"))
                        continue
                    element = self.doc.GetElement(ElementId(eid_int))
                    if element is None:
                        results.append((False, sheet_row.sheet_name, id_text, "Element no longer exists"))
                        continue

                    field_failures = []
                    updated_count = 0
                    elem_maps = _element_param_maps(element)
                    type_maps = _element_param_maps(_element_type_or_none(self.doc, element))
                    for ci, f in enumerate(fields):
                        col = ci + 1
                        if col >= len(data_row):
                            continue
                        text = data_row[col]
                        param = _resolve_field_parameter(f, field_names[ci], elem_maps, type_maps)
                        if param is None or param.IsReadOnly:
                            continue
                        try:
                            _write_param_from_text(self.doc, param, text)
                            updated_count += 1
                        except Exception as e:
                            field_failures.append("{0}: {1}".format(_read_field_name(f), e))

                    if field_failures:
                        results.append((False, sheet_row.sheet_name, id_text,
                                        "{0} field(s) updated, {1} failed - {2}".format(
                                            updated_count, len(field_failures), "; ".join(field_failures))))
                    else:
                        results.append((True, sheet_row.sheet_name, id_text,
                                        "{0} field(s) updated".format(updated_count)))
                if pb.cancelled:
                    break
        t.Commit()

        ok_count = sum(1 for ok, _, _, _ in results if ok)
        fail_count = len(results) - ok_count
        html = '<h2 style="font-family:sans-serif;color:#ddd;">DeeScheduleXL Import Results</h2>'
        html += '<p style="color:#ddd;">{0} succeeded, {1} failed.</p>'.format(ok_count, fail_count)
        for ok, sheet_name, id_text, detail in results:
            bg = "#2e7d32" if ok else "#c62828"
            icon = "&#10003;" if ok else "&#10007;"
            html += (
                '<div style="padding:4px 10px;margin:2px 0;background:{0};color:#fff;'
                'border-radius:4px;font-family:monospace;font-size:12px;">'
                '{1}&nbsp; <b>[{2}] {3}</b> &mdash; {4}</div>'.format(bg, icon, sheet_name, id_text, detail))
        output.print_html(html)

    # -- live editor tab ------------------------------------------------------
    def load_editor_click(self, sender, args):
        chosen_name = self.editor_schedule_cb.SelectedItem
        row = next((r for r in self._schedule_rows if r.name == chosen_name), None)
        if row is None:
            forms.alert("Pick a Schedule first.")
            return
        if not row.is_itemized:
            forms.alert("Only itemized Schedules can be loaded into the Live Editor "
                        "(one row per element is required).")
            return

        with forms.ProgressBar(title="DeeScheduleXL — loading editor...", cancellable=True):
            dt, fields, field_headers, locked_indices, diag = _build_editor_table(self.doc, row)

        self._editor_table = dt
        self._editor_fields = fields
        self._editor_field_headers = field_headers
        self._editor_locked_headers = set(field_headers[i] for i in locked_indices)

        self.editor_grid.ItemsSource = None
        self.editor_grid.ItemsSource = dt.DefaultView

        if diag:
            output.print_html(
                '<h3 style="color:#ddd;">DeeScheduleXL Live Editor diagnostic (first element)</h3>'
                '<pre style="color:#ddd;background:#222;padding:10px;border-radius:4px;'
                'white-space:pre-wrap;">{0}</pre>'.format(diag.replace("<", "&lt;").replace(">", "&gt;")))

    def editor_grid_auto_generating_column(self, sender, args):
        header = str(args.PropertyName)
        if header == "ElementId":
            args.Cancel = True
            return
        args.Column.Header = header
        if header in self._editor_locked_headers:
            args.Column.IsReadOnly = True

    def _paste_into_editor_grid(self):
        dt = self._editor_table
        if dt is None or not Clipboard.ContainsText():
            return
        text = Clipboard.GetText()
        if not text:
            return
        lines = text.replace("\r\n", "\n").rstrip("\n").split("\n")
        paste_rows = [line.split("\t") for line in lines]

        current_cell = self.editor_grid.CurrentCell
        if current_cell is None or current_cell.Column is None:
            forms.alert("Click a starting cell first.")
            return
        start_col_index = self.editor_grid.Columns.IndexOf(current_cell.Column)
        try:
            start_row_index = self.editor_grid.Items.IndexOf(current_cell.Item)
        except Exception:
            start_row_index = -1
        if start_col_index < 0 or start_row_index < 0:
            return

        view = dt.DefaultView
        for r, row_values in enumerate(paste_rows):
            target_row_index = start_row_index + r
            if target_row_index >= view.Count:
                break
            row_view = view[target_row_index]
            for c, val in enumerate(row_values):
                col_index = start_col_index + c
                if col_index >= self.editor_grid.Columns.Count:
                    break
                col = self.editor_grid.Columns[col_index]
                if col.IsReadOnly:
                    continue
                header = col.Header
                try:
                    row_view[header] = val
                except Exception:
                    continue
        self.editor_grid.Items.Refresh()

    def editor_grid_preview_key_down(self, sender, args):
        if args.Key == Key.V and (Keyboard.Modifiers & ModifierKeys.Control) == ModifierKeys.Control:
            self._paste_into_editor_grid()
            args.Handled = True

    def apply_editor_click(self, sender, args):
        dt = self._editor_table
        fields = self._editor_fields
        field_headers = self._editor_field_headers
        if dt is None or fields is None:
            forms.alert("Load a Schedule into the editor first.")
            return
        if not forms.alert(
                "Write the current Live Editor values back onto the model now?\n\nContinue?",
                title="DeeScheduleXL - Confirm", yes=True, no=True):
            return

        results = []
        t = Transaction(self.doc, "DeeScheduleXL - Apply Live Editor Changes")
        t.Start()
        total = dt.Rows.Count
        with forms.ProgressBar(title="DeeScheduleXL — applying...", cancellable=True) as pb:
            for i in range(total):
                if pb.cancelled:
                    break
                if i % 50 == 0 or i == total - 1:
                    pb.update_progress(i, total)
                data_row = dt.Rows[i]
                id_text = str(data_row["ElementId"])
                try:
                    eid_int = int(id_text)
                except (ValueError, TypeError):
                    results.append((False, id_text, "Invalid Element Id"))
                    continue
                element = self.doc.GetElement(ElementId(eid_int))
                if element is None:
                    results.append((False, id_text, "Element no longer exists"))
                    continue

                elem_maps = _element_param_maps(element)
                type_maps = _element_param_maps(_element_type_or_none(self.doc, element))
                field_failures = []
                updated_count = 0
                for f, header in zip(fields, field_headers):
                    try:
                        text = str(data_row[header])
                    except Exception:
                        continue
                    param = _resolve_field_parameter(f, _read_field_name(f), elem_maps, type_maps)
                    if param is None or param.IsReadOnly:
                        continue
                    try:
                        _write_param_from_text(self.doc, param, text)
                        updated_count += 1
                    except Exception as e:
                        field_failures.append("{0}: {1}".format(header, e))

                if field_failures:
                    results.append((False, id_text, "{0} updated, {1} failed - {2}".format(
                        updated_count, len(field_failures), "; ".join(field_failures))))
                else:
                    results.append((True, id_text, "{0} field(s) updated".format(updated_count)))
        t.Commit()

        ok_count = sum(1 for ok, _, _ in results if ok)
        fail_count = len(results) - ok_count
        html = '<h2 style="font-family:sans-serif;color:#ddd;">DeeScheduleXL Live Editor Results</h2>'
        html += '<p style="color:#ddd;">{0} succeeded, {1} failed.</p>'.format(ok_count, fail_count)
        for ok, id_text, detail in results:
            bg = "#2e7d32" if ok else "#c62828"
            icon = "&#10003;" if ok else "&#10007;"
            html += (
                '<div style="padding:4px 10px;margin:2px 0;background:{0};color:#fff;'
                'border-radius:4px;font-family:monospace;font-size:12px;">'
                '{1}&nbsp; <b>{2}</b> &mdash; {3}</div>'.format(bg, icon, id_text, detail))
        output.print_html(html)

    def close_click(self, sender, args):
        self.Close()


def main():
    doc = __revit__.ActiveUIDocument.Document
    window = DeeScheduleXLWindow(_XAML_FILE, doc)
    window.ShowDialog()


main()
