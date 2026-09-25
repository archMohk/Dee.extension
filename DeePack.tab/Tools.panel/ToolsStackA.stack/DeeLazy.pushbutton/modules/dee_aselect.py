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

Pinned elements are handled the same way: MoveElements RAISES for the
ENTIRE batch if even one element in it is pinned (Project Base Point
and several of _STARTS_UNCHECKED's other categories are commonly
pinned by default, so this hit real projects immediately). Rather than
fail the whole move or silently drop pinned elements, move_elements()
unpins whichever ids are pinned right before the move and pins those
exact same ids back right after - all inside the SAME transaction as
the move, so a failure anywhere rolls the pin state back too. The user
sees the pinned count up front, in the confirmation dialog, before
anything happens.

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
2. Selection.SetElementIds' / ElementTransformUtils.MoveElements' behaviour
   on a very large (100,000+) element set - a live report described Revit
   closing outright (no error dialog) after clicking Select then Move on
   what was likely an "All"-ticked whole project. That shape - nothing
   raised, Revit just gone - is consistent with a native/out-of-memory
   failure, which no amount of try/except in this script can catch (a
   managed exception handler only ever sees a MANAGED exception). Added
   _LARGE_SELECTION_WARN_THRESHOLD (50,000 elements) so Select and Move
   both stop for one extra confirmation past that size and suggest
   narrowing the ticked categories - this cannot guarantee Revit won't
   still struggle with a huge set, but it stops the tool from silently
   handing the whole project to the API in one call. Still not
   exercised live at real 100,000+ scale.
3. The exact category NAME Revit uses in this project's language for
   each entry in _STARTS_UNCHECKED - written from the standard English
   names; a localized Revit UI may use different strings, in which case
   that one category simply starts checked like any other (fails safe).
