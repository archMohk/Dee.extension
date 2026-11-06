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
    Context manager around pyrevit.forms.ProgressBar that degrades to a
    silent no-op when the bar cannot be created. forms.ProgressBar sets
    Window.TaskbarItemInfo, which throws NotImplementedException under
    Remote Desktop / a shell without a taskbar; without this wrapper the
    tool would crash there before doing any work. Use exactly like the
    real bar:

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
class _NullProgress(object):
    """What SafeProgress hands back when the real bar could not be
    created: accepts every attribute the real one has, does nothing."""
    cancelled = False
    title = ""
    step = 1
    max_value = 0

    def update_progress(self, i, total):
        pass

    def update_title(self, title):
        self.title = title

    def __setattr__(self, name, value):
        object.__setattr__(self, name, value)


class SafeProgress(object):
    def __init__(self, **kwargs):
        self._kwargs = kwargs
        self._real = None
        self._null = None

    def __enter__(self):
        try:
            self._real = forms.ProgressBar(**self._kwargs)
            return self._real.__enter__()
        except Exception:
            self._real = None
            self._null = _NullProgress()
            self._null.title = self._kwargs.get("title", "")
            return self._null

    def __exit__(self, exc_type, exc_value, tb):
        if self._real is not None:
            try:
                return self._real.__exit__(exc_type, exc_value, tb)
            except Exception:
                return False
        return False

    # Also usable without `with` (a few tools kept a reference around).
    @property
    def cancelled(self):
        if self._real is not None:
            try:
                return bool(self._real.cancelled)
            except Exception:
                return False
        return False

    def update_progress(self, i, total):
        if self._real is not None:
            try:
                self._real.update_progress(i, total)
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
