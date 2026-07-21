# -*- coding: utf-8 -*-
"""
DeeCordiPoint (LazyPack)
Creates/reuses a 3D view named "DeeLazy Coordinates View" and isolates
Levels, Site, the Project Base Point, Survey Point, and Internal Origin
in it (Revit's own Temporary Hide/Isolate), so you can see exactly
where the project's coordinate reference points sit relative to the
model.

Also offers:
- Relocate Survey Point: if the Survey Point is far from the Internal
  Origin (a common cause of Revit accuracy/rendering issues once a
  surveyor's real-world coordinates are acquired), un-clips it, moves
  it to coincide with your chosen target (Internal Origin or Project
  Base Point), then re-clips it - the standard Revit technique for
  bringing an inconveniently-far Survey Point back close to the model
  without changing its real-world coordinate value.
- Export Points to Excel: writes the Key Points grid to an xlsx file
  (via the shared lib/xlsx_writer.py).
- Scan/Export Linked Models: lists every RevitLinkInstance's placement
  origin relative to the host, plus that link's own Project Base
  Point/Survey Point if the link is currently loaded.

Needs live-Revit verification: the "Clipped" and "Angle to True North"
parameter names on BasePoint elements, and whether
ElementTransformUtils.MoveElement works on an unclipped BasePoint the
same way as a normal element.
"""
import os
import math

from pyrevit import forms, script
from Autodesk.Revit.DB import (
    FilteredElementCollector, BuiltInCategory, Category, ElementId, Transaction,
    View3D, ViewFamilyType, ViewFamily, TemporaryViewMode, XYZ, ElementTransformUtils,
    BasePoint, InternalOrigin, RevitLinkInstance, UnitUtils, UnitTypeId, SpecTypeId,
    BuiltInParameter,
)

from System.Collections.Generic import List
import clr
clr.AddReference("System.Windows.Forms")
from System.Windows.Forms import SaveFileDialog, DialogResult, MessageBox

import xlsx_writer

output = script.get_output()

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")
_VIEW_NAME = "DeeLazy Coordinates View"


# --------------------------------------------------------------------------
# Units
# --------------------------------------------------------------------------
def _length_unit_type_id(doc):
    try:
        return doc.GetUnits().GetFormatOptions(SpecTypeId.Length).GetUnitTypeId()
    except Exception:
        return UnitTypeId.Feet


def _internal_to_display(doc, value_internal):
    uid = _length_unit_type_id(doc)
    try:
        return UnitUtils.ConvertFromInternalUnits(value_internal, uid)
    except Exception:
        return UnitUtils.ConvertFromInternalUnits(value_internal, UnitTypeId.Feet)


def _display_to_internal(doc, value_display):
    uid = _length_unit_type_id(doc)
    try:
        return UnitUtils.ConvertToInternalUnits(value_display, uid)
    except Exception:
        return UnitUtils.ConvertToInternalUnits(value_display, UnitTypeId.Feet)


_UNIT_ABBR = [
    (UnitTypeId.Millimeters, "mm"),
    (UnitTypeId.Centimeters, "cm"),
    (UnitTypeId.Meters, "m"),
    (UnitTypeId.Feet, "ft"),
    (UnitTypeId.FeetFractionalInches, "ft"),
    (UnitTypeId.FractionalInches, "in"),
    (UnitTypeId.Inches, "in"),
]


def _unit_abbreviation(doc):
    uid = _length_unit_type_id(doc)
    for u, abbr in _UNIT_ABBR:
        if u == uid:
            return abbr
    return "ft"


# --------------------------------------------------------------------------
# Defensive reads
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
    for bip in (BuiltInParameter.SYMBOL_NAME_PARAM, BuiltInParameter.ALL_MODEL_TYPE_NAME):
        try:
            p = element.get_Parameter(bip)
            if p is not None:
                val = p.AsString()
                if val:
                    return val
        except Exception:
            continue
    return None


# --------------------------------------------------------------------------
# Coordinate points
# --------------------------------------------------------------------------
def _get_project_base_point(doc):
    try:
        return BasePoint.GetProjectBasePoint(doc)
    except Exception:
        return None


def _get_survey_point(doc):
    try:
        return BasePoint.GetSurveyPoint(doc)
    except Exception:
        return None


