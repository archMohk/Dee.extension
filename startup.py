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

Also starts two background watchers, each its own UIApplication.Idling
subscription so they run for the rest of the Revit session with no
button click at all: lib/dee_prayer_service.py (prayer-time toasts) and
lib/dee_broadcast_service.py (DeeCall admin broadcasts). Each wrapped in
its own try/except, same as the update check above - a failure in
either must never break the ribbon, Revit startup, or the other
watcher. See dee_prayer_service's own docstring for the full mechanism
and its known, still-to-be-live-tested risk (whether a subscription
made here genuinely survives a whole session).
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

try:
    from pyrevit import HOST_APP
    import dee_broadcast_service
    dee_broadcast_service.start_watching(HOST_APP.uiapp)
except Exception:
    pass
