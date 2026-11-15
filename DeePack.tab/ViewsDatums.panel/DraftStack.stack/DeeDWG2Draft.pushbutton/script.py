# -*- coding: utf-8 -*-
"""
DeeDWG2Draft - one Drafting View per DWG file, Import or Link.
All Revit work lives in lib/dee_draft_import_service.py; this file only
wires the WPF window to it.
"""
import os

import clr
clr.AddReference("System.Windows.Forms")
from System.Windows.Forms import FolderBrowserDialog, OpenFileDialog, DialogResult

from pyrevit import forms, script
import dee_branding
import dee_ui
import dee_draft_import_service as core
import dee_telemetry
dee_telemetry.check_access("DeeDWG2Draft")

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")


class DeeDWG2DraftWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, doc):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.doc = doc
        self._rows = []
        for label, _ in core.UNIT_CHOICES:
            self.unit_cb.Items.Add(label)
        for label, _ in core.COLOR_CHOICES:
            self.color_cb.Items.Add(label)
        self.unit_cb.SelectedIndex = 0
        self.color_cb.SelectedIndex = 0

    def _refresh(self):
        self.file_grid.ItemsSource = None
        self.file_grid.ItemsSource = self._rows
        self.status_tb.Text = "{0} DWG file(s) listed - {1} checked.".format(
            len(self._rows), sum(1 for r in self._rows if r.selected))

    def _add_paths(self, paths):
        known = set(r.path.lower() for r in self._rows)
        for p in paths:
            if p.lower() not in known:
                self._rows.append(core.FileRow(p))
                known.add(p.lower())
        self._refresh()

    def add_files_click(self, sender, args):
        dlg = OpenFileDialog()
        dlg.Filter = "AutoCAD drawings (*.dwg)|*.dwg"
        dlg.Multiselect = True
        if dlg.ShowDialog() == DialogResult.OK:
            self._add_paths(list(dlg.FileNames))

    def add_folder_click(self, sender, args):
        dlg = FolderBrowserDialog()
        if dlg.ShowDialog() == DialogResult.OK:
            self._add_paths(core.list_files(dlg.SelectedPath, ".dwg", bool(self.subfolders_cb.IsChecked)))

    def select_all_click(self, sender, args):
        for r in self._rows:
            r.selected = True
        self._refresh()

    def select_none_click(self, sender, args):
        for r in self._rows:
            r.selected = False
        self._refresh()

    def remove_unchecked_click(self, sender, args):
        self._rows = [r for r in self._rows if r.selected]
        self._refresh()

    def create_click(self, sender, args):
        rows = [r for r in self._rows if r.selected]
        if not rows:
            forms.alert("Check at least one DWG file first.")
            return
        if core.drafting_view_type_id(self.doc) is None:
            forms.alert("This project has no Drafting View type.")
            return
        for r in rows:
            r.view_name = core.clean_view_name(r.view_name)
        mode = core.MODE_LINK if self.mode_cb.SelectedIndex == 1 else core.MODE_IMPORT
        unit = core.UNIT_CHOICES[max(0, self.unit_cb.SelectedIndex)][1]
        color = core.COLOR_CHOICES[max(0, self.color_cb.SelectedIndex)][1]
        try:
            scale = int(self.scale_tb.Text)
        except Exception:
            scale = 10
        when_exists = "suffix" if self.exists_cb.SelectedIndex == 1 else "skip"
        with dee_ui.SafeProgress(title="DeeDWG2Draft - creating drafting views...",
                                 cancellable=True) as pb:
            result = core.run_dwg_batch(
                self.doc, rows, mode, unit, color, bool(self.visible_cb.IsChecked),
                scale, when_exists, progress=pb)
        core.print_report(script.get_output(), "DeeDWG2Draft - DWG to Drafting Views", result)
        self.status_tb.Text = "Created {0}, skipped {1}. See the pyRevit output window for details.".format(
            len(result.created), len(result.skipped))

    def close_click(self, sender, args):
        self.Close()


uiapp = __revit__
if uiapp.ActiveUIDocument is None:
    forms.alert("Open a Revit project first.")
else:
    DeeDWG2DraftWindow(_XAML_FILE, uiapp.ActiveUIDocument.Document).ShowDialog()
