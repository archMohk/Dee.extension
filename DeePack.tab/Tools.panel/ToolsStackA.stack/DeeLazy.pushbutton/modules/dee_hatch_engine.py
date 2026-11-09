# -*- coding: utf-8 -*-
"""
DeeLazy - DeeHATCH engine (pure Python: no Revit, no WPF)

Everything DeeHATCH computes lives here so it can be tested with plain
CPython outside Revit (IronPython 2.7 and CPython 3 both run it):

  * PAT files     parse_pat() / write_pat()  - AutoCAD .pat format
                  (acad.pat, acadiso.pat, any custom file, Revit's own
                  exported .pat files with ;%UNITS / ;%TYPE headers)
  * lines -> PAT  lines_to_grids()  - the "Make Pattern" solver
  * image -> PAT  trace_outlines() / scan_fill() - monochrome bitmap to
                  hatch lines (outline tracing and/or scan-line fill)
  * transforms    transform_grids() - scale, rotate, flip
  * Revit form    revit_form() - normalises a PAT line into what
                  Revit's FillGrid accepts (positive offset, dash list
                  starting with a dash, even length, no zero dots)
  * preview       preview_segments() - every visible dash inside a
                  rectangle, used by the window's live preview

--------------------------------------------------------------------
PAT line = one family of parallel dashed lines
--------------------------------------------------------------------
    angle, x-origin, y-origin, shift, offset [, dash, -gap, ...]
Line k of the family starts at  origin + k * (shift * u + offset * n)
where u is the line direction and n its left normal. Dashes are
positive, gaps negative, 0 is a dot. This module calls one such line a
Grid (Revit's FillGrid is the same thing).

--------------------------------------------------------------------
The Make Pattern solver (lines_to_grids)
--------------------------------------------------------------------
A pattern repeats on a W x H tile. A segment drawn inside the tile can
only be reproduced by ONE PAT line if its direction is a lattice
vector (a*W, b*H) with a, b coprime integers - otherwise the copies in
the neighbouring tiles would not line up. So each segment's angle is
snapped to the nearest such direction with |a|, |b| <= max_mult (the
"Precision" setting; pyRevit's "Use Highest Resolution" does the same
by growing its search domain). For direction (a, b):

    L      = |(aW, bH)|            period along the line
    offset = W * H / L             spacing between the parallel lines
    shift  = (i*a*W^2 + j*b*H^2)/L where j*a - i*b = 1 (extended gcd)
    dashes = [length, -(L - length)]

The segment keeps its length and centre (rotated about its centre, as
pyRevit does). Then - unlike pyRevit, which writes one PAT line per
segment - every segment that lands on the SAME family of lines is
merged into one PAT line with several dashes (the dash intervals are
unioned modulo L). Lines drawn twice on opposite tile edges therefore
collapse into one, overlapping pieces join, and an image's scan rows
become one PAT line per row instead of one per run.
"""
from __future__ import division

import bisect
import io
import math
import os
import re

MM_PER_FT = 304.8
INCH_PER_FT = 12.0
_TWO_PI = 2.0 * math.pi


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------
class Grid(object):
    """One PAT line (= one Revit FillGrid). angle in DEGREES; dashes in
    AutoCAD sign convention (dash > 0, gap < 0, 0 = dot)."""

    __slots__ = ("angle", "ox", "oy", "shift", "offset", "dashes")

    def __init__(self, angle, ox, oy, shift, offset, dashes=None):
        self.angle = float(angle)
        self.ox = float(ox)
        self.oy = float(oy)
        self.shift = float(shift)
        self.offset = float(offset)
        self.dashes = [float(d) for d in (dashes or [])]

    def copy(self):
        return Grid(self.angle, self.ox, self.oy, self.shift, self.offset, self.dashes)

    def scaled(self, k):
        return Grid(self.angle, self.ox * k, self.oy * k, self.shift * k,
                    self.offset * k, [d * k for d in self.dashes])

    def __repr__(self):
        return "<Grid {0}>".format(format_grid(self))


