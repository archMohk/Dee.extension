# -*- coding: utf-8 -*-
"""Runs under a normal Python (needs PyMuPDF), NOT inside Revit.

    py -I dee_pdf_vector_helper.py <in.pdf> <out.json> <left> <top> <right> <bottom> [rot] [mirror] [hatch]
    py -I dee_pdf_vector_helper.py --preview <in.pdf> <out.png> <page> <left> <top> <right> <bottom> <rot> <mirror> <maxpx>

Extract mode reads the VECTOR content of every page (margins in PDF points
are trimmed first - that is how the sheet border and title block are left
out), turns it by `rot` (0/90/180/270 degrees clockwise, after an optional
left-right `mirror`) and writes JSON for lib/dee_pdf_vector_service.py:

    {"pages": [{"w": width_pt, "h": height_pt,
                "lines":   [[x1, y1, x2, y2, stroke_width_pt, 0xRRGGBB], ...],
                "fills":   [[0xRRGGBB, [[x, y], ...]], ...],
                "hatches": [{"poly": [[x, y], ...], "angle": rad, "spacing": pt,
                             "rgb": 0xRRGGBB, "solid": bool}, ...]}]}

Coordinates are points from the top-left of the trimmed, turned area (y down).
- Curves are flattened to short straight segments.
- Whole paths that cross the trimmed edge are dropped, never cut.
- White fills are dropped: in a drawing they only hide what lies behind.
- Duplicate segments and collinear runs inside one path are merged.
- Hatching: runs of same-direction strokes sitting close together (any
  colour or weight) are taken out of "lines" and returned as ONE hatch
  region, outlined by the outermost strokes. Spacing finer than ~0.9 pt
  becomes a solid fill of the averaged colour.

Preview mode renders the trimmed, turned page to a PNG.
"""
import json
import math
import sys

EPS = 0.1           # shortest segment kept, in points
CURVE_STEPS = 12
HATCH_MIN_LINES = 6
HATCH_MAX_GAP = 10.0      # widest stroke spacing still called a hatch (pt)
HATCH_ANGLE_TOL = math.radians(1.5)
DENSE_SPACING = 0.9       # tighter than this -> solid fill


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


# ---------------------------------------------------------------------------
# turning
# ---------------------------------------------------------------------------
def _lin(rot, mirror, x, y):
    if mirror:
        x = -x
    if rot == 90:
        return -y, x
    if rot == 180:
        return -x, -y
    if rot == 270:
        return y, -x
    return x, y


def _turner(w, h, rot, mirror):
    """(function (x, y) -> (x', y'), new_w, new_h) keeping the box at 0,0."""
    cs = [_lin(rot, mirror, x, y) for x, y in ((0, 0), (w, 0), (0, h), (w, h))]
    ox = min(c[0] for c in cs)
    oy = min(c[1] for c in cs)
    nw = max(c[0] for c in cs) - ox
    nh = max(c[1] for c in cs) - oy

    def T(x, y):
        a, b = _lin(rot, mirror, x, y)
        return a - ox, b - oy
    return T, nw, nh


# ---------------------------------------------------------------------------
# hatch detection
# ---------------------------------------------------------------------------
def _angle_groups(lines):
    items = []
    for i, l in enumerate(lines):
        dx, dy = l[2] - l[0], l[3] - l[1]
        L = math.hypot(dx, dy)
        if L < 0.3:
            continue
        items.append([math.atan2(dy, dx) % math.pi, L, i])
    if not items:
        return []
    items.sort()
    groups, cur = [], [items[0]]
    for it in items[1:]:
        if it[0] - cur[0][0] <= HATCH_ANGLE_TOL:
            cur.append(it)
        else:
            groups.append(cur)
            cur = [it]
    groups.append(cur)
    if len(groups) > 1 and groups[0][0][0] + math.pi - groups[-1][0][0] <= HATCH_ANGLE_TOL:
        last = groups.pop()
        for it in last:
            it[0] -= math.pi
        groups[0] = last + groups[0]
    return groups


def _find(parent, i):
    while parent[i] != i:
        parent[i] = parent[parent[i]]
        i = parent[i]
    return i


