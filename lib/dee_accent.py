# -*- coding: utf-8 -*-
"""
dee_accent
The personal "accent colour" - the orange in every DeePack ribbon icon,
the branding bar under every tool window and the DeeOrange theme brush.
Picked in DeeControl -> Accent Colour; stored per PC in
lib/.dee_accent.json (gitignored), so it never touches the shared repo.

How the icons are recoloured
----------------------------
tools/icons/dee_icons.py draws every icon in exactly two kinds of colour:
neutral grey ink (R = G = B, plus pure white details) and ONE accent,
ICON_ACCENT. So recolouring is a single linear colour transform

    M = I + (T - A) d^T / |d|^2,   d = A - mean(A) * (1, 1, 1)

which leaves every grey exactly where it was (d is perpendicular to
(1, 1, 1)) and sends the accent A to the new colour T. Because it is
linear, the anti-aliased edge pixels where ink meets accent are carried
along correctly too. GDI+ applies it natively through a ColorMatrix, so
recolouring all 86 buttons takes well under a second.

Recoloured copies go to %APPDATA%\\Dee\\accent_icons\\<hex>\\..., keyed by
colour, and are rebuilt whenever the source icon is newer (an update
redrew it). The ribbon then gets them through pyRevit's own ButtonIcons
- the same mechanism dee_force_state uses for DeeForce's on/off icon.

Nothing here ever raises into a caller: a failure just leaves the
default orange in place.
"""
import json
import os

import clr
clr.AddReference("System.Drawing")
from System.Drawing import Bitmap, Graphics, Rectangle, GraphicsUnit  # noqa: E402
from System.Drawing.Imaging import (ColorMatrix, ImageAttributes,  # noqa: E402
                                    PixelFormat, ImageFormat)
from System.Drawing.Drawing2D import CompositingMode  # noqa: E402

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_EXT_ROOT = os.path.dirname(_THIS_DIR)
_TAB_ROOT = os.path.join(_EXT_ROOT, "DeePack.tab")
_TAB_NAME = "DeePack"
_CONFIG_PATH = os.path.join(_THIS_DIR, ".dee_accent.json")
_ICONED_SUFFIXES = (".pushbutton", ".pulldown")

# The accent tools/icons/dee_icons.py bakes into every icon (its PALETTES
# "accent", identical in light and dark). Keep the two in step.
ICON_ACCENT = "#CC7020"
# The brand orange used by the window branding bar / DeeOrange brush.
BRAND_ORANGE = "#F2994D"

PRESETS = [
    ("Orange (default)", ICON_ACCENT),
    ("Blue", "#1F6FB2"),
    ("Teal", "#00897B"),
    ("Green", "#3E8E41"),
    ("Purple", "#7B4FB5"),
    ("Red", "#C0392B"),
    ("Pink", "#C2185B"),
    ("Gold", "#B8860B"),
    ("Slate", "#4A6278"),
]


# ---------------- colour helpers ----------------
def normalize_hex(value):
    """'#abc', 'abc', '#AABBCC' -> '#AABBCC'; anything else -> None."""
    if not value:
        return None
    v = value.strip().lstrip("#")
    if len(v) == 3:
        v = "".join(ch * 2 for ch in v)
    if len(v) != 6:
        return None
    try:
        int(v, 16)
    except ValueError:
        return None
    return "#" + v.upper()


def hex_to_rgb(value):
    v = normalize_hex(value) or ICON_ACCENT
    return int(v[1:3], 16), int(v[3:5], 16), int(v[5:7], 16)


def is_default(value):
    return normalize_hex(value) in (None, ICON_ACCENT)


# ---------------- the setting ----------------
def load_accent():
    """The saved accent as '#RRGGBB', or ICON_ACCENT when none is saved."""
    try:
        with open(_CONFIG_PATH, "r") as f:
            return normalize_hex(json.load(f).get("accent")) or ICON_ACCENT
    except Exception:
        return ICON_ACCENT


def save_accent(value):
    value = normalize_hex(value) or ICON_ACCENT
    try:
        with open(_CONFIG_PATH, "w") as f:
            f.write(json.dumps({"accent": value}, indent=2))
    except Exception:
        pass
    return value


def window_accent():
    """Colour for the window branding bar / DeeOrange: the brand orange
    unless the user picked their own accent."""
    accent = load_accent()
    return BRAND_ORANGE if is_default(accent) else accent


# ---------------- icon recolouring ----------------
def _matrix(target_hex):
    a = [c / 255.0 for c in hex_to_rgb(ICON_ACCENT)]
    t = [c / 255.0 for c in hex_to_rgb(target_hex)]
    mean = sum(a) / 3.0
    d = [c - mean for c in a]
    dd = sum(c * c for c in d)
    cm = ColorMatrix()   # starts as identity
    # GDI+ multiplies ROW vectors: out = in . G, so G[i][j] = M[j][i].
    for i in range(3):
        for j in range(3):
            value = (1.0 if i == j else 0.0) + (t[j] - a[j]) * d[i] / dd
            setattr(cm, "Matrix{0}{1}".format(i, j), value)
    return cm


