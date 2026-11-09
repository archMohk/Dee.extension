# -*- coding: utf-8 -*-
"""
deew_batch_pipeline
DeeWBatchSaveToCloud's per-model work (BatchPipeline), moved out of the button script so it
can also run inside a BACKGROUND Revit (lib/dee_background +
lib/deew_background_ui/_worker). Unchanged logic; see the class
docstring. reserve_names() lets the host fix every cloud model name
before the items are dealt to several Revits.
"""
import os
import re
import time

import deew_model_scanner as scanner
import deew_document_manager as docmgr
import deew_cloud_service as cloudsvc
import deew_report_generator as reportgen
import acc_api

STATUS_FIELD = "upload_status"
SUCCESS = ("Uploaded",)


def _revit_version_text(app):
    try:
        return str(app.VersionNumber)
    except Exception:
        return "Unknown"


_BACKUP_PATTERN = re.compile(r"^(.+)\.\d{4}\.rvt$", re.IGNORECASE)


def _delete_local_backups(file_path, logger):
    """Deletes only files matching Revit's own numbered-backup naming
    pattern in the SAME folder as `file_path` - never the file itself.
    Best-effort: a locked/permission-denied backup file is skipped,
    not raised, since cleanup failing must never fail the whole batch
    entry for that model (the upload already succeeded by the time
    this runs)."""
    try:
        folder = os.path.dirname(file_path)
        base_name = os.path.splitext(os.path.basename(file_path))[0]
        removed = 0
        for entry in os.listdir(folder):
            match = _BACKUP_PATTERN.match(entry)
            if match and match.group(1).lower() == base_name.lower():
                try:
                    os.remove(os.path.join(folder, entry))
                    removed += 1
                except Exception:
                    pass
        if logger is not None and removed:
            logger.info("Deleted local backup file(s)", file=file_path, count=removed)
        return removed
    except Exception as e:
        if logger is not None:
            logger.exception("Backup cleanup failed", e, file=file_path)
        return 0




