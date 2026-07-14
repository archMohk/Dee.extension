# -*- coding: utf-8 -*-
"""UI Automation for Revit's Cloud Model 'Publish Settings' dialog.

Dialog structure confirmed from live screenshots (2026-07-02):
  - Window title: "Publish Settings"
  - "Select Sets" list: each row is a set name; a toolbar above the list
    has icon buttons whose UIA Name matches their tooltip - "New Set",
    "Duplicate", "Rename", "Delete".
  - Clicking "New Set" opens a small "New Set" window with a "Name:"
    textbox (default text pre-selected, so typing replaces it) and
    OK/Cancel buttons.
  - Selecting a set in "Select Sets" populates the "Edit Set" section
    below with a checkable list of views/sheets ("Include | Type | Name").
  - A single "Save & Close" button commits and closes the whole dialog.

Still no public Revit API for any of this - it's UI Automation end to end,
and the one remaining unverified piece is the ribbon click that opens this
dialog in the first place (open_publish_settings). Every public function
raises on failure so the caller can catch once and degrade gracefully.
"""
import time

import clr
clr.AddReference("UIAutomationClient")
clr.AddReference("UIAutomationTypes")
clr.AddReference("System.Windows.Forms")
from System.Windows.Automation import (
    AutomationElement, Condition, PropertyCondition, TreeScope,
    ControlType, InvokePattern, SelectionItemPattern, TogglePattern,
    ToggleState, ValuePattern
)
from System.Windows.Forms import SendKeys

# Not available in every environment - degrade gracefully rather than let
# a missing type crash the whole module at import time (it did once).
try:
    from System.Windows.Automation import LegacyIAccessiblePattern
except ImportError:
    LegacyIAccessiblePattern = None

# AutomationElement.SetFocus() silently fails with "cannot receive focus"
# if the containing top-level window isn't already the OS foreground
# window - a script-driven Revit session isn't guaranteed to be, since
# nothing physically clicked it. SetForegroundWindow forces that.
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


def _find(parent, name_contains=None, control_type=None,
          scope=TreeScope.Descendants, timeout=10):
    """Poll for a descendant whose Name contains `name_contains`
    (case-insensitive) and matches `control_type`. Raises after timeout."""
    deadline = time.time() + timeout
    name_l = name_contains.lower() if name_contains else None
    cond = (PropertyCondition(AutomationElement.ControlTypeProperty, control_type)
            if control_type is not None else Condition.TrueCondition)
    while time.time() < deadline:
        try:
            found = parent.FindAll(scope, cond)
        except Exception:
            found = None
        if found:
            for el in found:
                try:
                    nm = el.Current.Name or ""
                except Exception:
                    continue
                if name_l is None or name_l in nm.lower():
                    return el
        time.sleep(0.4)
    raise Exception("UI element not found: name~='{0}' type={1}".format(
        name_contains, control_type))


def _find_optional(parent, name_contains=None, control_type=None,
                    scope=TreeScope.Descendants, timeout=3):
    try:
        return _find(parent, name_contains, control_type, scope, timeout)
    except Exception:
        return None


def _is_onscreen(element):
    try:
        return not element.Current.IsOffscreen
    except Exception:
        return True  # unknown - don't over-filter, assume visible


def _find_preferring_onscreen(parent, name_contains, control_type=None, timeout=10):
    """Same idea as _find, but when multiple elements match by name,
    prefers one that's actually visible (IsOffscreen == False). Revit's
    ribbon appears to keep controls from INACTIVE tabs present in the
    automation tree, just not rendered - a plain name search can match one
    of those, which would explain a "successful" find that still fails
    every click method (there's nothing real to click)."""
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
    raise Exception(
        "UI element not found (onscreen-preferring): name~='{0}' "
        "type={1}".format(name_contains, control_type))


def _find_preferring_onscreen_optional(parent, name_contains, control_type=None, timeout=3):
    try:
        return _find_preferring_onscreen(parent, name_contains, control_type, timeout)
    except Exception:
        return None


