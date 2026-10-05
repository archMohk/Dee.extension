# -*- coding: utf-8 -*-
"""
dee_mcp_tools
The tool registry for DeeMCP (DeePack.tab/Tools.panel/DeeMCP.pushbutton) -
every capability an external MCP client can invoke via a `tools/call`
JSON-RPC request. Modeled deliberately on DeeLazy's
`modules/__init__.py` REGISTERED_MODULES pattern: a plain list any
future session can extend by writing one handler function and adding
one entry here - no changes needed to the dispatcher in
lib/dee_mcp_service.py to add another tool.

Every handler has the signature `handler(doc, uidoc, uiapp, arguments)
-> text` and must never raise for an ordinary failure (bad arguments, a
Revit API error) - it should catch its own exceptions and return a
clear error string instead, exactly like dee_ai_sandbox.TurnTransaction
already does for execute_revit_python. dee_mcp_service.py wraps every
call in a try/except as a last resort, but a handler that reports its
own failures clearly is much more useful to an AI client than a raw
JSON-RPC internal-error response.

--------------------------------------------------------------------
Tool 1: execute_revit_python - arbitrary read/write Revit API access
--------------------------------------------------------------------
Thin wrapper around dee_ai_sandbox.TurnTransaction, the exact same
transactional sandbox DeeAI.pushbutton already uses live today (one
Transaction per call here - see _run_python below for why this differs
slightly from DeeAI's one-Transaction-per-chat-turn framing - one
SubTransaction nested inside it, so this is not new risk, it is the
same proven mechanism invoked from a different caller). The `code`
input_schema (the actual JSON schema, not the prose description) is
imported directly from dee_ai_service.EXECUTE_TOOL - it is genuinely
caller-independent (just "a string of IronPython source"), so there is
one source of truth for it rather than two copies that could drift.
The DESCRIPTION text below is DeeMCP's own, not copied verbatim -
DeeAI's own description talks about "this conversation", which would
be inaccurate here: DeeMCP has no conversation concept, variables
persist for the SERVER's lifetime instead (see below).

Exec globals persist for the lifetime of the SERVER (reset only when
DeeMCP's server is stopped/restarted - see lib/dee_mcp_server.py), so
variables an AI client sets in one tools/call are still there in the
next, like a REPL - but `doc`/`uidoc`/`uiapp`/`__revit__` are
refreshed from the LIVE active document on every single call (never
cached from server-start time), per DeeMCP's "never act on a stale/
closed document" security rule. This means element references the AI
cached from an earlier call can become invalid if the active document
changes in between - an inherent limitation of a REPL-style namespace
spanning multiple real-world document states, not something papered
over here.

DeeMCP's bundle.yaml declares `context: zero-doc` (same reasoning as
DeeOpener.pushbutton) so the button itself is not greyed out with no
project open - the whole point being that an AI client can open or
create a document via `uiapp`, not just manipulate one already open.
`doc` is None in that case, and `Transaction(None, ...)` raises, so
`_run_python` below skips TurnTransaction entirely and runs the code
directly when there is no document yet - there is nothing to make
undoable until a document exists. Once the AI's code opens/activates
one (e.g. `uiapp.OpenAndActivateDocument(...)`), the NEXT call sees a
real `doc` again (freshly read per call, per the rule above) and goes
through the normal transactional path.

--------------------------------------------------------------------
Tool 2: set_view_template_link_display - a concrete "existing tool,
exposed" example, not just a theoretical framework
--------------------------------------------------------------------
Bulk-sets the Revit Links "Display Settings" (By Host View / By Linked
View) across View Templates, matching what DeeLazy's DeeVTEMP module
(modules/dee_vtemp.py) already does interactively. The actual
Revit-API logic (_build_link_settings/_option_label/apply) is a LOCAL
COPY of dee_vtemp.py's own functions, not an import - dee_vtemp.py
lives inside DeeLazy.pushbutton's own modules/ folder, which is never
on lib/'s import path (only the specific pushbutton that owns a
modules/ folder has it on sys.path when ITS OWN script.py runs), so
reaching across pushbutton boundaries like that is not viable. Same
reasoning dee_sheet_links_service.py already documents for its own
local copy of a link-naming helper: duplicate the small pure function,
don't entangle two unrelated tools' import surfaces.

Only "By Host View" and "By Linked View" are exposed (matching
dee_vtemp.py's own explicitly scoped first pass) - "Custom" link
display is out of scope here too.
"""
import os
import sys
import traceback

from Autodesk.Revit.DB import (
    FilteredElementCollector, View, RevitLinkInstance,
    RevitLinkGraphicsSettings, LinkVisibility,
    Transaction, ModelPathUtils,
)

import dee_ai_sandbox as sandbox
import dee_ai_service

try:
    from StringIO import StringIO
