# -*- coding: utf-8 -*-
"""
dee_schedule_ui
The windows of DeeSchedule:

    schedule(...)     the "Schedule a task" dialog every export tool opens
                      from its Schedule button. Also lists what is already
                      scheduled in that tool, with Run / Resume / Pause /
                      Stop / Delete.
    edit(...)         the same dialog for an existing task
    ManagerWindow     the DeeSchedule button: every task of every tool,
                      plus Export / Import for moving them to another PC

Both windows share TaskListWindow, so the controls behave identically
everywhere.
"""
import os
import traceback

import clr
clr.AddReference("System")      # System.Diagnostics.Process on .NET 8 (Revit 2025+)
clr.AddReference("System.Windows.Forms")
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")
import System
from System import TimeSpan
from System.Diagnostics import Process
from System.Windows import Thickness, Visibility
from System.Windows.Controls import CheckBox
from System.Windows.Forms import (
    SaveFileDialog, OpenFileDialog, FolderBrowserDialog, DialogResult)
from System.Windows.Threading import DispatcherTimer

from pyrevit import forms

import dee_branding
import dee_schedule_core as core
import dee_schedule_engine as engine

_TITLE = "DeeSchedule"
_TASK_XAML = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "DeeScheduleTaskDialog.xaml")
_ALL = "All tools"
_FORCE = "DeeForce"
_ANY_YEAR = "Any version"

_REPEATS = [("Once", core.ONCE), ("Every few minutes / hours", core.INTERVAL),
            ("Every day", core.DAILY), ("On week days", core.WEEKLY),
            ("Every month", core.MONTHLY)]
_WARNINGS = [("No", 0), ("30 seconds", 30), ("1 minute", 60), ("5 minutes", 300)]


def _when_text(stamp):
    """"Today 21:30", "Tomorrow 09:00", "Tue 2026-12-01 09:00"."""
    dt = core.parse(stamp)
    if dt is None:
        return "-"
    days = (dt.date() - core.now().date()).days
    at = dt.strftime("%H:%M")
    if days == 0:
        return "Today " + at
    if days == 1:
        return "Tomorrow " + at
    if days == -1:
        return "Yesterday " + at
    return "{0} {1}".format(core.DAY_NAMES[dt.weekday()], dt.strftime("%Y-%m-%d %H:%M"))


def _models_text(sources):
    names = [s.get("label") or os.path.basename(s.get("path") or "") or "?" for s in sources]
    if len(names) <= 2:
        return u", ".join(names)
    return u"{0} + {1} more".format(names[0], len(names) - 1)


def _folder_dialog(description, start=""):
    dlg = FolderBrowserDialog()
    dlg.Description = description
    if start and os.path.isdir(start):
        dlg.SelectedPath = start
    return dlg.SelectedPath if dlg.ShowDialog() == DialogResult.OK else None


class TaskRow(object):
    """One grid row - plain attributes, bound by name."""

    def __init__(self, task, year, enabled):
        self.id = task["id"]
        self.task = task
        self.tool = task["tool"]
        self.name = task["name"]
        self.model = _models_text(task.get("sources") or [])
        self.when = core.describe_trigger(task["trigger"])
        self.status = core.status_text(task, year, enabled)
        self.next_text = (_when_text(task.get("next_run"))
                          if task.get("state") == core.ACTIVE else "-")
        self.last_text = _when_text(task.get("last_run"))
        self.result = core.result_text(task)

    def key(self):
        return (self.id, self.name, self.model, self.when, self.status,
                self.next_text, self.last_text, self.result)


