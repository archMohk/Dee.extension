# -*- coding: utf-8 -*-
"""
DeeSheetLinks (Masterplan)
Placeholder - frozen via bundle.yaml's never-true `context: rule:
"(zero-doc)&!(zero-doc)"` (the exact pattern lib/dee_control_panel_
service.py uses to grey out a button: a logical contradiction Revit's
availability check can never satisfy, so the button stays visible and
greyed rather than hidden, keeping its tooltip readable).

Explicitly asked to be frozen until DeeLinkDist is finished and
confirmed working live - this script can never actually run while that
context rule stands, so its own body is not load-bearing; kept as a
plain alert only so the file is a valid, runnable script the moment
the freeze is lifted (deleting the context: block in bundle.yaml) and
real work replaces this body.
"""
from pyrevit import forms

forms.alert("DeeSheetLinks is not built yet - it is frozen until DeeLinkDist "
           "is finished.", title="DeeSheetLinks")
