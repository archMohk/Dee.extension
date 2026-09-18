# -*- coding: utf-8 -*-
"""
DeeFamily
Browses every loaded family TYPE in the project with a real thumbnail per
type (not just per family - different sizes within one family can look
different, matching Revit's own Type Selector). Tiles are grouped under a
header per Family, filterable by Discipline (a best-effort bucket this
module maintains itself - Revit has no public "discipline" field on
Category, so this is a maintained lookup table, not a Revit API value),
and searchable by name/family/category. Dragging a tile places that type
into the model, the same way dragging a type out of Project Browser does.

--------------------------------------------------------------------
Thumbnails - ElementType.GetPreviewImage, confirmed via Autodesk's own
API docs before writing any code (in the Revit API since 2011, stable)
--------------------------------------------------------------------
FamilySymbol.GetPreviewImage(System.Drawing.Size) returns a
System.Drawing.Bitmap (or None) - the exact image Revit's own UI shows
when picking a type. No temp views, no rendering, no reading the .rfa
file's internals - one direct API call per type.

That Bitmap is a WinForms/GDI+ object; this tool's UI is WPF, so each one
is converted to a System.Windows.Media.Imaging.BitmapSource via the
standard CreateBitmapSourceFromHBitmap technique. The HBITMAP handle
CreateBitmapSourceFromHBitmap wraps is NOT owned/released by it - the
caller must explicitly release it (ctypes gdi32.DeleteObject) or every
thumbnail leaks a native GDI handle, which is exactly the kind of thing
that looks fine on a handful of families and causes visible corruption/
crashes across the whole session on a project with hundreds of them.

--------------------------------------------------------------------
Drag-to-place - the first modeless window + ExternalEvent tool in this
repo. Everything else in this extension is a modal ShowDialog() window.
--------------------------------------------------------------------
There is no public Revit API for "accept a drop from an external window
onto the Revit canvas" - Project Browser's own drag is internal to Revit.
The real, supported equivalent is UIDocument.
PromptForFamilyInstancePlacement(symbol): the same command that underlies
dragging a type out of Project Browser - it puts Revit into interactive
placement mode with that type's preview riding the cursor, until the user
clicks in the view to place it (or Esc cancels).

Calling that (or any Revit API method) from a window's event handler is
only legal inside a valid Revit API context. ShowDialog() provides that
for free because Revit is blocked, waiting. Show() (non-modal - the only
way to keep Revit interactive so the user can click in the view while
this window stays open) does NOT - script.py returns immediately after
opening the window, so a later click runs with no API context at all.
The fix is ExternalEvent/IExternalEventHandler: the click marshals a
request onto Revit's own idle loop, which then runs it with a real
context (matches Autodesk's own ModelessForm_ExternalEvent sample). And
because pyRevit tears down the IronPython engine when script.py returns
unless told otherwise, bundle.yaml declares `engine: persistent: true` -
without it, the ExternalEvent raised after the window opens would have
nowhere live to land.

What "click and drag" actually does: press-and-drag a tile past a small
threshold arms placement (raises the ExternalEvent, which activates the
type if needed and calls PromptForFamilyInstancePlacement); the user then
moves into the Revit view - Revit itself shows the family preview on the
cursor - and clicks to drop it. Visually indistinguishable from dragging
out of Project Browser; under the hood it's "drag-to-arm, then Revit's
own placement mode finishes the drop", because Revit gives no other way
in from an external window.

NEEDS LIVE-REVIT VERIFICATION: whether every loaded family type actually
has a preview image to return (some annotation/detail families may not);
whether an inactive FamilySymbol needs Activate() first before
GetPreviewImage works (defensively wrapped either way - a failure here
just means that one tile shows no image, never a crash); real-world
timing for a large project (hundreds of types), shown behind a progress
bar rather than assumed instant; the whole modeless+ExternalEvent+drag
mechanism end to end, since nothing like it exists elsewhere in this repo
to have already proven the pattern live.
"""
import os

import clr
clr.AddReference("System.Drawing")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")

