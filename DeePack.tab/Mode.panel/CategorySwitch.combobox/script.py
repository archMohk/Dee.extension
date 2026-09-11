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
import dee_ribbon_mode as mode


def __selfinit__(component, ui_item, uiapp):
    """Restores the last category the user picked (persisted to
    lib/.dee_ribbon_mode.json) and applies it immediately, so the
    ribbon already reflects it before the user ever touches the
    dropdown - the combo box itself defaults to its first member
    ("All") until this runs."""
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
    return True


def __cmb_on_change__(sender, args, ctx):
    try:
        category = ctx.current_name
        mode.apply_category(ctx.uiapp, category)
        mode.save_last_category(category)
    except Exception:
        pass
