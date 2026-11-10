# -*- coding: utf-8 -*-
"""
DeeLazy - URS Files module
One window, two tabs:

Duplicate File
    Pick ONE Revit model from ACC, choose how many copies and a base
    name, and each copy is created as a NEW worksharing-enabled Cloud
    Model in the ACC folder you pick.

Levels & Grids Only
    Scan ACC (whole project or one folder) for Revit files, tick any
    number of them, and each one is copied into a NEW worksharing Cloud
    Model that keeps only its Levels and Grids (see
    dee_urs_levels_grids.py for what is removed and in what order).
    Optional: reset the coordinates to the internal origin.

The ACC source models are NEVER written to in either tab: each source
is opened headless and DETACHED (acc_file_browser.
open_cloud_document_detached) and closed with save_modified=False.

Upload path (identical in both tabs, the same sequence DeeInitiator's
InitiatorPipeline runs):
  detached source -> deew_document_manager.save_copy_as (local temp .rvt)
  -> open_document_best_detach -> enable_worksharing
  -> deew_cloud_service.save_to_cloud (Document.SaveAsCloudModel)
  -> close -> temp folder deleted.

Revit itself must be signed in to an Autodesk account that can write to
the destination folder - SaveAsCloudModel uploads with Revit's own
sign-in, not the APS token used for browsing (see deew_cloud_service).
"""
import os
import shutil
import tempfile
import datetime

import clr
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")

from pyrevit import forms

import dee_branding
import acc_auth
import acc_api
import acc_file_browser as afb
import deew_cloud_service as cloudsvc
import deew_document_manager as docmgr
from dee_ui import SafeProgress


_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "DeeURSFiles.xaml")
_CACHE_FILE = os.path.join(_THIS_DIR, ".deeursfiles_acc_cache.json")
_MAX_COPIES = 200
_BAD_CHARS = set('\\/:*?"<>|')
# values match dee_urs_levels_grids.ANCHOR_* (that module is imported
# lazily, so the strings are repeated here rather than imported)
_ANCHORS = [
    ("Lower-left grid intersection (A/1)", "intersection"),
    ("Centre of all grids", "centre"),
]


# ==========================================================================
# helpers
# ==========================================================================
def _clean_name(text):
    text = (text or "").strip()
    if text.lower().endswith(".rvt"):
        text = text[:-4]
    return "".join("_" if c in _BAD_CHARS else c for c in text).strip()


def _display_base(display_name):
    """'Model.rvt  [abcd1234]' -> 'Model'."""
    name = (display_name or "").split("  [")[0]
    if name.lower().endswith(".rvt"):
        name = name[:-4]
    return name


def _build_names(base, count):
    if count == 1:
        return [base]
    width = max(2, len(str(count)))
    return ["{0}_{1}".format(base, str(i).zfill(width)) for i in range(1, count + 1)]


def _existing_names(project_id, token):
    """Lower-cased names (no .rvt) of cloud models already in a project -
    best effort, empty set on failure."""
    names = set()
    try:
        for _iid, name in acc_api.search_cloud_models(project_id, token):
            n = (name or "").lower()
            if n.endswith(".rvt"):
                n = n[:-4]
            names.add(n)
    except Exception:
        pass
    return names


def _dest_text(dest):
    if not dest:
        return "(none)"
    return "{0} / {1} / {2}".format(dest.get("hub_name", "?"), dest.get("project_name", "?"),
                                    dest.get("folder_name", "?"))


class _Source(object):
    """Where scanned / picked files live: one hub + project."""
    def __init__(self, token, hub_id, region, hub_name, project_id, project_name):
        self.token = token
        self.hub_id = hub_id
        self.region = region
        self.hub_name = hub_name
        self.project_id = project_id
        self.project_name = project_name


class FileRow(object):
    def __init__(self, name, item_id):
        self.name = name
        self.item_id = item_id
        self.selected = False


