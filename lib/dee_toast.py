# -*- coding: utf-8 -*-
"""
dee_toast
The shared toast-notification renderer - a dark rounded card with a
colored left accent bar and a soft drop shadow, always non-modal.
Extracted from lib/dee_prayer_service.py (where this exact design was
built and confirmed live - a real toast fired correctly for Dhuhr) once
lib/dee_broadcast_service.py (DeeCall) needed the identical mechanism
for a completely different kind of message. Anything in this extension
that wants to pop a toast calls show_toast() here instead of building
its own window.

Two dismiss styles, per show_toast()'s requires_ack argument: the
default auto-dismisses after duration_sec (a DispatcherTimer closes it);
requires_ack=True instead adds a Close button and never starts a timer
at all - the toast stays on screen until the user actively clicks it.
An optional image_base64 (already-encoded, e.g. from DeeCall's Attach
Image) renders above the text via a plain BitmapImage-from-MemoryStream
decode - a bad/corrupt payload drops the image only, never the rest of
the toast (see _decode_image_bytes/_bitmap_from_bytes). Whenever an
image is shown, a small "Save Image" link sits under it - a
SaveFileDialog writing the same raw decoded bytes straight to disk, so
the recipient isn't stuck with a toast-sized preview only.

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
clr.AddReference("System.Windows.Forms")

from System import TimeSpan, Convert
from System.IO import MemoryStream, File
from System.Windows import (
    Window, WindowStyle, ResizeMode, Thickness, CornerRadius,
    SystemParameters, FontWeights, GridLength, GridUnitType,
    VerticalAlignment, HorizontalAlignment, TextWrapping)
from System.Windows.Controls import StackPanel, TextBlock, Border, Grid, ColumnDefinition, Button, Image
from System.Windows.Input import Cursors
from System.Windows.Media import SolidColorBrush, Color, Brushes, Stretch
from System.Windows.Media.Effects import DropShadowEffect
from System.Windows.Media.Imaging import BitmapImage, BitmapCacheOption
from System.Windows.Threading import DispatcherTimer
from System.Windows.Forms import SaveFileDialog, DialogResult

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

# Extra window height added on top of the saved/passed height when a
# toast needs a Close button (requires_ack) and/or an image - both grow
# the card beyond whatever size the user picked for a plain text toast,
# so they're added on top rather than eating into it.
_ACK_BUTTON_EXTRA = 40
_IMAGE_HEIGHT = 90
_IMAGE_EXTRA = _IMAGE_HEIGHT + 10
# A "Save Image" link under the image - independent of requires_ack, so
# even an auto-dismissing toast with an image gets a chance to save it
# before it closes.
_SAVE_LINK_EXTRA = 22


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


def _decode_image_bytes(image_base64):
    """Returns raw decoded bytes, or None if image_base64 is empty or
    isn't valid base64. Kept separate from the BitmapImage build below
    because the Save Image button needs the raw bytes to write to disk,
    not just something WPF can render."""
    if not image_base64:
        return None
    try:
        return Convert.FromBase64String(image_base64)
    except Exception:
        return None


def _bitmap_from_bytes(data):
    """Returns a frozen BitmapImage built from raw image bytes, or None
    if the bytes aren't a decodable image - a bad/corrupt payload should
    drop the image, not break the rest of the toast."""
    try:
        stream = MemoryStream(data)
        bmp = BitmapImage()
        bmp.BeginInit()
        bmp.CacheOption = BitmapCacheOption.OnLoad
        bmp.StreamSource = stream
        bmp.EndInit()
        bmp.Freeze()
        return bmp
    except Exception:
        return None


def show_toast(headline, title_text, sub_text, accent_color,
                duration_sec=None, position=None, width=None, height=None,
                requires_ack=False, image_base64=None):
    """headline: small bold label above the title (e.g. "PRAYER TIME —
    DEE.EXTENSION"). title_text: the big bold line (e.g. a prayer name,
    or "Announcement"). sub_text: the smaller line under it. accent_color
    is a System.Windows.Media.Color, used for the left bar and headline
    text - callers pick their own (green/orange/etc. - see
    dee_prayer_service.py's _ACCENT_NOW/_ACCENT_REMINDER for the existing
    convention). requires_ack: True shows a Close button and never auto-
    dismisses (for a message the user must actively acknowledge);
    False (default) is the original auto-dismiss-after-duration_sec
    behavior. image_base64: optional base64-encoded image shown above the
    text. Never raises - a broken toast must never break the caller (the
    Idling handler, or the button that triggered it)."""
    try:
        settings = load_settings()
        position = position or settings.get("position", DEFAULT_SETTINGS["position"])
        width = clamp_width(width if width is not None else
                             settings.get("width", DEFAULT_SETTINGS["width"]))
        base_height = clamp_height(height if height is not None else
                                    settings.get("height", DEFAULT_SETTINGS["height"]))
        duration_sec = clamp_duration(duration_sec if duration_sec is not None else
                                       settings.get("duration_sec", DEFAULT_SETTINGS["duration_sec"]))

        image_bytes = _decode_image_bytes(image_base64)
        image = _bitmap_from_bytes(image_bytes) if image_bytes is not None else None
        if image is None:
            image_bytes = None
        height = base_height
        if requires_ack:
            height += _ACK_BUTTON_EXTRA
        if image is not None:
            height += _IMAGE_EXTRA + _SAVE_LINK_EXTRA

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

        save_b = None
        if image is not None:
            image_ctrl = Image()
            image_ctrl.Source = image
            image_ctrl.Stretch = Stretch.Uniform
            image_ctrl.Height = _IMAGE_HEIGHT
            image_ctrl.Margin = Thickness(0, 0, 0, 2)
            content.Children.Add(image_ctrl)

            # A plain link-styled Button (no border/background) rather
            # than a full button - reads as "Save Image", not another
            # box competing with Close for attention.
            save_b = Button()
            save_b.Content = u"Save Image"
            save_b.FontSize = 11
            save_b.Foreground = SolidColorBrush(accent_color)
            save_b.Background = Brushes.Transparent
            save_b.BorderThickness = Thickness(0)
            save_b.Padding = Thickness(0)
            save_b.HorizontalAlignment = HorizontalAlignment.Left
            save_b.Cursor = Cursors.Hand
            save_b.Margin = Thickness(0, 0, 0, 6)
            content.Children.Add(save_b)

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

        close_b = None
        if requires_ack:
            close_b = Button()
            close_b.Content = u"Close"
            close_b.Width = 70
            close_b.Height = 24
            close_b.Margin = Thickness(0, 10, 0, 0)
            close_b.HorizontalAlignment = HorizontalAlignment.Right
            content.Children.Add(close_b)

        Grid.SetColumn(content, 1)
        grid.Children.Add(content)

        outer.Child = grid
        window.Content = outer

        left, top = _compute_origin(position, width, height)
        window.Left = left
        window.Top = top

        window.Show()

        if save_b is not None:
            def _on_save_click(sender, args):
                try:
                    dlg = SaveFileDialog()
                    dlg.Filter = "JPEG Image (*.jpg)|*.jpg"
                    dlg.FileName = "DeeCall_Image.jpg"
                    dlg.Title = "Save Image"
                    if dlg.ShowDialog() == DialogResult.OK:
                        File.WriteAllBytes(dlg.FileName, image_bytes)
                except Exception:
                    pass
            save_b.Click += _on_save_click

        if close_b is not None:
            # requires_ack: no timer at all - only the Close button
            # dismisses this one, per its whole point.
            def _on_close_click(sender, args):
                try:
                    window.Close()
                except Exception:
                    pass
            close_b.Click += _on_close_click
        else:
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
