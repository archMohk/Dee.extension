# -*- coding: utf-8 -*-
"""
dee_settings_file
Generic "Save Settings..."/"Load Settings..." file round-trip, generalizing
DeeInitiator.pushbutton's own save_set_click/load_set_click (the only
whole-tool-state file export/import in this extension before this module
existed) so other tools can get the same explicit, file-based save/load
cheaply - a SaveFileDialog/OpenFileDialog, not a silent background
convenience.

This is deliberately NOT a replacement for lib/deew_settings.py's existing
per-tool auto-remember (no dialog, no file the user picks, restores
silently on open) - the two coexist. This module is for "I want to save
THIS configuration to a file I can reopen later, or hand to a colleague,"
the same reasoning DeeInitiator's own module docstring already gives for
why it uses a real dialog instead of deew_settings here.

Since lib/dee_branding.py's DeeBrandedWindow has no awareness of what
controls a subclass actually has (confirmed - it's purely cosmetic), this
cannot be made fully generic/reflection-based: each tool supplies its own
collect_fn (return a plain dict of its current settings) and apply_fn
(take a loaded dict, apply it back onto its own controls) - usually thin
wrappers around code the tool already has for its existing deew_settings
save/restore.
"""
import json

import clr
clr.AddReference("System.Windows.Forms")
from System.Windows.Forms import SaveFileDialog, OpenFileDialog, DialogResult, MessageBox

from pyrevit import forms


def save_settings_file(tool_title, default_filename, collect_fn):
    """Shows a SaveFileDialog, writes {"format": "<tool_title>_SettingsV1",
    "settings": collect_fn()} as plain-ASCII JSON (json.dumps' own default
    ensure_ascii=True - the same deliberate choice DeeInitiator's own
    save_set_click makes, sidestepping IronPython 2.7 str/unicode
    encoding issues rather than risking a mis-encoded file). Returns
    True/False - never raises, shows its own forms.alert on failure."""
    dlg = SaveFileDialog()
    dlg.Filter = "{0} Settings (*.json)|*.json".format(tool_title)
    dlg.FileName = default_filename
    if dlg.ShowDialog() != DialogResult.OK:
        return False

    try:
        settings = collect_fn()
    except Exception as e:
        forms.alert("Could not collect current settings:\n{0}".format(e))
        return False

    data = {
        "format": "{0}_SettingsV1".format(tool_title.replace(" ", "")),
        "settings": settings,
    }
    try:
        with open(dlg.FileName, "w") as f:
            f.write(json.dumps(data, indent=2))
    except Exception as e:
        forms.alert("Could not save settings:\n{0}".format(e))
        return False

    MessageBox.Show("Settings saved to:\n{0}".format(dlg.FileName), tool_title)
    return True


def load_settings_file(tool_title, apply_fn):
    """Shows an OpenFileDialog, reads+validates the JSON, calls
    apply_fn(data["settings"]). Returns True/False - never raises, shows
    its own forms.alert on failure."""
    dlg = OpenFileDialog()
    dlg.Filter = "{0} Settings (*.json)|*.json".format(tool_title)
    dlg.Title = "Load {0} Settings".format(tool_title)
    if dlg.ShowDialog() != DialogResult.OK:
        return False

    try:
        with open(dlg.FileName, "r") as f:
            data = json.load(f)
    except Exception as e:
        forms.alert("Could not read that file:\n{0}".format(e))
        return False

    if not isinstance(data, dict) or "settings" not in data:
        forms.alert("That doesn't look like a {0} Settings file.".format(tool_title))
        return False

    try:
        apply_fn(data["settings"])
    except Exception as e:
        forms.alert("Could not apply the loaded settings:\n{0}".format(e))
        return False

    MessageBox.Show("Settings loaded from:\n{0}".format(dlg.FileName), tool_title)
    return True
