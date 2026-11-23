# -*- coding: utf-8 -*-
"""
dee_schedule_core
The part of DeeSchedule that knows nothing about Revit: where scheduled
tasks are stored, when each one is due, and how they move to another PC.
Plain Python (IronPython 2.7 and CPython 3), so it is covered by
tools/checks/test_schedule_core.py without opening Revit.

--------------------------------------------------------------------
Where tasks live
--------------------------------------------------------------------
%LOCALAPPDATA%\\DeePack\\DeeSchedule\\schedules.json - per Windows user,
per PC, outside the extension folder. Deliberately NOT under lib/: an
extension folder synced by OneDrive (or shared on a server) would hand
the same tasks to every PC and they would all export at once. Moving
tasks between PCs is always explicit: Export / Import.

Every pyRevit command can get its own copy of this module, and more than
one Revit can be open, so nothing is kept in memory: each change is a
locked read-modify-write of the file (mutate()).

--------------------------------------------------------------------
Task states
--------------------------------------------------------------------
active    runs on its schedule
paused    keeps its schedule but does not run until resumed
stopped   schedule switched off (a run in progress is cancelled too);
          Resume starts it again from now
finished  a run-once task that has run, or one past its end date

"Running" is not a stored state: a task is running exactly while its
lock file exists, and that file is opened DeleteOnClose, so a crashed
Revit can never leave a task stuck on "running".
"""
import calendar
import copy
import datetime
import io
import json
import os
import time
import uuid

try:
    import System
    from System.IO import (File, FileStream, FileMode, FileAccess, FileShare,
                           FileOptions)
    from System.Threading import Mutex
    _NET = True
except Exception:
    _NET = False

try:
    _TEXT = (str, unicode)  # noqa: F821 - IronPython 2.7
except NameError:
    _TEXT = (str,)

STORE_FORMAT = 1
EXPORT_FORMAT = "DeeSchedule_ExportV1"

# The export tools, in ribbon order. Only these can be scheduled, and only
# these are accepted from an imported file.
TOOLS = ["DeePrinter", "DeeNWCs", "Dee3D", "DeeScheduleXL", "DeeTransmit"]

ACTIVE, PAUSED, STOPPED, FINISHED = "active", "paused", "stopped", "finished"
STATES = (ACTIVE, PAUSED, STOPPED, FINISHED)

ONCE, INTERVAL, DAILY, WEEKLY, MONTHLY = "once", "interval", "daily", "weekly", "monthly"
TRIGGER_TYPES = (ONCE, INTERVAL, DAILY, WEEKLY, MONTHLY)

MIN_INTERVAL_MINUTES = 5
# A run that starts this late is still "on time" - Revit only runs a task
# when it is idle, so a long command can push it back.
GRACE = datetime.timedelta(minutes=15)
HISTORY_LIMIT = 50
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

DEFAULT_SETTINGS = {
    "enabled": True,        # master switch for this PC
    "warn_seconds": 30,     # heads-up toast before a run; 0 = none
    "notify": True,         # toast when a run ends
}

# Fields that describe THIS PC's runs - never exported, reset on import.
_RUNTIME = ("next_run", "last_run", "last_end", "last_status", "last_message",
            "last_output", "run_count", "fail_count", "history")


class ScheduleError(Exception):
    """A message meant to be shown to the user as it is."""


# --------------------------------------------------------------------------
# dates (strptime is avoided: slow, and not thread-safe in IronPython)
# --------------------------------------------------------------------------
def fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else ""


def parse(text):
    if not text:
        return None
    try:
        date_part, _sep, time_part = str(text).strip().replace("T", " ").partition(" ")
        y, m, d = [int(x) for x in date_part.split("-")]
        bits = [int(x) for x in time_part.split(":")] if time_part else []
        bits += [0] * (3 - len(bits))
        return datetime.datetime(y, m, d, bits[0], bits[1], bits[2])
    except Exception:
        return None


