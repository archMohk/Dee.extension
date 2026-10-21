# -*- coding: utf-8 -*-
"""
DeeLazy - Dependent / Non-Dependent Views module
Scans every view in the project, classifies each as Dependent or
Independent (with its parent view / dependent count and the sheet it is
on), and converts ticked DEPENDENT views into INDEPENDENT ones. Each
conversion replaces the dependent view with an independent copy that
keeps the same name, and - if the dependent view was placed on a sheet -
puts the independent copy on that same sheet in the same position, so
nothing silently drops off a sheet.

--------------------------------------------------------------------
How a conversion works (one Transaction per view, all wrapped in one
TransactionGroup so the whole batch is a single undo step)
--------------------------------------------------------------------
1. D.Duplicate(ViewDuplicateOption.WithDetailing | Duplicate) - the copy
   is checked to be NON-dependent (View.GetPrimaryViewId() invalid); if
   Revit hands back a dependent copy anyway, that view's changes are
   rolled back and it is reported as failed rather than left half-done.
2. Every Viewport that shows D is re-pointed at the copy by setting
   Viewport.ViewId (keeps the viewport's position, type, detail number,
   rotation and label offset untouched). If that setter is unavailable
   or throws on this Revit version, the viewport is deleted and
   recreated on the same sheet at the same box center with its type,
   rotation, label offset and detail number copied across (same calls
   as DeeVSDupl.pushbutton / DeeView.pushbutton already use).
3. The original dependent view D is deleted and the copy takes D's name.
A view that is the ACTIVE view cannot be deleted - it is skipped with a
clear message (switch to another view and rerun).

--------------------------------------------------------------------
Revit API facts relied on (members confirmed by .NET reflection against
the installed RevitAPI.dll, Revit 2026)
--------------------------------------------------------------------
- View.GetPrimaryViewId(), View.GetDependentViewIds(),
  View.CanViewBeDuplicated(ViewDuplicateOption), View.Duplicate(...)
- ViewDuplicateOption.Duplicate / AsDependent / WithDetailing
- Viewport.Create / CanAddViewToSheet / GetBoxCenter / SetBoxCenter /
  Rotation / LabelOffset / GetTypeId / ChangeTypeId, and a SETTABLE
  Viewport.ViewId
- BuiltInParameter.VIEWPORT_DETAIL_NUMBER

--------------------------------------------------------------------
NEEDS LIVE-REVIT VERIFICATION (flagged, not silently assumed correct)
--------------------------------------------------------------------
- That View.Duplicate() on a DEPENDENT view yields an INDEPENDENT copy.
  This is the standard way to detach a dependent view, but it has not
  been exercised live here - guarded by the post-duplicate check above.
- That the Viewport.ViewId setter exists on the Revit version in use
  (it exists in 2026; older versions fall back to delete + recreate).
- Annotations that belong to the PARENT view (its detail lines, text,
  dimensions) are not owned by the dependent view, so they do not come
  across to the independent copy; annotations owned by the dependent
  view itself do (the "Copy detailing" option).
"""
import os
import time

import clr
clr.AddReference("WindowsBase")
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")

from pyrevit import forms, script
import dee_branding
from Autodesk.Revit.DB import (
    FilteredElementCollector, View, ViewType, Viewport, Transaction,
    TransactionGroup, ViewDuplicateOption, BuiltInParameter,
)

import utils

output = script.get_output()

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "DeeDependentViews.xaml")

_ALLOWED_VIEW_TYPES = set([
    ViewType.FloorPlan, ViewType.CeilingPlan, ViewType.EngineeringPlan, ViewType.AreaPlan,
    ViewType.Elevation, ViewType.Section, ViewType.Detail, ViewType.ThreeD,
])

FILTER_DEPENDENT = "Dependent views"
FILTER_INDEPENDENT = "Non-dependent views"
FILTER_ALL = "All views"
FILTER_CHOICES = [FILTER_DEPENDENT, FILTER_INDEPENDENT, FILTER_ALL]


