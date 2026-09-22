# -*- coding: utf-8 -*-
"""
dee_toast
The shared toast-notification renderer - a dark rounded card with a
colored left accent bar and a soft drop shadow, non-modal, auto-
dismissing. Extracted from lib/dee_prayer_service.py (where this exact
design was built and confirmed live - a real toast fired correctly for
Dhuhr) once lib/dee_broadcast_service.py (DeeCall) needed the identical
mechanism for a completely different kind of message. Anything in this
extension that wants to pop a toast calls show_toast() here instead of
building its own window.

Appearance (position on screen, size, how long it stays) is one shared
per-PC setting store ("DeeNotifications", via lib/deew_settings.py) -
not per-feature - so the "Test Notification" button in the Notification
Center window previews exactly what BOTH prayer notifications and DeeCall
broadcasts will look like, and adjusting it once affects every caller.
show_toast()'s duration_sec/position/width/height parameters are for
PREVIEWING an unsaved value (the Test button passes the current, maybe-
not-yet-saved field values); every real caller just omits them and gets
whatever's actually saved.
"""
import clr
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
clr.AddReference("WindowsBase")

from System import TimeSpan
from System.Windows import (
    Window, WindowStyle, ResizeMode, Thickness, CornerRadius,
    SystemParameters, FontWeights, GridLength, GridUnitType,
    VerticalAlignment, TextWrapping)
from System.Windows.Controls import StackPanel, TextBlock, Border, Grid, ColumnDefinition
from System.Windows.Media import SolidColorBrush, Color, Brushes
from System.Windows.Media.Effects import DropShadowEffect
from System.Windows.Threading import DispatcherTimer

import deew_settings

TOOL_NAME = "DeeNotifications"

# position: where on the primary screen's work area the toast appears.
POSITIONS = [
    ("bottom_right", "Bottom Right"),
    ("bottom_left", "Bottom Left"),
    ("top_right", "Top Right"),
    ("top_left", "Top Left"),
]
DEFAULT_SETTINGS = {
    "position": "bottom_right",
    "width": 320,
    "height": 100,
    "duration_sec": 10,
}

_MIN_WIDTH, _MAX_WIDTH = 220, 500
_MIN_HEIGHT, _MAX_HEIGHT = 70, 220
_MIN_DURATION, _MAX_DURATION = 1, 120
_MARGIN = 16.0


def load_settings():
    return deew_settings.load(TOOL_NAME, dict(DEFAULT_SETTINGS))


def save_settings(settings):
    deew_settings.save(TOOL_NAME, settings)


def clamp_width(w):
    return max(_MIN_WIDTH, min(_MAX_WIDTH, w))


def clamp_height(h):
    return max(_MIN_HEIGHT, min(_MAX_HEIGHT, h))


def clamp_duration(d):
    return max(_MIN_DURATION, min(_MAX_DURATION, d))


def _build_shadow():
    effect = DropShadowEffect()
    effect.Color = Color.FromRgb(0, 0, 0)
    effect.Opacity = 0.4
    effect.BlurRadius = 20
    effect.ShadowDepth = 4
    effect.Direction = 270
    return effect


def _compute_origin(position, width, height):
    work_area = SystemParameters.WorkArea
    if position == "bottom_left":
        return work_area.Left + _MARGIN, work_area.Bottom - height - _MARGIN
    if position == "top_right":
        return work_area.Right - width - _MARGIN, work_area.Top + _MARGIN
    if position == "top_left":
        return work_area.Left + _MARGIN, work_area.Top + _MARGIN
    # default / "bottom_right"
    return work_area.Right - width - _MARGIN, work_area.Bottom - height - _MARGIN


def show_toast(headline, title_text, sub_text, accent_color,
                duration_sec=None, position=None, width=None, height=None):
    """headline: small bold label above the title (e.g. "PRAYER TIME —
    DEE.EXTENSION"). title_text: the big bold line (e.g. a prayer name,
    or "Announcement"). sub_text: the smaller line under it. accent_color
    is a System.Windows.Media.Color, used for the left bar and headline
    text - callers pick their own (green/orange/etc. - see
    dee_prayer_service.py's _ACCENT_NOW/_ACCENT_REMINDER for the existing
    convention). Never raises - a broken toast must never break the
    caller (the Idling handler, or the button that triggered it)."""
    try:
        settings = load_settings()
        position = position or settings.get("position", DEFAULT_SETTINGS["position"])
        width = clamp_width(width if width is not None else
                             settings.get("width", DEFAULT_SETTINGS["width"]))
        height = clamp_height(height if height is not None else
                               settings.get("height", DEFAULT_SETTINGS["height"]))
        duration_sec = clamp_duration(duration_sec if duration_sec is not None else
                                       settings.get("duration_sec", DEFAULT_SETTINGS["duration_sec"]))

        window = Window()
        window.WindowStyle = getattr(WindowStyle, "None")
        window.ResizeMode = ResizeMode.NoResize
        window.ShowInTaskbar = False
        window.Topmost = True
        window.AllowsTransparency = True
        window.Background = Brushes.Transparent
        window.Width = width
        window.Height = height

        outer = Border()
        outer.CornerRadius = CornerRadius(10)
        outer.Background = SolidColorBrush(Color.FromRgb(0x1A, 0x1D, 0x26))
        outer.Effect = _build_shadow()

        grid = Grid()
        accent_col = ColumnDefinition()
        accent_col.Width = GridLength(6)
        content_col = ColumnDefinition()
        content_col.Width = GridLength(1, GridUnitType.Star)
        grid.ColumnDefinitions.Add(accent_col)
        grid.ColumnDefinitions.Add(content_col)

        accent_bar = Border()
        accent_bar.Background = SolidColorBrush(accent_color)
        accent_bar.CornerRadius = CornerRadius(10, 0, 0, 10)
        Grid.SetColumn(accent_bar, 0)
        grid.Children.Add(accent_bar)

        content = StackPanel()
        content.Margin = Thickness(16, 14, 16, 14)
        content.VerticalAlignment = VerticalAlignment.Center

        headline_tb = TextBlock()
        headline_tb.Text = headline
        headline_tb.FontSize = 10
        headline_tb.FontWeight = FontWeights.Bold
        headline_tb.Foreground = SolidColorBrush(accent_color)
        content.Children.Add(headline_tb)

        title_tb = TextBlock()
        title_tb.Text = title_text
        title_tb.Foreground = SolidColorBrush(Color.FromRgb(0xF2, 0xF3, 0xF5))
        title_tb.FontSize = 22
        title_tb.FontWeight = FontWeights.Bold
        title_tb.Margin = Thickness(0, 2, 0, 2)
        title_tb.TextWrapping = TextWrapping.Wrap
        content.Children.Add(title_tb)

        sub_tb = TextBlock()
        sub_tb.Text = sub_text
        sub_tb.Foreground = SolidColorBrush(Color.FromRgb(0x9A, 0xA1, 0xB0))
        sub_tb.FontSize = 13
        sub_tb.TextWrapping = TextWrapping.Wrap
        content.Children.Add(sub_tb)

        Grid.SetColumn(content, 1)
        grid.Children.Add(content)

        outer.Child = grid
        window.Content = outer

        left, top = _compute_origin(position, width, height)
        window.Left = left
        window.Top = top

        window.Show()

        timer = DispatcherTimer()
        timer.Interval = TimeSpan.FromSeconds(duration_sec)

        def _on_tick(sender, args):
            try:
                timer.Stop()
            except Exception:
                pass
            try:
                window.Close()
            except Exception:
                pass

        timer.Tick += _on_tick
        timer.Start()
    except Exception:
        pass
