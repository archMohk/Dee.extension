# -*- coding: utf-8 -*-
"""
DeeLazy - DeeASelect module
Selects EVERY element in the whole project - model, annotation, links,
everything - in one click, with a category checklist shown FIRST so the
user can review the breakdown and untick whole categories before the
actual selection happens. All categories start CHECKED (matching the
plain meaning of "select all"), EXCEPT a short list of datum/setting/
graphic-only categories that start UNCHECKED instead (see
_STARTS_UNCHECKED below) - they still show up and can be ticked back on
by hand, they are just not part of the default "everything" sweep.

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
that has none. All / None / Model Only / Annotation Only / Model +
Annotation are quick PRESET buttons built from that CategoryType, on
top of the always-available per-category checkboxes - not a filter
applied at scan time, so switching presets never re-scans the model.
Model Only and Annotation Only are each single-type; Model + Annotation
ticks both together in one click (excluding only "Other": levels,
grids, links, and anything else that is neither).

An earlier revision added a "movable elements only" filter (excluding
pinned/grouped/location-less elements); live feedback asked for it
back out - this tool's whole point is "everything", full stop - so it
was removed rather than left toggled off by default.

--------------------------------------------------------------------
_STARTS_UNCHECKED - categories that are risky or pointless to move
--------------------------------------------------------------------
Live feedback named a specific set of categories that should not be
part of a whole-project move by default: Project Base Point and Survey
Point (moving either shifts the model's own coordinate system, not
just some geometry - a site-wide, easy-to-regret change), Sun Path
(a view decoration, not model content), Constraints and Automatic
Sketch Dimensions (sketch-mode helper graphics, not permanent
annotation), Callout Heads, Color Fill Legends and Schedule Graphics
(sheet/view furniture with no reason to travel with the model). They
are matched by category NAME (case-insensitive) - unrecognised or
renamed categories simply never match, which fails safe by leaving
them in the normal "start checked" bucket rather than silently
excluding something new. One entry, "Building Type Settings", is a
best-effort guess at what the live report meant by "Building types
setting" and is harmless if no such category exists in a given Revit
version.

--------------------------------------------------------------------
Move Selected Categories By X / Y
--------------------------------------------------------------------
Why this exists: opening a 3D view, selecting only MODEL elements and
moving them deletes some annotation (live-confirmed) - a dimension or
tag whose reference moves out from under it in a separate operation can
be orphaned. Selecting the model AND its annotation together and moving
them in ONE ElementTransformUtils.MoveElements call (not a per-element
loop) moves every reference atomically, which is exactly what keeps
dimensions and tags intact - the same thing Revit's own multi-select
drag already does. move_elements() below is deliberately a single call
over the whole id list for this reason.

X/Y are typed in the project's own display length units (utils.
display_to_internal handles the conversion) and apply to whatever
categories are currently ticked - Select and Move share the exact same
"gather ids from ticked categories" step, so ticking Model + Annotation
then entering an offset moves precisely that set. A small live preview
(direction arrow + a plain-language line) shows which way the move
will go before it happens; the Move button itself still asks for
confirmation, since unlike Select this genuinely changes the model.

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
3. The exact category NAME Revit uses in this project's language for
   each entry in _STARTS_UNCHECKED - written from the standard English
   names; a localized Revit UI may use different strings, in which case
   that one category simply starts checked like any other (fails safe).
"""
import math
import os

import clr
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")

from System.Collections.Generic import List
from System.Windows.Controls import Canvas
from System.Windows.Shapes import Line, Polygon, Ellipse
from System.Windows.Media import Brushes, PointCollection
from System.Windows import Point

from pyrevit import forms, script
import dee_branding
import utils

from Autodesk.Revit.DB import (
    FilteredElementCollector, ElementId, CategoryType, Transaction,
    ElementTransformUtils, XYZ,
)

output = script.get_output()

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "DeeASelect.xaml")

_NO_CATEGORY = u"(No category)"

