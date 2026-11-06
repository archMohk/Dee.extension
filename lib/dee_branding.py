# -*- coding: utf-8 -*-
"""
dee_branding
Adds a thin footer bar (orange background, white hyperlink text) to
the bottom of every Dee.extension tool window, linking to
www.archMKD.com.

Wraps whatever the window's own XAML already put in Window.Content
inside a new outer DockPanel with the bar docked at the bottom - this
works no matter what the original root element was (Grid, DockPanel,
StackPanel, ...), so no existing ui.xaml file needs editing. A window
opts in purely by inheriting DeeBrandedWindow instead of
pyrevit.forms.WPFWindow, and calling
DeeBrandedWindow.__init__(self, xaml_file) instead of
forms.WPFWindow.__init__(self, xaml_file) - everything else about the
window (its own __init__ logic, event handlers, XAML) is untouched.

DeeRoundedWindow (experimental, DeeSheet only for now): real rounded
corners on Windows 10, via WindowStyle=None + AllowsTransparency=True +
a hand-built title bar with a light/dark toggle. A first attempt at
this (applied to all 27 windows at once) broke a close button and then
froze a window - traced to pyrevit.forms.WPFWindow.setup_owner()
(called during forms.WPFWindow.__init__, before any of this module's
own code runs) doing
System.Windows.Interop.WindowInteropHelper(self).Owner = <Revit's
window>, which forces the window's native handle to exist earlier than
usual - WindowStyle/AllowsTransparency can only be changed before that
handle exists. The actual fix: forms.WPFWindow.__init__ takes a
set_owner kwarg; DeeRoundedWindow passes set_owner=False to skip
pyrevit's early setup_owner() call, sets WindowStyle/AllowsTransparency
itself while no handle exists yet, then calls self.setup_owner() once
that's settled (success or fallback) - restoring the normal owner
relationship pyrevit expects every window to have, just at the right
moment instead of too early.

DeeBrandedWindow (all other 26 windows) intentionally does not attempt
any of this - it only asks Windows' own DWM to round corners the safe
way (DWMWA_WINDOW_CORNER_PREFERENCE, Windows 11 only, a silent no-op on
Windows 10, never touches WindowStyle/AllowsTransparency/chrome, so it
can't cause this class of bug).
"""
import os

import clr
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("System")
from System import Uri, IntPtr
from System.Diagnostics import Process
from System.IO import FileStream, FileMode, FileAccess
from System.Windows.Markup import XamlReader
from System.Windows import (
    Thickness, HorizontalAlignment, VerticalAlignment, FontWeights,
    WindowStyle, CornerRadius, Rect, TextTrimming
)
from System.Windows.Controls import (DockPanel, Dock, TextBlock, Border, Grid,
                                     Button, ProgressBar, ColumnDefinition)
from System.Windows import GridLength, GridUnitType, Visibility
from System.Windows.Documents import Hyperlink, Run
from System.Windows.Media import SolidColorBrush, Color, Brushes, RectangleGeometry
from System.Windows.Input import Cursors
from System.Windows.Interop import WindowInteropHelper
from System.Windows.Shell import WindowChrome

from pyrevit import forms

try:
    import deew_settings
except Exception:
    # Only used for remembering the DeeRoundedWindow theme choice - a
    # missing/broken deew_settings must not break this whole module
    # for the other 26 windows that don't touch it at all.
    deew_settings = None

try:
    import ctypes
    _dwmapi = ctypes.windll.dwmapi
    _dwmapi.DwmSetWindowAttribute.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
    _dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
except Exception:
    _dwmapi = None

_SITE_URL = "http://www.archMKD.com"
_SITE_LABEL = "www.archMKD.com"
_BAR_HEIGHT = 22.0
_BAR_BG = Color.FromRgb(0xF2, 0x99, 0x4D)


