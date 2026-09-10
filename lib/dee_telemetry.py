# -*- coding: utf-8 -*-
"""Lightweight usage telemetry - one row per tool run, sent to a Supabase
table, so usage (who installed the tool, how often, which buttons) is
visible in one place across every machine this extension runs on.

Design rules, in order of importance:
1. NEVER block or break the calling tool. Every failure here (no
   internet, Supabase down, a typo in the URL) is swallowed silently -
   whether telemetry succeeds or fails must be completely invisible to
   the person using the actual tool. The network call itself runs on a
   background thread so a slow/hanging connection can't stall the UI.
2. Identity is asked for ONCE per machine - a plain name/email via
   forms.ask_for_string (no password, no account), cached locally at
   TOKEN_CACHE_PATH's sibling .dee_identity.json. This function is only
   ever called from the very top of a tool's script.py, before that
   tool opens any window of its own - calling forms.ask_for_string from
   INSIDE another modal window crashes Revit (see
   feedback_no_nested_modal_ask_for_string.md); calling it first, before
   anything else runs, avoids that entirely.
3. The embedded Supabase key is the public "anon" key, safe to ship in
   code distributed to any PC - the table's Row Level Security policy
   allows ONLY inserting new rows for that role (no select/update/
   delete), so even a fully extracted key could never be used to read
   or tamper with anyone else's usage data, only add junk rows.
"""
import json
import os
import threading

import clr
clr.AddReference("System")
clr.AddReference("System.Net.Http")
from System.Net.Http import HttpClient, HttpRequestMessage, HttpMethod, StringContent
from System.Net.Http.Headers import MediaTypeHeaderValue

# archMohk's "Dee-extension-telemetry" Supabase project (a fresh project
# - a much older, long-dormant one was tried first and abandoned after
# extensive debugging: see project_dee_telemetry.md for the full story,
# including the real fix that mattered - return=minimal (below) avoids
# ever needing a SELECT policy for anon, which is also just the right
# design for this table anyway (insert-only, by intent).
# SUPABASE_ANON_KEY is a "publishable" key (Supabase's current name for
# what used to be called the anon key) - explicitly documented by
# Supabase as "safe to use in a browser if you have enabled RLS... and
# configured policies", which is exactly this table's setup (insert-only
# for the anon role, verified via the Policies page before this was
# wired in anywhere).
SUPABASE_URL = "https://gmuvmeolvkgqkmwvfbjj.supabase.co"
SUPABASE_ANON_KEY = "sb_publishable_uAWO1Q4nFmhDZ-wuMFMClw_-ezcJQtL"
TABLE_NAME = "tool_usage"

_THIS_DIR = os.path.dirname(__file__)
_IDENTITY_PATH = os.path.join(_THIS_DIR, ".dee_identity.json")

_client = HttpClient()


def _load_identity():
    if os.path.exists(_IDENTITY_PATH):
        try:
            with open(_IDENTITY_PATH, "r") as f:
                data = json.load(f)
            name = (data.get("name") or "").strip()
            if name:
                return name
        except Exception:
            pass
    return None


def _save_identity(name):
    try:
        with open(_IDENTITY_PATH, "w") as f:
            json.dump({"name": name}, f)
    except Exception:
        pass


def _windows_username():
    try:
        return os.environ.get("USERNAME") or "unknown"
    except Exception:
        return "unknown"


def _machine_name():
    try:
        return os.environ.get("COMPUTERNAME") or "unknown"
    except Exception:
        return "unknown"


def get_or_prompt_identity():
    """Returns the saved name/email, prompting ONCE (a single text-entry
    dialog) the very first time any Dee tool runs on this machine. Must
    be called before the calling tool opens any window of its own - see
    this module's docstring. Never raises: if the prompt itself fails
    for any reason, falls back to the Windows username so a usage row
    can still be recorded rather than lost."""
    name = _load_identity()
    if name:
        return name

    try:
        from pyrevit import forms
        entered = forms.ask_for_string(
            default="",
            prompt="First time using a Dee tool on this PC - enter your "
                   "name or email (asked once, then remembered):",
            title="Dee.extension"
        )
        name = (entered or "").strip()
    except Exception:
        name = ""

    if not name:
        name = _windows_username()

    _save_identity(name)
    return name


def _send(tool_name, user_name):
    try:
        if not SUPABASE_URL or not SUPABASE_ANON_KEY:
            return
        url = "{0}/rest/v1/{1}".format(SUPABASE_URL.rstrip("/"), TABLE_NAME)
        body = json.dumps({
            "tool_name": tool_name,
            "user_name": user_name,
            "windows_username": _windows_username(),
            "machine_name": _machine_name(),
        })
        request = HttpRequestMessage(HttpMethod.Post, url)
        request.Headers.Add("apikey", SUPABASE_ANON_KEY)
        request.Headers.Add("Authorization", "Bearer " + SUPABASE_ANON_KEY)
        request.Headers.Add("Prefer", "return=minimal")
        content = StringContent(body)
        content.Headers.ContentType = MediaTypeHeaderValue("application/json")
        request.Content = content
        _client.SendAsync(request).Result
    except Exception:
        pass


def log_usage(tool_name):
    """Call this ONE line, first thing, at the top of a tool's script.py
    (before any window/forms call of its own). Resolves identity
    synchronously (near-instant after the first run on a machine, since
    it's then just a local file read) and fires the actual network POST
    on a background thread so the tool's own UI never waits on it."""
    try:
        user_name = get_or_prompt_identity()
    except Exception:
        user_name = _windows_username()

    t = threading.Thread(target=_send, args=(tool_name, user_name))
    t.daemon = True
    t.start()
