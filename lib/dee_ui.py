# -*- coding: utf-8 -*-
"""
dee_ui - the one shared UI / Revit-plumbing kit for Dee.extension tools.

Before this module existed every batch tool carried its own copy of the
same half-dozen helpers (59 copies of _SafeProgress, 12 of _pump, 5+
ElementId shims). They are gathered here once so a fix lands everywhere.

    from dee_ui import SafeProgress, pump, eid_int, require_doc, run_in_group, RunSummary

eid_int(eid)
    Integer value of an ElementId on every Revit release. Revit 2024
    added ElementId.Value (Int64) and Revit 2026 REMOVED IntegerValue, so
    a bare `.IntegerValue` raises AttributeError there. Accepts an
    ElementId, an int, or anything with .Id (an Element).

SafeProgress(**kwargs)
    The one progress bar. With a Dee tool window open it is drawn in a
    strip at the bottom of that window - status line, bar and a Cancel
    button - and the rest of the window is locked until the work ends
    (closing the window = Cancel). With no tool window open it falls back
    to pyRevit's pop-up bar (also with Cancel), and to a silent no-op if
    even that cannot be created (forms.ProgressBar's TaskbarItemInfo
    throws under Remote Desktop). Cancel is always offered. A loop that
    never checks pb.cancelled is stopped at its next update_progress()
    after Cancel. Use exactly like the real bar:

        with SafeProgress(title="Doing {value} of {max_value}", cancellable=True) as pb:
            for i, item in enumerate(items, 1):
                if pb.cancelled:
                    break
                ...
                pb.update_progress(i, len(items))

pump(window)
    Let WPF repaint `window` (progress bars, log text) in the middle of a
    long synchronous loop on the UI thread. One nested DispatcherFrame
    at Background priority - safe to call often.

require_doc(tool_name)
    Returns the active Document, or shows "Open a Revit project first"
    and ends the script. For tools whose bundle.yaml says zero-doc (so
    the button is clickable with nothing open) but that need a document
    after all.

run_in_group(doc, name, fn)
    Runs fn() inside a TransactionGroup so a tool that opens several
    Transactions leaves ONE entry in Revit's undo list. fn may open its
    own Transactions as before. If fn returns False or raises, the group
    rolls back (every Transaction inside it is undone); otherwise it is
    assimilated under `name`. Returns fn's return value.

RunSummary
    Collects per-item outcomes during a batch run and shows one closing
    dialog ("12 done, 3 skipped") listing every skipped item and why -
    replaces `except Exception: pass` with something the user can act on.

message / confirm / error
    Thin wrappers around pyrevit.forms.alert with the extension's title
    convention, so every tool asks the same way. Kept deliberately
    simple; they are the single place to swap in a branded dialog later.
"""
import sys

from pyrevit import forms

try:
    import clr
    clr.AddReference("WindowsBase")
    clr.AddReference("PresentationFramework")
    from System import Action
    from System.Windows.Threading import Dispatcher, DispatcherFrame, DispatcherPriority
except Exception:  # not inside Revit (repo checks, unit tests)
    Action = Dispatcher = DispatcherFrame = DispatcherPriority = None

try:
    from Autodesk.Revit.DB import TransactionGroup
except Exception:
    TransactionGroup = None


APP_TITLE = "Dee.extension"


# ---------------------------------------------------------------------------
# ElementId
# ---------------------------------------------------------------------------
def eid_int(eid):
    """Integer value of an ElementId (or Element, or int) on any Revit
    release. Returns -1 for None / InvalidElementId-like inputs."""
    if eid is None:
        return -1
    if isinstance(eid, (int, long)):
        return int(eid)
    inner = getattr(eid, "Id", None)
    if inner is not None and not hasattr(eid, "IntegerValue") and not hasattr(eid, "Value"):
        eid = inner
    try:
        return int(eid.Value)          # Revit 2024+
    except Exception:
        pass
    try:
        return int(eid.IntegerValue)   # Revit <= 2025
    except Exception:
        pass
    try:
        return int(eid)
    except Exception:
        return -1


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------
class DeeCancelled(BaseException):
    """Raised out of update_progress() to stop a loop that never checks
    `.cancelled` itself (see _ProgressBase). A BaseException on purpose:
    a tool's per-item `except Exception:` must not swallow it and carry
    on with the next item. SafeProgress.__exit__ catches it, so the
    `with` block simply ends."""


