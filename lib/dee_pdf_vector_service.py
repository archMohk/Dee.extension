# -*- coding: utf-8 -*-
"""
dee_pdf_vector_service
PDF -> real Revit linework: every stroke becomes a Detail Line and every
solid fill (arrowheads, solid shapes, grey/colour fills) becomes a Filled
Region, in a new Drafting View per PDF page. Hatching in these PDFs is
drawn as many short strokes, so it arrives as detail lines too.

--------------------------------------------------------------------
How it works
--------------------------------------------------------------------
1. lib/dee_pdf_vector_helper.py (normal Python + PyMuPDF, run as a
   subprocess - IronPython has no PDF library) reads the page vectors,
   trims the border / title block, and writes JSON.
2. This module reads that JSON and builds the elements, one
   Transaction per page, so a bad page never rolls back the others.

Revit API facts relied on (checked against RevitAPI.dll 2024/2026)
- Document.Create.NewDetailCurve(view, curve) -> DetailCurve; its
  LineStyle is a GraphicsStyle.
- Line.CreateBound throws for a curve shorter than
  Application.ShortCurveTolerance (about 0.8 mm at REAL size). PDF
  strokes are mostly tiny, so the drawing is enlarged by N (the "scale
  1:N" box) and the view is set to 1:N - it still prints at paper size.
  Segments still too short after that are skipped and counted.
- FilledRegion.Create(doc, filledRegionTypeId, viewId, IList<CurveLoop>)
  needs closed, non-self-intersecting loops; a loop that fails is
  counted, never fatal.
- Overlapping detail lines raise Revit warnings; a failures
  preprocessor deletes warnings so no dialog interrupts a batch.
- Line weight: a PDF stroke is mapped thin/medium/wide to the project's
  own line styles by their pen number (not by name, so it works in any
  language), solid patterns only.
"""
import json
import os
import shutil
import subprocess
import tempfile
import time

from System.Collections.Generic import List
from Autodesk.Revit.DB import (
    Transaction, XYZ, Line, CurveLoop, FilledRegion, FilledRegionType,
    FillPatternElement, FillPatternTarget, Color, OverrideGraphicSettings,
    FilteredElementCollector, BuiltInCategory, GraphicsStyleType, ElementId,
    DetailElementOrderUtils, IFailuresPreprocessor, FailureSeverity,
    FailureProcessingResult,
)

import dee_draft_import_service as base

_MM_PER_FT = 304.8


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
# extraction (subprocess)
# ---------------------------------------------------------------------------
def extract(path, margins):
    """([page dicts], error text). margins = (left, top, right, bottom) pt."""
    py = base.find_pymupdf_python()
    if py is None:
        return None, ("No Python with PyMuPDF was found. Install it once with:  "
                      "py -m pip install pymupdf")
    helper = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dee_pdf_vector_helper.py")
    tmp = tempfile.mkdtemp(prefix="dee_pdfvec_")
    out = os.path.join(tmp, "v.json")
    try:
        cmd = py + ["-I", helper, path, out] + [str(m) for m in margins]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             creationflags=0x08000000)
        _, err = p.communicate()
        if p.returncode != 0 or not os.path.isfile(out):
            return None, (err or "extraction failed").strip()
        with open(out) as fh:
            return json.load(fh)["pages"], ""
    except Exception as ex:
        return None, str(ex)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Revit helpers
# ---------------------------------------------------------------------------
def _eid(e):
    try:
        return e.Value
    except Exception:
        return e.IntegerValue


def line_styles(doc):
    """[(pen_number, GraphicsStyle)] of the solid, visible line styles."""
    out = []
    cat = doc.Settings.Categories.get_Item(BuiltInCategory.OST_Lines)
    for sub in cat.SubCategories:
        try:
            if sub.Name.startswith("<"):
                continue
            pat = sub.GetLinePatternId(GraphicsStyleType.Projection)
            if pat is not None and _eid(pat) != -1:      # patterned (dash etc.)
                continue
            w = sub.GetLineWeight(GraphicsStyleType.Projection)
            gs = sub.GetGraphicsStyle(GraphicsStyleType.Projection)
            if w is None or gs is None:
                continue
            out.append((int(w), gs))
        except Exception:
            continue
    return out


def pick_style(styles, pen):
    if not styles:
        return None
    return min(styles, key=lambda s: abs(s[0] - pen))[1]


def bucket_pen(width_pt):
    if width_pt < 0.5:
        return 1
    if width_pt < 1.0:
        return 3
    return 5


