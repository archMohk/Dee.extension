# -*- coding: utf-8 -*-
"""
DeeLazy - DeeViewSelect module
Sheet-driven bulk editing of views. Pick sheets (with a filter), load the
views actually placed on those sheets, pick the ones you want (with a
second filter), then run any of the supported actions on them - or just
select their viewports in Revit and go do the work by hand.

Why sheet-driven: every other view tool in this extension selects views
by their own properties. The question "everything on these drawings" is a
different question, and it is the one people actually ask during a sheet
set review.

--------------------------------------------------------------------
One row per PLACEMENT, not per view
--------------------------------------------------------------------
A legend or schedule is routinely placed on many sheets, so the view list
holds one row per viewport. That is what makes viewport-level actions
(viewport type, remove from sheet) addressable at all. View-LEVEL actions
de-duplicate by view id before running and report how many rows collapsed,
so a legend on 8 sheets is never renamed 8 times.

--------------------------------------------------------------------
Revit API facts relied on here (verified before writing, not guessed)
--------------------------------------------------------------------
- View.DetailLevel / DisplayStyle / Discipline / ViewTemplateId /
  CropBoxActive / CropBoxVisible / Scale - all get AND set.
- DisplayStyle has 10 members: Wireframe, HLR, Shading, ShadingWithEdges,
  FlatColors, Realistic, RealisticWithEdges, Rendering, Raytrace,
  Undefined. Only the ones a real view can be set to are offered.
- ViewDiscipline has 6: Architectural, Structural, Mechanical,
  Electrical, Plumbing, Coordination.
- BuiltInParameter.VIEW_DESCRIPTION IS "Title on Sheet", and it lives on
  the VIEW, not on the Viewport.
- Viewport.ChangeTypeId(ElementId) changes the viewport's title type.
- uidoc.Selection.SetElementIds(List[ElementId]) - already used from
  inside a modal DeeLazy window by dee_sselect, so this is a proven path
  here rather than a new risk.
- Crop toggles reuse view_cropping's own set_crop_active /
  set_crop_visible rather than reimplementing them, and Annotation Crop
  goes through utils.set_bool_param_by_name("Annotation Crop") for the
  reason view_cropping documents: there is no direct bool property for
  it, and the BuiltInParameter name is not reliable across versions.

--------------------------------------------------------------------
Template-controlled settings are verified, never assumed
--------------------------------------------------------------------
Setting a property a view template controls does not always raise - it
can simply not take. So every display action READS THE VALUE BACK after
writing and, if it did not change, reports that view by name along with
the template that blocked it. A blocked view is never counted as a
success.

--------------------------------------------------------------------
NEEDS LIVE-REVIT VERIFICATION (flagged, not silently assumed correct)
--------------------------------------------------------------------
- Which of Scale / DetailLevel / DisplayStyle / crop actually apply to a
  ViewSchedule or a Legend placed on a sheet. Each action is wrapped
  per-view and reports the real Revit error rather than assuming, but
  the exact behaviour per view type has not been exercised live.
- That "Annotation Crop" is the unlocalised parameter name in the user's
  Revit language (same caveat view_cropping already carries).
- Viewport.ChangeTypeId against a type collected from OST_Viewports -
  Revit may reject a type that is not valid for that particular
  viewport; the per-row try/except reports it.
"""
import os

import clr
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")

from System.Collections.Generic import List

from pyrevit import forms, script
import dee_branding

from Autodesk.Revit.DB import (
    FilteredElementCollector, BuiltInCategory, BuiltInParameter, ElementId,
    Transaction, View, Viewport, ScheduleSheetInstance,
    ViewDetailLevel, DisplayStyle, ViewDiscipline, StorageType,
)

import utils
from modules import view_cropping

output = script.get_output()

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "DeeViewSelect.xaml")

SCALES = [1, 2, 5, 10, 20, 25, 50, 75, 100, 125, 150, 200, 250, 500, 1000, 2000, 5000]

DETAIL_LEVELS = [("Coarse", "Coarse"), ("Medium", "Medium"), ("Fine", "Fine")]

# Rendering/Raytrace are deliberately not offered - they are render modes
# rather than a drafting display style, and Undefined is not a real choice.
DISPLAY_STYLES = [
    ("Wireframe", "Wireframe"),
    ("Hidden Line", "HLR"),
    ("Shaded", "Shading"),
    ("Shaded with Edges", "ShadingWithEdges"),
    ("Consistent Colors", "FlatColors"),
    ("Realistic", "Realistic"),
    ("Realistic with Edges", "RealisticWithEdges"),
]

