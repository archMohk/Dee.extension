# -*- coding: utf-8 -*-
"""
DeeLazy - Updater module
Pick one or more ACC projects, see all their Revit files in one list,
tick the ones to update, and run:

  1. Synchronize - each ticked model is opened ATTACHED in one or more
     BACKGROUND Revits (lib/dee_background + dee_updater_worker),
     synchronized with central (relinquish everything) and closed. The
     user's own Revit session stays free.
  2. Publish - once the background Revits are done, every model that
     synced OK is published on ACC (acc_api.publish_item - the same
     command DeePublisher / DeeS.Publish / the ACC Docs Publish button
     use), with or without links.

Error files are passed over, never fatal: a model whose cloud IDs ACC
does not return, that is open in this session, or that a background
Revit cannot open is marked "Skipped" with the reason and the batch
carries on. A model that opened but failed to sync is NOT published.

Scanning reuses the shared ACC scan (acc_file_browser.list_project_files
- cached per project, the same cache/rescan flow as the other ACC tools).
"""
import os
import time
import datetime

import clr
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
import System

from pyrevit import forms

import dee_branding
import dee_background
import acc_auth
import acc_api
import acc_file_browser as afb
from dee_ui import SafeProgress


_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "DeeUpdater.xaml")
_CACHE_FILE = os.path.join(_THIS_DIR, ".deeupdater_acc_cache.json")
_SYNC_COMMENT = "DeeLazy - Updater"

ST_READY = ""
ST_QUEUED = "Queued (background)"
ST_SKIPPED = "Skipped"
ST_SYNCED = "Synced"
ST_SYNC_FAILED = "Sync failed"
ST_PUBLISHED = "Synced + Published"
ST_PUB_ONLY = "Published"
ST_PUB_FAILED = "Publish failed"
ST_NOT_RUN = "Not processed"
ST_LOW_DISK = "Not processed - low disk"
ST_OK = (ST_SYNCED, ST_PUBLISHED, ST_PUB_ONLY)


def _strip_rvt(name):
    name = (name or "").split("  [")[0].strip()
    return name[:-4] if name.lower().endswith(".rvt") else name


class FileRow(object):
    def __init__(self, project_id, project_name, name, item_id):
        self.project_id = project_id
        self.project = project_name
        self.name = name
        self.item_id = item_id
        self.key = "{0}|{1}".format(project_id, item_id)
        self.selected = False
        self.status = ST_READY
        self.detail = ""


class UpdaterWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, uiapp):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.uiapp = uiapp
        self.application = uiapp.Application
        self._hub = None             # dict: hub_id, region, hub_name
        self._token = None
        self._rows = []
        self._guids = {}             # row.key -> (project_guid str, model_guid str)
        self._job = None
        self._timer = None
        self._run_opts = None
        self._started = None
        self._dupes = {}            # duplicate row.key -> primary row (same cloud model)
        self._ready = True
        self._log("Ready. Pick ACC project(s), tick the files, then Run.")

    # ---------------- shared ----------------
    def _log(self, message):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        try:
            self.log_tb.AppendText(u"[{0}] {1}\n".format(ts, message))
            self.log_tb.ScrollToEnd()
        except Exception:
            pass

    def close_click(self, sender, args):
        if self._job is not None:
            if not forms.alert("A background job is still running. Closing this window stops watching it "
                               "(the background Revits finish their current model, then skip the rest).\n\n"
                               "Close anyway?", yes=True, no=True):
                return
            self._job.stop()
        self.Close()

    def _fresh_token(self):
        try:
            token = acc_auth.get_access_token()
        except Exception as e:
            self._log("Autodesk sign-in failed: {0}".format(e))
            forms.alert("Could not sign in to Autodesk:\n{0}".format(e))
            return None
        if token:
            self._token = token
        return token

    # ---------------- projects + scan ----------------
    def pick_click(self, sender, args):
        token = self._fresh_token()
        if not token:
            return
        hub = afb.pick_hub(token)
        if not hub:
            return
        hub_id, region, hub_name = hub
        try:
            projects = acc_api.list_projects(hub_id, token)
        except Exception as e:
            forms.alert("Could not list projects:\n{0}".format(e))
            return
        if not projects:
            forms.alert("No projects found in '{0}'.".format(hub_name))
            return
        lookup = {}
        for pid, pname in projects:
            label = pname if pname not in lookup else "{0}  [{1}]".format(pname, pid[-6:])
            lookup[label] = (pid, pname)
        picked = forms.SelectFromList.show(sorted(lookup.keys()), title="Updater - Select Project(s)",
                                           multiselect=True, button_name="Use Selected Projects")
        if not picked:
            return
        cache_mode = "cache" if self.cache_cb.IsChecked else "rescan"
        rows = []
        per_project = []
        warnings = []
        for label in picked:
            pid, pname = lookup[label]
            self._log("Scanning {0}...".format(pname))
            items = afb.list_project_files(hub_id, pid, token, _CACHE_FILE, cache_mode=cache_mode)
            for folder_name, err in (getattr(afb, "last_scan_warnings", None) or []):
                warnings.append((pname, folder_name, err))
            if not items:
                self._log("  no Revit files found (or scan cancelled).")
                per_project.append((pname, 0))
                continue
            for name in sorted(items.keys()):
                rows.append(FileRow(pid, pname, name, items[name]))
            per_project.append((pname, len(items)))
            self._log("  {0} Revit file(s).".format(len(items)))
        self._hub = {"hub_id": hub_id, "region": region, "hub_name": hub_name}
        self._rows = rows
        self._guids = {}
        for proj, folder, err in warnings:
            self._log(u"  ⚠ [{0}] folder '{1}' could not be scanned: {2}".format(proj, folder, err))
        self.info_tb.Text = "{0} Revit file(s) in {1} project(s) of {2}: {3}".format(
            len(rows), len(picked), hub_name,
            ", ".join("{0} ({1})".format(p, n) for p, n in per_project))
        self._refresh()

    def _visible(self):
        text = (self.search_tb.Text or "").strip().lower()
        if not text:
            return list(self._rows)
        words = text.split()
        return [r for r in self._rows
                if all(w in (r.name + " " + r.project + " " + r.status).lower() for w in words)]

    def _refresh(self):
        self.files_grid.ItemsSource = None
        self.files_grid.ItemsSource = self._visible()
        self._update_count()

    def _update_count(self):
        n = len([r for r in self._rows if r.selected])
        self.count_tb.Text = "{0} of {1} ticked".format(n, len(self._rows))

    def search_changed(self, sender, args):
        if getattr(self, "_ready", False):
            self._refresh()

    def all_click(self, sender, args):
        for r in self._visible():
            r.selected = True
        self.files_grid.Items.Refresh()
        self._update_count()

    def none_click(self, sender, args):
        for r in self._rows:
            r.selected = False
        self.files_grid.Items.Refresh()
        self._update_count()

    def failed_click(self, sender, args):
        """Ticks only the rows of the last run that did not finish OK."""
        n = 0
        for r in self._rows:
            r.selected = bool(r.status) and r.status not in ST_OK
            n += 1 if r.selected else 0
        self.files_grid.Items.Refresh()
        self._update_count()
        self._log("{0} failed / not processed file(s) ticked.".format(n))

    def _min_free_gb(self):
        try:
            return max(0.0, float((self.minfree_tb.Text or "").strip()))
        except Exception:
            return 15.0

    def grid_changed(self, sender, args):
        self._update_count()

    def _commit_grid(self):
        try:
            self.files_grid.CommitEdit()
            self.files_grid.CommitEdit()
        except Exception:
            pass

    # ---------------- run ----------------
    def _open_titles(self):
        titles = set()
        try:
            for d in self.application.Documents:
                try:
                    if not d.IsLinked and not d.IsFamilyDocument:
                        titles.add(_strip_rvt(d.Title).lower())
                except Exception:
                    pass
        except Exception:
            pass
        return titles

    def _resolve_guids(self, row):
        """(project_guid, model_guid) strings, or (None, reason). A guessed
        "lineage" answer is never used - Revit refuses it with "The
        central model is missing" (see dee_remove_links._acc_cloud_path)."""
        if row.key in self._guids:
            return self._guids[row.key]
        label = ""
        for attempt in range(3):
            token = self._token or self._fresh_token()
            if not token:
                return None, "not signed in to Autodesk"
            try:
                pg, mg, label = afb.get_cloud_path_guids(row.project_id, row.item_id, token)
            except Exception as e:
                label = "error: {0}".format(e)
            if label and not label.startswith("lineage") and not label.startswith("error"):
                self._guids[row.key] = (str(pg), str(mg))
                return self._guids[row.key]
            time.sleep(2 * (attempt + 1))
            self._fresh_token()
        return None, "ACC did not return this model's cloud IDs ({0})".format(label or "no answer")

    def run_click(self, sender, args):
        if self._job is not None:
            forms.alert("A background job is still running - wait for it to finish (or stop it) first.")
            return
        self._commit_grid()
        picked = [r for r in self._rows if r.selected]
        if not picked:
            forms.alert("Pick project(s) and tick at least one file first.")
            return
        do_sync = bool(self.sync_cb.IsChecked)
        do_publish = bool(self.publish_cb.IsChecked)
        if not do_sync and not do_publish:
            forms.alert("Tick Synchronize and/or Publish.")
            return
        without_links = self.pubmode_cb.SelectedIndex == 1
        instances = dee_background.instances_from_combo(self.par_cb, 2)
        steps = []
        if do_sync:
            steps.append("open each model in {0} background Revit(s), synchronize (relinquish all) and close it"
                         .format(min(instances, len(picked))))
        if do_publish:
            steps.append("publish {0} on ACC{1}".format(
                "without links" if without_links else "with links",
                " (only the models that synced OK)" if do_sync else ""))
        if not forms.alert("{0} file(s) will be updated:\n\n- {1}\n\nFiles that cannot be opened are skipped "
                           "and reported. Start?".format(len(picked), "\n- ".join(steps)), yes=True, no=True):
            return
        if not self._fresh_token():
            return
        for r in picked:
            r.status, r.detail = ST_READY, ""
        self._run_opts = {"sync": do_sync, "publish": do_publish, "without_links": without_links,
                          "picked": picked}
        self._started = time.time()
        if do_sync:
            if not self._start_sync(picked, instances):
                self._run_opts = None
            return
        self._publish(picked, without_links)
        self._finish()

    def _start_sync(self, picked, instances):
        open_titles = self._open_titles()
        items = []
        self._dupes = {}
        primary_of = {}             # model guid -> first row with it
        with SafeProgress(window=self, title="Resolving cloud models...", cancellable=True) as pb:
            for i, row in enumerate(picked):
                if pb.cancelled:
                    self._log("Cancelled.")
                    return False
                pb.update_progress(i, len(picked))
                pb.title = "Resolving cloud models ({0}/{1})...".format(i + 1, len(picked))
                if _strip_rvt(row.name).lower() in open_titles:
                    row.status, row.detail = ST_SKIPPED, "open in this Revit session - sync it here"
                    self._log(u"  ✗ {0}: skipped - {1}".format(row.name, row.detail))
                    continue
                pg, mg = self._resolve_guids(row)
                if pg is None:
                    row.status, row.detail = ST_SKIPPED, mg
                    self._log(u"  ✗ {0}: skipped - {1}".format(row.name, mg))
                    continue
                first = primary_of.get(mg.lower())
                row.status = ST_QUEUED
                if first is not None:
                    # the same cloud model listed in another folder - open it once only
                    self._dupes[row.key] = first
                    self._log(u"  = {0}: same cloud model as {1} [{2}] - opened once".format(
                        row.name, first.name, first.project))
                    continue
                primary_of[mg.lower()] = row
                items.append({"key": row.key, "name": row.name, "project": row.project,
                              "project_guid": pg, "model_guid": mg})
        self.files_grid.Items.Refresh()
        if not items:
            forms.alert("Nothing to do - no ticked model could be resolved on ACC.")
            return False
        try:
            job = dee_background.BackgroundJob.start(
                "updater", "dee_updater_worker", "process_model", items,
                {"region": self._hub["region"], "sync_comment": (self.comment_tb.Text or _SYNC_COMMENT).strip()
                 or _SYNC_COMMENT, "audit": bool(self.audit_cb.IsChecked),
                 "min_free_gb": self._min_free_gb(), "clean_cache": bool(self.clean_cb.IsChecked)},
                str(self.application.VersionNumber), instances=instances,
                extra_paths=[_THIS_DIR], log=self._log)
        except Exception as e:
            for r in self._rows:
                if r.status == ST_QUEUED:
                    r.status = ST_READY
            self.files_grid.Items.Refresh()
            forms.alert(str(e))
            return False
        self._job = job
        self._log("{0} background Revit(s) starting (about a minute each). {1} model(s) in total{2}."
                  .format(len(job.workers), len(items),
                          " ({0} duplicate listing(s) share a model)".format(len(self._dupes)) if self._dupes else ""))
        self._set_running(True)
        self._timer = dee_background.start_timer(
            job, lambda tag, line: self._log(u"[{0}] {1}".format(tag, line)), self._on_job_finished)
        return True

    def _set_running(self, running):
        for b in (self.run_b, self.pick_b):
            b.IsEnabled = not running
        self.stop_b.Visibility = System.Windows.Visibility.Visible if running else System.Windows.Visibility.Collapsed

    def stop_click(self, sender, args):
        if self._job is None:
            return
        self._job.stop()
        self._log("Stop requested - each background Revit finishes its current model, then skips the rest.")

    def _on_job_finished(self, job, how):
        self._job = None
        try:
            if self._timer is not None:
                self._timer.Stop()
        except Exception:
            pass
        self._timer = None
        self._set_running(False)
        opts = self._run_opts or {}
        results = job.results()
        synced = []
        worker_of = {}
        for w in job.workers:
            for k in w["keys"]:
                worker_of[k] = w
        for row in opts.get("picked", []):
            if row.status != ST_QUEUED or row.key in self._dupes:
                continue
            res = results.get(row.key)
            if res is None:
                w = worker_of.get(row.key)
                row.status = ST_NOT_RUN
                row.detail = ("background Revit {0} ended before reaching it - log: {1}".format(w["idx"], w["log"])
                              if w else "the background Revit ended before reaching it")
            elif res.get("low_disk"):
                row.status, row.detail = ST_LOW_DISK, res.get("detail") or "low disk space"
            elif not res.get("ok"):
                row.status, row.detail = ST_SKIPPED, "could not open: " + (res.get("detail") or "")
            elif res.get("synced"):
                row.status, row.detail = ST_SYNCED, res.get("sync_detail") or ""
                synced.append(row)
            else:
                row.status, row.detail = ST_SYNC_FAILED, res.get("sync_detail") or ""
        for w in job.workers:
            if w.get("finished") in ("exited", "failed"):
                self._log("Background Revit {0} stopped early ({1}) - its log: {2}".format(
                    w["idx"], w["finished"], w["log"]))
        if any(r.status == ST_LOW_DISK for r in opts.get("picked", [])):
            self._log("Some models were not opened because the disk was low - free space on C: and "
                      "use 'Tick Failed' to run them again.")
        if how == "exited":
            self._log("A background Revit ended without reporting - see the logs under {0}".format(job.root))
        elif how == "failed":
            self._log("A background job crashed - see the log above and the logs under {0}".format(job.root))
        self._log("Sync finished: {0} synced.".format(len(synced)))
        self.files_grid.Items.Refresh()
        if opts.get("publish") and synced:
            self._publish(synced, opts.get("without_links"))
        for key, first in self._dupes.items():
            row = self._row_by_key(key)
            if row is not None:
                row.status = first.status
                row.detail = u"same cloud model as {0} [{1}]".format(first.name, first.project)
        self._finish()

    def _row_by_key(self, key):
        for r in self._rows:
            if r.key == key:
                return r
        return None

    def _publish(self, rows, without_links):
        token = self._fresh_token()
        if not token:
            for r in rows:
                r.status, r.detail = ST_PUB_FAILED, "not signed in to Autodesk"
            return
        label = "without links" if without_links else "with links"
        with SafeProgress(window=self, title="Publishing...", cancellable=True) as pb:
            for i, row in enumerate(rows):
                if pb.cancelled:
                    self._log("Publish cancelled - {0} file(s) not published.".format(len(rows) - i))
                    break
                pb.update_progress(i, len(rows))
                pb.title = "Publishing {0} ({1}/{2})".format(row.name, i + 1, len(rows))
                ok, detail = acc_api.publish_item(row.project_id, row.item_id, without_links, token)
                if not ok and ("Unauthorized" in detail or "401" in detail):
                    token = self._fresh_token() or token
                    ok, detail = acc_api.publish_item(row.project_id, row.item_id, without_links, token)
                synced = row.status == ST_SYNCED
                if ok:
                    row.status = ST_PUBLISHED if synced else ST_PUB_ONLY
                    row.detail = "publish {0} ({1})".format(detail, label)
                else:
                    row.status = ST_PUB_FAILED
                    row.detail = ("synced, " if synced else "") + "publish failed: " + detail
                self._log((u"  ✓ published " if ok else u"  ✗ publish failed ") + row.name +
                          ("" if ok else " - " + detail))
            pb.update_progress(len(rows), len(rows))
        self.files_grid.Items.Refresh()

    def _finish(self):
        opts = self._run_opts or {}
        picked = opts.get("picked", [])
        self._run_opts = None
        counts = {}
        for r in picked:
            counts[r.status or ST_NOT_RUN] = counts.get(r.status or ST_NOT_RUN, 0) + 1
        mins = int((time.time() - (self._started or time.time())) / 60)
        summary = ", ".join("{0}: {1}".format(k, v) for k, v in sorted(counts.items()))
        self._log("Updater finished in {0} min - {1}".format(mins, summary))
        self.files_grid.Items.Refresh()
        problems = [r for r in picked if r.status not in (ST_SYNCED, ST_PUBLISHED, ST_PUB_ONLY)]
        text = "{0} file(s) processed in {1} min.\n\n{2}".format(len(picked), mins, summary.replace(", ", "\n"))
        if problems:
            listed = "\n".join(u"{0} - {1}".format(r.name, r.status) for r in problems[:12])
            text += "\n\nNeeds attention:\n" + listed + ("\n..." if len(problems) > 12 else "")
        forms.alert(text, title="Updater")


def launch(uiapp):
    window = UpdaterWindow(_XAML_FILE, uiapp)
    window.ShowDialog()


TOOL_INFO = {
    "id": "updater",
    "title": "Updater",
    "description": "Pick one or more ACC projects, tick files, and each one is opened in background Revit(s), synchronized, closed and published. Files that cannot open are skipped.",
    "icon": u"\U0001F504",  # anticlockwise arrows - refresh / update
    "category": "Files, Links & ACC",
    "launch": launch,
}
