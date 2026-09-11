# -*- coding: utf-8 -*-
"""Usage telemetry AND access control for Dee.extension, backed by Supabase.

Two things live in this one module because they share the same identity
resolution and the same "call this one line at the top of a tool" shape:

1. **Telemetry** - one row per tool run (who, which tool, when), so usage
   is visible in one place across every machine this extension runs on.
2. **Access control** - `check_access()` is a remote kill-switch. Every
   email is either enabled or disabled in the `allowed_users` Supabase
   table; a brand-new/never-seen email is DISABLED by default (opt-in,
   not opt-out - confirmed explicitly, not assumed) and gets a pending
   row created automatically so the owner can review and enable it from
   the dashboard. Emails are the source of truth: only the identity
   prompt is what changed from "name or email" to explicitly "email".

Design rules, in order of importance:
1. `check_access()` is a deliberate exception to "never block" - a
   license gate that doesn't actually gate anything is pointless. If the
   check itself can't be completed (no internet, Supabase down), it
   FAILS CLOSED: the tool is blocked rather than silently allowed -
   confirmed explicitly with the tool owner, not assumed, since the
   opposite (fail-open) is just as defensible a default and either one
   is a real product decision, not an engineering detail.
   `log_usage()` alone (used internally by check_access on success)
   remains fire-and-forget/never-blocking - only the access CHECK itself
   is synchronous by necessity.
2. Identity is asked for ONCE per machine - a plain email via
   forms.ask_for_string (no password, no account), cached locally at
   .dee_identity.json next to this file. This function is only ever
   called from the very top of a tool's script.py, before that tool
   opens any window of its own - calling forms.ask_for_string from
   INSIDE another modal window crashes Revit (see
   feedback_no_nested_modal_ask_for_string.md); calling it first, before
   anything else runs, avoids that entirely.
3. The embedded Supabase key is the public "publishable" key, safe to
   ship in code distributed to any PC. tool_usage's RLS policy allows
   ONLY inserting new rows for anon (no select/update/delete - a leaked
   key can never read or tamper with usage data). allowed_users has NO
   direct anon access at all - anon can only call check_user_access(),
   a SECURITY DEFINER function that returns a single boolean for one
   email, never exposing the table itself - so a leaked key can check
   "is this one email enabled" and nothing else, not enumerate or read
   anyone's status.
"""
import json
import os
import sys
import threading

import clr
clr.AddReference("System")
clr.AddReference("System.Net.Http")
from System.Net.Http import HttpClient, HttpRequestMessage, HttpMethod, StringContent
from System.Net.Http.Headers import MediaTypeHeaderValue

# archMohk's "Dee-extension-telemetry" Supabase project (a fresh project
# - a much older, long-dormant one was tried first and abandoned after
# extensive debugging: see project_dee_telemetry.md for the full story,
# including the real fix that mattered for inserts - return=minimal
# (below) avoids ever needing a SELECT policy for anon, which is also
# just the right design for tool_usage anyway (insert-only, by intent).
SUPABASE_URL = "https://gmuvmeolvkgqkmwvfbjj.supabase.co"
SUPABASE_ANON_KEY = "sb_publishable_uAWO1Q4nFmhDZ-wuMFMClw_-ezcJQtL"
TABLE_NAME = "tool_usage"

_THIS_DIR = os.path.dirname(__file__)
_IDENTITY_PATH = os.path.join(_THIS_DIR, ".dee_identity.json")
_STATUS_PATH = os.path.join(_THIS_DIR, ".dee_access_status.json")

_client = HttpClient()


def get_cached_identity():
    """Returns the saved email WITHOUT prompting - None if never set.
    Safe to call from any context (e.g. the branding bar) since it
    never opens a dialog and never blocks or touches the network."""
    return _load_identity()


def _save_status(result):
    """Best-effort local cache of the last check_access() result, so
    something purely DISPLAY-oriented (the branding bar) can show the
    current standing without a second network round-trip on every
    window open. Never raises - losing this is harmless, the bar just
    shows nothing until the next successful check."""
    try:
        with open(_STATUS_PATH, "w") as f:
            json.dump(result, f)
    except Exception:
        pass


