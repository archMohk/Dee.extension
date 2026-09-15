# -*- coding: utf-8 -*-
"""
DeeV.S.Dupl.
Batch-duplicates a chosen set of Sheets, Views and Schedules in one run.

Pick Items - every real Sheet/View/Schedule in the project, pre-checked
(uncheck what you don't want). Naming - a required Prefix/Batch Name, two
naming modes (Mode A: one shared batch counter across everything checked,
e.g. REV-1, REV-2...; Mode B: prefix + each item's own original name,
counter only used to break a real Revit name collision), numeric or
alphabetic counter style, and an optional global toggle to also duplicate
each checked item's View Template as an independent copy (off by default -
off means duplicates keep sharing the ORIGINAL template, exactly like
Revit's own Duplicate View). Every duplicate is tagged with a new shared
text parameter, "Dee Duplicate Batch", set to the Prefix/Batch Name, so the
batch can be grouped/isolated afterward via a Browser Organization rule in
Revit itself.

Sheet duplication has no direct Revit API/UI equivalent (Revit's own UI
does not offer "Duplicate Sheet" either), so it's assembled from parts:
ViewSheet.Create with the original's title block TYPE (never copies the
placed instance - a fresh one is auto-placed already), Viewport.Create/
ScheduleSheetInstance.Create to re-place duplicated views/schedules at
their original positions, and ElementTransformUtils.CopyElements for the
sheet's own drawn annotation only (lines/text/detail items/filled regions/
images - NOT viewports/title blocks/schedule graphics, which that API
cannot copy this way - see dee_draft_coper.py's _NOT_COPYABLE table, the
same finding this reuses). v1 deliberately does not clone Sheet Issue
Date/Approved-Checked-Drawn By/revision-on-sheet assignments - only
Number/Name/the tag parameter, the title block, placed content, and
sheet-owned annotation.

Sheet Number: the naming result is also appended to the original Sheet
Number (e.g. A-101 -> A-101-REV1), not just applied to the Name, so
duplicates sort recognisably by number like everything else in the
Project Browser - confirmed with the user rather than assumed.

NEEDS LIVE-REVIT VERIFICATION (novel ground, no precedent elsewhere in
this codebase):
- Sheet duplication end-to-end - each sub-step has precedent individually
  elsewhere in this repo, but never chained together in one transaction.
- In-document View Template duplication via ElementTransformUtils.
  CopyElements(doc, [template_id], doc, ...) - DeeVTemplate.py only ever
  exercises the cross-document form; the same-document self-copy is a
  well-known technique but untested in this codebase.
- BuiltInCategory.OST_Schedules as a bindable shared-parameter category -
  believed correct but unconfirmed live; degrades gracefully if it fails
  (Sheets/Views still get tagged, Schedules just don't, clearly reported).
"""
import os

from pyrevit import forms, script
import dee_branding
from Autodesk.Revit.DB import (
    FilteredElementCollector, BuiltInCategory, ElementId, Transaction,
    View, ViewSheet, ViewSchedule, ViewType, Viewport, ScheduleSheetInstance,
    ViewDuplicateOption, ElementTransformUtils, CopyPasteOptions, Transform,
    BuiltInParameter,
)
from System.Collections.Generic import List

import dee_shared_param_service
import dee_sheet_renamer_service as renamer

import dee_telemetry
dee_telemetry.check_access("DeeVSDupl")


output = script.get_output()
_XAML_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.xaml")
_TAG_PARAM_NAME = "Dee Duplicate Batch"

# Revit rejects these characters in a Sheet Number/Name and most other
# element names with a runtime exception - copied locally from
# dee_sheet_renamer_service._INVALID_NAME_CHARS (that module's own is a
# private constant) so a bad name shows up Invalid in Preview instead of
# failing partway through Run.
_INVALID_NAME_CHARS = set("\\:{}[]|;<>?`~")

