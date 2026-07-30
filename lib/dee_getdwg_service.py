# -*- coding: utf-8 -*-
"""
dee_getdwg_service
Finds CAD Import/Link instances (ImportInstance - covers both "Import
CAD" and "Link CAD", distinguished only by .IsLinked) placed far from
the document's Internal Origin, and lets the user Unpin + move the
checked ones back to (0,0,0) in one Transaction - the standard fix for
Revit's "very far from the origin" warning and the view-navigation/
performance problems a stray DWG/DXF inserted at its own arbitrary
huge coordinates causes.

--------------------------------------------------------------------
Revit API facts relied on here (verified before writing, not guessed)
--------------------------------------------------------------------
- Both "Import CAD" and "Link CAD" produce an ImportInstance element -
  ImportInstance.IsLinked (bool) is the only difference: True for a
  Link, False for an Import. FilteredElementCollector(doc).OfClass(
  ImportInstance) finds both kinds in one pass.
- Instance.GetTransform() returns the element's placement Transform in
  the document's INTERNAL coordinate system - .Origin is exactly the
  point needed. The Internal Origin is BY DEFINITION (0,0,0) in that
  same system (unlike the Project Base Point / Survey Point, which can
  be relocated) - so "distance from Internal Origin" is simply
  transform.Origin.DistanceTo(XYZ.Zero), no separate lookup needed.
- ElementTransformUtils.MoveElement(doc, id, translation) is a pure
  translation - moving the transform's Origin to (0,0,0) this way never
  touches rotation/scale, matching lib/dee_align_service.py's
  "translate only" convention already established for DeeAlign.
- A CAD instance's file name is not reliably on Element.Name - its
  ElementType (a CADLinkType for both Import and Link cases) is the
  reliable source, with Element.Name as a fallback.
"""
import time

from Autodesk.Revit.DB import (
    FilteredElementCollector, ImportInstance, Transaction,
    ElementTransformUtils, XYZ, ElementId,
    WorksharingUtils, CheckoutStatus,
    UnitUtils, UnitTypeId, SpecTypeId,
)

from pyrevit import script

output = script.get_output()

_EPSILON = 1e-9
DEFAULT_THRESHOLD_DISPLAY = 1000.0


# ==========================================================================
# Units (same small pattern already proven in DeeAligner/DeeReLevel/DeeLazy)
# ==========================================================================
def _length_unit_type_id(doc):
    try:
        return doc.GetUnits().GetFormatOptions(SpecTypeId.Length).GetUnitTypeId()
    except Exception:
        return UnitTypeId.Feet


def internal_to_display(doc, value_internal):
    uid = _length_unit_type_id(doc)
    try:
        return UnitUtils.ConvertFromInternalUnits(value_internal, uid)
    except Exception:
        return value_internal


def display_to_internal(doc, value_display):
    uid = _length_unit_type_id(doc)
    try:
        return UnitUtils.ConvertToInternalUnits(value_display, uid)
    except Exception:
        return value_display


_UNIT_ABBR = [
    (UnitTypeId.Millimeters, "mm"),
    (UnitTypeId.Centimeters, "cm"),
    (UnitTypeId.Meters, "m"),
    (UnitTypeId.Feet, "ft"),
    (UnitTypeId.FeetFractionalInches, "ft"),
    (UnitTypeId.FractionalInches, "in"),
    (UnitTypeId.Inches, "in"),
]


def unit_abbreviation(doc):
    uid = _length_unit_type_id(doc)
    for u, abbr in _UNIT_ABBR:
        if u == uid:
            return abbr
    return "ft"


def safe_float(text, default=0.0):
    try:
        return float(text)
    except Exception:
        return default


# ==========================================================================
# Scan
# ==========================================================================
def _read_file_name(doc, element):
    """CAD file names live on the ElementType (a CADLinkType, for both
    Import and Link instances), not reliably on the instance itself."""
    try:
        el_type = doc.GetElement(element.GetTypeId())
        if el_type is not None:
            n = el_type.Name
            if n:
                return n
    except Exception:
        pass
    try:
        n = element.Name
        if n:
            return n
    except Exception:
        pass
    return "(unnamed CAD instance)"


