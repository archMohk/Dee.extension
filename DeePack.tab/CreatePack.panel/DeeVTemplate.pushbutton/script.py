# -*- coding: utf-8 -*-
"""
DeeV.Template (CreatePack)
Scans the View Templates in the current file and lets you push any/all
of them into any/all of the other Revit documents currently open in the
same Revit session (linked models excluded), via a cross-document
ElementTransformUtils.CopyElements call - the same mechanism Revit's
own copy/paste between projects uses.

If a View Template with the same name already exists in a target file,
"Skip Existing" leaves it untouched and "Overwrite Existing" deletes it
in the target first, then copies the source version in as a plain
replacement (no merge of individual settings).

Needs live-Revit verification: ElementTransformUtils.CopyElements's
exact signature/behavior for View elements specifically (View Templates
are real View elements with IsTemplate=True, not a separate "type"), and
Document identity comparison for excluding the source document itself
from the target list.
"""
import os

from pyrevit import forms, script
from Autodesk.Revit.DB import (
    FilteredElementCollector, View, ElementId, Transaction,
    ElementTransformUtils, CopyPasteOptions, Transform,
)

from System.Collections.Generic import List

output = script.get_output()

_XAML_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.xaml")


# --------------------------------------------------------------------------
# Defensive reads
# --------------------------------------------------------------------------
def _read_name(element):
    if element is None:
        return None
    try:
        n = element.Name
        if n:
            return n
    except Exception:
        return None
    return None


def _view_type_text(view):
    try:
        return str(view.ViewType)
    except Exception:
        return "(unknown)"


# --------------------------------------------------------------------------
# Scanning
# --------------------------------------------------------------------------
def _scan_view_templates(doc):
    rows = []
    for v in FilteredElementCollector(doc).OfClass(View):
        try:
            if v.IsTemplate:
                rows.append(TemplateRow(v))
        except Exception:
            continue
    return rows


def _other_open_documents(doc):
    app = doc.Application
    others = []
    for d in app.Documents:
        try:
            if d.Equals(doc):
                continue
            if d.IsLinked:
                continue
            others.append(DocRow(d))
        except Exception:
            continue
    return others


def _existing_template_by_name(dest_doc, name):
    for v in FilteredElementCollector(dest_doc).OfClass(View):
        try:
            if v.IsTemplate and _read_name(v) == name:
                return v
        except Exception:
            continue
    return None


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------
class TemplateRow(object):
    def __init__(self, view):
        self.view = view
        self.selected = False
        self.name = _read_name(view) or "(unnamed)"
        self.view_type = _view_type_text(view)


class DocRow(object):
    def __init__(self, document):
        self.document = document
        self.selected = False
        try:
            self.title = document.Title
        except Exception:
            self.title = "(unknown document)"


# --------------------------------------------------------------------------
# Transfer
# --------------------------------------------------------------------------
def _transfer_to_document(source_doc, dest_doc_row, template_rows, dup_mode):
    dest_doc = dest_doc_row.document
    results = []
    to_copy = []

    for row in template_rows:
        existing = _existing_template_by_name(dest_doc, row.name)
        if existing is not None:
            if dup_mode == "Skip Existing":
                results.append((None, row.name, dest_doc_row.title,
                                 "Skipped - already exists in '{0}'".format(dest_doc_row.title)))
                continue
            try:
                dest_doc.Delete(existing.Id)
            except Exception as e:
                results.append((False, row.name, dest_doc_row.title,
                                 "FAILED deleting existing template: {0}".format(e)))
                continue
        to_copy.append(row)

    if not to_copy:
        return results

    options = CopyPasteOptions()
    t = Transaction(dest_doc, "DeeV.Template - Transfer View Templates")
    t.Start()
    try:
        ElementTransformUtils.CopyElements(
            source_doc, List[ElementId]([r.view.Id for r in to_copy]),
            dest_doc, Transform.Identity, options)
        t.Commit()
        for r in to_copy:
            results.append((True, r.name, dest_doc_row.title, "Copied to '{0}'".format(dest_doc_row.title)))
    except Exception as e:
        t.RollBack()
        for r in to_copy:
            results.append((False, r.name, dest_doc_row.title, "FAILED: {0}".format(e)))
    return results