def _find_exact_or_contains(parent, name, control_type=None, timeout=8):
    """Prefers an exact Name match over a substring match. This file has
    accumulated many similarly-named leftover sets/views from repeated
    testing (e.g. "BIM Coordination View" is a substring of both
    "01-BIM Coordination View" and "BIM Coordination View 2") - a pure
    substring search could silently grab the wrong one, same bug class
    already found and fixed in the view-creation code."""
    deadline = time.time() + timeout
    name_l = name.lower()
    cond = (PropertyCondition(AutomationElement.ControlTypeProperty, control_type)
            if control_type is not None else Condition.TrueCondition)
    while time.time() < deadline:
        try:
            found = parent.FindAll(TreeScope.Descendants, cond)
        except Exception:
            found = None
        if found:
            substring_match = None
            for el in found:
                try:
                    nm = el.Current.Name or ""
                except Exception:
                    continue
                if nm == name:
                    return el
                if substring_match is None and name_l in nm.lower():
                    substring_match = el
            if substring_match is not None:
                return substring_match
        time.sleep(0.4)
    raise Exception("UI element not found: name=='{0}' (or containing it) "
                     "type={1}".format(name, control_type))


def _find_exact_or_contains_optional(parent, name, control_type=None, timeout=3):
    try:
        return _find_exact_or_contains(parent, name, control_type, timeout)
    except Exception:
        return None


