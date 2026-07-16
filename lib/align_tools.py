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
