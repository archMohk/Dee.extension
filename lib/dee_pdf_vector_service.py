# -*- coding: utf-8 -*-
"""
dee_pdf_vector_service
PDF -> real Revit linework in a new Drafting View per PDF page:
  * every stroke        -> Detail Line using the project's OWN line styles,
                           chosen by stroke width (table or nearest pen)
  * every solid fill    -> Filled Region (one region type per colour)
  * runs of close, same-direction strokes (hatching, any colour/weight)
                        -> ONE Filled Region with a drafting hatch pattern
                           (angle and spacing measured from the strokes);
                           spacing finer than ~0.3 mm -> one solid region.

--------------------------------------------------------------------
How it works
--------------------------------------------------------------------
1. lib/dee_pdf_vector_helper.py (normal Python + PyMuPDF, run as a
   subprocess - IronPython has no PDF library) reads the page vectors,
   trims the border / title block, turns the page, finds the hatches and
   writes JSON.  It also renders the orientation preview (png).
2. This module reads that JSON and builds the elements, one
   Transaction per page, so a bad page never rolls back the others.

Scale
- Line.CreateBound throws for a curve shorter than
  Application.ShortCurveTolerance (about 0.8 mm at REAL size). The drawing
  is therefore enlarged by N and the view is set to 1:N - it still prints
  at paper size.  N is typed by the user, or "Auto": the smallest standard
  scale at which the shortest stroke is still long enough, so no line is
  dropped.  Drafting hatch patterns are paper-sized and are not enlarged.

Speed
- Weight comes from existing line styles (no new styles made); only coloured
  strokes get a colour override when "keep colours" is on.
- Hatching collapses hundreds of strokes into one element.
"""
import json
import math
import os
import shutil
import subprocess
import tempfile
import time

from System.Collections.Generic import List
from Autodesk.Revit.DB import (
    Transaction, XYZ, Line, CurveLoop, FilledRegion, FilledRegionType,
    FillPattern, FillPatternElement, FillPatternTarget, FillPatternHostOrientation,
    LinePatternElement, Color, FilteredElementCollector, BuiltInCategory,
    Category, GraphicsStyleType, ElementId, DetailElementOrderUtils,
    IFailuresPreprocessor, FailureSeverity, FailureProcessingResult,
    OverrideGraphicSettings,
)

import dee_draft_import_service as base

_MM_PER_PT = 25.4 / 72.0
_FT_PER_PT = 1.0 / 72.0 / 12.0
STANDARD_SCALES = [1, 2, 5, 10, 20, 25, 50, 100, 200]


class _SwallowWarnings(IFailuresPreprocessor):
    def PreprocessFailures(self, accessor):
        try:
            for f in list(accessor.GetFailureMessages()):
                if f.GetSeverity() == FailureSeverity.Warning:
                    accessor.DeleteWarning(f)
        except Exception:
            pass
        return FailureProcessingResult.Continue


# ---------------------------------------------------------------------------
# helper subprocess
# ---------------------------------------------------------------------------
def _helper_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "dee_pdf_vector_helper.py")


def _run(cmd):
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         creationflags=0x08000000)
    _, err = p.communicate()
    return p.returncode, (err or "").strip()


def extract(path, margins, rot=0, mirror=False, hatch=True):
    """([page dicts], error text). margins = (left, top, right, bottom) pt."""
    py = base.find_pymupdf_python()
    if py is None:
        return None, ("No Python with PyMuPDF was found. Install it once with:  "
                      "py -m pip install pymupdf")
    tmp = tempfile.mkdtemp(prefix="dee_pdfvec_")
    out = os.path.join(tmp, "v.json")
    try:
        cmd = py + ["-I", _helper_path(), path, out] + [str(m) for m in margins] + \
            [str(int(rot)), "1" if mirror else "0", "1" if hatch else "0"]
        rc, err = _run(cmd)
        if rc != 0 or not os.path.isfile(out):
            return None, err or "extraction failed"
        with open(out) as fh:
            return json.load(fh)["pages"], ""
    except Exception as ex:
        return None, str(ex)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def render_preview(path, page_index, margins, rot, mirror, out_png, maxpx=700):
    """Writes a png of the trimmed, turned page. Returns (ok, error text)."""
    py = base.find_pymupdf_python()
    if py is None:
        return False, "PyMuPDF is not installed."
    try:
        cmd = py + ["-I", _helper_path(), "--preview", path, out_png, str(int(page_index))] + \
            [str(m) for m in margins] + [str(int(rot)), "1" if mirror else "0", str(maxpx)]
        rc, err = _run(cmd)
        return (rc == 0 and os.path.isfile(out_png)), err
    except Exception as ex:
        return False, str(ex)


