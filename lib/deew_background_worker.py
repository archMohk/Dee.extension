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
    ensure_cloud_upload(ctx) -> (ok, detail): makes Document.SaveAsCloudModel
                               usable in this Revit (see below). Cached.

Why ensure_cloud_upload exists: `pyrevit run` starts Revit with ONLY its
own PyRevitRunner.addin, so manifests under ProgramData/AppData Addins
are not read - including Autodesk "Cloud Models for Revit"
(Autodesk.Collaborate.addin, CollaborateDB). That add-in registers the
CloudExternalServer on Revit's cloud external service, and
SaveAsCloudModel looks it up internally: without it every upload fails
after the full open/detach with "An internal error has occurred"
(journal: ExternalService::getServer throws). Verified against the
background journals of a 6/6-failed run on 2026-11-09.
"""
import os

# Revit's cloud external service and the server CollaborateDB registers on it
# (ids read from a normal Revit 2024 journal: "Registered an external server:
# Name(CloudExternalServer) ... ServerId(31ebe9da-...) ServiceId(279080e0-...)").
CLOUD_SERVICE_ID = "279080e0-8c45-44eb-9027-d4eb37ece90d"
CLOUD_SERVER_ID = "31ebe9da-aa3f-430d-a83f-fed6f1089070"
COLLABORATE_ADDIN = "Autodesk.Collaborate.addin"

NO_CLOUD_UPLOAD = (
    "Cloud upload is not available in a background Revit: Autodesk 'Cloud Models for Revit' "
    "(Autodesk.Collaborate.addin) is not loaded there, and SaveAsCloudModel needs it. "
    "Run this upload with 'Run by: This Revit session'.")


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


def _cloud_server_registered():
    """True / False, or None when the registry can't be queried (then the
    caller lets the upload try rather than blocking on a failed check)."""
    try:
        import System
        from Autodesk.Revit.DB.ExternalService import ExternalServiceRegistry, ExternalServiceId
        service = ExternalServiceRegistry.GetService(ExternalServiceId(System.Guid(CLOUD_SERVICE_ID)))
        if service is None:
            return False
        return bool(service.IsRegisteredServerId(System.Guid(CLOUD_SERVER_ID)))
    except Exception:
        return None


def _collaborate_manifests(version):
    roots = [os.environ.get("ProgramData") or r"C:\ProgramData",
             os.environ.get("APPDATA") or ""]
    found = []
    for root in roots:
        if not root:
            continue
        path = os.path.join(root, "Autodesk", "Revit", "Addins", str(version), COLLABORATE_ADDIN)
        if os.path.isfile(path):
            found.append(path)
    return found


def ensure_cloud_upload(ctx):
    """Returns (ok, detail). Checks the cloud server SaveAsCloudModel needs
    is registered; if not, loads Autodesk.Collaborate.addin into this
    Revit (UIApplication.LoadAddIn) and checks again. Done once per
    worker - the result is cached on ctx and logged once."""
    cached = getattr(ctx, "cloud_upload_check", None)
    if cached is not None:
        return cached
    state = _cloud_server_registered()
    if state is None or state:
        result = (True, "")
    else:
        try:
            version = ctx.app.VersionNumber
        except Exception:
            version = ""
        errors = []
        manifests = _collaborate_manifests(version)
        if ctx.uiapp is None:
            errors.append("no UIApplication to load add-ins with")
        elif not manifests:
            errors.append("{0} not found for Revit {1} - is Cloud Models for Revit installed?".format(
                COLLABORATE_ADDIN, version))
        for manifest in manifests if ctx.uiapp is not None else []:
            try:
                ctx.uiapp.LoadAddIn(manifest)
            except Exception as e:
                errors.append("LoadAddIn({0}): {1}".format(manifest, str(e).split("\n")[0]))
            if _cloud_server_registered() is not False:
                break
        if _cloud_server_registered() is False:
            detail = NO_CLOUD_UPLOAD + (" ({0})".format("; ".join(errors)) if errors else "")
            ctx.log("  " + detail)
            result = (False, detail)
        else:
            ctx.log("  Loaded Cloud Models for Revit into this background Revit for the upload.")
            result = (True, "")
    ctx.cloud_upload_check = result
    return result


def job_folder_name(ctx):
    return os.path.basename(ctx.folder)
