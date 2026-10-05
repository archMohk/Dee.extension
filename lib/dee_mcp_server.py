# -*- coding: utf-8 -*-
"""
dee_mcp_server
Start/stop lifecycle for DeeMCP's HTTP server - the piece
DeePack.tab/Tools.panel/DeeMCP.pushbutton/script.py calls when the user
clicks Start/Stop. Built on pyRevit's own `pyrevit.routes` module
(shipped with pyRevit itself, confirmed present on this machine before
writing this - see the approved plan for the research behind this
choice), which already solves the hard problem of marshaling an
HTTP request onto Revit's own main/API thread via its own internal
ExternalEvent bridge - this file registers ONE route and otherwise
only manages starting/stopping a plain HTTP listener, never touches
ExternalEvent directly.

--------------------------------------------------------------------
Why a directly-constructed RoutesServer, not routes.activate_server()
--------------------------------------------------------------------
`routes.activate_server()` is pyRevit's own public convenience
function, but it picks host/port from `pyrevit.userconfig.user_config.
routes_host` / `.routes_port` - global pyRevit settings shared with any
OTHER pyRevit routes usage on this machine, not something specific to
DeeMCP - and its default host, read from pyRevit's own compiled
defaults, is not guaranteed to be loopback-only (confirmed by reading
`RoutesServer.__str__`'s own `self.host or "0.0.0.0"` fallback - an
empty/unset host binds every interface). Given DeeMCP's security
requirement (loopback-only, always, no exceptions), this file
constructs `pyrevit.routes.server.server.RoutesServer` DIRECTLY instead,
passing host="127.0.0.1" explicitly every time - this is reaching one
level past `pyrevit.routes`'s official `__all__`-exported surface
(confirmed by reading routes/server/__init__.py's `__all__`, which does
not list `RoutesServer`), a deliberate, verified choice (by reading
pyRevit's actual installed source, not guessed), flagged here as
something to re-check if a future pyRevit version restructures that
module - a routing-level `add_route`/`API.route()` registration (the
PUBLIC, documented part of `routes`) still works unchanged regardless
of which RoutesServer instance ends up listening, since routes are
stored in routes' own global router dict independent of any one server
instance.

Also deliberately bypasses `serverinfo.register()` (which activate_
server() uses) - that function caches host/port to a per-Revit-process
pickle file and returns the CACHED value on a second call within the
same process, which would silently defeat a loopback-override attempt
on a second Start/Stop cycle in the same Revit session. Picking our own
port directly, every time, avoids that caching entirely.

--------------------------------------------------------------------
State across repeated button clicks
--------------------------------------------------------------------
Uses `pyrevit.coreutils.envvars` (`set_pyrevit_env_var`/
`get_pyrevit_env_var`) - pyRevit's own public, documented mechanism
(AppDomain.GetData/SetData-backed) for sharing state across separate
script executions within the same Revit session, the EXACT mechanism
`routes.activate_server()` itself already uses for its own analogous
"is a server already running" state. Chosen over a plain module-level
global specifically because it is unclear (and left here as a NEEDS
LIVE VERIFICATION note) whether a plain `lib/*.py` module's top-level
state reliably survives across separate clicks of a `persistent: true`
button the way `envvars` is documented to - using the proven mechanism
sidesteps the question entirely.

--------------------------------------------------------------------
Scope: no Server-Sent-Events / streaming (NEEDS LIVE VERIFICATION)
--------------------------------------------------------------------
pyRevit's routes HTTP handler writes exactly one `send_response` /
`end_headers` / `wfile.write` per request (confirmed by reading
routes/server/server.py's `_write_response`) - there is no chunked-
transfer or `text/event-stream` support. MCP's Streamable HTTP
transport allows a server to answer a request with a single plain JSON
body instead of opening an SSE stream (this is spec-legal, not a
workaround), so this is NOT a correctness gap for `tools/call` style
request/response use - but it does mean DeeMCP cannot push server-
initiated notifications to a connected client. If a real MCP client
turns out to require SSE specifically, the fix is a hand-rolled server
thread instead of `pyrevit.routes` for this one route - not something
this session can verify without a live client to test against.
"""
import os
import json
import uuid

from pyrevit import routes
from pyrevit.routes.server.server import RoutesServer
from pyrevit.coreutils import envvars

import dee_mcp_service
import dee_mcp_tools

API_NAME = "dee-mcp"
ROUTE_PATTERN = "/mcp"
DEFAULT_PORT = 8793
PORT_SEARCH_RANGE = 50
HOST = "127.0.0.1"

_ENV_SERVER = "DEEMCP_SERVER"
_ENV_TOKEN = "DEEMCP_TOKEN"
_ENV_PORT = "DEEMCP_PORT"
_ENV_COUNT = "DEEMCP_REQUEST_COUNT"

# This file lives at <extension root>/lib/dee_mcp_server.py, so one
# dirname up from lib/ is the extension root - the same folder this
# developer's own Claude Code session already uses as its project
# directory, which is exactly why writing .mcp.json here is useful:
# a Claude Code session opened on this same folder picks it up with no
# manual copy/paste. For a DIFFERENT Claude Code project folder, this
# file would need to be copied there instead - not something DeeMCP can
# know about or reach on its own.
_EXTENSION_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MCP_JSON_PATH = os.path.join(_EXTENSION_ROOT, ".mcp.json")
_GITIGNORE_PATH = os.path.join(_EXTENSION_ROOT, ".gitignore")

