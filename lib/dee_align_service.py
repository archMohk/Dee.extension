# -*- coding: utf-8 -*-
"""
dee_align_service
Shared engine behind every DeeAlign.panel button (Align Left/Right/
Center/Top/Bottom, Distribute Horizontal/Vertical). Works on the
CURRENT SELECTION of arbitrary model/annotation elements - not tied to
sheets/viewports like DeeAligner's lib/align_tools.py, which is a
different tool for a different domain (2D sheet layout) and is
intentionally not reused here.

--------------------------------------------------------------------
Design for extensibility (per spec - many future commands are planned:
Align to View Center/Level/Grid/Work Plane/Reference Plane, Match
Rotation/Elevation/Offset, Equal Edge/Gap Distribution, Circular/Polar
Distribution, Align Along Curve/Path, Arrange in Grid/Matrix, Auto
Stack, Mirror and Align, Align by Family Origin/Insertion Point/
BoundingBox/Geometry Center, etc.)
--------------------------------------------------------------------
- Every alignment mode is just an entry in _ALIGN_AXIS/_bbox_metric -
  adding "align to view center" etc. later means adding one more mode
  string + one more _bbox_metric branch (or, for modes that don't fit
  the "reference bbox edge" shape at all, a new sibling function next
  to align_elements/distribute_elements) - never touching the per-
  button scripts or the movability/reporting plumbing below.
- The reference element is chosen via a small named-strategy registry
  (register_reference_strategy) instead of a hardcoded "elements[0]" -
  today only "first" is registered (per spec: "the first selected
  element shall always be used as the reference"), but "last",
  "largest", "smallest", "manual" can be added later as one function +
  one registration line each.
- Movability checks, bbox caching, the single Transaction, and the
  skip/summary reporting are all shared and mode-agnostic, so new
  commands automatically get the same robustness for free.

--------------------------------------------------------------------
Revit API facts relied on here (verified before writing, not guessed)
--------------------------------------------------------------------
- Element.get_BoundingBox(View) - passing None returns the element's
  model-space bounding box for ordinary model elements, but returns
  NULL for view-specific elements (Text Notes, Tags, Annotation
  Symbols, Filled Regions, Detail Items/Components, and view-hosted
  Image/CAD imports all only report a bounding box when given the
  actual view they're placed in). get_model_bounding_box() below tries
  None first, then falls back to doc.ActiveView - since anything the
  user could have selected is necessarily visible/selectable in the
  active view, this fallback covers exactly the annotation categories
  the spec requires supporting without hardcoding a category list.
- ElementTransformUtils.MoveElement(doc, ElementId, XYZ) is a generic
  translate that updates whatever Location the element has (point or
  curve) internally and preserves rotation/orientation by construction
  (it is a pure translation, never a rotation) - this is why alignment
  math here never needs to branch on LocationPoint vs LocationCurve;
  get_location_kind() exists only for reporting/future reference-
  point strategies, not because the move itself needs it.
- Element.GroupId != ElementId.InvalidElementId means the element is a
  MEMBER of a group (not the group instance itself) - Revit does not
  support moving individual group members without ungrouping, so these
  are skipped; a Group instance selected directly has GroupId ==
  InvalidElementId (unless it is itself nested inside a parent group)
  and moves like any other element.
- WorksharingUtils.GetCheckoutStatus(doc, ElementId) - only meaningful
  when doc.IsWorkshared; CheckoutStatus.OwnedByOtherUser means another
  user currently owns the element and it cannot be edited here.
- There is no generic Element.IsReadOnly API - "read-only" failures
  (design options, phase issues, view-specific edit restrictions, etc)
  are instead caught generically as a failed MoveElement call and
  reported with the real exception text, rather than guessed at with a
  property that doesn't exist.
"""
import time

from Autodesk.Revit.DB import (
    Transaction, ElementTransformUtils, XYZ, ElementId,
    LocationPoint, LocationCurve, RevitLinkInstance,
    WorksharingUtils, CheckoutStatus,
)

from pyrevit import forms, script

output = script.get_output()

_EPSILON = 1e-9


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
# Reference-element strategy registry (see module docstring)
# ==========================================================================
def _reference_first(elements, bbox_cache):
    return elements[0]


_REFERENCE_STRATEGIES = {
    "first": _reference_first,
}


def register_reference_strategy(name, fn):
    """fn(elements, bbox_cache) -> chosen reference element. Call this
    once (e.g. at module import time) to add "last"/"largest"/
    "smallest"/"manual" strategies later without touching pick_reference
    or any of the align/distribute functions below."""
    _REFERENCE_STRATEGIES[name] = fn


def pick_reference(elements, bbox_cache, strategy="first"):
    fn = _REFERENCE_STRATEGIES.get(strategy, _reference_first)
    return fn(elements, bbox_cache)


