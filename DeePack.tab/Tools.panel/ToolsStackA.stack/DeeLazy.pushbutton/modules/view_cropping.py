# -*- coding: utf-8 -*-
"""
DeeLazy - View Cropping module
Batch-edit Crop Region settings across many views at once: Crop Region
Active, Crop Region Visibility, Annotation Crop, Crop Offset (Top/
Bottom/Left/Right), and Scope Box assignment.

--------------------------------------------------------------------
Scope for this first pass (explicit user choice - NOT everything a
fuller "View Cropping" tool could eventually do)
--------------------------------------------------------------------
Crop Region Shape (non-rectangular shapes, matching/duplicating a
shape between views), split-crop, Far Clip, and View Depth were all
explicitly deferred - only the "core" crop properties below are
implemented. Every action here applies IMMEDIATELY once confirmed (a
plain confirmation dialog shows the affected view count first) - there
is no staged preview/undo-before-commit step, per explicit user choice
(that pattern exists in DeeAligner instead, where it was requested).

--------------------------------------------------------------------
Revit API facts relied on here (verified against revitapidocs.com
before writing, not guessed)
--------------------------------------------------------------------
- View.CropBoxActive (bool, get/set) - whether the Crop Region is
  active/enabled for the view at all.
- View.CropBoxVisible (bool, get/set) - whether the (already-active)
  crop boundary is visible - this single property is ALSO what "Show/
  Hide Crop Box" in the original spec meant (Revit has no separate
  "Crop Box" visibility distinct from "Crop Region" visibility - the
  spec's two sections were describing the same property twice; this
  module exposes ONE action group for it, not two).
- View.CropBox (BoundingBoxXYZ, get/set) - the crop geometry itself, IN
  THE VIEW'S OWN LOCAL COORDINATE SYSTEM (X=left/right, Y=bottom/top),
  used for the Top/Bottom/Left/Right crop OFFSET actions. The standard,
  well-documented pattern for changing it is: read the BoundingBoxXYZ,
  build a NEW one with the same .Transform and adjusted Min/Max, then
  reassign it back to view.CropBox (mutating the read-back object's
  Min/Max in place does not reliably take effect).
- "Annotation Crop" has NO direct bool property on View or on
  ViewCropRegionShapeManager (confirmed by reviewing that class's full
  documented property list before assuming otherwise - it only exposes
  per-side annotation crop OFFSETS and a capability check,
  CanHaveAnnotationCrop). The Yes/No toggle shown in Revit's Properties
  palette is a plain view PARAMETER instead - looked up here by NAME
  ("Annotation Crop") via utils.get/set_bool_param_by_name rather than
  a BuiltInParameter enum whose exact name wasn't confirmed reliable
  across Revit versions, matching this repo's established practice for
  exactly this situation (e.g. DeeAligner's Image Width/Height lookup).
- BuiltInParameter.VIEWER_VOLUME_OF_INTEREST_CROP - the Scope Box
  assignment parameter on a View (holds an ElementId, or
  ElementId.InvalidElementId for "none assigned").
- Scope Box elements live under BuiltInCategory.OST_VolumeOfInterest.
- View.GetPrimaryViewId() - ElementId.InvalidElementId for a primary
  (non-dependent) view, the parent view's id for a dependent one -
  used for the Dependent/Parent view filters.

--------------------------------------------------------------------
NEEDS LIVE-REVIT VERIFICATION (flagged, not silently assumed correct)
--------------------------------------------------------------------
- Whether every one of the supported ViewTypes (FloorPlan, CeilingPlan,
  Section, Elevation, Detail, DraftingView, ThreeD) actually exposes a
  usable CropBoxActive/CropBoxVisible/CropBox in every Revit version -
  handled defensively (each action wraps its own per-view API calls in
  try/except and reports "skipped - not supported" rather than
  assuming), but not yet exercised live for every type.
- Whether "Annotation Crop" is the exact, unlocalized parameter display
  name in all supported Revit versions/languages - if not found by
  that name on a given view, the row simply shows "(not available)"
  rather than guessing a wrong parameter.
"""
import os
import time

