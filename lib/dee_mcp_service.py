# -*- coding: utf-8 -*-
"""
dee_mcp_service
The JSON-RPC 2.0 dispatcher behind DeeMCP's one HTTP route - implements
just enough of the Model Context Protocol (MCP) for `initialize`,
`tools/list`, and `tools/call` to work against any standards-compliant
MCP client over Streamable HTTP's plain-JSON response mode (see
lib/dee_mcp_server.py's module docstring for why Server-Sent-Events/
streaming is explicitly out of scope for this first version - pyRevit's
`routes` module this server is built on is single-shot request/response
only, which the MCP spec allows a server to use, just not require).

--------------------------------------------------------------------
Where this function actually runs (important, not obvious)
--------------------------------------------------------------------
`mcp_endpoint` below is registered as a pyrevit.routes handler function
(see lib/dee_mcp_server.py) and declares `doc`/`uiapp` parameters -
pyRevit's routes framework detects that via reflection and AUTOMATICALLY
marshals every call onto Revit's own main/API thread via its own
ExternalEvent bridge (routes/server/handler.py, confirmed by reading
pyRevit's own source before writing this), blocking the HTTP request
thread until it's done. This file never creates or manages an
ExternalEvent itself - that plumbing is already handled by pyRevit.

The bearer-token check happens as the FIRST thing inside this
already-marshaled function, before `doc` is touched at all - an
unauthorized request still costs one harmless ExternalEvent round-trip
(cheap), but never gets real access to the open document.

--------------------------------------------------------------------
MCP wire-format note
--------------------------------------------------------------------
lib/dee_mcp_tools.py's registry reuses dee_ai_service.EXECUTE_TOOL's
existing schema shape verbatim, which uses Claude Messages API's
`input_schema` (snake_case) key. MCP's own `tools/list` response
expects `inputSchema` (camelCase) instead - translated here, at the
wire boundary, rather than renaming the shared schema dict itself and
risking a silent mismatch with dee_ai_service.py's own copy.

NEEDS LIVE-REVIT VERIFICATION (per this codebase's standing practice):
the exact field names/shapes below follow the current public MCP
specification as documented at the time this was written, but this has
never been exercised against a real MCP client from this session - see
lib/dee_mcp_server.py's own NEEDS LIVE VERIFICATION notes.
"""
import time
import traceback

from pyrevit import routes

import dee_mcp_tools as tools

MCP_PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "dee-mcp"
SERVER_VERSION = "1.0.0"

# JSON-RPC 2.0 standard error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
# MCP-specific: not part of JSON-RPC itself, used here for "no document
# open" / "unauthorized" style failures that aren't a protocol error.
UNAUTHORIZED = -32001


def _rpc_result(request_id, result):
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _check_token(request, expected_token):
    try:
        auth = request.headers.get("Authorization") or request.headers.get("authorization") or ""
    except Exception:
        auth = ""
    if not auth.startswith("Bearer "):
        return False
    return auth[len("Bearer "):].strip() == expected_token


def _tool_schemas_mcp():
    """dee_mcp_tools' internal schema shape (Claude-style `input_schema`)
    translated to MCP's `inputSchema` key at this wire boundary only."""
    out = []
    for schema in tools.list_schemas():
        out.append({
            "name": schema["name"],
            "description": schema.get("description", ""),
            "inputSchema": schema.get("input_schema", {"type": "object", "properties": {}}),
        })
    return out


def _handle_initialize(request_id):
    return _rpc_result(request_id, {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        "capabilities": {"tools": {}},
    })


def _handle_tools_list(request_id):
    return _rpc_result(request_id, {"tools": _tool_schemas_mcp()})


def _handle_tools_call(request_id, params, doc, uidoc, uiapp):
    params = params or {}
    tool_name = params.get("name")
    arguments = params.get("arguments") or {}
    if not tool_name:
        return _rpc_error(request_id, INVALID_PARAMS, "Missing 'name' in tools/call params.")
    if doc is None:
        return _rpc_result(request_id, {
            "content": [{"type": "text", "text": "No Revit document is currently open."}],
            "isError": True,
        })
    ok, text = tools.call_tool(tool_name, doc, uidoc, uiapp, arguments)
    return _rpc_result(request_id, {
        "content": [{"type": "text", "text": text}],
        "isError": not ok,
    })


def _dispatch(body, doc, uidoc, uiapp):
    if not isinstance(body, dict):
        return _rpc_error(None, INVALID_REQUEST, "Request body must be a JSON object.")

    request_id = body.get("id")
    method = body.get("method")
    params = body.get("params")

    # JSON-RPC 2.0: a request with no "id" is a notification - the
    # client does not expect (and must not receive) a correlated
    # response body. Acknowledged by the caller as HTTP 204.
    is_notification = "id" not in body

    if not method:
        return None if is_notification else _rpc_error(request_id, INVALID_REQUEST, "Missing 'method'.")

    if method == "initialize":
        return None if is_notification else _handle_initialize(request_id)
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "tools/list":
        return None if is_notification else _handle_tools_list(request_id)
    if method == "tools/call":
        return None if is_notification else _handle_tools_call(request_id, params, doc, uidoc, uiapp)

    return None if is_notification else _rpc_error(
        request_id, METHOD_NOT_FOUND, "Unknown method: '{0}'".format(method))


def mcp_endpoint(request, doc, uidoc, uiapp):
    """The one route handler DeeMCP registers with pyrevit.routes. See
    module docstring for why declaring doc/uidoc/uiapp here is what
    makes pyRevit auto-marshal this whole function onto Revit's main
    thread - this file never touches ExternalEvent directly."""
    import dee_mcp_server as server
    import deew_logger

    logger = deew_logger.DeeWLogger("DeeMCP")
    start = time.time()
    body = request.data if isinstance(request.data, dict) else {}
    method = body.get("method", "?")

    expected_token = server.current_token()
    if not expected_token or not _check_token(request, expected_token):
        logger.warning("Rejected unauthorized request", method=method)
        return routes.make_response(
            {"jsonrpc": "2.0", "error": {"code": UNAUTHORIZED, "message": "Unauthorized - missing or invalid bearer token."}},
            status=401)

    try:
        response = _dispatch(body, doc, uidoc, uiapp)
        elapsed_ms = int((time.time() - start) * 1000)
        if method == "tools/call":
            tool_name = (body.get("params") or {}).get("name", "?")
            ok = True
            if isinstance(response, dict) and response.get("result", {}).get("isError"):
                ok = False
            logger.info("tools/call", tool=tool_name, ok=ok, elapsed_ms=elapsed_ms)
        else:
            logger.debug("RPC method handled", method=method, elapsed_ms=elapsed_ms)
        server.record_request()
        if response is None:
            # Notification - client expects no correlated response body.
            return routes.make_response(None, status=routes.NO_CONTENT)
        return response
    except Exception as e:
        logger.exception("Unhandled error dispatching RPC", e, method=method)
        return _rpc_error(body.get("id"), INTERNAL_ERROR, "{0}: {1}".format(e, traceback.format_exc()))