DISCIPLINES = [
    ("Architectural", "Architectural"),
    ("Structural", "Structural"),
    ("Mechanical", "Mechanical"),
    ("Electrical", "Electrical"),
    ("Plumbing", "Plumbing"),
    ("Coordination", "Coordination"),
]

YES_NO = [("On", True), ("Off", False)]

RENAME_MODES = [
    ("Add prefix", "prefix"),
    ("Add suffix", "suffix"),
    ("Find and replace", "replace"),
]

# Actions that operate on the VIEW itself, so repeated placements of the
# same view must be de-duplicated before running.
VIEW_LEVEL_ACTIONS = set([
    "template_apply", "template_remove", "scale", "detail", "style",
    "discipline", "crop_active", "crop_visible", "anno_crop", "rename",
    "title_on_sheet", "param",
])


def matches(haystack, query):
    """AND-of-terms - the same filter rule DeeSuperLINK and DeeAssemb use,
    so every list in this extension filters the same way."""
    if not query:
        return True
    low = haystack.lower()
    return all(term in low for term in query.lower().split())


class SheetRow(object):
    """ElementIds and plain strings only - never a live Element. This window
    waits on the user between scanning and acting."""

    def __init__(self, sheet_id, number, name, view_count):
        self.sheet_id = sheet_id
        self.number = number
        self.name = name
        self.view_count = view_count
        self.selected = False

    @property
    def haystack(self):
        return "{0} {1}".format(self.number, self.name)


class ViewRow(object):
    def __init__(self, view_id, viewport_id, name, view_type, sheet_label,
                 scale_text, detail_text, template_name, is_schedule):
        self.view_id = view_id
        self.viewport_id = viewport_id      # None for a schedule instance
        self.name = name
        self.view_type = view_type
        self.sheet_label = sheet_label
        self.scale_text = scale_text
        self.detail_text = detail_text
        self.template_name = template_name
        self.is_schedule = is_schedule
        self.selected = False

    @property
    def haystack(self):
        return "{0} {1} {2}".format(self.name, self.view_type, self.sheet_label)


# ==========================================================================
# scanning
# ==========================================================================
def scan_sheets(doc):
    placements = _placements_by_sheet(doc)
    rows = []
    for sheet in utils.all_sheets(doc):
        try:
            rows.append(SheetRow(
                sheet_id=sheet.Id,
                number=sheet.SheetNumber,
                name=utils.read_name(sheet) or "(unnamed)",
                view_count=len(placements.get(sheet.Id.IntegerValue, []))))
        except Exception:
            continue
    rows.sort(key=lambda r: r.number.lower())
    return rows


def _placements_by_sheet(doc):
    """{sheet_id_int: [(view_id, viewport_id_or_None), ...]} in ONE pass per
    collector, matching this extension's "one collector pass, not one per
    item" convention."""
    out = {}
    try:
        for vp in FilteredElementCollector(doc).OfClass(Viewport):
            try:
                out.setdefault(vp.SheetId.IntegerValue, []).append((vp.ViewId, vp.Id))
            except Exception:
                continue
    except Exception:
        pass
    try:
        for ssi in FilteredElementCollector(doc).OfClass(ScheduleSheetInstance):
            try:
                if ssi.IsTitleblockRevisionSchedule:
                    continue
            except Exception:
                pass
            try:
                out.setdefault(ssi.SheetId.IntegerValue, []).append((ssi.ScheduleId, None))
            except Exception:
                continue
    except Exception:
        pass
    return out


def _describe(doc, view):
    """(scale_text, detail_text, template_name) read defensively - a schedule
    or legend legitimately has none of these."""
    try:
        scale_text = "1:{0}".format(view.Scale)
    except Exception:
        scale_text = "-"
    try:
        detail_text = str(view.DetailLevel)
    except Exception:
        detail_text = "-"
    template_name = "(none)"
    try:
        tid = view.ViewTemplateId
        if tid is not None and tid != ElementId.InvalidElementId:
            tpl = doc.GetElement(tid)
            template_name = utils.read_name(tpl) or "(unnamed)"
    except Exception:
        pass
    return scale_text, detail_text, template_name


