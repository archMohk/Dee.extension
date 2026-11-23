# -*- coding: utf-8 -*-
"""DeeNWCs background probe - runs the "More than one Revit" export for real,
inside Revit, on two models it makes itself (two background Revits are
started). Run it after touching DeeNWCs' nwc_worker.py or lib/dee_background.py:

    pyrevit run "<abs path>/Dee.extension/tools/checks/nwc_background_probe.py" --revit=2024

Checks: the options window's Run by controls, the error tick box is not cut
off, ACC output settings, and that both background Revits start, take one
model each and report back. Without a Navisworks exporter in that Revit the
only failure expected per model is "no Navisworks exporter" - the result
rows then prove the plumbing; with one installed they are real exports.

Result: nwc_background_probe_<year>.log next to this file; last line PASS/FAIL.
"""
import io
import os
import shutil
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
EXT = os.environ.get("DEE_EXTENSION_ROOT", os.path.dirname(os.path.dirname(HERE)))
if not os.path.isdir(os.path.join(EXT, "lib")):
    EXT = r"D:\MK\OneDrive Folders\OneDrive\PyRevit Extensions\Dee.extension"
OUT = os.path.join(EXT, "tools", "checks", "themes")
if not os.path.isdir(OUT):
    os.makedirs(OUT)
sys.path.insert(0, os.path.join(EXT, "lib"))
WORK = tempfile.mkdtemp(prefix="dee_nwc_probe_")
os.environ["DEE_SCHEDULE_DIR"] = os.path.join(WORK, "store")

uiapp = __revit__  # noqa: F821
app = uiapp.Application
LOG = os.path.join(OUT, "nwc_background_probe_{0}.log".format(app.VersionNumber))
lines, failed = [], []


def log(text):
    lines.append(u"{0}".format(text))
    with io.open(LOG, "w", encoding="utf-8") as f:
        f.write(u"\n".join(lines))


def check(what, ok, detail=""):
    log(u"{0}  {1}  {2}".format("ok  " if ok else "FAIL", what, detail))
    if not ok:
        failed.append(what)


def main():
    import dee_schedule_engine as engine
    from Autodesk.Revit.DB import UnitSystem, SaveAsOptions

    sp = engine.load_runner(uiapp, "DeeNWCs")
    sp["_save_settings"] = lambda s: None
    win = sp["DeeNWCsOptionsWindow"](sp["_XAML_FILE"])
    check("Run by defaults to this session", win.run_session_rb.IsChecked is True)
    win.run_bg_rb.IsChecked = True
    win.run_instances_cb.SelectedIndex = 1
    win.output_acc_rb.IsChecked = True
    win._finish("run")
    check("window gives background + 3 Revits + ACC output",
          win.config["run_by"] == "background" and win.config["instances"] == 3
          and win.config["output"] == "acc", win.status_tb.Text)

    win = sp["DeeNWCsOptionsWindow"](sp["_XAML_FILE"])
    box = win.dee_delete_errors_cb
    check("error tick box wraps (is not cut off)",
          box is not None and box.Content.__class__.__name__ == "TextBlock")

    docs = []
    for n in (1, 2):
        doc = app.NewProjectDocument(UnitSystem.Metric)
        doc.SaveAs(os.path.join(WORK, "Model {0}.rvt".format(n)), SaveAsOptions())
        docs.append(doc)
    printed = []

    class Out(object):
        def print_md(self, text):
            printed.append(text)
    sp["output"] = Out()
    config = sp["_load_settings"]()
    config["run_by"], config["instances"] = "background", 2
    config["super_config"] = dict(config["super_config"], enabled=True)
    folder = os.path.join(WORK, "nwc")
    os.makedirs(folder)
    rows = sp["_run_background"](uiapp, config, folder, None, None, docs, None)
    log(u"\n".join(printed))
    for row in rows:
        log(u"   {0}".format(row))
    check("one result per model", len(rows) == 2, rows)
    check("both Revits started and reported",
          not any("ended before" in (r[3] or "") or "Could not start" in (r[3] or "")
                  for r in rows), rows)
    if sp["_has_exporter"]():
        check("files written", len([f for f in os.listdir(folder) if f.endswith(".nwc")]) == 2,
              os.listdir(folder))
    else:
        check("only failure is the missing exporter",
              all("exporter" in (r[3] or "") for r in rows), rows)
    for doc in docs:
        doc.Close(False)


try:
    main()
except Exception:
    check("crash", False, traceback.format_exc())
shutil.rmtree(WORK, ignore_errors=True)
log("FAIL" if failed else "PASS")