def _get_internal_origin(doc):
    try:
        return InternalOrigin.Get(doc)
    except Exception:
        return None


def _point_position(element):
    if element is None:
        return None
    try:
        pos = element.Position
        if pos is not None:
            return pos
    except Exception:
        pass
    try:
        loc = element.Location
        pt = getattr(loc, "Point", None)
        if pt is not None:
            return pt
    except Exception:
        pass
    return None


def _clipped_param(element):
    if element is None:
        return None
    try:
        return element.LookupParameter("Clipped")
    except Exception:
        return None


def _is_clipped(element):
    p = _clipped_param(element)
    if p is None:
        return None
    try:
        return bool(p.AsInteger())
    except Exception:
        return None


def _angle_to_true_north(element):
    if element is None:
        return None
    try:
        p = element.LookupParameter("Angle to True North")
        if p is not None:
            return p.AsDouble()
    except Exception:
        pass
    return None


def _format_point_text(doc, pos):
    if pos is None:
        return "(n/a)"
    return "{0:.1f}, {1:.1f}, {2:.1f}".format(
        _internal_to_display(doc, pos.X), _internal_to_display(doc, pos.Y), _internal_to_display(doc, pos.Z))


# --------------------------------------------------------------------------
# 3D view helpers (same pattern already proven in DeeRehoster/DeeCleaner)
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


def _build_coordinates_view(doc, pbp, sp, io):
    view = _ensure_view(doc, _VIEW_NAME)

    ids = []
    for el in (pbp, sp, io):
        if el is not None:
            ids.append(el.Id)

    for bic in (BuiltInCategory.OST_Levels, BuiltInCategory.OST_Site):
        try:
            cat = Category.GetCategory(doc, bic)
            if cat is not None:
                try:
                    if view.GetCategoryHidden(cat.Id):
                        view.SetCategoryHidden(cat.Id, False)
                except Exception:
                    pass
        except Exception:
            pass
        try:
            for el in FilteredElementCollector(doc, view.Id).OfCategory(bic).WhereElementIsNotElementType():
                ids.append(el.Id)
        except Exception:
            continue

    if not ids:
        raise Exception("Could not find any Levels/Site elements or coordinate points to isolate.")

    try:
        if view.IsInTemporaryViewMode(TemporaryViewMode.TemporaryHideIsolate):
            view.DisableTemporaryViewMode(TemporaryViewMode.TemporaryHideIsolate)
    except Exception:
        pass
    view.IsolateElementsTemporary(List[ElementId](ids))
    return view


def _relocate_survey_point(doc, sp, target_pos):
    sp_pos = _point_position(sp)
    if sp_pos is None:
        raise Exception("Could not read the Survey Point's current position.")
    clip_param = _clipped_param(sp)
    was_clipped = _is_clipped(sp)
    if was_clipped and clip_param is not None:
        clip_param.Set(0)
        doc.Regenerate()
    dx = target_pos.X - sp_pos.X
    dy = target_pos.Y - sp_pos.Y
    dz = target_pos.Z - sp_pos.Z
    ElementTransformUtils.MoveElement(doc, sp.Id, XYZ(dx, dy, dz))
    doc.Regenerate()
    if was_clipped and clip_param is not None:
        clip_param.Set(1)
        doc.Regenerate()


def _scan_link_points(doc):
    rows = []
    for link in FilteredElementCollector(doc).OfClass(RevitLinkInstance):
        try:
            link_type = doc.GetElement(link.GetTypeId())
            name = _read_name(link_type) or _read_name(link) or "(unnamed link)"
        except Exception:
            name = "(unnamed link)"
        origin = None
        try:
            origin = link.GetTotalTransform().Origin
        except Exception:
            origin = None
        own_pbp_pos = None
        own_survey_pos = None
        try:
            link_doc = link.GetLinkDocument()
            if link_doc is not None:
                own_pbp_pos = _point_position(_get_project_base_point(link_doc))
                own_survey_pos = _point_position(_get_survey_point(link_doc))
        except Exception:
            pass
        rows.append(LinkRow(doc, name, origin, own_pbp_pos, own_survey_pos))
    return rows


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------
class PointRow(object):
    def __init__(self, doc, name, element):
        self.name = name
        self.element = element
        pos = _point_position(element)
        self.position = pos
        if pos is not None:
            self.x_text = "{0:.3f}".format(_internal_to_display(doc, pos.X))
            self.y_text = "{0:.3f}".format(_internal_to_display(doc, pos.Y))
            self.z_text = "{0:.3f}".format(_internal_to_display(doc, pos.Z))
        else:
            self.x_text = self.y_text = self.z_text = "(unknown)"
        angle = _angle_to_true_north(element)
        self.angle_text = "{0:.2f} deg".format(math.degrees(angle)) if angle is not None else "N/A"
        clipped = _is_clipped(element)
        self.clipped_text = "Yes" if clipped is True else ("No" if clipped is False else "N/A")