import clr
clr.AddReference("WindowsBase")
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
clr.AddReference("System.Windows.Forms")

from pyrevit import forms, script
import dee_branding
from Autodesk.Revit.DB import (
    FilteredElementCollector, View, ViewType, ViewSheet, Viewport,
    Transaction, ElementId, BoundingBoxXYZ, XYZ, BuiltInParameter, BuiltInCategory,
)
from System.Windows.Forms import SaveFileDialog, DialogResult, MessageBox

import utils
import deew_progress_service as progsvc
import xlsx_writer

output = script.get_output()

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ViewCropping.xaml")

_SUPPORTED_VIEW_TYPES = set([
    ViewType.FloorPlan, ViewType.CeilingPlan, ViewType.Section, ViewType.Elevation,
    ViewType.Detail, ViewType.DraftingView, ViewType.ThreeD,
])

_ANNOTATION_CROP_PARAM_NAME = "Annotation Crop"

_REPORT_HEADERS = ["View Name", "View Type", "Sheet", "Action", "Result", "Detail"]
_REPORT_COL_WIDTHS = [30, 16, 26, 22, 12, 40]


# ==========================================================================
# View scanning / classification
# ==========================================================================
def _is_supported_candidate(view):
    if view is None:
        return False
    try:
        if not isinstance(view, View):
            return False
        if view.IsTemplate:
            return False
        if view.ViewType not in _SUPPORTED_VIEW_TYPES:
            return False
    except Exception:
        return False
    return True


def _scale_text(view):
    try:
        return str(view.Scale)
    except Exception:
        return ""


def _discipline_text(view):
    try:
        return str(view.Discipline)
    except Exception:
        return ""


def _view_template_name(doc, view):
    try:
        tid = view.ViewTemplateId
        if tid is not None and tid.IntegerValue > 0:
            t = doc.GetElement(tid)
            return utils.read_name(t) or "(unnamed template)"
    except Exception:
        pass
    return "(none)"


class ViewRow(object):
    """One row per candidate view - crop-state fields are snapshotted
    at scan/refresh time (never mutated in place by an action; a
    refresh() re-reads them from Revit so the grid always reflects
    ground truth after any action)."""

    def __init__(self, doc, view, sheet):
        self.doc = doc
        self.view = view
        self.id = view.Id
        self.selected = False
        self.name = utils.read_name(view) or "(unnamed view)"
        self.view_type = str(view.ViewType)
        self.sheet = sheet
        self.is_dependent = utils.is_dependent_view(view)
        self.scale = _scale_text(view)
        self.discipline = _discipline_text(view)
        self.view_template = _view_template_name(doc, view)
        self.crop_active = None
        self.crop_visible = None
        self.annotation_crop = None
        self.refresh_crop_state()

    def refresh_crop_state(self):
        try:
            self.crop_active = bool(self.view.CropBoxActive)
        except Exception:
            self.crop_active = None
        try:
            self.crop_visible = bool(self.view.CropBoxVisible)
        except Exception:
            self.crop_visible = None
        self.annotation_crop = utils.get_bool_param_by_name(self.view, _ANNOTATION_CROP_PARAM_NAME)

    @property
    def sheet_text(self):
        return utils.sheet_label(self.sheet)

    @property
    def crop_active_text(self):
        return _tri_text(self.crop_active)

    @property
    def crop_visible_text(self):
        return _tri_text(self.crop_visible)

    @property
    def annotation_crop_text(self):
        return _tri_text(self.annotation_crop)


def _tri_text(value):
    if value is None:
        return "(n/a)"
    return "Yes" if value else "No"


def scan_entire_project(doc):
    view_to_sheet = utils.build_view_to_sheet_map(doc)
    rows = []
    for view in FilteredElementCollector(doc).OfClass(View):
        if not _is_supported_candidate(view):
            continue
        sheet = view_to_sheet.get(view.Id.IntegerValue)
        rows.append(ViewRow(doc, view, sheet))
    return rows


