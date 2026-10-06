# -*- coding: utf-8 -*-
"""
DeeLazy - DeeBulkRename module
Adds a whole METHOD PIPELINE to the name of every View, Sheet, Schedule,
and Legend in the project - modeled directly on Advanced Renamer's own
numbered method list (RegEx, Name, Replace, Case, Remove, Add, Auto
Date, Numbering), per live request/screenshot. Each method has its own
Enabled checkbox and runs in FIXED order, top to bottom, on the result
of the one before it - untick a method to skip it entirely. A live
Preview (Old Name / New Name / Status) shows the exact result of the
whole pipeline before Rename actually runs.

The pipeline engine itself (apply_methods() and every _*_method()
helper) now lives in lib/dee_rename_pipeline.py, NOT here - this was its
first home, then extracted so other tools with their own naming feature
(DeeAssemb, DeeLevels, DeeView, ...) can reuse the exact same, already-
tested engine instead of each carrying its own copy. This module only
keeps what's specific to IT: the View/Sheet/Schedule/Legend scan and the
two-tab window.

Two tabs: Pick Items (the checklist - unchanged in spirit from the first
version of this tool) and Rename Methods (the new pipeline + preview +
Run). Decoupled on purpose: Pick Items answers "what", Rename Methods
answers "how" - the same split DeeV.S.Dupl. already uses successfully
for its own Pick Items / Naming tabs.

Scope - what "every View/Sheet/Schedule/Legend" means here
------------------------------------------------------------
FilteredElementCollector(doc).OfClass(View) returns Views, Sheets
(ViewSheet) and Schedules (ViewSchedule) alike - they all derive from
View in the Revit API - so one collector pass covers all four kinds;
_kind_of() tells them apart afterward by isinstance/ViewType rather than
needing four separate collectors. View/schedule TEMPLATES and Revit's
own internal/titleblock-revision schedules are excluded from the scan -
see scan_items()'s own docstring.

--------------------------------------------------------------------
The eight methods, in the FIXED order they always run
--------------------------------------------------------------------
1. RegEx - re.sub(match, replace, name) - an invalid pattern leaves the
   name unchanged for this step rather than raising (reported as a
   still-visible, uncorrupted preview rather than a crash).
2. Name - Keep (no change) / Remove (blank the name out entirely,
   useful when the whole new name will come from Add/Numbering instead)
   / Fixed (replace with a typed constant).
3. Replace - plain substring find/replace (not regex), with Match Case
   and "first occurrence only" options.
4. Case - Same/UPPERCASE/lowercase/Title Case/Sentence case, with a
   comma-separated Exceptions list whose words keep their own casing
   regardless of the mode (case-insensitive match, restores the
   EXACT casing typed in the Exceptions box).
5. Remove - First N / Last N characters, a From-To character range,
   Crop Before/After a given substring (drops everything before/after
   the FIRST match of that substring), and Digits/Symbols/Trim-spaces
   toggles.
6. Add - Prefix, Insert-text-at-position, and Suffix, combined in that
   order (insert happens on the pre-prefix/suffix name, then prefix and
   suffix wrap the result - matches Advanced Renamer's own behavior).
7. Auto Date - inserts TODAY's date (Revit elements have no exposed
   file-style creation/modified timestamp the way a file does, so this
   is deliberately simpler than the reference tool: current date only),
   in DMY/MDY/YMD order, at a chosen position.
8. Numbering - a per-CHECKED-ROW sequential counter (0-based index
   across the checked rows, in the order they appear in the checklist),
   with Start/Increment/Pad/Separator/Position, inserted independently
   of Add's own Prefix/Suffix (applied last, after everything else).

Per-item try/except throughout apply_methods(): a single row's own bad
input (e.g. an invalid regex, a from/to range outside the name's length)
degrades that ONE row's preview gracefully rather than aborting the
whole batch - matching this codebase's established fail-safe convention.

Renaming itself is exactly Element.Name = new_name (View's own public
setter, valid for View/ViewSheet/ViewSchedule alike) - inside one
Transaction, per-item try/except so a single name collision within its
own kind's namespace is reported and skipped rather than aborting the
whole batch.

--------------------------------------------------------------------
NEEDS LIVE-REVIT VERIFICATION (per this codebase's convention)
--------------------------------------------------------------------
1. IsTitleblockRevisionSchedule / IsInternalKeynoteSchedule are believed
   correct ViewSchedule property names, but not exercised live from
   this session - if either doesn't exist on a given Revit version, the
   try/except simply means that exclusion never fires (fails safe: the
   schedule stays IN the scan rather than crashing it).
2. The whole 8-method pipeline is brand new and has not been exercised
   live at all yet - each method's own transform logic is plain Python
   string manipulation (no Revit API involved until the final rename),
   so the main live-verification risk is Element.Name's setter behavior
   across all four kinds together in one batch/Transaction, same as the
   first version of this tool.
"""
import os

