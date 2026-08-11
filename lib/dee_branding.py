# -*- coding: utf-8 -*-
"""
dee_branding
Adds a thin footer bar (orange background, light gray hyperlink text)
linking to www.archMKD.com to the bottom of every Dee.extension tool
window.

Wraps whatever the window's own XAML already put in Window.Content
inside a new outer DockPanel with the bar docked at the bottom - this
works no matter what the original root element was (Grid, DockPanel,
StackPanel, ...), so no existing ui.xaml file needs editing. A window
opts in purely by inheriting DeeBrandedWindow instead of
pyrevit.forms.WPFWindow, and calling
DeeBrandedWindow.__init__(self, xaml_file) instead of
forms.WPFWindow.__init__(self, xaml_file) - everything else about the
window (its own __init__ logic, event handlers, XAML) is untouched.

The bar height is added on top of the window's own Height (when it has
one) so the original content isn't squeezed to make room for it.
"""
import clr
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("System")
from System import Uri, IntPtr
from System.Diagnostics import Process
from System.Windows import Thickness, HorizontalAlignment
from System.Windows.Controls import DockPanel, Dock, TextBlock, Border
from System.Windows.Documents import Hyperlink, Run
from System.Windows.Media import SolidColorBrush, Color
from System.Windows.Interop import WindowInteropHelper

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

# DWMWA_WINDOW_CORNER_PREFERENCE / DWMWCP_ROUNDSMALL - a Windows 11 DWM
# attribute (added in the Windows 11 update to dwmapi.dll) that asks
# the OS compositor to draw small-radius rounded corners on a normal
# top-level window, keeping the native title bar / drag / resize /
# minimize-maximize-close chrome completely intact - unlike the classic
# WindowStyle="None" + AllowsTransparency trick, which replaces that
# chrome with hand-built XAML (dragging, resizing, min/max would all
# need to be reimplemented per window). On Windows 10 this attribute
# doesn't exist - DwmSetWindowAttribute just returns a failure HRESULT,
# which this module never checks, so corners silently stay square there
# rather than erroring.
_DWMWA_WINDOW_CORNER_PREFERENCE = 33
_DWMWCP_ROUNDSMALL = 3


def _open_site(sender, args):
    try:
        Process.Start(_SITE_URL)
    except Exception:
        pass
    args.Handled = True


def _round_corners(window):
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
        _round_corners(self)

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