_EXCLUDED_VIEW_TYPES = set()
for _vt_name in ("Internal", "ProjectBrowser", "SystemBrowser", "Undefined"):
    try:
        _EXCLUDED_VIEW_TYPES.add(getattr(ViewType, _vt_name))
    except Exception:
        pass


def _not_copyable_sheet_categories():
    """OST_Viewports/OST_TitleBlocks/OST_ScheduleGraphics as category id
    ints - built defensively (not a literal set of BuiltInCategory members)
    so a member missing in some Revit version degrades to "not flagged"
    rather than breaking this whole module on import. Same finding as
    dee_draft_coper.py's _NOT_COPYABLE table: CopyElements cannot copy a
    Viewport or a schedule placement, and copying a title block would
    stack a second one onto the new sheet's own auto-placed instance."""
    wanted = ["OST_Viewports", "OST_TitleBlocks", "OST_ScheduleGraphics"]
    out = set()
    for member in wanted:
        try:
            out.add(int(getattr(BuiltInCategory, member)))
        except Exception:
            continue
    return out


_NOT_COPYABLE = _not_copyable_sheet_categories()


# --------------------------------------------------------------------------
# Defensive reads
# --------------------------------------------------------------------------
def _read_name(element):
    """Element.Name can throw a bare "Name" exception on some element
    types in this Revit/IronPython combination - falls back to the
    Parameter system, same fix already proven in DeeSheet.pushbutton."""
    if element is None:
        return None
    try:
        n = element.Name
        if n:
            return n
    except Exception:
        pass
    try:
        p = element.get_Parameter(BuiltInParameter.SYMBOL_NAME_PARAM)
        if p is not None:
            val = p.AsString()
            if val:
                return val
    except Exception:
        pass
    try:
        p = element.get_Parameter(BuiltInParameter.ALL_MODEL_TYPE_NAME)
        if p is not None:
            val = p.AsString()
            if val:
                return val
    except Exception:
        pass
    return None


def _view_type_text(view):
    try:
        return str(view.ViewType)
    except Exception:
        return "(unknown)"


def _has_invalid_chars(text):
    return any(c in _INVALID_NAME_CHARS for c in (text or ""))


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------
class PickerRow(object):
    def __init__(self, element, kind, number, name, type_label, has_template):
        self.element = element
        self.kind = kind  # "Sheet" / "View" / "Schedule"
        self.number = number
        self.name = name
        self.type_label = type_label
        self.has_template = has_template
        self.selected = True


class PreviewRow(object):
    def __init__(self, picker_row):
        self.picker_row = picker_row
        self.kind = picker_row.kind
        self.old_number = picker_row.number
        self.old_name = picker_row.name
        self.new_number = picker_row.number
        self.new_name = picker_row.name
        self.status = ""

    @property
    def old_text(self):
        if self.kind == "Sheet":
            return u"{0} - {1}".format(self.old_number, self.old_name)
        return self.old_name

    @property
    def new_text(self):
        if self.kind == "Sheet":
            return u"{0} - {1}".format(self.new_number, self.new_name)
        return self.new_name


class ResultRow(object):
    def __init__(self, ok, kind, original, new_label, detail):
        self.ok = ok
        self.kind = kind
        self.original = original
        self.new_label = new_label
        self.status_text = "OK" if ok else "FAILED"
        self.detail = detail


# --------------------------------------------------------------------------
# Collection - one FilteredElementCollector pass per kind, defensive
# per-item try/except (matches dee_sheet_renamer_service.scan()'s style)
# --------------------------------------------------------------------------
def _collect_sheets(doc):
    rows = []
    for sh in FilteredElementCollector(doc).OfClass(ViewSheet):
        try:
            if getattr(sh, "IsPlaceholder", False):
                continue
            rows.append(PickerRow(sh, "Sheet", sh.SheetNumber or "",
                                   _read_name(sh) or "", "Sheet", False))
        except Exception:
            continue
    rows.sort(key=lambda r: r.number)
    return rows


