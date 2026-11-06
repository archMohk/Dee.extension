# -*- coding: utf-8 -*-
"""
DeeLazy - URS Files module
Pick ONE Revit model from ACC, choose how many copies you want, and the
tool creates that many NEW worksharing-enabled Cloud Models from it in
an ACC folder you pick.

--------------------------------------------------------------------
How it works (every step reuses a helper other Dee tools already use)
--------------------------------------------------------------------
1. Hub -> Project -> folder browse -> Revit file
   (acc_file_browser.pick_hub / pick_project / browse_and_pick_cloud_file).
2. Number of copies + base name ("<base>_01", "<base>_02", ...).
3. Destination Hub/Project/Folder (deew_cloud_service.pick_destination,
   or the same project as the source).
4. The source is opened HEADLESS and DETACHED
   (acc_file_browser.open_cloud_document_detached) and saved once to a
   local temp .rvt (deew_document_manager.save_copy_as). The ACC source
   model is never written to - it is closed with save_modified=False.
5. For each copy: the temp file is opened detached
   (deew_document_manager.open_document_best_detach), worksharing is
   enabled if it is not already (enable_worksharing), and the document
   is uploaded as a new Cloud Model (deew_cloud_service.save_to_cloud ->
   Document.SaveAsCloudModel). This is the exact open -> worksharing ->
   upload sequence DeeInitiator's InitiatorPipeline already runs.
6. The temp folder is deleted and a summary is shown.

Known open-time dialogs are auto-answered for the whole run via
acc_file_browser.make_dialog_handler, as the other batch ACC tools do.

Revit itself must be signed in to the Autodesk account that can write to
the destination folder - SaveAsCloudModel uploads with Revit's own
sign-in, not the APS token used for browsing (see deew_cloud_service).
"""
import os
import shutil
import tempfile
import datetime

from pyrevit import forms

import acc_auth
import acc_api
import acc_file_browser as afb
import deew_cloud_service as cloudsvc
import deew_document_manager as docmgr
from dee_ui import SafeProgress

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_CACHE_FILE = os.path.join(_THIS_DIR, ".deeursfiles_acc_cache.json")
_MAX_COPIES = 200


def _ask_copy_count():
    while True:
        text = forms.ask_for_string(
            default="1",
            prompt="How many copies of the selected file do you want? (1-{0})".format(_MAX_COPIES),
            title="URS Files - Number of Copies")
        if text is None:
            return None
        try:
            n = int(text.strip())
        except Exception:
            forms.alert("Please type a whole number.")
            continue
        if 1 <= n <= _MAX_COPIES:
            return n
        forms.alert("Please type a number between 1 and {0}.".format(_MAX_COPIES))


def _ask_base_name(default_base):
    text = forms.ask_for_string(
        default=default_base,
        prompt="Base name for the new files.\n"
               "Copies are named <base>_01, <base>_02, ... (a single copy uses the base name as is).",
        title="URS Files - File Name")
    if text is None:
        return None
    text = text.strip()
    if text.lower().endswith(".rvt"):
        text = text[:-4]
    bad = set('\\/:*?"<>|')
    text = "".join("_" if c in bad else c for c in text).strip()
    return text or None


def _build_names(base, count):
    if count == 1:
        return [base]
    width = max(2, len(str(count)))
    return ["{0}_{1}".format(base, str(i).zfill(width)) for i in range(1, count + 1)]


def _pick_destination(token, hub_id, hub_name, region, project_id, project_name):
    choice = forms.CommandSwitchWindow.show(
        ["Same project ({0})".format(project_name), "Another hub / project"],
        message="Where should the new Cloud Models be saved?")
    if not choice:
        return None
    if choice.startswith("Same project"):
        folder = afb.pick_folder(hub_id, project_id, token)
        if not folder:
            return None
        folder_id, folder_path = folder
        return {
            "token": token, "hub_id": hub_id, "hub_name": hub_name, "region": region,
            "project_id": project_id, "project_name": project_name,
            "folder_id": folder_id, "folder_name": folder_path,
        }
    return cloudsvc.pick_destination(token)


def _existing_names(project_id, token):
    """Names already used by cloud models in the destination project,
    lower-cased and without .rvt - best effort, empty set on failure."""
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


