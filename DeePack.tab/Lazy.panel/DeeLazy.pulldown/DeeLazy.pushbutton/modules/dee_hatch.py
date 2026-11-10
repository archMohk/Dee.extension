# -*- coding: utf-8 -*-
"""
DeeLazy - DeeHATCH module
A fill-pattern workshop in one window, four tabs, one live preview:

  1. From Lines      pyRevit's Make Pattern, rebuilt: pick detail/model
                     lines (arcs and splines are tessellated, filled
                     regions contribute their own pattern lines) and the
                     two tile corners, then Create or Export .pat. Same
                     options as the original - Drafting/Model, scale,
                     rotation, flip H/V, precision ("highest resolution"),
                     create Filled Region Type - plus a live preview and
                     a smarter solver (see dee_hatch_engine: segments on
                     the same line family are merged into one PAT line,
                     so lines repeated on opposite tile edges or drawn in
                     overlapping pieces no longer become duplicate lines).
  2. From AutoCAD .pat  load acad.pat / acadiso.pat / Revit's .pat files
                     / any custom file (a "Find" button lists every .pat
                     shipped with AutoCAD and Revit on this PC), preview
                     each hatch, tick and import - with units, prefix,
                     scale and rotation. Batch Import PAT, with eyes.
  3. From Image      a monochrome bitmap becomes a hatch: traced outlines
                     and/or scan-line fill, threshold / invert /
                     resolution / smoothing, the image is one tile.
  4. Manage Patterns every fill pattern in the model with how many
                     Filled Region Types, Materials and View Filters use
                     it (plus how many filled regions draw it), bulk
                     rename (find/replace, regex, prefix, suffix, case),
                     delete unused, duplicate, make Filled Region Types,
                     export to .pat.

All the geometry lives in dee_hatch_engine.py (pure Python, tested
outside Revit); this file is the Revit + WPF side only.

Picking (tab 1) closes the window, picks in the view and opens a fresh
window again (launch() loop) - never a pick from inside an open WPF
dialog, the rule every DeeLazy tool follows. Everything else (creating
patterns, renaming, deleting) runs in short transactions straight from
the window's buttons, which is fine while it is shown modally.

NEEDS LIVE-REVIT VERIFICATION: view-filter usage counting on every
Revit version in use (each API call is guarded, so the worst case is a
lower "Used" number, never an error). View category and per-element
graphic overrides are NOT scanned - the Delete confirmation says so.
"""
import glob
import io
import os
import re

import clr
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
clr.AddReference("WindowsBase")
clr.AddReference("System.Windows.Forms")

from System import Array, Byte, Uri
from System.Collections.Generic import List
from System.Windows import Point, Visibility
from System.Windows.Controls import TabControl
from System.Windows.Media import Brushes, PixelFormats, ScaleTransform, StreamGeometry
from System.Windows.Media.Imaging import (
    BitmapCacheOption, BitmapImage, BitmapSource, FormatConvertedBitmap, TransformedBitmap,
)
from System.Windows.Forms import DialogResult, FolderBrowserDialog, OpenFileDialog, SaveFileDialog

from pyrevit import forms
import dee_branding
import deew_settings

from Autodesk.Revit import DB
from Autodesk.Revit.DB import (
    CurveElement, ElementId, FillGrid, FillPattern, FillPatternElement, FillPatternHostOrientation,
    FillPatternTarget, FilledRegion, FilledRegionType, FilteredElementCollector, Line, Material,
    Plane, SketchPlane, Transaction, UV, View, ViewType,
)
from Autodesk.Revit.Exceptions import OperationCanceledException
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType

from modules import dee_hatch_engine as E

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "DeeHatch.xaml")
_SETTINGS = "dee_hatch"
_MM = 304.8
_PRECISION = [("Low - fast, few angles", 4), ("Normal", 8), ("High", 16), ("Very high", 32)]
_WARN_LINES = 1500
_TAB_LINES, _TAB_PAT, _TAB_IMAGE, _TAB_MANAGE = 0, 1, 2, 3

_NO_DETAIL_VIEWS = tuple(getattr(ViewType, n) for n in (
    "ThreeD", "Schedule", "ColumnSchedule", "PanelSchedule", "Report", "ProjectBrowser",
    "SystemBrowser", "Internal", "Undefined", "Walkthrough", "Rendering", "CostReport",
    "LoadsReport", "PresureLossReport", "SystemsAnalysisReport") if hasattr(ViewType, n))


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def _idv(eid):
    try:
        return eid.Value
    except AttributeError:
        return eid.IntegerValue


def _float(text, default, positive=True):
    try:
        v = float((text or "").strip().replace(",", "."))
        if positive and v <= 0:
            return default
        return v
    except Exception:
        return default


def _view_scale(view):
    if view is None or view.ViewType == ViewType.DrawingSheet:
        return 1
    try:
        return max(1, int(view.Scale))
    except Exception:
        return 1


def _mm(ft):
    return E.fmt_num(ft * _MM, 2)


def _target_enum(model):
    return FillPatternTarget.Model if model else FillPatternTarget.Drafting


def _all_fill_patterns(doc):
    out = []
    for fpe in FilteredElementCollector(doc).OfClass(FillPatternElement):
        try:
            out.append((fpe, fpe.GetFillPattern()))
        except Exception:
            pass
    return out


def _grids_of(fp):
    """Revit FillPattern -> engine Grids (feet)."""
    rows = []
    for fg in fp.GetFillGrids():
        try:
            segs = [float(s) for s in fg.GetSegments()]
        except Exception:
            segs = []
        rows.append((fg.Angle, fg.Origin.U, fg.Origin.V, fg.Shift, fg.Offset, segs))
    return E.grids_from_revit(rows)


def _unique_name(doc, base):
    taken = set(fp.Name.lower() for _e, fp in _all_fill_patterns(doc))
    name, i = base, 2
    while name.lower() in taken:
        name = u"{0} ({1})".format(base, i)
        i += 1
    return name


def make_fill_pattern(doc, name, model, rgrids, exists_mode=0):
    """Creates or updates a FillPatternElement. exists_mode: 0 overwrite,
    1 add a number, 2 skip. Returns (element or None, 'created' /
    'updated' / 'skipped'). Must run inside a transaction."""
    target = _target_enum(model)
    existing = None
    for fpe, fp in _all_fill_patterns(doc):
        if fp.Target == target and fp.Name.lower() == name.lower():
            existing = (fpe, fp)
            break
    if existing is not None:
        if existing[1].IsSolidFill:
            raise ValueError(u"'{0}' is Revit's Solid fill and can't be replaced.".format(name))
        if exists_mode == 2:
            return existing[0], "skipped"
        if exists_mode == 1:
            name = _unique_name(doc, name)
            existing = None
    orient = FillPatternHostOrientation.ToHost if model else FillPatternHostOrientation.ToView
    fp = FillPattern(name, target, orient)
    grids = List[FillGrid]()
    for ang, ox, oy, sh, off, segs in rgrids:
        fg = FillGrid()
        fg.Angle = ang
        fg.Origin = UV(ox, oy)
        fg.Offset = off
        fg.Shift = sh
        if segs:
            fg.SetSegments(List[float](segs))
        grids.Add(fg)
    fp.SetFillGrids(grids)
    if existing is not None:
        existing[0].SetFillPattern(fp)
        return existing[0], "updated"
    return FillPatternElement.Create(doc, fp), "created"


