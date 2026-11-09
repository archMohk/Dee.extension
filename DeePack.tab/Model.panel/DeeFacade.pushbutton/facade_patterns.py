# -*- coding: utf-8 -*-
"""
facade_patterns
Pure 2D geometry for DeeFacade - no Revit, no .NET, so it runs (and is
tested) in plain CPython as well as in pyRevit's IronPython 2.7.

Everything is in the wall's own elevation coordinates: x runs along the
wall, y is height. Units are whatever the caller passes (DeeFacade
passes feet, Revit's internal unit) - nothing here assumes mm or ft.

build(opts, outer, obstacles, tol, cache) is the one entry point:
  outer      the wall's outer profile as a polygon [(x, y), ...]
  obstacles  rectangles (x0, y0, x1, y1) the openings must keep clear of
             (doors, windows, openings already in the profile)
  tol        the shortest edge Revit will accept (ShortCurveTolerance)
  cache      optional dict, reused between calls: the raw tiles of a
             pattern only depend on the pattern's own settings, so moving
             the mullion / rounding / fade / attractor sliders never
             recomputes a Voronoi diagram (keeps the live preview smooth)
It returns (holes, stats). A hole is either
  {"kind": "poly",   "pts": [(x, y), ...], "round": 0..1}
  {"kind": "circle", "c": (x, y), "r": radius}
and hole_segments() turns one into the lines / arcs Revit draws.

Every hole is a closed, simple loop that stays inside the outer profile
and never touches another hole. No edge is shorter than min_edge(tol):
straight pieces of ~2 mm between rounded corners are what made Revit
fail with "Unexpected internal error" on long walls.
"""
from __future__ import division

import math
import random

PATTERNS = ("voronoi", "hexagon", "diamond", "grid", "circles",
            "stars", "triangles", "waves", "pixels")
DIRECTIONS = ("top", "bottom", "left", "right", "edges", "centre", "points")


