# -*- coding: utf-8 -*-
"""
DeeLazy - Text to Lines module
Type a text, pick a font, and the letters are drawn in the active view
as Detail Lines (straight parts) and spline Detail Curves (round parts)
- an "explode text" for Revit, which has no built-in way to do it.

--------------------------------------------------------------------
How the outline is obtained
--------------------------------------------------------------------
WPF already knows every installed font's glyph outlines:
FormattedText.BuildGeometry() returns them as a geometry made of
LineSegments and Bezier segments (cubic for OpenType/CFF fonts,
quadratic for TrueType). That geometry is normalised with
PathGeometry.CreateFromGeometry and walked figure by figure:
  - straight segments  -> Revit Line
  - cubic Bezier       -> Revit NurbSpline (a cubic Bezier IS a degree-3
                          NURBS with knots 0,0,0,0,1,1,1,1 - exact, not
                          an approximation)
  - quadratic Bezier   -> raised to cubic first (exact), then as above
  - ArcSegment         -> fonts never produce these; treated as a line
                          to its end point so the outline still closes
The same geometry feeds the preview Path in the window, so what the
preview shows is exactly what gets drawn.

--------------------------------------------------------------------
Revit's short-curve limit
--------------------------------------------------------------------
Revit refuses curves shorter than Application.ShortCurveTolerance
(~0.8 mm). Tiny glyph pieces (serif tips, small text) are therefore
MERGED into the next piece rather than dropped: the builder keeps the
last vertex actually drawn and only emits a curve once the distance
from it is long enough, so the outline never gets a gap. Text whose
whole letters are below the limit simply cannot be drawn - the window
warns when the model-size height is that small.

Size convention: "Text height" is the cap height (height of a capital
letter), the same thing Revit's own Text Size measures, via
Typeface.CapsHeight.

NEEDS LIVE-REVIT VERIFICATION: NurbSpline detail curves on every
Revit version in use (a failed spline falls back to straight lines
per curve, so the worst case is "lines only", never an error).
"""
import os

import clr
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
clr.AddReference("WindowsBase")

from System.Collections.Generic import List
from System.Globalization import CultureInfo
from System.Windows import FlowDirection, FontStyles, FontWeights, FontStretches, Point, TextAlignment
from System.Windows.Media import (
    Brushes, FontFamily, Fonts, FormattedText, Typeface, PathGeometry,
    LineSegment, PolyLineSegment, BezierSegment, PolyBezierSegment,
    QuadraticBezierSegment, PolyQuadraticBezierSegment,
)

from pyrevit import forms
import dee_branding
import deew_settings

from Autodesk.Revit.DB import (
    XYZ, Line, NurbSpline, Transaction, ViewType, SketchPlane, Plane,
    BuiltInCategory, GraphicsStyleType, ElementId, UnitUtils, UnitTypeId,
)
from Autodesk.Revit.Exceptions import OperationCanceledException

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "TextToLines.xaml")
_SETTINGS = "dee_text_to_lines"
_EM = 100.0   # em size the outline is built at; scaled to the real height afterwards

# Built by name so a member missing in some Revit version is just skipped.
_NO_DETAIL_VIEWS = tuple(getattr(ViewType, n) for n in (
    "ThreeD", "Schedule", "ColumnSchedule", "PanelSchedule", "Report", "ProjectBrowser",
    "SystemBrowser", "Internal", "Undefined", "Walkthrough", "Rendering", "CostReport",
    "LoadsReport", "PresureLossReport", "SystemsAnalysisReport") if hasattr(ViewType, n))


def _mm_to_ft(mm):
    try:
        return UnitUtils.ConvertToInternalUnits(float(mm), UnitTypeId.Millimeters)
    except Exception:
        return float(mm) / 304.8


def _float(text, default):
    try:
        v = float((text or "").strip().replace(",", "."))
        return v if v > 0 else default
    except Exception:
        return default


# --------------------------------------------------------------------------
# Glyph outlines (pure WPF - no Revit needed)
# --------------------------------------------------------------------------
def _typeface(font_name, bold, italic):
    return Typeface(FontFamily(font_name),
                    FontStyles.Italic if italic else FontStyles.Normal,
                    FontWeights.Bold if bold else FontWeights.Normal,
                    FontStretches.Normal)


