# -*- coding: utf-8 -*-
"""
DeeLinkReview
1. Batch-select cloud Revit files
2. Open each one (briefly) and read its linked RVT files via the Revit API
3. Show which linked files are shared across the batch, with how many
   files reference each one
4. You pick ONE linked file to review - if multiple host files reference
   it, you then pick which host file to open
5. Opens that host file and Revit's own Coordination Review dialog

There is no public Revit API for Coordination Review's status, changes,
or Accept/Reject/Postpone actions - this tool only gets you to the dialog
faster across a batch; the actual review decisions are yours, made in
Revit's own UI.
"""
from pyrevit import forms, script
from Autodesk.Revit.DB import (
    OpenOptions, WorksetConfigurationOption, WorksetConfiguration,
    ModelPathUtils, FilteredElementCollector, RevitLinkType
)
from Autodesk.Revit.UI import RevitCommandId, PostableCommand, TaskDialogResult
import System
import threading
try:
    import Queue as queue
except ImportError:
    import queue
import json
import os
import base64
import time
import datetime

import acc_auth
import acc_api
import coordination_review_ui

output = script.get_output()

# shared cache with DeeOpener / DeePublisher / DeeCoord / DeeBIMview
_CACHE_FILE = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "DeeOpener.pushbutton", ".acc_file_cache.json"
))


def _load_cache(project_id):
    try:
        with open(_CACHE_FILE, "r") as fh:
            cache = json.load(fh)
        entry = cache.get(project_id)
        if entry and isinstance(entry.get("files"), dict):
            return entry["ts"], entry["files"]
    except Exception:
        pass
    return None, None


def _save_cache(project_id, files, published_ids=None):
    try:
        try:
            with open(_CACHE_FILE, "r") as fh:
                cache = json.load(fh)
        except Exception:
            cache = {}
        cache[project_id] = {
            "ts": time.time(),
            "files": files,
            "published_ids": list(published_ids or []),
        }
        with open(_CACHE_FILE, "w") as fh:
            json.dump(cache, fh)
    except Exception:
        pass


def to_guid(aps_id):
    return System.Guid(aps_id.split(".")[-1])


def _lineage_to_guid_hexstr(item_id):
    encoded = item_id.split(":")[-1]
    padded = encoded + "=" * (-len(encoded) % 4)
    raw = bytearray(base64.urlsafe_b64decode(padded))
    h = ''.join('{0:02x}'.format(b) for b in raw)
    return System.Guid('{0}-{1}-{2}-{3}-{4}'.format(
        h[0:8], h[8:12], h[12:16], h[16:20], h[20:32]
    ))


def get_cloud_path_guids(browsing_project_id, item_id, token):
    """Same cross-project-safe GUID resolution used by DeeOpener."""
    PROJ_FIELDS = ("projectGuid", "projectId", "project_guid")
    MODEL_FIELDS = ("modelId", "modelGuid", "c4rModelId", "model_id")

    def _extract(ext_data, label):
        mguid = None
        for f in MODEL_FIELDS:
            v = ext_data.get(f)
            if v and isinstance(v, str) and '-' in v:
                try:
                    mguid = System.Guid(v)
                    break
                except Exception:
                    pass
        if mguid is None:
            return None
        for f in PROJ_FIELDS:
            v = ext_data.get(f)
            if v and isinstance(v, str) and '-' in v:
                try:
                    return System.Guid(v), mguid, label + "+" + f
                except Exception:
                    pass
        return to_guid(browsing_project_id), mguid, label + "+browsing-proj"

    try:
        resp = acc_api._get(
            acc_api.BASE_URL + "/data/v1/projects/{0}/items/{1}".format(
                browsing_project_id, item_id),
            token
        )
        ext = (resp.get("data", {})
                   .get("attributes", {})
                   .get("extension", {})
                   .get("data", {}))
        result = _extract(ext, "item.ext")
        if result:
            return result
    except Exception:
        pass

    try:
        tip = acc_api._get(
            acc_api.BASE_URL + "/data/v1/projects/{0}/items/{1}/tip".format(
                browsing_project_id, item_id),
            token
        )
        tip_d = tip.get("data", {})
        tip_ext = tip_d.get("attributes", {}).get("extension", {}).get("data", {})
        result = _extract(tip_ext, "tip.ext")
        if result:
            return result

        tip_id = tip_d.get("id", "")
        if "vf." in tip_id:
            vf = tip_id.split("vf.")[-1].split("?")[0]
            enc = vf + "=" * (-len(vf) % 4)
            raw = bytearray(base64.urlsafe_b64decode(enc))
            h = ''.join('{0:02x}'.format(b) for b in raw)
            return (
                to_guid(browsing_project_id),
                System.Guid('{0}-{1}-{2}-{3}-{4}'.format(
                    h[0:8], h[8:12], h[12:16], h[16:20], h[20:32])),
                "vf-urn"
            )
    except Exception:
        pass

    return to_guid(browsing_project_id), _lineage_to_guid_hexstr(item_id), "lineage"