def _accent_color():
    """The user's accent colour (DeeControl -> Accent Colour), else the
    brand orange. Read per window, so a new pick shows in the next
    window opened without a reload."""
    try:
        import dee_accent
        r, g, b = dee_accent.hex_to_rgb(dee_accent.window_accent())
        return Color.FromRgb(r, g, b)
    except Exception:
        return _BAR_BG


_LINK_FG = Color.FromRgb(0xFF, 0xFF, 0xFF)
_LINK_FG_DARK = Color.FromRgb(0x22, 0x22, 0x22)


def _text_on(color):
    """White text on a dark/saturated accent, near-black on a light one
    (e.g. a pale yellow), so the website link always stays readable."""
    lum = (0.2126 * color.R + 0.7152 * color.G + 0.0722 * color.B) / 255.0
    return _LINK_FG_DARK if lum > 0.62 else _LINK_FG


def set_window_accent(window, color=None):
    """Puts the accent brushes straight into the window's own Resources.
    The branding bar, its link, the progress strip and every ProgressBar
    look them up by name (DynamicResource), so calling this again on an
    open window repaints all of them at once."""
    try:
        if color is None:
            color = _accent_color()
        elif not isinstance(color, Color):
            import dee_accent
            r, g, b = dee_accent.hex_to_rgb(color)
            color = Color.FromRgb(r, g, b)
        window.Resources["DeeOrangeColor"] = color
        window.Resources["DeeOrange"] = SolidColorBrush(color)
        window.Resources["DeeOnOrange"] = SolidColorBrush(_text_on(color))
    except Exception:
        pass


def repaint_open_windows(color=None):
    """After Apply in Accent Colour: every Dee window still on screen
    takes the new colour now, not only the next one opened."""
    for win in list(_open_windows):
        set_window_accent(win, color)

# DWMWA_WINDOW_CORNER_PREFERENCE / DWMWCP_ROUNDSMALL - a Windows 11 DWM
# attribute that asks the OS compositor to draw small-radius rounded
# corners on a normal top-level window, keeping the native title bar /
# drag / resize / minimize-maximize-close chrome completely intact.
# Windows 10 has no equivalent - DwmSetWindowAttribute just returns a
# failure HRESULT there, which this module never checks, so corners
# silently stay square rather than erroring.
_DWMWA_WINDOW_CORNER_PREFERENCE = 33
_DWMWCP_ROUNDSMALL = 3


def _open_site(sender, args):
    try:
        Process.Start(_SITE_URL)
    except Exception:
        pass
    args.Handled = True


def _round_corners_dwm(window):
    if _dwmapi is None:
        return
    try:
        hwnd = WindowInteropHelper(window).Handle
        if hwnd == IntPtr.Zero:
            return
        pref = ctypes.c_int(_DWMWCP_ROUNDSMALL)
        _dwmapi.DwmSetWindowAttribute(
            hwnd.ToInt64(), _DWMWA_WINDOW_CORNER_PREFERENCE,
            ctypes.byref(pref), ctypes.sizeof(pref))
    except Exception:
        pass


def _build_bar():
    bar = Border()
    bar.Background = SolidColorBrush(_accent_color())
    bar.SetResourceReference(Border.BackgroundProperty, "DeeOrange")
    bar.Padding = Thickness(8, 2, 8, 2)
    bar.Height = _BAR_HEIGHT
    DockPanel.SetDock(bar, Dock.Bottom)

    text = TextBlock()
    text.HorizontalAlignment = HorizontalAlignment.Center
    text.VerticalAlignment = VerticalAlignment.Center
    text.FontSize = 14
    text.FontWeight = FontWeights.SemiBold

    link = Hyperlink(Run(_SITE_LABEL))
    try:
        link.NavigateUri = Uri(_SITE_URL)
    except Exception:
        pass
    link.Foreground = SolidColorBrush(_text_on(_accent_color()))
    link.SetResourceReference(Hyperlink.ForegroundProperty, "DeeOnOrange")
    link.RequestNavigate += _open_site
    text.Inlines.Add(link)

    bar.Child = text
    return bar