# ==========================================================================
# Scanning
# ==========================================================================
def _is_candidate(view):
    try:
        if view is None or not isinstance(view, View):
            return False
        if view.IsTemplate:
            return False
        if view.ViewType not in _ALLOWED_VIEW_TYPES:
            return False
    except Exception:
        return False
    return True


def _scale_text(view):
    try:
        return "1:{0}".format(view.Scale)
    except Exception:
        return ""


class ViewRow(object):
    def __init__(self, doc, view, sheet):
        self.view = view
        self.selected = False
        self.name = utils.read_name(view) or "(unnamed view)"
        self.view_type = str(view.ViewType)
        self.scale = _scale_text(view)
        self.sheet = sheet
        self.sheet_text = utils.sheet_label(sheet)

        parent_id = utils.get_primary_view_id(view)
        self.is_dependent = parent_id is not None
        self.kind = "Dependent" if self.is_dependent else "Non-dependent"
        self.related = ""
        if self.is_dependent:
            try:
                self.related = "Parent: {0}".format(utils.read_name(doc.GetElement(parent_id)) or "?")
            except Exception:
                self.related = "Parent: ?"
        else:
            try:
                count = len(list(view.GetDependentViewIds()))
            except Exception:
                count = 0
            if count:
                self.related = "{0} dependent view(s)".format(count)


def scan_all_views(doc):
    view_to_sheet = utils.build_view_to_sheet_map(doc)
    rows = []
    for view in FilteredElementCollector(doc).OfClass(View):
        if not _is_candidate(view):
            continue
        rows.append(ViewRow(doc, view, view_to_sheet.get(utils.id_value(view.Id))))
    rows.sort(key=lambda r: (r.name.lower(), r.view_type))
    return rows


# ==========================================================================
# Conversion
# ==========================================================================
class Result(object):
    def __init__(self, view_name, ok, detail, sheet_text=""):
        self.view_name = view_name
        self.ok = ok
        self.result = "OK" if ok else "Skipped/Failed"
        self.detail = detail
        self.sheet_text = sheet_text


def _viewports_showing(doc, view_id):
    out = []
    for vp in FilteredElementCollector(doc).OfClass(Viewport):
        try:
            if vp.ViewId == view_id:
                out.append(vp)
        except Exception:
            continue
    return out


def _recreate_viewport(doc, vp, new_view_id):
    """Fallback when Viewport.ViewId cannot be re-pointed: capture the old
    viewport's placement, delete it (which also frees its detail number),
    and recreate it on the same sheet for the new view."""
    sheet_id = vp.SheetId
    center = vp.GetBoxCenter()
    type_id = vp.GetTypeId()
    detail_number = None
    try:
        p = vp.get_Parameter(BuiltInParameter.VIEWPORT_DETAIL_NUMBER)
        if p is not None and p.HasValue:
            detail_number = p.AsString()
    except Exception:
        pass
    rotation = None
    label_offset = None
    try:
        rotation = vp.Rotation
    except Exception:
        pass
    try:
        label_offset = vp.LabelOffset
    except Exception:
        pass

    doc.Delete(vp.Id)
    if not Viewport.CanAddViewToSheet(doc, sheet_id, new_view_id):
        raise Exception("the independent copy cannot be added to the sheet")
    new_vp = Viewport.Create(doc, sheet_id, new_view_id, center)
    try:
        new_vp.SetBoxCenter(center)
    except Exception:
        pass
    try:
        new_vp.ChangeTypeId(type_id)
    except Exception:
        pass
    if rotation is not None:
        try:
            new_vp.Rotation = rotation
        except Exception:
            pass
    if label_offset is not None:
        try:
            new_vp.LabelOffset = label_offset
        except Exception:
            pass
    if detail_number:
        try:
            p = new_vp.get_Parameter(BuiltInParameter.VIEWPORT_DETAIL_NUMBER)
            if p is not None and not p.IsReadOnly:
                p.Set(detail_number)
        except Exception:
            pass


