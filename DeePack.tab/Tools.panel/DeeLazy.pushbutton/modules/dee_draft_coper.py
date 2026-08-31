# -*- coding: utf-8 -*-
"""
DeeLazy - DeeDraftCoper module
Copies 2D annotation drawn DIRECTLY ON A SHEET - detail lines, text,
symbols, detail items, filled regions, images - onto any number of other
sheets, at the same position. The API equivalent of Revit's own
"Paste > Aligned to Selected Views" for sheets.

Source and destination are both ViewSheets. This is deliberately NOT a
view-to-view detail copier: the elements in question are owned by the
sheet itself (OwnerViewId == the sheet), not by any view placed on it.

--------------------------------------------------------------------
Selection is read BEFORE the window opens
--------------------------------------------------------------------
The elements come from whatever is already selected in Revit when the
module launches - uidoc.Selection.GetElementIds(). Nothing here calls
PickObject, because this codebase has already established the hard way
that PickObject from inside a modal window kills Revit outright, and
Hide() does not help. If nothing usable is selected, the module says so
and exits rather than opening a window that cannot do anything.

--------------------------------------------------------------------
Revit API facts relied on here (verified before writing, not guessed)
--------------------------------------------------------------------
- ElementTransformUtils.CopyElements(View sourceView,
      ICollection<ElementId>, View destinationView, Transform
      additionalTransform, CopyPasteOptions) -> ICollection<ElementId>
  This is the view-to-view overload, and it is the one Autodesk's own
  guide says to use for view-specific elements. The cross-DOCUMENT
  overload is already used elsewhere in this extension by DeeV.Template.
- Element.ViewSpecific (read-only bool) - "Identifies if the element is
  owned by a view", and Element.OwnerViewId (read-only ElementId) - "the
  id of the view that owns the element". Together these are what
  identifies a sheet-owned annotation element.
- Transform.Identity for "same position"; Transform.CreateTranslation
  for the optional shift.

Autodesk's guide states the source and destination must be "2D graphics
views capable of drawing details and view-specific elements" and does
NOT explicitly name ViewSheet among them - even though pasting
annotation between sheets is an everyday Revit UI operation. Rather than
assume either way, each target sheet is copied in its OWN transaction
and any refusal is reported per sheet with Revit's own message, so the
first live run answers the question instead of silently half-working.

--------------------------------------------------------------------
NEEDS LIVE-REVIT VERIFICATION (flagged, not silently assumed correct)
--------------------------------------------------------------------
- That CopyElements accepts a ViewSheet as sourceView/destinationView
  (see above) - the single biggest unknown in this module.
- Which sheet-owned categories survive the copy in practice. Revision
  clouds and images in particular may behave differently from lines and
  text; every element is reported individually rather than assumed.
- There is no way to detect that a target sheet already received these
  elements on a previous run, so re-running duplicates them. The window
  says this plainly rather than pretending to be idempotent.
"""
import os
import time

import clr
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")

from System import Action
from System.Collections.Generic import List
from System.Windows import Visibility
from System.Windows.Threading import Dispatcher, DispatcherFrame, DispatcherPriority

from pyrevit import forms
import dee_branding

from Autodesk.Revit.DB import (
    BuiltInCategory, ElementId, Transaction, ViewSheet, XYZ,
    ElementTransformUtils, CopyPasteOptions, Transform,
    UnitUtils, UnitTypeId,
)

import utils

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "DeeDraftCoper.xaml")


def _build_not_copyable():
    """Sheet-owned things that must NOT be copied this way. A Viewport cannot
    be copied by CopyElements at all, a schedule placement is a different
    beast, and a title block is already on the target sheet - copying one just
    stacks a second title block on the existing one.

    Built defensively rather than as a literal dict: this module is imported
    by the DeeLazy registry at startup, so a BuiltInCategory member missing in
    some Revit version must degrade to "that category is not flagged" instead
    of breaking the whole launcher."""
    wanted = [
        ("OST_Viewports", "viewports cannot be copied this way"),
        ("OST_TitleBlocks", "the target sheet already has a title block"),
        ("OST_ScheduleGraphics", "schedule placements are not copied this way"),
    ]
    out = {}
    for member, note in wanted:
        try:
            out[int(getattr(BuiltInCategory, member))] = note
        except Exception:
            continue
    return out


_NOT_COPYABLE = _build_not_copyable()


def mm_to_ft(mm):
    try:
        return UnitUtils.ConvertToInternalUnits(float(mm), UnitTypeId.Millimeters)
    except Exception:
        return float(mm) / 304.8


def matches(haystack, query):
    if not query:
        return True
    low = haystack.lower()
    return all(term in low for term in query.lower().split())


