# -*- coding: utf-8 -*-
"""
DeeLazy - Remove Links: background worker function.

Runs inside a second Revit through lib/dee_background (see that module
for the mechanics: job folders, stub, log, results, stop file). One call
per host model:

    process_host(ctx, item) -> {"ok", "detail", "saved", "save_detail", "rows": {row_key: status}}

item = {"key": item_id, "name": "A.rvt", "project_guid": "...", "model_guid": "...",
        "remove_all": bool, "rows": [{"type_id": "123", "name": "B.rvt", "kind": "Revit"}]}
ctx.options = {"action": "remove" | "unload", "region": "US", "sync_comment": "..."}
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import dee_background as bg


def _row_key(row):
    return row.get("type_id") or (row.get("name") or "")


def _still(doc, unloaded_count_as_gone):
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


def _apply(ctx, item, doc):
    """Removes / unloads the item's rows in the open doc. Returns
    (changed_count, {row_key: status})."""
    import dee_remove_links as rl
    log = ctx.log
    action = ctx.options.get("action", "remove")
    rows = item.get("rows") or []
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
            _ids, _names, still_cad = _still(doc, False)
            for r in cad_rows:
                statuses[_row_key(r)] = rl.ST_REMOVED if r.get("type_id") not in still_cad else "Remove failed"
            for nm, why in sorted(reasons.items()):
                log(u"  ! {0}: {1}".format(nm, why))
            log("  removed {0} of {1} CAD link/import type(s)".format(done, targeted))
            changed += done

    if rvt_rows:
        ids = [r["type_id"] for r in rvt_rows if r.get("type_id")]
        names = [r.get("name") for r in rvt_rows]
        remove_all = bool(item.get("remove_all"))
        if action == "unload":
            done, targeted, missing, reasons = rl.unload_links(doc, ids, names, remove_all=remove_all)
            ok_status, fail_status = rl.ST_UNLOADED, "Unload failed"
            still, still_names, _cad = _still(doc, True)
        else:
            done, targeted, missing, reasons = rl.remove_links(doc, ids, names, remove_all=remove_all)
            ok_status, fail_status = rl.ST_REMOVED, "Remove failed"
            still, still_names, _cad = _still(doc, False)
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


def process_host(ctx, item):
    import dee_remove_links as rl
    rl._SYNC_COMMENT = ctx.options.get("sync_comment") or rl._SYNC_COMMENT
    res = {"ok": False, "detail": "", "saved": False, "save_detail": "", "rows": {}}
    doc, detail = bg.open_cloud_model(ctx, ctx.options["region"], item["project_guid"], item["model_guid"])
    if doc is None:
        res["detail"] = detail
        ctx.log(u"  ✗ could not open - " + detail)
        return res
    res["ok"] = True
    res["detail"] = "opened " + detail
    ctx.log("  opened " + detail)
    try:
        changed, statuses = _apply(ctx, item, doc)
        res["rows"] = statuses
        if changed:
            ok, sdetail = rl.save_after_remove(doc)
            res["saved"], res["save_detail"] = bool(ok), sdetail
            ctx.log(u"  ✓ " + sdetail if ok else u"  ✗ sync/save failed: " + sdetail)
            if not ok:
                for k, st in list(statuses.items()):
                    if st in rl.ST_DONE:
                        statuses[k] = "Not synced"
        else:
            res["saved"], res["save_detail"] = True, "nothing changed"
    finally:
        bg.close_model(ctx, doc)
    return res