def parse_time(text):
    """"9:05", "21:30", "9" -> (hour, minute), or None."""
    try:
        bits = str(text).strip().replace(".", ":").split(":")
        hour = int(bits[0])
        minute = int(bits[1]) if len(bits) > 1 else 0
        if 0 <= hour <= 23 and 0 <= minute <= 59 and len(bits) <= 2:
            return hour, minute
    except Exception:
        pass
    return None


def now():
    return datetime.datetime.now().replace(microsecond=0)


# --------------------------------------------------------------------------
# triggers
# --------------------------------------------------------------------------
def check_trigger(trigger):
    """Raises ScheduleError if the trigger makes no sense."""
    if not isinstance(trigger, dict) or trigger.get("type") not in TRIGGER_TYPES:
        raise ScheduleError("Unknown schedule type.")
    start = parse(trigger.get("start"))
    if start is None:
        raise ScheduleError("Pick a valid start date and time.")
    if trigger.get("end"):
        end = parse(trigger.get("end"))
        if end is None:
            raise ScheduleError("The end date is not a valid date.")
        if end.date() < start.date():
            raise ScheduleError("The end date is before the start date.")
    kind = trigger["type"]
    if kind == INTERVAL:
        minutes = _int(trigger.get("minutes"))
        if minutes < MIN_INTERVAL_MINUTES:
            raise ScheduleError("The shortest repeat is every {0} minutes.".format(
                MIN_INTERVAL_MINUTES))
    elif kind == WEEKLY:
        days = trigger.get("days")
        if not days or any(_int(d, -1) not in range(7) for d in days):
            raise ScheduleError("Tick at least one week day.")
    elif kind == MONTHLY:
        if not 1 <= _int(trigger.get("day")) <= 31:
            raise ScheduleError("The day of the month must be 1 to 31.")


def _int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def _month_day(year, month, day, t):
    last = calendar.monthrange(year, month)[1]
    return datetime.datetime.combine(datetime.date(year, month, min(day, last)), t)


def next_after(trigger, after):
    """First run strictly after `after`, or None when there is none left.
    A day of month past the month's end means its last day."""
    start = parse(trigger.get("start"))
    if start is None:
        return None
    kind = trigger.get("type")
    result = None
    if kind == ONCE:
        result = start if start > after else None
    elif kind == INTERVAL:
        step = datetime.timedelta(minutes=max(MIN_INTERVAL_MINUTES, _int(trigger.get("minutes"))))
        if start > after:
            result = start
        else:
            gone = after - start
            seconds = gone.days * 86400 + gone.seconds
            count = seconds // (step.days * 86400 + step.seconds) + 1
            result = start + step * int(count)
    elif kind == DAILY:
        result = datetime.datetime.combine(max(start.date(), after.date()), start.time())
        if result <= after:
            result += datetime.timedelta(days=1)
    elif kind == WEEKLY:
        days = set(_int(d, -1) for d in (trigger.get("days") or []))
        day = max(start.date(), after.date())
        for _i in range(9):
            candidate = datetime.datetime.combine(day, start.time())
            if candidate > after and candidate >= start and day.weekday() in days:
                result = candidate
                break
            day += datetime.timedelta(days=1)
    elif kind == MONTHLY:
        base = max(start.date(), after.date())
        year, month = base.year, base.month
        for _i in range(3):
            candidate = _month_day(year, month, max(1, _int(trigger.get("day"), 1)), start.time())
            if candidate > after and candidate >= start:
                result = candidate
                break
            month += 1
            if month > 12:
                year, month = year + 1, 1
    if result is None:
        return None
    end = parse(trigger.get("end"))
    if end is not None and result.date() > end.date():
        return None
    return result


