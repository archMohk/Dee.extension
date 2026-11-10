# -*- coding: utf-8 -*-
"""
DeeLazy - Updater: background worker function.

Runs inside a second Revit through lib/dee_background (job folders,
stub, log, results, stop file - see that module). One call per model:

    process_model(ctx, item) -> {"ok", "stage", "detail", "synced", "sync_detail", "low_disk"}

item = {"key": row key, "name": "A.rvt", "project": "Project name",
        "project_guid": "...", "model_guid": "..."}
ctx.options = {"region": "US", "sync_comment": "...", "audit": bool,
               "min_free_gb": 15, "clean_cache": bool}

A model that cannot be opened is reported (ok=False, stage="open") and
the worker moves on - a bad file never stops the batch.

Disk space (live run 2026-11-10: 3 Revits x ~45 models filled C: and
every later open failed with "Insufficient disk space" and then the
misleading "The central server could not be reached"):
  - every cloud model opened leaves a full local copy under
    %LOCALAPPDATA%\\Autodesk\\Revit\\Autodesk Revit <ver>\\CollaborationCache
    \\<user>\\<project guid>\\ : <model>.rvt, <model>_backup\\ and
    CentralCache\\<model>.rvt. After a SUCCESSFUL sync (everything pushed
    and relinquished) those are deleted. After a failed sync they are
    kept. A model Revit had to leave open (activate route) is cleaned
    after the next open replaces it.
  - before each open the free space is checked; below min_free_gb the
    worker first deletes cached LINKED models (LinkedModels\\ - files in
    use by any Revit are locked and simply skipped), and if still low it
    stops: the rest come back as low_disk / not processed.
"""
import os
import sys
import shutil

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import dee_background as bg

_FAILURES_ATTACHED = [False]
_LOW_DISK = [False]
_DEFERRED_CLEAN = []        # (project_guid, model_guid) of a model left open by the activate route


def _attach_failures(ctx):
    if _FAILURES_ATTACHED[0]:
        return
    try:
        import dee_known_dialogs
        dee_known_dialogs.attach_failures(ctx.uiapp or ctx.app, delete_errors=False)
        _FAILURES_ATTACHED[0] = True
    except Exception as e:
        ctx.log("  (warnings handler not attached: {0})".format(e))


# ---------------------------------------------------------------- disk
def _cache_root(ctx):
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Autodesk", "Revit", "Autodesk Revit {0}".format(ctx.app.VersionNumber),
                        "CollaborationCache")


def _free_gb(path):
    try:
        from System.IO import DriveInfo, Path
        return DriveInfo(Path.GetPathRoot(path)).AvailableFreeSpace / (1024.0 ** 3)
    except Exception:
        return None


def _project_dirs(ctx, project_guid):
    root = _cache_root(ctx)
    out = []
    try:
        for user in os.listdir(root):
            d = os.path.join(root, user, str(project_guid))
            if os.path.isdir(d):
                out.append(d)
    except Exception:
        pass
    return out


def _remove(path):
    """Deletes a file / folder. Returns bytes freed (0 when locked)."""
    size = 0
    try:
        if os.path.isdir(path):
            for dp, _dn, fn in os.walk(path):
                for f in fn:
                    try:
                        size += os.path.getsize(os.path.join(dp, f))
                    except Exception:
                        pass
            shutil.rmtree(path)
            return size if not os.path.exists(path) else 0
        if os.path.isfile(path):
            size = os.path.getsize(path)
            os.remove(path)
            return size
    except Exception:
        return 0
    return 0


def clean_model_cache(ctx, project_guid, model_guid):
    """Deletes one model's local copy. Returns GB freed."""
    mg = str(model_guid).lower()
    freed = 0
    for d in _project_dirs(ctx, project_guid):
        for parent in (d, os.path.join(d, "CentralCache")):
            try:
                names = os.listdir(parent)
            except Exception:
                continue
            for n in names:
                if n.lower().startswith(mg):
                    freed += _remove(os.path.join(parent, n))
    return freed / (1024.0 ** 3)


def clean_linked_cache(ctx):
    """Deletes cached linked models (all projects). Files open in any
    Revit are locked and skipped. Returns GB freed."""
    root = _cache_root(ctx)
    freed = 0
    try:
        for user in os.listdir(root):
            udir = os.path.join(root, user)
            if not os.path.isdir(udir):
                continue
            for proj in os.listdir(udir):
                lm = os.path.join(udir, proj, "LinkedModels")
                if not os.path.isdir(lm):
                    continue
                for dp, _dn, fn in os.walk(lm):
                    for f in fn:
                        freed += _remove(os.path.join(dp, f))
    except Exception:
        pass
    return freed / (1024.0 ** 3)


