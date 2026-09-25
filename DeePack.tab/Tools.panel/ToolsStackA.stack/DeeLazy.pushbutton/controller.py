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

from System.Windows import Thickness, TextWrapping, VerticalAlignment, FontWeights, CornerRadius
from System.Windows.Controls import Border, DockPanel, Dock, TextBlock
from System.Windows.Input import Cursors
from System.Windows.Media import Brushes, SolidColorBrush, Color

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
        # Set by a card's click handler, read by script.py AFTER
        # ShowDialog() returns - the actual launch happens there, never
        # from inside this still-open window. None means the user
        # closed the launcher without picking anything.
        self.picked_tool_info = None
        self._build_cards()

    def _build_cards(self):
        for tool_info in REGISTERED_MODULES:
            card = self._make_card(tool_info)
            self.cards_panel.Children.Add(card)

    def _make_card(self, tool_info):
        """The whole card is the button - there is no separate Open button,
        because clicking anywhere on the card launches the tool and the hover
        highlight plus the hand cursor are the affordance.

        Still a DockPanel rather than a plain vertical StackPanel: the Border
        has a fixed Height, and a StackPanel's content height isn't clamped to
        it, so a longer description (a later module's) would silently spill
        past the card's visible edge. DockPanel.LastChildFill gives the
        description exactly the space left under the title, so it clips inside
        the card instead."""
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

        # Last child - DockPanel.LastChildFill (default True) gives this
        # whatever space is left under the title, so a long description clips
        # within that space instead of spilling past the card's fixed height.
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
        hover and opens on click. Handlers are built in closures because every
        card needs its own bound to its own tool_info - the same reason
        _make_launch_handler already exists."""
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
        """Records which tool was picked and closes the launcher
        immediately - it does NOT call tool_info["launch"] itself. That
        call happens in script.py, once this window's ShowDialog() has
        actually returned, so the picked tool's own window is never
        opened from inside this one (see this module's own docstring
        note, and DeeMono/Dee3DView's hub for the same pattern)."""
        def handler(sender, args):
            self.picked_tool_info = tool_info
            self.Close()
        return handler

    def close_click(self, sender, args):
        self.Close()