def scan_views_on_sheets(doc, sheet_ids, progress_cb=None):
    placements = _placements_by_sheet(doc)
    rows = []
    total = max(len(sheet_ids), 1)
    for i, sheet_id in enumerate(sheet_ids):
        if progress_cb is not None and progress_cb(i, total):
            break
        sheet = doc.GetElement(sheet_id)
        if sheet is None:
            continue
        label = utils.sheet_label(sheet)
        for view_id, viewport_id in placements.get(sheet_id.IntegerValue, []):
            view = doc.GetElement(view_id)
            if view is None:
                continue
            try:
                if view.IsTemplate:
                    continue
            except Exception:
                pass
            scale_text, detail_text, template_name = _describe(doc, view)
            try:
                view_type = str(view.ViewType)
            except Exception:
                view_type = "-"
            rows.append(ViewRow(
                view_id=view_id,
                viewport_id=viewport_id,
                name=utils.read_name(view) or "(unnamed)",
                view_type=view_type,
                sheet_label=label,
                scale_text=scale_text,
                detail_text=detail_text,
                template_name=template_name,
                is_schedule=viewport_id is None))
    rows.sort(key=lambda r: (r.sheet_label.lower(), r.name.lower()))
    return rows


def collect_view_templates(doc):
    out = []
    for v in FilteredElementCollector(doc).OfClass(View):
        try:
            if v.IsTemplate:
                out.append((v.Id, utils.read_name(v) or "(unnamed)"))
        except Exception:
            continue
    out.sort(key=lambda t: t[1].lower())
    return out


def collect_viewport_types(doc):
    out = []
    try:
        for t in (FilteredElementCollector(doc)
                  .OfCategory(BuiltInCategory.OST_Viewports)
                  .WhereElementIsElementType()):
            try:
                out.append((t.Id, utils.read_name(t) or "(unnamed)"))
            except Exception:
                continue
    except Exception:
        pass
    out.sort(key=lambda t: t[1].lower())
    return out


# ==========================================================================
# actions - each takes ONE resolved view/viewport and returns (ok, detail).
# None of them ever raise: the batch loop must never die on one bad view.
# ==========================================================================
def _template_blocking(doc, view):
    try:
        tid = view.ViewTemplateId
        if tid is not None and tid != ElementId.InvalidElementId:
            return utils.read_name(doc.GetElement(tid)) or "(unnamed)"
    except Exception:
        pass
    return None


def _set_and_verify(doc, view, attr, value, shown):
    """Writes view.<attr> = value and READS IT BACK. A view template can
    absorb the write without raising, so an unverified 'success' would be a
    lie - this reports the blocking template by name instead."""
    try:
        current = getattr(view, attr)
    except Exception as e:
        return False, "not supported by this view type ({0})".format(e)
    if current == value:
        return True, "already {0}".format(shown)
    try:
        setattr(view, attr, value)
    except Exception as e:
        return False, str(e)
    try:
        if getattr(view, attr) == value:
            return True, "set to {0}".format(shown)
    except Exception:
        return True, "set to {0} (could not read back)".format(shown)
    tpl = _template_blocking(doc, view)
    if tpl:
        return False, "blocked by view template '{0}'".format(tpl)
    return False, "Revit did not accept the change"


def act_template_apply(doc, view, template_id):
    if template_id is None:
        return False, "no view template chosen"
    if view.Id == template_id:
        return False, "a view cannot be its own template"
    return _set_and_verify(doc, view, "ViewTemplateId", template_id,
                           utils.read_name(doc.GetElement(template_id)) or "template")


def act_template_remove(doc, view):
    return _set_and_verify(doc, view, "ViewTemplateId",
                           ElementId.InvalidElementId, "no template")


def act_scale(doc, view, scale):
    return _set_and_verify(doc, view, "Scale", int(scale), "1:{0}".format(scale))


def act_detail(doc, view, name):
    try:
        value = getattr(ViewDetailLevel, name)
    except AttributeError:
        return False, "detail level '{0}' not available in this Revit version".format(name)
    return _set_and_verify(doc, view, "DetailLevel", value, name)


def act_style(doc, view, member, shown):
    try:
        value = getattr(DisplayStyle, member)
    except AttributeError:
        return False, "visual style '{0}' not available in this Revit version".format(shown)
    return _set_and_verify(doc, view, "DisplayStyle", value, shown)


def act_discipline(doc, view, name):
    try:
        value = getattr(ViewDiscipline, name)
    except AttributeError:
        return False, "discipline '{0}' not available in this Revit version".format(name)
    return _set_and_verify(doc, view, "Discipline", value, name)


def act_crop_active(doc, view, value):
    return view_cropping.set_crop_active(view, value)


def act_crop_visible(doc, view, value):
    return view_cropping.set_crop_visible(view, value)


