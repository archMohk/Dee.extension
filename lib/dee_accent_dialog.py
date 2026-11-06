# -*- coding: utf-8 -*-
"""
dee_accent_dialog
The Accent Colour picker opened from DeeControl. Presets, a custom hex
box, a live preview on real ribbon icons, and Apply - which saves the
colour (lib/.dee_accent.json) and repaints the live ribbon straight away,
no pyRevit reload needed. See lib/dee_accent.py for how it works.
"""
import os

import clr
clr.AddReference("PresentationCore")
from System import Uri, UriKind  # noqa: E402
from System.Windows import Thickness  # noqa: E402
from System.Windows.Controls import Button, Image  # noqa: E402
from System.Windows.Media import SolidColorBrush, Color  # noqa: E402
from System.Windows.Media.Imaging import BitmapImage, BitmapCacheOption  # noqa: E402

import dee_accent
import dee_branding

_XAML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "DeeAccentDialog.xaml")
_PREVIEW_BUTTONS = ["DeePublisher", "DeeSuperLINK", "Dee3DView", "DeeCleaner",
                    "DeeLazy", "DeeForce", "DeeIcon", "DeeMAPLink"]


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
        self._saved = dee_accent.load_accent()
        self._current = self._saved
        bundles = dee_accent._bundle_dirs()
        self._preview_sources = [os.path.join(bundles[n], "icon.png")
                                 for n in _PREVIEW_BUTTONS if n in bundles]
        self._build_presets()
        self._ready = True
        self.hex_tb.Text = self._current
        self._refresh()

    # ---- building ----
    def _build_presets(self):
        for label, hex_value in dee_accent.PRESETS:
            b = Button()
            b.Width = 48
            b.Height = 28
            b.Margin = Thickness(0, 0, 6, 6)
            b.Background = _brush(hex_value)
            b.ToolTip = "{0}  {1}".format(label, hex_value)
            b.Tag = hex_value
            b.Click += self._preset_click
            self.presets_panel.Children.Add(b)

    def _refresh(self):
        self.swatch_b.Background = _brush(self._current)
        self.preview_panel.Children.Clear()
        for src in self._preview_sources:
            try:
                img = Image()
                img.Width = 40
                img.Height = 40
                img.Margin = Thickness(4)
                img.Source = _load_image(dee_accent.resolve(src, self._current))
                self.preview_panel.Children.Add(img)
            except Exception:
                pass
        changed = self._current != self._saved
        self.status_tb.Text = ("Not applied yet - click Apply." if changed
                               else "Current colour: {0}".format(self._saved))

    # ---- events ----
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
        self._current = value
        self._refresh()

    def reset_click(self, sender, args):
        self.hex_tb.Text = dee_accent.ICON_ACCENT

    def apply_click(self, sender, args):
        self._saved = dee_accent.save_accent(self._current)
        count = 0
        if self._uiapp is not None:
            try:
                count = dee_accent.apply_to_ribbon(self._uiapp, self._saved)
            except Exception:
                count = 0
        self._refresh()
        self.status_tb.Text = ("Saved {0}. {1} ribbon buttons repainted; tool windows "
                               "pick it up the next time they open.".format(self._saved, count))

    def close_click(self, sender, args):
        self.Close()


def show(uiapp=None):
    AccentDialog(uiapp).ShowDialog()
