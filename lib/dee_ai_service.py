# -*- coding: utf-8 -*-
"""
dee_ai_service
Agentic tool-use loop for DeeAI - a chat assistant embedded in Revit
that can read and directly modify the active document. The actual
per-vendor wire format (Claude/Anthropic, ChatGPT/OpenAI, or a
free-form "Custom" OpenAI-compatible endpoint) lives in
lib/dee_ai_providers.py; this file owns the parts that are the SAME
regardless of which provider is talking: the tool definition, the
Revit-facing system prompt, and the round-trip loop that executes
`execute_revit_python` tool calls against the sandbox until the AI
produces a final answer.

--------------------------------------------------------------------
Why raw HTTPS instead of official vendor SDKs
--------------------------------------------------------------------
This extension runs on pyRevit's IronPython 2.7.12 engine - official
SDKs (Anthropic's, OpenAI's) depend on httpx/pydantic/typing machinery
that doesn't run reliably there (the same constraint already solved
once in this codebase, in lib/acc_auth.py, for Autodesk's APS OAuth).
Every provider's calls go through dee_ai_providers.py's shared
`_post_json`, built on .NET's HttpClient - exactly like acc_auth.py's
proven token-exchange call.

--------------------------------------------------------------------
Design choices (explicit user decisions, not assumptions)
--------------------------------------------------------------------
- The ONLY tool exposed is execute_revit_python - arbitrary IronPython
  code, run immediately with NO per-call confirmation dialog, per the
  user's explicit choice of "full autonomous code execution" over a
  curated tool list or a propose-then-approve gate. The safety net is
  purely transactional (see dee_ai_sandbox.TurnTransaction) - every
  turn is undoable in one step; nothing is silently blocked. This tool
  definition is shared verbatim across every provider (see
  dee_ai_providers.py's own per-provider wire-format translation of it)
  so there is one description of what this capability does, not one
  copy per vendor that could drift.
- Multiple providers, added by explicit user request ("connect options
  to which AI tool, make it a list") - see dee_ai_providers.py for the
  registry and each vendor's wire-format specifics.
- Each provider's API key is remembered locally after first entry (also
  an explicit user choice, a change from this tool's original "never
  save, re-enter every time" default) - see DeeAI.pushbutton/script.py
  for the load/save wiring via lib/deew_settings.py, gitignored like
  every other per-machine settings file that module already manages.
"""
import dee_ai_sandbox as sandbox
import dee_ai_providers as providers

DEFAULT_PROVIDER = "anthropic"

# Kept for backward compatibility / as the Anthropic provider's own
# defaults - dee_ai_providers.PROVIDERS is the real source of truth for
# per-provider model choices now, since each vendor has its own list.
DEFAULT_MODEL = providers.get_provider(DEFAULT_PROVIDER)["default_model"]
MODEL_CHOICES = providers.get_provider(DEFAULT_PROVIDER)["model_choices"]
EFFORT_CHOICES = ["low", "medium", "high", "xhigh", "max"]
DEFAULT_EFFORT = "high"

MAX_TOKENS = 8192
MAX_TOOL_ITERATIONS = 8

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

    if doc is None:
        # Genuinely no document open - distinct from "doc exists but
        # doc.Title/doc.IsWorkshared happened to fail", which the
        # except branches below still cover separately. DeeAI's
        # bundle.yaml declares context: zero-doc specifically so this
        # case is reachable, e.g. to ask the AI to open or create a
        # project via uiapp before anything document-scoped applies.
        doc_state = (
            "No document is currently open. You can open an existing "
            "one or create a new one via `uiapp` (e.g. "
            "`uiapp.OpenAndActivateDocument(...)` or "
            "`uiapp.Application.NewProjectDocument(...)`) - `doc`/"
            "`uidoc` will be None until you do."
        )
    else:
        try:
            doc_title = doc.Title
        except Exception:
            doc_title = "an unnamed document"
        try:
            workshared = "workshared" if bool(doc.IsWorkshared) else "not workshared"
        except Exception:
            workshared = "unknown worksharing state"
        doc_state = "The active document is '{0}' ({1}).".format(doc_title, workshared)

    return (
        "You are DeeAI, an assistant embedded in Autodesk Revit via "
        "pyRevit, running inside {revit_version}. {doc_state} You act "
        "by writing IronPython 2.7 code and calling the "
        "execute_revit_python tool - there is no other way for you to "
        "affect the model. Python 2.7 syntax "
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
    ).format(revit_version=revit_version, doc_state=doc_state)