_THEME_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "DeeTheme.xaml")


def _load_theme_dictionary():
    """Parse lib/DeeTheme.xaml into a fresh ResourceDictionary.

    Fresh per window on purpose: a ResourceDictionary merged into one
    window and then into another is fine for brushes, but a parse costs
    a few ms and sharing one instance across windows opened at different
    times has bitten WPF before (frozen Freezables, ownership checks)."""
    stream = FileStream(_THEME_FILE, FileMode.Open, FileAccess.Read)
    try:
        return XamlReader.Load(stream)
    finally:
        stream.Close()


def apply_theme(window):
    """Merge DeeTheme.xaml into a window's Resources.

    Implicit styles in the dictionary then apply to every control in the
    window that doesn't set the same property inline. Safe to call on any
    window; a failure leaves the window exactly as its own XAML drew it."""
    try:
        theme = _load_theme_dictionary()
        try:
            # The user's accent replaces the theme's orange.
            accent = _accent_color()
            theme["DeeOrangeColor"] = accent
            theme["DeeOrange"] = SolidColorBrush(accent)
        except Exception:
            pass
        window.Resources.MergedDictionaries.Add(theme)
        return True
    except Exception:
        return False


# ============================================================================
# In-window progress strip
# ============================================================================
# Every branded window carries a hidden strip just above the branding bar:
# a status line, a progress bar and a Cancel button. dee_ui.SafeProgress
# shows it (instead of a separate pop-up bar) whenever a tool window is
# open, so every tool reports progress the same way, at the bottom of its
# own window. Only the strip is drawn here; the progress logic lives in
# dee_ui.
_open_windows = []


def active_window():
    """The most recently opened Dee window that is still on screen and
    has a progress strip, or None."""
    for win in reversed(_open_windows):
        try:
            if win.IsVisible and getattr(win, "_dee_strip", None) is not None:
                return win
        except Exception:
            continue
    return None


def _track_window(window):
    def _loaded(sender, args):
        if window not in _open_windows:
            _open_windows.append(window)

    def _closed(sender, args):
        try:
            _open_windows.remove(window)
        except ValueError:
            pass

    def _closing(sender, args):
        # Closing the window while work is running means Cancel - never
        # tear the window down under a loop that is still using it.
        try:
            if window._dee_progress_depth > 0:
                args.Cancel = True
                progress_cancel(window)
        except Exception:
            pass

    try:
        window.Loaded += _loaded
        window.Closed += _closed
        window.Closing += _closing
    except Exception:
        pass


def _wrap_with_progress_strip(window, content):
    """Returns a DockPanel holding `content` with the (collapsed)
    progress strip docked under it, and remembers the parts on the
    window. Any failure returns `content` unchanged."""
    try:
        strip = Border()
        strip.SetResourceReference(Border.BackgroundProperty, "DeeButtonBg")
        strip.SetResourceReference(Border.BorderBrushProperty, "DeeBorder")
        strip.BorderThickness = Thickness(0, 1, 0, 0)
        strip.Padding = Thickness(10, 5, 10, 5)
        strip.Visibility = Visibility.Collapsed
        DockPanel.SetDock(strip, Dock.Bottom)

        grid = Grid()
        for width in (GridLength(1, GridUnitType.Star), GridLength(260),
                      GridLength(100)):
            col = ColumnDefinition()
            col.Width = width
            grid.ColumnDefinitions.Add(col)

        text = TextBlock()
        text.VerticalAlignment = VerticalAlignment.Center
        text.TextTrimming = TextTrimming.CharacterEllipsis
        text.Margin = Thickness(0, 0, 10, 0)
        Grid.SetColumn(text, 0)

        bar = ProgressBar()
        bar.Height = 16
        bar.Minimum = 0
        bar.Maximum = 1
        bar.VerticalAlignment = VerticalAlignment.Center
        bar.SetResourceReference(ProgressBar.ForegroundProperty, "DeeOrange")
        Grid.SetColumn(bar, 1)

        cancel = Button()
        cancel.Content = "Cancel"
        cancel.Height = 24
        cancel.Margin = Thickness(10, 0, 0, 0)
        cancel.ToolTip = "Stop after the item currently being processed."

        def _cancel_click(sender, args):
            progress_cancel(window)
        cancel.Click += _cancel_click
        Grid.SetColumn(cancel, 2)

        grid.Children.Add(text)
        grid.Children.Add(bar)
        grid.Children.Add(cancel)
        strip.Child = grid

        panel = DockPanel()
        panel.Children.Add(strip)
        panel.Children.Add(content)

        window._dee_strip = strip
        window._dee_strip_text = text
        window._dee_strip_bar = bar
        window._dee_strip_cancel = cancel
        window._dee_strip_content = content
        window._dee_progress_depth = 0
        window._dee_progress_listeners = []
        _track_window(window)
        return panel
    except Exception:
        window._dee_strip = None
        return content


