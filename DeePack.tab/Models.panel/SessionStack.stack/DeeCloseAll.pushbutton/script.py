# -*- coding: utf-8 -*-
"""
DeeCloseAll
Lists every currently open Revit document as a checklist so you pick which
ones to process, then applies one of three closing modes to all of them:

  Synchronize & Close        - pushes changes to central (or saves local
                                files), relinquishes, then closes
  Relinquish & Close          - releases your worksets/borrowed elements
                                WITHOUT pushing changes - unsaved local
                                changes are LOST - then closes
  Close without Relinquish    - closes as-is; your worksets/elements stay
                                checked out under your name until someone
                                else deals with it or you reopen later

The currently active document can't be closed synchronously (Revit forbids
Document.Close() on the active document), so its close is deferred via
PostCommand and completes right after this tool exits.
"""
from pyrevit import forms, script
from Autodesk.Revit.DB import (
    RelinquishOptions, SynchronizeWithCentralOptions, TransactWithCentralOptions,
    WorksharingUtils
)
from Autodesk.Revit.UI import RevitCommandId, PostableCommand
import deew_failure_handler as ffh
import deew_logger
import dee_telemetry
dee_telemetry.check_access("DeeCloseAll")


output = script.get_output()


def _doc_label(doc):
    try:
        return doc.Title
    except Exception:
        return str(doc)


def _close(doc, is_active):
    """Returns True if closed now, None if deferred (active document)."""
    if is_active:
        return None
    doc.Close(False)
    return True


def _transact_options(logger):
    """A TransactWithCentralOptions with a failure preprocessor attached
    so a checked-out element or other sync-time conflict resolves itself
    (or fails that one document) instead of popping a native dialog no
    one is present to click in this batch - same fix as DeeSuperLINK's
    live "crashing" report."""
    options = TransactWithCentralOptions()
    try:
        options.SetFailuresPreprocessor(ffh.DeeWFailuresPreprocessor(logger))
    except Exception:
        pass  # not supported on this API surface - falls back to old behavior
    return options


def _sync_and_close(doc, is_active, logger=None):
    if doc.IsWorkshared:
        relinquish = RelinquishOptions(True)
        swc_options = SynchronizeWithCentralOptions()
        swc_options.SetRelinquishOptions(relinquish)
        swc_options.Comment = "Auto-sync by DeeCloseAll"
        swc_options.SaveLocalBefore = True
        doc.SynchronizeWithCentral(_transact_options(logger), swc_options)
    else:
        doc.Save()
    return _close(doc, is_active)


def _relinquish_and_close(doc, is_active, logger=None):
    if doc.IsWorkshared:
        WorksharingUtils.RelinquishOwnership(
            doc, RelinquishOptions(True), _transact_options(logger))
    return _close(doc, is_active)


def _close_only(doc, is_active):
    return _close(doc, is_active)


class _SafeProgress(object):
    """forms.ProgressBar tries to set Window.TaskbarItemInfo on its host
    window - a genuine WPF-level bug (not this extension's code):
    Window.TaskbarItemInfo throws NotImplementedException whenever the
    underlying ITaskbarList::HrInit COM call fails, which is documented
    to happen specifically under Remote Desktop/Terminal Services or a
    custom shell without a taskbar (live-confirmed in DeeSheetLinks).

    Wraps the real forms.ProgressBar and falls back to running with NO
    progress UI at all if entering it fails, so the tool degrades
    gracefully under RDP instead of crashing - everyone else still gets
    the real progress bar exactly as before. `pb.update_progress(...)`/
    `pb.cancelled` are safe no-ops in the fallback case, so callers never
    need an extra branch."""
    def __init__(self, **kwargs):
        self._kwargs = kwargs
        self._real = None

    def __enter__(self):
        try:
            self._real = forms.ProgressBar(**self._kwargs)
            return self._real.__enter__()
        except Exception:
            self._real = None
            return self

    def __exit__(self, exc_type, exc_value, tb):
        if self._real is not None:
            return self._real.__exit__(exc_type, exc_value, tb)
        return False

    @property
    def cancelled(self):
        return False

    def update_progress(self, i, total):
        pass