class ForceRow(object):
    """DeeForce's auto-sync, shown next to the export tasks. It lives only
    in this Revit session, so it can be run and stopped but not edited,
    exported or deleted."""
    id = "deeforce"
    task = None

    def __init__(self, state):
        minutes = state.load_interval_minutes()
        active = state.is_active()
        self.tool = _FORCE
        self.name = "Synchronize all open models"
        self.model = "Every open workshared model"
        self.when = "Every {0:g} minutes".format(float(minutes))
        self.status = "Running" if active else "Off"
        remaining = state.seconds_to_next_sync() if active else None
        self.next_text = "-"
        if remaining is not None:
            self.next_text = "in {0} min".format(max(0, int(remaining // 60)))
        self.last_text = "-"
        self.result = "-"

    def key(self):
        return (self.id, self.when, self.status, self.next_text)


# ==========================================================================
# the shared task list
# ==========================================================================
class TaskListWindow(dee_branding.DeeBrandedWindow):
    """Needs a DataGrid named tasks_grid. Subclasses say which rows."""
    _ready = False

    def __init__(self, xaml_file, uiapp):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.uiapp = uiapp
        self.year = engine.revit_year(uiapp)
        self._row_keys = None
        self._working = False
        self._timer = DispatcherTimer()
        self._timer.Interval = TimeSpan.FromSeconds(5)
        self._timer.Tick += self._tick
        self.Closed += self._on_closed

    def start_timer(self):
        self._timer.Start()

    def _on_closed(self, sender, args):
        self._timer.Stop()

    def _guard(self, fn, *args):
        """A failure inside a WPF handler is otherwise invisible."""
        try:
            return fn(*args)
        except core.ScheduleError as e:
            forms.alert(u"{0}".format(e), title=_TITLE)
        except Exception as e:
            forms.alert(u"DeeSchedule hit an error:\n\n{0}\n\n{1}".format(
                e, traceback.format_exc()[-900:]), title=_TITLE)

    # ---- rows
    def build_rows(self):
        return []

    def after_refresh(self):
        pass

    def refresh(self, force=False):
        rows = self.build_rows()
        keys = [r.key() for r in rows]
        if not force and keys == self._row_keys:
            return
        chosen = set(r.id for r in self.tasks_grid.SelectedItems)
        self._row_keys = keys
        self.tasks_grid.ItemsSource = None
        self.tasks_grid.ItemsSource = rows
        for row in rows:
            if row.id in chosen:
                self.tasks_grid.SelectedItems.Add(row)
        self.after_refresh()

    def _tick(self, sender, args):
        if self._working or engine.is_busy():
            return
        try:
            self.on_tick()
            self.refresh()
        except Exception:
            pass

    def on_tick(self):
        pass

    # ---- selection
    def selected(self, include_force=False):
        rows = list(self.tasks_grid.SelectedItems)
        if not include_force:
            rows = [r for r in rows if r.task is not None]
        if not rows:
            forms.alert("Select one or more tasks in the list first.", title=_TITLE)
        return rows

    def say(self, text):
        self.status_tb.Text = text

    # ---- actions
    def run_click(self, sender, args):
        self._guard(self._run)

    def _run(self):
        rows = self.selected(include_force=True)
        if not rows:
            return
        done = []
        self._working = True
        try:
            for row in rows:
                if row.task is None:
                    self._force(True)
                    done.append(u"{0}: on".format(row.tool))
                    continue
                self.say(u"Running {0}...".format(row.name))
                status, message = engine.run_task(
                    self.uiapp, row.id, manual=True, window=self)
                done.append(u"{0}: {1}".format(
                    row.name, message if status == "skipped" else u"{0} - {1}".format(
                        core.RESULT_LABELS.get(status, status), message)))
                if status == "cancelled":
                    break
        finally:
            self._working = False
            self.refresh(force=True)
        self.say(u"  |  ".join(done))
        if len(rows) > 1 or not done or ": Done" not in done[0]:
            forms.alert(u"\n\n".join(done), title=_TITLE)

    def resume_click(self, sender, args):
        self._guard(self._each, core.resume, "back on schedule", True)

    def pause_click(self, sender, args):
        self._guard(self._each, core.pause, "paused", False)

    def stop_click(self, sender, args):
        self._guard(self._each, core.stop, "stopped", False)

    def _each(self, action, word, force_on):
        rows = self.selected(include_force=True)
        if not rows:
            return
        count, problems = 0, []
        for row in rows:
            try:
                if row.task is None:
                    self._force(force_on)
                    count += 1
                elif action(row.id):
                    count += 1
            except core.ScheduleError as e:
                problems.append(u"{0}".format(e))
        self.refresh(force=True)
        self.say(u"{0} task(s) {1}.".format(count, word))
        if problems:
            forms.alert(u"\n\n".join(problems), title=_TITLE)

    def _force(self, on):
        import dee_force_state
        if on:
            dee_force_state.enable(self.uiapp)
        else:
            dee_force_state.disable(self.uiapp)

    def delete_click(self, sender, args):
        self._guard(self._delete)

    def _delete(self):
        rows = self.selected()
        if not rows:
            return
        names = u"\n".join(u"   - {0}: {1}".format(r.tool, r.name) for r in rows[:15])
        if len(rows) > 15:
            names += u"\n   ... and {0} more".format(len(rows) - 15)
        if not forms.alert(
                u"Delete {0} scheduled task(s)?\n\n{1}\n\nThis cannot be undone. Files "
                u"already exported are kept.".format(len(rows), names),
                title=_TITLE, yes=True, no=True):
            return
        for row in rows:
            core.delete(row.id)
        self.refresh(force=True)
        self.say("{0} task(s) deleted.".format(len(rows)))


# ==========================================================================
# create / edit
# ==========================================================================
class TaskDialog(TaskListWindow):
    def __init__(self, uiapp, tool, task=None, spec=None):
        TaskListWindow.__init__(self, _TASK_XAML, uiapp)
        self.tool = tool
        self.task = task            # the task being edited, or None
        self.spec = spec or {}      # what schedule() was given
        self.created = None
        self._extra_boxes = []

        for label, _kind in _REPEATS:
            self.repeat_cb.Items.Add(label)
        for label in ("minutes", "hours"):
            self.unit_cb.Items.Add(label)
        self.unit_cb.SelectedIndex = 0

        if task is not None:
            self._load_task(task)
        else:
            self._load_new()
        self._ready = True
        self._sync()
        self.refresh(force=True)
        self.start_timer()

    def build_rows(self):
        if self.task is not None:
            return []
        data = core.load()
        enabled = data["settings"].get("enabled", True)
        return [TaskRow(t, self.year, enabled) for t in data["tasks"] if t["tool"] == self.tool]

    # ---- loading
    def _load_new(self):
        spec = self.spec
        self.Title = u"{0} - Schedule".format(self.tool)
        self.name_tb.Text = spec.get("name") or self.tool
        self.summary_tb.Text = spec.get("summary") or ""
        self.models_tb.Text = u"Model: " + u"; ".join(
            s.get("label") or s.get("path") or "?" for s in spec["sources"])
        self.note_tb.Text = spec.get("note") or ""
        for key, label, default, tip in spec.get("extras") or []:
            box = CheckBox()
            box.Content = label
            box.IsChecked = bool(default)
            box.Margin = Thickness(0, 2, 0, 2)
            if tip:
                box.ToolTip = tip
            self.extras_panel.Children.Add(box)
            self._extra_boxes.append((key, box))
        self.output_tb.Text = spec.get("output") or ""
        self.repeat_cb.SelectedIndex = 2
        self.start_dp.SelectedDate = System.DateTime.Today
        self.time_tb.Text = "21:00"
        self.d0_cb.IsChecked = True
        self._fill_years(self.year)

    def _load_task(self, task):
        self.Title = u"{0} - Edit task".format(task["tool"])
        self.ok_b.Content = "Save changes"
        self.existing_gb.Visibility = Visibility.Collapsed
        self.name_tb.Text = task["name"]
        self.summary_tb.Text = task.get("summary") or ""
        self.models_tb.Text = u"Model: " + u"; ".join(
            s.get("label") or s.get("path") or "?" for s in task["sources"])
        self.note_tb.Text = ("To change what is exported, open {0} and schedule it again."
                             .format(task["tool"]))
        self.output_tb.Text = task.get("output") or ""
        self.dated_cb.IsChecked = bool(task.get("dated_folder"))
        self.missed_cb.IsChecked = bool(task.get("run_missed", True))
        self._fill_years(task.get("revit_year") or "")

        trigger = task["trigger"]
        kinds = [k for _l, k in _REPEATS]
        self.repeat_cb.SelectedIndex = kinds.index(trigger["type"])
        start = core.parse(trigger["start"])
        self.start_dp.SelectedDate = System.DateTime(start.year, start.month, start.day)
        self.time_tb.Text = start.strftime("%H:%M")
        end = core.parse(trigger.get("end"))
        if end is not None:
            self.end_dp.SelectedDate = System.DateTime(end.year, end.month, end.day)
        minutes = int(trigger.get("minutes") or 60)
        if minutes % 60 == 0:
            self.unit_cb.SelectedIndex = 1
            self.minutes_tb.Text = str(minutes // 60)
        else:
            self.minutes_tb.Text = str(minutes)
        self.day_tb.Text = str(trigger.get("day") or 1)
        for d in trigger.get("days") or []:
            self._day_boxes()[int(d)].IsChecked = True

    def _fill_years(self, wanted):
        years = [y for y in (wanted, self.year) if y]
        for year in sorted(set(years)):
            self.year_cb.Items.Add("Revit {0} only".format(year))
        self.year_cb.Items.Add(_ANY_YEAR)
        label = "Revit {0} only".format(wanted) if wanted else _ANY_YEAR
        self.year_cb.SelectedItem = label

    def _day_boxes(self):
        return [self.d0_cb, self.d1_cb, self.d2_cb, self.d3_cb,
                self.d4_cb, self.d5_cb, self.d6_cb]

    # ---- reading the form
    def _kind(self):
        return _REPEATS[max(0, self.repeat_cb.SelectedIndex)][1]

    def _read_trigger(self):
        clock = core.parse_time(self.time_tb.Text)
        if clock is None:
            raise core.ScheduleError("Enter the time as hours and minutes, e.g. 21:30.")
        date = self.start_dp.SelectedDate
        if date is None:
            raise core.ScheduleError("Pick a start date.")
        kind = self._kind()
        trigger = {"type": kind, "start": "{0:04d}-{1:02d}-{2:02d} {3:02d}:{4:02d}:00".format(
            date.Year, date.Month, date.Day, clock[0], clock[1])}
        end = self.end_dp.SelectedDate
        if end is not None and kind != core.ONCE:
            trigger["end"] = "{0:04d}-{1:02d}-{2:02d}".format(end.Year, end.Month, end.Day)
        if kind == core.INTERVAL:
            try:
                minutes = int((self.minutes_tb.Text or "").strip())
            except Exception:
                raise core.ScheduleError("Enter how often to repeat as a whole number.")
            trigger["minutes"] = minutes * (60 if self.unit_cb.SelectedIndex == 1 else 1)
        elif kind == core.WEEKLY:
            trigger["days"] = [i for i, b in enumerate(self._day_boxes()) if b.IsChecked is True]
        elif kind == core.MONTHLY:
            try:
                trigger["day"] = int((self.day_tb.Text or "").strip())
            except Exception:
                raise core.ScheduleError("Enter the day of the month as a number, 1 to 31.")
        core.check_trigger(trigger)
        return trigger

    def _read_output(self):
        output = (self.output_tb.Text or "").strip()
        if not output or not os.path.isabs(output):
            raise core.ScheduleError("Pick the output folder.")
        return output

    def _read_year(self):
        label = self.year_cb.SelectedItem or _ANY_YEAR
        return "" if label == _ANY_YEAR else label.replace("Revit ", "").replace(" only", "")

    # ---- events
    def _show(self, element, wanted):
        element.Visibility = Visibility.Visible if wanted else Visibility.Collapsed

    def _sync(self):
        kind = self._kind()
        self._show(self.interval_panel, kind == core.INTERVAL)
        self._show(self.weekly_panel, kind == core.WEEKLY)
        self._show(self.monthly_panel, kind == core.MONTHLY)
        self._show(self.end_panel, kind != core.ONCE)
        try:
            trigger = self._read_trigger()
            nxt = core.next_after(trigger, core.now())
            self.preview_tb.Text = (
                "{0}  -  next run: {1}".format(core.describe_trigger(trigger),
                                               _when_text(core.fmt(nxt)))
                if nxt else "That time has already passed.")
        except core.ScheduleError as e:
            self.preview_tb.Text = u"{0}".format(e)

    def repeat_changed(self, sender, args):
        if self._ready:
            self._guard(self._sync)

    def when_changed(self, sender, args):
        if self._ready:
            self._guard(self._sync)

    def browse_click(self, sender, args):
        def run():
            folder = _folder_dialog("Where should the scheduled files go?", self.output_tb.Text)
            if folder:
                self.output_tb.Text = folder
        self._guard(run)

    def ok_click(self, sender, args):
        try:
            self._save()
        except core.ScheduleError as e:
            self.say(u"{0}".format(e))
            return
        except Exception as e:
            self.say(u"Could not save the task: {0}".format(e))
            return
        self.Close()

    def _save(self):
        trigger = self._read_trigger()
        output = self._read_output()
        name = (self.name_tb.Text or "").strip()
        if not name:
            raise core.ScheduleError("Give the task a name.")
        if self.task is not None:
            self.created = core.update_task(
                self.task["id"], name=name, trigger=trigger, output=output,
                dated_folder=self.dated_cb.IsChecked is True,
                run_missed=self.missed_cb.IsChecked is True,
                revit_year=self._read_year())
            return
        payload = dict(self.spec["payload"])
        for key, box in self._extra_boxes:
            payload[key] = box.IsChecked is True
        self.created = core.add_task(core.new_task(
            self.tool, name, trigger, self.spec["sources"], payload,
            output=output, dated_folder=self.dated_cb.IsChecked is True,
            run_missed=self.missed_cb.IsChecked is True,
            revit_year=self._read_year(), summary=self.spec.get("summary") or ""))

    def cancel_click(self, sender, args):
        self.Close()


def schedule(tool, uiapp, sources, payload, summary="", name="", output="",
             extras=None, note=""):
    """Opens the Schedule dialog. `sources` come from engine.source_from_*;
    `payload` must be plain JSON data. `extras` are (payload key, label,
    default, tooltip) tick boxes. Returns the new task, or None."""
    dlg = TaskDialog(uiapp, tool, spec={
        "sources": sources, "payload": payload, "summary": summary,
        "name": name, "output": output, "extras": extras, "note": note})
    dlg.ShowDialog()
    if dlg.created is not None:
        off = "" if core.settings().get("enabled", True) else (
            "\n\nThe scheduler is switched OFF on this PC - turn it on in DeeSchedule.")
        forms.alert(u"Scheduled: {0}\n\n{1}\nNext run: {2}\n\nKeep Revit open at that time. "
                    u"See and control every task with the DeeSchedule button.{3}".format(
                        dlg.created["name"], core.describe_trigger(dlg.created["trigger"]),
                        _when_text(dlg.created["next_run"]), off), title=tool)
    return dlg.created


def schedule_doc(tool, uiapp, doc, payload, **kwargs):
    """schedule() for the one model a tool is open on."""
    try:
        sources = [engine.source_from_doc(doc)]
    except core.ScheduleError as e:
        forms.alert(u"{0}".format(e), title=tool)
        return None
    return schedule(tool, uiapp, sources, payload, **kwargs)


def edit(uiapp, task):
    dlg = TaskDialog(uiapp, task["tool"], task=task)
    dlg.ShowDialog()
    return dlg.created


# ==========================================================================
# the DeeSchedule button
# ==========================================================================
class ToolRow(object):
    def __init__(self, tool, count, active):
        self.tool = tool
        self.label = "{0}   {1}".format(tool, "({0} of {1} on)".format(active, count)
                                        if count else "(none)")


class ManagerWindow(TaskListWindow):
    def __init__(self, xaml_file, uiapp):
        TaskListWindow.__init__(self, xaml_file, uiapp)
        self._tool = _ALL
        self._tool_labels = None
        settings = core.settings()
        self.enabled_cb.IsChecked = bool(settings.get("enabled", True))
        self.notify_cb.IsChecked = bool(settings.get("notify", True))
        for label, _seconds in _WARNINGS:
            self.warn_cb.Items.Add(label)
        values = [s for _l, s in _WARNINGS]
        warn = settings.get("warn_seconds")
        self.warn_cb.SelectedIndex = values.index(warn) if warn in values else 1
        self.refresh(force=True)
        self._ready = True
        self.start_timer()

    # ---- rows
    def build_rows(self):
        data = core.load()
        enabled = data["settings"].get("enabled", True)
        self._all = data["tasks"]
        rows = [TaskRow(t, self.year, enabled) for t in data["tasks"]]
        force = self._force_row()
        self._update_tools(force)
        if self._tool != _ALL:
            rows = [r for r in rows if r.tool == self._tool]
        rows.sort(key=lambda r: (r.task.get("next_run") or "9999", r.tool, r.name.lower()))
        if force is not None and self._tool in (_ALL, _FORCE):
            if force.status != "Off" or self._tool == _FORCE:
                rows.append(force)
        return rows

    def _force_row(self):
        try:
            import dee_force_state
            return ForceRow(dee_force_state)
        except Exception:
            return None

    def _update_tools(self, force):
        def row(tool, items):
            return ToolRow(tool, len(items),
                           len([t for t in items if t["state"] == core.ACTIVE]))
        rows = [row(_ALL, self._all)]
        rows += [row(t, [x for x in self._all if x["tool"] == t]) for t in core.TOOLS]
        if force is not None:
            rows.append(ToolRow(_FORCE, 1, 0 if force.status == "Off" else 1))
        labels = [r.label for r in rows]
        if labels == self._tool_labels:
            return
        self._tool_labels = labels
        ready, self._ready = self._ready, False
        try:
            self.tools_lb.ItemsSource = None
            self.tools_lb.ItemsSource = rows
            for r in rows:
                if r.tool == self._tool:
                    self.tools_lb.SelectedItem = r
        finally:
            self._ready = ready

    def after_refresh(self):
        active = [t for t in self._all if t["state"] == core.ACTIVE and t.get("next_run")]
        text = "{0} scheduled task(s), {1} on".format(len(self._all), len(active))
        if active:
            first = min(active, key=lambda t: t["next_run"])
            text += u"  -  next: {0}, {1}".format(
                first["name"], _when_text(first["next_run"]))
        self.header_tb.Text = text
        self._show_details()

    def tool_changed(self, sender, args):
        def run():
            row = self.tools_lb.SelectedItem
            if row is not None:
                self._tool = row.tool
                self.refresh(force=True)
        if self._ready:
            self._guard(run)

    def on_tick(self):
        # This window is modal, so the startup watcher's event waits behind
        # it; due tasks are run from here instead.
        if core.due_tasks(core.load(), core.now(), self.year):
            self._working = True
            try:
                engine.run_due(self.uiapp, window=self)
            finally:
                self._working = False

    # ---- details
    def selection_changed(self, sender, args):
        if self._ready:
            self._guard(self._show_details)

    def _show_details(self):
        rows = list(self.tasks_grid.SelectedItems)
        if len(rows) != 1:
            self.details_tb.Text = (
                "{0} tasks selected.".format(len(rows)) if rows
                else "Select a task to see what it exports and its run history.")
            return
        row = rows[0]
        if row.task is None:
            self.details_tb.Text = (
                "DeeForce synchronizes every open workshared model while it is on. "
                "Run now / Resume turns it on for this Revit session, Pause / Stop turns "
                "it off. Set its interval with the DeeForce button.")
            return
        t = row.task
        lines = [u"{0} - {1}".format(t["tool"], t["name"]),
                 u"Exports:  {0}".format(t.get("summary") or "-"),
                 u"When:     {0}".format(row.when),
                 u"Output:   {0}{1}".format(
                     t.get("output") or "-",
                     "   (dated subfolder per run)" if t.get("dated_folder") else ""),
                 u"Revit:    {0}".format(t.get("revit_year") or "any version"),
                 u"Missed:   {0}".format(
                     "runs when Revit is open again" if t.get("run_missed", True)
                     else "skipped"),
                 u"Runs:     {0}, {1} failed".format(
                     t.get("run_count") or 0, t.get("fail_count") or 0)]
        for src in t.get("sources") or []:
            lines.append(u"Model:    {0}  [{1}]".format(
                src.get("label") or "?",
                src.get("path") if src["kind"] == "file" else "ACC / BIM 360"))
        for what, path in core.missing_paths(t):
            lines.append(u"NOT FOUND on this PC - {0}: {1}  (use Fix paths...)".format(what, path))
        history = list(t.get("history") or [])
        if history:
            lines += [u"", u"History (newest first):"]
            for h in reversed(history):
                lines.append(u"  {0}  {1}  {2}".format(
                    h.get("start") or "", core.RESULT_LABELS.get(h.get("status"), "?"),
                    h.get("message") or ""))
        self.details_tb.Text = u"\r\n".join(lines)

    # ---- single-task buttons
    def _one(self):
        rows = self.selected()
        if len(rows) > 1:
            forms.alert("Select just one task for this.", title=_TITLE)
            return None
        return rows[0] if rows else None

    def edit_click(self, sender, args):
        self._guard(self._edit)

    def _edit(self):
        if not list(self.tasks_grid.SelectedItems):
            return      # a double-click on the header or empty space
        row = self._one()
        if row is None:
            return
        task = core.get_task(row.id)
        if task is not None and edit(self.uiapp, task) is not None:
            self.say(u"'{0}' saved.".format(task["name"]))
        self.refresh(force=True)

    def output_click(self, sender, args):
        def run():
            row = self._one()
            if row is None:
                return
            for folder in (row.task.get("last_output"), row.task.get("output")):
                if folder and os.path.isdir(folder):
                    Process.Start("explorer.exe", u'"{0}"'.format(folder))
                    return
            forms.alert("The output folder does not exist yet.", title=_TITLE)
        self._guard(run)

    def log_click(self, sender, args):
        def run():
            row = self._one()
            if row is None:
                return
            path = core.last_log(row.id)
            if not path:
                forms.alert("This task has not run yet.", title=_TITLE)
                return
            Process.Start("notepad.exe", u'"{0}"'.format(path))
        self._guard(run)

    def select_all_click(self, sender, args):
        self.tasks_grid.SelectAll()

    # ---- scheduler settings
    def settings_changed(self, sender, args):
        def run():
            core.save_settings(
                enabled=self.enabled_cb.IsChecked is True,
                notify=self.notify_cb.IsChecked is True,
                warn_seconds=_WARNINGS[max(0, self.warn_cb.SelectedIndex)][1])
            self.refresh(force=True)
        if self._ready:
            self._guard(run)

    def _show_settings(self):
        settings = core.settings()
        values = [s for _l, s in _WARNINGS]
        ready, self._ready = self._ready, False
        try:
            self.enabled_cb.IsChecked = bool(settings.get("enabled", True))
            self.notify_cb.IsChecked = bool(settings.get("notify", True))
            warn = settings.get("warn_seconds")
            self.warn_cb.SelectedIndex = values.index(warn) if warn in values else 1
        finally:
            self._ready = ready

    # ---- moving to another PC
    def export_click(self, sender, args):
        self._guard(self._export)

    def _export(self):
        every = core.tasks()
        if not every:
            forms.alert("There are no scheduled tasks to export yet.", title=_TITLE)
            return
        ids = set(r.id for r in self.tasks_grid.SelectedItems if r.task is not None)
        chosen = every
        if ids and len(ids) < len(every):
            some = "Only the {0} selected task(s)".format(len(ids))
            answer = forms.alert(
                "Which tasks should be exported?", title=_TITLE,
                options=["All {0} tasks".format(len(every)), some, "Cancel"])
            if not answer or answer == "Cancel":
                return
            if answer == some:
                chosen = [t for t in every if t["id"] in ids]
        dlg = SaveFileDialog()
        dlg.Title = "Export scheduled tasks"
        dlg.Filter = "DeeSchedule tasks (*.json)|*.json"
        dlg.FileName = "DeeSchedule_{0}.json".format(core.now().strftime("%Y-%m-%d"))
        if dlg.ShowDialog() != DialogResult.OK:
            return
        count = core.export_to_file(dlg.FileName, chosen)
        self.say(u"{0} task(s) exported.".format(count))
        forms.alert(
            u"{0} task(s) and the scheduler settings saved to:\n{1}\n\nOn the other PC open "
            u"DeeSchedule and press Import tasks. The models themselves are not in the file - "
            u"the other PC needs access to the same ACC project, server or folders.".format(
                count, dlg.FileName), title=_TITLE)

    def import_click(self, sender, args):
        self._guard(self._import)

    def _import(self):
        dlg = OpenFileDialog()
        dlg.Title = "Import scheduled tasks"
        dlg.Filter = "DeeSchedule tasks (*.json)|*.json"
        if dlg.ShowDialog() != DialogResult.OK:
            return
        incoming, settings, problems = core.read_export(dlg.FileName)
        if not incoming:
            raise core.ScheduleError(u"No usable task in that file.\n\n" + u"\n".join(problems))

        have = set(t["id"] for t in core.tasks())
        same = [t for t in incoming if t["id"] in have]
        mode = "skip"
        if same:
            options = ["Replace them", "Keep both", "Skip them"]
            answer = forms.alert(
                u"{0} of the {1} task(s) are already on this PC. What should happen to "
                u"those?".format(len(same), len(incoming)), title=_TITLE,
                options=options + ["Cancel"])
            if answer not in options:
                return
            mode = ["replace", "copy", "skip"][options.index(answer)]

        paused = "Import paused (check them first)"
        start = "Import and start"
        answer = forms.alert(
            u"Import {0} task(s)?".format(len(incoming)), title=_TITLE,
            options=[paused, start, "Cancel"])
        if answer not in (paused, start):
            return
        take = False
        if settings:
            take = forms.alert(
                "Also use the scheduler settings from that file (on/off, notifications, "
                "warning time)?", title=_TITLE, yes=True, no=True)
        counts = core.import_tasks(incoming, mode, start=(answer == start),
                                   new_settings=settings if take else None)
        self._show_settings()
        self.refresh(force=True)
        text = "{added} added, {replaced} replaced, {skipped} skipped.".format(**counts)
        if counts["finished"]:
            text += "\n{0} had no run left (their date has passed).".format(counts["finished"])
        if answer == paused:
            text += "\n\nThey are paused - select them and press Resume when ready."
        if problems:
            text += u"\n\nNot imported:\n" + u"\n".join(problems)
        self.say(text.split("\n")[0])
        forms.alert(text, title=_TITLE)

        ids = set(t["id"] for t in incoming)
        if core.missing_folders([t for t in core.tasks() if t["id"] in ids]):
            if forms.alert("Some models or folders of these tasks do not exist on this PC. "
                           "Point them to the right place now?", title=_TITLE,
                           yes=True, no=True):
                self._relink()

    def relink_click(self, sender, args):
        self._guard(self._relink)

    def _relink(self):
        folders = core.missing_folders(core.tasks())
        if not folders:
            forms.alert("Every model and output folder exists on this PC.", title=_TITLE)
            return
        changed, skipped = 0, set()
        while True:
            # Fixing one folder can fix others below it, so ask afresh.
            left = [f for f in core.missing_folders(core.tasks()) if f not in skipped]
            if not left:
                break
            old = left[0]
            answer = forms.alert(
                u"Not found on this PC:\n\n{0}\n\nChoose the folder that replaces it?".format(old),
                title=_TITLE, options=["Choose folder...", "Skip", "Stop here"])
            if answer == "Skip":
                skipped.add(old)
                continue
            if answer != "Choose folder...":
                break
            new = _folder_dialog(u"Folder that replaces {0}".format(old))
            if not new:
                skipped.add(old)
                continue
            changed += core.relink(old, new)
            skipped.add(old)
        self.refresh(force=True)
        self.say("{0} task(s) re-pointed.".format(changed))

    def close_click(self, sender, args):
        self.Close()
