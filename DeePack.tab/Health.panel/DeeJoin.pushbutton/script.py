# -*- coding: utf-8 -*-
"""
DeeJoin (Health)
Batch Join Geometry - Revit's own JoinGeometryUtils, run across many
elements at once instead of one pair at a time by hand. First use of
JoinGeometryUtils in this codebase.

Three modes, picked via radio buttons (see ui.xaml):
- Current View: every element visible in the active view, joined against
  every OTHER element in that same set whose bounding box actually
  overlaps it - simplest mode, no setup beyond Run Join.
- By Category: two independent category checklists (Group A / Group B,
  whole document, not view-scoped) - every checked-category element in A
  gets joined against every intersecting checked-category element in B.
- By Type: same shape as By Category, but each side's checklist is
  specific family/system TYPES within a category rather than whole
  categories - lets a very targeted join (e.g. just one wall type
  against just one floor type) without touching every other wall/floor.
Switching to By Category or By Type lazily scans the whole model ONCE
(_index_model) and builds both groupings (category->elements AND
type->elements) from that single pass, so picking one mode doesn't cost
a second full scan if the user also opens the other.

Not every category (or type) pair is geometrically joinable - a failed
JoinGeometry call is an expected, reportable outcome, never treated as a
crash (see _try_join). The end-of-run summary (Joined / Already joined /
Could not join) is the tool's only "report" - no pre-action confirmation
dialog, matching this codebase's established DeePack convention (see
project_deepack_ux_audit memory).

Spatial pre-filtering (see _find_candidate_pairs) reuses the exact
Outline + BoundingBoxIntersectsFilter + FilteredElementCollector(doc,
<id list>) technique DeeDistributor.pushbutton already proved live for
finding nearby elements - this keeps a few hundred/thousand-element join
run in the realm of "Revit's own native collector does the spatial
search," not an O(n^2) Python double loop. Still genuinely new, higher-
risk API surface for this codebase (first-ever JoinGeometryUtils call) -
needs real live testing on a real project to confirm this scales
acceptably, not just assumed safe from the design.
"""
import os

import clr
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
clr.AddReference("WindowsBase")
from System.Windows import Visibility, Thickness
from System.Windows.Controls import CheckBox
from System.Collections.Generic import List

from Autodesk.Revit.DB import (
    FilteredElementCollector, Transaction, Outline, BoundingBoxIntersectsFilter,
    ElementId, JoinGeometryUtils, XYZ)

from pyrevit import forms
import dee_branding
import dee_telemetry
dee_telemetry.check_access("DeeJoin")

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")

# A small tolerance (feet) so barely-touching elements still get
# considered - Revit's own bounding boxes are exact, and two elements
# meant to be flush can otherwise miss each other by float rounding.
_BBOX_PAD = 0.05


class _SafeProgress(object):
    """forms.ProgressBar tries to set Window.TaskbarItemInfo on its host
    window - throws NotImplementedException under Remote Desktop/no
    taskbar (see feedback_progressbar_before_window_shown memory).
    Copied verbatim from DeeHealth.pushbutton/script.py, the same
    already-fixed template every batch tool in this repo uses - falls
    back to running with no progress UI instead of crashing under RDP."""
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


def _bbox_outline(elem):
    """A padded Outline from elem's model-space bounding box, or None if
    it doesn't have one (most annotation-only categories) - doubles as a
    cheap "is this even shaped like something joinable" filter before any
    real join attempt."""
    try:
        bbox = elem.get_BoundingBox(None)
    except Exception:
        bbox = None
    if bbox is None:
        return None
    try:
        return Outline(
            XYZ(bbox.Min.X - _BBOX_PAD, bbox.Min.Y - _BBOX_PAD, bbox.Min.Z - _BBOX_PAD),
            XYZ(bbox.Max.X + _BBOX_PAD, bbox.Max.Y + _BBOX_PAD, bbox.Max.Z + _BBOX_PAD))
    except Exception:
        return None


def _view_candidates(doc, view):
    ids = []
    try:
        for e in FilteredElementCollector(doc, view.Id).WhereElementIsNotElementType():
            if _bbox_outline(e) is not None:
                ids.append(e.Id)
    except Exception:
        pass
    return ids


