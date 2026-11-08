# -*- coding: utf-8 -*-
"""
deew_background_worker
Worker-side helpers shared by the DeeW.Cloud pipelines when they run in
a background Revit (lib/dee_background.run_job calls the tool's
worker(ctx, item) once per model).

    setup(ctx, tool_name)   -> DeeWLogger writing into the job folder,
                               DeeW dialog handler + failures hook on
                               that Revit, delete-errors policy from
                               the job options. Done once per worker
                               (cached on ctx).
    local_model(item)       -> deew_model_scanner.ScannedModel rebuilt
                               from the item's path (+ version warning).
    cloud_token(ctx)        -> the token the host put in the options,
                               else acc_auth's cached sign-in.
    row_to_result(row, status_field, success_values)
                            -> the result dict the host turns back into
                               a report row (deew_background_ui).
"""
import os


def setup(ctx, tool_name):
    if getattr(ctx, "deew_logger", None) is not None:
        return ctx.deew_logger
    import deew_logger
    logger = deew_logger.DeeWLogger(tool_name, log_dir=ctx.folder)
    ctx.deew_logger = logger
    try:
        import deew_failure_handler as ffh
        import dee_known_dialogs
        if ctx.options.get("auto_resolve_dialogs", True):
            handler = ffh.make_dialog_handler(logger)
        else:
            handler = ffh.make_known_dialogs_handler(logger)
        if ctx.uiapp is not None:
            ctx.uiapp.DialogBoxShowing += handler
            dee_known_dialogs.attach_failures(ctx.uiapp, delete_errors=bool(ctx.options.get("delete_errors")))
        else:
            dee_known_dialogs.set_delete_errors(bool(ctx.options.get("delete_errors")))
        ctx.deew_handler = handler
    except Exception as e:
        ctx.log("DeeW dialog handler not attached: {0}".format(e))
    return logger


def local_model(ctx, item):
    import deew_model_scanner as scanner
    model = scanner.scan_file(item["path"])
    try:
        scanner.annotate_version_mismatch([model], str(ctx.app.VersionNumber))
    except Exception:
        pass
    model.selected = True
    return model


def cloud_token(ctx):
    token = ctx.options.get("token")
    if token:
        return token
    import acc_auth
    return acc_auth.get_access_token()


def _jsonable(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    try:
        return u"{0}".format(value)
    except Exception:
        return str(value)


def row_to_result(row, status_field, success_values=()):
    data = {}
    for k, v in vars(row).items():
        if k.startswith("_") or k == "live_document":
            continue
        data[k] = _jsonable(v)
    status = getattr(row, status_field, "") or ""
    ok = status in success_values if success_values else ("fail" not in status.lower())
    return {"ok": bool(ok), "status": status, "detail": getattr(row, "errors", "") or "", "row": data,
            "location": getattr(row, "location", "") or ""}


def job_folder_name(ctx):
    return os.path.basename(ctx.folder)
