# -*- coding: utf-8 -*-
"""Runs under a normal Python (needs PyMuPDF), NOT inside Revit.

    py -I dee_pdf_vector_helper.py <in.pdf> <out.json> <left> <top> <right> <bottom>

Reads the VECTOR content of every page of <in.pdf> (the margins, in PDF
points, are trimmed off first - that is how the sheet border and title
block are left out) and writes JSON that lib/dee_pdf_vector_service.py
turns into Revit detail lines and filled regions:

    {"pages": [{"w": width_pt, "h": height_pt,
                "lines": [[x1, y1, x2, y2, stroke_width_pt, 0xRRGGBB], ...],
                "fills": [[0xRRGGBB, [[x, y], ...]], ...]}]}

Coordinates are points from the top-left of the trimmed area (y down).
- Curves are flattened to short straight segments.
- Whole paths that cross the trimmed edge are dropped, never cut.
- White fills are dropped: in a drawing they only hide what lies behind.
- Duplicate segments and collinear runs inside one path are merged.
"""
import json
import math
import sys

EPS = 0.02          # shortest segment kept, in points
CURVE_STEPS = 12


def _bezier(p1, p2, p3, p4, n=CURVE_STEPS):
    pts = []
    for i in range(1, n + 1):
        t = i / float(n)
        u = 1 - t
        x = u**3 * p1[0] + 3 * u * u * t * p2[0] + 3 * u * t * t * p3[0] + t**3 * p4[0]
        y = u**3 * p1[1] + 3 * u * u * t * p2[1] + 3 * u * t * t * p3[1] + t**3 * p4[1]
        pts.append((x, y))
    return pts


def _rgb(c):
    return (int(round(c[0] * 255)) << 16) | (int(round(c[1] * 255)) << 8) | int(round(c[2] * 255))


def _subpaths(items):
    """Chains of points: consecutive items that join end-to-start share a chain."""
    chains, cur = [], None
    for it in items:
        kind = it[0]
        if kind == "re":
            r = it[1]
            chains.append(([(r.x0, r.y0), (r.x1, r.y0), (r.x1, r.y1), (r.x0, r.y1)], True))
            cur = None
            continue
        if kind == "qu":
            q = it[1]
            chains.append(([(q.ul.x, q.ul.y), (q.ur.x, q.ur.y), (q.lr.x, q.lr.y), (q.ll.x, q.ll.y)], True))
            cur = None
            continue
        if kind == "l":
            a, pts = (it[1].x, it[1].y), [(it[2].x, it[2].y)]
        elif kind == "c":
            a = (it[1].x, it[1].y)
            pts = _bezier(a, (it[2].x, it[2].y), (it[3].x, it[3].y), (it[4].x, it[4].y))
        else:
            continue
        if cur is not None and math.hypot(cur[-1][0] - a[0], cur[-1][1] - a[1]) < 0.01:
            cur.extend(pts)
        else:
            cur = [a] + pts
            chains.append((cur, False))
    return chains


def _merge_collinear(pts):
    out = [pts[0]]
    for p in pts[1:]:
        if len(out) >= 2:
            a, b = out[-2], out[-1]
            cross = (b[0] - a[0]) * (p[1] - b[1]) - (b[1] - a[1]) * (p[0] - b[0])
            dot = (b[0] - a[0]) * (p[0] - b[0]) + (b[1] - a[1]) * (p[1] - b[1])
            if abs(cross) < 1e-6 and dot > 0:
                out[-1] = p
                continue
        out.append(p)
    return out


def extract(path, left, top, right, bottom):
    import pymupdf
    doc = pymupdf.open(path)
    pages = []
    for page in doc:
        R = page.rect
        clip = pymupdf.Rect(R.x0 + left, R.y0 + top, R.x1 - right, R.y1 - bottom)
        if clip.is_empty or clip.width < 10 or clip.height < 10:
            raise ValueError("margins leave nothing of page {0}".format(page.number + 1))
        tol = 0.5
        lines, fills, seen = [], [], set()
        for d in page.get_drawings():
            r = d["rect"]
            if (r.x0 < clip.x0 - tol or r.y0 < clip.y0 - tol or
                    r.x1 > clip.x1 + tol or r.y1 > clip.y1 + tol):
                continue
            chains = _subpaths(d["items"])
            stroke, fill = d.get("color"), d.get("fill")
            width = d.get("width") or 0.0
            ox, oy = clip.x0, clip.y0

            if stroke is not None:
                col = _rgb(stroke)
                for pts, closed in chains:
                    if (closed or d.get("closePath")) and len(pts) > 2:
                        pts = pts + [pts[0]]
                    pts = _merge_collinear(pts)
                    for a, b in zip(pts, pts[1:]):
                        if math.hypot(b[0] - a[0], b[1] - a[1]) < EPS:
                            continue
                        seg = (round(a[0] - ox, 3), round(a[1] - oy, 3),
                               round(b[0] - ox, 3), round(b[1] - oy, 3))
                        key = seg if seg[:2] <= seg[2:] else (seg[2], seg[3], seg[0], seg[1])
                        key = key + (round(width, 2), col)
                        if key in seen:
                            continue
                        seen.add(key)
                        lines.append(list(seg) + [round(width, 2), col])

            if fill is not None and (d.get("fill_opacity") is None or d["fill_opacity"] > 0):
                if min(fill) < 0.98:           # white = mask, skip
                    col = _rgb(fill)
                    for pts, closed in chains:
                        if len(pts) >= 3:
                            fills.append([col, [[round(x - ox, 3), round(y - oy, 3)] for x, y in pts]])
        pages.append({"w": clip.width, "h": clip.height, "lines": lines, "fills": fills})
    return {"pages": pages}


def main(argv):
    src, dst = argv[1], argv[2]
    left, top, right, bottom = [float(v) for v in argv[3:7]]
    data = extract(src, left, top, right, bottom)
    with open(dst, "w") as fh:
        json.dump(data, fh, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception as ex:
        sys.stderr.write(str(ex))
        sys.exit(1)
