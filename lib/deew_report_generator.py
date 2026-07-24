# -*- coding: utf-8 -*-
"""
deew_report_generator
CSV / TXT / Excel report export shared by every DeeW.Cloud tool,
matching the spec's exact column list: File Name, Original Location,
Cloud Model Name, Cloud GUID, ACC Hub, ACC Project, ACC Folder,
Original Model Type, Worksharing Enabled, Upload Status, Warnings,
Errors, Processing Time, Date, Revit Version, User.

Reuses this repo's existing lib/xlsx_writer.py for the Excel export
(confirmed to py_compile cleanly under Python 3 - the same dependency-
free, zipfile-only writer already used by every IronPython 2 tool in
this extension, imported here unchanged).
"""
import csv
import datetime
import getpass
import io

import xlsx_writer

REPORT_HEADERS = [
    "File Name", "Original Location", "Cloud Model Name", "Cloud GUID",
    "ACC Hub", "ACC Project", "ACC Folder", "Original Model Type",
    "Worksharing Enabled", "Upload Status", "Warnings", "Errors",
    "Processing Time", "Date", "Revit Version", "User",
]

_EXCEL_COL_WIDTHS = [26, 34, 26, 20, 18, 18, 18, 16, 16, 14, 30, 30, 14, 18, 12, 16]


class ReportRow(object):
    """One row per processed file - plain fields matching the spec's
    report columns exactly, in the same order, so building an export
    row is just `row.to_list()`."""

    def __init__(self, file_name, original_location, revit_version, model_type):
        self.file_name = file_name
        self.original_location = original_location
        self.cloud_model_name = ""
        self.cloud_guid = ""
        self.acc_hub = ""
        self.acc_project = ""
        self.acc_folder = ""
        self.original_model_type = model_type
        self.worksharing_enabled = "No"
        self.upload_status = "Pending"
        self.warnings = ""
        self.errors = ""
        self.processing_time_seconds = 0.0
        self.date_text = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.revit_version = revit_version
        try:
            self.user = getpass.getuser()
        except Exception:
            self.user = "Unknown"

    def to_list(self):
        return [
            self.file_name, self.original_location, self.cloud_model_name, self.cloud_guid,
            self.acc_hub, self.acc_project, self.acc_folder, self.original_model_type,
            self.worksharing_enabled, self.upload_status, self.warnings, self.errors,
            "{0:.1f}s".format(self.processing_time_seconds), self.date_text, self.revit_version,
            self.user,
        ]

    def status_tag(self):
        """None/"ok"/"fail"/"skip" - drives xlsx_writer's row-tint
        coloring, matching this repo's established report style."""
        status = (self.upload_status or "").lower()
        if "fail" in status or "error" in status:
            return "fail"
        if "skip" in status:
            return "skip"
        if "success" in status or "uploaded" in status or "saved" in status:
            return "ok"
        return None


def get_cloud_model_guid(document):
    """Best-effort retrieval of the newly-created cloud model's GUID
    after SaveAsCloudModel. NEEDS LIVE VERIFICATION: the exact Revit
    API call chain for reading back a cloud model's identity after
    SaveAsCloudModel is not fully confirmed - this degrades to "N/A"
    rather than raising or asserting a guessed API surface."""
    try:
        model_path = document.GetWorksharingCentralModelPath()
        if model_path is None:
            return "N/A"
        try:
            from Autodesk.Revit.DB import ModelPathUtils
            if ModelPathUtils.IsCloudPath(model_path):
                # Different Revit API versions expose the underlying
                # cloud identity differently; try the most likely
                # accessor and fall back gracefully.
                try:
                    return str(ModelPathUtils.GetModelGUID(model_path))
                except Exception:
                    return "Cloud (GUID unavailable)"
        except Exception:
            pass
        return "N/A"
    except Exception:
        return "N/A"


def export_csv(path, rows):
    with io.open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(REPORT_HEADERS)
        for row in rows:
            writer.writerow(row.to_list())


def export_txt(path, rows):
    with io.open(path, "w", encoding="utf-8") as f:
        f.write("\t".join(REPORT_HEADERS) + "\n")
        for row in rows:
            f.write("\t".join(str(v) for v in row.to_list()) + "\n")


def export_excel(path, title, rows):
    xlsx_rows = [(row.to_list(), row.status_tag()) for row in rows]
    xlsx_writer.write_themed_xlsx(path, title, REPORT_HEADERS, _EXCEL_COL_WIDTHS, xlsx_rows)


def export(path, title, rows):
    """Picks the export format from the file extension - one entry
    point for every DeeW.Cloud tool's Export Report button."""
    lower = path.lower()
    if lower.endswith(".csv"):
        export_csv(path, rows)
    elif lower.endswith(".txt"):
        export_txt(path, rows)
    else:
        export_excel(path, title, rows)
