# -*- coding: utf-8 -*-
"""
dee_icons
Generates every DeePack ribbon icon from one shared visual language, so all
63 buttons look like one family instead of 63 unrelated placeholders.

Run it with plain CPython 3 + Pillow (NOT inside Revit - this is a build
tool, not a pyRevit script):

    python tools/dee_icons.py              # write all icons
    python tools/dee_icons.py --sheet      # also write a contact sheet
    python tools/dee_icons.py --check      # report coverage, write nothing

--------------------------------------------------------------------
The visual language
--------------------------------------------------------------------
- One 96x96 canvas for every button. pyRevit downsamples to 32px (large
  buttons) and 16px (stacked buttons), so the artwork is drawn simply
  enough to survive being shrunk to 16 pixels.
- Line art with a single filled accent, in the DeePack orange (#F2994D)
  already used by the branding footer and the DeeLazy card hover. Each
  icon puts the accent on the ONE thing the button actually does, so the
  eye lands on the verb rather than the noun.
- Everything is drawn 4x oversized and downsampled with LANCZOS, because
  Pillow has no anti-aliased stroke. Round caps/joins are faked by dotting
  a circle at each vertex - at 4x that is invisible and it stops thin
  diagonal strokes looking chipped.
- Backgrounds stay TRANSPARENT. Revit tints the ribbon differently across
  versions and themes, so a baked-in panel colour would eventually clash.

--------------------------------------------------------------------
Light and dark
--------------------------------------------------------------------
pyRevit loads `icon.dark.png` when Revit is in dark theme and `icon.png`
otherwise. Both are generated from the SAME geometry - only the ink colour
changes (near-black #2B2B2B for light, near-white #ECECEC for dark). The
accent orange is deliberately identical in both: it is legible on either
ground and it is what makes the set read as one brand.
"""
import argparse
import math
import os
import sys

from PIL import Image, ImageDraw

SIZE = 96
SS = 4                      # supersample factor
STROKE = 7.5                # base stroke width, in 96-space units
#   Tuned against the 16px stacked-button size, not the 96px source. At 6.0
#   strokes fell to roughly one pixel there and went faint; 8.0 started
#   filling in the denser glyphs (DeeGrid). 7.5 stays solid at 16px and
#   still reads cleanly at 32px and 48px.

PALETTES = {
    "light": {"ink": (43, 43, 43, 255), "accent": (242, 153, 77, 255)},
    "dark":  {"ink": (236, 236, 236, 255), "accent": (242, 153, 77, 255)},
}

HERE = os.path.dirname(os.path.abspath(__file__))
TAB_ROOT = os.path.join(os.path.dirname(HERE), "DeePack.tab")


# ==========================================================================
# primitive constructors - glyphs are plain data, so they stay readable
# ==========================================================================
def ln(x1, y1, x2, y2, c="ink", w=1.0):
    return ("line", x1, y1, x2, y2, c, w)


def pl(pts, c="ink", w=1.0, close=False):
    return ("poly", pts, c, w, close)


def pf(pts, c="accent"):
    return ("fill", pts, c)


def rc(x0, y0, x1, y1, c="ink", w=1.0, r=0):
    return ("rect", x0, y0, x1, y1, c, w, r)


def rf(x0, y0, x1, y1, c="accent", r=0):
    return ("rectf", x0, y0, x1, y1, c, r)


def ci(cx, cy, rad, c="ink", w=1.0):
    return ("circ", cx, cy, rad, c, w)


def cf(cx, cy, rad, c="accent"):
    return ("circf", cx, cy, rad, c)


def ar(x0, y0, x1, y1, start, end, c="ink", w=1.0):
    return ("arc", x0, y0, x1, y1, start, end, c, w)


# -- reusable motifs -------------------------------------------------------
def cloud(cx=48, cy=44, s=1.0, c="ink", w=1.0):
    """The shared cloud silhouette, so every ACC tool is recognisably one
    family. Drawn as an arc-and-base outline rather than blobs, because
    overlapping circles turn to mush at 16px."""
    def X(v):
        return cx + (v - 48) * s

    def Y(v):
        return cy + (v - 44) * s
    return [pl([(X(26), Y(56)), (X(22), Y(52)), (X(22), Y(44)), (X(29), Y(39)),
                (X(32), Y(30)), (X(42), Y(26)), (X(52), Y(29)), (X(57), Y(36)),
                (X(66), Y(38)), (X(70), Y(46)), (X(67), Y(55)), (X(62), Y(56))],
               c, w),
            ln(X(26), Y(56), X(62), Y(56), c, w)]