def _collect_views(doc):
    rows = []
    for v in FilteredElementCollector(doc).OfClass(View).WhereElementIsNotElementType():
        try:
            if v.IsTemplate:
                continue
            if isinstance(v, ViewSheet) or isinstance(v, ViewSchedule):
                continue
            if v.ViewType in _EXCLUDED_VIEW_TYPES:
                continue
            has_tmpl = False
            try:
                has_tmpl = v.ViewTemplateId != ElementId.InvalidElementId
            except Exception:
                has_tmpl = False
            rows.append(PickerRow(v, "View", "", _read_name(v) or "",
                                   _view_type_text(v), has_tmpl))
        except Exception:
            continue
    rows.sort(key=lambda r: r.name)
    return rows


def _collect_schedules(doc):
    rows = []
    for s in FilteredElementCollector(doc).OfClass(ViewSchedule):
        try:
            if getattr(s, "IsTitleblockRevisionSchedule", False):
                continue
        except Exception:
            pass
        try:
            rows.append(PickerRow(s, "Schedule", "", _read_name(s) or "", "Schedule", False))
        except Exception:
            continue
    rows.sort(key=lambda r: r.name)
    return rows


def _all_taken_names(doc):
    """Every current View/ViewSheet/ViewSchedule name in one pool - View
    covers all three (they're all View subclasses). Used as the collision
    pool for Mode B's tie-breaker; conservative in the safe direction
    (may flag a rename as colliding in a case Revit would actually allow,
    never the reverse)."""
    names = set()
    for v in FilteredElementCollector(doc).OfClass(View):
        try:
            n = _read_name(v)
            if n:
                names.add(n)
        except Exception:
            continue
    return names


def _all_taken_numbers(doc):
    numbers = set()
    for sh in FilteredElementCollector(doc).OfClass(ViewSheet):
        try:
            if sh.SheetNumber:
                numbers.add(sh.SheetNumber)
        except Exception:
            continue
    return numbers


def _placements_by_sheet(doc):
    """{sheet_id_int: [Viewport, ...]}, {sheet_id_int: [ScheduleSheetInstance, ...]}
    in one pass per collector over the whole document, matching this
    extension's "one collector pass, not one per item" convention (see
    dee_view_select.py's _placements_by_sheet)."""
    vp_by_sheet = {}
    for vp in FilteredElementCollector(doc).OfClass(Viewport):
        try:
            vp_by_sheet.setdefault(vp.SheetId.IntegerValue, []).append(vp)
        except Exception:
            continue
    ssi_by_sheet = {}
    for ssi in FilteredElementCollector(doc).OfClass(ScheduleSheetInstance):
        try:
            if ssi.IsTitleblockRevisionSchedule:
                continue
        except Exception:
            pass
        try:
            ssi_by_sheet.setdefault(ssi.SheetId.IntegerValue, []).append(ssi)
        except Exception:
            continue
    return vp_by_sheet, ssi_by_sheet


def _sheet_owned_annotation_ids(doc, sheet_id):
    """ElementIds of the sheet's OWN drawn annotation only - ViewSpecific
    elements whose OwnerViewId is this sheet, excluding viewports/title
    blocks/schedule graphics (see _NOT_COPYABLE)."""
    ids = []
    for el in FilteredElementCollector(doc, sheet_id).WhereElementIsNotElementType():
        try:
            if not el.ViewSpecific:
                continue
            if el.OwnerViewId != sheet_id:
                continue
            cat = el.Category
            if cat is not None and cat.Id.IntegerValue in _NOT_COPYABLE:
                continue
            ids.append(el.Id)
        except Exception:
            continue
    return ids


