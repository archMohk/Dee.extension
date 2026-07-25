# -*- coding: utf-8 -*-
"""
align_tools
Revit-API-free alignment/distribution math for DeeAligner. Every
function takes a list of bounding boxes - plain (min_x, max_x, min_y,
max_y) tuples, in whatever unit the caller uses (internal feet for
Revit sheet space) - and returns a same-length list of (dx, dy) deltas,
one per input box, in the same order. The caller applies each delta to
the corresponding Viewport/Image using whatever Revit API move
mechanism applies to it - this module has no notion of what it's
actually aligning, just geometry.

Distribute functions space item CENTERS evenly between the first and
last item (by current center position along that axis) - a simpler,
more predictable convention than equalizing edge-to-edge gaps, and
those two only differ when items are very different sizes.
"""


def _center_x(box):
    return (box[0] + box[1]) / 2.0


def _center_y(box):
    return (box[2] + box[3]) / 2.0


def align_left(boxes):
    target = min(b[0] for b in boxes)
    return [(target - b[0], 0.0) for b in boxes]


def align_right(boxes):
    target = max(b[1] for b in boxes)
    return [(target - b[1], 0.0) for b in boxes]


def align_bottom(boxes):
    target = min(b[2] for b in boxes)
    return [(0.0, target - b[2]) for b in boxes]


def align_top(boxes):
    target = max(b[3] for b in boxes)
    return [(0.0, target - b[3]) for b in boxes]


def align_center_horizontal(boxes):
    """Aligns all items' horizontal centers to the center of the overall
    selection's bounding box (not the average of centers) - matches the
    usual "align center" convention in design tools."""
    overall_min = min(b[0] for b in boxes)
    overall_max = max(b[1] for b in boxes)
    target = (overall_min + overall_max) / 2.0
    return [(target - _center_x(b), 0.0) for b in boxes]


def align_middle_vertical(boxes):
    overall_min = min(b[2] for b in boxes)
    overall_max = max(b[3] for b in boxes)
    target = (overall_min + overall_max) / 2.0
    return [(0.0, target - _center_y(b)) for b in boxes]


def distribute_horizontal(boxes):
    """Fewer than 3 items has nothing meaningful to distribute (the two
    endpoints would just stay put) - returns zero deltas."""
    n = len(boxes)
    if n < 3:
        return [(0.0, 0.0) for _ in boxes]
    order = sorted(range(n), key=lambda i: _center_x(boxes[i]))
    first_center = _center_x(boxes[order[0]])
    last_center = _center_x(boxes[order[-1]])
    step = (last_center - first_center) / (n - 1)
    deltas = [(0.0, 0.0)] * n
    for rank, idx in enumerate(order):
        target = first_center + step * rank
        deltas[idx] = (target - _center_x(boxes[idx]), 0.0)
    return deltas


def distribute_vertical(boxes):
    n = len(boxes)
    if n < 3:
        return [(0.0, 0.0) for _ in boxes]
    order = sorted(range(n), key=lambda i: _center_y(boxes[i]))
    first_center = _center_y(boxes[order[0]])
    last_center = _center_y(boxes[order[-1]])
    step = (last_center - first_center) / (n - 1)
    deltas = [(0.0, 0.0)] * n
    for rank, idx in enumerate(order):
        target = first_center + step * rank
        deltas[idx] = (0.0, target - _center_y(boxes[idx]))
    return deltas


def boxes_overlap(a, b):
    """True if two (min_x, max_x, min_y, max_y) boxes intersect (touching
    edges don't count as overlap)."""
    return a[0] < b[1] and b[0] < a[1] and a[2] < b[3] and b[2] < a[3]


def find_overlapping_pairs(boxes):
    """Returns a list of (i, j) index pairs (i < j) whose boxes overlap -
    an O(n^2) scan, fine for the handful of items on a typical sheet."""
    pairs = []
    n = len(boxes)
    for i in range(n):
        for j in range(i + 1, n):
            if boxes_overlap(boxes[i], boxes[j]):
                pairs.append((i, j))
    return pairs


def scale_box(box, factor, anchor="center"):
    """Returns a new (min_x, max_x, min_y, max_y) box scaled by `factor`
    around its own center (anchor="center") or its bottom-left corner
    (anchor="min"). Does not move other items - purely resizes one box."""
    w = (box[1] - box[0]) * factor
    h = (box[3] - box[2]) * factor
    if anchor == "min":
        return (box[0], box[0] + w, box[2], box[2] + h)
    cx = _center_x(box)
    cy = _center_y(box)
    return (cx - w / 2.0, cx + w / 2.0, cy - h / 2.0, cy + h / 2.0)


