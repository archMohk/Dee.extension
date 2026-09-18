# -*- coding: utf-8 -*-
"""
DeeLazy - Lazy Line Tag module
Finds title text on chosen sheets, pairs each with its nearby underline
(a Detail Line) and tag bubble (a small Generic Annotation/Detail Item
symbol), and resizes/repositions them to fit the text - the manual "drag
the line so it matches the title" chore, batched across many sheets.

--------------------------------------------------------------------
Why this targets a plain TextNote + DetailLine + FamilyInstance, not
Revit's own built-in "View Title"
--------------------------------------------------------------------
Researched before writing any code: Revit's built-in View Title (the one
that auto-appears under a Viewport) is controlled at the VIEWPORT TYPE
level, not as an independent per-instance element - confirmed via
Autodesk's own community documentation. There is no API to adjust its
line length per-instance; the only "fix" is creating multiple View Title
family Types with different fixed line lengths and manually assigning the
right one per Viewport Type. That is almost certainly why this is being
done by hand in the first place - the title/line/tag in question are a
manually-placed TextNote + Detail Line + small annotation FamilyInstance,
each independently selectable and independently measurable/movable, which
IS fully achievable. Confirmed with the user directly. Because the scan
below only ever looks at real TextNote elements, Revit's own built-in
View Title mechanism (which isn't a TextNote at all) naturally produces
no match and is correctly left untouched - no special-case detection
needed for it.

--------------------------------------------------------------------
The pairing heuristic - genuinely new geometry code, no precedent
anywhere in this repo (flagged, not silently assumed correct)
--------------------------------------------------------------------
For each TextNote found on a sheet: the nearest horizontal Detail Line
whose vertical gap below the text is within tolerance becomes its
"line"; the nearest small annotation FamilyInstance to its left, within
a search radius, becomes its "tag". Either can be missing (reported, not
guessed at) - a TextNote with no line AND no tag nearby is simply not a
title-group candidate and is skipped from the preview grid entirely
(nothing useful to report about ordinary sheet text that never had a
line/tag near it).

Text width is read via TextNote.get_BoundingBox(sheet) - the same
pattern already proven live in this repo (dee_sselect.py's legend-
building code, create_legend()), applied here to an EXISTING TextNote
instead of a freshly-created one.

NEEDS LIVE-REVIT VERIFICATION: the pairing heuristic itself (untested
geometry code); whether OST_GenericAnnotation/OST_DetailComponents is
the right category filter for "tag" symbols on a real project (may need
narrowing/widening once tested against an actual tag family).
"""
import os
import time

from pyrevit import forms
import dee_branding

from Autodesk.Revit.DB import (
    FilteredElementCollector, ViewSheet, TextNote, DetailLine, FamilyInstance,
    BuiltInCategory, Line, XYZ, Transaction, ElementTransformUtils,
    UnitUtils, UnitTypeId,
)

import deew_settings

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "LineTag.xaml")
_PRESET_TOOL_NAME = "dee_line_tag_presets"

_HORIZONTAL_SLOP_MM = 3.0   # how far a line's two endpoints may differ in Y
                            # and still count as "horizontal"


def _mm_to_ft(mm):
    try:
        return UnitUtils.ConvertToInternalUnits(float(mm), UnitTypeId.Millimeters)
    except Exception:
        return float(mm) / 304.8


def _ft_to_mm(ft):
    try:
        return UnitUtils.ConvertFromInternalUnits(float(ft), UnitTypeId.Millimeters)
    except Exception:
        return float(ft) * 304.8


def _tag_category_ids():
    """Built defensively (int category ids, not a literal set of
    BuiltInCategory members) so a member missing in some Revit version
    degrades to "not matched" rather than breaking this whole module on
    import - same convention already used elsewhere in this codebase
    (e.g. DeeVSDupl's _not_copyable_sheet_categories)."""
    wanted = ["OST_GenericAnnotation", "OST_DetailComponents"]
    out = set()
    for member in wanted:
        try:
            out.add(int(getattr(BuiltInCategory, member)))
        except Exception:
            continue
    return out


