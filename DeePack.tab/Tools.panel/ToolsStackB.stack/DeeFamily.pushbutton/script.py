# -*- coding: utf-8 -*-
"""
DeeFamily
Browses every loaded family TYPE in the project with a real thumbnail per
type (not just per family - different sizes within one family can look
different, matching Revit's own Type Selector), searchable/filterable by
name, family, and category.

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
This is the one genuinely new, first-of-its-kind piece of interop code in
this repo - flagged for live verification, not silently assumed correct.

NEEDS LIVE-REVIT VERIFICATION: whether every loaded family type actually
has a preview image to return (some annotation/detail families may not);
whether an inactive FamilySymbol needs Activate() first before
GetPreviewImage works (defensively wrapped either way - a failure here
just means that one tile shows no image, never a crash); real-world
timing for a large project (hundreds of types), shown behind a progress
bar rather than assumed instant.
"""
import os

import clr
clr.AddReference("System.Drawing")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")

import ctypes
from System import IntPtr
from System.Drawing import Size as DrawingSize
from System.Windows import Int32Rect
from System.Windows.Interop import Imaging
from System.Windows.Media.Imaging import BitmapSizeOptions

from pyrevit import forms
import dee_branding

from Autodesk.Revit.DB import FilteredElementCollector, Family, BuiltInParameter

import dee_telemetry
dee_telemetry.check_access("DeeFamily")


_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")
_THUMB_PX = 128
_ALL_CATEGORIES_LABEL = "All Categories"


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
        self.type_name = _read_name(symbol) or "(unnamed type)"
        self.thumbnail = None


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
    rows.sort(key=lambda r: (r.category_name, r.family_name, r.type_name))
    return rows


def _matches(row, query, category):
    if category and category != _ALL_CATEGORIES_LABEL and row.category_name != category:
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


class DeeFamilyWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, doc):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.doc = doc
        self._all_rows = []
        self._filtered_rows = []
        self._scan_and_load()

    def _scan_and_load(self):
        rows = _collect_family_types(self.doc)
        with _SafeProgress(title="DeeFamily - loading {value} of {max_value} thumbnails...",
                            cancellable=False) as pb:
            for i, row in enumerate(rows):
                pb.update_progress(i, len(rows))
                row.thumbnail = _get_thumbnail(row.symbol)
        self._all_rows = rows

        categories = sorted(set(r.category_name for r in rows))
        self.category_cb.ItemsSource = [_ALL_CATEGORIES_LABEL] + categories
        self.category_cb.SelectedIndex = 0
        self._refresh()

    def _refresh(self):
        query = (self.search_tb.Text or "").strip()
        category = self.category_cb.SelectedItem
        self._filtered_rows = [r for r in self._all_rows if _matches(r, query, category)]
        self.tiles_ic.ItemsSource = None
        self.tiles_ic.ItemsSource = self._filtered_rows
        self.summary_tb.Text = u"{0} of {1} type(s) shown.".format(
            len(self._filtered_rows), len(self._all_rows))

    def filter_click(self, sender, args):
        self._refresh()

    def clear_filter_click(self, sender, args):
        self.search_tb.Text = ""
        self.category_cb.SelectedIndex = 0
        self._refresh()

    def category_changed(self, sender, args):
        self._refresh()

    def refresh_click(self, sender, args):
        self._scan_and_load()

    def close_click(self, sender, args):
        self.Close()


def main():
    doc = __revit__.ActiveUIDocument.Document
    window = DeeFamilyWindow(_XAML_FILE, doc)
    window.ShowDialog()


main()