class PatDef(object):
    """One pattern read from (or written to) a .pat file."""

    def __init__(self, name, desc=u"", grids=None, units=None, target=None):
        self.name = name
        self.desc = desc
        self.grids = list(grids or [])
        self.units = units      # "MM" / "INCH" / None = not stated
        self.target = target    # "MODEL" / "DRAFTING" / None = not stated
        self.warnings = []

    def __repr__(self):
        return "<PatDef {0} grids={1} units={2} type={3}>".format(
            self.name, len(self.grids), self.units, self.target)


# --------------------------------------------------------------------------
# PAT read / write
# --------------------------------------------------------------------------
_HEADER_RE = re.compile(r";+\s*%\s*(UNITS|TYPE|VERSION)\s*=\s*([A-Za-z0-9.]+)", re.I)
_UNIT_ALIASES = {"MM": "MM", "MILLIMETERS": "MM", "MILLIMETRES": "MM", "METRIC": "MM",
                 "INCH": "INCH", "INCHES": "INCH", "IN": "INCH", "IMPERIAL": "INCH"}
_TYPE_ALIASES = {"MODEL": "MODEL", "DRAFTING": "DRAFTING", "DETAIL": "DRAFTING"}


def read_text(path):
    """PAT files come in UTF-8, UTF-8 with BOM, or an ANSI code page."""
    raw = io.open(path, "rb").read()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("latin-1", "replace")


def parse_pat(text):
    """Returns (patterns, file_units, file_type). Units/type set before
    the first pattern apply to the whole file; set after a *NAME line
    they apply to that pattern only (Revit writes ;%TYPE that way)."""
    file_units = None
    file_type = None
    pats = []
    cur = None
    for raw in (text or u"").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(";"):
            m = _HEADER_RE.match(line)
            if not m:
                continue
            key, val = m.group(1).upper(), m.group(2).upper()
            if key == "UNITS":
                val = _UNIT_ALIASES.get(val)
                if cur is None:
                    file_units = val or file_units
                elif val:
                    cur.units = val
            elif key == "TYPE":
                val = _TYPE_ALIASES.get(val)
                if cur is None:
                    file_type = val or file_type
                elif val:
                    cur.target = val
            continue
        if line.startswith("*"):
            if cur is not None and cur.grids:
                pats.append(cur)
            name, _sep, desc = line[1:].partition(",")
            cur = PatDef(name.strip(), desc.strip())
            continue
        if cur is None:
            continue
        code = line.split(";")[0]
        parts = [p.strip() for p in code.split(",")]
        parts = [p for p in parts if p]
        try:
            nums = [float(p) for p in parts]
        except ValueError:
            cur.warnings.append(u"Skipped unreadable line: " + line)
            continue
        if len(nums) < 5:
            cur.warnings.append(u"Skipped short line (needs 5+ numbers): " + line)
            continue
        cur.grids.append(Grid(nums[0], nums[1], nums[2], nums[3], nums[4], nums[5:]))
    if cur is not None and cur.grids:
        pats.append(cur)
    for p in pats:
        if p.units is None:
            p.units = file_units
        if p.target is None:
            p.target = file_type
    return pats, file_units, file_type


def guess_units(path, file_units):
    """Header first; else the file name (AutoCAD ships acad.pat in
    inches and acadiso.pat in mm); else mm."""
    if file_units:
        return file_units
    base = os.path.basename(path or "").lower()
    if any(k in base for k in ("iso", "metric", "mm")):
        return "MM"
    if base in ("acad.pat", "acadlt.pat", "acdb.pat", "revit.pat") or "imperial" in base or "inch" in base:
        return "INCH"
    return "MM"


def unit_to_feet(units):
    return 1.0 / (MM_PER_FT if units == "MM" else INCH_PER_FT)


def fmt_num(v, decimals=6):
    s = ("{0:." + str(decimals) + "f}").format(float(v))
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    if s in ("-0", ""):
        s = "0"
    return s


def format_grid(g):
    vals = [g.angle, g.ox, g.oy, g.shift, g.offset] + list(g.dashes)
    return u", ".join(fmt_num(v) for v in vals)


