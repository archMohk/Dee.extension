# -*- coding: utf-8 -*-
"""
DeeLinkMAP
Scans an ACC project OR a local folder for Revit files, lets you tick
which ones to include, and discovers each ticked file's own Revit
links. Produces a single self-contained, interactive HTML map (files
as nodes, links as edges) opened in your browser -
lib/dee_linkmap_service.py owns the map generation.

--------------------------------------------------------------------
ACC models are read over the API first, with nothing opened
--------------------------------------------------------------------
On an ACC source the run starts by asking ACC itself for each model's
links (lib/acc_links_service.py) - no document opened at all, a whole
155-model project answered in about 20 seconds live. Only models ACC
cannot answer for fall through to the open-each-file path below.

This is what fixed the two symptoms the open-only version had:

- "could not open: Opening was canceled" on exactly the files that HAD
  links. A headless attached open has to resolve the model's cloud
  links too, and every link in the project tested comes back
  publishStatus NotPublished, so Revit aborted the entire open. The
  models with links - the only ones that carry any information for a
  link MAP - were the ones the old path could never read.

- Runs dying at the time budget after a handful of files, because an
  open costs minutes each and the budget is 6 minutes total.

The API route cannot answer for a version published the older way
(publishType "WithoutLinks"); those still get opened. It also cannot
report link instance counts or transforms - see acc_links_service's
own docstring.

--------------------------------------------------------------------
The TransmissionData no-open path was REMOVED after a live crash
--------------------------------------------------------------------
The first build of this tool tried Autodesk.Revit.DB.TransmissionData.
ReadTransmissionData() first (reads a file's links straight from disk,
with no document ever opened), falling back to a headless open only
when that failed. Confirmed live, twice: it crashed Revit immediately,
before processing even the first file - not an occasional bad file,
a consistent, reproducible failure. Since that happens at the native
API level, no amount of Python try/except around the call can catch
or recover from it, so rather than leave a checkbox that reliably
crashes Revit when ticked, the whole no-open path was removed. See
dee_linkmap_service.read_links_no_open's own docstring - the function
still exists there for reference/future investigation, but nothing in
this script calls it anymore.

The remaining open-and-read path ALSO crashed live, after processing
several files successfully - consistent with resource accumulation
across many open/close cycles in one Revit session rather than a
single bad file. Two mitigations were added in response: an explicit
.NET garbage-collection pass after each document close (a documented
community practice for exactly this Revit-API batch-processing
symptom), and a time-budget safety net (mirroring the fix already
proven for DeeS.Publish's own ACC-scan crash earlier this same
session) that stops the batch cleanly - reporting what was skipped -
rather than pushing through to another crash.

--------------------------------------------------------------------
Reused rather than reinvented
--------------------------------------------------------------------
Source picking (ACC hub/project or local folder+recursive), the
embedded ETA-aware progress bar (_pump/_progress_begin/_progress_
render/_progress_step/_progress_done_one/_progress_end/
_progress_set_total), and the single-list search/select-all/none
pattern are all copied from DeeMAPLink, adapted from its two-list
match-building UI down to one plain "tick files to include" list,
since DeeLinkMAP does not create or pair anything - it only reads and
maps what already exists.

The open path reuses the same deew_failure_handler dialog handling +
one-host-at-a-time discipline every other unattended batch tool in
this codebase already relies on.

NEEDS LIVE-REVIT VERIFICATION - the GC-pass and time-budget
mitigations are a well-reasoned response to what was observed live,
not yet confirmed to fully resolve it (may need a lower per-run file
count too, if resource accumulation turns out to be steep).
"""
import os
import time
import datetime
import webbrowser

from pyrevit import forms, script
import dee_branding

import clr
clr.AddReference("PresentationFramework")
clr.AddReference("WindowsBase")
clr.AddReference("System.Windows.Forms")
from System import Action, GC
from System.Windows import Visibility
from System.Windows.Threading import Dispatcher, DispatcherFrame, DispatcherPriority
from System.Windows.Forms import (
    FolderBrowserDialog, SaveFileDialog, DialogResult)

