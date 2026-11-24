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

Add-ins in the background Revits
    `pyrevit run` starts Revit with ONLY the pyRevit runner loaded - none of
    the add-ins installed on the PC (Navisworks exporter, ...) are there, so
    e.g. NavisworksExportOptions fails with "A Navisworks Exporter is not
    available". start(addins=["nwexportrevit"]) hands the named manifests
    (<name>.addin from the Revit year's Addins folders) to the runner
    (`pyrevit run --import`), with their relative Assembly paths made
    absolute; Revit then loads those add-ins in the background session.

A Revit that crashes
    Revit keeps a crashed process - and with it the whole batch, which waits
    for every Revit - alive until somebody closes Autodesk's "Revit 2024.3
    Error Report" box. poll() looks for that box in every worker Revit, ends
    the Revit, reports the model it died on ({"ok": False, "detail": ...}) and
    starts a new Revit for the models not reached yet (twice at most).

Opening models
    open_cloud_model / open_local_model try Application.OpenDocumentFile and,
    when that is refused, UIApplication.OpenAndActivateDocument. The second
    one builds a full UI view in a Revit that has none - it crashed a
    background Revit when the model's starting view was a sheet - so a tool
    that can live without it passes activate=False and retries instead.

Worker functions must be importable from a plain module on sys.path
(lib/ and the tool's own folder are put there by the stub). They must
not touch the host window - only ctx.log(). Anything a second Revit
cannot see (documents open in the user's session, the active view, the
selection) must be handled by the tool in-session instead.
"""
import os
import io
import re
import sys
import json
import time
import datetime
import traceback

_EXIT_WHEN_DONE = [False]
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


def _addin_dirs(revit_year):
    year = str(revit_year)
    return [os.path.join(os.environ.get("ProgramData") or r"C:\ProgramData",
                         "Autodesk", "Revit", "Addins", year),
            os.path.join(os.environ.get("APPDATA") or "", "Autodesk", "Revit", "Addins", year)]


def prepare_addins(names, revit_year, target_folder):
    """Copies <name>.addin for the Revit year into target_folder with every
    <Assembly> path made absolute (the manifest's own folder is no longer
    the base). Returns ([copied names], [names not found])."""
    copied, missing = [], []
    for name in names or []:
        found = None
        for folder in _addin_dirs(revit_year):
            path = os.path.join(folder, name + ".addin")
            if os.path.isfile(path):
                found = path
                break
        if found is None:
            missing.append(name)
            continue
        base = os.path.dirname(found)

        def absolute(match, _base=base):
            rel = match.group(1).strip().strip('"').strip()
            if not os.path.isabs(rel):
                rel = os.path.normpath(os.path.join(_base, rel.replace("/", os.sep)))
            return u"<Assembly>{0}</Assembly>".format(rel)
        with io.open(found, "r", encoding="utf-8-sig") as f:
            text = f.read()
        text = re.sub(r"<Assembly>(.*?)</Assembly>", absolute, text, flags=re.S)
        if not os.path.isdir(target_folder):
            os.makedirs(target_folder)
        with io.open(os.path.join(target_folder, name + ".addin"), "w", encoding="utf-8") as f:
            f.write(text)
        copied.append(name)
    return copied, missing


def pyrevitlib_dir():
    try:
        import pyrevit
        return os.path.dirname(os.path.dirname(os.path.abspath(pyrevit.__file__)))
    except Exception:
        return None


# ==========================================================================
# host side
# ==========================================================================
CRASH_TITLES = ("error report",)    # Autodesk's "Revit 2024.3 Error Report" box
_MAX_RESTARTS = 2


def remaining_after_crash(chunk, key_field, done_keys):
    """(item the worker died on, items it had not started) for a Revit that
    crashed. Items go through in order, so the first one without a stored
    result is the one it died on."""
    for i, item in enumerate(chunk):
        if item.get(key_field) not in done_keys:
            return item, list(chunk[i + 1:])
    return None, []


def crash_window(pid):
    """Title of the crash box ("Revit 2024.3 Error Report") a Revit process
    is showing, else None. A crashed Revit keeps its process - and the whole
    batch waiting for it - until somebody closes that box."""
    try:
        import dee_native_dialogs
        for title in dee_native_dialogs.visible_titles(pid):
            low = title.lower()
            if any(t in low for t in CRASH_TITLES):
                return title
    except Exception:
        pass
    return None


def _kill_revit(pid):
    """Ends a background Revit and its helper processes. Only ever given the
    pid a worker reported for itself; anything that is not a Revit is left."""
    from System.Diagnostics import Process, ProcessStartInfo
    try:
        proc = Process.GetProcessById(int(pid))
        if proc.ProcessName.lower() != "revit":
            return
    except Exception:
        return
    try:
        psi = ProcessStartInfo("taskkill", "/PID {0} /T /F".format(int(pid)))
        psi.UseShellExecute = False
        psi.CreateNoWindow = True
        Process.Start(psi).WaitForExit(15000)
    except Exception:
        try:
            proc.Kill()
        except Exception:
            pass


class BackgroundJob(object):
    """One batch spread over N background Revits. Create it with
    start(); then call poll() from a timer (DispatcherTimer, 1 s) until
    it returns done=True; then results() gives {item_key: result}.

    on_line(tag, text) receives every worker log line; tag is "bg" or
    "bgN" when several Revits run.

    A Revit that crashes (its "Error Report" box appears) is ended by
    poll(); the model it died on gets {"ok": False, "detail": ...} and the
    models it had not started go to a new Revit (twice at most per Revit)."""

    def __init__(self):
        self.workers = []
        self.started = None
        self.root = None
        self.tool = ""
        self._noted = 0
        self._spec = {}
        self._made_up = {}       # results for the models a crashed Revit died on

    # ----- start
    @classmethod
    def start(cls, tool, worker_module, worker_func, items, options, revit_year,
              instances=1, extra_paths=None, log=None, key_field="key", addins=None):
        """Writes the job folders and starts the Revits. Returns the job,
        or raises with a readable message (pyRevit CLI missing...).
        addins: names of installed add-in manifests (without .addin) the
        background Revits must load too - see the module docstring."""
        job = cls()
        job.tool = tool
        items = list(items or [])
        if not items:
            raise ValueError("no items to process")
        count = max(1, min(int(instances or 1), len(items)))
        chunks = [items[i::count] for i in range(count)]
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        job.root = os.path.join(_JOBS_ROOT, tool, stamp)
        paths = [str(p) for p in [pyrevitlib_dir(), _LIB_DIR] + list(extra_paths or []) if p]
        job.started = time.time()
        job._spec = {"worker_module": worker_module, "worker_func": worker_func,
                     "key_field": key_field, "options": options or {}, "paths": paths,
                     "revit_year": revit_year, "addins": addins, "log": log}
        for idx, chunk in enumerate(chunks, 1):
            try:
                worker = job._spawn(idx, os.path.join(job.root, "revit_{0}".format(idx)), chunk)
            except Exception:
                job.stop()
                raise
            if log:
                log("Background Revit {0}/{1} starting with {2} model(s) - {3}".format(
                    idx, count, len(chunk), worker["folder"]))
            if idx < count:
                time.sleep(3)      # stagger the launches
        try:
            import dee_running     # DeeControl -> Running Tools
            dee_running.add_job(tool, len(items), [w["folder"] for w in job.workers],
                                [w["process"] for w in job.workers])
        except Exception:
            pass
        return job

    def _spawn(self, idx, folder, chunk, restarts=0):
        """Writes one job folder and starts `pyrevit run` for it."""
        from System.Diagnostics import Process, ProcessStartInfo
        spec = self._spec
        try:
            os.makedirs(folder)
        except Exception:
            pass
        job_file = os.path.join(folder, "job.json")
        stub_file = os.path.join(folder, "run_{0}.py".format(self.tool))
        _write_json(job_file, {"tool": self.tool, "worker_module": spec["worker_module"],
                               "worker_func": spec["worker_func"], "key_field": spec["key_field"],
                               "options": spec["options"], "items": chunk, "paths": spec["paths"]})
        with io.open(stub_file, "w", encoding="utf-8") as f:
            f.write(_STUB.format(paths=spec["paths"], job=str(job_file)))
        try:
            psi = ProcessStartInfo()
            psi.FileName = "pyrevit"
            # --allowdialogs: without it Revit runs the journal in strict
            # playback and ANY dialog the journal did not script (e.g.
            # "The host model failed to open") ends the run with
            # "Journal Error - the journal file could not be run to
            # completion". With it, dialogs are allowed and the
            # DialogBoxShowing handler answers the known ones.
            psi.Arguments = u'run "{0}" --revit={1} --allowdialogs'.format(stub_file, spec["revit_year"])
            if spec["addins"]:
                import_folder = os.path.join(folder, "addins")
                copied, missing = prepare_addins(spec["addins"], spec["revit_year"], import_folder)
                if copied:
                    psi.Arguments += u' --import "{0}"'.format(import_folder)
                if missing and spec["log"]:
                    spec["log"]("Add-in(s) not installed for Revit {0}: {1}".format(
                        spec["revit_year"], ", ".join(missing)))
            psi.UseShellExecute = False
            psi.CreateNoWindow = True
            psi.WorkingDirectory = folder
            process = Process.Start(psi)
        except Exception as e:
            raise Exception("Could not start the pyRevit CLI (`pyrevit run`) - is it installed and on the "
                            "PATH? {0}".format(e))
        worker = {"idx": idx, "folder": folder, "process": process, "offset": 0, "finished": None,
                  "pid": None, "restarts": restarts, "chunk": chunk,
                  "log": os.path.join(folder, "log.txt"),
                  "results": os.path.join(folder, "results.json"),
                  "keys": [it.get(spec["key_field"]) for it in chunk]}
        self.workers.append(worker)
        return worker

    # ----- while running
    def poll(self, on_line=None):
        """Reads new log lines from every worker. Returns (done, how)
        where how is "done" / "failed" / "exited" once done."""
        many = len(set(w["idx"] for w in self.workers)) > 1
        for w in list(self.workers):
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
                        else:
                            if "Worker started:" in line:
                                found = re.search(r"\(pid (\d+)\)", line)
                                if found:
                                    w["pid"] = int(found.group(1))
                            if on_line:
                                on_line(tag, line)
            except Exception:
                pass
            if w["finished"] is None:
                self._look_for_crash(w, tag, on_line)
            if w["finished"] is None:
                try:
                    exited = w["process"].HasExited
                except Exception:
                    exited = True
                if exited:
                    if w.get("exit_seen") or w["pid"] is None:
                        w["finished"] = "exited"
                        if w["pid"] is not None:
                            (on_line or (lambda _t, _x: None))(
                                tag, u"This Revit ended without finishing (crashed or was closed).")
                            self._recover(w, tag, on_line or (lambda _t, _x: None))
                    else:
                        # its last log lines (DONE) may not have been read yet
                        w["exit_seen"] = True
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

    def _look_for_crash(self, w, tag, on_line):
        pid = w.get("pid")
        now = time.time()
        if not pid or now - w.get("looked", 0) < 3:
            return
        w["looked"] = now
        title = crash_window(pid)
        if not title:
            return
        say = on_line or (lambda _tag, _text: None)
        say(tag, u"This Revit crashed ('{0}') - closing it.".format(title))
        _kill_revit(pid)
        try:
            w["process"].Kill()
        except Exception:
            pass
        w["finished"] = "exited"
        w["crashed"] = True
        self._recover(w, tag, say)

    def _recover(self, w, tag, say):
        """The model a crashed Revit died on is reported; the ones it had
        not started go to a new Revit."""
        key_field = self._spec["key_field"]
        done = set((_read_json(w["results"], {}) or {}).keys())
        died_on, rest = remaining_after_crash(w["chunk"], key_field, done)
        if died_on is not None:
            key = died_on.get(key_field)
            self._made_up[key] = {
                "ok": False, "crashed": True,
                "detail": u"Revit crashed while working on this model, so it was skipped. "
                          u"Run it again on its own."}
            say(tag, u"The crash happened on: {0}".format(died_on.get("name") or key))
        stopped = os.path.exists(os.path.join(w["folder"], "stop.txt"))
        if not rest or stopped or w["restarts"] >= _MAX_RESTARTS:
            if rest and not stopped:
                say(tag, u"This Revit crashed {0} times - {1} model(s) were not processed.".format(
                    w["restarts"] + 1, len(rest)))
            return
        number = w["restarts"] + 1
        folder = os.path.join(self.root, "revit_{0}_again{1}".format(w["idx"], number))
        say(tag, u"Starting a new Revit for the {0} model(s) it had not reached.".format(len(rest)))
        try:
            self._spawn(w["idx"], folder, rest, restarts=number)
            w["replaced"] = True
        except Exception as e:
            say(tag, u"Could not start a new Revit: {0}".format(e))

    def stop(self):
        """Each Revit finishes its current model, then skips the rest."""
        request_stop([w["folder"] for w in self.workers])

    def results(self):
        merged = {}
        for w in self.workers:
            merged.update(_read_json(w["results"], {}) or {})
        for key, res in self._made_up.items():
            merged.setdefault(key, res)
        return merged

    def keys(self):
        out = []
        for w in self.workers:
            for k in w["keys"]:
                if k not in out:
                    out.append(k)
        return out


def request_stop(folders):
    for folder in folders:
        try:
            with io.open(os.path.join(folder, "stop.txt"), "w", encoding="utf-8") as f:
                f.write(u"stop")
        except Exception:
            pass


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


def exit_when_done():
    """Call when this background Revit's journal can no longer close it (a
    native box made playback fail - see lib/dee_native_dialogs): once the
    job has written its results, run_job ends the process itself."""
    _EXIT_WHEN_DONE[0] = True


def _exit_now_if_asked(log):
    if not _EXIT_WHEN_DONE[0]:
        return
    log("(the journal could not close this Revit - ending it)")

    def kill():
        try:
            from System.Diagnostics import Process
            Process.GetCurrentProcess().Kill()
        except Exception:
            os._exit(0)
    import threading
    timer = threading.Timer(3.0, kill)
    timer.daemon = True
    timer.start()


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


def open_cloud_model(ctx, region, project_guid, model_guid, detach=False, audit=False,
                     activate=True):
    """Opens a cloud model in the worker. Headless Application.OpenDocumentFile
    first; Revit's own OpenAndActivateDocument when that is refused (the
    document then stays open until the next one is activated - see
    close_model) unless activate=False. Returns (doc_or_None, detail)."""
    import System
    from Autodesk.Revit.DB import ModelPathUtils
    try:
        model_path = ModelPathUtils.ConvertCloudGUIDsToCloudPath(
            region, System.Guid(str(project_guid)), System.Guid(str(model_guid)))
    except Exception as e:
        return None, "cloud path: {0}".format(e)
    return open_model_path(ctx, model_path, detach=detach, audit=audit, activate=activate)


def open_local_model(ctx, file_path, detach=False, audit=False, activate=True):
    from Autodesk.Revit.DB import ModelPathUtils
    try:
        model_path = ModelPathUtils.ConvertUserVisiblePathToModelPath(file_path)
    except Exception as e:
        return None, "path: {0}".format(e)
    return open_model_path(ctx, model_path, detach=detach, audit=audit, activate=activate)


def open_model_path(ctx, model_path, detach=False, audit=False, activate=True):
    from Autodesk.Revit.DB import (OpenOptions, DetachFromCentralOption, WorksetConfiguration,
                                   WorksetConfigurationOption)
    if detach:
        detach_options = [DetachFromCentralOption.DetachAndPreserveWorksets,
                          DetachFromCentralOption.DetachAndDiscardWorksets,
                          DetachFromCentralOption.DoNotDetach]
    else:
        detach_options = [DetachFromCentralOption.DoNotDetach]
    errors = []
    for route in (("headless", "activate") if activate else ("headless",)):
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
        # Every headless try ended in "Opening was canceled": the model itself
        # aborts its own open (a failure on load, a refused dialog), and Revit's
        # own open route would hit the same wall - another minute of retries
        # only delays the answer.
        if route == "headless" and errors and all("cancel" in e.lower() for e in errors):
            break
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
        _exit_now_if_asked(log)
    except Exception:
        try:
            _write_json(results_file, results)
        except Exception:
            pass
        log("Worker crashed:\n" + traceback.format_exc())
        log(LOG_FAIL_MARKER)
        _exit_now_if_asked(log)