import ctypes
from System import IntPtr
from System.Windows import Int32Rect, SystemParameters
from System.Windows.Input import MouseButtonState
from System.Windows.Interop import Imaging
from System.Windows.Media.Imaging import BitmapSizeOptions
from System.Drawing import Size as DrawingSize

from pyrevit import forms
import dee_branding

from Autodesk.Revit.DB import (
    FilteredElementCollector, Family, BuiltInParameter, Transaction)
from Autodesk.Revit.UI import IExternalEventHandler, ExternalEvent

import dee_telemetry
dee_telemetry.check_access("DeeFamily")


_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")
_THUMB_PX = 128
_ALL_DISCIPLINES_LABEL = "All Disciplines"
_OTHER_DISCIPLINE = "Other"

# Revit has no public "discipline" field on Category - this is a
# maintained best-effort lookup, not a Revit API value. Unmapped
# categories fall back to _OTHER_DISCIPLINE rather than guessing.
_DISCIPLINE_MAP = {}
for _n in (
        "Doors", "Windows", "Furniture", "Furniture Systems", "Casework",
        "Specialty Equipment", "Planting", "Entourage", "Railings",
        "Stairs", "Ramps", "Curtain Panels", "Curtain Wall Mullions",
        "Curtain Systems", "Roofs", "Walls", "Floors", "Ceilings",
        "Columns", "Generic Models", "Site", "Parking", "Roads",
        "Signage", "Mass", "Detail Items", "Generic Annotations"):
    _DISCIPLINE_MAP[_n] = "Architecture"
for _n in (
        "Structural Columns", "Structural Framing", "Structural Foundations",
        "Structural Stiffeners", "Structural Trusses",
        "Structural Connections", "Structural Rebar",
        "Structural Area Reinforcement", "Structural Path Reinforcement",
        "Structural Fabric Reinforcement", "Structural Fabric Areas"):
    _DISCIPLINE_MAP[_n] = "Structural"
for _n in (
        "Mechanical Equipment", "Air Terminals", "Duct Fittings",
        "Duct Accessories", "Duct Insulations", "Duct Linings", "Ducts",
        "Flex Ducts", "HVAC Zones"):
    _DISCIPLINE_MAP[_n] = "Mechanical"
for _n in (
        "Electrical Equipment", "Electrical Fixtures", "Lighting Fixtures",
        "Lighting Devices", "Communication Devices", "Data Devices",
        "Fire Alarm Devices", "Nurse Call Devices", "Security Devices",
        "Telephone Devices", "Cable Tray", "Cable Tray Fittings",
        "Conduit", "Conduit Fittings"):
    _DISCIPLINE_MAP[_n] = "Electrical"
for _n in (
        "Plumbing Fixtures", "Pipe Fittings", "Pipe Accessories", "Pipes",
        "Pipe Insulations", "Sprinklers"):
    _DISCIPLINE_MAP[_n] = "Plumbing"


def _discipline_for_category(category_name):
    return _DISCIPLINE_MAP.get(category_name, _OTHER_DISCIPLINE)


def _read_name(element):
    """Element.Name can throw a bare "Name" exception on some element
    types in this Revit/IronPython combination - falls back to the
    Parameter system, same fix already proven in DeeSheet.pushbutton."""
    if element is None:
        return None
    try:
        n = element.Name
        if n:
            return n
    except Exception:
        pass
    try:
        p = element.get_Parameter(BuiltInParameter.SYMBOL_NAME_PARAM)
        if p is not None:
            val = p.AsString()
            if val:
                return val
    except Exception:
        pass
    return None


