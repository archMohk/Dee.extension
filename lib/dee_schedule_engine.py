# -*- coding: utf-8 -*-
"""
dee_schedule_engine
Runs DeeSchedule tasks inside Revit. Storage, timing and transfer live in
lib/dee_schedule_core.py; the windows in lib/dee_schedule_ui.py.

--------------------------------------------------------------------
One runner per tool: the tool's own script.py
--------------------------------------------------------------------
A scheduled export must do exactly what the button does, so nothing is
re-implemented here. The tool's script.py is executed with
__dee_scheduled__ set (it then defines everything but opens no window and
shows no prompt) and its scheduled_run(ctx, task) is called:

    ctx.open_model(src)   Document - the open one, or opened for this run
    ctx.progress(i, n, text)   False once the run should stop
    ctx.cancelled         same answer, without moving the bar
    ctx.folder()          this run's output folder (created)
    ctx.add(ok, label, detail)   one result line; ok True / False / None

--------------------------------------------------------------------
When tasks run
--------------------------------------------------------------------
start_watching() (startup.py) starts a DispatcherTimer that looks at the
store every 20 s and raises an ExternalEvent when something is due.
Revit runs that event only when it is idle - never in the middle of a
command, a dialog or a Dee tool - and it is a full API context, so models
can be opened and exported. A plain Idling handler would not do: with no
mouse or keyboard activity (overnight) Idling stops firing, while the
timer keeps Revit's message loop turning.

Revit has to be running. A task whose time passes while Revit is closed
runs at the next start, or is skipped, as the task says.

--------------------------------------------------------------------
Which copy of the model
--------------------------------------------------------------------
A model open in this session is exported as it is on screen. Any other is
opened in the background DETACHED, exported, and closed without saving,
so a scheduled run never locks, syncs or changes a central model.
force_copy=True (DeeTransmit, which deletes content) never touches an
open model: it always works on a detached or temporary copy.
"""
import datetime
import os
import shutil
import sys
import tempfile
import traceback

import System
import clr
clr.AddReference("WindowsBase")
clr.AddReference("PresentationCore")
from System import TimeSpan
from System.Windows.Media import Color
from System.Windows.Threading import DispatcherTimer

from Autodesk.Revit.DB import ModelPathUtils
from Autodesk.Revit.UI import IExternalEventHandler, ExternalEvent

import dee_schedule_core as core

_LIB_DIR = os.path.dirname(os.path.abspath(__file__))
_TAB_DIR = os.path.join(os.path.dirname(_LIB_DIR), "DeePack.tab")
_WATCH_SLOT = "dee_schedule.watcher"
_BUSY_SLOT = "dee_schedule.busy"
_POLL_SECONDS = 20

_OK = Color.FromRgb(0x2E, 0x7D, 0x32)
_WARN = Color.FromRgb(0xF2, 0x99, 0x4D)
_FAIL = Color.FromRgb(0xC6, 0x28, 0x28)


def revit_year(uiapp):
    try:
        return str(uiapp.Application.VersionNumber)
    except Exception:
        return ""


def is_busy():
    """True while any scheduled run is in progress in this Revit."""
    try:
        return bool(System.AppDomain.CurrentDomain.GetData(_BUSY_SLOT))
    except Exception:
        return False


def _set_busy(value):
    System.AppDomain.CurrentDomain.SetData(_BUSY_SLOT, bool(value))


def _toast(title, text, color):
    try:
        import dee_toast
        dee_toast.show_toast(u"DEESCHEDULE", title, text, color)
    except Exception:
        pass


# --------------------------------------------------------------------------
# which model
# --------------------------------------------------------------------------
def source_from_doc(doc):
    """What a task has to remember to find this model again. Raises
    ScheduleError for a model that cannot be found again."""
    label = doc.Title
    try:
        in_cloud = bool(doc.IsModelInCloud)
    except Exception:
        in_cloud = False
    if in_cloud:
        path = doc.GetCloudModelPath()
        return {"kind": "cloud", "label": label,
                "region": _region_of(path),
                "project": str(path.GetProjectGUID()),
                "model": str(path.GetModelGUID())}
    path = _central_path(doc) or doc.PathName
    if not path:
        raise core.ScheduleError(
            u"'{0}' has never been saved, so a scheduled run could not find it. "
            u"Save it first.".format(label))
    return {"kind": "file", "label": label, "path": path}