def act_anno_crop(doc, view, value):
    current = utils.get_bool_param_by_name(view, "Annotation Crop")
    if current is None:
        return False, "this view has no 'Annotation Crop' parameter"
    if bool(current) == bool(value):
        return True, "already {0}".format("on" if value else "off")
    try:
        if utils.set_bool_param_by_name(view, "Annotation Crop", value):
            return True, "set to {0}".format("on" if value else "off")
    except Exception as e:
        return False, str(e)
    tpl = _template_blocking(doc, view)
    if tpl:
        return False, "blocked by view template '{0}'".format(tpl)
    return False, "Revit did not accept the change"


def build_new_name(current, mode, find_text, replace_text):
    """Pure string logic - unit-tested at the bottom of this file."""
    if mode == "prefix":
        return "{0}{1}".format(find_text, current)
    if mode == "suffix":
        return "{0}{1}".format(current, find_text)
    if mode == "replace":
        if not find_text:
            return current
        return current.replace(find_text, replace_text)
    return current


def act_rename(doc, view, mode, find_text, replace_text):
    try:
        current = view.Name
    except Exception as e:
        return False, "could not read the current name ({0})".format(e)
    new_name = build_new_name(current, mode, find_text, replace_text)
    if new_name == current:
        return True, "unchanged"
    if not new_name.strip():
        return False, "that would leave an empty view name"
    try:
        view.Name = new_name
        return True, "renamed to '{0}'".format(new_name)
    except Exception as e:
        return False, "Revit rejected '{0}' ({1})".format(new_name, e)


def act_title_on_sheet(doc, view, text):
    try:
        p = view.get_Parameter(BuiltInParameter.VIEW_DESCRIPTION)
    except Exception:
        p = None
    if p is None:
        return False, "this view has no 'Title on Sheet' parameter"
    if p.IsReadOnly:
        tpl = _template_blocking(doc, view)
        return False, ("blocked by view template '{0}'".format(tpl) if tpl
                       else "'Title on Sheet' is read-only on this view")
    try:
        p.Set(text)
        return True, "set to '{0}'".format(text)
    except Exception as e:
        return False, str(e)


def act_param(doc, view, param_name, raw_value):
    """Writes any named parameter, coercing to whatever the parameter's own
    StorageType is rather than assuming text."""
    p = utils.find_param_by_name(view, param_name)
    if p is None:
        return False, "no parameter named '{0}' on this view".format(param_name)
    if p.IsReadOnly:
        tpl = _template_blocking(doc, view)
        return False, ("blocked by view template '{0}'".format(tpl) if tpl
                       else "'{0}' is read-only on this view".format(param_name))
    try:
        st = p.StorageType
        if st == StorageType.String:
            p.Set(raw_value)
        elif st == StorageType.Integer:
            text = raw_value.strip().lower()
            if text in ("yes", "true", "on", "1"):
                p.Set(1)
            elif text in ("no", "false", "off", "0"):
                p.Set(0)
            else:
                p.Set(int(float(text)))
        elif st == StorageType.Double:
            p.Set(float(raw_value))
        else:
            return False, "'{0}' holds an ElementId - not supported here".format(param_name)
        return True, "set to '{0}'".format(raw_value)
    except Exception as e:
        return False, str(e)


