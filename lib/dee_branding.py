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
from System import Uri
from System.Diagnostics import Process
from System.Windows import Thickness, HorizontalAlignment
from System.Windows.Controls import DockPanel, Dock, TextBlock, Border
from System.Windows.Documents import Hyperlink, Run
from System.Windows.Media import SolidColorBrush, Color

from pyrevit import forms

_SITE_URL = "http://www.archMKD.com"
_SITE_LABEL = "www.archMKD.com"
_BAR_HEIGHT = 26.0
_BAR_BG = Color.FromRgb(0xE0, 0x7A, 0x1E)
_LINK_FG = Color.FromRgb(0xF0, 0xF0, 0xF0)


def _open_site(sender, args):
    try:
        Process.Start(_SITE_URL)
    except Exception:
        pass
    args.Handled = True


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
