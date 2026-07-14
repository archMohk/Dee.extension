# -*- coding: utf-8 -*-
"""3-legged OAuth helper for Autodesk Platform Services (APS).

Caches the access token on disk so the user isn't prompted to log in
on every single run within the token's lifetime.
"""
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

APS_AUTH_URL = "https://developer.api.autodesk.com/authentication/v2/authorize"
APS_TOKEN_URL = "https://developer.api.autodesk.com/authentication/v2/token"
SCOPES = "data:read data:write account:read"

_THIS_DIR = os.path.dirname(__file__)
_EXTENSION_DIR = os.path.dirname(_THIS_DIR)
CONFIG_PATH = os.path.join(_EXTENSION_DIR, "acc_config.json")
TOKEN_CACHE_PATH = os.path.join(_THIS_DIR, ".acc_token_cache.json")


def _load_config():
    if not os.path.exists(CONFIG_PATH):
        raise Exception(
            "acc_config.json not found. This tool needs an Autodesk "
            "Platform Services config file at:\n{0}\n\n"
            "Ask your administrator for this file - it is not part of "
            "the public tool download.".format(CONFIG_PATH))
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def _load_cached_token():
    if os.path.exists(TOKEN_CACHE_PATH):
        with open(TOKEN_CACHE_PATH, "r") as f:
            data = json.load(f)
        if data.get("expires_at", 0) > time.time() + 60:
            return data.get("access_token")
    return None


def _save_token(token, expires_in):
    data = {"access_token": token, "expires_at": time.time() + int(expires_in)}
    with open(TOKEN_CACHE_PATH, "w") as f:
        json.dump(data, f)


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


def get_access_token(force_login=False):
    if not force_login:
        cached = _load_cached_token()
        if cached:
            return cached

    config = _load_config()
    client_id = config["client_id"]
    client_secret = config["client_secret"]
    redirect_uri = config["redirect_uri"]

    auth_url = (
        "{0}?response_type=code&client_id={1}&redirect_uri={2}&scope={3}"
        .format(APS_AUTH_URL, client_id, redirect_uri, SCOPES.replace(" ", "%20"))
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
    pairs["client_secret"] = client_secret
    pairs["redirect_uri"] = redirect_uri
    content = FormUrlEncodedContent(pairs)

    response = client.PostAsync(APS_TOKEN_URL, content).Result
    body = response.Content.ReadAsStringAsync().Result
    if not response.IsSuccessStatusCode:
        raise Exception("Token exchange failed: {0}".format(body))

    data = json.loads(body)
    token = data["access_token"]
    _save_token(token, data.get("expires_in", 3600))
    return token