def run(uiapp):
    application = uiapp.Application

    # ---------- 1. pick the source file ----------
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

    picked = afb.browse_and_pick_cloud_file(hub_id, project_id, token, _CACHE_FILE)
    if not picked:
        return
    item_id, source_name = picked
    source_base = source_name.split("  [")[0]
    if source_base.lower().endswith(".rvt"):
        source_base = source_base[:-4]

    # ---------- 2. how many / what names ----------
    count = _ask_copy_count()
    if not count:
        return
    base = _ask_base_name(source_base)
    if not base:
        return
    names = _build_names(base, count)

    # ---------- 3. destination ----------
    destination = _pick_destination(token, hub_id, hub_name, region, project_id, project_name)
    if not destination:
        return

    taken = _existing_names(destination["project_id"], token)
    clashes = [n for n in names if n.lower() in taken]
    if clashes:
        listed = "\n".join(clashes[:15]) + ("\n..." if len(clashes) > 15 else "")
        if not forms.alert(
                "{0} name(s) already exist in '{1}' and will be skipped:\n\n{2}\n\nContinue with the rest?"
                .format(len(clashes), destination.get("project_name"), listed),
                yes=True, no=True):
            return
        names = [n for n in names if n not in clashes]
        if not names:
            return

    preview = "\n".join(names[:15]) + ("\n... ({0} total)".format(len(names)) if len(names) > 15 else "")
    if not forms.alert(
            "Source:  {0}\nCopies:  {1}\nDestination:  {2} / {3} / {4}\n\n"
            "Every copy will be a worksharing-enabled Cloud Model:\n\n{5}\n\nStart?"
            .format(source_name, len(names), destination.get("hub_name"),
                    destination.get("project_name"), destination.get("folder_name"), preview),
            yes=True, no=True):
        return

    # ---------- 4 + 5. copy, enable worksharing, upload ----------
    temp_dir = tempfile.mkdtemp(prefix="DeeURSFiles_")
    temp_path = os.path.join(temp_dir, base + ".rvt")
    results = []          # (name, ok, detail)
    dialog_log = []
    handler = afb.make_dialog_handler(dialog_log)
    uiapp.DialogBoxShowing += handler
    started = datetime.datetime.now()
    try:
        with SafeProgress(title="URS Files - opening source model...",
                          cancellable=True, indeterminate=True) as pb:
            src_doc, detail = afb.open_cloud_document_detached(
                application, region, project_id, item_id, token)
            if src_doc is None:
                forms.alert("Could not open '{0}':\n{1}".format(source_name, detail))
                return
            try:
                ok, detail = docmgr.save_copy_as(src_doc, temp_path, compact=True, overwrite=True)
            finally:
                docmgr.close_document(src_doc, save_modified=False)
            if not ok:
                forms.alert("Could not make a working copy of '{0}':\n{1}".format(source_name, detail))
                return

            pb.indeterminate = False
            for i, name in enumerate(names):
                if pb.cancelled:
                    for rest in names[i:]:
                        results.append((rest, False, "Cancelled"))
                    break
                pb.title = "URS Files - {0} ({1}/{2})".format(name, i + 1, len(names))
                pb.update_progress(i, len(names))
                doc = None
                try:
                    doc, detail = docmgr.open_document_best_detach(application, temp_path)
                    if doc is None:
                        results.append((name, False, "Could not open working copy: " + detail))
                        continue
                    if not docmgr.enable_worksharing(doc):
                        results.append((name, False, "Could not enable worksharing"))
                        continue
                    ok, detail = cloudsvc.save_to_cloud(doc, destination, name)
                    results.append((name, ok, detail))
                except Exception as e:
                    results.append((name, False, str(e)))
                finally:
                    if doc is not None:
                        docmgr.close_document(doc, save_modified=False)
            pb.update_progress(len(names), len(names))
    finally:
        try:
            uiapp.DialogBoxShowing -= handler
        except Exception:
            pass
        shutil.rmtree(temp_dir, ignore_errors=True)

    # ---------- 6. summary ----------
    done = [r for r in results if r[1]]
    failed = [r for r in results if not r[1]]
    elapsed = datetime.datetime.now() - started
    lines = ["{0} of {1} copies created as worksharing Cloud Models in {2} / {3} / {4}  ({5}s)."
             .format(len(done), len(results), destination.get("hub_name"),
                     destination.get("project_name"), destination.get("folder_name"),
                     int(elapsed.total_seconds()))]
    if done:
        lines.append("")
        lines.extend(u"✓ " + r[0] for r in done[:25])
        if len(done) > 25:
            lines.append("... and {0} more".format(len(done) - 25))
    if failed:
        lines.append("")
        lines.append("Failed:")
        lines.extend(u"✗ {0} - {1}".format(r[0], r[2]) for r in failed[:15])
    if clashes:
        lines.append("")
        lines.append("Skipped (name already in project): {0}".format(len(clashes)))
    forms.alert("\n".join(lines), title="URS Files")


def launch(uiapp):
    run(uiapp)


TOOL_INFO = {
    "id": "urs_files",
    "title": "URS Files",
    "description": "Pick a Revit file from ACC and duplicate it into any number of new worksharing-enabled Cloud Models.",
    "icon": u"\U0001F4D1",  # bookmark tabs - multiple copies of a file
    "launch": launch,
}
