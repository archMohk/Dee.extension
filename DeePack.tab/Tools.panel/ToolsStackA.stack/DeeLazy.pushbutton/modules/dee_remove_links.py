# -*- coding: utf-8 -*-
"""
DeeLazy - Remove Links module
Find the Revit links in many models at once, export their paths to
Excel, and/or remove them. Three places to scan, one tab each:

Open Models (this session)
    Every project document open in this Revit session. Links are read
    and removed in the open documents directly; saving / synchronizing
    afterwards is optional (off by default, so the user can review).

ACC Files
    Scan an ACC project or folder, tick host models. Links are read
    from ACC's published-version data (acc_links_service - no model is
    opened). A host ACC cannot report (old publish, no access...) is
    opened headless + DETACHED and read instead, so nothing is written.
    Removing opens each host ATTACHED, deletes the links and
    synchronizes back to ACC (or saves a non-workshared cloud model).

Local Files
    Add .rvt files / a folder. Each ticked file is opened DETACHED to
    read its links (TransmissionData.ReadTransmissionData is NOT used -
    DeeLinkMAP found it crashes Revit, see dee_linkmap_service).
    Removing opens the file attached, deletes, saves or synchronizes.

Removing a link = deleting its RevitLinkType (its instances go with it).
Nested links belong to the linked model and cannot be removed from the
host, so they are listed but never ticked or deleted.
"""
import os
import re
import time
import datetime
import traceback

import clr
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
import System
from System import GC

from pyrevit import forms
from Autodesk.Revit.DB import (
    FilteredElementCollector, RevitLinkType, RevitLinkInstance, ModelPathUtils, Element,
    OpenOptions, DetachFromCentralOption, WorksetConfiguration, WorksetConfigurationOption,
)

import dee_branding
import acc_auth
import acc_file_browser as afb
import deew_document_manager as docmgr
from dee_ui import SafeProgress


_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "DeeRemoveLinks.xaml")
_CACHE_FILE = os.path.join(_THIS_DIR, ".deeremovelinks_acc_cache.json")
_BACKUP_RE = re.compile(r"\.\d{4}\.rvt$", re.IGNORECASE)

MODE_SESSION, MODE_ACC, MODE_LOCAL = 0, 1, 2
_MODE_LABELS = {MODE_SESSION: "open models", MODE_ACC: "ACC files", MODE_LOCAL: "local files"}
_SYNC_COMMENT = "DeeLazy - Remove Links"

ST_REMOVED = "Removed"
ST_NESTED = "Nested (skip)"

_XLSX_HEADERS = ["Host Model", "Host Location", "Link Name", "Link Path",
                 "Type", "Status", "Instances", "Read From"]
_XLSX_WIDTHS = [34, 50, 34, 70, 12, 16, 10, 22]


# ==========================================================================
# rows
# ==========================================================================
class HostRow(object):
    """One ACC item or local file in the host lists."""
    def __init__(self, name, key, folder=""):
        self.name = name
        self.key = key          # ACC item_id or local full path
        self.folder = folder
        self.selected = False


class LinkRow(object):
    def __init__(self, host, host_key, host_location, link_name, path, attachment="",
                 status="", instances="", nested=False, type_id=None, read_from=""):
        self.host = host
        self.host_key = host_key            # session doc index / ACC item_id / local path
        self.host_location = host_location
        self.link_name = link_name
        self.path = path
        self.attachment = attachment
        self.status = status
        self.instances = instances
        self.nested = nested
        self.type_id = type_id              # RevitLinkType id (int) when read from a model
        self.read_from = read_from
        self.selected = not nested


# ==========================================================================
# Revit helpers (work on an already-open document - never open/close)
# ==========================================================================
def _eid(element_id):
    try:
        return int(element_id.Value)
    except Exception:
        pass
    try:
        return int(element_id.IntegerValue)
    except Exception:
        return 0


def _name(element):
    try:
        return element.Name
    except Exception:
        try:
            return Element.Name.GetValue(element)
        except Exception:
            return "?"


def _user_path(model_path):
    try:
        return ModelPathUtils.ConvertModelPathToUserVisiblePath(model_path)
    except Exception:
        return ""