def source_from_acc(region, project_id, item_id, token, label):
    """The same for a file picked from an ACC project. The GUIDs are
    resolved now, so the run itself needs no ACC token - only the Autodesk
    sign-in Revit already has."""
    import acc_file_browser as afb
    project, model, _src = afb.get_cloud_path_guids(project_id, item_id, token)
    return {"kind": "cloud", "label": label, "region": region or "US",
            "project": str(project), "model": str(model)}


def source_from_file(path):
    return {"kind": "file", "label": os.path.splitext(os.path.basename(path))[0],
            "path": path}


def _region_of(path):
    try:
        return path.Region or "US"
    except Exception:
        return "US"


def _central_path(doc):
    try:
        if doc.IsWorkshared and not doc.IsDetached:
            mp = doc.GetWorksharingCentralModelPath()
            if mp is not None:
                return ModelPathUtils.ConvertModelPathToUserVisiblePath(mp) or ""
    except Exception:
        pass
    return ""


def _same_path(a, b):
    return bool(a) and bool(b) and os.path.normcase(os.path.normpath(a)) == \
        os.path.normcase(os.path.normpath(b))


def _project_docs(app):
    docs = []
    for doc in app.Documents:
        try:
            if not doc.IsLinked and not doc.IsFamilyDocument:
                docs.append(doc)
        except Exception:
            continue
    return docs


def find_open(app, src):
    """The open document this source means, or None. A detached copy never
    counts - it is somebody's scratch work, not the model."""
    for doc in _project_docs(app):
        try:
            if bool(doc.IsDetached):
                continue
        except Exception:
            pass
        try:
            if src["kind"] == "cloud":
                if doc.IsModelInCloud and \
                        str(doc.GetCloudModelPath().GetModelGUID()).lower() == src["model"].lower():
                    return doc
            elif _same_path(doc.PathName, src["path"]) or \
                    _same_path(_central_path(doc), src["path"]):
                return doc
        except Exception:
            continue
    return None


