# -*- coding: utf-8 -*-
"""
dee_broadcast_service
DeeCall's background half: lets the owner (allowed_users.is_admin) send a
plain-text message that shows up as a toast on every user's PC, the next
time their own Idling watcher checks in. Same overall shape as
lib/dee_prayer_service.py - a separate UIApplication.Idling subscription,
throttled, reusing lib/dee_toast.py for the actual popup - but checked
less often (broadcasts aren't time-critical the way prayer times are).

--------------------------------------------------------------------
Why sending needs a server-side check, not just a client-side one
--------------------------------------------------------------------
Every user runs the exact same shipped code with the same embedded
PUBLIC Supabase key - "only the owner can send" can't be enforced by a
plain insert policy, since anyone with the extension has that key. The
actual gate is server-side: a SECURITY DEFINER RPC,
send_broadcast_message(sender_email, message_text), checks
allowed_users.is_admin (the same flag added earlier this session for the
pending-new-user alert) before inserting anything - a non-admin caller's
message is silently rejected, never written. send_message() below just
calls that RPC and reports whether it actually went through.

--------------------------------------------------------------------
Delivery: no per-user server-side read-tracking needed at all
--------------------------------------------------------------------
Each PC keeps its own "highest message id already shown" locally (via
lib/deew_settings.py, tool name "DeeBroadcast"). Every throttled check
asks Supabase for anything with a higher id (a plain, harmless anon
SELECT - broadcast_messages has no write access for anon, only read,
since these are meant to be seen by everyone by design), shows each new
row as a toast, advances the local high-water mark. Simpler than
per-user "seen" rows server-side, and consistent with how every other
per-PC preference in this extension is stored.
"""
import datetime
import json

import clr
clr.AddReference("System")
clr.AddReference("System.Net.Http")
clr.AddReference("PresentationCore")

from System import TimeSpan
from System.Net.Http import HttpClient, HttpRequestMessage, HttpMethod, StringContent
from System.Net.Http.Headers import MediaTypeHeaderValue
from System.Windows.Media import Color

import deew_settings
import dee_toast
import dee_telemetry

TOOL_NAME = "DeeBroadcast"
_CHECK_INTERVAL = datetime.timedelta(seconds=60)

# Distinct from prayer's green ("now")/orange ("reminder") - a plain
# announcement isn't either of those, so it gets its own accent rather
# than borrowing one of theirs.
_ACCENT = Color.FromRgb(0x4A, 0x90, 0xE2)

_client = HttpClient()
_client.Timeout = TimeSpan.FromSeconds(8)

_state = {"last_check": None}


def send_message(sender_email, message_text):
    """Synchronous, like dee_telemetry.refresh_status() - this is a
    deliberate, user-initiated action (the Send button), not a
    background poll, so the caller needs to know success/failure right
    away. Returns (ok, reason) - reason is None on success, or a short
    string ("not_admin", "empty_message", or the raw error) on failure.
    Never raises."""
    try:
        url = "{0}/rest/v1/rpc/send_broadcast_message".format(
            dee_telemetry.SUPABASE_URL.rstrip("/"))
        body = json.dumps({"sender_email": sender_email, "message_text": message_text})
        request = HttpRequestMessage(HttpMethod.Post, url)
        request.Headers.Add("apikey", dee_telemetry.SUPABASE_ANON_KEY)
        request.Headers.Add("Authorization", "Bearer " + dee_telemetry.SUPABASE_ANON_KEY)
        content = StringContent(body)
        content.Headers.ContentType = MediaTypeHeaderValue("application/json")
        request.Content = content
        response = _client.SendAsync(request).Result
        response_body = response.Content.ReadAsStringAsync().Result
        if not response.IsSuccessStatusCode:
            return False, "http_error: {0}".format(response_body)
        result = json.loads(response_body)
        if isinstance(result, dict) and result.get("ok"):
            return True, None
        reason = result.get("reason") if isinstance(result, dict) else None
        return False, reason or "unknown"
    except Exception as e:
        return False, str(e)


def _fetch_new_messages(since_id):
    url = ("{0}/rest/v1/broadcast_messages?id=gt.{1}&order=id.asc"
           "&select=id,sender_email,message_text,created_at"
           .format(dee_telemetry.SUPABASE_URL.rstrip("/"), since_id))
    try:
        request = HttpRequestMessage(HttpMethod.Get, url)
        request.Headers.Add("apikey", dee_telemetry.SUPABASE_ANON_KEY)
        request.Headers.Add("Authorization", "Bearer " + dee_telemetry.SUPABASE_ANON_KEY)
        response = _client.SendAsync(request).Result
        if not response.IsSuccessStatusCode:
            return None
        body = response.Content.ReadAsStringAsync().Result
        rows = json.loads(body)
        return rows if isinstance(rows, list) else None
    except Exception:
        return None


def _check_and_notify():
    now = datetime.datetime.now()
    last = _state["last_check"]
    if last is not None and (now - last) < _CHECK_INTERVAL:
        return
    _state["last_check"] = now

    settings = deew_settings.load(TOOL_NAME, {"last_seen_id": 0})
    since_id = settings.get("last_seen_id", 0)

    rows = _fetch_new_messages(since_id)
    if not rows:
        return

    max_id = since_id
    for row in rows:
        rid = row.get("id")
        text = (row.get("message_text") or "").strip()
        if text:
            dee_toast.show_toast(
                u"ANNOUNCEMENT — DEE.EXTENSION", text, u"", _ACCENT)
        if isinstance(rid, (int, float)) and rid > max_id:
            max_id = rid

    if max_id != since_id:
        settings["last_seen_id"] = max_id
        deew_settings.save(TOOL_NAME, settings)


def _on_idling(sender, args):
    try:
        _check_and_notify()
    except Exception:
        pass


def start_watching(uiapp):
    """Call once from startup.py. `-=` before `+=` makes this safe to
    call again on a Reload without stacking duplicate subscriptions -
    same pattern as dee_prayer_service.start_watching()."""
    try:
        uiapp.Idling -= _on_idling
    except Exception:
        pass
    try:
        uiapp.Idling += _on_idling
    except Exception:
        pass
