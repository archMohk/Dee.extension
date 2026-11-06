# -*- coding: utf-8 -*-
"""
dee_effects
Optional window effects, switched on in About -> Interface -> Effects.
Any combination can be on at once; every one is off by default and each
is applied to every Dee window (DeeBrandedWindow) as it opens, and to the
ones already open when the user clicks Apply.

  glass     Frosted glass: the window turns see-through and whatever is
            behind it is blurred (Windows' own blur-behind, the same one
            the Start menu uses - works on Windows 10 and 11). "glass_level"
            0..100 sets how see-through.
  outline   A thin accent-coloured outline around the window.
  topline   An accent-coloured line along the top of the window.
  fade      Windows fade and slide in when they open.
  rounded   Rounded corners and a slim title bar in the theme colours
            (lib/dee_branding.py builds it). Chrome can only change before
            a window exists, so this one shows on windows opened after
            Apply.
  titlebar  The title bar painted in the accent colour: the Windows title
            bar on Windows 11, the rounded window's own title bar
            anywhere (Windows 10 has no API for its own).

Stored per PC next to the colours, in lib/.dee_accent.json ("effects").
Nothing here ever raises into a caller: an effect that can't be applied
is simply skipped and the window looks as it always did.
"""
import clr
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
from System import Environment, TimeSpan  # noqa: E402
from System.Windows import Thickness, Visibility, Window, Duration  # noqa: E402
from System.Windows.Controls import Border  # noqa: E402
from System.Windows.Interop import WindowInteropHelper, HwndSource  # noqa: E402
from System.Windows.Media import SolidColorBrush, Color, Colors, TranslateTransform  # noqa: E402
from System.Windows.Media.Animation import DoubleAnimation, QuadraticEase, EasingMode  # noqa: E402

import dee_accent

DEFAULTS = {
    "glass": False,
    "glass_level": 45,
    "outline": False,
    "topline": False,
    "fade": False,
    "rounded": False,
    "titlebar": False,
}

# (key, label, tooltip) in the order the Effects tab lists them.
EFFECTS = [
    ("glass", "Glass",
     "Frosted glass: the window becomes see-through and what is behind it is blurred."),
    ("outline", "Accent outline",
     "A thin accent-coloured outline around every tool window."),
    ("topline", "Accent top line",
     "An accent-coloured line along the top of every tool window."),
    ("fade", "Fade in",
     "Windows fade and slide in gently when they open."),
    ("rounded", "Rounded windows",
     "Rounded corners and a slim title bar on every tool window. "
     "Shows on windows opened after Apply."),
    ("titlebar", "Accent title bar",
     "The title bar in the accent colour (Windows 11, or any PC with "
     "Rounded windows on)."),
]


def is_windows_11():
    try:
        return Environment.OSVersion.Version.Build >= 22000
    except Exception:
        return False


# ---------------- the setting ----------------
def load_effects():
    data = dee_accent._load_config().get("effects")
    out = dict(DEFAULTS)
    if isinstance(data, dict):
        for key in DEFAULTS:
            if key in data:
                out[key] = data[key]
    try:
        out["glass_level"] = max(0, min(100, int(out["glass_level"])))
    except Exception:
        out["glass_level"] = DEFAULTS["glass_level"]
    return out


def save_effects(effects):
    clean = dict(DEFAULTS)
    clean.update(dict((k, effects[k]) for k in DEFAULTS if k in effects))
    dee_accent.update_config({"effects": clean})
    return clean


# ---------------- Win32 ----------------
try:
    import ctypes

    class _AccentPolicy(ctypes.Structure):
        _fields_ = [("AccentState", ctypes.c_int),
                    ("AccentFlags", ctypes.c_int),
                    ("GradientColor", ctypes.c_uint),
                    ("AnimationId", ctypes.c_int)]

    class _WinCompAttrData(ctypes.Structure):
        _fields_ = [("Attribute", ctypes.c_int),
                    ("Data", ctypes.c_void_p),
                    ("SizeOfData", ctypes.c_int)]

    # Private DLL handles, so these argtypes never clash with another
    # module's (ctypes.windll shares one function object per DLL). Every
    # pointer is passed as a plain address: IronPython's ctypes rejects
    # byref() for a c_void_p parameter ("expected pointer, got
    # NativeArgument").
    _user32 = ctypes.WinDLL("user32")
    _user32.SetWindowCompositionAttribute.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _user32.SetWindowCompositionAttribute.restype = ctypes.c_int
    _dwmapi = ctypes.WinDLL("dwmapi")
    _dwmapi.DwmSetWindowAttribute.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
    _dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
    _user32.SetWindowRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
    _user32.SetWindowRgn.restype = ctypes.c_int
    _gdi32 = ctypes.WinDLL("gdi32")
    _gdi32.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6
    _gdi32.CreateRoundRectRgn.restype = ctypes.c_void_p
