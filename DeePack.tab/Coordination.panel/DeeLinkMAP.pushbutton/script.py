# -*- coding: utf-8 -*-
"""
DeeLinkMAP
Scans an ACC project OR a local folder for Revit files, lets you tick
which ones to include, and discovers each ticked file's own Revit
links - primarily WITHOUT opening it at all (TransmissionData read
straight from disk), only falling back to a quick headless open when
that fast read fails for a particular file. Produces a single
self-contained, interactive HTML map (files as nodes, links as edges)
opened in your browser - lib/dee_linkmap_service.py owns the map
generation and the no-open/fallback-open logic; see that module's own
docstring for the full technical story (the TransmissionData
mechanism, its one documented gap around cloud-hosted references, and
why a fallback-open path exists at all).

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

The fallback-open path reuses the same deew_failure_handler dialog
handling + one-host-at-a-time discipline every other unattended batch
tool in this codebase already relies on.

NEEDS LIVE-REVIT VERIFICATION - see dee_linkmap_service.py's own
docstring: the whole TransmissionData path is new to this codebase,
and the fallback-open path (while built on proven pieces) has never
run live in this specific combination.
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
from System import Action
from System.Windows import Visibility
from System.Windows.Threading import Dispatcher, DispatcherFrame, DispatcherPriority
from System.Windows.Forms import (
    FolderBrowserDialog, SaveFileDialog, DialogResult)

from Autodesk.Revit.DB import ModelPathUtils

import acc_auth
import acc_file_browser as afb
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

    # ---------------- model path / open, per source mode ----------------
    def _model_path_for(self, name):
        if self._source_mode() == "acc":
            item_id = self._all_items.get(name)
            return afb.cloud_model_path(self._region, self._project_id, item_id, self._token)
        file_path = self._all_items.get(name)
        try:
            return ModelPathUtils.ConvertUserVisiblePathToModelPath(file_path), "ok"
        except Exception as e:
            return None, str(e)

    def _open_attached(self, name):
        if self._source_mode() == "acc":
            item_id = self._all_items.get(name)
            return afb.open_cloud_document_attached(
                self.application, self._region, self._project_id, item_id, self._token)
        file_path = self._all_items.get(name)
        return docmgr.open_document_no_detach(self.application, file_path, logger=self.logger)

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

        use_fast_scan = self.fast_scan_cb.IsChecked is True
        if use_fast_scan:
            scan_desc = ("Each file is tried directly from disk first (the "
                         "experimental fast read); one that can't be read that "
                         "way is opened briefly (headless) instead, then closed.")
        else:
            scan_desc = ("Each file is opened briefly (headless) to read its "
                         "links, then closed - the fast no-open read is off.")
        if not forms.alert(
                "Scan {0} file(s) for their Revit links?\n\n{1} Nothing is ever "
                "modified.".format(len(ticked), scan_desc),
                title="DeeLinkMAP - Confirm", yes=True, no=True):
            return

        results = []
        file_nodes = {}
        no_open_count = opened_count = failed_count = 0

        self._progress_begin(len(ticked))
        dialog_handler = ffh.make_dialog_handler(self.logger)
        try:
            self.uiapp.DialogBoxShowing += dialog_handler
        except Exception as e:
            self.logger.exception("Could not attach dialog handler", e)

        try:
            for row in ticked:
                self._progress_step("Scanning {0}".format(row.name))
                node = svc.FileNode(row.name, row.ref)

                targets = None
                if use_fast_scan:
                    model_path, path_detail = self._model_path_for(row.name)
                    targets = svc.read_links_no_open(model_path) if model_path is not None else None

                if targets is not None:
                    node.raw_link_targets = targets
                    node.scan_method = "no-open"
                    no_open_count += 1
                    results.append((True, row.name,
                                     "{0} link(s) - read without opening".format(len(targets))))
                    self._log("  '{0}': {1} link(s), no open needed".format(row.name, len(targets)))
                else:
                    self._log("  '{0}': fast read unavailable, opening as fallback...".format(row.name))
                    doc, open_detail = self._open_attached(row.name)
                    if doc is None:
                        node.error = "could not open: {0}".format(open_detail)
                        failed_count += 1
                        results.append((False, row.name, node.error))
                        self._log("  '{0}': FAILED - {1}".format(row.name, node.error))
                    else:
                        try:
                            targets2 = svc.read_links_by_opening(doc)
                            node.raw_link_targets = targets2
                            node.scan_method = "opened"
                            opened_count += 1
                            results.append((None, row.name,
                                             "{0} link(s) - read by opening (fallback)".format(len(targets2))))
                            self._log("  '{0}': {1} link(s), opened as fallback".format(
                                row.name, len(targets2)))
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

                file_nodes[row.name] = node
                self._progress_done_one()
        finally:
            try:
                self.uiapp.DialogBoxShowing -= dialog_handler
            except Exception:
                pass
            self._progress_end()

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

        self._report(results)
        self.status_tb.Text = (
            "{0} file(s) scanned - {1} without opening, {2} by opening, {3} failed. "
            "{4} link relationship(s) mapped.".format(
                len(ticked), no_open_count, opened_count, failed_count, len(edges)))
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