def _doc_location(doc):
    try:
        if doc.IsModelInCloud:
            return _user_path(doc.GetCloudModelPath()) or "Cloud model"
    except Exception:
        pass
    try:
        return doc.PathName or "(not saved)"
    except Exception:
        return ""


def read_links(doc):
    """[{type_id, name, path, attachment, status, instances, nested}]."""
    counts = {}
    try:
        for inst in FilteredElementCollector(doc).OfClass(RevitLinkInstance):
            try:
                tid = _eid(inst.GetTypeId())
                counts[tid] = counts.get(tid, 0) + 1
            except Exception:
                continue
    except Exception:
        pass
    out = []
    try:
        types = list(FilteredElementCollector(doc).OfClass(RevitLinkType))
    except Exception:
        types = []
    for lt in types:
        tid = _eid(lt.Id)
        path, status = "", ""
        try:
            ext = lt.GetExternalFileReference()
            if ext is not None:
                try:
                    path = _user_path(ext.GetAbsolutePath())
                except Exception:
                    path = ""
                if not path:
                    path = _user_path(ext.GetPath())
                try:
                    status = str(ext.GetLinkedFileStatus())
                except Exception:
                    status = ""
        except Exception:
            pass
        attachment = ""
        try:
            attachment = str(lt.AttachmentType)
        except Exception:
            pass
        nested = False
        try:
            nested = bool(lt.IsNestedLink)
        except Exception:
            pass
        out.append({"type_id": tid, "name": _name(lt), "path": path, "attachment": attachment,
                    "status": status, "instances": counts.get(tid, 0), "nested": nested})
    return out


def remove_links(doc, type_ids=None, names=None, remove_all=False):
    """Deletes RevitLinkTypes (and with them their instances). Matches by
    type id first, then by link name (lower case). Nested links are
    skipped. Returns (removed, targeted, missing_names)."""
    import dee_transmit_service as ts   # lazy: heavy module, keep the launcher safe
    type_ids = set(type_ids or [])
    wanted_names = set((n or "").lower() for n in (names or []))
    found_names = set()
    targets = []
    for lt in FilteredElementCollector(doc).OfClass(RevitLinkType):
        try:
            if lt.IsNestedLink:
                continue
        except Exception:
            pass
        nm = (_name(lt) or "").lower()
        if remove_all or _eid(lt.Id) in type_ids or nm in wanted_names:
            targets.append(lt.Id)
            found_names.add(nm)
    if targets:
        ts.delete_ids(doc, targets, "DeeLazy - Remove Revit Links")
    removed = len([t for t in targets if doc.GetElement(t) is None])
    missing = sorted(n for n in wanted_names if n and n not in found_names)
    return removed, len(targets), missing


def save_after_remove(doc):
    """Sync a workshared (attached) model, plain Save otherwise."""
    if docmgr.is_workshared(doc) and not docmgr.is_detached(doc):
        return docmgr.synchronize_with_central(doc, comment=_SYNC_COMMENT)
    return docmgr.save_standalone(doc)


def _release():
    try:
        GC.Collect()
        GC.WaitForPendingFinalizers()
        GC.Collect()
    except Exception:
        pass


def _norm(path):
    return os.path.normcase(os.path.normpath(path or ""))


def _strip_rvt(name):
    name = (name or "").split("  [")[0].strip()
    return name[:-4] if name.lower().endswith(".rvt") else name