def describe_trigger(trigger):
    start = parse(trigger.get("start"))
    if start is None:
        return "?"
    at = start.strftime("%H:%M")
    kind = trigger.get("type")
    if kind == ONCE:
        text = "Once, {0} {1}".format(start.strftime("%Y-%m-%d"), at)
    elif kind == INTERVAL:
        minutes = _int(trigger.get("minutes"))
        if minutes % 60 == 0 and minutes:
            hours = minutes // 60
            text = "Every hour" if hours == 1 else "Every {0} hours".format(hours)
        else:
            text = "Every {0} minutes".format(minutes)
        text += " from {0}".format(at)
    elif kind == DAILY:
        text = "Daily at {0}".format(at)
    elif kind == WEEKLY:
        days = sorted(_int(d) for d in (trigger.get("days") or []))
        text = "{0} at {1}".format(", ".join(DAY_NAMES[d] for d in days if 0 <= d < 7), at)
    elif kind == MONTHLY:
        text = "Day {0} of each month at {1}".format(trigger.get("day"), at)
    else:
        text = "?"
    end = parse(trigger.get("end"))
    if end is not None and kind != ONCE:
        text += ", until {0}".format(end.strftime("%Y-%m-%d"))
    return text


# --------------------------------------------------------------------------
# the store
# --------------------------------------------------------------------------
def store_dir():
    override = os.environ.get("DEE_SCHEDULE_DIR")      # tests
    if override:
        return override
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "DeePack", "DeeSchedule")


def store_path():
    return os.path.join(store_dir(), "schedules.json")


def _ensure_dir(path):
    if not os.path.isdir(path):
        try:
            os.makedirs(path)
        except Exception:
            if not os.path.isdir(path):
                raise


def stamp():
    """Changes whenever the store file changes - a cheap 'did anything
    happen?' for timers."""
    try:
        st = os.stat(store_path())
        return (st.st_mtime, st.st_size)
    except Exception:
        return None


def _read_text(path):
    # Another Revit may be replacing the file this very moment.
    last = None
    for _i in range(10):
        try:
            with io.open(path, "r", encoding="utf-8") as f:
                return f.read()
        except (IOError, OSError) as e:
            last = e
            if not os.path.exists(path):
                return None
            time.sleep(0.05)
    raise ScheduleError("The schedule file cannot be read: {0}".format(last))


def _parse_store(text):
    data = json.loads(text)
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        raise ValueError("not a schedule file")
    return data


def load():
    """{"settings": {...}, "tasks": [...]}. A missing file is an empty
    store. A damaged file falls back to the .bak beside it; if that is
    gone too the damaged one is set aside, never silently overwritten."""
    data = None
    path = store_path()
    text = _read_text(path) if os.path.exists(path) else None
    if text is not None:
        try:
            data = _parse_store(text)
        except ValueError:
            try:
                data = _parse_store(_read_text(path + ".bak") or "")
            except Exception:
                data = None
            try:
                os.rename(path, "{0}.damaged-{1}".format(path, int(time.time())))
            except Exception:
                pass
    if data is None:
        data = {"tasks": []}
    settings = dict(DEFAULT_SETTINGS)
    settings.update(data.get("settings") or {})
    data["settings"] = settings
    data["tasks"] = [t for t in data["tasks"] if isinstance(t, dict) and t.get("id")]
    return data


def _write(data):
    _ensure_dir(store_dir())
    path = store_path()
    tmp = "{0}.{1}.tmp".format(path, uuid.uuid4().hex[:8])
    # ensure_ascii (the default): no encoding surprises in IronPython
    text = json.dumps({"format": STORE_FORMAT, "settings": data["settings"],
                       "tasks": data["tasks"]}, indent=1)
    with io.open(tmp, "w", encoding="utf-8") as f:
        f.write(text if isinstance(text, type(u"")) else text.decode("ascii"))
    if os.path.exists(path):
        if _NET:
            File.Replace(tmp, path, path + ".bak")
        else:
            os.replace(tmp, path)
    else:
        os.rename(tmp, path)


