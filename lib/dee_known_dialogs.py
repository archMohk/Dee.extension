# -*- coding: utf-8 -*-
"""
dee_known_dialogs
Open-time Revit dialogs that EVERY Dee tool must answer the same way,
checked first by each tool's DialogBoxShowing handler (DeeOpener, DeeCoord,
DeeBIMview, DeeLinkReview, DeeTransmit, lib/acc_file_browser and
lib/deew_failure_handler - the last one covers every DeeW.Cloud tool,
DeeSYNC, DeeCloseAll, DeeInitiator, DeeRelinquish, DeeLinkMAP, DeeMAPLink,
DeeSuperLINK, DeeLazy and dee_force_state).

One list, one place: a dialog found live in one tool is fixed for all of
them, instead of being re-discovered tool by tool.

Usage inside a handler:
    hit = dee_known_dialogs.answer(args)
    if hit:
        log(hit)        # human-readable line for the tool's report
        return
"""

# Win32 / TaskDialogResult codes OverrideResult accepts.
IDOK = 1
IDCANCEL = 2
IDCLOSE = 8

# (needles, answer, report line). A dialog matches when ANY needle is found
# in "<DialogId> <Message>" lowercased. Order matters - first match wins.
KNOWN_DIALOGS = [
    # "Copied Central Model" - "This central model has been copied or moved
    # from <old path> to <new path>. If you want this file to remain a
    # central model, resave the file as a central model ... If you do not
    # save the file as a central model, it becomes a local user copy
    # belonging to user <name>." Shown when opening a central that was
    # copied off the server (a local folder, an ACC download, a transmittal).
    # Found live 2026-11-06 opening E:\OLD\...\26-007-...-FAC-AR-0001.rvt
    # with DeeOpener. Its ONLY button is Close: answering OK (what
    # deew_failure_handler's "central" key used to send) or Cancel leaves it
    # sitting on screen and the batch hangs. Close is also harmless - the
    # file simply opens as a local user copy, the original central on the
    # server is never touched.
    (("copied_central", "copiedcentral", "copied central",
      "has been copied or moved", "becomes a local user copy"),
     IDCLOSE,
     "Auto-closed 'Copied Central Model' notice (central was copied/moved - "
     "opened as a local user copy)"),
    # The Revit WARNINGS dialog ("0 Errors, 2 Warnings" with << 1 of 2 >>,
    # Show / More Info / Expand, a fix button like "Delete Room(s)", OK /
    # Cancel) - e.g. "Multiple Rooms are in the same enclosed region ...
    # 'Redundant Room'". DialogId dialog_revit_docwarndialog. OK = accept
    # the warnings and carry on; never the "Delete ..." fix button, never
    # Cancel (which rolls back / aborts the open). Normally it never even
    # shows, because the FailuresProcessing hook below clears warnings
    # first; this is the backstop.
    (("docwarn", "multiple rooms are in the same enclosed region"),
     IDOK,
     "Auto-accepted Revit warnings dialog (OK)"),
]


def _text_of(args):
    dialog_id = ""
    message = ""
    try:
        dialog_id = args.DialogId or ""
    except Exception:
        pass
    try:
        message = args.Message or ""
    except Exception:
        pass
    return (dialog_id + " " + message).lower()


def match(args, text=None):
    """(answer_code, report_line) for a known dialog, else None."""
    if text is None:
        text = _text_of(args)
    else:
        text = text.lower()
    for needles, code, line in KNOWN_DIALOGS:
        for needle in needles:
            if needle in text:
                return code, line
    return None


def answer(args, text=None):
    """Answers a known dialog in place. Returns its report line, or None when
    the dialog is not one of ours (the caller then runs its own rules)."""
    hit = match(args, text)
    if hit is None:
        return None
    code, line = hit
    try:
        args.OverrideResult(code)
    except Exception:
        return None
    return line


# --------------------------------------------------------------------------
# Warnings raised while a Dee tool is running (FailuresProcessing)
# --------------------------------------------------------------------------
# DialogBoxShowing alone is not enough for the warnings dialog: Revit shows it
# from failure processing, at the end of a transaction or while a model is
# opened/regenerated. Application.FailuresProcessing fires just before that,
# so Warning-severity failures are deleted there (= "OK", the model keeps the
# elements and the warnings stay listed under Review Warnings). Errors are
# left to Revit's normal handling - forcing an Error through can corrupt a
# model; a tool that wants errors rolled back already uses
# deew_failure_handler's IFailuresPreprocessor.
#
# Attached next to every tool's DialogBoxShowing handler and removed with it,
# so it is only active while a Dee tool runs - never for the user's own work.
# The delegate and a ref-count live in the AppDomain, not in this module:
# pyRevit can give each command a fresh copy of this module, and detach must
# find the exact delegate attach subscribed.
_SLOT = "dee_known_dialogs.failures"


def _on_failures_processing(sender, args):
    try:
        from Autodesk.Revit.DB import FailureSeverity
        accessor = args.GetFailuresAccessor()
        for failure in list(accessor.GetFailureMessages()):
            try:
                if failure.GetSeverity() == FailureSeverity.Warning:
                    accessor.DeleteWarning(failure)
            except Exception:
                pass
    except Exception:
        pass


def _application(uiapp):
    try:
        return uiapp.Application
    except Exception:
        return uiapp


def attach_failures(uiapp):
    """Start auto-accepting warnings. Never raises."""
    try:
        import System
        from System import EventHandler
        from Autodesk.Revit.DB.Events import FailuresProcessingEventArgs
        domain = System.AppDomain.CurrentDomain
        slot = domain.GetData(_SLOT)
        if slot is not None:
            slot[1] = slot[1] + 1
            return
        delegate = EventHandler[FailuresProcessingEventArgs](_on_failures_processing)
        _application(uiapp).FailuresProcessing += delegate
        domain.SetData(_SLOT, [delegate, 1])
    except Exception:
        pass


def detach_failures(uiapp):
    """Stop auto-accepting warnings once the last attached tool is done.
    Never raises."""
    try:
        import System
        domain = System.AppDomain.CurrentDomain
        slot = domain.GetData(_SLOT)
        if slot is None:
            return
        slot[1] = slot[1] - 1
        if slot[1] > 0:
            return
        domain.SetData(_SLOT, None)
        _application(uiapp).FailuresProcessing -= slot[0]
    except Exception:
        pass
