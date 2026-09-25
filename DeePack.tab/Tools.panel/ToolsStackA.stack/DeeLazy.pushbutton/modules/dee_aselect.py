# -*- coding: utf-8 -*-
"""
DeeLazy - DeeASelect module
Selects EVERY element in the whole project - model, annotation, links,
everything - in one click, with a category checklist shown FIRST so the
user can review the breakdown and untick whole categories before the
actual selection happens. All categories start CHECKED (matching the
plain meaning of "select all"); the checklist itself IS the "message
before the process" this module exists to give - not a second
confirmation on top of it, since selecting elements is never
destructive (nothing is changed, deleted or moved).

Scope: the WHOLE document, not the active view - every non-type element
FilteredElementCollector(doc).WhereElementIsNotElementType() returns,
regardless of what the current view happens to show. Elements outside
the active view are still genuinely selected in Revit (later actions -
Filter, a Schedule, Delete, a parameter edit - see them), they are just
not visibly HIGHLIGHTED until a view showing them is opened; the
dialog's own header text says this plainly so it is never a surprise.

No CategoryType filtering happens during the scan itself - Model,
Annotation, Internal (levels, grids, reference planes, ...) and
anything else all get their own row, grouped by Category.Name exactly
like DeeCtotopo's own "(No category)" convention for the rare element
that has none. "Model Only" / "Annotation Only" are quick PRESET
buttons built from that CategoryType, on top of the always-available
per-category checkboxes - not a filter applied at scan time, so
switching presets never re-scans the model.

--------------------------------------------------------------------
NEEDS LIVE-REVIT VERIFICATION (per this codebase's convention)
--------------------------------------------------------------------
1. Whether elements hosted inside a Model/Detail Group are returned
   individually by the plain collector alongside the Group instance
   itself, or only the Group instance is - not exhaustively confirmed
   across Revit versions. Either way every element the collector DOES
   return is included; nothing is deliberately excluded.
2. Selection.SetElementIds' behaviour/performance on a very large
   (100,000+) element set - wrapped in try/except so a refusal reports
   the real Revit error rather than crashing the window, but not
   exercised live at that scale.
"""
import os

import clr
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")

from System.Collections.Generic import List

from pyrevit import forms, script
import dee_branding

from Autodesk.Revit.DB import FilteredElementCollector, ElementId, CategoryType

output = script.get_output()

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "DeeASelect.xaml")

_NO_CATEGORY = u"(No category)"


class _SafeProgress(object):
    """forms.ProgressBar tries to set Window.TaskbarItemInfo on its host
    window - a genuine WPF-level bug (not this extension's code), fully
    documented in dee_view_select.py's own copy of this class. Wraps the
    real forms.ProgressBar and falls back to no progress UI at all if
    entering it fails, so a Remote Desktop session degrades gracefully
    instead of crashing."""
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


def _category_name(element):
    try:
        name = element.Category.Name
        if name:
            return name
    except Exception:
        pass
    return _NO_CATEGORY


def _category_type_label(element):
    try:
        ct = element.Category.CategoryType
        if ct == CategoryType.Model:
            return u"Model"
        if ct == CategoryType.Annotation:
            return u"Annotation"
        return u"Other"
    except Exception:
        return u"Other"


class CategoryRow(object):
    """One checkable row - a category name, its element count, its
    Model/Annotation/Other grouping (for the two preset buttons), and
    whether it is currently ticked. Starts ticked: the default action
    this whole module exists for IS "select everything"."""
    def __init__(self, name, count, type_label):
        self.name = name
        self.count = count
        self.type_label = type_label
        self.checked = True


def scan_categories(doc):
    """One pass over the whole document. Returns (rows, id_map) - rows
    for the checklist, id_map={category_name: [ElementId, ...]} so the
    final selection never has to re-scan the model; it only has to
    concatenate whichever buckets are still ticked when Select is
    clicked."""
    buckets = {}  # name -> {"ids": [ElementId,...], "type_label": str}
    try:
        collector = FilteredElementCollector(doc).WhereElementIsNotElementType()
    except Exception:
        return [], {}
    for el in collector:
        name = _category_name(el)
        bucket = buckets.get(name)
        if bucket is None:
            bucket = {"ids": [], "type_label": _category_type_label(el)}
            buckets[name] = bucket
        try:
            bucket["ids"].append(el.Id)
        except Exception:
            continue
    rows = [CategoryRow(name, len(b["ids"]), b["type_label"])
            for name, b in buckets.items()]
    rows.sort(key=lambda r: r.name.lower())
    id_map = dict((name, b["ids"]) for name, b in buckets.items())
    return rows, id_map