_TAG_CATEGORY_IDS = _tag_category_ids()


def _read_text(text_note):
    try:
        return text_note.Text or ""
    except Exception:
        return ""


def _text_preview(text):
    text = (text or "").replace("\r", " ").replace("\n", " ").strip()
    return text if len(text) <= 60 else text[:57] + "..."


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------
class SheetRow(object):
    def __init__(self, sheet):
        self.sheet = sheet
        self.number = sheet.SheetNumber or ""
        self.name = sheet.Name or ""
        self.selected = True


class TitleGroup(object):
    def __init__(self, sheet, text_note, text_bbox):
        self.sheet = sheet
        self.sheet_number = sheet.SheetNumber or ""
        self.text_note = text_note
        self.text_bbox = text_bbox
        self.text_preview = _text_preview(_read_text(text_note))

        self.line = None
        self.line_p0 = None
        self.line_p1 = None
        self.line_y = None
        self.old_line_length_ft = None
        self.new_line_p0 = None
        self.new_line_p1 = None

        self.tag = None
        self.tag_bbox = None
        self.tag_delta = None  # XYZ translation

        self.included = True
        self.status = ""

    @property
    def old_line_text(self):
        if self.old_line_length_ft is None:
            return "-"
        return "{0:.0f}".format(_ft_to_mm(self.old_line_length_ft))

    @property
    def new_line_text(self):
        if self.new_line_p0 is None:
            return "-"
        length_ft = (self.new_line_p1.X - self.new_line_p0.X)
        return "{0:.0f}".format(_ft_to_mm(length_ft))

    @property
    def tag_move_text(self):
        if self.tag_delta is None:
            return "-"
        dx = _ft_to_mm(self.tag_delta.X)
        dy = _ft_to_mm(self.tag_delta.Y)
        if abs(dx) < 0.5 and abs(dy) < 0.5:
            return "already in place"
        return "dx {0:+.0f}mm, dy {1:+.0f}mm".format(dx, dy)


class ResultRow(object):
    def __init__(self, ok, sheet_number, text_preview, detail):
        self.ok = ok
        self.sheet_number = sheet_number
        self.text_preview = text_preview
        self.status_text = "OK" if ok else "FAILED"
        self.detail = detail


class Settings(object):
    def __init__(self):
        self.padding_mm = 2.0
        self.alignment = "left"  # "left" or "center"
        self.tag_offset_x_mm = 5.0
        self.tag_offset_y_mm = 0.0
        self.max_gap_mm = 5.0
        self.tag_search_mm = 50.0

    def to_dict(self):
        return {
            "padding_mm": self.padding_mm, "alignment": self.alignment,
            "tag_offset_x_mm": self.tag_offset_x_mm, "tag_offset_y_mm": self.tag_offset_y_mm,
            "max_gap_mm": self.max_gap_mm, "tag_search_mm": self.tag_search_mm,
        }

    @staticmethod
    def from_dict(data):
        s = Settings()
        s.padding_mm = float(data.get("padding_mm", s.padding_mm))
        s.alignment = data.get("alignment", s.alignment)
        s.tag_offset_x_mm = float(data.get("tag_offset_x_mm", s.tag_offset_x_mm))
        s.tag_offset_y_mm = float(data.get("tag_offset_y_mm", s.tag_offset_y_mm))
        s.max_gap_mm = float(data.get("max_gap_mm", s.max_gap_mm))
        s.tag_search_mm = float(data.get("tag_search_mm", s.tag_search_mm))
        return s


# --------------------------------------------------------------------------
# Presets - reuses lib/deew_settings.py's generic per-tool JSON store,
# same pattern as DeeVSDupl's selection presets.
# --------------------------------------------------------------------------
def list_presets():
    data = deew_settings.load(_PRESET_TOOL_NAME, {})
    return sorted(data.keys())


def load_preset(name):
    data = deew_settings.load(_PRESET_TOOL_NAME, {})
    raw = data.get(name)
    return Settings.from_dict(raw) if raw is not None else None


