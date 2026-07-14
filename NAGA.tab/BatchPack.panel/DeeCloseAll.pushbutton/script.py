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


def _sync_and_close(doc, is_active):
    if doc.IsWorkshared:
        relinquish = RelinquishOptions(True)
        swc_options = SynchronizeWithCentralOptions()
        swc_options.SetRelinquishOptions(relinquish)
        swc_options.Comment = "Auto-sync by DeeCloseAll"
        swc_options.SaveLocalBefore = True
        doc.SynchronizeWithCentral(TransactWithCentralOptions(), swc_options)
    else:
        doc.Save()
    return _close(doc, is_active)


def _relinquish_and_close(doc, is_active):
    if doc.IsWorkshared:
        WorksharingUtils.RelinquishOwnership(
            doc, RelinquishOptions(True), TransactWithCentralOptions())
    return _close(doc, is_active)


def _close_only(doc, is_active):
    return _close(doc, is_active)


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

    with forms.ProgressBar(title="DeeCloseAll — starting...", cancellable=True) as pb:
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
                    closed = _sync_and_close(doc, is_active)
                elif mode.startswith("Relinquish"):
                    closed = _relinquish_and_close(doc, is_active)
                else:
                    closed = _close_only(doc, is_active)

                if closed is None:
                    active_pending_label = label
                    results.append((label, "Processed - close deferred (active document)", None))
                else:
                    results.append((label, "Done ({0}, closed)".format(mode), True))
            except Exception as e:
                results.append((label, "FAILED: {0}".format(e), False))

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