from pyrevit import forms, script
import dee_branding
from dee_rename_pipeline import apply_methods

from Autodesk.Revit.DB import (
    FilteredElementCollector, View, ViewSheet, ViewSchedule, ViewType, Transaction,
)

output = script.get_output()

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "DeeBulkRename.xaml")


def _kind_of(view):
    if isinstance(view, ViewSheet):
        return u"Sheet"
    if isinstance(view, ViewSchedule):
        return u"Schedule"
    try:
        if view.ViewType == ViewType.Legend:
            return u"Legend"
    except Exception:
        pass
    return u"View"


def _is_excluded_schedule(view):
    try:
        if view.IsTitleblockRevisionSchedule:
            return True
    except Exception:
        pass
    try:
        if view.IsInternalKeynoteSchedule:
            return True
    except Exception:
        pass
    return False


class RenameRow(object):
    def __init__(self, element, kind, old_name):
        self.element = element
        self.kind = kind
        self.old_name = old_name
        self.new_name = old_name
        self.status = u""
        self.checked = True


def scan_items(doc):
    """One pass over every View/Sheet/Schedule/Legend - templates and
    Revit's own internal/titleblock-revision schedules excluded. Sorted
    by kind then name so the checklist reads as four visually-grouped
    blocks."""
    rows = []
    try:
        collector = FilteredElementCollector(doc).OfClass(View)
    except Exception:
        return rows
    for v in collector:
        try:
            if v.IsTemplate:
                continue
        except Exception:
            continue
        kind = _kind_of(v)
        if kind == u"Schedule" and _is_excluded_schedule(v):
            continue
        try:
            name = v.Name
        except Exception:
            continue
        rows.append(RenameRow(v, kind, name))
    rows.sort(key=lambda r: (r.kind, r.old_name.lower()))
    return rows


