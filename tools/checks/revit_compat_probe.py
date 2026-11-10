# -*- coding: utf-8 -*-
"""Dee.extension compatibility probe - run inside Revit via `pyrevit run`.

    pyrevit run "<abs path>/Dee.extension/tools/checks/revit_compat_probe.py" --revit=2024
    pyrevit run "<abs path>/Dee.extension/tools/checks/revit_compat_probe.py" --revit=2026

The path must be absolute - pyrevit run resolves a relative one against a
temp working folder and then exits with an empty execution log.

Each call starts a fresh headless Revit of that year, runs this file with the
engine pyRevit attaches to it, prints the report to the console and exits.
Last verified 2026-11-10: PASS on 2024.3.4 (.NET Framework 4.8) and 2026.4
(.NET 8); both on IronPython 2.7.12. Note ElementId.IntegerValue is absent
in 2026 - every use in this codebase is a guarded fallback behind .Value.

Imports every lib/*.py module and compiles every .py under DeePack.tab with the
engine pyRevit actually uses for this Revit version, then exercises the Revit
API members the extension depends on that changed between releases. Nothing
is executed from the tools themselves. The report is printed (pyrevit run
captures it) and also written next to the script.
"""
import os
import sys
import io
import traceback
import time

HERE = os.path.dirname(os.path.abspath(__file__))
EXT = os.environ.get("DEE_EXTENSION_ROOT",
                     r"D:\MK\OneDrive Folders\OneDrive\PyRevit Extensions\Dee.extension")
LIB = os.path.join(EXT, "lib")
TAB = os.path.join(EXT, "DeePack.tab")

app = __revit__.Application  # noqa: F821 - injected by pyRevit
ver = app.VersionNumber
OUT = os.path.join(HERE, "compat_result_%s.txt" % ver)
lines = []


def log(s):
    lines.append(s)


log("Revit %s build %s" % (ver, app.VersionBuild))
log("Engine: %s  sys.version=%s" % (
    getattr(sys, "implementation", None) and sys.implementation.name or "ironpython",
    sys.version.replace("\n", " ")))
try:
    import System
    log("CLR: %s" % System.Environment.Version)
    log("Runtime: %s" % System.Runtime.InteropServices.RuntimeInformation.FrameworkDescription)
except Exception as e:
    log("CLR info failed: %s" % e)

for p in (LIB, EXT):
    if p not in sys.path:
        sys.path.insert(0, p)

# ---- 1. import every lib module ------------------------------------------
lib_fail = []
lib_ok = 0
t0 = time.time()
for name in sorted(os.listdir(LIB)):
    if not name.endswith(".py") or name.startswith("."):
        continue
    mod = name[:-3]
    try:
        __import__(mod)
        lib_ok += 1
    except Exception:
        lib_fail.append((mod, traceback.format_exc().strip().splitlines()[-1]))
log("")
log("lib imports: %d ok, %d failed (%.1fs)" % (lib_ok, len(lib_fail), time.time() - t0))
for m, e in lib_fail:
    log("  FAIL %s: %s" % (m, e))

# ---- 2. compile every tool script -----------------------------------------
comp_fail = []
comp_ok = 0
for dp, dn, fn in os.walk(TAB):
    dn[:] = [d for d in dn if d != "__pycache__"]
    for f in fn:
        if f.endswith(".py"):
            path = os.path.join(dp, f)
            try:
                src = io.open(path, encoding="utf-8").read()
                compile(src, path, "exec")
                comp_ok += 1
            except Exception as e:
                comp_fail.append((os.path.relpath(path, TAB), str(e).splitlines()[-1]))
log("")
log("tool scripts compiled: %d ok, %d failed" % (comp_ok, len(comp_fail)))
for m, e in comp_fail:
    log("  FAIL %s: %s" % (m, e))

# ---- 3. DeeLazy modules import (they need their own folder on sys.path) ---
lazy = os.path.join(TAB, "Lazy.panel", "DeeLazy.pushbutton")
sys.path.insert(0, lazy)
lazy_fail = []
try:
    import modules  # noqa
    log("")
    log("DeeLazy modules registered: %d" % len(modules.REGISTERED_MODULES))
except Exception:
    lazy_fail.append(traceback.format_exc().strip().splitlines()[-1])
    log("")
    log("DeeLazy modules import FAILED: %s" % lazy_fail[-1])

# ---- 4. Revit API members the extension relies on -------------------------
from Autodesk.Revit import DB  # noqa
api_checks = [
    ("ElementId.IntegerValue (obsolete 2024+, used 40x)", lambda: DB.ElementId(DB.BuiltInCategory.OST_Walls).IntegerValue),
    ("ElementId.Value (2024+)", lambda: DB.ElementId(DB.BuiltInCategory.OST_Walls).Value),
    ("ElementId(Int64) ctor", lambda: DB.ElementId(System.Int64(5))),
    ("Toposolid class (2024+)", lambda: DB.Toposolid),
    ("Wall.CreateProfileSketch (2022+)", lambda: DB.Wall.CreateProfileSketch),
    ("SketchEditScope", lambda: DB.SketchEditScope),
    ("DocumentPreviewSettings", lambda: DB.DocumentPreviewSettings),
    ("PDFExportOptions", lambda: DB.PDFExportOptions),
    ("NavisworksExportOptions", lambda: DB.NavisworksExportOptions),
    ("WorksharingUtils.GetCheckoutStatus", lambda: DB.WorksharingUtils.GetCheckoutStatus),
    ("ModelPathUtils.ConvertCloudGUIDsToCloudPath", lambda: DB.ModelPathUtils.ConvertCloudGUIDsToCloudPath),
    ("TransmissionData", lambda: DB.TransmissionData),
    ("JoinGeometryUtils", lambda: DB.JoinGeometryUtils),
    ("SpatialElementGeometryCalculator", lambda: DB.SpatialElementGeometryCalculator),
    ("RevitLinkType.LoadFrom", lambda: DB.RevitLinkType.LoadFrom),
    ("UnitUtils.ConvertFromInternalUnits(ForgeTypeId)", lambda: DB.UnitUtils.ConvertFromInternalUnits(1.0, DB.UnitTypeId.Millimeters)),
]
log("")
log("Revit API members:")
for label, fn in api_checks:
    try:
        fn()
        log("  ok   %s" % label)
    except Exception as e:
        log("  MISSING %s: %s" % (label, str(e).splitlines()[0][:120]))

# ---- 5. .NET assemblies the tools AddReference ----------------------------
import clr
log("")
log(".NET assemblies:")
for asm in ("PresentationFramework", "PresentationCore", "WindowsBase", "System.Windows.Forms",
            "System.Drawing", "System.Net.Http", "UIAutomationClient", "UIAutomationTypes", "AdWindows", "System"):
    try:
        clr.AddReference(asm)
        log("  ok   %s" % asm)
    except Exception as e:
        log("  FAIL %s: %s" % (asm, str(e).splitlines()[0][:120]))

ok = not lib_fail and not comp_fail and not lazy_fail
log("")
log("RESULT: %s" % ("PASS" if ok else "FAIL"))
io.open(OUT, "w", encoding="utf-8").write(u"\n".join(lines))
print(u"\n".join(lines))
