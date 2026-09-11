# -*- coding: utf-8 -*-
"""
dee_ribbon_mode
Backs DeePack's "View" combo box (Mode.panel/CategorySwitch.combobox) -
picking a category shows only the DeePack.tab panels relevant to that
workflow, decluttering the ~13-panel tab. "All" (the default) restores
today's behavior with nothing hidden.

The combo box itself is pyRevit's native declarative .combobox bundle
type (members: in bundle.yaml + __cmb_on_change__ in script.py) - no
raw Revit API ribbon-building code needed, and no risk to anything the
combo box doesn't explicitly touch: this module only ever flips
RibbonPanel.Visible (a normal, reversible, public API property) on
panels that already exist, never creates/destroys/reorders anything.

Panel names below are each panel's bundle.yaml `title:` (== what
RibbonPanel.Name resolves to at runtime), NOT the .panel folder name -
two panels (Sheets & Export, Views & Datums) differ from their folder
name, confirmed by reading every DeePack.tab/*.panel/bundle.yaml before
writing this.
"""
import json
import os

TAB_NAME = "DeePack"

# Panels always shown regardless of category: Mode so the switcher
# itself stays reachable, About because it's tiny/branding/support and
# not tied to any one workflow.
ALWAYS_VISIBLE = set(["Mode", "About"])

# category name -> list of panel titles to show. "All" is None, meaning
# "don't filter" rather than an explicit (and easily-stale) full list.
CATEGORY_PANELS = {
    "All": None,
    "Cloud & Sync": ["Models", "Cloud"],
    "Coordination": ["Coordination", "Masterplan"],
    "Design": ["Align", "Rooms", "Tools"],
    "Documentation": ["Views & Datums", "Sheets & Export", "Quantities"],
    "Health": ["Health"],
}

DEFAULT_CATEGORY = "All"

_THIS_DIR = os.path.dirname(__file__)
_STATE_PATH = os.path.join(_THIS_DIR, ".dee_ribbon_mode.json")


def load_last_category():
    """Returns the last category the user picked, or DEFAULT_CATEGORY
    if none was ever saved (first run) or the file can't be read.
    Never raises."""
    try:
        if os.path.exists(_STATE_PATH):
            with open(_STATE_PATH, "r") as f:
                data = json.load(f)
            saved = data.get("category")
            if saved in CATEGORY_PANELS:
                return saved
    except Exception:
        pass
    return DEFAULT_CATEGORY


def save_last_category(category):
    """Best-effort persistence so the choice survives to the next Revit
    session. Never raises - losing the saved preference is harmless
    (falls back to DEFAULT_CATEGORY), so it must never be allowed to
    disrupt the combo box itself."""
    try:
        with open(_STATE_PATH, "w") as f:
            json.dump({"category": category}, f)
    except Exception:
        pass


def apply_category(uiapp, category):
    """Shows/hides DeePack's panels for `category`. Never raises - a
    failure here must never take down the combo box or, worse, Revit
    itself; worst case the ribbon just doesn't filter this time."""
    try:
        keep = CATEGORY_PANELS.get(category, None)
        panels = uiapp.GetRibbonPanels(TAB_NAME)
    except Exception:
        return
    for panel in panels:
        try:
            name = panel.Name
        except Exception:
            continue
        try:
            if name in ALWAYS_VISIBLE or keep is None:
                panel.Visible = True
            else:
                panel.Visible = name in keep
        except Exception:
            pass
