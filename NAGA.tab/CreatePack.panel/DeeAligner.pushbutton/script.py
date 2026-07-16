# -*- coding: utf-8 -*-
"""
DeeAligner (CreatePack)
Aligns/distributes Viewports and Image instances placed on a sheet, and
can auto-arrange a set of Images into a grid within the sheet's title
block bounds.

Load a sheet from the dropdown (lists every Viewport and Image on it),
or select Viewports/Images in Revit first and click Use Current
Selection - either way populates the same grid, where the Include
checkbox controls which rows an alignment button acts on (align needs
at least 2 checked, distribute needs at least 3). Auto-Arrange only
touches checked Images, laying them into a grid sized to the sheet's
title block bounds - it does NOT check for or avoid overlapping
viewports/other content, so it's meant for images placed in space
you've already reserved (e.g. a logo/photo strip), not a general
layout solver.

Architecture: lib/align_tools.py holds the alignment/distribute/grid
math as plain (min_x, max_x, min_y, max_y) bounding-box tuples with no
Revit dependency (unit-tested standalone). This script only reads each
Viewport/Image's bounding box in sheet space, calls into align_tools for
the target deltas, and applies them via each category's own Revit API
move mechanism (Viewport.SetBoxCenter vs ElementTransformUtils.MoveElement
for Images) - the two need different calls since Viewport position on a
sheet isn't a regular movable element the normal way.

Needs live-Revit verification before trusting on real sheets: GetBoxOutline/
SetBoxCenter for Viewports, get_BoundingBox(sheet) for Images (this is how
an Image placed directly on a sheet is expected to report its position),
GetAllViewports, and OwnerViewId for an Image selected via Use Current
Selection.
"""
import os

import clr
clr.AddReference("WindowsBase")
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")

from pyrevit import forms, script
from Autodesk.Revit.DB import (
    FilteredElementCollector, ViewSheet, Viewport, ImageInstance, Transaction,
    ElementTransformUtils, BuiltInCategory, BuiltInParameter, XYZ,
)

import align_tools

output = script.get_output()

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")


# -- defensive reads (same pattern already proven elsewhere in this extension) --
def _read_name(element):
    if element is None:
        return None
    try:
        n = element.Name
        if n:
            return n
    except Exception:
        pass
    for bip in (BuiltInParameter.SYMBOL_NAME_PARAM, BuiltInParameter.ALL_MODEL_TYPE_NAME,
                BuiltInParameter.DATUM_TEXT):
        try:
            p = element.get_Parameter(bip)
            if p is not None:
                val = p.AsString()
                if val:
                    return val
        except Exception:
            continue
    return None


def _viewport_bbox(vp):
    outline = vp.GetBoxOutline()
    mn = outline.MinimumPoint
    mx = outline.MaximumPoint
    return mn.X, mx.X, mn.Y, mx.Y


def _image_bbox(img, sheet):
    try:
        bbox = img.get_BoundingBox(sheet)
    except Exception:
        bbox = None
    if bbox is None:
        return None
    return bbox.Min.X, bbox.Max.X, bbox.Min.Y, bbox.Max.Y


def _move_viewport(vp, dx, dy):
    center = vp.GetBoxCenter()
    vp.SetBoxCenter(XYZ(center.X + dx, center.Y + dy, center.Z))


def _move_image(doc, img, dx, dy):
    ElementTransformUtils.MoveElement(doc, img.Id, XYZ(dx, dy, 0.0))


def _get_sheet_bounds(doc, sheet):
    try:
        title_blocks = list(FilteredElementCollector(doc, sheet.Id)
                             .OfCategory(BuiltInCategory.OST_TitleBlocks)
                             .WhereElementIsNotElementType())
    except Exception:
        title_blocks = []
    for tb in title_blocks:
        try:
            bbox = tb.get_BoundingBox(sheet)
            if bbox is not None:
                return bbox.Min.X, bbox.Max.X, bbox.Min.Y, bbox.Max.Y
        except Exception:
            continue
    return None


def _scan_sheet_items(doc, sheet):
    rows = []
    try:
        vp_ids = list(sheet.GetAllViewports())
    except Exception:
        vp_ids = []
    for vp_id in vp_ids:
        vp = doc.GetElement(vp_id)
        if vp is None:
            continue
        try:
            view = doc.GetElement(vp.ViewId)
            name = _read_name(view) or "(view)"
            min_x, max_x, min_y, max_y = _viewport_bbox(vp)
            rows.append(AlignableRow(vp, "Viewport", name, min_x, max_x, min_y, max_y))
        except Exception:
            continue

    try:
        images = list(FilteredElementCollector(doc, sheet.Id).OfClass(ImageInstance))
    except Exception:
        images = []
    for img in images:
        try:
            bbox = _image_bbox(img, sheet)
            if bbox is None:
                continue
            min_x, max_x, min_y, max_y = bbox
            type_elem = doc.GetElement(img.GetTypeId())
            name = _read_name(type_elem) or "(image)"
            rows.append(AlignableRow(img, "Image", name, min_x, max_x, min_y, max_y))
        except Exception:
            continue
    return rows