# --------------------------------------------------------------------------
# one run
# --------------------------------------------------------------------------
class RunContext(object):
    def __init__(self, uiapp, task, progress):
        self.uiapp = uiapp
        self.app = uiapp.Application
        self.task = task
        self.results = []
        self.lines = []
        self._progress = progress
        self._opened = []
        self._temp = []
        self._folder = None

    # ---- reporting
    def log(self, text):
        self.lines.append(u"{0}  {1}".format(
            datetime.datetime.now().strftime("%H:%M:%S"), text))

    def add(self, ok, label, detail=""):
        self.results.append((ok, label, detail))
        mark = {True: "OK", False: "FAILED", None: "SKIPPED"}.get(ok, "")
        self.log(u"[{0}] {1}  {2}".format(mark, label, detail))

    @property
    def cancelled(self):
        try:
            if self._progress is not None and self._progress.cancelled:
                return True
        except Exception:
            pass
        return core.stop_requested(self.task["id"])

    def progress(self, i, total, text=""):
        try:
            if self._progress is not None:
                if text:
                    self._progress.title = u"DeeSchedule - {0}: {1}".format(
                        self.task["name"], text)
                self._progress.update_progress(i, max(1, total))
        except Exception:
            pass
        return not self.cancelled

    # ---- output
    def folder(self):
        if self._folder is None:
            base = self.task.get("output") or ""
            if not base or base.startswith("%"):
                raise core.ScheduleError("The task has no valid output folder.")
            if self.task.get("dated_folder"):
                base = os.path.join(base, core.now().strftime("%Y-%m-%d_%H.%M"))
            if not os.path.isdir(base):
                os.makedirs(base)
            self._folder = base
        return self._folder

    # ---- models
    def open_model(self, src, detach="preserve", force_copy=False):
        """The Document for one source. Raises with a readable reason."""
        label = src.get("label") or ""
        existing = find_open(self.app, src)
        if existing is not None and not force_copy:
            if existing.IsModifiable:
                raise core.ScheduleError(u"'{0}' is being edited right now.".format(label))
            self.log(u"Using the open model '{0}'".format(existing.Title))
            return existing

        before = list(self.app.Documents)
        self.progress(0, 1, u"opening {0}".format(label))
        if src["kind"] == "cloud":
            doc = self._open_cloud(src, detach)
        else:
            doc = self._open_file(src, detach, existing)
        # OpenDocumentFile hands back the live document when that file is
        # already open. It is not ours: never close it, never strip it.
        if any(doc.Equals(d) for d in before):
            if force_copy:
                raise core.ScheduleError(
                    u"'{0}' is open in this Revit, so a separate copy could not be "
                    u"opened. Close it and run again.".format(label))
            return doc
        self._opened.append(doc)
        self.log(u"Opened '{0}' in the background".format(label))
        return doc

    def _options(self, detach):
        import deew_document_manager as dm
        return dm.build_open_options(detach, audit=False, open_all_worksets=True)

    def _open_cloud(self, src, detach):
        path = ModelPathUtils.ConvertCloudGUIDsToCloudPath(
            src.get("region") or "US", System.Guid(src["project"]), System.Guid(src["model"]))
        return self.app.OpenDocumentFile(path, self._options(detach))

    def _open_file(self, src, detach, existing):
        path = src["path"]
        if "://" not in path and not os.path.isfile(path):
            raise core.ScheduleError(u"Model not found: {0}".format(path))
        workshared = True
        if "://" not in path:
            try:
                from Autodesk.Revit.DB import BasicFileInfo
                workshared = bool(BasicFileInfo.Extract(path).IsWorkshared)
            except Exception:
                workshared = False
            if existing is not None and not workshared:
                # Revit will not open the same plain file twice.
                folder = tempfile.mkdtemp(prefix="deeschedule_")
                self._temp.append(folder)
                copy = os.path.join(folder, os.path.basename(path))
                shutil.copyfile(path, copy)
                path = copy
        model_path = ModelPathUtils.ConvertUserVisiblePathToModelPath(path)
        return self.app.OpenDocumentFile(
            model_path, self._options(detach if workshared else "none"))

    def close_model(self, doc):
        """Closes a model this run opened, without saving. Anything else
        is left exactly as it is."""
        for mine in list(self._opened):
            if mine is doc:
                self._opened.remove(mine)
                try:
                    mine.Close(False)
                except Exception as e:
                    self.log("Could not close a model: {0}".format(e))

    def cleanup(self):
        for doc in list(self._opened):
            self.close_model(doc)
        for folder in self._temp:
            shutil.rmtree(folder, ignore_errors=True)
        self._temp = []


def find_script(tool):
    """<tool>.pushbutton/script.py wherever it sits on the ribbon."""
    want = tool + ".pushbutton"
    for root, dirs, _files in os.walk(_TAB_DIR):
        if want in dirs:
            path = os.path.join(root, want, "script.py")
            if os.path.isfile(path):
                return path
    return None


def load_runner(uiapp, tool):
    """The tool's script.py namespace, loaded without opening anything."""
    path = find_script(tool)
    if path is None:
        raise core.ScheduleError("{0} is not installed on this PC.".format(tool))
    for folder in (_LIB_DIR, os.path.dirname(path)):
        if folder not in sys.path:
            sys.path.append(folder)
    space = {"__file__": path, "__name__": "dee_scheduled_" + tool.lower(),
             "__revit__": uiapp, "__dee_scheduled__": True}
    execfile(path, space)  # noqa: F821 - IronPython 2.7
    if "scheduled_run" not in space:
        raise core.ScheduleError("{0} cannot be scheduled.".format(tool))
    return space


