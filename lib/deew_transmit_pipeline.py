# -*- coding: utf-8 -*-
"""
deew_transmit_pipeline
DeeW.Transmit's per-model work, moved out of the button script so it
can also run inside a BACKGROUND Revit (lib/dee_background +
lib/deew_background_ui/_worker): open detached -> clean -> save a copy
into the destination folder -> close. The source is never written.

worker(ctx, item) is what the background Revit calls once per item;
items are deew_background_ui.local_item / cloud_item dicts and
ctx.options is the window's _current_options() plus "token".
"""
import os
import time

import deew_model_scanner as scanner
import deew_document_manager as docmgr
import deew_clean_service as cleansvc
import deew_report_generator as reportgen
import acc_file_browser as afb

STATUS_FIELD = "save_status"
SUCCESS = ("Copy saved",)


def _revit_version_text(app):
    try:
        return str(app.VersionNumber)
    except Exception:
        return "Unknown"


class CloudTransmitItem(object):
    """One row per cloud model added via "Add Cloud Models..." - same
    plain shape acc_file_browser's pick_hub/pick_project/
    list_project_files already return."""

    def __init__(self, hub_id, hub_name, region, project_id, project_name, item_id, display_name, token):
        self.selected = False
        self.hub_id = hub_id
        self.hub_name = hub_name
        self.region = region
        self.project_id = project_id
        self.project_name = project_name
        self.item_id = item_id
        self.display_name = display_name
        self.token = token
        self.status = "Ready"

    @property
    def location_text(self):
        return "{0} / {1}".format(self.hub_name, self.project_name)


