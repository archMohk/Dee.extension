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
import dee_branding
from pyrevit.loader import sessionmgr
import dee_telemetry
dee_telemetry.check_access("Update")


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


class _SafeProgress(object):
    """forms.ProgressBar tries to set Window.TaskbarItemInfo on its host
    window - a genuine WPF-level bug (not this extension's code):
    Window.TaskbarItemInfo throws NotImplementedException whenever the
    underlying ITaskbarList::HrInit COM call fails, which is documented
    to happen specifically under Remote Desktop/Terminal Services or a
    custom shell without a taskbar (live-confirmed in DeeSheetLinks).

    Wraps the real forms.ProgressBar and falls back to running with NO
    progress UI at all if entering it fails, so the tool degrades
    gracefully under RDP instead of crashing - everyone else still gets
    the real progress bar exactly as before. `pb.update_progress(...)`/
    `pb.cancelled` are safe no-ops in the fallback case, so callers never
    need an extra branch."""
    def __init__(self, **kwargs):
        self._kwargs = kwargs
        self._real = None

    def __enter__(self):
        try:
            self._real = forms.ProgressBar(**self._kwargs)
            return self._real.__enter__()
        except Exception:
            self._real = None
            return self

    def __exit__(self, exc_type, exc_value, tb):
        if self._real is not None:
            return self._real.__exit__(exc_type, exc_value, tb)
        return False

    @property
    def cancelled(self):
        return False

    def update_progress(self, i, total):
        pass


class UpdateWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
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
    with _SafeProgress(title="Dee Update - updating and reloading...", indeterminate=True):
        ok, output = _run_pyrevit_update()
        if not ok and output:
            forms.alert(
                "Could not pull the latest update automatically:\n\n{0}\n\n"
                "Reloading anyway with whatever is already on this PC."
                .format(output),
                title="Dee Update")
        sessionmgr.reload_pyrevit()

# Reload rebuilds the ribbon in place, but some changes - a new panel,
# a moved button, ComboBox member lists - have repeatedly needed a
# genuine full Revit restart to actually show correctly (found live,
# same-day, several times this session), not just this reload. Rather
# than let every updated user rediscover that the hard way, tell them
# up front every time.
if window.confirmed:
    forms.alert(
        "Update complete.\n\n"
        "Please CLOSE Revit completely and reopen it now - some changes "
        "only take full effect after a real restart, not just a reload.",
        title="Dee Update - Restart Revit")