def recolor_file(src, dst, target_hex):
    """Writes src recoloured to target_hex as a PNG at dst. True on success."""
    bmp = out = g = None
    try:
        bmp = Bitmap(src)
        w, h = bmp.Width, bmp.Height
        out = Bitmap(w, h, PixelFormat.Format32bppArgb)
        g = Graphics.FromImage(out)
        g.CompositingMode = CompositingMode.SourceCopy
        attrs = ImageAttributes()
        attrs.SetColorMatrix(_matrix(target_hex))
        g.DrawImage(bmp, Rectangle(0, 0, w, h), 0, 0, w, h, GraphicsUnit.Pixel, attrs)
        folder = os.path.dirname(dst)
        if not os.path.isdir(folder):
            os.makedirs(folder)
        out.Save(dst, ImageFormat.Png)
        return True
    except Exception:
        return False
    finally:
        for obj in (g, out, bmp):
            try:
                if obj is not None:
                    obj.Dispose()
            except Exception:
                pass


def _cache_root(target_hex):
    base = os.environ.get("APPDATA") or _THIS_DIR
    return os.path.join(base, "Dee", "accent_icons", target_hex.lstrip("#").upper())


def resolve(icon_path, target_hex=None):
    """Recoloured copy of icon_path for the current accent (built on
    demand, cached), or icon_path itself for the default accent or on any
    failure. Safe to call on any icon file."""
    try:
        target_hex = normalize_hex(target_hex) or load_accent()
        if is_default(target_hex) or not icon_path or not os.path.isfile(icon_path):
            return icon_path
        rel = os.path.relpath(os.path.abspath(icon_path), _EXT_ROOT)
        if rel.startswith(".."):
            rel = os.path.basename(icon_path)
        dst = os.path.join(_cache_root(target_hex), rel)
        if (not os.path.isfile(dst)
                or os.path.getmtime(dst) < os.path.getmtime(icon_path)):
            if not recolor_file(icon_path, dst, target_hex):
                return icon_path
        return dst
    except Exception:
        return icon_path


# ---------------- the live ribbon ----------------
def _bundle_dirs():
    found = {}
    for root, dirs, _files in os.walk(_TAB_ROOT):
        for d in dirs:
            for suffix in _ICONED_SUFFIXES:
                if d.endswith(suffix):
                    found[d[:-len(suffix)]] = os.path.join(root, d)
    return found


def _iter_items(uiapp):
    try:
        panels = uiapp.GetRibbonPanels(_TAB_NAME)
    except Exception:
        return
    for panel in panels:
        try:
            items = list(panel.GetItems())
        except Exception:
            continue
        for item in items:
            yield item
            try:
                nested = item.GetItems()
            except Exception:
                nested = None
            if nested:
                for child in nested:
                    yield child


def _theme_icon(bundle_dir):
    """The file pyRevit itself would pick (icon.dark.png in a dark Revit
    2024+), via pyRevit's own resolver so the two never disagree."""
    try:
        from pyrevit.revit.ui import resolve_icon_file
        path = resolve_icon_file(bundle_dir, "icon.png")
        if path:
            return path
    except Exception:
        pass
    path = os.path.join(bundle_dir, "icon.png")
    return path if os.path.isfile(path) else None


def _set_icon(item, path):
    try:
        from pyrevit.coreutils.ribbon import ButtonIcons
        icons = ButtonIcons(path)
        try:
            item.Image = icons.small_bitmap
        except Exception:
            pass
        try:
            item.LargeImage = icons.large_bitmap
        except Exception:
            pass
        return True
    except Exception:
        return False


def apply_to_ribbon(uiapp, target_hex=None):
    """Swaps every DeePack button's icon for the accent-coloured one
    (or back to the original files for the default accent). Returns how
    many buttons were updated."""
    target_hex = normalize_hex(target_hex) or load_accent()
    bundles = _bundle_dirs()
    count = 0
    for item in _iter_items(uiapp):
        try:
            name = item.Name
        except Exception:
            continue
        bundle_dir = bundles.get(name)
        if not bundle_dir:
            continue
        src = _theme_icon(bundle_dir)
        if name == "DeeForce":
            src = _deeforce_icon(bundle_dir) or src
        if not src:
            continue
        if _set_icon(item, resolve(src, target_hex)):
            count += 1
    return count


def _deeforce_icon(bundle_dir):
    """DeeForce shows icon.on.png while auto-sync runs - keep that state."""
    try:
        import dee_force_state
        if dee_force_state.is_active():
            # dee_force_state always swaps in icon.on.png - match it.
            path = os.path.join(bundle_dir, "icon.on.png")
            if os.path.isfile(path):
                return path
    except Exception:
        pass
    return None


# ---------------- startup ----------------
def _on_idling(sender, args):
    try:
        sender.Idling -= _on_idling
    except Exception:
        pass
    try:
        apply_to_ribbon(sender)
    except Exception:
        pass


def apply_on_startup(uiapp):
    """Called from startup.py, which runs BEFORE pyRevit builds the
    ribbon - so the swap waits for Revit's first Idling, when the
    buttons exist. Does nothing at all for the default orange."""
    try:
        if is_default(load_accent()):
            return
        try:
            uiapp.Idling -= _on_idling
        except Exception:
            pass
        uiapp.Idling += _on_idling
    except Exception:
        pass