def clean_pat_name(name):
    """PAT names can't hold commas; AutoCAD also dislikes spaces at the
    ends and some symbols."""
    s = u"".join(c for c in (name or u"") if c not in u",*;\r\n\t")
    return s.strip() or u"Pattern"


def write_pat(patterns, units, credit=u"DeeHATCH (Dee.extension)"):
    """patterns: list of PatDef whose grids are already in `units`.
    Returns the file text. Each pattern carries its own ;%TYPE line
    (Revit reads it per pattern)."""
    out = [u";%UNITS=" + ("MM" if units == "MM" else "INCH"),
           u";%VERSION=3.0",
           u";  Written by " + credit]
    for p in patterns:
        out.append(u"*{0},{1}".format(clean_pat_name(p.name), (p.desc or u"").replace(u"\n", u" ")))
        out.append(u";%TYPE=" + ("MODEL" if p.target == "MODEL" else "DRAFTING"))
        for g in p.grids:
            out.append(format_grid(g))
    return u"\n".join(out) + u"\n"


# --------------------------------------------------------------------------
# Transforms and Revit normal form
# --------------------------------------------------------------------------
def _uv(angle_deg):
    a = math.radians(angle_deg)
    return math.cos(a), math.sin(a)


def transform_grids(grids, scale=1.0, rotation=0.0, flip_h=False, flip_v=False):
    """p' = scale * R(rotation) * F * p, F mirrors x (flip_h) / y (flip_v).
    Works on the geometry, so any combination is exact."""
    fx = -1.0 if flip_h else 1.0
    fy = -1.0 if flip_v else 1.0
    ca, sa = math.cos(math.radians(rotation)), math.sin(math.radians(rotation))

    def lin(x, y):
        x, y = x * fx, y * fy
        return ca * x - sa * y, sa * x + ca * y

    out = []
    for g in grids:
        ux, uy = _uv(g.angle)
        nx, ny = -uy, ux
        sx = g.shift * ux + g.offset * nx
        sy = g.shift * uy + g.offset * ny
        ox, oy = lin(g.ox, g.oy)
        ux2, uy2 = lin(ux, uy)
        sx2, sy2 = lin(sx, sy)
        nx2, ny2 = -uy2, ux2
        shift = (sx2 * ux2 + sy2 * uy2) * scale
        offset = (sx2 * nx2 + sy2 * ny2) * scale
        if offset < 0:   # step -V generates the same set of lines as +V
            shift, offset = -shift, -offset
        ang = math.degrees(math.atan2(uy2, ux2))
        out.append(Grid(ang, ox * scale, oy * scale, shift, offset,
                        [d * scale for d in g.dashes]))
    return out


def normalize_dashes(dashes, dot_len):
    """AutoCAD dash list -> (along, segs) where segs alternate dash/gap,
    start with a dash, have even length and contain no zero, and `along`
    is how far the origin must move along the line to keep the dashes
    where they were. segs == [] means a continuous line; None means the
    line is all gaps (invisible)."""
    if not dashes:
        return 0.0, []
    items = []
    dots = 0
    for d in dashes:
        if d > 0:
            items.append([True, d])
        elif d < 0:
            items.append([False, -d])
        else:
            items.append([True, dot_len])
            dots += 1
    if dots:   # give the dots' length back so the period stays the same
        gaps = [it for it in items if not it[0]]
        if gaps:
            big = max(gaps, key=lambda it: it[1])
            if big[1] > dots * dot_len * 2:
                big[1] -= dots * dot_len

    def merge(lst):
        res = []
        for it in lst:
            if it[1] <= 0:
                continue
            if res and res[-1][0] == it[0]:
                res[-1][1] += it[1]
            else:
                res.append([it[0], it[1]])
        return res

    items = merge(items)
    if not any(it[0] for it in items):
        return None
    if all(it[0] for it in items):
        return 0.0, []
    along = 0.0
    while not items[0][0]:
        along += items[0][1]
        items.append(items.pop(0))
    items = merge(items)
    if items[-1][0] and len(items) > 1:
        last = items.pop()
        along -= last[1]
        items[0][1] += last[1]
    if len(items) % 2:
        items.append([False, 0.0])   # cannot happen after the steps above
    return along, [it[1] for it in items]


