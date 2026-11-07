# -*- coding: utf-8 -*-
"""
DeeLazy - Remove Links: background worker.

Runs INSIDE A SECOND REVIT started by the pyRevit CLI
(`pyrevit run <stub.py> --revit=<year>`). The Remove Links window writes
a job file (which ACC models, which links, remove or unload) plus a tiny
stub that imports this module and calls main(). This Revit opens each
cloud model, removes / unloads the links, synchronizes it back to ACC
and closes it, appending to a log file the window tails live and to a
results file the window reads when the job ends.

Why a second Revit: the user's own session stays free while the batch
runs, and a crash or a stuck dialog in here never touches their work.
The Autodesk sign-in is shared by every Revit on the machine, so no
extra login is needed.

Job file (JSON):
    {"action": "remove" | "unload",
     "include_cad": bool,
     "region": "US",
     "sync_comment": "...",
     "hosts": [{"key": item_id, "name": "A.rvt",
                "project_guid": "...", "model_guid": "...",
                "remove_all": bool,
                "rows": [{"type_id": "123", "name": "B.rvt", "kind": "Revit"}]}]}

Results file (JSON): {host_key: {"opened": bool, "detail": "...",
                                 "saved": bool, "save_detail": "...",
                                 "rows": {type_id_or_name: status}}}
"""
import os
import io
import sys
import json
import time
import datetime
import traceback

# module-level so a stub that only sets sys.path can import us
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

LOG_DONE_MARKER = "=== DONE ==="
LOG_FAIL_MARKER = "=== FAILED ==="


class _Log(object):
    def __init__(self, path):
        self.path = path

    def __call__(self, message):
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        line = u"[{0}] {1}\n".format(stamp, message)
        for _ in range(5):          # the window may be reading the file
            try:
                with io.open(self.path, "a", encoding="utf-8") as f:
                    f.write(line)
                return
            except Exception:
                time.sleep(0.2)


def _uiapp_of(handle):
    """`__revit__` is a UIApplication in the runner; tolerate an
    Application or a UIControlledApplication anyway."""
    if handle is None:
        return None, None
    app = getattr(handle, "Application", None)
    if app is not None and hasattr(handle, "OpenAndActivateDocument"):
        return handle, app
    if hasattr(handle, "OpenDocumentFile"):
        return None, handle
    return None, app


def _open(uiapp, app, model_path):
    """Headless open first (clean close afterwards); Revit's own
    OpenAndActivateDocument route when that is refused."""
    from Autodesk.Revit.DB import (OpenOptions, DetachFromCentralOption, WorksetConfiguration,
                                   WorksetConfigurationOption)
    errors = []
    for route in ("headless", "activate"):
        try:
            oo = OpenOptions()
            oo.DetachFromCentralOption = DetachFromCentralOption.DoNotDetach
            try:
                oo.SetOpenWorksetsConfiguration(WorksetConfiguration(WorksetConfigurationOption.OpenAllWorksets))
            except Exception:
                pass
            if route == "headless":
                return app.OpenDocumentFile(model_path, oo), route
            if uiapp is None:
                raise Exception("no UIApplication for OpenAndActivateDocument")
            uidoc = uiapp.OpenAndActivateDocument(model_path, oo, False)
            return uidoc.Document, route
        except Exception as e:
            errors.append("{0}: {1}".format(route, str(e).split("\n")[0]))
    return None, " | ".join(errors)


def _write_results(path, results):
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(results, indent=1))
    if os.path.exists(path):
        os.remove(path)
    os.rename(tmp, path)


def _still_names(doc, unloaded_count_as_gone):
    from Autodesk.Revit.DB import FilteredElementCollector, RevitLinkType, CADLinkType
    import dee_remove_links as rl
    ids, names = set(), set()
    for lt in FilteredElementCollector(doc).OfClass(RevitLinkType):
        if unloaded_count_as_gone:
            try:
                if rl._enum_word(lt.GetLinkedFileStatus()) == "Unloaded":
                    continue
            except Exception:
                pass
        ids.add(rl._eid(lt.Id))
        names.add((rl._name(lt) or "").lower())
    cad_ids = set(rl._eid(ct.Id) for ct in FilteredElementCollector(doc).OfClass(CADLinkType))
    return ids, names, cad_ids


