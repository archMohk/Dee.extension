# -*- coding: utf-8 -*-
"""
dee_force_state
Backs DeeForce - a background, toggleable auto-sync: while ON, every
open workshared model gets synchronized with central on a fixed
interval, with a visible on-screen countdown starting 30 seconds
before each sync so the user is never surprised by an unattended sync
firing mid-edit.

--------------------------------------------------------------------
Architecture - the one genuinely novel pattern in this codebase
--------------------------------------------------------------------
Every other Dee tool is a plain pushbutton: script.py runs once per
click and finishes. DeeForce instead needs code that keeps running on
an interval for as long as it stays ON, spanning many separate button
clicks and Revit's own idle time. The mechanism is
UIApplication.Idling (fires repeatedly whenever Revit has nothing else
to do, and can be told to keep firing via
IdlingEventArgs.SetRaiseWithoutDelay() instead of waiting for the next
real idle moment) - DeeForce.pushbutton's script.py just flips a
switch (enable()/disable() below), which subscribes/unsubscribes
_on_idling().

Both the toggle STATE and the actual handler FUNCTION live here, at
module level, specifically because this module stays cached in
sys.modules for the life of the Revit process - a LATER click of
DeeForce (a brand new script.py execution, its own fresh Python
module namespace) still reaches this SAME cached module object and
therefore the SAME _on_idling function reference, which is what makes
`uiapp.Idling -= _on_idling` from click #2 actually remove what
`uiapp.Idling += _on_idling` added from click #1's entirely separate
execution. A per-click-local handler closure would not work here.

Deliberately does NOT persist the on/off state itself across Revit
restarts (only the configured interval does) - resetting to OFF every
fresh session means a background auto-sync is always something you
explicitly turned on THIS session, never a silent leftover from last
time you used Revit.

NEEDS LIVE-REVIT VERIFICATION - first use of UIApplication.Idling in
this codebase for a long-lived, cross-click subscription (every other
Idling/DialogBoxShowing use this session is scoped to one script run,
unsubscribed in that same run's own finally block).
"""
import json
import os
import time

_THIS_DIR = os.path.dirname(__file__)
_CONFIG_PATH = os.path.join(_THIS_DIR, ".dee_force_config.json")

DEFAULT_INTERVAL_MINUTES = 10
MIN_INTERVAL_MINUTES = 1
MAX_INTERVAL_MINUTES = 240
WARNING_SECONDS = 30

_active = False
_handler = None
_last_sync_time = None
_countdown_window = None
_countdown_label = None
_last_shown_seconds = None


# ---------------- configured interval (persists across sessions) ----------------
def load_interval_minutes():
    try:
        if os.path.exists(_CONFIG_PATH):
            with open(_CONFIG_PATH, "r") as f:
                data = json.load(f)
            value = data.get("interval_minutes")
            if (isinstance(value, (int, float))
                    and MIN_INTERVAL_MINUTES <= value <= MAX_INTERVAL_MINUTES):
                return value
    except Exception:
        pass
    return DEFAULT_INTERVAL_MINUTES


def save_interval_minutes(minutes):
    try:
        with open(_CONFIG_PATH, "w") as f:
            json.dump({"interval_minutes": minutes}, f)
    except Exception:
        pass


def is_active():
    return _active


_TAB_NAME = "DeePack"
_BUTTON_NAME = "DeeForce"
_bundle_dir_cache = None


def _find_deeforce_bundle_dir():
    """Walks DeePack.tab looking for the DeeForce.pushbutton folder
    rather than hardcoding which panel it lives under - it has already
    moved once (Models -> Health) and may move again; this way the
    icon files are always found regardless. Result is cached after the
    first successful find (folder location doesn't change mid-session)."""
    global _bundle_dir_cache
    if _bundle_dir_cache is not None:
        return _bundle_dir_cache
    tab_root = os.path.join(os.path.dirname(_THIS_DIR), "DeePack.tab")
    try:
        for root, dirs, _files in os.walk(tab_root):
            for d in dirs:
                if d == "DeeForce.pushbutton":
                    _bundle_dir_cache = os.path.join(root, d)
                    return _bundle_dir_cache
    except Exception:
        pass
    return None


