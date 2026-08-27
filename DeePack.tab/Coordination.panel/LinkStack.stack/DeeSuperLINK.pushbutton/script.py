# -*- coding: utf-8 -*-
"""
DeeSuperLINK
Links ACC/BIM360 cloud models into each other with NO Revit file open.

Two directions:
  ONE -> MANY : pick one model, link it into many host models.
  MANY -> ONE : pick one host, link many models into it.

--------------------------------------------------------------------
How it differs from DeeLINK (the button below it)
--------------------------------------------------------------------
DeeLINK links cloud files into the document you already have open. It
is interactive and single-host by nature.

DeeSuperLINK never touches an open document - it is `context: zero-doc`
and opens each HOST model itself, headless and ATTACHED, creates the
link, then Synchronizes With Central so the link reaches the real
shared model. That is the whole point: setting up a federated model
without opening anything by hand.

Because it opens hosts ATTACHED and syncs, this writes to real cloud
models. That is deliberate and is what the tool is for, but it is a
categorically bigger action than DeeLINK's, so the confirmation names
exactly how many models will be modified.

--------------------------------------------------------------------
Reused rather than reinvented
--------------------------------------------------------------------
- acc_auth / acc_api / acc_file_browser - hub/project/file browsing,
  already proven by DeeLINK, DeeOpener and the DeeW.Cloud tools.
- acc_file_browser.open_cloud_document_attached() - added for this
  tool; the headless ATTACHED sibling of the detached opener written
  for DeeW.Transmit. Neither uses OpenAndActivateDocument, which
  activates documents into Revit's UI and proved unusable for batch
  work (see that function's docstring for the history).
- acc_file_browser.cloud_model_path() - the ModelPath a link needs.
- deew_document_manager.synchronize_with_central / close_document.
- The link call itself mirrors DeeLINK's proven link_file():
  RevitLinkType.Create(doc, cloud_path, RevitLinkOptions(False)) then
  RevitLinkInstance.Create(doc, type_id, placement).

--------------------------------------------------------------------
Placement - all four Revit options, verified not assumed
--------------------------------------------------------------------
ImportPlacement has exactly four members (checked against the API docs
before writing, because "Base Point" is NOT its own member and the
naming is easy to get wrong):
  Origin   -> Internal Origin to Internal Origin
  Site     -> Project Base Point to Project Base Point. The docs read
              "Placement at Base Point. Useful for Revit links only."
              This is the Base Point option, despite the name.
  Shared   -> By Shared Coordinates
  Centered -> Center to Center
DeeLINK already maps these the same way, so both tools agree.

--------------------------------------------------------------------
NEEDS LIVE-REVIT VERIFICATION (flagged, not assumed)
--------------------------------------------------------------------
- Whether RevitLinkType.Create accepts a cloud ModelPath from inside a
  headlessly-opened host. DeeLINK proves it works against the ACTIVE
  document; doing it in a background document is the new part.
- Whether a host opened attached-and-headless can Synchronize With
  Central without a UI document. DeeW.Clean does the same thing and is
  itself still unverified live, so this shares that risk rather than
  resting on a proven path.
- A model cannot link itself; that case is filtered out here, but a
  circular A->B->A link is NOT detected and Revit's own behaviour for
  it is untested.
"""
import os
import datetime

from pyrevit import forms, script
import dee_branding

from Autodesk.Revit.DB import (
    RevitLinkType, RevitLinkOptions, RevitLinkInstance, ImportPlacement,
    Transaction, FilteredElementCollector,
)

import acc_auth
import acc_api
import acc_file_browser as afb
import deew_document_manager as docmgr
import deew_logger

output = script.get_output()

_TOOL_NAME = "DeeSuperLINK"
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")
_CACHE_FILE = os.path.join(_THIS_DIR, ".acc_file_cache.json")

PLACEMENT_OPTIONS = [
    ("Internal Origin to Internal Origin", ImportPlacement.Origin),
    ("Project Base Point to Project Base Point", ImportPlacement.Site),
    ("By Shared Coordinates", ImportPlacement.Shared),
    ("Center to Center", ImportPlacement.Centered),
]

MODE_ONE_TO_MANY = "one_to_many"
MODE_MANY_TO_ONE = "many_to_one"


class FileRow(object):
    """One ACC Revit file. Plain values only - no Revit objects are held
    across the window's lifetime."""

    def __init__(self, name, item_id):
        self.selected = False
        self.name = name
        self.item_id = item_id
        self.status = ""


def existing_link_names(doc):
    """Lowercased names of RevitLinkTypes already in the host, so a
    re-run does not stack duplicate links."""
    names = set()
    try:
        for lt in FilteredElementCollector(doc).OfClass(RevitLinkType):
            try:
                n = lt.Name
                if n:
                    names.add(n.lower())
            except Exception:
                continue
    except Exception:
        pass
    return names