def _format_title(template, value, max_value):
    try:
        return template.format(value=value, max_value=max_value)
    except Exception:
        return template


class _ProgressBase(object):
    """The object a `with SafeProgress(...) as pb:` block gets, whichever
    bar is behind it. Same surface as pyrevit.forms.ProgressBar: title,
    update_progress(), cancelled, reset(), indeterminate, step, max_value.

    Cancel handling. A tool that reads `pb.cancelled` after Cancel was
    clicked stops itself, cleanly, its own way. A tool that never looks
    is stopped for it: its next update_progress() raises DeeCancelled,
    which ends the `with` block (SafeProgress swallows it). Transactions
    open inside the block roll back as the exception passes through."""

    _PUMP_INTERVAL = 0.05   # seconds between repaints in a tight loop

    def __init__(self, title="", indeterminate=False, cancellable=True,
                 step=1, auto_stop=True, **_ignored):
        object.__setattr__(self, "_title_template", title or "")
        object.__setattr__(self, "_indeterminate", bool(indeterminate))
        object.__setattr__(self, "_cancellable", True if cancellable is None else bool(cancellable))
        object.__setattr__(self, "_cancel_requested", False)
        object.__setattr__(self, "_cancel_seen", False)
        object.__setattr__(self, "_auto_stop", auto_stop)
        object.__setattr__(self, "_value", 0)
        object.__setattr__(self, "_last_pump", 0.0)
        object.__setattr__(self, "step", step)
        object.__setattr__(self, "max_value", 0)

    # -- pyRevit-compatible surface --
    @property
    def cancelled(self):
        if self._cancel_requested:
            object.__setattr__(self, "_cancel_seen", True)
        return self._cancel_requested

    @property
    def title(self):
        return self._title_template

    @title.setter
    def title(self, value):
        object.__setattr__(self, "_title_template", value or "")
        self._show(force_pump=True)

    @property
    def indeterminate(self):
        return self._indeterminate

    @indeterminate.setter
    def indeterminate(self, value):
        object.__setattr__(self, "_indeterminate", bool(value))
        self._show(force_pump=True)

    def update_title(self, title):
        self.title = title

    def update_progress(self, new_value, max_value=1):
        if (self._cancel_requested and not self._cancel_seen
                and self._auto_stop and self._cancellable):
            raise DeeCancelled()
        object.__setattr__(self, "_value", new_value)
        object.__setattr__(self, "max_value", max_value)
        self._show()

    def reset(self):
        object.__setattr__(self, "_value", 0)
        self._show(force_pump=True)

    def request_cancel(self):
        object.__setattr__(self, "_cancel_requested", True)

    # -- backend hooks --
    def _show(self, force_pump=False):
        pass

    def _current_title(self):
        return _format_title(self._title_template, self._value, self.max_value)


class _WindowProgress(_ProgressBase):
    """Draws into the strip at the bottom of an open Dee window."""

    def __init__(self, window, **kwargs):
        _ProgressBase.__init__(self, **kwargs)
        object.__setattr__(self, "_window", window)

    def open(self):
        import dee_branding
        dee_branding.progress_begin(self._window, self._cancellable, self.request_cancel)
        self._show(force_pump=True)
        return self

    def close(self):
        import dee_branding
        try:
            dee_branding.progress_end(self._window, self.request_cancel)
        except Exception:
            pass
        pump(self._window)

    def _show(self, force_pump=False):
        import time
        import dee_branding
        try:
            dee_branding.progress_set(
                self._window, title=self._current_title(),
                value=self._value, maximum=self.max_value,
                indeterminate=self._indeterminate)
        except Exception:
            return
        now = time.time()
        if force_pump or now - self._last_pump >= self._PUMP_INTERVAL:
            object.__setattr__(self, "_last_pump", now)
            # Repaints the strip AND lets a Cancel click through.
            pump(self._window)


