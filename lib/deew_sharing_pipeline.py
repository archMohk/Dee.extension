# -*- coding: utf-8 -*-
"""
deew_sharing_pipeline
DeeWSharing's per-model work (SharingPipeline), moved out of the button script so it
can also run inside a BACKGROUND Revit (lib/dee_background +
lib/deew_background_ui/_worker). Unchanged logic; see the class
docstring. reserve_names() lets the host fix every cloud model name
before the items are dealt to several Revits.
"""
import os
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


class SharingPipeline(object):
    """Owns the actual per-file Validate -> Determine -> Open ->
    Detach/Worksharing -> Save-to-Cloud -> Verify -> Close pipeline.
    Kept separate from the Window class (single responsibility: this
    class does the Revit/cloud work, the Window class only handles
    UI wiring) even though both live in this one script.py file, per
    this repo's established per-tool-file convention."""

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

        if self.options.get("overwrite_cloud"):
            # See module docstring - true overwrite (deleting the
            # existing cloud item) is not implemented; skip rather
            # than risk an unverified destructive operation.
            return None, "Skipped - a cloud model named '{0}' already exists (overwrite not performed)".format(base_name)

        # Rename Duplicate (default): find the next free "(n)" suffix.
        n = 2
        candidate = "{0} ({1})".format(base_name, n)
        while candidate in existing:
            n += 1
            candidate = "{0} ({1})".format(base_name, n)
        return candidate, None

    def _detach_option(self, scanned_model):
        if scanned_model.model_type in (scanner.MODEL_TYPE_CENTRAL, scanner.MODEL_TYPE_LOCAL):
            if not self.options.get("detach"):
                return "none"
            return "discard" if self.options.get("discard_worksets") else "preserve"
        return "none"

    def process_one(self, scanned_model, token, dialog_handler_registered):
        row = reportgen.ReportRow(
            scanned_model.file_name, scanned_model.file_path,
            _revit_version_text(self.application), scanned_model.model_type)
        row.warnings = scanned_model.version_warning
        start = time.time()
        document = None
        try:
            if scanned_model.model_type == scanner.MODEL_TYPE_CLOUD:
                row.upload_status = "Skipped - already a cloud model"
                self.logger.info("Skipped (already cloud)", file=scanned_model.file_name)
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
                self.logger.info("Uploaded", file=scanned_model.file_name, detail=detail)
            else:
                row.upload_status = "Failed - upload error"
                row.errors = detail
                self.logger.error("Upload failed", file=scanned_model.file_name, detail=detail)

            return row
        except Exception as e:
            row.upload_status = "Failed - unexpected error"
            row.errors = str(e)
            self.logger.exception("Unexpected error processing file", e, file=scanned_model.file_name)
            return row
        finally:
            if document is not None and self.options.get("close_after", True):
                docmgr.close_document(document, save_modified=False, logger=self.logger)
            row.processing_time_seconds = time.time() - start

# ==========================================================================
# background Revit entry point
# ==========================================================================
def worker(ctx, item):
    import deew_background_worker as bgw
    logger = bgw.setup(ctx, "DeeWSharing")
    destination = dict(ctx.options.get("destination") or {})
    token = bgw.cloud_token(ctx)
    destination["token"] = token
    pipeline = SharingPipeline(ctx.app, destination, ctx.options, logger)
    row = pipeline.process_one(bgw.local_model(ctx, item), token, False)
    ctx.log(u"  {0}{1}".format(row.upload_status, (" - " + row.errors) if row.errors else ""))
    return bgw.row_to_result(row, STATUS_FIELD, SUCCESS)
