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
RibbonPanel.Visible / RibbonItem.Visible (normal, reversible, public
API properties) on panels/items that already exist, never creates,
destroys, or reorders anything.

Panel names below are each panel's bundle.yaml `title:` (== what
RibbonPanel.Name resolves to at runtime), NOT the .panel folder name -
two panels (Sheets & Export, Views & Datums) differ from their folder
name, confirmed by reading every DeePack.tab/*.panel/bundle.yaml before
writing this.

--------------------------------------------------------------------
FAVORITE_CATEGORY - a fundamentally different mechanism from the rest
--------------------------------------------------------------------
Every other category shows/hides whole PANELS. Favorite instead shows
every panel but hides individual BUTTONS within them, matched against
a personal pick list DeeControl writes to lib/.dee_favorites.json
(dee_control_panel_service.load_favorites/save_favorites - a separate,
gitignored, per-machine file, not the team-wide on/off state
DeeControl's own dee_control_panel.json holds).

This is the first per-BUTTON (not just per-panel) ribbon visibility
toggle in this codebase - NEEDS LIVE-REVIT VERIFICATION, specifically:
whether a pyRevit-created RibbonItem's own .Name reliably equals its
bundle folder name (confirmed true for panels via their `title:`
field; assumed, not yet confirmed, for individual buttons/pulldowns).
A name that doesn't match anything live is silently skipped rather
than erroring either way, so the worst case of this being wrong is
"a favorited button doesn't show up," never a crash.
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

# Handled separately from CATEGORY_PANELS - see the module docstring.
FAVORITE_CATEGORY = "Favorite"

DEFAULT_CATEGORY = "All"

_THIS_DIR = os.path.dirname(__file__)
_STATE_PATH = os.path.join(_THIS_DIR, ".dee_ribbon_mode.json")
_FAVORITES_PATH = os.path.join(_THIS_DIR, ".dee_favorites.json")
_DEBUG_LOG_PATH = os.path.join(_THIS_DIR, ".dee_favorites_debug.log")


def _write_debug_log(lines):
    """Live evidence for exactly what _apply_favorites did/failed on,
    per item - added because a live run showed DeeSYNC staying visible
    in Favorite mode while every other non-favorited button hid
    correctly, and static reading of this module alone couldn't
    explain why. Best-effort, never raises, overwrites each run (only
    the latest matters)."""
    try:
        import datetime
        with open(_DEBUG_LOG_PATH, "w") as f:
            f.write("Favorite mode debug - {0}\n".format(datetime.datetime.now()))
            for line in lines:
                f.write(line + "\n")
    except Exception:
        pass


def _valid_category(name):
    return name in CATEGORY_PANELS or name == FAVORITE_CATEGORY


def load_last_category():
    """Returns the last category the user picked, or DEFAULT_CATEGORY
    if none was ever saved (first run) or the file can't be read.
    Never raises."""
    try:
        if os.path.exists(_STATE_PATH):
            with open(_STATE_PATH, "r") as f:
                data = json.load(f)
            saved = data.get("category")
            if _valid_category(saved):
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


def load_favorite_names():
    """The set of bare button names DeeControl has marked as favorite.
    Never raises - an unreadable/missing file just means no favorites,
    not an error."""
    try:
        if os.path.exists(_FAVORITES_PATH):
            with open(_FAVORITES_PATH, "r") as f:
                data = json.load(f)
            return set(data.get("favorites", []))
    except Exception:
        pass
    return set()


def has_favorites():
    return len(load_favorite_names()) > 0


def _iter_ribbon_items(panel):
    """Yields every RibbonItem directly on a panel, plus a pulldown/
    split button's own nested children. pyRevit's .stack bundle type
    has no separate wrapper object at the API level - stacked items
    are ordinary panel items, just laid out vertically - so panel
    .GetItems() already covers those with no special-casing needed."""
    try:
        items = list(panel.GetItems())
    except Exception:
        return
    for item in items:
        yield item
        try:
            nested = item.GetItems()
        except Exception:
            nested = None
        if nested:
            for child in nested:
                yield child


def _apply_favorites(uiapp, panels):
    favorites = load_favorite_names()
    debug = ["favorites: {0}".format(sorted(favorites))]
    for panel in panels:
        try:
            panel.Visible = True
            panel_name = panel.Name
        except Exception as e:
            debug.append("PANEL - could not read/show: {0}".format(e))
            continue
        if panel_name in ALWAYS_VISIBLE:
            debug.append("{0} - always-visible panel, items untouched".format(panel_name))
            continue
        for item in _iter_ribbon_items(panel):
            try:
                item_name = item.Name
            except Exception as e:
                debug.append("{0} - item.Name read FAILED: {1}".format(panel_name, e))
                continue
            want = item_name in favorites
            try:
                item.Visible = want
                debug.append("{0}/{1} - set Visible={2}".format(panel_name, item_name, want))
            except Exception as e:
                debug.append("{0}/{1} - FAILED to set Visible={2}: {3}".format(
                    panel_name, item_name, want, e))
    _write_debug_log(debug)


def apply_category(uiapp, category):
    """Shows/hides DeePack's panels (and, for Favorite, individual
    buttons within them) for `category`. Never raises - a failure here
    must never take down the combo box or, worse, Revit itself; worst
    case the ribbon just doesn't filter this time."""
    try:
        panels = uiapp.GetRibbonPanels(TAB_NAME)
    except Exception:
        return

    if category == FAVORITE_CATEGORY:
        _apply_favorites(uiapp, panels)
        return

    keep = CATEGORY_PANELS.get(category, None)
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
            # Undo any per-item hiding a previous Favorite selection
            # left behind - every non-Favorite category always shows
            # every item within a visible panel.
            for item in _iter_ribbon_items(panel):
                try:
                    item.Visible = True
                except Exception:
                    pass
        except Exception:
            pass
