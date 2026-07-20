# -*- coding: utf-8 -*-
"""About DeePack - branded window (logo + text + clickable website), replacing
the old plain forms.alert popup. The logo is the extension's own root
icon.png (four folders up from this button)."""
import os
import webbrowser

import clr
clr.AddReference("WindowsBase")
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
from System import Uri
from System.Windows.Media.Imaging import BitmapImage

from pyrevit import forms

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")
_LOGO_FILE = os.path.abspath(os.path.join(_THIS_DIR, "..", "..", "..", "..", "icon.png"))
_WEBSITE_URL = "https://www.archmkd.com"


class AboutWindow(forms.WPFWindow):
    def __init__(self, xaml_file):
        forms.WPFWindow.__init__(self, xaml_file)
        try:
            if os.path.exists(_LOGO_FILE):
                bmp = BitmapImage()
                bmp.BeginInit()
                bmp.UriSource = Uri(_LOGO_FILE)
                bmp.EndInit()
                self.logo_img.Source = bmp
        except Exception:
            pass

    def website_click(self, sender, args):
        webbrowser.open(_WEBSITE_URL)

    def close_click(self, sender, args):
        self.Close()


AboutWindow(_XAML_FILE).ShowDialog()
