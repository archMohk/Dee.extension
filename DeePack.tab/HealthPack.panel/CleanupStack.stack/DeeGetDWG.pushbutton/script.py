# -*- coding: utf-8 -*-
"""
DeeGetDWG - finds CAD Import/Link instances placed far from the
Internal Origin and lets you Unpin + move the checked ones back to
(0,0,0). All scan/move logic lives in lib/dee_getdwg_service.py; this
file only wires the WPF window to it.
"""
import os

from pyrevit import forms
import dee_branding

import dee_getdwg_service as core

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")


class DeeGetDWGWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, doc):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.doc = doc
        self._rows = []
        self.threshold_unit_tb.Text = core.unit_abbreviation(doc)
        self.scan_click(None, None)

    # ---------------- scan ----------------
    def scan_click(self, sender, args):
        self._rows = core.scan(self.doc)
        self.apply_threshold_click(None, None)

    def _refresh_grid(self):
        self.cad_grid.ItemsSource = None
        self.cad_grid.ItemsSource = self._rows
        checked_count = sum(1 for r in self._rows if r.selected)
        self.status_tb.Text = "{0} CAD import/link(s) found - {1} checked.".format(
            len(self._rows), checked_count)

    def apply_threshold_click(self, sender, args):
        threshold_display = core.safe_float(self.threshold_tb.Text, core.DEFAULT_THRESHOLD_DISPLAY)
        threshold_internal = core.display_to_internal(self.doc, threshold_display)
        for row in self._rows:
            row.selected = row.distance_internal > threshold_internal
        self._refresh_grid()

    # ---------------- selection ----------------
    def select_all_click(self, sender, args):
        for row in self._rows:
            row.selected = True
        self.cad_grid.Items.Refresh()

    def select_none_click(self, sender, args):
        for row in self._rows:
            row.selected = False
        self.cad_grid.Items.Refresh()

    # ---------------- apply ----------------
    def move_selected_click(self, sender, args):
        selected = [r for r in self._rows if r.selected]
        if not selected:
            forms.alert("Check at least one CAD import/link first.")
            return
        result = core.move_to_origin(self.doc, selected)
        core.print_report(result)
        self._refresh_grid()
        self.status_tb.Text = "Moved {0}, skipped {1}. See the pyRevit output window for details.".format(
            result.moved_count, len(result.skipped))

    def close_click(self, sender, args):
        self.Close()


uiapp = __revit__
if uiapp.ActiveUIDocument is None:
    forms.alert("Open a Revit project first.")
else:
    window = DeeGetDWGWindow(_XAML_FILE, uiapp.ActiveUIDocument.Document)
    window.ShowDialog()