def progress_begin(window, cancellable, listener):
    """Shows the strip (or joins one already showing) and locks the rest
    of the window so a second click cannot start overlapping work."""
    window._dee_progress_depth += 1
    window._dee_progress_listeners.append(listener)
    if window._dee_progress_depth == 1:
        window._dee_strip_bar.Value = 0
        window._dee_strip_bar.Maximum = 1
        window._dee_strip_bar.IsIndeterminate = False
        window._dee_strip_cancel.IsEnabled = True
        window._dee_strip_cancel.Content = "Cancel"
        window._dee_strip.Visibility = Visibility.Visible
        try:
            window._dee_strip_content.IsEnabled = False
        except Exception:
            pass
    if not cancellable:
        # A step that genuinely cannot be interrupted still shows the
        # button, greyed, so the strip looks the same in every tool.
        window._dee_strip_cancel.IsEnabled = False


def progress_set(window, title=None, value=None, maximum=None, indeterminate=None):
    if title is not None:
        window._dee_strip_text.Text = title
        window._dee_strip_text.ToolTip = title
    if indeterminate is not None:
        window._dee_strip_bar.IsIndeterminate = bool(indeterminate)
    if maximum is not None and maximum > 0:
        window._dee_strip_bar.Maximum = float(maximum)
    if value is not None:
        window._dee_strip_bar.Value = max(0.0, min(float(value), window._dee_strip_bar.Maximum))


def progress_end(window, listener):
    try:
        window._dee_progress_listeners.remove(listener)
    except ValueError:
        pass
    window._dee_progress_depth = max(0, window._dee_progress_depth - 1)
    if window._dee_progress_depth == 0:
        window._dee_strip.Visibility = Visibility.Collapsed
        window._dee_strip_text.Text = ""
        try:
            window._dee_strip_content.IsEnabled = True
        except Exception:
            pass


def progress_cancel(window):
    try:
        window._dee_strip_cancel.IsEnabled = False
        window._dee_strip_cancel.Content = "Stopping..."
        for listener in list(window._dee_progress_listeners):
            try:
                listener()
            except Exception:
                pass
    except Exception:
        pass


