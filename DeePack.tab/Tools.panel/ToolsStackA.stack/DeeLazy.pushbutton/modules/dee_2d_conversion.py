# -*- coding: utf-8 -*-
"""
DeeLazy - 2D Conversion module
A black-and-white image (logo, sign, stamp, signature, scanned sketch)
becomes real Revit 2D in the active view:

  * Detail Lines   - the traced outline of every ink shape (and of the
                     white islands inside them), in a chosen line style
  * Filled Regions - every ink shape as a Filled Region of a chosen type
                     (= the hatch), its white islands left open as holes
  * a fill pattern - optional: the same image as one repeating tile of a
                     new fill pattern + Filled Region Type, exactly like
                     DeeHATCH's "From Image" tab

Tracing (dee_hatch_engine.image_shapes, tested outside Revit): pixel
boundaries are walked into closed loops - ink outlines counter-
clockwise, holes clockwise - loops that touch themselves at a corner
(two pixels of one shape meeting diagonally) are cut there, because
Revit sketches reject self-touching loops; holes are matched to the
smallest outline around them; then each loop is straightened by the
Smoothing tolerance (Douglas-Peucker). Image loading (transparency =
white) is shared with DeeHATCH.

Placement follows the DeeLazy rule: Place closes the window, then the
bottom-left corner is picked in the view - no pick from inside an open
WPF dialog.

Revit's short-curve limit (~0.8 mm in the model) is handled like Text to
Lines: corners closer than that to the previous kept corner are merged.
A Filled Region that Revit still refuses after smoothing is retried with
the exact (unsmoothed) pixel outline before it is counted as failed.

NEEDS LIVE-REVIT VERIFICATION: FilledRegion.Create with many holes and
SetLineStyleId on every Revit version in use (both guarded).
"""
import math
import os

import clr
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
clr.AddReference("WindowsBase")
clr.AddReference("System.Windows.Forms")

from System.Collections.Generic import List
from System.Windows import Point, Visibility
from System.Windows.Media import FillRule, StreamGeometry
from System.Windows.Forms import DialogResult, OpenFileDialog

from pyrevit import forms
import dee_branding
import deew_settings

from Autodesk.Revit import DB
from Autodesk.Revit.DB import (
    BuiltInCategory, CurveLoop, ElementId, FilledRegion, FilledRegionType, FilteredElementCollector,
    GraphicsStyleType, Line, Plane, SketchPlane, Transaction, ViewType,
)
from Autodesk.Revit.Exceptions import OperationCanceledException

from modules import dee_hatch_engine as E
from modules import dee_hatch as H

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "Conversion2D.xaml")
_SETTINGS = "dee_2d_conversion"
_MM = 304.8
_MAX_RES = 600


def _float(text, default, positive=True):
    try:
        v = float((text or "").strip().replace(",", "."))
        if positive and v <= 0:
            return default
        return v
    except Exception:
        return default


def _line_styles(doc):
    out = []
    try:
        cat = doc.Settings.Categories.get_Item(BuiltInCategory.OST_Lines)
        for sub in cat.SubCategories:
            gs = sub.GetGraphicsStyle(GraphicsStyleType.Projection)
            if gs is not None:
                out.append((sub.Name, gs))
    except Exception:
        pass
    out.sort(key=lambda t: t[0].lower())
    return out


def _region_types(doc):
    out = [(DB.Element.Name.GetValue(t), t) for t in FilteredElementCollector(doc).OfClass(FilledRegionType)]
    out.sort(key=lambda t: t[0].lower())
    return out


class Item(object):
    """ComboBox entry that shows a name and carries a Revit object."""

    def __init__(self, name, obj):
        self.name = name
        self.obj = obj

    def ToString(self):
        return self.name

    def __str__(self):
        return self.name


def _fill_combo(cb, pairs, want, fallback=None):
    items = [Item(n, o) for n, o in pairs]
    for it in items:
        cb.Items.Add(it)
    for name in (want, fallback):
        for it in items:
            if name and it.name == name:
                cb.SelectedItem = it
                return
    if items:
        cb.SelectedIndex = 0


def _picked(cb):
    it = cb.SelectedItem
    return it.name if it is not None else u""