def _formatted_text(text, typeface, align):
    def make():
        try:   # .NET 4.6.2+ overload (pixelsPerDip); the old one is obsolete
            return FormattedText(text, CultureInfo.CurrentUICulture, FlowDirection.LeftToRight,
                                 typeface, _EM, Brushes.Black, 1.0)
        except Exception:
            return FormattedText(text, CultureInfo.CurrentUICulture, FlowDirection.LeftToRight,
                                 typeface, _EM, Brushes.Black)
    ft = make()
    if align != "Left" and "\n" in text:
        # Centre/right each line within the widest one.
        ft.MaxTextWidth = ft.WidthIncludingTrailingWhitespace + 1.0
        ft.TextAlignment = TextAlignment.Center if align == "Center" else TextAlignment.Right
    return ft


def build_outline(text, font_name, bold, italic, align):
    """Returns (geometry, figures, baseline, cap_height) in WPF units at
    _EM size, y pointing DOWN. figures = list of (start_pt, closed, items),
    items = ('L', p) or ('C', c1, c2, p) with points as (x, y) tuples."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return None, [], 0.0, 1.0
    tf = _typeface(font_name, bold, italic)
    ft = _formatted_text(text, tf, align)
    geom = ft.BuildGeometry(Point(0, 0))
    pg = PathGeometry.CreateFromGeometry(geom)
    figures = []
    for fig in pg.Figures:
        start = (fig.StartPoint.X, fig.StartPoint.Y)
        cur = start
        items = []
        for seg in fig.Segments:
            if isinstance(seg, LineSegment):
                cur = (seg.Point.X, seg.Point.Y)
                items.append(("L", cur))
            elif isinstance(seg, PolyLineSegment):
                for p in seg.Points:
                    cur = (p.X, p.Y)
                    items.append(("L", cur))
            elif isinstance(seg, BezierSegment):
                cur = (seg.Point3.X, seg.Point3.Y)
                items.append(("C", (seg.Point1.X, seg.Point1.Y), (seg.Point2.X, seg.Point2.Y), cur))
            elif isinstance(seg, PolyBezierSegment):
                pts = list(seg.Points)
                for i in range(0, len(pts) - 2, 3):
                    cur = (pts[i + 2].X, pts[i + 2].Y)
                    items.append(("C", (pts[i].X, pts[i].Y), (pts[i + 1].X, pts[i + 1].Y), cur))
            elif isinstance(seg, QuadraticBezierSegment):
                q = (seg.Point1.X, seg.Point1.Y)
                end = (seg.Point2.X, seg.Point2.Y)
                items.append(_quad_to_cubic(cur, q, end))
                cur = end
            elif isinstance(seg, PolyQuadraticBezierSegment):
                pts = list(seg.Points)
                for i in range(0, len(pts) - 1, 2):
                    q = (pts[i].X, pts[i].Y)
                    end = (pts[i + 1].X, pts[i + 1].Y)
                    items.append(_quad_to_cubic(cur, q, end))
                    cur = end
            else:   # ArcSegment etc. - not produced by fonts
                try:
                    cur = (seg.Point.X, seg.Point.Y)
                    items.append(("L", cur))
                except Exception:
                    pass
        if items:
            figures.append((start, bool(fig.IsClosed), items))
    try:
        cap = tf.CapsHeight * _EM
    except Exception:
        cap = 0.7 * _EM
    return pg, figures, ft.Baseline, (cap if cap > 0 else 0.7 * _EM)


def _quad_to_cubic(p0, q, p3):
    c1 = (p0[0] + 2.0 / 3.0 * (q[0] - p0[0]), p0[1] + 2.0 / 3.0 * (q[1] - p0[1]))
    c2 = (p3[0] + 2.0 / 3.0 * (q[0] - p3[0]), p3[1] + 2.0 / 3.0 * (q[1] - p3[1]))
    return ("C", c1, c2, p3)


def _bezier_point(p0, c1, c2, p3, t):
    u = 1.0 - t
    a, b, c, d = u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t
    return (a * p0[0] + b * c1[0] + c * c2[0] + d * p3[0],
            a * p0[1] + b * c1[1] + c * c2[1] + d * p3[1])


def _dist(a, b):
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def _off_chord(p, a, b):
    """Distance of p from the straight line a-b."""
    L = _dist(a, b)
    if L < 1e-12:
        return _dist(p, a)
    return abs((b[0] - a[0]) * (a[1] - p[1]) - (a[0] - p[0]) * (b[1] - a[1])) / L


def count_items(figures, lines_only, segments):
    lines = curves = 0
    for _start, closed, items in figures:
        for it in items:
            if it[0] == "L":
                lines += 1
            elif lines_only:
                lines += segments
            else:
                curves += 1
        if closed:
            lines += 1
    return lines, curves


# --------------------------------------------------------------------------
# Revit side
# --------------------------------------------------------------------------
class _Builder(object):
    """Turns outline figures (local 2D units) into Revit curves, merging
    pieces shorter than Revit's short-curve tolerance into the next one."""

    def __init__(self, to_world, tol, lines_only, segments):
        self.to_world = to_world     # (x, y) local -> XYZ
        self.tol = tol               # in local units
        self.lines_only = lines_only
        self.segments = max(2, segments)
        self.curves = []             # Revit Curve objects
        self.n_lines = 0
        self.n_splines = 0
        self.n_fallback = 0

    def _line(self, a, b):
        try:
            self.curves.append(Line.CreateBound(self.to_world(a), self.to_world(b)))
            self.n_lines += 1
            return True
        except Exception:
            return False

    def _spline(self, p0, c1, c2, p3):
        pts = List[XYZ]([self.to_world(p) for p in (p0, c1, c2, p3)])
        knots = List[float]([0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0])
        try:
            self.curves.append(NurbSpline.CreateCurve(3, knots, pts))
            self.n_splines += 1
            return True
        except Exception:
            return False

    def figure(self, start, closed, items):
        cur = start      # last vertex actually drawn
        for it in items:
            if it[0] == "L":
                cur = self._to(cur, it[1])
                continue
            c1, c2, end = it[1], it[2], it[3]
            flat = max(_off_chord(c1, cur, end), _off_chord(c2, cur, end)) < self.tol * 0.05
            if flat:
                cur = self._to(cur, end)
            elif self.lines_only or _dist(cur, end) < self.tol * 3:
                cur = self._flatten(cur, c1, c2, end)
            elif self._spline(cur, c1, c2, end):
                cur = end
            else:
                self.n_fallback += 1
                cur = self._flatten(cur, c1, c2, end)
        if closed:
            self._to(cur, start)

    def _flatten(self, p0, c1, c2, p3):
        cur = p0
        for i in range(1, self.segments + 1):
            cur = self._to(cur, _bezier_point(p0, c1, c2, p3, float(i) / self.segments))
        return cur

    def _to(self, cur, p):
        if _dist(cur, p) >= self.tol and self._line(cur, p):
            return p
        return cur   # too short: keep the old vertex, the next piece bridges it


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


