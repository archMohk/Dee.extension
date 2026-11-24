# -*- coding: utf-8 -*-
"""Background-job crash probe - a real background Revit really crashes.
Run it after touching lib/dee_background.py's crash handling:

    pyrevit run "<abs path>/Dee.extension/tools/checks/crash_recovery_probe.py" --revit=2024

It starts ONE background Revit with five models: one, boom_box, two,
boom_thread, three. boom_box shows a window titled "Revit 2024.3 Error Report"
and hangs (a Revit that crashed on its main thread stays alive behind that
box), boom_thread kills the process without any box. The probe checks that the job
  * notices both, ends the Revit and reports that model as failed (crashed),
  * keeps the results already written ("one", "two"),
  * starts a new Revit each time for the models not reached yet.

Result: crash_recovery_probe_<year>.log in tools/checks/themes; last line
PASS/FAIL. A Revit still alive at the end is ended by the probe.
"""
import io
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
EXT = os.environ.get("DEE_EXTENSION_ROOT", os.path.dirname(os.path.dirname(HERE)))
if not os.path.isdir(os.path.join(EXT, "lib")):
    EXT = r"D:\MK\OneDrive Folders\OneDrive\PyRevit Extensions\Dee.extension"
OUT = os.path.join(EXT, "tools", "checks", "themes")
if not os.path.isdir(OUT):
    os.makedirs(OUT)
sys.path.insert(0, os.path.join(EXT, "lib"))

uiapp = __revit__  # noqa: F821
app = uiapp.Application
LOG = os.path.join(OUT, "crash_recovery_probe_{0}.log".format(app.VersionNumber))
lines, failed = [], []


def log(text):
    lines.append(u"[{0}] {1}".format(time.strftime("%H:%M:%S"), text))
    with io.open(LOG, "w", encoding="utf-8") as f:
        f.write(u"\n".join(lines))


def check(what, ok, detail=""):
    log(u"{0}  {1}  {2}".format("ok  " if ok else "FAIL", what, detail))
    if not ok:
        failed.append(what)


def main():
    import dee_background as bg
    items = [{"key": k, "name": k, "crash": {"boom_box": "box", "boom_thread": "thread"}.get(k)}
             for k in ("one", "boom_box", "two", "boom_thread", "three")]
    job = bg.BackgroundJob.start("DeeCrashProbe", "crash_worker", "process", items, {},
                                 str(app.VersionNumber), instances=1, extra_paths=[HERE], log=log)
    try:
        end = time.time() + 600
        done, how = False, None
        while time.time() < end:
            done, how = job.poll(lambda tag, line: log(u"{0} {1}".format(tag, line)))
            if done:
                break
            time.sleep(2)
        check("the job finished by itself", done, "how={0}".format(how))
        got = job.results()
        ok = lambda k: got.get(k, {}).get("ok") is True
        crashed = lambda k: got.get(k, {}).get("ok") is False and got.get(k, {}).get("crashed") is True
        check("'one' kept its result", ok("one"), str(got.get("one")))
        check("'boom_box' is reported as crashed", crashed("boom_box"), str(got.get("boom_box")))
        check("'two' was processed by a new Revit", ok("two"), str(got.get("two")))
        check("'boom_thread' is reported as crashed", crashed("boom_thread"), str(got.get("boom_thread")))
        check("'three' was processed by a newer Revit", ok("three"), str(got.get("three")))
        check("three Revits were used", len(job.workers) == 3, "workers={0}".format(len(job.workers)))
        check("every key has a result", sorted(got) == sorted(job.keys()),
              "got={0} keys={1}".format(sorted(got), job.keys()))
        box = [t for t in lines if "This Revit crashed ('" in t]
        check("the Error Report box was found and its Revit ended by the job", bool(box),
              box[0][-70:] if box else "no")
    finally:
        for w in job.workers:
            if w.get("pid"):
                bg._kill_revit(w["pid"])
            try:
                w["process"].Kill()
            except Exception:
                pass


try:
    main()
except Exception:
    failed.append("exception")
    log(traceback.format_exc())
log("PASS" if not failed else "FAIL: " + ", ".join(failed))
