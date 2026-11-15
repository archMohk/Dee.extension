# -*- coding: utf-8 -*-
"""
DeePDF2Draft - one Drafting View per PDF page, Import or Link, with an
optional step that trims the sheet border and title block off first.
All Revit work lives in lib/dee_draft_import_service.py; this file only
wires the WPF window to it.
"""
import os
import tempfile

import clr
clr.AddReference("System.Windows.Forms")
from System import Uri
from System.Windows.Controls import DataGridComboBoxColumn, DataGridLength, DataGridLengthUnitType
from System.Windows.Data import Binding, BindingMode
from System.Windows.Media.Imaging import BitmapImage, BitmapCacheOption
from System.Windows.Forms import FolderBrowserDialog, OpenFileDialog, DialogResult

from pyrevit import forms, script
import dee_branding
import dee_ui
import dee_draft_import_service as core
import dee_pdf_vector_service as vector
import dee_telemetry
dee_telemetry.check_access("DeePDF2Draft")

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")


def _num(text, default):
    try:
        return float(text)
    except Exception:
        return default


class WeightRow(object):
    def __init__(self, bucket, mm, count, style):
        self.bucket = bucket
        self.mm = mm
        self.count = count
        self.style = style


class DeePDF2DraftWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, doc):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.doc = doc
        self._rows = []
        self._weights = []
        col = DataGridComboBoxColumn()
        col.Header = "Revit line style"
        col.ItemsSource = vector.project_style_labels(doc)
        b = Binding("style")
        b.Mode = BindingMode.TwoWay
        col.SelectedItemBinding = b
        col.Width = DataGridLength(1, DataGridLengthUnitType.Star)
        self.weight_grid.Columns.Add(col)
        self._ready = True

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
        if dlg.ShowDialog() == DialogResult.OK:
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

    # ---- orientation preview -------------------------------------------
    def _turn(self):
        return (0, 90, 180, 270)[max(0, self.rot_cb.SelectedIndex)], bool(self.mirror_cb.IsChecked)

    def _margins(self):
        if self.crop_cb.IsChecked:
            return (_num(self.crop_left_tb.Text, 0), _num(self.crop_top_tb.Text, 0),
                    _num(self.crop_right_tb.Text, 0), _num(self.crop_bottom_tb.Text, 0))
        return (0, 0, 0, 0)

    def _show_preview(self):
        row = self.file_grid.SelectedItem
        if row is None:
            self.preview_tb.Text = "Click a file in the list to see it."
            return
        if core.find_pymupdf_python() is None:
            self.preview_tb.Text = ("Preview needs PyMuPDF (py -m pip install pymupdf). "
                                    "Press Create once and it will offer to install it.")
            return
        rot, mirror = self._turn()
        png = os.path.join(tempfile.gettempdir(), "dee_pdf_preview.png")
        ok, err = vector.render_preview(row.path, 0, self._margins(), rot, mirror, png, 700)
        if not ok:
            self.preview_tb.Text = "Preview failed: {0}".format(err)
            return
        bi = BitmapImage()
        bi.BeginInit()
        bi.CacheOption = BitmapCacheOption.OnLoad
        bi.UriSource = Uri(png)
        bi.EndInit()
        bi.Freeze()
        self.preview_img.Source = bi
        self.preview_tb.Text = "{0} (page 1). Turn {1}{2}.".format(
            row.file_name, rot, ", mirrored" if mirror else "")

    def _scan(self):
        row = self.file_grid.SelectedItem or next((r for r in self._rows if r.selected), None)
        if row is None:
            self.preview_tb.Text = "Click a file in the list first."
            return
        rows, err = vector.scan_weights(self.doc, row.path, self._margins(), *self._turn())
        if rows is None:
            self.preview_tb.Text = "Weight scan failed: {0}".format(err)
            return
        self._weights = [WeightRow(*r) for r in rows]
        self.weight_grid.ItemsSource = None
        self.weight_grid.ItemsSource = self._weights

    def scan_click(self, sender, args):
        self._scan()

    def file_selected(self, sender, args):
        try:
            self._show_preview()
            if not self._weights and core.find_pymupdf_python() is not None:
                self._scan()
        except Exception as ex:
            self.preview_tb.Text = "Preview failed: {0}".format(ex)

    def preview_changed(self, sender, args):
        if getattr(self, "_ready", False):
            self.file_selected(sender, args)

    def refresh_preview_click(self, sender, args):
        self.file_selected(sender, args)

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
        as_lines = self.convert_cb.SelectedIndex == 0
        rot, mirror = self._turn()
        scale_text = (self.scale_n_cb.Text or "").strip().lower()
        scale_n = 0 if scale_text in ("", "auto") else max(1, int(_num(scale_text, 0)))
        if not as_lines and (rot or mirror):
            forms.alert("Turning and mirroring apply to 'Lines and fills'. "
                        "Picture mode places the page as drawn.")
        crop = None
        if self.crop_cb.IsChecked:
            crop = (_num(self.crop_left_tb.Text, 0), _num(self.crop_top_tb.Text, 0),
                    _num(self.crop_right_tb.Text, 0), _num(self.crop_bottom_tb.Text, 0))
        if (as_lines or crop is not None) and core.find_pymupdf_python() is None:
            if not forms.alert("Reading the PDF drawing (and cutting off the title block) needs "
                               "PyMuPDF, a free Python library, and it is not installed.\n\n"
                               "Install it now? (runs: py -m pip install --user pymupdf)",
                               yes=True, no=True):
                return
            with dee_ui.SafeProgress(title="Installing PyMuPDF...", indeterminate=True):
                ok, msg = core.install_pymupdf()
            if not ok:
                forms.alert("PyMuPDF could not be installed:\n\n{0}".format(msg))
                return
        with dee_ui.SafeProgress(title="DeePDF2Draft - creating drafting views...",
                                 cancellable=True) as pb:
            if as_lines:
                result = vector.run_batch(
                    self.doc, rows, crop or (0, 0, 0, 0), bool(self.allpages_cb.IsChecked),
                    scale_n, max(0.0, _num(self.min_mm_tb.Text, 0.0)),
                    bool(self.colors_cb.IsChecked), bool(self.fills_cb.IsChecked),
                    when_exists, rot=rot, mirror=mirror,
                    hatch=bool(self.hatch_cb.IsChecked),
                    weight_map=dict((w.bucket, w.style) for w in self._weights) or None,
                    progress=pb)
            else:
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
