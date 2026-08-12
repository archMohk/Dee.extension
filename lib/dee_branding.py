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

Rounded corners: a real, custom-chrome version of this (WindowStyle=
None + AllowsTransparency=True + a hand-built title bar/close button)
was tried and reverted - pyrevit.forms.WPFWindow.setup_owner() (called
during forms.WPFWindow.__init__, i.e. before any of this module's own
code ever runs) sets the window's owner via
System.Windows.Interop.WindowInteropHelper(self).Owner = <Revit's
window>, which forces the window's native handle to exist earlier than
usual - WindowStyle/AllowsTransparency can only be changed before that
handle exists, so setting them afterward is unreliable in ways that
showed up live as a broken close button and then a frozen window,
neither reproducible or fixable without live debugging access. Not
worth that risk for a cosmetic feature, so this module only attempts
rounding the safe way: asking Windows' own DWM to do it
(DWMWA_WINDOW_CORNER_PREFERENCE) - Windows 11 only, a silent no-op on
Windows 10 (this project's current dev machine), never touches
WindowStyle/AllowsTransparency/chrome, so it can't cause this class of
bug.
"""
import clr
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("System")
from System import Uri, IntPtr
from System.Diagnostics import Process
from System.Windows import Thickness, HorizontalAlignment, VerticalAlignment, FontWeights
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


def _build_bar():
    bar = Border()
    bar.Background = SolidColorBrush(_BAR_BG)
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