def save_preset(name, settings):
    data = deew_settings.load(_PRESET_TOOL_NAME, {})
    data[name] = settings.to_dict()
    return deew_settings.save(_PRESET_TOOL_NAME, data)


def delete_preset(name):
    data = deew_settings.load(_PRESET_TOOL_NAME, {})
    if name in data:
        del data[name]
        return deew_settings.save(_PRESET_TOOL_NAME, data)
    return True


# --------------------------------------------------------------------------
# Scan - collection
# --------------------------------------------------------------------------
def _collect_text_notes(doc, sheet):
    rows = []
    for tn in FilteredElementCollector(doc, sheet.Id).OfClass(TextNote):
        try:
            bbox = tn.get_BoundingBox(sheet)
            if bbox is None:
                continue
            rows.append((tn, bbox))
        except Exception:
            continue
    return rows


def _collect_horizontal_lines(doc, sheet, slop_ft):
    rows = []
    for dl in FilteredElementCollector(doc, sheet.Id).OfClass(DetailLine):
        try:
            curve = dl.Location.Curve
            if not isinstance(curve, Line):
                continue
            p0 = curve.GetEndPoint(0)
            p1 = curve.GetEndPoint(1)
            if abs(p0.Y - p1.Y) > slop_ft:
                continue
            rows.append((dl, p0, p1))
        except Exception:
            continue
    return rows


def _collect_tag_candidates(doc, sheet):
    rows = []
    for fi in FilteredElementCollector(doc, sheet.Id).OfClass(FamilyInstance):
        try:
            cat = fi.Category
            if cat is None or cat.Id.IntegerValue not in _TAG_CATEGORY_IDS:
                continue
            bbox = fi.get_BoundingBox(sheet)
            if bbox is None:
                continue
            rows.append((fi, bbox))
        except Exception:
            continue
    return rows


# --------------------------------------------------------------------------
# Scan - pairing
# --------------------------------------------------------------------------
def scan_sheet(doc, sheet, settings):
    """Returns a list of TitleGroup - one per TextNote that has at least
    a line OR a tag matched nearby. A TextNote with neither is ordinary
    sheet text (not a title), silently skipped rather than reported."""
    max_gap_ft = _mm_to_ft(settings.max_gap_mm)
    tag_search_ft = _mm_to_ft(settings.tag_search_mm)
    slop_ft = _mm_to_ft(_HORIZONTAL_SLOP_MM)

    text_rows = _collect_text_notes(doc, sheet)
    if not text_rows:
        return []
    line_rows = _collect_horizontal_lines(doc, sheet, slop_ft)
    tag_rows = _collect_tag_candidates(doc, sheet)

    groups = []
    for tn, tbbox in text_rows:
        text_left = tbbox.Min.X
        text_right = tbbox.Max.X
        text_bottom = tbbox.Min.Y
        text_center_x = (text_left + text_right) / 2.0
        half_width = (text_right - text_left) / 2.0

        group = TitleGroup(sheet, tn, tbbox)

        best_line = None
        best_gap = None
        for dl, p0, p1 in line_rows:
            line_y = (p0.Y + p1.Y) / 2.0
            gap = text_bottom - line_y
            if gap < -slop_ft or gap > max_gap_ft:
                continue
            line_min_x = min(p0.X, p1.X)
            line_max_x = max(p0.X, p1.X)
            line_center_x = (line_min_x + line_max_x) / 2.0
            if abs(line_center_x - text_center_x) > half_width + tag_search_ft:
                continue
            if best_gap is None or gap < best_gap:
                best_gap = gap
                best_line = (dl, p0, p1, line_y)
        if best_line is not None:
            group.line, group.line_p0, group.line_p1, group.line_y = best_line
            group.old_line_length_ft = (
                max(group.line_p0.X, group.line_p1.X) - min(group.line_p0.X, group.line_p1.X))

        best_tag = None
        best_dist = None
        ref_y = group.line_y if group.line is not None else text_bottom
        for fi, fbbox in tag_rows:
            tag_center_x = (fbbox.Min.X + fbbox.Max.X) / 2.0
            tag_center_y = (fbbox.Min.Y + fbbox.Max.Y) / 2.0
            if tag_center_x > text_left:
                continue
            dx = text_left - tag_center_x
            if dx > tag_search_ft:
                continue
            dy = abs(tag_center_y - ref_y)
            if dy > tag_search_ft:
                continue
            dist = (dx * dx + dy * dy) ** 0.5
            if best_dist is None or dist < best_dist:
                best_dist = dist
                best_tag = (fi, fbbox)
        if best_tag is not None:
            group.tag, group.tag_bbox = best_tag

        if group.line is None and group.tag is None:
            continue

        if group.line is not None and group.tag is not None:
            group.status = "Ready"
        elif group.line is None:
            group.status = "No line found"
        else:
            group.status = "No tag found"
        groups.append(group)
    return groups


