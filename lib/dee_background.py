# -*- coding: utf-8 -*-
"""
dee_background
Run a Dee batch in one or more BACKGROUND Revit instances instead of the
user's own session.

How it works
    1. The tool (host side, inside the user's Revit) calls
       BackgroundJob.start(...) with a list of items (one dict per model),
       an options dict, the worker module + function name, and how many
       Revits to use. Items are dealt round-robin into that many job
       folders under %LOCALAPPDATA%\\DeeLazy\\background\\<tool>\\<stamp>_N,
       each holding job.json and a stub script, and one
       `pyrevit run <stub> --revit=<year>` is started per folder.
    2. Each stub runs inside its own new Revit, imports THIS module and
       calls run_job(): it imports the worker module, then for every item
       calls  worker(ctx, item) -> dict  (ctx has uiapp, app, log, options,
       folder). The result dict is stored in results.json after every
       item; a stop.txt in the folder makes the loop skip the rest.
    3. The host polls the log files (BackgroundJob.poll()), shows the
       lines, and when every Revit has finished reads the merged results.

Why: the user's session stays free, a crash or stuck dialog in a worker
never touches their work, and several Revits can chew through a long
list at once. The Autodesk sign-in is machine-wide, so cloud models open
in the workers without another login.

Worker functions must be importable from a plain module on sys.path
(lib/ and the tool's own folder are put there by the stub). They must
not touch the host window - only ctx.log(). Anything a second Revit
cannot see (documents open in the user's session, the active view, the
selection) must be handled by the tool in-session instead.
"""
import os
import io
import sys
import json
import time
import datetime
import traceback

LOG_DONE_MARKER = "=== DONE ==="
LOG_FAIL_MARKER = "=== FAILED ==="
_JOBS_ROOT = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "DeeLazy", "background")
_LIB_DIR = os.path.dirname(os.path.abspath(__file__))

_STUB = u'''# -*- coding: utf-8 -*-
# Dee background job stub - run by `pyrevit run` inside a second Revit.
import sys
for _p in {paths!r}:
    if _p not in sys.path:
        sys.path.insert(0, _p)
import dee_background
try:
    _handle = __revit__
except NameError:
    _handle = None
dee_background.run_job(_handle, {job!r})
'''


# ==========================================================================
# shared helpers (both sides)
# ==========================================================================
class FileLog(object):
    """Appends timestamped lines to a file; tolerant of the host reading
    the same file at the same time."""
    def __init__(self, path):
        self.path = path

    def __call__(self, message):
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        line = u"[{0}] {1}\n".format(stamp, message)
        for _ in range(5):
            try:
                with io.open(self.path, "a", encoding="utf-8") as f:
                    f.write(line)
                return
            except Exception:
                time.sleep(0.2)


def _write_json(path, data):
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, indent=1))
    if os.path.exists(path):
        os.remove(path)
    os.rename(tmp, path)


def _read_json(path, default=None):
    try:
        with io.open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def pyrevitlib_dir():
    try:
        import pyrevit
        return os.path.dirname(os.path.dirname(os.path.abspath(pyrevit.__file__)))
    except Exception:
        return None


