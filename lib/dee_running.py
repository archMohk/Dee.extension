# -*- coding: utf-8 -*-
"""
dee_running
What is running right now - the list behind DeeControl -> Running Tools.

    Background service  DeeIcon, Prayer times, DeeCall, DeeSchedule,
                        DeeForce, DeeMCP
    Scheduled export    a DeeSchedule task being exported (any Revit)
    Background Revit    a batch a tool handed to other Revits
                        (lib/dee_background.py)
    Open window         any Dee tool window still on screen

--------------------------------------------------------------------
Why AppDomain slots
--------------------------------------------------------------------
startup.py runs in an engine of its own, so the modules it starts are NOT
the module objects a button imports: a module-level flag set there is
invisible here. Only AppDomain data is shared by every engine in this
Revit, and only plain .NET objects are put in it. Settings files, lock
files and WPF's own list of windows are shared already.

Every reader is on its own try/except: one that fails costs its own rows
and nothing else. Nothing here opens a Transaction or touches a model.
"""
import io
import os

import System
from System.Collections import ArrayList, Hashtable

_STARTED = "dee_running.started."
_JOBS = "dee_running.jobs"

SERVICE = "Background service"
SCHEDULED = "Scheduled export"
BACKGROUND = "Background Revit"
WINDOW = "Open window"
_KIND_ORDER = [SCHEDULED, BACKGROUND, WINDOW, SERVICE]


def _domain():
    return System.AppDomain.CurrentDomain


# ---------------- reported by the tools themselves ----------------
def mark_started(name):
    """Called by a service's start_watching(). Never raises."""
    try:
        _domain().SetData(_STARTED + name, System.DateTime.Now)
    except Exception:
        pass


def is_started(name):
    try:
        return _domain().GetData(_STARTED + name) is not None
    except Exception:
        return False


def add_job(tool, total, folders, processes):
    """Called by dee_background when a batch has started. Never raises."""
    try:
        jobs = _domain().GetData(_JOBS)
        if jobs is None:
            jobs = ArrayList.Synchronized(ArrayList())
            _domain().SetData(_JOBS, jobs)
        entry = Hashtable()
        entry["tool"] = tool
        entry["total"] = total
        entry["started"] = System.DateTime.Now
        entry["folders"] = ArrayList(list(folders))
        entry["processes"] = ArrayList(list(processes))
        jobs.Add(entry)
    except Exception:
        pass


# ---------------- rows ----------------
class Row(object):
    """One line of the list. `action` is the button's text ("" = no
    button) and `run` what it does: run() returns a message or None."""

    def __init__(self, tool, kind, on, status, detail, action="", run=None):
        self.tool = tool
        self.kind = kind
        self.on = on
        self.status = status
        self.detail = detail
        self.action = action
        self.run = run
        self.opacity = 1.0 if on else 0.55
        self.action_visible = "Visible" if action else "Collapsed"

    def key(self):
        return (self.tool, self.kind, self.on, self.status, self.detail, self.action)


def _switch(on):
    return "Turn Off" if on else "Turn On"


def _watched(name, enabled):
    """(on, status) of a service that startup.py starts."""
    if not enabled:
        return False, "Off"
    if not is_started(name):
        return False, "Not started"
    return True, "Running"


_RELOAD = u"Starts at the next Revit launch or pyRevit reload."


def _icon_rows(uiapp):
    import dee_icon_service as service
    enabled = service.is_enabled()
    on, status = _watched("DeeIcon", enabled)
    detail = (_RELOAD if status == "Not started" else
              u"Brands the file thumbnail when a model is opened or saved.")

    def toggle():
        service.set_enabled(not enabled)
        return u"DeeIcon is now {0}.".format("off" if enabled else "on")
    return [Row("DeeIcon", SERVICE, on, status, detail, _switch(enabled), toggle)]


def _prayer_rows(uiapp):
    import dee_prayer_service as service
    enabled = bool(service.load_settings().get("enabled", True))
    on, status = _watched("Prayer times", enabled)
    detail = (_RELOAD if status == "Not started" else
              u"Prayer time notifications.")

    def toggle():
        data = service.load_settings()
        data["enabled"] = not enabled
        service.save_settings(data)
        return u"Prayer time notifications are now {0}.".format("off" if enabled else "on")
    return [Row("Prayer times", SERVICE, on, status, detail, _switch(enabled), toggle)]


def _call_rows(uiapp):
    on, status = _watched("DeeCall", True)
    detail = (_RELOAD if status == "Not started" else
              u"Shows messages from the administrator. Always on.")
    return [Row("DeeCall", SERVICE, on, status, detail)]


def _schedule_rows(uiapp):
    import dee_schedule_core as core
    data = core.load()
    enabled = bool(data["settings"].get("enabled", True))
    timer = _domain().GetData("dee_schedule.watcher")
    active = [t for t in data["tasks"] if t.get("state") == core.ACTIVE]
    if not enabled:
        on, status = False, "Off"
    elif timer is None or not timer.IsEnabled:
        on, status = False, "Not started"
    else:
        on, status = True, "Running"
    detail = _RELOAD if status == "Not started" else u"{0} active task(s).".format(len(active))

    def toggle():
        core.save_settings(enabled=not enabled)
        return u"DeeSchedule is now {0}.".format("off" if enabled else "on")
    rows = [Row("DeeSchedule", SERVICE, on, status, detail, _switch(enabled), toggle)]

    for task in data["tasks"]:
        if not core.is_running(task["id"]):
            continue
        stopping = core.stop_requested(task["id"])

        def stop(task_id=task["id"], name=task.get("name")):
            if not core.is_running(task_id):
                return u"{0} had already finished.".format(name)
            core.request_stop(task_id)
            return u"{0} stops after the item it is on.".format(name)
        rows.append(Row(
            task.get("tool") or "DeeSchedule", SCHEDULED, True,
            "Stopping..." if stopping else "Running", task.get("name") or u"",
            "" if stopping else "Stop", None if stopping else stop))
    return rows


