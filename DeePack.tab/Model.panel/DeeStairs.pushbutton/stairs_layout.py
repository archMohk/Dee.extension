# -*- coding: utf-8 -*-
"""
stairs_layout
The plan geometry behind DeeStairs - pure Python, no Revit imports, so it is
tested outside Revit (CPython and pyRevit's IronPython 2.7) and script.py only
has to turn the result into Revit runs and landings.

Everything is worked out in a LOCAL frame, in feet:

    origin (0, 0)  = the centre line of the first run, at its first riser
    +x             = the direction of travel of the first run
    +y             = to the LEFT of that direction

A left turn is built first; a right turn is the same plan mirrored in y.

What a plan holds
-----------------
Run       one Revit stairs run. kind is
            'straight' - CreateStraightRun   (p0 -> p1 is its centre line)
            'spiral'   - CreateSpiralRun     (spiral = centre / radius / angles)
            'sketched' - CreateSketchedRun   (winders: left / right boundary
                         chains, riser lines and walking line as line / arc
                         segments)
          step0 = how many risers lie below the run, so its base elevation is
          step0 x riser height.
Landing   a closed polygon between two runs, at step height `step`.

How many treads a run covers
----------------------------
A run with n risers has n - 1 treads when it starts and ends with a riser
(the landing or floor above is the last "tread"); starting or ending on a tread
instead adds one each:

    treads = risers - begins_with_riser - ends_with_riser + 1

and the run's centre line is treads x tread-depth long. script.py checks the
risers Revit really made (Run.ActualRisersNumber) and feeds any difference back
through `extras` (one integer per run, added to its treads), so a wrong guess
here is corrected on the first run rather than leaving the stairs incomplete.
"""
import math

SHAPES = ("straight", "straight_landing", "l_shape", "u_shape",
          "winder_l", "winder_u", "spiral")

_PI = math.pi
_HALF_PI = math.pi / 2.0


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------
class Run(object):
    def __init__(self, kind, risers, step0, begin=True, end=True):
        self.kind = kind
        self.risers = risers
        self.step0 = step0
        self.begin = begin              # begins with a riser (straight / spiral)
        self.end = end                  # ends with a riser   (straight / spiral)
        self.p0 = None                  # straight: centre line start
        self.p1 = None                  # straight: centre line end
        self.spiral = None              # dict(center, rc, a0, sweep, ccw)
        self.sketch = None              # dict(left, right, risers, path)
        self.outline = []               # polygon, for the preview
        self.tread_lines = []           # [((x, y), (x, y)), ...]
        self.center = []                # walking line, in travel order
        self.length = 0.0               # walking-line length


class Landing(object):
    def __init__(self, polygon, step):
        self.polygon = polygon          # counter-clockwise, closed implicitly
        self.step = step                # risers below (its top is at step x riser)


class Plan(object):
    def __init__(self, shape, turn):
        self.shape = shape
        self.turn = turn
        self.runs = []
        self.landings = []
        self.errors = []
        self.warnings = []
        self.notes = []                 # plain facts for the summary line
        self.tread = 0.0                # tread depth the plan was built with
        self.winder_tread = None        # winders: tread at the walking line
        self.width = 0.0
        self.total_risers = 0
        self.bbox = (0.0, 0.0, 1.0, 1.0)

    @property
    def ok(self):
        return not self.errors

    @property
    def walk_length(self):
        return sum(r.length for r in self.runs)

    def flights(self):
        return [r.risers for r in self.runs]


def treads_for(risers, begin=True, end=True):
    return risers - (1 if begin else 0) - (1 if end else 0) + 1


# --------------------------------------------------------------------------
# small geometry helpers
# --------------------------------------------------------------------------
def _rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _arc_pts(center, r, a0, a1, step_deg=6.0):
    """Points along an arc from angle a0 to a1 (radians, either direction)."""
    sweep = a1 - a0
    n = max(2, int(math.ceil(abs(math.degrees(sweep)) / step_deg)))
    return [(center[0] + r * math.cos(a0 + sweep * i / float(n)),
             center[1] + r * math.sin(a0 + sweep * i / float(n))) for i in range(n + 1)]


def _seg_line(p, q):
    return ("L", p, q)


def _seg_arc(center, r, a0, a1):
    am = (a0 + a1) / 2.0
    return ("A",
            (center[0] + r * math.cos(a0), center[1] + r * math.sin(a0)),
            (center[0] + r * math.cos(am), center[1] + r * math.sin(am)),
            (center[0] + r * math.cos(a1), center[1] + r * math.sin(a1)))