def _ensure_revit_builtin(uiapp):
    """pyRevit may have cleared its builtins once startup.py ended; some
    shared modules read __revit__ from there."""
    try:
        import __builtin__
        if getattr(__builtin__, "__revit__", None) is None:
            __builtin__.__revit__ = uiapp
    except Exception:
        pass


def run_task(uiapp, task_id, manual=False, window=None):
    """Runs one task now. Returns (status, message); never raises."""
    if is_busy():
        return "skipped", "Another scheduled task is running - try again when it ends."
    task = core.get_task(task_id)
    if task is None:
        return "skipped", "That task no longer exists."
    if not core.year_matches(task, revit_year(uiapp)):
        return "skipped", "This task runs in Revit {0}; this is Revit {1}.".format(
            task.get("revit_year"), revit_year(uiapp))

    lock = core.RunLock(task_id)
    if not lock.acquired:
        return "skipped", "This task is already running in another Revit."
    _set_busy(True)
    try:
        _ensure_revit_builtin(uiapp)
        task = core.begin_run(task_id, manual=manual)
        if task is None:
            return "skipped", "Not due any more."
        status, message, output = _execute(uiapp, task, window)
        core.end_run(task_id, status, message, output)
    except Exception as e:
        status, message = "failed", u"{0}".format(e)
        try:
            core.end_run(task_id, status, message)
        except Exception:
            pass
    finally:
        _set_busy(False)
        lock.release()
        core.clear_stop(task_id)

    try:
        if core.settings().get("notify", True):
            color = _OK if status == "ok" else (_FAIL if status == "failed" else _WARN)
            _toast(task["name"], u"{0} - {1}".format(
                core.RESULT_LABELS.get(status, status), message), color)
    except Exception:
        pass
    return status, message


def _execute(uiapp, task, window):
    """(status, message, output folder) for one started run."""
    import dee_known_dialogs
    import dee_telemetry
    import dee_ui

    allowed, reason = dee_telemetry.is_allowed(task["tool"])
    if not allowed:
        return "failed", reason, ""

    progress = dee_ui.open_progress(
        window=window, title=u"DeeSchedule - {0}".format(task["name"]), cancellable=True)
    ctx = RunContext(uiapp, task, progress)
    ctx.log(u"{0}: {1}".format(task["tool"], task["name"]))
    previous = dee_known_dialogs.delete_errors_enabled()
    dialogs = []
    handler = None
    crashed = ""
    try:
        space = load_runner(uiapp, task["tool"])
        make_handler = space.get("scheduled_dialog_handler")
        if make_handler is None:
            import acc_file_browser as afb
            make_handler = afb.make_dialog_handler
        handler = make_handler(dialogs)
        uiapp.DialogBoxShowing += handler
        dee_known_dialogs.attach_failures(
            uiapp, delete_errors=bool(task["payload"].get("delete_errors")))
        space["scheduled_run"](ctx, task)
    except dee_ui.DeeCancelled:
        pass
    except Exception as e:
        crashed = u"{0}".format(e)
        ctx.log(crashed)
        if not isinstance(e, core.ScheduleError):
            ctx.log(traceback.format_exc())
    finally:
        # Models are closed while the dialog handler is still listening.
        try:
            ctx.cleanup()
        except Exception as e:
            ctx.log("Clean-up: {0}".format(e))
        if handler is not None:
            try:
                uiapp.DialogBoxShowing -= handler
            except Exception:
                pass
            dee_known_dialogs.detach_failures(uiapp)
        dee_known_dialogs.set_delete_errors(previous)
        cancelled = ctx.cancelled
        try:
            progress.close()
        except Exception:
            pass

    for entry in dialogs:
        ctx.log(u"Revit dialog answered automatically: {0}".format(
            u" ".join(u"{0}".format(x) for x in entry if not isinstance(x, bool))))
    status, message = summarize(ctx.results, crashed, cancelled)
    ctx.log(u"{0} - {1}".format(core.RESULT_LABELS.get(status, status), message))
    core.write_log(task["id"], ctx.lines)
    return status, message, ctx._folder or ""


