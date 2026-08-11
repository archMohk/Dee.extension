# -*- coding: utf-8 -*-
"""
dee_branding
Adds two things to the bottom (and top) of every Dee.extension tool
window: a thin orange footer bar with a white hyperlink to
www.archMKD.com, and small rounded window corners.

Real rounded corners on Windows 10 require replacing the OS-native
window chrome entirely (WindowStyle=None + AllowsTransparency=True) -
there is no way to keep the native title bar/frame AND get rounded
corners pre-Windows 11. So this module:
  1. Switches every window to WindowStyle=None + AllowsTransparency,
     wraps the window's own original content in a white, CornerRadius
     Border whose Clip is kept in sync with its actual size (that's
     what actually produces the rounded look).
  2. Attaches a System.Windows.Shell.WindowChrome so resizing by
     dragging an edge/corner keeps working automatically, without
     hand-building resize logic.
  3. Draws a minimal custom title bar (window Title text + an X close
     button) since the native one is gone - dragging the window is
     done via Window.DragMove() on that bar's MouseLeftButtonDown.
  4. Also asks Windows' DWM for small rounded corners the native way
     (DWMWA_WINDOW_CORNER_PREFERENCE) - redundant once the custom
     chrome above is in place (there's no native frame left for DWM to
     round), but harmless to leave in, and it is what would matter if
     a future version of this module ever stops replacing the chrome.

If anything in the custom-chrome path fails for any reason, this
module reverts the window to a normal native-chrome window and falls
back to just the footer bar (the simpler, already-proven-safe look) -
never leaves a window stuck with no native AND no custom way to move,
resize, or close it.

A window opts into all of this purely by inheriting DeeBrandedWindow
instead of pyrevit.forms.WPFWindow, and calling
DeeBrandedWindow.__init__(self, xaml_file) instead of
forms.WPFWindow.__init__(self, xaml_file) - no existing ui.xaml file
needs editing.
"""
import clr
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("System")
from System import Uri, IntPtr
from System.Diagnostics import Process
from System.Windows import (
    Thickness, HorizontalAlignment, VerticalAlignment, WindowStyle,
    CornerRadius, Rect
)
from System.Windows.Controls import DockPanel, Dock, TextBlock, Border, Button, Grid
from System.Windows.Documents import Hyperlink, Run
from System.Windows.Media import SolidColorBrush, Color, Brushes, RectangleGeometry
from System.Windows.Input import Cursors
from System.Windows.Interop import WindowInteropHelper
from System.Windows.Shell import WindowChrome

from pyrevit import forms

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
_BAR_HEIGHT = 26.0
_BAR_BG = Color.FromRgb(0xF2, 0x99, 0x4D)
_LINK_FG = Color.FromRgb(0xFF, 0xFF, 0xFF)

_TITLE_BAR_HEIGHT = 28.0
_TITLE_BAR_BG = Color.FromRgb(0x2B, 0x2B, 0x2B)
_TITLE_BAR_FG = Color.FromRgb(0xF0, 0xF0, 0xF0)
_CORNER_RADIUS = 10.0
_BORDER_STROKE = Color.FromRgb(0xB0, 0xB0, 0xB0)

# DWMWA_WINDOW_CORNER_PREFERENCE / DWMWCP_ROUNDSMALL - see module
# docstring point 4. Windows 11 only; DwmSetWindowAttribute just fails
# silently (never checked) on Windows 10.
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
    bar.Background = SolidColorBrush(_BAR_BG)
    bar.Padding = Thickness(8, 4, 8, 4)
    bar.Height = _BAR_HEIGHT
    DockPanel.SetDock(bar, Dock.Bottom)

    text = TextBlock()
    text.HorizontalAlignment = HorizontalAlignment.Center
    text.FontSize = 11

    link = Hyperlink(Run(_SITE_LABEL))
    try:
        link.NavigateUri = Uri(_SITE_URL)
    except Exception:
        pass
    link.Foreground = SolidColorBrush(_LINK_FG)
    link.RequestNavigate += _open_site
    text.Inlines.Add(link)

    bar.Child = text
    return bar


def _try_drag(window):
    try:
        window.DragMove()
    except Exception:
        pass


def _mark_handled(sender, args):
    args.Handled = True


def _build_title_bar(window):
    bar = Grid()
    bar.Height = _TITLE_BAR_HEIGHT
    bar.Background = SolidColorBrush(_TITLE_BAR_BG)
    DockPanel.SetDock(bar, Dock.Top)

    title = TextBlock()
    title.Text = window.Title or ""
    title.Foreground = SolidColorBrush(_TITLE_BAR_FG)
    title.FontSize = 12
    title.VerticalAlignment = VerticalAlignment.Center
    title.HorizontalAlignment = HorizontalAlignment.Left
    title.Margin = Thickness(10, 0, 0, 0)
    bar.Children.Add(title)

    close_btn = Button()
    close_btn.Content = "X"
    close_btn.Width = 34
    close_btn.HorizontalAlignment = HorizontalAlignment.Right
    close_btn.VerticalAlignment = VerticalAlignment.Stretch
    close_btn.Background = Brushes.Transparent
    close_btn.BorderThickness = Thickness(0)
    close_btn.Foreground = SolidColorBrush(_TITLE_BAR_FG)
    close_btn.FontSize = 12
    close_btn.Cursor = Cursors.Hand
    WindowChrome.SetIsHitTestVisibleInChrome(close_btn, True)
    close_btn.Click += lambda sender, args: window.Close()
    # Marks the mouse-down Handled during the tunneling (Preview) phase
    # so it never reaches the title bar's own MouseLeftButtonDown
    # handler below and starts a drag - ButtonBase listens for its own
    # mouse events with handledEventsToo=True internally, so the button
    # still clicks normally despite this.
    close_btn.PreviewMouseLeftButtonDown += _mark_handled
    bar.Children.Add(close_btn)

    bar.MouseLeftButtonDown += lambda sender, args: _try_drag(window)

    return bar


def _detach(element):
    """Best-effort: if a failed _build_rounded_content call left
    original_content re-parented onto some now-abandoned container
    (e.g. it made it into the DockPanel but the build failed a step
    later), the fallback path re-adding it elsewhere would otherwise
    throw "already has a logical parent". Only matters for the narrow
    window between that Add and the end of _build_rounded_content -
    harmless no-op the rest of the time."""
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


def _build_rounded_content(window, original_content):
    border = Border()
    border.CornerRadius = CornerRadius(_CORNER_RADIUS)
    border.Background = SolidColorBrush(Color.FromRgb(0xFF, 0xFF, 0xFF))
    border.BorderBrush = SolidColorBrush(_BORDER_STROKE)
    border.BorderThickness = Thickness(1)

    inner = DockPanel()
    inner.Children.Add(_build_title_bar(window))
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


class DeeBrandedWindow(forms.WPFWindow):
    def __init__(self, xaml_file):
        forms.WPFWindow.__init__(self, xaml_file)
        try:
            self._apply_branding()
        except Exception:
            # Branding must never block a tool from opening - worst
            # case the window just looks like it did before this
            # module existed.
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

            self.Content = _build_rounded_content(self, original_content)
            self._bump_height(_BAR_HEIGHT + _TITLE_BAR_HEIGHT)
            return
        except Exception:
            pass

        _detach(original_content)

        # Custom chrome didn't come together cleanly - revert fully to
        # a normal native-chrome window and fall back to just the
        # footer bar, rather than risk a window with no native or
        # custom way to move, resize, or close.
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