def _find_button_like(parent, name_contains, timeout=8):
    """Searches for a button by name, trying ControlType.Button first (the
    common case), then falling back to no type filter at all. Guards
    against the exact bug class already found once in the ribbon tabs -
    assuming the wrong ControlType silently finds nothing."""
    try:
        return _find(parent, name_contains=name_contains,
                     control_type=ControlType.Button, timeout=timeout)
    except Exception:
        return _find(parent, name_contains=name_contains,
                     timeout=max(3, timeout // 2))


def _invoke(element):
    """Tries every click mechanism a control might expose, in order of
    preference. Revit's ribbon is a mix of WPF and legacy MFC-style
    controls - some only answer to LegacyIAccessible, not modern UIA
    patterns, which is why a plain InvokePattern-only approach can find
    a button but still fail to actually click it."""
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
    # Last resort - no UIA pattern worked at all (or LegacyIAccessiblePattern
    # isn't available in this environment). Focus the element and send a
    # keyboard Space, the standard Windows convention for activating
    # whatever control currently has focus - works without needing any
    # pattern support at all.
    try:
        element.SetFocus()
        time.sleep(0.15)
        SendKeys.SendWait(" ")
        return
    except Exception as e:
        raise Exception("Element does not support Invoke, Select, Legacy, "
                         "or focus+Space activation: {0}".format(e))


def _set_checked(element, checked=True):
    toggle = element.GetCurrentPattern(TogglePattern.Pattern)
    state = ToggleState.On if checked else ToggleState.Off
    if toggle.Current.ToggleState != state:
        toggle.Toggle()


def _find_window(title_contains, timeout=15):
    return _find(_root(), name_contains=title_contains,
                 control_type=ControlType.Window, scope=TreeScope.Children,
                 timeout=timeout)


def _dump_button_names(parent, limit=40):
    """Diagnostic aid: lists Button-type elements currently under `parent`.
    Used only in error messages so a search failure tells us what the
    automation tree actually contains, instead of a bare 'not found'."""
    try:
        cond = PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Button)
        found = parent.FindAll(TreeScope.Descendants, cond)
        names = []
        for el in found:
            try:
                nm = el.Current.Name
            except Exception:
                nm = None
            if nm:
                names.append(nm)
            if len(names) >= limit:
                break
        return ", ".join(names) if names else "(none found)"
    except Exception as e:
        return "(dump failed: {0})".format(e)


def _get_main_window(uiapp):
    """MainWindowHandle can be unreliable to resolve via FromHandle in some
    pyRevit/IronPython setups, so fall back to finding the window by its
    title bar (confirmed format: "Autodesk Revit 2024.3 - <file>...").
    Also forces the window to the OS foreground - a script-driven session
    isn't guaranteed to already be foreground, and several UIA operations
    (SetFocus in particular) silently fail on background windows."""
    try:
        hwnd = uiapp.MainWindowHandle
        if hwnd and int(hwnd) != 0:
            _bring_window_to_foreground(hwnd)
            return AutomationElement.FromHandle(hwnd)
    except Exception:
        pass
    window = _find(_root(), name_contains="Autodesk Revit",
                    control_type=ControlType.Window, scope=TreeScope.Children,
                    timeout=10)
    try:
        _bring_window_to_foreground(window.Current.NativeWindowHandle)
    except Exception:
        pass
    return window


# Diagnostic evidence (live run) showed the ribbon never actually left the
# Architecture tab - the "Collaborate" search below was filtered to
# ControlType.TabItem, which apparently doesn't match how Revit exposes its
# ribbon tabs via UI Automation, so the search silently found nothing and
# the click never happened. Trying several control types fixes that without
# needing to know the real one for certain.
_TAB_CONTROL_TYPES = [ControlType.TabItem, ControlType.Button,
                      ControlType.ListItem, ControlType.Custom, None]


def _click_ribbon_tab(main_window, tab_name, timeout_each=5):
    last_err = None
    for ct in _TAB_CONTROL_TYPES:
        try:
            tab = _find_preferring_onscreen(main_window, tab_name, control_type=ct,
                                            timeout=timeout_each)
            _invoke(tab)
            time.sleep(1.5)  # ribbon panel repaint after switching tabs
            return
        except Exception as e:
            last_err = e
    raise Exception("Could not click ribbon tab '{0}' after trying several "
                     "control types - last error: {1}".format(tab_name, last_err))


def _click_ribbon_tab_verified(main_window, tab_name, verify_marker,
                                timeout_each=5, max_attempts=2):
    """Clicks the tab, then actually checks that a marker unique to that
    tab (e.g. a button only present on Collaborate) is both present AND
    on-screen afterward, retrying instead of just assuming the click
    worked. Checking mere presence wasn't enough - Revit's ribbon appears
    to keep inactive tabs' controls in the tree, so a marker existing
    doesn't prove the tab visually switched."""
    last_err = None
    for attempt in range(max_attempts):
        try:
            _click_ribbon_tab(main_window, tab_name, timeout_each=timeout_each)
        except Exception as e:
            last_err = e
            continue
        marker = _find_optional(main_window, name_contains=verify_marker, timeout=6)
        if marker is not None and _is_onscreen(marker):
            return
        last_err = Exception(
            "clicked '{0}' but '{1}' marker was not found afterward".format(
                tab_name, verify_marker))
    raise Exception(
        "Could not verify ribbon tab '{0}' is active after {1} attempt(s) - "
        "{2}".format(tab_name, max_attempts, last_err))


def open_publish_settings(uiapp):
    """Locates and invokes the ribbon 'Publish Settings' button (Collaborate
    tab > Manage Models panel, confirmed via live screenshot - it's a direct
    button, not nested in a dropdown), then waits for its dialog window."""
    main_window = _get_main_window(uiapp)

    try:
        _click_ribbon_tab_verified(main_window, "Collaborate", "Synchronize with Central")
    except Exception:
        pass  # tab may already be active - keep trying to find the button anyway

    btn = _find_preferring_onscreen_optional(main_window, "Publish Settings", timeout=15)
    if btn is None:
        # fallback in case some Revit versions nest it under a dropdown
        dropdown = _find_preferring_onscreen_optional(main_window, "Manage Models", timeout=6)
        if dropdown is not None:
            _invoke(dropdown)
            time.sleep(0.8)
            btn = _find_preferring_onscreen_optional(main_window, "Publish Settings", timeout=8)
    if btn is None:
        raise Exception(
            "Could not locate 'Publish Settings' ribbon button "
            "(Collaborate tab, Manage Models panel). Buttons actually "
            "visible right now: [{0}]".format(_dump_button_names(main_window)))

    _invoke(btn)
    time.sleep(1.5)
    dialog = _find_window("Publish Settings", timeout=15)
    # The dialog is a NEW top-level window, separate from the main Revit
    # window foregrounded earlier - it doesn't automatically inherit that
    # status just because its parent is foreground, and a simulated click
    # (not a genuine physical one) can leave Windows treating it as
    # background, which is exactly what causes "cannot receive focus"
    # on controls inside it.
    try:
        _bring_window_to_foreground(dialog.Current.NativeWindowHandle)
    except Exception:
        pass
    return dialog


def _ensure_set_selected(dialog, set_name):
    """Selects `set_name` in the 'Select Sets' list if it already exists
    (populating the 'Edit Set' section below), otherwise creates it via
    the 'New Set' toolbar button + its Name popup."""
    existing = _find_exact_or_contains_optional(dialog, set_name, timeout=2)
    if existing is not None:
        _invoke(existing)
        time.sleep(0.5)
        return True

    new_btn = _find_button_like(dialog, "New Set", timeout=8)
    _invoke(new_btn)
    time.sleep(1)

    name_dialog = _find_window("New Set", timeout=8)
    try:
        _bring_window_to_foreground(name_dialog.Current.NativeWindowHandle)
    except Exception:
        pass
    name_box = _find_optional(name_dialog, control_type=ControlType.Edit, timeout=5)
    if name_box is None:
        name_box = _find(name_dialog, timeout=3)
    try:
        value_pattern = name_box.GetCurrentPattern(ValuePattern.Pattern)
        value_pattern.SetValue(set_name)
    except Exception:
        name_box.SetFocus()
        SendKeys.SendWait("^a")
        SendKeys.SendWait(set_name)

    ok_btn = _find_button_like(name_dialog, "OK", timeout=5)
    _invoke(ok_btn)
    time.sleep(1)
    return False


def _check_view_in_set(dialog, view_name):
    """Checks the Include checkbox for `view_name` in the 'Edit Set' list
    below the currently-selected set. Uses exact-match-first lookup - this
    list can contain several similarly-named leftover views (e.g.
    "01-BIM Coordination View", "BIM Coordination View 2"), and checking
    the wrong one would silently misconfigure the publish set."""
    view_row = _find_exact_or_contains(dialog, view_name, timeout=8)
    try:
        _set_checked(view_row, True)
        return
    except Exception:
        pass
    # TogglePattern wasn't available on whatever we matched - fall back to
    # the full multi-pattern _invoke() rather than just Select(), since
    # Select() only highlights the row without actually checking its box.
    _invoke(view_row)


def configure_publish_settings(uiapp, set_name, view_name):
    """Full flow: open dialog, ensure set exists & is selected, check the
    coordination view within it, Save & Close.
    Raises on any failure - caller must catch and degrade gracefully. Each
    stage is separately tagged so a failure points at exactly which one -
    same lesson learned from the earlier Views debugging, where a bare
    error message with no stage info cost several rounds of guessing."""
    try:
        dialog = open_publish_settings(uiapp)
    except Exception as e:
        raise Exception("[open dialog] {0}".format(e))

    try:
        try:
            _ensure_set_selected(dialog, set_name)
        except Exception as e:
            raise Exception("[ensure set selected] {0}".format(e))

        try:
            _check_view_in_set(dialog, view_name)
        except Exception as e:
            raise Exception("[check view in set] {0}".format(e))

        try:
            save_btn = _find_button_like(dialog, "Save & Close", timeout=8)
            _invoke(save_btn)
            time.sleep(1)
        except Exception as e:
            raise Exception("[save & close] {0}".format(e))
    except Exception:
        try:
            cancel_btn = _find_optional(dialog, name_contains="Cancel", timeout=3)
            if cancel_btn is not None:
                _invoke(cancel_btn)
        except Exception:
            pass
        raise
