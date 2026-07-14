# -*- coding: utf-8 -*-
"""
Update
Reloads pyRevit (re-scans every extension from disk), using the exact
same API pyRevit's own built-in Reload button uses
(pyrevit.loader.sessionmgr.reload_pyrevit) - see
pyRevitCore.extension/pyRevit.tab/pyRevit.panel/tools.stack/Reload.pushbutton
for reference.

This only re-reads whatever is ALREADY on disk - since the tools live in
a OneDrive-synced folder (junctioned into this extension's normal
location), this button does NOT force OneDrive itself to sync any
faster. It also does not rename an already-created ribbon TAB (Revit
only builds tab titles once at startup) - that specific kind of change
still needs a full Revit restart, same as pyRevit's own Reload.
"""
from pyrevit import forms
from pyrevit.loader import sessionmgr

if forms.alert(
        "Reload pyRevit now to pick up any tool updates already synced to this PC?\n\n"
        "This re-scans every tool from disk - it will NOT make OneDrive sync any "
        "faster, and a renamed ribbon TAB specifically still needs a full Revit "
        "restart to show up (new/changed buttons and scripts do not).",
        title="Dee Update", yes=True, no=True):
    sessionmgr.reload_pyrevit()