# ==========================================================================
# host side
# ==========================================================================
class BackgroundJob(object):
    """One batch spread over N background Revits. Create it with
    start(); then call poll() from a timer (DispatcherTimer, 1 s) until
    it returns done=True; then results() gives {item_key: result}.

    on_line(tag, text) receives every worker log line; tag is "bg" or
    "bgN" when several Revits run."""

    def __init__(self):
        self.workers = []
        self.started = None
        self.root = None
        self.tool = ""
        self._noted = 0

    # ----- start
    @classmethod
    def start(cls, tool, worker_module, worker_func, items, options, revit_year,
              instances=1, extra_paths=None, log=None, key_field="key"):
        """Writes the job folders and starts the Revits. Returns the job,
        or raises with a readable message (pyRevit CLI missing...)."""
        from System.Diagnostics import Process, ProcessStartInfo
        job = cls()
        job.tool = tool
        items = list(items or [])
        if not items:
            raise ValueError("no items to process")
        count = max(1, min(int(instances or 1), len(items)))
        chunks = [items[i::count] for i in range(count)]
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        job.root = os.path.join(_JOBS_ROOT, tool, stamp)
        paths = [p for p in [pyrevitlib_dir(), _LIB_DIR] + list(extra_paths or []) if p]
        job.started = time.time()
        for idx, chunk in enumerate(chunks, 1):
            folder = os.path.join(job.root, "revit_{0}".format(idx))
            try:
                os.makedirs(folder)
            except Exception:
                pass
            job_file = os.path.join(folder, "job.json")
            stub_file = os.path.join(folder, "run_{0}.py".format(tool))
            _write_json(job_file, {"tool": tool, "worker_module": worker_module, "worker_func": worker_func,
                                   "key_field": key_field, "options": options or {}, "items": chunk,
                                   "paths": [str(p) for p in paths]})
            with io.open(stub_file, "w", encoding="utf-8") as f:
                f.write(_STUB.format(paths=[str(p) for p in paths], job=str(job_file)))
            try:
                psi = ProcessStartInfo()
                psi.FileName = "pyrevit"
                psi.Arguments = u'run "{0}" --revit={1}'.format(stub_file, revit_year)
                psi.UseShellExecute = False
                psi.CreateNoWindow = True
                psi.WorkingDirectory = folder
                process = Process.Start(psi)
            except Exception as e:
                job.stop()
                raise Exception("Could not start the pyRevit CLI (`pyrevit run`) - is it installed and on the "
                                "PATH? {0}".format(e))
            job.workers.append({"idx": idx, "folder": folder, "process": process, "offset": 0, "finished": None,
                                "log": os.path.join(folder, "log.txt"),
                                "results": os.path.join(folder, "results.json"),
                                "keys": [it.get(key_field) for it in chunk]})
            if log:
                log("Background Revit {0}/{1} starting with {2} model(s) - {3}".format(idx, count, len(chunk), folder))
            if idx < count:
                time.sleep(3)      # stagger the launches
        return job

    # ----- while running
    def poll(self, on_line=None):
        """Reads new log lines from every worker. Returns (done, how)
        where how is "done" / "failed" / "exited" once done."""
        many = len(self.workers) > 1
        for w in self.workers:
            if w["finished"]:
                continue
            tag = "bg{0}".format(w["idx"]) if many else "bg"
            try:
                if os.path.exists(w["log"]):
                    with io.open(w["log"], "r", encoding="utf-8") as f:
                        f.seek(w["offset"])
                        chunk = f.read()
                        w["offset"] = f.tell()
                    for line in chunk.splitlines():
                        if not line.strip():
                            continue
                        if LOG_DONE_MARKER in line:
                            w["finished"] = "done"
                        elif LOG_FAIL_MARKER in line:
                            w["finished"] = "failed"
                        elif on_line:
                            on_line(tag, line)
            except Exception:
                pass
            if w["finished"] is None:
                try:
                    if w["process"].HasExited:
                        w["finished"] = "exited"
                except Exception:
                    w["finished"] = "exited"
            if w["finished"] and many and on_line:
                on_line(tag, "finished ({0}).".format(w["finished"]))
        if any(w["finished"] is None for w in self.workers):
            minutes = int((time.time() - self.started) / 60)
            if minutes and minutes != self._noted and on_line:
                self._noted = minutes
                on_line("bg", "still running ({0} min)...".format(minutes))
            return False, None
        hows = set(w["finished"] for w in self.workers)
        return True, ("failed" if "failed" in hows else ("exited" if "exited" in hows else "done"))

    def stop(self):
        """Each Revit finishes its current model, then skips the rest."""
        for w in self.workers:
            try:
                with io.open(os.path.join(w["folder"], "stop.txt"), "w", encoding="utf-8") as f:
                    f.write(u"stop")
            except Exception:
                pass

    def results(self):
        merged = {}
        for w in self.workers:
            merged.update(_read_json(w["results"], {}) or {})
        return merged

    def keys(self):
        return [k for w in self.workers for k in w["keys"]]


