# -*- coding: utf-8 -*-
"""
dee_initiator_pipeline
DeeInitiator's create/upload phase (InitiatorReportRow + InitiatorPipeline),
moved out of the button script so it can also run inside a BACKGROUND
Revit (lib/dee_background + lib/deew_background_ui/_worker): copy the
party's template to <new name>.rvt in the party's output folder, open it
best-detach, enable worksharing, SaveAsCloudModel to the party's ACC
destination, keep or delete the local copy. The link phase stays in the
window (it needs the new item ids and the matches set up there).

worker(ctx, item): item = {"key", "name", "party_id", "template_path",
"party_folder", "destination": {...}}; ctx.options = {"delete_after_upload",
"token"}.
"""
import os
import time
import shutil
import datetime

import deew_document_manager as docmgr
import deew_cloud_service as cloudsvc

STATUS_FIELD = "upload_status"
SUCCESS = ("Uploaded",)


class _Party(object):
    """Plain stand-in for the window's DeliveryParty inside a worker."""
    def __init__(self, item, token):
        self.id = item.get("party_id")
        self.template_path = item.get("template_path")
        self.destination = dict(item.get("destination") or {})
        if token:
            self.destination["token"] = token


class InitiatorReportRow(object):
    def __init__(self, party_id, template_used, output_folder, new_file_name, revit_version):
        self.party_id = party_id
        self.template_used = template_used
        self.output_folder = output_folder
        self.new_file_name = new_file_name
        self.file_validation = "Validated"
        self.acc_destination_text = ""
        self.upload_status = "Pending"
        self.worksharing_enabled = "No"
        self.local_copy = ""
        self.processing_time_seconds = 0.0
        self.errors = ""
        self.date_text = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.revit_version = revit_version
        try:
            self.user = os.environ.get("USERNAME", "Unknown")
        except Exception:
            self.user = "Unknown"

    def to_list(self):
        return [
            self.party_id, self.template_used, self.output_folder, self.new_file_name,
            self.file_validation, self.acc_destination_text, self.upload_status,
            self.worksharing_enabled, self.local_copy, "{0:.1f}s".format(self.processing_time_seconds),
            self.errors, self.date_text, self.revit_version, self.user,
        ]

    def status_tag(self):
        status = (self.upload_status or "").lower()
        if "fail" in status:
            return "fail"
        if "uploaded" in status:
            return "ok"
        return None


class InitiatorPipeline(object):
    def __init__(self, application, delete_after_upload, logger):
        self.application = application
        self.delete_after_upload = delete_after_upload
        self.logger = logger

    def process_one(self, party, new_name, party_output_folder, revit_version_text):
        row = InitiatorReportRow(party.id, party.template_path, party_output_folder, new_name, revit_version_text)
        row.acc_destination_text = "{0} / {1} / {2}".format(
            party.destination.get("hub_name", "?"), party.destination.get("project_name", "?"),
            party.destination.get("folder_name", "?"))
        start = time.time()
        document = None
        target_path = None
        try:
            target_path = docmgr.unique_target_path(party_output_folder, new_name + ".rvt")
            try:
                shutil.copy2(party.template_path, target_path)
            except Exception as e:
                row.upload_status = "Failed - could not duplicate"
                row.errors = str(e)
                return row

            document, detail = docmgr.open_document_best_detach(self.application, target_path, logger=self.logger)
            if document is None:
                row.upload_status = "Failed - could not open duplicate"
                row.errors = detail
                return row

            ok = docmgr.enable_worksharing(document, logger=self.logger)
            row.worksharing_enabled = "Yes" if ok else "Failed"

            success, detail = cloudsvc.save_to_cloud(document, party.destination, new_name)
            if success:
                row.upload_status = "Uploaded"
            else:
                row.upload_status = "Failed - upload error"
                row.errors = detail
            return row
        except Exception as e:
            row.upload_status = "Failed - unexpected error"
            row.errors = str(e)
            self.logger.exception("Unexpected error processing file", e, file=new_name)
            return row
        finally:
            if document is not None:
                docmgr.close_document(document, save_modified=False, logger=self.logger)
            # Delete only ever happens here, after close, and ONLY on a
            # CONFIRMED successful upload - a failed upload always leaves
            # the local duplicate in place so nothing is silently lost.
            if target_path and row.upload_status == "Uploaded":
                if self.delete_after_upload:
                    try:
                        os.remove(target_path)
                        row.local_copy = "Deleted"
                    except Exception:
                        row.local_copy = "Kept (delete failed)"
                else:
                    row.local_copy = "Kept"
            row.processing_time_seconds = time.time() - start


# ==========================================================================
# background Revit entry point
# ==========================================================================
def worker(ctx, item):
    import deew_background_worker as bgw
    logger = bgw.setup(ctx, "DeeInitiator")
    party = _Party(item, bgw.cloud_token(ctx))
    pipeline = InitiatorPipeline(ctx.app, bool(ctx.options.get("delete_after_upload")), logger)
    try:
        version = str(ctx.app.VersionNumber)
    except Exception:
        version = "Unknown"
    can_upload, why_not = bgw.ensure_cloud_upload(ctx)
    if not can_upload:
        # Fail before duplicating/opening - the upload cannot work here.
        row = InitiatorReportRow(party.id, party.template_path, item["party_folder"], item["name"], version)
        row.upload_status = "Failed - upload error"
        row.errors = why_not
        return bgw.row_to_result(row, STATUS_FIELD, SUCCESS)
    row = pipeline.process_one(party, item["name"], item["party_folder"], version)
    ctx.log(u"  {0}{1}".format(row.upload_status, (" - " + row.errors) if row.errors else ""))
    return bgw.row_to_result(row, STATUS_FIELD, SUCCESS)
