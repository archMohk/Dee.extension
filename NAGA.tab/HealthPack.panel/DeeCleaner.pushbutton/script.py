# -*- coding: utf-8 -*-
"""
DeeCleaner (HealthPack)
Three independent cleanup scans over the whole project:

1. Zero-Area Rooms - Rooms with Area <= 0 (typically Unplaced or Not
   Enclosed) - list + delete.
2. In-Place Families - model in-place family instances, with their
   category/level/location - list + isolate the checked ones in a
   dedicated 3D view (Revit's own Temporary Hide/Isolate, applied to a
   view named "DeeCleaner - In-Place Families").
3. Unused Groups - Model Group / Detail Group types with zero placed
   instances anywhere in the project - list + delete.

Needs live-Revit verification: View.IsolateElementsTemporary /
TemporaryViewMode enum usage for the isolate-view feature (the same
_ensure_view 3D-view helper already proven in DeeRehoster is reused
here, only the isolate call itself is new).
"""
import os

from pyrevit import forms, script
from Autodesk.Revit.DB import (
    FilteredElementCollector, BuiltInCategory, BuiltInParameter, ElementId,
    Transaction, View3D, ViewFamilyType, ViewFamily, TemporaryViewMode,
    UnitUtils, UnitTypeId, SpecTypeId, Group, GroupType, FamilyInstance,
)
from Autodesk.Revit.DB.Architecture import Room

from System.Collections.Generic import List

output = script.get_output()

_XAML_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.xaml")
_ISOLATE_VIEW_NAME = "DeeCleaner - In-Place Families"


# --------------------------------------------------------------------------
# Units
# --------------------------------------------------------------------------
def _length_unit_type_id(doc):
    try:
        return doc.GetUnits().GetFormatOptions(SpecTypeId.Length).GetUnitTypeId()
    except Exception:
        return UnitTypeId.Millimeters


def _internal_to_display(doc, value_internal):
    uid = _length_unit_type_id(doc)
    try:
        return UnitUtils.ConvertFromInternalUnits(value_internal, uid)
    except Exception:
        return UnitUtils.ConvertFromInternalUnits(value_internal, UnitTypeId.Millimeters)


_AREA_UNIT_ABBR = [
    (UnitTypeId.SquareMeters, "m2"),
    (UnitTypeId.SquareFeet, "ft2"),
]


def _format_area(doc, area_internal):
    try:
        uid = doc.GetUnits().GetFormatOptions(SpecTypeId.Area).GetUnitTypeId()
        val = UnitUtils.ConvertFromInternalUnits(area_internal, uid)
    except Exception:
        uid = UnitTypeId.SquareMeters
        val = UnitUtils.ConvertFromInternalUnits(area_internal, uid)
    abbr = "m2"
    for u, a in _AREA_UNIT_ABBR:
        if u == uid:
            abbr = a
            break
    return "{0:.2f} {1}".format(val, abbr)


# --------------------------------------------------------------------------
# Defensive reads (same pattern used throughout this extension)
# --------------------------------------------------------------------------
def _read_name(element):
    if element is None:
        return None
    try:
        n = element.Name
        if n:
            return n
    except Exception:
        pass
    for bip in (BuiltInParameter.SYMBOL_NAME_PARAM, BuiltInParameter.ALL_MODEL_TYPE_NAME,
                BuiltInParameter.DATUM_TEXT):
        try:
            p = element.get_Parameter(bip)
            if p is not None:
                val = p.AsString()
                if val:
                    return val
        except Exception:
            continue
    return None


def _read_room_number(room):
    try:
        n = room.Number
        if n:
            return n
    except Exception:
        pass
    return "?"


def _read_room_name(room):
    try:
        n = room.Name
        if n:
            return n
    except Exception:
        pass
    return "(unnamed)"


def _room_area_internal(room):
    try:
        return room.Area
    except Exception:
        return 0.0