def _get_thumbnail(symbol):
    """Returns a frozen BitmapSource, or None if no preview image exists
    or anything about the conversion fails - never raises."""
    try:
        bitmap = symbol.GetPreviewImage(DrawingSize(_THUMB_PX, _THUMB_PX))
    except Exception:
        return None
    if bitmap is None:
        return None
    hbitmap = None
    try:
        hbitmap = bitmap.GetHbitmap()
        src = Imaging.CreateBitmapSourceFromHBitmap(
            hbitmap, IntPtr.Zero, Int32Rect.Empty, BitmapSizeOptions.FromEmptyOptions())
        src.Freeze()
        return src
    except Exception:
        return None
    finally:
        try:
            bitmap.Dispose()
        except Exception:
            pass
        if hbitmap is not None:
            try:
                # CreateBitmapSourceFromHBitmap does NOT take ownership of
                # the HBITMAP - releasing it here is required or every
                # thumbnail leaks a native GDI handle (see module docstring).
                ctypes.windll.gdi32.DeleteObject(hbitmap.ToInt64())
            except Exception:
                pass


class FamilyTypeRow(object):
    def __init__(self, symbol, family_name, category_name):
        self.symbol = symbol
        self.family_name = family_name or "(unnamed family)"
        self.category_name = category_name or "(uncategorized)"
        self.discipline = _discipline_for_category(self.category_name)
        self.type_name = _read_name(symbol) or "(unnamed type)"
        self.thumbnail = None


class FamilyGroup(object):
    def __init__(self, family_name, discipline, category_name, types):
        self.family_name = family_name
        self.discipline = discipline
        self.category_name = category_name
        self.subtitle = u"{0} · {1}".format(discipline, category_name)
        self.types = types


def _collect_family_types(doc):
    rows = []
    for fam in FilteredElementCollector(doc).OfClass(Family):
        try:
            cat = fam.FamilyCategory
            cat_name = cat.Name if cat is not None else None
            fam_name = _read_name(fam)
        except Exception:
            continue
        try:
            type_ids = fam.GetFamilySymbolIds()
        except Exception:
            continue
        for tid in type_ids:
            try:
                symbol = doc.GetElement(tid)
                if symbol is None:
                    continue
                rows.append(FamilyTypeRow(symbol, fam_name, cat_name))
            except Exception:
                continue
    rows.sort(key=lambda r: (r.discipline, r.family_name, r.type_name))
    return rows


def _group_by_family(rows):
    groups = {}
    order = []
    for row in rows:
        key = row.family_name
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(row)
    result = []
    for key in order:
        types = sorted(groups[key], key=lambda r: r.type_name)
        first = types[0]
        result.append(FamilyGroup(key, first.discipline, first.category_name, types))
    result.sort(key=lambda g: (g.discipline, g.family_name))
    return result


def _matches(row, query, discipline):
    if discipline and discipline != _ALL_DISCIPLINES_LABEL and row.discipline != discipline:
        return False
    if not query:
        return True
    low = query.lower()
    haystack = u"{0} {1} {2}".format(row.family_name, row.type_name, row.category_name).lower()
    return all(term in haystack for term in low.split())


class _SafeProgress(object):
    """forms.ProgressBar tries to set Window.TaskbarItemInfo on its host
    window - throws NotImplementedException under Remote Desktop/no
    taskbar (live-confirmed in DeeSheetLinks). Falls back to no progress
    UI at all rather than crashing."""
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


class _PlaceEventHandler(IExternalEventHandler):
    """Runs on Revit's own idle loop (via ExternalEvent.Raise) so it has
    a valid API context even though the window that requested it is
    modeless. Activates the type if needed, then hands off to Revit's
    own interactive placement command - never wraps
    PromptForFamilyInstancePlacement itself in a Transaction, since Revit
    manages that internally while the user is placing instances."""

    def __init__(self, state):
        self._state = state

    def Execute(self, uiapp):
        symbol = self._state.get("symbol")
        self._state["symbol"] = None
        if symbol is None:
            return
        try:
            uidoc = uiapp.ActiveUIDocument
            if uidoc is None:
                return
            doc = uidoc.Document
            if not symbol.IsActive:
                t = Transaction(doc, "Activate family type")
                t.Start()
                try:
                    symbol.Activate()
                    doc.Regenerate()
                    t.Commit()
                except Exception:
                    t.RollBack()
                    return
            uidoc.PromptForFamilyInstancePlacement(symbol)
        except Exception:
            pass

    def GetName(self):
        return "DeeFamily place handler"