def _run_deferred_clean(ctx):
    while _DEFERRED_CLEAN:
        pg, mg = _DEFERRED_CLEAN.pop()
        if getattr(ctx, "activated", None) is not None and getattr(ctx, "pending_close", None) is not None:
            _DEFERRED_CLEAN.append((pg, mg))     # still the open one
            return
        gb = clean_model_cache(ctx, pg, mg)
        if gb:
            ctx.log("  (cleaned the previous model's local copy: {0:.1f} GB)".format(gb))


def _disk_ok(ctx):
    """True when there is room to open the next model."""
    limit = float(ctx.options.get("min_free_gb") or 0)
    if limit <= 0:
        return True
    root = _cache_root(ctx)
    free = _free_gb(root)
    if free is None or free >= limit:
        return True
    ctx.log("  low disk space: {0:.1f} GB free (limit {1:.0f} GB) - clearing cached linked models...".format(free, limit))
    gb = clean_linked_cache(ctx)
    free = _free_gb(root) or 0
    ctx.log("  freed {0:.1f} GB - now {1:.1f} GB free".format(gb, free))
    return free >= limit


# ---------------------------------------------------------------- sync
def _error_text(e):
    text = (str(e) or "").strip()
    name = type(e).__name__
    try:
        net = getattr(e, "clsException", None)
        if net is not None:
            name = net.GetType().FullName
            text = text or (net.Message or "").strip()
    except Exception:
        pass
    return "{0}: {1}".format(name, text) if text else "{0} (Revit gave no message)".format(name)


def _sync(ctx, doc):
    """Synchronize (relinquish everything) a workshared cloud model; a
    non-workshared cloud model is saved with SaveCloudModel. Returns
    (ok, detail)."""
    import deew_document_manager as docmgr
    from Autodesk.Revit.DB import (TransactWithCentralOptions, SynchronizeWithCentralOptions,
                                   RelinquishOptions)
    comment = ctx.options.get("sync_comment") or "DeeLazy - Updater"
    if docmgr.is_detached(doc):
        return False, "opened detached - cannot synchronize"
    if docmgr.is_workshared(doc):
        try:
            so = SynchronizeWithCentralOptions()
            so.Comment = comment
            # same settings as deew_document_manager.synchronize_with_central
            # (proven on the first live run) - only the error text differs
            so.SaveLocalBefore = True
            so.SaveLocalAfter = True
            so.SetRelinquishOptions(RelinquishOptions(True))
            doc.SynchronizeWithCentral(TransactWithCentralOptions(), so)
            return True, "Synchronized with central"
        except Exception as e:
            return False, _error_text(e)
    if docmgr.is_cloud_model(doc):
        try:
            doc.SaveCloudModel()
            return True, "Saved to cloud (not workshared)"
        except Exception as e:
            return False, _error_text(e)
    return docmgr.save_standalone(doc)


def process_model(ctx, item):
    _attach_failures(ctx)
    res = {"ok": False, "stage": "open", "detail": "", "synced": False, "sync_detail": "", "low_disk": False}
    clean = bool(ctx.options.get("clean_cache", True))
    if clean:
        _run_deferred_clean(ctx)
    if _LOW_DISK[0] or not _disk_ok(ctx):
        _LOW_DISK[0] = True
        res["low_disk"] = True
        res["detail"] = "low disk space - not opened"
        ctx.log(u"  ✗ not opened - low disk space")
        return res
    pg, mg = item["project_guid"], item["model_guid"]
    doc, detail = bg.open_cloud_model(ctx, ctx.options["region"], pg, mg,
                                      detach=False, audit=bool(ctx.options.get("audit")))
    if doc is None:
        res["detail"] = detail
        ctx.log(u"  ✗ could not open - skipped: " + detail)
        if "disk space" in (detail or "").lower():
            res["low_disk"] = True
        return res
    res["ok"] = True
    res["stage"] = "sync"
    res["detail"] = "opened " + detail
    ctx.log("  opened " + detail)
    try:
        ok, sdetail = _sync(ctx, doc)
        res["synced"], res["sync_detail"] = bool(ok), sdetail
        ctx.log((u"  ✓ " if ok else u"  ✗ sync failed: ") + sdetail)
        if ok:
            res["stage"] = "done"
    except Exception as e:
        res["sync_detail"] = _error_text(e)
        ctx.log(u"  ✗ sync failed: " + res["sync_detail"])
    finally:
        bg.close_model(ctx, doc)
        ctx.log("  closed")
    if clean and res["synced"]:
        if getattr(ctx, "pending_close", None) is doc:
            _DEFERRED_CLEAN.append((pg, mg))      # Revit keeps it open until the next model
        else:
            gb = clean_model_cache(ctx, pg, mg)
            ctx.log("  local copy removed ({0:.1f} GB freed)".format(gb))
    return res
