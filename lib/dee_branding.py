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
import clr
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("System")
from System import Uri, IntPtr
from System.Diagnostics import Process
from System.Windows import (
    Thickness, HorizontalAlignment, VerticalAlignment, FontWeights,
    WindowStyle, CornerRadius, Rect, TextTrimming
)
from System.Windows.Controls import DockPanel, Dock, TextBlock, Border, Grid
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
    import dee_telemetry
except Exception:
    dee_telemetry = None

try:
    import acc_auth
except Exception:
    acc_auth = None

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
_LINK_FG = Color.FromRgb(0xFF, 0xFF, 0xFF)

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


def _build_status_text():
    """Signed-in email + access standing + ACC readiness, all purely
    from local/cached state (dee_telemetry.status_summary() reads the
    last check_access() result, acc_auth.is_configured() only checks
    acc_config.json exists) - never a network call, so this never
    slows down or risks failing a window open. Any piece that can't be
    determined (module missing, never checked yet) is just left out
    rather than shown as an error."""
    parts = []
    try:
        if dee_telemetry is not None:
            email = dee_telemetry.get_cached_identity()
            if email:
                parts.append(email)
            summary = dee_telemetry.status_summary()
            if summary:
                parts.append(summary)
    except Exception:
        pass
    try:
        if acc_auth is not None:
            parts.append("ACC Ready" if acc_auth.is_configured() else "ACC Not configured")
    except Exception:
        pass
    return u"   ·   ".join(parts)


def _build_bar():
    bar = Border()
    bar.Background = SolidColorBrush(_BAR_BG)
    bar.Padding = Thickness(8, 2, 8, 2)
    bar.Height = _BAR_HEIGHT
    DockPanel.SetDock(bar, Dock.Bottom)

    inner = DockPanel()
    inner.LastChildFill = True

    status_str = _build_status_text()
    if status_str:
        status_block = TextBlock()
        status_block.Text = status_str
        status_block.Foreground = SolidColorBrush(_LINK_FG)
        status_block.FontSize = 11
        status_block.VerticalAlignment = VerticalAlignment.Center
        status_block.HorizontalAlignment = HorizontalAlignment.Left
        status_block.TextTrimming = TextTrimming.CharacterEllipsis
        status_block.MaxWidth = 320
        status_block.Margin = Thickness(0, 0, 12, 0)
        DockPanel.SetDock(status_block, Dock.Left)
        inner.Children.Add(status_block)

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
    link.Foreground = SolidColorBrush(_LINK_FG)
    link.RequestNavigate += _open_site
    text.Inlines.Add(link)

    # Added last, with no explicit Dock - DockPanel.LastChildFill makes
    # it take the remaining space after status_block, and its own
    # Center alignment centers it within that remaining area.
    inner.Children.Add(text)

    bar.Child = inner
    return bar


class DeeBrandedWindow(forms.WPFWindow):
    def __init__(self, xaml_file):
        forms.WPFWindow.__init__(self, xaml_file)
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
        outer.Children.Add(original_content)
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