class LinkRow(object):
    def __init__(self, doc, name, origin, own_pbp_pos, own_survey_pos):
        self.name = name
        if origin is not None:
            self.origin_x_text = "{0:.3f}".format(_internal_to_display(doc, origin.X))
            self.origin_y_text = "{0:.3f}".format(_internal_to_display(doc, origin.Y))
            self.origin_z_text = "{0:.3f}".format(_internal_to_display(doc, origin.Z))
        else:
            self.origin_x_text = self.origin_y_text = self.origin_z_text = "(unknown)"
        self.own_pbp_text = _format_point_text(doc, own_pbp_pos)
        self.own_survey_text = _format_point_text(doc, own_survey_pos)


# --------------------------------------------------------------------------
# Window
# --------------------------------------------------------------------------
class DeeCordiPointWindow(forms.WPFWindow):
    def __init__(self, xaml_file, doc):
        forms.WPFWindow.__init__(self, xaml_file)
        self.doc = doc
        self._points = []
        self._links = []
        self.threshold_unit_tb.Text = _unit_abbreviation(doc)
        self._refresh_points()

    def _refresh_points(self):
        pbp = _get_project_base_point(self.doc)
        sp = _get_survey_point(self.doc)
        io = _get_internal_origin(self.doc)
        rows = [
            PointRow(self.doc, "Internal Origin", io),
            PointRow(self.doc, "Project Base Point", pbp),
            PointRow(self.doc, "Survey Point", sp),
        ]
        self._points = rows
        self.points_grid.ItemsSource = None
        self.points_grid.ItemsSource = rows

        io_pos = _point_position(io)
        sp_pos = _point_position(sp)
        if io_pos is not None and sp_pos is not None:
            dist_internal = io_pos.DistanceTo(sp_pos)
            dist_display = _internal_to_display(self.doc, dist_internal)
            self.distance_tb.Text = "Survey Point is {0:.1f} {1} from the Internal Origin.".format(
                dist_display, _unit_abbreviation(self.doc))
        else:
            self.distance_tb.Text = ""

    def _safe_float(self, text, default):
        try:
            return float(text)
        except Exception:
            return default

    def _check_threshold_warning(self):
        io_pos = _point_position(_get_internal_origin(self.doc))
        sp_pos = _point_position(_get_survey_point(self.doc))
        if io_pos is None or sp_pos is None:
            return
        threshold_display = self._safe_float(self.threshold_tb.Text, 10000.0)
        threshold_internal = _display_to_internal(self.doc, threshold_display)
        dist = io_pos.DistanceTo(sp_pos)
        if dist > threshold_internal:
            forms.alert(
                "The Survey Point is {0:.1f} {1} from the Internal Origin, over your {2:.0f} {1} "
                "threshold. Consider using Relocate Survey Point below.".format(
                    _internal_to_display(self.doc, dist), _unit_abbreviation(self.doc), threshold_display),
                title="DeeCordiPoint")

    def build_view_click(self, sender, args):
        pbp = _get_project_base_point(self.doc)
        sp = _get_survey_point(self.doc)
        io = _get_internal_origin(self.doc)
        t = Transaction(self.doc, "DeeCordiPoint - Build Coordinates View")
        t.Start()
        try:
            _build_coordinates_view(self.doc, pbp, sp, io)
            t.Commit()
        except Exception as e:
            t.RollBack()
            forms.alert("Could not build the Coordinates view: {0}".format(e))
            return
        self.view_status_tb.Text = (
            "'{0}' is ready - isolated Levels, Site, and the coordinate points "
            "(Revit's Temporary Hide/Isolate).".format(_VIEW_NAME))
        self._refresh_points()
        self._check_threshold_warning()

    def export_points_click(self, sender, args):
        if not self._points:
            forms.alert("Nothing to export yet.")
            return
        dlg = SaveFileDialog()
        dlg.Filter = "Excel Workbook (*.xlsx)|*.xlsx"
        dlg.FileName = "DeeCordiPoint_Points.xlsx"
        if dlg.ShowDialog() != DialogResult.OK:
            return
        rows = [([r.name, r.x_text, r.y_text, r.z_text, r.angle_text, r.clipped_text], None)
                for r in self._points]
        try:
            xlsx_writer.write_themed_xlsx(
                dlg.FileName, "DeeCordiPoint - Coordinate Points",
                ["Point", "X", "Y", "Z", "Angle to True North", "Clipped"],
                [22, 14, 14, 14, 20, 12], rows)
        except Exception as e:
            forms.alert("Could not export: {0}".format(e))
            return
        MessageBox.Show("Exported {0} row(s) to:\n{1}".format(len(rows), dlg.FileName), "DeeCordiPoint")

    def relocate_to_origin_click(self, sender, args):
        self._relocate("origin")

    def relocate_to_pbp_click(self, sender, args):
        self._relocate("pbp")

    def _relocate(self, target_kind):
        sp = _get_survey_point(self.doc)
        if sp is None:
            forms.alert("Could not find the Survey Point in this project.")
            return
        target_el = _get_internal_origin(self.doc) if target_kind == "origin" else _get_project_base_point(self.doc)
        target_label = "the Internal Origin" if target_kind == "origin" else "the Project Base Point"
        target_pos = _point_position(target_el)
        if target_pos is None:
            forms.alert("Could not read the position of {0}.".format(target_label))
            return

        if not forms.alert(
                "Un-clip the Survey Point, move it to coincide with {0}, then re-clip it?\n\n"
                "Its real-world coordinate value will not change - only its on-screen distance "
                "from the model.".format(target_label),
                title="DeeCordiPoint - Confirm", yes=True, no=True):
            return

        t = Transaction(self.doc, "DeeCordiPoint - Relocate Survey Point")
        t.Start()
        try:
            _relocate_survey_point(self.doc, sp, target_pos)
            t.Commit()
        except Exception as e:
            t.RollBack()
            forms.alert("Could not relocate the Survey Point: {0}".format(e))
            return

        forms.alert("Survey Point relocated to {0}.".format(target_label), title="DeeCordiPoint")
        self._refresh_points()

    def scan_links_click(self, sender, args):
        rows = _scan_link_points(self.doc)
        self._links = rows
        self.links_grid.ItemsSource = None
        self.links_grid.ItemsSource = rows
        self.links_status_tb.Text = "{0} link(s).".format(len(rows))

    def export_links_click(self, sender, args):
        if not self._links:
            forms.alert("Scan Linked Models first - nothing to export yet.")
            return
        dlg = SaveFileDialog()
        dlg.Filter = "Excel Workbook (*.xlsx)|*.xlsx"
        dlg.FileName = "DeeCordiPoint_Links.xlsx"
        if dlg.ShowDialog() != DialogResult.OK:
            return
        rows = [([r.name, r.origin_x_text, r.origin_y_text, r.origin_z_text,
                  r.own_pbp_text, r.own_survey_text], None) for r in self._links]
        try:
            xlsx_writer.write_themed_xlsx(
                dlg.FileName, "DeeCordiPoint - Linked Models",
                ["Link Name", "Origin X", "Origin Y", "Origin Z", "Own Project Base Point", "Own Survey Point"],
                [30, 14, 14, 14, 26, 26], rows)
        except Exception as e:
            forms.alert("Could not export: {0}".format(e))
            return
        MessageBox.Show("Exported {0} row(s) to:\n{1}".format(len(rows), dlg.FileName), "DeeCordiPoint")

    def close_click(self, sender, args):
        self.Close()


def main():
    doc = __revit__.ActiveUIDocument.Document
    window = DeeCordiPointWindow(_XAML_FILE, doc)
    window.ShowDialog()


main()