def _pick_source():
    """Sign in -> hub -> project. Returns _Source or None."""
    try:
        token = acc_auth.get_access_token()
    except Exception as e:
        forms.alert("Could not sign in to Autodesk:\n{0}".format(e))
        return None
    if not token:
        return None
    hub = afb.pick_hub(token)
    if not hub:
        return None
    hub_id, region, hub_name = hub
    project = afb.pick_project(hub_id, token)
    if not project:
        return None
    project_id, project_name = project
    return _Source(token, hub_id, region, hub_name, project_id, project_name)


def _pick_destination(source):
    """Same project as `source` (folder pick only) or any hub/project."""
    options = []
    if source is not None:
        options.append("Same project ({0})".format(source.project_name))
    options.append("Pick hub / project / folder")
    choice = forms.CommandSwitchWindow.show(options, message="Where should the new Cloud Models be saved?")
    if not choice:
        return None
    if choice.startswith("Same project"):
        folder = afb.pick_folder(source.hub_id, source.project_id, source.token)
        if not folder:
            return None
        folder_id, folder_path = folder
        return {
            "token": source.token, "hub_id": source.hub_id, "hub_name": source.hub_name,
            "region": source.region, "project_id": source.project_id,
            "project_name": source.project_name,
            "folder_id": folder_id, "folder_name": folder_path,
        }
    token = source.token if source is not None else None
    return cloudsvc.pick_destination(token)


def _upload_copy(application, local_path, destination, name):
    """Opens the local working copy detached, makes sure it is workshared,
    uploads it as a new Cloud Model. Returns (ok, detail). Never raises."""
    doc = None
    try:
        doc, detail = docmgr.open_document_best_detach(application, local_path)
        if doc is None:
            return False, "Could not open working copy: " + detail
        if not docmgr.enable_worksharing(doc):
            return False, "Could not enable worksharing"
        return cloudsvc.save_to_cloud(doc, destination, name)
    except Exception as e:
        return False, str(e)
    finally:
        if doc is not None:
            docmgr.close_document(doc, save_modified=False)


