# -*- coding: utf-8 -*-
"""
dee_ai_providers
Provider abstraction for DeeAI - lets dee_ai_service.AIConversation run
its tool-use loop generically over more than one AI vendor's API, each
with its own wire format, hand-built the same way dee_ai_service.py's
original Claude-only client already did (raw HTTPS via .NET's
HttpClient - IronPython 2.7 cannot run the official SDKs, same
constraint lib/acc_auth.py solved once already for Autodesk's APS
OAuth).

Added by explicit user request ("connect options to which AI tool,
make it a list") - Claude (Anthropic, the original/only provider) plus
ChatGPT (OpenAI) plus a free-form "Custom (OpenAI-compatible API)" slot
for anything else that speaks OpenAI's wire format (many providers do -
OpenRouter, Groq, Together, local Ollama/LM Studio servers, DeepSeek,
etc. - so "Custom" covers far more than literally "a different OpenAI
account" despite reusing OpenAI's request/response shape).

--------------------------------------------------------------------
Why ChatGPT uses Chat Completions, not the newer Responses API
--------------------------------------------------------------------
Verified live against OpenAI's current docs before writing this (not
guessed): OpenAI now recommends the Responses API for new work, and
Chat Completions is explicitly flagged as NOT recommended for some of
OpenAI's current flagship models when tool-calling is needed. Chat
Completions remains fully supported (not deprecated) and has a flatter,
simpler wire format well-suited to hand-rolled IronPython 2.7 requests,
so it's what this file builds against. Practical consequence: if the
model ID entered doesn't support tool-calling on Chat Completions, tool
calls will silently never happen (the model will just reply in plain
text) rather than erroring clearly - not something this file can detect
or work around, since there is no reliable "does this model support X"
signal in the API response itself.

NEEDS LIVE VERIFICATION, explicitly flagged rather than guessed past:
the exact set of CURRENT model IDs OpenAI offers. The live doc check
behind this file surfaced OpenAI model names unfamiliar from training
data and could not fully confirm which ones support tool-calling on
Chat Completions specifically (vs requiring Responses API) - rather
than hardcode a possibly-wrong/fabricated "current flagship" model ID
that could silently fail for a user's account, the model field is a
free-text, user-editable ComboBox with only long-established, high-
confidence names offered as starting suggestions (gpt-4o-mini/gpt-4o).
Check platform.openai.com/docs/models for your account's actual
available models before relying on this.

--------------------------------------------------------------------
Chat Completions wire format relied on here (verified live)
--------------------------------------------------------------------
- POST {base_url} (default https://api.openai.com/v1/chat/completions),
  Authorization: Bearer <key>, Content-Type: application/json.
- Body: model, messages ([{role, content} / assistant messages may
  carry tool_calls, tool-result messages use role "tool"]), tools
  ([{"type": "function", "function": {"name","description",
  "parameters"}, "strict": true}] - strict mode needs
  additionalProperties: false added to the schema), tool_choice:
  "auto", max_completion_tokens (NOT the older/deprecated "max_tokens"
  field name - current field name as of this verification).
- Response: choices[0].message (role "assistant", content may be None,
  tool_calls: [{"id","type":"function","function":{"name",
  "arguments": "<JSON-encoded string, must json.loads() it}}]),
  choices[0].finish_reason in {"stop","tool_calls","length",
  "content_filter"}.
- Tool result reported back as a NEW message: {"role": "tool",
  "tool_call_id": <id from the tool_call>, "content": <string>}.
"""
import json

import clr
clr.AddReference("System.Net.Http")
from System.Net.Http import HttpClient, HttpRequestMessage, HttpMethod, StringContent
from System import TimeSpan
from System.Text import Encoding

REQUEST_TIMEOUT_SECONDS = 180


def _post_json(url, headers, payload):
    """Shared raw-HTTPS POST, reused by every provider below - same
    proven .NET HttpClient pattern as acc_auth.py's token exchange and
    this file's own original Anthropic-only call. Returns the parsed
    JSON body; raises a plain Exception with a clear message on any
    transport or non-success-status failure (callers catch this)."""
    client = HttpClient()
    client.Timeout = TimeSpan.FromSeconds(REQUEST_TIMEOUT_SECONDS)
    request = HttpRequestMessage(HttpMethod.Post, url)
    for key, value in headers.items():
        request.Headers.Add(key, value)
    request.Content = StringContent(json.dumps(payload), Encoding.UTF8, "application/json")
    try:
        response = client.SendAsync(request).Result
        response_body = response.Content.ReadAsStringAsync().Result
    except Exception as e:
        raise Exception("Could not reach {0}: {1}".format(url, e))
    if not response.IsSuccessStatusCode:
        raise Exception("API error ({0}): {1}".format(int(response.StatusCode), response_body))
    return json.loads(response_body)