def _read_sheet_titleblock_type_id(doc, sheet):
    """The TYPE of the title block already placed on `sheet` - precedent
    at DeeSheet.pushbutton/script.py and DeeAligner.pushbutton/script.py.
    Never copies the placed INSTANCE - ViewSheet.Create auto-places a
    fresh one of the same type on the new sheet already."""
    try:
        tbs = list(FilteredElementCollector(doc, sheet.Id)
                   .OfCategory(BuiltInCategory.OST_TitleBlocks)
                   .WhereElementIsNotElementType())
    except Exception:
        tbs = []
    if not tbs:
        return None
    try:
        return tbs[0].GetTypeId()
    except Exception:
        return None


# --------------------------------------------------------------------------
# Naming engine - local to this tool (first tool needing mixed-kind /
# dual-mode naming; see lib/dee_sheet_renamer_service.py for the reused
# render_template/sequence_token pieces, and _to_alpha copied locally
# since dee_sheet_renamer_service._to_alpha is that module's own private
# helper, not meant for cross-module import)
# --------------------------------------------------------------------------
def _to_alpha(n):
    n = max(1, int(n))
    letters = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _tie_breaker(n, is_alpha):
    return _to_alpha(n) if is_alpha else str(n)


def _generate_mode_a(preview_rows, prefix, separator, pad, is_alpha):
    seq_tok = renamer.sequence_token(pad=pad, letters=is_alpha)
    template = u"{0}{1}{2}".format(prefix, separator, seq_tok)
    for i, row in enumerate(preview_rows, start=1):
        ctx = {"sheet": row.picker_row.element, "original_number": row.old_number,
               "original_name": row.old_name, "serial_value": i}
        row.new_name = renamer.render_template(template, ctx)


def _generate_mode_b(preview_rows, prefix, separator, is_alpha, taken_names):
    taken = set(taken_names)
    for row in preview_rows:
        base = prefix + row.old_name
        candidate = base
        n = 1
        while candidate in taken:
            n += 1
            candidate = u"{0}{1}{2}".format(base, separator, _tie_breaker(n, is_alpha))
        taken.add(candidate)
        row.new_name = candidate


def _assign_sheet_numbers(preview_rows, taken_numbers):
    """Applies the naming result to the Sheet Number too (confirmed with
    the user), not just the Name - original_number + "-" + new_name, with
    the same collision-retry as Mode B whenever that exact number is
    already taken."""
    taken = set(taken_numbers)
    for row in preview_rows:
        if row.kind != "Sheet":
            continue
        base = u"{0}-{1}".format(row.old_number, row.new_name)
        candidate = base
        n = 1
        while candidate in taken:
            n += 1
            candidate = u"{0}-{1}".format(base, n)
        taken.add(candidate)
        row.new_number = candidate


def _compute_statuses(preview_rows):
    seen_names = {}
    seen_numbers = {}
    for r in preview_rows:
        seen_names.setdefault(r.new_name, []).append(r)
        if r.kind == "Sheet":
            seen_numbers.setdefault(r.new_number, []).append(r)

    for r in preview_rows:
        if not (r.new_name or "").strip():
            r.status = "Empty"
            continue
        if r.kind == "Sheet" and not (r.new_number or "").strip():
            r.status = "Empty"
            continue
        if _has_invalid_chars(r.new_name) or (r.kind == "Sheet" and _has_invalid_chars(r.new_number)):
            r.status = "Invalid"
            continue
        if len(seen_names.get(r.new_name, [])) > 1:
            r.status = "Duplicate"
            continue
        if r.kind == "Sheet" and len(seen_numbers.get(r.new_number, [])) > 1:
            r.status = "Duplicate"
            continue
        r.status = "Ready"


# --------------------------------------------------------------------------
# Shared parameter (Phase 1 - own transactions, committed before anything
# else runs)
# --------------------------------------------------------------------------
def _ensure_tag_parameter(doc):
    warnings = []
    for bic, label in ((BuiltInCategory.OST_Sheets, "Sheets"),
                        (BuiltInCategory.OST_Views, "Views"),
                        (BuiltInCategory.OST_Schedules, "Schedules")):
        try:
            res = dee_shared_param_service.ensure_shared_parameters(
                doc, doc.Application, [_TAG_PARAM_NAME], bic, label,
                transaction_name="DeeVSDupl - Create/Extend Shared Parameter ({0})".format(label))
            ok, detail = res.get(_TAG_PARAM_NAME, (False, "no result"))
        except Exception as e:
            ok, detail = False, str(e)
        if not ok:
            warnings.append(u"{0}: {1}".format(label, detail))
    return warnings


