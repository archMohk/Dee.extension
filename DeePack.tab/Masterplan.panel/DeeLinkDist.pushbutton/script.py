# -*- coding: utf-8 -*-
"""
DeeLinkDist (Masterplan)
Distributes Revit links across a masterplan from an Excel sheet: pick
one already-loaded link per Building Typology, and one NEW instance of
that link is created for every matching row, positioned at that row's
X/Y/Z and rotated by its own angle - see lib/dee_link_dist_service.py
for the Revit API facts this relies on (this is the first tool in this
codebase to CREATE a link instance via the API, rather than only
reading or moving one a user already placed).

--------------------------------------------------------------------
Two-page wizard, same shape as DeeW.Transmit's TabControl window
--------------------------------------------------------------------
Page 1 downloads an empty template (native SaveFileDialog) and loads a
filled one back (native OpenFileDialog) - both native common dialogs
opened from inside this already-modal window, the same safe pattern
ColorDialog already uses throughout this codebase (see
feedback-no-nested-modal-ask-for-string.md for the actual boundary:
never a SECOND WPF window or Selection.PickObject from inside this
one - a native dialog is a different, proven-safe thing). Parsed rows
populate a read-only DataGrid; any row that failed to parse (missing
X/Y/Z, blank typology) is listed underneath rather than silently
dropped.

Page 2's per-typology link picker is built in CODE, not hand-written
XAML rows - one ComboBox per DISTINCT typology found in the loaded
sheet, mirroring dee_mono's own "one entry per role, built in
_build_preview_cells" pattern rather than a fixed row count baked into
the XAML. "Place Links" reads every ComboBox's current pick, resolves
it back to the actual RevitLinkType, and hands the whole batch to
dee_link_dist_service.place_links in one call.

--------------------------------------------------------------------
X/Y/Z unit and shared parameters
--------------------------------------------------------------------
A ComboBox on page 1 (unit_cb, defaulting to Meters) picks which of
dee_link_dist_service.UNIT_OPTIONS the sheet's X/Y/Z values are in -
the template's own column headers are unit-agnostic ("X", not "X (m)")
precisely so the SAME downloaded template works whichever unit is
chosen, rather than needing a different template per unit.

Three text boxes on page 2 (param_typology_tb/param_parcel_tb/
param_developer_tb, pre-filled with the same default names the service
module itself falls back to) let the user rename the Shared Parameters
written onto every placed link instance - read at Place time via
_selected_param_names() and handed straight to place_links'
param_names argument. Creating/binding those parameters (to the Revit
Links category) is entirely dee_link_dist_service.ensure_link_
parameters' job, called automatically from inside place_links - this
window never touches the Revit API for that itself.
"""
import os
import traceback

import clr
clr.AddReference("System.Windows.Forms")
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
from System.Windows.Forms import OpenFileDialog, SaveFileDialog, DialogResult
from System.Windows import Thickness, VerticalAlignment
from System.Windows.Controls import StackPanel, TextBlock, ComboBox, Orientation

from pyrevit import forms, script

import dee_branding
import dee_link_dist_service as core

output = script.get_output()

_TOOL = "DeeLinkDist"
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")
_NOT_MAPPED = "(not mapped)"


class PlacementReportRow(object):
    """Flat view-model for report_grid - deliberately not binding the
    DataGrid straight to core.RowResult (which nests a BuildingRow
    inside it): a flat object with top-level attributes is the proven
    binding shape already used everywhere else in this codebase, rather
    than relying on an untested nested-property binding path."""
    def __init__(self, row_result):
        self.row_number = row_result.row.row_number
        self.typology = row_result.row.typology
        self.result = "Placed" if row_result.ok else "Skipped"
        self.message = row_result.message


