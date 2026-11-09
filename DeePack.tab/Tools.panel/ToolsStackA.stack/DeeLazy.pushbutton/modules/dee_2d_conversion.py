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

Simplify (fewest elements): with "Lines + curves" every outline is cut
at its real corners and each run between corners is drawn as ONE line
if it is straight within the Simplify tolerance, else as the fewest
cubic curves that stay within it (dee_hatch_engine.fit_loop - Schneider
curve fitting). Each curve is an exact NurbSpline detail curve; a
circle that was ~80 lines becomes 4 curves. "Straight lines only" keeps
the polyline result (Smoothing tolerance), for DWG/CNC use.

Revit's short-curve limit (~0.8 mm in the model) is handled like Text to
Lines: a piece shorter than that is merged into the next one.
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
from System.Windows.Forms import DialogResult, OpenFileDialog, SaveFileDialog

from pyrevit import forms
import dee_branding
import deew_settings

from Autodesk.Revit import DB
from Autodesk.Revit.DB import (
    BuiltInCategory, CurveLoop, ElementId, FilledRegion, FilledRegionType, FilteredElementCollector,
    GraphicsStyleType, Line, NurbSpline, Plane, SketchPlane, Transaction, ViewType, XYZ,
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


def _level_settings(level):
    """The one Detail <-> Simple slider (0..10) -> the four tracing
    settings. Tuned on test logos: 0 follows every pixel step, 3 (the
    default) keeps ~99% of the ink area with ~1/8 of the elements, 10 is
    the fewest pieces (~95% of the area, small specks dropped)."""
    s = max(0, min(10, int(round(level))))
    return {"smooth": round(0.4 + 0.2 * s, 2), "simplify": round(0.3 * 1.35 ** s, 2),
            "corner": 35 + 5 * s, "speck": round(1 + 0.3 * s * s, 1)}


def _loop_pieces(loop):
    """Polyline loop (first == last) -> straight pieces."""
    return [("L", a, b) for a, b in zip(loop, loop[1:]) if a != b]


def _merge_short(pieces, tol):
    """Pieces of one closed loop -> the same loop with every piece at
    least `tol` long: a short piece is dropped and the next one starts
    where it started. None if too little is left to close a loop."""
    if not pieces:
        return None
    start = cur = pieces[0][1]
    out = []
    for pc in pieces:
        end = pc[-1]
        if math.hypot(end[0] - cur[0], end[1] - cur[1]) < tol:
            continue
        out.append(("L", cur, end) if pc[0] == "L" else ("C", cur, pc[2], pc[3], end))
        cur = end
    if out and cur != start:
        if math.hypot(start[0] - cur[0], start[1] - cur[1]) >= tol:
            out.append(("L", cur, start))
        else:
            last = out[-1]
            out[-1] = ("L", last[1], start) if last[0] == "L" else ("C", last[1], last[2], last[3], start)
    curves = len([p for p in out if p[0] == "C"])
    if len(out) < 2 or (curves == 0 and len(out) < 3):
        return None
    return out


def _count(geo):
    n_l = n_c = loops = 0
    for outer, holes in geo:
        for pcs in ([outer] + holes if outer else []):
            loops += 1
            for p in pcs:
                if p[0] == "L":
                    n_l += 1
                else:
                    n_c += 1
    return n_l, n_c, loops


# --------------------------------------------------------------------------
# Window
# --------------------------------------------------------------------------
class Conversion2DWindow(dee_branding.DeeBrandedWindow):
    _ready = False   # handlers fire while the XAML loads
    _quiet = False   # True while the slider fills in the advanced boxes

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
        self.mode_cb.SelectedIndex = int(st.get("mode", 0))
        self.simplify_tb.Text = str(st.get("simplify", "1"))
        self.corner_tb.Text = str(st.get("corner", "50"))
        self.level_sl.Value = float(st.get("level", 3))
        if "level" not in st:            # first run: let the slider decide
            self._apply_level()
        self.advanced_cb.IsChecked = bool(st.get("advanced", False))
        self._show_advanced()
        self.width_tb.Text = str(st.get("width", "100"))
        self.paper_cb.IsChecked = bool(st.get("paper", True))
        make = st.get("make")
        if make is None:      # settings saved before the Lines/Hatch choice
            make = 2 if (st.get("lines", True) and st.get("regions")) else (1 if st.get("regions") else 0)
        (self.make_lines_rb, self.make_hatch_rb, self.make_both_rb)[int(make) if make in (0, 1, 2) else 0].IsChecked = True
        self._sync_make()
        self.group_cb.IsChecked = bool(st.get("group", False))
        self.one_region_cb.IsChecked = bool(st.get("one_region", True))
        (self.dest_rfa_rb if st.get("dest") == "family" else self.dest_view_rb).IsChecked = True
        self.rfa_path_tb.Text = st.get("rfa_path", u"")
        self.rfa_load_cb.IsChecked = bool(st.get("rfa_load", True))
        self._sync_dest()
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
            "mode": self.mode_cb.SelectedIndex, "simplify": self.simplify_tb.Text, "corner": self.corner_tb.Text,
            "level": int(self.level_sl.Value), "advanced": bool(self.advanced_cb.IsChecked),
            "width": self.width_tb.Text, "paper": bool(self.paper_cb.IsChecked),
            "lines": self._want_lines(), "regions": self._want_hatch(), "make": self._make(),
            "group": bool(self.group_cb.IsChecked), "pattern": bool(self.pattern_cb.IsChecked),
            "one_region": bool(self.one_region_cb.IsChecked),
            "dest": "family" if self.dest_rfa_rb.IsChecked else "view",
            "rfa_path": (self.rfa_path_tb.Text or u"").strip(), "rfa_load": bool(self.rfa_load_cb.IsChecked),
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
        if self._ready and not self._quiet:
            self._refresh()

    # ---------------- the one Detail <-> Simple slider ----------------
    def _apply_level(self):
        st = _level_settings(self.level_sl.Value)
        self._quiet = True
        try:
            self.smooth_tb.Text = E.fmt_num(st["smooth"], 2)
            self.simplify_tb.Text = E.fmt_num(st["simplify"], 2)
            self.corner_tb.Text = str(st["corner"])
            self.speck_tb.Text = E.fmt_num(st["speck"], 1)
        finally:
            self._quiet = False

    def _is_custom(self):
        st = _level_settings(self.level_sl.Value)
        try:
            return (abs(_float(self.smooth_tb.Text, -1, False) - st["smooth"]) > 1e-6 or
                    abs(_float(self.simplify_tb.Text, -1, False) - st["simplify"]) > 1e-6 or
                    abs(_float(self.corner_tb.Text, -1, False) - st["corner"]) > 1e-6 or
                    abs(_float(self.speck_tb.Text, -1, False) - st["speck"]) > 1e-6)
        except Exception:
            return True

    def level_changed(self, sender, args):
        if not self._ready:
            return
        self._apply_level()
        self._refresh()

    def _show_advanced(self):
        vis = Visibility.Visible if self.advanced_cb.IsChecked else Visibility.Collapsed
        for c in (self.smooth_lbl, self.smooth_tb, self.speck_lbl, self.speck_tb,
                  self.simplify_lbl, self.simplify_tb, self.corner_lbl, self.corner_tb):
            c.Visibility = vis

    def advanced_changed(self, sender, args):
        if self._ready:
            self._show_advanced()

    # ---------------- Lines only / Hatch only / both ----------------
    def _make(self):
        if self.make_hatch_rb.IsChecked:
            return 1
        if self.make_both_rb.IsChecked:
            return 2
        return 0

    def _want_lines(self):
        return self._make() in (0, 2)

    def _want_hatch(self):
        return self._make() in (1, 2)

    def _sync_make(self):
        """Grey out the settings the chosen output doesn't use."""
        lines, hatch = self._want_lines(), self._want_hatch()
        for c in (self.lines_lbl, self.line_style_cb):
            c.IsEnabled = lines
        for c in (self.regions_lbl, self.region_type_cb, self.region_line_lbl, self.region_line_cb, self.one_region_cb):
            c.IsEnabled = hatch

    # ---------------- destination: view or .rfa ----------------
    def _sync_dest(self):
        fam = bool(self.dest_rfa_rb.IsChecked)
        self.rfa_panel.IsEnabled = fam
        self.group_cb.IsEnabled = not fam
        self.place_b.Content = "Create family..." if fam else "Place..."

    def dest_changed(self, sender, args):
        if self._ready:
            self._sync_dest()
            if self.dest_rfa_rb.IsChecked and not (self.rfa_path_tb.Text or u"").strip():
                self.rfa_browse_click(None, None)

    def _default_rfa_path(self):
        name = (self.pattern_name_tb.Text or u"").strip() or u"Detail Item"
        folder = os.path.dirname(self.rfa_path_tb.Text or u"") or os.path.dirname(self.path_tb.Text or u"")
        return os.path.join(folder or os.path.expanduser("~"), H._safe_filename(name) + u".rfa")

    def rfa_browse_click(self, sender, args):
        dlg = SaveFileDialog()
        dlg.Filter = "Revit family (*.rfa)|*.rfa"
        dlg.Title = "2D Conversion - save the Detail Item family"
        cur = (self.rfa_path_tb.Text or u"").strip() or self._default_rfa_path()
        if os.path.isdir(os.path.dirname(cur)):
            dlg.InitialDirectory = os.path.dirname(cur)
        dlg.FileName = os.path.basename(cur)
        if dlg.ShowDialog() == DialogResult.OK:
            self.rfa_path_tb.Text = dlg.FileName

    def make_changed(self, sender, args):
        if not self._ready:
            return
        self._sync_make()
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

    def geometry(self, curves=None):
        """[(outer_pieces, [hole_pieces])] - one entry per shape, in the
        same order whatever the mode, so a refused shape can be retried
        in a simpler form. curves None = the window's mode."""
        if self._mask is None:
            return []
        if curves is None:
            curves = self.mode_cb.SelectedIndex != 1
        smooth = max(0.0, _float(self.smooth_tb.Text, 0.0, positive=False))
        if not curves:
            return [(_loop_pieces(o), [_loop_pieces(hl) for hl in hs]) for o, hs in self.shapes(smooth)]
        tol = min(20.0, max(0.1, _float(self.simplify_tb.Text, 1.0)))
        corner = min(170.0, max(5.0, _float(self.corner_tb.Text, 50.0)))
        speck = max(0.0, _float(self.speck_tb.Text, 0.0, positive=False))
        key = ("fit", smooth, speck, tol, corner)
        if key not in self._shapes:
            self._shapes[key] = E.fit_shapes(self.shapes(0.0), smooth, tol, corner)
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
        geo = self.geometry()

        def Q(p):
            return Point(p[0], h - p[1])

        sg = StreamGeometry()
        sg.FillRule = FillRule.EvenOdd
        ctx = sg.Open()
        for outer, holes in geo:
            for pcs in ([outer] + holes if outer else []):
                ctx.BeginFigure(Q(pcs[0][1]), True, True)
                for pc in pcs:
                    if pc[0] == "L":
                        ctx.LineTo(Q(pc[2]), True, False)
                    else:
                        ctx.BezierTo(Q(pc[2]), Q(pc[3]), Q(pc[4]), True, False)
        ctx.Close()
        n_l, n_c, n_loops = _count(geo)
        self.level_info_tb.Text = u"Level {0}{1}: {2} line(s) + {3} curve(s) = {4} element(s)".format(
            int(self.level_sl.Value), u" (advanced values edited)" if self._is_custom() else u"",
            n_l, n_c, n_l + n_c)
        sg.Freeze()
        self.line_path.Data = sg if self._want_lines() else None
        self.fill_path.Data = sg if self._want_hatch() else None

        scale = self._scale()
        width_mm = _float(self.width_tb.Text, 100.0)
        px_mm = width_mm * scale / w           # model mm per pixel
        self.height_tb.Text = u"height {0} mm".format(E.fmt_num(width_mm * h / w, 1))
        info = u"{0} × {1} px (from {2} × {3}) · {4} shape(s), {5} outline loop(s) · {6} line(s) + {7} curve(s)".format(
            w, h, orig[0], orig[1], len([g for g in geo if g[0]]), n_loops, n_l, n_c)
        if self.mode_cb.SelectedIndex != 1:
            info += u"  (straight lines only would be {0})".format(_count(self.geometry(False))[0])
        self.info_tb.Text = info
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
        if o["dest"] == "family":
            if not o["rfa_path"]:
                self.rfa_browse_click(None, None)
                o["rfa_path"] = (self.rfa_path_tb.Text or u"").strip()
                if not o["rfa_path"]:
                    return
            if not o["rfa_path"].lower().endswith(".rfa"):
                o["rfa_path"] += ".rfa"
            if not (o["lines"] or o["regions"]):
                forms.alert("Choose Lines, Hatch or both for the family.")
                return
        elif (o["lines"] or o["regions"]) and self.view.ViewType in H._NO_DETAIL_VIEWS:
            forms.alert("The active view can't hold detail lines or filled regions.\n"
                        "Open a plan, section, elevation, drafting view or sheet and run it again.")
            return
        if o["pattern"] and not (o["pattern_name"] or u"").strip():
            forms.alert("Type a name for the fill pattern.")
            return
        if not any(g[0] for g in self.geometry()):
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


def _curve_builder(P, tol_px, counts):
    """-> loop_lines(pieces): pieces of one loop -> Revit curves (Line /
    exact NurbSpline), short pieces merged, refused splines flattened."""
    knots = List[float]([0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0])

    def loop_lines(pieces):
        pcs = _merge_short(pieces, tol_px)
        if pcs is None:
            return None
        out = []
        for pc in pcs:
            if pc[0] == "L":
                out.append(Line.CreateBound(P(pc[1]), P(pc[2])))
                continue
            try:
                out.append(NurbSpline.CreateCurve(3, knots, List[XYZ]([P(q) for q in pc[1:]])))
                counts["splines"] += 1
            except Exception:     # spline refused: same piece as short lines
                counts["flattened"] += 1
                pts = E.piece_points(pc, 6)
                kept = [pts[0]]
                for q in pts[1:-1]:
                    if math.hypot(q[0] - kept[-1][0], q[1] - kept[-1][1]) >= tol_px:
                        kept.append(q)
                kept.append(pts[-1])
                out.extend(Line.CreateBound(P(a), P(b)) for a, b in zip(kept, kept[1:]))
        return out
    return loop_lines


def _draw(doc, view, window, o, loop_lines, line_style, edge_style, rtype):
    """Creates the lines and/or filled regions of the image in `view` of
    `doc` (a project or a family). Runs inside the caller's transaction.
    Returns a stats dict with the new element ids."""
    factory = doc.FamilyCreate if doc.IsFamilyDocument else doc.Create
    st = {"ids": List[ElementId](), "lines": 0, "regions": 0,
          "failed_lines": 0, "failed_regions": 0, "notes": []}
    if o["lines"]:
        for outer, holes in window.geometry():
            for loop in ([outer] + holes if outer else []):
                try:
                    curves = loop_lines(loop)
                except Exception:
                    curves = None
                for c in curves or []:
                    try:
                        dc = factory.NewDetailCurve(view, c)
                        if line_style is not None:
                            try:
                                dc.LineStyle = line_style
                            except Exception:
                                pass
                        st["ids"].Add(dc.Id)
                        st["lines"] += 1
                    except Exception:
                        st["failed_lines"] += 1
    if o["regions"]:
        if rtype is None:
            raise ValueError("Pick a Filled Region Type.")
        # a shape Revit refuses is retried simpler: straight lines, then
        # the exact pixel outline
        tries = [window.geometry()]
        if window.mode_cb.SelectedIndex != 1:
            tries.append(window.geometry(False))
        tries.append([(_loop_pieces(ol), [_loop_pieces(hl) for hl in hls]) for ol, hls in window.shapes(0.0)])
        made = []
        done_as_one = False
        if o.get("one_region", True):
            fr, note = _one_region(doc, view, rtype, tries, loop_lines)
            if fr is not None:
                done_as_one = True
                made.append(fr)
            if note:
                st["notes"].append(note)
        for i in range(0 if done_as_one else len(tries[0])):
            if not tries[0][i][0]:
                continue
            fr = None
            for geo in tries:
                if i < len(geo) and geo[i][0]:
                    fr = _region(doc, view, rtype, geo[i][0], geo[i][1], loop_lines)
                    if fr is not None:
                        break
            if fr is None:
                st["failed_regions"] += 1
            else:
                made.append(fr)
        for fr in made:
            if edge_style is not None:
                try:
                    fr.SetLineStyleId(edge_style.Id)
                except Exception:
                    pass
            st["ids"].Add(fr.Id)
        st["regions"] = len(made)
    return st


def _summary(o, st, counts, extra):
    msg = []
    if o["lines"]:
        msg.append(u"{0} detail line(s) and curve(s){1}.".format(
            st["lines"], u", {0} too short to draw".format(st["failed_lines"]) if st["failed_lines"] else u""))
    if counts["flattened"]:
        msg.append(u"{0} curve(s) Revit refused were drawn as short lines instead.".format(counts["flattened"]))
    if o["regions"]:
        msg.append(u"{0} filled region(s){1}.".format(
            st["regions"], u", {0} shape(s) Revit refused (try a higher Simplify level or a lower resolution)".format(
                st["failed_regions"]) if st["failed_regions"] else u""))
    return msg + st["notes"] + extra


def place(uidoc, window, o):
    if o.get("dest") == "family":
        _make_family(uidoc, window, o)
        return
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

    counts = {"splines": 0, "flattened": 0}
    loop_lines = _curve_builder(P, tol_px, counts)
    styles = dict(_line_styles(doc))
    region_types = dict(_region_types(doc))
    st = None
    report = []
    t = Transaction(doc, "DeeLazy - 2D Conversion")
    t.Start()
    try:
        if want_2d:
            st = _draw(doc, view, window, o, loop_lines, styles.get(o["line_style"]),
                       styles.get(o["region_line"]), region_types.get(o["region_type"]))
        if o["pattern"]:
            try:
                name, how, n = _make_pattern(doc, window, o)
                report.append(u"Fill pattern '{0}' {1} ({2} lines) + Filled Region Type '{0}'.".format(name, how, n))
            except Exception as e:
                report.append(u"Fill pattern failed: {0}".format(e))
        if st is not None and o["group"] and st["ids"].Count > 1:
            try:
                grp = doc.Create.NewGroup(st["ids"])
                st["ids"] = List[ElementId]([grp.Id])
            except Exception:
                pass
        t.Commit()
    except Exception as e:
        t.RollBack()
        forms.alert(u"2D Conversion failed - nothing was changed:\n{0}".format(e))
        return
    st = st or {"ids": List[ElementId](), "lines": 0, "regions": 0, "failed_lines": 0,
                "failed_regions": 0, "notes": []}
    try:
        if st["ids"].Count:
            uidoc.Selection.SetElementIds(st["ids"])
    except Exception:
        pass
    if st["failed_lines"] or st["failed_regions"] or o["pattern"] or counts["flattened"] or st["notes"]:
        forms.alert(u"2D Conversion done:\n\n" + u"\n".join(_summary(o, st, counts, report)))


# --------------------------------------------------------------------------
# Detail Item family (.rfa)
# --------------------------------------------------------------------------
def find_detail_item_template(app):
    """Revit's own 'Metric Detail Item.rft' (or 'Detail Item.rft') from the
    family template folder of this install - any language folder."""
    roots = []
    try:
        base = app.FamilyTemplatePath
        if base:
            roots += [base, os.path.dirname(base.rstrip("\\/"))]
    except Exception:
        pass
    pd = os.environ.get("ProgramData") or r"C:\ProgramData"
    try:
        roots.append(os.path.join(pd, "Autodesk", "RVT " + str(app.VersionNumber), "Family Templates"))
    except Exception:
        pass
    found = []
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                lf = f.lower()
                if lf.endswith(".rft") and "detail item" in lf and "tag" not in lf and "line based" not in lf:
                    found.append(os.path.join(dirpath, f))
        if found:
            break

    def rank(p):
        lf = os.path.basename(p).lower()
        eng = "english" in p.lower()
        return (0 if lf.startswith("metric") else 1, 0 if eng else 1, len(lf))
    found.sort(key=rank)
    return found[0] if found else None


class _LoadOptions(DB.IFamilyLoadOptions):
    """Reload over an older version of the same family."""

    def OnFamilyFound(self, familyInUse, overwriteParameterValues):
        overwriteParameterValues.Value = True
        return True

    def OnSharedFamilyFound(self, sharedFamily, familyInUse, source, overwriteParameterValues):
        source.Value = DB.FamilySource.Family
        overwriteParameterValues.Value = True
        return True


def _family_line_style(fdoc, pdoc, name):
    """A Detail Items sub-category in the family with the same name,
    weight and colour as the project line style `name`."""
    if not name:
        return None
    try:
        if name.startswith("<"):                      # <Invisible lines> etc.
            for gs_name, gs in _line_styles(fdoc):
                if gs_name == name:
                    return gs
            return None
        fam_cat = fdoc.OwnerFamily.FamilyCategory
        sub = None
        for s in fam_cat.SubCategories:
            if s.Name == name:
                sub = s
                break
        if sub is None:
            sub = fdoc.Settings.Categories.NewSubcategory(fam_cat, name)
            try:
                pcat = pdoc.Settings.Categories.get_Item(BuiltInCategory.OST_Lines).SubCategories.get_Item(name)
                w = pcat.GetLineWeight(GraphicsStyleType.Projection)
                if w:
                    sub.SetLineWeight(w, GraphicsStyleType.Projection)
                sub.LineColor = pcat.LineColor
            except Exception:
                pass
        return sub.GetGraphicsStyle(GraphicsStyleType.Projection)
    except Exception:
        return None


def _copy_region_type(fdoc, pdoc, rtype):
    """Copies the project's Filled Region Type (with its fill patterns)
    into the family; returns the family's copy."""
    if rtype is None:
        return None
    name = DB.Element.Name.GetValue(rtype)
    for n, t in _region_types(fdoc):
        if n == name:
            return t
    ids = List[ElementId]([rtype.Id])
    opts = DB.CopyPasteOptions()
    new_ids = DB.ElementTransformUtils.CopyElements(pdoc, ids, fdoc, DB.Transform.Identity, opts)
    for nid in new_ids:
        e = fdoc.GetElement(nid)
        if isinstance(e, FilledRegionType):
            return e
    types = _region_types(fdoc)
    return types[0][1] if types else None


def _family_view(fdoc):
    for v in FilteredElementCollector(fdoc).OfClass(DB.ViewPlan):
        if not v.IsTemplate:
            return v
    for v in FilteredElementCollector(fdoc).OfClass(DB.View):
        if not v.IsTemplate and v.ViewType not in H._NO_DETAIL_VIEWS:
            return v
    return None


def _make_family(uidoc, window, o):
    doc = uidoc.Document
    app = doc.Application
    path = o.get("rfa_path") or u""
    template = find_detail_item_template(app)
    if not template:
        dlg = OpenFileDialog()
        dlg.Filter = "Family template (*.rft)|*.rft"
        dlg.Title = "2D Conversion - pick the 'Metric Detail Item.rft' family template"
        if dlg.ShowDialog() != DialogResult.OK:
            return
        template = dlg.FileName

    _rows, w, h, _orig = window._lum
    k = _float(o["width"], 100.0) / _MM * o["scale"] / w      # feet per pixel
    tol_px = app.ShortCurveTolerance * 1.01 / k

    def P(p):     # image centre = family origin (the insertion point)
        return XYZ((p[0] - w / 2.0) * k, (p[1] - h / 2.0) * k, 0.0)

    counts = {"splines": 0, "flattened": 0}
    loop_lines = _curve_builder(P, tol_px, counts)
    fdoc = None
    st = None
    try:
        fdoc = app.NewFamilyDocument(template)
        fview = _family_view(fdoc)
        if fview is None:
            raise ValueError(u"The template has no plan view to draw in:\n" + template)
        t = Transaction(fdoc, "2D Conversion")
        t.Start()
        try:
            line_style = _family_line_style(fdoc, doc, o["line_style"]) if o["lines"] else None
            edge_style = _family_line_style(fdoc, doc, o["region_line"]) if o["regions"] else None
            rtype = _copy_region_type(fdoc, doc, dict(_region_types(doc)).get(o["region_type"])) if o["regions"] else None
            st = _draw(fdoc, fview, window, o, loop_lines, line_style, edge_style, rtype)
            t.Commit()
        except Exception:
            t.RollBack()
            raise
        if not st["ids"].Count:
            raise ValueError("Nothing could be drawn into the family.")
        folder = os.path.dirname(path)
        if folder and not os.path.isdir(folder):
            os.makedirs(folder)
        so = DB.SaveAsOptions()
        so.OverwriteExistingFile = True
        fdoc.SaveAs(path, so)
        family = None
        if o.get("rfa_load"):
            family = fdoc.LoadFamily(doc, _LoadOptions())
    except Exception as e:
        forms.alert(u"Could not make the Detail Item family:\n{0}".format(e))
        return
    finally:
        if fdoc is not None:
            try:
                fdoc.Close(False)
            except Exception:
                pass

    extra = [u"Saved: " + path]
    placed = False
    if family is not None:
        extra.append(u"Loaded into the project as '{0}'.".format(family.Name))
        view = doc.ActiveView
        if view.ViewType not in H._NO_DETAIL_VIEWS and view.ViewType != ViewType.DrawingSheet:
            try:
                _ensure_work_plane(doc, view)
                pt = uidoc.Selection.PickPoint("2D Conversion: click where to place the Detail Item (its centre) - Esc to only load it")
                t = Transaction(doc, "DeeLazy - 2D Conversion place Detail Item")
                t.Start()
                try:
                    sym = doc.GetElement(list(family.GetFamilySymbolIds())[0])
                    if not sym.IsActive:
                        sym.Activate()
                        doc.Regenerate()
                    inst = doc.Create.NewFamilyInstance(pt, sym, view)
                    t.Commit()
                    placed = True
                    uidoc.Selection.SetElementIds(List[ElementId]([inst.Id]))
                except Exception as e:
                    t.RollBack()
                    extra.append(u"Could not place it: {0}".format(e))
            except OperationCanceledException:
                pass
    if placed:
        extra.append(u"Placed in '{0}'.".format(doc.ActiveView.Name))
    report = []
    if o["pattern"]:
        t = Transaction(doc, "DeeLazy - 2D Conversion fill pattern")
        t.Start()
        try:
            name, how, n = _make_pattern(doc, window, o)
            t.Commit()
            report.append(u"Fill pattern '{0}' {1} ({2} lines) + Filled Region Type '{0}'.".format(name, how, n))
        except Exception as e:
            t.RollBack()
            report.append(u"Fill pattern failed: {0}".format(e))
    forms.alert(u"Detail Item family done:\n\n" + u"\n".join(_summary(o, st, counts, extra + report)))


def _create_region(doc, view, rtype, curve_lists):
    loops = List[CurveLoop]()
    for lines in curve_lists:
        cl = CurveLoop()
        for c in lines:
            cl.Append(c)
        loops.Add(cl)
    return FilledRegion.Create(doc, rtype.Id, view.Id, loops)


def _shape_curves(shape, loop_lines):
    """(outer_pieces, [hole_pieces]) -> [curve list per loop] or None."""
    outer, holes = shape
    if not outer:
        return None
    try:
        first = loop_lines(outer)
    except Exception:
        return None
    if not first:
        return None
    out = [first]
    for hp in holes:
        try:
            c = loop_lines(hp)
        except Exception:
            c = None
        if c:
            out.append(c)
    return out


def _works(doc, view, rtype, curve_lists):
    """Would Revit accept this region? Tested and rolled back."""
    st = DB.SubTransaction(doc)
    st.Start()
    try:
        _create_region(doc, view, rtype, curve_lists)
        return True
    except Exception:
        return False
    finally:
        st.RollBack()


def _one_region(doc, view, rtype, tries, loop_lines):
    """Every shape of the image in ONE Filled Region (all outlines and
    holes as loops of one sketch). tries = the same shape list in
    simpler and simpler forms. Returns (region or None, note)."""
    n = len(tries[0])
    for geo in tries:                    # 1. everything at once
        lists = []
        for shape in geo:
            c = _shape_curves(shape, loop_lines)
            if c:
                lists.extend(c)
        if lists:
            try:
                return _create_region(doc, view, rtype, lists), None
            except Exception:
                pass
    # 2. some shape is the problem: per shape, take the first form Revit
    #    accepts on its own, drop shapes it never accepts, then combine
    chosen, dropped = [], 0
    for i in range(n):
        if not tries[0][i][0]:
            continue
        ok = None
        for geo in tries:
            if i < len(geo):
                c = _shape_curves(geo[i], loop_lines)
                if c and _works(doc, view, rtype, c):
                    ok = c
                    break
        if ok is None:
            dropped += 1
        else:
            chosen.extend(ok)
    if chosen:
        try:
            fr = _create_region(doc, view, rtype, chosen)
            note = (u"{0} tiny or broken shape(s) were left out of the Filled Region.".format(dropped)
                    if dropped else None)
            return fr, note
        except Exception:
            pass
    return None, (u"Revit would not take all shapes as one Filled Region (some shapes touch "
                  u"each other) - they were made as separate Filled Regions instead.")


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
