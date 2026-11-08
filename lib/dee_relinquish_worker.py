# -*- coding: utf-8 -*-
"""
dee_relinquish_worker
DeeRelinquish's per-model work for a BACKGROUND Revit (lib/dee_background
+ lib/deew_background_ui/_worker): open the ACC model attached,
optionally synchronize first, release the current user's ownership for
the ticked categories, close without saving.

item = {"key": item_id, "name": ..., "item_id": ...}
ctx.options = {"region", "project_id", "flags": {category: bool},
               "sync_first": bool, "token": ..., "user": <host Revit user>}
Returns {"ok", "status", "detail", "synced"}.

The release only touches what the signed-in user owns, so the worker
checks it runs as the same Autodesk user as the window that sent it.
"""
import acc_file_browser as afb
import deew_document_manager as docmgr
import dee_relinquish_service as core


def worker(ctx, item):
    import deew_background_worker as bgw
    logger = bgw.setup(ctx, "DeeRelinquish")
    opts = ctx.options
    res = {"ok": False, "status": "", "detail": "", "synced": False}
    try:
        me = ctx.app.Username
    except Exception:
        me = ""
    if opts.get("user") and me and me != opts["user"]:
        res["status"] = "FAILED - this Revit is signed in as '{0}', not '{1}'".format(me, opts["user"])
        ctx.log(u"  ✗ " + res["status"])
        return res
    doc = None
    try:
        doc, detail = afb.open_cloud_document_attached(
            ctx.app, opts["region"], opts["project_id"], item["item_id"], bgw.cloud_token(ctx))
        if doc is None:
            res["status"] = "FAILED to open - {0}".format(detail)
            ctx.log(u"  ✗ could not open: " + detail)
            return res
        if opts.get("sync_first"):
            ok, sdetail = core.synchronize_only(doc, logger=logger)
            res["synced"] = bool(ok)
            ctx.log("  synchronized" if ok else "  sync FAILED: {0}".format(sdetail))
        ok, rdetail = core.relinquish_document(doc, opts.get("flags") or {}, logger=logger)
        res["ok"] = bool(ok)
        res["detail"] = rdetail
        res["status"] = ("OK - " if ok else "FAILED - ") + rdetail
        ctx.log((u"  ✓ " if ok else u"  ✗ ") + rdetail)
        return res
    finally:
        if doc is not None:
            docmgr.close_document(doc, save_modified=False, logger=logger)