def _seg_points(seg):
    """Points of a segment, for outlines and tests."""
    if seg[0] == "L":
        return [seg[1], seg[2]]
    c, r, a0, a1 = _circle_from_3(seg[1], seg[2], seg[3])
    return _arc_pts(c, r, a0, a1, 6.0)


def _circle_from_3(p, m, q):
    ax, ay = p
    bx, by = m
    cx, cy = q
    d = 2.0 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) +
          (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) +
          (cx * cx + cy * cy) * (bx - ax)) / d
    r = math.hypot(ax - ux, ay - uy)
    a0 = math.atan2(ay - uy, ax - ux)
    am = math.atan2(by - uy, bx - ux)
    a1 = math.atan2(cy - uy, cx - ux)
    # take the sweep that passes through the middle point
    s = a1 - a0
    while s <= -_PI:
        s += 2 * _PI
    while s > _PI:
        s -= 2 * _PI
    mid = a0 + s / 2.0
    dm = (am - mid + _PI) % (2 * _PI) - _PI
    if abs(dm) > 1e-6:
        s = s - 2 * _PI if s > 0 else s + 2 * _PI
    return (ux, uy), r, a0, a0 + s


def polygon_area(poly):
    a = 0.0
    for i in range(len(poly)):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % len(poly)]
        a += x0 * y1 - x1 * y0
    return a / 2.0


def _chain_points(segs):
    pts = []
    for s in segs:
        sp = _seg_points(s)
        if pts and _dist(pts[-1], sp[0]) < 1e-9:
            sp = sp[1:]
        pts.extend(sp)
    return pts


def _drop_short(segs, tol=1e-6):
    out = []
    for s in segs:
        if s[0] == "L" and _dist(s[1], s[2]) < tol:
            continue
        out.append(s)
    return out


# --------------------------------------------------------------------------
# straight run (also used by the landing shapes)
# --------------------------------------------------------------------------
def _straight_run(origin, heading, risers, step0, begin, end, T, W, extra=0):
    r = Run("straight", risers, step0, begin, end)
    tr = treads_for(risers, begin, end) + extra
    length = max(tr, 1) * T
    ux, uy = math.cos(heading), math.sin(heading)
    nx, ny = -uy, ux                                  # left normal
    hw = W / 2.0
    p0 = origin
    p1 = (p0[0] + ux * length, p0[1] + uy * length)
    r.p0, r.p1 = p0, p1
    r.length = length
    r.center = [p0, p1]
    r.outline = [(p0[0] + nx * hw, p0[1] + ny * hw), (p1[0] + nx * hw, p1[1] + ny * hw),
                 (p1[0] - nx * hw, p1[1] - ny * hw), (p0[0] - nx * hw, p0[1] - ny * hw)]
    lines = []
    for i in range(risers):
        s = (i + (0 if begin else 1)) * T
        c = (p0[0] + ux * s, p0[1] + uy * s)
        lines.append(((c[0] - nx * hw, c[1] - ny * hw), (c[0] + nx * hw, c[1] + ny * hw)))
    r.tread_lines = lines
    return r


# --------------------------------------------------------------------------
# winders: one sketched run with fanned risers around each corner
# --------------------------------------------------------------------------
def _corner_hit_left_turn_a(pivot, theta, W):
    """Corner of a heading +x flight turning left (pivot = inner corner, at the
    left end of the entering line). Ray at theta from straight down (-y) toward
    +x, to the outer edge of the W x W corner square."""
    d = (math.sin(theta), -math.cos(theta))
    if theta <= _PI / 4.0 + 1e-12:
        t = W / max(math.cos(theta), 1e-12)
    else:
        t = W / max(math.sin(theta), 1e-12)
    return (pivot[0] + d[0] * t, pivot[1] + d[1] * t)


def _corner_hit_left_turn_b(pivot, theta, W):
    """Second corner of a U winder: enters heading +y at the line y = pivot.y,
    pivot = left end of that line. Ray at theta from +x toward +y."""
    d = (math.cos(theta), math.sin(theta))
    if theta <= _PI / 4.0 + 1e-12:
        t = W / max(math.cos(theta), 1e-12)
    else:
        t = W / max(math.sin(theta), 1e-12)
    return (pivot[0] + d[0] * t, pivot[1] + d[1] * t)