class TransmitPipeline(object):
    """Per-file Open (detached) -> Clean -> Save Copy -> Close. Kept
    separate from the Window class per this repo's UI-vs-work
    convention, and structured to mirror DeeW.Clean's CleanPipeline so
    the two stay easy to compare."""

    def __init__(self, application, options, logger):
        self.application = application
        self.options = options
        self.logger = logger

    def _target_name(self, source_file_name):
        base, ext = os.path.splitext(source_file_name)
        if not ext:
            ext = ".rvt"
        suffix = self.options.get("name_suffix", "") or ""
        return base + suffix + ext

    def _apply_clean_result(self, row, clean_result):
        row.purged_count = clean_result.purged_count
        row.zero_area_rooms_deleted = clean_result.zero_area_rooms_deleted
        row.unused_groups_deleted = clean_result.unused_groups_deleted
        row.inplace_families_found = clean_result.inplace_families_found
        row.sheets_deleted = clean_result.sheets_deleted
        row.views_deleted = clean_result.views_deleted
        row.unused_views_deleted = clean_result.unused_views_deleted
        if clean_result.errors:
            row.errors = "; ".join(clean_result.errors)
        if not clean_result.purge_supported and self.options.get("purge_unused"):
            note = "Purge Unused needs Revit 2024+ (this session is {0})".format(
                _revit_version_text(self.application))
            row.warnings = (row.warnings + "; " + note) if row.warnings else note

    def _add_error(self, row, detail):
        row.errors = (row.errors + "; " + detail) if row.errors else detail

    def _save_copy(self, row, document, source_file_name):
        dest_folder = self.options.get("dest_folder", "")
        target = docmgr.unique_target_path(dest_folder, self._target_name(source_file_name))
        ok, detail = docmgr.save_copy_as(
            document, target, compact=self.options.get("compact_on_save", False),
            overwrite=False, logger=self.logger)
        if ok:
            row.save_status = "Copy saved"
            row.location = detail          # the saved copy's real path
        else:
            row.save_status = "Failed - could not save copy"
            self._add_error(row, detail)
        return ok

    def process_local(self, scanned_model):
        row = reportgen.CleanReportRow(
            scanned_model.file_name, scanned_model.file_path, "Local",
            scanned_model.model_type, _revit_version_text(self.application))
        start = time.time()
        document = None
        try:
            if scanned_model.model_type == scanner.MODEL_TYPE_CLOUD:
                row.save_status = "Skipped - already a cloud model (use Add Cloud Models... instead)"
                return row
            if scanned_model.model_type in (scanner.MODEL_TYPE_CORRUPTED, scanner.MODEL_TYPE_READ_ONLY):
                row.save_status = "Skipped - {0}".format(scanned_model.status)
                row.errors = scanned_model.error
                return row

            # Detach where the model supports it, fall back where it
            # does not - a standalone .rvt has nothing to detach from
            # and Revit rejects the option outright.
            document, open_detail = docmgr.open_document_best_detach(
                self.application, scanned_model.file_path,
                audit=self.options.get("audit", False), logger=self.logger)
            if document is None:
                row.save_status = "Failed - could not open"
                row.errors = open_detail
                return row
            row.warnings = (row.warnings + "; " + open_detail) if row.warnings else open_detail

            clean_result = cleansvc.clean_document(document, self.options)
            self._apply_clean_result(row, clean_result)
            self._save_copy(row, document, scanned_model.file_name)
            return row
        except Exception as e:
            row.save_status = "Failed - unexpected error"
            row.errors = str(e)
            self.logger.exception("Unexpected error transmitting file", e, file=scanned_model.file_name)
            return row
        finally:
            if document is not None:
                # save_modified=False always: the detached document has
                # already been written to its new path, and there is
                # nothing we ever want flushed back toward the origin.
                docmgr.close_document(document, save_modified=False, logger=self.logger)
            row.processing_time_seconds = time.time() - start

    def process_cloud(self, item, uiapp):
        """Cloud models are opened HEADLESS + DETACHED (see
        acc_file_browser.open_cloud_document_detached for why). The
        earlier version used the UI-activating, non-detaching
        open_cloud_file, which made SaveAs illegal on the still-attached
        central and destabilised every subsequent open in the batch."""
        row = reportgen.CleanReportRow(
            item.display_name, item.location_text, "Cloud", "Cloud Model",
            _revit_version_text(self.application))
        start = time.time()
        document = None
        try:
            document, open_detail = afb.open_cloud_document_detached(
                self.application, item.region, item.project_id, item.item_id, item.token,
                audit=self.options.get("audit", False))
            if document is None:
                row.save_status = "Failed - could not open"
                row.errors = open_detail
                return row
            row.warnings = (row.warnings + "; " + open_detail) if row.warnings else open_detail

            clean_result = cleansvc.clean_document(document, self.options)
            self._apply_clean_result(row, clean_result)
            # The document is detached, so this writes only to the new
            # path - the ACC model is never synchronized or altered.
            self._save_copy(row, document, item.display_name)
            return row
        except Exception as e:
            row.save_status = "Failed - unexpected error"
            row.errors = str(e)
            self.logger.exception("Unexpected error transmitting cloud file", e, file=item.display_name)
            return row
        finally:
            if document is not None:
                try:
                    docmgr.close_document(document, save_modified=False, logger=self.logger)
                except Exception:
                    pass
            row.processing_time_seconds = time.time() - start


# ==========================================================================
# background Revit entry point
# ==========================================================================
def worker(ctx, item):
    import deew_background_worker as bgw
    logger = bgw.setup(ctx, "DeeWTransmit")
    pipeline = TransmitPipeline(ctx.app, ctx.options, logger)
    if item.get("kind") == "cloud":
        cloud = CloudTransmitItem(item["hub_id"], item["hub_name"], item["region"], item["project_id"],
                                  item["project_name"], item["item_id"], item["display_name"], bgw.cloud_token(ctx))
        row = pipeline.process_cloud(cloud, ctx.uiapp)
    else:
        row = pipeline.process_local(bgw.local_model(ctx, item))
    ctx.log(u"  {0}{1}".format(row.save_status, (" - " + row.errors) if row.errors else ""))
    return bgw.row_to_result(row, STATUS_FIELD, SUCCESS)