class _StoreLock(object):
    def __init__(self):
        self._mutex = None
        self._held = False

    def __enter__(self):
        if _NET:
            self._mutex = Mutex(False, "Local\\DeeScheduleStore")
            try:
                self._held = self._mutex.WaitOne(5000)
            except Exception:
                # AbandonedMutexException: the holder died; it is ours now
                self._held = True
            if not self._held:
                self._mutex.Dispose()
                raise ScheduleError("The schedule file is busy - try again.")
        return self

    def __exit__(self, *_exc):
        if self._mutex is not None:
            try:
                if self._held:
                    self._mutex.ReleaseMutex()
            finally:
                self._mutex.Dispose()


def mutate(fn):
    """Locked read-modify-write; returns whatever fn(data) returns."""
    with _StoreLock():
        data = load()
        result = fn(data)
        _write(data)
        return result


def find(data, task_id):
    for task in data["tasks"]:
        if task.get("id") == task_id:
            return task
    return None


def get_task(task_id):
    return find(load(), task_id)


def tasks(tool=None):
    return [t for t in load()["tasks"] if tool is None or t.get("tool") == tool]


def settings():
    return load()["settings"]


def save_settings(**changes):
    def fn(data):
        data["settings"].update(changes)
    mutate(fn)


# --------------------------------------------------------------------------
# run locks / stop requests (files, so they work across Revit sessions)
# --------------------------------------------------------------------------
def _lock_path(task_id):
    return os.path.join(store_dir(), "run_{0}.lock".format(_safe(task_id)))


def _stop_path(task_id):
    return os.path.join(store_dir(), "stop_{0}.flag".format(_safe(task_id)))


def _safe(task_id):
    return "".join(c for c in str(task_id) if c.isalnum())[:40]


class RunLock(object):
    """Held for the length of one run. `acquired` is False when the task
    is already running in this or another Revit."""

    def __init__(self, task_id):
        self._path = _lock_path(task_id)
        self._handle = None
        _ensure_dir(store_dir())
        try:
            if _NET:
                self._handle = FileStream(
                    self._path, FileMode.CreateNew, FileAccess.Write,
                    getattr(FileShare, "None"), 64, FileOptions.DeleteOnClose)
            else:
                self._handle = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except Exception:
            self._handle = None
        self.acquired = self._handle is not None

    def release(self):
        if self._handle is None:
            return
        try:
            if _NET:
                self._handle.Dispose()
            else:
                os.close(self._handle)
                os.remove(self._path)
        except Exception:
            pass
        self._handle = None


def is_running(task_id):
    return os.path.exists(_lock_path(task_id))


def request_stop(task_id):
    _ensure_dir(store_dir())
    with io.open(_stop_path(task_id), "w", encoding="utf-8") as f:
        f.write(u"stop")


def stop_requested(task_id):
    return os.path.exists(_stop_path(task_id))


def clear_stop(task_id):
    try:
        os.remove(_stop_path(task_id))
    except Exception:
        pass


# --------------------------------------------------------------------------
# creating and controlling tasks
# --------------------------------------------------------------------------
def new_task(tool, name, trigger, sources, payload, output="", dated_folder=False,
             run_missed=True, revit_year="", summary=""):
    task = {
        "id": uuid.uuid4().hex,
        "tool": tool,
        "name": (name or "").strip() or tool,
        "created": fmt(now()),
        "state": ACTIVE,
        "trigger": trigger,
        "run_missed": bool(run_missed),
        "revit_year": str(revit_year or ""),
        "sources": sources,
        "output": output or "",
        "dated_folder": bool(dated_folder),
        "summary": summary or "",
        "payload": payload,
    }
    _reset_runtime(task)
    return task


def _reset_runtime(task):
    for key in _RUNTIME:
        task.pop(key, None)
    task.update({"next_run": "", "last_run": "", "last_end": "", "last_status": "",
                 "last_message": "", "last_output": "", "run_count": 0,
                 "fail_count": 0, "history": []})