def _set_tag(element, value):
    try:
        p = element.LookupParameter(_TAG_PARAM_NAME)
        if p is not None and not p.IsReadOnly:
            p.Set(value)
    except Exception:
        pass


# --------------------------------------------------------------------------
# View Template duplication (Phase 2 pre-pass - only when the toggle is
# on; one transaction per DISTINCT template, not one per view, so 3
# checked views sharing a template get one shared new copy, mirroring
# their original relationship)
# --------------------------------------------------------------------------
def _duplicate_templates(doc, view_elements):
    template_map = {}
    distinct_ids = []
    seen = set()
    for el in view_elements:
        try:
            tid = el.ViewTemplateId
        except Exception:
            continue
        if tid is None or tid == ElementId.InvalidElementId:
            continue
        if tid.IntegerValue in seen:
            continue
        seen.add(tid.IntegerValue)
        distinct_ids.append(tid)

    for tid in distinct_ids:
        t = Transaction(doc, "DeeVSDupl - Duplicate View Template")
        t.Start()
        try:
            new_ids = ElementTransformUtils.CopyElements(
                doc, List[ElementId]([tid]), doc, Transform.Identity, CopyPasteOptions())
            new_id = list(new_ids)[0]
            t.Commit()
            template_map[tid.IntegerValue] = new_id
        except Exception:
            t.RollBack()
            continue
    return template_map


def _apply_mapped_template(new_view, original_view, dup_templates, template_map):
    if not dup_templates:
        return
    try:
        old_tid = original_view.ViewTemplateId
        if old_tid is not None and old_tid.IntegerValue in template_map:
            new_view.ViewTemplateId = template_map[old_tid.IntegerValue]
    except Exception:
        pass


# --------------------------------------------------------------------------
# Phase 3 - per-item duplication, one Transaction per top-level checked
# item (isolates a single bad item's rollback from the rest of the batch)
# --------------------------------------------------------------------------
def _duplicate_view_or_schedule(doc, row, dup_templates, template_map, prefix):
    original = row.picker_row.element
    t = Transaction(doc, "DeeVSDupl - Duplicate {0}".format(row.kind))
    t.Start()
    try:
        new_id = original.Duplicate(ViewDuplicateOption.Duplicate)
        new_el = doc.GetElement(new_id)
        _apply_mapped_template(new_el, original, dup_templates, template_map)
        new_el.Name = row.new_name
        _set_tag(new_el, prefix)
        t.Commit()
        return True, "Duplicated as '{0}'".format(row.new_name)
    except Exception as e:
        t.RollBack()
        return False, "FAILED: {0}".format(e)


