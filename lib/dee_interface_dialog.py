# -*- coding: utf-8 -*-
"""
dee_interface_dialog
The window behind About -> Interface. Four tabs:

Colours - three colours, one at a time ("Colour the: Accent / Lines /
  Website bar"):
  - Accent: the orange in every ribbon icon and the progress bars.
  - Lines:  the grey line work of the ribbon icons.
  - Website bar: the bar with www.archMKD.com under each tool window.
    Its own colour; reset, it follows the accent as it always did.
  Each has a 44-colour palette, "More colours..." (the full Windows
  colour picker) and a hex box.
Icon - the style every ribbon icon is drawn in (dee_accent.STYLES:
  Original, 3D, Glow, Tile, Inverted), each shown as a card drawn with
  the colours picked above, so the colours drive every style.
Effects - any combination of glass, accent outline, accent top line,
  fade in and accent title bar (lib/dee_effects.py).
Theme & shape - Light or Dark (lib/dee_dark.py), and the outline of every tool window: Standard, Rounded, Soft float,
  Cut corners, Leaf or Ticket (lib/dee_shapes.py), each drawn as a card;
  "Try it" opens a sample window in the picked shape.

Everything previews live on real ribbon icons, a sample progress bar and
this window itself. Apply saves it all (lib/.dee_accent.json) and
repaints the live ribbon and every open Dee window straight away - no
pyRevit reload. See lib/dee_accent.py and lib/dee_effects.py.
"""
import os

import clr
clr.AddReference("PresentationCore")
clr.AddReference("System.Windows.Forms")
clr.AddReference("System.Drawing")
from System import Uri, UriKind  # noqa: E402
from System.Windows import Thickness, Rect, CornerRadius, HorizontalAlignment  # noqa: E402
from System.Windows.Controls import (Button, Image, CheckBox, Border, Canvas,  # noqa: E402
                                     Grid, StackPanel, TextBlock)
from System.Windows.Input import Cursors  # noqa: E402
from System.Windows.Media import SolidColorBrush, Color  # noqa: E402
from System.Windows.Media.Effects import DropShadowEffect  # noqa: E402
from System.Windows.Shapes import Path, Rectangle  # noqa: E402
from System.Windows.Media.Imaging import BitmapImage, BitmapCacheOption  # noqa: E402
from System.Windows.Forms import ColorDialog, DialogResult  # noqa: E402
import System.Drawing  # noqa: E402

import dee_accent
import dee_branding
import dee_effects
import dee_shapes

_XAML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "DeeInterface.xaml")
_SAMPLE_XAML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "DeeShapeSample.xaml")
_CARD_W, _CARD_H = 112.0, 70.0     # the little window drawn on each shape card
_PREVIEW_BUTTONS = ["DeePublisher", "DeeSuperLINK", "Dee3DView", "DeeCleaner",
                    "DeeLazy", "DeeForce", "DeeIcon", "DeeMAPLink"]

_SELECTED_BORDER = SolidColorBrush(Color.FromRgb(0x22, 0x22, 0x22))
_PLAIN_BORDER = SolidColorBrush(Color.FromRgb(0xCC, 0xCC, 0xCC))

# Accent palette as defined; the Lines palette swaps the first swatch for
# the default grey and keeps the orange available as a line colour too.
_ACCENT_PRESETS = list(dee_accent.PRESETS)
_LINE_PRESETS = ([("Grey (default)", dee_accent.ICON_INK),
                  ("Orange", dee_accent.ICON_ACCENT)]
                 + list(dee_accent.PRESETS[1:]))
# The website bar palette: the brand orange the bar always had first,
# then the same colours as the accent.
_BAR_PRESETS = ([("Brand orange", dee_accent.BRAND_ORANGE)]
                + list(dee_accent.PRESETS[1:]))
_NAMES = {
    "accent": dict((h, n) for n, h in _ACCENT_PRESETS),
    "line": dict((h, n) for n, h in _LINE_PRESETS),
    "bar": dict((h, n) for n, h in _BAR_PRESETS),
}
_PRESETS = {"accent": _ACCENT_PRESETS, "line": _LINE_PRESETS, "bar": _BAR_PRESETS}
_STYLE_CARD_ICON = 48


def _brush(hex_value):
    r, g, b = dee_accent.hex_to_rgb(hex_value)
    return SolidColorBrush(Color.FromRgb(r, g, b))


