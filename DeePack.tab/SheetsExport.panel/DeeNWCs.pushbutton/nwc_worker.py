# -*- coding: utf-8 -*-
"""
DeeNWCs - background worker.

Runs inside a second (third, ...) Revit through lib/dee_background, which
deals the models out between the Revits and collects what each returns.
One call per model:

    process_model(ctx, item) -> {"ok": bool, "rows": [[ok, label, view, detail], ...],
                                 "failed_uploads": [[path, detail], ...]}

item    = {"key": ..., "name": "A.rvt", "source": <dee_schedule_engine source>}
options = {"config": <DeeNWCs settings>, "export_folder": folder,
           "acc_dest": ACC destination or None, "delete_errors": bool}

A background Revit (`pyrevit run`) loads no installed add-ins by itself, so
lib/dee_background starts it with the Navisworks exporter's manifest handed
over (addins=["nwexportrevit"]) - without it every model failed with "this
Revit has no Navisworks exporter installed".

The export itself is DeeNWCs' own _export_for_document (script.py is
loaded the way DeeSchedule loads it), so a background run produces exactly
what the button produces. Models are opened DETACHED and closed without
saving: nothing is ever synchronized or changed on the server.

Revit's own "open and activate" route is never used here (activate=False):
it builds a full UI view in this UI-less Revit and crashed one on a model
whose starting view is a sheet. A model that fails to open headless is
simply tried again.

A model that fails to open is tried again (Revit's "The host model failed
to open. Please try again." is very often a one-off); the dialog itself is
closed by dee_known_dialogs. A model that still cannot be opened is
reported and the worker moves on.
"""
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import dee_background as bg

_OPEN_TRIES = 3
_STATE = {}


def _setup(ctx):
    """Once per Revit: the DeeNWCs script namespace, the failures handler
    and (for ACC output) the uploader."""
    if "space" in _STATE:
        return _STATE
    import dee_schedule_engine
    import dee_known_dialogs
    _STATE["space"] = dee_schedule_engine.load_runner(ctx.uiapp, "DeeNWCs")
    # For the whole life of this Revit, not just during an export: the
    # exporter's boxes can also come while a model closes or Revit quits,
    # and a box nobody answers keeps this Revit (and the batch) alive.
    import dee_native_dialogs

    def answered(title, text):
        ctx.log(u"  (answered '{0}') {1}".format(title, text))
        if "journal" in title.lower():
            # playback of the journal's Quit line failed behind the exporter's
            # box: nothing will close this Revit but us, once the job is done.
            bg.exit_when_done()
    _STATE["dialogs"] = dee_native_dialogs.Watcher(
        rules=dee_native_dialogs.NWC_RULES + dee_native_dialogs.JOURNAL_RULES,
        on_caught=answered).start()
    try:
        dee_known_dialogs.attach_failures(
            ctx.uiapp or ctx.app, delete_errors=bool(ctx.options.get("delete_errors")))
    except Exception as e:
        ctx.log("  (warnings handler not attached: {0})".format(e))
    dest = ctx.options.get("acc_dest")
    if dest:
        import acc_upload
        _STATE["uploader"] = acc_upload.Uploader(
            dest, ctx.options["export_folder"], delete_after=True)
    return _STATE


def _open(ctx, src):
    detail = ""
    for attempt in range(_OPEN_TRIES):
        if src.get("kind") == "cloud":
            doc, detail = bg.open_cloud_model(
                ctx, src.get("region") or "US", src["project"], src["model"], detach=True,
                activate=False)
        else:
            doc, detail = bg.open_local_model(ctx, src["path"], detach=True, activate=False)
        if doc is not None:
            return doc, detail
        ctx.log("  open failed ({0}): {1}".format(attempt + 1, detail))
        if attempt + 1 < _OPEN_TRIES:
            time.sleep(8 * (attempt + 1))
    return None, detail


def process_model(ctx, item):
    label = item.get("name") or item.get("key")
    state = _setup(ctx)
    space = state["space"]
    if not space["_has_exporter"]():
        return {"ok": False, "failed_uploads": [], "rows": [
            [False, label, "-", "this Revit has no Navisworks exporter installed"]]}

    doc, detail = _open(ctx, item["source"])
    if doc is None:
        return {"ok": False, "failed_uploads": [], "rows": [
            [False, label, "-", u"Could not open: {0}".format(detail)]]}
    uploader = state.get("uploader")
    before = len(uploader.failed) if uploader is not None else 0
    try:
        rows = space["_export_for_document"](
            doc, label, ctx.options["config"], ctx.options["export_folder"], set(), uploader)
    finally:
        bg.close_model(ctx, doc)
    rows = [[r[0], r[1], r[2], r[3]] for r in rows]
    for row in rows:
        ctx.log(u"  {0} [{1}] {2}".format(
            {True: "OK", False: "FAILED", None: "skipped"}.get(row[0], "note"), row[2], row[3]))
    failed = [[p, d] for p, d in (uploader.failed[before:] if uploader is not None else [])]
    return {"ok": all(r[0] is not False for r in rows), "rows": rows, "failed_uploads": failed}
