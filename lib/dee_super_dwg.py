# -*- coding: utf-8 -*-
"""Super DWG: turns a Revit-exported DWG into ONE self-contained file.

Revit cannot do this itself. A DWG only ever links to raster images (the
files Revit writes beside it - logos, pictures), so a "normal" export
leaves loose image files next to the drawing. The Super DWG step opens the
exported drawing in AutoCAD's headless engine (accoreconsole.exe - no
window, no plugins, no clipboard) and

  * redraws every attached raster image as solid-colour fills inside the
    drawing - same place, size, rotation and layer, neighbouring pixels of
    one colour merged, transparent pixels left empty - and removes the
    image and its link (the picture becomes drawing geometry, so it is no
    longer an editable picture, and it travels with the file everywhere);
  * binds every external reference (xref) into the drawing;
  * saves in the same DWG version the export used.

It needs full AutoCAD (AutoCAD LT has no headless engine) installed on the
PC that runs the export. The AutoCAD work is done by lib/dee_super_dwg.ps1
in a separate PowerShell process; this module finds AutoCAD, hands that
script a batch of drawings in one go (a few drawings per AutoCAD session,
not one session per drawing) and reads back one result per drawing.

No Revit dependency: tools/checks/test_super_dwg.py runs it without Revit.
Works on IronPython 2.7 and CPython.
"""
import json
import os
import re
import subprocess
import tempfile
import time

LIB_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(LIB_DIR, "dee_super_dwg.ps1")

# Safety nets only (a run normally takes seconds): the script has its own,
# tighter limits per AutoCAD session. Seconds in all, plus per drawing.
_BASE_TIMEOUT = 600
_PER_DWG_TIMEOUT = 240


def _kill_tree(proc):
    """Ends PowerShell and the AutoCAD console it started."""
    try:
        subprocess.call(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                        stdout=open(os.devnull, "wb"), stderr=subprocess.STDOUT)
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


def find_autocad():
    """Path of the newest full AutoCAD's accoreconsole.exe, or None. Looks
    for '...\\Autodesk\\AutoCAD <year>\\accoreconsole.exe'; AutoCAD LT is
    not matched on purpose (it has no headless engine)."""
    found = []
    seen = set()
    for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        root = os.environ.get(var)
        if not root:
            continue
        base = os.path.join(root, "Autodesk")
        if base in seen:
            continue
        seen.add(base)
        try:
            names = os.listdir(base)
        except OSError:
            continue
        for n in names:
            m = re.match(r"^AutoCAD (\d{4})$", n)
            exe = os.path.join(base, n, "accoreconsole.exe")
            if m and os.path.isfile(exe):
                found.append((int(m.group(1)), exe))
    return max(found)[1] if found else None


def _hidden_startupinfo():
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        return si
    except Exception:
        return None


def run_batch(jobs, script=None, timeout=None):
    """Runs the Super DWG step on a batch of drawings.

    `jobs` is a list of {"dwg": <exported drawing>, "out": <final path>}.
    Returns one result dict per job, in order:
        {"out", "ok", "embedded", "xrefs_bound", "left_linked", "message"}
    `left_linked` counts images/xrefs that could not be made part of the
    drawing; with any, the saved file still needs its image files.
    Never raises for a failed drawing - that is ok False with the reason
    in `message`. A failure of the whole run (no PowerShell, timeout, the
    script crashed) gives every job ok False with the same reason, so the
    caller can keep each plain DWG instead.
    """
    if not jobs:
        return []
    script = script or SCRIPT
    work = tempfile.mkdtemp(prefix="dee_superdwg_")
    jobs_file = os.path.join(work, "jobs.json")
    result_file = os.path.join(work, "results.json")
    try:
        with open(jobs_file, "w") as f:
            json.dump(jobs, f)

        def _all_failed(why):
            return [{"out": j["out"], "ok": False, "embedded": 0,
                     "xrefs_bound": 0, "left_linked": 0, "message": why}
                    for j in jobs]

        cmd = ["powershell.exe", "-NoProfile", "-NonInteractive",
               "-ExecutionPolicy", "Bypass", "-File", script,
               "-JobsFile", jobs_file, "-ResultFile", result_file]
        limit = timeout or (_BASE_TIMEOUT + _PER_DWG_TIMEOUT * len(jobs))
        # The script prints only a few progress lines (AutoCAD's own console output
        # goes to files it manages), so reading the pipe once the process has ended
        # cannot stall it - and, unlike redirecting to a file, it works on IronPython.
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    startupinfo=_hidden_startupinfo())
        except Exception as e:
            return _all_failed(u"could not start PowerShell: {0}".format(e))
        started = time.time()
        while proc.poll() is None:
            if time.time() - started > limit:
                _kill_tree(proc)
                try:
                    proc.wait()
                except Exception:
                    pass
                return _all_failed(
                    u"AutoCAD did not finish within {0} s - it may still be "
                    u"running; try again".format(int(limit)))
            time.sleep(0.5)
        try:
            console = proc.stdout.read().decode("utf-8", "replace").strip()
        except Exception:
            console = ""

        try:
            # Binary read + explicit decode (IronPython's io.open is not dependable);
            # utf-8-sig because PowerShell writes a BOM unless told not to.
            with open(result_file, "rb") as f:
                results = json.loads(f.read().decode("utf-8-sig"))
        except Exception:
            return _all_failed(u"AutoCAD step produced no result{0}".format(
                u": " + console[-300:] if console else u""))
        by_out = dict((r.get("out"), r) for r in results)
        return [by_out.get(j["out"]) or
                {"out": j["out"], "ok": False, "embedded": 0, "xrefs_bound": 0,
                 "left_linked": 0, "message": u"no result for this drawing"}
                for j in jobs]
    finally:
        try:
            for n in os.listdir(work):
                os.remove(os.path.join(work, n))
            os.rmdir(work)
        except Exception:
            pass
