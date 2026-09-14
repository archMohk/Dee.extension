# -*- coding: utf-8 -*-
"""
Update - branded window (logo + text + website), replacing the old plain
forms.alert confirm dialog. Underlying behavior is unchanged:

Two sources, carrying exactly the same files:

GitHub - runs 'pyrevit extensions update --all' as an external process
to git-pull the latest changes for every git-based extension registered
on this PC. --all is used instead of a specific name so this works
regardless of what name someone chose at install time. Only does
anything for a copy installed with 'pyrevit extend'.

Supabase mirror - downloads the changed files directly (see
lib/dee_update_source.py). Needs no git and no access to github.com,
which is the point: on a network that blocks GitHub the first route does
not exist at all. It can also say exactly which files differ BEFORE
downloading anything, which the pyRevit updater cannot.

The window defaults to whichever route this copy actually arrived by,
because offering GitHub to a mirror install just runs a command that
quietly finds nothing to do.

Then reloads pyRevit (re-scans every extension from disk), using the
exact same API pyRevit's own built-in Reload button uses
(pyrevit.loader.sessionmgr.reload_pyrevit) - see
pyRevitCore.extension/pyRevit.tab/pyRevit.panel/tools.stack/Reload.pushbutton
for reference.

On the developer's own PC (this tool lives in a plain OneDrive-synced
folder, not a 'pyrevit extend' clone) the update step is a harmless
no-op - there is nothing registered for it to find. It also does not
rename an already-created ribbon TAB (Revit only builds tab titles once
at startup) - that specific kind of change still needs a full Revit
restart, same as pyRevit's own Reload.
"""
import os
import webbrowser

import clr
clr.AddReference("WindowsBase")
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
clr.AddReference("System")
from System import Uri
from System.Windows.Media.Imaging import BitmapImage
from System.Diagnostics import Process, ProcessStartInfo

from pyrevit import forms
import dee_branding
from pyrevit.loader import sessionmgr
import dee_telemetry
import dee_update_source as upd
dee_telemetry.check_access("Update")


_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")
_LOGO_FILE = os.path.abspath(os.path.join(_THIS_DIR, "..", "..", "..", "..", "icon.png"))
_WEBSITE_URL = "https://www.archmkd.com"


def _run_pyrevit_update():
    psi = ProcessStartInfo()
    psi.FileName = "pyrevit"
    psi.Arguments = "extensions update --all"
    psi.UseShellExecute = False
    psi.RedirectStandardOutput = True
    psi.RedirectStandardError = True
    psi.CreateNoWindow = True
    try:
        proc = Process.Start(psi)
        out = proc.StandardOutput.ReadToEnd()
        err = proc.StandardError.ReadToEnd()
        proc.WaitForExit()
        return proc.ExitCode == 0, (out + err).strip()
    except Exception as ex:
        return False, str(ex)


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


class UpdateWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.confirmed = False
        self.source = upd.SOURCE_GITHUB
        try:
            if os.path.exists(_LOGO_FILE):
                bmp = BitmapImage()
                bmp.BeginInit()
                bmp.UriSource = Uri(_LOGO_FILE)
                bmp.EndInit()
                self.logo_img.Source = bmp
        except Exception:
            pass

        # Default to the route this copy actually arrived by. Offering
        # GitHub to someone whose install has no .git would just run a
        # command that quietly finds nothing to do.
        self._root = upd.extension_root(_THIS_DIR)
        detected = upd.detect_install_source(self._root)
        if detected == upd.SOURCE_SUPABASE:
            self.source_mirror_rb.IsChecked = True
        self._describe_install(detected)

    def _describe_install(self, detected):
        if detected == upd.SOURCE_GITHUB:
            text = ("This copy is a git clone, so GitHub is the natural route. "
                    "The mirror works too and needs no GitHub access.")
        elif detected == upd.SOURCE_SUPABASE:
            commit = upd.installed_commit(self._root)
            text = ("This copy came from the Supabase mirror{0}. It is NOT a git "
                    "clone, so the GitHub route has nothing to pull - use the "
                    "mirror.".format(" (commit {0})".format(commit) if commit else ""))
        else:
            text = ("This copy is neither a git clone nor a mirror install - a "
                    "hand-copied or synced folder. The GitHub route will find "
                    "nothing to update; the mirror can still bring it up to date.")
        self.status_tb.Text = text

    def _chosen(self):
        return (upd.SOURCE_SUPABASE if self.source_mirror_rb.IsChecked is True
                else upd.SOURCE_GITHUB)

    def website_click(self, sender, args):
        webbrowser.open(_WEBSITE_URL)

    def check_click(self, sender, args):
        """Says what an update WOULD do, and changes nothing."""
        if self._chosen() == upd.SOURCE_GITHUB:
            forms.alert(
                "The GitHub route hands the work to pyRevit's own updater, "
                "which does not report in advance what it would change.\n\n"
                "Press Update Now to run it, or switch to the mirror - that "
                "one can tell you exactly which files differ before "
                "downloading anything.",
                title="Dee Update - Check")
            return

        url, bucket = upd.load_mirror_config(self._root)
        manifest, detail = upd.fetch_manifest(url, bucket)
        if manifest is None:
            forms.alert(
                "Could not read the mirror:\n\n{0}\n\nIf the mirror has "
                "never been published, that is expected - use GitHub for "
                "now.".format(detail),
                title="Dee Update - Mirror Unavailable")
            return
        plan = upd.plan_update(self._root, manifest)
        forms.alert(upd.describe_plan(plan), title="Dee Update - Check")

    def update_click(self, sender, args):
        self.confirmed = True
        self.source = self._chosen()
        self.Close()

    def cancel_click(self, sender, args):
        self.confirmed = False
        self.Close()