def sheet(x0=22, y0=18, x1=74, y1=78, c="ink", w=1.0):
    """Sheet with a title-block corner - the shared 'drawing' motif."""
    return [rc(x0, y0, x1, y1, c, w, 3),
            ln(x1 - 20, y1 - 16, x1, y1 - 16, c, w * 0.8),
            ln(x1 - 20, y1 - 16, x1 - 20, y1, c, w * 0.8)]


def doc(x0=26, y0=16, x1=70, y1=80, c="ink", w=1.0, fold=14):
    return [pl([(x0, y0), (x1 - fold, y0), (x1, y0 + fold), (x1, y1), (x0, y1)],
               c, w, True),
            pl([(x1 - fold, y0), (x1 - fold, y0 + fold), (x1, y0 + fold)], c, w)]


def arrow(x1, y1, x2, y2, c="accent", w=1.2, head=10.0):
    """Line with a solid triangular head - the accent verb in most icons."""
    ang = math.atan2(y2 - y1, x2 - x1)
    bx = x2 - head * math.cos(ang)
    by = y2 - head * math.sin(ang)
    left = (bx + head * 0.5 * math.cos(ang + math.pi / 2),
            by + head * 0.5 * math.sin(ang + math.pi / 2))
    right = (bx + head * 0.5 * math.cos(ang - math.pi / 2),
             by + head * 0.5 * math.sin(ang - math.pi / 2))
    return [ln(x1, y1, bx, by, c, w), pf([(x2, y2), left, right], c)]


def magnifier(cx=60, cy=58, r=14, c="accent", w=1.2):
    return [ci(cx, cy, r, c, w),
            ln(cx + r * 0.72, cy + r * 0.72, cx + r * 1.5, cy + r * 1.5, c, w * 1.1)]


def refresh(cx=48, cy=48, r=22, c="accent", w=1.2):
    """Two-thirds circle with an arrowhead - sync / update / re-run."""
    ops = [ar(cx - r, cy - r, cx + r, cy + r, 40, 320, c, w)]
    ops += pf([(cx + r * 0.94, cy - r * 0.06), (cx + r * 0.52, cy - r * 0.42),
               (cx + r * 0.52, cy + r * 0.34)], c),
    return ops


def bars(y_list, x0, x1, c="ink", w=1.0, h=9):
    out = []
    for y in y_list:
        out.append(rc(x0, y - h / 2.0, x1, y + h / 2.0, c, w, 2))
    return out


def person(cx=48, cy=44, s=1.0, c="ink", w=1.0):
    return [ci(cx, cy - 12 * s, 9 * s, c, w),
            ar(cx - 17 * s, cy - 2 * s, cx + 17 * s, cy + 32 * s, 200, 340, c, w)]


def table(x0=20, y0=24, x1=76, y1=74, rows=3, cols=3, c="ink", w=1.0):
    ops = [rc(x0, y0, x1, y1, c, w, 3)]
    for i in range(1, rows):
        y = y0 + (y1 - y0) * i / float(rows)
        ops.append(ln(x0, y, x1, y, c, w * 0.7))
    for i in range(1, cols):
        x = x0 + (x1 - x0) * i / float(cols)
        ops.append(ln(x, y0, x, y1, c, w * 0.7))
    return ops


def cube(cx=48, cy=48, s=22, c="ink", w=1.0):
    """Isometric box - the shared '3D model' motif."""
    return [pl([(cx, cy - s), (cx + s, cy - s * 0.5), (cx + s, cy + s * 0.5),
                (cx, cy + s), (cx - s, cy + s * 0.5), (cx - s, cy - s * 0.5)],
               c, w, True),
            pl([(cx - s, cy - s * 0.5), (cx, cy), (cx + s, cy - s * 0.5)], c, w),
            ln(cx, cy, cx, cy + s, c, w)]


def link(c="ink", w=1.0, accent_second=True):
    """Two interlocking capsules - the shared 'link' motif."""
    c2 = "accent" if accent_second else c
    return [rc(20, 38, 54, 58, c, w, 10), rc(42, 38, 76, 58, c2, w, 10)]


