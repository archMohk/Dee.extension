# -*- coding: utf-8 -*-
"""
dee_ai_sandbox
Executes AI-generated IronPython code against the live Revit document.
This is DeeAI's one and only "take action" capability - by explicit
user choice there is no per-action confirmation dialog (see
docs/pages/DeeAI.html for the tradeoff), so the safety net here is
purely transactional, not a gate:

- Every user chat turn runs inside ONE outer Transaction, so a single
  Ctrl+Z in Revit undoes everything the AI did in response to one
  message - matching this extension's "single transaction per command"
  convention, just applied at the level of "one command = one chat
  turn" instead of one button click.
- Every individual tool call additionally runs inside its own
  SubTransaction nested in that Transaction, so one bad/failing call
  only rolls back its own change - it never discards or corrupts
  successful calls that already happened earlier in the same turn.

Exec globals (doc/uidoc/uiapp) are built ONCE per conversation and
reused for every call within it - variables the AI sets in one call
are still there in the next, like a REPL/notebook, so it doesn't need
to re-collect elements it already looked up earlier in the same chat.
"""
import sys
import traceback

from Autodesk.Revit.DB import Transaction, SubTransaction

try:
    from StringIO import StringIO
except ImportError:
    from io import StringIO


def build_exec_globals(doc, uidoc, uiapp):
    return {
        "__builtins__": __builtins__,
        "doc": doc,
        "uidoc": uidoc,
        "uiapp": uiapp,
        "__revit__": uiapp,
    }


class TurnTransaction(object):
    """Context manager for one chat turn:
        with TurnTransaction(doc, "DeeAI") as turn:
            ok, output = turn.run(code, exec_globals)
    Commits on normal exit (including hitting the tool-call iteration
    cap with partial progress made); rolls back the WHOLE turn only if
    an unhandled exception escapes the loop around it (e.g. a network
    or API failure) - individual tool-call failures are isolated by
    their own SubTransaction and never reach here."""

    def __init__(self, doc, name):
        self.doc = doc
        self._t = Transaction(doc, name)

    def __enter__(self):
        self._t.Start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type is None:
                self._t.Commit()
            else:
                self._t.RollBack()
        except Exception:
            pass
        return False

    def run(self, code, exec_globals):
        """Runs one tool call's code in its own SubTransaction. Never
        raises - returns (ok, output_text): captured stdout on
        success, or the traceback text on failure."""
        sub = SubTransaction(self.doc)
        sub.Start()
        old_stdout = sys.stdout
        buf = StringIO()
        sys.stdout = buf
        try:
            exec(code, exec_globals)
            sys.stdout = old_stdout
            sub.Commit()
            output = buf.getvalue()
            return True, (output if output.strip() else "(no output - code ran without printing anything)")
        except Exception:
            sys.stdout = old_stdout
            tb = traceback.format_exc()
            try:
                sub.RollBack()
            except Exception:
                pass
            return False, tb