def revit_form(g, dot_len=None):
    """Grid -> (angle_rad, ox, oy, shift, offset, segs) ready for a Revit
    FillGrid (same units as the grid), or None if it can't be drawn."""
    if abs(g.offset) < 1e-12:
        return None
    shift, offset = g.shift, g.offset
    if offset < 0:
        shift, offset = -shift, -offset
    period = sum(abs(d) for d in g.dashes)
    if dot_len is None:
        dot_len = period * 0.01 if period > 0 else offset * 0.05
    norm = normalize_dashes(g.dashes, max(dot_len, 1e-9))
    if norm is None:
        return None
    along, segs = norm
    ux, uy = _uv(g.angle)
    ox, oy = g.ox + along * ux, g.oy + along * uy
    if segs:
        p = sum(segs)
        if p > 0:
            shift = shift % p
    else:
        shift = 0.0
    return math.radians(g.angle), ox, oy, shift, offset, segs


def grids_from_revit(rows):
    """[(angle_rad, ox, oy, shift, offset, segs)] -> [Grid] (signed dashes)."""
    out = []
    for ang, ox, oy, sh, off, segs in rows:
        dashes = [s if i % 2 == 0 else -s for i, s in enumerate(segs or [])]
        out.append(Grid(math.degrees(ang), ox, oy, sh, off, dashes))
    return out


# --------------------------------------------------------------------------
# Lines -> PAT (the Make Pattern solver)
# --------------------------------------------------------------------------
def _gcd(a, b):
    a, b = abs(a), abs(b)
    while b:
        a, b = b, a % b
    return a


def _egcd(a, b):
    """x, y with a*x + b*y = gcd(|a|, |b|), for any signs."""
    old_r, r = abs(a), abs(b)
    old_s, s = 1, 0
    old_t, t = 0, 1
    while r:
        q = old_r // r
        old_r, r = r, old_r - q * r
        old_s, s = s, old_s - q * s
        old_t, t = t, old_t - q * t
    x = old_s * (1 if a >= 0 else -1)
    y = old_t * (1 if b >= 0 else -1)
    return x, y


class _Lattice(object):
    """All directions (a*W, b*H) a W x H tile can repeat along, sorted
    by angle in [0, pi)."""

    def __init__(self, W, H, max_mult):
        self.W, self.H = float(W), float(H)
        n = max(1, int(max_mult))
        dirs = []
        for b in range(0, n + 1):
            for a in range(-n, n + 1):
                if b == 0 and a != 1:
                    continue
                if a == 0 and b != 1:
                    continue
                if _gcd(a, b) != 1:
                    continue
                ang = math.atan2(b * self.H, a * self.W)
                dirs.append((ang, math.hypot(a * self.W, b * self.H), a, b))
        dirs.sort()
        self.dirs = dirs
        self.angles = [d[0] for d in dirs]
        self._fam = {}

    def snap(self, phi, tol=1e-4):
        """Nearest lattice direction to angle phi (radians, any value).
        Among directions within `tol` of phi the shortest period wins."""
        phi = phi % math.pi
        i = bisect.bisect_left(self.angles, phi)
        cands = []
        n = len(self.dirs)
        for j in (i - 2, i - 1, i, i + 1):
            if 0 <= j < n:
                cands.append((abs(self.angles[j] - phi), j))
        # wrap-around: direction 0 is also direction pi
        cands.append((abs(self.angles[0] + math.pi - phi), 0))
        cands.append((abs(phi + math.pi - self.angles[-1]), n - 1))
        best_err, best = min(cands)
        near = [(self.dirs[j][1], j) for e, j in cands if e <= tol]
        if near:
            best = min(near)[1]
            best_err = min(e for e, j in cands if j == best)
        return self.dirs[best], best_err

    def family(self, a, b):
        key = (a, b)
        f = self._fam.get(key)
        if f is None:
            W, H = self.W, self.H
            L = math.hypot(a * W, b * H)
            ang = math.atan2(b * H, a * W)
            d = W * H / L
            x, y = _egcd(a, b)
            j, i = x, -y                     # j*a - i*b = 1
            shift = ((i * a * W * W + j * b * H * H) / L) % L
            f = (ang, L, d, shift)
            self._fam[key] = f
        return f


