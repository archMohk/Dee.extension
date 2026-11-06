# -*- coding: utf-8 -*-
"""
Extension startup script - pyRevit runs this automatically every time it
loads this extension (every Revit launch, and every manual Reload),
BEFORE it builds the ribbon from what is on disk.

1. Self-update for 'pyrevit extend' installs
   For colleagues who installed the public GitHub copy, this runs
   'pyrevit extensions update --all' so already-pulled script.py changes
   take effect in this same session. New/renamed buttons still need one
   more Reload (pyRevit limitation).

   It used to run SYNCHRONOUSLY and wait up to 15 s on EVERY launch -
   the single biggest startup cost of the extension, paid even when
   nothing changed. Now it runs on a background thread (Revit never
   waits) and at most once per calendar day per PC (stamp in
   lib/.deew_settings via deew_settings). On the developer's own PC
   (plain OneDrive folder, not a 'pyrevit extend' clone) the CLI call is
   a harmless no-op either way. Never raises, never blocks.

2. Background watchers, each in its own try/except so a failure in one
   can never break the ribbon, Revit startup, or the others:
   lib/dee_prayer_service.py (prayer-time toasts) and
   lib/dee_broadcast_service.py (DeeCall admin broadcasts) on
   UIApplication.Idling - both do their network calls on a worker
   thread and only touch the UI from Idling; lib/dee_icon_service.py
   (DeeIcon's real-file-thumbnail branding) on DocumentOpened/Saved.
"""
import datetime
import threading

import clr
clr.AddReference("System")
from System.Diagnostics import Process, ProcessStartInfo

_UPDATE_TOOL = "DeeStartupUpdate"


def _already_pulled_today():
    try:
        import deew_settings
        data = deew_settings.load(_UPDATE_TOOL, {"last_pull_date": ""})
        today = datetime.date.today().isoformat()
        if data.get("last_pull_date") == today:
            return True
        data["last_pull_date"] = today
        deew_settings.save(_UPDATE_TOOL, data)
    except Exception:
        pass
    return False


def _pull_in_background():
    try:
        psi = ProcessStartInfo()
        psi.FileName = "pyrevit"
        psi.Arguments = "extensions update --all"
        psi.UseShellExecute = False
        psi.RedirectStandardOutput = True
        psi.RedirectStandardError = True
        psi.CreateNoWindow = True
        proc = Process.Start(psi)
        if not proc.WaitForExit(120000):
            proc.Kill()
    except Exception:
        pass


try:
    if not _already_pulled_today():
        _t = threading.Thread(target=_pull_in_background)
        _t.daemon = True
        _t.start()
except Exception:
    pass

# Before pyRevit builds the ribbon: drop bundle folders an update emptied
# (a moved/removed button leaves its folder behind, and pyRevit still shows
# it - e.g. DeeAccount lingering on the Cloud panel after moving to About).
try:
    import os
    import dee_update_source
    dee_update_source.prune_empty_bundles(os.path.dirname(os.path.abspath(__file__)))
except Exception:
    pass

# Before pyRevit builds the ribbon: drop bundle folders an update emptied
# (a moved/removed button leaves its folder behind, and pyRevit still shows
# it - e.g. DeeAccount lingering on the Cloud panel after moving to About).
try:
    import os
    import dee_update_source
    dee_update_source.prune_empty_bundles(os.path.dirname(os.path.abspath(__file__)))
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

# The user's accent colour on the ribbon icons (DeeControl -> Accent
# Colour). Waits for Revit's first Idling, after the ribbon is built.
try:
    from pyrevit import HOST_APP
    import dee_accent
    dee_accent.apply_on_startup(HOST_APP.uiapp)
except Exception:
    pass

try:
    from pyrevit import HOST_APP
    import dee_icon_service
    dee_icon_service.start_watching(HOST_APP.uiapp)
except Exception:
    pass
