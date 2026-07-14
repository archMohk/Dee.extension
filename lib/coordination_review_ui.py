# -*- coding: utf-8 -*-
"""UI Automation to open Revit's Coordination Review dialog.

Coordination Review has no public Revit API at all - not for reading
status/differences, and not for the Accept/Reject/Postpone actions. This
module ONLY gets Revit to the point of showing that dialog; reviewing
status, changes, and taking actions is left entirely to the user in
Revit's own UI - automating those decisions would risk silently applying
the wrong action to a real model with no way to verify success.

Reuses the same foreground-window / on-screen-preferring-search approach
already proven out for the Publish Settings dialog (see
publish_settings_ui.py for the fuller history of why each piece exists).
"""
import time

import clr
clr.AddReference("UIAutomationClient")
clr.AddReference("UIAutomationTypes")
from System.Windows.Automation import (
    AutomationElement, Condition, PropertyCondition, TreeScope,
    ControlType, InvokePattern, SelectionItemPattern
)

try:
    from System.Windows.Automation import LegacyIAccessiblePattern
except ImportError:
    LegacyIAccessiblePattern = None

try:
    import ctypes
    _user32 = ctypes.windll.user32
except Exception:
    _user32 = None


def _bring_window_to_foreground(hwnd):
    if _user32 is None:
        return
    try:
        _user32.SetForegroundWindow(int(hwnd))
        time.sleep(0.3)
    except Exception:
        pass


def _root():
    return AutomationElement.RootElement


def _is_onscreen(element):
    try:
        return not element.Current.IsOffscreen
    except Exception:
        return True


def _find_preferring_onscreen(parent, name_contains, control_type=None, timeout=10):
    """Prefers a visible match over an off-screen one - Revit's ribbon
    appears to keep inactive tabs' controls in the automation tree."""
    deadline = time.time() + timeout
    name_l = name_contains.lower() if name_contains else None
    cond = (PropertyCondition(AutomationElement.ControlTypeProperty, control_type)
            if control_type is not None else Condition.TrueCondition)
    while time.time() < deadline:
        try:
            found = parent.FindAll(TreeScope.Descendants, cond)
        except Exception:
            found = None
        if found:
            offscreen_fallback = None
            for el in found:
                try:
                    nm = el.Current.Name or ""
                except Exception:
                    continue
                if name_l is None or name_l in nm.lower():
                    if _is_onscreen(el):
                        return el
                    if offscreen_fallback is None:
                        offscreen_fallback = el
            if offscreen_fallback is not None:
                return offscreen_fallback
        time.sleep(0.4)
    raise Exception("UI element not found: name~='{0}' type={1}".format(
        name_contains, control_type))


def _invoke(element):
    try:
        pattern = element.GetCurrentPattern(InvokePattern.Pattern)
        pattern.Invoke()
        return
    except Exception:
        pass
    try:
        sel = element.GetCurrentPattern(SelectionItemPattern.Pattern)
        sel.Select()
        return
    except Exception:
        pass
    if LegacyIAccessiblePattern is not None:
        try:
            legacy = element.GetCurrentPattern(LegacyIAccessiblePattern.Pattern)
            legacy.DoDefaultAction()
            return
        except Exception:
            pass
    element.SetFocus()


def _get_main_window(uiapp):
    hwnd = uiapp.MainWindowHandle
    _bring_window_to_foreground(hwnd)
    return AutomationElement.FromHandle(hwnd)


_TAB_CONTROL_TYPES = [ControlType.TabItem, ControlType.Button,
                      ControlType.ListItem, ControlType.Custom, None]


def _click_ribbon_tab(main_window, tab_name, timeout_each=5):
    last_err = None
    for ct in _TAB_CONTROL_TYPES:
        try:
            tab = _find_preferring_onscreen(main_window, tab_name, control_type=ct,
                                            timeout=timeout_each)
            _invoke(tab)
            time.sleep(1.5)
            return
        except Exception as e:
            last_err = e
    raise Exception("Could not click ribbon tab '{0}' - {1}".format(tab_name, last_err))


def open_coordination_review(uiapp):
    """Opens Revit's Coordination Review dialog (Collaborate tab > Coordinate
    panel - confirmed present via an earlier live screenshot, alongside
    Coordination Settings, Reconcile Hosting, Interference Check).

    Only gets the dialog open - reviewing status/changes and choosing
    Accept/Reject/Postpone is left entirely to the user. Raises on failure
    so the caller can tell the user to open it manually instead."""
    main_window = _get_main_window(uiapp)

    try:
        _click_ribbon_tab(main_window, "Collaborate")
    except Exception:
        pass  # tab may already be active - keep trying to find the button anyway

    btn = _find_preferring_onscreen(main_window, "Coordination Review", timeout=15)
    _invoke(btn)
    time.sleep(1.5)
