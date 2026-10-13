# -*- coding: utf-8 -*-
"""3-legged OAuth helper for Autodesk Platform Services (APS).

Caches the access token on disk so the user isn't prompted to log in
on every single run within the token's lifetime.

Uses Authorization Code Grant with PKCE (RFC 7636), NOT a client
secret - this is what APS itself recommends for a desktop app given to
many different people, each logging in with their own Autodesk
account: a client_secret can't stay secret once it ships inside
software end users run locally on machines you don't control
(confirmed via aps.autodesk.com/blog/new-application-types - "Desktop,
Mobile, Single-Page App" is the registration type built for exactly
this, and it uses PKCE instead of a secret). Switched from a
client_secret flow 2026-09-11 for that reason - acc_config.json only
needs client_id/redirect_uri/region now, so it is no longer sensitive
(an old file with a leftover client_secret field still works fine,
that field is just ignored). The APS app registration itself must be
the "Desktop, Mobile, Single-Page App" type for this to work - a
"Traditional Web App" registration (what a client_secret-based flow
requires) will reject a token exchange that has no client_secret.

--------------------------------------------------------------------
Named account slots (DeeAccount.pushbutton) - Work vs Personal
--------------------------------------------------------------------
Every one of the ~19 tools that call get_access_token() does so with
no `account` argument - this module resolves "which account" to
`get_active_account()` internally by default, so adding a second,
independently-cached Autodesk identity (e.g. the user's own personal
account, kept separate from their work/company one) needed ZERO
changes at any of those call sites. The DEFAULT_ACCOUNT slot keeps
using the exact same TOKEN_CACHE_PATH filename this module always has,
so an existing single-account setup is completely undisturbed; only a
second, explicitly-chosen slot gets its own separate cache file.

This does NOT implement SSO/SAML/OIDC federation (Microsoft Entra ID,
Okta, Google Workspace, etc.) - every sign-in still opens Autodesk's
own hosted login page (APS_AUTH_URL) exactly as before. Whether that
page redirects to a company identity provider depends entirely on how
the signing-in Autodesk account's organization is configured on
Autodesk's own side (Autodesk Admin Console) - nothing this extension
does or could do changes that. What this adds is purely "remember more
than one already-authenticated Autodesk account, separately."
"""
import base64
import hashlib
import json
import os
import time
import webbrowser

import clr
clr.AddReference("System")
clr.AddReference("System.Net.Http")
import System
from System.Net import HttpListener
from System.Net.Http import HttpClient, FormUrlEncodedContent
from System.Collections.Generic import Dictionary

import deew_settings

APS_AUTH_URL = "https://developer.api.autodesk.com/authentication/v2/authorize"
APS_TOKEN_URL = "https://developer.api.autodesk.com/authentication/v2/token"
SCOPES = "data:read data:write account:read"

_THIS_DIR = os.path.dirname(__file__)
_EXTENSION_DIR = os.path.dirname(_THIS_DIR)
CONFIG_PATH = os.path.join(_EXTENSION_DIR, "acc_config.json")
TOKEN_CACHE_PATH = os.path.join(_THIS_DIR, ".acc_token_cache.json")

DEFAULT_ACCOUNT = "work"
_ACCOUNT_SWITCH_SETTINGS = "acc_account_switch"


def get_active_account():
    """Which named account slot get_access_token() uses when a caller
    doesn't pass one explicitly - i.e. every existing call site in this
    extension. Defaults to DEFAULT_ACCOUNT (the original, single-account
    behavior) until a user explicitly switches via DeeAccount.pushbutton."""
    data = deew_settings.load(_ACCOUNT_SWITCH_SETTINGS, {"active_account": DEFAULT_ACCOUNT})
    return data.get("active_account") or DEFAULT_ACCOUNT


def set_active_account(account):
    deew_settings.save(_ACCOUNT_SWITCH_SETTINGS, {"active_account": account})


def _token_cache_path(account):
    """DEFAULT_ACCOUNT keeps the exact original filename - an existing
    single-account setup is never disrupted by this feature existing.
    Any other named slot gets its own separate file."""
    if account == DEFAULT_ACCOUNT:
        return TOKEN_CACHE_PATH
    safe = "".join(c for c in account if c.isalnum() or c in ("_", "-")) or "account"
    return os.path.join(_THIS_DIR, ".acc_token_cache_{0}.json".format(safe))


def _load_config():
    if not os.path.exists(CONFIG_PATH):
        raise Exception(
            "acc_config.json not found. This tool needs an Autodesk "
            "Platform Services config file at:\n{0}\n\n"
            "Copy acc_config.example.json (in the extension's root folder) "
            "to acc_config.json and fill in your own client_id - see that "
            "file's own comment for where to get one.".format(CONFIG_PATH))
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def is_configured():
    """Cheap, local-only readiness check: does acc_config.json exist
    and have a non-empty client_id? Never touches the network and
    never triggers the PKCE login flow - purely for display purposes
    (e.g. the branding bar's "ACC: Ready/Not configured" indicator),
    answering "is this machine SET UP to attempt ACC at all", not
    "is the current login still valid" (that would need a network
    call and isn't worth making just to render a status line)."""
    try:
        config = _load_config()
        return bool(config.get("client_id"))
    except Exception:
        return False


