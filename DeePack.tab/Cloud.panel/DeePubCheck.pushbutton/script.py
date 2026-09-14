# -*- coding: utf-8 -*-
"""
DeePubCheck
Ask ACC which cloud models still have synced-but-unpublished changes,
show the answer per file, then publish only the ones that need it.

Why this exists alongside the two publish tools already here
-----------------------------------------------------------
DeeS.Publish publishes EVERY file in a project. DeePublisher publishes
whatever you tick. Neither asks first, so both routinely re-publish
models that were already up to date - real time, real ACC processing,
for nothing. This tool answers the question before acting.

It also feeds DeeLinkMAP directly. ACC can only report a model's Revit
links for versions published the newer way; a "normal publish, with
links" is what produces one. Every model published here with links is a
model DeeLinkMAP can then read without opening anything.

Runs with NO Revit file open
----------------------------
There is nothing to open and nothing that has to be open. The bundle is
context: zero-doc, so the button stays live in an empty Revit, and the
window holds no UIApplication, Document or UIDocument at all - it talks
to ACC over HTTPS and to nothing else. Publishing happens on ACC's own
servers; it keeps running there after this window is closed, and after
Revit is closed.

Nothing is published until Publish Selected is pressed - the status
check is a read.

Honest about what it does not know
----------------------------------
The publish-status command's response shape has not been confirmed
against the live service yet (see lib/dee_publish_status_service.py).
Anything ACC returns that the service does not recognise is shown as
"Unknown" with ACC's own wording in the next column, and is NEVER
pre-ticked for publishing. You can still tick one by hand. The first
real run writes those raw responses to the log, which is what turns an
Unknown into a known.
"""
from pyrevit import forms, script
import datetime
import os
import time

import dee_branding

import clr
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
from System import Action
from System.Windows import Visibility
from System.Windows.Threading import Dispatcher, DispatcherFrame, DispatcherPriority

import acc_api
import acc_auth
import acc_file_browser as afb
import deew_logger
import dee_publish_status_service as pubsvc
import dee_telemetry

dee_telemetry.check_access("DeePubCheck")

_TOOL_NAME = "DeePubCheck"
_THIS_DIR = os.path.dirname(__file__)
_CACHE_FILE = os.path.join(_THIS_DIR, ".acc_file_cache.json")


def matches_search(name, query):
    """AND-of-terms search, same rule the other ACC tools use: every
    word typed has to appear somewhere in the name, in any order."""
    terms = (query or "").lower().split()
    if not terms:
        return True
    low = (name or "").lower()
    return all(term in low for term in terms)


class FileRow(object):
    def __init__(self, item_id, name):
        self.selected = False
        self.item_id = item_id
        self.name = name
        self.status = "-"
        self.detail = ""
        self.publish_type = ""
        self.version = ""