# --------------------------------------------------------------------------
# Compute new targets (preview) - pure, no Revit writes
# --------------------------------------------------------------------------
def compute_targets(group, settings):
    padding_ft = _mm_to_ft(settings.padding_mm)
    text_left = group.text_bbox.Min.X
    text_right = group.text_bbox.Max.X
    text_center_x = (text_left + text_right) / 2.0
    target_length_ft = (text_right - text_left) + 2 * padding_ft

    if group.line is not None:
        if settings.alignment == "center":
            new_min_x = text_center_x - target_length_ft / 2.0
            new_max_x = text_center_x + target_length_ft / 2.0
        else:
            new_min_x = text_left - padding_ft
            new_max_x = new_min_x + target_length_ft
        z = group.line_p0.Z
        group.new_line_p0 = XYZ(new_min_x, group.line_y, z)
        group.new_line_p1 = XYZ(new_max_x, group.line_y, z)

    if group.tag is not None:
        ref_y = group.line_y if group.line is not None else group.text_bbox.Min.Y
        target_center_x = text_left - _mm_to_ft(settings.tag_offset_x_mm)
        target_center_y = ref_y + _mm_to_ft(settings.tag_offset_y_mm)
        cur_center_x = (group.tag_bbox.Min.X + group.tag_bbox.Max.X) / 2.0
        cur_center_y = (group.tag_bbox.Min.Y + group.tag_bbox.Max.Y) / 2.0
        group.tag_delta = XYZ(target_center_x - cur_center_x, target_center_y - cur_center_y, 0)


# --------------------------------------------------------------------------
# Run - the only part that writes to the document
# --------------------------------------------------------------------------
def apply_group(doc, group):
    """One Transaction per group, isolating a single bad group's rollback
    from the rest of the batch. Returns (ok, detail)."""
    t = Transaction(doc, "Lazy Line Tag - fit '{0}'".format(group.text_preview[:40]))
    t.Start()
    try:
        bits = []
        if group.line is not None and group.new_line_p0 is not None:
            group.line.Location.Curve = Line.CreateBound(group.new_line_p0, group.new_line_p1)
            bits.append("line resized")
        if group.tag is not None and group.tag_delta is not None:
            dx = abs(_ft_to_mm(group.tag_delta.X))
            dy = abs(_ft_to_mm(group.tag_delta.Y))
            if dx >= 0.5 or dy >= 0.5:
                ElementTransformUtils.MoveElement(doc, group.tag.Id, group.tag_delta)
                bits.append("tag moved")
        t.Commit()
        return True, (", ".join(bits) if bits else "nothing to change")
    except Exception as e:
        if t.HasStarted() and not t.HasEnded():
            t.RollBack()
        return False, str(e)


class _SafeProgress(object):
    """forms.ProgressBar tries to set Window.TaskbarItemInfo on its host
    window - throws NotImplementedException under Remote Desktop/no
    taskbar (live-confirmed in DeeSheetLinks). Falls back to no progress
    UI at all rather than crashing."""
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


