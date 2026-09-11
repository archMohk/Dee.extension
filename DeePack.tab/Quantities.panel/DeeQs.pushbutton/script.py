# -*- coding: utf-8 -*-
"""
DeeQs (Phase 1)
Scans every real model element in the project, computes a real
measured quantity per element (Area/Volume/Length/Count depending on
category - see lib/dee_qs_service.py's CATEGORY_QUANTITY_MAP), and
segregates those quantities by a chosen parameter's value (e.g. Level)
into an editable Bill of Quantities: one Section per distinct
segregation value, one Item per Category within it. Exports a styled
.xlsx via lib/xlsx_writer.py's write_boq_xlsx.

Three tabs, a linear Scan -> Generate BOQ Structure -> Preview & Export
flow (matching DeeDistributor's wizard shape, simplified since DeeQs
never writes to the Revit model at all - the whole tool is read-only
against the document, so there is no Transaction/dirty-flag concept to
carry between tabs, only a confirm-before-discard on re-Scan once a
BOQ Structure already exists).

All scan/pivot/BOQ-model logic lives in lib/dee_qs_service.py; this
file is the WPF wiring shell only, matching this stack's own
DeeRoomStamp.pushbutton/script.py shape.

Add/Rename Section and Add Item deliberately do NOT use
forms.ask_for_string (a live-Revit crash was reported the first time
this window's Add Section button was clicked - Revit froze then
closed). forms.ask_for_string opens its own modal window via
ShowDialog(), and this whole window is ALREADY modal
(window.ShowDialog() at the bottom of this file) - a second nested
ShowDialog() launched from an event handler of an already-modal
pyRevit/WPF window hosted inside Revit's own Win32 message loop is a
known-risky pattern (owner/threading edge cases in that interop layer
can hang or crash the host process outright, not just throw a
catchable exception). Since forms.alert (also modal) is used
extensively elsewhere in this codebase without any reported issue, the
suspicion is specific to ask_for_string's dialog, not modality itself
- but the safest fix that removes the risk regardless of the exact
mechanism is to never open a SECOND window at all: section_name_tb/
item_desc_tb are plain inline TextBoxes on this same window instead.

This is Phase 1 of a multi-phase build (see the project's plan file
for the full roadmap) - Advanced Columns/Grouping/3-sheet export,
Branding/Cover Page/image embedding, Numbering modes 2-3/Parameters/
Units managers/Templates, and multi-board tabs/Config Export-Import
are explicit later phases, not implemented here.
"""
import os

from pyrevit import forms, script
import dee_branding
import dee_qs_service as core
import xlsx_writer

import System
import clr
clr.AddReference("System.Windows.Forms")
from System.Windows.Forms import SaveFileDialog, DialogResult, MessageBox
import dee_telemetry
dee_telemetry.check_access("DeeQs")


output = script.get_output()

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")


def _today_string():
    try:
        return System.DateTime.Now.ToString("yyyy-MM-dd")
    except Exception:
        return ""


class CategoryChoice(object):
    def __init__(self, name, checked=True):
        self.name = name
        self.checked = checked


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


class DeeQsWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, doc, uidoc):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.doc = doc
        self.uidoc = uidoc
        self._universe = None
        self._category_choices = []
        self._scan_rows = []
        self._pivot = ([], [], {})
        self._sections = []
        self._active_section = None

        with _SafeProgress(title="DeeQs - scanning project elements...", cancellable=True) as pb:
            self._universe = core.build_element_universe(self.doc, self._progress_cb(pb))

        self._category_choices = [CategoryChoice(n, True) for n in self._universe.category_names]
        self.category_lb.ItemsSource = self._category_choices
        self.param_suggestions_lb.ItemsSource = self._universe.param_names
        self.param2_suggestions_lb.ItemsSource = self._universe.param_names
        if "Level" in self._universe.param_names:
            self.param_search_tb.Text = "Level"

        self.pivot_grid.ItemsSource = []
        self.sections_lb.ItemsSource = self._sections
        self.items_grid.ItemsSource = []
        self.date_tb.Text = _today_string()

        self.status_tb.Text = (
            "{0} model element(s) found across {1} categor(y/ies). Pick categories and a "
            "segregation parameter, then Scan.").format(
                len(self._universe.cached_elements), len(self._universe.category_names))

    def _progress_cb(self, pb):
        def cb(i, total):
            if i % 200 == 0 or i == total - 1:
                try:
                    pb.update_progress(i, total)
                except Exception:
                    pass
            return pb.cancelled
        return cb

    # ======================================================================
    # Tab 1: Scan
    # ======================================================================
    def cat_all_click(self, sender, args):
        for c in self._category_choices:
            c.checked = True
        self.category_lb.Items.Refresh()

    def cat_none_click(self, sender, args):
        for c in self._category_choices:
            c.checked = False
        self.category_lb.Items.Refresh()

    def param_search_changed(self, sender, args):
        filt = (self.param_search_tb.Text or "").lower()
        if filt:
            self.param_suggestions_lb.ItemsSource = [n for n in self._universe.param_names if filt in n.lower()]
        else:
            self.param_suggestions_lb.ItemsSource = self._universe.param_names

    def param_suggestion_selected(self, sender, args):
        picked = self.param_suggestions_lb.SelectedItem
        if picked:
            self.param_search_tb.Text = picked

    def param2_search_changed(self, sender, args):
        filt = (self.param2_search_tb.Text or "").lower()
        if filt:
            self.param2_suggestions_lb.ItemsSource = [n for n in self._universe.param_names if filt in n.lower()]
        else:
            self.param2_suggestions_lb.ItemsSource = self._universe.param_names

    def param2_suggestion_selected(self, sender, args):
        picked = self.param2_suggestions_lb.SelectedItem
        if picked:
            self.param2_search_tb.Text = picked

    def scan_click(self, sender, args):
        param_name = (self.param_search_tb.Text or "").strip()
        if not param_name:
            forms.alert("Type or pick a parameter to segregate by first.")
            return
        param_name_2 = (self.param2_search_tb.Text or "").strip() or None
        selected = set(c.name for c in self._category_choices if c.checked)
        if not selected:
            forms.alert("Check at least one category first.")
            return

        with _SafeProgress(title="DeeQs - measuring quantities...", cancellable=True) as pb:
            self._scan_rows = core.scan_for_quantities(
                self.doc, self._universe.cached_elements, param_name, selected,
                segregation_param_name_2=param_name_2, progress_cb=self._progress_cb(pb))

        categories, values, grid = core.build_pivot(self._scan_rows)
        self._pivot = (categories, values, grid)
        display_rows = core.pivot_display_rows(self.doc, categories, values, grid)
        self.pivot_grid.ItemsSource = None
        self.pivot_grid.ItemsSource = display_rows
        self.status_tb.Text = (
            "{0} element(s) measured, segregated into {1} value(s) across {2} categor(y/ies). "
            "Click 'Generate BOQ Structure' to continue.").format(
                len(self._scan_rows), len(values), len(categories))

    def generate_boq_click(self, sender, args):
        if not self._scan_rows:
            forms.alert("Scan first.")
            return
        if self._sections:
            if not forms.alert(
                    "Regenerating the BOQ Structure from the current scan will discard any manual "
                    "edits made in the BOQ Structure tab. Continue?",
                    title="DeeQs - Confirm", yes=True, no=True):
                return
        categories, values, grid = self._pivot
        self._sections = core.seed_boq_from_pivot(self.doc, categories, values, grid)
        self._active_section = None
        self._refresh_sections_list()
        self.items_grid.ItemsSource = []
        self._update_grand_total()
        self.main_tabs.SelectedIndex = 1
        self.status_tb.Text = "BOQ Structure generated: {0} section(s).".format(len(self._sections))

    # ======================================================================
    # Tab 2: BOQ Structure
    # ======================================================================
    def _refresh_sections_list(self):
        self.sections_lb.ItemsSource = None
        self.sections_lb.ItemsSource = self._sections
        if self._active_section in self._sections:
            self.sections_lb.SelectedItem = self._active_section

    def _refresh_items_grid(self):
        self.items_grid.ItemsSource = None
        self.items_grid.ItemsSource = self._active_section.items if self._active_section else []
        self._update_grand_total()

    def _update_grand_total(self):
        total = sum(s.subtotal for s in self._sections)
        self.grand_total_tb.Text = "Grand Total: {0:.2f}".format(total)

    def sections_lb_selection_changed(self, sender, args):
        self._active_section = self.sections_lb.SelectedItem
        self.section_name_tb.Text = self._active_section.title if self._active_section else ""
        self._refresh_items_grid()

    def section_add_click(self, sender, args):
        name = (self.new_section_name_tb.Text or "").strip()
        if not name:
            forms.alert("Type a new section name in the 'New section name' box first.")
            return
        section = core.BoqSection(name)
        self._sections.append(section)
        core.renumber_all(self._sections)
        self._active_section = section
        self.new_section_name_tb.Text = ""
        self._refresh_sections_list()
        self._refresh_items_grid()

    def section_rename_click(self, sender, args):
        section = self.sections_lb.SelectedItem
        if not section:
            forms.alert("Select a section first.")
            return
        name = (self.section_name_tb.Text or "").strip()
        if not name:
            forms.alert("Type the new section name in the box above first.")
            return
        section.title = name
        self._active_section = section
        self._refresh_sections_list()

    def section_delete_click(self, sender, args):
        section = self.sections_lb.SelectedItem
        if not section:
            forms.alert("Select a section first.")
            return
        if not forms.alert(
                "Delete section '{0}' and its {1} item(s)?".format(section.title, len(section.items)),
                title="DeeQs - Confirm", yes=True, no=True):
            return
        self._sections.remove(section)
        core.renumber_all(self._sections)
        self._active_section = None
        self._refresh_sections_list()
        self._refresh_items_grid()

    def item_add_click(self, sender, args):
        if not self._active_section:
            forms.alert("Select a section first.")
            return
        desc = (self.item_desc_tb.Text or "").strip()
        if not desc:
            forms.alert("Type an item description in the box above first.")
            return
        self._active_section.items.append(core.BoqItem(description=desc))
        core.renumber_all(self._sections)
        self.item_desc_tb.Text = ""
        self._refresh_items_grid()
        self._refresh_sections_list()

    def item_delete_click(self, sender, args):
        if not self._active_section:
            forms.alert("Select a section first.")
            return
        highlighted = list(self.items_grid.SelectedItems)
        if not highlighted:
            forms.alert("Click an item (Shift-click or Ctrl-click for more) to highlight rows first.")
            return
        if not forms.alert("Delete {0} item(s)?".format(len(highlighted)), title="DeeQs - Confirm", yes=True, no=True):
            return
        for it in highlighted:
            if it in self._active_section.items:
                self._active_section.items.remove(it)
        core.renumber_all(self._sections)
        self._refresh_items_grid()
        self._refresh_sections_list()

    def item_up_click(self, sender, args):
        if not self._active_section:
            return
        row = self.items_grid.SelectedItem
        if not row:
            return
        items = self._active_section.items
        idx = items.index(row)
        if idx > 0:
            items[idx - 1], items[idx] = items[idx], items[idx - 1]
            core.renumber_all(self._sections)
            self._refresh_items_grid()
            self.items_grid.SelectedItem = row

    def item_down_click(self, sender, args):
        if not self._active_section:
            return
        row = self.items_grid.SelectedItem
        if not row:
            return
        items = self._active_section.items
        idx = items.index(row)
        if idx < len(items) - 1:
            items[idx + 1], items[idx] = items[idx], items[idx + 1]
            core.renumber_all(self._sections)
            self._refresh_items_grid()
            self.items_grid.SelectedItem = row

    def items_grid_row_edit_ending(self, sender, args):
        # Same WPF constraint as DeeSheet's rn_grid_row_edit_ending -
        # CollectionView.Refresh() can't run while a row edit is still
        # committing, so recomputing Amount/Subtotal/Grand Total is
        # deferred one dispatcher cycle.
        try:
            self.Dispatcher.BeginInvoke(System.Action(self._on_item_edited))
        except Exception:
            pass

    def _on_item_edited(self):
        self.items_grid.Items.Refresh()
        self._refresh_sections_list()
        self._update_grand_total()

    # ======================================================================
    # Tab 3: Preview & Export
    # ======================================================================
    def _project_info(self):
        return {
            "project_name": self.project_name_tb.Text or "",
            "project_no": self.project_no_tb.Text or "",
            "client": self.client_tb.Text or "",
            "consultant": self.consultant_tb.Text or "",
            "date": self.date_tb.Text or "",
        }

    def _currency(self):
        return self.currency_tb.Text or "$"

    def preview_click(self, sender, args):
        if not self._sections:
            forms.alert("Generate a BOQ Structure first (Tab 1 - Scan, then Generate BOQ Structure).")
            return
        info = self._project_info()
        currency = self._currency()
        html = ['<h2 style="font-family:sans-serif;color:#ddd;">DeeQs - Bill of Quantities Preview</h2>']
        html.append(
            '<div style="font-family:sans-serif;color:#ccc;font-size:12px;margin-bottom:8px;">'
            'Project: <b>{0}</b> &nbsp; No: {1} &nbsp; Client: {2} &nbsp; Consultant: {3} &nbsp; Date: {4}'
            '</div>'.format(info["project_name"], info["project_no"], info["client"],
                             info["consultant"], info["date"]))
        grand_total = 0.0
        for section in self._sections:
            html.append(
                '<div style="background:#37474f;color:#fff;padding:5px 10px;margin-top:8px;'
                'font-family:monospace;font-weight:bold;">{0}  {1}</div>'.format(
                    section.section_no, section.title))
            html.append('<table style="width:100%;font-family:monospace;font-size:12px;color:#ddd;">')
            html.append(
                '<tr><th align="left">Item</th><th align="left">Description</th>'
                '<th align="left">Unit</th><th align="right">Qty</th>'
                '<th align="right">Rate</th><th align="right">Amount</th></tr>')
            for item in section.items:
                html.append(
                    '<tr><td>{0}</td><td>{1}</td><td>{2}</td><td align="right">{3:.2f}</td>'
                    '<td align="right">{4:.2f}</td><td align="right">{5:.2f}</td></tr>'.format(
                        item.item_no, item.description, item.unit, item.quantity, item.rate, item.amount))
            html.append('</table>')
            html.append(
                '<div style="text-align:right;font-family:monospace;color:#ddd;">'
                'Subtotal ({0}): <b>{1}</b></div>'.format(currency, section.subtotal_text))
            grand_total += section.subtotal
        html.append(
            '<hr><div style="text-align:right;font-family:sans-serif;color:#fff;font-size:14px;">'
            'Grand Total ({0}): <b>{1:.2f}</b></div>'.format(currency, grand_total))
        output.print_html("".join(html))
        self.status_tb.Text = "Preview printed to the pyRevit output window."

    def export_click(self, sender, args):
        if not self._sections:
            forms.alert("Generate a BOQ Structure first (Tab 1 - Scan, then Generate BOQ Structure).")
            return
        dlg = SaveFileDialog()
        dlg.Filter = "Excel Workbook (*.xlsx)|*.xlsx"
        dlg.FileName = "DeeQs_BOQ.xlsx"
        if dlg.ShowDialog() != DialogResult.OK:
            return
        with _SafeProgress(title="DeeQs - exporting...", indeterminate=True):
            try:
                xlsx_writer.write_boq_xlsx(dlg.FileName, self._project_info(), self._sections, self._currency())
            except Exception as e:
                forms.alert("Could not export: {0}".format(e))
                return
        self.status_tb.Text = "Exported to {0}".format(dlg.FileName)
        MessageBox.Show("Exported Bill of Quantities to:\n{0}".format(dlg.FileName), "DeeQs")

    def export_advanced_click(self, sender, args):
        if not self._sections:
            forms.alert("Generate a BOQ Structure first (Tab 1 - Scan, then Generate BOQ Structure).")
            return
        dlg = SaveFileDialog()
        dlg.Filter = "Excel Workbook (*.xlsx)|*.xlsx"
        dlg.FileName = "DeeQs_BOQ_Advanced.xlsx"
        if dlg.ShowDialog() != DialogResult.OK:
            return
        with _SafeProgress(title="DeeQs - exporting (Summary/Detailed/Data)...", indeterminate=True):
            try:
                raw_rows = core.raw_rows_for_export(self.doc, self._scan_rows)
                xlsx_writer.write_boq_advanced_xlsx(
                    dlg.FileName, self._project_info(), self._sections, self._currency(), raw_rows=raw_rows)
            except Exception as e:
                forms.alert("Could not export: {0}".format(e))
                return
        self.status_tb.Text = "Exported (Advanced) to {0}".format(dlg.FileName)
        MessageBox.Show(
            "Exported Bill of Quantities (Summary/Detailed/Data) to:\n{0}".format(dlg.FileName), "DeeQs")

    def close_click(self, sender, args):
        self.Close()


uiapp = __revit__
if uiapp.ActiveUIDocument is None:
    forms.alert("Open a Revit project first.")
else:
    window = DeeQsWindow(_XAML_FILE, uiapp.ActiveUIDocument.Document, uiapp.ActiveUIDocument)
    window.ShowDialog()