class CadRow(object):
    """One CAD Import/Link instance - position/pinned state is
    snapshotted at scan/refresh time (never mutated in place by the
    move itself; refresh() re-reads from Revit so the grid always
    reflects ground truth after an action)."""

    def __init__(self, doc, element):
        self.doc = doc
        self.element = element
        self.id = element.Id
        self.selected = False
        try:
            self.is_linked = bool(element.IsLinked)
        except Exception:
            self.is_linked = False
        self.kind_text = "Linked" if self.is_linked else "Imported"
        self.file_name = _read_file_name(doc, element)
        self.origin = XYZ.Zero
        self.distance_internal = 0.0
        self.pinned = False
        self.refresh()

    def refresh(self):
        try:
            self.origin = self.element.GetTransform().Origin
        except Exception:
            self.origin = XYZ.Zero
        self.distance_internal = self.origin.DistanceTo(XYZ.Zero)
        try:
            self.pinned = bool(self.element.Pinned)
        except Exception:
            self.pinned = False

    @property
    def pinned_text(self):
        return "Yes" if self.pinned else "No"

    @property
    def distance_text(self):
        val = internal_to_display(self.doc, self.distance_internal)
        return "{0:,.1f} {1}".format(val, unit_abbreviation(self.doc))

    @property
    def position_text(self):
        d = self.doc
        x = internal_to_display(d, self.origin.X)
        y = internal_to_display(d, self.origin.Y)
        z = internal_to_display(d, self.origin.Z)
        return "X {0:,.0f}, Y {1:,.0f}, Z {2:,.0f}".format(x, y, z)


def scan(doc):
    rows = []
    for el in FilteredElementCollector(doc).OfClass(ImportInstance):
        try:
            rows.append(CadRow(doc, el))
        except Exception:
            continue
    rows.sort(key=lambda r: -r.distance_internal)
    return rows


# ==========================================================================
# Move to Internal Origin
# ==========================================================================
def check_movable(doc, element):
    """Returns (ok, reason). reason is None when ok is True."""
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


class MoveResult(object):
    def __init__(self):
        self.moved_count = 0
        self.skipped = []  # list of (label, reason)
        self.elapsed_seconds = 0.0

    def add_skip(self, label, reason):
        self.skipped.append((label, reason))


def move_to_origin(doc, rows):
    """rows: the already-checked CadRow list. Unpins (if needed) and
    translates each back to the Internal Origin (0,0,0) - a pure
    translation, so rotation/scale are always preserved exactly - all
    inside ONE Transaction, matching this extension's "single
    transaction per command" convention. Never raises for a single bad
    row - failures are collected and reported, the rest still run."""
    start = time.time()
    result = MoveResult()

    t = Transaction(doc, "DeeGetDWG - Move CAD to Internal Origin")
    t.Start()
    try:
        for row in rows:
            el = row.element
            ok, reason = check_movable(doc, el)
            if not ok:
                result.add_skip(row.file_name, reason)
                continue
            try:
                if row.pinned:
                    el.Pinned = False
                translation = XYZ.Zero - row.origin
                if translation.GetLength() > _EPSILON:
                    ElementTransformUtils.MoveElement(doc, el.Id, translation)
                result.moved_count += 1
            except Exception as e:
                result.add_skip(row.file_name, "Move failed: {0}".format(e))
        t.Commit()
    except Exception:
        t.RollBack()
        raise

    for row in rows:
        row.refresh()

    result.elapsed_seconds = time.time() - start
    return result


def print_report(result):
    """Logs to the pyRevit output console - no popup dialog after a
    successful run, matching the same preference already applied to
    DeeAlign (the action should just happen, not interrupt with a
    message to dismiss)."""
    html = [
        '<h2 style="font-family:sans-serif;color:#ddd;">DeeGetDWG - Move CAD to Internal Origin</h2>',
        '<p style="color:#ddd;">Moved {0} - Skipped {1} - {2:.2f}s.</p>'.format(
            result.moved_count, len(result.skipped), result.elapsed_seconds),
    ]
    for label, reason in result.skipped:
        html.append(
            '<div style="padding:4px 10px;margin:2px 0;background:#c62828;color:#fff;'
            'border-radius:4px;font-family:monospace;font-size:12px;">'
            '&#10007;&nbsp; <b>{0}</b> &mdash; {1}</div>'.format(label, reason))
    output.print_html("".join(html))