def _thin(loop, tol):
    """Closed loop -> corners at least `tol` apart (short pieces merged
    into the next one). None if fewer than 3 corners remain."""
    pts = loop[:-1] if loop[0] == loop[-1] else list(loop)
    kept = [pts[0]]
    for p in pts[1:]:
        if math.hypot(p[0] - kept[-1][0], p[1] - kept[-1][1]) >= tol:
            kept.append(p)
    while len(kept) > 2 and math.hypot(kept[0][0] - kept[-1][0], kept[0][1] - kept[-1][1]) < tol:
        kept.pop()
    return kept if len(kept) >= 3 else None


# --------------------------------------------------------------------------
# Window
# --------------------------------------------------------------------------
class Conversion2DWindow(dee_branding.DeeBrandedWindow):
    _ready = False   # handlers fire while the XAML loads

    def __init__(self, xaml_file, uiapp):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.uiapp = uiapp
        self.doc = uiapp.ActiveUIDocument.Document
        self.view = self.doc.ActiveView
        self.request = None
        self._lum = None
        self._mask = None
        self._shapes = {}
        st = self._st = deew_settings.load(_SETTINGS, {}) or {}

        styles = _line_styles(self.doc)
        _fill_combo(self.line_style_cb, styles, st.get("line_style"), "Thin Lines")
        _fill_combo(self.region_line_cb, styles, st.get("region_line"), "<Invisible lines>")
        _fill_combo(self.region_type_cb, _region_types(self.doc), st.get("region_type"), "Solid Black")

        self.thr_sl.Value = float(st.get("thr", 128))
        self.thr_tb.Text = str(int(self.thr_sl.Value))
        self.invert_cb.IsChecked = bool(st.get("invert"))
        self.res_tb.Text = str(st.get("res", "150"))
        self.smooth_tb.Text = str(st.get("smooth", "0.75"))
        self.speck_tb.Text = str(st.get("speck", "2"))
        self.width_tb.Text = str(st.get("width", "100"))
        self.paper_cb.IsChecked = bool(st.get("paper", True))
        self.lines_cb.IsChecked = bool(st.get("lines", True))
        self.regions_cb.IsChecked = bool(st.get("regions", False))
        self.group_cb.IsChecked = bool(st.get("group", False))
        self.pattern_cb.IsChecked = bool(st.get("pattern", False))
        (self.pat_model_rb if st.get("pat_model") else self.pat_drafting_rb).IsChecked = True

        path = st.get("path")
        if path and os.path.isfile(path):
            self._load(path, keep_name=False)
        self.Closed += self._on_closed
        self._ready = True
        self._refresh()

    # ---------------- settings ----------------
    def _opts(self):
        return {
            "path": self.path_tb.Text, "thr": int(self.thr_sl.Value), "invert": bool(self.invert_cb.IsChecked),
            "res": self.res_tb.Text, "smooth": self.smooth_tb.Text, "speck": self.speck_tb.Text,
            "width": self.width_tb.Text, "paper": bool(self.paper_cb.IsChecked),
            "lines": bool(self.lines_cb.IsChecked), "regions": bool(self.regions_cb.IsChecked),
            "group": bool(self.group_cb.IsChecked), "pattern": bool(self.pattern_cb.IsChecked),
            "pat_model": bool(self.pat_model_rb.IsChecked), "pattern_name": self.pattern_name_tb.Text,
            "line_style": _picked(self.line_style_cb), "region_line": _picked(self.region_line_cb),
            "region_type": _picked(self.region_type_cb),
        }

    def _on_closed(self, sender, args):
        try:
            deew_settings.save(_SETTINGS, self._opts())
        except Exception:
            pass

    # ---------------- image ----------------
    def browse_click(self, sender, args):
        dlg = OpenFileDialog()
        dlg.Filter = "Images (*.png;*.jpg;*.jpeg;*.bmp;*.gif;*.tif;*.tiff)|*.png;*.jpg;*.jpeg;*.bmp;*.gif;*.tif;*.tiff|All files (*.*)|*.*"
        dlg.Title = "2D Conversion - pick a black and white image"
        if dlg.ShowDialog() == DialogResult.OK:
            self._load(dlg.FileName, keep_name=False)
            self._refresh()

    def _res(self):
        return int(min(_MAX_RES, max(8, _float(self.res_tb.Text, 150))))

    def _load(self, path, keep_name=True):
        try:
            self._lum = H.load_luminance(path, self._res())
        except Exception as e:
            self._lum = None
            self.status_tb.Text = u"Could not read the image: {0}".format(e)
            return
        self.path_tb.Text = path
        if not keep_name or not (self.pattern_name_tb.Text or u"").strip():
            self.pattern_name_tb.Text = os.path.splitext(os.path.basename(path))[0]
        self._rebuild_mask()

    def _rebuild_mask(self):
        self._shapes = {}
        if self._lum is None:
            self._mask = None
            self.mask_im.Source = None
            return
        rows = self._lum[0]
        thr = int(self.thr_sl.Value)
        inv = bool(self.invert_cb.IsChecked)
        self._mask = [[(v >= thr) if inv else (v < thr) for v in row] for row in rows]
        try:
            self.mask_im.Source = H.mask_bitmap(self._mask)
        except Exception:
            self.mask_im.Source = None

    def mask_changed(self, sender, args):
        if not self._ready:
            return
        self.thr_tb.Text = str(int(self.thr_sl.Value))
        self._rebuild_mask()
        self._refresh()

    def res_changed(self, sender, args):
        if not self._ready:
            return
        if self.path_tb.Text and os.path.isfile(self.path_tb.Text):
            self._load(self.path_tb.Text)
        self._refresh()

    def option_changed(self, sender, args):
        if self._ready:
            self._refresh()

    def shapes(self, smooth=None):
        if self._mask is None:
            return []
        if smooth is None:
            smooth = max(0.0, _float(self.smooth_tb.Text, 0.0, positive=False))
        speck = max(0.0, _float(self.speck_tb.Text, 0.0, positive=False))
        key = (smooth, speck)
        if key not in self._shapes:
            self._shapes[key] = E.image_shapes(self._mask, smooth, speck)
        return self._shapes[key]

    def _scale(self):
        if not self.paper_cb.IsChecked or self.view.ViewType == ViewType.DrawingSheet:
            return 1
        try:
            return max(1, int(self.view.Scale))
        except Exception:
            return 1

    # ---------------- preview ----------------
    def _refresh(self):
        if self._mask is None:
            self.line_path.Data = None
            self.fill_path.Data = None
            self.info_tb.Text = u""
            self.height_tb.Text = u""
            self.status_tb.Text = u"Browse for a black-and-white image to start."
            return
        _rows, w, h, orig = self._lum
        self.canvas_grid.Width = w
        self.canvas_grid.Height = h
        self.mask_im.Visibility = Visibility.Visible if self.show_img_cb.IsChecked else Visibility.Hidden
        self.line_path.StrokeThickness = max(w, h) / 450.0
        shapes = self.shapes()

        def add_loop(ctx, loop, filled):
            ctx.BeginFigure(Point(loop[0][0], h - loop[0][1]), filled, True)
            for x, y in loop[1:-1]:
                ctx.LineTo(Point(x, h - y), True, False)

        sg = StreamGeometry()
        sg.FillRule = FillRule.EvenOdd
        ctx = sg.Open()
        n_loops = n_corners = 0
        for outer, holes in shapes:
            for loop in [outer] + holes:
                add_loop(ctx, loop, True)
                n_loops += 1
                n_corners += len(loop) - 1
        ctx.Close()
        sg.Freeze()
        self.line_path.Data = sg if self.lines_cb.IsChecked or not self.regions_cb.IsChecked else None
        self.fill_path.Data = sg if self.regions_cb.IsChecked else None

        scale = self._scale()
        width_mm = _float(self.width_tb.Text, 100.0)
        px_mm = width_mm * scale / w           # model mm per pixel
        self.height_tb.Text = u"height {0} mm".format(E.fmt_num(width_mm * h / w, 1))
        self.info_tb.Text = u"{0} × {1} px (from {2} × {3}) · {4} shape(s), {5} outline loop(s), about {6} lines".format(
            w, h, orig[0], orig[1], len(shapes), n_loops, n_corners)
        msg = u"Model size {0} × {1} mm in '{2}'{3}.".format(
            E.fmt_num(width_mm * scale, 0), E.fmt_num(width_mm * scale * h / w, 0), self.view.Name,
            u" (1:{0})".format(scale) if scale != 1 else u"")
        tol_mm = self.doc.Application.ShortCurveTolerance * _MM
        if px_mm < tol_mm * 1.5:
            msg += u"  Warning: one pixel is only {0} mm in the model - Revit can't draw lines under ~{1} mm, so small details merge. Make it wider or lower the resolution.".format(
                E.fmt_num(px_mm, 2), E.fmt_num(tol_mm, 1))
        if self.view.ViewType in H._NO_DETAIL_VIEWS:
            msg += u"  The active view can't hold 2D elements - open a plan, section, drafting view or sheet."
        self.status_tb.Text = msg

    # ---------------- buttons ----------------
    def place_click(self, sender, args):
        if self._mask is None:
            self.status_tb.Text = "Pick an image first."
            return
        o = self._opts()
        if not (o["lines"] or o["regions"] or o["pattern"]):
            forms.alert("Tick at least one of Detail Lines, Filled Regions or fill pattern.")
            return
        if (o["lines"] or o["regions"]) and self.view.ViewType in H._NO_DETAIL_VIEWS:
            forms.alert("The active view can't hold detail lines or filled regions.\n"
                        "Open a plan, section, elevation, drafting view or sheet and run it again.")
            return
        if o["pattern"] and not (o["pattern_name"] or u"").strip():
            forms.alert("Type a name for the fill pattern.")
            return
        if not self.shapes():
            forms.alert("No ink found - move the threshold or tick Invert.")
            return
        o["scale"] = self._scale()
        self.request = o
        self.Close()

    def close_click(self, sender, args):
        self.Close()