class _PopupProgress(_ProgressBase):
    """pyrevit.forms.ProgressBar, for work that runs with no Dee window
    on screen. Always offers Cancel, like the in-window strip."""

    def __init__(self, **kwargs):
        _ProgressBase.__init__(self, **kwargs)
        object.__setattr__(self, "_bar", None)

    def open(self):
        bar = forms.ProgressBar(title=self._title_template,
                                indeterminate=self._indeterminate,
                                cancellable=self._cancellable, step=self.step)
        bar.__enter__()
        object.__setattr__(self, "_bar", bar)
        return self

    def close(self):
        try:
            if self._bar is not None:
                self._bar.__exit__(None, None, None)
        except Exception:
            pass

    @property
    def cancelled(self):
        try:
            if self._bar is not None and self._bar.cancelled:
                object.__setattr__(self, "_cancel_requested", True)
        except Exception:
            pass
        return _ProgressBase.cancelled.fget(self)

    def update_progress(self, new_value, max_value=1):
        try:
            if self._bar is not None and self._bar.cancelled:
                object.__setattr__(self, "_cancel_requested", True)
        except Exception:
            pass
        _ProgressBase.update_progress(self, new_value, max_value)

    def _show(self, force_pump=False):
        bar = self._bar
        if bar is None:
            return
        try:
            bar.title = self._title_template
        except Exception:
            pass
        try:
            bar.indeterminate = self._indeterminate
        except Exception:
            pass
        try:
            bar.update_progress(self._value, self.max_value or 1)
        except Exception:
            pass


class _NullProgress(_ProgressBase):
    """What SafeProgress hands back when no bar could be created at all:
    accepts everything the real ones do, draws nothing."""

    def open(self):
        return self

    def close(self):
        pass


def open_progress(window=None, **kwargs):
    """An opened progress object without a `with` block - for the few
    wrappers that keep one around. Call .close() when done. Never raises
    DeeCancelled (no `with` to catch it): check .cancelled instead."""
    kwargs.setdefault("auto_stop", False)
    if window is None:
        try:
            import dee_branding
            window = dee_branding.active_window()
        except Exception:
            window = None
    if window is not None:
        try:
            return _WindowProgress(window, **kwargs).open()
        except Exception:
            pass
    try:
        return _PopupProgress(**kwargs).open()
    except Exception:
        # e.g. forms.ProgressBar's TaskbarItemInfo throwing under Remote
        # Desktop - the reason this wrapper exists in the first place.
        return _NullProgress(**kwargs).open()


class SafeProgress(object):
    """`with SafeProgress(title=..., cancellable=True) as pb:` - the one
    progress bar every tool uses. With a Dee tool window open it is the
    strip at the bottom of that window (status, bar, Cancel); otherwise
    pyRevit's pop-up bar; if neither can be created, a silent no-op.
    Cancel is always offered. Pass window= to target a specific window."""

    def __init__(self, window=None, **kwargs):
        self._window = window
        self._kwargs = kwargs
        self._pb = None

    def __enter__(self):
        kwargs = dict(self._kwargs)
        kwargs.setdefault("auto_stop", True)
        self._pb = open_progress(window=self._window, **kwargs)
        return self._pb

    def __exit__(self, exc_type, exc_value, tb):
        try:
            if self._pb is not None:
                self._pb.close()
        except Exception:
            pass
        # A loop stopped by Cancel ends the block quietly.
        return exc_type is not None and issubclass(exc_type, DeeCancelled)

    # Also usable without `with` (a few tools kept a reference around).
    @property
    def cancelled(self):
        try:
            return bool(self._pb.cancelled) if self._pb is not None else False
        except Exception:
            return False

    def update_progress(self, i, total):
        try:
            if self._pb is not None:
                self._pb.update_progress(i, total)
        except DeeCancelled:
            raise
        except Exception:
            pass