def scan_level(level, project_id, token, max_workers=8):
    next_level = []
    found_items = []
    lock = threading.Lock()
    q = queue.Queue()
    for fid in level:
        q.put(fid)

    def worker():
        while True:
            try:
                fid = q.get_nowait()
            except queue.Empty:
                return
            try:
                for attempt in range(3):
                    try:
                        subfolders, items = acc_api.list_folder_contents(project_id, fid, token)
                        with lock:
                            for sfid, _n in subfolders:
                                next_level.append(sfid)
                            for iid, iname in items:
                                found_items.append((iid, iname))
                        break
                    except Exception:
                        if attempt < 2:
                            time.sleep(2)
            finally:
                q.task_done()

    workers = []
    for _ in range(min(max_workers, max(1, len(level)))):
        t = threading.Thread(target=worker)
        t.daemon = True
        t.start()
        workers.append(t)
    for t in workers:
        t.join()
    return next_level, found_items


# ── auto-handled dialogs ──────────────────────────────────────────────────
# Revit fires DialogBoxShowing for every modal dialog it's about to display,
# which lets us auto-answer known ones instead of the batch hanging on a
# popup no one is watching.
def _make_dialog_handler(log_list):
    def handler(sender, args):
        try:
            dialog_id = (args.DialogId or "") if hasattr(args, "DialogId") else ""
            message = ""
            try:
                message = args.Message or ""
            except Exception:
                pass
            text = (dialog_id + " " + message).lower()

            if "unresolved" in text or "could not find or read" in text:
                if hasattr(args, "OverrideResult"):
                    args.OverrideResult(int(TaskDialogResult.CommandLink2))
                    log_list.append((
                        "Auto-dismissed 'Unresolved References' dialog "
                        "(Ignore and continue opening the project)", True))
                return

            if "cannot be ignored" in text or "resaves the element to central" in text:
                if hasattr(args, "OverrideResult"):
                    args.OverrideResult(int(TaskDialogResult.Cancel))
                    log_list.append((
                        "Auto-dismissed 'element owned by another user' error (Cancel)", None))
                return

            if "extents greater than" in text or "will be truncated" in text:
                # CAD-link geometry-extents warning ("Click OK to continue,
                # Cancel to exit import") - a plain OK/Cancel TaskDialog, not
                # a command-link one, so it needs TaskDialogResult.Ok instead.
                if hasattr(args, "OverrideResult"):
                    args.OverrideResult(int(TaskDialogResult.Ok))
                    log_list.append((
                        "Auto-dismissed 'geometry extents/truncated' CAD warning (OK)", True))
                return

            if "numerical data" in text and "truncated" in text:
                # Informational, single-button ("Close") variant of the same
                # CAD numeric-range warning - distinct wording ("has been
                # truncated") from the OK/Cancel one above so it needs its
                # own match and its own result value.
                if hasattr(args, "OverrideResult"):
                    args.OverrideResult(int(TaskDialogResult.Close))
                    log_list.append((
                        "Auto-dismissed 'numerical data truncated' CAD warning (Close)", True))
        except Exception:
            pass
    return handler


