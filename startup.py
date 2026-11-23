# -*- coding: utf-8 -*-
"""
Extension startup script - pyRevit runs this automatically every time it
loads this extension (every Revit launch, and every manual Reload),
BEFORE it builds the ribbon from what is on disk.

1. Self-update on EVERY launch (lib/dee_auto_update.py)
   Git clones fast-forward from GitHub, Supabase-mirror installs pull the
   changed files from the mirror. It runs first and waits at most 8 s, so
   the new files are normally on disk before pyRevit builds the ribbon -
   new buttons show up in the same session. On a slow / missing network
   Revit carries on and the update finishes in the background for the
   next launch. A copy with local changes is never touched. Never raises.

2. Background watchers, each in its own try/except so a failure in one
   can never break the ribbon, Revit startup, or the others:
   lib/dee_prayer_service.py (prayer-time toasts) and
   lib/dee_broadcast_service.py (DeeCall admin broadcasts) on
   UIApplication.Idling - both do their network calls on a worker
   thread and only touch the UI from Idling; lib/dee_icon_service.py
   (DeeIcon's real-file-thumbnail branding) on DocumentOpened/Saved;
   lib/dee_schedule_engine.py (DeeSchedule's scheduled exports) on a
   timer plus an ExternalEvent.
"""
import os

# 1) Update FIRST, so a new release is on disk before pyRevit builds the
#    ribbon below. lib/dee_auto_update.py waits at most 8 seconds (a normal
#    check takes a second or two); on a slow network Revit carries on and the
#    update finishes in the background for next time. Never raises.
try:
    import dee_auto_update
    dee_auto_update.run_at_startup(os.path.dirname(os.path.abspath(__file__)), budget_seconds=8)
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

# DeeSchedule: runs the scheduled export tasks whose time has come, only
# while Revit is idle.
try:
    from pyrevit import HOST_APP
    import dee_schedule_engine
    dee_schedule_engine.start_watching(HOST_APP.uiapp)
except Exception:
    pass