def _instance_level_name(doc, instance):
    try:
        lvl_id = instance.LevelId
        if lvl_id and lvl_id != ElementId.InvalidElementId:
            n = _read_name(doc.GetElement(lvl_id))
            if n:
                return n
    except Exception:
        pass
    try:
        p = instance.get_Parameter(BuiltInParameter.FAMILY_LEVEL_PARAM)
        if p is not None:
            n = _read_name(doc.GetElement(p.AsElementId()))
            if n:
                return n
    except Exception:
        pass
    return "(none)"


def _instance_category_name(instance):
    try:
        cat = instance.Category
        if cat is not None and cat.Name:
            return cat.Name
    except Exception:
        pass
    return "(none)"


def _bbox_center(doc, element):
    try:
        bbox = element.get_BoundingBox(None)
        if bbox is not None:
            c = (bbox.Min + bbox.Max) * 0.5
            return c
    except Exception:
        pass
    return None


def _location_text(doc, element):
    c = _bbox_center(doc, element)
    if c is None:
        return "(unknown)"
    return "{0:.2f}, {1:.2f}, {2:.2f}".format(
        _internal_to_display(doc, c.X), _internal_to_display(doc, c.Y), _internal_to_display(doc, c.Z))


def _is_in_place(instance):
    try:
        family = instance.Symbol.Family
        return bool(family.IsInPlace)
    except Exception:
        return False


def _group_kind(grouptype):
    try:
        cat = grouptype.Category
        if cat is not None and cat.Name:
            return cat.Name
    except Exception:
        pass
    return "(unknown)"


# --------------------------------------------------------------------------
# 3D view helpers (same pattern already proven in DeeRehoster)
# --------------------------------------------------------------------------
def _as_element_id(value):
    if isinstance(value, ElementId):
        return value
    if hasattr(value, "Id"):
        return value.Id
    return value


def _find_3d_view(doc, name):
    for v in FilteredElementCollector(doc).OfClass(View3D):
        if not v.IsTemplate and v.Name == name:
            return v
    return None


def _ensure_3d_view_type(doc):
    for vft in FilteredElementCollector(doc).OfClass(ViewFamilyType):
        try:
            if vft.ViewFamily != ViewFamily.ThreeDimensional:
                continue
        except Exception:
            continue
        return _as_element_id(vft.Id)
    raise Exception("No usable 3D ViewFamilyType found in this project")


def _ensure_view(doc, name):
    view = _find_3d_view(doc, name)
    if view is not None:
        return view
    type_id = _ensure_3d_view_type(doc)
    view = View3D.CreateIsometric(doc, _as_element_id(type_id))
    view.Name = name
    return view


# --------------------------------------------------------------------------
# Scanning
# --------------------------------------------------------------------------
def _scan_zero_area_rooms(doc):
    rows = []
    for r in FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_Rooms):
        if not isinstance(r, Room):
            continue
        area = _room_area_internal(r)
        if area <= 0:
            rows.append(RoomZeroRow(r, doc))
    return rows


def _scan_inplace_families(doc):
    rows = []
    for fi in FilteredElementCollector(doc).OfClass(FamilyInstance):
        try:
            if _is_in_place(fi):
                rows.append(InPlaceRow(fi, doc))
        except Exception:
            continue
    return rows


def _scan_unused_groups(doc):
    used_type_ids = set()
    try:
        for g in FilteredElementCollector(doc).OfClass(Group):
            try:
                used_type_ids.add(g.GetTypeId())
            except Exception:
                continue
    except Exception:
        pass

    rows = []
    for gt in FilteredElementCollector(doc).OfClass(GroupType):
        try:
            if gt.Id in used_type_ids:
                continue
            rows.append(GroupRow(gt))
        except Exception:
            continue
    return rows


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------
class RoomZeroRow(object):
    def __init__(self, room, doc):
        self.room = room
        self.selected = False
        self.number = _read_room_number(room)
        self.name = _read_room_name(room)
        level = None
        try:
            level = room.Level
        except Exception:
            level = None
        self.level_name = _read_name(level) or "(no level)"
        self.area_text = _format_area(doc, _room_area_internal(room))


