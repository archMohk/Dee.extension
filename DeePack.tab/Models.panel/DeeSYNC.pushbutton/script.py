from Autodesk.Revit.DB import *
from pyrevit import forms, script
import dee_telemetry
dee_telemetry.check_access("DeeSYNC")
import deew_failure_handler as ffh
import deew_logger



from dee_ui import SafeProgress as _SafeProgress


app = __revit__.Application

options = ["Sync Only", "Sync and Close"]
choice = forms.CommandSwitchWindow.show(
    options,
    message="Select an action for all open workshared models:"
)

if not choice:
    script.exit()

close_after_sync = choice == "Sync and Close"

results = []
docs_to_close = []

logger = deew_logger.DeeWLogger("DeeSYNC")
# Sync-with-central across several open models, unattended (Sync and Close
# runs the whole list without the user watching each one) - a checked-out
# element ("cannot be ignored... resaves to central") or any other native
# dialog fired mid-sync would otherwise hang Revit indefinitely on a batch
# no one is present to click through. Same risk category, same fix, as
# DeeSuperLINK's live "crashing" report.
dialog_handler = ffh.make_dialog_handler(logger)
try:
    __revit__.DialogBoxShowing += dialog_handler
except Exception:
    pass

try:
    with _SafeProgress(title="DeeSYNC - synchronizing open models...", indeterminate=True):
        for doc in app.Documents:

            # Skip families and non-workshared docs (and links, which appear as documents too)
            if doc.IsFamilyDocument:
                continue

            if doc.IsLinked:
                continue

            if not doc.IsWorkshared:
                results.append("{} - SKIPPED (not workshared)".format(doc.Title))
                continue

            try:
                trans_opts = TransactWithCentralOptions()
                try:
                    trans_opts.SetFailuresPreprocessor(ffh.DeeWFailuresPreprocessor(logger))
                except Exception:
                    pass  # not supported on this API surface - falls back to old behavior
                sync_opts = SynchronizeWithCentralOptions()
                sync_opts.Comment = "Batch sync - all open models"
                sync_opts.SetRelinquishOptions(RelinquishOptions(True))

                doc.SynchronizeWithCentral(trans_opts, sync_opts)
                results.append("{} - OK".format(doc.Title))
                docs_to_close.append(doc)

            except Exception as e:
                results.append("{} - FAILED (skipped): {}".format(doc.Title, str(e)))
finally:
    try:
        __revit__.DialogBoxShowing -= dialog_handler
    except Exception:
        pass

print("\n".join(results))

if close_after_sync:
    # Revit refuses Document.Close() on the ACTIVE document - skip it and
    # say so instead of reporting a confusing failure (DeeCloseAll has the
    # deferred-close dance for that case; here it is simply left open).
    try:
        active_doc = __revit__.ActiveUIDocument.Document
    except Exception:
        active_doc = None
    for doc in docs_to_close:
        if active_doc is not None and doc.Equals(active_doc):
            print("{} - left open (it is the active document; close it from "
                  "Revit or use DeeCloseAll)".format(doc.Title))
            continue
        try:
            doc.Close(False)
        except Exception as e:
            print("Failed to close {}: {}".format(doc.Title, str(e)))