def _icon_path(filename):
    bundle_dir = _find_deeforce_bundle_dir()
    if not bundle_dir:
        return None
    return os.path.join(bundle_dir, filename)


def _find_deeforce_button(uiapp):
    """Searches every DeePack panel (not just Health) for the item
    named DeeForce, so this keeps working if the button is ever moved
    to a different panel again without needing a code change here."""
    try:
        panels = uiapp.GetRibbonPanels(_TAB_NAME)
    except Exception:
        return None
    for panel in panels:
        try:
            items = panel.GetItems()
        except Exception:
            continue
        for item in items:
            try:
                if item.Name == _BUTTON_NAME:
                    return item
            except Exception:
                continue
    return None


def _set_button_icon(uiapp, icon_path):
    """Swaps the live DeeForce button's icon - reuses pyRevit's own
    ButtonIcons (the exact mechanism it uses for every icon on the
    whole ribbon already), rather than re-implementing WPF bitmap
    loading/resizing from scratch. Never raises - a failure here only
    means the icon doesn't visually update, the actual on/off toggle
    and sync behavior are unaffected either way."""
    try:
        if not icon_path or not os.path.isfile(icon_path):
            return
        button = _find_deeforce_button(uiapp)
        if button is None:
            return
        from pyrevit.coreutils.ribbon import ButtonIcons
        icons = ButtonIcons(icon_path)
        try:
            button.Image = icons.small_bitmap
        except Exception:
            pass
        try:
            button.LargeImage = icons.large_bitmap
        except Exception:
            pass
    except Exception:
        pass


# ---------------- the countdown overlay ----------------
def _ensure_countdown_window():
    """Built once per ON period the first time the warning window is
    needed, reused for every subsequent tick within that same
    countdown - never raises; if WPF construction fails for any
    reason, the countdown simply never appears but the sync itself
    still happens on schedule."""
    global _countdown_window, _countdown_label
    if _countdown_window is not None:
        return
    try:
        import clr
        clr.AddReference("PresentationFramework")
        clr.AddReference("PresentationCore")
        clr.AddReference("WindowsBase")
        from System.Windows import (
            Window, WindowStyle, ResizeMode, Thickness, HorizontalAlignment,
            FontWeights, SizeToContent, CornerRadius)
        from System.Windows.Controls import Border, TextBlock, StackPanel, Orientation
        from System.Windows.Media import SolidColorBrush, Color

        win = Window()
        win.Title = "Dee.extension"
        win.WindowStyle = getattr(WindowStyle, "None")
        win.ResizeMode = ResizeMode.NoResize
        win.ShowInTaskbar = False
        win.Topmost = True
        win.SizeToContent = SizeToContent.WidthAndHeight
        win.Left = 40
        win.Top = 40
        win.Background = SolidColorBrush(Color.FromRgb(0x2B, 0x2B, 0x2B))
        win.AllowsTransparency = False

        border = Border()
        border.BorderBrush = SolidColorBrush(Color.FromRgb(0xF2, 0x99, 0x4D))
        border.BorderThickness = Thickness(2)
        border.CornerRadius = CornerRadius(6)
        border.Padding = Thickness(16, 10, 16, 10)

        stack = StackPanel()
        stack.Orientation = Orientation.Vertical

        title_tb = TextBlock()
        title_tb.Text = "Dee.extension - Auto-Sync"
        title_tb.Foreground = SolidColorBrush(Color.FromRgb(0xF2, 0x99, 0x4D))
        title_tb.FontWeight = FontWeights.Bold
        title_tb.FontSize = 12
        title_tb.HorizontalAlignment = HorizontalAlignment.Center
        stack.Children.Add(title_tb)

        count_tb = TextBlock()
        count_tb.Foreground = SolidColorBrush(Color.FromRgb(0xFF, 0xFF, 0xFF))
        count_tb.FontSize = 20
        count_tb.FontWeight = FontWeights.Bold
        count_tb.HorizontalAlignment = HorizontalAlignment.Center
        count_tb.Margin = Thickness(0, 4, 0, 0)
        stack.Children.Add(count_tb)

        border.Child = stack
        win.Content = border

        _countdown_window = win
        _countdown_label = count_tb
    except Exception:
        _countdown_window = None
        _countdown_label = None


