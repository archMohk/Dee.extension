# -*- coding: utf-8 -*-
"""
deew_document_manager
Revit Document lifecycle helpers shared by every DeeW.Cloud tool:
open (with the correct detach/audit/workset strategy), enable
worksharing, compact+save, close+dispose. Every function here handles
ONE file at a time and is meant to be called in a tight loop without
ever holding more than one Document open simultaneously - required
for processing "hundreds of models" without exhausting memory (spec:
"Never keep multiple Revit documents open simultaneously... Release
memory after each document").

Revit API facts relied on here (verified against official
documentation before writing, not guessed):
  Application.OpenDocumentFile(ModelPath, OpenOptions) -> Document
  OpenOptions.DetachFromCentralOption (enum: DoNotDetach,
      DetachAndPreserveWorksets, DetachAndDiscardWorksets)
  OpenOptions.Audit (bool)
  OpenOptions.SetOpenWorksetsConfiguration(WorksetConfiguration)
  Document.EnableWorksharing(string gridsAndLevelsWorksetName,
      string otherElementsWorksetName) - the exact two default names
      Revit's own "Enable Worksharing" UI command uses are
      "Shared Levels and Grids" and "Workset1", matching this
      package's spec verbatim.
  Document.IsWorkshared (property on an OPEN document - the only
      fully reliable way to tell Central from Local Copy from
      Detached; see deew_model_scanner.py's docstring for why the
      closed-file pre-scan can't do this reliably on its own)
  SaveAsOptions.Compact (bool)
  Document.Close(bool saveModified)

NEEDS LIVE VERIFICATION (flagged, not silently assumed correct):
  - Document.IsModelInCloud - used to detect an already-cloud file so
    it can be skipped per spec ("If Cloud: Skip"). Property name/
    availability should be spot-checked per Revit version.
  - WorksetConfigurationOption.OpenAllWorksets - used for the "Open
    All Worksets" processing option.
  - Whether Document.EnableWorksharing() needs to run inside an open
    Transaction or is a document-level call made outside one (written
    here as a call made OUTSIDE a Transaction, matching the majority
    of Revit API examples seen for this specific method - if wrong,
    this is a one-line fix to wrap it).
"""
from Autodesk.Revit.DB import (
    OpenOptions, DetachFromCentralOption, ModelPathUtils, SaveAsOptions,
    WorksetConfiguration, WorksetConfigurationOption,
)

_DEFAULT_GRIDS_LEVELS_WORKSET = "Shared Levels and Grids"
_DEFAULT_OTHER_WORKSET = "Workset1"


def build_open_options(detach_option="preserve", audit=False, open_all_worksets=False):
    """detach_option: "none" / "preserve" / "discard" - maps to
    DetachFromCentralOption. Returns a ready-to-use OpenOptions."""
    options = OpenOptions()
    mapping = {
        "none": DetachFromCentralOption.DoNotDetach,
        "preserve": DetachFromCentralOption.DetachAndPreserveWorksets,
        "discard": DetachFromCentralOption.DetachAndDiscardWorksets,
    }
    options.DetachFromCentralOption = mapping.get(detach_option, DetachFromCentralOption.DoNotDetach)
    options.Audit = bool(audit)
    if open_all_worksets:
        try:
            config = WorksetConfiguration(WorksetConfigurationOption.OpenAllWorksets)
            options.SetOpenWorksetsConfiguration(config)
        except Exception:
            pass
    return options


def open_document(application, file_path, detach_option="preserve", audit=False,
                   open_all_worksets=False, logger=None):
    """Opens a single local RVT file with the given strategy. Never
    raises - returns (document_or_None, error_message)."""
    try:
        model_path = ModelPathUtils.ConvertUserVisiblePathToModelPath(file_path)
        options = build_open_options(detach_option, audit, open_all_worksets)
        document = application.OpenDocumentFile(model_path, options)
        return document, ""
    except Exception as e:
        if logger is not None:
            logger.exception("Failed to open document", e, file=file_path)
        return None, str(e)


def is_cloud_model(document):
    """See NEEDS LIVE VERIFICATION above re: IsModelInCloud."""
    try:
        return bool(document.IsModelInCloud)
    except Exception:
        return False


def is_workshared(document):
    try:
        return bool(document.IsWorkshared)
    except Exception:
        return False


def enable_worksharing(document, logger=None):
    """Enables worksharing using Revit's own default workset names
    ("Shared Levels and Grids" / "Workset1"), matching Revit's own
    "Enable Worksharing" UI command exactly. No-ops (returns True) if
    already workshared - EnableWorksharing on an already-workshared
    document would raise, so this checks first rather than relying on
    a try/except to swallow that distinction silently."""
    try:
        if is_workshared(document):
            return True
        document.EnableWorksharing(_DEFAULT_GRIDS_LEVELS_WORKSET, _DEFAULT_OTHER_WORKSET)
        return True
    except Exception as e:
        if logger is not None:
            logger.exception("EnableWorksharing failed", e)
        return False


def compact_and_save_local(document, logger=None):
    """Local SaveAs-in-place with Compact=True - used when the spec's
    "Compact" option is requested independent of the cloud upload
    itself. Not required before SaveAsCloudModel (which has no
    separate compact flag) - offered as its own step since the spec
    lists Compact as an independent processing option."""
    try:
        options = SaveAsOptions()
        options.Compact = True
        current_path = document.PathName
        if not current_path:
            return False
        document.SaveAs(current_path, options)
        return True
    except Exception as e:
        if logger is not None:
            logger.exception("Compact/save failed", e)
        return False


def close_document(document, save_modified=False, logger=None):
    """Closes and releases a Document - always call this before
    moving to the next file (spec: "Never keep multiple Revit
    documents open simultaneously... Release memory after each
    document... Dispose every API object properly")."""
    try:
        document.Close(save_modified)
        return True
    except Exception as e:
        if logger is not None:
            logger.exception("Failed to close document", e)
        return False