def _view_scale(view):
    if view.ViewType == ViewType.DrawingSheet:
        return 1
    try:
        return max(1, int(view.Scale))
    except Exception:
        return 1


class StyleItem(object):
    def __init__(self, name, gs):
        self.name = name
        self.gs = gs

    def ToString(self):
        return self.name

    def __str__(self):
        return self.name


# --------------------------------------------------------------------------
# Window
# --------------------------------------------------------------------------
class TextToLinesWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, uiapp):
        self._ready = False
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.uiapp = uiapp
        self.uidoc = uiapp.ActiveUIDocument
        self.doc = self.uidoc.Document
        self.view = self.doc.ActiveView
        self.request = None      # filled by Place; read by launch() after ShowDialog
        self._settings = deew_settings.load(_SETTINGS, {}) or {}

        names = sorted(set(f.Source for f in Fonts.SystemFontFamilies), key=lambda s: s.lower())
        for n in names:
            self.font_cb.Items.Add(n)
        want = self._settings.get("font") or "Arial"
        self.font_cb.SelectedItem = want if want in names else ("Arial" if "Arial" in names else (names[0] if names else None))

        self._styles = [StyleItem(n, gs) for n, gs in _line_styles(self.doc)]
        for s in self._styles:
            self.style_cb.Items.Add(s)
        want_style = self._settings.get("style") or "Thin Lines"
        for s in self._styles:
            if s.name == want_style:
                self.style_cb.SelectedItem = s
                break
        if self.style_cb.SelectedItem is None and self._styles:
            self.style_cb.SelectedIndex = 0

        st = self._settings
        if st.get("text"):
            self.text_tb.Text = st["text"]
        self.height_tb.Text = str(st.get("height", "5"))
        self.bold_cb.IsChecked = bool(st.get("bold", False))
        self.italic_cb.IsChecked = bool(st.get("italic", False))
        self.paper_cb.IsChecked = bool(st.get("paper", True))
        self.group_cb.IsChecked = bool(st.get("group", True))
        self.segments_tb.Text = str(st.get("segments", "6"))
        if st.get("lines_only"):
            self.mode_lines_rb.IsChecked = True
        align = st.get("align", "Left")
        for i, label in enumerate(("Left", "Center", "Right")):
            if label == align:
                self.align_cb.SelectedIndex = i

        self._ready = True
        self._refresh()
        try:
            self.text_tb.Focus()
            self.text_tb.SelectAll()
        except Exception:
            pass

    # ---------------- options ----------------
    def _align(self):
        item = self.align_cb.SelectedItem
        try:
            return str(item.Content)
        except Exception:
            return "Left"

    def _opts(self):
        return {
            "text": self.text_tb.Text or "",
            "font": self.font_cb.SelectedItem or "Arial",
            "bold": bool(self.bold_cb.IsChecked),
            "italic": bool(self.italic_cb.IsChecked),
            "height": _float(self.height_tb.Text, 5.0),
            "paper": bool(self.paper_cb.IsChecked),
            "align": self._align(),
            "lines_only": bool(self.mode_lines_rb.IsChecked),
            "segments": int(_float(self.segments_tb.Text, 6)),
            "group": bool(self.group_cb.IsChecked),
            "style": self.style_cb.SelectedItem.name if self.style_cb.SelectedItem is not None else "",
        }

    def option_changed(self, sender, args):
        if getattr(self, "_ready", False):
            self._refresh()

    def _refresh(self):
        o = self._opts()
        try:
            geom, figures, _base, cap = build_outline(o["text"], o["font"], o["bold"], o["italic"], o["align"])
        except Exception as e:
            self.preview_path.Data = None
            self.status_tb.Text = u"Could not read that font: {0}".format(e)
            return
        self.preview_path.Data = geom
        if not figures:
            self.status_tb.Text = "Type some text to draw."
            return
        lines, curves = count_items(figures, o["lines_only"], o["segments"])
        scale = _view_scale(self.view) if o["paper"] else 1
        model_mm = o["height"] * scale
        msg = u"About {0} lines + {1} curves.  Model height {2:g} mm in '{3}'".format(
            lines, curves, model_mm, self.view.Name)
        if o["paper"] and scale != 1:
            msg += u" (1:{0})".format(scale)
        msg += "."
        if model_mm < 4:
            msg += u"  Warning: very small - Revit can't draw lines under ~0.8 mm, so fine detail will be simplified."
        if self.view.ViewType in _NO_DETAIL_VIEWS:
            msg += u"  The active view can't hold detail lines - open a plan, section, drafting view or sheet."
        self.status_tb.Text = msg

    # ---------------- buttons ----------------
    def place_click(self, sender, args):
        o = self._opts()
        if not (o["text"] or "").strip():
            self.status_tb.Text = "Type some text first."
            return
        if self.view.ViewType in _NO_DETAIL_VIEWS:
            forms.alert("The active view can't hold detail lines.\n"
                        "Open a plan, section, elevation, drafting view or sheet and run it again.")
            return
        try:
            deew_settings.save(_SETTINGS, o)
        except Exception:
            pass
        self.request = o
        self.Close()

    def close_click(self, sender, args):
        try:
            deew_settings.save(_SETTINGS, self._opts())
        except Exception:
            pass
        self.Close()