except Exception:
    ctypes = None
    _gdi32 = None

_WCA_ACCENT_POLICY = 19
_ACCENT_DISABLED = 0
_ACCENT_ENABLE_BLURBEHIND = 3
_DWMWA_CAPTION_COLOR = 35
_DWMWA_TEXT_COLOR = 36
_DWMWA_COLOR_DEFAULT = 0xFFFFFFFF


def _hwnd(window):
    try:
        h = WindowInteropHelper(window).Handle
        return None if h.ToInt64() == 0 else h
    except Exception:
        return None


def _set_blur(hwnd, on):
    if ctypes is None:
        return False
    try:
        policy = _AccentPolicy()
        policy.AccentState = _ACCENT_ENABLE_BLURBEHIND if on else _ACCENT_DISABLED
        policy.AccentFlags = 2
        policy.GradientColor = 0x01FFFFFF
        policy.AnimationId = 0
        data = _WinCompAttrData()
        data.Attribute = _WCA_ACCENT_POLICY
        data.Data = ctypes.addressof(policy)
        data.SizeOfData = ctypes.sizeof(policy)
        return bool(_user32.SetWindowCompositionAttribute(hwnd.ToInt64(),
                                                          ctypes.addressof(data)))
    except Exception:
        return False


def _dwm_color(hwnd, attribute, value):
    if ctypes is None:
        return
    try:
        v = ctypes.c_uint(value)
        _dwmapi.DwmSetWindowAttribute(hwnd.ToInt64(), attribute,
                                      ctypes.addressof(v), ctypes.sizeof(v))
    except Exception:
        pass


def _round_region(window, hwnd, off=False):
    """Clips a rounded window to its rounded shape, so the blurred glass
    behind it doesn't show as a square at the corners. Re-done on resize."""
    if _gdi32 is None:
        return
    try:
        if off:
            _user32.SetWindowRgn(hwnd.ToInt64(), None, True)
            return
        if not getattr(window, "_dee_region_hooked", False):
            window._dee_region_hooked = True

            def _resized(sender, args):
                if getattr(window, "_dee_glass_saved", None) is not None:
                    _round_region(window, hwnd)
            window.SizeChanged += _resized
        src = HwndSource.FromHwnd(hwnd)
        m = src.CompositionTarget.TransformToDevice
        w = int(round(window.ActualWidth * m.M11))
        h = int(round(window.ActualHeight * m.M22))
        border = window._dee_round_border
        r = border.CornerRadius.TopLeft
        d = max(1, int(round(2 * r * m.M11)))
        if w <= 0 or h <= 0:
            return
        rgn = _gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, d, d)
        _user32.SetWindowRgn(hwnd.ToInt64(), rgn, True)   # Windows owns rgn now
    except Exception:
        pass


def _colorref(color):
    return color.R | (color.G << 8) | (color.B << 16)


# ---------------- the effects ----------------
_GLASS_KEYS = ("DeeWindowBg", "DeeSurface")


def _with_alpha(brush, alpha):
    try:
        c = brush.Color
    except Exception:
        c = Colors.White
    return SolidColorBrush(Color.FromArgb(alpha, c.R, c.G, c.B))


def _glass(window, on, level):
    rounded = getattr(window, "_dee_round_border", None) is not None
    if getattr(window, "AllowsTransparency", False) and not rounded:
        return      # other custom-chrome windows draw their own body
    hwnd = _hwnd(window)
    if hwnd is None:
        return
    src = HwndSource.FromHwnd(hwnd)
    if on:
        if getattr(window, "_dee_glass_saved", None) is None:
            window._dee_glass_saved = {
                "background": window.Background,
                "target": src.CompositionTarget.BackgroundColor,
                "resources": dict((k, window.TryFindResource(k)) for k in _GLASS_KEYS),
            }
        saved = window._dee_glass_saved
        # level 0 = barely see-through, 100 = very clear glass.
        alpha = int(round(235 - 1.7 * level))
        base = saved["background"] if saved["background"] is not None else saved["resources"].get("DeeWindowBg")
        src.CompositionTarget.BackgroundColor = Colors.Transparent
        if rounded:
            # The rounded border paints DeeWindowBg; the window stays clear.
            window.Resources["DeeWindowBg"] = _with_alpha(
                saved["resources"].get("DeeWindowBg"), alpha)
            window.Resources["DeeSurface"] = _with_alpha(
                saved["resources"].get("DeeSurface"), min(255, alpha + 25))
            _round_region(window, hwnd)
        else:
            window.Background = _with_alpha(base, alpha)
            for key in _GLASS_KEYS:
                original = saved["resources"].get(key)
                if original is not None:
                    window.Resources[key] = _with_alpha(original, min(255, alpha + 25))
        _set_blur(hwnd, True)
    else:
        saved = getattr(window, "_dee_glass_saved", None)
        if saved is None:
            return
        _set_blur(hwnd, False)
        src.CompositionTarget.BackgroundColor = saved["target"]
        if rounded:
            _round_region(window, hwnd, off=True)
        else:
            window.Background = saved["background"]
        for key in _GLASS_KEYS:
            if window.Resources.Contains(key):
                window.Resources.Remove(key)
        window._dee_glass_saved = None


