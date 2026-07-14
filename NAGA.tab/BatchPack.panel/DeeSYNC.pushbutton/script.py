from Autodesk.Revit.DB import *
from pyrevit import forms, script

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
        sync_opts = SynchronizeWithCentralOptions()
        sync_opts.Comment = "Batch sync - all open models"
        sync_opts.SetRelinquishOptions(RelinquishOptions(True))

        doc.SynchronizeWithCentral(trans_opts, sync_opts)
        results.append("{} - OK".format(doc.Title))
        docs_to_close.append(doc)

    except Exception as e:
        results.append("{} - FAILED: {}".format(doc.Title, str(e)))

print("\n".join(results))

if close_after_sync:
    for doc in docs_to_close:
        try:
            doc.Close(False)
        except Exception as e:
            print("Failed to close {}: {}".format(doc.Title, str(e)))