def make_independent(doc, row, copy_detailing):
    """One dependent view -> one independent view with the same name, on
    the same sheet in the same position. Never raises; any failure rolls
    back just this view's changes and is returned as a failed Result."""
    view = row.view
    name = row.name

    try:
        active = doc.ActiveView
        if active is not None and active.Id == view.Id:
            return Result(name, False, "This is the active view and cannot be deleted - "
                                       "switch to another view and run again.", row.sheet_text)
    except Exception:
        pass

    t = Transaction(doc, "DeeLazy - Make View Independent")
    t.Start()
    try:
        option = ViewDuplicateOption.Duplicate
        if copy_detailing:
            try:
                if view.CanViewBeDuplicated(ViewDuplicateOption.WithDetailing):
                    option = ViewDuplicateOption.WithDetailing
            except Exception:
                pass
        new_view = doc.GetElement(view.Duplicate(option))
        if utils.is_dependent_view(new_view):
            raise Exception("Revit created the copy as a dependent view again.")

        placed_on = []
        for vp in _viewports_showing(doc, view.Id):
            placed_on.append(utils.sheet_label(doc.GetElement(vp.SheetId)))
            try:
                vp.ViewId = new_view.Id
            except Exception:
                _recreate_viewport(doc, vp, new_view.Id)

        if _viewports_showing(doc, view.Id):
            raise Exception("the view is still placed on a sheet after the swap.")

        doc.Delete(view.Id)
        new_view.Name = name
        t.Commit()
    except Exception as e:
        try:
            t.RollBack()
        except Exception:
            pass
        return Result(name, False, "Not changed: {0}".format(e), row.sheet_text)

    if placed_on:
        detail = "Now independent, placed on {0} in the same position.".format(", ".join(placed_on))
    else:
        detail = "Now independent (it was not on a sheet)."
    return Result(name, True, detail, ", ".join(placed_on))


def run_batch(doc, rows, copy_detailing):
    """Every view is its own Transaction (one bad view never undoes the
    others); the TransactionGroup only makes the whole batch ONE undo
    step in Revit."""
    results = []
    tg = TransactionGroup(doc, "DeeLazy - Make Views Independent")
    tg.Start()
    try:
        for row in rows:
            try:
                results.append(make_independent(doc, row, copy_detailing))
            except Exception as e:
                results.append(Result(row.name, False, str(e), row.sheet_text))
    finally:
        tg.Assimilate()
    return results


# ==========================================================================
# Report
# ==========================================================================
def _report_html(results, elapsed_seconds):
    ok_count = sum(1 for r in results if r.ok)
    html = [
        '<h2 style="font-family:sans-serif;color:#ddd;">DeeLazy - Dependent / Non-Dependent Views</h2>',
        '<p style="color:#ddd;">{0} view(s) processed - {1} made independent, {2} skipped/failed - {3:.1f}s.</p>'.format(
            len(results), ok_count, len(results) - ok_count, elapsed_seconds),
    ]
    for r in results:
        bg = "#2e7d32" if r.ok else "#c62828"
        icon = "&#10003;" if r.ok else "&#10007;"
        html.append(
            '<div style="padding:4px 10px;margin:2px 0;background:{0};color:#fff;'
            'border-radius:4px;font-family:monospace;font-size:12px;">'
            '{1}&nbsp; <b>{2}</b> &mdash; {3}</div>'.format(bg, icon, r.view_name, r.detail))
    output.print_html("".join(html))