def solid_region_type(doc):
    """(FilledRegionType id, solid FillPatternElement id) - made if missing."""
    solid_pat = None
    for fpe in FilteredElementCollector(doc).OfClass(FillPatternElement):
        try:
            fp = fpe.GetFillPattern()
            if fp.IsSolidFill and fp.Target == FillPatternTarget.Drafting:
                solid_pat = fpe.Id
                break
        except Exception:
            continue
    if solid_pat is None:
        return None, None
    first = None
    for t in FilteredElementCollector(doc).OfClass(FilledRegionType):
        first = first or t
        try:
            if _eid(t.ForegroundPatternId) == _eid(solid_pat):
                return t.Id, solid_pat
        except Exception:
            continue
    if first is None:
        return None, solid_pat
    new = first.Duplicate("Dee PDF Solid")
    new.ForegroundPatternId = solid_pat
    return new.Id, solid_pat


def _is_black(rgb):
    return ((rgb >> 16) & 255) < 40 and ((rgb >> 8) & 255) < 40 and (rgb & 255) < 40


def _color(rgb):
    return Color((rgb >> 16) & 255, (rgb >> 8) & 255, rgb & 255)


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


# ---------------------------------------------------------------------------
# build one page
# ---------------------------------------------------------------------------
class PageStats(object):
    def __init__(self):
        self.lines = 0
        self.fills = 0
        self.too_short = 0
        self.failed_lines = 0
        self.failed_fills = 0

    def text(self):
        s = "{0} lines, {1} fills".format(self.lines, self.fills)
        if self.too_short:
            s += ", {0} too short for Revit".format(self.too_short)
        if self.failed_lines or self.failed_fills:
            s += ", {0} lines / {1} fills failed".format(self.failed_lines, self.failed_fills)
        return s


def build_page(doc, view, page, scale_n, min_mm, keep_colors, do_fills, styles, region_ids):
    stats = PageStats()
    tol = max(doc.Application.ShortCurveTolerance * 1.05, min_mm / _MM_PER_FT)
    k = scale_n / 72.0 / 12.0                    # PDF points -> feet, enlarged by N
    cx, cy = page["w"] / 2.0, page["h"] / 2.0

    def P(x, y):
        return XYZ((x - cx) * k, (cy - y) * k, 0.0)

    style_for = {}
    for seg in page["lines"]:
        a, b = P(seg[0], seg[1]), P(seg[2], seg[3])
        if a.DistanceTo(b) <= tol:
            stats.too_short += 1
            continue
        try:
            dc = doc.Create.NewDetailCurve(view, Line.CreateBound(a, b))
            pen = bucket_pen(seg[4])
            if pen not in style_for:
                style_for[pen] = pick_style(styles, pen)
            if style_for[pen] is not None:
                dc.LineStyle = style_for[pen]
            if keep_colors and not _is_black(seg[5]):
                ogs = OverrideGraphicSettings()
                ogs.SetProjectionLineColor(_color(seg[5]))
                view.SetElementOverrides(dc.Id, ogs)
            stats.lines += 1
        except Exception:
            stats.failed_lines += 1

    if do_fills and region_ids[0] is not None:
        made = List[ElementId]()
        for rgb, pts in page["fills"]:
            loop_pts = _clean_loop([P(x, y) for x, y in pts], tol)
            if len(loop_pts) < 3 or _area(loop_pts) < tol * tol:
                stats.failed_fills += 1
                continue
            try:
                loop = CurveLoop()
                for i in range(len(loop_pts)):
                    loop.Append(Line.CreateBound(loop_pts[i], loop_pts[(i + 1) % len(loop_pts)]))
                loops = List[CurveLoop]()
                loops.Add(loop)
                fr = FilledRegion.Create(doc, region_ids[0], view.Id, loops)
                if not _is_black(rgb):
                    ogs = OverrideGraphicSettings()
                    ogs.SetSurfaceForegroundPatternColor(_color(rgb))
                    ogs.SetSurfaceForegroundPatternId(region_ids[1])
                    view.SetElementOverrides(fr.Id, ogs)
                made.Add(fr.Id)
                stats.fills += 1
            except Exception:
                stats.failed_fills += 1
        if made.Count:
            try:
                DetailElementOrderUtils.SendToBack(doc, view, made)
            except Exception:
                pass
    return stats


# ---------------------------------------------------------------------------
# batch
# ---------------------------------------------------------------------------
def run_batch(doc, rows, margins, all_pages, scale_n, min_mm, keep_colors, do_fills,
              when_exists, progress=None):
    result = base.BatchResult()
    start = time.time()
    taken = base.existing_view_names(doc)
    total = len(rows)
    for i, row in enumerate(rows):
        if progress is not None:
            progress.update_progress(i, total)
        pages, err = extract(row.path, margins)
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
                view = base.make_drafting_view(doc, name, scale_n)
                region_ids = solid_region_type(doc) if do_fills else (None, None)
                stats = build_page(doc, view, page, scale_n, min_mm, keep_colors,
                                   do_fills, line_styles(doc), region_ids)
                t.Commit()
                taken.add(name)
                result.created.append((name, label))
                result.notes.append("{0}: {1}".format(name, stats.text()))
            except Exception as ex:
                try:
                    t.RollBack()
                except Exception:
                    pass
                result.skipped.append((label, str(ex)))
    result.seconds = time.time() - start
    return result