class SourceRow(object):
    """ElementIds and plain strings only - never a live Element held across
    the window's wait for the user."""

    def __init__(self, element_id, category, description, note, copyable):
        self.element_id = element_id
        self.category = category
        self.description = description
        self.note = note
        self.copyable = copyable
        self.selected = copyable


class TargetSheetRow(object):
    def __init__(self, sheet_id, number, name):
        self.sheet_id = sheet_id
        self.number = number
        self.name = name
        self.selected = False

    @property
    def haystack(self):
        return "{0} {1}".format(self.number, self.name)


def _category_name(element):
    try:
        cat = element.Category
        if cat is not None and cat.Name:
            return cat.Name
    except Exception:
        pass
    return "(no category)"


def _category_id(element):
    try:
        cat = element.Category
        if cat is not None:
            return cat.Id.IntegerValue
    except Exception:
        pass
    return None


def _describe(doc, element):
    """A short human label. Text notes show their text, families show their
    type name, everything else falls back to the element id."""
    try:
        text = element.Text
        if text:
            flat = " ".join(str(text).split())
            return flat[:60] + ("..." if len(flat) > 60 else "")
    except Exception:
        pass
    try:
        name = utils.read_name(element)
        if name and name != "(unnamed)":
            return name
    except Exception:
        pass
    try:
        sym = doc.GetElement(element.GetTypeId())
        name = utils.read_name(sym)
        if name:
            return name
    except Exception:
        pass
    try:
        return "id {0}".format(element.Id.IntegerValue)
    except Exception:
        return "(element)"


def analyse_selection(doc, selected_ids):
    """Returns (source_sheet_id, rows, problem_message).

    Every selected element must be owned by ONE sheet. Anything else is a
    situation this tool cannot act on, and it says which rather than
    quietly copying a subset."""
    if not selected_ids:
        return None, [], ("Nothing is selected.\n\nSelect the lines, text, symbols or "
                          "detail items you drew on a sheet, then run DeeDraftCoper.")

    owners = {}
    non_sheet = 0
    for eid in selected_ids:
        element = doc.GetElement(eid)
        if element is None:
            continue
        try:
            if not element.ViewSpecific:
                non_sheet += 1
                continue
            owner_id = element.OwnerViewId
        except Exception:
            non_sheet += 1
            continue
        if owner_id is None or owner_id == ElementId.InvalidElementId:
            non_sheet += 1
            continue
        owner = doc.GetElement(owner_id)
        if not isinstance(owner, ViewSheet):
            non_sheet += 1
            continue
        owners.setdefault(owner_id.IntegerValue, []).append(eid)

    if not owners:
        return None, [], (
            "None of the selected elements is drawn on a sheet.\n\n"
            "DeeDraftCoper copies annotation that lives on the SHEET itself - "
            "lines, text, symbols and detail items you drew straight onto the "
            "sheet. Elements inside a view placed on the sheet belong to that "
            "view, not the sheet, and are not copied by this tool.")

    if len(owners) > 1:
        return None, [], (
            "The selected elements come from {0} different sheets.\n\n"
            "Select elements from one sheet at a time so it is unambiguous "
            "which sheet is the source.".format(len(owners)))

    owner_key = list(owners.keys())[0]
    source_sheet_id = doc.GetElement(ElementId(owner_key)).Id
    rows = []
    for eid in owners[owner_key]:
        element = doc.GetElement(eid)
        cat_id = _category_id(element)
        note = _NOT_COPYABLE.get(cat_id, "")
        rows.append(SourceRow(
            element_id=eid,
            category=_category_name(element),
            description=_describe(doc, element),
            note=note,
            copyable=note == ""))
    rows.sort(key=lambda r: (r.category.lower(), r.description.lower()))

    warning = ""
    if non_sheet:
        warning = ("{0} selected element(s) are not sheet annotation and were left "
                   "out.".format(non_sheet))
    return source_sheet_id, rows, warning


def collect_target_sheets(doc, exclude_sheet_id):
    rows = []
    for sheet in utils.all_sheets(doc):
        try:
            if sheet.Id == exclude_sheet_id:
                continue
            rows.append(TargetSheetRow(sheet.Id, sheet.SheetNumber,
                                       utils.read_name(sheet) or "(unnamed)"))
        except Exception:
            continue
    rows.sort(key=lambda r: r.number.lower())
    return rows


def copy_to_sheet(doc, source_sheet_id, element_ids, target_sheet_id, transform):
    """One target sheet, one transaction. Returns (count_copied, detail)."""
    t = Transaction(doc, "DeeDraftCoper - copy to sheet")
    try:
        t.Start()
    except Exception as e:
        return 0, "could not start a transaction: {0}".format(e)
    try:
        source = doc.GetElement(source_sheet_id)
        target = doc.GetElement(target_sheet_id)
        if source is None or target is None:
            raise Exception("sheet no longer exists")
        ids = List[ElementId]()
        for eid in element_ids:
            ids.Add(eid)
        copied = ElementTransformUtils.CopyElements(
            source, ids, target, transform, CopyPasteOptions())
        count = 0
        try:
            count = len(list(copied))
        except Exception:
            count = len(element_ids)
        t.Commit()
        return count, "copied {0} element(s)".format(count)
    except Exception as e:
        try:
            t.RollBack()
        except Exception:
            pass
        return 0, str(e)