class DeeLinkDistWindow(dee_branding.DeeBrandedWindow):
    _ready = False

    def __init__(self, xaml_file, doc):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.doc = doc
        self.rows = []
        self._mapping_combos = {}

        for label, _unit_type_id in core.UNIT_OPTIONS:
            self.unit_cb.Items.Add(label)
        self.unit_cb.SelectedIndex = [l for l, _u in core.UNIT_OPTIONS].index(
            core.DEFAULT_UNIT_LABEL)

        self._refresh_link_types()
        self._ready = True

    def _selected_unit_label(self):
        i = self.unit_cb.SelectedIndex
        options = core.UNIT_OPTIONS
        if 0 <= i < len(options):
            return options[i][0]
        return core.DEFAULT_UNIT_LABEL

    def _selected_param_names(self):
        return {
            "typology": (self.param_typology_tb.Text or "").strip() or "Building Typology",
            "parcel_id": (self.param_parcel_tb.Text or "").strip() or "Parcel ID",
            "developer_id": (self.param_developer_tb.Text or "").strip() or "Developer ID",
        }

    def _guard(self, fn, *args):
        try:
            return fn(*args)
        except Exception as e:
            self.status_tb.Text = "ERROR: {0}".format(e)
            forms.alert("DeeLinkDist hit an error:\n\n{0}\n\n{1}".format(
                e, traceback.format_exc()[-900:]), title=_TOOL)

    # ---------------- link list ----------------
    def _refresh_link_types(self):
        self._link_types = core.list_link_types(self.doc)
        self._link_types_by_name = {}
        self._link_names = []
        for lt in self._link_types:
            name = core.link_type_display_name(lt)
            self._link_types_by_name[name] = lt
            self._link_names.append(name)
        self._build_mapping_rows()

    def refresh_links_click(self, sender, args):
        self._guard(self._refresh_link_types)

    # ---------------- page 1: excel ----------------
    def download_template_click(self, sender, args):
        def run():
            dlg = SaveFileDialog()
            dlg.Filter = "Excel Workbook (*.xlsx)|*.xlsx"
            dlg.FileName = "DeeLinkDist_Template.xlsx"
            if dlg.ShowDialog() != DialogResult.OK:
                return
            core.write_template(dlg.FileName)
            self.status_tb.Text = "Empty template saved to {0}.".format(dlg.FileName)
            forms.alert("Empty template saved to:\n{0}".format(dlg.FileName), title=_TOOL)
        self._guard(run)

    def load_excel_click(self, sender, args):
        def run():
            dlg = OpenFileDialog()
            dlg.Filter = "Excel Workbook (*.xlsx)|*.xlsx"
            if dlg.ShowDialog() != DialogResult.OK:
                return
            with forms.ProgressBar(title="DeeLinkDist - reading Excel...", indeterminate=True):
                rows, errors = core.read_building_rows(dlg.FileName)
            self.rows = rows
            self.rows_grid.ItemsSource = None
            self.rows_grid.ItemsSource = self.rows
            self.row_errors_tb.Text = "\n".join(errors) if errors else "No issues."
            self.excel_status_tb.Text = "{0} row(s) loaded, {1} skipped.".format(
                len(rows), len(errors))
            self.status_tb.Text = self.excel_status_tb.Text
            self._build_mapping_rows()
        self._guard(run)

    # ---------------- page 2: mapping ----------------
    def _build_mapping_rows(self):
        self.mapping_panel.Children.Clear()
        self._mapping_combos = {}
        for typology in core.distinct_typologies(self.rows):
            row_panel = StackPanel()
            row_panel.Orientation = Orientation.Horizontal
            row_panel.Margin = Thickness(0, 0, 0, 6)

            label = TextBlock()
            label.Text = typology
            label.Width = 220
            label.VerticalAlignment = VerticalAlignment.Center
            row_panel.Children.Add(label)

            combo = ComboBox()
            combo.Width = 300
            combo.Height = 24
            combo.Items.Add(_NOT_MAPPED)
            for name in self._link_names:
                combo.Items.Add(name)
            combo.SelectedIndex = 0
            row_panel.Children.Add(combo)

            self._mapping_combos[typology] = combo
            self.mapping_panel.Children.Add(row_panel)

    def place_links_click(self, sender, args):
        def run():
            if not self.rows:
                forms.alert("Load a filled Excel file first (page 1).", title=_TOOL)
                return
            mapping = {}
            for typology, combo in self._mapping_combos.items():
                name = combo.SelectedItem
                if name and name != _NOT_MAPPED:
                    link_type = self._link_types_by_name.get(name)
                    if link_type is not None:
                        mapping[typology.strip().lower()] = link_type
            if not mapping:
                forms.alert("Map at least one Building Typology to a link first.", title=_TOOL)
                return

            with forms.ProgressBar(title="DeeLinkDist - placing links...", indeterminate=True):
                result = core.place_links(
                    self.doc, self.rows, mapping,
                    unit_label=self._selected_unit_label(),
                    param_names=self._selected_param_names())

            report_rows = [PlacementReportRow(r) for r in result.row_results]
            self.report_grid.ItemsSource = None
            self.report_grid.ItemsSource = report_rows

            self.status_tb.Text = "Placed {0}, skipped {1}, out of {2} row(s).".format(
                result.applied, result.skipped, len(self.rows))
            self._report(result)
        self._guard(run)

    def _report(self, result):
        html = '<h2 style="font-family:sans-serif;">DeeLinkDist</h2>'
        html += ('<div style="font-family:sans-serif;font-size:12px;">'
                 '<b>X/Y/Z unit:</b> {0}'.format(self._selected_unit_label()))
        if result.parameter_setup:
            parts = []
            for name, (ok, detail) in sorted(result.parameter_setup.items()):
                parts.append("{0}: {1}".format(name, detail if ok else "FAILED - " + detail))
            html += "<br><b>Link parameters:</b> " + "; ".join(parts)
        html += "</div>"
        bg = "#2e7d32" if not result.errors else "#8d6e19"
        html += ('<div style="margin-top:8px;padding:7px 11px;background:{0};color:#fff;'
                 'border-radius:4px;font-family:monospace;font-size:12px;">'
                 '{1} link(s) placed, {2} skipped, out of {3} row(s).</div>'.format(
                     bg, result.applied, result.skipped, len(self.rows)))
        if result.errors:
            html += '<div style="font-family:sans-serif;font-size:11px;color:#a55;margin-top:6px;">'
            for e in result.errors[:12]:
                html += "&bull; {0}<br>".format(e)
            html += "</div>"
        skipped = [r for r in result.row_results if not r.ok]
        if skipped:
            html += ('<div style="font-family:sans-serif;font-size:11px;color:#a55;'
                     'margin-top:6px;">')
            for r in skipped[:20]:
                html += "&bull; Row {0} ({1}): {2}<br>".format(
                    r.row.row_number, r.row.typology, r.message)
            html += "</div>"
        output.print_html(html)

    def close_click(self, sender, args):
        self.Close()


def main():
    uiapp = __revit__
    uidoc = uiapp.ActiveUIDocument
    if uidoc is None:
        forms.alert("Open a Revit project first.", title=_TOOL)
        return
    doc = uidoc.Document
    if doc.IsFamilyDocument:
        forms.alert("DeeLinkDist works on a project document. It cannot be run inside the "
                    "Family Editor.", title=_TOOL)
        return

    window = DeeLinkDistWindow(_XAML_FILE, doc)
    window.ShowDialog()


main()