def _load_image(path):
    bmp = BitmapImage()
    bmp.BeginInit()
    bmp.CacheOption = BitmapCacheOption.OnLoad   # never keep the file locked
    bmp.UriSource = Uri(path, UriKind.Absolute)
    bmp.EndInit()
    return bmp


class InterfaceDialog(dee_branding.DeeBrandedWindow):
    _ready = False

    def __init__(self, uiapp=None):
        dee_branding.DeeBrandedWindow.__init__(self, _XAML)
        self._uiapp = uiapp
        self._saved_accent = dee_accent.load_accent()
        self._saved_line = dee_accent.load_line()       # None = grey
        self._saved_bar = dee_accent.load_bar()         # None = follow accent
        self._saved_style = dee_accent.load_style()
        self._accent = self._saved_accent
        self._line = self._saved_line
        self._bar = self._saved_bar
        self._style = self._saved_style
        self._target = "accent"
        bundles = dee_accent._bundle_dirs()
        self._preview_sources = [os.path.join(bundles[n], "icon.png")
                                 for n in _PREVIEW_BUTTONS if n in bundles]
        self._swatches = []
        self._build_presets()
        self._style_cards = {}
        self._style_images = {}
        self._build_styles()
        self._saved_effects = dee_effects.load_effects()
        self._effects = dict(self._saved_effects)
        self._effect_boxes = {}
        self._build_effects()
        self._shape_cards = {}
        self._build_shapes()
        if self._effects.get("theme") == "dark":
            self.theme_dark_rb.IsChecked = True
        else:
            self.theme_light_rb.IsChecked = True
        dee_effects.when_ready(self, lambda: dee_effects.apply(self, self._effects))
        self._ready = True
        self.hex_tb.Text = self._target_value()
        self._refresh()

    # ---- the colour being edited ----
    def _bar_default(self):
        """What the website bar shows with no colour of its own."""
        return dee_accent.window_accent(self._accent)

    def _target_value(self):
        if self._target == "accent":
            return self._accent
        if self._target == "bar":
            return self._bar or self._bar_default()
        return self._line or dee_accent.ICON_INK

    def _set_target_value(self, value):
        if self._target == "accent":
            self._accent = value
        elif self._target == "bar":
            # Picking the accent's own colour means "follow the accent".
            self._bar = None if value == self._bar_default() else value
        else:
            # Picking the default grey means "no line colour".
            self._line = None if value == dee_accent.ICON_INK else value

    # ---- building ----
    def _build_presets(self):
        self.presets_panel.Children.Clear()
        self._swatches = []
        presets = _PRESETS[self._target]
        for label, hex_value in presets:
            b = Button()
            b.Width = 36
            b.Height = 26
            b.Margin = Thickness(0, 0, 6, 6)
            b.Background = _brush(hex_value)
            b.BorderBrush = _PLAIN_BORDER
            b.BorderThickness = Thickness(1)
            b.ToolTip = "{0}  {1}".format(label, hex_value)
            b.Tag = hex_value
            b.Click += self._preset_click
            self._swatches.append(b)
            self.presets_panel.Children.Add(b)

    def _build_effects(self):
        win11 = dee_effects.is_windows_11()
        for key, label, tip in dee_effects.EFFECTS:
            box = CheckBox()
            box.Content = label
            box.ToolTip = tip
            box.Margin = Thickness(0, 0, 0, 8)
            box.IsChecked = bool(self._effects.get(key))
            if key == "titlebar" and not win11:
                # Windows 10: only shaped windows have a title bar of ours.
                box.Content = label + "  (Windows 10: with any shape but Standard)"
            box.Tag = key
            box.Checked += self._effect_toggled
            box.Unchecked += self._effect_toggled
            self._effect_boxes[key] = box
            self.effects_panel.Children.Add(box)
            if key == "glass":
                # the See-through slider sits right under Glass
                row = self.glass_level_row
                row.Parent.Children.Remove(row)
                self.effects_panel.Children.Add(row)
        self.glass_level_sl.Value = self._effects["glass_level"]
        self._sync_effect_controls()

    # ---- icon styles ----
    def _style_source(self, style):
        """The sample icon for a style card, in the current colours."""
        if not self._preview_sources:
            return None
        return dee_accent.resolve(self._preview_sources[0], self._accent,
                                  self._line, style)

    def _build_styles(self):
        for key, label, desc in dee_accent.STYLES:
            card = Border()
            card.Width = 98
            card.Margin = Thickness(4)
            card.Padding = Thickness(6, 8, 6, 6)
            card.CornerRadius = CornerRadius(6)
            card.Cursor = Cursors.Hand
            card.ToolTip = desc
            card.Tag = key
            card.SetResourceReference(Border.BackgroundProperty, "DeeSurface")
            stack = StackPanel()
            img = Image()
            img.Width = img.Height = _STYLE_CARD_ICON
            img.HorizontalAlignment = HorizontalAlignment.Center
            stack.Children.Add(img)
            name = TextBlock()
            name.Text = label
            name.Margin = Thickness(0, 6, 0, 0)
            name.HorizontalAlignment = HorizontalAlignment.Center
            stack.Children.Add(name)
            card.Child = stack
            card.MouseLeftButtonUp += self._style_click
            self._style_cards[key] = card
            self._style_images[key] = img
            self.styles_panel.Children.Add(card)
        self._refresh_style_images()
        self._sync_style_cards()

    def _refresh_style_images(self):
        """Redraws every style card in the colours picked right now."""
        for key, img in self._style_images.items():
            try:
                src = self._style_source(key)
                if src:
                    img.Source = _load_image(src)
            except Exception:
                pass

    def _sync_style_cards(self):
        for key, card in self._style_cards.items():
            picked = key == self._style
            card.BorderThickness = Thickness(2 if picked else 1)
            card.SetResourceReference(Border.BorderBrushProperty,
                                      "DeeOrange" if picked else "DeeBorder")
        for key, label, desc in dee_accent.STYLES:
            if key == self._style:
                self.style_desc_tb.Text = "{0}: {1}".format(label, desc)

    def _style_click(self, sender, args):
        if not self._ready:
            return
        self._style = sender.Tag
        self._sync_style_cards()
        self._refresh()

    # ---- shapes ----
    def _draw_shape(self, key):
        """A small window drawn in the shape: title strip, two lines of
        'content' and the website bar, clipped to the shape's outline."""
        pad = 6.0 if key == "soft" else 2.0
        rect = Rect(pad, pad, _CARD_W - 2 * pad, _CARD_H - 2 * pad)
        title_h = 12.0
        geom = dee_shapes.geometry(key, rect, pad + title_h)
        canvas = Canvas()
        canvas.Width, canvas.Height = _CARD_W, _CARD_H
        body = Path()
        body.Data = geom
        body.SetResourceReference(Path.FillProperty, "DeeWindowBg")
        if key == "soft":
            fx = DropShadowEffect()
            fx.BlurRadius = 8
            fx.ShadowDepth = 2
            fx.Direction = 270
            fx.Opacity = 0.35
            body.Effect = fx
        canvas.Children.Add(body)
        inside = Grid()
        inside.Width, inside.Height = _CARD_W, _CARD_H
        inside.Clip = geom
        parts = Canvas()

        def _bar(x, y, w, h, resource):
            r = Rectangle()
            r.Width, r.Height = w, h
            r.SetResourceReference(Rectangle.FillProperty, resource)
            Canvas.SetLeft(r, x)
            Canvas.SetTop(r, y)
            parts.Children.Add(r)

        _bar(0, 0, _CARD_W, pad + title_h, "DeeBorder")                  # title bar
        _bar(pad + 10, pad + title_h + 9, _CARD_W * 0.55, 4, "DeeBorder")
        _bar(pad + 10, pad + title_h + 18, _CARD_W * 0.38, 4, "DeeBorder")
        _bar(0, _CARD_H - pad - 6, _CARD_W, pad + 6, "DeeBar")            # website bar
        inside.Children.Add(parts)
        canvas.Children.Add(inside)
        edge = Path()
        edge.Data = geom
        edge.StrokeThickness = 1
        edge.SetResourceReference(Path.StrokeProperty, "DeeBorderStrong")
        canvas.Children.Add(edge)
        return canvas

    def _build_shapes(self):
        for key, label, desc in dee_shapes.SHAPES:
            card = Border()
            card.Width = 160
            card.Margin = Thickness(4)
            card.Padding = Thickness(6, 8, 6, 6)
            card.CornerRadius = CornerRadius(6)
            card.Cursor = Cursors.Hand
            card.ToolTip = desc
            card.Tag = key
            card.SetResourceReference(Border.BackgroundProperty, "DeeSurface")
            stack = StackPanel()
            picture = self._draw_shape(key)
            picture.HorizontalAlignment = HorizontalAlignment.Center
            stack.Children.Add(picture)
            name = TextBlock()
            name.Text = label
            name.Margin = Thickness(0, 6, 0, 0)
            name.HorizontalAlignment = HorizontalAlignment.Center
            stack.Children.Add(name)
            card.Child = stack
            card.MouseLeftButtonUp += self._shape_click
            self._shape_cards[key] = card
            self.shapes_panel.Children.Add(card)
        self._sync_shape_cards()

    def _sync_shape_cards(self):
        current = self._effects.get("shape", "standard")
        for key, card in self._shape_cards.items():
            picked = key == current
            card.BorderThickness = Thickness(2 if picked else 1)
            card.SetResourceReference(Border.BorderBrushProperty,
                                      "DeeOrange" if picked else "DeeBorder")
        for key, label, desc in dee_shapes.SHAPES:
            if key == current:
                self.shape_desc_tb.Text = "{0}: {1}".format(label, desc)

    def _shape_click(self, sender, args):
        if not self._ready:
            return
        self._effects["shape"] = sender.Tag
        self._sync_shape_cards()
        self._effects_changed()

    def theme_changed(self, sender, args):
        if not self._ready:
            return
        self._effects["theme"] = "dark" if self.theme_dark_rb.IsChecked else "light"
        self._effects_changed()

    def try_shape_click(self, sender, args):
        try:
            sample = _ShapeSample(self._effects.get("shape", "standard"),
                                  self._effects.get("theme") == "dark")
            try:
                sample.Owner = self
            except Exception:
                pass
            effects = dict(self._effects)
            dee_effects.when_ready(sample, lambda: dee_effects.apply(sample, effects))
            sample.Show()
        except Exception as err:
            self.status_tb.Text = "Couldn't open the sample: {0}".format(err)

    def _sync_effect_controls(self):
        self.glass_level_row.IsEnabled = bool(self._effects["glass"])
        if not dee_effects.is_windows_11():
            box = self._effect_boxes.get("titlebar")
            if box is not None:
                box.IsEnabled = self._effects.get("shape", "standard") != "standard"
        self.glass_level_tb.Text = "{0}%".format(int(self._effects["glass_level"]))

    def _effects_changed(self):
        self._sync_effect_controls()
        dee_effects.apply(self, self._effects)
        self._update_status()

    def _colours_changed(self):
        return ((self._accent, self._line, self._bar, self._style)
                != (self._saved_accent, self._saved_line, self._saved_bar,
                    self._saved_style))

    def _update_status(self):
        changed = self._colours_changed() or self._effects != self._saved_effects
        if changed:
            self.status_tb.Text = "Not applied yet - click Apply."
            if (self._effects.get("shape") != self._saved_effects.get("shape")
                    or self._effects.get("theme") != self._saved_effects.get("theme")):
                self.status_tb.Text += (" A new theme or shape shows on windows opened"
                                        " after Apply (Try it shows it now).")
        else:
            on = [label for key, label, _tip in dee_effects.EFFECTS if self._effects.get(key)]
            shape = dict((k, l) for k, l, _d in dee_shapes.SHAPES).get(
                self._effects.get("shape"), "Standard")
            style = dict((k, l) for k, l, _d in dee_accent.STYLES).get(
                self._saved_style, "Original")
            self.status_tb.Text = ("Current: {4} theme, accent {0}, lines {1}, bar {5}, "
                                   "icons {6}, shape {2}, effects: {3}").format(
                self._saved_accent, self._saved_line or "grey", shape, ", ".join(on) or "none",
                self._effects.get("theme", "light"), self._saved_bar or "accent", style)

    def _refresh(self):
        current = self._target_value()
        self.swatch_b.Background = _brush(current)
        self.name_tb.Text = _NAMES[self._target].get(current, "Custom")
        if self._target == "bar" and self._bar is None:
            self.name_tb.Text = "Same as accent"
        for b in self._swatches:
            picked = b.Tag == current
            b.BorderBrush = _SELECTED_BORDER if picked else _PLAIN_BORDER
            b.BorderThickness = Thickness(3 if picked else 1)
        self.reset_b.Content = {"accent": "Reset to Orange",
                                "line": "Reset to Grey",
                                "bar": "Reset to Accent"}[self._target]
        # This window's own website bar + the sample progress bar.
        dee_branding.set_window_accent(self, dee_accent.window_accent(self._accent),
                                       dee_accent.bar_accent(self._accent, self._bar))
        self.preview_panel.Children.Clear()
        for src in self._preview_sources:
            try:
                img = Image()
                img.Width = 40
                img.Height = 40
                img.Margin = Thickness(4)
                img.Source = _load_image(dee_accent.resolve(src, self._accent, self._line,
                                                            self._style))
                self.preview_panel.Children.Add(img)
            except Exception:
                pass
        self._refresh_style_images()
        self._update_status()

    # ---- events ----
    def _effect_toggled(self, sender, args):
        if not self._ready:
            return
        self._effects[sender.Tag] = bool(sender.IsChecked)
        self._effects_changed()

    def glass_level_changed(self, sender, args):
        if not self._ready:
            return
        self._effects["glass_level"] = int(round(self.glass_level_sl.Value))
        self._effects_changed()

    def effects_off_click(self, sender, args):
        for box in self._effect_boxes.values():
            box.IsChecked = False      # each fires _effect_toggled

    def target_changed(self, sender, args):
        if not self._ready:
            return
        if self.target_line_rb.IsChecked:
            self._target = "line"
        elif self.target_bar_rb.IsChecked:
            self._target = "bar"
        else:
            self._target = "accent"
        self._build_presets()
        self.hex_tb.Text = self._target_value()   # hex_changed refreshes
        self._refresh()

    def _preset_click(self, sender, args):
        self.hex_tb.Text = sender.Tag   # hex_changed does the rest

    def hex_changed(self, sender, args):
        if not self._ready:
            return
        value = dee_accent.normalize_hex(self.hex_tb.Text)
        if value is None:
            self.hex_error_tb.Text = "Use #RRGGBB"
            return
        self.hex_error_tb.Text = ""
        self._set_target_value(value)
        self._refresh()

    def more_click(self, sender, args):
        dlg = ColorDialog()
        dlg.FullOpen = True
        dlg.AnyColor = True
        try:
            r, g, b = dee_accent.hex_to_rgb(self._target_value())
            dlg.Color = System.Drawing.Color.FromArgb(r, g, b)
        except Exception:
            pass
        try:
            if dlg.ShowDialog() == DialogResult.OK:
                c = dlg.Color
                self.hex_tb.Text = "#{0:02X}{1:02X}{2:02X}".format(c.R, c.G, c.B)
        finally:
            dlg.Dispose()

    def reset_click(self, sender, args):
        self.hex_tb.Text = {"accent": dee_accent.ICON_ACCENT,
                            "line": dee_accent.ICON_INK,
                            "bar": self._bar_default()}[self._target]

    def apply_click(self, sender, args):
        self._saved_accent = dee_accent.save_accent(self._accent, self._line,
                                                    self._style, self._bar)
        self._saved_line = self._line
        self._saved_bar = self._bar
        self._saved_style = self._style
        count = 0
        if self._uiapp is not None:
            try:
                count = dee_accent.apply_to_ribbon(self._uiapp, self._saved_accent,
                                                   self._saved_line, self._saved_style)
            except Exception:
                count = 0
        try:
            dee_branding.repaint_open_windows(dee_accent.window_accent(),
                                              dee_accent.bar_accent())
        except Exception:
            pass
        self._saved_effects = dee_effects.save_effects(self._effects)
        for win in dee_branding.open_windows():
            if win is not self:
                dee_effects.apply(win, self._saved_effects)
        self._refresh()
        self.status_tb.Text = ("Saved. {0} ribbon buttons repainted, and every open "
                               "Dee window updated.".format(count))

    def close_click(self, sender, args):
        self.Close()


class _ShapeSample(dee_branding.DeeBrandedWindow):
    """The small window "Try it" opens, in the shape being picked."""

    def __init__(self, shape, dark=False):
        dee_branding.DeeBrandedWindow.__init__(self, _SAMPLE_XAML, shape=shape, dark=dark)
        label = dict((k, l) for k, l, _d in dee_shapes.SHAPES).get(shape, shape)
        self.sample_tb.Text = "Shape: {0}, {1} theme".format(label, "dark" if dark else "light")

    def close_click(self, sender, args):
        self.Close()


def show(uiapp=None):
    InterfaceDialog(uiapp).ShowDialog()