def start_timer(job, on_line, on_finish, seconds=1):
    """Hooks a WPF DispatcherTimer to job.poll(). on_finish(job, how)."""
    from System import TimeSpan
    from System.Windows.Threading import DispatcherTimer
    timer = DispatcherTimer()
    timer.Interval = TimeSpan.FromSeconds(seconds)

    def tick(sender, args):
        done, how = job.poll(on_line)
        if done:
            timer.Stop()
            on_finish(job, how)
    timer.Tick += tick
    timer.Start()
    return timer


INSTANCE_CHOICES = ["1", "2", "3", "4", "6", "8"]


def instances_from_combo(combo, default=1):
    try:
        return int(combo.SelectedItem.Content)
    except Exception:
        return default


# ==========================================================================
# worker side (runs inside the background Revit)
# ==========================================================================
class Context(object):
    def __init__(self, uiapp, app, log, options, folder):
        self.uiapp = uiapp
        self.app = app
        self.log = log
        self.options = options
        self.folder = folder
        self.dialogs = []      # (text, ok) auto-answered dialogs, cleared per item


def _uiapp_of(handle):
    """`__revit__` is a UIApplication in pyRevitRunner; tolerate an
    Application anyway. Returns (uiapp_or_None, app)."""
    if handle is None:
        return None, None
    app = getattr(handle, "Application", None)
    if app is not None and hasattr(handle, "OpenAndActivateDocument"):
        return handle, app
    if hasattr(handle, "OpenDocumentFile"):
        return None, handle
    return None, app


def open_cloud_model(ctx, region, project_guid, model_guid, detach=False, audit=False):
    """Opens a cloud model in the worker. Headless Application.OpenDocumentFile
    first; Revit's own OpenAndActivateDocument when that is refused (the
    document then stays open until the next one is activated - see
    close_model). Returns (doc_or_None, detail)."""
    import System
    from Autodesk.Revit.DB import ModelPathUtils
    try:
        model_path = ModelPathUtils.ConvertCloudGUIDsToCloudPath(
            region, System.Guid(str(project_guid)), System.Guid(str(model_guid)))
    except Exception as e:
        return None, "cloud path: {0}".format(e)
    return open_model_path(ctx, model_path, detach=detach, audit=audit)


def open_local_model(ctx, file_path, detach=False, audit=False):
    from Autodesk.Revit.DB import ModelPathUtils
    try:
        model_path = ModelPathUtils.ConvertUserVisiblePathToModelPath(file_path)
    except Exception as e:
        return None, "path: {0}".format(e)
    return open_model_path(ctx, model_path, detach=detach, audit=audit)


def open_model_path(ctx, model_path, detach=False, audit=False):
    from Autodesk.Revit.DB import (OpenOptions, DetachFromCentralOption, WorksetConfiguration,
                                   WorksetConfigurationOption)
    if detach:
        detach_options = [DetachFromCentralOption.DetachAndPreserveWorksets,
                          DetachFromCentralOption.DetachAndDiscardWorksets,
                          DetachFromCentralOption.DoNotDetach]
    else:
        detach_options = [DetachFromCentralOption.DoNotDetach]
    errors = []
    for route in ("headless", "activate"):
        for d in detach_options:
            try:
                oo = OpenOptions()
                oo.DetachFromCentralOption = d
                oo.Audit = bool(audit)
                try:
                    oo.SetOpenWorksetsConfiguration(WorksetConfiguration(WorksetConfigurationOption.OpenAllWorksets))
                except Exception:
                    pass
                if route == "headless":
                    doc = ctx.app.OpenDocumentFile(model_path, oo)
                else:
                    if ctx.uiapp is None:
                        raise Exception("no UIApplication")
                    doc = ctx.uiapp.OpenAndActivateDocument(model_path, oo, False).Document
                    ctx.activated = doc
                return doc, "{0} ({1})".format(route, str(d).replace("DetachFromCentralOption.", ""))
            except Exception as e:
                errors.append("{0}/{1}: {2}".format(route, str(d).split(".")[-1], str(e).split("\n")[0]))
    return None, " | ".join(errors)