# ---------------------------------------------------------------------------
# Revit helpers
# ---------------------------------------------------------------------------
def _eid(e):
    try:
        return e.Value
    except Exception:
        return e.IntegerValue


def _color(rgb):
    return Color((rgb >> 16) & 255, (rgb >> 8) & 255, rgb & 255)


# Pen number -> assumed paper thickness (mm). The Revit API cannot read the
# project's line-weight table, so this ladder (a common metric one) is only
# used to PICK a starting style; the user can change every row in the window.
PEN_LADDER = [(1, 0.10), (2, 0.18), (3, 0.25), (4, 0.35), (5, 0.50), (6, 0.70),
              (7, 1.00), (8, 1.40), (9, 2.00), (10, 2.80), (11, 4.00), (12, 5.60),
              (13, 8.00), (14, 11.0), (15, 16.0), (16, 22.0)]


def width_bucket(width_pt):
    """Strokes within 0.05 mm of each other count as one weight."""
    return int(round(width_pt * _MM_PER_PT / 0.05))


def pen_for(width_pt):
    mm = max(0.01, width_pt * _MM_PER_PT)
    return min(PEN_LADDER, key=lambda pm: abs(math.log(mm / pm[1])))[0]


class ProjectLineStyles(object):
    """The project's OWN line styles (Lines subcategories) - nothing is
    created. A PDF stroke width is mapped to one of them either through the
    user's table (weight_map: width bucket -> style label) or, when there is
    no table row, to the solid style whose pen number is closest."""
    def __init__(self, doc, weight_map=None):
        self.doc = doc
        self.weight_map = weight_map or {}
        self.items = []                      # (label, pen, solid, GraphicsStyle)
        self.by_label = {}
        try:
            solid_id = _eid(LinePatternElement.GetSolidPatternId())
        except Exception:
            solid_id = -1
        parent = doc.Settings.Categories.get_Item(BuiltInCategory.OST_Lines)
        for sub in parent.SubCategories:
            try:
                n = sub.Name
                if n.startswith("<") or n.startswith("Dee PDF"):
                    continue
                w = sub.GetLineWeight(GraphicsStyleType.Projection)
                gs = sub.GetGraphicsStyle(GraphicsStyleType.Projection)
                if w is None or gs is None:
                    continue
                pat = sub.GetLinePatternId(GraphicsStyleType.Projection)
                solid = pat is None or _eid(pat) in (-1, solid_id)
                label = "{0} (pen {1})".format(n, int(w))
                self.items.append((label, int(w), solid, gs))
                self.by_label[label] = gs
            except Exception:
                continue
        self.items.sort(key=lambda it: (it[1], it[0]))
        self._cache = {}

    def labels(self):
        return [it[0] for it in self.items]

    def auto_label(self, width_pt):
        if not self.items:
            return None
        pen = pen_for(width_pt)
        pool = [it for it in self.items if it[2]] or self.items
        return min(pool, key=lambda it: (abs(it[1] - pen), it[1]))[0]

    def label_for(self, width_pt):
        b = width_bucket(width_pt)
        if b in self._cache:
            return self._cache[b]
        label = None
        if self.weight_map:
            key = b if b in self.weight_map else min(self.weight_map, key=lambda k: abs(k - b))
            label = self.weight_map[key]
        if label not in self.by_label:
            label = self.auto_label(width_pt)
        self._cache[b] = label
        return label

    def style_for(self, width_pt):
        label = self.label_for(width_pt)
        return self.by_label.get(label)

    def pen_of(self, width_pt):
        label = self.label_for(width_pt)
        for it in self.items:
            if it[0] == label:
                return it[1]
        return 1