def _duplicate_sheet(doc, row, dup_templates, template_map, prefix, vp_by_sheet, ssi_by_sheet):
    original_sheet = row.picker_row.element
    t = Transaction(doc, "DeeVSDupl - Duplicate Sheet")
    t.Start()
    try:
        tb_type_id = _read_sheet_titleblock_type_id(doc, original_sheet)
        if tb_type_id is None:
            raise Exception("could not find this sheet's title block type")
        new_sheet = ViewSheet.Create(doc, tb_type_id)
        new_sheet.SheetNumber = row.new_number
        new_sheet.Name = row.new_name
        _set_tag(new_sheet, prefix)

        detail_bits = []
        for vp in vp_by_sheet.get(original_sheet.Id.IntegerValue, []):
            src_view = doc.GetElement(vp.ViewId)
            if src_view is None:
                continue
            new_view_id = src_view.Duplicate(ViewDuplicateOption.Duplicate)
            new_view = doc.GetElement(new_view_id)
            _apply_mapped_template(new_view, src_view, dup_templates, template_map)
            try:
                new_view.Name = u"{0} - {1}".format(row.new_name, _read_name(src_view) or "View")
            except Exception:
                pass
            _set_tag(new_view, prefix)
            if Viewport.CanAddViewToSheet(doc, new_sheet.Id, new_view.Id):
                Viewport.Create(doc, new_sheet.Id, new_view.Id, vp.GetBoxCenter())
                detail_bits.append("1 view")

        for ssi in ssi_by_sheet.get(original_sheet.Id.IntegerValue, []):
            src_sched = doc.GetElement(ssi.ScheduleId)
            if src_sched is None:
                continue
            new_sched_id = src_sched.Duplicate(ViewDuplicateOption.Duplicate)
            new_sched = doc.GetElement(new_sched_id)
            try:
                new_sched.Name = u"{0} - {1}".format(row.new_name, _read_name(src_sched) or "Schedule")
            except Exception:
                pass
            _set_tag(new_sched, prefix)
            ScheduleSheetInstance.Create(doc, new_sheet.Id, new_sched.Id, ssi.Point)
            detail_bits.append("1 schedule")

        ann_ids = _sheet_owned_annotation_ids(doc, original_sheet.Id)
        if ann_ids:
            ElementTransformUtils.CopyElements(
                original_sheet, List[ElementId](ann_ids), new_sheet, Transform.Identity, CopyPasteOptions())
            detail_bits.append("{0} annotation element(s)".format(len(ann_ids)))

        t.Commit()
        return True, "Duplicated as '{0} - {1}' ({2})".format(
            row.new_number, row.new_name,
            ", ".join(detail_bits) if detail_bits else "no placed content")
    except Exception as e:
        t.RollBack()
        return False, "FAILED: {0}".format(e)


class _SafeProgress(object):
    """forms.ProgressBar tries to set Window.TaskbarItemInfo on its host
    window, which throws NotImplementedException under Remote Desktop/no
    taskbar (live-confirmed in DeeSheetLinks). Falls back to no progress
    UI at all rather than crashing - pb.update_progress(...)/pb.cancelled
    are safe no-ops in the fallback case."""
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


def _print_report(results, warnings):
    html = ['<h2 style="font-family:sans-serif;color:#ddd;">DeeV.S.Dupl. - Results</h2>']
    for r in results:
        bg = "#2e7d32" if r.ok else "#c62828"
        icon = "&#10003;" if r.ok else "&#10007;"
        html.append(
            '<div style="padding:6px 12px;margin:3px 0;background:{0};color:#fff;'
            'border-radius:4px;font-family:monospace;font-size:12px;">'
            '{1}&nbsp; <b>[{2}] {3}</b> -&gt; {4} &mdash; {5}</div>'.format(
                bg, icon, r.kind, r.original, r.new_label, r.detail))
    ok_count = sum(1 for r in results if r.ok)
    html.append('<hr><b style="font-family:sans-serif;">{0} / {1} duplicated successfully.</b>'.format(
        ok_count, len(results)))
    if warnings:
        html.append('<div style="font-family:sans-serif;color:#e67e22;margin-top:6px;">'
                     'Parameter warnings: {0}</div>'.format("; ".join(warnings)))
    output.print_html("".join(html))


def _matches(row, query):
    if not query:
        return True
    low = query.lower()
    haystack = u"{0} {1} {2} {3}".format(row.kind, row.number, row.name, row.type_label).lower()
    return all(term in haystack for term in low.split())


