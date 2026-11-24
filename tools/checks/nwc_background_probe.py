# -*- coding: utf-8 -*-
"""DeeNWCs background probe - runs the "More than one Revit" export for real,
inside Revit, on two models it makes itself (two background Revits are
started). Run it after touching DeeNWCs' nwc_worker.py or lib/dee_background.py:

    pyrevit run "<abs path>/Dee.extension/tools/checks/nwc_background_probe.py" --revit=2024

Checks: the options window's Run by controls, the error tick box is not cut
off, ACC output settings, and that both background Revits start, take one
model each, load the Navisworks exporter and write an NWC file each (needs
the Navisworks exporter installed for that Revit year).

Result: nwc_background_probe_<year>.log next to this file; last line PASS/FAIL.
"""
import io
import json
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
    from Autodesk.Revit.DB import (UnitSystem, SaveAsOptions, Transaction, FilteredElementCollector,
                                   Level, Wall, Line, XYZ)

    sp = engine.load_runner(uiapp, "DeeNWCs")
    sp["_save_settings"] = lambda s: None
    # Not this user's saved settings: the defaults.
    sp["_load_settings"] = lambda: json.loads(json.dumps(sp["_DEFAULT_SETTINGS"]))
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

    win = sp["DeeNWCsOptionsWindow"](sp["_XAML_FILE"])
    win.view_contains_tb.Text = u"NAVIS; COORDINATION"
    win.output_acc_rb.IsChecked = True
    check("View name contains box takes several lines", win.view_contains_tb.AcceptsReturn is True)
    win._finish("run")
    check("window keeps several texts", win.config.get("view_contains") == u"NAVIS; COORDINATION",
          win.config.get("view_contains") if win.config else win.status_tb.Text)
    win = sp["DeeNWCsOptionsWindow"](sp["_XAML_FILE"])
    win.view_contains_tb.Text = u" ; , "
    win.output_acc_rb.IsChecked = True
    win._finish("run")
    check("a box with only separators is refused", not win.config and "at least one text" in win.status_tb.Text,
          win.status_tb.Text)

    win = sp["DeeNWCsOptionsWindow"](sp["_XAML_FILE"])
    check("'export all matching views' is ticked by default", win.view_all_cb.IsChecked is True)
    win.view_all_cb.IsChecked = False
    win.output_acc_rb.IsChecked = True
    win._finish("run")
    check("window keeps the option off", bool(win.config) and win.config.get("view_all") is False,
          win.config.get("view_all") if win.config else win.status_tb.Text)

    docs = []
    for n in (1, 2):
        doc = app.NewProjectDocument(UnitSystem.Metric)
        if n == 1:      # model 1 has something to export, model 2 stays empty
            t = Transaction(doc, "probe")
            t.Start()
            level = FilteredElementCollector(doc).OfClass(Level).FirstElement()
            for i in range(3):
                Wall.Create(doc, Line.CreateBound(XYZ(0, i * 10, 0), XYZ(20, i * 10, 0)), level.Id, False)
            from Autodesk.Revit.DB import ViewFamilyType, ViewFamily, View3D
            vft = [x for x in FilteredElementCollector(doc).OfClass(ViewFamilyType)
                   if x.ViewFamily == ViewFamily.ThreeDimensional][0]
            for view_name in (u"Navis Test", u"Coord Test"):     # for the "View name contains" checks
                View3D.CreateIsometric(doc, vft.Id).Name = view_name
            t.Commit()
        doc.SaveAs(os.path.join(WORK, "Model {0}.rvt".format(n)), SaveAsOptions())
        docs.append(doc)
    # several texts in "View name contains": any of them matches
    check("view texts are split on ; , | and new lines",
          sp["_view_terms"](u"Navis; coord,NWC|x\r\ny;; navis ") == [u"Navis", u"coord", u"NWC", u"x", u"y"],
          sp["_view_terms"](u"Navis; coord,NWC|x\r\ny;; navis "))
    names = lambda text: sorted(v.Name for v in sp["_find_matching_views"](docs[0], text))
    check("one text matches its views", names(u"navis") == [u"Navis Test"], names(u"navis"))
    check("another text matches other views", names(u"COORD") == [u"Coord Test"], names(u"COORD"))
    # any of the texts matches, and a view matched by two of them is not doubled
    check("several texts match any of them",
          names(u"zzz; navis\nCOORD") == [u"Coord Test", u"Navis Test"], names(u"zzz; navis\nCOORD"))
    check("a view matched by two texts is listed once",
          names(u"navis; test; Navis Test") == [u"Coord Test", u"Navis Test"],
          names(u"navis; test; Navis Test"))
    check("no text matches nothing", names(u"zzz; yyy") == [] and names(u" ; ") == [], names(u"zzz; yyy"))
    check("the label lists every text", sp["_terms_label"](u"A; b") == u"'A' or 'b'", sp["_terms_label"](u"A; b"))
    # the "export all matching views" option and same-name files, with a stand-in exporter
    check("_pick_views: all of them", [v for v in sp["_pick_views"]([1, 2, 3], True)[0]] == [1, 2, 3])
    check("_pick_views: only the first", sp["_pick_views"]([1, 2, 3], False) == ([1], [2, 3]))
    used = set()
    first = sp["_free_output_name"](u"F", u"Level 1", u"CeilingPlan", used)
    same = sp["_free_output_name"](u"F", u"level 1", u"CeilingPlan", used)
    third = sp["_free_output_name"](u"F", u"Level 1", u"CeilingPlan", used)
    other = sp["_free_output_name"](u"G", u"Level 1", u"CeilingPlan", used)
    check("same file name twice gets the view type, then a number",
          (first, same, third, other) == (u"Level 1", u"level 1 (CeilingPlan)", u"Level 1 (CeilingPlan) 2", u"Level 1"),
          (first, same, third, other))
    sent = []
    real_export = sp["_export_view_to_nwc"]

    def fake_export(doc, view, folder, name, cfg, skipped, uploader=None):
        sent.append((view.Name, name))
        return True, u"fake"
    sp["_export_view_to_nwc"] = fake_export
    cfg = json.loads(json.dumps(sp["_DEFAULT_SETTINGS"]))
    cfg["view_contains"] = u"coord; navis"
    rows = sp["_export_for_document"](docs[0], u"Model 1", cfg, WORK, set(), None)
    check("all matching views are exported, first-typed text first",
          sent == [(u"Coord Test", u"Model 1_Coord Test"), (u"Navis Test", u"Model 1_Navis Test")]
          and len(rows) == 2 and "not exported" not in rows[0][3], (sent, rows))
    del sent[:]
    cfg["view_all"] = False
    rows = sp["_export_for_document"](docs[0], u"Model 1", cfg, WORK, set(), None)
    check("option off: only the first one, the rest named in the result",
          sent == [(u"Coord Test", u"Model 1_Coord Test")] and len(rows) == 1
          and u"1 other matching view(s) not exported" in rows[0][3] and u"Navis Test" in rows[0][3], (sent, rows))
    del sent[:]
    cfg["view_all"] = True
    cfg["naming"]["output_template"] = u"{RevitFileName}"
    sp["_export_for_document"](docs[0], u"Model 1", cfg, WORK, set(), None)
    check("a naming template without the view name does not overwrite",
          sent == [(u"Coord Test", u"Model 1"), (u"Navis Test", u"Model 1 (ThreeD)")], sent)
    sp["_export_view_to_nwc"] = real_export
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
    # This probe's own Revit is a `pyrevit run` session (no exporter), the
    # workers get the exporter handed over - so a real NWC file must appear.
    # The empty model makes the exporter show its native "No suitable
    # geometry found" box: it must be answered (no hang) and reported.
    check("model with walls exported", rows[0][0] is True, rows[0])
    check("empty model: exported, or skipped with Navisworks' own words",
          rows[1][0] is True or (rows[1][0] is None and "Navisworks said" in (rows[1][3] or "")),
          rows[1])
    check("NWC file written", any(f.endswith(".nwc") for f in os.listdir(folder)), os.listdir(folder))
    for doc in docs:
        doc.Close(False)


try:
    main()
except Exception:
    check("crash", False, traceback.format_exc())
shutil.rmtree(WORK, ignore_errors=True)
log("FAIL" if failed else "PASS")