class RegionTypes(object):
    """FilledRegionTypes for solid colours and hatch patterns, made once."""
    def __init__(self, doc):
        self.doc = doc
        self.by_name = {}
        self.base = None
        for t in FilteredElementCollector(doc).OfClass(FilledRegionType):
            try:
                self.by_name[t.Name] = t
                self.base = self.base or t
            except Exception:
                pass
        self.solid_pat = None
        for fpe in FilteredElementCollector(doc).OfClass(FillPatternElement):
            try:
                fp = fpe.GetFillPattern()
                if fp.IsSolidFill and fp.Target == FillPatternTarget.Drafting:
                    self.solid_pat = fpe.Id
                    break
            except Exception:
                pass
        self.pattern_ids = {}
        self.cache = {}
        try:
            cat = Category.GetCategory(doc, BuiltInCategory.OST_InvisibleLines)
            self.invisible = cat.GetGraphicsStyle(GraphicsStyleType.Projection).Id
        except Exception:
            self.invisible = None

    @property
    def available(self):
        return self.base is not None and self.solid_pat is not None

    def _make(self, name, pattern_id, rgb, pen=1):
        t = self.by_name.get(name)
        if t is None:
            t = self.base.Duplicate(name)
            self.by_name[name] = t
        t.ForegroundPatternId = pattern_id
        t.ForegroundPatternColor = _color(rgb)
        try:
            t.BackgroundPatternId = ElementId.InvalidElementId
            t.LineWeight = int(pen)
        except Exception:
            pass
        return t.Id

    def solid(self, rgb):
        key = ("s", rgb)
        if key not in self.cache:
            self.cache[key] = self._make("Dee PDF Solid {0:06X}".format(rgb), self.solid_pat, rgb)
        return self.cache[key]

    def pattern(self, angle_pdf, spacing_pt):
        phi = (-angle_pdf) % math.pi                      # PDF y points down
        deg = round(math.degrees(phi) * 2.0) / 2.0 % 180.0
        mm = max(0.2, round(spacing_pt * _MM_PER_PT / 0.05) * 0.05)
        name = "Dee Hatch {0:.1f}deg {1:.2f}mm".format(deg, mm)
        if name not in self.pattern_ids:
            fpe = FillPatternElement.GetFillPatternElementByName(
                self.doc, FillPatternTarget.Drafting, name)
            if fpe is None:
                fp = FillPattern(name, FillPatternTarget.Drafting,
                                 FillPatternHostOrientation.ToView,
                                 math.radians(deg), mm / 304.8)
                fpe = FillPatternElement.Create(self.doc, fp)
            self.pattern_ids[name] = fpe.Id
        return name, self.pattern_ids[name]

    def hatch(self, angle_pdf, spacing_pt, rgb, pen=1):
        pname, pid = self.pattern(angle_pdf, spacing_pt)
        key = (pname, rgb, pen)
        if key not in self.cache:
            self.cache[key] = self._make("{0} {1:06X} p{2}".format(pname, rgb, pen), pid, rgb, pen)
        return self.cache[key]


def _clean_loop(pts, tol):
    out = []
    for p in pts:
        if not out or abs(p.X - out[-1].X) + abs(p.Y - out[-1].Y) > tol:
            out.append(p)
    while len(out) > 1 and abs(out[0].X - out[-1].X) + abs(out[0].Y - out[-1].Y) <= tol:
        out.pop()
    return out


def _area(pts):
    a = 0.0
    for i in range(len(pts)):
        j = (i + 1) % len(pts)
        a += pts[i].X * pts[j].Y - pts[j].X * pts[i].Y
    return abs(a) / 2.0


def auto_scale(doc, page, min_pt):
    """Smallest standard scale at which the shortest kept stroke is still
    longer than Revit's short-curve limit."""
    shortest = None
    for s in page["lines"]:
        ln = math.hypot(s[2] - s[0], s[3] - s[1])
        if ln >= min_pt and (shortest is None or ln < shortest):
            shortest = ln
    if not shortest:
        return 1
    need = doc.Application.ShortCurveTolerance * 1.1 / (shortest * _FT_PER_PT)
    for n in STANDARD_SCALES:
        if n >= need:
            return n
    return STANDARD_SCALES[-1]