def _winder_run(n, T, W, k, a, b, m, u_shape):
    """Winder L (u_shape False) or U (True), left turn. a straight risers
    before the first corner, b after the last; m = extra straight risers
    between the two corners of a U."""
    hw = W / 2.0
    xc = a * T
    p1 = (xc, hw)                                     # inner corner of corner 1
    risers = []
    # every riser runs from the LEFT boundary chain to the RIGHT one
    for i in range(a):
        x = i * T
        risers.append(((x, hw), (x, -hw)))
    for j in range(k + 1):
        th = _HALF_PI * j / float(k)
        risers.append((p1, _corner_hit_left_turn_a(p1, th, W)))

    left = []
    right = []
    path = []
    if not u_shape:
        y_end = hw + b * T
        for j in range(1, b + 1):
            y = hw + j * T
            risers.append(((xc, y), (xc + W, y)))
        left = [_seg_line((0.0, hw), (xc, hw)), _seg_line((xc, hw), (xc, y_end))]
        right = [_seg_line((0.0, -hw), (xc + W, -hw)), _seg_line((xc + W, -hw), (xc + W, y_end))]
        path = [_seg_line((0.0, 0.0), (xc, 0.0)),
                _seg_arc(p1, hw, -_HALF_PI, 0.0),
                _seg_line((xc + hw, hw), (xc + hw, y_end))]
    else:
        for j in range(1, m + 1):
            y = hw + j * T
            risers.append(((xc, y), (xc + W, y)))
        y_a = hw + (m + 1) * T
        p2 = (xc, y_a)
        for j in range(k + 1):
            th = _HALF_PI * j / float(k)
            risers.append((p2, _corner_hit_left_turn_b(p2, th, W)))
        for j in range(1, b + 1):
            x = xc - j * T
            risers.append(((x, y_a), (x, y_a + W)))
        x_end = xc - b * T
        left = [_seg_line((0.0, hw), (xc, hw)), _seg_line((xc, hw), (xc, y_a)),
                _seg_line((xc, y_a), (x_end, y_a))]
        right = [_seg_line((0.0, -hw), (xc + W, -hw)), _seg_line((xc + W, -hw), (xc + W, y_a + W)),
                 _seg_line((xc + W, y_a + W), (x_end, y_a + W))]
        path = [_seg_line((0.0, 0.0), (xc, 0.0)),
                _seg_arc(p1, hw, -_HALF_PI, 0.0),
                _seg_line((xc + hw, hw), (xc + hw, y_a)),
                _seg_arc(p2, hw, 0.0, _HALF_PI),
                _seg_line((xc, y_a + hw), (x_end, y_a + hw))]

    left = _drop_short(left)
    right = _drop_short(right)
    path = _drop_short(path)

    r = Run("sketched", n, 0, True, True)
    r.sketch = {"left": left, "right": right,
                "risers": [_seg_line(p, q) for p, q in risers], "path": path}
    r.tread_lines = risers
    lp = _chain_points(left)
    rp = _chain_points(right)
    r.outline = lp + list(reversed(rp))
    r.center = _chain_points(path)
    length = 0.0
    for s in path:
        pts = _seg_points(s)
        for i in range(len(pts) - 1):
            length += _dist(pts[i], pts[i + 1])
    r.length = length
    return r


# --------------------------------------------------------------------------
# spiral
# --------------------------------------------------------------------------
def _spiral_run(n, T, W, rc, sweep, begin, end, extra=0):
    r = Run("spiral", n, 0, begin, end)
    tr = max(treads_for(n, begin, end) + extra, 1)
    if sweep is None:
        sweep = tr * T / rc
        tread = T
    else:
        tread = rc * sweep / tr
    center = (0.0, rc)
    a0 = -_HALF_PI
    r.spiral = {"center": center, "rc": rc, "a0": a0, "sweep": sweep, "ccw": True,
                "tread": tread}
    hw = W / 2.0
    inner = max(rc - hw, 1e-6)
    outer = rc + hw
    r.outline = _arc_pts(center, inner, a0, a0 + sweep) + \
        list(reversed(_arc_pts(center, outer, a0, a0 + sweep)))
    lines = []
    for i in range(n):
        ang = a0 + (i + (0 if begin else 1)) * tread / rc
        lines.append(((center[0] + inner * math.cos(ang), center[1] + inner * math.sin(ang)),
                      (center[0] + outer * math.cos(ang), center[1] + outer * math.sin(ang))))
    r.tread_lines = lines
    r.center = _arc_pts(center, rc, a0, a0 + sweep)
    r.length = rc * sweep
    return r, tread