def _force_rows(uiapp):
    import dee_force_state as state
    on = state.is_active()
    detail = u"Synchronizes every open workshared model every {0} minute(s).".format(
        state.load_interval_minutes())
    remaining = state.seconds_to_next_sync() if on else None
    if remaining is not None:
        detail += u" Next in about {0} min.".format(int(remaining // 60) + 1)

    def toggle():
        if on:
            state.disable(uiapp)
        else:
            state.enable(uiapp)
        return u"DeeForce is now {0}.".format("on" if state.is_active() else "off")
    return [Row("DeeForce", SERVICE, on, "Running" if on else "Off", detail,
                _switch(on), toggle)]


def _mcp_rows(uiapp):
    # The environment variables only - importing dee_mcp_server would
    # register its web route again, from this engine.
    from pyrevit.coreutils import envvars
    server = envvars.get_pyrevit_env_var("DEEMCP_SERVER")
    if server is None:
        return [Row("DeeMCP", SERVICE, False, "Off",
                    u"Local AI server. Start it from the DeeMCP button.")]

    def stop():
        # Its window stops the server as it closes, in its own engine.
        for win in dee_windows():
            if "DeeMCP" in (win.Title or ""):
                win.Close()
        if envvars.get_pyrevit_env_var("DEEMCP_SERVER") is not None:
            server.stop()
            for name in ("DEEMCP_SERVER", "DEEMCP_TOKEN", "DEEMCP_PORT"):
                envvars.set_pyrevit_env_var(name, None)
        return u"The DeeMCP server is stopped."
    return [Row("DeeMCP", SERVICE, True, "Running",
                u"Local AI server on port {0}, {1} request(s) served.".format(
                    envvars.get_pyrevit_env_var("DEEMCP_PORT"),
                    envvars.get_pyrevit_env_var("DEEMCP_REQUEST_COUNT") or 0),
                "Stop", stop)]


# ---------------- background Revits ----------------
def _worker_alive(folder, process):
    import dee_background as bg
    try:
        if process.HasExited:
            return False
    except Exception:
        return False
    try:
        with io.open(os.path.join(folder, "log.txt"), "r", encoding="utf-8") as f:
            text = f.read()
        return bg.LOG_DONE_MARKER not in text and bg.LOG_FAIL_MARKER not in text
    except Exception:
        return True         # no log yet: still starting


def _job_rows(uiapp):
    import dee_background as bg
    jobs = _domain().GetData(_JOBS)
    rows = []
    for entry in (list(jobs) if jobs is not None else []):
        folders = list(entry["folders"])
        alive = [f for f, p in zip(folders, list(entry["processes"])) if _worker_alive(f, p)]
        if not alive:
            jobs.Remove(entry)
            continue
        done = sum(len(bg._read_json(os.path.join(f, "results.json"), {}) or {}) for f in folders)
        stopping = all(os.path.exists(os.path.join(f, "stop.txt")) for f in alive)

        def stop(where=alive):
            bg.request_stop(where)
            return u"Each background Revit stops after the model it is on."
        rows.append(Row(
            entry["tool"], BACKGROUND, True, "Stopping..." if stopping else "Running",
            u"{0} of {1} model(s) done, {2} Revit(s), since {3}.".format(
                done, entry["total"], len(alive), entry["started"].ToString("HH:mm")),
            "" if stopping else "Stop", None if stopping else stop))
    return rows


# ---------------- windows ----------------
def dee_windows():
    """Every Dee window on screen, whichever engine opened it: WPF's own
    list, kept to the windows lib/dee_branding put its colours in."""
    import clr
    clr.AddReference("PresentationCore")
    clr.AddReference("PresentationFramework")
    from System.Windows import PresentationSource, Window
    found = []
    for source in list(PresentationSource.CurrentSources):
        try:
            win = source.RootVisual
            if isinstance(win, Window) and win.IsVisible and win.Resources.Contains("DeeBar"):
                found.append(win)
        except Exception:
            continue
    return found


def _window_rows(uiapp, skip=None):
    rows = []
    for win in dee_windows():
        if skip is not None and win.Equals(skip):
            continue
        title = win.Title or u"Dee window"
        for head in (u"Dee.extension", u"DeePack"):
            if title.startswith(head):
                title = title[len(head):].lstrip(u" -–—:") or title

        def close(target=win, name=title):
            target.Close()
            return u"{0} is closed.".format(name)
        detail = (u"Closing it also stops the DeeMCP server." if "DeeMCP" in title
                  else u"Window open on screen.")
        rows.append(Row(title, WINDOW, True, "Open", detail, "Close", close))
    return rows


# ---------------- the list ----------------
def rows(uiapp, window=None, include_off=True):
    """(rows, problems). `window` is the caller's own window, left out."""
    found, problems = [], []
    readers = [("DeeSchedule", _schedule_rows), ("Background Revits", _job_rows),
               ("Windows", lambda app: _window_rows(app, window)),
               ("DeeForce", _force_rows), ("DeeIcon", _icon_rows),
               ("Prayer times", _prayer_rows), ("DeeCall", _call_rows), ("DeeMCP", _mcp_rows)]
    for name, reader in readers:
        try:
            found.extend(reader(uiapp))
        except Exception as e:
            problems.append(u"{0}: {1}".format(name, e))
    if not include_off:
        found = [r for r in found if r.on]
    found.sort(key=lambda r: (not r.on, _KIND_ORDER.index(r.kind), r.tool.lower()))
    return found, problems