def lines_to_grids(segments, W, H, max_mult=8):
    """segments: [(x1, y1, x2, y2)] in tile coordinates (origin = tile's
    bottom-left corner, y up). Returns (grids, info) where info holds
    the worst angle correction (degrees) and counts."""
    W, H = float(W), float(H)
    if W <= 0 or H <= 0:
        raise ValueError("Tile width and height must be positive.")
    lat = _Lattice(W, H, max_mult)
    size = max(W, H)
    eps = size * 1e-9
    groups = {}          # (a, b) -> {key: [r, [(t, len)]]}
    worst = 0.0
    used = 0
    for x1, y1, x2, y2 in segments:
        dx, dy = x2 - x1, y2 - y1
        ln = math.hypot(dx, dy)
        if ln <= eps:
            continue
        (ang, L, a, b), err = lat.snap(math.atan2(dy, dx))
        worst = max(worst, err)
        used += 1
        _ang, L, d, shift = lat.family(a, b)
        ux, uy = math.cos(ang), math.sin(ang)
        nx, ny = -uy, ux
        cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
        sx, sy = cx - ux * ln / 2.0, cy - uy * ln / 2.0
        q = sx * nx + sy * ny
        k = math.floor(q / d)
        r = q - k * d
        if d - r < d * 1e-7:      # r ~ d: same line as r = 0 one step up
            r -= d
            k += 1
        if r < 0:
            r = 0.0
        t = (sx * ux + sy * uy) - k * shift
        key = int(round(r / d * 1e6)) % 1000000
        fam = groups.setdefault((a, b), {})
        entry = fam.setdefault(key, [r, []])
        entry[1].append((t, ln))

    grids = []
    for (a, b), fam in sorted(groups.items()):
        ang, L, d, shift = lat.family(a, b)
        ux, uy = math.cos(ang), math.sin(ang)
        nx, ny = -uy, ux
        for key in sorted(fam):
            r, items = fam[key]
            dashes, start = _union_dashes(items, L)
            ox = r * nx + start * ux
            oy = r * ny + start * uy
            grids.append(Grid(math.degrees(ang), ox, oy, shift, d, dashes))
    return grids, {"max_angle_error": math.degrees(worst), "segments": used,
                   "families": len(groups), "grids": len(grids)}


def _union_dashes(items, L):
    """items: [(t, length)] on one line of period L -> (dashes, start)."""
    eps = L * 1e-9
    ivs = []
    for t, ln in items:
        if ln >= L - eps:
            return [], 0.0
        s = t % L
        e = s + ln
        if e <= L + eps:
            ivs.append((s, min(e, L)))
        else:
            ivs.append((s, L))
            ivs.append((0.0, e - L))
    ivs.sort()
    merged = []
    for s, e in ivs:
        if merged and s <= merged[-1][1] + eps:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    if len(merged) == 1 and merged[0][0] <= eps and merged[0][1] >= L - eps:
        return [], 0.0
    if len(merged) > 1 and merged[0][0] <= eps and merged[-1][1] >= L - eps:
        first = merged.pop(0)
        merged[-1][1] = L + first[1]
    start = merged[0][0]
    dashes = []
    for i, (s, e) in enumerate(merged):
        nxt = merged[i + 1][0] if i + 1 < len(merged) else merged[0][0] + L
        dashes.append(e - s)
        dashes.append(-(nxt - e))
    # a gap that is ~0 after merging would make Revit unhappy: fold it
    clean = []
    for v in dashes:
        if clean and v < 0 and -v <= eps:
            continue
        if clean and v > 0 and clean[-1] > 0:
            clean[-1] += v
            continue
        clean.append(v)
    if len(clean) >= 2 and clean[-1] > 0 and clean[0] > 0:
        clean[0] += clean.pop()
    if not any(v < 0 for v in clean):
        return [], 0.0
    return clean, start