# ---------------------------------------------------------------------------
# line-weight scan (for the weight table in the window)
# ---------------------------------------------------------------------------
def scan_weights(doc, path, margins, rot=0, mirror=False):
    """-> ([(bucket, width_mm, stroke_count, auto style label)], error).
    Reads the first page of `path`; labels are the project's own styles."""
    pages, err = extract(path, margins, rot, mirror, False)
    if pages is None:
        return None, err
    counts, widths = {}, {}
    for seg in pages[0]["lines"]:
        b = width_bucket(seg[4])
        counts[b] = counts.get(b, 0) + 1
        widths[b] = seg[4]
    styles = ProjectLineStyles(doc)
    rows = []
    for b in sorted(counts):
        rows.append((b, round(widths[b] * _MM_PER_PT, 2), counts[b], styles.auto_label(widths[b])))
    return rows, ""


def project_style_labels(doc):
    return ProjectLineStyles(doc).labels()


# ---------------------------------------------------------------------------
# build one page
# ---------------------------------------------------------------------------
class PageStats(object):
    def __init__(self):
        self.lines = 0
        self.fills = 0
        self.hatches = 0
        self.too_short = 0
        self.failed_lines = 0
        self.failed_fills = 0

    def text(self):
        s = "{0} lines, {1} hatch regions, {2} fills".format(self.lines, self.hatches, self.fills)
        if self.too_short:
            s += ", {0} too short for Revit".format(self.too_short)
        if self.failed_lines or self.failed_fills:
            s += ", {0} lines / {1} fills failed".format(self.failed_lines, self.failed_fills)
        return s


def _draw_lines(doc, view, segs, P, tol, min_pt, styles, keep_colors, stats, ogs_cache):
    for seg in segs:
        if min_pt and math.hypot(seg[2] - seg[0], seg[3] - seg[1]) < min_pt:
            stats.too_short += 1
            continue
        a, b = P(seg[0], seg[1]), P(seg[2], seg[3])
        if a.DistanceTo(b) <= tol:
            stats.too_short += 1
            continue
        try:
            dc = doc.Create.NewDetailCurve(view, Line.CreateBound(a, b))
            gs = styles.style_for(seg[4])
            if gs is not None:
                dc.LineStyle = gs
            rgb = seg[5]
            if keep_colors and not _is_black(rgb):       # colour = override on the original style
                ogs = ogs_cache.get(rgb)
                if ogs is None:
                    ogs = OverrideGraphicSettings()
                    ogs.SetProjectionLineColor(_color(rgb))
                    ogs_cache[rgb] = ogs
                view.SetElementOverrides(dc.Id, ogs)
            stats.lines += 1
        except Exception:
            stats.failed_lines += 1


def _is_black(rgb):
    return ((rgb >> 16) & 255) < 40 and ((rgb >> 8) & 255) < 40 and (rgb & 255) < 40


def _make_region(doc, view, type_id, pts, tol, invisible):
    loop_pts = _clean_loop(pts, tol)
    if len(loop_pts) < 3 or _area(loop_pts) < tol * tol:
        return None
    loop = CurveLoop()
    for i in range(len(loop_pts)):
        loop.Append(Line.CreateBound(loop_pts[i], loop_pts[(i + 1) % len(loop_pts)]))
    loops = List[CurveLoop]()
    loops.Add(loop)
    fr = FilledRegion.Create(doc, type_id, view.Id, loops)
    if invisible is not None:
        try:
            fr.SetLineStyleId(invisible)
        except Exception:
            pass
    return fr.Id