def detect_hatches(lines):
    """-> (remaining_lines, hatches)"""
    used = set()
    hatches = []
    for grp in _angle_groups(lines):
        if len(grp) < HATCH_MIN_LINES:
            continue
        tot = sum(g[1] for g in grp)
        th = sum(g[0] * g[1] for g in grp) / tot
        dx, dy = math.cos(th), math.sin(th)
        nx, ny = -dy, dx
        ch = []
        for _, L, i in grp:
            l = lines[i]
            t1 = l[0] * dx + l[1] * dy
            t2 = l[2] * dx + l[3] * dy
            o = ((l[0] + l[2]) / 2.0) * nx + ((l[1] + l[3]) / 2.0) * ny
            ch.append((o, min(t1, t2), max(t1, t2), i))
        ch.sort()
        n = len(ch)
        parent = list(range(n))
        for a in range(n):
            b = a + 1
            while b < n and ch[b][0] - ch[a][0] <= HATCH_MAX_GAP:
                same = ch[b][0] - ch[a][0] < 0.05
                gap = max(ch[a][1], ch[b][1]) - min(ch[a][2], ch[b][2])
                if (gap <= HATCH_MAX_GAP) if same else (gap <= 0):
                    parent[_find(parent, b)] = _find(parent, a)
                b += 1
        comps = {}
        for k in range(n):
            comps.setdefault(_find(parent, k), []).append(ch[k])
        for comp in comps.values():
            if len(comp) < HATCH_MIN_LINES:
                continue
            comp.sort()
            bins = []                       # [o_sum, count, tmin, tmax]
            for o, t1, t2, _ in comp:
                if bins and o - bins[-1][0] / bins[-1][1] <= 0.12:
                    b = bins[-1]
                    b[0] += o
                    b[1] += 1
                    b[2] = min(b[2], t1)
                    b[3] = max(b[3], t2)
                else:
                    bins.append([o, 1, t1, t2])
            if len(bins) < 5:
                continue
            centres = [b[0] / b[1] for b in bins]
            diffs = [centres[k + 1] - centres[k] for k in range(len(centres) - 1)]
            s = sorted(diffs)[len(diffs) // 2]
            if s <= 0 or s > HATCH_MAX_GAP:
                continue
            ok = sum(1 for d in diffs if 0.5 * s <= d <= 1.6 * s)
            if ok < 0.55 * len(diffs):
                continue
            poly_a = [(dx * bins[k][2] + nx * centres[k], dy * bins[k][2] + ny * centres[k])
                      for k in range(len(bins))]
            poly_b = [(dx * bins[k][3] + nx * centres[k], dy * bins[k][3] + ny * centres[k])
                      for k in range(len(bins) - 1, -1, -1)]
            poly = poly_a + poly_b
            weight, colours, wsum = {}, {}, 0.0
            for _, t1, t2, i in comp:
                used.add(i)
                l = lines[i]
                ln = t2 - t1
                colours[l[5]] = colours.get(l[5], 0.0) + ln
                wsum += l[4] * ln
                weight[i] = ln
            lensum = sum(weight.values()) or 1.0
            col = max(colours, key=lambda c: colours[c])
            solid = s < DENSE_SPACING
            if solid:
                cov = min(1.0, (wsum / lensum) / s)
                r, g, b = (col >> 16) & 255, (col >> 8) & 255, col & 255
                col = (int(255 - (255 - r) * cov) << 16) | (int(255 - (255 - g) * cov) << 8) | int(255 - (255 - b) * cov)
            hatches.append({"poly": [[round(x, 3), round(y, 3)] for x, y in poly],
                            "angle": round(th, 5), "spacing": round(s, 3),
                            "rgb": col, "solid": solid,
                            "width": round(wsum / lensum, 2),
                            "members": [lines[c[3]] for c in comp]})
    rest = [l for i, l in enumerate(lines) if i not in used]
    return rest, hatches


# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------
def extract(path, left, top, right, bottom, rot=0, mirror=False, hatch=True):
    import pymupdf
    doc = pymupdf.open(path)
    pages = []
    for page in doc:
        R = page.rect
        clip = pymupdf.Rect(R.x0 + left, R.y0 + top, R.x1 - right, R.y1 - bottom)
        if clip.is_empty or clip.width < 10 or clip.height < 10:
            raise ValueError("margins leave nothing of page {0}".format(page.number + 1))
        T, nw, nh = _turner(clip.width, clip.height, rot, mirror)
        tol = 0.5
        lines, fills, seen = [], [], set()
        ox, oy = clip.x0, clip.y0
        for d in page.get_drawings():
            r = d["rect"]
            if (r.x0 < clip.x0 - tol or r.y0 < clip.y0 - tol or
                    r.x1 > clip.x1 + tol or r.y1 > clip.y1 + tol):
                continue
            chains = _subpaths(d["items"])
            stroke, fill = d.get("color"), d.get("fill")
            width = d.get("width") or 0.0

            if stroke is not None:
                col = _rgb(stroke)
                for pts, closed in chains:
                    if (closed or d.get("closePath")) and len(pts) > 2:
                        pts = pts + [pts[0]]
                    pts = _merge_collinear(pts)
                    for a, b in zip(pts, pts[1:]):
                        if math.hypot(b[0] - a[0], b[1] - a[1]) < EPS:
                            continue
                        p, q = T(a[0] - ox, a[1] - oy), T(b[0] - ox, b[1] - oy)
                        seg = (round(p[0], 3), round(p[1], 3), round(q[0], 3), round(q[1], 3))
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
                            fills.append([col, [[round(v, 3) for v in T(x - ox, y - oy)] for x, y in pts]])
        hatches = []
        if hatch:
            lines, hatches = detect_hatches(lines)
        pages.append({"w": nw, "h": nh, "lines": lines, "fills": fills, "hatches": hatches})
    return {"pages": pages}


def preview(src, dst, page_no, left, top, right, bottom, rot, mirror, maxpx):
    import pymupdf
    doc = pymupdf.open(src)
    page = doc[max(0, min(len(doc) - 1, page_no))]
    R = page.rect
    clip = pymupdf.Rect(R.x0 + left, R.y0 + top, R.x1 - right, R.y1 - bottom)
    if clip.is_empty:
        clip = R
    a, b = _lin(rot, mirror, 1, 0)
    c, d = _lin(rot, mirror, 0, 1)
    z = maxpx / max(clip.width, clip.height)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(a * z, b * z, c * z, d * z, 0, 0),
                          clip=clip, alpha=False)
    pix.save(dst)


def main(argv):
    if argv[1] == "--preview":
        src, dst = argv[2], argv[3]
        pg = int(argv[4])
        l, t, r, b = [float(v) for v in argv[5:9]]
        preview(src, dst, pg, l, t, r, b, int(argv[9]), argv[10] == "1", float(argv[11]))
        return 0
    src, dst = argv[1], argv[2]
    left, top, right, bottom = [float(v) for v in argv[3:7]]
    rot = int(argv[7]) if len(argv) > 7 else 0
    mirror = len(argv) > 8 and argv[8] == "1"
    hatch = not (len(argv) > 9 and argv[9] == "0")
    data = extract(src, left, top, right, bottom, rot, mirror, hatch)
    with open(dst, "w") as fh:
        json.dump(data, fh, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception as ex:
        sys.stderr.write(str(ex))
        sys.exit(1)