# --------------------------------------------------------------------------
# Window
# --------------------------------------------------------------------------
class DeeVSDuplWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, doc):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.doc = doc
        self._picker_all_rows = []
        self._picker_filtered_rows = []
        self._preview_rows = []
        self._results = []

        self.mode_a_rb.IsChecked = True
        self.counter_numeric_rb.IsChecked = True

        self._scan()

    # -- Pick Items -------------------------------------------------------
    def _scan(self):
        self._picker_all_rows = (_collect_sheets(self.doc) +
                                  _collect_views(self.doc) +
                                  _collect_schedules(self.doc))
        self._refresh_picker_grid()

    def _refresh_picker_grid(self):
        query = (self.pick_search_tb.Text or "").strip()
        self._picker_filtered_rows = [r for r in self._picker_all_rows if _matches(r, query)]
        self.picker_grid.ItemsSource = None
        self.picker_grid.ItemsSource = self._picker_filtered_rows
        self._update_picker_summary()

    def _update_picker_summary(self):
        total = len(self._picker_all_rows)
        selected = [r for r in self._picker_all_rows if r.selected]
        n_sheets = sum(1 for r in selected if r.kind == "Sheet")
        n_views = sum(1 for r in selected if r.kind == "View")
        n_scheds = sum(1 for r in selected if r.kind == "Schedule")
        self.picker_summary_tb.Text = u"{0} of {1} selected ({2} sheets, {3} views, {4} schedules)".format(
            len(selected), total, n_sheets, n_views, n_scheds)

    def pick_filter_click(self, sender, args):
        self._refresh_picker_grid()

    def pick_clear_filter_click(self, sender, args):
        self.pick_search_tb.Text = ""
        self._refresh_picker_grid()

    def select_all_click(self, sender, args):
        for r in self._picker_all_rows:
            r.selected = True
        self._refresh_picker_grid()

    def select_none_click(self, sender, args):
        for r in self._picker_all_rows:
            r.selected = False
        self._refresh_picker_grid()

    def select_all_sheets_click(self, sender, args):
        for r in self._picker_all_rows:
            if r.kind == "Sheet":
                r.selected = True
        self._refresh_picker_grid()

    def select_all_views_click(self, sender, args):
        for r in self._picker_all_rows:
            if r.kind == "View":
                r.selected = True
        self._refresh_picker_grid()

    def select_all_schedules_click(self, sender, args):
        for r in self._picker_all_rows:
            if r.kind == "Schedule":
                r.selected = True
        self._refresh_picker_grid()

    # -- Naming -------------------------------------------------------
    def counter_style_changed(self, sender, args):
        try:
            self.pad_width_tb.IsEnabled = bool(self.counter_numeric_rb.IsChecked)
        except Exception:
            pass

    def _read_naming_inputs(self):
        prefix = (self.prefix_tb.Text or "").strip()
        separator = self.separator_tb.Text or ""
        is_alpha = bool(self.counter_alpha_rb.IsChecked)
        try:
            pad = int(self.pad_width_tb.Text) if (self.pad_width_tb.Text or "").strip() else 0
        except Exception:
            pad = 0
        mode = "A" if bool(self.mode_a_rb.IsChecked) else "B"
        return prefix, separator, is_alpha, pad, mode

    def generate_preview_click(self, sender, args):
        checked = [r for r in self._picker_all_rows if r.selected]
        if not checked:
            forms.alert("Check at least one Sheet, View or Schedule on the Pick Items tab first.")
            return
        prefix, separator, is_alpha, pad, mode = self._read_naming_inputs()
        if not prefix:
            forms.alert("Type a Prefix / Batch Name on the Naming tab first.")
            return

        preview_rows = [PreviewRow(r) for r in checked]
        if mode == "A":
            _generate_mode_a(preview_rows, prefix, separator, pad, is_alpha)
        else:
            taken_names = _all_taken_names(self.doc)
            _generate_mode_b(preview_rows, prefix, separator, is_alpha, taken_names)
        taken_numbers = _all_taken_numbers(self.doc)
        _assign_sheet_numbers(preview_rows, taken_numbers)
        _compute_statuses(preview_rows)

        self._preview_rows = preview_rows
        self.preview_grid.ItemsSource = None
        self.preview_grid.ItemsSource = self._preview_rows
        self._update_preview_counts()

    def reset_preview_click(self, sender, args):
        self._preview_rows = []
        self.preview_grid.ItemsSource = None
        self.preview_counts_tb.Text = "Check items on Pick Items, set a Prefix, then click Generate Preview."

    def _update_preview_counts(self):
        ready = sum(1 for r in self._preview_rows if r.status == "Ready")
        dup = sum(1 for r in self._preview_rows if r.status == "Duplicate")
        invalid = sum(1 for r in self._preview_rows if r.status == "Invalid")
        empty = sum(1 for r in self._preview_rows if r.status == "Empty")
        self.preview_counts_tb.Text = u"{0} Ready, {1} Duplicate, {2} Invalid, {3} Empty (of {4})".format(
            ready, dup, invalid, empty, len(self._preview_rows))

    def run_click(self, sender, args):
        if not self._preview_rows:
            forms.alert("Click Generate Preview first.")
            return
        ready_rows = [r for r in self._preview_rows if r.status == "Ready"]
        if not ready_rows:
            forms.alert("No rows are Ready to duplicate - check the Naming tab preview "
                         "for Duplicate/Invalid/Empty rows.")
            return
        if not forms.alert("Duplicate {0} item(s)?".format(len(ready_rows)),
                            title="DeeV.S.Dupl. - Confirm", yes=True, no=True):
            return

        prefix, _sep, _alpha, _pad, _mode = self._read_naming_inputs()
        dup_templates = bool(self.dup_templates_cb.IsChecked)

        self.run_status_tb.Text = "Running..."
        results = []
        warnings = _ensure_tag_parameter(self.doc)

        template_map = {}
        if dup_templates:
            elements_for_templates = [r.picker_row.element for r in ready_rows if r.kind != "Sheet"]
            vp_by_sheet, _ssi = _placements_by_sheet(self.doc)
            for r in ready_rows:
                if r.kind != "Sheet":
                    continue
                for vp in vp_by_sheet.get(r.picker_row.element.Id.IntegerValue, []):
                    v = self.doc.GetElement(vp.ViewId)
                    if v is not None:
                        elements_for_templates.append(v)
            template_map = _duplicate_templates(self.doc, elements_for_templates)

        vp_by_sheet, ssi_by_sheet = _placements_by_sheet(self.doc)

        with _SafeProgress(title="DeeV.S.Dupl. - Duplicating...", cancellable=True) as pb:
            for i, r in enumerate(ready_rows):
                pb.update_progress(i, len(ready_rows))
                if getattr(pb, "cancelled", False):
                    break
                if r.kind == "Sheet":
                    ok, detail = _duplicate_sheet(self.doc, r, dup_templates, template_map, prefix,
                                                   vp_by_sheet, ssi_by_sheet)
                    old_label = u"{0} - {1}".format(r.old_number, r.old_name)
                    new_label = u"{0} - {1}".format(r.new_number, r.new_name)
                else:
                    ok, detail = _duplicate_view_or_schedule(self.doc, r, dup_templates, template_map, prefix)
                    old_label = r.old_name
                    new_label = r.new_name
                results.append(ResultRow(ok, r.kind, old_label, new_label, detail))

        self._results = results
        ok_count = sum(1 for r in results if r.ok)
        status = u"{0} / {1} duplicated successfully.".format(ok_count, len(results))
        if warnings:
            status += u"  (parameter warnings: {0})".format("; ".join(warnings))
        self.run_status_tb.Text = status

        self.results_grid.ItemsSource = None
        self.results_grid.ItemsSource = self._results
        self.results_summary_tb.Text = status
        _print_report(results, warnings)
        self._scan()
        self.main_tabs.SelectedIndex = 2

    def close_click(self, sender, args):
        self.Close()


def main():
    doc = __revit__.ActiveUIDocument.Document
    window = DeeVSDuplWindow(_XAML_FILE, doc)
    window.ShowDialog()


main()