class RefusedError(Exception):
    def __init__(self, category, explanation):
        self.category = category
        self.explanation = explanation
        super(RefusedError, self).__init__(
            "The AI declined this request (category: {0}). {1}".format(
                category or "unspecified", explanation or ""))


class AIConversation(object):
    """One chat conversation for one open DeeAI window. Holds the
    provider-shaped message history and the persistent exec-globals
    dict shared by every execute_revit_python call in this
    conversation. `provider_id`/`base_url` select which vendor's wire
    format (lib/dee_ai_providers.py) this conversation speaks - never
    mixed mid-conversation (switching providers in the UI starts a new
    conversation, since each vendor's message history shape is
    incompatible with the others')."""

    def __init__(self, doc, uidoc, uiapp, provider_id=DEFAULT_PROVIDER, model=None,
                 effort=DEFAULT_EFFORT, max_tokens=MAX_TOKENS, base_url=None):
        self.doc = doc
        self.uidoc = uidoc
        self.uiapp = uiapp
        self.provider_id = provider_id
        self.model = model or providers.get_provider(provider_id)["default_model"]
        self.effort = effort
        self.max_tokens = max_tokens
        self.base_url = base_url
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

        # Refreshed every message, not just once at conversation start:
        # DeeAI can open with NO document (bundle.yaml: context:
        # zero-doc) and the AI's own code may open/create one via
        # uiapp mid-conversation - without this, self.doc/exec_globals
        # would keep reporting None for the rest of the chat even after
        # a real document exists, silently losing the Transaction
        # safety net. Preserves every OTHER variable already in
        # exec_globals (the REPL-persistence this class is built
        # around) - only the doc/uidoc/uiapp/__revit__ entries change.
        self.uidoc = self.uiapp.ActiveUIDocument
        self.doc = self.uidoc.Document if self.uidoc is not None else None
        self.exec_globals.update({
            "doc": self.doc, "uidoc": self.uidoc, "uiapp": self.uiapp, "__revit__": self.uiapp,
        })
        self.system_prompt = build_system_prompt(self.doc, self.uiapp)

        # NOTE (documented limitation, not silently glossed over): if
        # THIS SAME message is the one whose code opens/creates the
        # document (e.g. "open project X and count its walls" in one
        # go), the TurnTransaction below is still built from the doc
        # captured just above - None - for this entire turn, so that
        # one turn runs un-transacted even after the document exists.
        # The NEXT separate message picks up the real doc correctly via
        # the refresh above. Splitting "open" and "edit" into two
        # messages gets the normal Transaction safety net on the edit.
        provider = providers.get_provider(self.provider_id)
        self.messages.append({"role": "user", "content": user_text})

        with sandbox.TurnTransaction(self.doc, "DeeAI") as turn:
            for _ in range(MAX_TOOL_ITERATIONS):
                payload = provider["build_payload"](
                    self.model, self.system_prompt, self.messages, EXECUTE_TOOL,
                    self.max_tokens, self.effort, self.base_url)
                data = provider["send"](api_key, payload, self.base_url)
                result = provider["interpret"](data)

                if result.finish == "refused":
                    category, explanation = result.refusal_detail or (None, None)
                    raise RefusedError(category, explanation)

                self.messages.append(result.assistant_message)

                if result.finish == "continue_same":
                    continue

                if result.finish == "tool_calls":
                    tool_results = []
                    for call in result.tool_calls:
                        code = (call.get("input") or {}).get("code", "")
                        emit("code", code)
                        ok, output = turn.run(code, self.exec_globals)
                        emit("result" if ok else "error", output)
                        tool_results.append({"id": call.get("id"), "output": output, "ok": ok})
                    self.messages.extend(provider["tool_result_entries"](tool_results))
                    continue

                if result.finish == "max_tokens":
                    return (result.text + "\n\n[DeeAI hit its response size limit before finishing - "
                                           "try again, or break the task into smaller steps.]").strip()

                # end_turn (or any other final reason) - this is the answer
                return result.text

        return ("DeeAI stopped after too many tool steps without a final answer - "
                "try asking again, or break the task into a smaller step.")