# ==========================================================================
# Window - UI wiring only
# ==========================================================================
class DeeDependentViewsWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, uiapp):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.uiapp = uiapp
        self.uidoc = uiapp.ActiveUIDocument
        self.doc = self.uidoc.Document
        self._all_rows = []
        self._results = []

        self.filter_cb.ItemsSource = FILTER_CHOICES
        self.filter_cb.SelectedIndex = 0
        self._scan()

    def _log(self, message):
        self.status_tb.Text = message

    def _scan(self):
        self._all_rows = scan_all_views(self.doc)
        self._refresh_grid()

    def _filter_mode(self):
        item = self.filter_cb.SelectedItem
        return item if item else FILTER_DEPENDENT

    def _visible_rows(self):
        mode = self._filter_mode()
        rows = self._all_rows
        if mode == FILTER_DEPENDENT:
            rows = [r for r in rows if r.is_dependent]
        elif mode == FILTER_INDEPENDENT:
            rows = [r for r in rows if not r.is_dependent]
        text = (self.search_tb.Text or "").strip().lower()
        if text:
            rows = [r for r in rows
                    if text in r.name.lower() or text in r.sheet_text.lower() or text in r.related.lower()]
        return rows

    def _refresh_grid(self):
        visible = self._visible_rows()
        self.views_grid.ItemsSource = None
        self.views_grid.ItemsSource = visible
        dependent_total = sum(1 for r in self._all_rows if r.is_dependent)
        self._log("{0} view(s) scanned - {1} dependent, {2} non-dependent - {3} shown.".format(
            len(self._all_rows), dependent_total, len(self._all_rows) - dependent_total, len(visible)))

    def filter_changed(self, sender, args):
        if not getattr(self, "_all_rows", None):
            return
        self._refresh_grid()

    def search_changed(self, sender, args):
        if not getattr(self, "_all_rows", None):
            return
        self._refresh_grid()

    def rescan_click(self, sender, args):
        self._scan()

    def select_all_click(self, sender, args):
        for r in self._visible_rows():
            if r.is_dependent:
                r.selected = True
        self.views_grid.Items.Refresh()

    def select_none_click(self, sender, args):
        for r in self._visible_rows():
            r.selected = False
        self.views_grid.Items.Refresh()

    def convert_click(self, sender, args):
        ticked = [r for r in self._all_rows if r.selected]
        if not ticked:
            forms.alert("Tick at least one dependent view first.")
            return
        targets = [r for r in ticked if r.is_dependent]
        if not targets:
            forms.alert("Only DEPENDENT views can be made independent - none of the ticked views are dependent.")
            return
        ignored = len(ticked) - len(targets)
        on_sheet = sum(1 for r in targets if r.sheet is not None)
        message = ("Make {0} dependent view(s) independent?\n\n"
                   "Each one is replaced by an independent copy with the same name; the {1} that are on a "
                   "sheet keep their sheet and position. The original dependent views are deleted. "
                   "This is a single undo step (Ctrl+Z).").format(len(targets), on_sheet)
        if ignored:
            message += "\n\n({0} ticked non-dependent view(s) will be ignored.)".format(ignored)
        if not forms.alert(message, yes=True, no=True):
            return

        copy_detailing = bool(self.copy_detailing_cb.IsChecked)
        start = time.time()
        results = run_batch(self.doc, targets, copy_detailing)
        elapsed = time.time() - start

        self._results = results
        self.report_grid.ItemsSource = None
        self.report_grid.ItemsSource = results
        _report_html(results, elapsed)
        ok_count = sum(1 for r in results if r.ok)
        self._scan()
        self._log("{0} of {1} view(s) made independent - {2:.1f}s. See the report.".format(
            ok_count, len(results), elapsed))

    def close_click(self, sender, args):
        self.Close()


# ==========================================================================
# Launch entry point (called by the DeeLazy home window)
# ==========================================================================
def launch(uiapp):
    if uiapp.ActiveUIDocument is None:
        forms.alert("Open a Revit project first.")
        return
    window = DeeDependentViewsWindow(_XAML_FILE, uiapp)
    window.ShowDialog()


TOOL_INFO = {
    "id": "dependent_views",
    "title": "Dependent / Non-Dependent Views",
    "description": "Scan all views, then turn selected dependent views into independent ones - keeping each one's name and its place on the sheet.",
    "icon": u"\U0001F500",  # shuffle arrows - convert
    "launch": launch,
}