class Turn(object):
    """Normalized result of one API round-trip, the same shape
    regardless of which provider produced it - this is what lets
    AIConversation.send()'s loop (lib/dee_ai_service.py) stay provider-
    agnostic.

    finish: "tool_calls" | "end_turn" | "max_tokens" | "refused" | "continue_same"
    assistant_message: the provider-shaped history entry to append AS-IS
        (each provider's own append/replay format - never reconstructed
        by hand here, to avoid subtly diverging from what that provider
        expects to see echoed back).
    tool_calls: [{"id": ..., "name": ..., "input": {...}}] - normalized
        across providers regardless of their own wire shape for this.
    text: final assistant text, only meaningful when finish is
        "end_turn" or "max_tokens".
    refusal_detail: (category, explanation) - only meaningful when
        finish == "refused".
    """
    def __init__(self, finish, assistant_message=None, tool_calls=None, text="", refusal_detail=None):
        self.finish = finish
        self.assistant_message = assistant_message
        self.tool_calls = tool_calls or []
        self.text = text
        self.refusal_detail = refusal_detail


# ==========================================================================
# Anthropic (Claude) - the original provider, unchanged behavior, just
# moved out of dee_ai_service.py into this common-shape file.
# ==========================================================================
_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"


def _anthropic_tool_schema(tool_def):
    return {
        "name": tool_def["name"],
        "description": tool_def["description"],
        "input_schema": tool_def["input_schema"],
    }


def _anthropic_build_payload(model, system_prompt, messages, tool_def, max_tokens, effort, base_url=None):
    return {
        "model": model,
        "max_tokens": max_tokens,
        "system": system_prompt,
        "output_config": {"effort": effort},
        "tools": [_anthropic_tool_schema(tool_def)],
        "messages": messages,
    }


def _anthropic_send(api_key, payload, base_url=None):
    headers = {
        "x-api-key": api_key,
        "anthropic-version": _ANTHROPIC_VERSION,
    }
    return _post_json(_ANTHROPIC_URL, headers, payload)


def _anthropic_interpret(data):
    stop_reason = data.get("stop_reason")
    content = data.get("content") or []

    if stop_reason == "refusal":
        details = data.get("stop_details") or {}
        return Turn("refused", assistant_message={"role": "assistant", "content": content},
                    refusal_detail=(details.get("category"), details.get("explanation")))

    if stop_reason == "pause_turn":
        return Turn("continue_same", assistant_message={"role": "assistant", "content": content})

    if stop_reason == "tool_use":
        tool_calls = [
            {"id": b.get("id"), "name": b.get("name"), "input": b.get("input") or {}}
            for b in content if b.get("type") == "tool_use"
        ]
        return Turn("tool_calls", assistant_message={"role": "assistant", "content": content},
                    tool_calls=tool_calls)

    text = "\n".join(b.get("text", "") for b in content if b.get("type") == "text" and b.get("text"))
    finish = "max_tokens" if stop_reason == "max_tokens" else "end_turn"
    return Turn(finish, assistant_message={"role": "assistant", "content": content}, text=text)


def _anthropic_tool_result_entries(results):
    """One Anthropic message carrying ALL of this round's tool results
    together (parallel tool use requires this - never split across
    separate messages)."""
    return [{
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": r["id"], "content": r["output"], "is_error": not r["ok"]}
            for r in results
        ],
    }]


# ==========================================================================
# OpenAI (ChatGPT) / "Custom" OpenAI-compatible - same wire format,
# only the base_url (and therefore which account/service ultimately
# handles the request) differs.
# ==========================================================================
_OPENAI_DEFAULT_URL = "https://api.openai.com/v1/chat/completions"


