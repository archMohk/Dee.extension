# -*- coding: utf-8 -*-
"""
DeeLazy - DeeBulkRename module
Adds a Prefix and/or Suffix to the Name of every View, Sheet, Schedule,
and Legend in the project in one batch - a checklist shown first (with
Kind/Old Name/New Name columns and quick presets) so the user can review
exactly what will be renamed, with the New Name updating LIVE as the
Prefix/Suffix fields change, before anything actually runs.

Scope - what "every View/Sheet/Schedule/Legend" means here
------------------------------------------------------------
FilteredElementCollector(doc).OfClass(View) returns Views, Sheets
(ViewSheet) and Schedules (ViewSchedule) alike - they all derive from
View in the Revit API - so one collector pass covers all four kinds;
_kind_of() tells them apart afterward by isinstance/ViewType rather than
needing four separate collectors.

Two things are deliberately EXCLUDED from the scan, neither of which the
live request mentioned wanting:
  - View/schedule TEMPLATES (IsTemplate) - a template is not something a
    user typically means by "my views", and renaming one has a much
    bigger blast radius (every view using it is affected in browser
    organization, not the template's own name only).
  - Revit's own internal/special-purpose schedules - Titleblock revision
    schedules (IsTitleblockRevisionSchedule) and the internal keynote
    schedule (IsInternalKeynoteSchedule) - renaming either is far more
    likely to be an accident than something wanted by a plain "add a
    prefix to my schedules" pass. Checked defensively (try/except per
    property) since both are ViewSchedule-specific and may not exist on
    every Revit version.

Renaming itself is exactly Element.Name = new_name (View's own public
setter, valid for View/ViewSheet/ViewSchedule alike) - no attempt is
made to also touch Sheet Number; unlike DeeV.S.Dupl. (which duplicates
AND renumbers), this tool only ever renames an EXISTING item in place,
where Number is left exactly as it was.

Per-item try/except: Revit enforces name-uniqueness within each kind's
own scope (e.g. two Floor Plans can't share a name; Sheets have their
own separate namespace from Schedules), so any single collision is
caught and reported as failed rather than aborting the whole batch -
the same fail-safe convention used throughout this codebase.

--------------------------------------------------------------------
NEEDS LIVE-REVIT VERIFICATION (per this codebase's convention)
--------------------------------------------------------------------
1. IsTitleblockRevisionSchedule / IsInternalKeynoteSchedule are believed
   correct ViewSchedule property names, but not exercised live from
   this session - if either doesn't exist on a given Revit version, the
   try/except simply means that exclusion never fires (fails safe: the
   schedule stays IN the scan rather than crashing it).
2. View.Name's setter behavior for every one of the four kinds together
   in one batch/Transaction is standard, well-established Revit API
   usage individually, but not exercised live as one combined run from
   this session.
"""
import os

from pyrevit import forms, script
import dee_branding

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
        self.checked = True


def scan_items(doc):
    """One pass over every View/Sheet/Schedule/Legend - see module
    docstring for what's excluded and why. Sorted by kind then name so
    the checklist reads as four visually-grouped blocks."""
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

        self._ready = True
        self._refresh_list()
        self._update_summary()
        if not self._rows:
            self.status_tb.Text = "No renamable Views/Sheets/Schedules/Legends found."

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
        self._update_summary()

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
        self._update_summary()

    def row_toggled(self, sender, args):
        self._update_summary()

    def _update_summary(self):
        checked = [r for r in self._rows if r.checked]
        self.summary_tb.Text = u"{0:,} of {1:,} item(s) checked.".format(
            len(checked), len(self._rows))

    def _refresh_new_names(self):
        prefix = self.prefix_tb.Text or u""
        suffix = self.suffix_tb.Text or u""
        for r in self._rows:
            r.new_name = u"{0}{1}{2}".format(prefix, r.old_name, suffix)

    def prefix_suffix_changed(self, sender, args):
        if not self._ready:
            return
        self._refresh_new_names()
        self._refresh_list()

    def rename_click(self, sender, args):
        checked_rows = [r for r in self._rows if r.checked]
        if not checked_rows:
            forms.alert("Tick at least one item first.", title="DeeBulkRename")
            return
        prefix = self.prefix_tb.Text or u""
        suffix = self.suffix_tb.Text or u""
        if not prefix and not suffix:
            forms.alert("Enter a Prefix and/or Suffix first.", title="DeeBulkRename")
            return

        proceed = forms.alert(
            u"Rename {0:,} item(s)?\n\nPrefix: '{1}'\nSuffix: '{2}'".format(
                len(checked_rows), prefix, suffix),
            title="DeeBulkRename", yes=True, no=True)
        if not proceed:
            return

        renamed, failed = [], []
        t = Transaction(self.doc, "DeeBulkRename - rename views/sheets/schedules/legends")
        t.Start()
        for r in checked_rows:
            new_name = u"{0}{1}{2}".format(prefix, r.old_name, suffix)
            try:
                r.element.Name = new_name
                renamed.append((r.old_name, new_name))
            except Exception as e:
                failed.append((r, str(e)))
        if renamed:
            t.Commit()
        else:
            t.RollBack()

        # Re-scan so the checklist's Old Name column reflects what the
        # project actually looks like now, not stale pre-rename names.
        self._rows = scan_items(self.doc)
        self._refresh_new_names()
        self._refresh_list()
        self._update_summary()

        note = (u" ({0:,} failed - a name collision within that kind is the usual cause, "
                u"see the output window)".format(len(failed)) if failed else u"")
        self.status_tb.Text = u"Renamed {0:,} item(s).{1}".format(len(renamed), note)
        if failed:
            lines = [u"**DeeBulkRename — failures**", u""]
            for r, err in failed:
                lines.append(u"- {0} [{1}]: {2}".format(r.old_name, r.kind, err))
            output.print_md(u"\n".join(lines))

    def close_click(self, sender, args):
        self.Close()


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
    "description": "Add a Prefix and/or Suffix to the name of every View, Sheet, Schedule, and Legend in the project - tick a checklist, watch the New Name update live, then rename.",
    "launch": launch,
}