def _slot_overlaps(box, blockers):
    for other in blockers:
        if boxes_overlap(box, other):
            return other
    return None


def shelf_pack(sizes, bounds, obstacles=None, spacing=0.0):
    """Places rectangles of the given (width, height) `sizes` inside
    `bounds` = (min_x, max_x, min_y, max_y), packing them into shelves
    (rows) left-to-right then top-to-bottom, sliding past any box in
    `obstacles` (e.g. existing Viewports/unchecked Images) instead of
    overlapping it. Returns a list the same length/order as `sizes`,
    each entry either a placed (min_x, max_x, min_y, max_y) box or None
    if no overlap-free spot could be found for that item.

    This is a simple, honest best-effort heuristic (shelf packing with
    obstacle sliding) - not a globally-optimal bin packer. It can fail
    to find a spot that a smarter/backtracking algorithm would find,
    particularly with irregularly-placed obstacles."""
    min_x, max_x, min_y, max_y = bounds
    blockers = list(obstacles or [])
    results = []
    cur_x = min_x
    cur_y = max_y
    row_height = 0.0

    for w, h in sizes:
        if w <= 0 or h <= 0 or w > (max_x - min_x) or h > (max_y - min_y):
            results.append(None)
            continue

        placed_box = None
        guard = 0
        while guard < 500:
            guard += 1
            if cur_x + w > max_x + 1e-9:
                cur_x = min_x
                cur_y -= (row_height + spacing)
                row_height = 0.0
            if cur_y - h < min_y - 1e-9:
                break

            candidate = (cur_x, cur_x + w, cur_y - h, cur_y)
            blocker = _slot_overlaps(candidate, blockers)
            if blocker is None:
                placed_box = candidate
                cur_x += w + spacing
                row_height = max(row_height, h)
                break
            else:
                cur_x = blocker[1] + spacing

        results.append(placed_box)
        if placed_box is not None:
            blockers.append(placed_box)

    return results


def grid_fit_pack(aspect_ratios, bounds, spacing_frac=0.04, margin=0.0):
    """Arranges `aspect_ratios` (one width/height ratio per item, e.g.
    from an image file's own pixel size) into a grid that fills `bounds`
    = (min_x, max_x, min_y, max_y) as fully as possible - used by
    DeeAligner's Super Image tab to lay out freshly-inserted images
    ("scale all the selected images to fit the sheet", per spec).

    `margin` (same unit as `bounds`) insets the usable area on all four
    sides BEFORE laying out the grid - a "safe offset from the border"
    so images never sit flush against the sheet's title block bounds,
    per spec. Applied once to the whole area, not per-cell (spacing_frac
    still controls the gaps BETWEEN cells, inside that inset area).

    Tries every column count from 1 to len(aspect_ratios) and keeps
    whichever grid covers the most total area once each item is
    CONTAIN-fit into its own cell (scaled to fit inside the cell without
    cropping or distorting its own aspect ratio, then centered in that
    cell) - a simple, deterministic, testable "best fit" heuristic. Not
    a global optimum, but it naturally prefers wide grids for mostly-
    landscape images and tall grids for mostly-portrait ones, rather
    than forcing every image into a fixed square grid regardless of
    shape (which is what a naive ceil(sqrt(count)) grid would do and
    would leave large gaps in most cells).

    Returns a same-length list of (min_x, max_x, min_y, max_y) boxes.
    An empty input or degenerate bounds (zero/negative width or height
    once the margin is applied) returns an empty list / a list of None
    respectively."""
    import math
    n = len(aspect_ratios)
    if n <= 0:
        return []
    min_x, max_x, min_y, max_y = bounds
    min_x += margin
    max_x -= margin
    min_y += margin
    max_y -= margin
    total_w = max_x - min_x
    total_h = max_y - min_y
    if total_w <= 0 or total_h <= 0:
        return [None] * n

    best_boxes = None
    best_coverage = -1.0
    for cols in range(1, n + 1):
        rows = int(math.ceil(n / float(cols)))
        spacing_x = spacing_frac * total_w / cols
        spacing_y = spacing_frac * total_h / rows
        cell_w = (total_w - spacing_x * (cols - 1)) / cols
        cell_h = (total_h - spacing_y * (rows - 1)) / rows
        if cell_w <= 0 or cell_h <= 0:
            continue

        boxes = []
        coverage = 0.0
        for i, ar in enumerate(aspect_ratios):
            col = i % cols
            row = i // cols
            cell_min_x = min_x + col * (cell_w + spacing_x)
            cell_max_y = max_y - row * (cell_h + spacing_y)
            cell_min_y = cell_max_y - cell_h
            safe_ar = ar if ar > 0 else 1.0
            if cell_w / cell_h > safe_ar:
                h = cell_h
                w = h * safe_ar
            else:
                w = cell_w
                h = w / safe_ar
            cx = cell_min_x + cell_w / 2.0
            cy = cell_min_y + cell_h / 2.0
            boxes.append((cx - w / 2.0, cx + w / 2.0, cy - h / 2.0, cy + h / 2.0))
            coverage += w * h

        if coverage > best_coverage:
            best_coverage = coverage
            best_boxes = boxes

    return best_boxes