class DeeBrandedWindow(forms.WPFWindow):
    def __init__(self, xaml_file, theme=True):
        forms.WPFWindow.__init__(self, xaml_file)
        set_window_accent(self)
        # Every branded window gets lib/DeeTheme.xaml (shared button /
        # input / grid look). theme=False is an escape hatch only - no
        # tool uses it, so every window shares the same light theme.
        if theme:
            apply_theme(self)
        try:
            self._add_footer_bar()
        except Exception:
            # A branding-bar failure should never block a tool from
            # opening - worst case the window just looks like it did
            # before this module existed.
            pass
        try:
            self.SourceInitialized += self._on_source_initialized
        except Exception:
            pass

    def _on_source_initialized(self, sender, args):
        _round_corners_dwm(self)

    def _add_footer_bar(self):
        original_content = self.Content
        if original_content is None:
            return
        self.Content = None
        outer = DockPanel()
        outer.Children.Add(_build_bar())
        outer.Children.Add(_wrap_with_progress_strip(self, original_content))
        self.Content = outer
        try:
            if self.Height and self.Height > 0:
                self.Height = self.Height + _BAR_HEIGHT
        except Exception:
            pass


# ============================================================================
# DeeRoundedWindow - experimental custom chrome (DeeSheet only for now)
# ============================================================================
_TITLE_BAR_HEIGHT = 28.0
_CORNER_RADIUS = 10.0
_CLOSE_HOVER_BG = Color.FromRgb(0xC0, 0x39, 0x2B)
_THEME_SETTINGS_TOOL = "dee_branding_theme"

_THEMES = {
    "light": {
        "body": Color.FromRgb(0xFF, 0xFF, 0xFF),
        "title_fg": Color.FromRgb(0x33, 0x33, 0x33),
        "border": Color.FromRgb(0xCC, 0xCC, 0xCC),
    },
    "dark": {
        "body": Color.FromRgb(0x2B, 0x2B, 0x2B),
        "title_fg": Color.FromRgb(0xF0, 0xF0, 0xF0),
        "border": Color.FromRgb(0x44, 0x44, 0x44),
    },
}


def _darken(color, factor=0.90):
    r = int(color.R * factor)
    g = int(color.G * factor)
    b = int(color.B * factor)
    return Color.FromRgb(max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b)))


def _load_theme():
    try:
        data = deew_settings.load(_THEME_SETTINGS_TOOL, {"theme": "light"})
        theme = data.get("theme", "light")
        return theme if theme in _THEMES else "light"
    except Exception:
        return "light"


def _save_theme(theme):
    try:
        deew_settings.save(_THEME_SETTINGS_TOOL, {"theme": theme})
    except Exception:
        pass


def _build_theme_brushes(theme_name):
    theme = _THEMES.get(theme_name, _THEMES["light"])
    return {
        "body": SolidColorBrush(theme["body"]),
        "title_bg": SolidColorBrush(_darken(theme["body"])),
        "title_fg": SolidColorBrush(theme["title_fg"]),
        "border": SolidColorBrush(theme["border"]),
    }


def _apply_theme_brushes(brushes, theme_name):
    """Mutates the shared brush instances' Color in place rather than
    replacing them - WPF repaints anything using these brushes
    automatically (SolidColorBrush.Color is a real dependency
    property), and every themed element (title bar, title text, close
    "X", outer border) points at the same handful of brush objects, so
    one mutation here updates all of them with no tree rebuild."""
    theme = _THEMES.get(theme_name, _THEMES["light"])
    brushes["body"].Color = theme["body"]
    brushes["title_bg"].Color = _darken(theme["body"])
    brushes["title_fg"].Color = theme["title_fg"]
    brushes["border"].Color = theme["border"]


def _toggle_swatch_color(current_theme):
    # Shows a preview of the theme a click will switch TO, not the
    # current one.
    other = "dark" if current_theme == "light" else "light"
    return _THEMES[other]["body"]


def _try_drag(window):
    try:
        window.DragMove()
    except Exception:
        pass