# --------------------------------------------------------------------------
# the planner
# --------------------------------------------------------------------------
def _split_two(n, f1, plan):
    """Risers of the first and second flight."""
    if n < 4:
        plan.errors.append(u"Two flights need at least 4 risers (this stair has {0}).".format(n))
        return n, 0
    n1 = (n + 1) // 2 if not f1 else int(f1)
    if n1 < 2 or n - n1 < 2:
        plan.warnings.append(u"First flight set to {0} risers is out of range - using a balanced split."
                             .format(n1))
        n1 = (n + 1) // 2
    return n1, n - n1


def _bbox(plan):
    xs = []
    ys = []
    for r in plan.runs:
        for p in r.outline:
            xs.append(p[0])
            ys.append(p[1])
    for l in plan.landings:
        for p in l.polygon:
            xs.append(p[0])
            ys.append(p[1])
    if not xs:
        return (0.0, 0.0, 1.0, 1.0)
    return (min(xs), min(ys), max(xs), max(ys))


def _mirror_pt(p):
    return (p[0], -p[1])


def _mirror_seg(s):
    return tuple([s[0]] + [_mirror_pt(p) for p in s[1:]])


def _mirror(plan):
    for r in plan.runs:
        if r.p0 is not None:
            r.p0 = _mirror_pt(r.p0)
            r.p1 = _mirror_pt(r.p1)
        r.outline = [_mirror_pt(p) for p in r.outline]
        r.tread_lines = [(_mirror_pt(p), _mirror_pt(q)) for p, q in r.tread_lines]
        r.center = [_mirror_pt(p) for p in r.center]
        if r.spiral is not None:
            c = r.spiral["center"]
            r.spiral["center"] = _mirror_pt(c)
            r.spiral["a0"] = -r.spiral["a0"]
            r.spiral["ccw"] = False
        if r.sketch is not None:
            for key in ("left", "right", "risers", "path"):
                r.sketch[key] = [_mirror_seg(s) for s in r.sketch[key]]
            # a mirrored left/right pair swaps sides, and so does every riser
            # (they still run from the left chain to the right one)
            r.sketch["left"], r.sketch["right"] = r.sketch["right"], r.sketch["left"]
            r.sketch["risers"] = [("L", s[2], s[1]) for s in r.sketch["risers"]]
    for l in plan.landings:
        l.polygon = [_mirror_pt(p) for p in reversed(l.polygon)]