def act_viewport_type(doc, viewport_id, type_id):
    if viewport_id is None:
        return False, "a schedule has no viewport type"
    vp = doc.GetElement(viewport_id)
    if vp is None:
        return False, "viewport no longer exists"
    try:
        if vp.GetTypeId() == type_id:
            return True, "already that type"
    except Exception:
        pass
    try:
        vp.ChangeTypeId(type_id)
        return True, "viewport type changed"
    except Exception as e:
        return False, str(e)


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
# window
# ==========================================================================
class DeeViewSelectWindow(dee_branding.DeeBrandedWindow):
    # Must exist BEFORE the base class loads the XAML - loading it fires the
    # TextChanged/SelectionChanged handlers below, at which point no
    # instance attribute exists yet.
    _ready = False

    def __init__(self, xaml_file, uiapp):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.uiapp = uiapp
        self.uidoc = uiapp.ActiveUIDocument
        self.doc = self.uidoc.Document

        self._sheet_rows = []
        self._view_rows = []
        self._templates = []
        self._viewport_types = []

        with _SafeProgress(title="DeeViewSelect - scanning sheets...", indeterminate=True):
            self._sheet_rows = scan_sheets(self.doc)
            self._templates = collect_view_templates(self.doc)
            self._viewport_types = collect_viewport_types(self.doc)

        self._fill_combos()
        self._refresh_sheets_grid()

        self._ready = True
        self._update_summaries()

    # ---------------- setup ----------------
    def _fill_combos(self):
        self.template_cb.Items.Clear()
        for _tid, label in self._templates:
            self.template_cb.Items.Add(label)
        if self._templates:
            self.template_cb.SelectedIndex = 0

        self.scale_cb.Items.Clear()
        for s in SCALES:
            self.scale_cb.Items.Add("1:{0}".format(s))
        self.scale_cb.SelectedIndex = SCALES.index(100)

        for combo, entries in ((self.detail_cb, DETAIL_LEVELS),
                               (self.style_cb, DISPLAY_STYLES),
                               (self.discipline_cb, DISCIPLINES),
                               (self.rename_mode_cb, RENAME_MODES)):
            combo.Items.Clear()
            for label, _value in entries:
                combo.Items.Add(label)
            combo.SelectedIndex = 0

        for combo in (self.crop_active_cb, self.crop_visible_cb, self.anno_crop_cb):
            combo.Items.Clear()
            for label, _value in YES_NO:
                combo.Items.Add(label)
            combo.SelectedIndex = 0

        self.viewport_type_cb.Items.Clear()
        for _tid, label in self._viewport_types:
            self.viewport_type_cb.Items.Add(label)
        if self._viewport_types:
            self.viewport_type_cb.SelectedIndex = 0

        self.view_type_filter_cb.Items.Clear()
        self.view_type_filter_cb.Items.Add("(All types)")
        self.view_type_filter_cb.SelectedIndex = 0

    # ---------------- tab 1: sheets ----------------
    def _refresh_sheets_grid(self):
        query = ""
        try:
            query = self.sheet_search_tb.Text or ""
        except Exception:
            pass
        hide_empty = self.sheets_hide_empty_cb.IsChecked is True
        shown = [r for r in self._sheet_rows
                 if matches(r.haystack, query) and not (hide_empty and r.view_count == 0)]
        self.sheets_grid.ItemsSource = None
        self.sheets_grid.ItemsSource = shown
        return shown

    def sheet_search_changed(self, sender, args):
        if not self._ready:
            return
        self._refresh_sheets_grid()
        self._update_summaries()

    def sheets_all_click(self, sender, args):
        for r in self._sheet_rows:
            r.selected = True
        self._refresh_sheets_grid()
        self._update_summaries()

    def sheets_none_click(self, sender, args):
        for r in self._sheet_rows:
            r.selected = False
        self._refresh_sheets_grid()
        self._update_summaries()

    def sheets_shown_click(self, sender, args):
        for r in self._refresh_sheets_grid():
            r.selected = True
        self._refresh_sheets_grid()
        self._update_summaries()

    def load_views_click(self, sender, args):
        sheet_ids = [r.sheet_id for r in self._sheet_rows if r.selected]
        if not sheet_ids:
            forms.alert("Tick at least one sheet first.", title="DeeViewSelect")
            return
        with _SafeProgress(title="DeeViewSelect - loading views...", cancellable=True) as pb:
            def cb(i, total):
                try:
                    pb.update_progress(i, total)
                except Exception:
                    pass
                return pb.cancelled
            self._view_rows = scan_views_on_sheets(self.doc, sheet_ids, cb)

        for r in self._view_rows:
            r.selected = True

        types = sorted(set(r.view_type for r in self._view_rows))
        self.view_type_filter_cb.Items.Clear()
        self.view_type_filter_cb.Items.Add("(All types)")
        for t in types:
            self.view_type_filter_cb.Items.Add(t)
        self.view_type_filter_cb.SelectedIndex = 0

        self._refresh_views_grid()
        self._update_summaries()
        self.main_tabs.SelectedIndex = 1
        self.status_tb.Text = "Loaded {0} view placement(s) from {1} sheet(s). All are checked - untick what you do not want.".format(
            len(self._view_rows), len(sheet_ids))

    # ---------------- tab 2: views ----------------
    def _refresh_views_grid(self):
        query = ""
        try:
            query = self.view_search_tb.Text or ""
        except Exception:
            pass
        type_filter = None
        idx = self.view_type_filter_cb.SelectedIndex
        if idx > 0:
            type_filter = str(self.view_type_filter_cb.SelectedItem)
        shown = [r for r in self._view_rows
                 if matches(r.haystack, query)
                 and (type_filter is None or r.view_type == type_filter)]
        self.views_grid.ItemsSource = None
        self.views_grid.ItemsSource = shown
        return shown

    def view_search_changed(self, sender, args):
        if not self._ready:
            return
        self._refresh_views_grid()
        self._update_summaries()

    def view_type_filter_changed(self, sender, args):
        if not self._ready:
            return
        self._refresh_views_grid()
        self._update_summaries()

    def views_all_click(self, sender, args):
        for r in self._view_rows:
            r.selected = True
        self._refresh_views_grid()
        self._update_summaries()

    def views_none_click(self, sender, args):
        for r in self._view_rows:
            r.selected = False
        self._refresh_views_grid()
        self._update_summaries()

    def views_shown_click(self, sender, args):
        for r in self._refresh_views_grid():
            r.selected = True
        self._refresh_views_grid()
        self._update_summaries()

    # ---------------- shared ----------------
    def _selected_views(self):
        return [r for r in self._view_rows if r.selected]

    def _update_summaries(self):
        sheets_picked = len([r for r in self._sheet_rows if r.selected])
        self.sheets_summary_tb.Text = "{0} of {1} sheet(s) checked.".format(
            sheets_picked, len(self._sheet_rows))
        picked = self._selected_views()
        distinct = len(set(r.view_id.IntegerValue for r in picked))
        self.views_summary_tb.Text = "{0} of {1} placement(s) checked ({2} distinct view(s)).".format(
            len(picked), len(self._view_rows), distinct)
        self.action_target_tb.Text = (
            "Actions below run on {0} checked placement(s) - {1} distinct view(s).".format(
                len(picked), distinct))
        self.status_tb.Text = "{0} sheet(s) checked, {1} view placement(s) checked.".format(
            sheets_picked, len(picked))

    def _log(self, line):
        try:
            self.log_tb.AppendText(line + "\r\n")
            self.log_tb.ScrollToEnd()
        except Exception:
            pass

    def _combo_value(self, combo, entries):
        idx = combo.SelectedIndex
        if 0 <= idx < len(entries):
            return entries[idx][1]
        return None

    # ---------------- "just select" ----------------
    def select_only_click(self, sender, args):
        rows = self._selected_views()
        if not rows:
            forms.alert("Tick at least one view on the Views tab first.",
                        title="DeeViewSelect")
            return
        ids = List[ElementId]()
        schedules = 0
        for r in rows:
            if r.viewport_id is None:
                schedules += 1
                continue
            ids.Add(r.viewport_id)
        if ids.Count == 0:
            forms.alert("None of the checked rows is a viewport - a schedule placed "
                        "on a sheet cannot be selected this way.", title="DeeViewSelect")
            return
        try:
            self.uidoc.Selection.SetElementIds(ids)
        except Exception as e:
            forms.alert("Revit refused the selection:\n{0}".format(e), title="DeeViewSelect")
            return

        sheets = set(r.sheet_label for r in rows if r.viewport_id is not None)
        note = ""
        if len(sheets) > 1:
            note = (" They span {0} sheets, so only the ones on the sheet you have "
                    "open are visible.".format(len(sheets)))
        if schedules:
            note += " {0} schedule placement(s) were skipped.".format(schedules)
        self._log("Selected {0} viewport(s) in Revit.{1}".format(ids.Count, note))
        self.status_tb.Text = "Selected {0} viewport(s) in Revit.{1}".format(ids.Count, note)

        if self.close_after_select_cb.IsChecked is True:
            self.Close()

    # ---------------- the action dispatcher ----------------
    def apply_click(self, sender, args):
        try:
            action = str(sender.Tag)
        except Exception:
            return
        rows = self._selected_views()
        if not rows:
            forms.alert("Tick at least one view on the Views tab first.",
                        title="DeeViewSelect")
            return

        params, error = self._resolve_action_inputs(action)
        if error:
            forms.alert(error, title="DeeViewSelect")
            return

        if action in VIEW_LEVEL_ACTIONS:
            targets, collapsed = self._dedupe_by_view(rows)
        else:
            targets, collapsed = rows, 0

        self._run(action, targets, params, collapsed)

    def _dedupe_by_view(self, rows):
        """A view on several sheets must be edited once, not once per sheet."""
        seen = set()
        out = []
        for r in rows:
            key = r.view_id.IntegerValue
            if key in seen:
                continue
            seen.add(key)
            out.append(r)
        return out, len(rows) - len(out)

    def _resolve_action_inputs(self, action):
        """Returns (params_dict, error_message). Everything is resolved to
        plain values BEFORE the transaction opens."""
        if action == "template_apply":
            idx = self.template_cb.SelectedIndex
            if not (0 <= idx < len(self._templates)):
                return None, "Pick a view template first."
            return {"template_id": self._templates[idx][0]}, None
        if action == "scale":
            idx = self.scale_cb.SelectedIndex
            if not (0 <= idx < len(SCALES)):
                return None, "Pick a scale first."
            return {"scale": SCALES[idx]}, None
        if action == "detail":
            return {"name": self._combo_value(self.detail_cb, DETAIL_LEVELS)}, None
        if action == "style":
            idx = self.style_cb.SelectedIndex
            if not (0 <= idx < len(DISPLAY_STYLES)):
                return None, "Pick a visual style first."
            return {"member": DISPLAY_STYLES[idx][1],
                    "shown": DISPLAY_STYLES[idx][0]}, None
        if action == "discipline":
            return {"name": self._combo_value(self.discipline_cb, DISCIPLINES)}, None
        if action == "crop_active":
            return {"value": self._combo_value(self.crop_active_cb, YES_NO)}, None
        if action == "crop_visible":
            return {"value": self._combo_value(self.crop_visible_cb, YES_NO)}, None
        if action == "anno_crop":
            return {"value": self._combo_value(self.anno_crop_cb, YES_NO)}, None
        if action == "rename":
            mode = self._combo_value(self.rename_mode_cb, RENAME_MODES)
            find_text = self.rename_find_tb.Text or ""
            if not find_text:
                return None, ("Type the prefix, suffix, or text to find in the first "
                              "box next to Rename views.")
            return {"mode": mode, "find_text": find_text,
                    "replace_text": self.rename_replace_tb.Text or ""}, None
        if action == "title_on_sheet":
            return {"text": self.title_on_sheet_tb.Text or ""}, None
        if action == "param":
            name = (self.param_name_tb.Text or "").strip()
            if not name:
                return None, "Type the parameter name first."
            return {"param_name": name,
                    "raw_value": self.param_value_tb.Text or ""}, None
        if action == "viewport_type":
            idx = self.viewport_type_cb.SelectedIndex
            if not (0 <= idx < len(self._viewport_types)):
                return None, "Pick a viewport type first."
            return {"type_id": self._viewport_types[idx][0]}, None
        if action == "template_remove":
            return {}, None
        return None, "Unknown action '{0}'.".format(action)

    def _dispatch(self, action, row, params):
        """Resolves the ElementId to a live Element HERE, inside the loop -
        never held across the window's wait for the user."""
        if action == "viewport_type":
            return act_viewport_type(self.doc, row.viewport_id, params["type_id"])

        view = self.doc.GetElement(row.view_id)
        if view is None:
            return False, "this view no longer exists"

        if action == "template_apply":
            return act_template_apply(self.doc, view, params["template_id"])
        if action == "template_remove":
            return act_template_remove(self.doc, view)
        if action == "scale":
            return act_scale(self.doc, view, params["scale"])
        if action == "detail":
            return act_detail(self.doc, view, params["name"])
        if action == "style":
            return act_style(self.doc, view, params["member"], params["shown"])
        if action == "discipline":
            return act_discipline(self.doc, view, params["name"])
        if action == "crop_active":
            return act_crop_active(self.doc, view, params["value"])
        if action == "crop_visible":
            return act_crop_visible(self.doc, view, params["value"])
        if action == "anno_crop":
            return act_anno_crop(self.doc, view, params["value"])
        if action == "rename":
            return act_rename(self.doc, view, params["mode"],
                              params["find_text"], params["replace_text"])
        if action == "title_on_sheet":
            return act_title_on_sheet(self.doc, view, params["text"])
        if action == "param":
            return act_param(self.doc, view, params["param_name"], params["raw_value"])
        return False, "unknown action"

    def _run(self, action, targets, params, collapsed):
        self._log("=" * 70)
        self._log("{0} - {1} view(s)".format(action, len(targets)))
        if collapsed:
            self._log("({0} extra placement(s) of the same view collapsed - a view "
                      "is only edited once)".format(collapsed))

        ok = 0
        failures = []
        t = Transaction(self.doc, "DeeViewSelect - {0}".format(action))
        try:
            t.Start()
        except Exception as e:
            forms.alert("Could not start a transaction:\n{0}".format(e),
                        title="DeeViewSelect")
            return
        try:
            with _SafeProgress(title="DeeViewSelect - {0}...".format(action),
                                   cancellable=True) as pb:
                for i, row in enumerate(targets):
                    if pb.cancelled:
                        self._log("Cancelled by user after {0} of {1}.".format(i, len(targets)))
                        break
                    try:
                        success, detail = self._dispatch(action, row, params)
                    except Exception as e:
                        success, detail = False, str(e)
                    if success:
                        ok += 1
                        self._log("  OK   {0} [{1}] - {2}".format(
                            row.name, row.sheet_label, detail))
                    else:
                        failures.append((row.name, row.sheet_label, detail))
                        self._log("  SKIP {0} [{1}] - {2}".format(
                            row.name, row.sheet_label, detail))
                    try:
                        pb.update_progress(i + 1, len(targets))
                    except Exception:
                        pass
            t.Commit()
        except Exception as e:
            try:
                t.RollBack()
            except Exception:
                pass
            forms.alert("The action failed and was rolled back:\n{0}".format(e),
                        title="DeeViewSelect")
            self._log("ROLLED BACK: {0}".format(e))
            return

        self._log("Done. {0} changed, {1} skipped.".format(ok, len(failures)))
        self._refresh_view_row_values()
        self.status_tb.Text = "{0}: {1} changed, {2} skipped. See the Log tab.".format(
            action, ok, len(failures))
        if failures:
            self.main_tabs.SelectedIndex = 3

    def _refresh_view_row_values(self):
        """Re-reads the columns an action may have changed. Values are read
        fresh from the document rather than assumed from what was written."""
        for row in self._view_rows:
            view = self.doc.GetElement(row.view_id)
            if view is None:
                continue
            row.scale_text, row.detail_text, row.template_name = _describe(self.doc, view)
            row.name = utils.read_name(view) or "(unnamed)"
        self._refresh_views_grid()
        self._update_summaries()

    # ---------------- destructive ----------------
    def remove_from_sheets_click(self, sender, args):
        rows = [r for r in self._selected_views() if r.viewport_id is not None]
        if not rows:
            forms.alert("Tick at least one view placed via a viewport. A schedule "
                        "placement is not removed by this action.",
                        title="DeeViewSelect")
            return
        sheets = sorted(set(r.sheet_label for r in rows))
        proceed = forms.alert(
            "Remove {0} view placement(s) from {1} sheet(s)?\n\n"
            "This deletes the viewports. The views themselves stay in the project "
            "with all their settings, but their position on the sheet is lost and "
            "this window cannot undo it.".format(len(rows), len(sheets)),
            title="DeeViewSelect", yes=True, no=True)
        if not proceed:
            return

        self._log("=" * 70)
        self._log("Remove from sheets - {0} placement(s)".format(len(rows)))
        removed = 0
        t = Transaction(self.doc, "DeeViewSelect - remove views from sheets")
        try:
            t.Start()
            with _SafeProgress(title="DeeViewSelect - removing from sheets...") as pb:
                for i, row in enumerate(rows):
                    try:
                        self.doc.Delete(row.viewport_id)
                        removed += 1
                        self._log("  OK   {0} [{1}] - removed".format(row.name, row.sheet_label))
                    except Exception as e:
                        self._log("  SKIP {0} [{1}] - {2}".format(row.name, row.sheet_label, e))
                    try:
                        pb.update_progress(i + 1, len(rows))
                    except Exception:
                        pass
            t.Commit()
        except Exception as e:
            try:
                t.RollBack()
            except Exception:
                pass
            forms.alert("Removal failed and was rolled back:\n{0}".format(e),
                        title="DeeViewSelect")
            return

        self._log("Done. {0} removed.".format(removed))
        # Those placements are gone, so re-scan the sheet counts rather than
        # trusting the pre-delete list. The tick state is carried over by id
        # so the user does not lose their sheet selection.
        checked = set(r.sheet_id.IntegerValue for r in self._sheet_rows if r.selected)
        self._sheet_rows = scan_sheets(self.doc)
        for r in self._sheet_rows:
            r.selected = r.sheet_id.IntegerValue in checked
        gone = set(x.viewport_id.IntegerValue for x in rows)
        self._view_rows = [r for r in self._view_rows
                           if r.viewport_id is None or r.viewport_id.IntegerValue not in gone]
        self._refresh_sheets_grid()
        self._refresh_views_grid()
        self._update_summaries()
        self.status_tb.Text = "Removed {0} view placement(s) from {1} sheet(s).".format(
            removed, len(sheets))

    def close_click(self, sender, args):
        self.Close()


# ==========================================================================
# Launch entry point (called by the DeeLazy home window)
# ==========================================================================
def launch(uiapp):
    if uiapp.ActiveUIDocument is None:
        forms.alert("Open a Revit project first.")
        return
    window = DeeViewSelectWindow(_XAML_FILE, uiapp)
    window.ShowDialog()


TOOL_INFO = {
    "id": "dee_view_select",
    "title": "DeeViewSelect",
    "description": "Pick sheets, load the views placed on them, then bulk-edit those views - templates, scale, detail, crop, names - or just select their viewports in Revit.",
    "launch": launch,
}