def _build_title_bar(window, brushes, state):
    bar = Grid()
    bar.Height = _TITLE_BAR_HEIGHT
    bar.Background = brushes["title_bg"]
    DockPanel.SetDock(bar, Dock.Top)

    title = TextBlock()
    title.Text = window.Title or ""
    title.Foreground = brushes["title_fg"]
    title.FontSize = 12
    title.VerticalAlignment = VerticalAlignment.Center
    title.HorizontalAlignment = HorizontalAlignment.Left
    title.Margin = Thickness(10, 0, 70, 0)
    title.TextTrimming = TextTrimming.CharacterEllipsis
    bar.Children.Add(title)

    # Small circle, left of the close button - toggles the light/dark
    # chrome theme and remembers the choice (lib/deew_settings.py).
    # Always shows a preview swatch of whichever theme a click will
    # switch TO.
    toggle = Border()
    toggle.Width = 14
    toggle.Height = 14
    toggle.CornerRadius = CornerRadius(7)
    toggle.HorizontalAlignment = HorizontalAlignment.Right
    toggle.VerticalAlignment = VerticalAlignment.Center
    toggle.Margin = Thickness(0, 0, 44, 0)
    toggle.Cursor = Cursors.Hand
    toggle.ToolTip = "Toggle light / dark"
    toggle.Background = SolidColorBrush(_toggle_swatch_color(state["theme"]))
    WindowChrome.SetIsHitTestVisibleInChrome(toggle, True)

    def _toggle_click(sender, args):
        args.Handled = True
        state["theme"] = "dark" if state["theme"] == "light" else "light"
        _save_theme(state["theme"])
        _apply_theme_brushes(brushes, state["theme"])
        toggle.Background = SolidColorBrush(_toggle_swatch_color(state["theme"]))

    toggle.MouseLeftButtonUp += _toggle_click
    bar.Children.Add(toggle)

    # Close area - a plain Border+TextBlock with a direct
    # MouseLeftButtonUp, not a Button - avoids depending on
    # ButtonBase's own Click state machine cooperating with
    # WindowChrome's non-client hit-testing right at the top-right
    # corner, which is what broke the first attempt at this.
    close_area = Border()
    close_area.Width = 32
    close_area.Height = _TITLE_BAR_HEIGHT
    close_area.HorizontalAlignment = HorizontalAlignment.Right
    close_area.VerticalAlignment = VerticalAlignment.Top
    close_area.Margin = Thickness(0, 0, 4, 0)
    close_area.Background = Brushes.Transparent
    close_area.Cursor = Cursors.Hand

    close_text = TextBlock()
    close_text.Text = "X"
    close_text.Foreground = brushes["title_fg"]
    close_text.FontSize = 12
    close_text.HorizontalAlignment = HorizontalAlignment.Center
    close_text.VerticalAlignment = VerticalAlignment.Center
    close_area.Child = close_text

    def _close_enter(sender, args):
        close_area.Background = SolidColorBrush(_CLOSE_HOVER_BG)

    def _close_leave(sender, args):
        close_area.Background = Brushes.Transparent

    def _close_click(sender, args):
        args.Handled = True
        try:
            window.Close()
        except Exception:
            pass

    close_area.MouseEnter += _close_enter
    close_area.MouseLeave += _close_leave
    close_area.MouseLeftButtonUp += _close_click
    WindowChrome.SetIsHitTestVisibleInChrome(close_area, True)
    bar.Children.Add(close_area)

    def _bar_mouse_down(sender, args):
        # Skip if the press originated on the toggle or close button,
        # so using either never also arms a drag.
        source = args.OriginalSource
        if source is close_area or source is close_text or source is toggle:
            return
        _try_drag(window)

    bar.MouseLeftButtonDown += _bar_mouse_down

    return bar


def _detach(element):
    """Best-effort: if a failed _build_rounded_content call left
    original_content re-parented onto some now-abandoned container,
    the fallback path re-adding it elsewhere would otherwise throw
    "already has a logical parent". Harmless no-op the rest of the
    time."""
    try:
        parent = element.Parent
    except Exception:
        return
    if parent is None:
        return
    try:
        if hasattr(parent, "Children"):
            parent.Children.Remove(element)
        elif hasattr(parent, "Child"):
            parent.Child = None
    except Exception:
        pass


