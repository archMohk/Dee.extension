# -*- coding: utf-8 -*-
"""
DeeLazy - a growing collection of bulk productivity tools.
This entry point only opens the card-based launcher (controller.py) -
each tool owns its own window/logic under modules/, see
modules/__init__.py for how to add a new one.

The picked tool is launched HERE, after window.ShowDialog() has
returned - never from inside the still-open launcher's own click
handler. Live feedback asked for the launcher to close the instant a
card is picked (rather than sitting open behind the tool until it
closes); closing first also matches this codebase's hard rule against
opening a second WPF dialog from inside an already-open one (see
DeeMono/Dee3DView's own hub for the same close-first-dispatch-after
pattern) - the previous version called launch() directly from the
click handler while this window was still open, which happened to work
but was exactly that pattern.
"""
import os

from pyrevit import forms

import controller
import dee_telemetry
dee_telemetry.check_access("DeeLazy")


_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")

uiapp = __revit__

window = controller.DeeLazyHomeWindow(_XAML_FILE, uiapp)
window.ShowDialog()

if window.picked_tool_info is not None:
    try:
        window.picked_tool_info["launch"](uiapp)
    except Exception as e:
        forms.alert(u"Could not open '{0}':\n{1}".format(
            window.picked_tool_info.get("title", "Tool"), e))
