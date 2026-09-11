from Autodesk.Revit.DB import *
from pyrevit import forms, script
import dee_telemetry
dee_telemetry.check_access("DeeSYNC")



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
