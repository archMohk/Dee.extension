# -*- coding: utf-8 -*-
"""Schedule probe - runs DeeSchedule for real, inside Revit, on a model it
makes itself. Run it after touching lib/dee_schedule_*.py or the
scheduled_run() of an export tool:

    pyrevit run "<abs path>/Dee.extension/tools/checks/schedule_probe.py" --revit=2024

It builds a small project in a temp folder, schedules all five export
tools on it, runs every task twice (model open in Revit; model closed and
opened in the background), then checks the files, the source model, stop,
the due-task runner, the watcher, export -> import, and that both windows
build. Uses its own store (DEE_SCHEDULE_DIR): this PC's real tasks are
never touched. Needs the Dee.extension sign-in (the tools' access check).

Results: schedule_probe_<year>.log next to this file (the PNGs of the two
windows too); the last line is PASS or FAIL.
"""
import io
import json
import os
import shutil
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
EXT = os.environ.get("DEE_EXTENSION_ROOT", os.path.dirname(os.path.dirname(HERE)))
# `pyrevit run` copies nothing, but __file__ may be a temp path
if not os.path.isdir(os.path.join(EXT, "lib")):
    EXT = r"D:\MK\OneDrive Folders\OneDrive\PyRevit Extensions\Dee.extension"
LIB = os.path.join(EXT, "lib")
OUT = os.path.join(EXT, "tools", "checks", "themes")
for p in (LIB, EXT):
    if p not in sys.path:
        sys.path.insert(0, p)
if not os.path.isdir(OUT):
    os.makedirs(OUT)

WORK = tempfile.mkdtemp(prefix="dee_sched_probe_")
os.environ["DEE_SCHEDULE_DIR"] = os.path.join(WORK, "store")

uiapp = __revit__  # noqa: F821
app = uiapp.Application
YEAR = app.VersionNumber
LOG = os.path.join(OUT, "schedule_probe_{0}.log".format(YEAR))
lines, failed = [], []


def log(text):
    lines.append(u"{0}".format(text))
    with io.open(LOG, "w", encoding="utf-8") as f:
        f.write(u"\n".join(lines))
    try:
        print(text)
    except Exception:
        pass


def check(what, ok, detail=""):
    log(u"{0}  {1}  {2}".format("ok  " if ok else "FAIL", what, detail))
    if not ok:
        failed.append(what)


def step(name, fn):
    try:
        return fn()
    except Exception as e:
        check(name, False, u"{0}\n{1}".format(e, traceback.format_exc()))


import clr  # noqa: E402
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
clr.AddReference("WindowsBase")
from System import DateTime  # noqa: E402
from System.IO import FileStream, FileMode  # noqa: E402
from System.Windows import Size, Rect  # noqa: E402
from System.Windows.Media import PixelFormats  # noqa: E402
from System.Windows.Media.Imaging import (  # noqa: E402
    RenderTargetBitmap, PngBitmapEncoder, BitmapFrame)
from Autodesk.Revit.DB import (  # noqa: E402
    Transaction, UnitSystem, ViewSheet, ElementId, View3D, ViewFamilyType, ViewFamily,
    FilteredElementCollector, Wall, Line, XYZ, Level, ViewSchedule, BuiltInCategory,
    SaveAsOptions, ScheduleFieldType)


def render(window, w, h, path):
    root = window.Content
    root.Measure(Size(w, h))
    root.Arrange(Rect(0, 0, w, h))
    root.UpdateLayout()
    bmp = RenderTargetBitmap(int(w), int(h), 96, 96, PixelFormats.Pbgra32)
    bmp.Render(root)
    enc = PngBitmapEncoder()
    enc.Frames.Add(BitmapFrame.Create(bmp))
    fs = FileStream(path, FileMode.Create)
    try:
        enc.Save(fs)
    finally:
        fs.Close()


