# -*- coding: utf-8 -*-
"""
DeeRoomXYD - scans every Room and reports the longest boundary edge
along the X axis and along the Y axis. Read-only. All scan/export
logic lives in lib/dee_room_xyd_service.py; this file only wires the
WPF window to it.
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
    def __init__(self, xaml_file, doc):
        forms.WPFWindow.__init__(self, xaml_file)
        self.doc = doc
        self._rows = []
        self.scan_click(None, None)

    def scan_click(self, sender, args):
        with forms.ProgressBar(title="DeeRoomXYD - scanning rooms...", indeterminate=True):
            self._rows = core.scan(self.doc)
        self.rooms_grid.ItemsSource = None
        self.rooms_grid.ItemsSource = self._rows
        self.status_tb.Text = "{0} room(s) scanned.".format(len(self._rows))

    def export_click(self, sender, args):
        if not self._rows:
            forms.alert("Nothing to export yet - click Rescan first.")
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

    def close_click(self, sender, args):
        self.Close()


uiapp = __revit__
if uiapp.ActiveUIDocument is None:
    forms.alert("Open a Revit project first.")
else:
    window = DeeRoomXYDWindow(_XAML_FILE, uiapp.ActiveUIDocument.Document)
    window.ShowDialog()