# ==========================================================================
# window
# ==========================================================================
class URSFilesWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, uiapp):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.uiapp = uiapp
        self.application = uiapp.Application

        # Duplicate tab state
        self._dup_source = None      # _Source
        self._dup_item_id = None
        self._dup_name = None
        self._dup_dest = None

        # Levels & Grids tab state
        self._lg_source = None       # _Source
        self._lg_rows = []
        self._lg_dest = None
        self.lg_anchor_cb.ItemsSource = [label for label, _v in _ANCHORS]
        self.lg_anchor_cb.SelectedIndex = 0

        self._ready = True
        self._log("Ready.")

    # ---------------- shared ----------------
    def _log(self, message):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        try:
            self.log_tb.AppendText(u"[{0}] {1}\n".format(ts, message))
            self.log_tb.ScrollToEnd()
        except Exception:
            pass

    def close_click(self, sender, args):
        self.Close()

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

    # ======================================================================
    # Tab 1 - Duplicate
    # ======================================================================
    def dup_pick_click(self, sender, args):
        source = _pick_source()
        if source is None:
            return
        picked = afb.browse_and_pick_cloud_file(source.hub_id, source.project_id,
                                                source.token, _CACHE_FILE)
        if not picked:
            return
        self._dup_source = source
        self._dup_item_id, self._dup_name = picked
        self.dup_source_tb.Text = "{0} / {1} / {2}".format(source.hub_name, source.project_name,
                                                          self._dup_name)
        self.dup_base_tb.Text = _display_base(self._dup_name)
        self._log("Source picked: " + self._dup_name)

    def dup_dest_click(self, sender, args):
        dest = _pick_destination(self._dup_source)
        if dest:
            self._dup_dest = dest
            self.dup_dest_tb.Text = _dest_text(dest)
            self._log("Destination: " + _dest_text(dest))

    def _dup_count(self):
        try:
            n = int((self.dup_count_tb.Text or "").strip())
        except Exception:
            return None
        return n if 1 <= n <= _MAX_COPIES else None

    def dup_names_changed(self, sender, args):
        if not getattr(self, "_ready", False):
            return
        n = self._dup_count()
        base = _clean_name(self.dup_base_tb.Text)
        if n is None:
            self.dup_preview_tb.Text = "Type a number of copies between 1 and {0}.".format(_MAX_COPIES)
            return
        if not base:
            self.dup_preview_tb.Text = "Type a base name."
            return
        names = _build_names(base, n)
        shown = ", ".join(names[:4]) + (" ... {0}".format(names[-1]) if n > 4 else "")
        self.dup_preview_tb.Text = "{0} file(s): {1}".format(n, shown)

    def dup_run_click(self, sender, args):
        if self._dup_source is None:
            forms.alert("Pick a source file first.")
            return
        count = self._dup_count()
        if count is None:
            forms.alert("Type a number of copies between 1 and {0}.".format(_MAX_COPIES))
            return
        base = _clean_name(self.dup_base_tb.Text)
        if not base:
            forms.alert("Type a base name.")
            return
        if not self._dup_dest:
            forms.alert("Pick a destination first.")
            return
        dest = self._dup_dest
        token = self._dup_source.token
        names = _build_names(base, count)

        taken = _existing_names(dest["project_id"], token)
        clashes = [n for n in names if n.lower() in taken]
        if clashes:
            listed = "\n".join(clashes[:15]) + ("\n..." if len(clashes) > 15 else "")
            if not forms.alert("{0} name(s) already exist in '{1}' and will be skipped:\n\n{2}\n\n"
                               "Continue with the rest?".format(len(clashes), dest.get("project_name"), listed),
                               yes=True, no=True):
                return
            names = [n for n in names if n not in clashes]
            if not names:
                return

        if not forms.alert("Create {0} worksharing Cloud Model(s) from '{1}' in\n{2}?"
                           .format(len(names), self._dup_name, _dest_text(dest)), yes=True, no=True):
            return

        results = self._with_dialog_handler(lambda: self._dup_run(names, dest))
        done = len([r for r in results if r[1]])
        self._log("Duplicate finished: {0} of {1} created.".format(done, len(results)))
        forms.alert("{0} of {1} copies created as worksharing Cloud Models in\n{2}."
                    "\n\nSee the log at the bottom of the window for details."
                    .format(done, len(results), _dest_text(dest)), title="URS Files")

    def _dup_run(self, names, dest):
        src = self._dup_source
        results = []
        temp_dir = tempfile.mkdtemp(prefix="DeeURSFiles_")
        temp_path = os.path.join(temp_dir, "source.rvt")
        try:
            with SafeProgress(window=self, title="Opening source model...",
                              cancellable=True, indeterminate=True) as pb:
                self._log("Opening " + self._dup_name + " (detached)...")
                doc, detail = afb.open_cloud_document_detached(
                    self.application, src.region, src.project_id, self._dup_item_id, src.token)
                if doc is None:
                    self._log("FAILED to open source: " + detail)
                    return results
                try:
                    ok, detail = docmgr.save_copy_as(doc, temp_path, compact=True, overwrite=True)
                finally:
                    docmgr.close_document(doc, save_modified=False)
                if not ok:
                    self._log("FAILED to make working copy: " + detail)
                    return results

                pb.indeterminate = False
                for i, name in enumerate(names):
                    pb.title = "Uploading {0} ({1}/{2})".format(name, i + 1, len(names))
                    pb.update_progress(i, len(names))
                    ok, detail = _upload_copy(self.application, temp_path, dest, name)
                    results.append((name, ok, detail))
                    self._log((u"  ✓ " if ok else u"  ✗ ") + name + ("" if ok else " - " + detail))
                pb.update_progress(len(names), len(names))
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
        return results

    # ======================================================================
    # Tab 2 - Levels & Grids only
    # ======================================================================
    def lg_scan_click(self, sender, args):
        source = _pick_source()
        if source is None:
            return
        how = forms.CommandSwitchWindow.show(
            ["Whole project", "One folder"], message="Scan which part of '{0}'?".format(source.project_name))
        if not how:
            return
        items = None
        where = source.project_name
        if how == "Whole project":
            items = afb.list_project_files(source.hub_id, source.project_id, source.token, _CACHE_FILE)
        else:
            folder = afb.pick_folder(source.hub_id, source.project_id, source.token)
            if not folder:
                return
            folder_id, folder_path = folder
            sub = forms.CommandSwitchWindow.show(["This Folder Only", "Include Subfolders"],
                                                 message="Scan '{0}':".format(folder_path))
            if not sub:
                return
            items = afb._scan_one_folder(source.project_id, folder_id, source.token,
                                         sub == "Include Subfolders", _CACHE_FILE)
            where = "{0} / {1}".format(source.project_name, folder_path)
        if not items:
            self._log("Scan found no Revit files.")
            return
        self._lg_source = source
        self._lg_rows = [FileRow(name, items[name]) for name in sorted(items.keys())]
        self.lg_source_tb.Text = "{0} Revit file(s) in {1} / {2}".format(
            len(self._lg_rows), source.hub_name, where)
        self._log("Scanned {0}: {1} file(s).".format(where, len(self._lg_rows)))
        self._lg_refresh()

    def _lg_visible(self):
        text = (self.lg_search_tb.Text or "").strip().lower()
        if not text:
            return list(self._lg_rows)
        return [r for r in self._lg_rows if text in r.name.lower()]

    def _lg_refresh(self):
        self.lg_grid.ItemsSource = None
        self.lg_grid.ItemsSource = self._lg_visible()

    def lg_search_changed(self, sender, args):
        if getattr(self, "_ready", False):
            self._lg_refresh()

    def lg_all_click(self, sender, args):
        for r in self._lg_visible():
            r.selected = True
        self.lg_grid.Items.Refresh()

    def lg_none_click(self, sender, args):
        for r in self._lg_rows:
            r.selected = False
        self.lg_grid.Items.Refresh()

    def lg_dest_click(self, sender, args):
        dest = _pick_destination(self._lg_source)
        if dest:
            self._lg_dest = dest
            self.lg_dest_tb.Text = _dest_text(dest)
            self._log("Levels & Grids destination: " + _dest_text(dest))

    def lg_run_click(self, sender, args):
        try:
            self.lg_grid.CommitEdit()
        except Exception:
            pass
        picked = [r for r in self._lg_rows if r.selected]
        if not picked:
            forms.alert("Scan and tick at least one file first.")
            return
        if not self._lg_dest:
            forms.alert("Pick a destination first.")
            return
        suffix = _clean_name(self.lg_suffix_tb.Text) if (self.lg_suffix_tb.Text or "").strip() else ""
        dest = self._lg_dest
        reset = bool(self.lg_reset_cb.IsChecked)
        keep_views = bool(self.lg_keep_views_cb.IsChecked)
        purge = bool(self.lg_purge_cb.IsChecked)
        move = bool(self.lg_move_cb.IsChecked)
        keep_scope = bool(self.lg_scope_cb.IsChecked)
        idx = self.lg_anchor_cb.SelectedIndex
        anchor_label, anchor = _ANCHORS[idx if 0 <= idx < len(_ANCHORS) else 0]
        opts = {"reset": reset, "keep_views": keep_views, "purge": purge,
                "move": move, "anchor": anchor, "keep_scope": keep_scope}

        jobs = [(r, _clean_name(_display_base(r.name)) + suffix) for r in picked]
        taken = _existing_names(dest["project_id"], self._lg_source.token)
        clashes = [n for _r, n in jobs if n.lower() in taken]
        if clashes:
            listed = "\n".join(clashes[:15]) + ("\n..." if len(clashes) > 15 else "")
            if not forms.alert("{0} name(s) already exist in '{1}' and will be skipped:\n\n{2}\n\n"
                               "Continue with the rest?".format(len(clashes), dest.get("project_name"), listed),
                               yes=True, no=True):
                return
            jobs = [(r, n) for r, n in jobs if n not in clashes]
            if not jobs:
                return

        if not forms.alert(
                "{0} file(s) will be copied into NEW Cloud Models with only Levels and Grids kept.\n\n"
                "Move grids and levels to internal origin: {5}\nKeep + move scope boxes: {6}\n"
                "Reset coordinates: {1}\nKeep floor plans / elevations: {2}\nPurge unused: {3}\n"
                "Destination: {4}\n\nThe original files are not changed. Start?"
                .format(len(jobs), "Yes" if reset else "No", "Yes" if keep_views else "No",
                        "Yes" if purge else "No", _dest_text(dest),
                        ("Yes - " + anchor_label) if move else "No", "Yes" if keep_scope else "No"),
                yes=True, no=True):
            return

        results = self._with_dialog_handler(lambda: self._lg_run(jobs, dest, opts))
        done = len([r for r in results if r[1]])
        self._log("Levels & Grids finished: {0} of {1} created.".format(done, len(results)))
        forms.alert("{0} of {1} Levels & Grids file(s) created in\n{2}."
                    "\n\nSee the log at the bottom of the window for details."
                    .format(done, len(results), _dest_text(dest)), title="URS Files")

    def _lg_run(self, jobs, dest, opts):
        # imported here, not at module load, so a Revit-version API gap in
        # the cleaner can never stop the whole DeeLazy launcher from opening
        from modules import dee_urs_levels_grids as lg
        src = self._lg_source
        results = []
        temp_dir = tempfile.mkdtemp(prefix="DeeURSFiles_LG_")
        try:
            with SafeProgress(window=self, title="Levels & Grids...", cancellable=True) as pb:
                for i, (row, new_name) in enumerate(jobs):
                    pb.update_progress(i, len(jobs))
                    prefix = "{0} ({1}/{2})".format(new_name, i + 1, len(jobs))
                    pb.title = prefix + " - opening"
                    self._log("Opening {0} (detached)...".format(row.name))
                    temp_path = os.path.join(temp_dir, "work_{0}.rvt".format(i + 1))
                    doc, detail = afb.open_cloud_document_detached(
                        self.application, src.region, src.project_id, row.item_id, src.token)
                    if doc is None:
                        results.append((new_name, False, detail))
                        self._log(u"  ✗ could not open: " + detail)
                        continue
                    try:
                        def note(step, _p=prefix):
                            pb.title = _p + " - " + step

                        result, coord_notes = lg.strip_to_levels_and_grids(
                            doc, keep_views=opts["keep_views"], purge=opts["purge"],
                            reset_coords=opts["reset"], note=note,
                            move_to_origin=opts["move"], anchor_mode=opts["anchor"],
                            keep_scope_boxes=opts["keep_scope"])
                        levels, grids = lg.count_levels_and_grids(doc)
                        self._log("  removed: {0}".format(result.summary()))
                        kept = "  kept: {0} level(s), {1} grid(s)".format(levels, grids)
                        if opts["keep_scope"]:
                            boxes = lg.count_scope_boxes(doc)
                            kept += ", {0} scope box(es)".format(boxes)
                            if not boxes:
                                kept += " - this file had none, and Revit's API cannot create one"
                        self._log(kept)
                        for n in coord_notes:
                            self._log("  " + n)
                        for e in result.errors:
                            self._log("  ! " + e)
                        pb.title = prefix + " - saving working copy"
                        ok, detail = docmgr.save_copy_as(doc, temp_path, compact=True, overwrite=True)
                    except Exception as e:
                        ok, detail = False, str(e)
                    finally:
                        docmgr.close_document(doc, save_modified=False)
                    if not ok:
                        results.append((new_name, False, detail))
                        self._log(u"  ✗ could not save working copy: " + detail)
                        continue

                    pb.title = prefix + " - uploading"
                    ok, detail = _upload_copy(self.application, temp_path, dest, new_name)
                    results.append((new_name, ok, detail))
                    self._log((u"  ✓ uploaded " if ok else u"  ✗ ") + new_name +
                              ("" if ok else " - " + detail))
                    try:
                        os.remove(temp_path)
                    except Exception:
                        pass
                pb.update_progress(len(jobs), len(jobs))
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
        return results


def launch(uiapp):
    window = URSFilesWindow(_XAML_FILE, uiapp)
    window.ShowDialog()


TOOL_INFO = {
    "id": "urs_files",
    "title": "URS Files",
    "description": "ACC files in bulk: duplicate a model into N worksharing Cloud Models, or make Levels & Grids-only copies (optional coordinate reset).",
    "icon": u"\U0001F4D1",  # bookmark tabs - multiple copies of a file
    "category": "Files, Links & ACC",
    "launch": launch,
}