class AlignableRow(object):
    def __init__(self, element, kind, name, min_x, max_x, min_y, max_y):
        self.element = element
        self.kind = kind
        self.id = element.Id
        self.name = name
        self.min_x = min_x
        self.max_x = max_x
        self.min_y = min_y
        self.max_y = max_y
        self.is_selected = True

    @property
    def bbox(self):
        return (self.min_x, self.max_x, self.min_y, self.max_y)

    @property
    def center_x_text(self):
        return "{0:.3f}".format((self.min_x + self.max_x) / 2.0)

    @property
    def center_y_text(self):
        return "{0:.3f}".format((self.min_y + self.max_y) / 2.0)


class DeeAlignerWindow(forms.WPFWindow):
    def __init__(self, xaml_file, doc):
        forms.WPFWindow.__init__(self, xaml_file)
        self.doc = doc
        self._items = []
        self._sheet = None
        self._sheet_by_name = {}
        self._scan_sheets()

    def _scan_sheets(self):
        names = []
        self._sheet_by_name = {}
        for sheet in FilteredElementCollector(self.doc).OfClass(ViewSheet):
            try:
                name = "{0} - {1}".format(sheet.SheetNumber, _read_name(sheet) or "(unnamed)")
                self._sheet_by_name[name] = sheet
                names.append(name)
            except Exception:
                continue
        names.sort()
        self.sheet_cb.ItemsSource = None
        self.sheet_cb.ItemsSource = names

    def load_sheet_click(self, sender, args):
        name = self.sheet_cb.SelectedItem
        sheet = self._sheet_by_name.get(name)
        if sheet is None:
            forms.alert("Pick a sheet first.")
            return
        with forms.ProgressBar(title="DeeAligner — scanning sheet...", cancellable=True):
            self._sheet = sheet
            self._items = _scan_sheet_items(self.doc, sheet)
        self._refresh_grid()

    def use_selection_click(self, sender, args):
        try:
            sel_ids = set(__revit__.ActiveUIDocument.Selection.GetElementIds())
        except Exception:
            forms.alert("Could not read the current Revit selection.")
            return
        if not sel_ids:
            forms.alert("Nothing selected in Revit - select Viewports/Images on a sheet first.")
            return

        rows = []
        sheet = None
        for eid in sel_ids:
            el = self.doc.GetElement(eid)
            if el is None:
                continue
            try:
                if isinstance(el, Viewport):
                    owner_sheet = self.doc.GetElement(el.SheetId)
                    if sheet is None:
                        sheet = owner_sheet
                    view = self.doc.GetElement(el.ViewId)
                    name = _read_name(view) or "(view)"
                    min_x, max_x, min_y, max_y = _viewport_bbox(el)
                    rows.append(AlignableRow(el, "Viewport", name, min_x, max_x, min_y, max_y))
                elif isinstance(el, ImageInstance):
                    owner_view = self.doc.GetElement(el.OwnerViewId)
                    if owner_view is None:
                        continue
                    if sheet is None:
                        sheet = owner_view
                    bbox = _image_bbox(el, owner_view)
                    if bbox is None:
                        continue
                    min_x, max_x, min_y, max_y = bbox
                    type_elem = self.doc.GetElement(el.GetTypeId())
                    name = _read_name(type_elem) or "(image)"
                    rows.append(AlignableRow(el, "Image", name, min_x, max_x, min_y, max_y))
            except Exception:
                continue

        if not rows:
            forms.alert("Selection doesn't contain any Viewports or Images placed on a sheet.")
            return
        self._items = rows
        self._sheet = sheet
        self._refresh_grid()

    def _refresh_grid(self):
        self.items_grid.ItemsSource = None
        self.items_grid.ItemsSource = self._items
        sheet_label = ""
        if self._sheet is not None:
            try:
                sheet_label = " on '{0} - {1}'".format(self._sheet.SheetNumber, _read_name(self._sheet) or "")
            except Exception:
                pass
        self.status_tb.Text = "{0} item(s) loaded{1}.".format(len(self._items), sheet_label)

    def _get_selected(self):
        return [r for r in self._items if r.is_selected]

    def _refresh_row_bbox(self, row):
        try:
            if row.kind == "Viewport":
                row.min_x, row.max_x, row.min_y, row.max_y = _viewport_bbox(row.element)
            elif self._sheet is not None:
                bbox = _image_bbox(row.element, self._sheet)
                if bbox is not None:
                    row.min_x, row.max_x, row.min_y, row.max_y = bbox
        except Exception:
            pass

    def _apply_alignment(self, op_name, align_fn, min_count=2):
        selected = self._get_selected()
        if len(selected) < min_count:
            forms.alert("Select at least {0} item(s) to {1}.".format(min_count, op_name.lower()))
            return
        boxes = [r.bbox for r in selected]
        deltas = align_fn(boxes)

        results = []
        t = Transaction(self.doc, "DeeAligner - {0}".format(op_name))
        t.Start()
        for row, (dx, dy) in zip(selected, deltas):
            try:
                if abs(dx) < 1e-9 and abs(dy) < 1e-9:
                    results.append((True, row.name, "Already aligned"))
                    continue
                if row.kind == "Viewport":
                    _move_viewport(row.element, dx, dy)
                else:
                    _move_image(self.doc, row.element, dx, dy)
                self._refresh_row_bbox(row)
                results.append((True, row.name, "Moved by ({0:.3f}, {1:.3f})".format(dx, dy)))
            except Exception as e:
                results.append((False, row.name, "FAILED: {0}".format(e)))
        t.Commit()

        self.items_grid.Items.Refresh()
        self._report_results(op_name, results)

    def _report_results(self, op_name, results):
        ok_count = sum(1 for ok, _, _ in results if ok)
        fail_count = len(results) - ok_count
        html = ['<h2 style="font-family:sans-serif;color:#ddd;">DeeAligner - {0} Results</h2>'.format(op_name),
                '<p style="color:#ddd;">{0} succeeded, {1} failed.</p>'.format(ok_count, fail_count)]
        for ok, name, detail in results:
            bg = "#2e7d32" if ok else "#c62828"
            icon = "&#10003;" if ok else "&#10007;"
            html.append(
                '<div style="padding:4px 10px;margin:2px 0;background:{0};color:#fff;'
                'border-radius:4px;font-family:monospace;font-size:12px;">'
                '{1}&nbsp; <b>{2}</b> &mdash; {3}</div>'.format(bg, icon, name, detail))
        output.print_html("".join(html))

    def align_left_click(self, sender, args):
        self._apply_alignment("Align Left", align_tools.align_left)

    def align_right_click(self, sender, args):
        self._apply_alignment("Align Right", align_tools.align_right)

    def align_top_click(self, sender, args):
        self._apply_alignment("Align Top", align_tools.align_top)

    def align_bottom_click(self, sender, args):
        self._apply_alignment("Align Bottom", align_tools.align_bottom)

    def align_center_h_click(self, sender, args):
        self._apply_alignment("Align Center Horizontal", align_tools.align_center_horizontal)

    def align_middle_v_click(self, sender, args):
        self._apply_alignment("Align Middle Vertical", align_tools.align_middle_vertical)

    def distribute_h_click(self, sender, args):
        self._apply_alignment("Distribute Horizontally", align_tools.distribute_horizontal, min_count=3)

    def distribute_v_click(self, sender, args):
        self._apply_alignment("Distribute Vertically", align_tools.distribute_vertical, min_count=3)

    def auto_arrange_click(self, sender, args):
        images = [r for r in self._items if r.kind == "Image" and r.is_selected]
        if not images:
            forms.alert("Check at least one Image to arrange.")
            return
        if self._sheet is None:
            forms.alert("Load a sheet first (Auto-Arrange needs the sheet's title block bounds).")
            return
        bounds = _get_sheet_bounds(self.doc, self._sheet)
        if bounds is None:
            forms.alert("Could not determine sheet bounds - no title block found on this sheet.")
            return
        centers = align_tools.grid_layout_centers(len(images), bounds)

        results = []
        t = Transaction(self.doc, "DeeAligner - Auto-Arrange Images")
        t.Start()
        for row, (cx, cy) in zip(images, centers):
            try:
                cur_cx = (row.min_x + row.max_x) / 2.0
                cur_cy = (row.min_y + row.max_y) / 2.0
                _move_image(self.doc, row.element, cx - cur_cx, cy - cur_cy)
                self._refresh_row_bbox(row)
                results.append((True, row.name, "Arranged into grid"))
            except Exception as e:
                results.append((False, row.name, "FAILED: {0}".format(e)))
        t.Commit()

        self.items_grid.Items.Refresh()
        self._report_results("Auto-Arrange Images", results)

    def close_click(self, sender, args):
        self.Close()


def main():
    doc = __revit__.ActiveUIDocument.Document
    window = DeeAlignerWindow(_XAML_FILE, doc)
    window.ShowDialog()


main()
