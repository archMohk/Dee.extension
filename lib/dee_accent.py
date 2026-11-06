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

The grey line work can be recoloured as well ("Lines" in the dialog).
Then the map becomes affine: white stays white, the theme's ink grey
(ICON_INK, or ICON_INK_DARK in icon.dark.png) goes to the line colour L,
the accent still goes to T - see _affine(). With no line colour it
reduces to exactly the matrix above.

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
# The grey line ink dee_icons.py uses (PALETTES "ink"), light / dark icons.
ICON_INK = "#7A7A7A"
ICON_INK_DARK = "#949494"
# The brand orange used by the window branding bar / DeeOrange brush.
BRAND_ORANGE = "#F2994D"

# The swatches in the Accent Colour window, row by row (11 per row).
# Anything else can still be picked with "More colours..." or a hex code.
PRESETS = [
    # oranges / yellows
    ("Orange (default)", ICON_ACCENT),
    ("Tangerine", "#E67E22"),
    ("Pumpkin", "#D35400"),
    ("Amber", "#D98E04"),
    ("Gold", "#B8860B"),
    ("Mustard", "#C9A227"),
    ("Sand", "#C2A26B"),
    ("Brown", "#795548"),
    ("Copper", "#B05A2C"),
    ("Terracotta", "#C0603F"),
    ("Coral", "#E0603A"),
    # reds / pinks / purples
    ("Red", "#C0392B"),
    ("Crimson", "#A31621"),
    ("Burgundy", "#7B1E3A"),
    ("Brick", "#8E3B2E"),
    ("Pink", "#C2185B"),
    ("Rose", "#D64C7F"),
    ("Magenta", "#A0287D"),
    ("Plum", "#7D3C6E"),
    ("Purple", "#7B4FB5"),
    ("Violet", "#5E35B1"),
    ("Lavender", "#8E7CC3"),
    # blues
    ("Indigo", "#3F51B5"),
    ("Navy", "#1A3A6B"),
    ("Royal Blue", "#2950C8"),
    ("Blue", "#1F6FB2"),
    ("Steel Blue", "#4682B4"),
    ("Sky", "#2E9BD6"),
    ("Azure", "#0078D4"),
    ("Cyan", "#0097A7"),
    ("Turquoise", "#1AA39A"),
    ("Teal", "#00897B"),
    ("Petrol", "#1D5C6B"),
    # greens / neutrals
    ("Emerald", "#1E8E5A"),
    ("Green", "#3E8E41"),
    ("Forest", "#2E6B30"),
    ("Olive", "#6B7F1E"),
    ("Lime", "#7CB342"),
    ("Sage", "#7A9A7E"),
    ("Slate", "#4A6278"),
    ("Blue Grey", "#607D8B"),
    ("Graphite", "#555555"),
    ("Charcoal", "#2F3640"),
    ("Black", "#111111"),
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
def _load_config():
    try:
        with open(_CONFIG_PATH, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_accent():
    """The saved accent as '#RRGGBB', or ICON_ACCENT when none is saved."""
    return normalize_hex(_load_config().get("accent")) or ICON_ACCENT


def load_line():
    """The saved icon line colour as '#RRGGBB', or None = keep the grey."""
    return normalize_hex(_load_config().get("line"))


def save_accent(value, line=None):
    """Saves both colours. line=None keeps the default grey lines."""
    value = normalize_hex(value) or ICON_ACCENT
    line = normalize_hex(line)
    try:
        with open(_CONFIG_PATH, "w") as f:
            f.write(json.dumps({"accent": value, "line": line}, indent=2))
    except Exception:
        pass
    return value


def all_default(accent=None, line=None):
    """True when the icons would come out exactly as shipped."""
    return is_default(accent) and normalize_hex(line) is None


def window_accent():
    """Colour for the window branding bar / DeeOrange: the brand orange
    unless the user picked their own accent."""
    accent = load_accent()
    return BRAND_ORANGE if is_default(accent) else accent


# ---------------- icon recolouring ----------------
def _inv3(m):
    a, b, c = m[0]
    d, e, f = m[1]
    g, h, i = m[2]
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    return [[(e * i - f * h) / det, (c * h - b * i) / det, (b * f - c * e) / det],
            [(f * g - d * i) / det, (a * i - c * g) / det, (c * d - a * f) / det],
            [(d * h - e * g) / det, (b * g - a * h) / det, (a * e - b * d) / det]]


def _affine(target_hex, line_hex=None, ink_hex=ICON_INK):
    """out = B.x + t (colours 0..1) with white -> white, ink -> line,
    accent -> target. Built in the basis u = grey axis, d = the accent's
    off-grey part, e = u x d (left alone)."""
    a = [c / 255.0 for c in hex_to_rgb(ICON_ACCENT)]
    tgt = [c / 255.0 for c in hex_to_rgb(target_hex)]
    k = hex_to_rgb(ink_hex)[0] / 255.0
    line = normalize_hex(line_hex)
    lc = [c / 255.0 for c in hex_to_rgb(line)] if line else [k, k, k]
    u = [1.0, 1.0, 1.0]
    mean = sum(a) / 3.0
    d = [c - mean for c in a]
    e = [u[1] * d[2] - u[2] * d[1], u[2] * d[0] - u[0] * d[2], u[0] * d[1] - u[1] * d[0]]
    bu = [(lc[j] - 1.0) / (k - 1.0) for j in range(3)]
    bd = [tgt[j] - 1.0 - (mean - 1.0) * bu[j] for j in range(3)]
    p_inv = _inv3([[u[r], d[r], e[r]] for r in range(3)])
    q = [[bu[r], bd[r], e[r]] for r in range(3)]
    b = [[sum(q[r][n] * p_inv[n][c] for n in range(3)) for c in range(3)] for r in range(3)]
    t = [1.0 - sum(b[r]) for r in range(3)]
    return b, t


def _matrix(target_hex, line_hex=None, ink_hex=ICON_INK):
    b, t = _affine(target_hex, line_hex, ink_hex)
    cm = ColorMatrix()   # starts as identity
    # GDI+ multiplies ROW vectors: out = in . G, so G[i][j] = B[j][i];
    # row 4 is the translation.
    for i in range(3):
        for j in range(3):
            setattr(cm, "Matrix{0}{1}".format(i, j), b[j][i])
        setattr(cm, "Matrix4{0}".format(i), t[i])
    return cm


def recolor_file(src, dst, target_hex, line_hex=None):
    """Writes src recoloured (accent -> target_hex, grey lines -> line_hex)
    as a PNG at dst. True on success."""
    bmp = out = g = None
    try:
        bmp = Bitmap(src)
        w, h = bmp.Width, bmp.Height
        out = Bitmap(w, h, PixelFormat.Format32bppArgb)
        g = Graphics.FromImage(out)
        g.CompositingMode = CompositingMode.SourceCopy
        attrs = ImageAttributes()
        ink = ICON_INK_DARK if ".dark." in os.path.basename(src).lower() else ICON_INK
        attrs.SetColorMatrix(_matrix(target_hex, line_hex, ink))
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


def _cache_root(target_hex, line_hex=None):
    base = os.environ.get("APPDATA") or _THIS_DIR
    key = target_hex.lstrip("#").upper()
    if line_hex:
        key += "_" + line_hex.lstrip("#").upper()
    return os.path.join(base, "Dee", "accent_icons", key)


_SAVED = object()


def resolve(icon_path, target_hex=None, line_hex=_SAVED):
    """Recoloured copy of icon_path for the current colours (built on
    demand, cached), or icon_path itself for the default colours or on
    any failure. Safe to call on any icon file. line_hex None means grey
    lines; left out, the saved line colour is used."""
    try:
        target_hex = normalize_hex(target_hex) or load_accent()
        line_hex = load_line() if line_hex is _SAVED else normalize_hex(line_hex)
        if (all_default(target_hex, line_hex) or not icon_path
                or not os.path.isfile(icon_path)):
            return icon_path
        rel = os.path.relpath(os.path.abspath(icon_path), _EXT_ROOT)
        if rel.startswith(".."):
            rel = os.path.basename(icon_path)
        dst = os.path.join(_cache_root(target_hex, line_hex), rel)
        if (not os.path.isfile(dst)
                or os.path.getmtime(dst) < os.path.getmtime(icon_path)):
            if not recolor_file(icon_path, dst, target_hex, line_hex):
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


def apply_to_ribbon(uiapp, target_hex=None, line_hex=_SAVED):
    """Swaps every DeePack button's icon for the accent-coloured one
    (or back to the original files for the default accent). Returns how
    many buttons were updated."""
    target_hex = normalize_hex(target_hex) or load_accent()
    line_hex = load_line() if line_hex is _SAVED else normalize_hex(line_hex)
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
        if _set_icon(item, resolve(src, target_hex, line_hex)):
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
    buttons exist. Does nothing at all for the default colours."""
    try:
        if all_default(load_accent(), load_line()):
            return
        try:
            uiapp.Idling -= _on_idling
        except Exception:
            pass
        uiapp.Idling += _on_idling
    except Exception:
        pass