# --------------------------------------------------------------------------
# Placement (runs after the window has closed)
# --------------------------------------------------------------------------
def _ensure_work_plane(doc, view):
    try:
        if view.SketchPlane is not None or view.ViewType == ViewType.DrawingSheet:
            return
    except Exception:
        return
    t = Transaction(doc, "DeeLazy - 2D Conversion work plane")
    t.Start()
    try:
        view.SketchPlane = SketchPlane.Create(doc, Plane.CreateByNormalAndOrigin(view.ViewDirection, view.Origin))
        t.Commit()
    except Exception:
        t.RollBack()


def _make_pattern(doc, window, o):
    """DeeHATCH 'From Image' with this window's mask: the image is one
    tile of width o['width'] mm. Runs inside the caller's transaction."""
    _rows, w, h, _orig = window._lum
    smooth = max(0.0, _float(o["smooth"], 0.0, positive=False))
    segs = E.image_segments(window._mask, "outline", smooth, 1)
    grids, _info = E.lines_to_grids(segs, w, h, 16)
    model = bool(o["pat_model"])
    width_ft = _float(o["width"], 100.0) / _MM * (o["scale"] if model else 1)
    grids = E.transform_grids(grids, width_ft / w)
    rgrids = [r for r in (E.revit_form(g) for g in grids) if r is not None]
    fpe, how = H.make_fill_pattern(doc, H._revit_name(o["pattern_name"].strip()), model, rgrids, 0)
    H.make_region_type(doc, fpe)
    return fpe.GetFillPattern().Name, how, len(rgrids)


