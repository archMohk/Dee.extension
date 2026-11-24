# -*- coding: utf-8 -*-
"""Worker for tools/checks/crash_recovery_probe.py: does nothing useful, and
misbehaves on purpose for an item with "crash" set:

  "box"     shows a window titled "Revit 2024.3 Error Report" and hangs - what
            a Revit that crashed on its main thread does (Revit's crash
            handler shows Autodesk's Error Report box and the process stays
            alive behind it, which is how one real background Revit held a
            whole batch for hours). The window is a plain message box here.
  "thread"  an unhandled exception on a .NET thread - the process just dies,
            with no box at all"""
import time


def process(ctx, item):
    ctx.log(u"working on {0}".format(item.get("name")))
    mode = item.get("crash")
    if mode == "box":
        import ctypes
        import threading
        threading.Thread(target=lambda: ctypes.windll.user32.MessageBoxW(
            None, u"A software problem has caused Revit to close unexpectedly.",
            u"Revit 2024.3 Error Report", 0)).start()
        time.sleep(900)
    elif mode == "thread":
        import System
        from System.Threading import Thread, ThreadStart

        def blow_up():
            raise System.InvalidOperationException("Dee crash test - on purpose")
        Thread(ThreadStart(blow_up)).Start()
        time.sleep(900)
    return {"ok": True, "rows": [[True, item.get("name"), "-", "fine"]]}
