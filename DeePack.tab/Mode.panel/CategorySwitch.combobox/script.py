# -*- coding: utf-8 -*-
"""
DeePack "View" combo box - switches which DeePack.tab panels are
visible by workflow category (Cloud & Sync / Coordination / Design /
Documentation / Health / All). All the actual show/hide logic lives in
lib/dee_ribbon_mode.py, kept out of this file so it stays testable
without a live Revit session.

pyRevit's .combobox bundle type recognizes two module-level functions
here: __selfinit__ (runs once, when the combo box is built) and
__cmb_on_change__ (runs every time the user picks a different item).
Both are called by pyRevit itself already wrapped in try/except
(pyrevit/loader/uimaker.py setup_combobox), so an exception here is
logged and swallowed rather than ever reaching Revit - this file's own
try/except below is a second, redundant layer for the same reason
every other tool in this codebase never lets a UI customization crash
the host.
"""
import os
import dee_ribbon_mode as mode

# script.py -> CategorySwitch.combobox -> Mode.panel -> DeePack.tab -> Dee.extension
_EXTENSION_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
_TAB_ICON_PATH = os.path.join(_EXTENSION_ROOT, "icon.png")


def __selfinit__(component, ui_item, uiapp):
    """Restores the last category the user picked (persisted to
    lib/.dee_ribbon_mode.json) and applies it immediately, so the
    ribbon already reflects it before the user ever touches the
    dropdown - the combo box itself defaults to its first member
    ("All") until this runs. Also where the experimental tab-icon
    attempt runs (see dee_ribbon_mode.set_tab_icon's own docstring) -
    reused here specifically because by the time a combo box's
    __selfinit__ fires, the DeePack tab it belongs to is guaranteed to
    already exist (pyRevit builds a bundle's own ribbon items before
    running its script), unlike an extension-level startup.py, which
    runs before ANY tab/panel exists yet for the whole session."""
    try:
        saved = mode.load_last_category()
        cmb = ui_item.get_rvtapi_object()
        for item in cmb.GetItems():
            if item.Name == saved:
                cmb.Current = item
                break
        mode.apply_category(uiapp, saved)
    except Exception:
        pass
    try:
        mode.set_tab_icon(uiapp, _TAB_ICON_PATH)
    except Exception as e:
        try:
            mode._write_tab_icon_log(
                ["Calling set_tab_icon() itself raised: {0}".format(e)])
        except Exception:
            pass
    return True


def __cmb_on_change__(sender, args, ctx):
    try:
        category = ctx.current_name
        mode.apply_category(ctx.uiapp, category)
        mode.save_last_category(category)
        if category == mode.FAVORITE_CATEGORY and not mode.has_favorites():
            from pyrevit import forms
            forms.alert(
                "No favorites marked yet - every button just got hidden.\n\n"
                "Open DeeControl (About panel) and tick some tools as "
                "favorites, then Save Favorites - no reload needed, just "
                "pick Favorite here again.",
                title="Dee.extension - Favorite view is empty")
    except Exception:
        pass
    # Second attempt point for the experimental tab icon (see
    # __selfinit__'s docstring and dee_ribbon_mode.set_tab_icon's own
    # docstring) - three full-restart cycles produced zero evidence
    # even the "entered" marker ran from __selfinit__, despite the SAME
    # function's OTHER logic (apply_category) reliably working from
    # that exact spot every time. __cmb_on_change__ fires on a genuine
    # WPF SelectionChanged-style event, a more ordinary UI-thread
    # context than __selfinit__'s ribbon-construction-time call, which
    # may matter for something reaching this deep into AdWindows
    # internals - reusing this proven-reliable trigger point rather
    # than repeating the same untried-from-here attempt again.
    try:
        mode.set_tab_icon(ctx.uiapp, _TAB_ICON_PATH)
    except Exception as e:
        try:
            mode._write_tab_icon_log(
                ["Calling set_tab_icon() from __cmb_on_change__ raised: {0}".format(e)])
        except Exception:
            pass