def crosshair(cx=48, cy=48, r=18, c="accent", w=1.2):
    return [ci(cx, cy, r, c, w), ln(cx - r - 8, cy, cx + r + 8, cy, c, w * 0.8),
            ln(cx, cy - r - 8, cx, cy + r + 8, c, w * 0.8)]


# ==========================================================================
# the glyphs - one entry per pushbutton folder name
# ==========================================================================
def _align_glyph(edge):
    """Shared builder for the six Align buttons: three bars plus a bold
    accent guide on whichever edge is being aligned to."""
    items = [(26, 30, 70, 42), (26, 46, 58, 58), (26, 62, 78, 74)]
    if edge in ("top", "middle", "bottom"):
        items = [(30, 26, 42, 70), (46, 26, 58, 58), (62, 26, 74, 78)]
    ops = []
    for x0, y0, x1, y1 in items:
        ops.append(rc(x0, y0, x1, y1, "ink", 0.85, 2))
    if edge == "left":
        ops.append(ln(20, 22, 20, 82, "accent", 1.5))
    elif edge == "right":
        ops.append(ln(84, 22, 84, 82, "accent", 1.5))
    elif edge == "center":
        ops.append(ln(52, 22, 52, 82, "accent", 1.5))
    elif edge == "top":
        ops.append(ln(22, 20, 82, 20, "accent", 1.5))
    elif edge == "bottom":
        ops.append(ln(22, 84, 82, 84, "accent", 1.5))
    elif edge == "middle":
        ops.append(ln(22, 52, 82, 52, "accent", 1.5))
    return ops