def _find_candidate_pairs(doc, group_a_ids, group_b_ids, same_group, pb=None):
    """Yields (id1, id2) pairs worth attempting a join on. For each A
    element, queries a FilteredElementCollector scoped to JUST
    group_b_ids with a BoundingBoxIntersectsFilter built from that A
    element's own bounding box (same Outline + BoundingBoxIntersectsFilter
    technique DeeDistributor.pushbutton already proved live) - the
    spatial search happens inside Revit's own collector, not a manual
    O(n^2) Python loop. same_group=True (Group A and B are the exact same
    element set) dedupes so (x, y) and (y, x) are never both yielded, and
    an element is never paired with itself either way."""
    b_id_list = List[ElementId](group_b_ids)
    total = len(group_a_ids)
    seen = set()
    for i, a_id in enumerate(group_a_ids):
        if pb is not None:
            pb.update_progress(i, total)
        a_elem = doc.GetElement(a_id)
        outline = _bbox_outline(a_elem) if a_elem is not None else None
        if outline is None:
            continue
        try:
            nearby_ids = FilteredElementCollector(doc, b_id_list).WherePasses(
                BoundingBoxIntersectsFilter(outline)).ToElementIds()
        except Exception:
            continue
        for b_id in nearby_ids:
            if b_id == a_id:
                continue
            if same_group:
                if a_id.IntegerValue < b_id.IntegerValue:
                    lo, hi = a_id, b_id
                else:
                    lo, hi = b_id, a_id
                key = (lo.IntegerValue, hi.IntegerValue)
                if key in seen:
                    continue
                seen.add(key)
                yield lo, hi
            else:
                yield a_id, b_id


def _try_join(doc, id1, id2, stats):
    """Never raises - not every category pair is geometrically joinable,
    and that's an expected, reportable outcome (stats["failed"]), not an
    error worth surfacing per-pair."""
    try:
        e1, e2 = doc.GetElement(id1), doc.GetElement(id2)
        if e1 is None or e2 is None:
            stats["failed"] += 1
            return
        if JoinGeometryUtils.AreElementsJoined(doc, e1, e2):
            stats["already"] += 1
            return
        JoinGeometryUtils.JoinGeometry(doc, e1, e2)
        stats["joined"] += 1
    except Exception:
        stats["failed"] += 1


def _run_join(doc, group_a_ids, group_b_ids, same_group):
    stats = {"joined": 0, "already": 0, "failed": 0}

    with _SafeProgress(title="DeeJoin - scanning {value} of {max_value}...", cancellable=True) as pb:
        pairs = list(_find_candidate_pairs(doc, group_a_ids, group_b_ids, same_group, pb))

    t = Transaction(doc, "DeeJoin - Join Geometry")
    t.Start()
    try:
        with _SafeProgress(title="DeeJoin - joining {value} of {max_value}...", cancellable=True) as pb:
            total = len(pairs)
            for i, (id1, id2) in enumerate(pairs):
                if pb.cancelled:
                    break
                pb.update_progress(i, total)
                _try_join(doc, id1, id2, stats)
        t.Commit()
    except Exception:
        t.RollBack()
        raise
    return stats


class DeeJoinWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, doc, view):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.doc = doc
        self.view = view
        self._model_indexed = False
        self._category_elements = {}   # category ElementId -> [element ids]
        self._type_elements = {}       # type ElementId -> [element ids]
        self._cat_a_checks = []
        self._cat_b_checks = []
        self._type_a_checks = []
        self._type_b_checks = []
        # Set in code, not XAML - mode_view_rb has a wired Checked handler
        # (mode_changed), and setting IsChecked from XAML on a control
        # with a wired handler can fire it before the rest of this window
        # is ready (see feedback_wpf_xaml_early_event_fire). Safe here
        # since this runs after every element mode_changed touches
        # already exists.
        self.mode_view_rb.IsChecked = True

    def mode_changed(self, sender, args):
        self.view_panel.Visibility = (
            Visibility.Visible if self.mode_view_rb.IsChecked else Visibility.Collapsed)
        self.category_panel.Visibility = (
            Visibility.Visible if self.mode_category_rb.IsChecked else Visibility.Collapsed)
        self.type_panel.Visibility = (
            Visibility.Visible if self.mode_type_rb.IsChecked else Visibility.Collapsed)
        if (self.mode_category_rb.IsChecked or self.mode_type_rb.IsChecked) and not self._model_indexed:
            self._index_model()

    def _index_model(self):
        """One full-document scan, shared by both By Category and By
        Type - builds category->elements AND type->elements together so
        switching between those two modes never costs a second scan."""
        self.status_tb.Text = u"Scanning model..."
        cat_map = {}
        type_map = {}
        try:
            with _SafeProgress(title="DeeJoin - indexing model...", cancellable=False):
                for e in FilteredElementCollector(self.doc).WhereElementIsNotElementType():
                    if _bbox_outline(e) is None:
                        continue
                    cat = e.Category
                    if cat is None:
                        continue
                    cat_id = cat.Id
                    if cat_id not in cat_map:
                        cat_map[cat_id] = [cat.Name, []]
                    cat_map[cat_id][1].append(e.Id)

                    try:
                        type_id = e.GetTypeId()
                    except Exception:
                        type_id = None
                    if type_id is not None and type_id != ElementId.InvalidElementId:
                        if type_id not in type_map:
                            type_elem = self.doc.GetElement(type_id)
                            type_name = getattr(type_elem, "Name", None) if type_elem else None
                            label = u"{0} — {1}".format(cat.Name, type_name or u"(unnamed type)")
                            type_map[type_id] = [label, []]
                        type_map[type_id][1].append(e.Id)
        except Exception as e:
            self.status_tb.Text = u"Could not scan the model: {0}".format(e)
            return

        self._category_elements = dict((cid, entry[1]) for cid, entry in cat_map.items())
        category_labels = sorted(
            [(entry[0], cid) for cid, entry in cat_map.items()], key=lambda x: x[0])
        self._type_elements = dict((tid, entry[1]) for tid, entry in type_map.items())
        type_labels = sorted(
            [(entry[0], tid) for tid, entry in type_map.items()], key=lambda x: x[0])

        self._cat_a_checks = self._populate_checklist(self.cat_a_panel, category_labels)
        self._cat_b_checks = self._populate_checklist(self.cat_b_panel, category_labels)
        self._type_a_checks = self._populate_checklist(self.type_a_panel, type_labels)
        self._type_b_checks = self._populate_checklist(self.type_b_panel, type_labels)
        self._model_indexed = True
        self.status_tb.Text = u""

    def _populate_checklist(self, panel, labels):
        panel.Children.Clear()
        checks = []
        for label, item_id in labels:
            cb = CheckBox()
            cb.Content = label
            cb.Tag = item_id
            cb.Margin = Thickness(0, 2, 0, 2)
            panel.Children.Add(cb)
            checks.append(cb)
        return checks

    def cat_a_filter_changed(self, sender, args):
        self._filter_checklist(self.cat_a_filter_tb, self._cat_a_checks)

    def cat_b_filter_changed(self, sender, args):
        self._filter_checklist(self.cat_b_filter_tb, self._cat_b_checks)

    def type_a_filter_changed(self, sender, args):
        self._filter_checklist(self.type_a_filter_tb, self._type_a_checks)

    def type_b_filter_changed(self, sender, args):
        self._filter_checklist(self.type_b_filter_tb, self._type_b_checks)

    def _filter_checklist(self, filter_tb, checks):
        query = (filter_tb.Text or u"").strip().lower()
        for cb in checks:
            text = (cb.Content or u"").lower()
            cb.Visibility = Visibility.Visible if query in text else Visibility.Collapsed

    def _checked_ids(self, checks):
        return set(cb.Tag for cb in checks if cb.IsChecked)

    def _elements_for(self, item_ids, mapping):
        result = []
        for item_id in item_ids:
            result.extend(mapping.get(item_id, []))
        return result

    def run_click(self, sender, args):
        if self.mode_view_rb.IsChecked:
            ids = _view_candidates(self.doc, self.view)
            if len(ids) < 2:
                forms.alert(u"Nothing to join - the current view has fewer than 2 joinable elements.")
                return
            stats = _run_join(self.doc, ids, ids, True)
        elif self.mode_category_rb.IsChecked:
            stats = self._run_grouped(self._cat_a_checks, self._cat_b_checks, self._category_elements,
                                       u"Pick at least one category in Group A and Group B.")
            if stats is None:
                return
        else:
            stats = self._run_grouped(self._type_a_checks, self._type_b_checks, self._type_elements,
                                       u"Pick at least one type in Group A and Group B.")
            if stats is None:
                return

        self.status_tb.Text = u"Joined: {0}   Already joined: {1}   Could not join: {2}".format(
            stats["joined"], stats["already"], stats["failed"])
        forms.alert(
            u"Joined: {0}\nAlready joined: {1}\nCould not join: {2}".format(
                stats["joined"], stats["already"], stats["failed"]),
            title="DeeJoin")

    def _run_grouped(self, a_checks, b_checks, mapping, empty_message):
        a_ids = self._checked_ids(a_checks)
        b_ids = self._checked_ids(b_checks)
        if not a_ids or not b_ids:
            forms.alert(empty_message, title="DeeJoin")
            return None
        a_elems = self._elements_for(a_ids, mapping)
        b_elems = self._elements_for(b_ids, mapping)
        if not a_elems or not b_elems:
            forms.alert(u"No elements found for the selected group(s).", title="DeeJoin")
            return None
        same_group = set(a_elems) == set(b_elems)
        return _run_join(self.doc, a_elems, b_elems, same_group)

    def close_click(self, sender, args):
        self.Close()


def main():
    uidoc = __revit__.ActiveUIDocument
    doc = uidoc.Document
    view = doc.ActiveView
    window = DeeJoinWindow(_XAML_FILE, doc, view)
    window.ShowDialog()


main()