# --------------------------------------------------------------------------
# Image -> lines
# --------------------------------------------------------------------------
def mask_from_strings(rows, ink="#"):
    """Test helper: ['.#.', '###'] -> mask (top row first)."""
    return [[c == ink for c in r] for r in rows]


def trace_outlines(mask, wrap=True):
    """Boundaries of the ink areas of a tile that REPEATS (the left edge
    touches the right one, top touches bottom). Returns polylines in
    pixel units, y up, origin bottom-left; each is a list of (x, y)
    corner points. A polyline that wraps around the whole tile ends one
    tile away from where it started.

    wrap=False traces a single picture instead: outside the image is
    blank, every polyline is a closed loop (first point == last), ink
    outlines run counter-clockwise and holes clockwise."""
    h = len(mask)
    w = len(mask[0]) if h else 0
    if not w or not h:
        return []
    if wrap:
        def ink(x, y):
            return mask[y % h][x % w]

        def vkey(vx, vy):
            return (vx % w, vy % h)
    else:
        def ink(x, y):
            return 0 <= x < w and 0 <= y < h and mask[y][x]

        def vkey(vx, vy):
            return (vx, vy)

    out_edges = {}

    def add(vx, vy, dx, dy):
        out_edges.setdefault(vkey(vx, vy), []).append((dx, dy))

    for y in range(h):
        Y0 = h - 1 - y
        row = mask[y]
        for x in range(w):
            if not row[x]:
                continue
            if not ink(x, y + 1):
                add(x, Y0, 1, 0)
            if not ink(x + 1, y):
                add(x + 1, Y0, 0, 1)
            if not ink(x, y - 1):
                add(x + 1, Y0 + 1, -1, 0)
            if not ink(x - 1, y):
                add(x, Y0 + 1, 0, -1)

    loops = []
    for start in sorted(out_edges):
        while out_edges.get(start):
            cx, cy = start
            pts = [(cx, cy)]
            d = out_edges[start].pop(0)
            guard = 0
            while True:
                cx, cy = cx + d[0], cy + d[1]
                pts.append((cx, cy))
                key = vkey(cx, cy)
                if key == start:
                    break
                outs = out_edges.get(key)
                if not outs:
                    break
                d = _pick_turn(d, outs)
                guard += 1
                if guard > 8 * w * h + 8:
                    break
            loops.append(_compress(pts))
    return loops


def _pick_turn(d, outs):
    """At a corner shared by two diagonal ink pixels there are two ways
    on; turn left first so diagonal neighbours stay separate shapes."""
    left = (-d[1], d[0])
    for want in (left, d, (d[1], -d[0])):
        if want in outs:
            outs.remove(want)
            return want
    return outs.pop(0)


def _compress(pts):
    """Drop points in the middle of straight runs."""
    if len(pts) < 3:
        return pts
    res = [pts[0]]
    for i in range(1, len(pts) - 1):
        ax, ay = res[-1]
        bx, by = pts[i]
        cx, cy = pts[i + 1]
        if (bx - ax) * (cy - by) - (by - ay) * (cx - bx) != 0:
            res.append(pts[i])
    res.append(pts[-1])
    return res


def simplify(pts, tol):
    """Douglas-Peucker (iterative). Closed loops (first == last) are
    split at the point farthest from the start so both ends stay put."""
    if tol <= 0 or len(pts) < 3:
        return list(pts)
    if pts[0] == pts[-1] and len(pts) > 3:
        x0, y0 = pts[0]
        far = max(range(1, len(pts) - 1), key=lambda i: (pts[i][0] - x0) ** 2 + (pts[i][1] - y0) ** 2)
        a = _dp(pts[:far + 1], tol)
        b = _dp(pts[far:], tol)
        return a[:-1] + b
    return _dp(pts, tol)