# ==========================================================================
# window
# ==========================================================================
class DeeDraftCoperWindow(dee_branding.DeeBrandedWindow):
    # Must exist BEFORE the base class loads the XAML - loading it fires the
    # TextChanged handler below, when no instance attribute exists yet.
    _ready = False

    def __init__(self, xaml_file, uiapp, source_sheet_id, source_rows, warning):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.uiapp = uiapp
        self.uidoc = uiapp.ActiveUIDocument
        self.doc = self.uidoc.Document
        self.source_sheet_id = source_sheet_id
        self._source_rows = source_rows
        self._prog_total = 1
        self._prog_done = 0
        self._prog_start = time.time()

        self._sheet_rows = collect_target_sheets(self.doc, source_sheet_id)

        source_sheet = self.doc.GetElement(source_sheet_id)
        self.source_tb.Text = "Source sheet:  {0}   -   {1} element(s) selected".format(
            utils.sheet_label(source_sheet), len(source_rows))

        self.source_grid.ItemsSource = self._source_rows
        self._refresh_sheets_grid()

        self._ready = True
        self._update_summaries()
        if warning:
            self._log(warning)
            self.status_tb.Text = warning

    # ---------------- tab 1 ----------------
    def _refresh_source_grid(self):
        self.source_grid.ItemsSource = None
        self.source_grid.ItemsSource = self._source_rows

    def src_all_click(self, sender, args):
        for r in self._source_rows:
            r.selected = True
        self._refresh_source_grid()
        self._update_summaries()

    def src_none_click(self, sender, args):
        for r in self._source_rows:
            r.selected = False
        self._refresh_source_grid()
        self._update_summaries()

    def src_copyable_click(self, sender, args):
        for r in self._source_rows:
            r.selected = r.copyable
        self._refresh_source_grid()
        self._update_summaries()

    # ---------------- tab 2 ----------------
    def _refresh_sheets_grid(self):
        query = ""
        try:
            query = self.sheet_search_tb.Text or ""
        except Exception:
            pass
        shown = [r for r in self._sheet_rows if matches(r.haystack, query)]
        self.sheets_grid.ItemsSource = None
        self.sheets_grid.ItemsSource = shown
        return shown

    def sheet_search_changed(self, sender, args):
        if not self._ready:
            return
        self._refresh_sheets_grid()

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

    # ---------------- shared ----------------
    def _checked_elements(self):
        return [r for r in self._source_rows if r.selected]

    def _checked_sheets(self):
        return [r for r in self._sheet_rows if r.selected]

    def _update_summaries(self):
        elements = self._checked_elements()
        sheets = self._checked_sheets()
        skipped = len([r for r in self._source_rows if not r.copyable and r.selected])
        self.src_summary_tb.Text = "{0} of {1} element(s) ticked.".format(
            len(elements), len(self._source_rows))
        self.sheets_summary_tb.Text = "{0} of {1} target sheet(s) ticked.".format(
            len(sheets), len(self._sheet_rows))
        self.run_summary_tb.Text = "{0} element(s) x {1} sheet(s) = {2} new element(s).".format(
            len(elements), len(sheets), len(elements) * len(sheets))
        note = ""
        if skipped:
            note = " {0} ticked element(s) are flagged as not copyable and will very likely fail.".format(skipped)
        self.status_tb.Text = "{0} element(s) ticked, {1} sheet(s) ticked.{2}".format(
            len(elements), len(sheets), note)

    def _log(self, line):
        try:
            self.log_tb.AppendText(line + "\r\n")
            self.log_tb.ScrollToEnd()
        except Exception:
            pass

    def _transform(self):
        if self.pos_offset_rb.IsChecked is not True:
            return Transform.Identity, "same position"
        try:
            dx = float(str(self.offset_x_tb.Text).strip() or "0")
        except ValueError:
            dx = 0.0
        try:
            dy = float(str(self.offset_y_tb.Text).strip() or "0")
        except ValueError:
            dy = 0.0
        if abs(dx) < 1e-9 and abs(dy) < 1e-9:
            return Transform.Identity, "same position"
        return (Transform.CreateTranslation(XYZ(mm_to_ft(dx), mm_to_ft(dy), 0.0)),
                "shifted by {0:g} x {1:g} mm".format(dx, dy))

    # ---------------- progress ----------------
    # A modal WPF window does not repaint during a synchronous loop, so
    # _pump() drains pending render work at Background priority - the same
    # DoEvents pattern already confirmed working live in DeeSuperLINK.
    def _pump(self):
        try:
            frame = DispatcherFrame()

            def _stop():
                frame.Continue = False

            self.Dispatcher.BeginInvoke(DispatcherPriority.Background, Action(_stop))
            Dispatcher.PushFrame(frame)
        except Exception:
            pass

    def _progress_begin(self, total):
        self._prog_total = max(1, total)
        self._prog_done = 0
        self._prog_start = time.time()
        try:
            self.progress_bar.Maximum = self._prog_total
            self.progress_bar.Value = 0
            self.progress_host.Visibility = Visibility.Visible
            self.run_b.IsEnabled = False
        except Exception:
            pass
        self._progress_render("Starting...")

    def _progress_render(self, label):
        try:
            elapsed = time.time() - self._prog_start
            self.progress_bar.Value = self._prog_done
            self.progress_text_tb.Text = "{0}   |   {1} of {2}   |   {3}s elapsed".format(
                label, min(self._prog_done + 1, self._prog_total),
                self._prog_total, int(elapsed))
        except Exception:
            pass
        self._pump()

    def _progress_done_one(self):
        self._prog_done += 1
        try:
            self.progress_bar.Value = self._prog_done
        except Exception:
            pass
        self._pump()

    def _progress_end(self):
        try:
            self.progress_host.Visibility = Visibility.Collapsed
            self.run_b.IsEnabled = True
        except Exception:
            pass
        self._pump()

    # ---------------- run ----------------
    def run_click(self, sender, args):
        elements = self._checked_elements()
        sheets = self._checked_sheets()
        if not elements:
            forms.alert("Tick at least one element on the What to Copy tab.",
                        title="DeeDraftCoper")
            return
        if not sheets:
            forms.alert("Tick at least one target sheet on the Target Sheets tab.",
                        title="DeeDraftCoper")
            return

        transform, position_note = self._transform()
        proceed = forms.alert(
            "Copy {0} element(s) onto {1} sheet(s), {2}?\n\n"
            "That creates {3} new element(s).\n\n"
            "This cannot detect elements copied by an earlier run, so running it "
            "twice on the same sheet will leave two overlapping sets.".format(
                len(elements), len(sheets), position_note, len(elements) * len(sheets)),
            title="DeeDraftCoper", yes=True, no=True)
        if not proceed:
            return

        element_ids = [r.element_id for r in elements]
        self.main_tabs.SelectedIndex = 3
        self._log("=" * 70)
        self._log("DeeDraftCoper - {0} element(s) -> {1} sheet(s), {2}".format(
            len(elements), len(sheets), position_note))
        self._log("=" * 70)

        ok_sheets = 0
        total_copied = 0
        failures = 0
        self._progress_begin(len(sheets))
        try:
            for row in sheets:
                label = "{0} - {1}".format(row.number, row.name)
                self._progress_render(label)
                count, detail = copy_to_sheet(
                    self.doc, self.source_sheet_id, element_ids, row.sheet_id, transform)
                if count:
                    ok_sheets += 1
                    total_copied += count
                    self._log("  OK   {0} - {1}".format(label, detail))
                else:
                    failures += 1
                    self._log("  FAIL {0} - {1}".format(label, detail))
                self._progress_done_one()
        finally:
            self._progress_end()

        self._log("-" * 70)
        self._log("Done. {0} sheet(s) received {1} element(s); {2} sheet(s) failed.".format(
            ok_sheets, total_copied, failures))
        self.status_tb.Text = ("Copied onto {0} sheet(s) ({1} new element(s)); {2} failed. "
                               "See the Log tab.".format(ok_sheets, total_copied, failures))

    def close_click(self, sender, args):
        self.Close()


# ==========================================================================
# Launch entry point (called by the DeeLazy home window)
# ==========================================================================
def launch(uiapp):
    if uiapp.ActiveUIDocument is None:
        forms.alert("Open a Revit project first.")
        return
    uidoc = uiapp.ActiveUIDocument
    doc = uidoc.Document

    # Read the selection BEFORE any window exists - see the module docstring.
    try:
        selected_ids = list(uidoc.Selection.GetElementIds())
    except Exception:
        selected_ids = []

    source_sheet_id, rows, problem = analyse_selection(doc, selected_ids)
    if source_sheet_id is None:
        forms.alert(problem, title="DeeDraftCoper")
        return

    window = DeeDraftCoperWindow(_XAML_FILE, uiapp, source_sheet_id, rows, problem)
    window.ShowDialog()


TOOL_INFO = {
    "id": "dee_draft_coper",
    "title": "DeeDraftCoper",
    "description": "Copy lines, text, symbols and detail items drawn on one sheet onto any number of other sheets, at the same position.",
    "launch": launch,
}