_STARTS_UNCHECKED = set(n.lower() for n in [
    u"Project Base Point",
    u"Survey Point",
    u"Sun Path",
    u"Constraints",
    u"Automatic Sketch Dimensions",
    u"Callout Heads",
    u"Color Fill Legends",
    u"Schedule Graphics",
    u"Schedules",
    u"Building Type Settings",
])


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
    Model/Annotation/Other grouping (for the preset buttons), and
    whether it is currently ticked. Starts ticked UNLESS its name is in
    _STARTS_UNCHECKED (datum/setting/graphic-only categories that are
    risky or pointless in a whole-project move)."""
    def __init__(self, name, count, type_label):
        self.name = name
        self.count = count
        self.type_label = type_label
        self.checked = name.lower() not in _STARTS_UNCHECKED

    @property
    def count_label(self):
        return u"{0:,}".format(self.count)


def scan_categories(doc):
    """One pass over the whole document. Returns (rows, id_map) - rows
    for the checklist, id_map={category_name: [ElementId, ...]} so the
    final selection (or move) never has to re-scan the model; it only
    has to concatenate whichever buckets are still ticked."""
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


def move_elements(doc, ids, dx_internal, dy_internal):
    """One ElementTransformUtils.MoveElements call for the WHOLE id
    list, not a per-element loop - see the module docstring for why
    moving model geometry and its annotation together, atomically, is
    what keeps dimensions and tags intact instead of orphaned."""
    translation = XYZ(dx_internal, dy_internal, 0.0)
    ElementTransformUtils.MoveElements(doc, ids, translation)


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

        self._unit_abbr = utils.unit_abbreviation(self.doc)
        self.move_x_unit_tb.Text = self._unit_abbr
        self.move_y_unit_tb.Text = self._unit_abbr

        self._ready = True
        self._refresh_list()
        self._update_summary()
        self._update_move_preview()
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

    def _set_all(self, value, only_types=None):
        """only_types: None ticks/unticks every row regardless of type;
        otherwise a set of type_label strings to restrict to (used by
        both the two single-type presets and the combined Model +
        Annotation one, rather than a separate code path per preset)."""
        for r in self._rows:
            if only_types is not None and r.type_label not in only_types:
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
        self._set_all(True, only_types=(u"Model",))

    def annotation_only_click(self, sender, args):
        self._set_all(False)
        self._set_all(True, only_types=(u"Annotation",))

    def model_and_annotation_click(self, sender, args):
        """Model Only / Annotation Only are each single-type and
        mutually exclusive - this ticks BOTH together (excluding only
        the 'Other' type: levels, grids, links, and anything else that
        isn't Model or Annotation)."""
        self._set_all(False)
        self._set_all(True, only_types=(u"Model", u"Annotation"))

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

    def _checked_ids(self):
        ids = List[ElementId]()
        for r in self._rows:
            if not r.checked:
                continue
            for eid in self._id_map.get(r.name, []):
                ids.Add(eid)
        return ids

    def select_click(self, sender, args):
        checked_rows = [r for r in self._rows if r.checked]
        if not checked_rows:
            forms.alert("Tick at least one category first.", title="DeeASelect")
            return
        ids = self._checked_ids()
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

    # ---------------- move by X / Y ----------------
    def _move_xy_display(self):
        dx = utils.safe_float(self.move_x_tb.Text, 0.0)
        dy = utils.safe_float(self.move_y_tb.Text, 0.0)
        return dx, dy

    def move_xy_changed(self, sender, args):
        if not self._ready:
            return
        self._update_move_preview()

    def _update_move_preview(self):
        """Draws a small direction-only arrow (fixed length, not to
        scale - the point is 'which way', not 'how far') plus a plain-
        language line, redrawn live on every keystroke in the X/Y
        boxes."""
        canvas = self.move_preview_cv
        canvas.Children.Clear()
        cx, cy, r = 40.0, 40.0, 28.0

        def add_line(x1, y1, x2, y2, brush, thickness, dashed=False):
            ln = Line()
            ln.X1, ln.Y1, ln.X2, ln.Y2 = x1, y1, x2, y2
            ln.Stroke = brush
            ln.StrokeThickness = thickness
            if dashed:
                from System.Windows.Media import DoubleCollection
                dashes = DoubleCollection()
                dashes.Add(4)
                dashes.Add(3)
                ln.StrokeDashArray = dashes
            canvas.Children.Add(ln)

        # faint +X / +Y axis crosshair for reference
        add_line(4, cy, 76, cy, Brushes.LightGray, 1, dashed=True)
        add_line(cx, 76, cx, 4, Brushes.LightGray, 1, dashed=True)

        dot = Ellipse()
        dot.Width = 6
        dot.Height = 6
        dot.Fill = Brushes.Gray
        Canvas.SetLeft(dot, cx - 3)
        Canvas.SetTop(dot, cy - 3)
        canvas.Children.Add(dot)

        dx, dy = self._move_xy_display()
        if dx == 0.0 and dy == 0.0:
            self.move_preview_tb.Text = u"No movement entered yet."
            return

        length = math.sqrt(dx * dx + dy * dy)
        ux, uy = dx / length, dy / length
        # Screen Y grows downward; Revit +Y is "up" in plan, so flip for display.
        ex, ey = cx + ux * r, cy - uy * r
        add_line(cx, cy, ex, ey, Brushes.SteelBlue, 2.5)

        dirx, diry = (ex - cx) / r, (ey - cy) / r
        perpx, perpy = -diry, dirx
        back_x, back_y = ex - dirx * 9, ey - diry * 9
        head = Polygon()
        pts = PointCollection()
        pts.Add(Point(ex, ey))
        pts.Add(Point(back_x + perpx * 4.5, back_y + perpy * 4.5))
        pts.Add(Point(back_x - perpx * 4.5, back_y - perpy * 4.5))
        head.Points = pts
        head.Fill = Brushes.SteelBlue
        canvas.Children.Add(head)

        self.move_preview_tb.Text = (
            u"Moves {0:+.2f} {2} in X, {1:+.2f} {2} in Y."
            .format(dx, dy, self._unit_abbr))

    def move_click(self, sender, args):
        checked_rows = [r for r in self._rows if r.checked]
        if not checked_rows:
            forms.alert("Tick at least one category first.", title="DeeASelect")
            return
        dx_disp, dy_disp = self._move_xy_display()
        if dx_disp == 0.0 and dy_disp == 0.0:
            forms.alert("Enter a non-zero X or Y value to move by.", title="DeeASelect")
            return
        ids = self._checked_ids()
        if ids.Count == 0:
            forms.alert("Nothing to move.", title="DeeASelect")
            return

        proceed = forms.alert(
            u"Move {0:,} element(s) across {1} categor(y/ies) by:\n\n"
            u"   X:  {2:+.2f} {4}\n   Y:  {3:+.2f} {4}\n\n"
            u"This changes the model. Continue?".format(
                ids.Count, len(checked_rows), dx_disp, dy_disp, self._unit_abbr),
            title="DeeASelect - Move", yes=True, no=True)
        if not proceed:
            return

        dx_internal = utils.display_to_internal(self.doc, dx_disp)
        dy_internal = utils.display_to_internal(self.doc, dy_disp)

        t = Transaction(self.doc, "DeeASelect - move selection")
        try:
            t.Start()
            move_elements(self.doc, ids, dx_internal, dy_internal)
            t.Commit()
        except Exception as e:
            try:
                t.RollBack()
            except Exception:
                pass
            forms.alert(u"Move failed and was rolled back:\n{0}".format(e),
                        title="DeeASelect")
            return

        try:
            self.uidoc.Selection.SetElementIds(ids)
        except Exception:
            pass

        self.status_tb.Text = (
            u"Moved {0:,} element(s) across {1} categor(y/ies) by X={2:+.2f}{4}, Y={3:+.2f}{4}."
            .format(ids.Count, len(checked_rows), dx_disp, dy_disp, self._unit_abbr))
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
    "description": "Select EVERY element in the project - model, annotation, links, everything - with a category checklist, one-click Model/Annotation presets, and an X/Y move that keeps dimensions and tags intact.",
    "launch": launch,
}
