# -*- coding: utf-8 -*-
"""
dee_ai_service
Claude API client + agentic tool-use loop for DeeAI - a chat assistant
embedded in Revit that can read and directly modify the active document.

--------------------------------------------------------------------
Why raw HTTPS instead of the official `anthropic` Python SDK
--------------------------------------------------------------------
This extension runs on pyRevit's IronPython 2.7.12 engine - the
official Anthropic SDK depends on httpx/pydantic/typing machinery that
doesn't run reliably there (the same constraint already solved once in
this codebase, in lib/acc_auth.py, for Autodesk's APS OAuth instead of
Anthropic's API). Calls go straight to the public Messages API
(https://api.anthropic.com/v1/messages) via .NET's HttpClient, exactly
like acc_auth.py's proven token-exchange call.

--------------------------------------------------------------------
Design choices (explicit user decisions, not assumptions)
--------------------------------------------------------------------
- Model defaults to "claude-opus-5", user-selectable in the UI.
- The ONLY tool exposed is execute_revit_python - arbitrary IronPython
  code, run immediately with NO per-call confirmation dialog, per the
  user's explicit choice of "full autonomous code execution" over a
  curated tool list or a propose-then-approve gate. The safety net is
  purely transactional (see dee_ai_sandbox.TurnTransaction) - every
  turn is undoable in one step; nothing is silently blocked.
- The API key is NEVER written to disk - the user re-enters it every
  time the DeeAI window is opened (their explicit choice over a saved,
  gitignored config file), and it only lives in memory in this
  window's process for as long as the window stays open.

--------------------------------------------------------------------
Claude API facts relied on here (verified against the current
Anthropic Messages API reference before writing, not guessed)
--------------------------------------------------------------------
- POST https://api.anthropic.com/v1/messages, headers x-api-key,
  anthropic-version: 2023-06-01, content-type: application/json.
- Claude Opus 5 thinking is ON by default (adaptive) when the
  `thinking` field is omitted - deliberately left omitted here rather
  than explicitly disabled, since disabling it on this model can make
  it write a tool call into plain visible text instead of a real
  tool_use block (the call would then silently never run).
- stop_reason == "refusal" can come back with an empty or partial
  content array - must check stop_reason before assuming content[0]
  exists.
- stop_reason == "tool_use" - every tool_use block must be executed,
  and ALL resulting tool_result blocks returned in a SINGLE follow-up
  user message (never split across messages - parallel tool use
  depends on this).
- stop_reason == "pause_turn" - resend the same assistant content
  as-is as the next request with no new user message; handled
  defensively here even though this integration declares no
  server-side tools (so it isn't expected to actually occur).
"""
import json

import clr
clr.AddReference("System.Net.Http")
from System.Net.Http import HttpClient, HttpRequestMessage, HttpMethod, StringContent
from System import TimeSpan
from System.Text import Encoding

import dee_ai_sandbox as sandbox

API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

DEFAULT_MODEL = "claude-opus-5"
MODEL_CHOICES = ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"]
EFFORT_CHOICES = ["low", "medium", "high", "xhigh", "max"]
DEFAULT_EFFORT = "high"

MAX_TOKENS = 8192
MAX_TOOL_ITERATIONS = 8
REQUEST_TIMEOUT_SECONDS = 180

EXECUTE_TOOL = {
    "name": "execute_revit_python",
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
        "between calls in this same conversation, like a REPL - no "
        "need to re-collect elements already looked up earlier in "
        "this chat."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "IronPython 2.7 source code to execute.",
            },
        },
        "required": ["code"],
    },
}


def build_system_prompt(doc, uiapp):
    try:
        app = uiapp.Application
        revit_version = "{0} {1}".format(app.VersionName, app.VersionNumber)
    except Exception:
        revit_version = "an unknown Revit version"
    try:
        doc_title = doc.Title
    except Exception:
        doc_title = "an unnamed document"
    try:
        workshared = "workshared" if bool(doc.IsWorkshared) else "not workshared"
    except Exception:
        workshared = "unknown worksharing state"

    return (
        "You are DeeAI, an assistant embedded in Autodesk Revit via "
        "pyRevit, running inside {revit_version}. The active document "
        "is '{doc_title}' ({workshared}). You act by writing IronPython "
        "2.7 code and calling the execute_revit_python tool - there is "
        "no other way for you to affect the model. Python 2.7 syntax "
        "only (no f-strings, no Python-3-only syntax). Import whatever "
        "Autodesk.Revit.DB / Autodesk.Revit.UI names you need at the "
        "top of your code (e.g. 'from Autodesk.Revit.DB import "
        "FilteredElementCollector, BuiltInCategory') - these assemblies "
        "are already loaded in-process, clr.AddReference is not needed "
        "for them. Never call Transaction/SubTransaction/"
        "TransactionGroup yourself - the host already wraps every call "
        "you make in one. Prefer efficient FilteredElementCollector "
        "usage over iterating every element in the document by hand. "
        "When unsure whether something exists on a given element or "
        "Revit version, wrap the risky part in try/except and print "
        "what was found rather than guessing silently. When finished "
        "acting, summarize in plain language what changed or was found "
        "- the user cannot see your code or its raw output, only your "
        "final text and whatever you chose to print."
    ).format(revit_version=revit_version, doc_title=doc_title, workshared=workshared)


