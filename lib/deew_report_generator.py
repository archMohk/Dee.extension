# -*- coding: utf-8 -*-
"""
deew_report_generator
CSV / TXT / Excel report export shared by every DeeW.Cloud tool,
matching the spec's exact column list: File Name, Original Location,
Cloud Model Name, Cloud GUID, ACC Hub, ACC Project, ACC Folder,
Original Model Type, Worksharing Enabled, Upload Status, Warnings,
Errors, Processing Time, Date, Revit Version, User.

Reuses this repo's existing lib/xlsx_writer.py for the Excel export
unchanged - the same dependency-free, zipfile-only writer already used
by every other tool in this extension.

CSV export is written manually (plain comma-joining with minimal
quoting) rather than via Python's stdlib `csv` module - that module is
documented as not reliably Unicode-safe under Python 2/IronPython 2
(the engine this whole extension, including DeeW.Cloud, runs on - see
deew_cloud_service.py's module docstring for why DeeW.Cloud isn't on
CPython 3 despite originally being built for it), so a small hand-
rolled writer avoids that class of bug entirely rather than working
around it.
"""
import os
import datetime

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
            # os.environ lookup rather than getpass.getuser() - this
            # extension only ever runs on Windows (a Revit add-in), so
            # the USERNAME env var is always present and avoids any
            # getpass-module portability quirks under IronPython 2.
            self.user = os.environ.get("USERNAME", "Unknown")
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


def _csv_escape(value):
    text = "" if value is None else str(value)
    if any(ch in text for ch in (",", '"', "\n", "\r")):
        text = '"' + text.replace('"', '""') + '"'
    return text


def export_csv(path, rows, headers=None):
    headers = headers if headers is not None else REPORT_HEADERS
    with open(path, "w") as f:
        f.write(",".join(_csv_escape(h) for h in headers) + "\r\n")
        for row in rows:
            f.write(",".join(_csv_escape(v) for v in row.to_list()) + "\r\n")


def export_txt(path, rows, headers=None):
    headers = headers if headers is not None else REPORT_HEADERS
    with open(path, "w") as f:
        f.write("\t".join(headers) + "\n")
        for row in rows:
            f.write("\t".join(str(v) for v in row.to_list()) + "\n")


def export_excel(path, title, rows, headers=None, col_widths=None):
    headers = headers if headers is not None else REPORT_HEADERS
    col_widths = col_widths if col_widths is not None else _EXCEL_COL_WIDTHS
    xlsx_rows = [(row.to_list(), row.status_tag()) for row in rows]
    xlsx_writer.write_themed_xlsx(path, title, headers, col_widths, xlsx_rows)


def export(path, title, rows, headers=None, col_widths=None):
    """Picks the export format from the file extension - one entry
    point for every DeeW.Cloud tool's Export Report button.
    headers/col_widths let a caller with a different row schema
    (e.g. CleanReportRow below) reuse this same export logic instead
    of duplicating it - defaults match the original upload-oriented
    ReportRow schema for full backward compatibility with existing
    callers (DeeW.Sharing, DeeW.Batch Save to Cloud)."""
    lower = path.lower()
    if lower.endswith(".csv"):
        export_csv(path, rows, headers=headers)
    elif lower.endswith(".txt"):
        export_txt(path, rows, headers=headers)
    else:
        export_excel(path, title, rows, headers=headers, col_widths=col_widths)


CLEAN_REPORT_HEADERS = [
    "File Name", "Location", "Source", "Model Type",
    "Unused Elements Purged", "Zero-Area Rooms Deleted", "Unused Groups Deleted",
    "In-Place Families Found", "Save/Sync Status", "Warnings", "Errors",
    "Processing Time", "Date", "Revit Version", "User",
]

CLEAN_EXCEL_COL_WIDTHS = [28, 34, 10, 16, 18, 20, 18, 18, 20, 30, 30, 14, 18, 12, 16]


class CleanReportRow(object):
    """One row per file processed by DeeW.Clean - a separate schema
    from ReportRow above rather than repurposing its upload-oriented
    fields ("Cloud Model Name", "Upload Status"), since DeeW.Clean
    never uploads anything - it purges/deletes elements in place and
    then saves or synchronizes the SAME file. Shares the same to_list()
    / status_tag() duck-typed interface as ReportRow so export() above
    works unchanged for either schema."""

    def __init__(self, file_name, location, source, model_type, revit_version):
        self.file_name = file_name
        self.location = location
        self.source = source  # "Local" or "Cloud"
        self.model_type = model_type
        self.purged_count = 0
        self.zero_area_rooms_deleted = 0
        self.unused_groups_deleted = 0
        self.inplace_families_found = 0
        self.save_status = "Pending"
        self.warnings = ""
        self.errors = ""
        self.processing_time_seconds = 0.0
        self.date_text = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.revit_version = revit_version
        try:
            self.user = os.environ.get("USERNAME", "Unknown")
        except Exception:
            self.user = "Unknown"

    def to_list(self):
        return [
            self.file_name, self.location, self.source, self.model_type,
            self.purged_count, self.zero_area_rooms_deleted, self.unused_groups_deleted,
            self.inplace_families_found, self.save_status, self.warnings, self.errors,
            "{0:.1f}s".format(self.processing_time_seconds), self.date_text, self.revit_version,
            self.user,
        ]

    def status_tag(self):
        status = (self.save_status or "").lower()
        if "fail" in status or "error" in status:
            return "fail"
        if "skip" in status:
            return "skip"
        if "saved" in status or "synchron" in status or "success" in status:
            return "ok"
        return None
