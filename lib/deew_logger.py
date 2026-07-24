# -*- coding: utf-8 -*-
"""
deew_logger
Structured logging service shared by every DeeW.Cloud tool. Every
entry is a plain dict (never a custom object that could itself throw
on serialization) so it flows straight into deew_report_generator's
CSV/Excel/TXT export without extra glue code, and into a tool's Live
Status UI panel the same way.

Writes to two places:
  1. An in-memory list (self.entries) - always available, drives the
     Live Status panel and the end-of-run report.
  2. A best-effort rotating text file under Dee.extension/logs/deew_cloud/
     (one file per tool per calendar day). Failing to write this file
     (locked, read-only, no permissions) is swallowed silently - losing
     the file log must never be allowed to interrupt the actual
     cloud-conversion work it describes.

Every public method here is wrapped so a broken logging call can never
propagate an exception into the calling tool - "never allow Revit to
crash because of an unhandled exception" applies to the logger itself
too, not just to Revit API calls.
"""
import os
import datetime
import threading


class DeeWLogger(object):
    """One instance per tool run. Not a singleton - each tool creates
    its own so concurrent runs (unlikely, but not impossible) never
    interleave log files."""

    def __init__(self, tool_name, log_dir=None):
        self.tool_name = tool_name
        self.entries = []
        self._lock = threading.Lock()
        self._file_path = None
        try:
            base_dir = log_dir or os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "deew_cloud")
            if not os.path.isdir(base_dir):
                os.makedirs(base_dir)
            stamp = datetime.datetime.now().strftime("%Y-%m-%d")
            self._file_path = os.path.join(base_dir, "{0}_{1}.log".format(tool_name, stamp))
        except Exception:
            self._file_path = None

    def _write(self, level, message, **context):
        try:
            ts = datetime.datetime.now()
            entry = {
                "timestamp": ts.strftime("%Y-%m-%d %H:%M:%S"),
                "level": level,
                "tool": self.tool_name,
                "message": message,
            }
            entry.update(context)
            with self._lock:
                self.entries.append(entry)
            self._append_to_file(entry)
            return entry
        except Exception:
            return None

    def _append_to_file(self, entry):
        if not self._file_path:
            return
        try:
            line = "[{0}] {1:<8s} {2}\n".format(entry["timestamp"], entry["level"], entry["message"])
            with open(self._file_path, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass

    def debug(self, message, **context):
        return self._write("DEBUG", message, **context)

    def info(self, message, **context):
        return self._write("INFO", message, **context)

    def warning(self, message, **context):
        return self._write("WARNING", message, **context)

    def error(self, message, **context):
        return self._write("ERROR", message, **context)

    def critical(self, message, **context):
        return self._write("CRITICAL", message, **context)

    def exception(self, message, exc, **context):
        """Records an exception WITHOUT re-raising it - the caller
        decides whether to continue or abort; this method only ever
        records, matching the "continue processing remaining files"
        requirement for batch tools."""
        try:
            context["exception_type"] = type(exc).__name__
            context["exception_message"] = str(exc)
        except Exception:
            pass
        return self._write("ERROR", message, **context)

    def entries_by_level(self, level):
        try:
            with self._lock:
                return [e for e in self.entries if e.get("level") == level]
        except Exception:
            return []

    def has_errors(self):
        try:
            with self._lock:
                return any(e.get("level") in ("ERROR", "CRITICAL") for e in self.entries)
        except Exception:
            return False

    def file_path(self):
        return self._file_path
