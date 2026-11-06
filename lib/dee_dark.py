# -*- coding: utf-8 -*-
"""
dee_dark
The dark theme (About -> Interface -> Theme & shape -> Dark).

Three layers, so no tool's own ui.xaml or script needs editing:

1. Palette: DeeTheme.xaml is loaded with its colours swapped for dark
   ones (PALETTE), and DeeThemeDark.xaml is merged on top of it - Windows'
   own system colours plus flat dark looks for drop-downs, tabs,
   scrollbars, text boxes, tables and lists.

2. The tool's XAML: before the window is built, every colour written
   into it (Background="#F5F5F5", Foreground="#555", <Setter
   Property="Background" Value="#C6E0B4"/>, Color="White"...) is turned
   into its dark counterpart (transform_xaml). Row-status colours in
   styles and triggers keep working - only their shade changes.

3. Colours a script sets while running (status text, row colours...):
   a light, throttled pass over the open window remaps any light colour
   set straight on an element (attach_scanner).

The colour rule (dark_color) is one function for all three:
  backgrounds / borders: light greys flip to dark greys, light tints
    (pastel status colours) become deep tints of the same hue; already
    dark or strong colours (the accent blue, existing dark panels) stay.
  text / strokes: dark greys flip to light greys, dark coloured text is
    lifted so it reads on a dark background; light text stays.
Applying it twice changes nothing, so re-running is always safe.
"""
import colorsys
import io
import os
import re

import clr
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
clr.AddReference("WindowsBase")
from System import TimeSpan  # noqa: E402
from System.Windows import FrameworkElement, DependencyPropertyHelper, BaseValueSource  # noqa: E402
from System.Windows.Controls import Control, Border, Panel, TextBlock  # noqa: E402
from System.Windows.Media import (SolidColorBrush, Color, ColorConverter,  # noqa: E402
                                  VisualTreeHelper, Visual)
from System.Windows.Shapes import Shape  # noqa: E402
from System.Windows.Threading import DispatcherTimer  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
THEME_FILE = os.path.join(_HERE, "DeeTheme.xaml")
EXTRAS_FILE = os.path.join(_HERE, "DeeThemeDark.xaml")

# DeeTheme.xaml keys -> dark values.
PALETTE = {
    "DeeAccentColor": "#2F7FC4",
    "DeeAccentDarkColor": "#1F6FB2",
    "DeeWindowBg": "#1E1F22",
    "DeeSurface": "#2B2D31",
    "DeeBorder": "#3C3F45",
    "DeeBorderStrong": "#5A5E66",
    "DeeText": "#E6E6E6",
    "DeeTextMuted": "#9AA0A6",
    "DeeButtonBg": "#33363B",
    "DeeButtonHover": "#3D4046",
    "DeeButtonPressed": "#474A51",
    "DeeStatusOk": "#2E4A2A",
    "DeeStatusWarn": "#5A4A1A",
    "DeeStatusFail": "#5A2A2A",
    "DeeStatusIgnored": "#3A3A3A",
}

_FG_PROPS = ("Foreground", "Stroke", "CaretBrush")
_BG_PROPS = ("Background", "BorderBrush", "Fill", "RowBackground", "AlternatingRowBackground",
             "HorizontalGridLinesBrush", "VerticalGridLinesBrush", "SelectionBrush")
# A brush/stop's Color="" is text or background depending on what the
# brush is for; its x:Key usually says (e.g. "MutedText").
_FG_KEY = re.compile(r'x:Key="[^"]*(text|fore|font|ink|label)[^"]*"', re.I)


# ---------------- the colour rule ----------------
def dark_rgb(r, g, b, kind):
    """kind "bg" (backgrounds, borders, fills) or "fg" (text, strokes)."""
    h, l, s = colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)
    grey = s < 0.2 or l > 0.97 or l < 0.03
    if kind == "fg":
        if grey:
            if l < 0.6:
                l = 0.9 - l * 0.75
        elif l < 0.5:
            l = 0.62
    else:
        if grey:
            if l > 0.5:
                l = 0.12 + (1.0 - l) * 0.8
        elif l > 0.75:
            l = 0.2 + (1.0 - l) * 0.5
            s *= 0.6
    rr, gg, bb = colorsys.hls_to_rgb(h, l, s)
    return int(round(rr * 255)), int(round(gg * 255)), int(round(bb * 255))


def dark_color(color, kind):
    r, g, b = dark_rgb(color.R, color.G, color.B, kind)
    return Color.FromArgb(color.A, r, g, b)


def _parse(value):
    try:
        c = ColorConverter.ConvertFromString(value.strip())
        return c
    except Exception:
        return None


def _hex(c):
    # int(): .NET Byte values don't take Python's "02X" format in IronPython
    a, r, g, b = int(c.A), int(c.R), int(c.G), int(c.B)
    if a == 255:
        return "#{0:02X}{1:02X}{2:02X}".format(r, g, b)
    return "#{0:02X}{1:02X}{2:02X}{3:02X}".format(a, r, g, b)


def _convert(value, kind):
    if value.startswith("{"):
        return value          # a binding / resource reference
    c = _parse(value)
    if c is None or c.A == 0:
        return value          # not a colour, or Transparent
    return _hex(dark_color(c, kind))


def _kind(prop):
    name = prop.split(".")[-1]
    if name in _FG_PROPS:
        return "fg"
    if name in _BG_PROPS:
        return "bg"
    return None