def _outline(window, on):
    border = getattr(window, "_dee_round_border", None)
    if border is not None:
        # The outline follows the rounded corners instead of the square
        # (invisible) window edge.
        border.BorderThickness = Thickness(2 if on else 1)
        border.SetResourceReference(Border.BorderBrushProperty,
                                    "DeeOrange" if on else "DeeBorderStrong")
        window._dee_outline_on = bool(on)
        return
    if on:
        window.BorderThickness = Thickness(2)
        window.SetResourceReference(Window.BorderBrushProperty, "DeeOrange")
    elif getattr(window, "_dee_outline_on", False):
        window.ClearValue(Window.BorderBrushProperty)
        window.ClearValue(Window.BorderThicknessProperty)
    window._dee_outline_on = bool(on)


def _topline(window, on):
    line = getattr(window, "_dee_topline", None)
    if line is not None:
        line.Visibility = Visibility.Visible if on else Visibility.Collapsed


def _titlebar(window, on):
    if getattr(window, "_dee_titlebar", None) is not None:
        # Rounded window: its own title bar, on any Windows version.
        if on:
            window.Resources["DeeTitleBg"] = window.TryFindResource("DeeOrange")
            window.Resources["DeeTitleFg"] = window.TryFindResource("DeeOnOrange")
        else:
            window.Resources["DeeTitleBg"] = window.TryFindResource("DeeSurface")
            window.Resources["DeeTitleFg"] = window.TryFindResource("DeeText")
        return
    if not is_windows_11():
        return
    hwnd = _hwnd(window)
    if hwnd is None:
        return
    if on:
        try:
            color = window.TryFindResource("DeeOrangeColor")
            text = window.TryFindResource("DeeOnOrange").Color
        except Exception:
            return
        _dwm_color(hwnd, _DWMWA_CAPTION_COLOR, _colorref(color))
        _dwm_color(hwnd, _DWMWA_TEXT_COLOR, _colorref(text))
    else:
        _dwm_color(hwnd, _DWMWA_CAPTION_COLOR, _DWMWA_COLOR_DEFAULT)
        _dwm_color(hwnd, _DWMWA_TEXT_COLOR, _DWMWA_COLOR_DEFAULT)


def _fade_in(window):
    try:
        content = window.Content
        if content is None:
            return
        shift = TranslateTransform(0, 10)
        content.RenderTransform = shift
        ease = QuadraticEase()
        ease.EasingMode = EasingMode.EaseOut
        fade = DoubleAnimation(0.0, 1.0, Duration(TimeSpan.FromMilliseconds(220)))
        fade.EasingFunction = ease
        slide = DoubleAnimation(10.0, 0.0, Duration(TimeSpan.FromMilliseconds(220)))
        slide.EasingFunction = ease
        content.BeginAnimation(content.OpacityProperty, fade)
        shift.BeginAnimation(TranslateTransform.YProperty, slide)
    except Exception:
        pass


def apply(window, effects=None):
    """(Re)applies every effect to a window that is already on screen -
    switching on what is on and back off what is off."""
    effects = effects or load_effects()
    for step in (lambda: _glass(window, effects["glass"], effects["glass_level"]),
                 lambda: _outline(window, effects["outline"]),
                 lambda: _topline(window, effects["topline"]),
                 lambda: _titlebar(window, effects["titlebar"])):
        try:
            step()
        except Exception:
            pass


def attach(window):
    """Called once per new Dee window: applies the saved effects as soon
    as the window has a native handle, and plays the fade on first show."""
    try:
        effects = load_effects()
    except Exception:
        return
    if not any(effects[k] for k in DEFAULTS if k != "glass_level"):
        return

    def _loaded(sender, args):
        if effects["fade"]:
            _fade_in(window)

    try:
        when_ready(window, lambda: apply(window, effects))
        window.Loaded += _loaded
    except Exception:
        pass


def when_ready(window, action):
    """Runs `action` once the window has its native handle - straight
    away if it already has one (rounded windows create it early, during
    their own setup), else on SourceInitialized."""
    if _hwnd(window) is not None:
        action()
        return

    def _initialized(sender, args):
        action()
    window.SourceInitialized += _initialized