def min_edge(tol):
    """Shortest line / arc chord ever emitted."""
    return 4.0 * tol


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
    one edge). Convex in -> convex out."""
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
        nx, ny = dy / ln, -dx / ln          # outward normal of a CCW edge
        edges.append((nx, ny, nx * p[0] + ny * p[1] - d))
    out = list(poly)
    for a, b, c in edges:
        out = clip_halfplane(out, a, b, c)
        if len(out) < 3:
            return []
    return out


def scale_about(poly, c, s):
    return [(c[0] + (p[0] - c[0]) * s, c[1] + (p[1] - c[1]) * s) for p in poly]


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


def clean(poly, min_len, min_turn_deg=0.5):
    """Merges vertices closer than min_len and drops corners that turn
    less than min_turn_deg (near-straight), so no edge is shorter than
    Revit accepts and no near-flat arc is ever filleted."""
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
    lim = math.sin(math.radians(min_turn_deg))
    changed = True
    while changed and len(pts) >= 3:
        changed = False
        n = len(pts)
        for i in range(n):
            a = pts[i - 1]
            b = pts[i]
            c = pts[(i + 1) % n]
            la, lc = _dist(a, b), _dist(b, c)
            if la < 1e-12 or lc < 1e-12:
                pts.pop(i)
                changed = True
                break
            cross = ((b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])) / (la * lc)
            dot = ((b[0] - a[0]) * (c[0] - b[0]) + (b[1] - a[1]) * (c[1] - b[1]))
            if abs(cross) < lim and dot > 0:
                pts.pop(i)
                changed = True
                break
    return pts if len(pts) >= 3 else []


# --------------------------------------------------------------------------
# direction / attractor / closure
# --------------------------------------------------------------------------
def attractor_fn(o):
    """w(x, y) in 0..1: 1 on an attractor point, smooth falloff to 0 at
    the radius. None when there are no points."""
    pts = o.get("attractors") or []
    R = max(o.get("att_radius", 10.0), 1e-6)
    if not pts:
        return None

    def w(x, y):
        best = 0.0
        for px, py in pts:
            d2 = ((x - px) ** 2 + (y - py) ** 2) / (R * R)
            if d2 < 1.0:
                v = (1.0 - d2) ** 2
                if v > best:
                    best = v
        return best
    return w


def direction_fn(direction, rect, attw=None):
    """t(x, y) in 0..1, 1 toward the chosen side (or the attractor points)."""
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
        "points": attw if attw is not None else (lambda x, y: 0.0),
    }
    f = fns.get(direction, fns["top"])
    return lambda x, y: max(0.0, min(1.0, f(x, y)))


def closure_fn(o, tfn, attw):
    """c(x, y) in 0..1: how far an opening at (x, y) closes up.
    Fade (toward a side) + attractor points (grow or close near them)."""
    fade = max(0.0, min(1.0, o.get("fade", 0.0)))
    s = max(0.0, min(1.0, o.get("att_strength", 0.0)))
    grow = o.get("att_mode", "grow") == "grow"
    use_att = attw is not None and s > 0

    def c(x, y):
        v = fade * tfn(x, y) if fade > 0 else 0.0
        if use_att:
            a = attw(x, y)
            v += s * (1.0 - a) if grow else s * a
        return max(0.0, min(1.0, v))
    return c


# --------------------------------------------------------------------------
# raw tiles per pattern
# items: ("convex", poly)  - clipped to the region, mullion inset applied
#        ("shape", poly, centre) - kept whole, closure scales it about centre
#        ("free", poly)  - final shape, already sized (closure built in)
#        ("circle", (cx, cy), r0)
# --------------------------------------------------------------------------
def voronoi_cells(seeds, rect):
    """Voronoi cell of every seed clipped to rect. Half-plane clipping with
    a bucket grid: neighbours ring by ring, stopping once no farther seed
    can cut the cell - near O(n) instead of O(n^2)."""
    rect_poly = [(rect[0], rect[1]), (rect[2], rect[1]), (rect[2], rect[3]), (rect[0], rect[3])]
    n = len(seeds)
    if not n:
        return []
    W = max(rect[2] - rect[0], 1e-9)
    H = max(rect[3] - rect[1], 1e-9)
    cs = max(math.sqrt(W * H / n), 1e-9)
    nx = int(W / cs) + 1
    ny = int(H / cs) + 1
    grid = {}
    keys = []
    for i, (x, y) in enumerate(seeds):
        k = (min(nx - 1, max(0, int((x - rect[0]) / cs))), min(ny - 1, max(0, int((y - rect[1]) / cs))))
        grid.setdefault(k, []).append(i)
        keys.append(k)
    cells = []
    maxring = max(nx, ny)
    for i, s in enumerate(seeds):
        cell = list(rect_poly)
        reach2 = max((p[0] - s[0]) ** 2 + (p[1] - s[1]) ** 2 for p in cell)
        ki, kj = keys[i]
        ring = 0
        while ring <= maxring:
            if ring == 0:
                buckets = [(ki, kj)]
            else:
                buckets = [(ki + dx, kj + dy) for dx in range(-ring, ring + 1)
                           for dy in (-ring, ring)]
                buckets += [(ki + dx, kj + dy) for dx in (-ring, ring)
                            for dy in range(-ring + 1, ring)]
            for b in buckets:
                for j in grid.get(b, ()):
                    if j == i:
                        continue
                    o = seeds[j]
                    a = o[0] - s[0]
                    bb = o[1] - s[1]
                    if a * a + bb * bb < 1e-18:
                        continue
                    c = (o[0] ** 2 + o[1] ** 2 - s[0] ** 2 - s[1] ** 2) / 2.0
                    cell = clip_halfplane(cell, a, bb, c)
                    if len(cell) < 3:
                        break
                if len(cell) < 3:
                    break
            if len(cell) < 3:
                break
            reach2 = max((p[0] - s[0]) ** 2 + (p[1] - s[1]) ** 2 for p in cell)
            # every seed in ring+1 is at least ring*cs away
            if (ring * cs) ** 2 > 4.0 * reach2:
                break
            ring += 1
        cells.append(cell if len(cell) >= 3 else [])
    return cells


def voronoi_tiles(o, rect, tfn):
    rng = random.Random(int(o.get("seed", 1)))
    n = max(2, int(o.get("vor_count", 60)))
    density = max(0.0, o.get("vor_density", 0.0))
    x0, y0, x1, y1 = rect
    seeds = []
    wmax = 1.0 + 7.0 * density
    tries = 0
    while len(seeds) < n and tries < n * 200:
        tries += 1
        x = x0 + rng.random() * (x1 - x0)
        y = y0 + rng.random() * (y1 - y0)
        if density > 0 and rng.random() * wmax > 1.0 + 7.0 * density * tfn(x, y):
            continue
        seeds.append((x, y))
    cells = voronoi_cells(seeds, rect)
    for _ in range(int(o.get("vor_relax", 0))):
        seeds = [centroid(c) if c else s for c, s in zip(cells, seeds)]
        cells = voronoi_cells(seeds, rect)
    return [("convex", c) for c in cells if c]


def _grid_range(lo, hi, step, pad=2):
    return range(int(math.floor(lo / step)) - pad, int(math.ceil(hi / step)) + pad + 1)


def hexagon_tiles(o, rect):
    w = max(o.get("hex_size", 1.0), 1e-6)
    R = w / math.sqrt(3.0)
    x0, y0, x1, y1 = rect
    tiles = []
    if o.get("hex_pointy", True):
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
    return [("convex", t) for t in tiles]


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
    return [("convex", t) for t in tiles]


def grid_tiles(o, rect):
    W = max(o.get("grid_w", 1.0), 1e-6)
    H = max(o.get("grid_h", 1.0), 1e-6)
    stagger = o.get("grid_stagger", False)
    x0, y0, x1, y1 = rect
    tiles = []
    for j in range(0, int(math.ceil((y1 - y0) / H)) + 1):
        off = (W / 2.0) if (stagger and j % 2) else 0.0
        for i in range(-1, int(math.ceil((x1 - x0) / W)) + 2):
            ax = x0 + i * W - off
            ay = y0 + j * H
            tiles.append([(ax, ay), (ax + W, ay), (ax + W, ay + H), (ax, ay + H)])
    return [("convex", t) for t in tiles]


def triangle_tiles(o, rect):
    """Triangular lattice, optionally jittered (organic triangles). The
    lattice connectivity is kept, so jitter <= 25% never flips a triangle."""
    S = max(o.get("tri_size", 1.0), 1e-6)
    h = S * math.sqrt(3.0) / 2.0
    jit = max(0.0, min(1.0, o.get("tri_jitter", 0.0))) * 0.25 * S
    rng = random.Random(int(o.get("seed", 1)))
    x0, y0, x1, y1 = rect
    js = list(range(int(math.floor(y0 / h)) - 1, int(math.ceil(y1 / h)) + 2))
    is_ = list(range(int(math.floor(x0 / S)) - 2, int(math.ceil(x1 / S)) + 3))
    P = {}
    for j in js:
        for i in is_:
            dx = (rng.random() * 2 - 1) * jit if jit else 0.0
            dy = (rng.random() * 2 - 1) * jit if jit else 0.0
            P[(i, j)] = (i * S + (j % 2) * S / 2.0 + dx, j * h + dy)
    tiles = []
    for j in js[:-1]:
        for i in is_[:-2]:
            if j % 2 == 0:
                tris = (((i, j), (i + 1, j), (i, j + 1)),
                        ((i, j + 1), (i + 1, j), (i + 1, j + 1)))
            else:
                tris = (((i, j), (i + 1, j), (i + 1, j + 1)),
                        ((i + 1, j + 1), (i + 1, j), (i + 2, j + 1)))
            for t in tris:
                if all(k in P for k in t):
                    tiles.append([P[k] for k in t])
    return [("convex", t) for t in tiles]


def pixel_tiles(o, rect):
    """Random blocks of 1..N modules (Mondrian / pixel look); `pix_fill`
    of them become openings."""
    M = max(o.get("pix_size", 1.0), 1e-6)
    maxm = max(1, int(o.get("pix_max", 4)))
    fill = max(0.0, min(1.0, o.get("pix_fill", 0.55)))
    rng = random.Random(int(o.get("seed", 1)))
    x0, y0, x1, y1 = rect
    cols = max(1, int(math.ceil((x1 - x0) / M - 1e-9)))
    rows = max(1, int(math.ceil((y1 - y0) / M - 1e-9)))
    if cols * rows > 60000:
        return []
    used = set()
    order = [(i, j) for j in range(rows) for i in range(cols)]
    rng.shuffle(order)
    tiles = []
    for i, j in order:
        if (i, j) in used:
            continue
        w = min(rng.randint(1, maxm), cols - i)
        h = min(rng.randint(1, maxm), rows - j)
        while w > 1 and any((i + a, j) in used for a in range(w)):
            w -= 1
        while h > 1 and any((i + a, j + b) in used for a in range(w) for b in range(h)):
            h -= 1
        for a in range(w):
            for b in range(h):
                used.add((i + a, j + b))
        if rng.random() < fill:
            ax, ay = x0 + i * M, y0 + j * M
            tiles.append(("convex", [(ax, ay), (ax + w * M, ay), (ax + w * M, ay + h * M), (ax, ay + h * M)]))
    return tiles


def star_tiles(o, rect, mullion, min_open):
    """Mashrabiya: 8-point stars on a square grid, plus (optionally) small
    octagons in the gaps between four stars."""
    P = max(o.get("star_pitch", 1.0), 1e-6)
    size = max(0.2, min(1.0, o.get("star_size", 0.9)))
    ratio = max(0.3, min(0.95, o.get("star_sharp", 0.77)))
    R = (P - mullion) / 2.0 * size
    if R <= 0:
        return []
    r = R * ratio
    x0, y0, x1, y1 = rect
    W, H = x1 - x0, y1 - y0
    if W < 2 * R or H < 2 * R:
        return []
    nx = int(math.floor((W - 2 * R) / P)) + 1
    ny = int(math.floor((H - 2 * R) / P)) + 1
    ox = x0 + (W - (nx - 1) * P) / 2.0
    oy = y0 + (H - (ny - 1) * P) / 2.0
    tiles = []
    for j in range(ny):
        for i in range(nx):
            cx, cy = ox + i * P, oy + j * P
            star = [(cx + (R if k % 2 == 0 else r) * math.cos(k * math.pi / 8.0),
                     cy + (R if k % 2 == 0 else r) * math.sin(k * math.pi / 8.0)) for k in range(16)]
            tiles.append(("shape", star, (cx, cy)))
    if o.get("star_fill", True):
        ro = P / math.sqrt(2.0) - R - mullion          # gap to the facing star tips
        if 2 * ro >= min_open:
            for j in range(ny - 1):
                for i in range(nx - 1):
                    cx, cy = ox + (i + 0.5) * P, oy + (j + 0.5) * P
                    octa = [(cx + ro * math.cos(math.pi / 8.0 + k * math.pi / 4.0),
                             cy + ro * math.sin(math.pi / 8.0 + k * math.pi / 4.0)) for k in range(8)]
                    tiles.append(("shape", octa, (cx, cy)))
    return tiles


def wave_tiles(o, rect, mullion, min_open, cfn, tol):
    """Parallel slots whose width follows a sine wave along their length;
    the closure (fade / attractors) narrows them further and can split a
    slot where it closes completely."""
    pitch = max(o.get("wave_pitch", 1.0), 1e-6)
    amp = max(0.0, min(1.0, o.get("wave_amp", 0.7)))
    lam = max(o.get("wave_len", 10.0), 1e-6)
    shift = o.get("wave_shift", 0.08)
    horiz = o.get("wave_horizontal", False)
    x0, y0, x1, y1 = rect
    A0, A1, B0, B1 = (y0, y1, x0, x1) if horiz else (x0, x1, y0, y1)
    maxw = pitch - mullion
    if maxw <= 0 or A1 - A0 < maxw:
        return []
    n = int(math.floor((A1 - A0 - maxw) / pitch)) + 1
    oa = A0 + (A1 - A0 - (n - 1) * pitch) / 2.0
    me = min_edge(tol)
    step = max(me * 1.5, lam / 24.0, (B1 - B0) / 600.0)
    nb = max(2, int(math.ceil((B1 - B0) / step)))
    bs = [B0 + (B1 - B0) * k / float(nb) for k in range(nb + 1)]
    wthr = max(me * 2.0, min_open * 0.35)
    tiles = []

    def xy(a, b):
        return (b, a) if horiz else (a, b)

    for k in range(n):
        ac = oa + k * pitch
        ws = []
        for b in bs:
            w = maxw * (1.0 - amp * 0.5 * (1.0 + math.sin(2 * math.pi * (b / lam + k * shift))))
            x, y = xy(ac, b)
            w *= (1.0 - cfn(x, y))
            ws.append(w)
        run = []
        for b, w in list(zip(bs, ws)) + [(None, 0.0)]:
            if b is not None and w >= wthr:
                run.append((b, w))
                continue
            if len(run) >= 2 and run[-1][0] - run[0][0] >= min_open:
                left = [xy(ac - w / 2.0, bb) for bb, w in run]
                right = [xy(ac + w / 2.0, bb) for bb, w in reversed(run)]
                tiles.append(("free", left + right))
            run = []
    return tiles


def _tiles(o, rect, tfn, cfn, mullion, min_open, tol):
    p = o.get("pattern", "voronoi")
    if p == "voronoi":
        return voronoi_tiles(o, rect, tfn)
    if p == "hexagon":
        return hexagon_tiles(o, rect)
    if p == "diamond":
        return diamond_tiles(o, rect)
    if p == "grid":
        return grid_tiles(o, rect)
    if p == "triangles":
        return triangle_tiles(o, rect)
    if p == "pixels":
        return pixel_tiles(o, rect)
    if p == "stars":
        return star_tiles(o, rect, mullion, min_open)
    if p == "waves":
        return wave_tiles(o, rect, mullion, min_open, cfn, tol)
    return []


# settings each pattern's raw tiles depend on (the cache key)
_TILE_KEYS = {
    "voronoi": ("seed", "vor_count", "vor_relax", "vor_density", "direction", "attractors", "att_radius"),
    "hexagon": ("hex_size", "hex_pointy"),
    "diamond": ("dia_w", "dia_h"),
    "grid": ("grid_w", "grid_h", "grid_stagger"),
    "triangles": ("tri_size", "tri_jitter", "seed"),
    "pixels": ("pix_size", "pix_max", "pix_fill", "seed"),
    "stars": ("star_pitch", "star_size", "star_sharp", "star_fill", "mullion", "min_open"),
}


def _cache_key(o, rect):
    p = o.get("pattern", "voronoi")
    keys = _TILE_KEYS.get(p)
    if keys is None:
        return None
    if p == "voronoi" and not o.get("vor_density"):
        keys = keys[:4]       # no gradient: direction / points do not matter
    vals = []
    for k in keys:
        v = o.get(k)
        if isinstance(v, list):
            v = tuple(tuple(x) for x in v)
        vals.append(v)
    return (p, tuple(round(x, 9) for x in rect), tuple(vals))


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
    return abs(area(h["pts"])) * (1.0 - 0.107 * h.get("round", 0.0))


def _inside(poly, rect):
    return all(rect[0] - 1e-9 <= p[0] <= rect[2] + 1e-9 and
               rect[1] - 1e-9 <= p[1] <= rect[3] + 1e-9 for p in poly)


def build(o, outer, obstacles, tol, cache=None):
    """See the module docstring. `o` holds lengths in the caller's unit."""
    me = min_edge(tol)
    outer = ccw(clean(outer, tol) or outer)
    ob = bbox(outer)
    mullion = max(o.get("mullion", 0.3), me)
    border = max(o.get("border", 0.0), mullion / 2.0, me)
    rect = (ob[0] + border, ob[1] + border, ob[2] - border, ob[3] - border)
    stats = {"holes": 0, "open_pct": 0.0, "dropped": 0}
    if rect[2] - rect[0] <= me or rect[3] - rect[1] <= me:
        return [], stats

    attw = attractor_fn(o)
    tfn = direction_fn(o.get("direction", "top"), rect, attw)
    cfn = closure_fn(o, tfn, attw)
    min_open = max(o.get("min_open", 0.0), 2 * me)
    rounding = max(0.0, min(1.0, o.get("round", 0.0)))
    keep_partial = o.get("keep_partial", True)
    pattern = o.get("pattern", "voronoi")
    oa = abs(area(outer))
    is_rect_outer = len(outer) == 4 and abs(oa - (ob[2] - ob[0]) * (ob[3] - ob[1])) < 1e-6 * oa
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
        rows = int(math.floor((H - d0) / dy)) + 1 if H >= d0 > 0 else 0
        cols = int(math.floor((W - d0) / S)) + 1 if W >= d0 > 0 else 0
        ox = rect[0] + (W - ((cols - 1) * S + d0)) / 2.0 + d0 / 2.0
        oy = rect[1] + (H - ((rows - 1) * dy + d0)) / 2.0 + d0 / 2.0
        for j in range(rows):
            off = S / 2.0 if (stagger and j % 2) else 0.0
            for i in range(cols + 1):
                cx = ox + i * S + off
                cy = oy + j * dy
                d = d0 * (1.0 - jitter * rng.random() * 0.8)
                d *= (1.0 - cfn(cx, cy))
                r = d / 2.0
                if d < min_open:
                    stats["dropped"] += 1
                    continue
                if cx - r < rect[0] - 1e-9 or cx + r > rect[2] + 1e-9 or \
                        cy - r < rect[1] - 1e-9 or cy + r > rect[3] + 1e-9:
                    continue
                holes.append({"kind": "circle", "c": (cx, cy), "r": r})
    else:
        key = _cache_key(o, rect) if cache is not None else None
        if key is not None and cache.get("key") == key:
            tiles = cache["tiles"]
        else:
            tiles = _tiles(o, rect, tfn, cfn, mullion, min_open, tol)
            if key is not None:
                cache["key"] = key
                cache["tiles"] = tiles

        for t in tiles:
            kind = t[0]
            if kind == "convex":
                tile = t[1]
                full = abs(area(tile))
                cell = clip_rect(tile, rect)
                if len(cell) < 3:
                    continue
                if pattern not in ("voronoi", "triangles", "pixels") and not keep_partial \
                        and abs(area(cell)) < full * 0.995:
                    continue
                cell = inset_convex(cell, mullion / 2.0)
                if len(cell) < 3:
                    stats["dropped"] += 1
                    continue
                cx, cy = centroid(cell)
                c = cfn(cx, cy)
                if c > 0:
                    inr = 2.0 * abs(area(cell)) / max(perimeter(cell), 1e-9)
                    cell = inset_convex(cell, c * inr * 0.98)
                    if len(cell) < 3:
                        stats["dropped"] += 1
                        continue
            elif kind == "shape":
                cell, cen = t[1], t[2]
                if not _inside(cell, rect):
                    continue
                c = cfn(cen[0], cen[1])
                if c > 0:
                    cell = scale_about(cell, cen, 1.0 - c)
            else:                                  # "free"
                cell = t[1]
                if not _inside(cell, rect):
                    continue
            cell = clean(ccw(cell), me)
            if not cell:
                stats["dropped"] += 1
                continue
            if 4.0 * abs(area(cell)) / max(perimeter(cell), 1e-9) < min_open:
                stats["dropped"] += 1
                continue
            holes.append({"kind": "poly", "pts": cell, "round": rounding})

    if obstacles:
        kept = []
        for h in holes:
            hb = _hole_bbox(h)
            if any(_boxes_hit(hb, r, mullion) for r in obstacles):
                stats["dropped"] += 1
            else:
                kept.append(h)
        holes = kept

    if not is_rect_outer:
        edge_gap = max(border, me)
        kept = []
        for h in holes:
            if all(point_in_poly(p, outer) and dist_to_outline(p, outer) >= edge_gap - 1e-9
                   for p in _sample_pts(h)):
                kept.append(h)
            else:
                stats["dropped"] += 1
        holes = kept

    stats["holes"] = len(holes)
    if oa > 0:
        stats["open_pct"] = 100.0 * sum(hole_area(h) for h in holes) / oa
    return holes, stats