# ==========================================================================
# window
# ==========================================================================
class RemoveLinksWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, uiapp):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.uiapp = uiapp
        self.application = uiapp.Application

        self._links = {MODE_SESSION: [], MODE_ACC: [], MODE_LOCAL: []}
        self._ses_docs = []          # documents found by the last session scan
        self._acc_source = None      # dict: token, hub_id, region, hub_name, project_id, project_name
        self._acc_rows = []
        self._acc_guids = {}         # item_id -> (project Guid, model Guid), verified with ACC
        self._loc_rows = []
        self._shown_mode = None

        self._ready = True
        self._refresh_links()
        self._log("Ready. Pick a tab, scan, then export the link paths or remove the ticked links.")

    # ---------------- shared ----------------
    def _log(self, message):
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        try:
            self.log_tb.AppendText(u"[{0}] {1}\n".format(stamp, message))
            self.log_tb.ScrollToEnd()
        except Exception:
            pass

    def close_click(self, sender, args):
        self.Close()

    def _mode(self):
        idx = self.main_tabs.SelectedIndex
        return idx if idx in (MODE_SESSION, MODE_ACC, MODE_LOCAL) else MODE_SESSION

    def _with_dialog_handler(self, fn):
        dialog_log = []
        handler = afb.make_dialog_handler(dialog_log)
        self.uiapp.DialogBoxShowing += handler
        try:
            return fn()
        finally:
            try:
                self.uiapp.DialogBoxShowing -= handler
            except Exception:
                pass
            for text, _ok in dialog_log:
                self._log("  (auto-answered) " + text)

    def _open_doc_titles(self):
        titles = set()
        paths = set()
        for d in self.application.Documents:
            try:
                if d.IsLinked or d.IsFamilyDocument:
                    continue
                titles.add(_strip_rvt(d.Title).lower())
                if d.PathName:
                    paths.add(_norm(d.PathName))
            except Exception:
                continue
        return titles, paths

    # ---------------- links grid ----------------
    def tabs_changed(self, sender, args):
        # SelectionChanged bubbles up from the inner DataGrids too - only
        # react when the selected TAB actually changed.
        if not getattr(self, "_ready", False):
            return
        if self._mode() != self._shown_mode:
            self._refresh_links()

    def _visible_links(self):
        rows = self._links[self._mode()]
        text = (self.links_search_tb.Text or "").strip().lower()
        if not text:
            return list(rows)
        return [r for r in rows if text in (r.host or "").lower() or text in (r.link_name or "").lower()
                or text in (r.path or "").lower()]

    def _refresh_links(self):
        mode = self._mode()
        self._shown_mode = mode
        self.links_grid.ItemsSource = None
        self.links_grid.ItemsSource = self._visible_links()
        rows = self._links[mode]
        hosts = len(set(r.host_key for r in rows))
        self.links_title_tb.Text = "Revit links found in {0}".format(_MODE_LABELS[mode])
        if rows:
            self.links_info_tb.Text = "{0} link(s) in {1} model(s), {2} ticked.".format(
                len(rows), hosts, len([r for r in rows if r.selected]))
        else:
            self.links_info_tb.Text = "Nothing scanned on this tab yet."

    def links_search_changed(self, sender, args):
        if getattr(self, "_ready", False):
            self._refresh_links()

    def links_all_click(self, sender, args):
        for r in self._visible_links():
            if not r.nested and r.status != ST_REMOVED:
                r.selected = True
        self._refresh_links()

    def links_none_click(self, sender, args):
        for r in self._links[self._mode()]:
            r.selected = False
        self._refresh_links()

    def _commit_grids(self):
        for g in (self.links_grid, self.acc_grid, self.loc_grid):
            try:
                g.CommitEdit()
            except Exception:
                pass

    # ---------------- export ----------------
    def export_click(self, sender, args):
        self._commit_grids()
        mode = self._mode()
        rows = self._links[mode]
        if not rows:
            forms.alert("Scan first - there are no links to export on this tab.")
            return
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
        path = forms.save_file(file_ext="xlsx", default_name="Revit_Links_{0}".format(stamp))
        if not path:
            return
        try:
            import xlsx_writer
            data = []
            for r in rows:
                tag = None
                if r.status == ST_REMOVED:
                    tag = "ok"
                elif r.status and ("fail" in r.status.lower() or "notfound" in r.status.lower()):
                    tag = "fail"
                elif r.nested:
                    tag = "skip"
                data.append(([r.host, r.host_location, r.link_name, r.path, r.attachment,
                              r.status, r.instances, r.read_from], tag))
            title = "Revit Links - {0} - {1}".format(
                _MODE_LABELS[mode], datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
            xlsx_writer.write_themed_xlsx(path, title, _XLSX_HEADERS, _XLSX_WIDTHS, data)
        except Exception as e:
            self._log("Export failed: {0}".format(e))
            forms.alert("Could not write the Excel file:\n{0}".format(e))
            return
        self._log("Exported {0} link row(s) to {1}".format(len(rows), path))
        if forms.alert("Exported {0} link(s) to:\n{1}\n\nOpen it now?".format(len(rows), path),
                       yes=True, no=True):
            try:
                os.startfile(path)
            except Exception:
                pass

    # ---------------- remove (dispatch) ----------------
    def remove_click(self, sender, args):
        self._commit_grids()
        mode = self._mode()
        picked = [r for r in self._links[mode] if r.selected and not r.nested and r.status != ST_REMOVED]
        if not picked:
            forms.alert("Tick at least one link first (nested links cannot be removed from the host).")
            return
        by_host = {}
        order = []
        for r in picked:
            if r.host_key not in by_host:
                by_host[r.host_key] = []
                order.append(r.host_key)
            by_host[r.host_key].append(r)

        if mode == MODE_SESSION:
            save = bool(self.ses_save_cb.IsChecked)
            after = ("Each model is then saved / synchronized." if save
                     else "The models are NOT saved - review and save them yourself.")
        elif mode == MODE_ACC:
            after = "Each model is opened, cleaned and synchronized back to ACC (this changes the real ACC models)."
        else:
            after = "Each file is opened, cleaned and saved / synchronized (this changes the real files)."
        if not forms.alert("Remove {0} Revit link(s) from {1} model(s)?\n\n{2}\n\nThis cannot be undone from here."
                           .format(len(picked), len(order), after), yes=True, no=True):
            return

        if mode == MODE_SESSION:
            self._with_dialog_handler(lambda: self._remove_session(order, by_host, save))
        elif mode == MODE_ACC:
            self._with_dialog_handler(lambda: self._remove_acc(order, by_host))
        else:
            self._with_dialog_handler(lambda: self._remove_local(order, by_host))
        self._refresh_links()
        done = len([r for r in picked if r.status == ST_REMOVED])
        self._log("Remove finished: {0} of {1} link(s) removed.".format(done, len(picked)))
        forms.alert("{0} of {1} link(s) removed.\n\nSee the log at the bottom of the window for details."
                    .format(done, len(picked)), title="Remove Links")

    def _all_rows_of_host(self, mode, key):
        return [r for r in self._links[mode] if r.host_key == key]

    def _apply(self, doc, mode, key, rows):
        """Removes `rows` from an open doc and marks them. Returns removed count."""
        host_rows = [r for r in self._all_rows_of_host(mode, key) if not r.nested and r.status != ST_REMOVED]
        remove_all = len(rows) == len(host_rows)
        ids = [r.type_id for r in rows if r.type_id]
        names = [r.link_name for r in rows]
        removed, targeted, missing = remove_links(doc, ids, names, remove_all=remove_all)
        still = set(_eid(lt.Id) for lt in FilteredElementCollector(doc).OfClass(RevitLinkType))
        still_names = set((_name(lt) or "").lower()
                          for lt in FilteredElementCollector(doc).OfClass(RevitLinkType))
        for r in rows:
            gone = (r.type_id not in still) if r.type_id else ((r.link_name or "").lower() not in still_names)
            r.status = ST_REMOVED if gone else "Remove failed"
            r.selected = False
        if missing:
            self._log("  not found in the model: " + ", ".join(missing))
        self._log("  removed {0} of {1} link type(s)".format(removed, targeted))
        return removed

    # ---------------- Tab 1: open session ----------------
    def ses_scan_click(self, sender, args):
        self._ses_docs = []
        rows = []
        for d in self.application.Documents:
            try:
                if d.IsLinked or d.IsFamilyDocument:
                    continue
            except Exception:
                continue
            index = len(self._ses_docs)
            self._ses_docs.append(d)
            title = d.Title
            location = _doc_location(d)
            links = read_links(d)
            for lk in links:
                rows.append(LinkRow(title, index, location, lk["name"], lk["path"], lk["attachment"],
                                    ST_NESTED if lk["nested"] else lk["status"], lk["instances"],
                                    nested=lk["nested"], type_id=lk["type_id"], read_from="Open model"))
            self._log("{0}: {1} link(s)".format(title, len(links)))
        self._links[MODE_SESSION] = rows
        self.ses_info_tb.Text = "{0} open model(s), {1} link(s).".format(len(self._ses_docs), len(rows))
        self._refresh_links()

    def _remove_session(self, order, by_host, save):
        for key in order:
            doc = self._ses_docs[key]
            self._log("{0}:".format(doc.Title))
            try:
                self._apply(doc, MODE_SESSION, key, by_host[key])
                if save:
                    ok, detail = save_after_remove(doc)
                    self._log(u"  ✓ " + detail if ok else u"  ✗ save/sync failed: " + detail)
            except Exception as e:
                self._log(u"  ✗ " + str(e))

    # ---------------- Tab 2: ACC ----------------
    def acc_scan_click(self, sender, args):
        try:
            token = acc_auth.get_access_token()
        except Exception as e:
            forms.alert("Could not sign in to Autodesk:\n{0}".format(e))
            return
        if not token:
            return
        hub = afb.pick_hub(token)
        if not hub:
            return
        hub_id, region, hub_name = hub
        project = afb.pick_project(hub_id, token)
        if not project:
            return
        project_id, project_name = project
        how = forms.CommandSwitchWindow.show(["Whole project", "One folder"],
                                             message="Scan which part of '{0}'?".format(project_name))
        if not how:
            return
        where = project_name
        if how == "Whole project":
            items = afb.list_project_files(hub_id, project_id, token, _CACHE_FILE)
        else:
            folder = afb.pick_folder(hub_id, project_id, token)
            if not folder:
                return
            folder_id, folder_path = folder
            sub = forms.CommandSwitchWindow.show(["This Folder Only", "Include Subfolders"],
                                                 message="Scan '{0}':".format(folder_path))
            if not sub:
                return
            items = afb._scan_one_folder(project_id, folder_id, token, sub == "Include Subfolders", _CACHE_FILE)
            where = "{0} / {1}".format(project_name, folder_path)
        if not items:
            self._log("Scan found no Revit files.")
            return
        self._acc_source = {"token": token, "hub_id": hub_id, "region": region, "hub_name": hub_name,
                            "project_id": project_id, "project_name": project_name}
        self._acc_rows = [HostRow(name, items[name]) for name in sorted(items.keys())]
        self._acc_guids = {}
        self._acc_where = "{0} / {1}".format(hub_name, where)
        self.acc_info_tb.Text = "{0} Revit file(s) in {1}. Tick files, then Read Links.".format(
            len(self._acc_rows), self._acc_where)
        self._log("Scanned {0}: {1} file(s).".format(where, len(self._acc_rows)))
        self._acc_refresh()

    def _acc_visible(self):
        text = (self.acc_search_tb.Text or "").strip().lower()
        if not text:
            return list(self._acc_rows)
        return [r for r in self._acc_rows if text in r.name.lower()]

    def _acc_refresh(self):
        self.acc_grid.ItemsSource = None
        self.acc_grid.ItemsSource = self._acc_visible()

    def acc_search_changed(self, sender, args):
        if getattr(self, "_ready", False):
            self._acc_refresh()

    def acc_all_click(self, sender, args):
        for r in self._acc_visible():
            r.selected = True
        self.acc_grid.Items.Refresh()

    def acc_none_click(self, sender, args):
        for r in self._acc_rows:
            r.selected = False
        self.acc_grid.Items.Refresh()

    def acc_read_click(self, sender, args):
        self._commit_grids()
        hosts = [r for r in self._acc_rows if r.selected]
        if not hosts:
            forms.alert("Scan ACC and tick at least one file first.")
            return
        src = self._acc_source
        if not self._fresh_token():
            return
        import acc_links_service as als
        rows = []
        fallback = []
        with SafeProgress(window=self, title="Reading links from ACC...", cancellable=True) as pb:
            def progress(done, total):
                pb.update_progress(done, total)
            results = als.scan_project(src["project_id"], [(h.key, h.name) for h in hosts], src["token"],
                                       on_progress=progress, should_cancel=lambda: pb.cancelled)
        by_id = dict((h.key, h) for h in hosts)
        for res in results:
            host = by_id.get(res.get("item_id"))
            if host is None:
                continue
            self._remember_guids(host.key, res.get("project_guid"), res.get("model_guid"))
            if res.get("status") != "ok":
                fallback.append(host)
                self._log("{0}: ACC cannot report links ({1}) - will open the model".format(
                    host.name, res.get("reason") or res.get("status")))
                continue
            links = res.get("links") or []
            for lk in links:
                folder = lk.get("folder_path") or ""
                name = lk.get("name") or "?"
                path = (folder.rstrip("/") + "/" + name) if folder else name
                rows.append(LinkRow(host.name, host.key, self._acc_where, name, path, "", "In ACC (published)",
                                    "", read_from="ACC published data"))
            self._log("{0}: {1} link(s) (ACC)".format(host.name, len(links)))
        if fallback:
            rows += self._with_dialog_handler(lambda: self._acc_read_by_opening(fallback))
        self._links[MODE_ACC] = rows
        self._refresh_links()

    # ---------------- ACC: sign-in + cloud ids ----------------
    def _fresh_token(self):
        """Re-reads the Autodesk sign-in right before talking to ACC. A
        token kept from the scan can expire while the user reviews the
        list, and an expired token is what made every model fail to open
        with "The central model is missing" (see _acc_cloud_path)."""
        try:
            token = acc_auth.get_access_token()
        except Exception as e:
            self._log("Autodesk sign-in failed: {0}".format(e))
            forms.alert("Could not sign in to Autodesk: {0}".format(e))
            return None
        if token and self._acc_source is not None:
            self._acc_source["token"] = token
        return token

    def _remember_guids(self, item_id, project_guid, model_guid):
        if not (item_id and project_guid and model_guid):
            return
        try:
            self._acc_guids[item_id] = (System.Guid(str(project_guid)), System.Guid(str(model_guid)))
        except Exception:
            pass

    def _acc_cloud_path(self, item_id):
        """(ModelPath or None, detail).

        afb.get_cloud_path_guids never fails: when ACC does not answer
        (expired sign-in, rate limit) it quietly GUESSES the ids from the
        item URN ("lineage"). That guess is wrong for normal cloud models
        and Revit then refuses with "The central model is missing". So the
        ids are taken from the scan when ACC gave them, otherwise asked for
        again with a fresh token - and a guessed answer is never used."""
        src = self._acc_source
        ids = self._acc_guids.get(item_id)
        if ids is None:
            label = ""
            for attempt in range(3):
                token = self._fresh_token()
                if not token:
                    return None, "not signed in to Autodesk"
                try:
                    pg, mg, label = afb.get_cloud_path_guids(src["project_id"], item_id, token)
                except Exception as e:
                    label = "error: {0}".format(e)
                if label and not label.startswith("lineage") and not label.startswith("error"):
                    ids = (pg, mg)
                    self._acc_guids[item_id] = ids
                    break
                time.sleep(2 * (attempt + 1))
            if ids is None:
                return None, ("ACC did not return this model's cloud IDs ({0}) - the Autodesk sign-in may "
                              "have expired or ACC is busy. Sign in again and retry.".format(label or "no answer"))
        try:
            return ModelPathUtils.ConvertCloudGUIDsToCloudPath(src["region"], ids[0], ids[1]), "ok"
        except Exception as e:
            # stale ids from an earlier read - forget them so a retry asks ACC again
            self._acc_guids.pop(item_id, None)
            return None, "Revit could not find this cloud model: {0}".format(e)

    def _open_acc(self, item_id, attached):
        """Headless open. attached=True -> DoNotDetach (to change + sync the
        real model). attached=False -> detached when the model allows it
        (read-only use), falling back to DoNotDetach for a non-workshared
        model, which is still never saved by the read path."""
        model_path, detail = self._acc_cloud_path(item_id)
        if model_path is None:
            return None, detail
        if attached:
            attempts = [DetachFromCentralOption.DoNotDetach]
        else:
            attempts = [DetachFromCentralOption.DetachAndPreserveWorksets,
                        DetachFromCentralOption.DetachAndDiscardWorksets,
                        DetachFromCentralOption.DoNotDetach]
        errors = []
        for option in attempts:
            try:
                oo = OpenOptions()
                oo.DetachFromCentralOption = option
                try:
                    oo.SetOpenWorksetsConfiguration(WorksetConfiguration(WorksetConfigurationOption.OpenAllWorksets))
                except Exception:
                    pass
                return self.application.OpenDocumentFile(model_path, oo), str(option)
            except Exception as e:
                errors.append("{0}: {1}".format(option, e))
        return None, " | ".join(errors)

    def _acc_read_by_opening(self, hosts):
        src = self._acc_source
        rows = []
        with SafeProgress(window=self, title="Opening models to read links...", cancellable=True) as pb:
            for i, host in enumerate(hosts):
                if pb.cancelled:
                    self._log("Cancelled.")
                    break
                pb.update_progress(i, len(hosts))
                pb.title = "{0} ({1}/{2}) - opening read-only".format(host.name, i + 1, len(hosts))
                doc, detail = self._open_acc(host.key, attached=False)
                if doc is None:
                    self._log(u"  ✗ {0}: could not open - {1}".format(host.name, detail))
                    continue
                try:
                    links = read_links(doc)
                    for lk in links:
                        rows.append(LinkRow(host.name, host.key, self._acc_where, lk["name"], lk["path"],
                                            lk["attachment"], ST_NESTED if lk["nested"] else lk["status"],
                                            lk["instances"], nested=lk["nested"], type_id=lk["type_id"],
                                            read_from="Opened model"))
                    self._log("{0}: {1} link(s) (opened)".format(host.name, len(links)))
                finally:
                    docmgr.close_document(doc, save_modified=False)
                    _release()
            pb.update_progress(len(hosts), len(hosts))
        return rows

    def _remove_acc(self, order, by_host):
        if not self._fresh_token():
            return
        open_titles, _paths = self._open_doc_titles()
        names = dict((h.key, h.name) for h in self._acc_rows)
        with SafeProgress(window=self, title="Removing links...", cancellable=True) as pb:
            for i, key in enumerate(order):
                if pb.cancelled:
                    self._log("Cancelled.")
                    break
                name = names.get(key, by_host[key][0].host)
                pb.update_progress(i, len(order))
                pb.title = "{0} ({1}/{2})".format(name, i + 1, len(order))
                self._log("{0}:".format(name))
                if _strip_rvt(name).lower() in open_titles:
                    self._log(u"  ✗ skipped - this model is open in Revit; use the Open Models tab")
                    continue
                doc, detail = self._open_acc(key, attached=True)
                if doc is None:
                    self._log(u"  ✗ could not open - " + detail)
                    for r in by_host[key]:
                        r.status = "Open failed"
                    continue
                try:
                    removed = self._apply(doc, MODE_ACC, key, by_host[key])
                    if removed:
                        pb.title = "{0} ({1}/{2}) - synchronizing".format(name, i + 1, len(order))
                        ok, detail = save_after_remove(doc)
                        self._log(u"  ✓ " + detail if ok else u"  ✗ sync/save failed: " + detail)
                        if not ok:
                            for r in by_host[key]:
                                if r.status == ST_REMOVED:
                                    r.status = "Not synced"
                except Exception as e:
                    self._log(u"  ✗ " + str(e))
                finally:
                    docmgr.close_document(doc, save_modified=False)
                    _release()
            pb.update_progress(len(order), len(order))

    # ---------------- Tab 3: local ----------------
    def _loc_add(self, paths):
        known = set(_norm(r.key) for r in self._loc_rows)
        added = 0
        for p in paths:
            if not p or not p.lower().endswith(".rvt") or _BACKUP_RE.search(p):
                continue
            if _norm(p) in known:
                continue
            known.add(_norm(p))
            row = HostRow(os.path.basename(p), p, os.path.dirname(p))
            row.selected = True
            self._loc_rows.append(row)
            added += 1
        self._loc_rows.sort(key=lambda r: (r.folder.lower(), r.name.lower()))
        self.loc_grid.ItemsSource = None
        self.loc_grid.ItemsSource = self._loc_rows
        self.loc_info_tb.Text = "{0} file(s) in the list.".format(len(self._loc_rows))
        self._log("Added {0} file(s).".format(added))

    def loc_add_files_click(self, sender, args):
        picked = forms.pick_file(file_ext="rvt", multi_file=True)
        if picked:
            self._loc_add(picked if isinstance(picked, list) else [picked])

    def loc_add_folder_click(self, sender, args):
        folder = forms.pick_folder()
        if not folder:
            return
        sub = forms.CommandSwitchWindow.show(["This Folder Only", "Include Subfolders"],
                                             message="Scan '{0}':".format(folder))
        if not sub:
            return
        found = []
        if sub == "Include Subfolders":
            for root, dirs, files in os.walk(folder):
                dirs[:] = [d for d in dirs if not d.lower().endswith("_backup")]
                found += [os.path.join(root, f) for f in files]
        else:
            found = [os.path.join(folder, f) for f in os.listdir(folder)]
        self._loc_add(found)

    def loc_clear_click(self, sender, args):
        self._loc_rows = []
        self._links[MODE_LOCAL] = []
        self.loc_grid.ItemsSource = None
        self.loc_info_tb.Text = "List cleared."
        self._refresh_links()

    def loc_all_click(self, sender, args):
        for r in self._loc_rows:
            r.selected = True
        self.loc_grid.Items.Refresh()

    def loc_none_click(self, sender, args):
        for r in self._loc_rows:
            r.selected = False
        self.loc_grid.Items.Refresh()

    def loc_read_click(self, sender, args):
        self._commit_grids()
        hosts = [r for r in self._loc_rows if r.selected]
        if not hosts:
            forms.alert("Add and tick at least one file first.")
            return
        rows = self._with_dialog_handler(lambda: self._loc_read(hosts))
        self._links[MODE_LOCAL] = rows
        self._refresh_links()

    def _loc_read(self, hosts):
        rows = []
        _titles, open_paths = self._open_doc_titles()
        with SafeProgress(window=self, title="Reading links...", cancellable=True) as pb:
            for i, host in enumerate(hosts):
                if pb.cancelled:
                    self._log("Cancelled.")
                    break
                pb.update_progress(i, len(hosts))
                pb.title = "{0} ({1}/{2})".format(host.name, i + 1, len(hosts))
                if _norm(host.key) in open_paths:
                    self._log(u"  ✗ {0}: open in Revit - use the Open Models tab".format(host.name))
                    continue
                doc, detail = docmgr.open_document_best_detach(self.application, host.key)
                if doc is None:
                    self._log(u"  ✗ {0}: could not open - {1}".format(host.name, detail))
                    continue
                try:
                    links = read_links(doc)
                    for lk in links:
                        rows.append(LinkRow(host.name, host.key, host.key, lk["name"], lk["path"],
                                            lk["attachment"], ST_NESTED if lk["nested"] else lk["status"],
                                            lk["instances"], nested=lk["nested"], type_id=lk["type_id"],
                                            read_from="Opened file"))
                    self._log("{0}: {1} link(s)".format(host.name, len(links)))
                except Exception as e:
                    self._log(u"  ✗ {0}: {1}".format(host.name, e))
                finally:
                    docmgr.close_document(doc, save_modified=False)
                    _release()
            pb.update_progress(len(hosts), len(hosts))
        return rows

    def _remove_local(self, order, by_host):
        _titles, open_paths = self._open_doc_titles()
        with SafeProgress(window=self, title="Removing links...", cancellable=True) as pb:
            for i, key in enumerate(order):
                if pb.cancelled:
                    self._log("Cancelled.")
                    break
                name = os.path.basename(key)
                pb.update_progress(i, len(order))
                pb.title = "{0} ({1}/{2})".format(name, i + 1, len(order))
                self._log("{0}:".format(name))
                if _norm(key) in open_paths:
                    self._log(u"  ✗ skipped - this file is open in Revit; use the Open Models tab")
                    continue
                doc, detail = docmgr.open_document_no_detach(self.application, key)
                if doc is None:
                    self._log(u"  ✗ could not open - " + detail)
                    for r in by_host[key]:
                        r.status = "Open failed"
                    continue
                try:
                    removed = self._apply(doc, MODE_LOCAL, key, by_host[key])
                    if removed:
                        ok, detail = save_after_remove(doc)
                        self._log(u"  ✓ " + detail if ok else u"  ✗ save/sync failed: " + detail)
                        if not ok:
                            for r in by_host[key]:
                                if r.status == ST_REMOVED:
                                    r.status = "Not saved"
                except Exception as e:
                    self._log(u"  ✗ " + str(e))
                finally:
                    docmgr.close_document(doc, save_modified=False)
                    _release()
            pb.update_progress(len(order), len(order))


def launch(uiapp):
    try:
        window = RemoveLinksWindow(_XAML_FILE, uiapp)
        window.ShowDialog()
    except Exception:
        forms.alert("Remove Links failed to start:\n\n" + traceback.format_exc())


TOOL_INFO = {
    "id": "remove_links",
    "title": "Remove Links",
    "description": "Find the Revit links in open models, ACC files or local files - export their paths to Excel or remove them in bulk.",
    "icon": u"✂",  # scissors - cutting links (the link emoji is taken by Links Into Views)
    "launch": launch,
}