def grid_layout_centers(count, bounds, margin_frac=0.05):
    """bounds: (min_x, max_x, min_y, max_y) of the available space.
    Returns `count` (center_x, center_y) positions arranged in a roughly
    square grid (ceil(sqrt(count)) columns), filling left-to-right,
    top-to-bottom, evenly spaced with a margin - used by DeeAligner's
    Auto-Arrange for images. Does not know about or avoid any other
    geometry (e.g. viewports) already on the sheet."""
    import math
    if count <= 0:
        return []
    min_x, max_x, min_y, max_y = bounds
    cols = int(math.ceil(math.sqrt(count)))
    rows = int(math.ceil(count / float(cols)))
    margin_x = margin_frac * (max_x - min_x)
    margin_y = margin_frac * (max_y - min_y)
    avail_w = (max_x - min_x) - 2 * margin_x
    avail_h = (max_y - min_y) - 2 * margin_y
    cell_w = avail_w / cols
    cell_h = avail_h / rows
    centers = []
    for i in range(count):
        col = i % cols
        row = i // cols
        cx = min_x + margin_x + cell_w * (col + 0.5)
        cy = max_y - margin_y - cell_h * (row + 0.5)
        centers.append((cx, cy))
    return centers


if __name__ == "__main__":
    import unittest

    class GridFitPackTests(unittest.TestCase):
        def test_empty_input(self):
            self.assertEqual(grid_fit_pack([], (0, 10, 0, 10)), [])

        def test_degenerate_bounds(self):
            self.assertEqual(grid_fit_pack([1.0, 1.0], (0, 0, 0, 10)), [None, None])

        def test_count_and_containment(self):
            bounds = (0.0, 100.0, 0.0, 60.0)
            boxes = grid_fit_pack([1.5, 1.0, 0.75, 2.0, 1.0], bounds)
            self.assertEqual(len(boxes), 5)
            for b in boxes:
                self.assertIsNotNone(b)
                min_x, max_x, min_y, max_y = b
                self.assertGreaterEqual(min_x, bounds[0] - 1e-6)
                self.assertLessEqual(max_x, bounds[1] + 1e-6)
                self.assertGreaterEqual(min_y, bounds[2] - 1e-6)
                self.assertLessEqual(max_y, bounds[3] + 1e-6)

        def test_preserves_aspect_ratio(self):
            bounds = (0.0, 100.0, 0.0, 100.0)
            boxes = grid_fit_pack([2.0], bounds, spacing_frac=0.0)
            min_x, max_x, min_y, max_y = boxes[0]
            w = max_x - min_x
            h = max_y - min_y
            self.assertAlmostEqual(w / h, 2.0, places=6)

        def test_no_overlap_within_grid(self):
            bounds = (0.0, 100.0, 0.0, 100.0)
            boxes = grid_fit_pack([1.0, 1.0, 1.0, 1.0], bounds)
            for i in range(len(boxes)):
                for j in range(i + 1, len(boxes)):
                    self.assertFalse(boxes_overlap(boxes[i], boxes[j]))

        def test_margin_insets_every_box(self):
            bounds = (0.0, 100.0, 0.0, 100.0)
            margin = 10.0
            boxes = grid_fit_pack([1.0, 1.0, 1.0], bounds, margin=margin)
            for b in boxes:
                self.assertIsNotNone(b)
                min_x, max_x, min_y, max_y = b
                self.assertGreaterEqual(min_x, bounds[0] + margin - 1e-6)
                self.assertLessEqual(max_x, bounds[1] - margin + 1e-6)
                self.assertGreaterEqual(min_y, bounds[2] + margin - 1e-6)
                self.assertLessEqual(max_y, bounds[3] - margin + 1e-6)

        def test_margin_too_large_returns_none(self):
            bounds = (0.0, 10.0, 0.0, 10.0)
            boxes = grid_fit_pack([1.0, 1.0], bounds, margin=6.0)
            self.assertEqual(boxes, [None, None])

    unittest.main()
