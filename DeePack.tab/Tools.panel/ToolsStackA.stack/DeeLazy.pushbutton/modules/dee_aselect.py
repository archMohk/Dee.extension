# -*- coding: utf-8 -*-
"""
DeeLazy - DeeMoveMirror module
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
Mirror Selected Categories
--------------------------------------------------------------------
Same ticked-ids gathering and the exact same Pinned/_NEVER_MOVE/join/
workset/failure-handling safety net as Move (see _run_mirror_transaction,
which move_click's own logic was factored to mirror) - the only real
difference is WHAT replaces MoveElements (ElementTransformUtils.
MirrorElements with a Plane) and how that Plane's axis is chosen.
Always REPLACES the originals with their mirrored position - no "keep a
copy" option, by explicit request, so there is only ever one behaviour
to reason about (matches Move's own "changes the model in place" shape).

Two ways to choose the axis:
- Flip Around X / Flip Around Y (_flip_click) run immediately, inside
  the still-open window, exactly like Move - the axis is a horizontal
  (X) or vertical (Y) line through the middle of the ticked elements'
  own combined bounding box (_combined_bbox), so there is nothing to
  pick and no window-closing involved.
- Pick Line As Axis (mirror_pick_click) needs an actual Revit pick,
  which this codebase's hard rule says can never happen from inside an
  already-open WPF window. So this button stashes the ids/exclusions
  gathered so far on self.mirror_pick_request and closes the window;
  launch() (below) reads that request AFTER ShowDialog() returns - by
  then no modal is open - does the real PickObject, builds the axis
  Plane from the picked line's endpoints (_axis_plane_from_line_points,
  flattened to the horizontal plane since this mirrors plan geometry),
  and only then runs _run_mirror_transaction. This is the first place in
  this module that needs a "gather state in the window -> close ->
  pick -> resume with that state" flow; DeeLazy's own launcher and the
  Dee3DView hub only ever do "close -> launch a fresh, self-contained
  tool", so this is new, not a copy of an existing exact pattern.

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
5. Mirror (ElementTransformUtils.MirrorElements, all three entry points -
   Flip Around X/Y and Pick Line As Axis) is new and has not been
   exercised live at all yet - it reuses Move's pinned/join/checkout/
   failure-handling safety net, but that reuse itself is unverified
   in a real Revit session, and the pick-after-close flow in
   mirror_pick_click/launch()/_run_mirror_pick_flow is the first of its
   kind in this module (see the Mirror section above).
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

from Autodesk.Revit.UI.Selection import ObjectType
# Line is aliased - System.Windows.Shapes.Line (the WPF preview-arrow shape,
# imported above) already owns the plain name "Line" in this module.
from Autodesk.Revit.DB import (
    FilteredElementCollector, ElementId, CategoryType, Transaction,
    ElementTransformUtils, XYZ, JoinGeometryUtils, WorksharingUtils,
    Plane, Line as RevitLine,
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
# mirror
# ==========================================================================
def mirror_elements(doc, ids, plane, pinned_ids):
    """Same shape as move_elements() - unpin, transform, re-pin, inside the
    caller's transaction - because MirrorElements has the exact same
    all-or-nothing pinned-element restriction as MoveElements."""
    if pinned_ids:
        _set_pinned(doc, pinned_ids, False)
    ElementTransformUtils.MirrorElements(doc, ids, plane, False)
    if pinned_ids:
        _set_pinned(doc, pinned_ids, True)


def _combined_bbox(doc, ids):
    """Unions get_BoundingBox(None) across `ids`. Returns (minx, miny, maxx,
    maxy) in internal units, or None if none of them has a usable bounding
    box (e.g. every ticked category is something view-specific with no
    model geometry)."""
    result = None
    for eid in ids:
        el = doc.GetElement(eid)
        if el is None:
            continue
        try:
            bbox = el.get_BoundingBox(None)
        except Exception:
            bbox = None
        if bbox is None:
            continue
        if result is None:
            result = [bbox.Min.X, bbox.Min.Y, bbox.Max.X, bbox.Max.Y]
        else:
            result[0] = min(result[0], bbox.Min.X)
            result[1] = min(result[1], bbox.Min.Y)
            result[2] = max(result[2], bbox.Max.X)
            result[3] = max(result[3], bbox.Max.Y)
    return tuple(result) if result else None


def _flip_plane(axis, cx, cy):
    """axis: 'x' - mirror across a HORIZONTAL line through the middle
    (flips top/bottom, standard "mirror about the X axis" convention);
    'y' - mirror across a VERTICAL line through the middle (flips
    left/right). Both planes pass through the same (cx, cy) bbox-center
    point - only the normal direction differs."""
    normal = XYZ(0.0, 1.0, 0.0) if axis == "x" else XYZ(1.0, 0.0, 0.0)
    origin = XYZ(cx, cy, 0.0)
    return Plane.CreateByNormalAndOrigin(normal, origin)


def _axis_plane_from_line_points(p1, p2):
    """Builds the vertical mirror plane whose plan trace is the line
    p1->p2 - the Z components of p1/p2 are deliberately ignored (flattened
    to the horizontal plane) since DeeMoveMirror mirrors plan-view geometry,
    not the picked line's own elevation. Returns None if the line's
    horizontal projection has ~zero length (a purely vertical pick, e.g. a
    column edge) - that can't define a plan mirror axis."""
    dx = p2.X - p1.X
    dy = p2.Y - p1.Y
    length = math.sqrt(dx * dx + dy * dy)
    if length < 1e-9:
        return None
    hx, hy = dx / length, dy / length
    normal = XYZ(-hy, hx, 0.0)
    return Plane.CreateByNormalAndOrigin(normal, XYZ(p1.X, p1.Y, 0.0))