def _already_linked(existing, link_name):
    """Revit link type names usually carry the .rvt extension, but not
    always - compare on the stem so both spellings match."""
    stem = os.path.splitext(link_name)[0].lower()
    for n in existing:
        if os.path.splitext(n)[0] == stem:
            return True
    return False


def link_into(doc, link_name, cloud_path, placement):
    """One Transaction per link, mirroring DeeLINK's link_file()."""
    t = Transaction(doc, "DeeSuperLINK: link {0}".format(link_name))
    t.Start()
    try:
        result = RevitLinkType.Create(doc, cloud_path, RevitLinkOptions(False))
        try:
            bad = result.ElementId.Value < 0
        except Exception:
            bad = result.ElementId.IntegerValue < 0
        if bad:
            t.RollBack()
            return False, "link type could not be created"
        RevitLinkInstance.Create(doc, result.ElementId, placement)
        t.Commit()
        return True, "linked"
    except Exception as e:
        if t.HasStarted() and not t.HasEnded():
            t.RollBack()
        return False, str(e)


class DeeSuperLinkWindow(dee_branding.DeeBrandedWindow):
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
        self._all_items = {}          # {display name: item_id}
        self._rows = []
        self._log_lines = []

        self.placement_cb.ItemsSource = [label for label, _v in PLACEMENT_OPTIONS]
        self.placement_cb.SelectedIndex = 0
        self._apply_mode_labels()
        self._log("Ready. Pick a hub and project to begin.")

    # ---------------- helpers ----------------
    def _log(self, message):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self._log_lines.append("[{0}] {1}".format(ts, message))
        try:
            self.log_tb.Text = "\n".join(self._log_lines[-400:])
            self.log_tb.ScrollToEnd()
        except Exception:
            pass

    def _mode(self):
        return MODE_ONE_TO_MANY if self.mode_one_to_many_rb.IsChecked is True else MODE_MANY_TO_ONE

    def _placement(self):
        idx = self.placement_cb.SelectedIndex
        if idx is None or idx < 0:
            idx = 0
        return PLACEMENT_OPTIONS[idx][1]

    def _apply_mode_labels(self):
        if self._mode() == MODE_ONE_TO_MANY:
            self.single_label_tb.Text = "Model to link:"
            self.many_label_tb.Text = "3. Host models to link INTO (tick them):"
        else:
            self.single_label_tb.Text = "Host model:"
            self.many_label_tb.Text = "3. Models to link INTO that host (tick them):"

    def mode_changed(self, sender, args):
        # Fires during XAML load before the fields exist.
        try:
            self._apply_mode_labels()
        except Exception:
            return
        self._refresh_rows()

    def single_changed(self, sender, args):
        try:
            self._refresh_rows()
        except Exception:
            pass

    # ---------------- project ----------------
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
        # hub_id is required by list_project_files' fresh-scan path
        # (acc_api.get_top_folders) - only the cached path works without it.
        self._hub_id = hub_id
        self._region = region
        self._project_id = project_id
        self._project_name = project_name
        self.project_tb.Text = "{0} / {1}".format(hub_name, project_name)
        self._log("Loading Revit files in '{0}'...".format(project_name))
        self.refresh_click(None, None)

    def refresh_click(self, sender, args):
        if not self._project_id:
            forms.alert("Pick a hub and project first.")
            return
        try:
            with forms.ProgressBar(title="DeeSuperLINK - loading project files...", indeterminate=True):
                self._all_items = afb.list_project_files(
                    self._hub_id, self._project_id, self._token, _CACHE_FILE) or {}
        except Exception as e:
            forms.alert("Could not list project files:\n{0}".format(e))
            return
        names = sorted(self._all_items.keys())
        self.single_cb.ItemsSource = names
        if names:
            self.single_cb.SelectedIndex = 0
        self._rows = [FileRow(n, self._all_items[n]) for n in names]
        self._refresh_rows()
        self._log("{0} Revit file(s) found.".format(len(names)))

    def _refresh_rows(self):
        """The file chosen in the single-side combo is excluded from the
        many-side list - a model cannot link into itself."""
        single = self.single_cb.SelectedItem
        visible = [r for r in self._rows if r.name != single]
        self.files_grid.ItemsSource = None
        self.files_grid.ItemsSource = visible
        self.status_tb.Text = "{0} file(s) available. {1} ticked.".format(
            len(visible), sum(1 for r in visible if r.selected))

    def select_all_click(self, sender, args):
        single = self.single_cb.SelectedItem
        for r in self._rows:
            r.selected = (r.name != single)
        self._refresh_rows()

    def select_none_click(self, sender, args):
        for r in self._rows:
            r.selected = False
        self._refresh_rows()

    # ---------------- run ----------------
    def run_click(self, sender, args):
        if not self._project_id:
            forms.alert("Pick a hub and project first.")
            return
        single = self.single_cb.SelectedItem
        if not single:
            forms.alert("Pick the single model in the dropdown first.")
            return
        ticked = [r for r in self._rows if r.selected and r.name != single]
        if not ticked:
            forms.alert("Tick at least one file in the list.")
            return

        mode = self._mode()
        placement_label = self.placement_cb.SelectedItem
        if mode == MODE_ONE_TO_MANY:
            hosts = ticked
            links_for = lambda host: [single]
            summary = "Link '{0}' into {1} host model(s)".format(single, len(hosts))
        else:
            hosts = [r for r in self._rows if r.name == single] or [FileRow(single, self._all_items[single])]
            links_for = lambda host: [r.name for r in ticked]
            summary = "Link {0} model(s) into '{1}'".format(len(ticked), single)

        if not forms.alert(
                "{0}, placed {1}?\n\nEach host is opened, linked, and SYNCHRONIZED back to ACC - "
                "this modifies {2} real cloud model(s).".format(
                    summary, placement_label, len(hosts)),
                title="DeeSuperLINK - Confirm", yes=True, no=True):
            return

        placement = self._placement()
        comment = self.sync_comment_tb.Text or ""
        skip_existing = (self.skip_existing_cb.IsChecked is True)
        results = []

        for host in hosts:
            self._log("Opening host '{0}'...".format(host.name))
            doc, detail = afb.open_cloud_document_attached(
                self.application, self._region, self._project_id, host.item_id, self._token)
            if doc is None:
                host.status = "Failed to open"
                results.append((False, host.name, "could not open: {0}".format(detail)))
                self._log("  FAILED to open - {0}".format(detail))
                continue
            try:
                existing = existing_link_names(doc) if skip_existing else set()
                linked_here = 0
                for link_name in links_for(host):
                    if skip_existing and _already_linked(existing, link_name):
                        results.append((None, "{0} -> {1}".format(link_name, host.name),
                                        "already linked - skipped"))
                        self._log("  '{0}' already linked - skipped".format(link_name))
                        continue
                    item_id = self._all_items.get(link_name)
                    cloud_path, path_detail = afb.cloud_model_path(
                        self._region, self._project_id, item_id, self._token)
                    if cloud_path is None:
                        results.append((False, "{0} -> {1}".format(link_name, host.name), path_detail))
                        self._log("  '{0}' path failed - {1}".format(link_name, path_detail))
                        continue
                    ok, link_detail = link_into(doc, link_name, cloud_path, placement)
                    results.append((ok, "{0} -> {1}".format(link_name, host.name), link_detail))
                    self._log("  '{0}': {1}".format(link_name, link_detail))
                    if ok:
                        linked_here += 1

                if linked_here:
                    ok_sync, sync_detail = docmgr.synchronize_with_central(
                        doc, comment=comment, compact=False, logger=self.logger)
                    host.status = "Synchronized" if ok_sync else "Linked but sync FAILED"
                    results.append((ok_sync, host.name,
                                    "synchronized" if ok_sync else "sync failed: {0}".format(sync_detail)))
                    self._log("  host {0}".format(host.status))
                else:
                    host.status = "Nothing to link"
            except Exception as e:
                host.status = "Failed"
                results.append((False, host.name, "unexpected error: {0}".format(e)))
                self.logger.exception("Unexpected error linking", e, file=host.name)
            finally:
                try:
                    docmgr.close_document(doc, save_modified=False, logger=self.logger)
                except Exception:
                    pass

        self._refresh_rows()
        self._report(results)
        ok_count = sum(1 for r in results if r[0] is True)
        fail = sum(1 for r in results if r[0] is False)
        self.status_tb.Text = "{0} succeeded, {1} failed. See the pyRevit output window.".format(
            ok_count, fail)
        self._log(self.status_tb.Text)

    def _report(self, results):
        html = ['<h2 style="font-family:sans-serif;color:#ddd;">DeeSuperLINK - Results</h2>']
        for ok, label, detail in results:
            bg = "#2e7d32" if ok else ("#8d6e00" if ok is None else "#c62828")
            icon = "&#10003;" if ok else ("&#9888;" if ok is None else "&#10007;")
            html.append(
                '<div style="padding:6px 12px;margin:3px 0;background:{0};color:#fff;'
                'border-radius:4px;font-family:monospace;font-size:12px;">'
                '{1}&nbsp; <b>{2}</b> &mdash; {3}</div>'.format(bg, icon, label, detail))
        ok_count = sum(1 for r in results if r[0] is True)
        html.append('<hr><b style="font-family:sans-serif;">{0} / {1} succeeded.</b>'.format(
            ok_count, len(results)))
        output.print_html("".join(html))

    def close_click(self, sender, args):
        self.Close()


def main():
    window = DeeSuperLinkWindow(_XAML_FILE, __revit__)
    window.ShowDialog()


main()
