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

Rounded windows (About -> Interface -> Effects -> "Rounded windows"):
an option for every tool. When on, DeeBrandedWindow swaps the Windows
frame for rounded corners and a slim title bar drawn in the same theme
colours (see the "Rounded windows" section below for how and why the
owner setup is deferred). Off, it only asks Windows 11's DWM for its
own small rounded corners (a silent no-op on Windows 10).
DeeRoundedWindow is kept as a plain alias of DeeBrandedWindow.
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
    WindowStyle, CornerRadius, Rect, TextTrimming, Window, WindowState,
    ResizeMode, SystemParameters, PresentationSource, SizeToContent
)
from System.Windows.Controls import (DockPanel, Dock, TextBlock, Border, Grid,
                                     Button, ProgressBar, ColumnDefinition)
from System.Windows import GridLength, GridUnitType, Visibility
from System.Windows.Documents import Hyperlink, Run
from System.Windows.Media import (SolidColorBrush, Color, Brushes, RectangleGeometry,
                                  FontFamily)
from System.Windows.Data import Binding
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


def open_windows():
    """Every Dee window currently on screen."""
    return list(_open_windows)


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
            ctypes.addressof(pref), ctypes.sizeof(pref))   # byref() fails on c_void_p in IronPython
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

        # Accent line along the top - hidden unless the "Accent top line"
        # effect is on (dee_effects).
        topline = Border()
        topline.Height = 3
        topline.SetResourceReference(Border.BackgroundProperty, "DeeOrange")
        topline.Visibility = Visibility.Collapsed
        DockPanel.SetDock(topline, Dock.Top)

        panel = DockPanel()
        panel.Children.Add(topline)
        panel.Children.Add(strip)
        panel.Children.Add(content)
        window._dee_topline = topline

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
    def __init__(self, xaml_file, theme=True, rounded=None):
        # rounded=None follows About -> Interface -> "Rounded windows".
        if rounded is None:
            rounded = rounded_wanted()
        # Rounded chrome must be set before the native handle exists, and
        # pyrevit's owner setup creates it - so defer that (see below).
        forms.WPFWindow.__init__(self, xaml_file, set_owner=not rounded)
        set_window_accent(self)
        # Every branded window gets lib/DeeTheme.xaml (shared button /
        # input / grid look). theme=False is an escape hatch only - no
        # tool uses it, so every window shares the same light theme.
        if theme:
            apply_theme(self)
        self._dee_rounded = False
        if rounded:
            try:
                _make_rounded(self)
            except Exception:
                pass
            try:
                self.setup_owner()
            except Exception:
                pass
        if not self._dee_rounded:
            try:
                self._add_footer_bar()
            except Exception:
                # A branding-bar failure should never block a tool from
                # opening - worst case the window just looks like it did
                # before this module existed.
                pass
        try:
            # Glass / outline / fade ... from About -> Interface.
            import dee_effects
            dee_effects.attach(self)
        except Exception:
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
# Rounded windows - an option for every tool (About -> Interface -> Effects)
# ============================================================================
# Real rounded corners on Windows 10 need custom chrome: WindowStyle=None +
# AllowsTransparency=True + a hand-built title bar. Both properties can only
# be changed before the window has a native handle, and pyrevit's
# forms.WPFWindow.__init__ normally creates it early (setup_owner). So when
# the option is on, DeeBrandedWindow passes set_owner=False, switches the
# chrome while no handle exists yet, then calls self.setup_owner() itself.
# Any failure reverts to the normal window - never a window that can't be
# moved or closed. Everything in the custom chrome is drawn from the same
# theme resources (DeeWindowBg / DeeSurface / DeeText / DeeBorder...) as
# the rest of the window, so a rounded tool looks exactly like any other.
_TITLE_BAR_HEIGHT = 30.0
_CORNER_RADIUS = 10.0
_CLOSE_HOVER_BG = Color.FromRgb(0xC4, 0x2B, 0x1C)
_GLYPH_FONT = "Segoe MDL2 Assets"
_GLYPH_MIN = u""
_GLYPH_MAX = u""
_GLYPH_RESTORE = u""
_GLYPH_CLOSE = u""


def rounded_wanted():
    """The "Rounded windows" option from About -> Interface -> Effects."""
    try:
        import dee_effects
        return bool(dee_effects.load_effects().get("rounded"))
    except Exception:
        return False


def _try_drag(window):
    try:
        window.DragMove()
    except Exception:
        pass