def scan_views(doc, views):
    """Wraps an arbitrary iterable of View elements (already known,
    e.g. from a sheet's viewports or the current selection) into
    ViewRow objects, skipping unsupported ones silently (the caller
    already knows what it asked for; this just applies the same
    candidacy rule uniformly)."""
    view_to_sheet = utils.build_view_to_sheet_map(doc)
    rows = []
    seen_ids = set()
    for view in views:
        if not _is_supported_candidate(view):
            continue
        if view.Id.IntegerValue in seen_ids:
            continue
        seen_ids.add(view.Id.IntegerValue)
        sheet = view_to_sheet.get(view.Id.IntegerValue)
        rows.append(ViewRow(doc, view, sheet))
    return rows


def views_on_sheet(doc, sheet):
    views = []
    try:
        for vp_id in sheet.GetAllViewports():
            vp = doc.GetElement(vp_id)
            if vp is None:
                continue
            view = doc.GetElement(vp.ViewId)
            if view is not None:
                views.append(view)
    except Exception:
        pass
    return views


# ==========================================================================
# Crop actions - each one operates on a SINGLE view and either succeeds,
# returns a clear skip reason, or raises (caller records the exception
# text) - never crashes the batch loop. All must run inside a
# Transaction (unlike DeeRelink's LoadFrom, these are normal parameter/
# property writes).
# ==========================================================================
def set_crop_active(view, value):
    try:
        if bool(view.CropBoxActive) == value:
            return True, "Already {0}".format("enabled" if value else "disabled")
        view.CropBoxActive = value
        return True, "Set to {0}".format("enabled" if value else "disabled")
    except Exception as e:
        return False, str(e)


def set_crop_visible(view, value):
    try:
        if bool(view.CropBoxVisible) == value:
            return True, "Already {0}".format("visible" if value else "hidden")
        view.CropBoxVisible = value
        return True, "Set to {0}".format("visible" if value else "hidden")
    except Exception as e:
        return False, str(e)


def toggle_crop_visible(view):
    try:
        new_value = not bool(view.CropBoxVisible)
        view.CropBoxVisible = new_value
        return True, "Toggled to {0}".format("visible" if new_value else "hidden")
    except Exception as e:
        return False, str(e)


def set_annotation_crop(view, value):
    ok = utils.set_bool_param_by_name(view, _ANNOTATION_CROP_PARAM_NAME, value)
    if ok:
        return True, "Set to {0}".format("enabled" if value else "disabled")
    return False, "'{0}' parameter not found or read-only on this view".format(_ANNOTATION_CROP_PARAM_NAME)


def apply_crop_offset(view, top=0.0, bottom=0.0, left=0.0, right=0.0):
    """Nudges the view's existing crop box outward (positive values) or
    inward (negative values) on each side, in internal (feet) units -
    a relative offset, not an absolute size, so it behaves consistently
    regardless of each view's current crop size."""
    try:
        bbox = view.CropBox
        if bbox is None:
            return False, "View has no crop box"
        new_box = BoundingBoxXYZ()
        new_box.Transform = bbox.Transform
        new_box.Min = XYZ(bbox.Min.X - left, bbox.Min.Y - bottom, bbox.Min.Z)
        new_box.Max = XYZ(bbox.Max.X + right, bbox.Max.Y + top, bbox.Max.Z)
        view.CropBox = new_box
        return True, "Offset applied (T{0:.3f} B{1:.3f} L{2:.3f} R{3:.3f})".format(top, bottom, left, right)
    except Exception as e:
        return False, str(e)


def set_scope_box(view, scope_box_id):
    p = utils.get_builtin_param(view, BuiltInParameter.VIEWER_VOLUME_OF_INTEREST_CROP)
    if p is None:
        return False, "Scope Box parameter not found on this view"
    if p.IsReadOnly:
        return False, "Scope Box parameter is read-only on this view"
    try:
        p.Set(scope_box_id if scope_box_id is not None else ElementId.InvalidElementId)
        return True, ("Removed" if scope_box_id is None else "Assigned")
    except Exception as e:
        return False, str(e)