def _line_from_element(el):
    """The straight Line an element can be mirrored against, or None.
    Covers detail lines, model lines, walls, beams and similar - anything
    whose Location is a LocationCurve holding a straight Line (not an arc
    or spline, which MirrorElements' plane-based API can't use directly
    as an axis)."""
    try:
        loc = el.Location
        curve = getattr(loc, "Curve", None)
        if isinstance(curve, RevitLine):
            return curve
    except Exception:
        pass
    return None


def _run_mirror_transaction(doc, uidoc, ids, checked_count, excluded_never_move,
                             plane, axis_label, skip_size_warning=False):
    """Shared by the in-window Flip buttons and the Pick-Line flow (which
    runs from launch(), after the window has already closed - see that
    function's own docstring for why). Mirrors move_click()'s confirm/
    transaction/status shape exactly, with mirror-specific wording.

    skip_size_warning=True lets the Pick-Line flow show its large-count
    warning BEFORE the user spends effort picking a line (see
    mirror_pick_click), instead of asking again here after the pick."""
    if not skip_size_warning and ids.Count >= _LARGE_SELECTION_WARN_THRESHOLD:
        proceed = forms.alert(
            u"This is a VERY large mirror - {0:,} element(s) across "
            u"{1} categor(y/ies).\n\nMirroring this many elements in one "
            u"operation can make Revit unresponsive, and on some machines "
            u"has been reported to close Revit entirely with no error "
            u"message. Revit's title bar may show \"(Not Responding)\" "
            u"while this runs - for an operation this size that is "
            u"expected, not a crash; do not force-close Revit. Consider "
            u"unticking a few categories first (e.g. use 'Model Only' or "
            u"'Annotation Only' instead of 'All').\n\nContinue anyway?"
            .format(ids.Count, checked_count),
            title="DeeMoveMirror - Large Mirror", yes=True, no=True)
        if not proceed:
            return

    pinned_ids = _pinned_among(doc, ids)
    pin_note = (
        u"\n\n{0:,} of these are currently PINNED - DeeMoveMirror will "
        u"unpin them, mirror everything, then pin those same elements "
        u"back automatically.".format(len(pinned_ids))
        if pinned_ids else u"")
    never_move_note = (
        u"\n\n{0:,} element(s) in Project Base Point / Survey Point were "
        u"EXCLUDED from this mirror - those anchor the model's coordinate "
        u"system and are never moved by DeeMoveMirror, even when ticked."
        .format(excluded_never_move)
        if excluded_never_move else u"")
    join_count = _join_partners_outside(doc, ids)
    if join_count:
        join_note = (
            u"\n\n{0:,} of these are geometrically JOINED to an element "
            u"OUTSIDE this mirror - Revit may need to break those joins to "
            u"complete it; DeeMoveMirror will let that happen automatically "
            u"instead of stopping on it.".format(join_count))
    elif join_count is None and ids.Count > _JOIN_SCAN_MAX_IDS:
        join_note = (
            u"\n\nThis mirror is too large to pre-check for broken "
            u"geometry joins - any join warnings Revit raises will be "
            u"resolved automatically instead of stopping the mirror.")
    else:
        join_note = u""

    proceed = forms.alert(
        u"Mirror {0:,} element(s) across {1} categor(y/ies)\nAxis: {2}"
        u"{3}{4}{5}\n\nOriginals are REPLACED by their mirrored position "
        u"(no copy is kept). This changes the model. Continue?".format(
            ids.Count, checked_count, axis_label, pin_note, never_move_note, join_note),
        title="DeeMoveMirror - Mirror", yes=True, no=True)
    if not proceed:
        return

    _try_checkout_for_move(doc, ids)

    t = Transaction(doc, "DeeMoveMirror - mirror selection")
    try:
        t.Start()
        ffh.apply_to_transaction(t)
        with _SafeProgress(title=u"DeeMoveMirror - mirroring {0:,} element(s)..."
                            .format(ids.Count), indeterminate=True):
            mirror_elements(doc, ids, plane, pinned_ids)
        t.Commit()
    except Exception as e:
        try:
            t.RollBack()
        except Exception:
            pass
        forms.alert(u"Mirror failed and was rolled back (any unpinning was "
                    u"rolled back too):\n{0}".format(e),
                    title="DeeMoveMirror")
        return

    try:
        uidoc.Selection.SetElementIds(ids)
    except Exception:
        pass

    pinned_note = (u" ({0:,} were temporarily unpinned and re-pinned)"
                   .format(len(pinned_ids)) if pinned_ids else u"")
    excluded_note = (u" ({0:,} Project Base Point/Survey Point element(s) excluded)"
                      .format(excluded_never_move) if excluded_never_move else u"")
    forms.alert(
        u"Mirrored {0:,} element(s) across {1} categor(y/ies) - axis: {2}.{3}{4}"
        .format(ids.Count, checked_count, axis_label, pinned_note, excluded_note),
        title="DeeMoveMirror - Mirror")


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
        with _SafeProgress(title="DeeMoveMirror - scanning the project...",
                            indeterminate=False, cancellable=False) as pb:
            def _scan_progress(done, total):
                try:
                    pb.title = u"DeeMoveMirror - scanning... {0:,} of {1:,}".format(done, total)
                    pb.update_progress(done, total)
                except Exception:
                    pass
            self._rows, self._id_map = scan_categories(self.doc, progress_cb=_scan_progress)

        self._unit_abbr = utils.unit_abbreviation(self.doc)
        self.move_x_unit_tb.Text = self._unit_abbr
        self.move_y_unit_tb.Text = self._unit_abbr

        # Set by mirror_pick_click(), read by launch() AFTER ShowDialog()
        # returns - the actual PickObject call happens there, never from
        # inside this still-open window (this codebase's hard rule against
        # a second modal/PickObject from inside an open WPF window - see
        # DeeLazy's own launcher for the same close-first-dispatch-after
        # shape). None means no Pick-Line mirror was requested.
        self.mirror_pick_request = None

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
        Returns (ids, excluded_count). Shared by Move AND Mirror (via
        _flip_ids_or_none/mirror_pick_click) - both transform element
        positions, so the same exclusion applies to each."""
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
            forms.alert("Tick at least one category first.", title="DeeMoveMirror")
            return
        ids = self._checked_ids()
        if ids.Count == 0:
            forms.alert("Nothing to select.", title="DeeMoveMirror")
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
                title="DeeMoveMirror - Large Selection", yes=True, no=True)
            if not proceed:
                return
        try:
            # An indeterminate spinner cannot animate during this single
            # blocking API call (same limitation noted on the scan above),
            # but it keeps a visible "still working" window up rather than
            # nothing at all while Revit's own title bar looks frozen.
            with _SafeProgress(title=u"DeeMoveMirror - selecting {0:,} element(s)..."
                                .format(ids.Count), indeterminate=True):
                self.uidoc.Selection.SetElementIds(ids)
        except Exception as e:
            forms.alert(u"Revit refused the selection:\n{0}".format(e),
                        title="DeeMoveMirror")
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
            forms.alert("Tick at least one category first.", title="DeeMoveMirror")
            return
        dx_disp, dy_disp = self._move_xy_display()
        if dx_disp == 0.0 and dy_disp == 0.0:
            forms.alert("Enter a non-zero X or Y value to move by.", title="DeeMoveMirror")
            return
        ids, excluded_never_move = self._move_ids()
        if ids.Count == 0:
            forms.alert("Nothing to move.", title="DeeMoveMirror")
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
                title="DeeMoveMirror - Large Move", yes=True, no=True)
            if not proceed:
                return

        pinned_ids = _pinned_among(self.doc, ids)
        pin_note = (
            u"\n\n{0:,} of these are currently PINNED - DeeMoveMirror will "
            u"unpin them, move everything, then pin those same elements "
            u"back automatically.".format(len(pinned_ids))
            if pinned_ids else u"")
        never_move_note = (
            u"\n\n{0:,} element(s) in Project Base Point / Survey Point were "
            u"EXCLUDED from this move - those anchor the model's coordinate "
            u"system and are never moved by DeeMoveMirror, even when ticked."
            .format(excluded_never_move)
            if excluded_never_move else u"")
        join_count = _join_partners_outside(self.doc, ids)
        if join_count:
            join_note = (
                u"\n\n{0:,} of these are geometrically JOINED to an element "
                u"OUTSIDE this move - Revit may need to break those joins to "
                u"complete the move; DeeMoveMirror will let that happen "
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
            title="DeeMoveMirror - Move", yes=True, no=True)
        if not proceed:
            return

        dx_internal = utils.display_to_internal(self.doc, dx_disp)
        dy_internal = utils.display_to_internal(self.doc, dy_disp)

        # Best-effort, outside the transaction - see _try_checkout_for_move's
        # own docstring for why this must never block the move on failure.
        _try_checkout_for_move(self.doc, ids)

        t = Transaction(self.doc, "DeeMoveMirror - move selection")
        try:
            t.Start()
            # Attached AFTER Start() - deew_failure_handler's own docstring
            # says attaching before Start() silently fails. Turns the
            # Warning-severity "Can't keep elements joined" failure (and
            # any other warning) into a silent continue instead of an
            # interactive dialog blocking this transaction.
            ffh.apply_to_transaction(t)
            with _SafeProgress(title=u"DeeMoveMirror - moving {0:,} element(s)..."
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
                        title="DeeMoveMirror")
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

    # ---------------- mirror ----------------
    def _flip_ids_or_none(self):
        """Shared validation for both Flip buttons - ticks a category,
        gathers ids (excluding _NEVER_MOVE, same as Move), reports the
        friendly alerts move_click already uses for the same cases.
        Returns (ids, excluded_never_move, checked_count) or None."""
        checked_rows = [r for r in self._rows if r.checked]
        if not checked_rows:
            forms.alert("Tick at least one category first.", title="DeeMoveMirror")
            return None
        ids, excluded_never_move = self._move_ids()
        if ids.Count == 0:
            forms.alert("Nothing to mirror.", title="DeeMoveMirror")
            return None
        return ids, excluded_never_move, len(checked_rows)

    def _flip_click(self, axis):
        gathered = self._flip_ids_or_none()
        if gathered is None:
            return
        ids, excluded_never_move, checked_count = gathered
        bbox = _combined_bbox(self.doc, ids)
        if bbox is None:
            forms.alert(u"None of the ticked elements has a usable bounding "
                        u"box - nothing to mirror against.", title="DeeMoveMirror")
            return
        minx, miny, maxx, maxy = bbox
        cx, cy = (minx + maxx) / 2.0, (miny + maxy) / 2.0
        plane = _flip_plane(axis, cx, cy)
        if axis == "x":
            cy_disp = utils.internal_to_display(self.doc, cy)
            axis_label = (u"horizontal line at Y={0:.2f}{1} (flips top/bottom)"
                          .format(cy_disp, self._unit_abbr))
        else:
            cx_disp = utils.internal_to_display(self.doc, cx)
            axis_label = (u"vertical line at X={0:.2f}{1} (flips left/right)"
                          .format(cx_disp, self._unit_abbr))
        _run_mirror_transaction(self.doc, self.uidoc, ids, checked_count,
                                 excluded_never_move, plane, axis_label)
        if self.close_after_cb.IsChecked is True:
            self.Close()

    def flip_x_click(self, sender, args):
        self._flip_click("x")

    def flip_y_click(self, sender, args):
        self._flip_click("y")

    def mirror_pick_click(self, sender, args):
        """Closes the window and stashes what to mirror - launch() picks
        the axis line and finishes the job once ShowDialog() returns (see
        mirror_pick_request's own comment in __init__ for why: PickObject
        can never be called while this window is still open)."""
        gathered = self._flip_ids_or_none()
        if gathered is None:
            return
        ids, excluded_never_move, checked_count = gathered
        if ids.Count >= _LARGE_SELECTION_WARN_THRESHOLD:
            proceed = forms.alert(
                u"This is a VERY large mirror - {0:,} element(s) across "
                u"{1} categor(y/ies).\n\nMirroring this many elements in "
                u"one operation can make Revit unresponsive, and on some "
                u"machines has been reported to close Revit entirely with "
                u"no error message. Consider unticking a few categories "
                u"first (e.g. use 'Model Only' or 'Annotation Only' "
                u"instead of 'All').\n\nPick a mirror axis anyway?"
                .format(ids.Count, checked_count),
                title="DeeMoveMirror - Large Mirror", yes=True, no=True)
            if not proceed:
                return
        self.mirror_pick_request = {
            "ids": ids,
            "excluded_never_move": excluded_never_move,
            "checked_count": checked_count,
        }
        self.Close()

    def close_click(self, sender, args):
        self.Close()


# ==========================================================================
# Launch entry point (called by the DeeLazy home window)
# ==========================================================================
def _run_mirror_pick_flow(uiapp, request):
    """Runs AFTER DeeASelectWindow has fully closed (called from launch(),
    never from inside the window itself - see mirror_pick_request's own
    comment). Does the actual PickObject, since this is the first and only
    point in this module's flow where no WPF modal is open."""
    uidoc = uiapp.ActiveUIDocument
    doc = uidoc.Document
    try:
        ref = uidoc.Selection.PickObject(
            ObjectType.Element,
            u"Pick a straight line, wall, or beam to mirror across")
    except Exception:
        # Esc / right-click-cancel - same silent no-op as declining any
        # other DeeMoveMirror confirmation, not an error.
        return
    el = doc.GetElement(ref.ElementId)
    line = _line_from_element(el) if el is not None else None
    if line is None:
        forms.alert(u"That element doesn't have a usable straight line - "
                    u"pick a straight detail line, model line, wall, or "
                    u"beam instead.", title="DeeMoveMirror - Mirror")
        return
    plane = _axis_plane_from_line_points(line.GetEndPoint(0), line.GetEndPoint(1))
    if plane is None:
        forms.alert(u"That line runs straight up/down and can't define a "
                    u"plan mirror axis - pick a line that runs across the "
                    u"plan instead.", title="DeeMoveMirror - Mirror")
        return
    _run_mirror_transaction(doc, uidoc, request["ids"], request["checked_count"],
                             request["excluded_never_move"], plane,
                             u"the picked line", skip_size_warning=True)


def launch(uiapp):
    if uiapp.ActiveUIDocument is None:
        forms.alert("Open a Revit project first.")
        return
    window = DeeASelectWindow(_XAML_FILE, uiapp)
    window.ShowDialog()

    request = window.mirror_pick_request
    if request is not None:
        _run_mirror_pick_flow(uiapp, request)


TOOL_INFO = {
    "id": "dee_aselect",
    "title": "DeeMoveMirror",
    "description": "Select EVERY element in the project - model, annotation, links, everything - with a category checklist, one-click Model/Annotation presets, and X/Y move plus mirror (flip or pick-a-line) that keep dimensions and tags intact.",
    "launch": launch,
}
