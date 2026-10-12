# -*- coding: utf-8 -*-
"""
DeeIcon (About)
Settings window for the automatic "save preview" feature backed by
lib/dee_icon_service.py - the on/off toggle and the custom logo are
configured here; the actual per-save work (export the active view,
composite the version badge + logo, write "<file>.DeeIcon.png" next to
the .rvt) lives entirely in that module and runs on its own, wired from
startup.py, with no further clicks needed once turned on.

Scope, decided up front (see the DeeIcon plan): a separate PNG file,
never Revit's own internal file-preview/thumbnail stream - there is no
documented API for reading or writing that, confirmed via research
before building this. One global enable toggle and one global logo for
the whole Dee.extension session/user, not per-project. Captures
whatever view is active at save time, not a specifically pinned
"thumbnail view".

"Generate Preview Now" runs the exact same
lib/dee_icon_service.generate_preview the automatic path uses, against
the CURRENTLY open document, so a user can see/tune the result without
needing to actually trigger a real Save.
"""
import os

import clr
clr.AddReference("System.Windows.Forms")
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
from System import Uri
from System.Windows import Visibility
from System.Windows.Media.Imaging import BitmapImage, BitmapCacheOption
from System.Windows.Forms import OpenFileDialog, DialogResult

import dee_branding
import dee_icon_service
import dee_telemetry
dee_telemetry.check_access("DeeIcon")

_TOOL = "DeeIcon"
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")

_IMAGE_FILE_FILTER = "Image Files (*.png;*.jpg;*.jpeg;*.bmp;*.gif)|*.png;*.jpg;*.jpeg;*.bmp;*.gif"


class DeeIconWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self._settings = dee_icon_service.load_settings()
        # Set in code, not XAML - enabled_cb has a wired Checked/Unchecked
        # handler, and setting IsChecked from XAML can fire it before the
        # rest of this window is ready (see feedback_wpf_xaml_early_event_fire).
        self.enabled_cb.IsChecked = bool(self._settings.get("enabled"))
        self._refresh_logo_preview()

    def _refresh_logo_preview(self):
        logo_path = self._settings.get("logo_path") or ""
        if logo_path and os.path.isfile(logo_path):
            try:
                bmp = BitmapImage()
                bmp.BeginInit()
                bmp.CacheOption = BitmapCacheOption.OnLoad
                bmp.UriSource = Uri(logo_path)
                bmp.EndInit()
                bmp.Freeze()
                self.logo_preview_img.Source = bmp
                self.logo_preview_border.Visibility = Visibility.Visible
                self.remove_logo_b.Visibility = Visibility.Visible
                self.logo_status_tb.Text = os.path.basename(logo_path)
                return
            except Exception:
                pass
        self.logo_preview_img.Source = None
        self.logo_preview_border.Visibility = Visibility.Collapsed
        self.remove_logo_b.Visibility = Visibility.Collapsed
        self.logo_status_tb.Text = "No logo selected."

    def enabled_changed(self, sender, args):
        self._settings["enabled"] = bool(self.enabled_cb.IsChecked)
        dee_icon_service.save_settings(self._settings)

    def pick_logo_click(self, sender, args):
        dlg = OpenFileDialog()
        dlg.Title = "Pick a Logo Image"
        dlg.Filter = _IMAGE_FILE_FILTER
        if dlg.ShowDialog() != DialogResult.OK:
            return
        self._settings["logo_path"] = dlg.FileName
        dee_icon_service.save_settings(self._settings)
        self._refresh_logo_preview()

    def remove_logo_click(self, sender, args):
        self._settings["logo_path"] = ""
        dee_icon_service.save_settings(self._settings)
        self._refresh_logo_preview()

    def generate_now_click(self, sender, args):
        try:
            doc = __revit__.ActiveUIDocument.Document if __revit__.ActiveUIDocument else None
        except Exception:
            doc = None
        if doc is None:
            self.generate_status_tb.Text = "Open a Revit project first."
            return
        ok, result = dee_icon_service.generate_preview(doc, self._settings)
        if ok:
            self.generate_status_tb.Text = "Saved: {0}".format(result)
        else:
            self.generate_status_tb.Text = "Could not generate a preview - {0}".format(result)

    def close_click(self, sender, args):
        self.Close()


def main():
    window = DeeIconWindow(_XAML_FILE)
    window.ShowDialog()


main()