# ---------------- 2. the tool's XAML ----------------
_ATTR = re.compile(r'(?<![\w:])([A-Za-z][\w]*(?:\.[A-Za-z]\w*)?)="([^"]*)"')
_SETTER = re.compile(r'<Setter\b[^>]*>', re.S)
_COLOR_ELEMENT = re.compile(r'(<Color\b[^>]*>)\s*([^<\s]+)\s*(</Color>)')
_BRUSH_TAG = re.compile(r'<(?:SolidColorBrush|GradientStop)\b[^>]*>', re.S)


def _brush_sub(match):
    tag = match.group(0)
    value = re.search(r'\bColor="([^"]*)"', tag)
    if not value:
        return tag
    kind = "fg" if _FG_KEY.search(tag) else "bg"
    return tag[:value.start(1)] + _convert(value.group(1), kind) + tag[value.end(1):]


def _attr_sub(match):
    prop, value = match.group(1), match.group(2)
    kind = _kind(prop)
    if kind is None:
        return match.group(0)
    return '{0}="{1}"'.format(prop, _convert(value, kind))


def _setter_sub(match):
    tag = match.group(0)
    prop = re.search(r'\bProperty="([^"]*)"', tag)
    value = re.search(r'\bValue="([^"]*)"', tag)
    if not prop or not value:
        return tag
    kind = _kind(prop.group(1))
    if kind is None:
        return tag
    new = _convert(value.group(1), kind)
    return tag[:value.start(1)] + new + tag[value.end(1):]


def transform_xaml(text):
    """The XAML with every written-in colour turned dark."""
    # Setters first (their Value= is keyed by Property=), then plain
    # attributes - Setter tags are skipped by the attribute pass because
    # "Value" / "Property" are not colour properties.
    text = _SETTER.sub(_setter_sub, text)
    text = _BRUSH_TAG.sub(_brush_sub, text)
    text = _ATTR.sub(_attr_sub, text)
    text = _COLOR_ELEMENT.sub(
        lambda m: m.group(1) + _convert(m.group(2), "fg" if _FG_KEY.search(m.group(1)) else "bg")
        + m.group(3), text)
    return text


def read(path):
    with io.open(path, encoding="utf-8-sig") as f:
        return f.read()


def dark_xaml_for(path):
    """A tool's XAML file, as dark XAML text (None if it can't be read)."""
    try:
        return transform_xaml(read(path))
    except Exception:
        return None


# ---------------- 1. the theme dictionaries ----------------
def theme_text():
    """DeeTheme.xaml with the dark palette."""
    text = read(THEME_FILE)
    for key, value in PALETTE.items():
        text = re.sub(r'(x:Key="{0}"(?:\s+Color=")?)(#[0-9A-Fa-f]{{3,8}})'.format(key),
                      lambda m, v=value: m.group(1) + v, text)
        text = re.sub(r'(<Color x:Key="{0}">)\s*#[0-9A-Fa-f]{{3,8}}\s*(</Color>)'.format(key),
                      lambda m, v=value: m.group(1) + v + m.group(2), text)
    # (grid lines / alternating rows: DeeThemeDark.xaml restyles DataGrid)
    return text


def extras_text():
    return read(EXTRAS_FILE)


# ---------------- 3. colours set by scripts ----------------
def _checks(obj):
    if isinstance(obj, Control):
        return ((Control.BackgroundProperty, "bg"), (Control.ForegroundProperty, "fg"),
                (Control.BorderBrushProperty, "bg"))
    if isinstance(obj, Border):
        return ((Border.BackgroundProperty, "bg"), (Border.BorderBrushProperty, "bg"))
    if isinstance(obj, TextBlock):
        return ((TextBlock.ForegroundProperty, "fg"), (TextBlock.BackgroundProperty, "bg"))
    if isinstance(obj, Panel):
        return ((Panel.BackgroundProperty, "bg"),)
    if isinstance(obj, Shape):
        return ((Shape.FillProperty, "bg"), (Shape.StrokeProperty, "fg"))
    return ()


def _fix(obj):
    for dp, kind in _checks(obj):
        try:
            src = DependencyPropertyHelper.GetValueSource(obj, dp)
            if (src.BaseValueSource != BaseValueSource.Local or src.IsExpression
                    or src.IsAnimated):
                continue
            brush = obj.GetValue(dp)
            if not isinstance(brush, SolidColorBrush) or brush.Color.A == 0:
                continue
            new = dark_color(brush.Color, kind)
            if new != brush.Color:
                b = SolidColorBrush(new)
                b.Opacity = brush.Opacity
                obj.SetValue(dp, b)
        except Exception:
            pass


def scan(root):
    """Darkens light colours set straight on elements under `root`."""
    stack = [root]
    while stack:
        obj = stack.pop()
        _fix(obj)
        try:
            if isinstance(obj, Visual):
                for i in range(VisualTreeHelper.GetChildrenCount(obj)):
                    stack.append(VisualTreeHelper.GetChild(obj, i))
        except Exception:
            pass


def attach_scanner(window):
    """Scans once the window is shown, then again (at most a few times a
    second, only while the window is changing) to catch colours a script
    sets later."""
    timer = DispatcherTimer()
    timer.Interval = TimeSpan.FromMilliseconds(600)

    def _tick(sender, args):
        timer.Stop()
        try:
            scan(window)
        except Exception:
            pass

    def _changed(sender, args):
        if not timer.IsEnabled:
            timer.Start()

    def _closed(sender, args):
        timer.Stop()
        try:
            window.LayoutUpdated -= _changed
        except Exception:
            pass

    timer.Tick += _tick
    window.Loaded += lambda s, a: scan(window)
    window.LayoutUpdated += _changed
    window.Closed += _closed
    window._dee_dark_timer = timer