def list_scope_boxes(doc):
    """{name: ElementId} for every Scope Box in the project."""
    boxes = {}
    try:
        for el in FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_VolumeOfInterest).WhereElementIsNotElementType():
            name = utils.read_name(el)
            if name:
                boxes[name] = el.Id
    except Exception:
        pass
    return boxes


# ==========================================================================
# Report
# ==========================================================================
class ReportRow(object):
    def __init__(self, view_name, view_type, sheet_text, action, ok, detail):
        self.view_name = view_name
        self.view_type = view_type
        self.sheet_text = sheet_text
        self.action = action
        self.result = "OK" if ok else "Skipped/Failed"
        self.detail = detail

    def to_list(self):
        return [self.view_name, self.view_type, self.sheet_text, self.action, self.result, self.detail]

    def status_tag(self):
        return "ok" if self.result == "OK" else "fail"


def _report_html(action_name, rows, elapsed_seconds):
    ok_count = sum(1 for r in rows if r.result == "OK")
    fail_count = len(rows) - ok_count
    html = [
        '<h2 style="font-family:sans-serif;color:#ddd;">DeeLazy - View Cropping: {0}</h2>'.format(action_name),
        '<p style="color:#ddd;">{0} view(s) processed - {1} succeeded, {2} skipped/failed - {3:.1f}s.</p>'.format(
            len(rows), ok_count, fail_count, elapsed_seconds),
    ]
    for r in rows:
        bg = "#2e7d32" if r.result == "OK" else "#c62828"
        icon = "&#10003;" if r.result == "OK" else "&#10007;"
        html.append(
            '<div style="padding:4px 10px;margin:2px 0;background:{0};color:#fff;'
            'border-radius:4px;font-family:monospace;font-size:12px;">'
            '{1}&nbsp; <b>{2}</b> ({3}) &mdash; {4}</div>'.format(bg, icon, r.view_name, r.view_type, r.detail))
    output.print_html("".join(html))


def export_report(path, title, rows):
    xlsx_rows = [(r.to_list(), r.status_tag()) for r in rows]
    xlsx_writer.write_themed_xlsx(path, title, _REPORT_HEADERS, _REPORT_COL_WIDTHS, xlsx_rows)


class _SafeProgress(object):
    """forms.ProgressBar tries to set Window.TaskbarItemInfo on its host
    window - a genuine WPF-level bug (not this extension's code):
    Window.TaskbarItemInfo throws NotImplementedException whenever the
    underlying ITaskbarList::HrInit COM call fails, which is documented
    to happen specifically under Remote Desktop/Terminal Services or a
    custom shell without a taskbar (live-confirmed in DeeSheetLinks).

    Wraps the real forms.ProgressBar and falls back to running with NO
    progress UI at all if entering it fails, so the tool degrades
    gracefully under RDP instead of crashing - everyone else still gets
    the real progress bar exactly as before. `pb.update_progress(...)`/
    `pb.cancelled` are safe no-ops in the fallback case, so callers never
    need an extra branch."""
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

    @property
    def cancelled(self):
        return False

    def update_progress(self, i, total):
        pass