def _load_cached_token(account):
    path = _token_cache_path(account)
    if os.path.exists(path):
        with open(path, "r") as f:
            data = json.load(f)
        if data.get("expires_at", 0) > time.time() + 60:
            return data.get("access_token")
    return None


def _save_token(token, expires_in, account):
    data = {"access_token": token, "expires_at": time.time() + int(expires_in)}
    with open(_token_cache_path(account), "w") as f:
        json.dump(data, f)


def account_status(account):
    """True if a valid, non-expired cached token already exists for
    this account slot - never touches the network and never triggers a
    login, same "cheap, local-only, display-purposes-only" spirit as
    is_configured() above. For the DeeAccount switcher UI."""
    try:
        return _load_cached_token(account) is not None
    except Exception:
        return False


def sign_out(account):
    """Deletes a slot's cached token - best-effort, never raises."""
    try:
        path = _token_cache_path(account)
        if os.path.exists(path):
            os.remove(path)
        return True
    except Exception:
        return False


def _b64url(raw):
    """Base64url per RFC 7636 - standard base64 with +/ swapped for -_ and
    the "=" padding stripped (the spec requires no padding, plain
    base64's does)."""
    return base64.urlsafe_b64encode(raw).rstrip("=")


def _new_pkce_pair():
    """Returns (code_verifier, code_challenge) per RFC 7636. The verifier
    is 43 URL-safe characters from 32 cryptographically random bytes
    (RFC 7636 requires 43-128 chars); the challenge is
    BASE64URL(SHA256(ASCII(verifier))) - hashed over the verifier
    STRING's own bytes, not the raw random bytes it was derived from."""
    verifier = _b64url(os.urandom(32))
    challenge = _b64url(hashlib.sha256(verifier).digest())
    return verifier, challenge


def _listen_for_code(redirect_uri):
    """Spins up a local HTTP listener and blocks until the OAuth
    redirect with the authorization code comes back."""
    prefix = redirect_uri if redirect_uri.endswith("/") else redirect_uri + "/"
    listener = HttpListener()
    listener.Prefixes.Add(prefix)
    listener.Start()

    try:
        context = listener.GetContext()  # blocks until the browser redirects here
        request = context.Request
        query = request.Url.Query  # e.g. "?code=xxx&state=..."

        code = None
        if query:
            for pair in query.lstrip("?").split("&"):
                if pair.startswith("code="):
                    code = pair.split("=", 1)[1]

        response = context.Response
        html = "<html><body><h2>Authentication complete. You can close this tab.</h2></body></html>"
        buffer = System.Text.Encoding.UTF8.GetBytes(html)
        response.ContentLength64 = buffer.Length
        response.OutputStream.Write(buffer, 0, buffer.Length)
        response.OutputStream.Close()
    finally:
        listener.Stop()

    return code


def get_access_token(force_login=False, account=None):
    """account=None (every existing call site) resolves to whichever
    slot is currently active (get_active_account()) - see the module
    docstring's "Named account slots" section. Passing an explicit
    account lets DeeAccount.pushbutton (re)authenticate one specific
    slot regardless of which one is active."""
    if account is None:
        account = get_active_account()
    if not force_login:
        cached = _load_cached_token(account)
        if cached:
            return cached

    config = _load_config()
    client_id = config["client_id"]
    redirect_uri = config["redirect_uri"]

    code_verifier, code_challenge = _new_pkce_pair()

    auth_url = (
        "{0}?response_type=code&client_id={1}&redirect_uri={2}&scope={3}"
        "&code_challenge={4}&code_challenge_method=S256"
        .format(APS_AUTH_URL, client_id, redirect_uri, SCOPES.replace(" ", "%20"),
                code_challenge)
    )
    webbrowser.open(auth_url)

    code = _listen_for_code(redirect_uri)
    if not code:
        raise Exception("Did not receive an authorization code from APS login.")

    client = HttpClient()
    pairs = Dictionary[str, str]()
    pairs["grant_type"] = "authorization_code"
    pairs["code"] = code
    pairs["client_id"] = client_id
    pairs["redirect_uri"] = redirect_uri
    pairs["code_verifier"] = code_verifier
    content = FormUrlEncodedContent(pairs)

    response = client.PostAsync(APS_TOKEN_URL, content).Result
    body = response.Content.ReadAsStringAsync().Result
    if not response.IsSuccessStatusCode:
        raise Exception("Token exchange failed: {0}".format(body))

    data = json.loads(body)
    token = data["access_token"]
    _save_token(token, data.get("expires_in", 3600), account)
    return token
