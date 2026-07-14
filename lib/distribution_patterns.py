# -*- coding: utf-8 -*-
"""distribution_patterns.py

Revit-API-FREE geometry/pattern module for DeeDistributor.

Every function in this module operates on plain (x, y) float tuples and
plain Python lists/dicts - no Autodesk.Revit.* imports, no pyrevit
imports, nothing outside the Python 2.7/3.x stdlib (math, random,
itertools). This lets the module be imported and unit-tested standalone
under either IronPython 2.7.12 (inside Revit) or a normal CPython3
interpreter (outside Revit), the same way xlsx_writer.py is importable
and testable outside IronPython.

Call sites should use a plain `import distribution_patterns` (not a
relative/package import), matching the existing `import xlsx_writer`
convention used elsewhere in this extension.

Polygon representation convention used throughout this module:
  - A "loop" is a list of (x, y) vertex tuples describing a closed
    polygon. Do NOT repeat the first point at the end.
  - A "boundary" for placement purposes is (outer_loop, hole_loops)
    where outer_loop is one loop and hole_loops is a list of zero or
    more loops representing interior voids/openings to exclude.
"""

import math
import random as _random


# ---------------------------------------------------------------------------
# Geometric primitives
# ---------------------------------------------------------------------------

def point_in_polygon(x, y, loop):
    """Standard even-odd ray-casting test. Returns True if (x, y) is
    strictly inside `loop` (a list of (x, y) tuples, no repeated closing
    vertex). Boundary-edge cases are treated as OUTSIDE (conservative -
    we'd rather skip a borderline point than place a family straddling
    a wall)."""
    inside = False
    n = len(loop)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = loop[i]
        xj, yj = loop[j]
        if ((yi > y) != (yj > y)) and \
           (x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def point_in_boundary(x, y, outer_loop, hole_loops):
    """True if inside outer_loop AND not inside any hole_loops loop -
    i.e. valid placement area accounting for interior openings/shafts
    modeled as room-boundary inner loops."""
    if not point_in_polygon(x, y, outer_loop):
        return False
    for hole in hole_loops:
        if point_in_polygon(x, y, hole):
            return False
    return True


def _line_intersect(ax0, ay0, ax1, ay1, bx0, by0, bx1, by1):
    """Infinite-line intersection (not segment-clamped), matching
    DeeFinisher's _line_intersection_xy logic (miter join, so
    intersecting the infinite lines - not the bounded segments - is
    correct)."""
    dax, day = ax1 - ax0, ay1 - ay0
    dbx, dby = bx1 - bx0, by1 - by0
    denom = dax * dby - day * dbx
    if abs(denom) < 1e-9:
        return None
    t = ((bx0 - ax0) * dby - (by0 - ay0) * dbx) / denom
    return (ax0 + dax * t, ay0 + day * t)


def offset_polygon_inward(loop, distance):
    """Returns a new loop, each edge moved `distance` inward (toward the
    polygon's own interior), with corners mitered by intersecting
    adjacent offset edges - same technique as DeeFinisher's
    _build_wall_finish_curves, generalized to a closed polygon with no
    "eligible/ineligible" per-edge concept (every edge of a placement
    boundary is eligible).

    Degenerate/collapsed results (self-intersecting after a large
    offset, e.g. a very narrow room) are a known limitation - see risk
    flags in the implementation spec. Callers should treat an inset
    whose resulting polygon area is <= 0 (or grew instead of shrank) as
    "offset too large for this boundary" and fall back to returning
    None, letting the caller decide (skip pattern, or clamp
    boundary_offset)."""
    n = len(loop)
    if n < 3 or distance <= 0:
        return list(loop)

    cx = sum(p[0] for p in loop) / n
    cy = sum(p[1] for p in loop) / n  # crude interior reference point

    offset_edges = []
    for i in range(n):
        x0, y0 = loop[i]
        x1, y1 = loop[(i + 1) % n]
        dx, dy = x1 - x0, y1 - y0
        length = (dx * dx + dy * dy) ** 0.5
        if length < 1e-9:
            continue
        dx, dy = dx / length, dy / length
        # perpendicular candidates; pick the one pointing toward centroid
        nx, ny = -dy, dx
        mx, my = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        if (nx * (cx - mx) + ny * (cy - my)) < 0:
            nx, ny = -nx, -ny
        offset_edges.append((x0 + nx * distance, y0 + ny * distance,
                              x1 + nx * distance, y1 + ny * distance))

    if len(offset_edges) < 3:
        return None

    result = []
    m = len(offset_edges)
    for i in range(m):
        ax0, ay0, ax1, ay1 = offset_edges[i - 1]
        bx0, by0, bx1, by1 = offset_edges[i]
        inter = _line_intersect(ax0, ay0, ax1, ay1, bx0, by0, bx1, by1)
        result.append(inter if inter is not None else (bx0, by0))

    # Sanity check: a valid inward offset should not grow the polygon or
    # flip its winding sign. If it does, the offset degenerated (self
    # intersecting / collapsed) - signal the caller to fall back.
    orig_area = polygon_area(loop)
    new_area = polygon_area(result)
    if orig_area == 0:
        return None
    if (orig_area > 0) != (new_area > 0):
        return None
    if abs(new_area) >= abs(orig_area):
        return None
    if abs(new_area) < 1e-9:
        return None

    return result


def polygon_bounds(loop):
    xs = [p[0] for p in loop]
    ys = [p[1] for p in loop]
    return (min(xs), min(ys), max(xs), max(ys))


def polygon_area(loop):
    """Signed shoelace area - sign indicates winding direction, useful
    for sanity-checking offset_polygon_inward results (if the offset
    polygon's area sign flips or magnitude grows, the offset degenerated
    and the caller should fall back / clamp)."""
    n = len(loop)
    a = 0.0
    for i in range(n):
        x0, y0 = loop[i]
        x1, y1 = loop[(i + 1) % n]
        a += x0 * y1 - x1 * y0
    return a / 2.0


# ---------------------------------------------------------------------------
# Lock-count-vs-lock-spacing solver (grid-family patterns)
# ---------------------------------------------------------------------------

def solve_grid_count_for_spacing(width, height, x_spacing, y_spacing):
    """Given an inset bounding width/height and desired spacing, returns
    (cols, rows, actual_x_spacing, actual_y_spacing) - actual spacing is
    adjusted so instances land evenly from edge to edge (count-1 gaps
    fit exactly in width/height) rather than leaving a partial-spacing
    remainder. This is the 'spacing-locked' mode."""
    cols = max(1, int(width // x_spacing) + 1)
    rows = max(1, int(height // y_spacing) + 1)
    actual_x = width / (cols - 1) if cols > 1 else 0.0
    actual_y = height / (rows - 1) if rows > 1 else 0.0
    return cols, rows, actual_x, actual_y


def solve_grid_spacing_for_count(width, height, target_count):
    """'Count-locked' mode: given a target total instance count, choose
    a cols x rows grid as close to target_count as possible while
    keeping cell aspect ratio close to width/height, then compute the
    resulting spacing. Returns (cols, rows, x_spacing, y_spacing)."""
    if target_count <= 1:
        return 1, 1, 0.0, 0.0
    aspect = (width / height) if height > 1e-9 else 1.0
    cols = max(1, int(round((target_count * aspect) ** 0.5)))
    rows = max(1, int(round(float(target_count) / cols)))
    actual_x = width / (cols - 1) if cols > 1 else 0.0
    actual_y = height / (rows - 1) if rows > 1 else 0.0
    return cols, rows, actual_x, actual_y


def solve_uniform_grid_spacing_for_count(width, height, target_count):
    """Same aspect-matching approach as solve_grid_spacing_for_count, but
    for the 'N equal CELLS, one fixture at each cell center' model used
    by uniform_grid (NOT the 'N points across N-1 gaps, flush to the
    edges' model used by grid/centered_grid). Actual spacing here is
    width/cols and height/rows - dividing by cols-1/rows-1 would be
    wrong for this model and would silently break the
    margin-equals-half-actual-spacing guarantee. Lets X/Y spacing be
    calculated directly from the room's own area and shape (aspect
    ratio) for a target fixture count, rather than typed by hand.
    Returns (cols, rows, x_spacing, y_spacing)."""
    if target_count <= 1:
        return 1, 1, width, height
    aspect = (width / height) if height > 1e-9 else 1.0
    cols = max(1, int(round((target_count * aspect) ** 0.5)))
    rows = max(1, int(round(float(target_count) / cols)))
    actual_x = width / cols
    actual_y = height / rows
    return cols, rows, actual_x, actual_y


def solve_single_row_spacing_for_count(width, target_count):
    """Single-row equivalent of solve_uniform_grid_spacing_for_count:
    one row, so cols = target_count directly and actual_x = width/cols.
    Returns (cols, x_spacing)."""
    cols = max(1, int(round(target_count)))
    actual_x = width / cols
    return cols, actual_x


# ---------------------------------------------------------------------------
# Pattern generators
# ---------------------------------------------------------------------------

def _centered_steps(center, lo, hi, spacing):
    steps = [center]
    if spacing <= 0:
        return steps
    k = 1
    while center + k * spacing <= hi + 1e-6:
        steps.append(center + k * spacing)
        k += 1
    k = 1
    while center - k * spacing >= lo - 1e-6:
        steps.append(center - k * spacing)
        k += 1
    return sorted(steps)


def grid(outer_loop, hole_loops, x_spacing, y_spacing, boundary_offset, **kw):
    """Regular rows/columns starting at the inset bounding box's
    min-corner, stepping by x_spacing/y_spacing."""
    inset = offset_polygon_inward(outer_loop, boundary_offset) or outer_loop
    minx, miny, maxx, maxy = polygon_bounds(inset)
    y = miny
    while y <= maxy + 1e-6:
        x = minx
        while x <= maxx + 1e-6:
            if point_in_boundary(x, y, inset, hole_loops):
                yield (x, y)
            x += x_spacing
        y += y_spacing


def offset_grid(outer_loop, hole_loops, x_spacing, y_spacing, boundary_offset, **kw):
    """Brick/running-bond: odd rows (0-indexed row counter) shifted by
    x_spacing/2."""
    inset = offset_polygon_inward(outer_loop, boundary_offset) or outer_loop
    minx, miny, maxx, maxy = polygon_bounds(inset)
    row = 0
    y = miny
    while y <= maxy + 1e-6:
        shift = (x_spacing / 2.0) if (row % 2 == 1) else 0.0
        x = minx + shift
        while x <= maxx + 1e-6:
            if point_in_boundary(x, y, inset, hole_loops):
                yield (x, y)
            x += x_spacing
        y += y_spacing
        row += 1


def centered_grid(outer_loop, hole_loops, x_spacing, y_spacing, boundary_offset, **kw):
    """Same regular grid, but the first point is placed at the inset
    bbox's CENTER and the grid expands outward symmetrically in both
    axes (rather than starting at min-corner) - so any leftover
    fractional margin is split evenly on both sides."""
    inset = offset_polygon_inward(outer_loop, boundary_offset) or outer_loop
    minx, miny, maxx, maxy = polygon_bounds(inset)
    cx, cy = (minx + maxx) / 2.0, (miny + maxy) / 2.0
    xs = _centered_steps(cx, minx, maxx, x_spacing)
    ys = _centered_steps(cy, miny, maxy, y_spacing)
    for y in ys:
        for x in xs:
            if point_in_boundary(x, y, inset, hole_loops):
                yield (x, y)


def boundary_offset_grid(outer_loop, hole_loops, x_spacing, y_spacing, boundary_offset, **kw):
    """'Maintains equal margins around the room boundary' - functionally
    the centered_grid algorithm run against the inset polygon's bbox,
    which by construction keeps a uniform boundary_offset margin on
    every side (that's what offset_polygon_inward guarantees, modulo
    the grid's own leftover spacing remainder which centered placement
    splits evenly). Implemented as a thin wrapper for clarity/intent."""
    for pt in centered_grid(outer_loop, hole_loops, x_spacing, y_spacing, boundary_offset, **kw):
        yield pt


def diagonal(outer_loop, hole_loops, x_spacing, y_spacing, boundary_offset, angle_degrees=45.0, **kw):
    """Points generated on a rotated grid: rotate the inset bbox corners
    by -angle_degrees into a local 'diagonal space', lay out a regular
    grid in local space at x_spacing/y_spacing, then rotate each
    candidate back by +angle_degrees before the point-in-boundary
    test."""
    inset = offset_polygon_inward(outer_loop, boundary_offset) or outer_loop
    minx, miny, maxx, maxy = polygon_bounds(inset)
    cx, cy = (minx + maxx) / 2.0, (miny + maxy) / 2.0
    theta = math.radians(angle_degrees)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    # local-space half-extents padded by the bbox diagonal so rotation
    # doesn't clip corners
    diag = ((maxx - minx) ** 2 + (maxy - miny) ** 2) ** 0.5
    half = diag / 2.0 + max(x_spacing, y_spacing)
    y = -half
    while y <= half:
        x = -half
        while x <= half:
            # rotate local (x, y) by theta back into world space, offset
            # by center
            wx = cx + x * cos_t - y * sin_t
            wy = cy + x * sin_t + y * cos_t
            if point_in_boundary(wx, wy, inset, hole_loops):
                yield (wx, wy)
            x += x_spacing
        y += y_spacing


def radial(outer_loop, hole_loops, x_spacing, y_spacing, boundary_offset,
           center=None, ring_spacing=None, angular_spacing_degrees=None, **kw):
    """Concentric rings starting at `center` (defaults to inset-bbox
    center). ring_spacing defaults to y_spacing (radial step between
    rings); angular step on ring k is chosen so points on that ring are
    spaced approximately x_spacing apart along the circumference:
    angle_step = x_spacing / max(radius, x_spacing) (radians), so inner
    rings naturally get fewer points and outer rings get more, keeping
    roughly uniform arc-length spacing - this is the standard
    'radial/polar grid' approach."""
    inset = offset_polygon_inward(outer_loop, boundary_offset) or outer_loop
    minx, miny, maxx, maxy = polygon_bounds(inset)
    if center is None:
        center = ((minx + maxx) / 2.0, (miny + maxy) / 2.0)
    cx, cy = center
    r_step = ring_spacing or y_spacing
    max_r = max(maxx - minx, maxy - miny)  # generous outer bound; points
    # outside inset are filtered anyway
    if point_in_boundary(cx, cy, inset, hole_loops):
        yield (cx, cy)
    r = r_step
    while r <= max_r:
        angle_step = x_spacing / max(r, x_spacing)
        a = 0.0
        two_pi = 2 * math.pi
        while a < two_pi - 1e-9:
            x = cx + r * math.cos(a)
            y = cy + r * math.sin(a)
            if point_in_boundary(x, y, inset, hole_loops):
                yield (x, y)
            a += angle_step
        r += r_step


def perimeter(outer_loop, hole_loops, x_spacing, y_spacing, boundary_offset, **kw):
    """Places points ONLY along the (inset) boundary polyline, walking
    each edge at x_spacing arc-length intervals - not a filled interior
    pattern. y_spacing is unused here (kept for uniform call
    signature)."""
    inset = offset_polygon_inward(outer_loop, boundary_offset) or outer_loop
    n = len(inset)
    for i in range(n):
        x0, y0 = inset[i]
        x1, y1 = inset[(i + 1) % n]
        length = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
        if length < 1e-9:
            continue
        steps = max(1, int(length // x_spacing))
        for s in range(steps + (1 if i == n - 1 else 0)):
            t = (s * x_spacing) / length
            if t > 1.0:
                break
            x = x0 + (x1 - x0) * t
            y = y0 + (y1 - y0) * t
            # perimeter points intentionally NOT re-tested against
            # point_in_boundary (they are ON the inset boundary by
            # construction); hole proximity is still worth checking
            if not any(point_in_polygon(x, y, h) for h in hole_loops):
                yield (x, y)


def random_pattern(outer_loop, hole_loops, x_spacing, y_spacing, boundary_offset,
                    target_count=50, min_spacing=None, max_attempts=2000, seed=None, **kw):
    """Rejection-sampling random placement: draw a uniform random point
    inside the inset bbox, keep it if point_in_boundary AND it is
    min_spacing away from every previously accepted point (min_spacing
    defaults to min(x_spacing, y_spacing)). Stops at target_count
    accepted points or max_attempts tries, whichever first - bounded
    runtime is essential since rejection sampling can stall on a nearly
    full room."""
    rng = _random.Random(seed)
    inset = offset_polygon_inward(outer_loop, boundary_offset) or outer_loop
    minx, miny, maxx, maxy = polygon_bounds(inset)
    min_sp = min_spacing if min_spacing is not None else min(x_spacing, y_spacing)
    accepted = []
    attempts = 0
    while len(accepted) < target_count and attempts < max_attempts:
        attempts += 1
        x = rng.uniform(minx, maxx)
        y = rng.uniform(miny, maxy)
        if not point_in_boundary(x, y, inset, hole_loops):
            continue
        if any((x - ax) ** 2 + (y - ay) ** 2 < min_sp * min_sp for ax, ay in accepted):
            continue
        accepted.append((x, y))
        yield (x, y)


def uniform_grid(outer_loop, hole_loops, x_spacing, y_spacing, boundary_offset, **kw):
    """'Lighting Distribution' style Uniform Grid: divides the room's own
    bounding box into cols x rows EQUAL cells, sized as close as possible
    to the requested x_spacing/y_spacing, and places one fixture at each
    cell's center. By construction (not approximation) this guarantees
    the left/right margins are BOTH exactly half the actual,
    cell-adjusted horizontal spacing, and the top/bottom margins are BOTH
    exactly half the actual vertical spacing - a symmetrical layout
    centered in the room rather than flush against the walls.

    boundary_offset is intentionally NOT applied here (no
    offset_polygon_inward call) - per this pattern's own definition, the
    margin to the boundary IS half the actual spacing, not a separately
    configurable value layered on top. Works against outer_loop's own
    bounding box for the cell math (rectangular case is exact), and
    point_in_boundary filters candidates for irregular shapes and holes."""
    minx, miny, maxx, maxy = polygon_bounds(outer_loop)
    width = maxx - minx
    height = maxy - miny
    if width <= 0 or height <= 0:
        return
    cols = max(1, int(round(width / x_spacing))) if x_spacing > 0 else 1
    rows = max(1, int(round(height / y_spacing))) if y_spacing > 0 else 1
    actual_x = width / cols
    actual_y = height / rows
    for r in range(rows):
        y = miny + (r + 0.5) * actual_y
        for c in range(cols):
            x = minx + (c + 0.5) * actual_x
            if point_in_boundary(x, y, outer_loop, hole_loops):
                yield (x, y)


def single_row(outer_loop, hole_loops, x_spacing, y_spacing, boundary_offset,
               row_position_mode="Centered", row_offset=0.0, **kw):
    """'Lighting Distribution' style Single Row: same equal-cell division
    logic as uniform_grid, but along X only, on a single row - left/right
    margins are both exactly half the actual (cell-adjusted) horizontal
    spacing. The row's Y position is either the boundary bbox's vertical
    center (row_position_mode='Centered', default), or
    miny + row_offset (row_position_mode='Custom Offset', row_offset
    measured up from the bbox's bottom edge). boundary_offset is
    intentionally NOT applied, same reasoning as uniform_grid."""
    minx, miny, maxx, maxy = polygon_bounds(outer_loop)
    width = maxx - minx
    if width <= 0:
        return
    cols = max(1, int(round(width / x_spacing))) if x_spacing > 0 else 1
    actual_x = width / cols
    if row_position_mode == "Custom Offset":
        y = miny + row_offset
    else:
        y = (miny + maxy) / 2.0
    for c in range(cols):
        x = minx + (c + 0.5) * actual_x
        if point_in_boundary(x, y, outer_loop, hole_loops):
            yield (x, y)


PATTERN_REGISTRY = {
    "Grid": grid,
    "Offset Grid (Brick)": offset_grid,
    "Centered Grid": centered_grid,
    "Boundary Offset Grid": boundary_offset_grid,
    "Diagonal": diagonal,
    "Radial": radial,
    "Perimeter": perimeter,
    "Random": random_pattern,
    "Uniform Grid": uniform_grid,
    "Single Row": single_row,
    # "Custom Pattern" is intentionally NOT registered here - future
    # patterns are added by writing a new generator function with the
    # same (outer_loop, hole_loops, x_spacing, y_spacing,
    # boundary_offset, **kw) signature and registering it here.
}
