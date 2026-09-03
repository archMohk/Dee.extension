# -*- coding: utf-8 -*-
"""
DeeRelinquish (Cloud)
Scans an ACC project's Revit models to show who owns which worksets -
WITHOUT opening any of them - then releases everything owned by the
current user in the models you pick.

All scan / relinquish logic lives in lib/dee_relinquish_service.py; this
file only wires the WPF window to it.

context: zero-doc - the whole point is cleaning up ownership without
having to open anything first.

--------------------------------------------------------------------
The one thing this tool deliberately cannot do
--------------------------------------------------------------------
Release ANOTHER user's ownership. The Revit API documentation for
WorksharingUtils.RelinquishOwnership states plainly that "Elements and
worksets owned by other users are ignored", and no ACC/APS endpoint for it
was found. Rather than quietly do nothing, other people's holds are listed
by model / workset / owner and the user is pointed at the only real route:
Collaborate > Manage Model > Manage Cloud Models. Agreed with the user
before this was built.

Reuses the machinery already proven by DeeSuperLINK and DeeW.Cloud:
acc_auth / acc_file_browser for hub-project-file browsing and for opening
a cloud model ATTACHED (a detached model has no central to relinquish to),
and the in-window progress bar with the DispatcherFrame pump.
"""
import os
import time

from pyrevit import forms, script
import dee_branding

import clr
clr.AddReference("PresentationFramework")
clr.AddReference("WindowsBase")
from System import Action
from System.Windows import Visibility
from System.Windows.Threading import Dispatcher, DispatcherFrame, DispatcherPriority

import acc_auth
import acc_file_browser as afb
import deew_document_manager as docmgr
import dee_relinquish_service as core

output = script.get_output()

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")
_CACHE_FILE = os.path.join(_THIS_DIR, "acc_cache.json")

OWN_FILTERS = [
    ("Everything", "all"),
    ("Only mine", "mine"),
    ("Only other people's", "others"),
    ("Only free worksets", "free"),
]