# ==========================================================================
# Element inspection - bounding box, movability, labeling
# ==========================================================================
def get_model_bounding_box(doc, element):
    try:
        bbox = element.get_BoundingBox(None)
        if bbox is not None:
            return bbox
    except Exception:
        pass
    try:
        view = doc.ActiveView
        if view is not None:
            return element.get_BoundingBox(view)
    except Exception:
        pass
    return None


def get_location_kind(element):
    """"point" / "curve" / "none" - informational only (see module
    docstring: the move itself doesn't need this)."""
    try:
        loc = element.Location
    except Exception:
        return "none"
    if isinstance(loc, LocationPoint):
        return "point"
    if isinstance(loc, LocationCurve):
        return "curve"
    return "none"


def check_movable(doc, element):
    """Returns (ok, reason). reason is None when ok is True."""
    if element is None:
        return False, "Element no longer exists"

    try:
        if isinstance(element, RevitLinkInstance):
            return False, "Linked model instance - not supported"
    except Exception:
        pass

    try:
        if element.Pinned:
            return False, "Pinned"
    except Exception:
        pass

    try:
        gid = element.GroupId
        if gid is not None and gid != ElementId.InvalidElementId:
            return False, "Member of a Group (select/move the Group itself instead)"
    except Exception:
        pass

    try:
        if doc.IsWorkshared:
            status = WorksharingUtils.GetCheckoutStatus(doc, element.Id)
            if status == CheckoutStatus.OwnedByOtherUser:
                return False, "Owned by another user (worksharing)"
    except Exception:
        pass

    return True, None


def element_label(element):
    try:
        cat_name = element.Category.Name if element.Category is not None else "Unknown Category"
    except Exception:
        cat_name = "Unknown Category"
    try:
        eid = element.Id.IntegerValue
    except Exception:
        eid = "?"
    name = None
    try:
        n = element.Name
        if n:
            name = n
    except Exception:
        pass
    if name:
        return "{0} - {1} (id {2})".format(cat_name, name, eid)
    return "{0} (id {1})".format(cat_name, eid)


# ==========================================================================
# Result / reporting
# ==========================================================================
class OperationResult(object):
    def __init__(self, action_title):
        self.action_title = action_title
        self.moved_count = 0
        self.skipped = []  # list of (label, reason)
        self.reference_label = None
        self.elapsed_seconds = 0.0
        self.error = None

    def add_skip(self, label, reason):
        self.skipped.append((label, reason))


def _is_distribute(action_title):
    return "Distribute" in action_title


def show_summary(result):
    """Logs the outcome to the pyRevit output console - no popup
    dialog after a successful run, per explicit user request (the
    action should just happen, not interrupt with a message to
    dismiss). Pre-condition failures (nothing selected, not enough
    elements) still use forms.alert since those happen BEFORE any
    action runs and the user would otherwise get no feedback at all
    about why nothing happened."""
    if result.error:
        forms.alert(result.error, title="DeeAlign - {0}".format(result.action_title))
        return
    _print_report(result)


def _print_report(result):
    header = "Distribution Completed" if _is_distribute(result.action_title) else "Alignment Completed"
    html = [
        '<h2 style="font-family:sans-serif;color:#ddd;">DeeAlign - {0}</h2>'.format(result.action_title),
        '<p style="color:#ddd;">{0} - Moved {1} - Skipped {2} - {3:.2f}s.</p>'.format(
            header, result.moved_count, len(result.skipped), result.elapsed_seconds),
    ]
    for label, reason in result.skipped:
        html.append(
            '<div style="padding:4px 10px;margin:2px 0;background:#c62828;color:#fff;'
            'border-radius:4px;font-family:monospace;font-size:12px;">'
            '&#10007;&nbsp; <b>{0}</b> &mdash; {1}</div>'.format(label, reason))
    output.print_html("".join(html))


# ==========================================================================
# Shared selection -> valid-elements-with-cached-bboxes pipeline
# ==========================================================================
def _collect_valid(doc, sel_ids, result):
    """Resolves ElementIds to elements, applies check_movable + bbox
    lookup once each, and returns (valid_elements, bbox_cache) - every
    align/distribute call does this exactly once, per the "cache
    BoundingBoxes, avoid recomputation" performance requirement."""
    bbox_cache = {}
    valid = []
    for eid in sel_ids:
        el = doc.GetElement(eid)
        ok, reason = check_movable(doc, el)
        if not ok:
            result.add_skip(element_label(el) if el is not None else "Element {0}".format(eid), reason)
            continue
        bbox = get_model_bounding_box(doc, el)
        if bbox is None:
            result.add_skip(element_label(el), "Missing bounding box")
            continue
        bbox_cache[el.Id.IntegerValue] = bbox
        valid.append(el)
    return valid, bbox_cache


