# -*- coding: utf-8 -*-
"""Icon style probe - renders a few DeePack icons in every lib/dee_accent
style, with and without custom colours, to tools/checks/icon_styles/ so
the result can be looked at. Runs under IronPython or `pyrevit run`:

    pyrevit run "<abs path>/Dee.extension/tools/checks/icon_style_probe.py" --revit=2024

Writes a contact sheet icon_styles/sheet.png (one row per icon, one
column per style) and a line per file to icon_styles/report.txt.
"""
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
EXT = os.environ.get("DEE_EXTENSION_ROOT", os.path.dirname(os.path.dirname(HERE)))
LIB = os.path.join(EXT, "lib")
OUT = os.path.join(HERE, "icon_styles")
for p in (LIB, EXT):
    if p not in sys.path:
        sys.path.insert(0, p)

import clr  # noqa: E402
clr.AddReference("System.Drawing")
from System.Drawing import Bitmap, Graphics, Color, SolidBrush, Rectangle  # noqa: E402
from System.Drawing.Imaging import ImageFormat, PixelFormat  # noqa: E402
from System.Drawing.Drawing2D import InterpolationMode  # noqa: E402

import dee_accent  # noqa: E402

BUTTONS = ["DeePublisher", "DeeSuperLINK", "Dee3DView", "DeeCleaner", "DeeLazy", "DeeIcon"]
COLOURS = [("default", None, None), ("blue_lines", "#1F6FB2", "#2F3640")]
CELL = 48
lines = []

if not os.path.isdir(OUT):
    os.makedirs(OUT)
bundles = dee_accent._bundle_dirs()
srcs = [os.path.join(bundles[n], "icon.png") for n in BUTTONS if n in bundles]
styles = [k for k, _l, _d in dee_accent.STYLES]
rows = len(srcs) * len(COLOURS)
sheet = Bitmap(CELL * len(styles) + 8, CELL * rows + 8, PixelFormat.Format32bppArgb)
g = Graphics.FromImage(sheet)
g.InterpolationMode = InterpolationMode.HighQualityBicubic
g.FillRectangle(SolidBrush(Color.FromArgb(255, 0x2F, 0x2F, 0x2F)), 0, 0,
                sheet.Width, sheet.Height // 2)
g.FillRectangle(SolidBrush(Color.White), 0, sheet.Height // 2,
                sheet.Width, sheet.Height - sheet.Height // 2)
row = 0
for cname, accent, line in COLOURS:
    for src in srcs:
        for col, style in enumerate(styles):
            try:
                path = dee_accent.resolve(src, accent or dee_accent.ICON_ACCENT, line, style)
                ok = os.path.isfile(path)
                bmp = Bitmap(path)
                g.DrawImage(bmp, Rectangle(4 + col * CELL, 4 + row * CELL, CELL, CELL))
                bmp.Dispose()
                lines.append("OK   %-8s %-10s %s -> %s" % (style, cname, os.path.basename(
                    os.path.dirname(src)), path))
            except Exception:
                lines.append("FAIL %-8s %-10s %s\n%s" % (style, cname, src, traceback.format_exc()))
        row += 1
g.Dispose()
sheet.Save(os.path.join(OUT, "sheet.png"), ImageFormat.Png)
sheet.Dispose()
with open(os.path.join(OUT, "report.txt"), "w") as f:
    f.write("\n".join(lines))
print("\n".join(lines))
print("sheet: " + os.path.join(OUT, "sheet.png"))

