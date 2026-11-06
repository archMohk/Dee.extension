# -*- coding: utf-8 -*-
"""
dee_interface_dialog
The window behind About -> Interface. Two tabs:

Colours - two colours, one at a time ("Colour the: Accent / Lines"):
  - Accent: the orange in every ribbon icon, the website bar under each
    tool window and the progress bars.
  - Lines:  the grey line work of the ribbon icons.
  Each has a 44-colour palette, "More colours..." (the full Windows
  colour picker) and a hex box.
Effects - any combination of glass, accent outline, accent top line,
  fade in, rounded windows and accent title bar (lib/dee_effects.py).

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
from System.Windows import Thickness  # noqa: E402
from System.Windows.Controls import Button, Image, CheckBox  # noqa: E402
from System.Windows.Media import SolidColorBrush, Color  # noqa: E402
from System.Windows.Media.Imaging import BitmapImage, BitmapCacheOption  # noqa: E402
from System.Windows.Forms import ColorDialog, DialogResult  # noqa: E402
import System.Drawing  # noqa: E402

import dee_accent
import dee_branding
import dee_effects

_XAML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "DeeInterface.xaml")
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
_NAMES = {
    "accent": dict((h, n) for n, h in _ACCENT_PRESETS),
    "line": dict((h, n) for n, h in _LINE_PRESETS),
}


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
        self._accent = self._saved_accent
        self._line = self._saved_line
        self._target = "accent"
        bundles = dee_accent._bundle_dirs()
        self._preview_sources = [os.path.join(bundles[n], "icon.png")
                                 for n in _PREVIEW_BUTTONS if n in bundles]
        self._swatches = []
        self._build_presets()
        self._saved_effects = dee_effects.load_effects()
        self._effects = dict(self._saved_effects)
        self._effect_boxes = {}
        self._build_effects()
        dee_effects.when_ready(self, lambda: dee_effects.apply(self, self._effects))
        self._ready = True
        self.hex_tb.Text = self._target_value()
        self._refresh()

    # ---- the colour being edited ----
    def _target_value(self):
        if self._target == "accent":
            return self._accent
        return self._line or dee_accent.ICON_INK

    def _set_target_value(self, value):
        if self._target == "accent":
            self._accent = value
        else:
            # Picking the default grey means "no line colour".
            self._line = None if value == dee_accent.ICON_INK else value

    # ---- building ----
    def _build_presets(self):
        self.presets_panel.Children.Clear()
        self._swatches = []
        presets = _ACCENT_PRESETS if self._target == "accent" else _LINE_PRESETS
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
                # Windows 10: only rounded windows have a title bar of ours.
                box.Content = label + "  (Windows 10: with Rounded windows)"
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

    def _sync_effect_controls(self):
        self.glass_level_row.IsEnabled = bool(self._effects["glass"])
        if not dee_effects.is_windows_11():
            box = self._effect_boxes.get("titlebar")
            if box is not None:
                box.IsEnabled = bool(self._effects.get("rounded"))
        self.glass_level_tb.Text = "{0}%".format(int(self._effects["glass_level"]))

    def _effects_changed(self):
        self._sync_effect_controls()
        dee_effects.apply(self, self._effects)
        self._update_status()

    def _update_status(self):
        changed = ((self._accent, self._line) != (self._saved_accent, self._saved_line)
                   or self._effects != self._saved_effects)
        if changed:
            self.status_tb.Text = "Not applied yet - click Apply."
            if self._effects.get("rounded") != self._saved_effects.get("rounded"):
                self.status_tb.Text += (" Rounded windows changes the window frame, so it "
                                        "shows on windows opened after Apply.")
        else:
            on = [label for key, label, _tip in dee_effects.EFFECTS if self._effects.get(key)]
            self.status_tb.Text = "Current: accent {0}, lines {1}, effects: {2}".format(
                self._saved_accent, self._saved_line or "grey", ", ".join(on) or "none")

    def _refresh(self):
        current = self._target_value()
        self.swatch_b.Background = _brush(current)
        self.name_tb.Text = _NAMES[self._target].get(current, "Custom")
        for b in self._swatches:
            picked = b.Tag == current
            b.BorderBrush = _SELECTED_BORDER if picked else _PLAIN_BORDER
            b.BorderThickness = Thickness(3 if picked else 1)
        self.reset_b.Content = ("Reset to Orange" if self._target == "accent"
                                else "Reset to Grey")
        # This window's own website bar + the sample progress bar.
        window_hex = (dee_accent.BRAND_ORANGE if dee_accent.is_default(self._accent)
                      else self._accent)
        dee_branding.set_window_accent(self, window_hex)
        self.preview_panel.Children.Clear()
        for src in self._preview_sources:
            try:
                img = Image()
                img.Width = 40
                img.Height = 40
                img.Margin = Thickness(4)
                img.Source = _load_image(dee_accent.resolve(src, self._accent, self._line))
                self.preview_panel.Children.Add(img)
            except Exception:
                pass
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
        self._target = "line" if self.target_line_rb.IsChecked else "accent"
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
        self.hex_tb.Text = (dee_accent.ICON_ACCENT if self._target == "accent"
                            else dee_accent.ICON_INK)

    def apply_click(self, sender, args):
        self._saved_accent = dee_accent.save_accent(self._accent, self._line)
        self._saved_line = self._line
        count = 0
        if self._uiapp is not None:
            try:
                count = dee_accent.apply_to_ribbon(self._uiapp, self._saved_accent,
                                                   self._saved_line)
            except Exception:
                count = 0
        try:
            dee_branding.repaint_open_windows(dee_accent.window_accent())
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


def show(uiapp=None):
    InterfaceDialog(uiapp).ShowDialog()
