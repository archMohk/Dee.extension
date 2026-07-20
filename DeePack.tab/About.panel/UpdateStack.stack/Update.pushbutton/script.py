# -*- coding: utf-8 -*-
"""
Update - branded window (logo + text + website), replacing the old plain
forms.alert confirm dialog. Underlying behavior is unchanged:

For installs done via 'pyrevit extend' (colleagues who installed the
public GitHub-distributed copy): first runs 'pyrevit extensions update
--all' as an external process to git-pull the latest changes for every
git-based extension registered on this PC. --all is used instead of a
specific name so this works regardless of what name someone chose at
install time.

Then reloads pyRevit (re-scans every extension from disk), using the
exact same API pyRevit's own built-in Reload button uses
(pyrevit.loader.sessionmgr.reload_pyrevit) - see
pyRevitCore.extension/pyRevit.tab/pyRevit.panel/tools.stack/Reload.pushbutton
for reference.

On the developer's own PC (this tool lives in a plain OneDrive-synced
folder, not a 'pyrevit extend' clone) the update step is a harmless
no-op - there is nothing registered for it to find. It also does not
rename an already-created ribbon TAB (Revit only builds tab titles once
at startup) - that specific kind of change still needs a full Revit
restart, same as pyRevit's own Reload.
"""
import os
import webbrowser

import clr
clr.AddReference("WindowsBase")
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
clr.AddReference("System")
from System import Uri
from System.Windows.Media.Imaging import BitmapImage
from System.Diagnostics import Process, ProcessStartInfo

from pyrevit import forms
from pyrevit.loader import sessionmgr

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")
_LOGO_FILE = os.path.abspath(os.path.join(_THIS_DIR, "..", "..", "..", "..", "icon.png"))
_WEBSITE_URL = "https://www.archmkd.com"


def _run_pyrevit_update():
    psi = ProcessStartInfo()
    psi.FileName = "pyrevit"
    psi.Arguments = "extensions update --all"
    psi.UseShellExecute = False
    psi.RedirectStandardOutput = True
    psi.RedirectStandardError = True
    psi.CreateNoWindow = True
    try:
        proc = Process.Start(psi)
        out = proc.StandardOutput.ReadToEnd()
        err = proc.StandardError.ReadToEnd()
        proc.WaitForExit()
        return proc.ExitCode == 0, (out + err).strip()
    except Exception as ex:
        return False, str(ex)


class UpdateWindow(forms.WPFWindow):
    def __init__(self, xaml_file):
        forms.WPFWindow.__init__(self, xaml_file)
        self.confirmed = False
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

    def update_click(self, sender, args):
        self.confirmed = True
        self.Close()

    def cancel_click(self, sender, args):
        self.confirmed = False
        self.Close()


window = UpdateWindow(_XAML_FILE)
window.ShowDialog()

if window.confirmed:
    ok, output = _run_pyrevit_update()
    if not ok and output:
        forms.alert(
            "Could not pull the latest update automatically:\n\n{0}\n\n"
            "Reloading anyway with whatever is already on this PC."
            .format(output),
            title="Dee Update")
    sessionmgr.reload_pyrevit()
