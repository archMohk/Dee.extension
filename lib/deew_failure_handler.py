# -*- coding: utf-8 -*-
"""
deew_failure_handler
Automatic dialog and failure suppression shared by every DeeW.Cloud
tool, so a batch run of hundreds of files never blocks on a modal
prompt (missing links, missing families, upgrade notices, duplicate
types, corrupted-but-recoverable elements, save warnings, and so on).

Two independent Revit API mechanisms, both needed together:

1. UIApplication.DialogBoxShowing - fires for Revit's own native
   Win32/WinForms dialogs (the ones that appear BEFORE a Document
   even finishes opening, e.g. "Some elements could not be found -
   missing links/families", upgrade notices, worksharing/central
   model messages). The handler calls args.OverrideResult(...) with
   a Win32 message-box result code to auto-dismiss with the safest
   answer (documented per-code below) instead of letting Revit show
   the dialog modally. This is a long-established, widely-documented
   Revit API pattern (used by community batch-processing tools) -
   still flagged for live verification per this repo's practice,
   since the exact dialog id `args.DialogId` string differs across
   Revit versions and dialog types, and only a live test can confirm
   every id this project actually encounters is covered.

2. IFailuresPreprocessor (attached to a Transaction's
   FailureHandlingOptions, same proven pattern already used in
   DeeReLevel this session) - fires for FAILURE MESSAGES raised
   DURING a transaction (duplicate types, unused workset warnings,
   recoverable corruption, etc.), as opposed to the native dialogs
   DialogBoxShowing catches. Warnings are auto-resolved (deleted);
   true Errors are logged and requested to roll back rather than
   silently forced through, since forcing an Error-severity failure
   through can leave a model in a worse, inconsistent state than
   simply failing that one file and moving on to the next.
"""
from Autodesk.Revit.DB import FailureProcessingResult, FailureSeverity, IFailuresPreprocessor


# Win32 message-box result codes DialogBoxShowingEventArgs.OverrideResult
# accepts - documented here since these raw integers are otherwise
# unreadable at the call site.
IDOK = 1
IDCANCEL = 2
IDABORT = 3
IDRETRY = 4
IDIGNORE = 5
IDYES = 6
IDNO = 7
IDCLOSE = 8


# Dialog id substrings (case-insensitive) this project has identified as
# safe to auto-dismiss, each mapped to the answer that keeps a batch
# process moving. This list should be extended as new dialog ids are
# discovered during live testing - matching every dialog Revit can show
# is not something that can be fully enumerated without testing against
# real projects with missing links, missing families, etc.
_AUTO_RESOLVE_IDS = {
    # substring found in args.DialogId (lowercased)      -> result code
    "missing":            IDCLOSE,   # missing links/families notices
    "cannotfindreferences": IDCLOSE,
    "upgrade":            IDCLOSE,   # "this file will be upgraded"
    "sharedpositioning":  IDOK,      # shared coordinates notices
    "transmitteddata":    IDOK,      # transmission-data notices
    "duplicate":          IDOK,      # duplicate types/marks
    "corrupt":            IDCLOSE,   # recoverable corruption notices
    "audit":              IDOK,
    "backup":             IDCLOSE,
    "readonly":           IDCLOSE,
    "central":            IDOK,      # central-model informational messages
    "worksharing":        IDOK,
    "cloud":              IDOK,
    "save":               IDOK,
    # "dialog_revit_docwarndialog" - the warnings list Revit shows while
    # OPENING a model that has warnings in it. Found live (DeeLinkMAP's
    # log, 2026-09-13): it matched nothing above, so it fell through to
    # the unrecognized-dialog default of IDCANCEL - which on an
    # open-time dialog means "cancel the open", actively aborting the
    # very thing the batch was trying to do, file after file. OK is the
    # right answer here: acknowledge the warnings and carry on opening.
    # This affects EVERY batch tool sharing this handler, not just the
    # one it was found in.
    "docwarn":            IDOK,
}


def make_dialog_handler(logger=None):
    """Returns a function suitable for
    `uiapp.DialogBoxShowing += handler` that auto-dismisses any dialog
    whose DialogId matches a known-safe entry above, and otherwise
    defaults to IDCLOSE/Cancel for anything unrecognized (a safe
    default - "don't proceed with the modal action" is a much less
    destructive default than blindly clicking OK on something
    genuinely unexpected)."""

    def handler(sender, args):
        try:
            dialog_id = ""
            try:
                dialog_id = (args.DialogId or "").lower()
            except Exception:
                dialog_id = ""

            resolved = False
            for key, code in _AUTO_RESOLVE_IDS.items():
                if key in dialog_id:
                    try:
                        args.OverrideResult(code)
                        resolved = True
                    except Exception:
                        pass
                    break

            if not resolved:
                # Unrecognized dialog - default to a safe "cancel/close"
                # rather than risk an unintended destructive click.
                try:
                    args.OverrideResult(IDCANCEL)
                except Exception:
                    pass

            if logger is not None:
                try:
                    if resolved:
                        logger.debug("Auto-resolved dialog",
                                     dialog_id=dialog_id, resolved=True)
                    else:
                        # WARNING, not DEBUG: an unrecognized dialog means
                        # this handler guessed Cancel, which can silently
                        # break whatever the batch was doing (exactly how
                        # dialog_revit_docwarndialog went unnoticed). It
                        # needs to stand out in the log and in the per-file
                        # "Issues" line of a tool's own report, both of
                        # which key off WARNING/ERROR level.
                        logger.warning(
                            "Unrecognized dialog - answered Cancel by default; "
                            "add it to _AUTO_RESOLVE_IDS if that is wrong",
                            dialog_id=dialog_id, resolved=False)
                except Exception:
                    pass
        except Exception as e:
            if logger is not None:
                try:
                    logger.exception("Dialog handler failed", e)
                except Exception:
                    pass

    return handler


class DeeWFailuresPreprocessor(IFailuresPreprocessor):
    """Suppresses Warning-severity failures (deletes them so no modal
    pops mid-batch) and requests a rollback the moment a real
    Error-severity failure appears - never lets a corrupted/invalid
    result get force-committed just to keep a batch moving, matching
    this project's established DeeReLevel precedent."""

    def __init__(self, logger=None):
        self.logger = logger

    def PreprocessFailures(self, failuresAccessor):
        try:
            failures = list(failuresAccessor.GetFailureMessages())
        except Exception:
            return FailureProcessingResult.Continue

        had_error = False
        for f in failures:
            try:
                severity = f.GetSeverity()
            except Exception:
                continue
            if severity == FailureSeverity.Error:
                had_error = True
                if self.logger is not None:
                    try:
                        self.logger.error("Unresolvable failure", detail=f.GetDescriptionText())
                    except Exception:
                        pass
            else:
                try:
                    failuresAccessor.DeleteWarning(f)
                except Exception:
                    pass

        if had_error:
            return FailureProcessingResult.ProceedWithRollBack
        return FailureProcessingResult.Continue


def apply_to_transaction(transaction, logger=None):
    """Attaches a fresh DeeWFailuresPreprocessor to `transaction`.
    Must be called AFTER transaction.Start() - calling
    GetFailureHandlingOptions()/SetFailureHandlingOptions() before
    Start() silently fails to attach the preprocessor (confirmed the
    hard way while building DeeReLevel earlier this session)."""
    options = transaction.GetFailureHandlingOptions()
    options.SetFailuresPreprocessor(DeeWFailuresPreprocessor(logger))
    transaction.SetFailureHandlingOptions(options)