4. A live 275-workset model report showed Move throw a mass workset-
   checkout prompt ("You are trying to checkout a large number of
   worksets...") followed by "Error 1 - Can't keep elements joined" -
   the latter because ElementTransformUtils.MoveElements moved an
   element still Join'd (JoinGeometryUtils) to another element outside
   the moved batch. Mitigated with _try_checkout_for_move() (pre-
   checkout before the transaction) and by attaching this codebase's
   existing deew_failure_handler.DeeWFailuresPreprocessor to the move
   Transaction (it silently deletes Warning-severity failures, which is
   this failure's severity, instead of surfacing an interactive dialog).
   _join_partners_outside() only COUNTS likely join breaks up front for
   the confirm dialog - it does not itself unjoin anything, so it is
   reporting-only; the failure preprocessor is the actual safety net.
   Neither GetJoinedElements' cost at real scale nor DeleteWarning's
   exact behavior for this specific failure ID has been exercised live.
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
import deew_failure_handler as ffh
import utils

from Autodesk.Revit.DB import (
    FilteredElementCollector, ElementId, CategoryType, Transaction,
    ElementTransformUtils, XYZ, JoinGeometryUtils, WorksharingUtils,
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

# HARD exclusion from Move specifically (not from Select - highlighting
# these is harmless) - live report: clicking Move closed Revit itself.
# Project Base Point and Survey Point anchor the model's own coordinate
# system; moving or unpinning either through the generic element-move
# API (rather than Revit's own dedicated Relocate Project workflow) is
# widely documented in the Revit API community as capable of corrupting
# document state or crashing the whole process outright - categorically
# different from moving ordinary geometry. _STARTS_UNCHECKED only keeps
# them off by DEFAULT, which the "All" preset overrides; this list
# cannot be overridden by any preset or manual tick - Move always
# leaves them out, unconditionally.
_NEVER_MOVE = set(n.lower() for n in [
    u"Project Base Point",
    u"Survey Point",
])

# Above this many elements, Select/Move ask for one extra confirmation
# instead of running immediately. A live report ("when i Click Select
# and Move the Revit Closed") described Revit itself disappearing, not
# an error dialog - that shape (no exception, no message, just gone) is
# what a native/out-of-memory failure looks like, and neither
# Selection.SetElementIds nor ElementTransformUtils.MoveElements can be
# wrapped safely against that in IronPython: a try/except only ever
# catches a MANAGED exception, and a genuine process crash never raises
# one. This cannot be fixed in script code - it is exactly the
# "NEEDS LIVE-REVIT VERIFICATION" risk this module's own docstring
# already named for very large (100,000+) element sets. The one thing
# code CAN do is stop and let the user narrow the ticked categories
# first, rather than silently building the full list and handing it to
# the Revit API on a "Select All"/"Model + Annotation" click.
_LARGE_SELECTION_WARN_THRESHOLD = 50000

# _join_partners_outside() is O(ticked ids) with one Revit API call per
# element - fine for a normal move, but a needless slowdown on a huge
# "All"-ticked confirm dialog where the failure preprocessor below is
# the real safety net anyway. Past this many ids the pre-count is
# skipped entirely (returns None) rather than made to scale.
_JOIN_SCAN_MAX_IDS = 20000


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


_SCAN_PROGRESS_STEP = 500


def scan_categories(doc, progress_cb=None):
    """One pass over the whole document. Returns (rows, id_map) - rows
    for the checklist, id_map={category_name: [ElementId, ...]} so the
    final selection (or move) never has to re-scan the model; it only
    has to concatenate whichever buckets are still ticked.

    progress_cb(done, total), if given, is called every
    _SCAN_PROGRESS_STEP elements (not every single one - that would
    slow down a 400,000-element scan just from the callback overhead).
    `total` comes from a second, separate GetElementCount() call on an
    identically-filtered collector - cheap relative to the full element
    loop below, and needed up front so the caller can show a real
    percentage instead of an indeterminate spinner that never redraws
    during this synchronous loop."""
    buckets = {}  # name -> {"ids": [ElementId,...], "type_label": str}
    try:
        collector = FilteredElementCollector(doc).WhereElementIsNotElementType()
    except Exception:
        return [], {}
    total = 0
    if progress_cb is not None:
        try:
            total = FilteredElementCollector(doc).WhereElementIsNotElementType().GetElementCount()
        except Exception:
            total = 0
    done = 0
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
        done += 1
        if progress_cb is not None and total and done % _SCAN_PROGRESS_STEP == 0:
            try:
                progress_cb(done, total)
            except Exception:
                pass
    rows = [CategoryRow(name, len(b["ids"]), b["type_label"])
            for name, b in buckets.items()]
    rows.sort(key=lambda r: r.name.lower())
    id_map = dict((name, b["ids"]) for name, b in buckets.items())
    return rows, id_map


def _pinned_among(doc, ids):
    """The subset of `ids` that is currently Pinned. Checked up front
    (before the confirm dialog even shows) so the user sees the count
    before committing, and reused inside the transaction so pin state
    is only read once."""
    out = []
    for eid in ids:
        el = doc.GetElement(eid)
        if el is None:
            continue
        try:
            if el.Pinned:
                out.append(eid)
        except Exception:
            continue
    return out


def _set_pinned(doc, ids, value):
    for eid in ids:
        el = doc.GetElement(eid)
        if el is None:
            continue
        try:
            el.Pinned = value
        except Exception:
            continue


def _join_partners_outside(doc, ids):
    """Reporting-only count of elements in `ids` that are geometrically
    Join'd (JoinGeometryUtils) to another element NOT in `ids` - moving
    such a pair without its partner is exactly what a live 275-workset
    model report showed raising "Error 1 - Can't keep elements joined".

    This function does NOT unjoin anything - it only counts, so the
    confirm dialog can warn the user up front. The actual safety net is
    deew_failure_handler.DeeWFailuresPreprocessor attached to the move
    Transaction (see move_click()), which silently resolves that exact
    Warning-severity failure instead of leaving it to surface as an
    interactive dialog.

    Scoped to CategoryType.Model elements only - annotation/internal
    categories (dimensions, tags, levels, grids, ...) don't participate
    in geometry joins, so checking them would only cost time for no
    signal. Returns None (skip / unknown) above _JOIN_SCAN_MAX_IDS,
    since GetJoinedElements is one Revit API call per element and this
    tool can be asked to move tens of thousands at once."""
    if ids.Count > _JOIN_SCAN_MAX_IDS:
        return None
    id_set = set(eid.IntegerValue for eid in ids)
    count = 0
    for eid in ids:
        el = doc.GetElement(eid)
        if el is None:
            continue
        try:
            if el.Category is None or el.Category.CategoryType != CategoryType.Model:
                continue
        except Exception:
            continue
        try:
            partners = JoinGeometryUtils.GetJoinedElements(doc, el)
        except Exception:
            continue
        for partner_id in partners:
            if partner_id.IntegerValue not in id_set:
                count += 1
                break
    return count


def _try_checkout_for_move(doc, ids):
    """Best-effort pre-checkout of every workset that owns an element in
    `ids`, called BEFORE the move Transaction starts - a live 275-
    workset model report showed Revit's own mid-transaction "you are
    trying to checkout a large number of worksets" prompt firing during
    Move. Doing the checkout explicitly, up front, in one call is the
    Revit-API-recommended way to avoid ad-hoc checkout prompts appearing
    from inside a later operation.

    Fully wrapped in try/except and never raises: this is a mitigation
    attempt, not a requirement, and a failure here must never block the
    move itself - MoveElements will simply trigger Revit's own checkout
    handling again if this didn't fully succeed. NEEDS LIVE-REVIT
    VERIFICATION: whether CheckoutElements itself can still surface that
    same interactive prompt for a very large workset count - not
    exercised live at 275-workset scale."""
    try:
        if not doc.IsWorkshared:
            return False
        WorksharingUtils.CheckoutElements(doc, ids)
        return True
    except Exception:
        return False


def move_elements(doc, ids, dx_internal, dy_internal, pinned_ids):
    """One ElementTransformUtils.MoveElements call for the WHOLE id
    list, not a per-element loop - see the module docstring for why
    moving model geometry and its annotation together, atomically, is
    what keeps dimensions and tags intact instead of orphaned.

    `pinned_ids` (a subset of `ids`, from _pinned_among) is unpinned
    immediately before the move and re-pinned immediately after -
    MoveElements RAISES for the entire batch if even one element in it
    is pinned (live-confirmed: this is exactly what silently blocked a
    whole-project move the moment it reached a pinned element - Project
    Base Point and several of _STARTS_UNCHECKED's other categories are
    commonly pinned by default). Both pin-state changes happen inside
    the SAME transaction as the move itself, so a failure anywhere in
    this function rolls back the pin changes along with the move -
    nothing is ever left unpinned by a failed attempt."""
    if pinned_ids:
        _set_pinned(doc, pinned_ids, False)
    translation = XYZ(dx_internal, dy_internal, 0.0)
    ElementTransformUtils.MoveElements(doc, ids, translation)
    if pinned_ids:
        _set_pinned(doc, pinned_ids, True)


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

        # Determinate, not the indeterminate spinner this used to be: an
        # indeterminate forms.ProgressBar does not redraw during a tight
        # synchronous loop with no update_progress() calls, so on a huge
        # model it just sat still for the whole scan with zero feedback -
        # exactly what a live report saw as Revit "(Not Responding)"
        # before this window had even appeared. scan_categories() now
        # drives real progress via progress_cb.
        with _SafeProgress(title="DeeASelect - scanning the project...",
                            indeterminate=False, cancellable=False) as pb:
            def _scan_progress(done, total):
                try:
                    pb.title = u"DeeASelect - scanning... {0:,} of {1:,}".format(done, total)
                    pb.update_progress(done, total)
                except Exception:
                    pass
            self._rows, self._id_map = scan_categories(self.doc, progress_cb=_scan_progress)

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

    def _move_ids(self):
        """Same as _checked_ids(), but ALWAYS excludes _NEVER_MOVE
        categories (Project Base Point, Survey Point) regardless of
        their checked state - see _NEVER_MOVE's own comment for why.
        Returns (ids, excluded_count)."""
        ids = List[ElementId]()
        excluded = 0
        for r in self._rows:
            if not r.checked:
                continue
            if r.name.lower() in _NEVER_MOVE:
                excluded += len(self._id_map.get(r.name, []))
                continue
            for eid in self._id_map.get(r.name, []):
                ids.Add(eid)
        return ids, excluded

    def select_click(self, sender, args):
        checked_rows = [r for r in self._rows if r.checked]
        if not checked_rows:
            forms.alert("Tick at least one category first.", title="DeeASelect")
            return
        ids = self._checked_ids()
        if ids.Count == 0:
            forms.alert("Nothing to select.", title="DeeASelect")
            return
        if ids.Count >= _LARGE_SELECTION_WARN_THRESHOLD:
            proceed = forms.alert(
                u"This is a VERY large selection - {0:,} element(s) across "
                u"{1} categor(y/ies).\n\nSelecting this many elements at "
                u"once can make Revit unresponsive, and on some machines "
                u"has been reported to close Revit entirely with no error "
                u"message. Revit's title bar may show \"(Not Responding)\" "
                u"while this runs - for a selection this size that is "
                u"expected, not a crash; do not force-close Revit. Consider "
                u"unticking a few categories first (e.g. use 'Model Only' "
                u"or 'Annotation Only' instead of 'All').\n\n"
                u"Select anyway?".format(ids.Count, len(checked_rows)),
                title="DeeASelect - Large Selection", yes=True, no=True)
            if not proceed:
                return
        try:
            # An indeterminate spinner cannot animate during this single
            # blocking API call (same limitation noted on the scan above),
            # but it keeps a visible "still working" window up rather than
            # nothing at all while Revit's own title bar looks frozen.
            with _SafeProgress(title=u"DeeASelect - selecting {0:,} element(s)..."
                                .format(ids.Count), indeterminate=True):
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
        ids, excluded_never_move = self._move_ids()
        if ids.Count == 0:
            forms.alert("Nothing to move.", title="DeeASelect")
            return
        if ids.Count >= _LARGE_SELECTION_WARN_THRESHOLD:
            proceed = forms.alert(
                u"This is a VERY large move - {0:,} element(s) across "
                u"{1} categor(y/ies).\n\nMoving this many elements in one "
                u"operation can make Revit unresponsive, and on some "
                u"machines has been reported to close Revit entirely with "
                u"no error message. Revit's title bar may show "
                u"\"(Not Responding)\" while this runs - for a move this "
                u"size that is expected, not a crash; do not force-close "
                u"Revit. Consider unticking a few categories first (e.g. "
                u"use 'Model Only' or 'Annotation Only' instead of "
                u"'All').\n\nContinue anyway?"
                .format(ids.Count, len(checked_rows)),
                title="DeeASelect - Large Move", yes=True, no=True)
            if not proceed:
                return

        pinned_ids = _pinned_among(self.doc, ids)
        pin_note = (
            u"\n\n{0:,} of these are currently PINNED - DeeASelect will "
            u"unpin them, move everything, then pin those same elements "
            u"back automatically.".format(len(pinned_ids))
            if pinned_ids else u"")
        never_move_note = (
            u"\n\n{0:,} element(s) in Project Base Point / Survey Point were "
            u"EXCLUDED from this move - those anchor the model's coordinate "
            u"system and are never moved by DeeASelect, even when ticked."
            .format(excluded_never_move)
            if excluded_never_move else u"")
        join_count = _join_partners_outside(self.doc, ids)
        if join_count:
            join_note = (
                u"\n\n{0:,} of these are geometrically JOINED to an element "
                u"OUTSIDE this move - Revit may need to break those joins to "
                u"complete the move; DeeASelect will let that happen "
                u"automatically instead of stopping on it.".format(join_count))
        elif join_count is None and ids.Count > _JOIN_SCAN_MAX_IDS:
            join_note = (
                u"\n\nThis move is too large to pre-check for broken "
                u"geometry joins - any join warnings Revit raises will be "
                u"resolved automatically instead of stopping the move.")
        else:
            join_note = u""

        proceed = forms.alert(
            u"Move {0:,} element(s) across {1} categor(y/ies) by:\n\n"
            u"   X:  {2:+.2f} {4}\n   Y:  {3:+.2f} {4}{5}{6}{7}\n\n"
            u"This changes the model. Continue?".format(
                ids.Count, len(checked_rows), dx_disp, dy_disp, self._unit_abbr,
                pin_note, never_move_note, join_note),
            title="DeeASelect - Move", yes=True, no=True)
        if not proceed:
            return

        dx_internal = utils.display_to_internal(self.doc, dx_disp)
        dy_internal = utils.display_to_internal(self.doc, dy_disp)

        # Best-effort, outside the transaction - see _try_checkout_for_move's
        # own docstring for why this must never block the move on failure.
        _try_checkout_for_move(self.doc, ids)

        t = Transaction(self.doc, "DeeASelect - move selection")
        try:
            t.Start()
            # Attached AFTER Start() - deew_failure_handler's own docstring
            # says attaching before Start() silently fails. Turns the
            # Warning-severity "Can't keep elements joined" failure (and
            # any other warning) into a silent continue instead of an
            # interactive dialog blocking this transaction.
            ffh.apply_to_transaction(t)
            with _SafeProgress(title=u"DeeASelect - moving {0:,} element(s)..."
                                .format(ids.Count), indeterminate=True):
                move_elements(self.doc, ids, dx_internal, dy_internal, pinned_ids)
            t.Commit()
        except Exception as e:
            try:
                t.RollBack()
            except Exception:
                pass
            forms.alert(u"Move failed and was rolled back (any unpinning was "
                        u"rolled back too):\n{0}".format(e),
                        title="DeeASelect")
            return

        try:
            self.uidoc.Selection.SetElementIds(ids)
        except Exception:
            pass

        pinned_note = (u" ({0:,} were temporarily unpinned and re-pinned)"
                       .format(len(pinned_ids)) if pinned_ids else u"")
        excluded_note = (u" ({0:,} Project Base Point/Survey Point element(s) excluded)"
                          .format(excluded_never_move) if excluded_never_move else u"")
        self.status_tb.Text = (
            u"Moved {0:,} element(s) across {1} categor(y/ies) by X={2:+.2f}{4}, Y={3:+.2f}{4}{5}{6}."
            .format(ids.Count, len(checked_rows), dx_disp, dy_disp, self._unit_abbr,
                    pinned_note, excluded_note))
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
