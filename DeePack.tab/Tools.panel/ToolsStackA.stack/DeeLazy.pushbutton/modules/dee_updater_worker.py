# -*- coding: utf-8 -*-
"""
DeeLazy - Updater: background worker function.

Runs inside a second Revit through lib/dee_background (job folders,
stub, log, results, stop file - see that module). One call per model:

    process_model(ctx, item) -> {"ok", "stage", "detail", "synced", "sync_detail"}

item = {"key": item_id, "name": "A.rvt", "project": "Project name",
        "project_guid": "...", "model_guid": "..."}
ctx.options = {"region": "US", "sync_comment": "...", "audit": bool}

A model that cannot be opened is reported (ok=False, stage="open") and
the worker simply moves on to the next one - a bad file never stops the
batch. Dialogs are auto-answered by the handler run_job attaches;
warnings / errors raised during the sync are handled by
dee_known_dialogs (attached once per worker).
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import dee_background as bg

_FAILURES_ATTACHED = [False]


def _attach_failures(ctx):
    if _FAILURES_ATTACHED[0]:
        return
    try:
        import dee_known_dialogs
        dee_known_dialogs.attach_failures(ctx.uiapp or ctx.app, delete_errors=False)
        _FAILURES_ATTACHED[0] = True
    except Exception as e:
        ctx.log("  (warnings handler not attached: {0})".format(e))


def _sync(ctx, doc):
    """Synchronize (relinquish everything) a workshared cloud model; a
    non-workshared cloud model is saved with SaveCloudModel. Returns
    (ok, detail)."""
    import deew_document_manager as docmgr
    comment = ctx.options.get("sync_comment") or "DeeLazy - Updater"
    if docmgr.is_detached(doc):
        return False, "opened detached - cannot synchronize"
    if docmgr.is_workshared(doc):
        return docmgr.synchronize_with_central(doc, comment=comment)
    if docmgr.is_cloud_model(doc):
        try:
            doc.SaveCloudModel()
            return True, "Saved to cloud (not workshared)"
        except Exception as e:
            return False, str(e)
    return docmgr.save_standalone(doc)


def process_model(ctx, item):
    _attach_failures(ctx)
    res = {"ok": False, "stage": "open", "detail": "", "synced": False, "sync_detail": ""}
    doc, detail = bg.open_cloud_model(ctx, ctx.options["region"], item["project_guid"], item["model_guid"],
                                      detach=False, audit=bool(ctx.options.get("audit")))
    if doc is None:
        res["detail"] = detail
        ctx.log(u"  ✗ could not open - skipped: " + detail)
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
        res["sync_detail"] = str(e)
        ctx.log(u"  ✗ sync failed: {0}".format(e))
    finally:
        bg.close_model(ctx, doc)
        ctx.log("  closed")
    return res