class DeePubCheckWindow(dee_branding.DeeBrandedWindow):
    # These must exist BEFORE the base class loads the XAML. Loading it
    # can fire TextChanged/Checked handlers while no instance attribute
    # exists yet, and search_changed -> _refresh_grid -> _visible would
    # then blow up on self._rows. Same guard DeeRelinquish carries, for
    # the same reason.
    _rows = []
    _all_items = {}

    def __init__(self, xaml_file):
        # No UIApplication, no Document, no UIDocument - deliberately.
        # This window talks to ACC over HTTPS and to nothing else, so
        # there is no object here that could need a model to be open.
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.logger = deew_logger.DeeWLogger(_TOOL_NAME)

        self._token = None
        self._hub_id = None
        self._region = None
        self._project_id = None
        self._project_name = ""
        self._all_items = {}
        self._rows = []
        self._log_lines = []
        self._prog_total = 1
        self._prog_done = 0
        self._prog_start = time.time()

        self._log("Ready. Pick an ACC hub and project to begin.")

    # ---------------- helpers ----------------
    def _log(self, message):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self._log_lines.append("[{0}] {1}".format(ts, message))
        try:
            self.log_tb.Text = "\n".join(self._log_lines[-400:])
            self.log_tb.ScrollToEnd()
        except Exception:
            pass

    def _guard(self, fn):
        try:
            fn()
        except Exception as e:
            import traceback
            self.status_tb.Text = "ERROR: {0}".format(e)
            self.logger.exception("Unhandled error", e)
            forms.alert("DeePubCheck hit an error:\n\n{0}\n\n{1}".format(
                e, traceback.format_exc()[-900:]), title="DeePubCheck")

    def _safe_text(self, textbox):
        try:
            return textbox.Text or ""
        except Exception:
            return ""

    # ---------------- in-window progress bar ----------------
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
            self.scan_b.IsEnabled = False
            self.publish_b.IsEnabled = False
        except Exception:
            pass
        self._progress_render("Starting...")

    def _progress_render(self, label):
        try:
            elapsed = time.time() - self._prog_start
            done = self._prog_done
            if done > 0:
                left = (elapsed / float(done)) * (self._prog_total - done)
                eta = "ETA {0:.0f}s".format(max(0, left))
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
            self.progress_bar.IsIndeterminate = False
            self.progress_host.Visibility = Visibility.Collapsed
            self.scan_b.IsEnabled = bool(self._project_id)
            self.publish_b.IsEnabled = bool(self._rows)
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
            forms.alert("Authentication failed:\n{0}".format(e), title="DeePubCheck")
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
        self.scan_b.IsEnabled = True
        self._log("Loading Revit files in '{0}'...".format(project_name))

        self._progress_begin(1, indeterminate=True)
        self._progress_step("Loading project files from ACC...")
        try:
            self._all_items = afb.list_project_files(
                self._hub_id, self._project_id, self._token, _CACHE_FILE) or {}
        except Exception as e:
            self._progress_end()
            forms.alert("Could not list project files:\n{0}".format(e), title="DeePubCheck")
            return
        self._progress_done_one()
        self._progress_end()

        self._rows = [FileRow(self._all_items[n], n) for n in sorted(self._all_items)]
        self._refresh_grid()
        self._log("{0} Revit file(s) found. Press 'Check Publish Status'.".format(len(self._rows)))
        self.status_tb.Text = "{0} file(s) found. Status not checked yet.".format(len(self._rows))

    # ---------------- status scan ----------------
    def scan_click(self, sender, args):
        self._guard(self._scan)

    def _scan(self):
        if not self._rows:
            forms.alert("Pick a hub and project first.", title="DeePubCheck")
            return

        items = [(r.item_id, r.name) for r in self._rows]
        self._log("Asking ACC for the publish status of {0} file(s)...".format(len(items)))
        self.logger.info("Publish status scan starting", files=len(items))
        self._progress_begin(len(items))

        def on_progress(done, total):
            self._prog_done = done
            self._progress_render("Checking publish status")

        try:
            rows = pubsvc.scan_statuses(
                self._project_id, items, self._token, max_workers=6,
                on_progress=on_progress, logger=self.logger)
        except Exception as e:
            self._progress_end()
            self.logger.exception("Publish status scan failed", e)
            forms.alert("Could not read publish status:\n\n{0}".format(e), title="DeePubCheck")
            return
        finally:
            self._progress_end()

        by_item = {}
        for row in rows:
            by_item[row.get("item_id")] = row
        for r in self._rows:
            row = by_item.get(r.item_id)
            if row:
                r.status = row.get("status") or pubsvc.JOB_UNKNOWN
                r.detail = row.get("detail") or ""
                r.publish_type = row.get("publish_type_label") or pubsvc.TYPE_UNKNOWN
                version = row.get("version_number")
                r.version = "v{0}".format(version) if version else ""

        counts = pubsvc.summarize(rows)
        self.logger.info("Publish status scan finished", **counts)
        self._log("Done. {0}".format(self._counts_text(counts)))

        # Pre-tick the models a re-publish would genuinely improve: the
        # ones published the old way, whose links ACC cannot report.
        # Unknowns are left alone - publishing on a guess is the thing
        # this tool exists to avoid.
        for r in self._rows:
            r.selected = (r.publish_type == pubsvc.TYPE_OLD)

        self._refresh_grid()
        self._update_summary()

        self._log("  Publish jobs: {0}".format(self._job_counts_text(counts)))
        if counts.get(pubsvc.JOB_UNKNOWN):
            self._log("  {0} file(s) returned job wording this tool does not "
                      "recognise - see the 'ACC said' column; the raw responses "
                      "are in the log file.".format(counts[pubsvc.JOB_UNKNOWN]))

        old_count = counts.get("types", {}).get(pubsvc.TYPE_OLD, 0)
        if old_count:
            forms.alert(
                "{0} of {1} model(s) were published the old way, with their "
                "links stripped. ACC holds no link data for those, so "
                "DeeLinkMAP has to open them one at a time - the slow path "
                "that has crashed Revit.\n\nThey are ticked. Publish them "
                "normally (with links) and ACC can report their links "
                "instantly.\n\n{2}".format(
                    old_count, len(self._rows), self._counts_text(counts)),
                title="DeePubCheck - Publish Type")
        else:
            forms.alert(
                "Every model here was published the current way, so ACC can "
                "report all their Revit links without opening anything.\n\n"
                "{0}\n\nNote: these APIs cannot say whether a model has been "
                "SYNCED since it was last published - see the tool's own notes "
                "for why.".format(self._counts_text(counts)),
                title="DeePubCheck - Publish Type")

    def _counts_text(self, counts):
        types = counts.get("types", {})
        parts = []
        for key in (pubsvc.TYPE_CURRENT, pubsvc.TYPE_OLD, pubsvc.TYPE_UNKNOWN):
            if types.get(key):
                parts.append("{0}: {1}".format(key, types[key]))
        return "   ".join(parts) if parts else "no files"

    def _job_counts_text(self, counts):
        parts = []
        for key in (pubsvc.JOB_DONE, pubsvc.JOB_RUNNING, pubsvc.JOB_NONE,
                    pubsvc.JOB_UNKNOWN, pubsvc.ERROR):
            if counts.get(key):
                parts.append("{0}: {1}".format(key, counts[key]))
        return "   ".join(parts) if parts else "-"

    # ---------------- grid ----------------
    def _visible(self):
        query = self._safe_text(self.search_tb)
        only_needed = self.only_needed_cb.IsChecked is True
        out = []
        for r in self._rows:
            if only_needed and r.publish_type != pubsvc.TYPE_OLD:
                continue
            if not matches_search(r.name, query):
                continue
            out.append(r)
        return out

    def _refresh_grid(self):
        try:
            self.files_grid.ItemsSource = None
            self.files_grid.ItemsSource = self._visible()
        except Exception:
            pass

    def _update_summary(self):
        ticked = len([r for r in self._rows if r.selected])
        self.summary_tb.Text = "{0} file(s) ticked to publish".format(ticked)
        try:
            self.publish_b.IsEnabled = ticked > 0
        except Exception:
            pass

    def search_changed(self, sender, args):
        self._refresh_grid()

    def clear_search_click(self, sender, args):
        self.search_tb.Text = ""
        self._refresh_grid()

    def only_needed_click(self, sender, args):
        self._refresh_grid()

    def select_needed_click(self, sender, args):
        for r in self._rows:
            if r.publish_type == pubsvc.TYPE_OLD:
                r.selected = True
        self._refresh_grid()
        self._update_summary()

    def select_none_click(self, sender, args):
        for r in self._rows:
            r.selected = False
        self._refresh_grid()
        self._update_summary()

    # ---------------- publish ----------------
    def publish_click(self, sender, args):
        self._guard(self._publish)

    def _publish(self):
        ticked = [r for r in self._rows if r.selected]
        if not ticked:
            forms.alert("Nothing is ticked.", title="DeePubCheck")
            return

        without_links = self.mode_no_links_rb.IsChecked is True
        mode_label = "without links" if without_links else "normal (with links)"

        unknowns = [r for r in ticked if r.publish_type != pubsvc.TYPE_OLD]
        extra = ""
        if unknowns:
            extra = ("\n\n{0} of them are NOT marked 'Needs publishing' - you "
                     "ticked them yourself. They will be published too."
                     .format(len(unknowns)))

        if not forms.alert(
                "Publish {0} file(s) to ACC?\n\nMode: {1}{2}\n\nThis changes "
                "what your whole team sees in ACC Docs. Publishing is queued "
                "on ACC and keeps running there after this window "
                "closes.".format(len(ticked), mode_label, extra),
                title="DeePubCheck - Confirm Publish", yes=True, no=True):
            return

        self._log("Publishing {0} file(s), {1}...".format(len(ticked), mode_label))
        self.logger.info("Publish starting", files=len(ticked), mode=mode_label)
        self._progress_begin(len(ticked))

        results = []
        ok_count = fail_count = 0
        for row in ticked:
            self._progress_step("Publishing {0}".format(row.name))
            # Logged BEFORE the call, so a crash or a closed window still
            # leaves a record of what was asked for.
            self.logger.info("Publishing", file=row.name, mode=mode_label)
            try:
                ok, detail = acc_api.publish_item(
                    self._project_id, row.item_id, without_links, self._token)
            except Exception as e:
                ok, detail = False, str(e)
            if ok:
                ok_count += 1
                row.status = pubsvc.JOB_RUNNING
                row.detail = detail
                row.selected = False
                self._log("  '{0}': {1}".format(row.name, detail))
            else:
                fail_count += 1
                self.logger.error("Publish failed", file=row.name, detail=detail)
                self._log("  '{0}': FAILED - {1}".format(row.name, detail))
            results.append((ok, row.name, detail))
            self._progress_done_one()

        self._progress_end()
        self._refresh_grid()
        self._update_summary()
        self._report(results)

        self.status_tb.Text = "{0} queued, {1} failed.".format(ok_count, fail_count)
        self._log(self.status_tb.Text)
        self.logger.info("Publish finished", queued=ok_count, failed=fail_count)

        forms.alert(
            "{0} file(s) queued for publishing, {1} failed.\n\nACC processes "
            "publishing in the background - a large model can take a while to "
            "finish. Re-run the status check later to confirm.".format(
                ok_count, fail_count),
            title="DeePubCheck - Done")

    def _report(self, results):
        html = ['<h2 style="font-family:sans-serif;color:#ddd;">DeePubCheck - Publish Results</h2>']
        for ok, label, detail in results:
            bg = "#2e7d32" if ok else "#c62828"
            html.append(
                '<div style="font-family:sans-serif;color:#fff;background:{0};'
                'padding:6px 10px;margin:3px 0;border-radius:3px;">'
                '<b>{1}</b><br/><span style="font-size:11px;">{2}</span></div>'.format(
                    bg, label, detail))
        try:
            script.get_output().print_html("".join(html))
        except Exception:
            pass

    def close_click(self, sender, args):
        self.Close()


if __name__ == "__main__":
    DeePubCheckWindow("ui.xaml").ShowDialog()
