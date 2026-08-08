# -*- coding: utf-8 -*-
"""
DeeRoomXYD - scans every Room visible in the active view and reports
the longest boundary edge along the X axis and along the Y axis, with
an option to place real Dimension elements for those edges in the
active view. All scan/dimension logic lives in
lib/dee_room_xyd_service.py; this file only wires the WPF window to it.
"""
import os

import clr
clr.AddReference("System.Windows.Forms")
from System.Windows.Forms import SaveFileDialog, DialogResult, MessageBox

from pyrevit import forms

import dee_room_xyd_service as core

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")


class DeeRoomXYDWindow(forms.WPFWindow):
    def __init__(self, xaml_file, doc, view):
        forms.WPFWindow.__init__(self, xaml_file)
        self.doc = doc
        self.view = view
        self._rows = []
        self.scan_click(None, None)

    def scan_click(self, sender, args):
        if not core.is_view_supported(self.view):
            self._rows = []
            self.rooms_grid.ItemsSource = None
            self.status_tb.Text = "Active view is not a Floor Plan / Ceiling Plan / Area Plan - switch views and click Rescan."
            return
        with forms.ProgressBar(title="DeeRoomXYD - scanning active view...", indeterminate=True):
            self._rows = core.scan(self.doc, self.view)
        self.rooms_grid.ItemsSource = None
        self.rooms_grid.ItemsSource = self._rows
        self.status_tb.Text = "{0} room(s) found in the active view.".format(len(self._rows))

    def export_click(self, sender, args):
        if not self._rows:
            forms.alert("Nothing to export yet - click Rescan Active View first.")
            return
        dlg = SaveFileDialog()
        dlg.Filter = "Excel Workbook (*.xlsx)|*.xlsx"
        dlg.FileName = "DeeRoomXYD_Report.xlsx"
        if dlg.ShowDialog() != DialogResult.OK:
            return
        try:
            core.export_report(dlg.FileName, self._rows)
        except Exception as e:
            forms.alert("Could not export report: {0}".format(e))
            return
        MessageBox.Show("Report exported to:\n{0}".format(dlg.FileName), "DeeRoomXYD")

    # ---------------- selection ----------------
    def select_all_click(self, sender, args):
        for row in self._rows:
            row.selected = True
        self.rooms_grid.Items.Refresh()

    def select_none_click(self, sender, args):
        for row in self._rows:
            row.selected = False
        self.rooms_grid.Items.Refresh()

    # ---------------- dimensions ----------------
    def place_dimensions_click(self, sender, args):
        if not core.is_view_supported(self.view):
            forms.alert("Dimensions can only be placed in a Floor Plan, Ceiling Plan, or Area Plan view.")
            return
        selected = [r for r in self._rows if r.selected]
        if not selected:
            forms.alert("Check at least one room first.")
            return
        result = core.place_dimensions(self.doc, self.view, selected)
        core.print_dimension_report(result)
        self.status_tb.Text = "Placed {0} dimension(s), skipped {1}. See the pyRevit output window for details.".format(
            result.placed_count, len(result.skipped))

    def close_click(self, sender, args):
        self.Close()


uiapp = __revit__
if uiapp.ActiveUIDocument is None:
    forms.alert("Open a Revit project first.")
else:
    window = DeeRoomXYDWindow(_XAML_FILE, uiapp.ActiveUIDocument.Document, uiapp.ActiveUIDocument.ActiveView)
    window.ShowDialog()