# --------------------------------------------------------------------------
# Window
# --------------------------------------------------------------------------
class DeeVTemplateWindow(forms.WPFWindow):
    def __init__(self, xaml_file, doc):
        forms.WPFWindow.__init__(self, xaml_file)
        self.doc = doc
        self._template_rows = []
        self._doc_rows = []

        self.duplicate_mode_cb.ItemsSource = ["Skip Existing", "Overwrite Existing"]
        self.duplicate_mode_cb.SelectedIndex = 0

        self._scan_templates()
        self._refresh_docs()

    # ---- View Templates ----
    def scan_templates_click(self, sender, args):
        self._scan_templates()

    def _scan_templates(self):
        self._template_rows = _scan_view_templates(self.doc)
        self.templates_grid.ItemsSource = None
        self.templates_grid.ItemsSource = self._template_rows
        self.templates_count_tb.Text = "{0} view template(s) in this file".format(len(self._template_rows))

    def templates_select_all_click(self, sender, args):
        for r in self._template_rows:
            r.selected = True
        self._refresh_grid(self.templates_grid, self._template_rows)

    def templates_deselect_all_click(self, sender, args):
        for r in self._template_rows:
            r.selected = False
        self._refresh_grid(self.templates_grid, self._template_rows)

    def templates_select_highlighted_click(self, sender, args):
        highlighted = list(self.templates_grid.SelectedItems)
        if not highlighted:
            forms.alert("Click a row (Shift-click or Ctrl-click for more) to highlight rows first.")
            return
        for r in highlighted:
            r.selected = True
        self._refresh_grid(self.templates_grid, self._template_rows)

    def templates_deselect_highlighted_click(self, sender, args):
        highlighted = list(self.templates_grid.SelectedItems)
        if not highlighted:
            forms.alert("Click a row (Shift-click or Ctrl-click for more) to highlight rows first.")
            return
        for r in highlighted:
            r.selected = False
        self._refresh_grid(self.templates_grid, self._template_rows)

    # ---- Open documents ----
    def refresh_docs_click(self, sender, args):
        self._refresh_docs()

    def _refresh_docs(self):
        self._doc_rows = _other_open_documents(self.doc)
        self.docs_grid.ItemsSource = None
        self.docs_grid.ItemsSource = self._doc_rows
        self.docs_count_tb.Text = "{0} other open file(s)".format(len(self._doc_rows))

    def docs_select_all_click(self, sender, args):
        for r in self._doc_rows:
            r.selected = True
        self._refresh_grid(self.docs_grid, self._doc_rows)

    def docs_deselect_all_click(self, sender, args):
        for r in self._doc_rows:
            r.selected = False
        self._refresh_grid(self.docs_grid, self._doc_rows)

    def docs_select_highlighted_click(self, sender, args):
        highlighted = list(self.docs_grid.SelectedItems)
        if not highlighted:
            forms.alert("Click a row (Shift-click or Ctrl-click for more) to highlight rows first.")
            return
        for r in highlighted:
            r.selected = True
        self._refresh_grid(self.docs_grid, self._doc_rows)

    def docs_deselect_highlighted_click(self, sender, args):
        highlighted = list(self.docs_grid.SelectedItems)
        if not highlighted:
            forms.alert("Click a row (Shift-click or Ctrl-click for more) to highlight rows first.")
            return
        for r in highlighted:
            r.selected = False
        self._refresh_grid(self.docs_grid, self._doc_rows)

    def _refresh_grid(self, grid, rows):
        grid.ItemsSource = None
        grid.ItemsSource = rows

    # ---- Transfer ----
    def transfer_click(self, sender, args):
        templates = [r for r in self._template_rows if r.selected]
        targets = [r for r in self._doc_rows if r.selected]
        if not templates:
            forms.alert("Check at least one View Template to transfer.")
            return
        if not targets:
            forms.alert("Check at least one target file (or Refresh Open Files if none are listed).")
            return

        if not forms.alert(
                "Transfer {0} View Template(s) to {1} target file(s)?".format(len(templates), len(targets)),
                title="DeeV.Template - Confirm", yes=True, no=True):
            return

        dup_mode = self.duplicate_mode_cb.SelectedItem or "Skip Existing"
        all_results = []
        with forms.ProgressBar(title="DeeV.Template — transferring...", cancellable=True) as pb:
            total = len(targets)
            for i, dest_row in enumerate(targets):
                if pb.cancelled:
                    break
                pb.update_progress(i, total)
                try:
                    all_results.extend(_transfer_to_document(self.doc, dest_row, templates, dup_mode))
                except Exception as e:
                    all_results.append((False, "-", dest_row.title, "FAILED: {0}".format(e)))

        self._report(all_results)

    def _report(self, results):
        ok_count = sum(1 for ok, _, _, _ in results if ok is True)
        skip_count = sum(1 for ok, _, _, _ in results if ok is None)
        fail_count = sum(1 for ok, _, _, _ in results if ok is False)
        html = ['<h2 style="font-family:sans-serif;color:#ddd;">DeeV.Template - Transfer Results</h2>',
                '<p style="color:#ddd;">{0} copied, {1} skipped, {2} failed.</p>'.format(
                    ok_count, skip_count, fail_count)]
        for ok, name, target, detail in results:
            bg = "#2e7d32" if ok is True else ("#455a64" if ok is None else "#c62828")
            icon = "&#10003;" if ok is True else ("&#9888;" if ok is None else "&#10007;")
            html.append(
                '<div style="padding:4px 10px;margin:2px 0;background:{0};color:#fff;'
                'border-radius:4px;font-family:monospace;font-size:12px;">'
                '{1}&nbsp; <b>{2}</b> &rarr; {3} &mdash; {4}</div>'.format(bg, icon, name, target, detail))
        output.print_html("".join(html))

    def close_click(self, sender, args):
        self.Close()


def main():
    doc = __revit__.ActiveUIDocument.Document
    window = DeeVTemplateWindow(_XAML_FILE, doc)
    window.ShowDialog()


main()
