# -*- coding: utf-8 -*-
"""
DeePDF2Draft - one Drafting View per PDF page, Import or Link, with an
optional step that trims the sheet border and title block off first.
All Revit work lives in lib/dee_draft_import_service.py; this file only
wires the WPF window to it.
"""
import os

import clr
clr.AddReference("System.Windows.Forms")
from System.Windows.Forms import FolderBrowserDialog, DialogResult
from Microsoft.Win32 import OpenFileDialog

from pyrevit import forms, script
import dee_branding
import dee_ui
import dee_draft_import_service as core
import dee_telemetry
dee_telemetry.check_access("DeePDF2Draft")

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")


def _num(text, default):
    try:
        return float(text)
    except Exception:
        return default


class DeePDF2DraftWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, doc):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.doc = doc
        self._rows = []

    def _refresh(self):
        self.file_grid.ItemsSource = None
        self.file_grid.ItemsSource = self._rows
        self.status_tb.Text = "{0} PDF file(s) listed - {1} checked.".format(
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
        dlg.Filter = "PDF files (*.pdf)|*.pdf"
        dlg.Multiselect = True
        if dlg.ShowDialog():
            self._add_paths(list(dlg.FileNames))

    def add_folder_click(self, sender, args):
        dlg = FolderBrowserDialog()
        if dlg.ShowDialog() == DialogResult.OK:
            self._add_paths(core.list_files(dlg.SelectedPath, ".pdf", bool(self.subfolders_cb.IsChecked)))

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
            forms.alert("Check at least one PDF file first.")
            return
        if core.drafting_view_type_id(self.doc) is None:
            forms.alert("This project has no Drafting View type.")
            return
        for r in rows:
            r.view_name = core.clean_view_name(r.view_name)
        mode = core.MODE_LINK if self.mode_cb.SelectedIndex == 1 else core.MODE_IMPORT
        dpi = max(36.0, min(1200.0, _num(self.dpi_tb.Text, 200.0)))
        scale = int(_num(self.scale_tb.Text, 1))
        when_exists = "suffix" if self.exists_cb.SelectedIndex == 1 else "skip"
        crop = None
        if self.crop_cb.IsChecked:
            crop = (_num(self.crop_left_tb.Text, 0), _num(self.crop_top_tb.Text, 0),
                    _num(self.crop_right_tb.Text, 0), _num(self.crop_bottom_tb.Text, 0))
            if core.find_pymupdf_python() is None:
                forms.alert("Cutting off the title block needs Python with PyMuPDF, and none "
                            "was found.\n\nInstall it once from a terminal:\n    py -m pip install pymupdf\n\n"
                            "Or untick 'Remove border and title block' to place the full pages.")
                return
        with dee_ui.SafeProgress(title="DeePDF2Draft - creating drafting views...",
                                 cancellable=True) as pb:
            result = core.run_pdf_batch(
                self.doc, rows, mode, dpi, bool(self.allpages_cb.IsChecked), scale,
                when_exists, crop=crop, progress=pb)
        core.print_report(script.get_output(), "DeePDF2Draft - PDF to Drafting Views", result)
        self.status_tb.Text = "Created {0}, skipped {1}. See the pyRevit output window for details.".format(
            len(result.created), len(result.skipped))

    def close_click(self, sender, args):
        self.Close()


uiapp = __revit__
if uiapp.ActiveUIDocument is None:
    forms.alert("Open a Revit project first.")
else:
    DeePDF2DraftWindow(_XAML_FILE, uiapp.ActiveUIDocument.Document).ShowDialog()
