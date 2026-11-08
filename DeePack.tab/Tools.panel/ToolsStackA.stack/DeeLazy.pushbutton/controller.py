# -*- coding: utf-8 -*-
"""
DeeLazy.controller
Launcher window: a category list on the left, a search box on top and
the tool cards grouped under category headers on the right. Everything
is built in code from modules.REGISTERED_MODULES (the number of modules
grows over time) - this file never needs to change to add a module:
give its TOOL_INFO a "category" (one of CATEGORIES below, or any new
name, which then shows up as its own group at the end) and it appears.
"""
import clr

clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("System.Windows.Forms")

from System.Windows import Thickness, TextWrapping, VerticalAlignment, FontWeights, CornerRadius, Visibility
from System.Windows.Controls import Border, DockPanel, Dock, TextBlock, WrapPanel, StackPanel, Orientation
from System.Windows.Input import Cursors, Key
from System.Windows.Media import Brushes, SolidColorBrush, Color, FontFamily

# TextBlock's default font (Segoe UI) has no glyphs for the emoji used as
# per-tool icons - Segoe UI Emoji ships on every Windows 10/11.
_EMOJI_FONT = FontFamily("Segoe UI Emoji")
_FALLBACK_ICON = u"\U0001F6E0"  # hammer and wrench

import dee_branding

from modules import REGISTERED_MODULES

# (name, icon, one-line hint). Order = order on screen. A TOOL_INFO whose
# "category" is not listed here still shows, in its own group at the end.
CATEGORIES = [
    ("Views & Sheets", u"\U0001F5C2", "Crop, rename, stamp, template and convert views and sheets in bulk"),
    ("Drafting & Annotation", u"✏", "Copy drafting between sheets, fit title lines, convert fill patterns"),
    ("Selection & Editing", u"\U0001F3AF", "Select, colour, move and mirror elements"),
    ("Files, Links & ACC", u"☁", "Work on many models at once - local files, open models or ACC"),
]
_OTHER = "Other"
_ALL = "All tools"

_ACCENT = Color.FromRgb(0xF2, 0x99, 0x4D)
_HOVER_BORDER = SolidColorBrush(_ACCENT)
_HOVER_FILL = SolidColorBrush(Color.FromArgb(0x28, 0xF2, 0x99, 0x4D))
_ACTIVE_FILL = SolidColorBrush(Color.FromArgb(0x40, 0xF2, 0x99, 0x4D))
_HEADER_BRUSH = SolidColorBrush(_ACCENT)
_IDLE_BORDER = Brushes.Gray


def _category_of(tool_info):
    return tool_info.get("category") or _OTHER


class DeeLazyHomeWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, uiapp):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.uiapp = uiapp
        # Set by a card's click handler, read by script.py AFTER
        # ShowDialog() returns - the launch happens there, never from
        # inside this still-open window.
        self.picked_tool_info = None
        self._current = _ALL
        self._cat_items = {}        # category name -> sidebar Border
        self._groups = {}           # category name -> (header Border, WrapPanel, [(card, tool_info)])
        self._build_sidebar()
        self._build_groups()
        self._ready = True
        self._apply_filter()
        self.search_tb.PreviewKeyDown += self._search_key
        try:
            self.search_tb.Focus()
        except Exception:
            pass

    # ---------------- categories ----------------
    def _ordered_categories(self):
        names = [c[0] for c in CATEGORIES]
        extra = sorted(set(_category_of(t) for t in REGISTERED_MODULES) - set(names))
        return names + extra

    def _category_meta(self, name):
        for cat_name, icon, hint in CATEGORIES:
            if cat_name == name:
                return icon, hint
        return u"\U0001F9F0", ""   # toolbox

    def _build_sidebar(self):
        total = len(REGISTERED_MODULES)
        self.cat_panel.Children.Add(self._make_cat_item(_ALL, u"⭐", total, "Every DeeLazy tool"))
        for name in self._ordered_categories():
            count = len([t for t in REGISTERED_MODULES if _category_of(t) == name])
            if not count:
                continue
            icon, hint = self._category_meta(name)
            self.cat_panel.Children.Add(self._make_cat_item(name, icon, count, hint))

    def _make_cat_item(self, name, icon, count, hint):
        border = Border()
        border.Margin = Thickness(0, 0, 0, 4)
        border.Padding = Thickness(8, 6, 8, 6)
        border.CornerRadius = CornerRadius(5)
        border.Background = Brushes.Transparent
        border.BorderThickness = Thickness(1)
        border.BorderBrush = Brushes.Transparent
        border.Cursor = Cursors.Hand
        border.ToolTip = hint or name
        row = DockPanel()
        count_tb = TextBlock()
        count_tb.Text = str(count)
        count_tb.Opacity = 0.6
        count_tb.VerticalAlignment = VerticalAlignment.Center
        DockPanel.SetDock(count_tb, Dock.Right)
        row.Children.Add(count_tb)
        icon_tb = TextBlock()
        icon_tb.Text = icon
        icon_tb.FontFamily = _EMOJI_FONT
        icon_tb.Margin = Thickness(0, 0, 8, 0)
        icon_tb.VerticalAlignment = VerticalAlignment.Center
        DockPanel.SetDock(icon_tb, Dock.Left)
        row.Children.Add(icon_tb)
        name_tb = TextBlock()
        name_tb.Text = name
        name_tb.VerticalAlignment = VerticalAlignment.Center
        name_tb.TextWrapping = TextWrapping.Wrap
        row.Children.Add(name_tb)
        border.Child = row
        border.Tag = name
        self._cat_items[name] = (border, name_tb)

        def on_enter(sender, args):
            if self._current != sender.Tag:
                sender.Background = _HOVER_FILL

        def on_leave(sender, args):
            if self._current != sender.Tag:
                sender.Background = Brushes.Transparent

        def on_click(sender, args):
            self._select_category(sender.Tag)

        border.MouseEnter += on_enter
        border.MouseLeave += on_leave
        border.MouseLeftButtonUp += on_click
        return border

    def _select_category(self, name):
        self._current = name
        for cat_name, (border, name_tb) in self._cat_items.items():
            active = cat_name == name
            border.Background = _ACTIVE_FILL if active else Brushes.Transparent
            border.BorderBrush = _HOVER_BORDER if active else Brushes.Transparent
            name_tb.FontWeight = FontWeights.Bold if active else FontWeights.Normal
        self._apply_filter()
        try:
            self.scroller.ScrollToTop()
        except Exception:
            pass

    # ---------------- groups + cards ----------------
    def _build_groups(self):
        for name in self._ordered_categories():
            tools = [t for t in REGISTERED_MODULES if _category_of(t) == name]
            if not tools:
                continue
            icon, hint = self._category_meta(name)
            header = Border()
            header.BorderThickness = Thickness(0, 0, 0, 2)
            header.BorderBrush = _HEADER_BRUSH
            header.Margin = Thickness(6, 8, 6, 4)
            header.Padding = Thickness(0, 0, 0, 3)
            head = StackPanel()
            head.Orientation = Orientation.Horizontal
            h_icon = TextBlock()
            h_icon.Text = icon
            h_icon.FontFamily = _EMOJI_FONT
            h_icon.FontSize = 16
            h_icon.Margin = Thickness(0, 0, 8, 0)
            head.Children.Add(h_icon)
            h_tb = TextBlock()
            h_tb.Text = name
            h_tb.FontSize = 16
            h_tb.FontWeight = FontWeights.Bold
            head.Children.Add(h_tb)
            h_hint = TextBlock()
            h_hint.Text = u"  -  " + hint if hint else ""
            h_hint.Opacity = 0.65
            h_hint.VerticalAlignment = VerticalAlignment.Bottom
            h_hint.Margin = Thickness(0, 0, 0, 1)
            head.Children.Add(h_hint)
            header.Child = head
            self.groups_panel.Children.Add(header)
            wrap = WrapPanel()
            wrap.Orientation = Orientation.Horizontal
            self.groups_panel.Children.Add(wrap)
            cards = []
            for tool_info in tools:
                card = self._make_card(tool_info)
                wrap.Children.Add(card)
                cards.append((card, tool_info))
            self._groups[name] = (header, wrap, cards)

    def _apply_filter(self):
        if not getattr(self, "_ready", False):
            return
        text = (self.search_tb.Text or "").strip().lower()
        shown = 0
        for name, (header, wrap, cards) in self._groups.items():
            in_cat = self._current in (_ALL, name)
            visible_here = 0
            for card, tool_info in cards:
                hit = in_cat and (not text or self._matches(tool_info, text))
                card.Visibility = Visibility.Visible if hit else Visibility.Collapsed
                visible_here += 1 if hit else 0
            header.Visibility = wrap.Visibility = Visibility.Visible if visible_here else Visibility.Collapsed
            shown += visible_here
        total = len(REGISTERED_MODULES)
        self.count_tb.Text = ("{0} of {1} tools".format(shown, total) if (text or self._current != _ALL)
                              else "{0} tools".format(total))

    @staticmethod
    def _matches(tool_info, text):
        hay = u" ".join([tool_info.get("title", ""), tool_info.get("description", ""),
                         tool_info.get("id", ""), _category_of(tool_info)]).lower()
        return all(word in hay for word in text.split())

    def search_changed(self, sender, args):
        self._apply_filter()

    def _search_key(self, sender, args):
        if args.Key == Key.Escape:
            self.search_tb.Text = ""
            args.Handled = True

    def _make_card(self, tool_info):
        """The whole card is the button: hover highlight + hand cursor are
        the affordance. DockPanel so a long description clips inside the
        fixed card height instead of spilling past it."""
        border = Border()
        border.Width = 235
        border.Height = 170
        border.Margin = Thickness(6)
        border.Padding = Thickness(10)
        border.BorderBrush = _IDLE_BORDER
        border.BorderThickness = Thickness(1)
        border.CornerRadius = CornerRadius(6)
        border.Background = Brushes.Transparent   # needed for the whole card to take mouse events
        border.Cursor = Cursors.Hand
        border.ToolTip = tool_info.get("description", "")
        self._wire_card_interaction(border, tool_info)

        panel = DockPanel()
        top = StackPanel()
        top.Orientation = Orientation.Horizontal
        top.Margin = Thickness(0, 0, 0, 6)
        icon_tb = TextBlock()
        icon_tb.Text = tool_info.get("icon") or _FALLBACK_ICON
        icon_tb.FontFamily = _EMOJI_FONT
        icon_tb.FontSize = 24
        icon_tb.Margin = Thickness(0, 0, 8, 0)
        icon_tb.VerticalAlignment = VerticalAlignment.Center
        top.Children.Add(icon_tb)
        title_tb = TextBlock()
        title_tb.Text = tool_info.get("title", tool_info.get("id", "Tool"))
        title_tb.FontWeight = FontWeights.Bold
        title_tb.FontSize = 14
        title_tb.TextWrapping = TextWrapping.Wrap
        title_tb.VerticalAlignment = VerticalAlignment.Center
        title_tb.MaxWidth = 170
        top.Children.Add(title_tb)
        DockPanel.SetDock(top, Dock.Top)
        panel.Children.Add(top)

        desc_tb = TextBlock()
        desc_tb.Text = tool_info.get("description", "")
        desc_tb.TextWrapping = TextWrapping.Wrap
        desc_tb.VerticalAlignment = VerticalAlignment.Top
        desc_tb.Opacity = 0.85
        panel.Children.Add(desc_tb)

        border.Child = panel
        return border

    def _wire_card_interaction(self, border, tool_info):
        launch = self._make_launch_handler(tool_info)

        def on_enter(sender, args):
            sender.BorderBrush = _HOVER_BORDER
            sender.BorderThickness = Thickness(2)
            sender.Background = _HOVER_FILL

        def on_leave(sender, args):
            sender.BorderBrush = _IDLE_BORDER
            sender.BorderThickness = Thickness(1)
            sender.Background = Brushes.Transparent

        border.MouseEnter += on_enter
        border.MouseLeave += on_leave
        border.MouseLeftButtonUp += launch

    def _make_launch_handler(self, tool_info):
        """Records the pick and closes the launcher - the launch itself
        happens in script.py once ShowDialog() has returned, so the
        tool's window is never opened from inside this one."""
        def handler(sender, args):
            self.picked_tool_info = tool_info
            self.Close()
        return handler

    def close_click(self, sender, args):
        self.Close()
