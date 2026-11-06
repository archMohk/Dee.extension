# -*- coding: utf-8 -*-
"""
dee_shapes
The window shapes picked in About -> Interface -> Shapes. One outline
definition per shape, used three ways:
  - dee_branding clips each tool window to it and strokes its outline,
  - dee_effects turns it into a Windows region so Glass blurs only
    inside the shape,
  - dee_interface_dialog draws the little preview cards from it.

  standard  The normal Windows frame (no custom shape).
  rounded   Rounded corners and a slim title bar.
  soft      Extra-round corners and a soft shadow - the window floats.
  chamfer   Corners cut off at an angle, like a drawing sheet.
  leaf      Two opposite corners rounded wide, the other two square.
  ticket    Rounded, with a half-circle notch on each side at the
            title bar line.
"""
import clr
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")
from System.Windows import Point, Rect, Size  # noqa: E402
from System.Windows.Media import (  # noqa: E402
    RectangleGeometry, EllipseGeometry, CombinedGeometry, GeometryCombineMode,
    GeometryGroup, StreamGeometry, SweepDirection, ToleranceType,
    PolyLineSegment, LineSegment)

# (key, label, description) in the order the Shapes tab shows them.
SHAPES = [
    ("standard", "Standard", "The normal Windows frame."),
    ("rounded", "Rounded", "Rounded corners and a slim title bar."),
    ("soft", "Soft float", "Extra-round corners and a soft shadow - the window floats above Revit."),
    ("chamfer", "Cut corners", "Corners cut at an angle, like a drawing sheet."),
    ("leaf", "Leaf", "Two opposite corners rounded wide, the other two square."),
    ("ticket", "Ticket", "Rounded, with a notch on each side under the title bar."),
]
KEYS = [s[0] for s in SHAPES]
DEFAULT = "standard"

ROUND_R = 10.0      # rounded / ticket corners
SOFT_R = 18.0       # soft corners
SOFT_MARGIN = 12.0  # room around a soft window for its shadow
CHAMFER = 14.0      # cut-corner size
LEAF_R = 26.0       # leaf corners
NOTCH_R = 7.0       # ticket notches


def normalize(shape):
    return shape if shape in KEYS else DEFAULT


def margin(shape):
    """Transparent room a shape needs around it (the soft shadow)."""
    return SOFT_MARGIN if shape == "soft" else 0.0


def corner_radius(shape):
    return {"rounded": ROUND_R, "soft": SOFT_R, "ticket": ROUND_R}.get(shape, 0.0)


def _polygon(points):
    g = StreamGeometry()
    ctx = g.Open()
    ctx.BeginFigure(points[0], True, True)
    for p in points[1:]:
        ctx.LineTo(p, True, False)
    ctx.Close()
    return g


def _leaf(x0, y0, x1, y1, r):
    g = StreamGeometry()
    ctx = g.Open()
    ctx.BeginFigure(Point(x0, y0 + r), True, True)
    ctx.ArcTo(Point(x0 + r, y0), Size(r, r), 0, False, SweepDirection.Clockwise, True, False)
    ctx.LineTo(Point(x1, y0), True, False)
    ctx.LineTo(Point(x1, y1 - r), True, False)
    ctx.ArcTo(Point(x1 - r, y1), Size(r, r), 0, False, SweepDirection.Clockwise, True, False)
    ctx.LineTo(Point(x0, y1), True, False)
    ctx.Close()
    return g


def geometry(shape, rect, notch_y=30.0, grow=0.0):
    """The shape's outline filling `rect`. notch_y is where the ticket's
    notches sit (the title bar line); `grow` widens them, used when the
    outline is stroked a little inside the edge."""
    x0, y0 = rect.Left, rect.Top
    x1, y1 = rect.Right, rect.Bottom
    r = min(rect.Width, rect.Height) / 2.0
    if shape == "chamfer":
        c = min(CHAMFER, r)
        g = _polygon([Point(x0 + c, y0), Point(x1 - c, y0), Point(x1, y0 + c),
                      Point(x1, y1 - c), Point(x1 - c, y1), Point(x0 + c, y1),
                      Point(x0, y1 - c), Point(x0, y0 + c)])
    elif shape == "leaf":
        g = _leaf(x0, y0, x1, y1, min(LEAF_R, r))
    elif shape == "ticket":
        body = RectangleGeometry(rect, ROUND_R, ROUND_R)
        holes = GeometryGroup()
        n = NOTCH_R + grow
        holes.Children.Add(EllipseGeometry(Point(x0 - grow, notch_y), n, n))
        holes.Children.Add(EllipseGeometry(Point(x1 + grow, notch_y), n, n))
        g = CombinedGeometry(GeometryCombineMode.Exclude, body, holes)
    else:
        rad = min(corner_radius(shape), r)
        g = RectangleGeometry(rect, rad, rad)
    if g.CanFreeze:
        g.Freeze()
    return g


def polygons(geom, sx=1.0, sy=1.0):
    """The outline as lists of whole-pixel points (scaled by sx/sy, for
    a Windows region)."""
    out = []
    flat = geom.GetFlattenedPathGeometry(0.5, ToleranceType.Absolute)
    for fig in flat.Figures:
        pts = [fig.StartPoint]
        for seg in fig.Segments:
            if isinstance(seg, PolyLineSegment):
                pts.extend(list(seg.Points))
            elif isinstance(seg, LineSegment):
                pts.append(seg.Point)
        if len(pts) >= 3:
            out.append([(int(round(p.X * sx)), int(round(p.Y * sy))) for p in pts])
    return out