def make_region_type(doc, fpe):
    """Filled Region Type named after the pattern, foreground = pattern.
    An existing type of that name is re-pointed instead. Must run in a
    transaction."""
    name = fpe.GetFillPattern().Name
    types = list(FilteredElementCollector(doc).OfClass(FilledRegionType))
    if not types:
        return None
    frt = None
    for t in types:
        if DB.Element.Name.GetValue(t) == name:
            frt = t
            break
    if frt is None:
        frt = types[0].Duplicate(name)
    try:
        frt.ForegroundPatternId = fpe.Id
        try:
            frt.ForegroundPatternColor = DB.Color(0, 0, 0)
        except Exception:
            pass
        try:
            frt.BackgroundPatternId = ElementId.InvalidElementId
        except Exception:
            pass
    except AttributeError:      # Revit 2018 and older
        frt.FillPatternId = fpe.Id
    return frt


def _export_path_dialog(default_name):
    dlg = SaveFileDialog()
    dlg.Filter = "Hatch pattern files (*.pat)|*.pat"
    dlg.FileName = _safe_filename(default_name) + ".pat"
    dlg.Title = "DeeHATCH - export .pat"
    if dlg.ShowDialog() != DialogResult.OK:
        return None
    return dlg.FileName


def _revit_name(name):
    """Revit refuses these characters in element names."""
    return re.sub(r"[\\:{}\[\]|;<>?`~]", "-", name or u"").strip()


def _safe_filename(name):
    s = re.sub(r'[\\/:*?"<>|]+', "_", E.clean_pat_name(name))
    return s.strip(" .") or "pattern"


def _write_text(path, text):
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _clean_for_export(grids_ft, factor):
    """Feet -> export units, in the normalised form Revit itself writes
    (dots turned into short dashes, dash lists starting with a dash)."""
    rows = [E.revit_form(g.scaled(factor)) for g in grids_ft]
    return E.grids_from_revit([r for r in rows if r is not None])


def find_pat_files():
    """Every .pat shipped with AutoCAD (per-user support folders) and
    Revit (ACADInterop + Data) on this PC."""
    roots = []
    appdata = os.environ.get("APPDATA") or ""
    pf = os.environ.get("ProgramFiles") or r"C:\Program Files"
    pats = [
        os.path.join(appdata, "Autodesk", "*", "*", "*", "Support", "*.pat"),
        os.path.join(appdata, "Autodesk", "*", "*", "Support", "*.pat"),
        os.path.join(pf, "Autodesk", "AutoCAD*", "Support", "*.pat"),
        os.path.join(pf, "Autodesk", "AutoCAD*", "Support", "*", "*.pat"),
        os.path.join(pf, "Autodesk", "Revit*", "ACADInterop", "*.pat"),
        os.path.join(pf, "Autodesk", "Revit*", "Data", "*.pat"),
    ]
    seen = set()
    for p in pats:
        try:
            for f in glob.glob(p):
                key = os.path.normcase(os.path.abspath(f))
                if key not in seen:
                    seen.add(key)
                    roots.append(f)
        except Exception:
            pass
    roots.sort(key=lambda s: (os.path.basename(s).lower(), s.lower()))
    return roots[:200]