# --------------------------------------------------------------------------
# hole -> lines / arcs
# --------------------------------------------------------------------------
def _arc_sweep(p, q, m, c):
    a0 = math.atan2(p[1] - c[1], p[0] - c[0])
    a1 = math.atan2(q[1] - c[1], q[0] - c[0])
    am = math.atan2(m[1] - c[1], m[0] - c[0])

    def norm(d):
        while d <= -math.pi:
            d += 2 * math.pi
        while d > math.pi:
            d -= 2 * math.pi
        return d
    d = norm(a1 - a0)
    dm = norm(am - a0)
    if (d > 0) != (dm > 0) or abs(dm) > abs(d):
        d = d - 2 * math.pi if d > 0 else d + 2 * math.pi
    return a0, d


def _arc_points(seg, steps):
    """steps+1 points from the arc's start to its end."""
    p, q, m, c = seg[1], seg[2], seg[3], seg[4]
    r = _dist(p, c)
    a0, d = _arc_sweep(p, q, m, c)
    pts = [(c[0] + r * math.cos(a0 + d * k / float(steps)),
            c[1] + r * math.sin(a0 + d * k / float(steps))) for k in range(steps)]
    return pts + [q]


def hole_segments(h, tol, arcs=True):
    """[("L", p, q) | ("A", p, q, mid, centre)] - a closed loop. A circle
    is two half-arcs. arcs=False approximates every arc with short
    straight chords (the fallback when Revit refuses the arcs)."""
    me = min_edge(tol)
    if h["kind"] == "circle":
        (cx, cy), r = h["c"], h["r"]
        e, w = (cx + r, cy), (cx - r, cy)
        segs = [("A", e, w, (cx, cy + r), (cx, cy)),
                ("A", w, e, (cx, cy - r), (cx, cy))]
    else:
        segs = _poly_segments(ccw(h["pts"]), h.get("round", 0.0), me)
    if arcs:
        return segs
    out = []
    for s in segs:
        if s[0] == "L":
            out.append(s)
            continue
        r = _dist(s[1], s[4])
        _a0, d = _arc_sweep(s[1], s[2], s[3], s[4])
        steps = max(1, min(8, int(abs(d) * r / (me * 1.2))))
        pts = _arc_points(s, steps)
        for k in range(len(pts) - 1):
            out.append(("L", pts[k], pts[k + 1]))
    return out