def _process_host(host, job, doc, log):
    """Removes / unloads the host's rows in the open doc. Returns
    (changed_count, {row_key: status})."""
    import dee_remove_links as rl
    action = job.get("action", "remove")
    rows = host.get("rows") or []
    cad_rows = [r for r in rows if r.get("kind") in rl._CAD_KINDS]
    rvt_rows = [r for r in rows if r.get("kind") not in rl._CAD_KINDS]
    statuses = {}
    changed = 0

    if cad_rows:
        if action == "unload":
            for r in cad_rows:
                statuses[_row_key(r)] = "Unload n/a (CAD)"
            log("  {0} CAD row(s) skipped - CAD links cannot be unloaded, only removed".format(len(cad_rows)))
        else:
            done, targeted, reasons = rl.remove_cad_links(doc, [r["type_id"] for r in cad_rows if r.get("type_id")])
            _ids, _names, still_cad = _still_names(doc, False)
            for r in cad_rows:
                statuses[_row_key(r)] = rl.ST_REMOVED if r.get("type_id") not in still_cad else "Remove failed"
            for nm, why in sorted(reasons.items()):
                log(u"  ! {0}: {1}".format(nm, why))
            log("  removed {0} of {1} CAD link/import type(s)".format(done, targeted))
            changed += done

    if rvt_rows:
        ids = [r["type_id"] for r in rvt_rows if r.get("type_id")]
        names = [r.get("name") for r in rvt_rows]
        remove_all = bool(host.get("remove_all"))
        if action == "unload":
            done, targeted, missing, reasons = rl.unload_links(doc, ids, names, remove_all=remove_all)
            ok_status, fail_status = rl.ST_UNLOADED, "Unload failed"
            still, still_names, _cad = _still_names(doc, True)
        else:
            done, targeted, missing, reasons = rl.remove_links(doc, ids, names, remove_all=remove_all)
            ok_status, fail_status = rl.ST_REMOVED, "Remove failed"
            still, still_names, _cad = _still_names(doc, False)
        for r in rvt_rows:
            tid = r.get("type_id")
            gone = (tid not in still) if tid else ((r.get("name") or "").lower() not in still_names)
            statuses[_row_key(r)] = ok_status if gone else fail_status
        if missing:
            log("  not found in the model: " + ", ".join(missing))
        for nm, why in sorted(reasons.items()):
            log(u"  ! {0}: {1}".format(nm, why))
        log("  {0} {1} of {2} Revit link type(s)".format(
            "unloaded" if action == "unload" else "removed", done, targeted))
        changed += done
    return changed, statuses


def _row_key(row):
    return row.get("type_id") or (row.get("name") or "")


def main(revit_handle, job_file):
    folder = os.path.dirname(job_file)
    log = _Log(os.path.join(folder, "log.txt"))
    results_file = os.path.join(folder, "results.json")
    results = {}
    try:
        with io.open(job_file, "r", encoding="utf-8") as f:
            job = json.load(f)
        uiapp, app = _uiapp_of(revit_handle)
        if app is None:
            raise Exception("no Revit Application handle (__revit__ missing)")
        log("Worker started in Revit {0} (pid {1}).".format(app.VersionNumber, os.getpid()))

        import System
        from Autodesk.Revit.DB import ModelPathUtils
        import dee_remove_links as rl
        import deew_document_manager as docmgr
        import acc_file_browser as afb
        rl._SYNC_COMMENT = job.get("sync_comment") or rl._SYNC_COMMENT

        dialog_log = []
        handler = afb.make_dialog_handler(dialog_log)
        if uiapp is not None:
            try:
                uiapp.DialogBoxShowing += handler
            except Exception:
                pass

        hosts = job.get("hosts") or []
        previous = None        # doc opened via the activate route: closable only after the next opens
        stop_file = os.path.join(folder, "stop.txt")
        for i, host in enumerate(hosts):
            if os.path.exists(stop_file):
                log("Stopped by the user - the remaining models were not touched.")
                break
            name = host.get("name") or host.get("key")
            res = {"opened": False, "detail": "", "saved": False, "save_detail": "", "rows": {}}
            results[host["key"]] = res
            log(u"{0} ({1}/{2}):".format(name, i + 1, len(hosts)))
            try:
                model_path = ModelPathUtils.ConvertCloudGUIDsToCloudPath(
                    job["region"], System.Guid(host["project_guid"]), System.Guid(host["model_guid"]))
            except Exception as e:
                res["detail"] = "cloud path: {0}".format(e)
                log(u"  ✗ could not build the cloud path - {0}".format(e))
                _write_results(results_file, results)
                continue
            doc, route = _open(uiapp, app, model_path)
            if doc is None:
                res["detail"] = route
                log(u"  ✗ could not open - " + route)
                _write_results(results_file, results)
                continue
            res["opened"] = True
            res["detail"] = "opened ({0})".format(route)
            log("  opened ({0})".format(route))
            if previous is not None:
                docmgr.close_document(previous, save_modified=False)
                previous = None
            try:
                changed, statuses = _process_host(host, job, doc, log)
                res["rows"] = statuses
                if changed:
                    ok, detail = rl.save_after_remove(doc)
                    res["saved"], res["save_detail"] = bool(ok), detail
                    log(u"  ✓ " + detail if ok else u"  ✗ sync/save failed: " + detail)
                    if not ok:
                        for k, st in list(statuses.items()):
                            if st in rl.ST_DONE:
                                statuses[k] = "Not synced"
                else:
                    res["saved"] = True
                    res["save_detail"] = "nothing changed"
            except Exception as e:
                res["detail"] += " | " + str(e)
                log(u"  ✗ " + str(e))
            finally:
                if route == "activate":
                    previous = doc           # active view's doc - close after the next one opens
                else:
                    docmgr.close_document(doc, save_modified=False)
                rl._release()
            for text, _ok in dialog_log:
                log("  (auto-answered) " + text)
            del dialog_log[:]
            _write_results(results_file, results)
        if previous is not None:
            log("  (the last model stays open in this Revit until it exits)")
        _write_results(results_file, results)
        log(LOG_DONE_MARKER)
    except Exception:
        try:
            _write_results(results_file, results)
        except Exception:
            pass
        log("Worker crashed:\n" + traceback.format_exc())
        log(LOG_FAIL_MARKER)