_api = routes.API(API_NAME)
_api.route(ROUTE_PATTERN, methods=["POST"])(dee_mcp_service.mcp_endpoint)


def _ensure_gitignored():
    """Best-effort, idempotent: adds a .mcp.json entry to .gitignore if
    one isn't already there, so a fresh git checkout of this extension
    never accidentally commits a live bearer token. Never raises - a
    missing/unwritable .gitignore (e.g. a plain end-user install that
    isn't a git checkout at all) just means there is nothing to
    protect, not a reason to fail the server start."""
    try:
        existing = ""
        if os.path.isfile(_GITIGNORE_PATH):
            with open(_GITIGNORE_PATH, "r") as f:
                existing = f.read()
        if any(line.strip() == ".mcp.json" for line in existing.splitlines()):
            return
        with open(_GITIGNORE_PATH, "a") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(
                "\n# Claude Code's own MCP server config for this project - holds\n"
                "# DeeMCP's live bearer token in plain text once added. Per-machine,\n"
                "# per-session, rotates every time the DeeMCP server is restarted -\n"
                "# never belongs in the public repo.\n"
                ".mcp.json\n"
            )
    except Exception:
        pass


def _write_mcp_json(url, token):
    """Writes/updates ONLY the "dee-mcp" entry inside .mcp.json, merging
    with whatever else is already there (other MCP servers the user
    configured) rather than overwriting the whole file. Never raises -
    returns True/False so start() can report whether it actually
    happened."""
    try:
        data = {}
        if os.path.isfile(_MCP_JSON_PATH):
            try:
                with open(_MCP_JSON_PATH, "r") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        if not isinstance(data, dict):
            data = {}
        servers = data.setdefault("mcpServers", {})
        if not isinstance(servers, dict):
            servers = {}
            data["mcpServers"] = servers
        servers[API_NAME] = {
            "type": "http",
            "url": url,
            "headers": {"Authorization": "Bearer {0}".format(token)},
        }
        with open(_MCP_JSON_PATH, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        return True
    except Exception:
        return False


def is_running():
    return envvars.get_pyrevit_env_var(_ENV_SERVER) is not None


def current_token():
    return envvars.get_pyrevit_env_var(_ENV_TOKEN)


def current_port():
    return envvars.get_pyrevit_env_var(_ENV_PORT)


def request_count():
    return envvars.get_pyrevit_env_var(_ENV_COUNT) or 0


def record_request():
    envvars.set_pyrevit_env_var(_ENV_COUNT, request_count() + 1)


def mcp_url():
    port = current_port()
    if not port:
        return None
    return "http://{0}:{1}/{2}{3}".format(HOST, port, API_NAME, ROUTE_PATTERN)


def get_status():
    return {
        "running": is_running(),
        "host": HOST,
        "port": current_port(),
        "url": mcp_url(),
        "token": current_token(),
        "request_count": request_count(),
        "mcp_json_path": _MCP_JSON_PATH,
    }


def start():
    """Starts the server if not already running. Never raises for an
    ordinary "no free port found" case - returns (ok, detail)."""
    if is_running():
        return True, "Already running."

    token = uuid.uuid4().hex + uuid.uuid4().hex

    server = None
    chosen_port = None
    last_error = None
    for port in range(DEFAULT_PORT, DEFAULT_PORT + PORT_SEARCH_RANGE):
        try:
            server = RoutesServer(host=HOST, port=port)
            chosen_port = port
            break
        except Exception as e:
            last_error = e
            continue

    if server is None:
        return False, "Could not bind any port in {0}-{1}: {2}".format(
            DEFAULT_PORT, DEFAULT_PORT + PORT_SEARCH_RANGE - 1, last_error)

    dee_mcp_tools.reset_execute_python_session()
    envvars.set_pyrevit_env_var(_ENV_SERVER, server)
    envvars.set_pyrevit_env_var(_ENV_TOKEN, token)
    envvars.set_pyrevit_env_var(_ENV_PORT, chosen_port)
    envvars.set_pyrevit_env_var(_ENV_COUNT, 0)

    _ensure_gitignored()
    url = mcp_url()
    wrote_config = _write_mcp_json(url, token)
    detail = "Started on {0}:{1}".format(HOST, chosen_port)
    if wrote_config:
        detail += " - .mcp.json updated ({0}); start a NEW Claude Code session on this folder to pick it up".format(_MCP_JSON_PATH)
    else:
        detail += " - could not write .mcp.json automatically; paste the URL/token into your MCP client by hand"
    return True, detail


def stop():
    """Stops the server if running. Never raises - returns (ok, detail)."""
    server = envvars.get_pyrevit_env_var(_ENV_SERVER)
    if server is None:
        return True, "Not running."
    try:
        server.stop()
    except Exception as e:
        return False, "Error stopping server: {0}".format(e)
    finally:
        envvars.set_pyrevit_env_var(_ENV_SERVER, None)
        envvars.set_pyrevit_env_var(_ENV_TOKEN, None)
        envvars.set_pyrevit_env_var(_ENV_PORT, None)
    return True, "Stopped."
