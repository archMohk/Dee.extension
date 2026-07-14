# -*- coding: utf-8 -*-
"""
Update
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
from pyrevit import forms
from pyrevit.loader import sessionmgr
import clr
clr.AddReference("System")
from System.Diagnostics import Process, ProcessStartInfo


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


if forms.alert(
        "Update Dee tools now?\n\n"
        "This pulls the latest version from GitHub (if this install was set "
        "up with 'pyrevit extend'), then reloads pyRevit to pick up the "
        "changes. A renamed ribbon TAB specifically still needs a full "
        "Revit restart to show up.",
        title="Dee Update", yes=True, no=True):
    ok, output = _run_pyrevit_update()
    if not ok and output:
        forms.alert(
            "Could not pull the latest update automatically:\n\n{0}\n\n"
            "Reloading anyway with whatever is already on this PC."
            .format(output),
            title="Dee Update")
    sessionmgr.reload_pyrevit()