def format_duration(seconds):
    seconds = int(max(0, seconds))
    if seconds < 60:
        return "{0}s".format(seconds)
    return "{0}m {1:02d}s".format(seconds // 60, seconds % 60)


class DeeRelinquishWindow(dee_branding.DeeBrandedWindow):
    # Must exist BEFORE the base class loads the XAML - loading it fires the
    # TextChanged/SelectionChanged handlers, when no instance attribute exists.
    _ready = False

    def __init__(self, xaml_file, uiapp):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.uiapp = uiapp
        self.app = uiapp.Application

        self._token = None
        self._hub_id = None
        self._region = None
        self._project_id = None
        self._all_items = {}
        self._model_rows = []
        self._ownership_rows = []
        self._prog_total = 1
        self._prog_done = 0
        self._prog_start = time.time()

        try:
            self.current_user = self.app.Username
        except Exception:
            self.current_user = ""
        self.user_tb.Text = "Signed in as: {0}".format(self.current_user or "(unknown)")

        self.own_filter_cb.Items.Clear()
        for label, _key in OWN_FILTERS:
            self.own_filter_cb.Items.Add(label)
        self.own_filter_cb.SelectedIndex = 0

        self._ready = True
        self._update_summaries()

    # ---------------- guard ----------------
    def _guard(self, fn, *args):
        """WPF swallows exceptions raised inside an event handler, which makes a
        real failure look exactly like 'the button does nothing'."""
        try:
            return fn(*args)
        except Exception as e:
            import traceback
            self.status_tb.Text = "ERROR: {0}".format(e)
            self._log("ERROR: {0}".format(e))
            forms.alert("DeeRelinquish hit an error:\n\n{0}\n\n{1}".format(
                e, traceback.format_exc()[-900:]), title="DeeRelinquish")

    def _log(self, line):
        try:
            self.log_tb.AppendText(line + "\r\n")
            self.log_tb.ScrollToEnd()
        except Exception:
            pass
        self._pump()

    # ---------------- project ----------------
    def pick_project_click(self, sender, args):
        self._guard(self._pick_project)

    def _pick_project(self):
        try:
            self._token = acc_auth.get_access_token()
        except Exception as e:
            forms.alert("Authentication failed:\n{0}".format(e), title="DeeRelinquish")
            return
        hub = afb.pick_hub(self._token)
        if not hub:
            return
        hub_id, region, hub_name = hub
        project = afb.pick_project(hub_id, self._token)
        if not project:
            return
        project_id, project_name = project
        # hub_id is required by list_project_files' fresh-scan path
        # (acc_api.get_top_folders) - only the cached path works without it.
        self._hub_id = hub_id
        self._region = region
        self._project_id = project_id
        self.project_tb.Text = "{0} / {1}".format(hub_name, project_name)

        with forms.ProgressBar(title="DeeRelinquish - loading project files...",
                               indeterminate=True):
            self._all_items = afb.list_project_files(
                self._hub_id, self._project_id, self._token, _CACHE_FILE) or {}

        self._model_rows = [core.ModelRow(name, self._all_items[name])
                            for name in sorted(self._all_items.keys())]
        self._ownership_rows = []
        self._refresh_models()
        self._refresh_ownership()
        self._log("{0} Revit file(s) found in '{1}'.".format(
            len(self._model_rows), project_name))
        self.status_tb.Text = ("{0} model(s) found. Click Scan Ownership - it reads "
                               "them without opening anything.".format(len(self._model_rows)))

    # ---------------- scan ----------------
    def scan_click(self, sender, args):
        self._guard(self._scan)

    def _scan(self):
        if not self._model_rows:
            forms.alert("Pick a hub and project first.", title="DeeRelinquish")
            return
        self._ownership_rows = []
        self._log("=" * 70)
        self._log("Scanning ownership of {0} model(s) - nothing is opened.".format(
            len(self._model_rows)))

        self._progress_begin(len(self._model_rows))
        try:
            for row in self._model_rows:
                self._progress_render(row.name)
                row.scanned = True
                row.scan_error = ""
                row.mine = row.others = row.free = 0
                row.other_owners = []

                model_path, detail = afb.cloud_model_path(
                    self._region, self._project_id, row.item_id, self._token)
                if model_path is None:
                    row.scan_error = detail
                    self._log("  ?    {0} - {1}".format(row.name, detail))
                    self._progress_done_one()
                    continue

                previews, error = core.scan_model(model_path, self.current_user)
                if previews is None:
                    row.scan_error = error
                    self._log("  ?    {0} - {1}".format(row.name, error))
                    self._progress_done_one()
                    continue

                row.mine, row.others, row.free, row.other_owners = \
                    core.summarize_worksets(previews, self.current_user)
                for preview in previews:
                    try:
                        owner = (preview.Owner or "").strip()
                        name = preview.Name
                    except Exception:
                        continue
                    self._ownership_rows.append(core.OwnershipRow(
                        row.name, row.item_id, name, owner,
                        core.same_user(owner, self.current_user)))
                self._log("  OK   {0} - {1}".format(row.name, row.status))
                self._progress_done_one()
        finally:
            self._progress_end()

        mine_models = len([r for r in self._model_rows if r.mine])
        for row in self._model_rows:
            row.selected = row.mine > 0
        self._refresh_models()
        self._refresh_ownership()
        self._log("-" * 70)
        self._log("Scan done. You hold worksets in {0} model(s).".format(mine_models))
        self.status_tb.Text = ("Scan done - you hold worksets in {0} model(s), and "
                               "those are ticked for you.".format(mine_models))

    # ---------------- models tab ----------------
    def _visible_models(self):
        query = ""
        try:
            query = self.model_search_tb.Text or ""
        except Exception:
            pass
        return [r for r in self._model_rows if core.matches(r.haystack, query)]

    def _refresh_models(self):
        shown = self._visible_models()
        self.models_grid.ItemsSource = None
        self.models_grid.ItemsSource = shown
        self._update_summaries()
        return shown

    def model_search_changed(self, sender, args):
        if not self._ready:
            return
        self._guard(self._refresh_models)

    def model_toggle_click(self, sender, args):
        def run():
            row = sender.DataContext
            if row is None:
                return
            row.selected = sender.IsChecked is True
            self._update_summaries()
        self._guard(run)

    def _set_models(self, rows, value):
        for row in rows:
            row.selected = value
        self._refresh_models()

    def models_all_click(self, sender, args):
        self._guard(self._set_models, self._model_rows, True)

    def models_none_click(self, sender, args):
        self._guard(self._set_models, self._model_rows, False)

    def models_mine_click(self, sender, args):
        def run():
            for row in self._model_rows:
                row.selected = row.mine > 0
            self._refresh_models()
        self._guard(run)

    # ---------------- ownership tab ----------------
    def _refresh_ownership(self):
        query = ""
        try:
            query = self.own_search_tb.Text or ""
        except Exception:
            pass
        idx = self.own_filter_cb.SelectedIndex
        key = OWN_FILTERS[idx][1] if 0 <= idx < len(OWN_FILTERS) else "all"

        rows = []
        for row in self._ownership_rows:
            if not core.matches(row.haystack, query):
                continue
            if key == "mine" and not row.is_mine:
                continue
            if key == "others" and (row.is_mine or not row.owner):
                continue
            if key == "free" and row.owner:
                continue
            rows.append(row)
        self.ownership_grid.ItemsSource = None
        self.ownership_grid.ItemsSource = rows

        mine = len([r for r in self._ownership_rows if r.is_mine])
        others = len([r for r in self._ownership_rows if r.owner and not r.is_mine])
        self.own_summary_tb.Text = (
            "{0} workset(s) scanned - {1} yours, {2} held by other people. "
            "Showing {3}.".format(len(self._ownership_rows), mine, others, len(rows)))
        return rows

    def own_filter_changed(self, sender, args):
        if not self._ready:
            return
        self._guard(self._refresh_ownership)

    # ---------------- shared ----------------
    def _selected_models(self):
        return [r for r in self._model_rows if r.selected]

    def _flags(self):
        flags = {}
        for name, _label, _help in core.CATEGORY_FLAGS:
            cb = getattr(self, "cat_{0}_cb".format(name), None)
            flags[name] = cb is not None and cb.IsChecked is True
        return flags

    def _update_summaries(self):
        picked = self._selected_models()
        with_mine = len([r for r in picked if r.mine])
        self.models_summary_tb.Text = "{0} of {1} model(s) ticked.".format(
            len(picked), len(self._model_rows))
        self.run_summary_tb.Text = (
            "{0} model(s) ticked, {1} where you actually hold something.".format(
                len(picked), with_mine))

    # ---------------- progress ----------------
    # A modal WPF window does not repaint during a synchronous loop, so _pump()
    # drains pending render work at Background priority - the DoEvents pattern
    # already confirmed working live in DeeSuperLINK.
    def _pump(self):
        try:
            frame = DispatcherFrame()

            def _stop():
                frame.Continue = False

            self.Dispatcher.BeginInvoke(DispatcherPriority.Background, Action(_stop))
            Dispatcher.PushFrame(frame)
        except Exception:
            pass

    def _progress_begin(self, total):
        self._prog_total = max(1, total)
        self._prog_done = 0
        self._prog_start = time.time()
        try:
            self.progress_bar.Maximum = self._prog_total
            self.progress_bar.Value = 0
            self.progress_host.Visibility = Visibility.Visible
            self.run_b.IsEnabled = False
            self.scan_b.IsEnabled = False
        except Exception:
            pass
        self._progress_render("Starting...")

    def _progress_render(self, label):
        try:
            elapsed = time.time() - self._prog_start
            done = self._prog_done
            if done > 0:
                remaining = (elapsed / float(done)) * (self._prog_total - done)
                eta = "ETA {0}".format(format_duration(remaining))
            else:
                eta = "ETA --"
            self.progress_bar.Value = done
            self.progress_text_tb.Text = "{0}   |   {1} of {2}   |   elapsed {3}   |   {4}".format(
                label, min(done + 1, self._prog_total), self._prog_total,
                format_duration(elapsed), eta)
        except Exception:
            pass
        self._pump()

    def _progress_done_one(self):
        self._prog_done += 1
        try:
            self.progress_bar.Value = self._prog_done
        except Exception:
            pass
        self._pump()

    def _progress_end(self):
        try:
            self.progress_host.Visibility = Visibility.Collapsed
            self.run_b.IsEnabled = True
            self.scan_b.IsEnabled = True
        except Exception:
            pass
        self._pump()

    # ---------------- run ----------------
    def run_click(self, sender, args):
        self._guard(self._run)

    def _run(self):
        picked = self._selected_models()
        if not picked:
            forms.alert("Tick at least one model on the Models tab.", title="DeeRelinquish")
            return
        flags = self._flags()
        if not core.any_flag_set(flags):
            forms.alert("Tick at least one category on the What to Release tab.",
                        title="DeeRelinquish")
            return

        sync_first = self.sync_first_cb.IsChecked is True
        no_holdings = [r for r in picked if r.scanned and not r.mine and not r.scan_error]
        warning = ""
        if no_holdings:
            warning = ("\n\n{0} of them show nothing owned by you - they will be opened "
                       "and closed with no change.".format(len(no_holdings)))
        sync_warning = ""
        if sync_first:
            sync_warning = ("\n\nSYNCHRONIZE FIRST IS ON. Any local changes in these "
                            "models will be PUBLISHED to everyone on the project.")

        if not forms.alert(
                "Relinquish your ownership in {0} model(s)?\n\n"
                "Each is opened from ACC, released and closed. Nothing owned by "
                "anyone else is touched.{1}{2}".format(len(picked), warning, sync_warning),
                title="DeeRelinquish", yes=True, no=True):
            return

        self.main_tabs.SelectedIndex = 3
        self._log("=" * 70)
        self._log("Relinquishing in {0} model(s){1}".format(
            len(picked), " (synchronizing first)" if sync_first else ""))
        self._log("=" * 70)

        ok = failed = 0
        synced = 0
        self._progress_begin(len(picked))
        try:
            for row in picked:
                row.result = ""
                self._progress_render(row.name)
                doc = None
                try:
                    doc, detail = afb.open_cloud_document_attached(
                        self.app, self._region, self._project_id, row.item_id, self._token)
                    if doc is None:
                        row.result = "FAILED to open - {0}".format(detail)
                        self._log("  FAIL {0} - could not open: {1}".format(row.name, detail))
                        failed += 1
                        continue

                    if sync_first:
                        sync_ok, sync_detail = core.synchronize_only(doc)
                        if sync_ok:
                            synced += 1
                            self._log("       {0} - synchronized".format(row.name))
                        else:
                            self._log("       {0} - sync FAILED: {1}".format(
                                row.name, sync_detail))

                    rel_ok, rel_detail = core.relinquish_document(doc, flags)
                    if rel_ok:
                        row.result = "OK - {0}".format(rel_detail)
                        self._log("  OK   {0} - {1}".format(row.name, rel_detail))
                        ok += 1
                    else:
                        row.result = "FAILED - {0}".format(rel_detail)
                        self._log("  FAIL {0} - {1}".format(row.name, rel_detail))
                        failed += 1
                except Exception as e:
                    row.result = "FAILED - {0}".format(e)
                    self._log("  FAIL {0} - {1}".format(row.name, e))
                    failed += 1
                finally:
                    if doc is not None:
                        # Never save: relinquish transacts with central by itself,
                        # and this tool must not write model content.
                        docmgr.close_document(doc, save_modified=False)
                    self._progress_done_one()
        finally:
            self._progress_end()

        self._log("-" * 70)
        self._log("Done. {0} released, {1} failed.".format(ok, failed))

        others = [r for r in self._ownership_rows if r.owner and not r.is_mine]
        note = ""
        if sync_first and synced:
            note = " {0} model(s) were synchronized first.".format(synced)
        output.print_html(core.report_html(self._model_rows, others, note))

        self._refresh_models()
        self.status_tb.Text = ("Done - {0} model(s) released, {1} failed. Re-scan to "
                               "confirm.".format(ok, failed))

    def close_click(self, sender, args):
        self.Close()


window = DeeRelinquishWindow(_XAML_FILE, __revit__)
window.ShowDialog()
