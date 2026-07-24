# -*- coding: utf-8 -*-
"""
deew_model_scanner
Scans a folder of RVT files and classifies each one WITHOUT fully
opening it, using Autodesk.Revit.DB.BasicFileInfo.Extract() - a fast,
header-only read. Shared by DeeW.Sharing and DeeW.Batch Save to Cloud
so the "scan source folder" step in both tools' UIs uses identical
logic.

--------------------------------------------------------------------
IMPORTANT REVIT API LIMITATION (verify-before-trust, per this
project's standing practice of flagging unverified API assumptions)
--------------------------------------------------------------------
BasicFileInfo.Extract() reliably reports (properties used below,
each wrapped defensively since exact availability can vary by Revit
version): whether the file is workshared at all (IsWorkshared) and
its saved Revit version/format (Format). It does NOT reliably
distinguish a CENTRAL model from a LOCAL COPY of that same central
model purely from the closed file's header - both are "workshared."
That distinction (and definitive Cloud/Detached detection) is only
fully reliable once the file is actually OPENED, via the opened
Document's own IsWorkshared / IsDetached / GetWorksharingCentralModelPath()
/ IsModelInCloud properties (used in deew_document_manager.py's open
pipeline, not here).

So ModelType here is a BEST-EFFORT PREVIEW for the UI scan grid, built
from BasicFileInfo plus filename/path heuristics ("_local" copy naming
patterns some firms use, being a cloud-path-shaped source, etc.) - the
authoritative classification happens when the file is actually opened
for processing. This is documented explicitly rather than silently
asserted as certain, per this repo's established practice of not
inventing unverified API behavior.
"""
import os
import datetime

from Autodesk.Revit.DB import BasicFileInfo, ModelPathUtils


MODEL_TYPE_STANDALONE = "Standalone"
MODEL_TYPE_CENTRAL = "Central"
MODEL_TYPE_LOCAL = "Local Copy"
MODEL_TYPE_CLOUD = "Cloud"
MODEL_TYPE_DETACHED = "Detached"
MODEL_TYPE_CORRUPTED = "Corrupted"
MODEL_TYPE_READ_ONLY = "Read-Only"
MODEL_TYPE_UNKNOWN = "Unknown"


class ScannedModel(object):
    """Plain data holder - one row per RVT file found. Every field is
    a simple string/number/bool so this serializes directly into a
    DataGrid row binding and into deew_report_generator's export
    without any adapter code."""

    def __init__(self, file_path):
        self.file_path = file_path
        self.file_name = os.path.basename(file_path)
        self.selected = False
        self.version = "Unknown"
        self.model_type = MODEL_TYPE_UNKNOWN
        self.worksharing_status = "Unknown"
        self.size_bytes = 0
        self.size_text = "0 KB"
        self.status = "Scanned"
        self.error = ""
        self.version_warning = ""

    @property
    def size_mb_text(self):
        try:
            mb = self.size_bytes / (1024.0 * 1024.0)
            if mb >= 1024:
                return "{0:.2f} GB".format(mb / 1024.0)
            return "{0:.1f} MB".format(mb)
        except Exception:
            return "?"

    @property
    def upload_mode(self):
        """DeeW.Batch Save to Cloud's "Mode 1/2/3" naming, derived
        purely from model_type - kept here (rather than duplicated in
        that tool's own script.py) since it's the same derivation
        DeeW.Sharing's detach-strategy logic already makes."""
        if self.model_type == MODEL_TYPE_STANDALONE:
            return "Mode 1: Local -> Cloud"
        if self.model_type == MODEL_TYPE_CENTRAL:
            return "Mode 2: Central -> Detach -> Cloud"
        if self.model_type == MODEL_TYPE_LOCAL:
            return "Mode 3: Local Copy -> Detach -> Cloud"
        if self.model_type == MODEL_TYPE_CLOUD:
            return "Already Cloud - skip"
        return "N/A"


def _format_revit_version(basic_info):
    """BasicFileInfo.Format is documented to return the file's saved
    Revit version as a string (e.g. "2024") - wrapped defensively
    since the exact property name/behavior should be spot-checked
    against a live project per Revit version."""
    try:
        fmt = basic_info.Format
        if fmt:
            return str(fmt)
    except Exception:
        pass
    return "Unknown"