def close_model(ctx, doc):
    """Closes a worker-opened document without saving. A document opened
    through the activate route is the active one and is left for the
    next open to replace (Revit refuses to close the active document)."""
    if doc is None:
        return
    if getattr(ctx, "activated", None) is doc:
        ctx.pending_close = doc
        return
    try:
        doc.Close(False)
    except Exception:
        pass


def _close_pending(ctx):
    doc = getattr(ctx, "pending_close", None)
    if doc is not None and getattr(ctx, "activated", None) is not doc:
        try:
            doc.Close(False)
        except Exception:
            pass
        ctx.pending_close = None


def release_memory():
    try:
        from System import GC
        GC.Collect()
        GC.WaitForPendingFinalizers()
        GC.Collect()
    except Exception:
        pass


def run_job(revit_handle, job_file):
    """Entry point called by the stub inside the background Revit."""
    folder = os.path.dirname(job_file)
    log = FileLog(os.path.join(folder, "log.txt"))
    results_file = os.path.join(folder, "results.json")
    results = {}
    try:
        job = _read_json(job_file)
        if not job:
            raise Exception("could not read the job file")
        for p in job.get("paths") or []:
            if p not in sys.path:
                sys.path.insert(0, p)
        uiapp, app = _uiapp_of(revit_handle)
        if app is None:
            raise Exception("no Revit Application handle (__revit__ missing)")
        log("Worker started: {0} in Revit {1} (pid {2}), {3} item(s).".format(
            job.get("tool"), app.VersionNumber, os.getpid(), len(job.get("items") or [])))

        module = __import__(job["worker_module"])
        func = getattr(module, job["worker_func"])
        ctx = Context(uiapp, app, log, job.get("options") or {}, folder)
        ctx.activated = None
        ctx.pending_close = None

        handler = None
        if uiapp is not None:
            try:
                import acc_file_browser as afb
                handler = afb.make_dialog_handler(ctx.dialogs)
                uiapp.DialogBoxShowing += handler
            except Exception as e:
                log("dialog handler not attached: {0}".format(e))

        key_field = job.get("key_field") or "key"
        stop_file = os.path.join(folder, "stop.txt")
        items = job.get("items") or []
        for i, item in enumerate(items):
            if os.path.exists(stop_file):
                log("Stopped by the user - the remaining items were not touched.")
                break
            key = item.get(key_field)
            log(u"{0} ({1}/{2}):".format(item.get("name") or key, i + 1, len(items)))
            try:
                res = func(ctx, item)
                if not isinstance(res, dict):
                    res = {"ok": bool(res)}
            except Exception as e:
                res = {"ok": False, "detail": str(e), "trace": traceback.format_exc()}
                log(u"  ✗ " + str(e))
            results[key] = res
            for text, _ok in ctx.dialogs:
                log("  (auto-answered) " + text)
            del ctx.dialogs[:]
            _close_pending(ctx)
            release_memory()
            _write_json(results_file, results)
        if ctx.activated is not None:
            log("  (the last model stays open in this Revit until it exits)")
        _write_json(results_file, results)
        log(LOG_DONE_MARKER)
    except Exception:
        try:
            _write_json(results_file, results)
        except Exception:
            pass
        log("Worker crashed:\n" + traceback.format_exc())
        log(LOG_FAIL_MARKER)