def _get_link_names(doc):
    """Names of every linked RVT file referenced by this document."""
    names = set()
    for lt in FilteredElementCollector(doc).OfClass(RevitLinkType):
        try:
            nm = lt.Name
            if nm:
                names.add(nm)
        except Exception:
            continue
    return names


def _open_file(uiapp, region, project_id, item_id, token):
    proj_guid, model_guid, _src = get_cloud_path_guids(project_id, item_id, token)
    cloud_path = ModelPathUtils.ConvertCloudGUIDsToCloudPath(region, proj_guid, model_guid)
    open_options = OpenOptions()
    open_options.SetOpenWorksetsConfiguration(
        WorksetConfiguration(WorksetConfigurationOption.OpenAllWorksets))
    uidoc = uiapp.OpenAndActivateDocument(cloud_path, open_options, False)
    return uidoc.Document


def main():
    try:
        token = acc_auth.get_access_token()
    except Exception as e:
        forms.alert("Authentication failed:\n{0}".format(e))
        return

    hubs = acc_api.list_hubs(token)
    if not hubs:
        forms.alert("No ACC/BIM360 hubs found.")
        return
    hub_lookup = {name: (hub_id, region) for hub_id, name, region in hubs}
    hub_name = forms.SelectFromList.show(
        sorted(hub_lookup.keys()), title="DeeLinkReview — Select Hub")
    if not hub_name:
        return
    hub_id, region = hub_lookup[hub_name]

    projects = acc_api.list_projects(hub_id, token)
    if not projects:
        forms.alert("No projects found in that hub.")
        return
    project_lookup = {name: proj_id for proj_id, name in projects}
    project_name = forms.SelectFromList.show(
        sorted(project_lookup.keys()), title="DeeLinkReview — Select Project")
    if not project_name:
        return
    project_id = project_lookup[project_name]

    # ── load or build file list ───────────────────────────────────────────────
    cache_ts, cached_files = _load_cache(project_id)
    all_items = None
    if cached_files is not None:
        age_str = datetime.datetime.fromtimestamp(cache_ts).strftime("%Y-%m-%d %H:%M")
        choice = forms.CommandSwitchWindow.show(
            ["Use Cached List ({0} files, saved {1})".format(
                len(cached_files), age_str),
             "Rescan Project (may take several minutes)"],
            message="A cached file list exists for this project."
        )
        if not choice:
            return
        if "Use Cached" in choice:
            all_items = {str(k): str(v) for k, v in cached_files.items()}

    if all_items is None:
        all_items = {}
        native_ids = set()

        def _add(iid, iname):
            key = iname
            if key in all_items and all_items[key] != iid:
                key = "{0}  [{1}]".format(iname, iid.split(":")[-1][:8])
            all_items[key] = iid

        with forms.ProgressBar(title="Scanning project for Revit files...",
                               cancellable=True, indeterminate=True) as pb:
            pb.title = "Step 1/2 — Searching native cloud models..."
            for iid, iname in acc_api.search_cloud_models(project_id, token):
                native_ids.add(iid)
                _add(iid, iname)

            pb.title = "Step 2/2 — BFS folder scan..."
            top_folders = acc_api.get_top_folders(hub_id, project_id, token)
            level = [fid for fid, _ in top_folders]
            done = 0
            while level:
                if pb.cancelled:
                    forms.alert("Scan cancelled.")
                    return
                next_level, found_items = scan_level(level, project_id, token)
                for iid, iname in found_items:
                    _add(iid, iname)
                done += len(level)
                level = next_level
                pb.title = "Step 2/2 — Scanning ({0} done, {1} found)...".format(
                    done, len(all_items))

        if not all_items:
            forms.alert("No Revit files found in that project.")
            return
        _save_cache(project_id, all_items, set(all_items.values()) - native_ids)

    # ── select files to batch-scan for links ─────────────────────────────────
    selected_names = forms.SelectFromList.show(
        sorted(all_items.keys()),
        title="DeeLinkReview — Select files to scan for linked models "
              "({0} available)".format(len(all_items)),
        multiselect=True,
        button_name="Scan Selected"
    )
    if not selected_names:
        return

    uiapp = __revit__
    link_to_hosts = {}
    total = len(selected_names)
    prev_doc = None

    dismissed_log = []
    dialog_handler = _make_dialog_handler(dismissed_log)
    uiapp.DialogBoxShowing += dialog_handler

    try:
        with forms.ProgressBar(title="DeeLinkReview — scanning...", cancellable=True) as pb:
            for i, name in enumerate(selected_names):
                if pb.cancelled:
                    break
                pb.update_progress(i, total)
                pb.title = "File {0}/{1}: {2} — Opening".format(i + 1, total, name)
                output.print_md("**File {0}/{1}: {2}**".format(i + 1, total, name))

                item_id = all_items[name]
                before_count = len(dismissed_log)
                try:
                    doc = _open_file(uiapp, region, project_id, item_id, token)
                except Exception as e:
                    output.print_md("Open FAILED: {0}".format(e))
                    doc = None
                for msg, _sev in dismissed_log[before_count:]:
                    output.print_md(msg)

                # Close the previous file now that a new one is active (Revit
                # forbids Document.Close() on the active document).
                if prev_doc is not None:
                    try:
                        prev_doc.Close(False)
                    except Exception as e:
                        output.print_md("Close FAILED for previous file: {0}".format(e))
                    prev_doc = None

                if doc is None:
                    continue

                try:
                    link_names = _get_link_names(doc)
                except Exception as e:
                    output.print_md("Could not read links: {0}".format(e))
                    link_names = set()

                for ln in link_names:
                    link_to_hosts.setdefault(ln, []).append(name)

                prev_doc = doc

            # Close the very last (still-active) file the same deferred way
            # DeeCoord does - can't close it synchronously.
            if prev_doc is not None:
                try:
                    cmd_id = RevitCommandId.LookupPostableCommandId(PostableCommand.Close)
                    if uiapp.CanPostCommand(cmd_id):
                        uiapp.PostCommand(cmd_id)
                except Exception:
                    pass

        if not link_to_hosts:
            forms.alert("No linked Revit files found across the selected files.")
            return

        # ── pick a linked file to review ─────────────────────────────────────
        sorted_links = sorted(link_to_hosts.items(), key=lambda kv: -len(kv[1]))
        display_to_link = {}
        display_list = []
        for link_name, hosts in sorted_links:
            label = "{0}  (in {1}/{2} files)".format(link_name, len(hosts), total)
            display_list.append(label)
            display_to_link[label] = link_name

        chosen_display = forms.SelectFromList.show(
            display_list,
            title="DeeLinkReview — Select a linked file to review",
            multiselect=False,
            button_name="Continue"
        )
        if not chosen_display:
            return
        chosen_link = display_to_link[chosen_display]
        hosting_files = link_to_hosts[chosen_link]

        if len(hosting_files) > 1:
            host_choice = forms.SelectFromList.show(
                sorted(hosting_files),
                title="DeeLinkReview — '{0}' appears in {1} files — pick one to open".format(
                    chosen_link, len(hosting_files)),
                multiselect=False,
                button_name="Open"
            )
            if not host_choice:
                return
        else:
            host_choice = hosting_files[0]

        # ── open the chosen host file and Coordination Review ───────────────
        item_id = all_items[host_choice]
        before_count = len(dismissed_log)
        try:
            _open_file(uiapp, region, project_id, item_id, token)
        except Exception as e:
            forms.alert("Could not open '{0}':\n{1}".format(host_choice, e))
            return
        for msg, _sev in dismissed_log[before_count:]:
            output.print_md(msg)

        try:
            coordination_review_ui.open_coordination_review(uiapp)
            forms.alert(
                "Opened Coordination Review for '{0}'.\n\n"
                "Review the status/changes and choose Accept, Reject, or "
                "Postpone yourself in the dialog - there's no API to safely "
                "automate those actions.".format(host_choice)
            )
        except Exception as e:
            forms.alert(
                "'{0}' is now open, but Coordination Review couldn't be "
                "opened automatically:\n{1}\n\n"
                "Please open it manually: Collaborate tab > Coordinate "
                "panel > Coordination Review.".format(host_choice, e)
            )
    finally:
        uiapp.DialogBoxShowing -= dialog_handler


main()