class RefusedError(Exception):
    def __init__(self, category, explanation):
        self.category = category
        self.explanation = explanation
        super(RefusedError, self).__init__(
            "Claude declined this request (category: {0}). {1}".format(
                category or "unspecified", explanation or ""))


def _post_json(api_key, payload):
    client = HttpClient()
    client.Timeout = TimeSpan.FromSeconds(REQUEST_TIMEOUT_SECONDS)
    request = HttpRequestMessage(HttpMethod.Post, API_URL)
    request.Headers.Add("x-api-key", api_key)
    request.Headers.Add("anthropic-version", ANTHROPIC_VERSION)
    request.Content = StringContent(json.dumps(payload), Encoding.UTF8, "application/json")
    try:
        response = client.SendAsync(request).Result
        response_body = response.Content.ReadAsStringAsync().Result
    except Exception as e:
        raise Exception("Could not reach api.anthropic.com: {0}".format(e))
    if not response.IsSuccessStatusCode:
        raise Exception("Claude API error ({0}): {1}".format(int(response.StatusCode), response_body))
    return json.loads(response_body)


def _extract_text(content):
    parts = [b.get("text", "") for b in content if b.get("type") == "text"]
    return "\n".join(p for p in parts if p)


class AIConversation(object):
    """One chat conversation for one open DeeAI window. Holds the
    Messages API history and the persistent exec-globals dict shared
    by every execute_revit_python call in this conversation."""

    def __init__(self, doc, uidoc, uiapp, model=DEFAULT_MODEL, effort=DEFAULT_EFFORT, max_tokens=MAX_TOKENS):
        self.doc = doc
        self.uidoc = uidoc
        self.uiapp = uiapp
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.system_prompt = build_system_prompt(doc, uiapp)
        self.messages = []
        self.exec_globals = sandbox.build_exec_globals(doc, uidoc, uiapp)

    def reset(self):
        self.messages = []
        self.exec_globals = sandbox.build_exec_globals(self.doc, self.uidoc, self.uiapp)

    def send(self, api_key, user_text, on_event=None):
        """Runs one full user turn (possibly several tool round-trips),
        all inside a single Transaction (see dee_ai_sandbox). Returns
        the assistant's final text reply. on_event(kind, text), if
        given, is called with kind in {"code","result","error"} as
        each tool call happens, for live transcript logging."""
        def emit(kind, text):
            if on_event is not None:
                on_event(kind, text)

        self.messages.append({"role": "user", "content": user_text})

        with sandbox.TurnTransaction(self.doc, "DeeAI") as turn:
            for _ in range(MAX_TOOL_ITERATIONS):
                payload = {
                    "model": self.model,
                    "max_tokens": self.max_tokens,
                    "system": self.system_prompt,
                    "output_config": {"effort": self.effort},
                    "tools": [EXECUTE_TOOL],
                    "messages": self.messages,
                }
                data = _post_json(api_key, payload)
                stop_reason = data.get("stop_reason")
                content = data.get("content") or []

                if stop_reason == "refusal":
                    details = data.get("stop_details") or {}
                    raise RefusedError(details.get("category"), details.get("explanation"))

                if stop_reason == "pause_turn":
                    self.messages.append({"role": "assistant", "content": content})
                    continue

                if stop_reason == "tool_use":
                    self.messages.append({"role": "assistant", "content": content})
                    tool_results = []
                    for block in content:
                        if block.get("type") != "tool_use":
                            continue
                        code = (block.get("input") or {}).get("code", "")
                        emit("code", code)
                        ok, output = turn.run(code, self.exec_globals)
                        emit("result" if ok else "error", output)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.get("id"),
                            "content": output,
                            "is_error": not ok,
                        })
                    self.messages.append({"role": "user", "content": tool_results})
                    continue

                if stop_reason == "max_tokens":
                    self.messages.append({"role": "assistant", "content": content})
                    reply = _extract_text(content)
                    return (reply + "\n\n[DeeAI hit its response size limit before finishing - "
                                     "try again, or break the task into smaller steps.]").strip()

                # end_turn (or any other final reason) - this is the answer
                self.messages.append({"role": "assistant", "content": content})
                return _extract_text(content)

        return ("DeeAI stopped after too many tool steps without a final answer - "
                "try asking again, or break the task into a smaller step.")
