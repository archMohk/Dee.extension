# -*- coding: utf-8 -*-
"""
dee_broadcast_service
DeeCall's background half: lets the owner (allowed_users.is_admin) send a
message - text, optionally with an image, optionally requiring the
recipient to click Close instead of auto-dismissing, optionally aimed at
specific people instead of everyone - that shows up as a toast on every
targeted user's PC, the next time their own Idling watcher checks in.
Same overall shape as lib/dee_prayer_service.py - a separate
UIApplication.Idling subscription, throttled, reusing lib/dee_toast.py
for the actual popup - but checked less often (broadcasts aren't
time-critical the way prayer times are).

--------------------------------------------------------------------
Targeting specific people: filtered on the RECEIVING end, not the DB
--------------------------------------------------------------------
target_emails (None = everyone, same as before this feature existed; a
list of lowercased emails = only those people) is stored on the row and
handed back to every PC exactly like every other column, via the same
already-public anon SELECT - broadcast_messages was never meant to hide
its content, only the TOAST is gated to the right audience. Each PC's
_check_and_notify() decides for itself whether to actually show a toast
by comparing target_emails against its own dee_telemetry
get_cached_identity() - honest tradeoff: a message's TEXT is technically
fetchable by anyone who queries the REST API directly (same as before
targeting existed), "targeted" only means "only the right people see it
pop up as a toast through the normal app". Good enough for an internal
team announcement tool; not meant to carry anything actually
confidential.

Picking recipients needs the team roster, which - unlike
broadcast_messages - was never meant to be public (it's basically
allowed_users), so DeeCall's compose window calls a THIRD RPC,
list_broadcast_recipients(admin_email), gated by the exact same
is_admin check as sending. Same self-reported-email trust model as
send_broadcast_message itself (see that RPC's own comment) - not a new
or weaker boundary, just the same one applied to a second endpoint.

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

--------------------------------------------------------------------
Images don't stay in the database forever
--------------------------------------------------------------------
An attached image is only meant to reach people while it's still
useful, not accumulate in the table indefinitely - _cleanup_old_images()
calls a second RPC, cleanup_old_broadcast_images(), once/day per PC
(its own throttle, separate from the message-check one above) that
nulls out image_base64 for any message older than 3 days. The message
TEXT is never touched, only the image. Several PCs calling this same
day is harmless - the RPC's own WHERE clause makes a repeat call a
no-op, so there's no need to coordinate who "owns" the cleanup.
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


def send_message(sender_email, message_text, requires_ack=False, image_base64=None,
                  target_emails=None):
    """Synchronous, like dee_telemetry.refresh_status() - this is a
    deliberate, user-initiated action (the Send button), not a
    background poll, so the caller needs to know success/failure right
    away. requires_ack/image_base64/target_emails are the sender's own
    choices from the DeeCall compose window - target_emails is None for
    "everyone" (unchanged default) or a list of emails for specific
    recipients only; see dee_toast.show_toast and _check_and_notify for
    what each does on the receiving end. Returns (ok, reason) - reason is
    None on success, or a short string ("not_admin", "empty_message",
    "image_too_large", "empty_targets", or the raw error) on failure.
    Never raises."""
    try:
        url = "{0}/rest/v1/rpc/send_broadcast_message".format(
            dee_telemetry.SUPABASE_URL.rstrip("/"))
        body = json.dumps({
            "sender_email": sender_email, "message_text": message_text,
            "requires_ack": bool(requires_ack), "image_base64": image_base64,
            "target_emails": target_emails,
        })
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


def list_recipients(admin_email):
    """Synchronous - the DeeCall window calls this once, lazily, only if
    the admin actually switches to "Specific users" (never on every
    open, since most sends are still "everyone"). Returns
    (ok, users, reason) - users is a list of {"email", "is_admin",
    "enabled"} dicts on success, [] on failure. Never raises."""
    try:
        url = "{0}/rest/v1/rpc/list_broadcast_recipients".format(
            dee_telemetry.SUPABASE_URL.rstrip("/"))
        body = json.dumps({"admin_email": admin_email})
        request = HttpRequestMessage(HttpMethod.Post, url)
        request.Headers.Add("apikey", dee_telemetry.SUPABASE_ANON_KEY)
        request.Headers.Add("Authorization", "Bearer " + dee_telemetry.SUPABASE_ANON_KEY)
        content = StringContent(body)
        content.Headers.ContentType = MediaTypeHeaderValue("application/json")
        request.Content = content
        response = _client.SendAsync(request).Result
        response_body = response.Content.ReadAsStringAsync().Result
        if not response.IsSuccessStatusCode:
            return False, [], "http_error: {0}".format(response_body)
        result = json.loads(response_body)
        if isinstance(result, dict) and result.get("ok"):
            return True, result.get("users") or [], None
        reason = result.get("reason") if isinstance(result, dict) else None
        return False, [], reason or "unknown"
    except Exception as e:
        return False, [], str(e)


def _cleanup_old_images():
    """Best-effort, throttled to once/day per PC via its own settings
    key (separate from last_seen_id, so a cleanup failure never blocks
    message delivery). Harmless if several PCs' watchers all call this
    the same day - see module docstring. Never raises."""
    settings = deew_settings.load(TOOL_NAME, {"last_seen_id": 0})
    today_str = datetime.date.today().isoformat()
    if settings.get("last_cleanup_date") == today_str:
        return
    try:
        url = "{0}/rest/v1/rpc/cleanup_old_broadcast_images".format(
            dee_telemetry.SUPABASE_URL.rstrip("/"))
        request = HttpRequestMessage(HttpMethod.Post, url)
        request.Headers.Add("apikey", dee_telemetry.SUPABASE_ANON_KEY)
        request.Headers.Add("Authorization", "Bearer " + dee_telemetry.SUPABASE_ANON_KEY)
        content = StringContent("{}")
        content.Headers.ContentType = MediaTypeHeaderValue("application/json")
        request.Content = content
        _client.SendAsync(request).Result
    except Exception:
        pass
    settings["last_cleanup_date"] = today_str
    deew_settings.save(TOOL_NAME, settings)


def _fetch_new_messages(since_id):
    url = ("{0}/rest/v1/broadcast_messages?id=gt.{1}&order=id.asc"
           "&select=id,sender_email,message_text,created_at,requires_ack,image_base64,target_emails"
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

    _cleanup_old_images()

    settings = deew_settings.load(TOOL_NAME, {"last_seen_id": 0})
    since_id = settings.get("last_seen_id", 0)

    rows = _fetch_new_messages(since_id)
    if not rows:
        return

    max_id = since_id
    my_email = None  # resolved lazily - only needed if a targeted row shows up
    for row in rows:
        rid = row.get("id")
        text = (row.get("message_text") or "").strip()
        targets = row.get("target_emails")
        show_it = True
        if targets:
            if my_email is None:
                my_email = (dee_telemetry.get_cached_identity() or u"").strip().lower()
            normalized_targets = [(t or u"").strip().lower() for t in targets]
            show_it = bool(my_email) and my_email in normalized_targets
        if text and show_it:
            dee_toast.show_toast(
                u"ANNOUNCEMENT — DEE.EXTENSION", text, u"", _ACCENT,
                requires_ack=bool(row.get("requires_ack")),
                image_base64=row.get("image_base64"))
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