class BatchPipeline(object):
    """Per-file Validate -> Determine -> Open -> Detach/Worksharing ->
    Save-to-Cloud -> Verify -> Close pipeline for batch runs. Deliberately
    a SEPARATE class from DeeWSharing's SharingPipeline (each tool's
    own script.py owns its own orchestration, per this repo's
    established one-file-per-tool convention) even though both call
    into the exact same shared lib/deew_*.py services underneath -
    only the duplicate-handling policy (3 modes here vs 2 in
    DeeW.Sharing) and the backup-cleanup step differ."""

    def __init__(self, application, destination, options, logger):
        self.application = application
        self.destination = destination
        self.options = options
        self.logger = logger
        self._cloud_model_names_cache = None

    def _existing_cloud_names(self, token):
        if self._cloud_model_names_cache is None:
            try:
                items = acc_api.search_cloud_models(self.destination["project_id"], token)
                self._cloud_model_names_cache = set(name for _id, name in items)
            except Exception as e:
                self.logger.exception("Could not list existing cloud models", e)
                self._cloud_model_names_cache = set()
        return self._cloud_model_names_cache


    def reserve_names(self, scanned_models, token):
        """Resolves the cloud name of every model up front, in order, so
        that background Revits (which each see the project's name list
        only once) never pick the same "(n)" twice. Returns
        {file_path: [name_or_None, skip_reason_or_None]}; resolved
        names are also honoured by process_one through
        options["resolved_names"]."""
        out = {}
        for m in scanned_models:
            name, skip = self._resolve_model_name(m, token)
            out[m.file_path] = [name, skip]
            if name:
                self._existing_cloud_names(token).add(name)
        return out

    def _resolve_model_name(self, scanned_model, token):
        preset = (self.options.get("resolved_names") or {}).get(scanned_model.file_path)
        if preset:
            return preset[0], preset[1]
        return self._resolve_model_name_live(scanned_model, token)

    def _resolve_model_name_live(self, scanned_model, token):
        base_name = os.path.splitext(scanned_model.file_name)[0]
        existing = self._existing_cloud_names(token)
        if base_name not in existing:
            return base_name, None

        if self.options.get("skip_existing"):
            return None, "Skipped - a cloud model named '{0}' already exists".format(base_name)
        if self.options.get("overwrite_existing"):
            # See module docstring's counterpart in DeeW.Sharing - true
            # overwrite (deleting the existing cloud item) requires an
            # unverified destructive Data Management API DELETE call
            # this package does not implement; skip rather than risk it.
            return None, "Skipped - a cloud model named '{0}' already exists (overwrite not performed)".format(base_name)

        n = 2
        candidate = "{0} ({1})".format(base_name, n)
        while candidate in existing:
            n += 1
            candidate = "{0} ({1})".format(base_name, n)
        return candidate, None

    def _detach_option(self, scanned_model):
        if scanned_model.model_type in (scanner.MODEL_TYPE_CENTRAL, scanner.MODEL_TYPE_LOCAL):
            return "preserve" if self.options.get("detach") else "none"
        return "none"

    def _is_still_open(self, document):
        try:
            for d in self.application.Documents:
                if d.Equals(document):
                    return True
        except Exception:
            pass
        return False

    def process_one(self, scanned_model, token):
        row = reportgen.ReportRow(
            scanned_model.file_name, scanned_model.file_path,
            _revit_version_text(self.application), scanned_model.model_type)
        row.warnings = scanned_model.version_warning
        start = time.time()
        document = None
        opened_by_us = True
        try:
            if scanned_model.model_type == scanner.MODEL_TYPE_CLOUD:
                row.upload_status = "Skipped - already a cloud model"
                return row
            if scanned_model.model_type in (scanner.MODEL_TYPE_CORRUPTED, scanner.MODEL_TYPE_READ_ONLY):
                row.upload_status = "Skipped - {0}".format(scanned_model.status)
                row.errors = scanned_model.error
                return row

            model_name, skip_reason = self._resolve_model_name(scanned_model, token)
            if skip_reason:
                row.upload_status = skip_reason
                return row
            row.cloud_model_name = model_name + ".rvt"

            if scanned_model.live_document is not None:
                # "Add Open Files" entry - already open in this same
                # Revit session. Detach is an OpenOptions setting that
                # only applies at open time, and this tool will never
                # close a document the user already had open just to
                # reopen it detached - so only a model that needs NO
                # detach (Standalone, or already Detached earlier in
                # this session) can be processed this way.
                if not self._is_still_open(scanned_model.live_document):
                    row.upload_status = "Failed - document was closed before it could be processed"
                    return row
                if scanned_model.model_type not in (scanner.MODEL_TYPE_STANDALONE, scanner.MODEL_TYPE_DETACHED):
                    row.upload_status = (
                        "Skipped - open in this session and still attached to its central "
                        "model; Save-to-Cloud requires detaching first, which this tool will "
                        "not do to a document you already had open. Close it and re-add it, "
                        "or reopen it Detached first.")
                    return row
                document = scanned_model.live_document
                opened_by_us = False
            else:
                detach_option = self._detach_option(scanned_model)
                document, err = docmgr.open_document(
                    self.application, scanned_model.file_path,
                    detach_option=detach_option, audit=self.options.get("audit", False),
                    open_all_worksets=self.options.get("open_all_worksets", False),
                    logger=self.logger)
                if document is None:
                    row.upload_status = "Failed - could not open"
                    row.errors = err
                    return row

            if docmgr.is_cloud_model(document):
                row.upload_status = "Skipped - already a cloud model"
                return row

            if self.options.get("enable_worksharing"):
                ok = docmgr.enable_worksharing(document, logger=self.logger)
                row.worksharing_enabled = "Yes" if ok else "Failed"
            else:
                row.worksharing_enabled = "Yes" if docmgr.is_workshared(document) else "No"

            if self.options.get("compact"):
                docmgr.compact_and_save_local(document, logger=self.logger)

            row.acc_hub = self.destination.get("hub_name", "")
            row.acc_project = self.destination.get("project_name", "")
            row.acc_folder = self.destination.get("folder_name", "")

            success, detail = cloudsvc.save_to_cloud(document, self.destination, model_name)
            if success:
                row.upload_status = "Uploaded"
                row.cloud_guid = reportgen.get_cloud_model_guid(document)
                if self.options.get("delete_local_backup"):
                    removed = _delete_local_backups(scanned_model.file_path, self.logger)
                    if removed:
                        note = "Removed {0} local backup file(s)".format(removed)
                        row.warnings = "{0}; {1}".format(row.warnings, note) if row.warnings else note
            else:
                row.upload_status = "Failed - upload error"
                row.errors = detail

            return row
        except Exception as e:
            row.upload_status = "Failed - unexpected error"
            row.errors = str(e)
            self.logger.exception("Unexpected error processing file", e, file=scanned_model.file_name)
            return row
        finally:
            if document is not None and opened_by_us and self.options.get("close_after", True):
                docmgr.close_document(document, save_modified=False, logger=self.logger)
            row.processing_time_seconds = time.time() - start

# ==========================================================================
# background Revit entry point
# ==========================================================================
def worker(ctx, item):
    import deew_background_worker as bgw
    logger = bgw.setup(ctx, "DeeWBatchSaveToCloud")
    options = ctx.options
    dest_index = item.get("dest_index")
    if dest_index is not None:
        # "Upload to several ACC projects": one item per (model, project)
        options = dict(ctx.options)
        destination = dict((ctx.options.get("destinations") or [])[dest_index])
        options["resolved_names"] = (ctx.options.get("resolved_names_by_dest") or [{}] * (dest_index + 1))[dest_index]
    else:
        destination = dict(ctx.options.get("destination") or {})
    token = bgw.cloud_token(ctx)
    destination["token"] = token
    model = bgw.local_model(ctx, item)
    can_upload, why_not = bgw.ensure_cloud_upload(ctx)
    if not can_upload:
        # Fail before the (minutes-long) open/detach - the upload cannot work here.
        row = reportgen.ReportRow(model.file_name, model.file_path,
                                  _revit_version_text(ctx.app), model.model_type)
        row.upload_status = "Failed - upload error"
        row.errors = why_not
        return bgw.row_to_result(row, STATUS_FIELD, SUCCESS)
    pipeline = BatchPipeline(ctx.app, destination, options, logger)
    row = pipeline.process_one(model, token)
    ctx.log(u"  {0}{1}".format(row.upload_status, (" - " + row.errors) if row.errors else ""))
    return bgw.row_to_result(row, STATUS_FIELD, SUCCESS)
