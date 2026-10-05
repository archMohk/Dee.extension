# -*- coding: utf-8 -*-
"""
DeeAI - chat window entry point. Wires the WPF chat UI to
dee_ai_service.AIConversation; all AI-provider wire-format / tool-
execution logic lives in lib/dee_ai_service.py + lib/dee_ai_providers.py
+ lib/dee_ai_sandbox.py.

Per-provider API keys (and the Custom provider's base URL/model, which
are also per-user config rather than source) are remembered locally via
lib/deew_settings.py - the same gitignored, per-machine JSON settings
service every DeeW.Cloud tool already uses, reused here rather than
inventing a new one. Saved right before each use (send_click) and when
the window closes, not on every keystroke - no reason to hit disk for
every character typed into a password field."""
import os

import clr
clr.AddReference("PresentationFramework")
from System.Windows import Visibility

from pyrevit import forms
import dee_branding

import dee_ai_service as ai
import dee_ai_providers as providers
import deew_settings
import dee_telemetry
dee_telemetry.check_access("DeeAI")


_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")

_TOOL_NAME = "DeeAI"
_DEFAULT_SETTINGS = {
    "provider": ai.DEFAULT_PROVIDER,
    "effort": ai.DEFAULT_EFFORT,
    # Per-provider: {"key": "...", "model": "...", "base_url": "..."} -
    # base_url only meaningful for "custom", harmless/unused otherwise.
    "by_provider": {},
}


class _SafeProgress(object):
    """forms.ProgressBar tries to set Window.TaskbarItemInfo on its host
    window - a genuine WPF-level bug (not this extension's code):
    Window.TaskbarItemInfo throws NotImplementedException whenever the
    underlying ITaskbarList::HrInit COM call fails, which is documented
    to happen specifically under Remote Desktop/Terminal Services or a
    custom shell without a taskbar (live-confirmed in DeeSheetLinks).

    Wraps the real forms.ProgressBar and falls back to running with NO
    progress UI at all if entering it fails, so the tool degrades
    gracefully under RDP instead of crashing - everyone else still gets
    the real progress bar exactly as before. `pb.update_progress(...)`/
    `pb.cancelled` are safe no-ops in the fallback case, so callers never
    need an extra branch."""
    def __init__(self, **kwargs):
        self._kwargs = kwargs
        self._real = None

    def __enter__(self):
        try:
            self._real = forms.ProgressBar(**self._kwargs)
            return self._real.__enter__()
        except Exception:
            self._real = None
            return self

    def __exit__(self, exc_type, exc_value, tb):
        if self._real is not None:
            return self._real.__exit__(exc_type, exc_value, tb)
        return False

    @property
    def cancelled(self):
        return False

    def update_progress(self, i, total):
        pass


class DeeAIWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, uiapp):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.uiapp = uiapp
        self.uidoc = uiapp.ActiveUIDocument
        self.doc = self.uidoc.Document
        self._loading = True

        self._settings = deew_settings.load(_TOOL_NAME, _DEFAULT_SETTINGS)

        self.provider_cb.ItemsSource = providers.provider_labels()
        self.effort_cb.ItemsSource = ai.EFFORT_CHOICES
        self.effort_cb.SelectedItem = self._settings.get("effort", ai.DEFAULT_EFFORT)

        saved_provider_id = self._settings.get("provider", ai.DEFAULT_PROVIDER)
        saved_provider = providers.get_provider(saved_provider_id)
        self.provider_cb.SelectedItem = saved_provider["label"]
        self._apply_provider(saved_provider["id"])

        self.Closing += self._on_closing
        self._loading = False
        self._log_line("DeeAI is ready. Pick a provider, enter its API key (remembered after this), type a message below, and click Send.")

    # ---------------- provider switching ----------------
    def _provider_settings(self, provider_id):
        by_provider = self._settings.setdefault("by_provider", {})
        return by_provider.setdefault(provider_id, {})

    def _apply_provider(self, provider_id):
        """Loads this provider's saved key/model/base_url into the UI
        and starts a FRESH conversation - message history is never
        portable between providers (each has its own wire shape), so
        switching providers always means a new conversation, same as
        clicking New Conversation."""
        provider = providers.get_provider(provider_id)
        saved = self._provider_settings(provider_id)

        self.key_hint_tb.Text = "(get a key at {0}, saved locally after first use)".format(provider["key_hint"])
        self.api_key_pb.Password = saved.get("key", "")

        self.base_url_panel.Visibility = Visibility.Visible if provider["needs_base_url"] else Visibility.Collapsed
        self.base_url_tb.Text = saved.get("base_url", "")

        self.model_cb.ItemsSource = provider["model_choices"]
        self.model_cb.Text = saved.get("model") or provider["default_model"]

        self.effort_cb.IsEnabled = (provider_id == "anthropic")

        self._conversation = ai.AIConversation(
            self.doc, self.uidoc, self.uiapp, provider_id=provider_id,
            model=self.model_cb.Text, effort=self.effort_cb.SelectedItem or ai.DEFAULT_EFFORT,
            base_url=self.base_url_tb.Text or None)

    def provider_changed(self, sender, args):
        if self._loading:
            return
        provider_id = providers.provider_id_by_label(self.provider_cb.SelectedItem)
        self._settings["provider"] = provider_id
        self._apply_provider(provider_id)
        self.transcript_tb.Text = ""
        self._log_line("Switched to {0} - started a new conversation (providers can't share history).".format(
            providers.get_provider(provider_id)["label"]))

    # ---------------- transcript logging ----------------
    def _log_line(self, text):
        current = self.transcript_tb.Text
        sep = "\n" if current else ""
        self.transcript_tb.Text = current + sep + text
        self.transcript_tb.CaretIndex = len(self.transcript_tb.Text)
        self.transcript_tb.ScrollToEnd()

    def _on_event(self, kind, text):
        if kind == "code":
            self._log_line("\n> DeeAI runs:\n{0}".format(text))
        elif kind == "result":
            self._log_line("< result:\n{0}".format(text))
        elif kind == "error":
            self._log_line("< error:\n{0}".format(text))

    # ---------------- settings ----------------
    def _save_settings(self):
        provider_id = providers.provider_id_by_label(self.provider_cb.SelectedItem)
        saved = self._provider_settings(provider_id)
        saved["key"] = self.api_key_pb.Password
        saved["model"] = self.model_cb.Text
        saved["base_url"] = self.base_url_tb.Text or ""
        self._settings["provider"] = provider_id
        self._settings["effort"] = self.effort_cb.SelectedItem or ai.DEFAULT_EFFORT
        deew_settings.save(_TOOL_NAME, self._settings)

    # ---------------- handlers ----------------
    def send_click(self, sender, args):
        user_text = (self.input_tb.Text or "").strip()
        if not user_text:
            return
        provider_id = providers.provider_id_by_label(self.provider_cb.SelectedItem)
        provider = providers.get_provider(provider_id)
        api_key = self.api_key_pb.Password
        # A "Custom" endpoint may be a local, unauthenticated server
        # (e.g. Ollama/LM Studio) - only Anthropic/OpenAI genuinely
        # require a key.
        if not api_key and provider_id != "custom":
            forms.alert("Enter your {0} API key first (get one at {1}) - it's remembered locally after this.".format(
                provider["label"], provider["key_hint"]))
            return
        if provider["needs_base_url"] and not (self.base_url_tb.Text or "").strip():
            forms.alert("Enter the Base URL for this Custom provider first.")
            return
        if not (self.model_cb.Text or "").strip():
            forms.alert("Enter a model name first.")
            return

        self._conversation.model = self.model_cb.Text or provider["default_model"]
        self._conversation.effort = self.effort_cb.SelectedItem or ai.DEFAULT_EFFORT
        self._conversation.base_url = self.base_url_tb.Text or None
        self._save_settings()

        self._log_line("\nYou: {0}".format(user_text))
        self.input_tb.Text = ""
        self.status_tb.Text = "Thinking..."

        try:
            with _SafeProgress(title="DeeAI - thinking...", indeterminate=True):
                reply = self._conversation.send(api_key, user_text, on_event=self._on_event)
            self._log_line("\nDeeAI: {0}".format(reply))
            self.status_tb.Text = "Ready."
        except ai.RefusedError as e:
            self._log_line("\n[DeeAI declined this request: {0}]".format(e))
            self.status_tb.Text = "Request declined."
        except Exception as e:
            self._log_line("\n[Error: {0}]".format(e))
            self.status_tb.Text = "Error - see transcript."
            forms.alert("DeeAI request failed:\n{0}".format(e))

    def new_conversation_click(self, sender, args):
        self._conversation.reset()
        self.transcript_tb.Text = ""
        self._log_line("New conversation started - previous context and variables have been cleared.")

    def close_click(self, sender, args):
        self.Close()

    def _on_closing(self, sender, args):
        try:
            self._save_settings()
        except Exception:
            pass


uiapp = __revit__
if uiapp.ActiveUIDocument is None:
    forms.alert("Open a Revit project first.")
else:
    window = DeeAIWindow(_XAML_FILE, uiapp)
    window.ShowDialog()