import acc_auth
import acc_file_browser as afb
import acc_links_service as als
import deew_document_manager as docmgr
import deew_failure_handler as ffh
import deew_logger
import dee_linkmap_service as svc
import dee_telemetry
dee_telemetry.check_access("DeeLinkMAP")


output = script.get_output()

_TOOL_NAME = "DeeLinkMAP"
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")
_CACHE_FILE = os.path.join(_THIS_DIR, ".acc_file_cache.json")

# A live run crashed Revit after opening/closing several files in a
# row - stops the batch cleanly (reporting the rest as skipped) rather
# than pushing further, mirroring the same time-budget mitigation
# already proven for DeeS.Publish's own ACC-scan crash this session.
_TIME_BUDGET_SECONDS = 360
_LARGE_BATCH_WARNING = 15


class FileRow(object):
    """One scanned Revit file. `ref` is an ACC item_id or a local file
    path, depending on which source mode found it."""
    def __init__(self, name, ref):
        self.selected = False
        self.name = name
        self.ref = ref


class DeeLinkMAPWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, uiapp):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.uiapp = uiapp
        self.application = uiapp.Application
        self.logger = deew_logger.DeeWLogger(_TOOL_NAME)

        self._token = None
        self._hub_id = None
        self._region = None
        self._project_id = None
        self._project_name = ""
        self._local_folder = None
        self._all_items = {}
        self._rows = []
        self._log_lines = []
        self._prog_total = 1
        self._prog_done = 0
        self._prog_start = time.time()

        self._log("Ready. Pick an ACC project or a local folder to begin.")

    # ---------------- helpers ----------------
    def _log(self, message):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self._log_lines.append("[{0}] {1}".format(ts, message))
        try:
            self.log_tb.Text = "\n".join(self._log_lines[-400:])
            self.log_tb.ScrollToEnd()
        except Exception:
            pass

    def _safe_text(self, textbox):
        try:
            return textbox.Text or ""
        except Exception:
            return ""

    def _source_mode(self):
        return "acc" if self.source_acc_rb.IsChecked is True else "local"

    # ---------------- in-window progress bar (copied from DeeMAPLink) ----------------
    def _pump(self):
        try:
            frame = DispatcherFrame()

            def _stop():
                frame.Continue = False

            self.Dispatcher.BeginInvoke(DispatcherPriority.Background, Action(_stop))
            Dispatcher.PushFrame(frame)
        except Exception:
            pass

    def _progress_begin(self, total_steps, indeterminate=False):
        self._prog_total = max(1, total_steps)
        self._prog_done = 0
        self._prog_start = time.time()
        try:
            self.progress_bar.IsIndeterminate = indeterminate
            self.progress_bar.Minimum = 0
            self.progress_bar.Maximum = self._prog_total
            self.progress_bar.Value = 0
            self.progress_host.Visibility = Visibility.Visible
            self.run_b.IsEnabled = False
        except Exception:
            pass
        self._progress_render("Starting...")

    def _progress_set_total(self, total_steps):
        self._prog_total = max(1, total_steps)
        try:
            self.progress_bar.IsIndeterminate = False
            self.progress_bar.Maximum = self._prog_total
        except Exception:
            pass

    def _progress_render(self, label):
        try:
            elapsed = time.time() - self._prog_start
            done = self._prog_done
            if done > 0:
                remaining = (elapsed / float(done)) * (self._prog_total - done)
                eta = "ETA {0:.0f}s".format(max(0, remaining))
            else:
                eta = "ETA --"
            self.progress_bar.Value = min(done, self._prog_total)
            self.progress_text_tb.Text = "{0}   |   {1} of {2}   |   {3}".format(
                label, min(done + 1, self._prog_total), self._prog_total, eta)
        except Exception:
            pass
        self._pump()

    def _progress_step(self, label):
        self._progress_render(label)

    def _progress_done_one(self):
        self._prog_done += 1
        try:
            self.progress_bar.Value = min(self._prog_done, self._prog_total)
        except Exception:
            pass
        self._pump()

    def _progress_end(self):
        try:
            self.progress_host.Visibility = Visibility.Collapsed
            self.run_b.IsEnabled = True
        except Exception:
            pass
        self._pump()

    # ---------------- source mode ----------------
    def source_mode_changed(self, sender, args):
        try:
            is_acc = self._source_mode() == "acc"
            self.acc_row.Visibility = Visibility.Visible if is_acc else Visibility.Collapsed
            self.local_row.Visibility = Visibility.Collapsed if is_acc else Visibility.Visible
        except Exception:
            return

    # ---------------- ACC ----------------
    def pick_project_click(self, sender, args):
        try:
            self._token = acc_auth.get_access_token()
        except Exception as e:
            forms.alert("Authentication failed:\n{0}".format(e))
            return
        hub = afb.pick_hub(self._token)
        if not hub:
            return
        hub_id, region, hub_name = hub
        project = afb.pick_project(hub_id, self._token)
        if not project:
            return
        project_id, project_name = project
        self._hub_id = hub_id
        self._region = region
        self._project_id = project_id
        self._project_name = project_name
        self.project_tb.Text = "{0} / {1}".format(hub_name, project_name)
        self._log("Loading Revit files in '{0}'...".format(project_name))
        self._scan_acc()

    def _scan_acc(self):
        if not self._project_id:
            forms.alert("Pick a hub and project first.")
            return
        self._progress_begin(1, indeterminate=True)
        self._progress_step("Loading project files from ACC (large projects can take a while)...")
        try:
            self._all_items = afb.list_project_files(
                self._hub_id, self._project_id, self._token, _CACHE_FILE) or {}
        except Exception as e:
            self._progress_end()
            forms.alert("Could not list project files:\n{0}".format(e))
            return
        self._progress_done_one()
        self._progress_end()
        self._after_scan()

    # ---------------- Local ----------------
    def pick_folder_click(self, sender, args):
        dlg = FolderBrowserDialog()
        dlg.Description = "Pick a folder to scan for Revit files"
        if dlg.ShowDialog() != DialogResult.OK:
            return
        self._local_folder = dlg.SelectedPath
        self.folder_tb.Text = self._local_folder

    def scan_folder_click(self, sender, args):
        if not self._local_folder:
            forms.alert("Pick a folder first.")
            return
        self._scan_local()

    def _scan_local(self):
        recursive = bool(self.recursive_cb.IsChecked)
        self._progress_begin(1, indeterminate=True)
        self._progress_step("Scanning folder...")
        state = {"known": False}

        def cb(i, total, name):
            if not state["known"]:
                self._progress_set_total(total)
                state["known"] = True
            self._prog_done = i
            self._progress_render("Scanning: {0}".format(name))

        self._all_items = svc.scan_local_folder(self._local_folder, recursive, progress_cb=cb)
        self._progress_done_one()
        self._progress_end()
        self._after_scan()

    # ---------------- after either scan ----------------
    def _after_scan(self):
        names = sorted(self._all_items.keys())
        self._rows = [FileRow(n, self._all_items[n]) for n in names]
        self._refresh_list()
        if not names:
            self.status_tb.Text = "No Revit files found."
            forms.alert(self.status_tb.Text)
        else:
            self.status_tb.Text = "{0} file(s) found. Tick the ones to map, then Scan and Build Map.".format(
                len(names))
        self._log(self.status_tb.Text)

    # ---------------- list ----------------
    def _visible(self):
        query = self._safe_text(self.search_tb)
        return [r for r in self._rows if svc.matches_search(r.name, query)]

    def _refresh_list(self):
        visible = self._visible()
        self.files_grid.ItemsSource = None
        self.files_grid.ItemsSource = visible

    def search_changed(self, sender, args):
        try:
            self._refresh_list()
        except Exception:
            pass

    def clear_search_click(self, sender, args):
        self.search_tb.Text = ""

    def select_all_click(self, sender, args):
        for r in self._visible():
            r.selected = True
        self._refresh_list()

    def select_none_click(self, sender, args):
        for r in self._visible():
            r.selected = False
        self._refresh_list()

    # ---------------- open, per source mode ----------------
    def _open_attached(self, name):
        if self._source_mode() == "acc":
            item_id = self._all_items.get(name)
            return afb.open_cloud_document_attached(
                self.application, self._region, self._project_id, item_id, self._token)
        file_path = self._all_items.get(name)
        return docmgr.open_document_no_detach(self.application, file_path, logger=self.logger)

    def _unread_node(self, row, file_nodes, note):
        """Puts a file that was never read onto the map anyway, carrying
        the reason. A file missing from the map entirely reads as 'this
        file has no links', which is a different - and wrong - claim from
        'nobody looked'."""
        node = svc.FileNode(row.name, row.ref)
        node.error = note
        file_nodes[row.name] = node
        return node

    # ---------------- ACC fast path ----------------
    def _acc_api_pass(self, ticked, file_nodes, results):
        """Reads each model's Revit links straight from ACC, opening
        nothing, and fills in file_nodes/results for every one it can
        answer for. Returns the rows it could NOT answer for, which the
        caller then opens the old way.

        Never raises: if the whole API pass fails (expired login, no
        network, an ACC outage), every row is handed back and the run
        behaves exactly as it did before this path existed."""
        items = []
        for row in ticked:
            item_id = self._all_items.get(row.name)
            if item_id:
                items.append((item_id, row.name))
        if not items:
            return ticked

        self._log("Asking ACC for links directly - nothing opened...")

        def on_progress(done, total):
            try:
                self.status_tb.Text = (
                    "Reading links from ACC: {0}/{1} (nothing opened)".format(done, total))
                self._pump()
            except Exception:
                pass

        try:
            scanned = als.scan_project(self._project_id, items, self._token,
                                       max_workers=6, on_progress=on_progress)
        except Exception as e:
            self.logger.exception("ACC link read failed", e)
            self._log("  ACC link read failed ({0}) - opening every file instead.".format(e))
            return ticked

        by_item = {}
        for entry in scanned:
            by_item[entry.get("item_id")] = entry

        unresolved = []
        for row in ticked:
            entry = by_item.get(self._all_items.get(row.name))
            if entry is None or entry.get("status") != "ok":
                unresolved.append(row)
                continue
            node = svc.FileNode(row.name, row.ref)
            node.raw_link_targets = [link.get("name")
                                     for link in entry.get("links", [])
                                     if link.get("name")]
            node.scan_method = "acc-api"
            file_nodes[row.name] = node
            results.append((True, row.name, "{0} link(s) - read from ACC, not opened".format(
                len(node.raw_link_targets))))

        answered = len(ticked) - len(unresolved)
        self._log("  ACC answered for {0} of {1} file(s) in seconds; {2} still "
                  "need opening.".format(answered, len(ticked), len(unresolved)))
        if unresolved:
            # Name the reason once rather than per file - on a project
            # published the old way this is every single remaining row,
            # and the fix (re-publish normally) is the same for all.
            reasons = {}
            for row in unresolved:
                entry = by_item.get(self._all_items.get(row.name)) or {}
                key = entry.get("reason") or entry.get("status") or "not returned by ACC"
                reasons[key] = reasons.get(key, 0) + 1
            for key in sorted(reasons):
                self._log("    {0}: {1} file(s)".format(key, reasons[key]))
        return unresolved

    # ---------------- run ----------------
    def run_click(self, sender, args):
        self._guard(self._run)

    def _guard(self, fn):
        try:
            fn()
        except Exception as e:
            import traceback
            self.status_tb.Text = "ERROR: {0}".format(e)
            forms.alert("DeeLinkMAP hit an error:\n\n{0}\n\n{1}".format(
                e, traceback.format_exc()[-900:]), title="DeeLinkMAP")

    def _run(self):
        ticked = [r for r in self._rows if r.selected]
        if not ticked:
            forms.alert("Tick at least one file first.")
            return

        default_name = "DeeLinkMAP_{0}.html".format(
            datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
        dlg = SaveFileDialog()
        dlg.Title = "Save the link map as a web page"
        dlg.Filter = "Web page (*.html)|*.html"
        dlg.DefaultExt = "html"
        dlg.AddExtension = True
        dlg.FileName = default_name
        if dlg.ShowDialog() != DialogResult.OK:
            return
        out_path = dlg.FileName

        if not forms.alert(
                "Scan {0} file(s) for their Revit links?\n\nOn an ACC "
                "project the links are read from ACC first, with nothing "
                "opened - that takes seconds. Only files ACC cannot answer "
                "for are then opened headlessly and closed again. Nothing "
                "is ever modified.".format(len(ticked)),
                title="DeeLinkMAP - Confirm", yes=True, no=True):
            return

        results = []
        file_nodes = {}
        opened_count = failed_count = skipped_count = 0
        api_count = 0
        run_start = time.time()
        time_budget_hit = False

        # ---- ACC fast path: read links over HTTPS, open nothing --------
        # Every model ACC can answer for is a model nobody has to open.
        # That matters most for exactly the files that were FAILING: a
        # headless attached open of a link-bearing cloud model has to
        # resolve its cloud links too, and those come back NotPublished,
        # so Revit aborted the whole open ("Opening was canceled") - the
        # models WITH links were the ones the old path could never read.
        # It is also what made a run die at its time budget after a
        # handful of files: opens cost minutes each, this costs about a
        # tenth of a second. Anything ACC cannot answer for falls
        # straight through to the open loop below, unchanged.
        remaining = ticked
        if self._source_mode() == "acc" and self._project_id:
            remaining = self._acc_api_pass(ticked, file_nodes, results)
            api_count = len(ticked) - len(remaining)

        # Files ACC could not answer for. Opening them is the only way
        # to be certain, but it costs minutes each and the time budget
        # will cut the run off long before a big project is done - which
        # is exactly why earlier runs 'did not get all the files'. So ask
        # rather than assume, and make the fast answer the default: a
        # complete map that marks those files 'not read' beats a partial
        # map that silently omits most of them.
        if remaining:
            open_them = forms.alert(
                "{0} file(s) could not be read from ACC.\n\n"
                "They were published the older way, so ACC holds no link "
                "data for them - it cannot say whether they have links or "
                "not.\n\nOpen them one at a time to find out? That is the "
                "only certain answer, but it takes minutes per file and the "
                "run stops at the {1}-minute budget, so a large batch will "
                "not finish.\n\nChoose No to finish now (seconds) with those "
                "files shown on the map as not read.\n\nThe lasting fix is "
                "to re-publish those models normally, with links - then ACC "
                "answers for them instantly.".format(
                    len(remaining), int(_TIME_BUDGET_SECONDS / 60)),
                title="DeeLinkMAP - Open the rest?", yes=True, no=True)
            if not open_them:
                for row in remaining:
                    self._unread_node(row, file_nodes,
                                      "not read - published without link data")
                    results.append((None, row.name, "not opened - shown on the map as not read"))
                    skipped_count += 1
                remaining = []

        self._progress_begin(len(remaining))
        dialog_handler = ffh.make_dialog_handler(self.logger)
        try:
            self.uiapp.DialogBoxShowing += dialog_handler
        except Exception as e:
            self.logger.exception("Could not attach dialog handler", e)

        try:
            for row in remaining:
                if time_budget_hit:
                    self._unread_node(row, file_nodes,
                                      "not read - run stopped at the time budget")
                    results.append((None, row.name, "skipped - time budget reached"))
                    skipped_count += 1
                    self._progress_done_one()
                    continue

                if time.time() - run_start > _TIME_BUDGET_SECONDS:
                    time_budget_hit = True
                    self._log("  Time budget reached ({0} min) - stopping cleanly, "
                              "remaining files will be reported as skipped.".format(
                                  int(_TIME_BUDGET_SECONDS / 60)))
                    self._unread_node(row, file_nodes,
                                      "not read - run stopped at the time budget")
                    results.append((None, row.name, "skipped - time budget reached"))
                    skipped_count += 1
                    self._progress_done_one()
                    continue

                self._progress_step("Scanning {0}".format(row.name))
                node = svc.FileNode(row.name, row.ref)

                doc, open_detail = self._open_attached(row.name)
                if doc is None:
                    node.error = "could not open: {0}".format(open_detail)
                    failed_count += 1
                    results.append((False, row.name, node.error))
                    self._log("  '{0}': FAILED - {1}".format(row.name, node.error))
                else:
                    try:
                        targets = svc.read_links_by_opening(doc)
                        node.raw_link_targets = targets
                        node.scan_method = "opened"
                        opened_count += 1
                        results.append((True, row.name, "{0} link(s)".format(len(targets))))
                        self._log("  '{0}': {1} link(s)".format(row.name, len(targets)))
                    except Exception as e:
                        node.error = str(e)
                        failed_count += 1
                        results.append((False, row.name, "error reading links: {0}".format(e)))
                        self._log("  '{0}': FAILED while reading - {1}".format(row.name, e))
                    finally:
                        try:
                            docmgr.close_document(doc, save_modified=False, logger=self.logger)
                        except Exception:
                            pass
                        # Explicit GC pass after every close - a documented
                        # community practice for exactly the "crashes after N
                        # files" symptom this tool hit live (Revit/the .NET
                        # runtime does not always eagerly release a closed
                        # document's native memory otherwise).
                        try:
                            GC.Collect()
                            GC.WaitForPendingFinalizers()
                            GC.Collect()
                        except Exception:
                            pass

                file_nodes[row.name] = node
                self._progress_done_one()
        finally:
            try:
                self.uiapp.DialogBoxShowing -= dialog_handler
            except Exception:
                pass
            self._progress_end()

        skipped_count = sum(1 for r in results if r[0] is None)

        nodes, edges = svc.build_graph(file_nodes)
        title = "DeeLinkMAP - {0}".format(self._project_name or self._local_folder or "")
        ok, detail = svc.export_html_map(nodes, edges, out_path, title=title)
        if ok:
            self._log("Map saved: {0}".format(out_path))
            try:
                webbrowser.open(out_path)
            except Exception:
                pass
        else:
            forms.alert("Could not write the map file:\n{0}".format(detail), title="DeeLinkMAP")

        if time_budget_hit:
            forms.alert(
                "Stopped after the {0}-minute time budget to avoid pushing "
                "further - {1} file(s) were not read. They are on the map, "
                "marked as not read, so the picture is complete about what "
                "is known and what is not. Re-run with just those files to "
                "pick up where this left off.".format(
                    int(_TIME_BUDGET_SECONDS / 60), skipped_count),
                title="DeeLinkMAP - Stopped Early")

        self._report(results)
        self.status_tb.Text = (
            "{0} read from ACC without opening, {1} opened and scanned, "
            "{2} failed, {3} skipped. {4} link relationship(s) mapped.".format(
                api_count, opened_count, failed_count, skipped_count, len(edges)))
        self._log(self.status_tb.Text)

    def _report(self, results):
        html = ['<h2 style="font-family:sans-serif;color:#ddd;">DeeLinkMAP - Results</h2>']
        for ok, label, detail in results:
            bg = "#2e7d32" if ok else ("#8d6e00" if ok is None else "#c62828")
            icon = "&#10003;" if ok else ("&#9888;" if ok is None else "&#10007;")
            html.append(
                '<div style="padding:6px 12px;margin:3px 0;background:{0};color:#fff;'
                'border-radius:4px;font-family:monospace;font-size:12px;">'
                '{1}&nbsp; <b>{2}</b> &mdash; {3}</div>'.format(bg, icon, label, detail))
        ok_count = sum(1 for r in results if r[0] is True)
        html.append('<hr><b style="font-family:sans-serif;">{0} / {1} scanned without needing to open.</b>'.format(
            ok_count, len(results)))
        output.print_html("".join(html))

    def close_click(self, sender, args):
        self.Close()


window = DeeLinkMAPWindow(_XAML_FILE, __revit__)
window.ShowDialog()