def _detach(element):
    """If a half-built layout left `element` parented somewhere, free it so
    the fallback can put it back in the window."""
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


def _caption_button(glyph, tooltip, on_click, hover_bg=None):
    area = Border()
    area.Width = 40
    area.Height = _TITLE_BAR_HEIGHT
    area.Background = Brushes.Transparent
    area.Cursor = Cursors.Hand
    area.ToolTip = tooltip
    text = TextBlock()
    text.Text = glyph
    text.FontFamily = FontFamily(_GLYPH_FONT)
    text.FontSize = 10
    text.HorizontalAlignment = HorizontalAlignment.Center
    text.VerticalAlignment = VerticalAlignment.Center
    text.SetResourceReference(TextBlock.ForegroundProperty, "DeeTitleFg")
    area.Child = text

    def _enter(sender, args):
        if hover_bg is not None:
            area.Background = SolidColorBrush(hover_bg)
            text.Foreground = Brushes.White
        else:
            area.Background = SolidColorBrush(Color.FromArgb(0x30, 0x80, 0x80, 0x80))

    def _leave(sender, args):
        area.Background = Brushes.Transparent
        text.SetResourceReference(TextBlock.ForegroundProperty, "DeeTitleFg")

    def _up(sender, args):
        # A plain Border + MouseLeftButtonUp, not a Button: ButtonBase's
        # click state machine and WindowChrome's hit-testing in the very
        # corner fought each other in an earlier attempt.
        args.Handled = True
        try:
            on_click()
        except Exception:
            pass

    area.MouseEnter += _enter
    area.MouseLeave += _leave
    area.MouseLeftButtonUp += _up
    WindowChrome.SetIsHitTestVisibleInChrome(area, True)
    return area, text


def _work_area(window):
    """The working area (screen minus taskbar) of the monitor the window
    is on, in WPF units."""
    try:
        clr.AddReference("System.Windows.Forms")
        from System.Windows.Forms import Screen
        hwnd = WindowInteropHelper(window).Handle
        area = Screen.FromHandle(hwnd).WorkingArea
        src = PresentationSource.FromVisual(window)
        m = src.CompositionTarget.TransformFromDevice
        return Rect(area.Left * m.M11, area.Top * m.M22,
                    area.Width * m.M11, area.Height * m.M22)
    except Exception:
        return SystemParameters.WorkArea


def _toggle_maximise(window):
    """WindowStyle=None windows cover the taskbar when truly maximised,
    so "maximise" here fills the monitor's working area instead."""
    border = getattr(window, "_dee_round_border", None)
    saved = getattr(window, "_dee_restore_bounds", None)
    if saved is not None:
        window.Left, window.Top, window.Width, window.Height = saved
        window._dee_restore_bounds = None
        if border is not None:
            border.CornerRadius = CornerRadius(_CORNER_RADIUS)
        glyph = _GLYPH_MAX
    else:
        window._dee_restore_bounds = (window.Left, window.Top,
                                      window.ActualWidth, window.ActualHeight)
        window.SizeToContent = SizeToContent.Manual
        area = _work_area(window)
        window.Left, window.Top = area.Left, area.Top
        window.Width, window.Height = area.Width, area.Height
        if border is not None:
            border.CornerRadius = CornerRadius(0)
        glyph = _GLYPH_RESTORE
    text = getattr(window, "_dee_max_text", None)
    if text is not None:
        text.Text = glyph