def pump(window=None, priority=None):
    """Process pending WPF messages so `window` repaints mid-loop."""
    if DispatcherFrame is None:
        return
    try:
        dispatcher = window.Dispatcher if window is not None else Dispatcher.CurrentDispatcher
        frame = DispatcherFrame()

        def _stop():
            frame.Continue = False

        dispatcher.BeginInvoke(priority or DispatcherPriority.Background, Action(_stop))
        Dispatcher.PushFrame(frame)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------
def active_doc(uiapp=None):
    """Active Document or None - never raises."""
    try:
        if uiapp is None:
            import __main__
            uiapp = getattr(__main__, "__revit__", None)
        uidoc = uiapp.ActiveUIDocument if uiapp is not None else None
        return uidoc.Document if uidoc is not None else None
    except Exception:
        return None


def require_doc(tool_name, uiapp=None, allow_family=True):
    """Return the active Document, or alert and end the script."""
    doc = active_doc(uiapp)
    if doc is None:
        forms.alert("Open a Revit project first.", title=tool_name or APP_TITLE)
        sys.exit()
    if not allow_family and getattr(doc, "IsFamilyDocument", False):
        forms.alert("{0} works on a project, not inside the Family Editor."
                    .format(tool_name or "This tool"), title=tool_name or APP_TITLE)
        sys.exit()
    return doc


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------
def run_in_group(doc, name, fn, *args, **kwargs):
    """Run fn(*args, **kwargs) inside one TransactionGroup named `name`.
    Rolls back if fn raises or returns False; assimilates otherwise, so
    Revit's undo shows ONE entry for the whole run."""
    if TransactionGroup is None or doc is None:
        return fn(*args, **kwargs)
    group = TransactionGroup(doc, name)
    group.Start()
    ok = False
    try:
        result = fn(*args, **kwargs)
        ok = result is not False
        return result
    finally:
        try:
            if ok and group.GetStatus().ToString() == "Started":
                group.Assimilate()
            elif group.GetStatus().ToString() == "Started":
                group.RollBack()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Batch outcome reporting
# ---------------------------------------------------------------------------
class RunSummary(object):
    """Collect what happened to each item of a batch, then show it once.

        summary = RunSummary("DeeCleaner")
        for v in views:
            try:
                ...
                summary.ok(v.Name)
            except Exception as e:
                summary.skip(v.Name, e)
        summary.show()
    """
    def __init__(self, tool_name, max_listed=25):
        self.tool_name = tool_name
        self.max_listed = max_listed
        self.done = []
        self.skipped = []   # (label, reason)

    def ok(self, label=None):
        self.done.append(label)

    def skip(self, label, reason=None):
        self.skipped.append((label, _short(reason)))

    @property
    def text(self):
        lines = ["{0} done, {1} skipped.".format(len(self.done), len(self.skipped))]
        if self.skipped:
            lines.append("")
            for label, reason in self.skipped[:self.max_listed]:
                lines.append(u"- {0}: {1}".format(label, reason) if reason else u"- {0}".format(label))
            if len(self.skipped) > self.max_listed:
                lines.append(u"... and {0} more".format(len(self.skipped) - self.max_listed))
        return u"\n".join(lines)

    def show(self, title=None, only_if_skipped=False):
        if only_if_skipped and not self.skipped:
            return
        forms.alert(self.text, title=title or self.tool_name)


def _short(reason, limit=140):
    if reason is None:
        return ""
    try:
        text = reason if isinstance(reason, basestring) else str(reason)
    except Exception:
        text = repr(reason)
    text = text.strip().splitlines()[0] if text.strip() else ""
    return text[:limit]


# ---------------------------------------------------------------------------
# Dialogs
# ---------------------------------------------------------------------------
def message(text, title=None):
    forms.alert(text, title=title or APP_TITLE)


def error(text, title=None):
    forms.alert(text, title=title or APP_TITLE, warn_icon=True)


def confirm(text, title=None, yes="Yes", no="No"):
    """True if the user picked `yes`."""
    return forms.alert(text, title=title or APP_TITLE, options=[yes, no]) == yes