def _update_from_mirror(root):
    """Returns (ok, message). Never raises - a failed update must leave
    the user with a working extension and an explanation."""
    url, bucket = upd.load_mirror_config(root)
    manifest, detail = upd.fetch_manifest(url, bucket)
    if manifest is None:
        return False, "Could not read the mirror: {0}".format(detail)

    plan = upd.plan_update(root, manifest)
    if not plan["download"] and not plan["delete"]:
        return True, "Already up to date (mirror is at commit {0}).".format(
            plan.get("commit"))

    if not forms.alert("{0}\n\nDownload now?".format(upd.describe_plan(plan)),
                       title="Dee Update - Mirror", yes=True, no=True):
        return False, "Cancelled."

    with _SafeProgress(title="Dee Update - downloading from the mirror...",
                       max_value=max(1, len(plan["download"])), step=1) as pb:
        def progress(index, total, _path):
            try:
                pb.update_progress(index, max(1, total))
            except Exception:
                pass
        result = upd.apply_update(root, url, bucket, manifest, plan,
                                  on_progress=progress)

    if result["complete"]:
        return True, "Updated {0} file(s) to commit {1}.".format(
            len(result["written"]), result.get("commit"))

    # Partial: say which files, and that the version was NOT recorded, so
    # the next run will retry them rather than assume they landed.
    names = ", ".join(path for path, _why in result["failed"][:5])
    return False, (
        "{0} of {1} file(s) could not be updated: {2}{3}\n\n"
        "Nothing was recorded as installed, so running the update again "
        "will retry them. If Revit has a file open, close Revit and use "
        "the installer instead.".format(
            len(result["failed"]), len(plan["download"]), names,
            "..." if len(result["failed"]) > 5 else ""))


window = UpdateWindow(_XAML_FILE)
window.ShowDialog()

if window.confirmed:
    if window.source == upd.SOURCE_SUPABASE:
        ok, message = _update_from_mirror(upd.extension_root(_THIS_DIR))
        if not ok:
            forms.alert(message, title="Dee Update - Mirror")
        else:
            with _SafeProgress(title="Dee Update - reloading...", indeterminate=True):
                sessionmgr.reload_pyrevit()
            forms.alert(message, title="Dee Update - Mirror")
    else:
        with _SafeProgress(title="Dee Update - updating and reloading...",
                           indeterminate=True):
            ok, output = _run_pyrevit_update()
            if not ok and output:
                forms.alert(
                    "Could not pull the latest update automatically:\n\n{0}\n\n"
                    "Reloading anyway with whatever is already on this PC."
                    .format(output),
                    title="Dee Update")
            sessionmgr.reload_pyrevit()

# Reload rebuilds the ribbon in place, but some changes - a new panel,
# a moved button, ComboBox member lists - have repeatedly needed a
# genuine full Revit restart to actually show correctly (found live,
# same-day, several times this session), not just this reload. Rather
# than let every updated user rediscover that the hard way, tell them
# up front every time.
if window.confirmed:
    forms.alert(
        "Update complete.\n\n"
        "Please CLOSE Revit completely and reopen it now - some changes "
        "only take full effect after a real restart, not just a reload.",
        title="Dee Update - Restart Revit")