# ==========================================================================
# window
# ==========================================================================
class DeeASelectWindow(dee_branding.DeeBrandedWindow):
    # Must exist BEFORE the base class loads the XAML - loading it can
    # fire TextChanged/Checked handlers before __init__ has finished.
    _ready = False

    def __init__(self, xaml_file, uiapp):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.uiapp = uiapp
        self.uidoc = uiapp.ActiveUIDocument
        self.doc = self.uidoc.Document

        with _SafeProgress(title="DeeASelect - scanning the project...", indeterminate=True):
            self._rows, self._id_map = scan_categories(self.doc)

        self._ready = True
        self._refresh_list()
        self._update_summary()
        if not self._rows:
            self.status_tb.Text = "This document has no selectable elements."

    def _shown_rows(self):
        query = ""
        try:
            query = (self.search_tb.Text or "").strip().lower()
        except Exception:
            pass
        if not query:
            return list(self._rows)
        return [r for r in self._rows if query in r.name.lower()]

    def _refresh_list(self):
        self.cats_lb.ItemsSource = None
        self.cats_lb.ItemsSource = self._shown_rows()

    def search_changed(self, sender, args):
        if not self._ready:
            return
        self._refresh_list()

    def _set_all(self, value, only_type=None):
        for r in self._rows:
            if only_type is not None and r.type_label != only_type:
                continue
            r.checked = value
        self._refresh_list()
        self._update_summary()

    def all_click(self, sender, args):
        self._set_all(True)

    def none_click(self, sender, args):
        self._set_all(False)

    def model_only_click(self, sender, args):
        self._set_all(False)
        self._set_all(True, only_type=u"Model")

    def annotation_only_click(self, sender, args):
        self._set_all(False)
        self._set_all(True, only_type=u"Annotation")

    def cat_toggled(self, sender, args):
        """The model is set from the CheckBox's own state, the same
        defensive pattern DeePrinter's SheetOption rows use, rather than
        trusting the TwoWay binding to have written it back first."""
        try:
            row = sender.DataContext
            if row is not None:
                row.checked = sender.IsChecked is True
        except Exception:
            pass
        self._update_summary()

    def _update_summary(self):
        checked_rows = [r for r in self._rows if r.checked]
        total_elems = sum(r.count for r in checked_rows)
        self.summary_tb.Text = (
            u"{0} of {1} categories checked - {2:,} element(s) will be selected."
            .format(len(checked_rows), len(self._rows), total_elems))

    def select_click(self, sender, args):
        checked_rows = [r for r in self._rows if r.checked]
        if not checked_rows:
            forms.alert("Tick at least one category first.", title="DeeASelect")
            return
        ids = List[ElementId]()
        for r in checked_rows:
            for eid in self._id_map.get(r.name, []):
                ids.Add(eid)
        if ids.Count == 0:
            forms.alert("Nothing to select.", title="DeeASelect")
            return
        try:
            self.uidoc.Selection.SetElementIds(ids)
        except Exception as e:
            forms.alert(u"Revit refused the selection:\n{0}".format(e),
                        title="DeeASelect")
            return
        self.status_tb.Text = (
            u"Selected {0:,} element(s) across {1} categor(y/ies)."
            .format(ids.Count, len(checked_rows)))
        if self.close_after_cb.IsChecked is True:
            self.Close()

    def close_click(self, sender, args):
        self.Close()


# ==========================================================================
# Launch entry point (called by the DeeLazy home window)
# ==========================================================================
def launch(uiapp):
    if uiapp.ActiveUIDocument is None:
        forms.alert("Open a Revit project first.")
        return
    window = DeeASelectWindow(_XAML_FILE, uiapp)
    window.ShowDialog()


TOOL_INFO = {
    "id": "dee_aselect",
    "title": "DeeASelect",
    "description": "Select EVERY element in the project - model, annotation, links, everything - with a category checklist to review or deselect some before it happens.",
    "launch": launch,
}