def main():
    uiapp = __revit__
    # Application.Documents includes every linked RVT loaded in memory
    # alongside the host files, not just the ones actually opened - those
    # show up with IsLinked=True and aren't something you can/should
    # independently sync, relinquish, or close.
    docs = [d for d in uiapp.Application.Documents if not d.IsLinked]
    if not docs:
        forms.alert("No open Revit documents found.")
        return

    active_title = None
    try:
        active_title = uiapp.ActiveUIDocument.Document.Title
    except Exception:
        pass

    labels = [_doc_label(d) for d in docs]
    doc_lookup = dict(zip(labels, docs))

    selected_labels = forms.SelectFromList.show(
        sorted(labels),
        title="DeeCloseAll — Select open document(s) to process ({0} open)".format(len(docs)),
        multiselect=True,
        button_name="Process Selected"
    )
    if not selected_labels:
        return

    mode = forms.CommandSwitchWindow.show(
        ["Synchronize & Close",
         "Relinquish & Close (unsaved changes lost)",
         "Close without Relinquish"],
        message="Choose how to close the selected document(s):"
    )
    if not mode:
        return

    confirm = forms.alert(
        "This will process {0} open document(s) using '{1}'.\n\n{2}\n\n"
        "Continue?".format(
            len(selected_labels), mode,
            "Changes will be pushed to central (or saved) before closing."
            if mode == "Synchronize & Close" else
            "Any unsynced local changes will be LOST."),
        title="DeeCloseAll — Confirm",
        options=["Continue", "Cancel"]
    )
    if confirm != "Continue":
        return

    results = []
    active_pending_label = None
    total = len(selected_labels)

    logger = deew_logger.DeeWLogger("DeeCloseAll")
    # Wired for the whole batch so a native dialog fired by ANY document
    # (e.g. an ownership conflict during sync) is auto-resolved instead
    # of hanging Revit indefinitely - same fix as DeeSuperLINK's live
    # "crashing" report.
    dialog_handler = ffh.make_dialog_handler(logger)
    try:
        __revit__.DialogBoxShowing += dialog_handler
    except Exception:
        pass

    try:
        with _SafeProgress(title="DeeCloseAll — starting...", cancellable=True) as pb:
            for i, label in enumerate(selected_labels):
                if pb.cancelled:
                    results.append((label, "Cancelled", None))
                    break
                pb.update_progress(i, total)
                pb.title = "Processing {0}/{1}: {2}".format(i + 1, total, label)
                output.print_md("**Processing {0}/{1}: {2}**".format(i + 1, total, label))

                doc = doc_lookup[label]
                is_active = (active_title is not None and label == active_title)

                try:
                    if mode == "Synchronize & Close":
                        closed = _sync_and_close(doc, is_active, logger)
                    elif mode.startswith("Relinquish"):
                        closed = _relinquish_and_close(doc, is_active, logger)
                    else:
                        closed = _close_only(doc, is_active)

                    if closed is None:
                        active_pending_label = label
                        results.append((label, "Processed - close deferred (active document)", None))
                    else:
                        results.append((label, "Done ({0}, closed)".format(mode), True))
                except Exception as e:
                    results.append((label, "FAILED (skipped): {0}".format(e), False))
    finally:
        try:
            __revit__.DialogBoxShowing -= dialog_handler
        except Exception:
            pass

    if active_pending_label is not None:
        try:
            cmd_id = RevitCommandId.LookupPostableCommandId(PostableCommand.Close)
            if uiapp.CanPostCommand(cmd_id):
                uiapp.PostCommand(cmd_id)
                results.append((active_pending_label,
                                "Close requested (completes after this tool exits)", None))
            else:
                results.append((active_pending_label,
                                "Close FAILED: command not postable right now", False))
        except Exception as e:
            results.append((active_pending_label, "Close FAILED: {0}".format(e), False))

    html = '<h2 style="font-family:sans-serif;color:#ddd;">DeeCloseAll Results</h2>'
    for label, detail, ok in results:
        bg = "#2e7d32" if ok else ("#455a64" if ok is None else "#c62828")
        icon = "&#10003;" if ok else ("&#9888;" if ok is None else "&#10007;")
        html += (
            '<div style="padding:8px 14px;margin:3px 0;background:{0};color:#fff;'
            'border-radius:4px;font-family:monospace;font-size:13px;">'
            '<b>{1}</b>&nbsp; {2} &mdash; {3}'
            '</div>'.format(bg, icon, label, detail)
        )
    output.print_html(html)


main()