# ==========================================================================
# Align Left / Right / Center / Top / Bottom
# ==========================================================================
_ALIGN_AXIS = {
    "left": "x", "right": "x", "center_x": "x",
    "top": "y", "bottom": "y", "middle": "y",
}


def _bbox_metric(bbox, mode):
    if mode == "left":
        return bbox.Min.X
    if mode == "right":
        return bbox.Max.X
    if mode == "center_x":
        return (bbox.Min.X + bbox.Max.X) / 2.0
    if mode == "top":
        return bbox.Max.Y
    if mode == "bottom":
        return bbox.Min.Y
    if mode == "middle":
        return (bbox.Min.Y + bbox.Max.Y) / 2.0
    raise ValueError("Unknown align mode: {0}".format(mode))


def align_elements(doc, uidoc, action_title, mode, reference_strategy="first"):
    start = time.time()
    result = OperationResult(action_title)

    sel_ids = list(uidoc.Selection.GetElementIds())
    if not sel_ids:
        result.error = "No elements selected."
        return result
    if len(sel_ids) < 2:
        result.error = "Select at least 2 elements - the first selected is used as the alignment reference."
        return result

    valid, bbox_cache = _collect_valid(doc, sel_ids, result)
    if len(valid) < 2:
        result.error = "Not enough movable elements selected (need at least 2 after skipping unsupported ones)."
        result.elapsed_seconds = time.time() - start
        return result

    reference = pick_reference(valid, bbox_cache, reference_strategy)
    result.reference_label = element_label(reference)
    ref_bbox = bbox_cache[reference.Id.IntegerValue]
    target_value = _bbox_metric(ref_bbox, mode)
    axis = _ALIGN_AXIS[mode]

    movers = [el for el in valid if el.Id != reference.Id]
    if not movers:
        result.elapsed_seconds = time.time() - start
        return result

    with _SafeProgress(title="DeeAlign - {0}...".format(action_title), indeterminate=True):
        t = Transaction(doc, "DeeAlign - {0}".format(action_title))
        t.Start()
        try:
            for el in movers:
                bbox = bbox_cache[el.Id.IntegerValue]
                delta = target_value - _bbox_metric(bbox, mode)
                try:
                    if abs(delta) > _EPSILON:
                        translation = XYZ(delta, 0, 0) if axis == "x" else XYZ(0, delta, 0)
                        ElementTransformUtils.MoveElement(doc, el.Id, translation)
                    result.moved_count += 1
                except Exception as e:
                    result.add_skip(element_label(el), "Move failed: {0}".format(e))
            t.Commit()
        except Exception:
            t.RollBack()
            raise

    result.elapsed_seconds = time.time() - start
    return result


# ==========================================================================
# Distribute Horizontal / Vertical
# ==========================================================================
def _center(bbox, axis):
    if axis == "x":
        return (bbox.Min.X + bbox.Max.X) / 2.0
    return (bbox.Min.Y + bbox.Max.Y) / 2.0


def distribute_elements(doc, uidoc, action_title, axis):
    start = time.time()
    result = OperationResult(action_title)

    sel_ids = list(uidoc.Selection.GetElementIds())
    if len(sel_ids) < 3:
        result.error = "Please select at least 3 elements."
        return result

    valid, bbox_cache = _collect_valid(doc, sel_ids, result)
    if len(valid) < 3:
        result.error = "Please select at least 3 elements. (Only {0} of the selected elements could be used.)".format(len(valid))
        result.elapsed_seconds = time.time() - start
        return result

    valid.sort(key=lambda el: _center(bbox_cache[el.Id.IntegerValue], axis))
    n = len(valid)
    first_center = _center(bbox_cache[valid[0].Id.IntegerValue], axis)
    last_center = _center(bbox_cache[valid[-1].Id.IntegerValue], axis)
    spacing = (last_center - first_center) / float(n - 1)

    result.reference_label = "{0} (fixed) .. {1} (fixed)".format(
        element_label(valid[0]), element_label(valid[-1]))

    with _SafeProgress(title="DeeAlign - {0}...".format(action_title), indeterminate=True):
        t = Transaction(doc, "DeeAlign - {0}".format(action_title))
        t.Start()
        try:
            for i in range(1, n - 1):
                el = valid[i]
                target = first_center + i * spacing
                delta = target - _center(bbox_cache[el.Id.IntegerValue], axis)
                try:
                    if abs(delta) > _EPSILON:
                        translation = XYZ(delta, 0, 0) if axis == "x" else XYZ(0, delta, 0)
                        ElementTransformUtils.MoveElement(doc, el.Id, translation)
                    result.moved_count += 1
                except Exception as e:
                    result.add_skip(element_label(el), "Move failed: {0}".format(e))
            t.Commit()
        except Exception:
            t.RollBack()
            raise

    result.elapsed_seconds = time.time() - start
    return result
