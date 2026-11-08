# -*- coding: utf-8 -*-
"""
deew_clean_pipeline
DeeW.Clean's per-model work (CleanPipeline + CloudCleanItem), moved out
of the button script so it can also run inside a BACKGROUND Revit
(lib/dee_background + lib/deew_background_ui/_worker): open the REAL
model attached -> clean -> synchronize / save -> close.

Cloud models are now opened headless + attached
(acc_file_browser.open_cloud_document_attached) instead of the
UI-activating open_cloud_file: same result (the cleanup is synchronized
back to ACC), no active-tab switching, and it works in a second Revit.
"""
import time

import deew_model_scanner as scanner
import deew_document_manager as docmgr
import deew_clean_service as cleansvc
import deew_report_generator as reportgen
import acc_file_browser as afb

STATUS_FIELD = "save_status"
SUCCESS = ("Saved", "Synchronized with Central")


def _revit_version_text(app):
    try:
        return str(app.VersionNumber)
    except Exception:
        return "Unknown"


class CloudCleanItem(object):
    """One row per cloud model added via "Add Cloud Models..." - plain
    fields matching acc_file_browser's existing pick_hub/pick_project/
    list_project_files return shapes, so no new ACC-browsing logic is
    needed here beyond calling those already-proven functions."""

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


class CleanPipeline(object):
    """Owns the actual per-file Open -> Clean -> Save/Sync -> Close
    pipeline, for both local and cloud sources - kept separate from
    the Window class per this repo's established one-tool-file
    convention (UI wiring vs. the real work)."""

    def __init__(self, application, options, logger):
        self.application = application
        self.options = options
        self.logger = logger

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

            document, err = docmgr.open_document_no_detach(
                self.application, scanned_model.file_path,
                audit=self.options.get("audit", False), open_all_worksets=True, logger=self.logger)
            if document is None:
                row.save_status = "Failed - could not open"
                row.errors = err
                return row

            clean_result = cleansvc.clean_document(document, self.options)
            self._apply_clean_result(row, clean_result)

            if docmgr.is_workshared(document):
                ok, detail = docmgr.synchronize_with_central(
                    document, comment=self.options.get("sync_comment", ""),
                    compact=self.options.get("compact_on_sync", False), logger=self.logger)
                row.save_status = "Synchronized with Central" if ok else "Failed - sync error"
                if not ok:
                    self._add_error(row, detail)
            else:
                ok, detail = docmgr.save_standalone(document, logger=self.logger)
                row.save_status = "Saved" if ok else "Failed - save error"
                if not ok:
                    self._add_error(row, detail)

            return row
        except Exception as e:
            row.save_status = "Failed - unexpected error"
            row.errors = str(e)
            self.logger.exception("Unexpected error cleaning file", e, file=scanned_model.file_name)
            return row
        finally:
            if document is not None and self.options.get("close_after", True):
                docmgr.close_document(document, save_modified=False, logger=self.logger)
            row.processing_time_seconds = time.time() - start

    def process_cloud(self, item, uiapp):
        row = reportgen.CleanReportRow(
            item.display_name, item.location_text, "Cloud", "Cloud Model",
            _revit_version_text(self.application))
        start = time.time()
        document = None
        try:
            # Headless + attached (DoNotDetach) so the cleanup synchronizes
            # back to the real cloud model; no UI activation, so this also
            # runs inside a background Revit. `uiapp` is kept for callers.
            document, detail = afb.open_cloud_document_attached(
                self.application, item.region, item.project_id, item.item_id, item.token,
                audit=self.options.get("audit", False))
            if document is None:
                row.save_status = "Failed - could not open"
                row.errors = detail
                return row

            clean_result = cleansvc.clean_document(document, self.options)
            self._apply_clean_result(row, clean_result)

            ok, sync_detail = docmgr.synchronize_with_central(
                document, comment=self.options.get("sync_comment", ""),
                compact=self.options.get("compact_on_sync", False), logger=self.logger)
            row.save_status = "Synchronized with Central" if ok else "Failed - sync error"
            if not ok:
                self._add_error(row, sync_detail)

            return row
        except Exception as e:
            row.save_status = "Failed - unexpected error"
            row.errors = str(e)
            self.logger.exception("Unexpected error cleaning cloud file", e, file=item.display_name)
            return row
        finally:
            if document is not None and self.options.get("close_after", True):
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
    logger = bgw.setup(ctx, "DeeWClean")
    pipeline = CleanPipeline(ctx.app, ctx.options, logger)
    if item.get("kind") == "cloud":
        cloud = CloudCleanItem(item["hub_id"], item["hub_name"], item["region"], item["project_id"],
                               item["project_name"], item["item_id"], item["display_name"], bgw.cloud_token(ctx))
        row = pipeline.process_cloud(cloud, ctx.uiapp)
    else:
        row = pipeline.process_local(bgw.local_model(ctx, item))
    ctx.log(u"  {0}{1}".format(row.save_status, (" - " + row.errors) if row.errors else ""))
    return bgw.row_to_result(row, STATUS_FIELD, SUCCESS)
