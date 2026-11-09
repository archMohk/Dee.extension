# -*- coding: utf-8 -*-
"""
facade_patterns
Pure 2D geometry for DeeFacade - no Revit, no .NET, so it runs (and is
tested) in plain CPython as well as in pyRevit's IronPython 2.7.

Everything is in the wall's own elevation coordinates: x runs along the
wall, y is height. Units are whatever the caller passes (DeeFacade
passes feet, Revit's internal unit) - nothing here assumes mm or ft.

build(opts, outer, obstacles, tol) is the one entry point:
  outer      the wall's outer profile as a polygon [(x, y), ...]
  obstacles  rectangles (x0, y0, x1, y1) the openings must keep clear of
             (doors, windows, openings already in the profile)
  tol        the shortest edge Revit will accept (ShortCurveTolerance)
It returns (holes, stats). A hole is either
  {"kind": "poly",   "pts": [(x, y), ...], "round": 0..1}
  {"kind": "circle", "c": (x, y), "r": radius}
and hole_segments() turns one into the lines / arcs Revit draws.

Every hole is guaranteed to be a closed, simple loop that stays inside
the outer profile and never touches another hole - the two things a
wall profile sketch or a family extrusion refuses.
"""
from __future__ import division

import math
import random

PATTERNS = ("voronoi", "hexagon", "diamond", "grid", "circles")
DIRECTIONS = ("top", "bottom", "left", "right", "edges", "centre")


# --------------------------------------------------------------------------
# polygon basics
# --------------------------------------------------------------------------
def area(poly):
    """Signed area (CCW positive)."""
    s = 0.0
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return s / 2.0


def perimeter(poly):
    n = len(poly)
    return sum(_dist(poly[i], poly[(i + 1) % n]) for i in range(n))


def centroid(poly):
    a = area(poly)
    if abs(a) < 1e-12:
        n = float(len(poly))
        return (sum(p[0] for p in poly) / n, sum(p[1] for p in poly) / n)
    cx = cy = 0.0
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        f = x1 * y2 - x2 * y1
        cx += (x1 + x2) * f
        cy += (y1 + y2) * f
    return (cx / (6.0 * a), cy / (6.0 * a))


def bbox(poly):
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return (min(xs), min(ys), max(xs), max(ys))


def ccw(poly):
    return poly if area(poly) >= 0 else list(reversed(poly))


def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def clip_halfplane(poly, a, b, c):
    """Keeps the part of `poly` where a*x + b*y <= c (Sutherland-Hodgman,
    one edge). Works for any polygon; convex in -> convex out."""
    out = []
    n = len(poly)
    if not n:
        return out
    for i in range(n):
        p = poly[i]
        q = poly[(i + 1) % n]
        fp = a * p[0] + b * p[1] - c
        fq = a * q[0] + b * q[1] - c
        if fp <= 0:
            out.append(p)
        if (fp < 0 < fq) or (fq < 0 < fp):
            t = fp / (fp - fq)
            out.append((p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t))
    return out


def clip_rect(poly, rect):
    x0, y0, x1, y1 = rect
    for a, b, c in ((-1, 0, -x0), (1, 0, x1), (0, -1, -y0), (0, 1, y1)):
        poly = clip_halfplane(poly, a, b, c)
        if len(poly) < 3:
            return []
    return poly


def inset_convex(poly, d):
    """Shrinks a convex polygon by `d` (every edge moves inward by d)."""
    if d <= 0:
        return list(poly)
    poly = ccw(poly)
    edges = []
    n = len(poly)
    for i in range(n):
        p = poly[i]
        q = poly[(i + 1) % n]
        dx, dy = q[0] - p[0], q[1] - p[1]
        ln = math.hypot(dx, dy)
        if ln < 1e-12:
            continue
        # outward normal of a CCW edge is (dy, -dx)
        nx, ny = dy / ln, -dx / ln
        edges.append((nx, ny, nx * p[0] + ny * p[1] - d))
    out = list(poly)
    for a, b, c in edges:
        out = clip_halfplane(out, a, b, c)
        if len(out) < 3:
            return []
    return out


def point_in_poly(pt, poly):
    x, y = pt
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y):
            xc = xi + (y - yi) * (xj - xi) / (yj - yi)
            if x < xc:
                inside = not inside
        j = i
    return inside