def build_model():
    doc = app.NewProjectDocument(UnitSystem.Metric)
    t = Transaction(doc, "probe")
    t.Start()
    level = FilteredElementCollector(doc).OfClass(Level).FirstElement() or Level.Create(doc, 0.0)
    for i in range(3):
        Wall.Create(doc, Line.CreateBound(XYZ(0, i * 10, 0), XYZ(20, i * 10, 0)), level.Id, False)
    vft = [v for v in FilteredElementCollector(doc).OfClass(ViewFamilyType)
           if v.ViewFamily == ViewFamily.ThreeDimensional][0]
    view = View3D.CreateIsometric(doc, vft.Id)
    view.Name = "NAVIS Probe"
    for n in (1, 2):
        sheet = ViewSheet.Create(doc, ElementId.InvalidElementId)
        sheet.SheetNumber = "P10{0}".format(n)
        sheet.Name = "Probe {0}".format(n)
    sched = ViewSchedule.CreateSchedule(doc, ElementId(BuiltInCategory.OST_Walls))
    sched.Name = "Probe Walls"
    for field in list(sched.Definition.GetSchedulableFields())[:4]:
        try:
            sched.Definition.AddField(field)
        except Exception:
            pass
    t.Commit()
    path = os.path.join(WORK, "Probe Model.rvt")
    doc.SaveAs(path, SaveAsOptions())
    return doc, path


def files(folder):
    found = []
    for root, _dirs, names in os.walk(folder):
        found += [os.path.join(root, n) for n in names if os.path.getsize(os.path.join(root, n)) > 0]
    return found


