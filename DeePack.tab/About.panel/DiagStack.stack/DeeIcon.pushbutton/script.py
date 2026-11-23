# -*- coding: utf-8 -*-
"""
DeeIcon (About)
Settings window for the real-file-thumbnail branding feature backed by
lib/dee_icon_service.py - the on/off toggle and the custom logo are
configured here; the actual work (a dedicated hidden view holding a
version-badge TextNote + logo ImageInstance, pointed at via Revit's own
Document.DocumentPreviewSettings.PreviewViewId so Revit's native save
process bakes it into the file's REAL embedded thumbnail - the one
Explorer and Revit's own Recent Files screen both show) lives entirely
in that module and runs on its own, automatically, whenever a project
is opened or saved - wired from startup.py, no further clicks needed
once turned on. See dee_icon_service.py's own docstring for why this
replaced an earlier separate-PNG-file version, and why it does NOT
touch the .rvt file's raw bytes.

One global On/Off switch (also in DeeControl -> Running Tools) and one
global logo for the whole Dee.extension session/user, not per-project.
Off stops the automatic branding at once and changes no model; "Remove
From This Document" takes it out of the open one.

"Apply To This Document" runs the exact same
lib/dee_icon_service.apply_branding the automatic path uses, against
the CURRENTLY open document, so a user can see the branding view appear
and confirm it took effect without needing to close/reopen or Save
first.
"""
import os

import clr
clr.AddReference("WindowsBase")
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
clr.AddReference("System.Windows.Forms")
from System.IO import File, MemoryStream
from System.Windows import Visibility
from System.Windows.Media.Imaging import BitmapImage, BitmapCacheOption
from System.Windows.Forms import OpenFileDialog, DialogResult

from pyrevit import forms

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
        self._refresh_power()
        self._refresh_logo_preview()

    def _refresh_power(self):
        on = bool(self._settings.get("enabled"))
        self.power_tb.Text = "DeeIcon is ON" if on else "DeeIcon is OFF"
        self.power_note_tb.Text = (
            "Every model you open or save is branded." if on else
            "No model is changed. Ones already branded stay so until removed below.")
        self.power_b.Content = "Turn Off" if on else "Turn On"
        self.generate_now_b.IsEnabled = on
        self.remove_now_b.IsEnabled = not on

    def _document(self):
        try:
            return __revit__.ActiveUIDocument.Document if __revit__.ActiveUIDocument else None
        except Exception:
            return None

    def _refresh_logo_preview(self):
        logo_path = self._settings.get("logo_path") or ""
        if logo_path and os.path.isfile(logo_path):
            try:
                bmp = BitmapImage()
                bmp.BeginInit()
                bmp.CacheOption = BitmapCacheOption.OnLoad
                bmp.StreamSource = MemoryStream(File.ReadAllBytes(logo_path))
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

    def power_click(self, sender, args):
        self._settings = dee_icon_service.set_enabled(not self._settings.get("enabled"))
        self._refresh_power()
        self.generate_status_tb.Text = ""

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

    def apply_now_click(self, sender, args):
        doc = self._document()
        if doc is None:
            self.generate_status_tb.Text = "Open a Revit project first."
            return
        ok, result = dee_icon_service.apply_branding(doc, self._settings)
        self.generate_status_tb.Text = result if ok else "Could not apply - {0}".format(result)

    def remove_now_click(self, sender, args):
        doc = self._document()
        if doc is None:
            self.generate_status_tb.Text = "Open a Revit project first."
            return
        if dee_icon_service.is_branded(doc) and not forms.alert(
                "Remove DeeIcon from this document?\n\n{0}\n\nThis deletes the view "
                "'DeeIcon - Do Not Delete' and its logo. Nothing else is touched.".format(doc.Title),
                title="DeeIcon", yes=True, no=True):
            return
        ok, result = dee_icon_service.remove_branding(doc)
        self.generate_status_tb.Text = result if ok else "Could not remove - {0}".format(result)

    def close_click(self, sender, args):
        self.Close()


def main():
    window = DeeIconWindow(_XAML_FILE)
    window.ShowDialog()


main()