except ImportError:
    from io import StringIO


# ==========================================================================
# Tool 1: execute_revit_python
# ==========================================================================
EXECUTE_PYTHON_SCHEMA = {
    "name": "execute_revit_python",
    # The schema itself (not the description) is the caller-independent
    # part - one source of truth with DeeAI's own tool definition.
    "input_schema": dee_ai_service.EXECUTE_TOOL["input_schema"],
    "description": (
        "Runs IronPython 2.7 code directly against the active Revit "
        "document to read or change the model. Call this whenever the "
        "request needs information from the model (counts, parameter "
        "values, element lists) or a change to it (create, modify, "
        "delete elements/parameters/views, etc.) - do not just describe "
        "what code would do, actually call this to run it. `doc`, "
        "`uidoc`, and `uiapp` are already defined in scope; do not "
        "redefine them and do not call Transaction/SubTransaction "
        "yourself - the code already runs inside one, managed by the "
        "host. Use print() to report results, counts, or element ids - "
        "the tool result returned is exactly the code's captured "
        "stdout (or a traceback if it raised). Variables persist "
        "between calls for as long as the DeeMCP server stays running, "
        "like a REPL - no need to re-collect elements already looked "
        "up earlier - but `doc`/`uidoc`/`uiapp` always reflect "
        "whichever document is active RIGHT NOW, which may differ from "
        "an earlier call if the user switched or closed documents."
    ),
}

# Persists for the lifetime of the server process - reset by
# lib/dee_mcp_server.py when the server (re)starts, never reset
# per-call. See module docstring for why doc/uidoc/uiapp are still
# refreshed every call despite this.
_exec_globals = None


def reset_execute_python_session():
    """Called by lib/dee_mcp_server.py when the server (re)starts, so a
    fresh Start never inherits variables from a previous server run."""
    global _exec_globals
    _exec_globals = None


def _run_python(doc, uidoc, uiapp, arguments):
    global _exec_globals
    code = (arguments or {}).get("code", "")
    if not code.strip():
        return "Error: no 'code' argument provided."

    if _exec_globals is None:
        _exec_globals = sandbox.build_exec_globals(doc, uidoc, uiapp)
    else:
        _exec_globals.update({
            "doc": doc, "uidoc": uidoc, "uiapp": uiapp, "__revit__": uiapp,
        })

    if doc is None:
        # No document open (DeeMCP's bundle.yaml declares context:
        # zero-doc specifically to allow this) - Transaction(None, ...)
        # raises, and there is nothing to make undoable yet anyway, so
        # run directly instead of going through TurnTransaction. This is
        # how an AI client opens/creates a document in the first place
        # (e.g. uiapp.OpenAndActivateDocument(...)) before anything
        # document-scoped becomes possible.
        return _run_bare(code, _exec_globals)

    # One Transaction per tools/call, not one per "conversation" like
    # DeeAI (there is no equivalent grouping concept in a stateless
    # JSON-RPC request) - still gets DeeAI's exact SubTransaction safety
    # net per individual call via TurnTransaction.run().
    with sandbox.TurnTransaction(doc, "DeeMCP - execute_revit_python") as turn:
        ok, output = turn.run(code, _exec_globals)
    return output if ok else "Error:\n{0}".format(output)


def _run_bare(code, exec_globals):
    """Same stdout-capture/traceback contract as
    dee_ai_sandbox.TurnTransaction.run(), minus the Transaction/
    SubTransaction wrapping that requires a real Document to exist."""
    old_stdout = sys.stdout
    buf = StringIO()
    sys.stdout = buf
    try:
        exec(code, exec_globals)
        sys.stdout = old_stdout
        output = buf.getvalue()
        return output if output.strip() else "(no output - code ran without printing anything)"
    except Exception:
        sys.stdout = old_stdout
        return "Error:\n" + traceback.format_exc()


# ==========================================================================
# Tool 2: set_view_template_link_display
# ==========================================================================
SET_LINK_DISPLAY_SCHEMA = {
    "name": "set_view_template_link_display",
    "description": (
        "Bulk-sets the Revit Links 'Display Settings' (By Host View or "
        "By Linked View) across View Templates in the active document - "
        "the same per-link dropdown normally set one at a time inside "
        "each View Template's own Visibility/Graphics Overrides > Revit "
        "Links tab. Applies to every View Template whose name contains "
        "'template_name_contains' (case-insensitive; empty/omitted "
        "means ALL View Templates), for every Revit Link currently "
        "placed in the document."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "display_option": {
                "type": "string",
                "enum": ["ByHostView", "ByLinkView"],
                "description": "Which Display Setting to apply.",
            },
            "template_name_contains": {
                "type": "string",
                "description": "Optional case-insensitive filter on View Template name. Omit or leave empty to apply to every View Template.",
            },
        },
        "required": ["display_option"],
    },
}


