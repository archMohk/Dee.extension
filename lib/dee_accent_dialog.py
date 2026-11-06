# -*- coding: utf-8 -*-
"""
dee_accent_dialog
The Icon Colours picker opened from DeeControl -> Accent Colour.

Two colours can be set, one at a time ("Colour the: Accent / Lines"):
  - Accent: the orange in every ribbon icon, the website bar under each
    tool window and the progress bars.
  - Lines:  the grey line work of the ribbon icons.
Each has a 44-colour palette, "More colours..." (the full Windows colour
picker) and a hex box, with a live preview on real ribbon icons, a sample
progress bar and this window's own website bar. Apply saves both
(lib/.dee_accent.json) and repaints the live ribbon and every open Dee
window straight away - no pyRevit reload. See lib/dee_accent.py.
"""
import os

import clr
clr.AddReference("PresentationCore")
clr.AddReference("System.Windows.Forms")
clr.AddReference("System.Drawing")
from System import Uri, UriKind  # noqa: E402
from System.Windows import Thickness  # noqa: E402
from System.Windows.Controls import Button, Image  # noqa: E402
from System.Windows.Media import SolidColorBrush, Color  # noqa: E402
from System.Windows.Media.Imaging import BitmapImage, BitmapCacheOption  # noqa: E402
from System.Windows.Forms import ColorDialog, DialogResult  # noqa: E402
import System.Drawing  # noqa: E402

import dee_accent
import dee_branding

_XAML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "DeeAccentDialog.xaml")
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


class AccentDialog(dee_branding.DeeBrandedWindow):
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
        changed = (self._accent, self._line) != (self._saved_accent, self._saved_line)
        self.status_tb.Text = ("Not applied yet - click Apply." if changed
                               else "Current: accent {0}, lines {1}".format(
                                   self._saved_accent, self._saved_line or "grey"))

    # ---- events ----
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
        self._refresh()
        self.status_tb.Text = ("Saved. {0} ribbon buttons repainted, and every open "
                               "Dee window updated.".format(count))

    def close_click(self, sender, args):
        self.Close()


def show(uiapp=None):
    AccentDialog(uiapp).ShowDialog()