def check_task(task):
    if task.get("tool") not in TOOLS:
        raise ScheduleError("Unknown tool: {0}".format(task.get("tool")))
    if not isinstance(task.get("payload"), dict):
        raise ScheduleError("The task has no settings.")
    if not isinstance(task.get("sources"), list) or not task["sources"]:
        raise ScheduleError("The task has no model to work on.")
    for src in task["sources"]:
        if not isinstance(src, dict) or src.get("kind") not in ("file", "cloud"):
            raise ScheduleError("The task has an unknown kind of model.")
    if task.get("state") not in STATES:
        raise ScheduleError("The task has an unknown state.")
    check_trigger(task.get("trigger"))


def add_task(task):
    check_task(task)
    nxt = next_after(task["trigger"], now())
    if nxt is None:
        raise ScheduleError("That time has already passed - pick a later one.")
    task["next_run"] = fmt(nxt)

    def fn(data):
        data["tasks"].append(task)
    mutate(fn)
    return task


def update_task(task_id, **changes):
    """Edits a task. A changed trigger re-plans its next run; a finished
    task given a new time becomes active again."""
    def fn(data):
        task = find(data, task_id)
        if task is None:
            raise ScheduleError("That task no longer exists.")
        merged = dict(task)
        merged.update(changes)
        check_task(merged)
        if "trigger" in changes and changes["trigger"] != task.get("trigger"):
            nxt = next_after(merged["trigger"], now())
            if nxt is None:
                raise ScheduleError("That time has already passed - pick a later one.")
            if merged["state"] in (ACTIVE, PAUSED):
                merged["next_run"] = fmt(nxt)
            elif merged["state"] == FINISHED:
                merged["state"] = ACTIVE
                merged["next_run"] = fmt(nxt)
        task.clear()
        task.update(merged)
        return task
    return mutate(fn)


def pause(task_id):
    def fn(data):
        task = find(data, task_id)
        if task is not None and task["state"] == ACTIVE:
            task["state"] = PAUSED
            return True
        return False
    return mutate(fn)


def resume(task_id):
    """Back on schedule from now - runs missed while paused or stopped are
    not caught up."""
    def fn(data):
        task = find(data, task_id)
        if task is None or task["state"] == ACTIVE:
            return False
        nxt = next_after(task["trigger"], now())
        if nxt is None:
            raise ScheduleError(
                u"'{0}' has no run left - use Edit to give it a new time.".format(task["name"]))
        task["state"] = ACTIVE
        task["next_run"] = fmt(nxt)
        return True
    done = mutate(fn)
    clear_stop_if_idle(task_id)
    return done


def clear_stop_if_idle(task_id):
    if not is_running(task_id):
        clear_stop(task_id)


def stop(task_id):
    """Cancels a run in progress and switches the schedule off."""
    if is_running(task_id):
        request_stop(task_id)

    def fn(data):
        task = find(data, task_id)
        if task is None or task["state"] == STOPPED:
            return False
        task["state"] = STOPPED
        task["next_run"] = ""
        return True
    return mutate(fn)


def delete(task_id):
    if is_running(task_id):
        request_stop(task_id)

    def fn(data):
        before = len(data["tasks"])
        data["tasks"] = [t for t in data["tasks"] if t.get("id") != task_id]
        return len(data["tasks"]) != before
    return mutate(fn)


# --------------------------------------------------------------------------
# what is due
# --------------------------------------------------------------------------
def year_matches(task, revit_year):
    want = str(task.get("revit_year") or "")
    return not want or want == str(revit_year)


def due_tasks(data, at, revit_year):
    """Active tasks whose time has come, oldest first, for this Revit."""
    if not data["settings"].get("enabled", True):
        return []
    due = []
    for task in data["tasks"]:
        if task.get("state") != ACTIVE or not year_matches(task, revit_year):
            continue
        nxt = parse(task.get("next_run"))
        if nxt is not None and nxt <= at and not is_running(task["id"]):
            due.append((nxt, task))
    due.sort(key=lambda pair: pair[0])
    return [t for _n, t in due]


def is_missed(task, at):
    nxt = parse(task.get("next_run"))
    return nxt is not None and at - nxt > GRACE