def summarize(results, crashed, cancelled):
    good = len([r for r in results if r[0] is True])
    bad = len([r for r in results if r[0] is False])
    if crashed:
        done = "{0} exported before it stopped: ".format(good) if good else ""
        return ("partial" if good else "failed"), done + crashed
    message = "{0} exported".format(good)
    if bad:
        first = [r for r in results if r[0] is False][0]
        message += u", {0} failed ({1}: {2})".format(bad, first[1], first[2])
    if cancelled:
        return "cancelled", message
    if bad:
        return ("partial" if good else "failed"), message
    if not good:
        skipped = [r for r in results if r[0] is None]
        return "nothing", u"{0}".format(skipped[0][2]) if skipped else "Nothing to export"
    return "ok", message


# --------------------------------------------------------------------------
# the watcher
# --------------------------------------------------------------------------
def run_due(uiapp, window=None, only=None):
    """Runs every task that is due, oldest first. `only` limits it to the
    given ids (the ones whose warning time is up)."""
    at = core.now()
    year = revit_year(uiapp)
    core.roll_missed(at, year)
    for task in core.due_tasks(core.load(), at, year):
        if only is not None and task["id"] not in only:
            continue
        run_task(uiapp, task["id"], window=window)


class _RunEvent(IExternalEventHandler):
    def __init__(self):
        self.ready = set()

    def Execute(self, uiapp):
        ids, self.ready = self.ready, set()
        try:
            run_due(uiapp, only=ids)
        except Exception:
            pass

    def GetName(self):
        return "DeeSchedule"


class _Watcher(object):
    """Looks at the store on a timer; warns, then raises the event."""

    def __init__(self, year):
        self.year = year
        self.event_handler = _RunEvent()
        self.event = ExternalEvent.Create(self.event_handler)
        self.warned = {}        # task id -> (next_run, warned at)
        self._stamp = object()
        self._data = None
        self.timer = DispatcherTimer()
        self.timer.Interval = TimeSpan.FromSeconds(_POLL_SECONDS)
        self.timer.Tick += self._tick
        self.timer.Start()

    def stop(self):
        try:
            self.timer.Stop()
        except Exception:
            pass

    def _tick(self, sender, args):
        try:
            self._check()
        except Exception:
            pass

    def _check(self):
        if is_busy():
            return
        stamp = core.stamp()
        if stamp is None:
            return
        if stamp != self._stamp:
            self._data = core.load()
            self._stamp = stamp
        at = core.now()
        due = core.due_tasks(self._data, at, self.year)
        if not due:
            self.warned.clear()
            return
        warn = int(self._data["settings"].get("warn_seconds") or 0)
        ready = set()
        for task in due:
            key = task.get("next_run")
            seen = self.warned.get(task["id"])
            if warn > 0 and (seen is None or seen[0] != key):
                skip = not task.get("run_missed", True) and core.is_missed(task, at)
                self.warned[task["id"]] = (key, at)
                if not skip:
                    _toast(u"{0} starts in {1} seconds".format(task["name"], warn),
                           u"{0} - open DeeSchedule to pause it".format(task["tool"]), _WARN)
                continue
            if seen is None or (at - seen[1]).total_seconds() >= warn:
                ready.add(task["id"])
        if ready:
            self.event_handler.ready |= ready
            self.event.Raise()


def start_watching(uiapp):
    """Called from startup.py (a valid API context). A pyRevit Reload calls
    it again: the previous watcher is stopped first."""
    domain = System.AppDomain.CurrentDomain
    old = domain.GetData(_WATCH_SLOT)
    if old is not None:
        try:
            old.Stop()
        except Exception:
            pass
    # Loaded now, while pyRevit's builtins are certainly there.
    for name in ("pyrevit.forms", "pyrevit.script", "dee_ui", "dee_telemetry",
                 "dee_known_dialogs", "acc_file_browser"):
        try:
            __import__(name)
        except Exception:
            pass
    watcher = _Watcher(revit_year(uiapp))
    # Only the timer goes in the slot: a plain .NET object any engine can
    # stop. It keeps the watcher alive through its Tick handler.
    domain.SetData(_WATCH_SLOT, watcher.timer)
    return watcher
