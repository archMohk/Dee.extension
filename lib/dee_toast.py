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

Any http(s):// URL (or a bare www.something) typed into title_text/
sub_text renders as a real, clickable Hyperlink (opens the system
default browser via System.Diagnostics.Process.Start) - no separate
"link" field anywhere, callers just type a normal message and a URL
inside it becomes clickable automatically. Same Hyperlink/
RequestNavigate pattern already proven live in lib/dee_branding.py's own
footer link.

Three color themes (THEMES/DEFAULT_THEME below): "dark" (the original
design - dark card, light text), "light" (white card, dark text - closer
to a native OS/browser notification), "colored" (the card background
itself is a darkened tint of accent_color, white text - reads as
strongly branded). All three share the exact same layout/structure
(accent bar, headline/title/sub, optional image/buttons) - only colors
change - so adding a theme never risks the sizing work above; only
_theme_colors() decides what's on screen. theme is per-CALL, not a
shared setting like position/size: DeeCall sends it per-message, Prayer
Times saves it as its own persistent choice - see each caller.

Appearance (position on screen, width, minimum height, how long it
stays) is one shared per-PC setting store ("DeeNotifications", via
lib/deew_settings.py) - not per-feature - so the "Test Notification"
button in the Notification Center window previews what every kind of
toast this extension shows will roughly look like. The actual window
HEIGHT is not fixed to that setting though - Window.SizeToContent grows
it to fit whatever's really in the card (wrapped text, an image, Close/
Save Image buttons), with the saved height acting as a floor, not a
ceiling; a bottom-anchored position (bottom_left/bottom_right) keeps its
bottom edge fixed by repositioning Top on SizeChanged once the real
height is known - see _compute_top's docstring. show_toast()'s
duration_sec/position/width/height parameters are for PREVIEWING an
unsaved value (the Test button passes the current, maybe-not-yet-saved
field values); every real caller just omits them and gets whatever's
actually saved.
"""
import re

import clr
clr.AddReference("System")
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
clr.AddReference("WindowsBase")
clr.AddReference("System.Windows.Forms")

from System import TimeSpan, Convert, Uri
from System.Diagnostics import Process
from System.IO import MemoryStream, File
from System.Windows import (
    Window, WindowStyle, ResizeMode, SizeToContent, Thickness, CornerRadius,
    SystemParameters, FontWeights, GridLength, GridUnitType,
    VerticalAlignment, HorizontalAlignment, TextWrapping)
from System.Windows.Controls import StackPanel, TextBlock, Border, Grid, ColumnDefinition, Button, Image
from System.Windows.Documents import Hyperlink, Run
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

_IMAGE_HEIGHT = 90

# Any http(s):// URL, or a bare www.something, typed into a toast's
# title/sub text becomes a real, clickable Hyperlink - no separate
# "link" field needed anywhere (DeeCall just types the link into the
# message like normal text). www.-only addresses get "https://"
# prepended for navigation (kept out of the DISPLAYED text, which stays
# exactly as typed). Trailing punctuation right after a URL (a period
# ending the sentence, a closing bracket, etc.) is peeled off so it
# doesn't get swallowed into the link itself. Same Hyperlink/
# RequestNavigate -> Process.Start pattern already proven live in
# lib/dee_branding.py's own footer link.
_URL_RE = re.compile(r"((?:https?://|www\.)[^\s<>\"]+)", re.IGNORECASE)
_TRAILING_PUNCT = u".,!?;:)]}\"'"

DEFAULT_THEME = "dark"
THEMES = [
    ("dark", "Dark"),
    ("light", "Light"),
    ("colored", "Colored"),
]

# Corner rounding for the whole card - shared by the outer card and the
# accent bar's own matching corners (must stay equal or the bar would
# either overhang or leave a square gap at the rounded edge).
_CORNER_RADIUS = 14


def _lighten(color, amount):
    """amount in [0,1] - 0 leaves color unchanged, 1 reaches white."""
    r = int(color.R + (255 - color.R) * amount)
    g = int(color.G + (255 - color.G) * amount)
    b = int(color.B + (255 - color.B) * amount)
    return Color.FromRgb(min(255, r), min(255, g), min(255, b))


def _darken(color, amount):
    """amount in [0,1] - 0 leaves color unchanged, 1 reaches black."""
    r = int(color.R * (1 - amount))
    g = int(color.G * (1 - amount))
    b = int(color.B * (1 - amount))
    return Color.FromRgb(max(0, r), max(0, g), max(0, b))


def _theme_colors(theme, accent_color):
    """Returns the 5 colors a card needs, all derived from just accent_
    color plus the theme choice - so every existing caller's own accent
    (prayer's green/orange, DeeCall's blue) still reads as itself in
    every theme, nothing hardcoded per-feature."""
    if theme == "light":
        return {
            "card_bg": Color.FromRgb(0xFF, 0xFF, 0xFF),
            "accent_bar": accent_color,
            "headline_fg": accent_color,
            "title_fg": Color.FromRgb(0x1A, 0x1D, 0x26),
            "sub_fg": Color.FromRgb(0x5A, 0x64, 0x72),
            "link_fg": Color.FromRgb(0x1A, 0x73, 0xE8),
        }
    if theme == "colored":
        card_bg = _darken(accent_color, 0.45)
        return {
            "card_bg": card_bg,
            "accent_bar": accent_color,
            "headline_fg": Color.FromRgb(0xFF, 0xFF, 0xFF),
            "title_fg": Color.FromRgb(0xFF, 0xFF, 0xFF),
            "sub_fg": _lighten(card_bg, 0.55),
            "link_fg": Color.FromRgb(0xFF, 0xFF, 0xFF),
        }
    # default / "dark"
    return {
        "card_bg": Color.FromRgb(0x1A, 0x1D, 0x26),
        "accent_bar": accent_color,
        "headline_fg": accent_color,
        "title_fg": Color.FromRgb(0xF2, 0xF3, 0xF5),
        "sub_fg": Color.FromRgb(0x9A, 0xA1, 0xB0),
        "link_fg": Color.FromRgb(0x6C, 0xB6, 0xFF),
    }


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


def _compute_left(position, width):
    work_area = SystemParameters.WorkArea
    if position in ("bottom_left", "top_left"):
        return work_area.Left + _MARGIN
    return work_area.Right - width - _MARGIN  # bottom_right / top_right


def _compute_top(position, actual_height):
    """actual_height: the window's real, laid-out height - unlike width,
    height now depends on content (see show_toast's SizeToContent), so
    this can't be computed until WPF has actually measured it. Top
    positions don't care (constant regardless of height); bottom
    positions anchor the BOTTOM edge, so Top must be recomputed whenever
    actual_height changes (wired to the window's SizeChanged)."""
    work_area = SystemParameters.WorkArea
    if position in ("top_left", "top_right"):
        return work_area.Top + _MARGIN
    return work_area.Bottom - actual_height - _MARGIN  # bottom_left / bottom_right


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


def _on_navigate(sender, args):
    try:
        Process.Start(str(args.Uri))
    except Exception:
        pass
    args.Handled = True


def _fill_linkified(text_block, text, link_color):
    """Populates text_block.Inlines with plain Runs, except any URL
    becomes a real Hyperlink - never touches .Text directly (Inlines and
    Text are mutually exclusive on a TextBlock). link_color: theme-
    dependent (a light blue reads fine on Dark/Colored's own dark
    backgrounds, but needs to be a darker blue on Light's white one)."""
    text = text or u""
    pos = 0
    for m in _URL_RE.finditer(text):
        if m.start() > pos:
            text_block.Inlines.Add(Run(text[pos:m.start()]))
        matched = m.group(1)
        trail = u""
        while matched and matched[-1] in _TRAILING_PUNCT:
            trail = matched[-1] + trail
            matched = matched[:-1]
        if matched:
            nav_target = matched if matched.lower().startswith(("http://", "https://")) \
                else u"https://" + matched
            try:
                link = Hyperlink(Run(matched))
                link.NavigateUri = Uri(nav_target)
                link.Foreground = SolidColorBrush(link_color)
                link.RequestNavigate += _on_navigate
                text_block.Inlines.Add(link)
            except Exception:
                # Not a URL Uri can actually parse (rare) - show as plain
                # text rather than dropping it.
                text_block.Inlines.Add(Run(matched))
        if trail:
            text_block.Inlines.Add(Run(trail))
        pos = m.end()
    if pos < len(text):
        text_block.Inlines.Add(Run(text[pos:]))


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
                requires_ack=False, image_base64=None, theme=None):
    """headline: small bold label above the title (e.g. "PRAYER TIME —
    DEE.EXTENSION"). title_text: the big bold line (e.g. a prayer name,
    or "Announcement"). sub_text: the smaller line under it. accent_color
    is a System.Windows.Media.Color, used for the left bar (every theme)
    and the headline text (Dark/Light) - callers pick their own (green/
    orange/etc. - see dee_prayer_service.py's _ACCENT_NOW/_ACCENT_REMINDER
    for the existing convention). requires_ack: True shows a Close button
    and never auto-dismisses (for a message the user must actively
    acknowledge); False (default) is the original auto-dismiss-after-
    duration_sec behavior. image_base64: optional base64-encoded image
    shown above the text. theme: "dark" (default)/"light"/"colored" - see
    THEMES and _theme_colors(); defaults to DEFAULT_THEME if None or
    unrecognized. Never raises - a broken toast must never break the
    caller (the Idling handler, or the button that triggered it)."""
    try:
        colors = _theme_colors(theme if theme in ("dark", "light", "colored") else DEFAULT_THEME,
                                accent_color)

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

        window = Window()
        window.WindowStyle = getattr(WindowStyle, "None")
        window.ResizeMode = ResizeMode.NoResize
        window.ShowInTaskbar = False
        window.Topmost = True
        window.AllowsTransparency = True
        window.Background = Brushes.Transparent
        window.Width = width
        # Height adjusts to whatever's actually in the card (wrapped
        # text, an image, the Close/Save Image buttons) instead of a
        # fixed guess that kept needing a new manual offset for every
        # new piece of content - base_height (the user's own saved/
        # passed size) becomes a MINIMUM, not the final word.
        window.MinHeight = base_height
        window.SizeToContent = SizeToContent.Height

        outer = Border()
        outer.CornerRadius = CornerRadius(_CORNER_RADIUS)
        outer.Background = SolidColorBrush(colors["card_bg"])
        outer.Effect = _build_shadow()

        grid = Grid()
        accent_col = ColumnDefinition()
        accent_col.Width = GridLength(6)
        content_col = ColumnDefinition()
        content_col.Width = GridLength(1, GridUnitType.Star)
        grid.ColumnDefinitions.Add(accent_col)
        grid.ColumnDefinitions.Add(content_col)

        accent_bar = Border()
        accent_bar.Background = SolidColorBrush(colors["accent_bar"])
        accent_bar.CornerRadius = CornerRadius(_CORNER_RADIUS, 0, 0, _CORNER_RADIUS)
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
            save_b.Foreground = SolidColorBrush(colors["link_fg"])
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
        headline_tb.Foreground = SolidColorBrush(colors["headline_fg"])
        content.Children.Add(headline_tb)

        title_tb = TextBlock()
        title_tb.Foreground = SolidColorBrush(colors["title_fg"])
        title_tb.FontSize = 22
        title_tb.FontWeight = FontWeights.Bold
        title_tb.Margin = Thickness(0, 2, 0, 2)
        title_tb.TextWrapping = TextWrapping.Wrap
        _fill_linkified(title_tb, title_text, colors["link_fg"])
        content.Children.Add(title_tb)

        sub_tb = TextBlock()
        sub_tb.Foreground = SolidColorBrush(colors["sub_fg"])
        sub_tb.FontSize = 13
        sub_tb.TextWrapping = TextWrapping.Wrap
        _fill_linkified(sub_tb, sub_text, colors["link_fg"])
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

        window.Left = _compute_left(position, width)
        # Initial guess using the minimum height, corrected below once
        # WPF actually knows the real (content-driven) height - avoids a
        # visible jump for the common case where content fits at
        # MinHeight, while still ending up correct when it doesn't.
        window.Top = _compute_top(position, base_height)

        def _reposition(sender=None, args=None):
            window.Top = _compute_top(position, window.ActualHeight)
        window.SizeChanged += _reposition

        window.Show()
        # SizeToContent can under-measure on the very first paint of a
        # WindowStyle=None + AllowsTransparency window (seen live: 3
        # lines of wrapped title text left the Close button clipped past
        # the bottom edge, even though the same design fixed a shorter
        # 2-line case cleanly). UpdateLayout() right after Show() forces
        # an authoritative, synchronous re-measure/re-arrange against the
        # REAL final content - not just another guessed offset - so
        # ActualHeight (and the reposition below) are correct before the
        # user ever perceives a wrong size.
        window.UpdateLayout()
        _reposition()

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