GLYPHS = {
    # ---------------- Models ----------------
    "DeeSYNC": lambda: doc(24, 20, 62, 76, "ink", 0.9) + refresh(62, 62, 17),
    "DeeOpener": lambda: [
        pl([(16, 72), (16, 30), (40, 30), (46, 38), (74, 38)], "ink", 1.0),
        pl([(16, 72), (26, 46), (86, 46), (76, 72)], "ink", 1.0, True),
    ] + arrow(52, 22, 52, 40, "accent", 1.2, 11),
    "DeeCloseAll": lambda: [
        rc(18, 22, 58, 62, "ink", 0.9, 3), rc(30, 34, 70, 74, "ink", 0.9, 3),
        ln(56, 60, 78, 82, "accent", 1.4), ln(78, 60, 56, 82, "accent", 1.4),
    ],

    # ---------------- Cloud ----------------
    "DeePublisher": lambda: cloud() + arrow(48, 84, 48, 62, "accent", 1.3, 12),
    "DeeSPublish": lambda: cloud() + arrow(36, 86, 36, 64, "accent", 1.2, 11)
                                   + arrow(60, 86, 60, 64, "accent", 1.2, 11),
    "DeeWSharing": lambda: cloud(48, 40, 0.85) + [
        cf(30, 78, 6), cf(66, 78, 6), cf(48, 66, 6),
        ln(30, 78, 48, 66, "accent", 1.0), ln(66, 78, 48, 66, "accent", 1.0),
    ],
    "DeeWBatchSaveToCloud": lambda: cloud(48, 36, 0.8) + [
        rc(28, 60, 56, 84, "ink", 0.9, 2), rc(36, 54, 64, 78, "ink", 0.9, 2),
    ] + arrow(70, 78, 70, 56, "accent", 1.2, 11),
    "DeeWClean": lambda: cloud(44, 40, 0.85) + [
        ln(70, 60, 56, 82, "ink", 1.1), pf([(52, 80), (64, 86), (58, 74)]),
        cf(76, 46, 4), cf(84, 58, 3),
    ],
    "DeeWTransmit": lambda: cloud(40, 36, 0.8) + [
        pl([(48, 62), (48, 84), (86, 84), (86, 62)], "ink", 1.0),
        ln(48, 62, 86, 62, "ink", 1.0),
    ] + arrow(67, 78, 67, 56, "accent", 1.2, 10),
    "DeeWAudit": lambda: cloud(42, 38, 0.85) + magnifier(64, 64, 15),
    "DeeWBatchUpgrade": lambda: cloud(48, 38, 0.85) + [
        pl([(34, 78), (48, 64), (62, 78)], "accent", 1.4),
        pl([(34, 90), (48, 76), (62, 90)], "accent", 1.4),
    ],
    "DeeWConsume": lambda: cloud(48, 36, 0.85) + arrow(48, 60, 48, 86, "accent", 1.3, 12),
    "DeeWPackage": lambda: cloud(48, 32, 0.72) + [
        pl([(28, 58), (48, 50), (68, 58), (68, 80), (48, 88), (28, 80)], "ink", 1.0, True),
        ln(28, 58, 48, 66, "ink", 0.9), ln(68, 58, 48, 66, "ink", 0.9),
        ln(48, 66, 48, 88, "accent", 1.1),
    ],
    "DeeWPublish": lambda: cloud(44, 36, 0.8) + [
        pf([(30, 74), (86, 56), (56, 88), (50, 74)]), ln(50, 74, 86, 56, "ink", 0.8),
    ],
    "DeeWSync": lambda: cloud(48, 34, 0.75) + refresh(48, 68, 17),
    "DeeWTransfer": lambda: cloud(48, 34, 0.75) + arrow(26, 62, 70, 62, "accent", 1.2, 11)
                                                + arrow(70, 80, 26, 80, "accent", 1.2, 11),
    "DeeWCloud": lambda: cloud(48, 42, 1.05) + [
        pl([(34, 72), (48, 84), (62, 72)], "accent", 1.5),
    ],
    "DeeGUID": lambda: [rc(16, 26, 80, 70, "ink", 1.0, 4)] + person(36, 44, 0.62, "ink", 0.9) + [
        ln(56, 40, 72, 40, "accent", 1.1), ln(56, 50, 72, 50, "accent", 1.1),
        ln(56, 60, 66, 60, "ink", 0.9),
    ],
    "DeeRelinquish": lambda: person(40, 40, 0.85) + [
        rc(58, 56, 84, 78, "accent", 1.2, 3),
        ar(62, 42, 80, 62, 180, 340, "accent", 1.2),
    ],

    # ---------------- Coordination ----------------
    "DeeCoord": lambda: cube(40, 46, 20, "ink", 0.9) + [
        ci(68, 68, 15, "accent", 1.2), ln(62, 68, 74, 68, "accent", 1.1),
        ln(68, 62, 68, 74, "accent", 1.1),
    ],
    "DeeBIMview": lambda: cube(48, 40, 21, "ink", 0.9) + [
        pl([(24, 76), (48, 62), (72, 76)], "accent", 1.2),
        pl([(24, 76), (48, 90), (72, 76)], "accent", 1.2), cf(48, 76, 5),
    ],
    "DeeLinkReview": lambda: [rc(16, 36, 50, 56, "ink", 1.0, 10),
                              rc(38, 36, 72, 56, "ink", 1.0, 10)] + magnifier(62, 68, 14),
    "DeeSuperLINK": lambda: link() + [
        pf([(48, 12), (52, 24), (64, 24), (54, 31), (58, 43), (48, 36),
            (38, 43), (42, 31), (32, 24), (44, 24)]),
    ],
    "DeeLINK": lambda: link(),
    "DeeRelink": lambda: [rc(14, 30, 48, 50, "ink", 1.0, 10),
                          rc(36, 30, 70, 50, "ink", 1.0, 10)] + refresh(56, 70, 16),

    # ---------------- SheetsExport ----------------
    "DeeSheet": lambda: sheet() + [
        rf(26, 24, 46, 32), ln(26, 40, 70, 40, "ink", 0.8),
        ln(26, 50, 70, 50, "ink", 0.8), ln(48, 24, 48, 62, "ink", 0.8),
    ],
    "DeePrinter": lambda: [
        rc(26, 18, 70, 38, "ink", 0.95, 2),
        rc(16, 38, 80, 66, "ink", 1.0, 4),
        rf(60, 46, 72, 54), rc(28, 60, 68, 84, "accent", 1.2, 2),
    ],
    "DeeAligner": lambda: [
        rc(18, 18, 78, 84, "ink", 0.85, 3),
        rc(26, 26, 52, 46, "ink", 0.9, 2), rc(26, 54, 52, 76, "ink", 0.9, 2),
        ln(60, 22, 60, 80, "accent", 1.5),
    ],
    "DeeNWCs": lambda: cube(40, 46, 19, "ink", 0.9) + arrow(64, 70, 86, 48, "accent", 1.3, 12),
    "DeeScheduleXL": lambda: table(18, 22, 66, 72, 3, 2, "ink", 0.95) + [
        ln(58, 58, 84, 84, "accent", 1.5), ln(84, 58, 58, 84, "accent", 1.5),
    ],

    # ---------------- ViewsDatums ----------------
    "DeeView": lambda: [
        rc(16, 20, 56, 60, "ink", 0.95, 3), rc(30, 34, 70, 74, "ink", 0.95, 3),
        ln(78, 52, 78, 78, "accent", 1.4), ln(65, 65, 91, 65, "accent", 1.4),
    ],
    "DeeVTemplate": lambda: [
        rc(16, 18, 54, 58, "ink", 0.95, 3), rf(20, 22, 50, 30),
        rc(38, 40, 78, 80, "accent", 1.2, 3),
    ] + arrow(56, 34, 70, 34, "accent", 1.1, 9),
    "DeeViewAdjust": lambda: [
        pl([(30, 24), (18, 24), (18, 36)], "accent", 1.5),
        pl([(66, 24), (78, 24), (78, 36)], "accent", 1.5),
        pl([(30, 76), (18, 76), (18, 64)], "accent", 1.5),
        pl([(66, 76), (78, 76), (78, 64)], "accent", 1.5),
        pl([(32, 42), (46, 32), (64, 44), (58, 66), (36, 62)], "ink", 1.1, True),
    ],
    "DeeLevels": lambda: [
        ln(14, 30, 68, 30, "ink", 1.0), ln(14, 50, 68, 50, "ink", 1.0),
        ln(14, 70, 68, 70, "ink", 1.0),
        ci(78, 30, 8, "accent", 1.1), ci(78, 50, 8, "ink", 0.9), ci(78, 70, 8, "ink", 0.9),
    ],
    "DeeGrid": lambda: [
        ln(28, 12, 28, 70, "ink", 1.0), ln(50, 12, 50, 70, "ink", 1.0),
        ln(14, 32, 74, 32, "ink", 1.0), ln(14, 54, 74, 54, "ink", 1.0),
        ci(28, 79, 8, "accent", 1.1), ci(50, 79, 8, "ink", 0.9), ci(83, 32, 8, "ink", 0.9),
    ],
    "DeeReLevel": lambda: [
        ln(14, 34, 62, 34, "ink", 1.0), ln(14, 66, 62, 66, "ink", 1.0),
        ci(72, 34, 8, "ink", 0.9), ci(72, 66, 8, "ink", 0.9),
    ] + arrow(38, 62, 38, 40, "accent", 1.3, 11),
    "DeeCordiPoint": lambda: crosshair(48, 46, 17) + [
        ln(14, 78, 82, 78, "ink", 1.0), cf(48, 46, 5),
    ],

    # ---------------- Rooms ----------------
    "DeeFinisher": lambda: [
        pl([(20, 26), (76, 26), (76, 70), (20, 70)], "ink", 1.0, True),
        rf(20, 62, 76, 70), ln(20, 38, 76, 38, "ink", 0.8),
        cf(48, 50, 6),
    ],
    "DeeDistributor": lambda: [
        pl([(18, 24), (78, 24), (78, 78), (18, 78)], "ink", 1.0, True),
        cf(34, 40, 6), cf(62, 40, 6), cf(34, 62, 6), cf(62, 62, 6), cf(48, 51, 6),
    ],
    "DeeRoomXYD": lambda: [
        pl([(20, 26), (76, 26), (76, 74), (20, 74)], "ink", 1.0, True),
    ] + arrow(26, 84, 70, 84, "accent", 1.2, 10) + arrow(88, 32, 88, 68, "accent", 1.2, 10),
    "DeeRoomStamp": lambda: [
        pl([(18, 30), (66, 30), (66, 74), (18, 74)], "ink", 1.0, True),
        rf(52, 16, 88, 40, "accent", 3), ln(58, 24, 82, 24, "ink", 0.8),
        ln(58, 32, 74, 32, "ink", 0.8),
    ],

    # ---------------- Quantities ----------------
    "DeeQs": lambda: table(16, 20, 64, 76, 3, 2, "ink", 0.95) + [
        pl([(70, 30), (90, 30), (76, 50), (90, 70), (70, 70)], "accent", 1.4),
    ],

    # ---------------- Align ----------------
    "AlignLeft": lambda: _align_glyph("left"),
    "AlignCenter": lambda: _align_glyph("center"),
    "AlignRight": lambda: _align_glyph("right"),
    "AlignTop": lambda: _align_glyph("top"),
    "AlignMiddle": lambda: _align_glyph("middle"),
    "AlignBottom": lambda: _align_glyph("bottom"),
    "DistributeHorizontal": lambda: [
        rc(14, 30, 28, 66, "ink", 0.9, 2), rc(41, 30, 55, 66, "accent", 1.2, 2),
        rc(68, 30, 82, 66, "ink", 0.9, 2),
        ln(28, 78, 41, 78, "accent", 1.1), ln(55, 78, 68, 78, "accent", 1.1),
    ],
    "DistributeVertical": lambda: [
        rc(30, 14, 66, 28, "ink", 0.9, 2), rc(30, 41, 66, 55, "accent", 1.2, 2),
        rc(30, 68, 66, 82, "ink", 0.9, 2),
        ln(78, 28, 78, 41, "accent", 1.1), ln(78, 55, 78, 68, "accent", 1.1),
    ],

    # ---------------- Health ----------------
    "DeeHealth": lambda: [
        rc(22, 18, 74, 82, "ink", 1.0, 4), rc(38, 12, 58, 24, "ink", 0.9, 3),
        pl([(28, 54), (38, 54), (44, 42), (52, 66), (58, 54), (68, 54)], "accent", 1.4),
    ],
    "DeeCleaner": lambda: [
        ln(64, 20, 42, 54, "ink", 1.2),
        pf([(30, 50), (54, 64), (40, 88), (20, 74)]),
        ln(30, 50, 54, 64, "ink", 0.8),
        cf(76, 34, 5), cf(84, 50, 4),
    ],
    # CAD doc dragged back to the internal origin, so the origin has to read
    # as a real target rather than a bare arrowhead.
    "DeeGetDWG": lambda: doc(14, 14, 50, 60, "ink", 0.95, 11) + [
        ln(22, 38, 42, 38, "ink", 0.8), ln(22, 48, 36, 48, "ink", 0.8),
        ci(74, 72, 13, "ink", 0.9), cf(74, 72, 5),
        ln(74, 53, 74, 91, "ink", 0.7), ln(55, 72, 93, 72, "ink", 0.7),
    ] + arrow(44, 66, 60, 70, "accent", 1.3, 11),
    "DeeRehoster": lambda: [
        ln(14, 26, 82, 26, "ink", 1.0), ln(14, 78, 82, 78, "ink", 1.0),
        rc(34, 34, 62, 56, "ink", 0.95, 2),
    ] + arrow(48, 60, 48, 74, "accent", 1.3, 10),

    # ---------------- Tools ----------------
    "DeeAI": lambda: [
        pl([(16, 24), (80, 24), (80, 64), (44, 64), (30, 80), (30, 64), (16, 64)],
           "ink", 1.0, True),
        pf([(48, 30), (52, 40), (62, 44), (52, 48), (48, 58), (44, 48),
            (34, 44), (44, 40)]),
    ],
    "DeeLazy": lambda: [
        rc(14, 18, 44, 44, "ink", 0.9, 3), rc(52, 18, 82, 44, "ink", 0.9, 3),
        rc(14, 52, 44, 78, "ink", 0.9, 3), rf(52, 52, 82, 78, "accent", 3),
    ],
    "DeeBlocktoFamily": lambda: [
        rc(12, 34, 40, 62, "ink", 1.0, 2),
    ] + arrow(46, 48, 62, 48, "accent", 1.3, 11) + [
        pl([(70, 30), (90, 42), (90, 62), (70, 74), (68, 62), (68, 42)], "ink", 1.0, True),
    ],
    "DeeAssemb": lambda: cube(34, 40, 18, "ink", 0.9) + [
        rc(52, 44, 88, 86, "accent", 1.2, 3), ln(58, 56, 82, 56, "ink", 0.8),
        ln(58, 66, 82, 66, "ink", 0.8), ln(58, 76, 72, 76, "ink", 0.8),
    ],

    # ---------------- About ----------------
    # Toggle switches, deliberately NOT lines-with-dots: that reads as
    # DeeLevels' level heads at 16px.
    "DeeControl": lambda: [
        rc(16, 20, 64, 40, "ink", 0.95, 10), cf(52, 30, 7),
        rc(16, 46, 64, 66, "ink", 0.95, 10), cf(28, 56, 7, "ink"),
        rc(16, 72, 64, 92, "ink", 0.95, 10), cf(52, 82, 7),
    ],
    "WebSite": lambda: [
        ci(48, 48, 32, "ink", 1.0), ln(16, 48, 80, 48, "ink", 0.85),
        ar(30, 16, 66, 80, 90, 270, "ink", 0.85), ar(30, 16, 66, 80, 270, 90, "ink", 0.85),
        cf(48, 48, 5),
    ],
    "WhatsApp": lambda: [
        pl([(16, 22), (80, 22), (80, 62), (42, 62), (26, 80), (26, 62), (16, 62)],
           "ink", 1.0, True),
        cf(34, 42, 5), cf(48, 42, 5), cf(62, 42, 5),
    ],
    "Update": lambda: refresh(48, 42, 24, "ink", 1.2) + arrow(48, 58, 48, 86, "accent", 1.3, 12),
    "Version": lambda: [
        ci(48, 48, 32, "ink", 1.0), cf(48, 32, 5),
        ln(48, 44, 48, 66, "accent", 1.5),
    ],
}