_open_window_ref = {"window": None}


class DeeFamilyWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, doc):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.doc = doc
        self._all_rows = []
        self._filtered_rows = []
        self._family_groups = []
        self._drag_row = None
        self._drag_start = None
        self._drag_armed = False
        self._place_state = {"symbol": None}
        self._place_handler = _PlaceEventHandler(self._place_state)
        self._place_event = ExternalEvent.Create(self._place_handler)
        self.Closed += self._on_closed
        self._scan_and_load()

    def _on_closed(self, sender, args):
        if _open_window_ref.get("window") is self:
            _open_window_ref["window"] = None

    def _scan_and_load(self):
        rows = _collect_family_types(self.doc)
        with _SafeProgress(title="DeeFamily - loading {value} of {max_value} thumbnails...",
                            cancellable=False) as pb:
            for i, row in enumerate(rows):
                pb.update_progress(i, len(rows))
                row.thumbnail = _get_thumbnail(row.symbol)
        self._all_rows = rows

        disciplines = sorted(set(r.discipline for r in rows))
        self.discipline_cb.ItemsSource = [_ALL_DISCIPLINES_LABEL] + disciplines
        self.discipline_cb.SelectedIndex = 0
        self._refresh()

    def _refresh(self):
        query = (self.search_tb.Text or "").strip()
        discipline = self.discipline_cb.SelectedItem
        self._filtered_rows = [r for r in self._all_rows if _matches(r, query, discipline)]
        self._family_groups = _group_by_family(self._filtered_rows)
        self.groups_ic.ItemsSource = None
        self.groups_ic.ItemsSource = self._family_groups
        self.summary_tb.Text = u"{0} type(s) in {1} famil{2} shown.".format(
            len(self._filtered_rows), len(self._family_groups),
            "y" if len(self._family_groups) == 1 else "ies")

    def filter_click(self, sender, args):
        self._refresh()

    def clear_filter_click(self, sender, args):
        self.search_tb.Text = ""
        self.discipline_cb.SelectedIndex = 0
        self._refresh()

    def discipline_changed(self, sender, args):
        self._refresh()

    def refresh_click(self, sender, args):
        self._scan_and_load()

    def close_click(self, sender, args):
        self.Close()

    # -- drag-to-place: press-and-drag a tile past a small threshold arms
    # placement; Revit's own interactive command finishes the drop. Same
    # mouse-capture/threshold shape as DeeLevels/DeeGrid's preview drag. --
    def tile_mouse_down(self, sender, args):
        sender.CaptureMouse()
        self._drag_row = sender.Tag
        self._drag_start = args.GetPosition(sender)
        self._drag_armed = False
        args.Handled = True

    def tile_mouse_move(self, sender, args):
        if self._drag_row is None or self._drag_armed:
            return
        if args.LeftButton != MouseButtonState.Pressed:
            return
        pos = args.GetPosition(sender)
        dx = pos.X - self._drag_start.X
        dy = pos.Y - self._drag_start.Y
        if abs(dx) < SystemParameters.MinimumHorizontalDragDistance and \
                abs(dy) < SystemParameters.MinimumVerticalDragDistance:
            return
        self._drag_armed = True
        row = self._drag_row
        try:
            sender.ReleaseMouseCapture()
        except Exception:
            pass
        self._request_place(row)
        args.Handled = True

    def tile_mouse_up(self, sender, args):
        try:
            sender.ReleaseMouseCapture()
        except Exception:
            pass
        self._drag_row = None
        self._drag_armed = False
        args.Handled = True

    def _request_place(self, row):
        self._place_state["symbol"] = row.symbol
        try:
            self._place_event.Raise()
        except Exception:
            pass


def main():
    existing = _open_window_ref.get("window")
    if existing is not None:
        try:
            if existing.IsLoaded:
                existing.Activate()
                return
        except Exception:
            pass

    doc = __revit__.ActiveUIDocument.Document
    window = DeeFamilyWindow(_XAML_FILE, doc)
    _open_window_ref["window"] = window
    window.Show()


main()