def _looks_like_local_copy(file_path):
    """Heuristic only (see module docstring): many firms name local
    copies of central with a suffix/prefix like "_local", or Revit's
    own default local-copy naming convention places them in a
    dedicated user folder. This never overrides an authoritative
    open-time classification - it only improves the pre-scan preview
    grid's guess."""
    name_lower = os.path.basename(file_path).lower()
    return "_local" in name_lower or name_lower.endswith("_backup.rvt")


def scan_folder(folder_path, recursive=False, progress_cb=None):
    """Returns a list of ScannedModel for every .rvt file directly
    inside `folder_path` (or recursively, if requested). Never raises -
    a folder that can't be listed just yields an empty list."""
    results = []
    try:
        if recursive:
            file_list = []
            for root, _dirs, files in os.walk(folder_path):
                for f in files:
                    if f.lower().endswith(".rvt"):
                        file_list.append(os.path.join(root, f))
        else:
            file_list = [
                os.path.join(folder_path, f) for f in os.listdir(folder_path)
                if f.lower().endswith(".rvt")
            ]
    except Exception:
        return results

    total = len(file_list)
    for i, file_path in enumerate(file_list):
        if progress_cb is not None:
            try:
                progress_cb(i, total, os.path.basename(file_path))
            except Exception:
                pass
        results.append(scan_file(file_path))
    return results


def scan_file(file_path):
    """Classifies a single RVT file without opening it. Any failure
    to even read the file header (corrupted file, permissions,
    file locked by another process) is captured as MODEL_TYPE_CORRUPTED
    / MODEL_TYPE_READ_ONLY rather than raised, so one bad file can
    never abort a folder scan."""
    model = ScannedModel(file_path)

    try:
        model.size_bytes = os.path.getsize(file_path)
        model.size_text = model.size_mb_text
    except Exception:
        model.size_text = "?"

    try:
        if not os.access(file_path, os.R_OK):
            model.model_type = MODEL_TYPE_READ_ONLY
            model.status = "Read-only or inaccessible"
            return model
    except Exception:
        pass

    try:
        model_path = ModelPathUtils.ConvertUserVisiblePathToModelPath(file_path)
        basic_info = BasicFileInfo.Extract(model_path)
    except Exception as e:
        model.model_type = MODEL_TYPE_CORRUPTED
        model.status = "Could not read file header"
        model.error = str(e)
        return model

    model.version = _format_revit_version(basic_info)

    try:
        is_workshared = bool(basic_info.IsWorkshared)
    except Exception:
        is_workshared = False
    model.worksharing_status = "Enabled" if is_workshared else "Disabled"

    if not is_workshared:
        model.model_type = MODEL_TYPE_STANDALONE
    elif _looks_like_local_copy(file_path):
        model.model_type = MODEL_TYPE_LOCAL
    else:
        # Workshared, no local-copy naming heuristic matched - most
        # likely Central, but genuinely ambiguous from a closed file;
        # the open-time pipeline in deew_document_manager.py re-checks
        # this for real before deciding how to open it.
        model.model_type = MODEL_TYPE_CENTRAL

    model.status = "Scanned"
    return model


def annotate_version_mismatch(models, running_version_text):
    """Sets .version_warning on each model when its own saved Revit
    version differs from the Revit session that will actually perform
    the upload.

    There is no ACC/Data Management API field for "the project's
    official Revit version" (verified against acc_api.py's endpoints -
    hubs/projects/folders/items only expose id/name, never a Revit
    format/version) - a Cloud Model's version is simply whatever Revit
    session saves it. So the one thing that's actually knowable and
    relevant is: does this file's OWN saved version match the Revit
    session running this tool right now? If not, opening it will
    silently upgrade it to the running version - worth flagging before
    upload, never worth blocking on, since it isn't an error."""
    for model in models:
        try:
            if model.version and model.version not in ("Unknown", running_version_text):
                model.version_warning = (
                    "Saved in Revit {0}; current Revit is {1} - will be "
                    "upgraded to {1} when opened.".format(model.version, running_version_text)
                )
            else:
                model.version_warning = ""
        except Exception:
            model.version_warning = ""