def _link_display_name(link_type):
    """Local copy of dee_vtemp.py's own _link_display_name (itself a
    local copy of lib/dee_sheet_links_service.py's) - see module
    docstring for why this is duplicated rather than imported."""
    try:
        ref = link_type.GetExternalFileReference()
        if ref is not None:
            model_path = ref.GetPath()
            if model_path is not None:
                visible_path = ModelPathUtils.ConvertModelPathToUserVisiblePath(model_path)
                if visible_path:
                    return os.path.basename(visible_path)
    except Exception:
        pass
    try:
        if link_type.Name:
            return link_type.Name
    except Exception:
        pass
    return "(unnamed link)"


def _build_link_settings(display_option):
    """RevitLinkGraphicsSettings has ONLY a no-arg constructor - verified
    directly against the installed RevitAPI.dll via .NET reflection
    (System.Reflection.MetadataLoadContext against Revit 2026's actual
    assembly) after the original guess (a one-arg constructor taking a
    'RevitLinkGraphicsDisplayOptions' enum - no such type exists at all)
    crashed this tool's very first live run with an ImportError. The
    real enum is Autodesk.Revit.DB.LinkVisibility (ByHostView/ByLinkView/
    Custom), set via the settable .LinkVisibilityType property."""
    settings = RevitLinkGraphicsSettings()
    settings.LinkVisibilityType = display_option
    return settings


def _set_view_template_link_display(doc, uidoc, uiapp, arguments):
    if doc is None:
        return "Error: no Revit document is open - open or create one first (e.g. via execute_revit_python)."
    args = arguments or {}
    option_text = args.get("display_option", "ByHostView")
    display_option = (LinkVisibility.ByLinkView
                       if option_text == "ByLinkView"
                       else LinkVisibility.ByHostView)
    name_filter = (args.get("template_name_contains") or "").strip().lower()

    templates = []
    for v in FilteredElementCollector(doc).OfClass(View):
        try:
            if not v.IsTemplate:
                continue
            name = v.Name or ""
            if name_filter and name_filter not in name.lower():
                continue
            templates.append(v)
        except Exception:
            continue

    links = []
    for inst in FilteredElementCollector(doc).OfClass(RevitLinkInstance):
        try:
            link_type = doc.GetElement(inst.GetTypeId())
            name = _link_display_name(link_type) if link_type is not None else "(unresolved link)"
        except Exception:
            name = "(unresolved link)"
        links.append((inst.Id, name))

    if not templates:
        return "No View Template matched - nothing changed."
    if not links:
        return "No Revit Links found in this document - nothing to set."

    lines = []
    ok_count = 0
    fail_count = 0
    t = Transaction(doc, "DeeMCP - Set Revit Links Display Setting")
    t.Start()
    try:
        for tmpl in templates:
            tmpl_name = tmpl.Name or "(unnamed template)"
            for link_id, link_name in links:
                try:
                    settings = _build_link_settings(display_option)
                    tmpl.SetLinkOverrides(link_id, settings)
                    ok_count += 1
                    lines.append("OK   {0} -> {1}".format(tmpl_name, link_name))
                except Exception as e:
                    fail_count += 1
                    lines.append("FAIL {0} -> {1}: {2}".format(tmpl_name, link_name, e))
        t.Commit()
    except Exception as e:
        t.RollBack()
        return "Transaction failed, nothing was changed: {0}".format(e)

    summary = "{0} template(s), {1} link(s): {2} succeeded, {3} failed.".format(
        len(templates), len(links), ok_count, fail_count)
    return summary + "\n" + "\n".join(lines)


# ==========================================================================
# Registry
# ==========================================================================
TOOLS = [
    {
        "schema": EXECUTE_PYTHON_SCHEMA,
        "handler": _run_python,
    },
    {
        "schema": SET_LINK_DISPLAY_SCHEMA,
        "handler": _set_view_template_link_display,
    },
]


def list_schemas():
    return [t["schema"] for t in TOOLS]


def get_handler(tool_name):
    for t in TOOLS:
        if t["schema"]["name"] == tool_name:
            return t["handler"]
    return None


def call_tool(tool_name, doc, uidoc, uiapp, arguments):
    """Never raises - returns (ok, text). Looks up the handler and runs
    it, catching anything the handler itself didn't already catch so a
    bug in one tool can never take down the whole DeeMCP dispatcher."""
    handler = get_handler(tool_name)
    if handler is None:
        return False, "Unknown tool: '{0}'".format(tool_name)
    try:
        return True, handler(doc, uidoc, uiapp, arguments)
    except Exception:
        return False, "Tool '{0}' raised an unhandled exception:\n{1}".format(
            tool_name, traceback.format_exc())