class InPlaceRow(object):
    def __init__(self, element, doc):
        self.element = element
        self.selected = False
        self.name = _read_name(element.Symbol.Family) or _read_name(element) or "(unnamed)"
        self.category_name = _instance_category_name(element)
        self.level_name = _instance_level_name(doc, element)
        self.location_text = _location_text(doc, element)


class GroupRow(object):
    def __init__(self, grouptype):
        self.grouptype = grouptype
        self.selected = False
        self.name = _read_name(grouptype) or "(unnamed)"
        self.kind = _group_kind(grouptype)
        self.instance_count_text = "0"


# --------------------------------------------------------------------------
# Window
# --------------------------------------------------------------------------
class DeeCleanerWindow(forms.WPFWindow):
    def __init__(self, xaml_file, doc):
        forms.WPFWindow.__init__(self, xaml_file)
        self.doc = doc
        self._room_rows = []
        self._inplace_rows = []
        self._group_rows = []

    def scan_click(self, sender, args):
        with forms.ProgressBar(title="DeeCleaner — scanning project...", cancellable=True):
            self._room_rows = _scan_zero_area_rooms(self.doc)
            self._inplace_rows = _scan_inplace_families(self.doc)
            self._group_rows = _scan_unused_groups(self.doc)

        self.rooms_grid.ItemsSource = None
        self.rooms_grid.ItemsSource = self._room_rows
        self.rooms_count_tb.Text = "{0} zero-area room(s)".format(len(self._room_rows))

        self.inplace_grid.ItemsSource = None
        self.inplace_grid.ItemsSource = self._inplace_rows
        self.inplace_count_tb.Text = "{0} in-place familie(s)".format(len(self._inplace_rows))

        self.groups_grid.ItemsSource = None
        self.groups_grid.ItemsSource = self._group_rows
        self.groups_count_tb.Text = "{0} unused group(s)".format(len(self._group_rows))

        self.status_tb.Text = "Scanned {0} zero-area room(s), {1} in-place familie(s), {2} unused group(s).".format(
            len(self._room_rows), len(self._inplace_rows), len(self._group_rows))

    def _refresh(self, grid, rows):
        grid.ItemsSource = None
        grid.ItemsSource = rows

    def _report(self, title, results):
        ok_count = sum(1 for ok, _, _ in results if ok)
        fail_count = len(results) - ok_count
        html = ['<h2 style="font-family:sans-serif;color:#ddd;">DeeCleaner - {0}</h2>'.format(title),
                '<p style="color:#ddd;">{0} succeeded, {1} failed.</p>'.format(ok_count, fail_count)]
        for ok, name, detail in results:
            bg = "#2e7d32" if ok else "#c62828"
            icon = "&#10003;" if ok else "&#10007;"
            html.append(
                '<div style="padding:4px 10px;margin:2px 0;background:{0};color:#fff;'
                'border-radius:4px;font-family:monospace;font-size:12px;">'
                '{1}&nbsp; <b>{2}</b> &mdash; {3}</div>'.format(bg, icon, name, detail))
        output.print_html("".join(html))

    # ---- Tab 1: Zero-Area Rooms ----
    def rooms_select_all_click(self, sender, args):
        for r in self._room_rows:
            r.selected = True
        self._refresh(self.rooms_grid, self._room_rows)

    def rooms_deselect_all_click(self, sender, args):
        for r in self._room_rows:
            r.selected = False
        self._refresh(self.rooms_grid, self._room_rows)

    def rooms_delete_click(self, sender, args):
        selected = [r for r in self._room_rows if r.selected]
        if not selected:
            forms.alert("Check at least one room to delete.")
            return
        if not forms.alert(
                "Delete {0} zero-area room(s)? This cannot be undone from this dialog.".format(len(selected)),
                title="DeeCleaner - Confirm", yes=True, no=True):
            return

        results = []
        t = Transaction(self.doc, "DeeCleaner - Delete Zero-Area Rooms")
        t.Start()
        for r in selected:
            try:
                self.doc.Delete(r.room.Id)
                results.append((True, "{0} - {1}".format(r.number, r.name), "Deleted"))
            except Exception as e:
                results.append((False, "{0} - {1}".format(r.number, r.name), "FAILED: {0}".format(e)))
        t.Commit()

        deleted_set = set(r for r, res in zip(selected, results) if res[0])
        self._room_rows = [r for r in self._room_rows if r not in deleted_set]
        self.rooms_count_tb.Text = "{0} zero-area room(s)".format(len(self._room_rows))
        self._refresh(self.rooms_grid, self._room_rows)
        self._report("Delete Zero-Area Rooms Results", results)

    # ---- Tab 2: In-Place Families ----
    def inplace_select_all_click(self, sender, args):
        for r in self._inplace_rows:
            r.selected = True
        self._refresh(self.inplace_grid, self._inplace_rows)

    def inplace_deselect_all_click(self, sender, args):
        for r in self._inplace_rows:
            r.selected = False
        self._refresh(self.inplace_grid, self._inplace_rows)

    def inplace_isolate_click(self, sender, args):
        selected = [r for r in self._inplace_rows if r.selected]
        if not selected:
            forms.alert("Check at least one in-place family to isolate.")
            return

        ids = List[ElementId]([r.element.Id for r in selected])
        t = Transaction(self.doc, "DeeCleaner - Isolate In-Place Families")
        t.Start()
        try:
            view = _ensure_view(self.doc, _ISOLATE_VIEW_NAME)
            try:
                if view.IsInTemporaryViewMode(TemporaryViewMode.TemporaryHideIsolate):
                    view.DisableTemporaryViewMode(TemporaryViewMode.TemporaryHideIsolate)
            except Exception:
                pass
            view.IsolateElementsTemporary(ids)
            t.Commit()
        except Exception as e:
            t.RollBack()
            forms.alert("Could not isolate elements: {0}".format(e))
            return

        forms.alert(
            "Isolated {0} element(s) in the '{1}' 3D view (Revit's Temporary Hide/Isolate - "
            "open that view to see them; reset or make it permanent from the View Control Bar "
            "at the bottom of the view).".format(len(selected), _ISOLATE_VIEW_NAME),
            title="DeeCleaner")

    # ---- Tab 3: Unused Groups ----
    def groups_select_all_click(self, sender, args):
        for r in self._group_rows:
            r.selected = True
        self._refresh(self.groups_grid, self._group_rows)

    def groups_deselect_all_click(self, sender, args):
        for r in self._group_rows:
            r.selected = False
        self._refresh(self.groups_grid, self._group_rows)

    def groups_delete_click(self, sender, args):
        selected = [r for r in self._group_rows if r.selected]
        if not selected:
            forms.alert("Check at least one group to delete.")
            return
        if not forms.alert(
                "Delete {0} unused group type(s)? This cannot be undone from this dialog.".format(len(selected)),
                title="DeeCleaner - Confirm", yes=True, no=True):
            return

        results = []
        t = Transaction(self.doc, "DeeCleaner - Delete Unused Groups")
        t.Start()
        for r in selected:
            try:
                self.doc.Delete(r.grouptype.Id)
                results.append((True, r.name, "Deleted"))
            except Exception as e:
                results.append((False, r.name, "FAILED: {0}".format(e)))
        t.Commit()

        deleted_set = set(r for r, res in zip(selected, results) if res[0])
        self._group_rows = [r for r in self._group_rows if r not in deleted_set]
        self.groups_count_tb.Text = "{0} unused group(s)".format(len(self._group_rows))
        self._refresh(self.groups_grid, self._group_rows)
        self._report("Delete Unused Groups Results", results)

    def close_click(self, sender, args):
        self.Close()


def main():
    doc = __revit__.ActiveUIDocument.Document
    window = DeeCleanerWindow(_XAML_FILE, doc)
    window.ShowDialog()


main()