def _dp(pts, tol):
    n = len(pts)
    if n < 3:
        return list(pts)
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        i0, i1 = stack.pop()
        ax, ay = pts[i0]
        bx, by = pts[i1]
        dx, dy = bx - ax, by - ay
        L = math.hypot(dx, dy)
        best, bi = -1.0, -1
        for i in range(i0 + 1, i1):
            px, py = pts[i]
            if L < 1e-12:
                dist = math.hypot(px - ax, py - ay)
            else:
                dist = abs(dx * (ay - py) - (ax - px) * dy) / L
            if dist > best:
                best, bi = dist, i
        if bi > 0 and best > tol:
            keep[bi] = True
            stack.append((i0, bi))
            stack.append((bi, i1))
    return [p for p, k in zip(pts, keep) if k]


def polylines_to_segments(polys):
    segs = []
    for p in polys:
        for i in range(len(p) - 1):
            (x1, y1), (x2, y2) = p[i], p[i + 1]
            if (x1, y1) != (x2, y2):
                segs.append((float(x1), float(y1), float(x2), float(y2)))
    return segs


def split_touching(loop):
    """A closed loop (first == last) that passes the same corner twice
    (two pixels of one shape touching diagonally) -> several simple
    closed loops, cut at that corner. Revit's sketches reject loops that
    touch themselves."""
    pts = list(loop[:-1]) if loop and loop[0] == loop[-1] else list(loop)
    out, stack, seen = [], [], {}
    for p in pts:
        if p in seen:
            i = seen[p]
            sub = stack[i:]
            out.append(sub + [p])
            for q in sub[1:]:
                del seen[q]
            stack = stack[:i + 1]
        else:
            seen[p] = len(stack)
            stack.append(p)
    if len(stack) >= 3:
        out.append(stack + [stack[0]])
    return [l for l in out if len(l) >= 4]


def loop_area(loop):
    """Signed area (shoelace): > 0 counter-clockwise."""
    a = 0.0
    for i in range(len(loop) - 1):
        a += loop[i][0] * loop[i + 1][1] - loop[i + 1][0] * loop[i][1]
    return a / 2.0


def point_in_loop(x, y, loop):
    inside = False
    for i in range(len(loop) - 1):
        x1, y1 = loop[i]
        x2, y2 = loop[i + 1]
        if (y1 > y) != (y2 > y):
            if x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
    return inside


def _ink_side_point(loop):
    """A point just beside the first edge, on its ink (left) side."""
    (x1, y1), (x2, y2) = loop[0], loop[1]
    L = math.hypot(x2 - x1, y2 - y1) or 1.0
    return (x1 + x2) / 2.0 - (y2 - y1) / L * 1e-3, (y1 + y2) / 2.0 + (x2 - x1) / L * 1e-3


def image_shapes(mask, smooth=0.75, min_area=0.0):
    """A single (non-repeating) picture -> [(outer_loop, [hole_loops])]
    in pixel units, y up. Ink outlines are outers, the white islands
    inside them are holes; specks below min_area pixels are dropped.
    Loops are closed (first == last) and simplified by `smooth` px."""
    loops = []
    for l in trace_outlines(mask, wrap=False):
        loops.extend(split_touching(l))
    outers, holes = [], []
    for l in loops:
        a = loop_area(l)
        if abs(a) < max(min_area, 1e-9):
            continue
        (outers if a > 0 else holes).append((abs(a), l))
    outers.sort()
    shapes = [[o, []] for _a, o in outers]
    for _a, h in holes:
        px, py = _ink_side_point(h)
        for s in shapes:          # smallest containing outer first
            if point_in_loop(px, py, s[0]):
                s[1].append(h)
                break

    def simp(l):
        s = simplify(l, smooth)
        return s if len(s) >= 4 and abs(loop_area(s)) > 1e-9 else l

    return [(simp(o), [simp(h) for h in hs]) for o, hs in shapes]


