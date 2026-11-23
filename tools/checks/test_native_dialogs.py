# -*- coding: utf-8 -*-
"""Tests for lib/dee_native_dialogs.py with real Windows message boxes.

Runs in pyRevit's IronPython, without Revit:

    powershell -ExecutionPolicy Bypass -File tools/checks/run_ironpython.ps1 tools/checks/test_native_dialogs.py
"""
import ctypes
import os
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "lib"))
import dee_native_dialogs as nd

user32 = ctypes.windll.user32


def show(title, text, result):
    def run():
        result.append(user32.MessageBoxW(None, text, title, 0))
    t = threading.Thread(target=run)
    t.daemon = True
    t.start()
    return t


def wait(cond, seconds=6.0):
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


class Native(unittest.TestCase):
    def test_answers_the_exporter_box(self):
        result = []
        w = nd.Watcher().start()
        try:
            show("Navisworks NWC Exporter", u"No suitable geometry found. Check the settings.", result)
            self.assertTrue(wait(lambda: result), "box was not answered")
        finally:
            w.stop()
        self.assertEqual(result, [1])
        self.assertEqual(w.messages(), [u"No suitable geometry found. Check the settings."])

    def test_two_in_a_row(self):
        w = nd.Watcher().start()
        try:
            for i in range(2):
                result = []
                show("Navisworks NWC Exporter", u"box %d" % i, result)
                self.assertTrue(wait(lambda: result))
        finally:
            w.stop()
        self.assertEqual(w.messages(), [u"box 0", u"box 1"])

    def test_leaves_other_dialogs_alone(self):
        result = []
        w = nd.Watcher().start()
        try:
            show("Some other program", u"keep me", result)
            time.sleep(1.5)
            self.assertEqual(result, [])
            self.assertEqual(w.caught, [])
        finally:
            w.stop()
            hwnd = user32.FindWindowW(None, u"Some other program")
            user32.PostMessageW(hwnd, 0x0010, 0, 0)       # WM_CLOSE
        self.assertTrue(wait(lambda: result))

    def test_poll_without_thread(self):
        result = []
        show("Navisworks NWC Exporter", u"polled", result)
        w = nd.Watcher()
        self.assertTrue(wait(lambda: (w.poll() or True) and result))
        self.assertEqual(w.messages(), [u"polled"])

    def test_nothing_to_do(self):
        w = nd.Watcher().start()
        time.sleep(0.5)
        w.stop()
        self.assertEqual(w.messages(), [])


suite = unittest.TestLoader().loadTestsFromTestCase(Native)
result = unittest.TextTestRunner(stream=sys.stdout, verbosity=1).run(suite)
print("PASS" if result.wasSuccessful() else "FAIL")