def main():
    log("Revit {0}  {1}".format(YEAR, WORK))
    import dee_schedule_core as core
    import dee_schedule_engine as engine
    import dee_schedule_ui as ui

    for tool in core.TOOLS:
        def load(tool=tool):
            space = engine.load_runner(uiapp, tool)
            check("load " + tool, callable(space["scheduled_run"]))
            return space
        step("load " + tool, load)

    doc, path = build_model()
    src = engine.source_from_doc(doc)
    check("source", src["kind"] == "file" and src["path"] == path, src)
    check("find_open", engine.find_open(app, src) is not None)

    sheets = [[s.UniqueId, s.SheetNumber] for s in FilteredElementCollector(doc).OfClass(ViewSheet)]
    view = [v for v in FilteredElementCollector(doc).OfClass(View3D)
            if not v.IsTemplate and v.Name == "NAVIS Probe"][0]
    sched = [s for s in FilteredElementCollector(doc).OfClass(ViewSchedule)
             if s.Name == "Probe Walls"][0]
    nwc = engine.load_runner(uiapp, "DeeNWCs")
    config = json.loads(json.dumps(nwc["_DEFAULT_SETTINGS"]))
    import dee_3d_export_service as d3
    _l, detail, lod = d3.QUALITY_PRESETS[0]

    payloads = {
        "DeePrinter": {
            "sheets": sheets + [["not-a-real-id", "Gone"]],
            "dicprj": {"folder_hierarchy": [{"name": "Sheet Name", "enabled": True}]},
            "dicdlg": {"pdfexport": True, "combinedpdf": True, "dwgexport": True}},
        "DeeNWCs": {"config": config},
        "Dee3D": {
            "view": view.UniqueId, "view_name": "NAVIS Probe", "model_name": "Probe",
            "file_name": "probe.html", "all_categories": True,
            "options": {"detail": detail, "lod": lod, "view_mode": d3.VIEW_MODES[0][1],
                        "categories": [], "include_links": True, "include_params": True,
                        "include_filters": True, "max_triangles": 100000}},
        "DeeScheduleXL": {"schedules": [[sched.UniqueId, "Probe Walls"]],
                          "bidirectional": True, "file_name": "walls.xlsx"},
        "DeeTransmit": {"options": {"sheets": "all", "views": "none", "legends": "none",
                                    "schedules": "all", "parameters": "none"}},
    }
    expect = {"DeePrinter": [".pdf"], "DeeNWCs": [".nwc"], "Dee3D": [".html"],
              "DeeScheduleXL": [".xlsx"], "DeeTransmit": [".rvt"]}
    daily = {"type": "daily", "start": "2020-01-01 03:00:00"}
    ids = {}
    for tool, payload in payloads.items():
        # as it will come back from the file
        payload = json.loads(json.dumps(payload))
        task = core.add_task(core.new_task(
            tool, tool + " probe", daily, [src], payload,
            output=os.path.join(WORK, "out", tool), dated_folder=False,
            revit_year=YEAR, summary="probe"))
        ids[tool] = task["id"]

    def run_all(label):
        for tool in core.TOOLS:
            folder = os.path.join(WORK, "out", tool)
            shutil.rmtree(folder, ignore_errors=True)
            t0 = time.time()
            status, message = engine.run_task(uiapp, ids[tool], manual=True)
            got = files(folder)
            names = [os.path.relpath(g, folder) for g in got]
            good = status in ("ok", "partial") and all(
                any(n.lower().endswith(e) for n in names) for e in expect[tool])
            if tool == "DeeNWCs" and not nwc["_has_exporter"]():
                good = status == "failed" and "exporter" in message
            check(u"{0}: {1}".format(label, tool), good, u"{0} - {1}  {2}  {3:.1f}s".format(
                status, message, names, time.time() - t0))
            if not good or status != "ok":
                log_path = core.last_log(ids[tool])
                if log_path:
                    log(io.open(log_path, encoding="utf-8").read())

    def buttons():
        """Every tool window builds with its Schedule button, and what the
        button hands over survives being written to the store."""
        import deew_settings
        panel = os.path.join(EXT, "DeePack.tab", "SheetsExport.panel")
        calls = []

        def capture(tool, _uiapp, what, payload, **kw):
            calls.append((tool, json.loads(json.dumps(payload)), kw))
        kept = (deew_settings.save, ui.schedule, ui.schedule_doc)
        deew_settings.save = lambda *a: True       # not this user's settings
        ui.schedule = ui.schedule_doc = capture
        try:
            sp = engine.load_runner(uiapp, "DeePrinter")
            rows = [sp["SheetOption"](s)
                    for s in FilteredElementCollector(doc).OfClass(ViewSheet)]
            for r in rows:
                r.state = True
            win = sp["DeePrinterWindow"](sp["_XAML_FILE"], rows, doc)
            win.chbox_pdfexport.IsChecked = True
            win.schedule_click(None, None)
            check("DeePrinter button", len(calls[-1][1]["sheets"]) == 2, calls[-1][2]["summary"])
            win.Close()

            sp = engine.load_runner(uiapp, "Dee3D")
            win = sp["Dee3DWindow"](sp["_XAML_FILE"], doc, [sp["ViewRow"](view)])
            win.schedule_click(None, None)
            check("Dee3D button", calls[-1][0] == "Dee3D" and calls[-1][1]["options"]["categories"],
                  calls[-1][2]["summary"])
            win.Close()

            sp = engine.load_runner(uiapp, "DeeScheduleXL")
            win = sp["DeeScheduleXLWindow"](sp["_XAML_FILE"], doc)
            win.schedules_select_all_click(None, None)
            win.schedule_click(None, None)
            check("DeeScheduleXL button", calls[-1][1]["schedules"], calls[-1][2]["summary"])
            win.Close()

            sp = engine.load_runner(uiapp, "DeeTransmit")
            win = sp["OptionsWindow"](sp["_XAML_FILE"], "probe")
            win.cad_cb.IsChecked = True
            win.schedule_click(None, None)
            check("DeeTransmit window", win.schedule and win.confirmed and win.options["cad"])
            sp["_schedule"](uiapp, [sp["Target"]("Probe Model", doc, False)], win.options)
            check("DeeTransmit button", calls[-1][0] == "DeeTransmit", calls[-1][2]["summary"])

            sp = engine.load_runner(uiapp, "DeeNWCs")
            sp["_save_settings"] = lambda s: None
            sp["_pick_local_documents"] = lambda u: [doc]
            win = sp["DeeNWCsOptionsWindow"](sp["_XAML_FILE"])
            win.source_local_rb.IsChecked = True
            win.schedule_click(None, None)
            check("DeeNWCs window", win.result == "schedule", win.status_tb.Text)
            sp["_schedule"](uiapp, win.config)
            check("DeeNWCs button", calls[-1][0] == "DeeNWCs", calls[-1][2]["summary"])
        finally:
            deew_settings.save, ui.schedule, ui.schedule_doc = kept
    step("buttons", buttons)

    run_all("open model")
    check("open model still open", engine.find_open(app, src) is not None)
    check("open model untouched", len(list(
        FilteredElementCollector(doc).OfClass(ViewSheet))) == 2 and not doc.IsModified)

    doc.Close(False)
    before_stat = os.stat(path).st_mtime
    run_all("closed model")
    check("nothing left open", engine.find_open(app, src) is None
          and not [d for d in app.Documents if d.PathName and WORK.lower() in d.PathName.lower()],
          [d.Title for d in app.Documents])
    check("source file untouched", os.stat(path).st_mtime == before_stat)

    # due runner
    tid = ids["DeeScheduleXL"]

    def due(data):
        core.find(data, tid)["next_run"] = core.fmt(core.now())
        for other in data["tasks"]:
            if other["id"] != tid:
                other["state"] = core.PAUSED
    core.mutate(due)
    runs = core.get_task(tid)["run_count"]
    engine.run_due(uiapp)
    back = core.get_task(tid)
    check("due task ran once and moved on", back["run_count"] == runs + 1
          and core.parse(back["next_run"]) > core.now(), back["next_run"])
    engine.run_due(uiapp)
    check("not run again", core.get_task(tid)["run_count"] == runs + 1)

    # lock / stop / wrong year
    lock = core.RunLock(tid)
    check("lock file", core.is_running(tid))
    check("second run refused", engine.run_task(uiapp, tid, manual=True)[0] == "skipped")
    lock.release()
    check("lock gone", not core.is_running(tid))
    core.update_task(tid, revit_year="1999")
    check("other Revit version refused", engine.run_task(uiapp, tid, manual=True)[0] == "skipped")
    core.update_task(tid, revit_year=YEAR, sources=[{"kind": "file", "path": r"Q:\no.rvt", "label": "x"}])
    status, message = engine.run_task(uiapp, tid, manual=True)
    check("missing model fails cleanly", status == "failed" and "not found" in message, message)
    core.update_task(tid, sources=[src])

    # a run that is told to stop part-way
    class StopBar(object):
        title = ""
        cancelled = False
        n = 0

        def update_progress(self, i, total):
            StopBar.n += 1
            if StopBar.n >= 2:
                core.request_stop(ids["DeePrinter"])

        def close(self):
            pass
    import dee_ui
    original = dee_ui.open_progress
    dee_ui.open_progress = lambda *a, **k: StopBar()
    try:
        status, message = engine.run_task(uiapp, ids["DeePrinter"], manual=True)
    finally:
        dee_ui.open_progress = original
    check("stop cancels a run", status == "cancelled", message)
    check("stop flag cleared", not core.stop_requested(ids["DeePrinter"]))

    # moving to another PC
    export = os.path.join(WORK, "export.json")
    core.export_to_file(export, core.tasks())
    os.environ["DEE_SCHEDULE_DIR"] = os.path.join(WORK, "store2")
    incoming, settings, problems = core.read_export(export)
    counts = core.import_tasks(incoming, start=True, new_settings=settings)
    check("import on another PC", counts["added"] == 5 and not problems, counts)
    status, message = engine.run_task(uiapp, ids["DeeScheduleXL"], manual=True)
    check("imported task runs", status == "ok", message)
    os.environ["DEE_SCHEDULE_DIR"] = os.path.join(WORK, "store")

    # windows
    def windows():
        xaml = os.path.join(EXT, "DeePack.tab", "SheetsExport.panel",
                            "DeeSchedule.pushbutton", "ui.xaml")
        win = ui.ManagerWindow(xaml, uiapp)
        win.tasks_grid.SelectedIndex = 0
        win._show_details()
        render(win, 1280, 760, os.path.join(OUT, "schedule_manager_{0}.png".format(YEAR)))
        check("manager rows", len(list(win.tasks_grid.ItemsSource)) >= 5)
        check("manager tools", len(list(win.tools_lb.ItemsSource)) >= 6,
              [r.label for r in win.tools_lb.ItemsSource])
        win.tasks_grid.SelectedItems.Clear()
        win.tasks_grid.SelectedItems.Add(
            [r for r in win.tasks_grid.ItemsSource if r.id == tid][0])
        win.pause_click(None, None)
        check("Pause button", core.get_task(tid)["state"] == core.PAUSED)
        win.resume_click(None, None)
        check("Resume button", core.get_task(tid)["state"] == core.ACTIVE)
        win.run_click(None, None)
        check("Run now button", core.get_task(tid)["last_status"] == "ok", win.status_tb.Text)
        win.stop_click(None, None)
        check("Stop button", core.get_task(tid)["state"] == core.STOPPED)
        win.enabled_cb.IsChecked = False
        win.settings_changed(None, None)
        check("Scheduler off", not core.settings()["enabled"])
        win.enabled_cb.IsChecked = True
        win.settings_changed(None, None)
        win._timer.Stop()
        win.Close()
    step("manager window", windows)

    def dialogs():
        dlg = ui.TaskDialog(uiapp, "DeePrinter", spec={
            "sources": [src], "payload": payloads["DeePrinter"], "summary": "2 sheet(s) to PDF",
            "name": "Probe", "output": WORK, "note": "",
            "extras": [("all_sheets", "Export every sheet", False, "tip")]})
        kinds = []
        for i in range(5):
            dlg.repeat_cb.SelectedIndex = i
            dlg._sync()
            kinds.append(dlg.preview_tb.Text)
        log(kinds)
        dlg.repeat_cb.SelectedIndex = 3
        dlg.start_dp.SelectedDate = DateTime.Today
        dlg._sync()
        render(dlg, 900, 800, os.path.join(OUT, "schedule_dialog_{0}.png".format(YEAR)))
        before = len(core.tasks())
        dlg.ok_click(None, None)
        check("dialog creates a task", len(core.tasks()) == before + 1, dlg.status_tb.Text)
        made = dlg.created
        check("dialog payload", made and made["payload"]["all_sheets"] is False
              and made["trigger"]["days"] == [0], made and made["trigger"])
        ed = ui.TaskDialog(uiapp, "DeePrinter", task=core.get_task(made["id"]))
        check("edit loads trigger", ed._read_trigger() == made["trigger"], ed._read_trigger())
        ed.name_tb.Text = "Renamed"
        ed.time_tb.Text = "99:99"
        ed.ok_click(None, None)
        check("bad time refused", "time" in ed.status_tb.Text.lower(), ed.status_tb.Text)
        ed.time_tb.Text = "22:15"
        ed.ok_click(None, None)
        check("edit saved", core.get_task(made["id"])["name"] == "Renamed")
    step("task dialog", dialogs)

    def watcher():
        from System.Windows.Threading import DispatcherTimer
        w = engine.start_watching(uiapp)
        check("watcher", w.timer.IsEnabled)
        again = engine.start_watching(uiapp)
        check("reload stops the old watcher", not w.timer.IsEnabled and again.timer.IsEnabled)

        def make_due(data):
            task = core.find(data, tid)
            task["state"] = core.ACTIVE
            task["next_run"] = core.fmt(core.now())
            data["settings"]["warn_seconds"] = 0
        core.mutate(make_due)
        again._check()
        check("watcher queues the due task", tid in again.event_handler.ready)
        n = core.get_task(tid)["run_count"]
        again.event_handler.Execute(uiapp)
        check("event runs it", core.get_task(tid)["run_count"] == n + 1)
        again.stop()
    step("watcher", watcher)


try:
    main()
except Exception as e:
    check("probe", False, u"{0}\n{1}".format(e, traceback.format_exc()))
shutil.rmtree(WORK, ignore_errors=True)
log("PASS" if not failed else "FAIL: {0}".format(failed))
