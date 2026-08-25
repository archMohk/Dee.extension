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
import os

from Autodesk.Revit.DB import (
    OpenOptions, DetachFromCentralOption, ModelPathUtils, SaveAsOptions,
    WorksetConfiguration, WorksetConfigurationOption,
    TransactWithCentralOptions, SynchronizeWithCentralOptions,
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


def open_document_no_detach(application, file_path, audit=False, open_all_worksets=True, logger=None):
    """Opens a local RVT file WITHOUT detaching from its central model -
    unlike open_document() (used by DeeW.Sharing/Batch, which always
    detach since their goal is an independent NEW cloud copy), DeeW.Clean
    needs to modify and then sync changes back to the REAL shared
    central/cloud model, so detaching would defeat the purpose. Never
    raises - returns (document_or_None, error_message)."""
    try:
        model_path = ModelPathUtils.ConvertUserVisiblePathToModelPath(file_path)
        options = build_open_options(detach_option="none", audit=audit, open_all_worksets=open_all_worksets)
        document = application.OpenDocumentFile(model_path, options)
        return document, ""
    except Exception as e:
        if logger is not None:
            logger.exception("Failed to open document (no detach)", e, file=file_path)
        return None, str(e)


def synchronize_with_central(document, comment="", compact=False, logger=None):
    """Pushes changes on an already-open, non-detached workshared
    Document back to its real central model - the SAME API for a
    traditional file-share central or an ACC/BIM360 cloud-hosted
    central (Revit's own Synchronize With Central UI command works
    identically for both). Property names verified against
    revitapidocs.com's SynchronizeWithCentralOptions page before
    writing this (Comment / Compact / SaveLocalBefore / SaveLocalAfter
    / the 5 Relinquish* booleans) - not guessed.

    Relinquishes everything (matching Synchronize With Central's own
    default dialog state) so a completed DeeW.Clean run never leaves
    elements/worksets checked out under the running user's name.
    Never raises - returns (success: bool, detail: str)."""
    try:
        transact_options = TransactWithCentralOptions()
        sync_options = SynchronizeWithCentralOptions()
        sync_options.Comment = comment or "DeeW.Clean - automated batch cleanup"
        sync_options.Compact = bool(compact)
        sync_options.SaveLocalBefore = True
        sync_options.SaveLocalAfter = True
        sync_options.RelinquishBorrowedElements = True
        sync_options.RelinquishFamilyWorksets = True
        sync_options.RelinquishProjectStandardWorksets = True
        sync_options.RelinquishUserCreatedWorksets = True
        sync_options.RelinquishViewWorksets = True
        document.SynchronizeWithCentral(transact_options, sync_options)
        return True, "Synchronized with central"
    except Exception as e:
        if logger is not None:
            logger.exception("SynchronizeWithCentral failed", e)
        return False, str(e)


def save_standalone(document, logger=None):
    """Plain in-place Save for a non-workshared Standalone document -
    SynchronizeWithCentral only applies to workshared models, so this
    covers the other branch of DeeW.Clean's per-file save decision."""
    try:
        document.Save()
        return True, "Saved"
    except Exception as e:
        if logger is not None:
            logger.exception("Save failed", e)
        return False, str(e)


def open_document_best_detach(application, file_path, audit=False, logger=None):
    """Opens a LOCAL file for copying, choosing the detach mode by
    trying rather than assuming - the same reasoning as
    acc_file_browser.open_cloud_document_detached (see its docstring).

    A standalone (non-workshared) .rvt has nothing to detach from, and
    Revit rejects any detach option on it with ArgumentException
    "Detach option is not valid... Parameter name: openOptions". Since
    the closed-file scan cannot reliably tell workshared from standalone
    (see deew_model_scanner's own documented limitation), the only
    dependable approach is to attempt the preferred mode and fall back.

    Falling back to DoNotDetach is safe for the copy workflow: callers
    never call Save() and always close with save_modified=False, so the
    source file is not written to either way.

    Returns (document_or_None, detail_string) - detail names the mode
    that worked."""
    attempts = [
        ("preserve", "detached (worksets preserved)"),
        ("discard", "detached (worksets discarded)"),
        ("none", "opened attached (model is not workshared)"),
    ]
    errors = []
    for detach_option, label in attempts:
        try:
            model_path = ModelPathUtils.ConvertUserVisiblePathToModelPath(file_path)
            options = build_open_options(detach_option, audit, open_all_worksets=True)
            document = application.OpenDocumentFile(model_path, options)
            return document, label
        except Exception as e:
            errors.append("{0}: {1}".format(label, e))
            continue
    if logger is not None:
        logger.error("Could not open document in any detach mode: {0}".format(file_path))
    return None, " | ".join(errors)


def unique_target_path(folder, file_name):
    """Returns a non-colliding path inside `folder`. DeeW.Transmit must
    never silently overwrite a previously issued model, so a repeat run
    produces 'Model (2).rvt' rather than replacing 'Model.rvt'."""
    base, ext = os.path.splitext(file_name)
    if not ext:
        ext = ".rvt"
    candidate = os.path.join(folder, base + ext)
    counter = 2
    while os.path.exists(candidate):
        candidate = os.path.join(folder, "{0} ({1}){2}".format(base, counter, ext))
        counter += 1
    return candidate


def save_copy_as(document, target_path, compact=False, overwrite=False, logger=None):
    """SaveAs a DETACHED document to a new path - the core of
    DeeW.Transmit, which must never modify the source model.

    Only ever call this on a document opened with a detach option: on a
    still-attached workshared model SaveAs would repoint the local file
    at a new central, which is exactly the kind of surprise this tool
    must not spring on a shared project.

    Returns (ok, detail)."""
    try:
        folder = os.path.dirname(target_path)
        if folder and not os.path.isdir(folder):
            os.makedirs(folder)
        options = SaveAsOptions()
        options.Compact = bool(compact)
        options.OverwriteExistingFile = bool(overwrite)
        model_path = ModelPathUtils.ConvertUserVisiblePathToModelPath(target_path)
        document.SaveAs(model_path, options)
        return True, target_path
    except Exception as e:
        if logger is not None:
            logger.exception("Save copy failed", e, file=target_path)
        return False, str(e)