def _build_plain_content(original_content):
    outer = DockPanel()
    outer.Children.Add(_build_bar())
    outer.Children.Add(original_content)
    return outer


def _build_rounded_content(window, original_content, brushes, state):
    border = Border()
    border.CornerRadius = CornerRadius(_CORNER_RADIUS)
    border.Background = brushes["body"]
    border.BorderBrush = brushes["border"]
    border.BorderThickness = Thickness(1)

    inner = DockPanel()
    inner.Children.Add(_build_title_bar(window, brushes, state))
    inner.Children.Add(_build_bar())
    inner.Children.Add(original_content)
    border.Child = inner

    def _on_size_changed(sender, args):
        try:
            w = max(border.ActualWidth, 0.0)
            h = max(border.ActualHeight, 0.0)
            border.Clip = RectangleGeometry(Rect(0, 0, w, h), _CORNER_RADIUS, _CORNER_RADIUS)
        except Exception:
            pass
    border.SizeChanged += _on_size_changed

    return border


class DeeRoundedWindow(forms.WPFWindow):
    """Experimental - see the module docstring. forms.WPFWindow is
    initialized with set_owner=False specifically so WindowStyle/
    AllowsTransparency can still be changed (only legal before the
    window's native handle exists); self.setup_owner() is called
    explicitly afterward, once that's settled either way, to restore
    the normal owner relationship pyrevit sets up for every window."""

    def __init__(self, xaml_file):
        forms.WPFWindow.__init__(self, xaml_file, set_owner=False)
        set_window_accent(self)
        try:
            self._apply_branding()
        except Exception:
            pass
        try:
            self.setup_owner()
        except Exception:
            pass
        try:
            self.SourceInitialized += self._on_source_initialized
        except Exception:
            pass

    def _on_source_initialized(self, sender, args):
        _round_corners_dwm(self)

    def _apply_branding(self):
        original_content = self.Content
        if original_content is None:
            return
        self.Content = None
        original_content = _wrap_with_progress_strip(self, original_content)

        theme_state = {"theme": _load_theme()}

        try:
            self.WindowStyle = getattr(WindowStyle, "None")
            self.AllowsTransparency = True
            self.Background = Brushes.Transparent

            chrome = WindowChrome()
            chrome.CaptionHeight = 0.0
            chrome.ResizeBorderThickness = Thickness(6)
            chrome.GlassFrameThickness = Thickness(0)
            chrome.UseAeroCaptionButtons = False
            chrome.CornerRadius = CornerRadius(0)
            WindowChrome.SetWindowChrome(self, chrome)

            brushes = _build_theme_brushes(theme_state["theme"])
            self.Content = _build_rounded_content(self, original_content, brushes, theme_state)
            self._bump_height(_BAR_HEIGHT + _TITLE_BAR_HEIGHT)
            return
        except Exception:
            pass

        _detach(original_content)

        # Custom chrome didn't come together cleanly - revert fully to
        # a normal native-chrome window and fall back to just the
        # footer bar, rather than risk a window with no native or
        # custom way to move, resize, or close. Still safe here: the
        # window's native handle doesn't exist yet either way (we're
        # still before the deferred self.setup_owner() call in
        # __init__), so WindowStyle/AllowsTransparency can still be
        # changed freely.
        try:
            self.WindowStyle = WindowStyle.SingleBorderWindow
            self.AllowsTransparency = False
            self.Background = SolidColorBrush(Color.FromRgb(0xFF, 0xFF, 0xFF))
            WindowChrome.SetWindowChrome(self, None)
        except Exception:
            pass

        try:
            self.Content = _build_plain_content(original_content)
            self._bump_height(_BAR_HEIGHT)
        except Exception:
            self.Content = original_content

    def _bump_height(self, extra):
        try:
            if self.Height and self.Height > 0:
                self.Height = self.Height + extra
        except Exception:
            pass
