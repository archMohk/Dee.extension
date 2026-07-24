# -*- coding: utf-8 -*-
"""
deew_settings
Persistent settings service shared by every DeeW.Cloud tool - remembers
the last source folder, ACC Hub/Project/Folder selection, and
processing options between sessions, restoring them automatically the
next time a tool opens (per spec: "Remember... Restore automatically").

One JSON file per tool under Dee.extension/lib/.deew_settings/ so
DeeW.Sharing and DeeW.Batch Save to Cloud (and future DeeW.Cloud tools)
never collide, but all share this exact read/write logic - no
duplicated persistence code per tool.

Never raises - a corrupted or unreadable settings file just means
"start fresh" (defaults are returned), not a crash. This directory is
listed in .gitignore since it holds machine-local paths and ACC
project/folder identifiers, not something to publish in a public repo.
"""
import os
import json

_LIB_DIR = os.path.dirname(os.path.abspath(__file__))
_SETTINGS_DIR = os.path.join(_LIB_DIR, ".deew_settings")


def _settings_path(tool_name):
    safe_name = "".join(c for c in tool_name if c.isalnum() or c in ("_", "-")) or "deew_tool"
    return os.path.join(_SETTINGS_DIR, "{0}.json".format(safe_name))


def load(tool_name, defaults=None):
    """Returns a plain dict - defaults merged UNDER whatever was
    actually saved, so adding a new default key later doesn't require
    migrating existing users' settings files."""
    result = dict(defaults or {})
    path = _settings_path(tool_name)
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                result.update(saved)
    except Exception:
        pass
    return result


def save(tool_name, settings_dict):
    """Best-effort - a failed save (locked file, read-only folder,
    permissions) just means settings won't persist this time, never a
    crash for the calling tool."""
    try:
        if not os.path.isdir(_SETTINGS_DIR):
            os.makedirs(_SETTINGS_DIR)
        path = _settings_path(tool_name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(settings_dict, f, indent=2)
        return True
    except Exception:
        return False
