# -*- coding: utf-8 -*-
"""
relevel_tools
Revit-API-free math for DeeReLevel. Every function here takes plain
numbers (internal feet, matching Revit's internal unit) and returns
plain numbers or simple tuples/lists - no Revit API objects touch this
module, so it can be unit-tested with plain `python` / IronPython
outside of Revit.

Core idea: for any element whose vertical position is expressed as
"Level elevation + an offset parameter" (Base Offset, Sill Height,
Height Offset From Level, Elevation From Level, Start/End Offset on
MEP curves, etc.), moving the Level by `delta` and then subtracting
that same `delta` from the offset parameter leaves the element's
absolute (real-world) elevation completely unchanged:

    new_absolute = new_level_elev + new_offset
                 = (old_level_elev + delta) + (old_offset - delta)
                 = old_level_elev + old_offset
                 = old_absolute

This is the single formula every category-specific handler in
DeeReLevel's script.py reduces to. For elements with two ends at
different elevations (sloped pipes/ducts), applying the same delta
subtraction to BOTH ends preserves both endpoint elevations exactly,
which also preserves the slope (rise/run is unchanged when both
elevations are unchanged).
"""


def compute_delta(old_level_elev, new_level_elev):
    """How far the level is moving, in internal feet. Positive = up."""
    return new_level_elev - old_level_elev


def adjust_offset(old_offset, delta):
    """The new value for a single 'offset from this level' parameter
    that keeps the element's absolute elevation fixed while the level
    moves by `delta`."""
    return old_offset - delta


def adjust_pair(old_start_offset, old_end_offset, delta):
    """Same as adjust_offset, applied to both ends of a two-ended
    element (a sloped pipe/duct run, a beam, etc.) so both endpoint
    elevations - and therefore the slope between them - are
    preserved exactly."""
    return (old_start_offset - delta, old_end_offset - delta)


def slope_ratio(start_elev, end_elev, horizontal_length):
    """Rise/run slope of a two-ended run. Returns 0.0 for a
    zero-length run rather than raising, since callers use this only
    for reporting/comparison, not for driving geometry."""
    if horizontal_length <= 1e-9:
        return 0.0
    return (end_elev - start_elev) / horizontal_length


def would_go_negative(old_offset, delta, min_allowed=None):
    """True if subtracting delta from old_offset would push the
    resulting offset below min_allowed (default: no floor, so this
    only flags an actual sign flip from >=0 to <0 when min_allowed is
    None - useful for catching 'wall base offset now negative'-style
    surprises the user should confirm before applying)."""
    new_offset = adjust_offset(old_offset, delta)
    if min_allowed is not None:
        return new_offset < min_allowed
    return old_offset >= 0.0 and new_offset < 0.0


def duplicate_elevation_conflicts(level_elevations):
    """level_elevations: list of (level_name, elevation) pairs (the
    PROPOSED/new elevations, already including any edits). Returns a
    list of (name_a, name_b, elevation) tuples for every pair of
    distinct levels that would end up at the same elevation (within
    a tight tolerance) - a common, easy-to-miss mistake when editing
    several levels at once."""
    tol = 1e-6
    conflicts = []
    items = list(level_elevations)
    for i in range(len(items)):
        name_a, elev_a = items[i]
        for j in range(i + 1, len(items)):
            name_b, elev_b = items[j]
            if abs(elev_a - elev_b) <= tol:
                conflicts.append((name_a, name_b, elev_a))
    return conflicts


def format_signed(value, decimals=3):
    """'+1.500' / '-0.250' / '0.000' style text for a delta/offset
    value, used throughout the UI grid and reports."""
    sign = "+" if value > 1e-9 else ("-" if value < -1e-9 else "")
    return "{0}{1:.{2}f}".format(sign, abs(value), decimals)


# --------------------------------------------------------------------------
# Self-test (run directly: `python relevel_tools.py`). Uses the stdlib
# unittest module so this is a real, runnable unit-test suite, per the
# "unit-testable architecture" requirement - independent of Revit/pyRevit.
# --------------------------------------------------------------------------
if __name__ == "__main__":
    import unittest

    class RelevelToolsTests(unittest.TestCase):
        def test_compute_delta(self):
            self.assertAlmostEqual(compute_delta(10.0, 11.5), 1.5)
            self.assertAlmostEqual(compute_delta(10.0, 8.0), -2.0)

        def test_adjust_offset_cancels_delta(self):
            delta = compute_delta(10.0, 11.5)
            new_offset = adjust_offset(2.0, delta)
            # absolute position must be identical before and after
            self.assertAlmostEqual(10.0 + 2.0, 11.5 + new_offset)

        def test_adjust_pair_preserves_slope(self):
            delta = compute_delta(10.0, 12.0)
            start_off, end_off = 0.5, -1.5
            new_start, new_end = adjust_pair(start_off, end_off, delta)
            old_start_abs = 10.0 + start_off
            old_end_abs = 10.0 + end_off
            new_start_abs = 12.0 + new_start
            new_end_abs = 12.0 + new_end
            self.assertAlmostEqual(old_start_abs, new_start_abs)
            self.assertAlmostEqual(old_end_abs, new_end_abs)
            self.assertAlmostEqual(
                slope_ratio(old_start_abs, old_end_abs, 20.0),
                slope_ratio(new_start_abs, new_end_abs, 20.0))

        def test_would_go_negative(self):
            delta = compute_delta(10.0, 11.0)
            self.assertTrue(would_go_negative(0.5, delta))
            self.assertFalse(would_go_negative(-0.5, delta))
            delta_down = compute_delta(10.0, 9.0)
            self.assertFalse(would_go_negative(0.5, delta_down))

        def test_duplicate_elevation_conflicts(self):
            conflicts = duplicate_elevation_conflicts(
                [("L1", 0.0), ("L2", 10.0), ("L3", 10.0), ("L4", 20.0)])
            self.assertEqual(len(conflicts), 1)
            self.assertEqual(conflicts[0][2], 10.0)

        def test_format_signed(self):
            self.assertEqual(format_signed(1.5), "+1.500")
            self.assertEqual(format_signed(-0.25), "-0.250")
            self.assertEqual(format_signed(0.0), "0.000")

    unittest.main()