def roll_missed(at, revit_year):
    """Tasks set to skip a run they missed (Revit was closed) move on to
    their next time; the skip is kept in their history."""
    data = load()
    skip = [t["id"] for t in due_tasks(data, at, revit_year)
            if not t.get("run_missed", True) and is_missed(t, at)]
    if not skip:
        return []

    def fn(store):
        for task_id in skip:
            task = find(store, task_id)
            if task is None or task["state"] != ACTIVE or not is_missed(task, at):
                continue
            _note(task, task["next_run"], "", "missed", "Revit was not available at that time")
            _advance(task, at)
    mutate(fn)
    return skip


def _advance(task, at):
    nxt = next_after(task["trigger"], at)
    if nxt is None:
        task["next_run"] = ""
        task["state"] = FINISHED
    else:
        task["next_run"] = fmt(nxt)


def _note(task, started, ended, status, message):
    history = list(task.get("history") or [])
    history.append({"start": started, "end": ended, "status": status, "message": message})
    task["history"] = history[-HISTORY_LIMIT:]


def begin_run(task_id, manual=False):
    """Marks the run as started and plans the next one BEFORE any work, so
    a crash mid-run can never put Revit in a start-crash loop. Returns a
    copy of the task, or None if it is gone or no longer due."""
    at = now()

    def fn(data):
        task = find(data, task_id)
        if task is None:
            return None
        if not manual:
            nxt = parse(task.get("next_run"))
            if task["state"] != ACTIVE or nxt is None or nxt > at:
                return None
            _advance(task, at)
        task["last_run"] = fmt(at)
        task["last_end"] = ""
        task["last_status"] = "running"
        task["last_message"] = ""
        return copy.deepcopy(task)
    result = mutate(fn)
    if result is not None:
        clear_stop(task_id)
    return result


def end_run(task_id, status, message, output=""):
    """status: ok / partial / failed / cancelled / nothing"""
    at = now()

    def fn(data):
        task = find(data, task_id)
        if task is None:
            return
        _note(task, task.get("last_run") or fmt(at), fmt(at), status, message)
        task["last_end"] = fmt(at)
        task["last_status"] = status
        task["last_message"] = message
        task["last_output"] = output or ""
        task["run_count"] = _int(task.get("run_count")) + 1
        if status == "failed":
            task["fail_count"] = _int(task.get("fail_count")) + 1
    mutate(fn)
    clear_stop(task_id)


RESULT_LABELS = {
    "ok": "Done", "partial": "Done with errors", "failed": "Failed",
    "cancelled": "Cancelled", "nothing": "Nothing to export", "missed": "Missed",
    "running": "Interrupted", "": "-",
}


def status_text(task, revit_year=None, enabled=True):
    if is_running(task["id"]):
        return "Stopping..." if stop_requested(task["id"]) else "Running"
    state = task.get("state")
    if state == PAUSED:
        return "Paused"
    if state == STOPPED:
        return "Stopped"
    if state == FINISHED:
        return "Finished"
    if not enabled:
        return "Scheduler off"
    if revit_year and not year_matches(task, revit_year):
        return "Waits for Revit {0}".format(task.get("revit_year"))
    return "Scheduled"


def result_text(task):
    status = task.get("last_status") or ""
    if status == "running" and is_running(task["id"]):
        return "Running now"
    label = RESULT_LABELS.get(status, status)
    message = task.get("last_message") or ""
    return u"{0} - {1}".format(label, message) if message else label


# --------------------------------------------------------------------------
# run logs
# --------------------------------------------------------------------------
def log_dir(task_id):
    return os.path.join(store_dir(), "logs", _safe(task_id))


def write_log(task_id, lines, keep=20):
    """One text file per run, newest `keep` kept. Returns its path."""
    folder = log_dir(task_id)
    try:
        _ensure_dir(folder)
        path = os.path.join(folder, now().strftime("%Y-%m-%d_%H.%M.%S") + ".txt")
        with io.open(path, "w", encoding="utf-8") as f:
            f.write(u"\r\n".join(u"{0}".format(x) for x in lines))
        for old in sorted(os.listdir(folder))[:-keep]:
            try:
                os.remove(os.path.join(folder, old))
            except Exception:
                pass
        return path
    except Exception:
        return ""


