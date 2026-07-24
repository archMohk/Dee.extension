# -*- coding: utf-8 -*-
"""
deew_progress_service
Shared progress-reporting service for DeeW.Cloud batch tools - tracks
per-file timing and success/fail/skip counts, and renders them
through pyrevit.forms.ProgressBar.

--------------------------------------------------------------------
Design note: why this wraps pyrevit.forms.ProgressBar instead of a
custom multi-field "Progress Window"
--------------------------------------------------------------------
The spec calls for a progress window showing the Progress Bar,
Current File, Current Step, Elapsed Time, Estimated Remaining Time,
Successful/Failed/Skipped Files, and Average Processing Time as
distinct fields. Building a fully custom, always-responsive WPF
window that keeps redrawing DURING a tight, synchronous processing
loop is real added risk here: the Revit API is strictly single-
threaded (every Document/Application call in this loop MUST run on
Revit's own UI thread - the same thread WPF would need to pump for
redraws), so a hand-built window needs to correctly re-derive
Dispatcher-pumping logic to avoid freezing mid-batch.
pyrevit.forms.ProgressBar already solves exactly this problem (it's
specifically designed to redraw correctly when update_progress() is
called repeatedly inside a synchronous loop), so this service reuses
that proven mechanism rather than re-deriving it, and renders every
required stat into one dense, readable status line instead of
separate labeled panels. All the REQUIRED INFORMATION is still shown -
only the visual layout differs from a literal multi-panel window.
"""
import time


class RunStats(object):
    """Plain counters - a simple object rather than a dict so callers
    get attribute access (stats.succeeded) instead of string keys,
    while still being trivially readable by deew_report_generator."""

    def __init__(self, total=0):
        self.total = total
        self.succeeded = 0
        self.failed = 0
        self.skipped = 0
        self.durations = []   # seconds per completed file, for the average

    def record(self, outcome, duration_seconds):
        if outcome == "success":
            self.succeeded += 1
        elif outcome == "failed":
            self.failed += 1
        elif outcome == "skipped":
            self.skipped += 1
        self.durations.append(duration_seconds)

    @property
    def average_seconds(self):
        if not self.durations:
            return 0.0
        return sum(self.durations) / float(len(self.durations))

    @property
    def processed(self):
        return self.succeeded + self.failed + self.skipped


def format_duration(seconds):
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return "{0}s".format(seconds)
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return "{0}m {1:02d}s".format(minutes, secs)
    hours, mins = divmod(minutes, 60)
    return "{0}h {1:02d}m".format(hours, mins)


class DeeWProgressService(object):
    """Context manager:
        with DeeWProgressService("DeeW.Sharing", total_files) as prog:
            for f in files:
                prog.step(f.name, "Opening")
                ...
                prog.step(f.name, "Uploading")
                ...
                prog.finish_file("success")
    """

    def __init__(self, tool_title, total_files):
        self.tool_title = tool_title
        self.total_files = total_files
        self.stats = RunStats(total=total_files)
        self._pb = None
        self._start_time = None
        self._file_start_time = None
        self._current_index = 0

    def __enter__(self):
        from pyrevit import forms
        self._start_time = time.time()
        try:
            self._pb = forms.ProgressBar(title=self.tool_title + " - starting...", cancellable=True)
        except Exception:
            self._pb = None
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if self._pb is not None:
                self._pb.close()
        except Exception:
            pass
        return False

    def step(self, current_file, current_step):
        self._file_start_time = time.time()
        self._render(current_file, current_step)

    def _render(self, current_file, current_step):
        if self._pb is None:
            return
        elapsed = time.time() - self._start_time
        avg = self.stats.average_seconds
        remaining_files = max(0, self.total_files - self.stats.processed)
        eta = avg * remaining_files if avg > 0 else 0
        title = (
            "{tool} | File: {file} | Step: {step} | "
            "OK {ok} / Fail {fail} / Skip {skip} of {total} | "
            "Elapsed {elapsed} | ETA {eta} | Avg {avg}/file".format(
                tool=self.tool_title, file=current_file, step=current_step,
                ok=self.stats.succeeded, fail=self.stats.failed, skip=self.stats.skipped,
                total=self.total_files, elapsed=format_duration(elapsed),
                eta=format_duration(eta), avg=format_duration(avg)))
        try:
            self._pb.title = title
            self._pb.update_progress(self._current_index, self.total_files)
        except Exception:
            pass

    def finish_file(self, outcome):
        """outcome: "success" / "failed" / "skipped" """
        duration = time.time() - (self._file_start_time or time.time())
        self.stats.record(outcome, duration)
        self._current_index += 1

    @property
    def cancelled(self):
        try:
            return bool(self._pb.cancelled)
        except Exception:
            return False

    @property
    def elapsed_seconds(self):
        return time.time() - self._start_time if self._start_time else 0.0