def _build_title_bar(window):
    bar = Grid()
    bar.Height = _TITLE_BAR_HEIGHT
    bar.SetResourceReference(Grid.BackgroundProperty, "DeeTitleBg")
    DockPanel.SetDock(bar, Dock.Top)
    bar.ColumnDefinitions.Add(ColumnDefinition())
    for _i in range(3):
        col = ColumnDefinition()
        col.Width = GridLength.Auto
        bar.ColumnDefinitions.Add(col)

    title = TextBlock()
    title.SetBinding(TextBlock.TextProperty, Binding("Title"))
    title.DataContext = window
    title.FontSize = 12
    title.Margin = Thickness(12, 0, 8, 0)
    title.VerticalAlignment = VerticalAlignment.Center
    title.TextTrimming = TextTrimming.CharacterEllipsis
    title.SetResourceReference(TextBlock.ForegroundProperty, "DeeTitleFg")
    bar.Children.Add(title)

    mode = window.ResizeMode
    no_resize = getattr(ResizeMode, "NoResize")
    buttons = []
    if mode != no_resize:
        def _minimise():
            window.WindowState = WindowState.Minimized
        buttons.append(_caption_button(_GLYPH_MIN, "Minimise", _minimise))
    if mode in (ResizeMode.CanResize, ResizeMode.CanResizeWithGrip):
        max_btn = _caption_button(_GLYPH_MAX, "Maximise / restore",
                                  lambda: _toggle_maximise(window))
        window._dee_max_text = max_btn[1]
        buttons.append(max_btn)
    buttons.append(_caption_button(_GLYPH_CLOSE, "Close", window.Close, _CLOSE_HOVER_BG))
    for i, (area, _text) in enumerate(buttons):
        Grid.SetColumn(area, 1 + i + (3 - len(buttons)))
        bar.Children.Add(area)
    hit = [b[0] for b in buttons] + [b[1] for b in buttons]
    can_max = mode in (ResizeMode.CanResize, ResizeMode.CanResizeWithGrip)

    def _down(sender, args):
        if args.OriginalSource in hit:
            return
        if args.ClickCount == 2 and can_max:
            _toggle_maximise(window)
            return
        if getattr(window, "_dee_restore_bounds", None) is not None:
            return      # filling the screen: stay put, like Windows does
        _try_drag(window)

    bar.MouseLeftButtonDown += _down
    window._dee_titlebar = bar
    return bar


def _set_round_chrome(window):
    window.WindowStyle = getattr(WindowStyle, "None")
    window.AllowsTransparency = True
    window.Background = Brushes.Transparent
    chrome = WindowChrome()
    chrome.CaptionHeight = 0.0
    no_resize = window.ResizeMode in (getattr(ResizeMode, "NoResize"), ResizeMode.CanMinimize)
    chrome.ResizeBorderThickness = Thickness(0 if no_resize else 6)
    chrome.GlassFrameThickness = Thickness(0)
    chrome.UseAeroCaptionButtons = False
    chrome.CornerRadius = CornerRadius(0)
    WindowChrome.SetWindowChrome(window, chrome)


def _clear_round_chrome(window):
    try:
        WindowChrome.SetWindowChrome(window, None)
        window.WindowStyle = WindowStyle.SingleBorderWindow
        window.AllowsTransparency = False
        window.ClearValue(Window.BackgroundProperty)
    except Exception:
        pass


def _make_rounded(window):
    """Rebuilds the window as a rounded one: title bar, the window's own
    content (with the progress strip) and the website bar, inside one
    rounded border. Returns False, leaving the window untouched, if any
    step fails."""
    original = window.Content
    if original is None:
        return False
    try:
        # Title-bar colours: the window's surface and text, unless the
        # "Accent title bar" effect repoints them at the accent.
        window.Resources["DeeTitleBg"] = window.TryFindResource("DeeSurface") or Brushes.White
        window.Resources["DeeTitleFg"] = window.TryFindResource("DeeText") or Brushes.Black
        _set_round_chrome(window)
        window.Content = None
        border = Border()
        border.CornerRadius = CornerRadius(_CORNER_RADIUS)
        border.BorderThickness = Thickness(1)
        border.SetResourceReference(Border.BackgroundProperty, "DeeWindowBg")
        border.SetResourceReference(Border.BorderBrushProperty, "DeeBorderStrong")
        inner = DockPanel()
        inner.Children.Add(_build_title_bar(window))
        inner.Children.Add(_build_bar())
        inner.Children.Add(_wrap_with_progress_strip(window, original))
        border.Child = inner

        def _clip(sender, args):
            try:
                r = border.CornerRadius.TopLeft
                border.Clip = RectangleGeometry(
                    Rect(0, 0, max(border.ActualWidth, 0.0), max(border.ActualHeight, 0.0)), r, r)
            except Exception:
                pass
        border.SizeChanged += _clip
        window.Content = border
        window._dee_round_border = border
        window._dee_rounded = True
        try:
            if window.Height and window.Height > 0:
                window.Height = window.Height + _BAR_HEIGHT + _TITLE_BAR_HEIGHT
        except Exception:
            pass
        return True
    except Exception:
        _detach(original)
        window.Content = original
        _clear_round_chrome(window)
        window._dee_round_border = None
        window._dee_titlebar = None
        window._dee_rounded = False
        return False


class DeeRoundedWindow(DeeBrandedWindow):
    """Kept so older code that names it keeps working: it is now simply a
    DeeBrandedWindow, and the rounded style follows the Interface option
    like every other tool."""
    pass
