# -*- coding: utf-8 -*-
"""
Revit-API-free helper logic for DeeHostLevel (HealthPack panel):
  - generate_level_colors: a distinct RGB color per level, in elevation
    order, so re-running a scan reassigns the same colors to the same
    relative level order.
  - compute_rehosted_offset: the elevation-offset math for "rehost to a
    different level" - keeping the element's absolute physical position
    unchanged (keep_in_place=True) or leaving the raw offset value alone
    (keep_in_place=False, so the element visually shifts by the
    difference between the old and new level's elevation).

Kept separate from script.py (which needs the live Revit API) so this
part can be unit-tested on its own, same as distribution_patterns.py.
"""
import colorsys


def generate_level_colors(count, saturation=0.65, value=0.85):
    """Returns `count` (r, g, b) tuples (0-255 ints), evenly spaced around
    the hue wheel so any number of levels gets visually distinct colors.
    Order matches whatever order the caller's levels are in (callers
    should pass levels sorted by elevation for a stable, predictable
    assignment)."""
    if count <= 0:
        return []
    colors = []
    for i in range(count):
        hue = float(i) / count
        r, g, b = colorsys.hsv_to_rgb(hue, saturation, value)
        colors.append((int(round(r * 255)), int(round(g * 255)), int(round(b * 255))))
    return colors


def compute_rehosted_offset(old_level_elevation, new_level_elevation,
                             old_offset, keep_in_place):
    """Both elevations/offset in the same unit (internal feet is fine -
    this is unit-agnostic). Returns the new offset value to write back.

    keep_in_place=True: the element's absolute elevation
    (level_elevation + offset) stays the same, so the new offset
    compensates for the difference between the two levels' elevations.

    keep_in_place=False: the offset value itself is left untouched, so
    the element's absolute elevation shifts by exactly the difference
    between the old and new level's elevations."""
    if keep_in_place:
        return old_offset + (old_level_elevation - new_level_elevation)
    return old_offset
