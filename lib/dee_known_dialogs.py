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