def build_page(doc, view, page, scale_n, min_mm, keep_colors, do_fills, styles, regions):
    stats = PageStats()
    tol = doc.Application.ShortCurveTolerance * 1.05
    min_pt = (min_mm / _MM_PER_PT) if min_mm > 0 else 0.0
    k = scale_n * _FT_PER_PT                    # PDF points -> feet, enlarged by N
    cx, cy = page["w"] / 2.0, page["h"] / 2.0

    def P(x, y):
        return XYZ((x - cx) * k, (cy - y) * k, 0.0)

    ogs_cache = {}
    _draw_lines(doc, view, page["lines"], P, tol, min_pt, styles, keep_colors, stats, ogs_cache)

    made = List[ElementId]()
    if do_fills and regions.available:
        for h in page.get("hatches", []):
            try:
                tid = regions.solid(h["rgb"]) if h["solid"] else \
                    regions.hatch(h["angle"], h["spacing"], h["rgb"], styles.pen_of(h.get("width", 0.5)))
                rid = _make_region(doc, view, tid, [P(x, y) for x, y in h["poly"]],
                                   tol, regions.invisible)
                if rid is None:
                    raise ValueError("empty")
                made.Add(rid)
                stats.hatches += 1
            except Exception:
                _draw_lines(doc, view, h.get("members", []), P, tol, min_pt, styles,
                            keep_colors, stats, ogs_cache)          # never lose the strokes
        for rgb, pts in page["fills"]:
            try:
                rid = _make_region(doc, view, regions.solid(rgb), [P(x, y) for x, y in pts],
                                   tol, regions.invisible)
                if rid is None:
                    stats.failed_fills += 1
                    continue
                made.Add(rid)
                stats.fills += 1
            except Exception:
                stats.failed_fills += 1
        if made.Count:
            try:
                DetailElementOrderUtils.SendToBack(doc, view, made)
            except Exception:
                pass
    else:
        for h in page.get("hatches", []):
            _draw_lines(doc, view, h.get("members", []), P, tol, min_pt, styles,
                        keep_colors, stats, ogs_cache)
    return stats


# ---------------------------------------------------------------------------
# batch
# ---------------------------------------------------------------------------
def run_batch(doc, rows, margins, all_pages, scale_n, min_mm, keep_colors, do_fills,
              when_exists, rot=0, mirror=False, hatch=True, weight_map=None, progress=None):
    """scale_n = 0 means Auto."""
    result = base.BatchResult()
    start = time.time()
    taken = base.existing_view_names(doc)
    total = len(rows)
    for i, row in enumerate(rows):
        if progress is not None:
            progress.update_progress(i, total)
        pages, err = extract(row.path, margins, rot, mirror, hatch and do_fills)
        if pages is None:
            result.skipped.append((row.file_name, err))
            if "PyMuPDF" in err:
                break
            continue
        if not all_pages:
            pages = pages[:1]
        for n, page in enumerate(pages, 1):
            label = row.file_name if n == 1 else "{0} (p{1})".format(row.file_name, n)
            bname = row.view_name if n == 1 else "{0} - p{1}".format(row.view_name, n)
            name = base.unique_name(bname, taken, when_exists)
            if name is None:
                result.skipped.append((label, "a view named '{0}' already exists".format(bname)))
                continue
            t = Transaction(doc, "Dee - " + label)
            t.Start()
            try:
                opts = t.GetFailureHandlingOptions()
                opts.SetFailuresPreprocessor(_SwallowWarnings())
                t.SetFailureHandlingOptions(opts)
                min_pt = (min_mm / _MM_PER_PT) if min_mm > 0 else 0.0
                n_scale = scale_n if scale_n > 0 else auto_scale(doc, page, min_pt)
                view = base.make_drafting_view(doc, name, n_scale)
                regions = RegionTypes(doc) if do_fills else None
                if do_fills and not regions.available:
                    result.notes.append("{0}: no solid drafting pattern / filled region type in "
                                        "this project - fills skipped".format(name))
                stats = build_page(doc, view, page, n_scale, min_mm, keep_colors,
                                   do_fills, ProjectLineStyles(doc, weight_map), regions or _NoRegions())
                t.Commit()
                taken.add(name)
                result.created.append((name, label))
                result.notes.append("{0} (1:{1}{2}): {3}".format(
                    name, n_scale, " auto" if scale_n <= 0 else "", stats.text()))
            except Exception as ex:
                try:
                    t.RollBack()
                except Exception:
                    pass
                result.skipped.append((label, str(ex)))
    result.seconds = time.time() - start
    return result


class _NoRegions(object):
    available = False
    invisible = None
