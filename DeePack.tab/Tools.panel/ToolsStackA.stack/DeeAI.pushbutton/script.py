# -*- coding: utf-8 -*-
"""
DeeAI - chat window entry point. Wires the WPF chat UI to
dee_ai_service.AIConversation; all Claude API / tool-execution logic
lives in lib/dee_ai_service.py + lib/dee_ai_sandbox.py.
"""
import os

from pyrevit import forms
import dee_branding

import dee_ai_service as ai
import dee_telemetry
dee_telemetry.check_access("DeeAI")


_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")


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

        self.model_cb.ItemsSource = ai.MODEL_CHOICES
        self.model_cb.SelectedItem = ai.DEFAULT_MODEL
        self.effort_cb.ItemsSource = ai.EFFORT_CHOICES
        self.effort_cb.SelectedItem = ai.DEFAULT_EFFORT

        self._conversation = ai.AIConversation(
            self.doc, self.uidoc, self.uiapp,
            model=ai.DEFAULT_MODEL, effort=ai.DEFAULT_EFFORT)

        self._log_line("DeeAI is ready. Enter your API key above, type a message below, and click Send.")

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

    # ---------------- handlers ----------------
    def send_click(self, sender, args):
        user_text = (self.input_tb.Text or "").strip()
        if not user_text:
            return
        api_key = self.api_key_pb.Password
        if not api_key:
            forms.alert("Enter your Anthropic API key first - it is not saved and must be re-entered each session.")
            return

        self._conversation.model = self.model_cb.SelectedItem or ai.DEFAULT_MODEL
        self._conversation.effort = self.effort_cb.SelectedItem or ai.DEFAULT_EFFORT

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


uiapp = __revit__
if uiapp.ActiveUIDocument is None:
    forms.alert("Open a Revit project first.")
else:
    window = DeeAIWindow(_XAML_FILE, uiapp)
    window.ShowDialog()
