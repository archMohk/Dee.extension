# -*- coding: utf-8 -*-
"""Tests for how lib/dee_background deals with a background Revit that
crashes (the "Revit 2024.3 Error Report" box keeps the process, and so the
whole batch, alive). Runs in pyRevit's IronPython, without Revit - the crash
box is a real Windows message box with that title:

    powershell -ExecutionPolicy Bypass -File tools/checks/run_ironpython.ps1 tools/checks/test_background_crash.py

The end-to-end version (a real Revit that really crashes) is
tools/checks/crash_recovery_probe.py.
"""
import ctypes
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "lib"))
import dee_background as bg
import dee_native_dialogs as nd

user32 = ctypes.windll.user32


def show(title, text="x"):
    result = []

    def run():
        result.append(user32.MessageBoxW(None, text, title, 0))
    t = threading.Thread(target=run)
    t.daemon = True
    t.start()
    return result


def wait(cond, seconds=6.0):
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


class Chunk(unittest.TestCase):
    ITEMS = [{"key": k, "name": k.upper()} for k in "abcd"]

    def test_died_on_the_first_without_a_result(self):
        died, rest = bg.remaining_after_crash(self.ITEMS, "key", {"a"})
        self.assertEqual(died["key"], "b")
        self.assertEqual([i["key"] for i in rest], ["c", "d"])

    def test_nothing_done_yet(self):
        died, rest = bg.remaining_after_crash(self.ITEMS, "key", set())
        self.assertEqual(died["key"], "a")
        self.assertEqual(len(rest), 3)

    def test_died_on_the_last_one(self):
        died, rest = bg.remaining_after_crash(self.ITEMS, "key", {"a", "b", "c"})
        self.assertEqual((died["key"], rest), ("d", []))

    def test_everything_done(self):
        self.assertEqual(bg.remaining_after_crash(self.ITEMS, "key", {"a", "b", "c", "d"}), (None, []))


class CrashBox(unittest.TestCase):
    def test_finds_the_error_report_box_of_a_process(self):
        result = show("Revit 2024.3 Error Report")
        try:
            self.assertTrue(wait(lambda: bg.crash_window(os.getpid())), "box not seen")
            self.assertEqual(bg.crash_window(os.getpid()), "Revit 2024.3 Error Report")
        finally:
            w = nd.Watcher([("error report", ("ok",))]).start()
            wait(lambda: result)
            w.stop()

    def test_other_boxes_are_not_a_crash(self):
        result = show("Navisworks NWC Exporter")
        try:
            self.assertTrue(wait(lambda: nd.visible_titles(os.getpid())), "box not seen")
            self.assertIsNone(bg.crash_window(os.getpid()))
        finally:
            w = nd.Watcher().start()
            wait(lambda: result)
            w.stop()

    def test_another_process_has_no_boxes_of_ours(self):
        self.assertEqual(nd.visible_titles(0x7FFFFFF0), [])


class Recover(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dee_crash_test_")
        self.said = []
        self.spawned = []
        job = bg.BackgroundJob()
        job.root = self.tmp
        job._spec = {"key_field": "key"}
        job._spawn = lambda idx, folder, chunk, restarts=0: self.spawned.append((idx, folder, chunk, restarts))
        self.job = job
        self.items = [{"key": k, "name": k.upper()} for k in "abcd"]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def worker(self, done, restarts=0, stop=False):
        folder = os.path.join(self.tmp, "revit_1")
        os.makedirs(folder)
        with io.open(os.path.join(folder, "results.json"), "w", encoding="utf-8") as f:
            f.write(u"" + json.dumps(dict((k, {"ok": True}) for k in done)))
        if stop:
            with io.open(os.path.join(folder, "stop.txt"), "w", encoding="utf-8") as f:
                f.write(u"stop")
        return {"idx": 1, "folder": folder, "results": os.path.join(folder, "results.json"),
                "chunk": self.items, "restarts": restarts}

    def say(self, tag, text):
        self.said.append(text)

    def test_reports_the_model_and_restarts_for_the_rest(self):
        w = self.worker(["a"])
        self.job._recover(w, "bg1", self.say)
        self.assertIn("b", self.job._made_up)
        self.assertFalse(self.job._made_up["b"]["ok"])
        self.assertIn("crashed", self.job._made_up["b"]["detail"])
        self.assertEqual(len(self.spawned), 1)
        idx, folder, chunk, restarts = self.spawned[0]
        self.assertEqual([i["key"] for i in chunk], ["c", "d"])
        self.assertEqual((idx, restarts), (1, 1))
        self.assertTrue(w["replaced"])

    def test_results_include_the_made_up_one(self):
        w = self.worker(["a"])
        self.job.workers.append(w)
        self.job._recover(w, "bg1", self.say)
        got = self.job.results()
        self.assertEqual(sorted(got), ["a", "b"])

    def test_gives_up_after_two_restarts(self):
        w = self.worker(["a"], restarts=2)
        self.job._recover(w, "bg1", self.say)
        self.assertEqual(self.spawned, [])
        self.assertIn("b", self.job._made_up)
        self.assertTrue(any("crashed 3 times" in s for s in self.said))

    def test_no_restart_after_the_user_pressed_stop(self):
        w = self.worker(["a"], stop=True)
        self.job._recover(w, "bg1", self.say)
        self.assertEqual(self.spawned, [])

    def test_last_model_crashed_nothing_to_restart(self):
        w = self.worker(["a", "b", "c"])
        self.job._recover(w, "bg1", self.say)
        self.assertIn("d", self.job._made_up)
        self.assertEqual(self.spawned, [])

    def test_keys_have_no_duplicates_after_a_restart(self):
        self.job.workers.append({"keys": ["a", "b", "c", "d"]})
        self.job.workers.append({"keys": ["c", "d"]})
        self.assertEqual(self.job.keys(), ["a", "b", "c", "d"])


suite = unittest.TestSuite()
for case in (Chunk, CrashBox, Recover):
    suite.addTests(unittest.TestLoader().loadTestsFromTestCase(case))
result = unittest.TextTestRunner(stream=sys.stdout, verbosity=1).run(suite)
print("PASS" if result.wasSuccessful() else "FAIL")
