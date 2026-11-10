# -*- coding: utf-8 -*-
"""Theme probe - builds the "Try it" sample window of About -> Interface in
every lib/dee_themes theme (standard and rounded shape), starts and stops
every lib/dee_theme_motion motion on it, builds the Interface window itself, and
renders each to tools/checks/themes/<theme>_<shape>.png so the result can be
looked at. Nothing is shown on screen. Runs inside Revit via `pyrevit run`:

    pyrevit run "<abs path>/Dee.extension/tools/checks/theme_probe.py" --revit=2024

One line per step to themes/report.txt; the last line is PASS or FAIL.
"""
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
EXT = os.environ.get("DEE_EXTENSION_ROOT", os.path.dirname(os.path.dirname(HERE)))
LIB = os.path.join(EXT, "lib")
OUT = os.path.join(HERE, "themes")
for p in (LIB, EXT):
    if p not in sys.path:
        sys.path.insert(0, p)

import clr  # noqa: E402
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
clr.AddReference("WindowsBase")
from System.IO import FileStream, FileMode  # noqa: E402
from System.Windows import Size, Rect  # noqa: E402
from System.Windows.Media import PixelFormats, LinearGradientBrush  # noqa: E402
from System.Windows.Media.Imaging import RenderTargetBitmap, PngBitmapEncoder, BitmapFrame  # noqa: E402

lines = []
failed = []


def log(s):
    lines.append(s)
    try:
        print(s)
    except Exception:
        pass


def fail(what, err):
    failed.append(what)
    log("FAIL %s: %s" % (what, err))
    log(traceback.format_exc())


def render(element, w, h, path):
    element.Measure(Size(w, h))
    element.Arrange(Rect(0, 0, w, h))
    element.UpdateLayout()
    bmp = RenderTargetBitmap(int(w), int(h), 96, 96, PixelFormats.Pbgra32)
    bmp.Render(element)
    enc = PngBitmapEncoder()
    enc.Frames.Add(BitmapFrame.Create(bmp))
    fs = FileStream(path, FileMode.Create)
    try:
        enc.Save(fs)
    finally:
        fs.Close()


if not os.path.isdir(OUT):
    os.makedirs(OUT)

try:
    import dee_themes
    import dee_theme_motion as dee_motion
    import dee_effects
    import dee_interface_dialog
    log("OK   imports: themes %s" % ", ".join(dee_themes.KEYS))
except Exception as err:
    fail("imports", err)
    dee_themes = None

if dee_themes is not None:
    for key in dee_themes.KEYS:
        try:
            pal = dee_themes.palette(key)
            log("OK   palette %-9s base=%s motion=%s keys=%d" % (
                key, dee_themes.base(key), dee_themes.has_motion(key), len(pal or {})))
        except Exception as err:
            fail("palette " + key, err)
        for shape in ("standard", "rounded"):
            what = "%s/%s" % (key, shape)
            win = None
            try:
                win = dee_interface_dialog._ShapeSample(shape, key)
                if win._dee_theme != key:
                    raise Exception("window theme is %r" % win._dee_theme)
                dark = dee_themes.is_dark(key)
                if bool(win._dee_dark) != dark:
                    raise Exception("dark flag %r for a %s theme" % (win._dee_dark, dee_themes.base(key)))
                bg = win.TryFindResource("DeeWindowBg")
                text = win.TryFindResource("DeeText")
                log("OK   window  %-18s bg=%s text=%s" % (what, bg.Color, text.Color))
                dee_motion.apply(win, key, dee_effects.DEFAULTS, glide=True)
                moving = dee_themes.has_motion(key)
                is_grad = isinstance(win.TryFindResource("DeeWindowBg"), LinearGradientBrush)
                if is_grad != moving:
                    raise Exception("glow gradient %s, expected %s" % (is_grad, moving))
                st = getattr(win, "_dee_motion", None) or {}
                render(win.Content, 440, 260, os.path.join(OUT, "%s_%s.png" % (key, shape)))
                log("OK   motion  %-18s on=%s breathing=%d lifted=%d shimmer=%s" % (
                    what, st.get("key"), len(st.get("brushes", [])), len(st.get("lifted", [])),
                    st.get("bar") is not None))
                dee_motion.stop(win)
                if isinstance(win.TryFindResource("DeeWindowBg"), LinearGradientBrush):
                    raise Exception("glow still on after stop")
                if (st.get("lifted") or st.get("brushes") or st.get("bar") is not None
                        or st.get("key") is not None):
                    raise Exception("state left after stop: %r" % st)
                log("OK   stopped %-18s" % what)
            except Exception as err:
                fail(what, err)
            finally:
                try:
                    if win is not None:
                        win.Close()
                except Exception:
                    pass

    dlg = None
    try:
        dlg = dee_interface_dialog.InterfaceDialog(None)
        n = dlg.themes_panel.Children.Count
        if n != len(dee_themes.KEYS):
            raise Exception("%d theme cards for %d themes" % (n, len(dee_themes.KEYS)))
        for key in dee_themes.KEYS:
            dlg._effects["theme"] = key
            dlg._sync_theme_cards()
            dee_motion.apply(dlg, key, dlg._effects)
        dlg.tabs.SelectedIndex = 3
        render(dlg.Content, 580, 640, os.path.join(OUT, "interface.png"))
        dee_motion.stop(dlg)
        log("OK   interface window: %d theme cards, desc=%r" % (n, dlg.theme_desc_tb.Text))
    except Exception as err:
        fail("interface window", err)
    finally:
        try:
            if dlg is not None:
                dlg.Close()
        except Exception:
            pass

log("FAIL (%d): %s" % (len(failed), ", ".join(failed)) if failed else "PASS")
with open(os.path.join(OUT, "report.txt"), "w") as f:
    f.write("\n".join(lines))