# --------------------------------------------------------------------------
# Placement (runs after the window has closed)
# --------------------------------------------------------------------------
def _ensure_work_plane(doc, view):
    """PickPoint needs a work plane in sections/elevations/drafting."""
    try:
        if view.SketchPlane is not None or view.ViewType == ViewType.DrawingSheet:
            return
    except Exception:
        return
    t = Transaction(doc, "DeeLazy - Text to Lines work plane")
    t.Start()
    try:
        plane = Plane.CreateByNormalAndOrigin(view.ViewDirection, view.Origin)
        view.SketchPlane = SketchPlane.Create(doc, plane)
        t.Commit()
    except Exception:
        t.RollBack()


def place(uidoc, o):
    doc = uidoc.Document
    view = doc.ActiveView
    _geom, figures, baseline, cap = build_outline(o["text"], o["font"], o["bold"], o["italic"], o["align"])
    if not figures:
        return

    _ensure_work_plane(doc, view)
    try:
        origin = uidoc.Selection.PickPoint("Text to Lines: click the insertion point (Esc to cancel)")
    except OperationCanceledException:
        return

    scale = _view_scale(view) if o["paper"] else 1
    height_ft = _mm_to_ft(o["height"] * scale)
    k = height_ft / cap                    # local WPF unit -> feet
    right, up = view.RightDirection, view.UpDirection

    xs = [p[0] for _s, _c, items in figures for it in items for p in it[1:]]
    xs += [s[0] for s, _c, _i in figures]
    x0 = {"Left": min(xs), "Center": (min(xs) + max(xs)) / 2.0, "Right": max(xs)}[o["align"]]

    def to_world(p):
        return origin + right * ((p[0] - x0) * k) + up * ((baseline - p[1]) * k)

    tol = doc.Application.ShortCurveTolerance * 1.01 / k
    b = _Builder(to_world, tol, o["lines_only"], o["segments"])
    for start, closed, items in figures:
        b.figure(start, closed, items)

    style = None
    for n, gs in _line_styles(doc):
        if n == o.get("style"):
            style = gs
            break

    ids = List[ElementId]()
    failed = 0
    t = Transaction(doc, "DeeLazy - Text to Lines")
    t.Start()
    try:
        for c in b.curves:
            try:
                dc = doc.Create.NewDetailCurve(view, c)
            except Exception:
                failed += 1
                continue
            if style is not None:
                try:
                    dc.LineStyle = style
                except Exception:
                    pass
            ids.Add(dc.Id)
        if o["group"] and ids.Count > 1:
            try:
                grp = doc.Create.NewGroup(ids)
                _name_group(doc, grp, o["text"])
                ids = List[ElementId]([grp.Id])
            except Exception:
                pass
        t.Commit()
    except Exception as e:
        t.RollBack()
        forms.alert(u"Text to Lines failed:\n{0}".format(e))
        return

    try:
        uidoc.Selection.SetElementIds(ids)
    except Exception:
        pass
    if failed or b.n_fallback:
        forms.alert(u"Drawn: {0} lines, {1} curves.\n{2} piece(s) could not be created{3}.".format(
            b.n_lines, b.n_splines, failed,
            u"; {0} curve(s) were drawn as straight lines instead".format(b.n_fallback) if b.n_fallback else ""))


def _name_group(doc, grp, text):
    base = u"Text - " + u" ".join((text or "").split())[:40]
    from Autodesk.Revit.DB import FilteredElementCollector, GroupType
    taken = set(gt.Name for gt in FilteredElementCollector(doc).OfClass(GroupType))
    name, i = base, 2
    while name in taken:
        name = u"{0} ({1})".format(base, i)
        i += 1
    try:
        grp.GroupType.Name = name
    except Exception:
        pass


def launch(uiapp):
    if uiapp.ActiveUIDocument is None:
        forms.alert("Open a Revit project first.")
        return
    window = TextToLinesWindow(_XAML_FILE, uiapp)
    window.ShowDialog()
    if window.request is not None:
        place(uiapp.ActiveUIDocument, window.request)


TOOL_INFO = {
    "id": "dee_text_to_lines",
    "title": "Text to Lines",
    "description": "Type a text and draw it as Detail Lines and spline curves in any font - an 'explode text' for Revit.",
    "icon": u"\U0001F524",  # input latin letters - text turned into geometry
    "category": "Drafting & Annotation",
    "launch": launch,
}