# ==========================================================================
# Window - UI wiring only; all real work happens in the plain functions
# above (separate business logic from UI, per spec's code requirements)
# ==========================================================================
class ViewCroppingWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, uiapp):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.uiapp = uiapp
        self.uidoc = uiapp.ActiveUIDocument
        self.doc = self.uidoc.Document
        self._all_rows = []
        self._rows = []
        self._report_rows = []

        unit_abbr = utils.unit_abbreviation(self.doc)
        self.offset_unit_tb.Text = unit_abbr
        self.offset_unit_tb0.Text = unit_abbr
        self._log("Ready. Load views using a Selection button on the left, then set Filters if needed.")

    # ---------------- logging/status ----------------
    def _log(self, message):
        self.status_tb.Text = message

    # ---------------- Selection (left panel) ----------------
    def load_current_sheet_click(self, sender, args):
        active = self.doc.ActiveView
        if not isinstance(active, ViewSheet):
            forms.alert("The active view is not a Sheet.")
            return
        views = views_on_sheet(self.doc, active)
        self._set_all_rows(scan_views(self.doc, views))

    def load_selected_sheets_click(self, sender, args):
        sheets = utils.all_sheets(self.doc)
        if not sheets:
            forms.alert("No sheets found in this project.")
            return
        labels = dict((utils.sheet_label(s), s) for s in sheets)
        picked = forms.SelectFromList.show(
            sorted(labels.keys()), title="Select Sheets", multiselect=True, button_name="Load Views")
        if not picked:
            return
        views = []
        for label in picked:
            views.extend(views_on_sheet(self.doc, labels[label]))
        self._set_all_rows(scan_views(self.doc, views))

    def load_entire_project_click(self, sender, args):
        with _SafeProgress(title="DeeLazy - View Cropping - scanning project...", indeterminate=True):
            rows = scan_entire_project(self.doc)
        self._set_all_rows(rows)

    def load_current_selection_click(self, sender, args):
        """Covers both "Browser Selection" and "Views selected manually"
        from the original spec - Revit's Selection API doesn't
        distinguish where a selection came from (canvas vs Project
        Browser), so there is only one real mechanism here, not two."""
        try:
            sel_ids = list(self.uidoc.Selection.GetElementIds())
        except Exception:
            forms.alert("Could not read the current selection.")
            return
        views = [self.doc.GetElement(eid) for eid in sel_ids]
        views = [v for v in views if isinstance(v, View)]
        if not views:
            forms.alert("Nothing selected - select view(s) in the Project Browser, "
                        "or a Viewport on a sheet, then try again.")
            return
        self._set_all_rows(scan_views(self.doc, views))

    def _set_all_rows(self, rows):
        self._all_rows = rows
        self._populate_filter_combos()
        self.clear_filters_click(None, None)

    # ---------------- Filters (left panel) ----------------
    def _populate_filter_combos(self):
        types = sorted(set(r.view_type for r in self._all_rows))
        self.filter_type_cb.ItemsSource = ["(Any)"] + types
        self.filter_type_cb.SelectedIndex = 0

        disciplines = sorted(set(r.discipline for r in self._all_rows if r.discipline))
        self.filter_discipline_cb.ItemsSource = ["(Any)"] + disciplines
        self.filter_discipline_cb.SelectedIndex = 0

        templates = sorted(set(r.view_template for r in self._all_rows))
        self.filter_template_cb.ItemsSource = ["(Any)"] + templates
        self.filter_template_cb.SelectedIndex = 0

    def apply_filters_click(self, sender, args):
        rows = self._all_rows

        picked_type = self.filter_type_cb.SelectedItem
        if picked_type and picked_type != "(Any)":
            rows = [r for r in rows if r.view_type == picked_type]

        picked_discipline = self.filter_discipline_cb.SelectedItem
        if picked_discipline and picked_discipline != "(Any)":
            rows = [r for r in rows if r.discipline == picked_discipline]

        picked_template = self.filter_template_cb.SelectedItem
        if picked_template and picked_template != "(Any)":
            rows = [r for r in rows if r.view_template == picked_template]

        name_sheet_text = (self.filter_name_sheet_tb.Text or "").strip().lower()
        if name_sheet_text:
            rows = [r for r in rows if name_sheet_text in r.name.lower()
                    or name_sheet_text in r.sheet_text.lower()]

        if bool(self.filter_dependent_only_cb.IsChecked):
            rows = [r for r in rows if r.is_dependent]
        if bool(self.filter_parent_only_cb.IsChecked):
            rows = [r for r in rows if not r.is_dependent]
        if bool(self.filter_crop_enabled_cb.IsChecked):
            rows = [r for r in rows if r.crop_active]
        if bool(self.filter_crop_disabled_cb.IsChecked):
            rows = [r for r in rows if r.crop_active is False]
        if bool(self.filter_annotation_crop_cb.IsChecked):
            rows = [r for r in rows if r.annotation_crop]

        self._rows = rows
        self._refresh_grid()

    def clear_filters_click(self, sender, args):
        try:
            self.filter_type_cb.SelectedIndex = 0
            self.filter_discipline_cb.SelectedIndex = 0
            self.filter_template_cb.SelectedIndex = 0
            self.filter_name_sheet_tb.Text = ""
            self.filter_dependent_only_cb.IsChecked = False
            self.filter_parent_only_cb.IsChecked = False
            self.filter_crop_enabled_cb.IsChecked = False
            self.filter_crop_disabled_cb.IsChecked = False
            self.filter_annotation_crop_cb.IsChecked = False
        except Exception:
            pass
        self._rows = list(self._all_rows)
        self._refresh_grid()

    # ---------------- Middle panel ----------------
    def _visible_rows(self):
        text = (self.search_tb.Text or "").strip().lower()
        if not text:
            return self._rows
        return [r for r in self._rows if text in r.name.lower()]

    def _refresh_grid(self):
        visible = self._visible_rows()
        self.views_grid.ItemsSource = None
        self.views_grid.ItemsSource = visible
        self._log("{0} view(s) loaded, {1} after filters, {2} shown.".format(
            len(self._all_rows), len(self._rows), len(visible)))

    def search_text_changed(self, sender, args):
        self._refresh_grid()

    def select_all_click(self, sender, args):
        for r in self._visible_rows():
            r.selected = True
        self.views_grid.Items.Refresh()

    def select_none_click(self, sender, args):
        for r in self._visible_rows():
            r.selected = False
        self.views_grid.Items.Refresh()

    def invert_selection_click(self, sender, args):
        for r in self._visible_rows():
            r.selected = not r.selected
        self.views_grid.Items.Refresh()

    def _get_selected_rows(self):
        """Uses the full scanned set, not just the currently-visible/
        filtered one - a checked row keeps its checked state even if a
        later filter or search hides it, matching how most batch tools
        behave (no silently-forgotten selections)."""
        return [r for r in self._all_rows if r.selected]

    # ---------------- Right panel - Crop Actions ----------------
    def _run_action(self, action_name, selected, action_fn):
        """action_fn(view) -> (ok: bool, detail: str) for ONE view.
        Confirms the affected count first (per explicit user choice -
        immediate apply with a count confirmation, not a staged
        preview), runs inside one Transaction with a progress bar,
        records a ReportRow per view, shows the HTML report, and
        refreshes the grid from ground truth afterward."""
        if not selected:
            forms.alert("Check at least one view first.")
            return
        if not forms.alert(
                "This will run '{0}' on {1} view(s). Continue?".format(action_name, len(selected)),
                yes=True, no=True):
            return

        start = time.time()
        report_rows = []
        t = Transaction(self.doc, "DeeLazy - View Cropping - {0}".format(action_name))
        t.Start()
        try:
            with progsvc.DeeWProgressService("DeeLazy - View Cropping", len(selected)) as prog:
                for row in selected:
                    if prog.cancelled:
                        break
                    prog.step(row.name, action_name)
                    try:
                        ok, detail = action_fn(row.view)
                    except Exception as e:
                        ok, detail = False, str(e)
                    report_rows.append(ReportRow(row.name, row.view_type, row.sheet_text, action_name, ok, detail))
                    row.refresh_crop_state()
                    prog.finish_file("success" if ok else "failed")
        finally:
            t.Commit()

        elapsed = time.time() - start
        self._report_rows = report_rows
        self.report_grid.ItemsSource = None
        self.report_grid.ItemsSource = report_rows
        self.views_grid.Items.Refresh()
        _report_html(action_name, report_rows, elapsed)
        self._log("'{0}' done - {1} view(s), {2:.1f}s. See the report tab/window.".format(
            action_name, len(selected), elapsed))

    def crop_active_enable_click(self, sender, args):
        self._run_action("Enable Crop Region", self._get_selected_rows(),
                          lambda v: set_crop_active(v, True))

    def crop_active_disable_click(self, sender, args):
        self._run_action("Disable Crop Region", self._get_selected_rows(),
                          lambda v: set_crop_active(v, False))

    def crop_visible_show_click(self, sender, args):
        self._run_action("Show Crop Region", self._get_selected_rows(),
                          lambda v: set_crop_visible(v, True))

    def crop_visible_hide_click(self, sender, args):
        self._run_action("Hide Crop Region", self._get_selected_rows(),
                          lambda v: set_crop_visible(v, False))

    def crop_visible_toggle_click(self, sender, args):
        self._run_action("Toggle Crop Region Visibility", self._get_selected_rows(),
                          lambda v: toggle_crop_visible(v))

    def annotation_crop_enable_click(self, sender, args):
        self._run_action("Enable Annotation Crop", self._get_selected_rows(),
                          lambda v: set_annotation_crop(v, True))

    def annotation_crop_disable_click(self, sender, args):
        self._run_action("Disable Annotation Crop", self._get_selected_rows(),
                          lambda v: set_annotation_crop(v, False))

    def apply_offset_click(self, sender, args):
        selected = self._get_selected_rows()
        if not selected:
            forms.alert("Check at least one view first.")
            return
        if bool(self.offset_uniform_cb.IsChecked):
            val = utils.display_to_internal(self.doc, utils.safe_float(self.offset_uniform_tb.Text, 0.0))
            top = bottom = left = right = val
        else:
            top = utils.display_to_internal(self.doc, utils.safe_float(self.offset_top_tb.Text, 0.0))
            bottom = utils.display_to_internal(self.doc, utils.safe_float(self.offset_bottom_tb.Text, 0.0))
            left = utils.display_to_internal(self.doc, utils.safe_float(self.offset_left_tb.Text, 0.0))
            right = utils.display_to_internal(self.doc, utils.safe_float(self.offset_right_tb.Text, 0.0))
        self._run_action("Apply Crop Offset", selected,
                          lambda v: apply_crop_offset(v, top, bottom, left, right))

    def assign_scope_box_click(self, sender, args):
        selected = self._get_selected_rows()
        if not selected:
            forms.alert("Check at least one view first.")
            return
        boxes = list_scope_boxes(self.doc)
        if not boxes:
            forms.alert("No Scope Boxes found in this project.")
            return
        picked_name = forms.SelectFromList.show(sorted(boxes.keys()), title="Select Scope Box", button_name="Assign")
        if not picked_name:
            return
        scope_id = boxes[picked_name]
        self._run_action("Assign Scope Box '{0}'".format(picked_name), selected,
                          lambda v: set_scope_box(v, scope_id))

    def remove_scope_box_click(self, sender, args):
        self._run_action("Remove Scope Box", self._get_selected_rows(),
                          lambda v: set_scope_box(v, None))

    # ---------------- Report / Close ----------------
    def export_report_click(self, sender, args):
        if not self._report_rows:
            forms.alert("Run an action first - nothing to export yet.")
            return
        dlg = SaveFileDialog()
        dlg.Filter = "Excel Workbook (*.xlsx)|*.xlsx"
        dlg.FileName = "DeeLazy_ViewCropping_Report.xlsx"
        if dlg.ShowDialog() != DialogResult.OK:
            return
        try:
            export_report(dlg.FileName, "DeeLazy - View Cropping Report", self._report_rows)
        except Exception as e:
            forms.alert("Could not export report: {0}".format(e))
            return
        MessageBox.Show("Report exported to:\n{0}".format(dlg.FileName), "DeeLazy - View Cropping")

    def close_click(self, sender, args):
        self.Close()


# ==========================================================================
# Launch entry point (called by the DeeLazy home window)
# ==========================================================================
def launch(uiapp):
    if uiapp.ActiveUIDocument is None:
        forms.alert("Open a Revit project first.")
        return
    window = ViewCroppingWindow(_XAML_FILE, uiapp)
    window.ShowDialog()


TOOL_INFO = {
    "id": "view_cropping",
    "title": "View Cropping",
    "description": "Batch-edit Crop Region active/visibility, Annotation Crop, Crop Offset, and Scope Box across many views at once.",
    "launch": launch,
}
