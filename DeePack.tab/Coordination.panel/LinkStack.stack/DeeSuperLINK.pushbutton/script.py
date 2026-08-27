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
import time
import datetime

from pyrevit import forms, script
import dee_branding

import clr
clr.AddReference("PresentationFramework")
clr.AddReference("WindowsBase")
from System import Action
from System.Windows import Visibility
from System.Windows.Threading import Dispatcher, DispatcherFrame, DispatcherPriority

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


def matches_search(name, query):
    """Case-insensitive AND-of-terms match. Multiple space-separated
    words must ALL appear, in any order, which is what makes long
    structured file names searchable: "vil cor" finds
    25-036-NAG-BIM-VIL-00A-COR-AR without typing the whole thing."""
    if not query:
        return True
    haystack = (name or "").lower()
    return all(term in haystack for term in query.lower().split())


def format_duration(seconds):
    seconds = int(max(0, seconds))
    if seconds < 60:
        return "{0}s".format(seconds)
    if seconds < 3600:
        return "{0}m {1:02d}s".format(seconds // 60, seconds % 60)
    return "{0}h {1:02d}m".format(seconds // 3600, (seconds % 3600) // 60)


def _placement_label(placement):
    for label, value in PLACEMENT_OPTIONS:
        if value == placement:
            return label
    return str(placement)


def _link_once(doc, link_name, cloud_path, placement):
    """A single attempt. Returns (ok, detail, instance_or_None)."""
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
            return False, "link type could not be created", None
        instance = RevitLinkInstance.Create(doc, result.ElementId, placement)
        t.Commit()
        return True, "linked", instance
    except Exception as e:
        if t.HasStarted() and not t.HasEnded():
            t.RollBack()
        return False, str(e), None


def _shared_coordinates_look_unestablished(instance):
    """After a Shared placement, an identity transform means the link
    landed exactly origin-on-origin - which is what Revit does when the
    two models have no shared-coordinate relationship to honour.

    This is a SIGNAL, not proof: a genuinely shared model whose shared
    position happens to coincide with the host's origin would look the
    same. It is therefore reported as a note, never used to silently
    re-place anything."""
    try:
        return bool(instance.GetTotalTransform().IsIdentity)
    except Exception:
        return False


def link_into(doc, link_name, cloud_path, placement, fallback=None):
    """Creates the link, falling back to `fallback` placement if the
    requested one is rejected outright.

    This exists for Shared coordinates: if the two models do not share
    coordinates, linking "By Shared Coordinates" is not meaningful. Two
    distinct behaviours are handled, because Revit can do either:
      - it REFUSES the placement -> the exception is caught and the
        fallback (Internal Origin) is attempted instead;
      - it ACCEPTS it and quietly places origin-to-origin -> nothing
        throws, so the result is checked and reported rather than
        pretended otherwise.

    Returns (ok, detail). detail names the placement actually used."""
    ok, detail, instance = _link_once(doc, link_name, cloud_path, placement)
    if ok:
        note = _placement_label(placement)
        if placement == ImportPlacement.Shared and _shared_coordinates_look_unestablished(instance):
            note += " (no shared-coordinate relationship found - Revit placed it origin-to-origin)"
        return True, "linked - {0}".format(note)

    if fallback is None or fallback == placement:
        return False, detail

    ok2, detail2, _inst = _link_once(doc, link_name, cloud_path, fallback)
    if ok2:
        return True, "linked - {0} (requested {1} was rejected: {2})".format(
            _placement_label(fallback), _placement_label(placement), detail)
    return False, "{0} failed ({1}); {2} also failed ({3})".format(
        _placement_label(placement), detail, _placement_label(fallback), detail2)


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
        self._prog_total = 1
        self._prog_done = 0
        self._prog_start = time.time()

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

    # ---------------- in-window progress bar ----------------
    #
    # A modal WPF window does not repaint while a long synchronous loop
    # runs on the UI thread, so the bar would only appear once the work
    # had already finished. _pump() drains pending render work at
    # Background priority (the standard WPF "DoEvents") after each
    # update, which is what makes the bar actually animate.
    #
    # The step text is centred ON the bar by stacking a TextBlock over
    # the ProgressBar in a Grid, rather than sitting beside it.
    def _pump(self):
        try:
            frame = DispatcherFrame()

            def _stop():
                frame.Continue = False

            self.Dispatcher.BeginInvoke(DispatcherPriority.Background, Action(_stop))
            Dispatcher.PushFrame(frame)
        except Exception:
            pass

    def _progress_begin(self, total_steps):
        self._prog_total = max(1, total_steps)
        self._prog_done = 0
        self._prog_start = time.time()
        try:
            self.progress_bar.Maximum = self._prog_total
            self.progress_bar.Value = 0
            self.progress_host.Visibility = Visibility.Visible
            self.run_b.IsEnabled = False
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

    def _progress_step(self, label):
        """Call BEFORE doing the work the label describes."""
        self._progress_render(label)

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
        except Exception:
            pass
        self._pump()

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

    def _eligible_rows(self):
        """Everything that could be used - the single-side file is
        excluded because a model cannot link into itself. Search does
        NOT apply here: this is the set Run actually works from."""
        single = self.single_cb.SelectedItem
        return [r for r in self._rows if r.name != single]

    def _visible_rows(self):
        query = ""
        try:
            query = self.search_tb.Text or ""
        except Exception:
            query = ""
        return [r for r in self._eligible_rows() if matches_search(r.name, query)]

    def _refresh_rows(self):
        visible = self._visible_rows()
        self.files_grid.ItemsSource = None
        self.files_grid.ItemsSource = visible

        eligible = self._eligible_rows()
        ticked_total = sum(1 for r in eligible if r.selected)
        visible_names = set(r.name for r in visible)
        ticked_hidden = sum(1 for r in eligible if r.selected and r.name not in visible_names)

        # A ticked file that the current search hides would still be
        # processed by Run. Saying so out loud is the difference between
        # a filter and a trap.
        try:
            self.hidden_warning_tb.Text = (
                "{0} ticked file(s) hidden by the search - they WILL still be used".format(ticked_hidden)
                if ticked_hidden else "")
        except Exception:
            pass

        self.status_tb.Text = "Showing {0} of {1} file(s). {2} ticked.".format(
            len(visible), len(eligible), ticked_total)

    def search_changed(self, sender, args):
        # Fires while the XAML is still loading, before the grid exists.
        try:
            self._refresh_rows()
        except Exception:
            pass

    def clear_search_click(self, sender, args):
        try:
            self.search_tb.Text = ""
        except Exception:
            pass

    def select_all_click(self, sender, args):
        """Applies to what the search is currently showing, not the whole
        project - ticking files you cannot see would be worse."""
        for r in self._visible_rows():
            r.selected = True
        self._refresh_rows()

    def select_none_click(self, sender, args):
        for r in self._visible_rows():
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
        # Deliberately the ELIGIBLE set, not the visible one: files
        # ticked before a search was typed still count. _refresh_rows
        # warns on screen when the search is hiding any of them.
        ticked = [r for r in self._eligible_rows() if r.selected]
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
        # Shared coordinates only mean something when the two models
        # actually share them - fall back to origin-to-origin otherwise,
        # per the user's explicit request.
        fallback = ImportPlacement.Origin if placement == ImportPlacement.Shared else None
        comment = self.sync_comment_tb.Text or ""
        skip_existing = (self.skip_existing_cb.IsChecked is True)
        results = []

        total_steps = sum(len(links_for(h)) + 1 for h in hosts)   # +1 = sync per host
        self._progress_begin(total_steps)

        for host in hosts:
            self._progress_step("Opening {0}".format(host.name))
            self._log("Opening host '{0}'...".format(host.name))
            doc, detail = afb.open_cloud_document_attached(
                self.application, self._region, self._project_id, host.item_id, self._token)
            if doc is None:
                host.status = "Failed to open"
                results.append((False, host.name, "could not open: {0}".format(detail)))
                self._log("  FAILED to open - {0}".format(detail))
                for _ in range(len(links_for(host)) + 1):
                    self._progress_done_one()
                continue
            try:
                existing = existing_link_names(doc) if skip_existing else set()
                linked_here = 0
                for link_name in links_for(host):
                    if skip_existing and _already_linked(existing, link_name):
                        results.append((None, "{0} -> {1}".format(link_name, host.name),
                                        "already linked - skipped"))
                        self._log("  '{0}' already linked - skipped".format(link_name))
                        self._progress_done_one()
                        continue
                    item_id = self._all_items.get(link_name)
                    cloud_path, path_detail = afb.cloud_model_path(
                        self._region, self._project_id, item_id, self._token)
                    if cloud_path is None:
                        results.append((False, "{0} -> {1}".format(link_name, host.name), path_detail))
                        self._log("  '{0}' path failed - {1}".format(link_name, path_detail))
                        self._progress_done_one()
                        continue
                    self._progress_step("Linking {0} -> {1}".format(link_name, host.name))
                    ok, link_detail = link_into(doc, link_name, cloud_path, placement, fallback)
                    results.append((ok, "{0} -> {1}".format(link_name, host.name), link_detail))
                    self._log("  '{0}': {1}".format(link_name, link_detail))
                    if ok:
                        linked_here += 1
                    self._progress_done_one()

                if linked_here:
                    self._progress_step("Synchronizing {0}".format(host.name))
                    ok_sync, sync_detail = docmgr.synchronize_with_central(
                        doc, comment=comment, compact=False, logger=self.logger)
                    host.status = "Synchronized" if ok_sync else "Linked but sync FAILED"
                    results.append((ok_sync, host.name,
                                    "synchronized" if ok_sync else "sync failed: {0}".format(sync_detail)))
                    self._log("  host {0}".format(host.status))
                    self._progress_done_one()
                else:
                    host.status = "Nothing to link"
                    self._progress_done_one()
            except Exception as e:
                host.status = "Failed"
                results.append((False, host.name, "unexpected error: {0}".format(e)))
                self.logger.exception("Unexpected error linking", e, file=host.name)
            finally:
                try:
                    docmgr.close_document(doc, save_modified=False, logger=self.logger)
                except Exception:
                    pass

        self._progress_end()
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