# ==========================================================================
# window
# ==========================================================================
class DeeBulkRenameWindow(dee_branding.DeeBrandedWindow):
    # Must exist BEFORE the base class loads the XAML - loading it can
    # fire TextChanged/Checked handlers before __init__ has finished.
    _ready = False

    def __init__(self, xaml_file, uiapp):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.uiapp = uiapp
        self.uidoc = uiapp.ActiveUIDocument
        self.doc = self.uidoc.Document

        self._rows = scan_items(self.doc)
        self._preview_rows = []

        self._ready = True
        self._refresh_list()
        self._update_pick_summary()
        if not self._rows:
            self.status_tb.Text = "No renamable Views/Sheets/Schedules/Legends found."

    # ---------------- Pick Items ----------------
    def _shown_rows(self):
        query = ""
        try:
            query = (self.search_tb.Text or "").strip().lower()
        except Exception:
            pass
        if not query:
            return self._rows
        return [r for r in self._rows
                if query in r.old_name.lower() or query in r.kind.lower()]

    def _refresh_list(self):
        self.rows_lb.ItemsSource = None
        self.rows_lb.ItemsSource = self._shown_rows()

    def search_changed(self, sender, args):
        if not self._ready:
            return
        self._refresh_list()

    def _set_all(self, value, kind=None):
        for r in self._rows:
            if kind is None or r.kind == kind:
                r.checked = value
        self._refresh_list()
        self._update_pick_summary()

    def all_click(self, sender, args):
        self._set_all(True)

    def none_click(self, sender, args):
        self._set_all(False)

    def sheets_only_click(self, sender, args):
        self._set_all(False)
        self._set_all(True, u"Sheet")

    def views_only_click(self, sender, args):
        self._set_all(False)
        self._set_all(True, u"View")

    def schedules_legends_click(self, sender, args):
        self._set_all(False)
        for r in self._rows:
            if r.kind in (u"Schedule", u"Legend"):
                r.checked = True
        self._refresh_list()
        self._update_pick_summary()

    def row_toggled(self, sender, args):
        self._update_pick_summary()

    def _update_pick_summary(self):
        checked = [r for r in self._rows if r.checked]
        self.pick_summary_tb.Text = u"{0:,} of {1:,} item(s) checked.".format(
            len(checked), len(self._rows))

    # ---------------- Rename Methods ----------------
    def method_changed(self, sender, args):
        pass  # methods are only ever read on Generate Preview / Rename - no live recompute needed

    def _read_int(self, textbox, default=0):
        try:
            return int((textbox.Text or "").strip())
        except Exception:
            return default

    def _read_methods(self):
        return {
            "regex": {
                "enabled": self.m_regex_en_cb.IsChecked is True,
                "match": self.m_regex_match_tb.Text or u"",
                "replace": self.m_regex_replace_tb.Text or u"",
                "case_sensitive": self.m_regex_case_cb.IsChecked is True,
            },
            "name": {
                "enabled": self.m_name_en_cb.IsChecked is True,
                "mode": ("remove" if self.m_name_mode_cb.SelectedIndex == 1
                         else "fixed" if self.m_name_mode_cb.SelectedIndex == 2 else "keep"),
                "fixed_text": self.m_name_fixed_tb.Text or u"",
            },
            "replace": {
                "enabled": self.m_replace_en_cb.IsChecked is True,
                "find": self.m_replace_find_tb.Text or u"",
                "with": self.m_replace_with_tb.Text or u"",
                "match_case": self.m_replace_matchcase_cb.IsChecked is True,
                "first_only": self.m_replace_first_cb.IsChecked is True,
            },
            "case": {
                "enabled": self.m_case_en_cb.IsChecked is True,
                "mode": ["same", "upper", "lower", "title", "sentence"][self.m_case_mode_cb.SelectedIndex],
                "exceptions": self.m_case_exceptions_tb.Text or u"",
            },
            "remove": {
                "enabled": self.m_remove_en_cb.IsChecked is True,
                "first_n": self._read_int(self.m_remove_firstn_tb, 0),
                "last_n": self._read_int(self.m_remove_lastn_tb, 0),
                "from_pos": self._read_int(self.m_remove_from_tb, 0),
                "to_pos": self._read_int(self.m_remove_to_tb, 0),
                "crop_before": self.m_remove_cropbefore_tb.Text or u"",
                "crop_after": self.m_remove_cropafter_tb.Text or u"",
                "remove_digits": self.m_remove_digits_cb.IsChecked is True,
                "remove_symbols": self.m_remove_symbols_cb.IsChecked is True,
                "trim": self.m_remove_trim_cb.IsChecked is True,
            },
            "add": {
                "enabled": self.m_add_en_cb.IsChecked is True,
                "prefix": self.m_add_prefix_tb.Text or u"",
                "suffix": self.m_add_suffix_tb.Text or u"",
                "insert_text": self.m_add_insert_tb.Text or u"",
                "insert_pos": self._read_int(self.m_add_insertpos_tb, 0),
            },
            "auto_date": {
                "enabled": self.m_date_en_cb.IsChecked is True,
                "position": "prefix" if self.m_date_pos_cb.SelectedIndex == 0 else "suffix",
                "format": ["dmy", "mdy", "ymd"][self.m_date_fmt_cb.SelectedIndex],
                "separator": self.m_date_sep_tb.Text or u"",
            },
            "numbering": {
                "enabled": self.m_numbering_en_cb.IsChecked is True,
                "position": "prefix" if self.m_numbering_pos_cb.SelectedIndex == 0 else "suffix",
                "start": self._read_int(self.m_numbering_start_tb, 1),
                "increment": self._read_int(self.m_numbering_incr_tb, 1) or 1,
                "pad": self._read_int(self.m_numbering_pad_tb, 0),
                "separator": self.m_numbering_sep_tb.Text or u"",
            },
        }

    def generate_preview_click(self, sender, args):
        checked_rows = [r for r in self._rows if r.checked]
        if not checked_rows:
            forms.alert("Tick at least one item on the Pick Items tab first.", title="DeeBulkRename")
            return
        methods = self._read_methods()

        preview_rows = []
        for idx, r in enumerate(checked_rows):
            new_name = apply_methods(r.old_name, idx, methods)
            row = RenameRow(r.element, r.kind, r.old_name)
            row.new_name = new_name
            preview_rows.append(row)

        seen = {}
        for row in preview_rows:
            seen.setdefault(row.new_name, []).append(row)
        for row in preview_rows:
            if not row.new_name.strip():
                row.status = u"Empty"
            elif len(seen[row.new_name]) > 1:
                row.status = u"Duplicate"
            else:
                row.status = u"Ready"

        self._preview_rows = preview_rows
        self.preview_grid.ItemsSource = None
        self.preview_grid.ItemsSource = self._preview_rows

        ready = sum(1 for r in preview_rows if r.status == u"Ready")
        dup = sum(1 for r in preview_rows if r.status == u"Duplicate")
        empty = sum(1 for r in preview_rows if r.status == u"Empty")
        self.preview_counts_tb.Text = u"{0} Ready, {1} Duplicate, {2} Empty (of {3})".format(
            ready, dup, empty, len(preview_rows))

    def rename_click(self, sender, args):
        if not self._preview_rows:
            forms.alert("Click Generate Preview first.", title="DeeBulkRename")
            return
        ready_rows = [r for r in self._preview_rows if r.status == u"Ready"]
        if not ready_rows:
            forms.alert("No rows are Ready to rename - check the preview for "
                        "Duplicate/Empty rows.", title="DeeBulkRename")
            return
        if not forms.alert(u"Rename {0:,} item(s)?".format(len(ready_rows)),
                            title="DeeBulkRename", yes=True, no=True):
            return

        renamed, failed = [], []
        t = Transaction(self.doc, "DeeBulkRename - rename views/sheets/schedules/legends")
        t.Start()
        for row in ready_rows:
            try:
                row.element.Name = row.new_name
                renamed.append((row.old_name, row.new_name))
            except Exception as e:
                failed.append((row, str(e)))
        if renamed:
            t.Commit()
        else:
            t.RollBack()

        # Re-scan so both tabs reflect the project's actual current names.
        self._rows = scan_items(self.doc)
        self._preview_rows = []
        self._refresh_list()
        self._update_pick_summary()
        self.preview_grid.ItemsSource = None
        self.preview_counts_tb.Text = "Check items on Pick Items, then click Generate Preview."

        note = (u" ({0:,} failed - a name collision within that kind is the usual cause, "
                u"see the output window)".format(len(failed)) if failed else u"")
        self.status_tb.Text = u"Renamed {0:,} item(s).{1}".format(len(renamed), note)
        if failed:
            lines = [u"**DeeBulkRename — failures**", u""]
            for row, err in failed:
                lines.append(u"- {0} [{1}]: {2}".format(row.old_name, row.kind, err))
            output.print_md(u"\n".join(lines))


# ==========================================================================
# Launch entry point (called by the DeeLazy home window)
# ==========================================================================
def launch(uiapp):
    if uiapp.ActiveUIDocument is None:
        forms.alert("Open a Revit project first.")
        return
    window = DeeBulkRenameWindow(_XAML_FILE, uiapp)
    window.ShowDialog()


TOOL_INFO = {
    "id": "dee_bulk_rename",
    "title": "DeeBulkRename",
    "description": "Rename every View, Sheet, Schedule, and Legend with a full method pipeline (RegEx, Replace, Case, Remove, Add, Auto Date, Numbering) - tick a checklist, preview the result, then rename.",
    "icon": u"\U0001F3F7",  # label tag - renaming
    "launch": launch,
}