def load_cached_status():
    """Returns the last check_access() result dict (allowed/reason/
    warning/expires_at), or None if never checked yet or unreadable.
    Purely local/cached - never hits the network - so it must never be
    used to GATE access, only to display it."""
    try:
        if os.path.exists(_STATUS_PATH):
            with open(_STATUS_PATH, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return None


def status_summary():
    """A short, display-ready status string for the branding bar, e.g.
    "Active", "Active - renew soon", or "Access: <reason>" for the rare
    case a stale cached status shows denied (a genuinely denied user's
    tool window never opens in the first place, since check_access()
    exits before that - this branch is defensive, not a normal path).
    Returns "" if check_access() has never run yet (e.g. this module
    was imported some other way)."""
    status = load_cached_status()
    if not status:
        return ""
    if not status.get("allowed"):
        headline = _DENIAL_HEADLINES.get(status.get("reason"), "inactive")
        return "Access: " + headline
    if status.get("warning"):
        return "Active - renew soon"
    return "Active"


def _load_identity():
    if os.path.exists(_IDENTITY_PATH):
        try:
            with open(_IDENTITY_PATH, "r") as f:
                data = json.load(f)
            email = (data.get("email") or data.get("name") or "").strip().lower()
            if email:
                return email
        except Exception:
            pass
    return None


def _save_identity(email):
    try:
        with open(_IDENTITY_PATH, "w") as f:
            json.dump({"email": email}, f)
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
    """Returns the saved email, prompting ONCE (a single text-entry
    dialog) the very first time any Dee tool runs on this machine. Must
    be called before the calling tool opens any window of its own - see
    this module's docstring. Never raises: if the prompt itself fails
    for any reason, falls back to the Windows username so a usage row
    can still be recorded rather than lost (that fallback value will
    never match a real email in allowed_users, so check_access() would
    correctly treat it as an unrecognized/disabled identity)."""
    email = _load_identity()
    if email:
        return email

    try:
        from pyrevit import forms
        entered = forms.ask_for_string(
            default="",
            prompt="First time using a Dee tool on this PC - enter your "
                   "email (asked once, then remembered):",
            title="Dee.extension"
        )
        # Lowercased here (not just in the SQL check_user_access function,
        # which already normalizes case for the access DECISION) so the
        # value actually STORED in tool_usage matches allowed_users
        # consistently - otherwise "Name@x.com" vs "name@x.com" pass the
        # same access check but show up as two different people in the
        # dashboard (live-caught: exactly this happened on first use).
        email = (entered or "").strip().lower()
    except Exception:
        email = ""

    if not email:
        email = _windows_username()

    _save_identity(email)
    return email


def _send(tool_name, user_email):
    try:
        if not SUPABASE_URL or not SUPABASE_ANON_KEY:
            return
        url = "{0}/rest/v1/{1}".format(SUPABASE_URL.rstrip("/"), TABLE_NAME)
        body = json.dumps({
            "tool_name": tool_name,
            "user_name": user_email,
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
    """Fire-and-forget usage log - never blocks, never raises. Called
    internally by check_access() on a successful (allowed) check; also
    safe to call standalone if a tool ever needs logging without the
    access gate for some reason."""
    try:
        user_email = get_or_prompt_identity()
    except Exception:
        user_email = _windows_username()

    t = threading.Thread(target=_send, args=(tool_name, user_email))
    t.daemon = True
    t.start()


CONTACT_INFO = (
    "WhatsApp: https://wa.me/971545462978\n"
    "Website: https://www.archmkd.com"
)

# Keyed on the "reason" field check_user_access() returns for a denial -
# see that SQL function's own definition for the exact set of reasons.
_DENIAL_HEADLINES = {
    "not_registered": "This email has not been set up for Dee.extension access yet.",
    "disabled": "Your access to Dee.extension tools is not currently active.",
    "expired": "Your Dee.extension subscription has expired.",
}


def _check_user_access_sync(email):
    """Calls the check_user_access(text) RPC. Returns the parsed response
    dict - {"allowed": bool, "reason": str, "warning": str|None,
    "expires_at": str|None} - see that SQL function's own definition for
    exactly what each field means. Raises on any failure (network,
    non-2xx, bad/unexpected JSON) so the caller can tell 'checked and
    disallowed' apart from 'could not check at all' - those two cases are
    handled differently by check_access() below."""
    url = "{0}/rest/v1/rpc/check_user_access".format(SUPABASE_URL.rstrip("/"))
    body = json.dumps({"check_email": email})
    request = HttpRequestMessage(HttpMethod.Post, url)
    request.Headers.Add("apikey", SUPABASE_ANON_KEY)
    request.Headers.Add("Authorization", "Bearer " + SUPABASE_ANON_KEY)
    content = StringContent(body)
    content.Headers.ContentType = MediaTypeHeaderValue("application/json")
    request.Content = content
    response = _client.SendAsync(request).Result
    response_body = response.Content.ReadAsStringAsync().Result
    if not response.IsSuccessStatusCode:
        raise Exception("check_user_access failed: {0}".format(response_body))
    result = json.loads(response_body)
    if not isinstance(result, dict) or "allowed" not in result:
        raise Exception("unexpected check_user_access response: {0}".format(response_body))
    return result


def check_access(tool_name):
    """Call this ONE line, first thing, at the top of a tool's script.py
    (before any window/forms call of its own - see get_or_prompt_identity's
    docstring for why). Resolves the user's email (prompting once per
    machine), checks it against allowed_users, and either returns
    normally (access granted - also fires log_usage() in the background,
    and shows a one-time non-blocking reminder if the subscription is
    expiring within 30 days) or ends the script via sys.exit() after
    showing why: not registered / disabled / expired, or undeterminable
    (network/Supabase problem - fails CLOSED by design, see this module's
    docstring point 1). A denied user gets an "Re-enter Email" option in
    case they mistyped it the first time - clears the local cache and
    re-checks rather than leaving them stuck with a typo forever."""
    from pyrevit import forms

    try:
        email = get_or_prompt_identity()
    except Exception:
        email = _windows_username()

    try:
        result = _check_user_access_sync(email)
    except Exception:
        forms.alert(
            "Could not verify access to this tool - check your internet "
            "connection and try again.",
            title="Dee.extension - Access Check Failed")
        sys.exit()
        return

    _save_status(result)

    if not result.get("allowed"):
        headline = _DENIAL_HEADLINES.get(result.get("reason"), _DENIAL_HEADLINES["disabled"])
        choice = forms.alert(
            "{0}\n\nSigned in as: {1}\n\nContact us to activate or renew:\n{2}"
            .format(headline, email, CONTACT_INFO),
            title="Dee.extension - Access Required",
            options=["OK", "Re-enter Email"])
        if choice == "Re-enter Email":
            try:
                os.remove(_IDENTITY_PATH)
            except Exception:
                pass
            check_access(tool_name)
            return
        sys.exit()
        return

    warning = result.get("warning")
    if warning:
        forms.alert(warning, title="Dee.extension - Subscription Reminder")

    log_usage(tool_name)