def _matches(row, query):
    if not query:
        return True
    low = query.lower()
    haystack = u"{0} {1}".format(row.number, row.name).lower()
    return all(term in haystack for term in low.split())


class LazyLineTagWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, uiapp):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.uiapp = uiapp
        self.doc = uiapp.ActiveUIDocument.Document
        self._sheet_rows = []
        self._filtered_sheet_rows = []
        self._groups = []
        self._results = []

        self.align_left_rb.IsChecked = True

        self._scan_sheets()
        self._refresh_preset_list()

    # ---------------- Sheets ----------------
    def _scan_sheets(self):
        rows = []
        for sheet in FilteredElementCollector(self.doc).OfClass(ViewSheet):
            try:
                if getattr(sheet, "IsPlaceholder", False):
                    continue
                rows.append(SheetRow(sheet))
            except Exception:
                continue
        rows.sort(key=lambda r: r.number)
        self._sheet_rows = rows
        self._refresh_sheet_grid()

    def _refresh_sheet_grid(self):
        query = (self.sheet_search_tb.Text or "").strip()
        self._filtered_sheet_rows = [r for r in self._sheet_rows if _matches(r, query)]
        self.sheet_grid.ItemsSource = None
        self.sheet_grid.ItemsSource = self._filtered_sheet_rows
        selected = sum(1 for r in self._sheet_rows if r.selected)
        self.sheet_summary_tb.Text = u"{0} of {1} selected".format(selected, len(self._sheet_rows))

    def sheet_filter_click(self, sender, args):
        self._refresh_sheet_grid()

    def sheet_clear_filter_click(self, sender, args):
        self.sheet_search_tb.Text = ""
        self._refresh_sheet_grid()

    def select_all_click(self, sender, args):
        for r in self._sheet_rows:
            r.selected = True
        self._refresh_sheet_grid()

    def select_none_click(self, sender, args):
        for r in self._sheet_rows:
            r.selected = False
        self._refresh_sheet_grid()

    def select_current_click(self, sender, args):
        try:
            active_view = self.doc.ActiveView
            active_id = active_view.Id if active_view is not None else None
        except Exception:
            active_id = None
        for r in self._sheet_rows:
            r.selected = (active_id is not None and r.sheet.Id == active_id)
        self._refresh_sheet_grid()
        if active_id is None or not any(r.selected for r in self._sheet_rows):
            forms.alert("The active view isn't a sheet - pick sheets manually instead.")

    # ---------------- Settings / Presets ----------------
    def _read_settings(self):
        s = Settings()
        try:
            s.padding_mm = float(self.padding_tb.Text)
        except Exception:
            pass
        s.alignment = "center" if bool(self.align_center_rb.IsChecked) else "left"
        try:
            s.tag_offset_x_mm = float(self.tag_offset_x_tb.Text)
        except Exception:
            pass
        try:
            s.tag_offset_y_mm = float(self.tag_offset_y_tb.Text)
        except Exception:
            pass
        try:
            s.max_gap_mm = float(self.max_gap_tb.Text)
        except Exception:
            pass
        try:
            s.tag_search_mm = float(self.tag_search_tb.Text)
        except Exception:
            pass
        return s

    def _apply_settings(self, s):
        self.padding_tb.Text = "{0:g}".format(s.padding_mm)
        self.align_center_rb.IsChecked = (s.alignment == "center")
        self.align_left_rb.IsChecked = (s.alignment != "center")
        self.tag_offset_x_tb.Text = "{0:g}".format(s.tag_offset_x_mm)
        self.tag_offset_y_tb.Text = "{0:g}".format(s.tag_offset_y_mm)
        self.max_gap_tb.Text = "{0:g}".format(s.max_gap_mm)
        self.tag_search_tb.Text = "{0:g}".format(s.tag_search_mm)

    def _refresh_preset_list(self):
        names = list_presets()
        current_text = self.preset_cb.Text
        self.preset_cb.ItemsSource = None
        self.preset_cb.ItemsSource = names
        self.preset_cb.Text = current_text

    def preset_save_click(self, sender, args):
        name = (self.preset_cb.Text or "").strip()
        if not name:
            forms.alert("Type a name for this preset first.")
            return
        if save_preset(name, self._read_settings()):
            self._refresh_preset_list()
            self.preset_cb.Text = name
            forms.alert("Saved '{0}'.".format(name))
        else:
            forms.alert("Could not save the preset.")

    def preset_load_click(self, sender, args):
        name = (self.preset_cb.Text or "").strip()
        if not name:
            forms.alert("Pick or type a saved preset name first.")
            return
        s = load_preset(name)
        if s is None:
            forms.alert("No saved preset named '{0}'.".format(name))
            return
        self._apply_settings(s)

    def preset_delete_click(self, sender, args):
        name = (self.preset_cb.Text or "").strip()
        if not name:
            return
        if not forms.alert("Delete the saved preset '{0}'?".format(name),
                            title="Lazy Line Tag - Confirm", yes=True, no=True):
            return
        delete_preset(name)
        self._refresh_preset_list()
        self.preset_cb.Text = ""

    # ---------------- Scan / Preview ----------------
    def scan_click(self, sender, args):
        selected_sheets = [r.sheet for r in self._sheet_rows if r.selected]
        if not selected_sheets:
            forms.alert("Check at least one sheet on the Sheets tab first.")
            return
        settings = self._read_settings()

        groups = []
        with _SafeProgress(title="Lazy Line Tag - scanning...", cancellable=False) as pb:
            for i, sheet in enumerate(selected_sheets):
                pb.update_progress(i, len(selected_sheets))
                try:
                    groups.extend(scan_sheet(self.doc, sheet, settings))
                except Exception:
                    continue

        for g in groups:
            try:
                compute_targets(g, settings)
            except Exception:
                g.status = "Error computing target"

        self._groups = groups
        self.preview_grid.ItemsSource = None
        self.preview_grid.ItemsSource = self._groups

        ready = sum(1 for g in groups if g.status == "Ready")
        partial = len(groups) - ready
        self.preview_summary_tb.Text = u"{0} title group(s) found on {1} sheet(s) - {2} fully matched, {3} partial.".format(
            len(groups), len(selected_sheets), ready, partial)
        self.main_tabs.SelectedIndex = 2

    # ---------------- Run ----------------
    def run_click(self, sender, args):
        if not self._groups:
            forms.alert("Click Scan first.")
            return
        to_run = [g for g in self._groups if g.included and (g.line is not None or g.tag is not None)]
        if not to_run:
            forms.alert("Nothing is checked to run - check some rows on the Preview tab first.")
            return
        if not forms.alert(
                "Adjust {0} title group(s) across {1} sheet(s)?".format(
                    len(to_run), len(set(g.sheet.Id for g in to_run))),
                title="Lazy Line Tag - Confirm", yes=True, no=True):
            return

        results = []
        with _SafeProgress(title="Lazy Line Tag - applying...", cancellable=True) as pb:
            for i, g in enumerate(to_run):
                pb.update_progress(i, len(to_run))
                if getattr(pb, "cancelled", False):
                    break
                ok, detail = apply_group(self.doc, g)
                results.append(ResultRow(ok, g.sheet_number, g.text_preview, detail))

        self._results = results
        self.results_grid.ItemsSource = None
        self.results_grid.ItemsSource = results
        ok_count = sum(1 for r in results if r.ok)
        self.results_summary_tb.Text = u"{0} / {1} adjusted successfully.".format(ok_count, len(results))
        self.main_tabs.SelectedIndex = 3

    def close_click(self, sender, args):
        self.Close()


def launch(uiapp):
    if uiapp.ActiveUIDocument is None:
        forms.alert("Open a Revit project first.")
        return
    window = LazyLineTagWindow(_XAML_FILE, uiapp)
    window.ShowDialog()


TOOL_INFO = {
    "id": "dee_line_tag",
    "title": "Lazy Line Tag",
    "description": "Auto-fit a title's underline and tag bubble to its text, across as many sheets as you pick.",
    "launch": launch,
}