def last_log(task_id):
    try:
        folder = log_dir(task_id)
        names = sorted(os.listdir(folder))
        return os.path.join(folder, names[-1]) if names else ""
    except Exception:
        return ""


# --------------------------------------------------------------------------
# moving tasks to another PC
# --------------------------------------------------------------------------
# Paths under these folders are written as %NAME%\... so the same task
# finds the same folder under a different Windows user name.
_PATH_VARS = ("OneDriveCommercial", "OneDriveConsumer", "OneDrive", "USERPROFILE")


def _path_roots():
    roots = []
    for name in _PATH_VARS:
        value = (os.environ.get(name) or "").rstrip("\\/")
        if value:
            roots.append((name, value))
    roots.sort(key=lambda r: -len(r[1]))       # longest first; sort is stable
    return roots


def _map_strings(value, fn):
    if isinstance(value, dict):
        return dict((k, _map_strings(v, fn)) for k, v in value.items())
    if isinstance(value, list):
        return [_map_strings(v, fn) for v in value]
    if isinstance(value, _TEXT):
        return fn(value)
    return value


def _starts_with_folder(text, folder):
    low, root = text.lower(), folder.rstrip("\\/").lower()
    return low == root or low.startswith(root + "\\") or low.startswith(root + "/")


def portable_path(text):
    for name, root in _path_roots():
        if _starts_with_folder(text, root):
            return "%{0}%{1}".format(name, text[len(root):])
    return text


def local_path(text):
    if not text.startswith("%"):
        return text
    for name in _PATH_VARS:
        token = "%{0}%".format(name)
        if text.lower().startswith(token.lower()):
            names = [name] + [n for n in _PATH_VARS if n.startswith("OneDrive") and n != name] \
                if name.startswith("OneDrive") else [name]
            for candidate in names:
                value = (os.environ.get(candidate) or "").rstrip("\\/")
                if value:
                    return value + text[len(token):]
    return text


def _map_task_paths(task, fn):
    """Only the parts of a task that hold paths; names and messages are
    left alone."""
    for key in ("sources", "payload", "output"):
        if key in task:
            task[key] = _map_strings(task[key], fn)
    return task


def build_export(task_list, include_settings=True):
    out = []
    for task in task_list:
        item = copy.deepcopy(task)
        for key in _RUNTIME:
            item.pop(key, None)
        out.append(_map_task_paths(item, portable_path))
    data = {"format": EXPORT_FORMAT, "exported": fmt(now()), "tasks": out}
    if include_settings:
        data["settings"] = settings()
    return data


def export_to_file(path, task_list, include_settings=True):
    text = json.dumps(build_export(task_list, include_settings), indent=2)
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(text if isinstance(text, type(u"")) else text.decode("ascii"))
    return len(task_list)


def read_export(path):
    """(tasks, settings or None, problems) - tasks have this PC's paths
    and clean run history. Raises ScheduleError if it is not such a file."""
    try:
        with io.open(path, "r", encoding="utf-8-sig") as f:
            data = json.loads(f.read())
    except Exception as e:
        raise ScheduleError("Could not read that file:\n{0}".format(e))
    if not isinstance(data, dict) or data.get("format") != EXPORT_FORMAT \
            or not isinstance(data.get("tasks"), list):
        raise ScheduleError("That is not a DeeSchedule export file.")
    good, problems = [], []
    for i, raw in enumerate(data["tasks"], 1):
        try:
            if not isinstance(raw, dict):
                raise ScheduleError("not a task")
            task = copy.deepcopy(raw)
            if not _safe(task.get("id") or ""):
                task["id"] = uuid.uuid4().hex
            task["id"] = _safe(task["id"])
            task["name"] = u"{0}".format(task.get("name") or task.get("tool") or "Task")
            task.setdefault("state", PAUSED)
            task.setdefault("output", "")
            _reset_runtime(task)
            check_task(task)
            good.append(_map_task_paths(task, local_path))
        except Exception as e:
            name = raw.get("name") if isinstance(raw, dict) else None
            problems.append(u"{0}: {1}".format(name or "Task {0}".format(i), e))
    incoming = data.get("settings")
    if isinstance(incoming, dict):
        incoming = dict((k, v) for k, v in incoming.items()
                        if k in DEFAULT_SETTINGS and isinstance(v, (bool, int)))
    else:
        incoming = None
    return good, incoming, problems


