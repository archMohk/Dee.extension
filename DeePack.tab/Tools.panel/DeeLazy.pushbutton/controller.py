# -*- coding: utf-8 -*-
"""
DeeLazy.controller
Card-based launcher window. Builds one card per entry in
modules.REGISTERED_MODULES via plain WPF constructors (the number of
modules grows over time, so the card list is built in code rather than
static XAML - the same "build it in code because the count varies"
pattern already used by DeeRelink's per-document tabs). This file
never needs to change to add a future module - see modules/__init__.py.
"""
import clr

clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("System.Windows.Forms")

from System.Windows import Thickness, TextWrapping, HorizontalAlignment, VerticalAlignment, FontWeights, CornerRadius
from System.Windows.Controls import Border, DockPanel, Dock, TextBlock, Button
from System.Windows.Input import Cursors
from System.Windows.Media import Brushes, SolidColorBrush, Color

from pyrevit import forms
import dee_branding

from modules import REGISTERED_MODULES

# The DeeLazy brand orange (same value dee_branding uses for the footer bar),
# reused here so a hovered card reads as part of the same tool rather than a
# generic Windows highlight.
_ACCENT = Color.FromRgb(0xF2, 0x99, 0x4D)
_HOVER_BORDER = SolidColorBrush(_ACCENT)
_HOVER_FILL = SolidColorBrush(Color.FromArgb(0x28, 0xF2, 0x99, 0x4D))
_IDLE_BORDER = Brushes.Gray


class DeeLazyHomeWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, uiapp):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.uiapp = uiapp
        self._build_cards()

    def _build_cards(self):
        for tool_info in REGISTERED_MODULES:
            card = self._make_card(tool_info)
            self.cards_panel.Children.Add(card)

    def _make_card(self, tool_info):
        """A DockPanel, not a plain vertical StackPanel, specifically so
        the Open button always stays anchored at the bottom of the card
        regardless of how long a module's description is - a
        StackPanel's total content height isn't clamped to the Border's
        fixed Height, so a longer description (a later module's, not
        necessarily this first one's) can silently push the button
        past the card's visible area instead of wrapping/clipping in
        its own space."""
        border = Border()
        border.Width = 250
        border.Height = 165
        border.Margin = Thickness(6)
        border.Padding = Thickness(10)
        border.BorderBrush = _IDLE_BORDER
        border.BorderThickness = Thickness(1)
        border.CornerRadius = CornerRadius(6)
        # A Border only receives mouse events where it has a Background, so
        # Transparent (not null) is what makes the WHOLE card hoverable and
        # clickable rather than just its text.
        border.Background = Brushes.Transparent
        border.Cursor = Cursors.Hand
        border.ToolTip = tool_info.get("description", "")
        self._wire_card_interaction(border, tool_info)

        panel = DockPanel()

        title_tb = TextBlock()
        title_tb.Text = tool_info.get("title", tool_info.get("id", "Tool"))
        title_tb.FontWeight = FontWeights.Bold
        title_tb.FontSize = 15
        title_tb.Margin = Thickness(0, 0, 0, 6)
        title_tb.TextWrapping = TextWrapping.Wrap
        DockPanel.SetDock(title_tb, Dock.Top)
        panel.Children.Add(title_tb)

        launch_b = Button()
        launch_b.Content = "Open"
        launch_b.Height = 28
        launch_b.Width = 90
        launch_b.HorizontalAlignment = HorizontalAlignment.Left
        launch_b.Click += self._make_launch_handler(tool_info)
        DockPanel.SetDock(launch_b, Dock.Bottom)
        panel.Children.Add(launch_b)

        # Last child - DockPanel.LastChildFill (default True) gives this
        # whatever space is left between the title and the button, so a
        # long description clips/scrolls within that space instead of
        # displacing the button.
        desc_tb = TextBlock()
        desc_tb.Text = tool_info.get("description", "")
        desc_tb.TextWrapping = TextWrapping.Wrap
        desc_tb.VerticalAlignment = VerticalAlignment.Top
        desc_tb.Margin = Thickness(0, 0, 0, 10)
        panel.Children.Add(desc_tb)

        border.Child = panel
        return border

    def _wire_card_interaction(self, border, tool_info):
        """Makes the whole card behave like one big button: it highlights on
        hover and opens on click, so the small Open button is a visual cue
        rather than the only target. Handlers are built in closures because
        every card needs its own - the same reason _make_launch_handler
        already exists."""
        launch = self._make_launch_handler(tool_info)

        def on_enter(sender, args):
            sender.BorderBrush = _HOVER_BORDER
            sender.BorderThickness = Thickness(2)
            sender.Background = _HOVER_FILL

        def on_leave(sender, args):
            sender.BorderBrush = _IDLE_BORDER
            sender.BorderThickness = Thickness(1)
            sender.Background = Brushes.Transparent

        def on_click(sender, args):
            launch(sender, args)

        border.MouseEnter += on_enter
        border.MouseLeave += on_leave
        border.MouseLeftButtonUp += on_click

    def _make_launch_handler(self, tool_info):
        def handler(sender, args):
            try:
                tool_info["launch"](self.uiapp)
            except Exception as e:
                forms.alert("Could not open '{0}':\n{1}".format(tool_info.get("title", "Tool"), e))
        return handler

    def close_click(self, sender, args):
        self.Close()
