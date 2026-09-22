# -*- coding: utf-8 -*-
"""
Extension startup script - pyRevit runs this automatically every time it
loads this extension (every Revit launch, and every manual Reload),
BEFORE it builds the ribbon from what is on disk.

For installs done via 'pyrevit extend' (colleagues who installed the
public GitHub copy), this silently runs 'pyrevit extensions update
--all' to git-pull the latest code first, so already-pulled script.py
changes take effect immediately in this same session - no manual
"Update" click needed. New/renamed buttons, panels, or tabs are
structural changes and still need one more Reload (or Revit restart)
after the pull to actually appear - same pyRevit limitation as always.

On the developer's own PC (plain OneDrive-synced folder, not a
'pyrevit extend' clone) this is a harmless no-op. Runs silently and
must never raise or hang - a failed/offline pull should never block
Revit startup.

Also starts the prayer-time notification watcher (lib/
dee_prayer_service.py) - subscribes to UIApplication.Idling so it can
run in the background for the rest of the Revit session with no button
click at all. Wrapped in its own try/except, same as the update check
above - a failure here must never break the ribbon or Revit startup.
See that module's own docstring for the full mechanism and its known,
still-to-be-live-tested risk (whether this subscription genuinely
survives a whole session).
"""
import clr
clr.AddReference("System")
from System.Diagnostics import Process, ProcessStartInfo

try:
    psi = ProcessStartInfo()
    psi.FileName = "pyrevit"
    psi.Arguments = "extensions update --all"
    psi.UseShellExecute = False
    psi.RedirectStandardOutput = True
    psi.RedirectStandardError = True
    psi.CreateNoWindow = True
    proc = Process.Start(psi)
    if not proc.WaitForExit(15000):
        proc.Kill()
except Exception:
    pass

try:
    from pyrevit import HOST_APP
    import dee_prayer_service
    dee_prayer_service.start_watching(HOST_APP.uiapp)
except Exception:
    pass