# ==========================================================================
# renderer
# ==========================================================================
def _rgba(palette, name):
    return palette[name]


def render(ops, theme):
    palette = PALETTES[theme]
    img = Image.new("RGBA", (SIZE * SS, SIZE * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def S(v):
        return v * SS

    def cap(x, y, w, colour):
        r = w / 2.0
        d.ellipse([S(x) - r, S(y) - r, S(x) + r, S(y) + r], fill=colour)

    def stroke(x1, y1, x2, y2, w, colour):
        d.line([S(x1), S(y1), S(x2), S(y2)], fill=colour, width=int(round(w)))
        cap(x1, y1, w, colour)
        cap(x2, y2, w, colour)

    for op in ops:
        kind = op[0]
        if kind == "line":
            _, x1, y1, x2, y2, c, w = op
            stroke(x1, y1, x2, y2, STROKE * w * SS / 2.0 * 2 / 2.0, _rgba(palette, c))
        elif kind == "poly":
            _, pts, c, w, close = op
            colour = _rgba(palette, c)
            width = STROKE * w * SS / 2.0
            seq = list(pts) + ([pts[0]] if close else [])
            for i in range(len(seq) - 1):
                stroke(seq[i][0], seq[i][1], seq[i + 1][0], seq[i + 1][1], width, colour)
        elif kind == "fill":
            _, pts, c = op
            d.polygon([(S(p[0]), S(p[1])) for p in pts], fill=_rgba(palette, c))
        elif kind == "rect":
            _, x0, y0, x1, y1, c, w, r = op
            colour = _rgba(palette, c)
            width = int(round(STROKE * w * SS / 2.0))
            if r:
                d.rounded_rectangle([S(x0), S(y0), S(x1), S(y1)], radius=S(r),
                                    outline=colour, width=width)
            else:
                d.rectangle([S(x0), S(y0), S(x1), S(y1)], outline=colour, width=width)
        elif kind == "rectf":
            _, x0, y0, x1, y1, c, r = op
            colour = _rgba(palette, c)
            if r:
                d.rounded_rectangle([S(x0), S(y0), S(x1), S(y1)], radius=S(r), fill=colour)
            else:
                d.rectangle([S(x0), S(y0), S(x1), S(y1)], fill=colour)
        elif kind == "circ":
            _, cx, cy, rad, c, w = op
            d.ellipse([S(cx - rad), S(cy - rad), S(cx + rad), S(cy + rad)],
                      outline=_rgba(palette, c), width=int(round(STROKE * w * SS / 2.0)))
        elif kind == "circf":
            _, cx, cy, rad, c = op
            d.ellipse([S(cx - rad), S(cy - rad), S(cx + rad), S(cy + rad)],
                      fill=_rgba(palette, c))
        elif kind == "arc":
            _, x0, y0, x1, y1, start, end, c, w = op
            d.arc([S(x0), S(y0), S(x1), S(y1)], start, end,
                  fill=_rgba(palette, c), width=int(round(STROKE * w * SS / 2.0)))

    return img.resize((SIZE, SIZE), Image.LANCZOS)


# ==========================================================================
# discovery + writing
# ==========================================================================
ICONED_SUFFIXES = (".pushbutton", ".pulldown")


def find_buttons(tab_root):
    """{folder_name: absolute path} for everything that shows its own icon on
    the ribbon. A .pulldown has a face of its own, so it needs artwork just as
    much as a .pushbutton does - missing it would leave one odd icon out."""
    found = {}
    for root, dirs, _files in os.walk(tab_root):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for d in dirs:
            for suffix in ICONED_SUFFIXES:
                if d.endswith(suffix):
                    found[d[:-len(suffix)]] = os.path.join(root, d)
                    break
    return found


def write_all(tab_root, dry_run=False):
    buttons = find_buttons(tab_root)
    written, missing = [], []
    for name in sorted(buttons):
        builder = GLYPHS.get(name)
        if builder is None:
            missing.append(name)
            continue
        if dry_run:
            written.append(name)
            continue
        for theme, filename in (("light", "icon.png"), ("dark", "icon.dark.png")):
            render(builder(), theme).save(os.path.join(buttons[name], filename))
        written.append(name)
    unused = sorted(set(GLYPHS) - set(buttons))
    return written, missing, unused


def contact_sheet(tab_root, out_path):
    """One PNG showing every icon on both a light and a dark ground - the only
    honest way to check contrast before trusting 126 files."""
    buttons = sorted(n for n in find_buttons(tab_root) if n in GLYPHS)
    cols = 11
    cell = 112
    rows = (len(buttons) + cols - 1) // cols
    pad_top = 34
    block = pad_top + rows * cell
    sheet_img = Image.new("RGBA", (cols * cell, block * 2), (255, 255, 255, 255))
    d = ImageDraw.Draw(sheet_img)
    d.rectangle([0, 0, cols * cell, block], fill=(250, 250, 250, 255))
    d.rectangle([0, block, cols * cell, block * 2], fill=(45, 45, 45, 255))
    d.text((12, 12), "LIGHT THEME  -  icon.png", fill=(30, 30, 30, 255))
    d.text((12, block + 12), "DARK THEME  -  icon.dark.png", fill=(235, 235, 235, 255))

    for i, name in enumerate(buttons):
        cx = (i % cols) * cell
        cy = pad_top + (i // cols) * cell
        light = render(GLYPHS[name](), "light")
        dark = render(GLYPHS[name](), "dark")
        sheet_img.paste(light, (cx + 8, cy + 4), light)
        sheet_img.paste(dark, (cx + 8, block + cy + 4), dark)
        label = name[:15]
        d.text((cx + 8, cy + 100), label, fill=(80, 80, 80, 255))
        d.text((cx + 8, block + cy + 100), label, fill=(190, 190, 190, 255))
    sheet_img.convert("RGB").save(out_path)
    return out_path, len(buttons)


def clipping_report(margin=1):
    """Any non-transparent pixel in the outer `margin` ring means the artwork
    is running off the canvas, which reads as a chopped icon once Revit scales
    it down. Returns [(name, edge_pixel_count)]."""
    bad = []
    for name in sorted(GLYPHS):
        img = render(GLYPHS[name](), "light")
        alpha = img.split()[3]
        w, h = alpha.size
        px = alpha.load()
        count = 0
        for x in range(w):
            for y in range(h):
                if (x < margin or y < margin or x >= w - margin or y >= h - margin)                         and px[x, y] > 8:
                    count += 1
        if count:
            bad.append((name, count))
    return bad


def main():
    parser = argparse.ArgumentParser(description="Generate DeePack ribbon icons.")
    parser.add_argument("--sheet", action="store_true", help="also write a contact sheet")
    parser.add_argument("--check", action="store_true", help="report coverage, write nothing")
    parser.add_argument("--clip", action="store_true", help="report artwork running off canvas")
    args = parser.parse_args()

    written, missing, unused = write_all(TAB_ROOT, dry_run=args.check)
    print("buttons with a glyph : {0}".format(len(written)))
    print("buttons MISSING one  : {0}".format(missing or "NONE"))
    print("glyphs with no button: {0}".format(unused or "NONE"))
    if not args.check:
        print("wrote {0} files ({1} buttons x 2 themes)".format(len(written) * 2, len(written)))
    if args.clip:
        bad = clipping_report()
        print("glyphs touching the canvas edge: {0}".format(
            ", ".join("{0} ({1}px)".format(n, c) for n, c in bad) if bad else "NONE"))
    if args.sheet:
        path, count = contact_sheet(TAB_ROOT, os.path.join(HERE, "icon_preview.png"))
        print("contact sheet: {0} ({1} icons)".format(path, count))
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