def _poly_segments(pts, frac, me):
    n = len(pts)
    if frac <= 0:
        return [("L", pts[i], pts[(i + 1) % n]) for i in range(n)]
    lim_lo = math.radians(5.0)
    lim_hi = math.radians(170.0)
    corners = []   # per vertex: (T_in, T_out, mid, centre) or None (sharp)
    for i in range(n):
        p = pts[i]
        a = pts[i - 1]
        b = pts[(i + 1) % n]
        la, lb = _dist(p, a), _dist(p, b)
        u1 = ((a[0] - p[0]) / la, (a[1] - p[1]) / la)
        u2 = ((b[0] - p[0]) / lb, (b[1] - p[1]) / lb)
        theta = math.acos(max(-1.0, min(1.0, u1[0] * u2[0] + u1[1] * u2[1])))
        if theta < lim_lo or theta > lim_hi:
            corners.append(None)
            continue
        # each corner may use at most half of each edge, minus a real line
        t = frac * max(0.0, min((la - me) / 2.0, (lb - me) / 2.0))
        r = t * math.tan(theta / 2.0)
        T1 = (p[0] + u1[0] * t, p[1] + u1[1] * t)
        T2 = (p[0] + u2[0] * t, p[1] + u2[1] * t)
        if _dist(T1, T2) < me or r < me / 2.0:
            corners.append(None)
            continue
        bx, by = u1[0] + u2[0], u1[1] + u2[1]
        bl = math.hypot(bx, by)
        bx, by = bx / bl, by / bl
        dc = r / math.sin(theta / 2.0)
        c = (p[0] + bx * dc, p[1] + by * dc)
        corners.append((T1, T2, (c[0] - bx * r, c[1] - by * r), c))
    segs = []
    for i in range(n):
        ci = corners[i]
        cj = corners[(i + 1) % n]
        if ci:
            segs.append(("A", ci[0], ci[1], ci[2], ci[3]))
        start = ci[1] if ci else pts[i]
        end = cj[0] if cj else pts[(i + 1) % n]
        segs.append(("L", start, end))
    return segs


def tessellate(segs, steps=6):
    """Points along a segment loop, for the preview."""
    out = []
    for s in segs:
        if s[0] == "L":
            out.append(s[1])
        else:
            out.extend(_arc_points(s, steps)[:-1])
    return out