def scan_fill(mask, step=1):
    """Horizontal hatch lines through the ink, one per `step` pixel rows
    (through each row's centre). Pixel units, y up."""
    h = len(mask)
    w = len(mask[0]) if h else 0
    segs = []
    step = max(1, int(step))
    for y in range(0, h, step):
        Y = h - 1 - y + 0.5
        row = mask[y]
        x = 0
        while x < w:
            if row[x]:
                x0 = x
                while x < w and row[x]:
                    x += 1
                segs.append((float(x0), Y, float(x), Y))
            else:
                x += 1
    return segs


def image_segments(mask, mode="outline", simplify_tol=0.75, fill_step=1):
    """mode: 'outline', 'fill' or 'both'. Pixel units, tile = w x h."""
    segs = []
    if mode in ("outline", "both"):
        polys = [simplify(p, simplify_tol) for p in trace_outlines(mask)]
        segs.extend(polylines_to_segments(polys))
    if mode in ("fill", "both"):
        segs.extend(scan_fill(mask, fill_step))
    return segs


# --------------------------------------------------------------------------
# Preview
# --------------------------------------------------------------------------
def _clip(x1, y1, x2, y2, xmin, ymin, xmax, ymax):
    """Liang-Barsky. Returns the clipped segment or None."""
    dx, dy = x2 - x1, y2 - y1
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x1 - xmin), (dx, xmax - x1), (-dy, y1 - ymin), (dy, ymax - y1)):
        if p == 0:
            if q < 0:
                return None
        else:
            t = q / p
            if p < 0:
                if t > t1:
                    return None
                if t > t0:
                    t0 = t
            else:
                if t < t0:
                    return None
                if t < t1:
                    t1 = t
    return (x1 + t0 * dx, y1 + t0 * dy, x1 + t1 * dx, y1 + t1 * dy)


def preview_segments(rgrids, x0, y0, x1, y1, max_segments=30000):
    """rgrids: revit_form() tuples. Returns (segments, truncated) for the
    rectangle (x0, y0)-(x1, y1)."""
    out = []
    truncated = False
    corners = ((x0, y0), (x1, y0), (x0, y1), (x1, y1))
    for rg in rgrids:
        if rg is None:
            continue
        ang, ox, oy, sh, d, segs = rg
        if d <= 0:
            continue
        ux, uy = math.cos(ang), math.sin(ang)
        nx, ny = -uy, ux
        qs = [(cx - ox) * nx + (cy - oy) * ny for cx, cy in corners]
        kmin = int(math.floor(min(qs) / d))
        kmax = int(math.ceil(max(qs) / d))
        if kmax - kmin > max_segments:
            truncated = True
            kmax = kmin + max_segments
        period = sum(segs) if segs else 0.0
        stepx, stepy = sh * ux + d * nx, sh * uy + d * ny
        for k in range(kmin, kmax + 1):
            bx, by = ox + k * stepx, oy + k * stepy
            ts = [(cx - bx) * ux + (cy - by) * uy for cx, cy in corners]
            tmin, tmax = min(ts), max(ts)
            if not segs:
                c = _clip(bx + tmin * ux, by + tmin * uy, bx + tmax * ux, by + tmax * uy, x0, y0, x1, y1)
                if c:
                    out.append(c)
            else:
                if period <= 0 or (tmax - tmin) / period * len(segs) > max_segments:
                    truncated = True
                    continue
                t = math.floor(tmin / period) * period
                while t < tmax:
                    for i in range(0, len(segs), 2):
                        a, b = t, t + segs[i]
                        if b > tmin and a < tmax:
                            c = _clip(bx + a * ux, by + a * uy, bx + b * ux, by + b * uy, x0, y0, x1, y1)
                            if c:
                                out.append(c)
                        t += segs[i] + (segs[i + 1] if i + 1 < len(segs) else 0.0)
            if len(out) > max_segments:
                return out, True
    return out, truncated


def natural_size(rgrids):
    """A length that shows a few repeats of the pattern: the largest
    repeat cell side (sqrt(offset * period)) over its lines."""
    best = 0.0
    for rg in rgrids:
        if rg is None:
            continue
        d, segs = rg[4], rg[5]
        p = sum(segs) if segs else d
        best = max(best, math.sqrt(abs(d) * p), abs(d))
    return best or 1.0
