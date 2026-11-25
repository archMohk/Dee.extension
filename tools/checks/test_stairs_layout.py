# -*- coding: utf-8 -*-
"""Tests for DeeStairs' plan geometry (Model.panel/DeeStairs.pushbutton/
stairs_layout.py). No Revit needed:

    python tools/checks/test_stairs_layout.py

Every shape is built for many risers / tread / width combinations, left and
right, and checked against closed-form areas and for the things Revit refuses
(sketched winder chains that do not connect, risers that miss the boundary,
runs and landings that overlap).
"""
import itertools
import math
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "DeePack.tab", "Model.panel", "DeeStairs.pushbutton"))
import stairs_layout as SL  # noqa: E402

EPS = 1e-6
T = 0.82      # ft ~ 250 mm
W = 3.28      # ft ~ 1000 mm


def area(poly):
    return abs(SL.polygon_area(poly))


def inside(pt, poly):
    x, y = pt
    c = False
    n = len(poly)
    for i in range(n):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % n]
        if (y0 > y) != (y1 > y):
            if x < (x1 - x0) * (y - y0) / (y1 - y0) + x0:
                c = not c
    return c


def seg_cross(a, b, c, d):
    """Proper or touching intersection of segments ab and cd."""
    def orient(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    def on(p, q, r):
        return (min(p[0], q[0]) - EPS <= r[0] <= max(p[0], q[0]) + EPS and
                min(p[1], q[1]) - EPS <= r[1] <= max(p[1], q[1]) + EPS)
    o1, o2, o3, o4 = orient(a, b, c), orient(a, b, d), orient(c, d, a), orient(c, d, b)
    if ((o1 > EPS and o2 < -EPS) or (o1 < -EPS and o2 > EPS)) and \
            ((o3 > EPS and o4 < -EPS) or (o3 < -EPS and o4 > EPS)):
        return True
    return ((abs(o1) <= EPS and on(a, b, c)) or (abs(o2) <= EPS and on(a, b, d)) or
            (abs(o3) <= EPS and on(c, d, a)) or (abs(o4) <= EPS and on(c, d, b)))


def dist_pt_seg(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    l2 = dx * dx + dy * dy
    t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


def dist_pt_poly(p, pts):
    return min(dist_pt_seg(p, pts[i], pts[i + 1]) for i in range(len(pts) - 1))


def grid(plan, step=0.12):
    x0, y0, x1, y1 = plan.bbox
    x = x0
    while x <= x1:
        y = y0
        while y <= y1:
            yield (x + 0.0137, y + 0.0071)      # off-lattice so edges are never hit
            y += step
        x += step


class Counts(unittest.TestCase):
    def test_every_shape_has_n_risers(self):
        for shape in SL.SHAPES:
            for n in (10, 11, 16, 17, 22, 25):
                for turn in ("left", "right"):
                    for begin, end in itertools.product((True, False), repeat=2):
                        p = SL.make_plan(shape, n, T, W, turn, begin, end, landing=W * 1.2, gap=0.6,
                                         winders=3, radius=5.0)
                        self.assertTrue(p.ok, (shape, n, p.errors))
                        self.assertEqual(sum(r.risers for r in p.runs), n, (shape, n))
                        for r in p.runs:
                            self.assertEqual(len(r.tread_lines), r.risers, (shape, n, r.kind))
                        steps = [r.step0 for r in p.runs]
                        self.assertEqual(steps[0], 0)
                        for i in range(1, len(p.runs)):
                            self.assertEqual(steps[i], steps[i - 1] + p.runs[i - 1].risers)
                        for l in p.landings:
                            self.assertIn(l.step, steps)

    def test_treads_formula(self):
        self.assertEqual(SL.treads_for(10, True, True), 9)
        self.assertEqual(SL.treads_for(10, True, False), 10)
        self.assertEqual(SL.treads_for(10, False, True), 10)
        self.assertEqual(SL.treads_for(10, False, False), 11)

    def test_too_few_risers(self):
        self.assertFalse(SL.make_plan("straight", 1, T, W).ok)
        self.assertFalse(SL.make_plan("u_shape", 3, T, W).ok)
        self.assertFalse(SL.make_plan("winder_u", 7, T, W, winders=3).ok)     # two corners need 8
        self.assertTrue(SL.make_plan("winder_u", 8, T, W, winders=3).ok)
        self.assertTrue(SL.make_plan("winder_u", 12, T, W, winders=3).ok)
        self.assertFalse(SL.make_plan("spiral", 12, T, W).ok)                    # no radius
        self.assertFalse(SL.make_plan("spiral", 12, T, W, radius=W / 2.0).ok)    # no inner radius

    def test_first_flight_override(self):
        p = SL.make_plan("l_shape", 18, T, W, flight1=5)
        self.assertEqual(p.flights(), [5, 13])
        p = SL.make_plan("l_shape", 18, T, W, flight1=17)          # out of range -> balanced
        self.assertEqual(p.flights(), [9, 9])
        self.assertTrue(p.warnings)
        self.assertEqual(SL.make_plan("l_shape", 17, T, W).flights(), [9, 8])


class Straight(unittest.TestCase):
    def test_length_and_area(self):
        p = SL.make_plan("straight", 18, T, W)
        r = p.runs[0]
        self.assertAlmostEqual(r.length, 17 * T)
        self.assertAlmostEqual(area(r.outline), 17 * T * W)
        self.assertAlmostEqual(r.tread_lines[0][0][0], 0.0)
        self.assertAlmostEqual(r.tread_lines[-1][0][0], 17 * T)
        p = SL.make_plan("straight", 18, T, W, begin=False, end=False)
        self.assertAlmostEqual(p.runs[0].length, 19 * T)
        self.assertAlmostEqual(p.runs[0].tread_lines[0][0][0], T)

    def test_extras_change_length_only(self):
        a = SL.make_plan("straight", 18, T, W)
        b = SL.make_plan("straight", 18, T, W, extras=[1])
        self.assertAlmostEqual(b.runs[0].length - a.runs[0].length, T)


class LandingShapes(unittest.TestCase):
    def check_no_overlap(self, p):
        polys = [r.outline for r in p.runs] + [l.polygon for l in p.landings]
        overlap = 0
        for pt in grid(p):
            hits = sum(1 for poly in polys if inside(pt, poly))
            if hits > 1:
                overlap += 1
        self.assertEqual(overlap, 0, (p.shape, p.turn))

    def test_no_overlap_and_touching(self):
        for shape in ("straight_landing", "l_shape", "u_shape"):
            for turn in ("left", "right"):
                for n in (10, 17, 24):
                    for gap in (0.0, 0.5):
                        p = SL.make_plan(shape, n, T, W, turn, landing=W * 1.3, gap=gap)
                        self.check_no_overlap(p)
                        r1, r2 = p.runs
                        lp = p.landings[0].polygon
                        # run 1's last riser line lies on the landing's edge
                        end1 = r1.tread_lines[-1]
                        for q in end1:
                            self.assertLess(dist_pt_poly(q, lp + [lp[0]]), EPS, (shape, turn))
                        # run 2's first riser line too
                        start2 = r2.tread_lines[0]
                        for q in start2:
                            self.assertLess(dist_pt_poly(q, lp + [lp[0]]), EPS, (shape, turn))

    def test_landing_areas(self):
        p = SL.make_plan("l_shape", 18, T, W, landing=W)
        self.assertAlmostEqual(area(p.landings[0].polygon), W * W)
        p = SL.make_plan("u_shape", 18, T, W, landing=W * 1.5, gap=0.5)
        self.assertAlmostEqual(area(p.landings[0].polygon), (W * 1.5) * (2 * W + 0.5))
        p = SL.make_plan("straight_landing", 18, T, W, landing=2.0)
        self.assertAlmostEqual(area(p.landings[0].polygon), 2.0 * W)

    def test_landing_height_is_the_top_of_run_one(self):
        p = SL.make_plan("u_shape", 19, T, W)
        self.assertEqual(p.landings[0].step, p.runs[0].risers)
        self.assertEqual(p.runs[1].step0, p.runs[0].risers)

    def test_right_is_mirror_of_left(self):
        for shape in ("l_shape", "u_shape", "winder_l", "winder_u", "spiral"):
            a = SL.make_plan(shape, 18, T, W, "left", landing=W, gap=0.6, radius=5.0)
            b = SL.make_plan(shape, 18, T, W, "right", landing=W, gap=0.6, radius=5.0)
            self.assertAlmostEqual(a.bbox[0], b.bbox[0])
            self.assertAlmostEqual(a.bbox[2], b.bbox[2])
            self.assertAlmostEqual(a.bbox[1], -b.bbox[3])
            self.assertAlmostEqual(a.bbox[3], -b.bbox[1])
            self.assertAlmostEqual(sum(area(r.outline) for r in a.runs),
                                   sum(area(r.outline) for r in b.runs))

    def test_left_turn_goes_left(self):
        for shape in ("l_shape", "u_shape", "winder_l", "winder_u", "spiral"):
            p = SL.make_plan(shape, 18, T, W, "left", radius=5.0)
            self.assertGreater(p.bbox[3], W / 2.0 + 0.5, shape)
            q = SL.make_plan(shape, 18, T, W, "right", radius=5.0)
            self.assertLess(q.bbox[1], -W / 2.0 - 0.5, shape)


class Winders(unittest.TestCase):
    def expected_area(self, n, k, W, a, b, m, u):
        if not u:
            return a * T * W + W * W + b * T * W
        return a * T * W + W * W + (m + 1) * T * W + W * W + b * T * W

    def test_areas_and_counts(self):
        for k in (2, 3, 4):
            for n in (14, 17, 22):
                p = SL.make_plan("winder_l", n, T, W, winders=k)
                self.assertTrue(p.ok, p.errors)
                a = (n - (k + 1)) // 2
                b = n - (k + 1) - a
                self.assertAlmostEqual(area(p.runs[0].outline), self.expected_area(n, k, W, a, b, 0, False), 5)
                self.assertEqual(len(p.runs[0].sketch["risers"]), n)
            for n in (16, 21, 26):
                p = SL.make_plan("winder_u", n, T, W, winders=k, gap=T * 2)
                self.assertTrue(p.ok, p.errors)
                m = 1
                rest = n - 2 * (k + 1) - m
                a = rest // 2
                b = rest - a
                self.assertAlmostEqual(area(p.runs[0].outline), self.expected_area(n, k, W, a, b, m, True), 5)
                self.assertEqual(len(p.runs[0].sketch["risers"]), n)

    def test_sketch_is_acceptable_to_revit(self):
        """The rules in StairsRun.CreateSketchedRun's documentation."""
        for shape in ("winder_l", "winder_u"):
            for turn in ("left", "right"):
                for k in (2, 3, 4):
                    for n in (16, 19, 24):
                        for a in (None, 0, 3):
                            p = SL.make_plan(shape, n, T, W, turn, winders=k, gap=0.0, flight1=a)
                            if not p.ok:
                                continue
                            sk = p.runs[0].sketch
                            left, right, risers, path = sk["left"], sk["right"], sk["risers"], sk["path"]
                            # each boundary and the path are one chain, end to end
                            for chain in (left, right, path):
                                for s0, s1 in zip(chain, chain[1:]):
                                    self.assertLess(SL._dist(s0[-1], s1[1]), EPS, (shape, turn, k, n, a))
                            # left and right do not touch each other
                            lp, rp = SL._chain_points(left), SL._chain_points(right)
                            for q in lp:
                                self.assertGreater(dist_pt_poly(q, rp), EPS * 10)
                            # every riser runs from the left chain to the right chain
                            for s in risers:
                                self.assertLess(dist_pt_poly(s[1], lp), 1e-7, (shape, turn, k, n, a))
                                self.assertLess(dist_pt_poly(s[2], rp), 1e-7, (shape, turn, k, n, a))
                            # the walking line crosses every riser, starts on the first, ends on the last
                            pp = SL._chain_points(path)
                            for s in risers:
                                crossed = any(seg_cross(pp[i], pp[i + 1], s[1], s[2]) for i in range(len(pp) - 1))
                                self.assertTrue(crossed, (shape, turn, k, n, a, s))
                            self.assertLess(dist_pt_seg(pp[0], risers[0][1], risers[0][2]), 1e-7)
                            self.assertLess(dist_pt_seg(pp[-1], risers[-1][1], risers[-1][2]), 1e-7)

    def test_outline_is_a_simple_polygon(self):
        for shape in ("winder_l", "winder_u"):
            for turn in ("left", "right"):
                p = SL.make_plan(shape, 20, T, W, turn, winders=3, gap=0.9)
                poly = p.runs[0].outline
                # drop repeated points
                pts = [poly[0]]
                for q in poly[1:]:
                    if SL._dist(q, pts[-1]) > 1e-9:
                        pts.append(q)
                n = len(pts)
                for i in range(n):
                    for j in range(i + 1, n):
                        if abs(i - j) <= 1 or (i == 0 and j == n - 1):
                            continue
                        self.assertFalse(seg_cross(pts[i], pts[(i + 1) % n], pts[j], pts[(j + 1) % n]),
                                         (shape, turn, i, j))

    def test_riser_lines_do_not_cross_each_other(self):
        p = SL.make_plan("winder_u", 24, T, W, winders=3, gap=0.9)
        lines = p.runs[0].tread_lines
        for i in range(len(lines)):
            for j in range(i + 1, len(lines)):
                a, b = lines[i]
                c, d = lines[j]
                shared = [q for q in (a, b) if SL._dist(q, c) < 1e-9 or SL._dist(q, d) < 1e-9]
                if shared:
                    continue
                self.assertFalse(seg_cross(a, b, c, d), (i, j))

    def test_winder_treads_are_even_on_the_walking_line(self):
        p = SL.make_plan("winder_l", 17, T, W, winders=3)
        self.assertAlmostEqual(p.winder_tread, math.pi * W / 12.0)


class Spiral(unittest.TestCase):
    def test_sweep_from_risers(self):
        p = SL.make_plan("spiral", 18, T, W, radius=5.0)
        s = p.runs[0].spiral
        self.assertAlmostEqual(s["sweep"], 17 * T / 5.0)
        # the outline is a 6-degree polyline, so a hair under the true arc area
        exact = s["sweep"] * 5.0 * W
        self.assertLess(abs(area(p.runs[0].outline) - exact) / exact, 0.005)
        self.assertTrue(s["ccw"])
        self.assertAlmostEqual(s["center"][1], 5.0)
        q = SL.make_plan("spiral", 18, T, W, "right", radius=5.0)
        self.assertFalse(q.runs[0].spiral["ccw"])
        self.assertAlmostEqual(q.runs[0].spiral["center"][1], -5.0)

    def test_fixed_sweep_changes_the_tread(self):
        p = SL.make_plan("spiral", 18, T, W, radius=5.0, sweep=math.radians(270))
        self.assertAlmostEqual(p.tread, 5.0 * math.radians(270) / 17.0)
        self.assertAlmostEqual(p.runs[0].spiral["sweep"], math.radians(270))

    def test_tread_lines_are_radial(self):
        p = SL.make_plan("spiral", 12, T, W, radius=5.0)
        c = p.runs[0].spiral["center"]
        for a, b in p.runs[0].tread_lines:
            ra, rb = SL._dist(a, c), SL._dist(b, c)
            self.assertAlmostEqual(ra, 5.0 - W / 2.0)
            self.assertAlmostEqual(rb, 5.0 + W / 2.0)

    def test_path_radius(self):
        self.assertAlmostEqual(SL.spiral_path_radius(5.0, W, 0, True), 5.0)
        self.assertAlmostEqual(SL.spiral_path_radius(5.0, W, 1, True), 5.0 - W / 2.0)     # left edge, turning left = inner
        self.assertAlmostEqual(SL.spiral_path_radius(5.0, W, 1, False), 5.0 + W / 2.0)    # left edge, turning right = outer
        self.assertAlmostEqual(SL.spiral_path_radius(5.0, W, -1, True), 5.0 + W / 2.0)


class World(unittest.TestCase):
    def test_to_world(self):
        x, y = SL.to_world((1.0, 0.0), (10.0, 20.0), math.pi / 2)
        self.assertAlmostEqual(x, 10.0)
        self.assertAlmostEqual(y, 21.0)

    def test_shifted_line(self):
        p0, p1 = SL.shifted_line((0, 0), (4, 0), 0.5)          # left of +x is +y
        self.assertAlmostEqual(p0[1], 0.5)
        self.assertAlmostEqual(p1[1], 0.5)
        p0, p1 = SL.shifted_line((0, 0), (0, 4), 0.5)          # left of +y is -x
        self.assertAlmostEqual(p0[0], -0.5)


if __name__ == "__main__":
    unittest.main()