def place(uidoc, window, o):
    doc = uidoc.Document
    view = doc.ActiveView
    want_2d = o["lines"] or o["regions"]
    origin = None
    if want_2d:
        _ensure_work_plane(doc, view)
        try:
            origin = uidoc.Selection.PickPoint("2D Conversion: click the BOTTOM-LEFT corner of the image (Esc to cancel)")
        except OperationCanceledException:
            return

    _rows, w, h, _orig = window._lum
    k = _float(o["width"], 100.0) / _MM * o["scale"] / w      # feet per pixel
    tol_px = doc.Application.ShortCurveTolerance * 1.01 / k
    right, up = view.RightDirection, view.UpDirection

    def P(p):
        return origin + right * (p[0] * k) + up * (p[1] * k)

    def loop_lines(loop):
        pts = _thin(loop, tol_px)
        if pts is None:
            return None
        return [Line.CreateBound(P(a), P(b)) for a, b in zip(pts, pts[1:] + pts[:1])]

    styles = dict(_line_styles(doc))
    region_types = dict(_region_types(doc))
    ids = List[ElementId]()
    n_lines = n_regions = 0
    failed_lines = failed_regions = 0
    report = []
    t = Transaction(doc, "DeeLazy - 2D Conversion")
    t.Start()
    try:
        if o["lines"]:
            style = styles.get(o["line_style"])
            for outer, holes in window.shapes():
                for loop in [outer] + holes:
                    try:
                        curves = loop_lines(loop)
                    except Exception:
                        curves = None
                    for c in curves or []:
                        try:
                            dc = doc.Create.NewDetailCurve(view, c)
                            if style is not None:
                                dc.LineStyle = style
                            ids.Add(dc.Id)
                            n_lines += 1
                        except Exception:
                            failed_lines += 1
        if o["regions"]:
            rtype = region_types.get(o["region_type"])
            if rtype is None:
                raise ValueError("Pick a Filled Region Type.")
            edge = styles.get(o["region_line"])
            smooth_shapes = window.shapes()
            exact_shapes = None
            for i, (outer, holes) in enumerate(smooth_shapes):
                fr = _region(doc, view, rtype, outer, holes, loop_lines)
                if fr is None:
                    if exact_shapes is None:
                        exact_shapes = window.shapes(0.0)
                    if i < len(exact_shapes):
                        fr = _region(doc, view, rtype, exact_shapes[i][0], exact_shapes[i][1], loop_lines)
                if fr is None:
                    failed_regions += 1
                    continue
                if edge is not None:
                    try:
                        fr.SetLineStyleId(edge.Id)
                    except Exception:
                        pass
                ids.Add(fr.Id)
                n_regions += 1
        if o["pattern"]:
            try:
                name, how, n = _make_pattern(doc, window, o)
                report.append(u"Fill pattern '{0}' {1} ({2} lines) + Filled Region Type '{0}'.".format(name, how, n))
            except Exception as e:
                report.append(u"Fill pattern failed: {0}".format(e))
        if o["group"] and ids.Count > 1:
            try:
                grp = doc.Create.NewGroup(ids)
                ids = List[ElementId]([grp.Id])
            except Exception:
                pass
        t.Commit()
    except Exception as e:
        t.RollBack()
        forms.alert(u"2D Conversion failed - nothing was changed:\n{0}".format(e))
        return
    try:
        if ids.Count:
            uidoc.Selection.SetElementIds(ids)
    except Exception:
        pass
    msg = []
    if o["lines"]:
        msg.append(u"{0} detail line(s){1}.".format(n_lines, u", {0} too short to draw".format(failed_lines) if failed_lines else u""))
    if o["regions"]:
        msg.append(u"{0} filled region(s){1}.".format(
            n_regions, u", {0} shape(s) Revit refused (try more smoothing or a lower resolution)".format(failed_regions) if failed_regions else u""))
    msg.extend(report)
    if failed_lines or failed_regions or o["pattern"]:
        forms.alert(u"2D Conversion done:\n\n" + u"\n".join(msg))