def _openai_tool_schema(tool_def):
    schema = dict(tool_def["input_schema"])
    schema["additionalProperties"] = False
    return {
        "type": "function",
        "function": {
            "name": tool_def["name"],
            "description": tool_def["description"],
            "parameters": schema,
        },
        "strict": True,
    }


def _openai_build_payload(model, system_prompt, messages, tool_def, max_tokens, effort, base_url=None):
    full_messages = [{"role": "system", "content": system_prompt}] + messages
    return {
        "model": model,
        "messages": full_messages,
        "tools": [_openai_tool_schema(tool_def)],
        "tool_choice": "auto",
        "max_completion_tokens": max_tokens,
    }


def _openai_send(api_key, payload, base_url=None):
    url = base_url or _OPENAI_DEFAULT_URL
    headers = {"Authorization": "Bearer {0}".format(api_key)} if api_key else {}
    return _post_json(url, headers, payload)


def _openai_interpret(data):
    choices = data.get("choices") or []
    if not choices:
        return Turn("end_turn", assistant_message={"role": "assistant", "content": ""}, text="(empty response)")

    choice = choices[0]
    message = choice.get("message") or {}
    finish_reason = choice.get("finish_reason")

    if finish_reason == "content_filter":
        return Turn("refused", assistant_message=message,
                     refusal_detail=("content_filter", "The provider's content filter blocked this response."))

    if finish_reason == "tool_calls" or message.get("tool_calls"):
        tool_calls = []
        for call in (message.get("tool_calls") or []):
            fn = call.get("function") or {}
            try:
                tool_input = json.loads(fn.get("arguments") or "{}")
            except Exception:
                tool_input = {}
            tool_calls.append({"id": call.get("id"), "name": fn.get("name"), "input": tool_input})
        return Turn("tool_calls", assistant_message=message, tool_calls=tool_calls)

    finish = "max_tokens" if finish_reason == "length" else "end_turn"
    return Turn(finish, assistant_message=message, text=message.get("content") or "")


def _openai_tool_result_entries(results):
    """OpenAI wants ONE message PER tool call (unlike Anthropic's one
    combined message) - role "tool", keyed by tool_call_id."""
    return [
        {"role": "tool", "tool_call_id": r["id"], "content": r["output"]}
        for r in results
    ]


# ==========================================================================
# Registry - add a future provider by adding one entry here, nothing
# else needs to change (same extensibility shape as DeeLazy's
# modules/__init__.py REGISTERED_MODULES).
# ==========================================================================
PROVIDERS = [
    {
        "id": "anthropic",
        "label": "Claude (Anthropic)",
        "needs_base_url": False,
        "default_model": "claude-opus-5",
        "model_choices": ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"],
        "key_hint": "console.anthropic.com",
        "build_payload": _anthropic_build_payload,
        "send": _anthropic_send,
        "interpret": _anthropic_interpret,
        "tool_result_entries": _anthropic_tool_result_entries,
    },
    {
        "id": "openai",
        "label": "ChatGPT (OpenAI)",
        "needs_base_url": False,
        "default_model": "gpt-4o-mini",
        # Deliberately NOT a fixed single choice - see module docstring's
        # NEEDS LIVE VERIFICATION note on current model-ID uncertainty.
        # The combo box built from this is editable; these are starting
        # suggestions only, not an exhaustive or guaranteed-current list.
        "model_choices": ["gpt-4o-mini", "gpt-4o"],
        "key_hint": "platform.openai.com",
        "build_payload": _openai_build_payload,
        "send": _openai_send,
        "interpret": _openai_interpret,
        "tool_result_entries": _openai_tool_result_entries,
    },
    {
        "id": "custom",
        "label": "Custom (OpenAI-compatible API)",
        "needs_base_url": True,
        "default_model": "",
        "model_choices": [],
        "key_hint": "your provider's dashboard",
        "build_payload": _openai_build_payload,
        "send": _openai_send,
        "interpret": _openai_interpret,
        "tool_result_entries": _openai_tool_result_entries,
    },
]


def get_provider(provider_id):
    for p in PROVIDERS:
        if p["id"] == provider_id:
            return p
    return PROVIDERS[0]


def provider_labels():
    return [p["label"] for p in PROVIDERS]


def provider_id_by_label(label):
    for p in PROVIDERS:
        if p["label"] == label:
            return p["id"]
    return PROVIDERS[0]["id"]