# --------------------------------------------------------------------------
# Image loading (WPF imaging - no System.Drawing needed)
# --------------------------------------------------------------------------
def load_luminance(path, max_px):
    """Returns (rows, w, h, (w0, h0)): rows = list of lists of 0..255
    grey values with transparency composited onto white, resized so the
    longer side is at most max_px."""
    bmp = BitmapImage()
    bmp.BeginInit()
    bmp.UriSource = Uri(path)
    bmp.CacheOption = BitmapCacheOption.OnLoad
    bmp.EndInit()
    w0, h0 = bmp.PixelWidth, bmp.PixelHeight
    src = bmp
    s = min(1.0, float(max_px) / max(w0, h0))
    if s < 1.0:
        src = TransformedBitmap(bmp, ScaleTransform(s, s))
    conv = FormatConvertedBitmap(src, PixelFormats.Bgra32, None, 0.0)
    w, h = conv.PixelWidth, conv.PixelHeight
    stride = w * 4
    buf = Array.CreateInstance(Byte, stride * h)
    conv.CopyPixels(buf, stride, 0)
    try:
        data = bytearray(buf)
    except Exception:
        data = [int(b) for b in buf]
    rows = []
    for y in range(h):
        base = y * stride
        row = []
        for x in range(w):
            i = base + x * 4
            b, g, r, a = data[i], data[i + 1], data[i + 2], data[i + 3]
            lum = (299 * r + 587 * g + 114 * b) // 1000
            row.append((lum * a + 255 * (255 - a)) // 255)
        rows.append(row)
    return rows, w, h, (w0, h0)


def mask_bitmap(mask):
    h = len(mask)
    w = len(mask[0]) if h else 0
    if not w:
        return None
    stride = (w + 3) & ~3
    data = bytearray(stride * h)
    for y in range(h):
        row = mask[y]
        base = y * stride
        for x in range(w):
            data[base + x] = 0 if row[x] else 255
    try:
        arr = Array[Byte](data)
    except Exception:
        arr = Array[Byte]([int(v) for v in data])
    bs = BitmapSource.Create(w, h, 96.0, 96.0, PixelFormats.Gray8, None, arr, stride)
    bs.Freeze()
    return bs


# --------------------------------------------------------------------------
# Usage scan (Manage tab)
# --------------------------------------------------------------------------
class PatRow(object):
    def __init__(self, fpe, fp):
        self.element = fpe
        self.id = _idv(fpe.Id)
        self.name = fp.Name
        self.new_name = u""
        self.target = "Model" if fp.Target == FillPatternTarget.Model else "Drafting"
        self.solid = bool(fp.IsSolidFill)
        try:
            n = fp.GetFillGrids().Count
        except Exception:
            n = 0
        self.lines = "solid" if self.solid else str(n)
        self.used = 0
        self.regions = 0
        self.where = u""
        self.checked = False


def _ids_from(obj, attrs):
    out = set()
    for a in attrs:
        try:
            v = getattr(obj, a)
        except Exception:
            continue
        if v is not None and v != ElementId.InvalidElementId:
            out.add(_idv(v))
    return out


def scan_patterns(doc):
    rows = [PatRow(fpe, fp) for fpe, fp in _all_fill_patterns(doc)]
    by_id = dict((r.id, r) for r in rows)
    counts = dict((r.id, {"types": 0, "mats": 0, "views": 0, "regions": 0}) for r in rows)

    inst = {}
    try:
        for fr in FilteredElementCollector(doc).OfClass(FilledRegion):
            k = _idv(fr.GetTypeId())
            inst[k] = inst.get(k, 0) + 1
    except Exception:
        pass
    try:
        for frt in FilteredElementCollector(doc).OfClass(FilledRegionType):
            ids = _ids_from(frt, ("ForegroundPatternId", "BackgroundPatternId", "FillPatternId"))
            for pid in ids:
                if pid in counts:
                    counts[pid]["types"] += 1
                    counts[pid]["regions"] += inst.get(_idv(frt.Id), 0)
    except Exception:
        pass
    try:
        for m in FilteredElementCollector(doc).OfClass(Material):
            ids = _ids_from(m, ("SurfaceForegroundPatternId", "SurfaceBackgroundPatternId",
                                "CutForegroundPatternId", "CutBackgroundPatternId",
                                "SurfacePatternId", "CutPatternId"))
            for pid in ids:
                if pid in counts:
                    counts[pid]["mats"] += 1
    except Exception:
        pass
    try:
        for v in FilteredElementCollector(doc).OfClass(View):
            try:
                fids = list(v.GetFilters())
            except Exception:
                continue
            hit = set()
            for fid in fids:
                try:
                    ogs = v.GetFilterOverrides(fid)
                except Exception:
                    continue
                hit |= _ids_from(ogs, ("SurfaceForegroundPatternId", "SurfaceBackgroundPatternId",
                                       "CutForegroundPatternId", "CutBackgroundPatternId",
                                       "ProjectionFillPatternId", "CutFillPatternId"))
            for pid in hit:
                if pid in counts:
                    counts[pid]["views"] += 1
    except Exception:
        pass

    for pid, c in counts.items():
        r = by_id[pid]
        r.used = c["types"] + c["mats"] + c["views"]
        r.regions = c["regions"]
        parts = []
        if c["types"]:
            parts.append(u"{0} region type{1}".format(c["types"], "s" if c["types"] > 1 else ""))
        if c["mats"]:
            parts.append(u"{0} material{1}".format(c["mats"], "s" if c["mats"] > 1 else ""))
        if c["views"]:
            parts.append(u"filters in {0} view{1}".format(c["views"], "s" if c["views"] > 1 else ""))
        r.where = u", ".join(parts) if parts else u"-"
    rows.sort(key=lambda r: (r.target, r.name.lower()))
    return rows


def rename_pattern(fpe, new_name):
    try:
        fpe.Name = new_name
        return
    except Exception:
        pass
    fp = fpe.GetFillPattern()
    fp.Name = new_name
    fpe.SetFillPattern(fp)


def apply_rename(name, find, replace, prefix, suffix, casing, match_case, use_regex):
    s = name
    if find:
        if use_regex:
            s = re.sub(find, replace, s, flags=0 if match_case else re.I)
        elif match_case:
            s = s.replace(find, replace)
        else:
            s = re.sub(re.escape(find), lambda _m: replace, s, flags=re.I)
    if casing == 1:
        s = s.upper()
    elif casing == 2:
        s = s.lower()
    elif casing == 3:
        s = s.title()
    return (prefix or u"") + s + (suffix or u"")


# --------------------------------------------------------------------------
# PAT tab rows
# --------------------------------------------------------------------------
class PatItem(object):
    def __init__(self, pd):
        self.pat = pd
        self.checked = False
        self.label = pd.name + (u"  -  " + pd.desc if pd.desc else u"")
        info = u"{0} line{1}".format(len(pd.grids), "" if len(pd.grids) == 1 else "s")
        if pd.target:
            info += u" · " + pd.target.lower()
        self.info = info


class _LineFilter(ISelectionFilter):
    def AllowElement(self, e):
        return isinstance(e, (CurveElement, FilledRegion))

    def AllowReference(self, ref, pt):
        return False


# --------------------------------------------------------------------------
# Window
# --------------------------------------------------------------------------
class DeeHatchWindow(dee_branding.DeeBrandedWindow):
    _ready = False   # handlers fire while the XAML loads - they check this

    def __init__(self, xaml_file, uiapp, state):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.uiapp = uiapp
        self.uidoc = uiapp.ActiveUIDocument
        self.doc = self.uidoc.Document
        self.view = self.doc.ActiveView
        self.state = state
        self.request = None
        self._st = deew_settings.load(_SETTINGS, {}) or {}
        self._lines_cache = {}
        self._img_lum = None
        self._img_mask = None
        self._img_cache = {}
        self._pat_items = []
        self._pat_units = None
        self._mg_rows = None
        self._mg_preview = None

        for label, _n in _PRECISION:
            self.lines_prec_cb.Items.Add(label)
            self.img_prec_cb.Items.Add(label)
        self._restore()
        self._fill_lines_names()
        self._update_lines_info()
        path = self.state.get("pat_path") or self._st.get("pat_path")
        if path and os.path.isfile(path):
            self._load_pat_file(path, quiet=True)
        ipath = self.state.get("img_path") or self._st.get("img_path")
        if ipath and os.path.isfile(ipath):
            self._load_image(ipath, quiet=True)
        self.Closed += self._on_closed     # the title-bar X saves too
        self._ready = True
        tab = self.state.get("tab", self._st.get("tab", 0))
        self.tabs.SelectedIndex = tab if tab in (0, 1, 2, 3) else 0
        self._on_tab()

    # ------------------------------------------------------------ settings
    def _restore(self):
        st = self._st
        (self.model_rb if st.get("model") else self.drafting_rb).IsChecked = True
        self.scale_tb.Text = str(st.get("scale", "1"))
        self.rotation_tb.Text = str(st.get("rotation", "0"))
        self.fliph_cb.IsChecked = bool(st.get("flip_h"))
        self.flipv_cb.IsChecked = bool(st.get("flip_v"))
        self.exists_cb.SelectedIndex = int(st.get("exists", 0))
        self.region_cb.IsChecked = bool(st.get("region"))
        self.export_units_cb.SelectedIndex = int(st.get("export_units", 0))
        self.mg_units_cb.SelectedIndex = int(st.get("export_units", 0))
        self.lines_prec_cb.SelectedIndex = int(st.get("lines_prec", 2))
        self.img_prec_cb.SelectedIndex = int(st.get("img_prec", 2))
        self.lines_name_cb.Text = self.state.get("lines_name") or st.get("lines_name", u"DeeHatch Pattern")
        self.pat_units_cb.SelectedIndex = int(st.get("pat_units", 0))
        self.pat_prefix_tb.Text = st.get("pat_prefix", u"")
        self.img_mode_cb.SelectedIndex = int(st.get("img_mode", 0))
        self.img_thr_sl.Value = float(st.get("img_thr", 128))
        self.img_thr_tb.Text = str(int(self.img_thr_sl.Value))
        self.img_invert_cb.IsChecked = bool(st.get("img_invert"))
        self.img_res_tb.Text = str(st.get("img_res", "80"))
        self.img_smooth_tb.Text = str(st.get("img_smooth", "0.75"))
        self.img_step_tb.Text = str(st.get("img_step", "1"))
        self.img_width_tb.Text = str(st.get("img_width", "100"))
        self.zoom_sl.Value = float(st.get("zoom", 4))
        self.frame_cb.IsChecked = bool(st.get("frame", True))
        self.dark_cb.IsChecked = bool(st.get("dark", False))

    def _save(self):
        st = {
            "model": bool(self.model_rb.IsChecked),
            "scale": self.scale_tb.Text, "rotation": self.rotation_tb.Text,
            "flip_h": bool(self.fliph_cb.IsChecked), "flip_v": bool(self.flipv_cb.IsChecked),
            "exists": self.exists_cb.SelectedIndex, "region": bool(self.region_cb.IsChecked),
            "export_units": self.export_units_cb.SelectedIndex,
            "lines_prec": self.lines_prec_cb.SelectedIndex, "img_prec": self.img_prec_cb.SelectedIndex,
            "lines_name": self.lines_name_cb.Text,
            "pat_units": self.pat_units_cb.SelectedIndex, "pat_prefix": self.pat_prefix_tb.Text,
            "pat_path": self.pat_path_tb.Text,
            "img_mode": self.img_mode_cb.SelectedIndex, "img_thr": int(self.img_thr_sl.Value),
            "img_invert": bool(self.img_invert_cb.IsChecked), "img_res": self.img_res_tb.Text,
            "img_smooth": self.img_smooth_tb.Text, "img_step": self.img_step_tb.Text,
            "img_width": self.img_width_tb.Text, "img_path": self.img_path_tb.Text,
            "zoom": self.zoom_sl.Value, "frame": bool(self.frame_cb.IsChecked),
            "dark": bool(self.dark_cb.IsChecked), "tab": self.tabs.SelectedIndex,
        }
        try:
            deew_settings.save(_SETTINGS, st)
        except Exception:
            pass

    # ------------------------------------------------------------ options
    def _is_model(self):
        return bool(self.model_rb.IsChecked)

    def _transform(self):
        return (_float(self.scale_tb.Text, 1.0), _float(self.rotation_tb.Text, 0.0, positive=False),
                bool(self.fliph_cb.IsChecked), bool(self.flipv_cb.IsChecked))

    @staticmethod
    def _prec(cb):
        i = cb.SelectedIndex
        return _PRECISION[i][1] if 0 <= i < len(_PRECISION) else 16

    def _status(self, text):
        self.status_tb.Text = text

    # ------------------------------------------------------------ events
    def option_changed(self, sender, args):
        if not self._ready:
            return
        if sender is self.drafting_rb or sender is self.model_rb:
            self._fill_lines_names()
            self._update_lines_info()
        if sender is self.pat_units_cb:
            self._update_pat_units_info()
        self._refresh_preview()

    def tab_changed(self, sender, args):
        # SelectionChanged of every ComboBox/ListBox/DataGrid inside the
        # tabs bubbles up to here too - only react to the TabControl's own.
        if not self._ready or not isinstance(args.OriginalSource, TabControl):
            return
        self._on_tab()

    def _on_tab(self):
        tab = self.tabs.SelectedIndex
        self.output_panel.Visibility = Visibility.Collapsed if tab == _TAB_MANAGE else Visibility.Visible
        self.create_b.Content = "Import ticked" if tab == _TAB_PAT else "Create Pattern"
        self.export_b.Content = "Export ticked .pat..." if tab == _TAB_PAT else "Export .pat..."
        if tab == _TAB_MANAGE and self._mg_rows is None:
            self._mg_scan()
        self._refresh_preview()

    def zoom_changed(self, sender, args):
        if not self._ready:
            return
        self._refresh_preview()

    # ------------------------------------------------------------ preview
    def _preview_source(self):
        """-> (grids_ft, tile_corners or None, info_text) for the active tab.
        Raises ValueError with a user-facing message when there's nothing."""
        tab = self.tabs.SelectedIndex
        if tab == _TAB_LINES:
            p = self._lines_pattern()
            return p["grids"], p["tile"], p["info"]
        if tab == _TAB_IMAGE:
            p = self._image_pattern()
            return p["grids"], p["tile"], p["info"]
        if tab == _TAB_PAT:
            item = self.pat_lb.SelectedItem
            if item is None:
                raise ValueError("Select a pattern in the list to preview it.")
            grids, units = self._pat_grids(item.pat)
            return grids, None, u"{0}: {1} line{2} · file units {3}".format(
                item.pat.name, len(grids), "" if len(grids) == 1 else "s", units)
        row = self.mg_grid.SelectedItem
        if row is None:
            raise ValueError("Select a pattern in the list to preview it.")
        if row.solid:
            raise ValueError("Solid fill - no lines to show.")
        if self._mg_preview is None or self._mg_preview[0] != row.id:
            self._mg_preview = (row.id, _grids_of(row.element.GetFillPattern()))
        return self._mg_preview[1], None, u"{0} ({1}) · {2} lines · used {3}x".format(
            row.name, row.target, row.lines, row.used)

    def _refresh_preview(self):
        if not self._ready:
            return
        zoom = int(self.zoom_sl.Value)
        self.preview_border.Background = Brushes.Black if self.dark_cb.IsChecked else Brushes.White
        self.preview_path.Stroke = Brushes.White if self.dark_cb.IsChecked else Brushes.Black
        try:
            grids, tile, info = self._preview_source()
        except Exception as e:
            self.preview_path.Data = None
            self.frame_path.Data = None
            self.preview_info_tb.Text = u"{0}".format(e)
            self.zoom_tb.Text = u""
            return
        rgrids = [r for r in (E.revit_form(g) for g in grids) if r is not None]
        if tile:
            xs = [p[0] for p in tile]
            ys = [p[1] for p in tile]
            cell = max(max(xs) - min(xs), max(ys) - min(ys)) or 1.0
            cx, cy = (max(xs) + min(xs)) / 2.0, (max(ys) + min(ys)) / 2.0
            size = cell * zoom
            self.zoom_tb.Text = u"{0} tile{1}".format(zoom, "" if zoom == 1 else "s")
        else:
            cell = E.natural_size(rgrids)
            cx = cy = 0.0
            size = cell * zoom
            self.zoom_tb.Text = u"{0} mm".format(_mm(size))
        x0, y0 = cx - size / 2.0, cy - size / 2.0
        segs, truncated = E.preview_segments(rgrids, x0, y0, x0 + size, y0 + size, 25000)
        k = 1000.0 / size

        def P(x, y):
            return Point((x - x0) * k, 1000.0 - (y - y0) * k)

        sg = StreamGeometry()
        ctx = sg.Open()
        for a, b, c, d in segs:
            ctx.BeginFigure(P(a, b), False, False)
            ctx.LineTo(P(c, d), True, False)
        ctx.Close()
        sg.Freeze()
        self.preview_path.Data = sg
        if tile and self.frame_cb.IsChecked:
            fg = StreamGeometry()
            fctx = fg.Open()
            fctx.BeginFigure(P(tile[0][0], tile[0][1]), False, True)
            for p in tile[1:]:
                fctx.LineTo(P(p[0], p[1]), True, False)
            fctx.Close()
            fg.Freeze()
            self.frame_path.Data = fg
        else:
            self.frame_path.Data = None
        if truncated:
            info += u"  ·  preview simplified (very dense - zoom in)"
        self.preview_info_tb.Text = info

    # ------------------------------------------------------------ tab 1: lines
    def _fill_lines_names(self):
        text = self.lines_name_cb.Text
        target = _target_enum(self._is_model())
        names = sorted(fp.Name for _e, fp in _all_fill_patterns(self.doc)
                       if fp.Target == target and not fp.IsSolidFill)
        self.lines_name_cb.Items.Clear()
        for n in names:
            self.lines_name_cb.Items.Add(n)
        self.lines_name_cb.Text = text

    def _update_lines_info(self):
        L = self.state.get("lines")
        if not L:
            self.lines_info_tb.Text = "Nothing picked yet."
            return
        txt = u"{0} element(s), {1} line pieces from '{2}' (1:{3}). Tile {4} × {5} mm in the model".format(
            L["count"], len(L["segments"]), L["view"], L["scale"], _mm(L["W"]), _mm(L["H"]))
        if not self._is_model() and L["scale"] != 1:
            txt += u" = {0} × {1} mm on paper".format(_mm(L["W"] / L["scale"]), _mm(L["H"] / L["scale"]))
        if L.get("fill_grids"):
            txt += u"; {0} line(s) taken from filled regions".format(len(L["fill_grids"]))
        self.lines_info_tb.Text = txt + "."

    def _lines_pattern(self):
        L = self.state.get("lines")
        if not L:
            raise ValueError("Press 'Pick lines & tile in the view...' to start.")
        prec = self._prec(self.lines_prec_cb)
        cached = self._lines_cache.get(prec)
        if cached is None:
            grids, info = E.lines_to_grids(L["segments"], L["W"], L["H"], prec)
            cached = (grids + [g.copy() for g in L.get("fill_grids", [])], info)
            self._lines_cache[prec] = cached
        grids, info = cached
        scale, rot, fh, fv = self._transform()
        factor = scale / (1.0 if self._is_model() else float(L["scale"]))
        out = E.transform_grids(grids, factor, rot, fh, fv)
        tile = self._tile_corners(L["W"], L["H"], factor, rot, fh, fv)
        text = u"{0} hatch line{1} · tile {2} × {3} mm{4}".format(
            len(out), "" if len(out) == 1 else "s", _mm(L["W"] * factor), _mm(L["H"] * factor),
            "" if self._is_model() else " on paper")
        if info["max_angle_error"] > 0.01:
            text += u" · sloped lines turned up to {0}° to repeat cleanly".format(E.fmt_num(info["max_angle_error"], 2))
        return {"grids": out, "tile": tile, "info": text, "name": (self.lines_name_cb.Text or u"").strip()}

    @staticmethod
    def _tile_corners(W, H, factor, rot, fh, fv):
        g = [E.Grid(0, x, y, 0, 1) for x, y in ((0, 0), (W, 0), (W, H), (0, H))]
        return [(t.ox, t.oy) for t in E.transform_grids(g, factor, rot, fh, fv)]

    def pick_click(self, sender, args):
        if self.view.ViewType in _NO_DETAIL_VIEWS:
            forms.alert("Open a plan, section, elevation or drafting view with the pattern lines first.")
            return
        self.state["lines_name"] = self.lines_name_cb.Text
        self.request = "pick"
        self._close()

    # ------------------------------------------------------------ tab 2: PAT
    def pat_browse_click(self, sender, args):
        dlg = OpenFileDialog()
        dlg.Filter = "Hatch pattern files (*.pat)|*.pat|All files (*.*)|*.*"
        dlg.Title = "DeeHATCH - open a .pat file"
        cur = self.pat_path_tb.Text
        if cur and os.path.isdir(os.path.dirname(cur)):
            dlg.InitialDirectory = os.path.dirname(cur)
        if dlg.ShowDialog() == DialogResult.OK:
            self._load_pat_file(dlg.FileName)

    def pat_find_click(self, sender, args):
        self._status("Looking for .pat files...")
        files = find_pat_files()
        self.pat_found_cb.Items.Clear()
        for f in files:
            self.pat_found_cb.Items.Add(f)
        self._status(u"Found {0} .pat file(s) - pick one from the list.".format(len(files)) if files
                     else "No AutoCAD or Revit .pat files found - use Browse.")
        if files:
            self.pat_found_cb.IsDropDownOpen = True

    def pat_found_changed(self, sender, args):
        if not self._ready:
            return
        f = self.pat_found_cb.SelectedItem
        if f:
            self._load_pat_file(str(f))

    def _load_pat_file(self, path, quiet=False):
        try:
            pats, file_units, file_type = E.parse_pat(E.read_text(path))
        except Exception as e:
            if not quiet:
                forms.alert(u"Could not read the file:\n{0}".format(e))
            return
        self.pat_path_tb.Text = path
        self.state["pat_path"] = path
        self._pat_units = E.guess_units(path, file_units)
        self._pat_items = [PatItem(p) for p in pats]
        types = [p.target for p in pats if p.target]
        if types and not quiet:
            model = types.count("MODEL") > len(types) / 2.0
            (self.model_rb if model else self.drafting_rb).IsChecked = True
        self._update_pat_units_info()
        self._pat_refresh_list()
        warn = sum(len(p.warnings) for p in pats)
        if not quiet:
            self._status(u"{0}: {1} pattern(s){2}.".format(
                os.path.basename(path), len(pats), u", {0} unreadable line(s) skipped".format(warn) if warn else ""))
        if self._pat_items and self.pat_lb.SelectedItem is None:
            self.pat_lb.SelectedIndex = 0

    def _update_pat_units_info(self):
        if self._pat_units is None:
            self.pat_units_info_tb.Text = u""
            return
        self.pat_units_info_tb.Text = u"(file: {0})".format("mm" if self._pat_units == "MM" else "inches")

    def _pat_file_units(self):
        i = self.pat_units_cb.SelectedIndex
        if i == 1:
            return "MM"
        if i == 2:
            return "INCH"
        return self._pat_units or "MM"

    def _pat_grids(self, pd):
        units = self._pat_file_units()
        scale, rot, fh, fv = self._transform()
        return E.transform_grids(pd.grids, E.unit_to_feet(units) * scale, rot, fh, fv), units

    def _pat_shown(self):
        text = (self.pat_search_tb.Text or u"").strip().lower()
        if not text:
            return list(self._pat_items)
        words = text.split()
        return [it for it in self._pat_items if all(w in it.label.lower() for w in words)]

    def _pat_refresh_list(self):
        sel = self.pat_lb.SelectedItem
        shown = self._pat_shown()
        self.pat_lb.ItemsSource = None
        self.pat_lb.ItemsSource = shown
        if sel is not None and sel in shown:
            self.pat_lb.SelectedItem = sel
        self._pat_update_count()

    def _pat_update_count(self):
        ticked = len([i for i in self._pat_items if i.checked])
        self.pat_count_tb.Text = u"{0} pattern(s) in the file, {1} shown, {2} ticked.".format(
            len(self._pat_items), len(self._pat_shown()), ticked)

    def pat_search_changed(self, sender, args):
        if self._ready:
            self._pat_refresh_list()

    def pat_all_click(self, sender, args):
        for it in self._pat_shown():
            it.checked = True
        self._pat_refresh_list()

    def pat_none_click(self, sender, args):
        for it in self._pat_items:
            it.checked = False
        self._pat_refresh_list()

    def pat_row_toggled(self, sender, args):
        item = sender.DataContext
        if isinstance(item, PatItem) and item.checked != bool(sender.IsChecked):
            item.checked = bool(sender.IsChecked)
            if self._ready:
                self._pat_update_count()

    def pat_selected(self, sender, args):
        if self._ready:
            self._refresh_preview()

    def _pat_targets(self):
        items = [i for i in self._pat_items if i.checked]
        if not items and self.pat_lb.SelectedItem is not None:
            items = [self.pat_lb.SelectedItem]
        return items

    # ------------------------------------------------------------ tab 3: image
    def img_browse_click(self, sender, args):
        dlg = OpenFileDialog()
        dlg.Filter = "Images (*.png;*.jpg;*.jpeg;*.bmp;*.gif;*.tif;*.tiff)|*.png;*.jpg;*.jpeg;*.bmp;*.gif;*.tif;*.tiff|All files (*.*)|*.*"
        dlg.Title = "DeeHATCH - pick a black and white image"
        if dlg.ShowDialog() == DialogResult.OK:
            self._load_image(dlg.FileName)

    def _load_image(self, path, quiet=False):
        res = int(min(400, max(8, _float(self.img_res_tb.Text, 80))))
        try:
            lum, w, h, orig = load_luminance(path, res)
        except Exception as e:
            if not quiet:
                forms.alert(u"Could not read the image:\n{0}".format(e))
            return
        self.img_path_tb.Text = path
        self.state["img_path"] = path
        self._img_lum = (lum, w, h, orig)
        if not (self.img_name_tb.Text or u"").strip() or not quiet:
            self.img_name_tb.Text = os.path.splitext(os.path.basename(path))[0]
        self._img_rebuild_mask()
        if self._ready:
            self._refresh_preview()

    def _img_rebuild_mask(self):
        self._img_cache = {}
        if self._img_lum is None:
            self._img_mask = None
            self.img_mask_im.Source = None
            return
        lum, w, h, orig = self._img_lum
        thr = int(self.img_thr_sl.Value)
        inv = bool(self.img_invert_cb.IsChecked)
        self._img_mask = [[(v >= thr) if inv else (v < thr) for v in row] for row in lum]
        try:
            self.img_mask_im.Source = mask_bitmap(self._img_mask)
        except Exception:
            self.img_mask_im.Source = None
        ink = sum(sum(1 for v in row if v) for row in self._img_mask)
        self.img_info_tb.Text = u"{0} × {1} px (from {2} × {3}), {4}% ink".format(
            w, h, orig[0], orig[1], int(round(100.0 * ink / max(1, w * h))))

    def img_option_changed(self, sender, args):
        if not self._ready:
            return
        if sender is self.img_thr_sl or sender is self.img_invert_cb:
            self.img_thr_tb.Text = str(int(self.img_thr_sl.Value))
            self._img_rebuild_mask()
        self._refresh_preview()

    def img_res_changed(self, sender, args):
        if not self._ready:
            return
        path = self.img_path_tb.Text
        if path and os.path.isfile(path):
            self._load_image(path, quiet=True)

    def _image_pattern(self):
        if self._img_mask is None:
            raise ValueError("Browse for a black-and-white image to start.")
        lum, w, h, _orig = self._img_lum
        mode = ("outline", "fill", "both")[max(0, self.img_mode_cb.SelectedIndex)]
        smooth = _float(self.img_smooth_tb.Text, 0.0, positive=False)
        step = int(_float(self.img_step_tb.Text, 1))
        prec = self._prec(self.img_prec_cb)
        key = (mode, smooth, step, prec)
        cached = self._img_cache.get(key)
        if cached is None:
            segs = E.image_segments(self._img_mask, mode, max(0.0, smooth), step)
            if not segs:
                raise ValueError("No ink found - move the threshold or tick Invert.")
            cached = E.lines_to_grids(segs, w, h, prec)
            self._img_cache[key] = cached
        grids, info = cached
        width_ft = _float(self.img_width_tb.Text, 100.0) / _MM
        px = width_ft / w
        self.img_height_tb.Text = u"height {0} mm".format(_mm(px * h))
        scale, rot, fh, fv = self._transform()
        factor = px * scale
        out = E.transform_grids(grids, factor, rot, fh, fv)
        tile = self._tile_corners(w, h, factor, rot, fh, fv)
        text = u"{0} hatch line{1} · tile {2} × {3} mm{4}".format(
            len(out), "" if len(out) == 1 else "s", _mm(w * factor), _mm(h * factor),
            "" if self._is_model() else " on paper")
        if len(out) > _WARN_LINES:
            text += u" · a lot of lines: lower the resolution or raise the smoothing"
        return {"grids": out, "tile": tile, "info": text, "name": (self.img_name_tb.Text or u"").strip()}

    # ------------------------------------------------------------ create / export
    def create_click(self, sender, args):
        tab = self.tabs.SelectedIndex
        if tab == _TAB_PAT:
            self._import_pats()
            return
        try:
            p = self._lines_pattern() if tab == _TAB_LINES else self._image_pattern()
        except Exception as e:
            forms.alert(u"{0}".format(e))
            return
        name = p["name"]
        if not name:
            forms.alert("Type a pattern name first.")
            return
        if name.lower() == "solid fill":
            forms.alert("'Solid fill' is reserved by Revit - pick another name.")
            return
        rgrids = [r for r in (E.revit_form(g) for g in p["grids"]) if r is not None]
        if not rgrids:
            forms.alert("The pattern has no lines.")
            return
        if len(rgrids) > _WARN_LINES and not forms.alert(
                u"This pattern has {0} lines - Revit will be slow to draw it.\nCreate it anyway?".format(len(rgrids)),
                yes=True, no=True):
            return
        res = self._create([(name, rgrids)])
        if res:
            self._fill_lines_names()

    def _create(self, items):
        """items: [(name, rgrids)] -> creates them in one transaction."""
        model = self._is_model()
        mode = max(0, self.exists_cb.SelectedIndex)
        done, skipped, failed = [], [], []
        t = Transaction(self.doc, "DeeHATCH - create fill pattern")
        t.Start()
        try:
            for name, rgrids in items:
                name = _revit_name(name)
                try:
                    fpe, how = make_fill_pattern(self.doc, name, model, rgrids, mode)
                except Exception as e:
                    failed.append(u"{0}: {1}".format(name, e))
                    continue
                if how == "skipped":
                    skipped.append(name)
                    continue
                if self.region_cb.IsChecked:
                    try:
                        make_region_type(self.doc, fpe)
                    except Exception as e:
                        failed.append(u"{0} (filled region type): {1}".format(name, e))
                done.append(u"{0} ({1})".format(fpe.GetFillPattern().Name, how))
            t.Commit()
        except Exception as e:
            t.RollBack()
            forms.alert(u"DeeHATCH failed - nothing was changed:\n{0}".format(e))
            return False
        self._mg_rows = None
        msg = u"{0} pattern(s) {1}: {2}".format(
            len(done), "drafting" if not model else "model", u", ".join(done[:6]) + (u"..." if len(done) > 6 else u""))
        if skipped:
            msg += u"  ·  {0} skipped (name exists)".format(len(skipped))
        self._status(msg)
        if failed:
            forms.alert(u"Some patterns could not be created:\n\n" + u"\n".join(failed[:20]))
        return bool(done)

    def _import_pats(self):
        items = self._pat_targets()
        if not items:
            forms.alert("Load a .pat file and tick the patterns to import.")
            return
        prefix = self.pat_prefix_tb.Text or u""
        batch = []
        for it in items:
            grids, _u = self._pat_grids(it.pat)
            rgrids = [r for r in (E.revit_form(g) for g in grids) if r is not None]
            if rgrids:
                batch.append((prefix + it.pat.name, rgrids))
        self._create(batch)

    def export_click(self, sender, args):
        tab = self.tabs.SelectedIndex
        units = "INCH" if self.export_units_cb.SelectedIndex == 1 else "MM"
        factor = _MM if units == "MM" else E.INCH_PER_FT
        target = "MODEL" if self._is_model() else "DRAFTING"
        try:
            if tab == _TAB_PAT:
                items = self._pat_targets()
                if not items:
                    raise ValueError("Tick the patterns to export.")
                defs = []
                for it in items:
                    grids, _u = self._pat_grids(it.pat)
                    defs.append(E.PatDef((self.pat_prefix_tb.Text or u"") + it.pat.name, it.pat.desc,
                                         _clean_for_export(grids, factor), target=target))
                default = defs[0].name if len(defs) == 1 else u"DeeHATCH patterns"
            else:
                p = self._lines_pattern() if tab == _TAB_LINES else self._image_pattern()
                if not p["name"]:
                    raise ValueError("Type a pattern name first.")
                defs = [E.PatDef(p["name"], u"made with DeeHATCH", _clean_for_export(p["grids"], factor), target=target)]
                default = p["name"]
        except Exception as e:
            forms.alert(u"{0}".format(e))
            return
        path = _export_path_dialog(default)
        if not path:
            return
        try:
            _write_text(path, E.write_pat(defs, units))
        except Exception as e:
            forms.alert(u"Could not write the file:\n{0}".format(e))
            return
        self._status(u"Exported {0} pattern(s) to {1}".format(len(defs), path))

    # ------------------------------------------------------------ tab 4: manage
    def _mg_scan(self):
        self._status("Scanning fill patterns and where they are used...")
        try:
            self._mg_rows = scan_patterns(self.doc)
        except Exception as e:
            self._mg_rows = []
            self._status(u"Scan failed: {0}".format(e))
            return
        self._mg_preview = None
        self._rn_compute()
        self._mg_refresh()
        unused = len([r for r in self._mg_rows if r.used == 0 and not r.solid])
        self._status(u"{0} fill patterns, {1} unused.".format(len(self._mg_rows), unused))

    def _mg_shown(self):
        rows = self._mg_rows or []
        text = (self.mg_search_tb.Text or u"").strip().lower()
        tgt = self.mg_target_cb.SelectedIndex
        unused = bool(self.mg_unused_cb.IsChecked)
        out = []
        for r in rows:
            if tgt == 1 and r.target != "Drafting":
                continue
            if tgt == 2 and r.target != "Model":
                continue
            if unused and (r.used or r.solid):
                continue
            if text and not all(w in r.name.lower() for w in text.split()):
                continue
            out.append(r)
        return out

    def _mg_refresh(self):
        sel = self.mg_grid.SelectedItem
        shown = self._mg_shown()
        self.mg_grid.ItemsSource = None
        self.mg_grid.ItemsSource = shown
        if sel is not None and sel in shown:
            self.mg_grid.SelectedItem = sel

    def mg_filter_changed(self, sender, args):
        if self._ready and self._mg_rows is not None:
            self._mg_refresh()

    def mg_refresh_click(self, sender, args):
        self._mg_scan()

    def mg_selected(self, sender, args):
        if self._ready:
            self._refresh_preview()

    def mg_row_toggled(self, sender, args):
        row = sender.DataContext
        # The binding sets IsChecked while rows are generated, which fires
        # this too - only a real change may refresh (else refresh loops).
        if isinstance(row, PatRow) and row.checked != bool(sender.IsChecked):
            row.checked = bool(sender.IsChecked)
            if self._ready:
                self._rn_compute()
                self.mg_grid.Items.Refresh()

    def mg_all_click(self, sender, args):
        for r in self._mg_shown():
            r.checked = not r.solid
        self._rn_compute()
        self._mg_refresh()

    def mg_none_click(self, sender, args):
        for r in self._mg_rows or []:
            r.checked = False
        self._rn_compute()
        self._mg_refresh()

    def _ticked(self):
        return [r for r in (self._mg_rows or []) if r.checked]

    def _rn_args(self):
        return (self.rn_find_tb.Text or u"", self.rn_replace_tb.Text or u"", self.rn_prefix_tb.Text or u"",
                self.rn_suffix_tb.Text or u"", max(0, self.rn_casing_cb.SelectedIndex),
                bool(self.rn_case_cb.IsChecked), bool(self.rn_regex_cb.IsChecked))

    def _rn_compute(self):
        a = self._rn_args()
        active = any(a[:4]) or a[4]
        for r in self._mg_rows or []:
            r.new_name = u""
            if r.checked and active and not r.solid:
                try:
                    new = apply_rename(r.name, *a).strip()
                except Exception:
                    new = u"(bad regex)"
                if new != r.name:
                    r.new_name = new

    def rn_changed(self, sender, args):
        if not self._ready or self._mg_rows is None:
            return
        self._rn_compute()
        self.mg_grid.Items.Refresh()

    def rn_apply_click(self, sender, args):
        self._rn_compute()
        todo = [r for r in self._ticked() if r.new_name and r.new_name != u"(bad regex)"]
        if not todo:
            forms.alert("Tick patterns and fill in Find / Replace / Prefix / Suffix / Case first - the New name column shows the result.")
            return
        renaming = set(r.id for r in todo)
        taken = set((r.target, r.name.lower()) for r in self._mg_rows if r.id not in renaming)
        ok, clash, failed = 0, [], []
        t = Transaction(self.doc, "DeeHATCH - rename fill patterns")
        t.Start()
        try:
            for r in todo:
                key = (r.target, r.new_name.lower())
                if key in taken or not r.new_name.strip():
                    clash.append(r.new_name)
                    taken.add((r.target, r.name.lower()))
                    continue
                try:
                    rename_pattern(r.element, r.new_name)
                    taken.add(key)
                    ok += 1
                except Exception as e:
                    failed.append(u"{0}: {1}".format(r.name, e))
                    taken.add((r.target, r.name.lower()))
            t.Commit()
        except Exception as e:
            t.RollBack()
            forms.alert(u"Rename failed - nothing was changed:\n{0}".format(e))
            return
        self._mg_scan()
        msg = u"Renamed {0} pattern(s).".format(ok)
        if clash:
            msg += u" {0} skipped because the name is taken: {1}".format(len(clash), u", ".join(clash[:5]))
        self._status(msg)
        if failed:
            forms.alert(u"Could not rename:\n" + u"\n".join(failed[:20]))
        self._fill_lines_names()

    def mg_delete_click(self, sender, args):
        ticked = self._ticked()
        dels = [r for r in ticked if r.used == 0 and r.regions == 0 and not r.solid]
        if not dels:
            forms.alert("Tick unused patterns first (Used = 0). Tip: tick 'Unused only', then 'Tick all shown'.")
            return
        kept = len(ticked) - len(dels)
        msg = (u"Delete {0} unused fill pattern(s)?{1}\n\n"
               u"Not used by any Filled Region Type, Material or View Filter. A pattern picked "
               u"only in Visibility/Graphics category or element overrides is not detected - "
               u"those overrides would lose their pattern. Ctrl+Z undoes it.").format(
            len(dels), u"\n({0} ticked pattern(s) are in use and will be kept.)".format(kept) if kept else u"")
        if not forms.alert(msg, yes=True, no=True):
            return
        ids = List[ElementId]([r.element.Id for r in dels])
        t = Transaction(self.doc, "DeeHATCH - delete unused fill patterns")
        t.Start()
        try:
            self.doc.Delete(ids)
            t.Commit()
        except Exception as e:
            t.RollBack()
            forms.alert(u"Delete failed - nothing was changed:\n{0}".format(e))
            return
        self._mg_scan()
        self._status(u"Deleted {0} unused fill pattern(s).".format(len(dels)))
        self._fill_lines_names()

    def mg_duplicate_click(self, sender, args):
        row = self.mg_grid.SelectedItem
        if row is None or row.solid:
            forms.alert("Select a (non-solid) pattern row to duplicate.")
            return
        t = Transaction(self.doc, "DeeHATCH - duplicate fill pattern")
        t.Start()
        try:
            src = row.element.GetFillPattern()
            fp = FillPattern(src)
            fp.Name = _unique_name(self.doc, src.Name + u" copy")
            FillPatternElement.Create(self.doc, fp)
            t.Commit()
        except Exception as e:
            t.RollBack()
            forms.alert(u"Duplicate failed:\n{0}".format(e))
            return
        self._mg_scan()
        self._status(u"Duplicated '{0}' as '{1}'.".format(src.Name, fp.Name))

    def mg_regions_click(self, sender, args):
        ticked = self._ticked()
        if not ticked:
            forms.alert("Tick the patterns to make Filled Region Types for.")
            return
        existing = set(DB.Element.Name.GetValue(t) for t in FilteredElementCollector(self.doc).OfClass(FilledRegionType))
        made, skipped = 0, 0
        t = Transaction(self.doc, "DeeHATCH - make filled region types")
        t.Start()
        try:
            for r in ticked:
                if r.name in existing:
                    skipped += 1
                    continue
                if make_region_type(self.doc, r.element) is not None:
                    made += 1
            t.Commit()
        except Exception as e:
            t.RollBack()
            forms.alert(u"Failed - nothing was changed:\n{0}".format(e))
            return
        self._mg_scan()
        self._status(u"Made {0} Filled Region Type(s){1}.".format(
            made, u", {0} skipped (a type with that name exists)".format(skipped) if skipped else u""))

    def mg_export_click(self, sender, args):
        rows = [r for r in self._ticked() if not r.solid]
        if not rows:
            forms.alert("Tick the patterns to export.")
            return
        dlg = FolderBrowserDialog()
        dlg.Description = "DeeHATCH - folder for the .pat files (one file per pattern)"
        if dlg.ShowDialog() != DialogResult.OK:
            return
        units = "INCH" if self.mg_units_cb.SelectedIndex == 1 else "MM"
        factor = _MM if units == "MM" else E.INCH_PER_FT
        n, failed = 0, []
        for r in rows:
            try:
                fp = r.element.GetFillPattern()
                pd = E.PatDef(r.name, u"exported from Revit by DeeHATCH",
                              _clean_for_export(_grids_of(fp), factor), target=r.target.upper())
                _write_text(os.path.join(dlg.SelectedPath, _safe_filename(r.name) + ".pat"), E.write_pat([pd], units))
                n += 1
            except Exception as e:
                failed.append(u"{0}: {1}".format(r.name, e))
        self._status(u"Exported {0} .pat file(s) to {1}".format(n, dlg.SelectedPath))
        if failed:
            forms.alert(u"Some patterns could not be exported:\n" + u"\n".join(failed[:20]))

    # ------------------------------------------------------------ close
    def _close(self):
        self.Close()      # settings are saved by _on_closed

    def close_click(self, sender, args):
        self._close()

    def _on_closed(self, sender, args):
        self._save()


# --------------------------------------------------------------------------
# Picking (runs while no window is open)
# --------------------------------------------------------------------------
def _ensure_work_plane(doc, view):
    try:
        if view.SketchPlane is not None or view.ViewType == ViewType.DrawingSheet:
            return
    except Exception:
        return
    t = Transaction(doc, "DeeHATCH - work plane")
    t.Start()
    try:
        view.SketchPlane = SketchPlane.Create(doc, Plane.CreateByNormalAndOrigin(view.ViewDirection, view.Origin))
        t.Commit()
    except Exception:
        t.RollBack()


def pick_lines(uidoc):
    """Returns the lines-tab state dict, or None if cancelled."""
    doc = uidoc.Document
    view = doc.ActiveView
    elems = []
    for eid in uidoc.Selection.GetElementIds():
        e = doc.GetElement(eid)
        if isinstance(e, (CurveElement, FilledRegion)):
            elems.append(e)
    if not elems:
        try:
            refs = uidoc.Selection.PickObjects(
                ObjectType.Element, _LineFilter(),
                "DeeHATCH: select the pattern lines (and filled regions), then press Finish")
        except OperationCanceledException:
            return None
        elems = [doc.GetElement(r) for r in refs]
    if not elems:
        return None
    _ensure_work_plane(doc, view)
    try:
        p1 = uidoc.Selection.PickPoint("DeeHATCH: click the BOTTOM-LEFT corner of the tile")
        p2 = uidoc.Selection.PickPoint("DeeHATCH: click the TOP-RIGHT corner of the tile")
    except OperationCanceledException:
        return None
    right, up = view.RightDirection, view.UpDirection
    u1, v1 = p1.DotProduct(right), p1.DotProduct(up)
    u2, v2 = p2.DotProduct(right), p2.DotProduct(up)
    x0, y0 = min(u1, u2), min(v1, v2)
    W, H = abs(u2 - u1), abs(v2 - v1)
    tol = doc.Application.ShortCurveTolerance
    if W < tol or H < tol:
        forms.alert("The two corners must make a rectangle - pick the bottom-left, then the top-right corner.")
        return None

    def uv(p):
        return p.DotProduct(right) - x0, p.DotProduct(up) - y0

    segs, fill_grids, used = [], [], 0
    scale = _view_scale(view)
    for e in elems:
        if isinstance(e, CurveElement):
            try:
                c = e.GeometryCurve
                pts = [c.GetEndPoint(0), c.GetEndPoint(1)] if isinstance(c, Line) else list(c.Tessellate())
            except Exception:
                continue
            for a, b in zip(pts[:-1], pts[1:]):
                (ax, ay), (bx, by) = uv(a), uv(b)
                segs.append((ax, ay, bx, by))
            used += 1
        elif isinstance(e, FilledRegion):
            frt = doc.GetElement(e.GetTypeId())
            ids = _ids_from(frt, ("ForegroundPatternId", "BackgroundPatternId", "FillPatternId"))
            for pid in ids:
                fpe = doc.GetElement(ElementId(pid))
                if not isinstance(fpe, FillPatternElement):
                    continue
                fp = fpe.GetFillPattern()
                if fp.IsSolidFill:
                    continue
                k = float(scale) if fp.Target == FillPatternTarget.Drafting else 1.0
                for g in _grids_of(fp):
                    g = g.scaled(k)
                    g.ox -= x0
                    g.oy -= y0
                    fill_grids.append(g)
            used += 1
    if not segs and not fill_grids:
        forms.alert("None of the picked elements has usable lines.")
        return None
    return {"segments": segs, "fill_grids": fill_grids, "W": W, "H": H, "scale": scale,
            "view": view.Name, "count": used}


def launch(uiapp):
    if uiapp.ActiveUIDocument is None:
        forms.alert("Open a Revit project first.")
        return
    state = {}
    while True:
        window = DeeHatchWindow(_XAML_FILE, uiapp, state)
        window.ShowDialog()
        if window.request != "pick":
            break
        state = window.state
        state["tab"] = _TAB_LINES
        picked = pick_lines(uiapp.ActiveUIDocument)
        if picked:
            state["lines"] = picked


TOOL_INFO = {
    "id": "dee_hatch",
    "title": "DeeHATCH",
    "description": "Make fill patterns from lines, AutoCAD .pat files or a black-and-white image - with live preview. Manage them: usage, bulk rename, purge.",
    "icon": u"\u25A6",   # square with orthogonal crosshatch fill
    "category": "Drafting & Annotation",
    "launch": launch,
}