def make_plan(shape, n, T, W, turn="left", begin=True, end=True, flight1=None,
              landing=None, gap=0.0, winders=3, radius=None, sweep=None, extras=None):
    """Plan for `n` risers.

    T, W, landing, gap, radius are feet; sweep is radians (None = from the
    risers and tread depth); extras = one integer per run, added to its tread
    count (see the module docstring)."""
    plan = Plan(shape, turn)
    plan.width = W
    plan.tread = T
    plan.total_risers = n
    ex = list(extras or [])
    while len(ex) < 4:
        ex.append(0)
    if n < 2:
        plan.errors.append(u"A stair needs at least 2 risers.")
        return plan
    if T <= 0 or W <= 0:
        plan.errors.append(u"Tread depth and run width must be greater than zero.")
        return plan
    if shape not in SHAPES:
        plan.errors.append(u"Unknown shape '{0}'.".format(shape))
        return plan

    D = landing if landing else W
    if shape == "straight":
        plan.runs.append(_straight_run((0.0, 0.0), 0.0, n, 0, begin, end, T, W, ex[0]))

    elif shape == "straight_landing":
        n1, n2 = _split_two(n, flight1, plan)
        if plan.ok:
            D = max(D, W * 0.5)
            r1 = _straight_run((0.0, 0.0), 0.0, n1, 0, begin, True, T, W, ex[0])
            x1 = r1.length
            plan.runs.append(r1)
            plan.landings.append(Landing(_rect(x1, -W / 2.0, x1 + D, W / 2.0), n1))
            plan.runs.append(_straight_run((x1 + D, 0.0), 0.0, n2, n1, True, end, T, W, ex[1]))
            plan.notes.append(u"landing {0:.0f} mm deep".format(D * 304.8))

    elif shape == "l_shape":
        n1, n2 = _split_two(n, flight1, plan)
        if plan.ok:
            if D < W:
                plan.warnings.append(u"Landing depth raised to the run width.")
                D = W
            r1 = _straight_run((0.0, 0.0), 0.0, n1, 0, begin, True, T, W, ex[0])
            x1 = r1.length
            plan.runs.append(r1)
            plan.landings.append(Landing(_rect(x1, -W / 2.0, x1 + D, W / 2.0), n1))
            plan.runs.append(_straight_run((x1 + D - W / 2.0, W / 2.0), _HALF_PI, n2, n1, True, end,
                                           T, W, ex[1]))

    elif shape == "u_shape":
        n1, n2 = _split_two(n, flight1, plan)
        if plan.ok:
            if D < W:
                plan.warnings.append(u"Landing depth raised to the run width.")
                D = W
            gap = max(gap, 0.0)
            yc2 = W + gap
            r1 = _straight_run((0.0, 0.0), 0.0, n1, 0, begin, True, T, W, ex[0])
            x1 = r1.length
            plan.runs.append(r1)
            plan.landings.append(Landing(_rect(x1, -W / 2.0, x1 + D, yc2 + W / 2.0), n1))
            plan.runs.append(_straight_run((x1, yc2), _PI, n2, n1, True, end, T, W, ex[1]))
            plan.notes.append(u"gap between flights {0:.0f} mm".format(gap * 304.8))

    elif shape in ("winder_l", "winder_u"):
        k = int(winders)
        if k < 2:
            k = 2
        u = shape == "winder_u"
        m = 0
        corners = 2 if u else 1
        if u:
            m = max(0, int(round(max(gap, 0.0) / T)) - 1)
            plan.notes.append(u"gap between flights {0:.0f} mm".format((m + 1) * T * 304.8))
        rest = n - corners * (k + 1) - m
        if rest < 0:
            plan.errors.append(u"{0} risers are too few for {1} winder{2} of {3} treads - "
                               u"use more risers or fewer winder treads.".format(
                                   n, corners, u"s" if corners > 1 else u"", k))
        else:
            if flight1 is not None and 0 <= int(flight1) <= rest:
                a = int(flight1)
            else:
                if flight1 is not None:
                    plan.warnings.append(u"First flight set to {0} risers is out of range - using a "
                                         u"balanced split.".format(flight1))
                a = rest // 2
            b = rest - a
            plan.runs.append(_winder_run(n, T, W, k, a, b, m, u))
            arc = _PI * W / (4.0 * k)
            plan.notes.append(u"winder treads {0:.0f} mm at the walking line".format(arc * 304.8))
            plan.winder_tread = arc

    elif shape == "spiral":
        if not radius or radius <= 0:
            plan.errors.append(u"Give the spiral a radius.")
        elif radius - W / 2.0 < 0.05:
            plan.errors.append(u"The spiral radius is too small for this width (inner radius "
                               u"{0:.0f} mm).".format((radius - W / 2.0) * 304.8))
        else:
            run, tread = _spiral_run(n, T, W, radius, sweep, begin, end, ex[0])
            plan.runs.append(run)
            plan.tread = tread
            deg = math.degrees(run.spiral["sweep"])
            plan.notes.append(u"sweep {0:.0f} deg".format(deg))
            if deg > 360.0:
                plan.warnings.append(u"The spiral turns more than a full circle - check the headroom.")

    if plan.errors:
        return plan
    if turn == "right" and shape not in ("straight", "straight_landing"):
        _mirror(plan)
    plan.bbox = _bbox(plan)
    return plan


# --------------------------------------------------------------------------
# helpers script.py uses to turn the local plan into world coordinates
# --------------------------------------------------------------------------
def to_world(p, origin, angle):
    c, s = math.cos(angle), math.sin(angle)
    return (origin[0] + p[0] * c - p[1] * s, origin[1] + p[0] * s + p[1] * c)


def shifted_line(p0, p1, offset):
    """The run's location line: its centre line moved `offset` to the left of
    the direction of travel (negative = to the right)."""
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    ln = math.hypot(dx, dy)
    if ln < 1e-12:
        return p0, p1
    nx, ny = -dy / ln, dx / ln
    return (p0[0] + nx * offset, p0[1] + ny * offset), (p1[0] + nx * offset, p1[1] + ny * offset)


def spiral_path_radius(rc, W, side, ccw):
    """Radius of the location arc. side: +1 left edge, 0 centre, -1 right edge."""
    t = 1.0 if ccw else -1.0
    return rc - side * t * W / 2.0