def _update_countdown(remaining_seconds):
    global _last_shown_seconds
    secs = int(max(0, remaining_seconds))
    if secs == _last_shown_seconds:
        return
    _last_shown_seconds = secs
    _ensure_countdown_window()
    if _countdown_window is None:
        return
    try:
        _countdown_label.Text = "Syncing all open models in {0}s...".format(secs)
        if not _countdown_window.IsVisible:
            _countdown_window.Show()
    except Exception:
        pass


def _close_countdown_window():
    global _countdown_window, _countdown_label, _last_shown_seconds
    if _countdown_window is not None:
        try:
            _countdown_window.Close()
        except Exception:
            pass
    _countdown_window = None
    _countdown_label = None
    _last_shown_seconds = None


# ---------------- the actual sync action ----------------
def _do_sync(uiapp):
    """Synchronizes every open, non-linked, workshared document -
    same filter DeeSYNC uses. Dialog/transaction-failure handling
    wired around the whole pass, same as every other unattended batch
    tool in this codebase, since this one is by definition unattended
    (no one is necessarily watching when a scheduled sync fires)."""
    import deew_document_manager as docmgr
    import deew_failure_handler as ffh
    import deew_logger

    logger = deew_logger.DeeWLogger("DeeForce")
    dialog_handler = ffh.make_dialog_handler(logger)
    try:
        uiapp.DialogBoxShowing += dialog_handler
    except Exception:
        pass
    try:
        app = uiapp.Application
        for doc in list(app.Documents):
            try:
                if doc.IsFamilyDocument or doc.IsLinked or not doc.IsWorkshared:
                    continue
                docmgr.synchronize_with_central(
                    doc, comment="DeeForce - scheduled auto-sync",
                    compact=False, logger=logger)
            except Exception as e:
                logger.exception("Auto-sync failed for a document", e)
    finally:
        try:
            uiapp.DialogBoxShowing -= dialog_handler
        except Exception:
            pass


# ---------------- the Idling handler ----------------
def _on_idling(sender, args):
    global _last_sync_time
    if not _active:
        return
    try:
        args.SetRaiseWithoutDelay()
    except Exception:
        pass
    try:
        interval_seconds = load_interval_minutes() * 60.0
        if _last_sync_time is None:
            _last_sync_time = time.time()
            return
        elapsed = time.time() - _last_sync_time
        remaining = interval_seconds - elapsed

        if remaining <= 0:
            _close_countdown_window()
            _do_sync(sender)
            _last_sync_time = time.time()
            return

        if remaining <= WARNING_SECONDS:
            _update_countdown(remaining)
    except Exception:
        pass


# ---------------- on/off ----------------
def enable(uiapp):
    global _active, _handler, _last_sync_time
    if _active:
        return
    _last_sync_time = time.time()
    _handler = _on_idling
    try:
        uiapp.Idling += _handler
        _active = True
    except Exception:
        _handler = None
        _active = False
    _set_button_icon(uiapp, _icon_path("icon.on.png"))


def disable(uiapp):
    global _active, _handler
    if not _active:
        return
    _active = False
    if _handler is not None:
        try:
            uiapp.Idling -= _handler
        except Exception:
            pass
    _handler = None
    _close_countdown_window()
    _set_button_icon(uiapp, _icon_path("icon.png"))
