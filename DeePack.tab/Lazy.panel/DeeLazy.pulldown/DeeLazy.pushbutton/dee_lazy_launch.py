# -*- coding: utf-8 -*-
"""Entry points used by the per-module buttons in DeeLazy.pulldown.

run_module(tool_id, uiapp)   - open one module's window directly, no launcher
run_category(name, uiapp)    - open the card launcher filtered to one category

Both go through the same access check as the DeeLazy Home button, so the
dropdown adds no new gate. Module scripts put this folder on sys.path
first; from here `modules`, `controller` and `utils` resolve exactly as
they do for script.py.
"""
import os

from pyrevit import forms

import dee_telemetry

_HERE = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_HERE, "ui.xaml")


def _tool(tool_id):
    import modules
    for t in modules.REGISTERED_MODULES:
        if t.get("id") == tool_id:
            return t
    return None


def run_module(tool_id, uiapp):
    dee_telemetry.check_access("DeeLazy")
    tool = _tool(tool_id)
    if tool is None:
        forms.alert(u"DeeLazy module '{0}' is not registered in modules/__init__.py.".format(tool_id))
        return
    try:
        tool["launch"](uiapp)
    except Exception as e:
        forms.alert(u"Could not open '{0}':\n{1}".format(tool.get("title", tool_id), e))


def run_category(name, uiapp):
    dee_telemetry.check_access("DeeLazy")
    import controller
    window = controller.DeeLazyHomeWindow(_XAML_FILE, uiapp)
    try:
        window._select_category(name)
    except Exception:
        pass
    window.ShowDialog()
    if window.picked_tool_info is not None:
        try:
            window.picked_tool_info["launch"](uiapp)
        except Exception as e:
            forms.alert(u"Could not open '{0}':\n{1}".format(
                window.picked_tool_info.get("title", "Tool"), e))