def _region(doc, view, rtype, outer, holes, loop_lines):
    """One FilledRegion for a shape; holes Revit dislikes are dropped
    one by one before giving up. None if it can't be made."""
    try:
        outer_lines = loop_lines(outer)
    except Exception:
        return None
    if not outer_lines:
        return None
    hole_loops = []
    for hl in holes:
        try:
            lines = loop_lines(hl)
        except Exception:
            lines = None
        if lines:
            hole_loops.append(lines)

    def make(hls):
        loops = List[CurveLoop]()
        for lines in [outer_lines] + hls:
            cl = CurveLoop()
            for c in lines:
                cl.Append(c)
            loops.Add(cl)
        return FilledRegion.Create(doc, rtype.Id, view.Id, loops)

    try:
        return make(hole_loops)
    except Exception:
        pass
    if not hole_loops:
        return None
    good = []
    for hl in hole_loops:          # find the holes that are fine on their own
        try:
            st = DB.SubTransaction(doc)
            st.Start()
            try:
                make([hl])
                good.append(hl)
            finally:
                st.RollBack()
        except Exception:
            pass
    try:
        return make(good)
    except Exception:
        try:
            return make([])
        except Exception:
            return None


def launch(uiapp):
    if uiapp.ActiveUIDocument is None:
        forms.alert("Open a Revit project first.")
        return
    window = Conversion2DWindow(_XAML_FILE, uiapp)
    window.ShowDialog()
    if window.request is not None:
        place(uiapp.ActiveUIDocument, window, window.request)


TOOL_INFO = {
    "id": "dee_2d_conversion",
    "title": "2D Conversion",
    "description": "Turn a black-and-white image into Detail Lines and Filled Regions (hatch) in the view - optionally also a fill pattern.",
    "icon": u"\U0001F5BC",   # framed picture - image turned into 2D
    "category": "Drafting & Annotation",
    "launch": launch,
}