def dist_to_segment(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    L2 = dx * dx + dy * dy
    if L2 < 1e-18:
        return _dist(p, a)
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


def dist_to_outline(p, poly):
    n = len(poly)
    return min(dist_to_segment(p, poly[i], poly[(i + 1) % n]) for i in range(n))


def clean(poly, min_len):
    """Merges vertices closer than min_len and drops (near-)collinear
    ones, so no edge is shorter than Revit accepts."""
    pts = list(poly)
    changed = True
    while changed and len(pts) >= 3:
        changed = False
        n = len(pts)
        for i in range(n):
            p = pts[i]
            q = pts[(i + 1) % n]
            if _dist(p, q) < min_len:
                m = ((p[0] + q[0]) / 2.0, (p[1] + q[1]) / 2.0)
                if i + 1 < n:
                    pts[i:i + 2] = [m]
                else:
                    pts = [m] + pts[1:-1]
                changed = True
                break
    if len(pts) < 3:
        return []
    out = []
    n = len(pts)
    for i in range(n):
        a = pts[i - 1]
        b = pts[i]
        c = pts[(i + 1) % n]
        cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
        if abs(cross) > 1e-9 * max(1.0, _dist(a, b) * _dist(b, c)):
            out.append(b)
    return out if len(out) >= 3 else []


# --------------------------------------------------------------------------
# direction (used by density gradient and fade)
# --------------------------------------------------------------------------
def direction_fn(direction, rect):
    """t(x, y) in 0..1, 1 toward the chosen side."""
    x0, y0, x1, y1 = rect
    W = max(x1 - x0, 1e-9)
    H = max(y1 - y0, 1e-9)
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0

    def edge(x, y):
        return min(1.0, max(abs(x - cx) / (W / 2.0), abs(y - cy) / (H / 2.0)))

    fns = {
        "top": lambda x, y: (y - y0) / H,
        "bottom": lambda x, y: 1.0 - (y - y0) / H,
        "right": lambda x, y: (x - x0) / W,
        "left": lambda x, y: 1.0 - (x - x0) / W,
        "edges": edge,
        "centre": lambda x, y: 1.0 - edge(x, y),
    }
    f = fns.get(direction, fns["top"])
    return lambda x, y: max(0.0, min(1.0, f(x, y)))


# --------------------------------------------------------------------------
# cells per pattern (raw tiles, before mullion inset)
# --------------------------------------------------------------------------
def voronoi_cells(seeds, rect):
    """Voronoi cell of every seed, clipped to rect - half-plane clipping,
    nearest neighbours first, stopping once no farther seed can cut."""
    rect_poly = [(rect[0], rect[1]), (rect[2], rect[1]), (rect[2], rect[3]), (rect[0], rect[3])]
    cells = []
    for i, s in enumerate(seeds):
        others = sorted(((s[0] - o[0]) ** 2 + (s[1] - o[1]) ** 2, j)
                        for j, o in enumerate(seeds) if j != i)
        cell = list(rect_poly)
        reach2 = max((p[0] - s[0]) ** 2 + (p[1] - s[1]) ** 2 for p in cell)
        for d2, j in others:
            if d2 > 4.0 * reach2:
                break
            if d2 < 1e-18:
                continue
            o = seeds[j]
            a = o[0] - s[0]
            b = o[1] - s[1]
            c = (o[0] ** 2 + o[1] ** 2 - s[0] ** 2 - s[1] ** 2) / 2.0
            cell = clip_halfplane(cell, a, b, c)
            if len(cell) < 3:
                break
            reach2 = max((p[0] - s[0]) ** 2 + (p[1] - s[1]) ** 2 for p in cell)
        cells.append(cell if len(cell) >= 3 else [])
    return cells


def voronoi_seeds(n, rect, rng, density, tfn):
    x0, y0, x1, y1 = rect
    seeds = []
    wmax = 1.0 + 7.0 * density
    tries = 0
    while len(seeds) < n and tries < n * 200:
        tries += 1
        x = x0 + rng.random() * (x1 - x0)
        y = y0 + rng.random() * (y1 - y0)
        if density > 0:
            w = 1.0 + 7.0 * density * tfn(x, y)
            if rng.random() * wmax > w:
                continue
        seeds.append((x, y))
    return seeds


def voronoi_tiles(o, rect, tfn):
    rng = random.Random(int(o.get("seed", 1)))
    n = max(2, int(o.get("vor_count", 60)))
    seeds = voronoi_seeds(n, rect, rng, max(0.0, o.get("vor_density", 0.0)), tfn)
    cells = voronoi_cells(seeds, rect)
    for _ in range(int(o.get("vor_relax", 0))):
        seeds = [centroid(c) if c else s for c, s in zip(cells, seeds)]
        cells = voronoi_cells(seeds, rect)
    return [c for c in cells if c]


def _grid_range(lo, hi, step, pad=2):
    i0 = int(math.floor(lo / step)) - pad
    i1 = int(math.ceil(hi / step)) + pad
    return range(i0, i1 + 1)


def hexagon_tiles(o, rect):
    w = max(o.get("hex_size", 1.0), 1e-6)   # across flats
    pointy = o.get("hex_pointy", True)
    R = w / math.sqrt(3.0)                  # circumradius
    x0, y0, x1, y1 = rect
    tiles = []
    if pointy:
        dx, dy = w, 1.5 * R
        angles = [math.radians(90 + 60 * k) for k in range(6)]
        for j in _grid_range(y0, y1, dy):
            off = (w / 2.0) if (j % 2) else 0.0
            for i in _grid_range(x0, x1, dx):
                cx, cy = i * dx + off, j * dy
                tiles.append([(cx + R * math.cos(a), cy + R * math.sin(a)) for a in angles])
    else:
        dx, dy = 1.5 * R, w
        angles = [math.radians(60 * k) for k in range(6)]
        for i in _grid_range(x0, x1, dx):
            off = (w / 2.0) if (i % 2) else 0.0
            for j in _grid_range(y0, y1, dy):
                cx, cy = i * dx, j * dy + off
                tiles.append([(cx + R * math.cos(a), cy + R * math.sin(a)) for a in angles])
    return tiles


def diamond_tiles(o, rect):
    W = max(o.get("dia_w", 1.0), 1e-6)
    H = max(o.get("dia_h", 1.0), 1e-6)
    x0, y0, x1, y1 = rect
    tiles = []
    for j in _grid_range(y0, y1, H):
        for i in _grid_range(x0, x1, W):
            for cx, cy in ((i * W, j * H), (i * W + W / 2.0, j * H + H / 2.0)):
                tiles.append([(cx - W / 2.0, cy), (cx, cy - H / 2.0),
                              (cx + W / 2.0, cy), (cx, cy + H / 2.0)])
    return tiles


def grid_tiles(o, rect):
    W = max(o.get("grid_w", 1.0), 1e-6)
    H = max(o.get("grid_h", 1.0), 1e-6)
    stagger = o.get("grid_stagger", False)
    x0, y0, x1, y1 = rect
    tiles = []
    # anchored on the rect's corner so an un-staggered grid starts flush
    for j in range(0, int(math.ceil((y1 - y0) / H)) + 1):
        off = (W / 2.0) if (stagger and j % 2) else 0.0
        for i in range(-1, int(math.ceil((x1 - x0) / W)) + 2):
            ax = x0 + i * W - off
            ay = y0 + j * H
            tiles.append([(ax, ay), (ax + W, ay), (ax + W, ay + H), (ax, ay + H)])
    return tiles


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------
def _hole_bbox(h):
    if h["kind"] == "circle":
        (cx, cy), r = h["c"], h["r"]
        return (cx - r, cy - r, cx + r, cy + r)
    return bbox(h["pts"])


def _boxes_hit(a, b, gap):
    return not (a[2] + gap <= b[0] or b[2] + gap <= a[0] or
                a[3] + gap <= b[1] or b[3] + gap <= a[1])


def _sample_pts(h):
    if h["kind"] == "circle":
        (cx, cy), r = h["c"], h["r"]
        return [(cx + r * math.cos(k * math.pi / 8), cy + r * math.sin(k * math.pi / 8))
                for k in range(16)]
    return h["pts"]


def hole_area(h):
    if h["kind"] == "circle":
        return math.pi * h["r"] ** 2
    a = abs(area(h["pts"]))
    # each rounded corner removes a bit; a close-enough estimate for stats
    return a * (1.0 - 0.2146 * h.get("round", 0.0) * 0.5)


def build(o, outer, obstacles, tol):
    """See the module docstring. `o` holds lengths in the caller's unit."""
    outer = ccw(clean(outer, tol) or outer)
    ob = bbox(outer)
    mullion = max(o.get("mullion", 0.3), 4 * tol)
    border = max(o.get("border", 0.0), mullion / 2.0, 4 * tol)
    rect = (ob[0] + border, ob[1] + border, ob[2] - border, ob[3] - border)
    stats = {"holes": 0, "open_pct": 0.0, "dropped": 0}
    if rect[2] - rect[0] <= 4 * tol or rect[3] - rect[1] <= 4 * tol:
        return [], stats

    tfn = direction_fn(o.get("direction", "top"), rect)
    fade = max(0.0, min(1.0, o.get("fade", 0.0)))
    min_open = max(o.get("min_open", 0.0), 6 * tol)
    rounding = max(0.0, min(1.0, o.get("round", 0.0)))
    keep_partial = o.get("keep_partial", True)
    pattern = o.get("pattern", "voronoi")
    is_rect_outer = len(outer) == 4 and abs(abs(area(outer)) -
                                            (ob[2] - ob[0]) * (ob[3] - ob[1])) < 1e-6 * abs(area(outer))
    holes = []

    if pattern == "circles":
        S = max(o.get("circ_spacing", 1.0), 1e-6)
        stagger = o.get("circ_stagger", True)
        dy = S * math.sqrt(3.0) / 2.0 if stagger else S
        d0 = min(S * max(0.05, min(0.98, o.get("circ_dia", 0.7))), S - mullion)
        jitter = max(0.0, min(1.0, o.get("circ_jitter", 0.0)))
        rng = random.Random(int(o.get("seed", 1)))
        W = rect[2] - rect[0]
        H = rect[3] - rect[1]
        rows = int(math.floor((H - d0) / dy)) + 1 if H >= d0 else 0
        cols = int(math.floor((W - d0) / S)) + 1 if W >= d0 else 0
        # centre the array in the region
        ox = rect[0] + (W - ((cols - 1) * S + d0)) / 2.0 + d0 / 2.0
        oy = rect[1] + (H - ((rows - 1) * dy + d0)) / 2.0 + d0 / 2.0
        for j in range(rows):
            off = S / 2.0 if (stagger and j % 2) else 0.0
            for i in range(cols + 1):
                cx = ox + i * S + off
                cy = oy + j * dy
                d = d0 * (1.0 - jitter * rng.random() * 0.8)
                d *= (1.0 - fade * tfn(cx, cy))
                r = d / 2.0
                if d < min_open:
                    stats["dropped"] += 1
                    continue
                if cx - r < rect[0] - 1e-9 or cx + r > rect[2] + 1e-9 or \
                        cy - r < rect[1] - 1e-9 or cy + r > rect[3] + 1e-9:
                    continue
                holes.append({"kind": "circle", "c": (cx, cy), "r": r})
    else:
        if pattern == "voronoi":
            tiles = voronoi_tiles(o, rect, tfn)
        elif pattern == "hexagon":
            tiles = hexagon_tiles(o, rect)
        elif pattern == "diamond":
            tiles = diamond_tiles(o, rect)
        else:
            tiles = grid_tiles(o, rect)

        for tile in tiles:
            full = abs(area(tile))
            cell = clip_rect(tile, rect)
            if len(cell) < 3:
                continue
            if pattern != "voronoi" and not keep_partial and abs(area(cell)) < full * 0.995:
                continue
            cell = inset_convex(cell, mullion / 2.0)
            if len(cell) < 3:
                stats["dropped"] += 1
                continue
            if fade > 0:
                cx, cy = centroid(cell)
                inr = 2.0 * abs(area(cell)) / max(perimeter(cell), 1e-9)
                cell = inset_convex(cell, fade * tfn(cx, cy) * inr * 0.98)
                if len(cell) < 3:
                    stats["dropped"] += 1
                    continue
            cell = clean(ccw(cell), 3 * tol)
            if not cell:
                stats["dropped"] += 1
                continue
            a = abs(area(cell))
            if 4.0 * a / max(perimeter(cell), 1e-9) < min_open:
                stats["dropped"] += 1
                continue
            holes.append({"kind": "poly", "pts": cell, "round": rounding})

    # keep clear of doors / windows / openings already in the profile
    gap = mullion
    if obstacles:
        kept = []
        for h in holes:
            hb = _hole_bbox(h)
            if any(_boxes_hit(hb, r, gap) for r in obstacles):
                stats["dropped"] += 1
            else:
                kept.append(h)
        holes = kept

    # a non-rectangular profile (gable, sloped top...): stay inside it
    if not is_rect_outer:
        edge_gap = max(border, 4 * tol)
        kept = []
        for h in holes:
            pts = _sample_pts(h)
            if all(point_in_poly(p, outer) and dist_to_outline(p, outer) >= edge_gap - 1e-9 for p in pts):
                kept.append(h)
            else:
                stats["dropped"] += 1
        holes = kept

    stats["holes"] = len(holes)
    tot = abs(area(outer))
    if tot > 0:
        stats["open_pct"] = 100.0 * sum(hole_area(h) for h in holes) / tot
    return holes, stats


# --------------------------------------------------------------------------
# hole -> lines / arcs
# --------------------------------------------------------------------------
def hole_segments(h, tol):
    """[("L", p, q) | ("A", p, q, mid, centre)] - a closed loop. A circle
    becomes two half-arcs (Revit sketches dislike one closed arc)."""
    if h["kind"] == "circle":
        (cx, cy), r = h["c"], h["r"]
        e, w = (cx + r, cy), (cx - r, cy)
        return [("A", e, w, (cx, cy + r), (cx, cy)),
                ("A", w, e, (cx, cy - r), (cx, cy))]

    pts = ccw(h["pts"])
    frac = h.get("round", 0.0)
    n = len(pts)
    if frac <= 0:
        return [("L", pts[i], pts[(i + 1) % n]) for i in range(n)]

    corners = []   # per vertex: (T_in, T_out, mid, centre) or None (sharp)
    for i in range(n):
        p = pts[i]
        a = pts[i - 1]
        b = pts[(i + 1) % n]
        la, lb = _dist(p, a), _dist(p, b)
        u1 = ((a[0] - p[0]) / la, (a[1] - p[1]) / la)
        u2 = ((b[0] - p[0]) / lb, (b[1] - p[1]) / lb)
        cosang = max(-1.0, min(1.0, u1[0] * u2[0] + u1[1] * u2[1]))
        theta = math.acos(cosang)
        if theta < 1e-3 or theta > math.pi - 1e-3:
            corners.append(None)
            continue
        # tangent length may use at most half of each edge, leaving tol of line
        tmax = max(0.0, min((la - tol) / 2.0, (lb - tol) / 2.0))
        t = frac * tmax
        r = t * math.tan(theta / 2.0)
        T1 = (p[0] + u1[0] * t, p[1] + u1[1] * t)
        T2 = (p[0] + u2[0] * t, p[1] + u2[1] * t)
        if _dist(T1, T2) < 2 * tol or r < tol:
            corners.append(None)
            continue
        bx, by = u1[0] + u2[0], u1[1] + u2[1]
        bl = math.hypot(bx, by)
        bx, by = bx / bl, by / bl
        dc = r / math.sin(theta / 2.0)
        c = (p[0] + bx * dc, p[1] + by * dc)
        mid = (c[0] - bx * r, c[1] - by * r)
        corners.append((T1, T2, mid, c))

    segs = []
    for i in range(n):
        ci = corners[i]
        cj = corners[(i + 1) % n]
        start = ci[1] if ci else pts[i]
        end = cj[0] if cj else pts[(i + 1) % n]
        if ci:
            segs.append(("A", ci[0], ci[1], ci[2], ci[3]))
        if _dist(start, end) >= tol:
            segs.append(("L", start, end))
        elif ci and cj:
            # tangent points met: snap the next arc onto this one's end
            corners[(i + 1) % n] = (start, cj[1], cj[2], cj[3])
    return segs


def tessellate(segs, steps=8):
    """Points along a segment loop, for the preview."""
    out = []
    for s in segs:
        if s[0] == "L":
            out.append(s[1])
            continue
        p, q, _m, c = s[1], s[2], s[3], s[4]
        r = _dist(p, c)
        a0 = math.atan2(p[1] - c[1], p[0] - c[0])
        a1 = math.atan2(q[1] - c[1], q[0] - c[0])
        am = math.atan2(_m[1] - c[1], _m[0] - c[0])
        d = a1 - a0
        # choose the sweep that passes through the mid point
        while d <= -math.pi:
            d += 2 * math.pi
        while d > math.pi:
            d -= 2 * math.pi
        dm = am - a0
        while dm <= -math.pi:
            dm += 2 * math.pi
        while dm > math.pi:
            dm -= 2 * math.pi
        if (d > 0) != (dm > 0) or abs(dm) > abs(d):
            d = d - 2 * math.pi if d > 0 else d + 2 * math.pi
        for k in range(steps):
            a = a0 + d * k / float(steps)
            out.append((c[0] + r * math.cos(a), c[1] + r * math.sin(a)))
    return out