def import_tasks(incoming, on_duplicate="skip", start=False, new_settings=None):
    """on_duplicate (same task id already on this PC): skip / replace /
    copy. start=False brings everything in paused so it can be checked
    first; start=True keeps each task's own state. Returns counts."""
    counts = {"added": 0, "replaced": 0, "skipped": 0, "finished": 0}

    def fn(data):
        at = now()
        for task in incoming:
            task = copy.deepcopy(task)
            existing = find(data, task["id"])
            if existing is not None:
                if on_duplicate == "skip":
                    counts["skipped"] += 1
                    continue
                if on_duplicate == "copy":
                    task["id"] = uuid.uuid4().hex
                    task["name"] = u"{0} (imported)".format(task["name"])
                    existing = None
            nxt = next_after(task["trigger"], at)
            if task["state"] in (ACTIVE, PAUSED):
                if nxt is None:
                    task["state"] = FINISHED
                    counts["finished"] += 1
                else:
                    task["next_run"] = fmt(nxt)
                    if not start:
                        task["state"] = PAUSED
            if existing is not None:
                existing.clear()
                existing.update(task)
                counts["replaced"] += 1
            else:
                data["tasks"].append(task)
                counts["added"] += 1
        if new_settings:
            data["settings"].update(new_settings)
    mutate(fn)
    return counts


def _is_checkable(path):
    """A path this PC can be expected to see with a plain file check
    (not a Revit Server RSN:// path)."""
    return bool(path) and "://" not in path


def missing_paths(task):
    """[(what, path)] this task needs and this PC does not have."""
    missing = []
    for src in task.get("sources") or []:
        path = src.get("path") or ""
        if src.get("kind") == "file" and _is_checkable(path) and not os.path.isfile(path):
            missing.append(("model", path))
    output = task.get("output") or ""
    # The output folder itself is created on demand; its drive or share
    # must exist.
    if output:
        root = os.path.splitdrive(output)[0]
        if output.startswith("%") or (root and not os.path.isdir(root + os.sep)):
            missing.append(("output folder", output))
    return missing


def missing_folders(task_list):
    """The distinct folders behind every missing path, for Relink."""
    folders = []
    for task in task_list:
        for what, path in missing_paths(task):
            folder = os.path.dirname(path) if what == "model" else path
            if folder and folder.lower() not in [f.lower() for f in folders]:
                folders.append(folder)
    return sorted(folders, key=lambda f: f.lower())


def relink(old_folder, new_folder, task_ids=None):
    """Replaces a folder everywhere in the tasks' models, settings and
    output. Returns how many tasks changed."""
    old, new = old_folder.rstrip("\\/"), new_folder.rstrip("\\/")

    def swap(text):
        return new + text[len(old):] if _starts_with_folder(text, old) else text

    def fn(data):
        changed = 0
        for task in data["tasks"]:
            if task_ids is not None and task["id"] not in task_ids:
                continue
            before = json.dumps([task.get("sources"), task.get("payload"), task.get("output")],
                                sort_keys=True)
            _map_task_paths(task, swap)
            after = json.dumps([task.get("sources"), task.get("payload"), task.get("output")],
                               sort_keys=True)
            if before != after:
                changed += 1
        return changed
    return mutate(fn)
