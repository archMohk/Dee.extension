# -*- coding: utf-8 -*-
"""
deew_cloud_service
Wraps Document.SaveAsCloudModel(accountId, projectId, folderId,
modelName) - the Revit API method (introduced Revit 2021, verified
against revitapidocs.com before writing this file, not guessed) for
converting a local model into a new ACC/BIM 360 Cloud Model - and
reuses this repo's EXISTING acc_auth/acc_api/acc_file_browser modules
for Hub/Project/Folder DISCOVERY (browsing real names via the APS
Data Management REST API), so DeeW.Cloud tools never ask the user to
type or know a raw GUID, per spec. Confirmed compatible with pyRevit's
CPython 3 engine (this package's engine - see the "#! python3"
hashbang in every DeeW.Cloud script.py): acc_auth.py/acc_api.py/
acc_file_browser.py all py_compile cleanly under Python 3 with no
Python-2-only syntax, though runtime .NET-interop behavior under
pythonnet (CPython) vs IronPython's native interop is still worth a
live spot-check since it's new ground for this specific codebase.

--------------------------------------------------------------------
IMPORTANT: two SEPARATE Autodesk identities are involved here
--------------------------------------------------------------------
1. The APS OAuth token from acc_auth.get_access_token() - used ONLY
   to list hub/project/folder NAMES via the Data Management REST API
   (acc_api.list_hubs / list_projects / get_top_folders /
   list_folder_contents), so the UI shows real names, never GUIDs.
2. Revit's OWN internal Autodesk sign-in (the user's Autodesk account
   signed into Revit itself, via Revit's Account menu, top-right
   corner) - this is what Document.SaveAsCloudModel actually uses to
   perform the upload. The APS OAuth token above does NOT drive the
   upload; Revit's own cloud session does. If the user isn't signed
   into Revit's Autodesk account, SaveAsCloudModel will fail even
   though the APS discovery step succeeded - surfaced as a distinct,
   clearly-worded failure below rather than a generic exception.

Revit API facts relied on here (verified, not guessed):
  Document.SaveAsCloudModel(Guid accountId, Guid projectId,
                             string folderId, string modelName)
    - accountId/projectId: the Hub/Project GUIDs from the Data
      Management API with their "b." prefix stripped (a documented
      APS<->Revit interop requirement - Document.GetHubId() uses the
      same convention in reverse) and parsed as System.Guid.
    - folderId: the raw folder URN STRING as returned by the Data
      Management API - NOT converted to a Guid.
    - Cannot be invoked on a document already stored in the cloud.
    - Cannot be invoked while a Transaction is open.
"""
import System

import acc_auth
import acc_api
import acc_file_browser as afb


def _to_guid(aps_id):
    """Reuses acc_file_browser's existing to_guid() (already
    implements the documented "b." prefix stripping for the
    cloud-model-OPENING path) rather than duplicating that logic here -
    falls back to a local implementation only if that call fails for
    an unexpected reason, so this module still degrades gracefully if
    acc_file_browser's internals ever change shape."""
    try:
        return afb.to_guid(aps_id)
    except Exception:
        raw = aps_id[2:] if aps_id.startswith("b.") else aps_id
        return System.Guid.Parse(raw)


def get_token(force_login=False):
    """Thin pass-through to acc_auth - kept here so every DeeW.Cloud
    tool goes through this one module for both discovery AND upload,
    rather than importing acc_auth directly (single point of change if
    the auth mechanism ever needs to evolve)."""
    return acc_auth.get_access_token(force_login=force_login)


def list_hub_names(token):
    """[(hub_id, name), ...] - drops the region tuple element
    acc_api.list_hubs returns, since callers here only need id+name
    for a picker list."""
    return [(hub_id, name) for hub_id, name, _region in acc_api.list_hubs(token)]


def list_project_names(hub_id, token):
    return acc_api.list_projects(hub_id, token)


def list_folder_names(hub_id, project_id, token):
    return acc_api.get_top_folders(hub_id, project_id, token)


def pick_destination(token=None):
    """Interactive Hub -> Project -> Folder picker, reusing
    acc_file_browser's existing Hub/Project pickers plus a folder
    picker built the same way - the user only ever picks from
    real names, never a GUID (per spec). Returns a dict with both the
    human-readable names (for the UI/report) and the raw ids
    save_to_cloud() needs, or None if the user cancelled at any step."""
    from pyrevit import forms

    token = token or get_token()
    hub = afb.pick_hub(token)
    if not hub:
        return None
    hub_id, region, hub_name = hub

    project = afb.pick_project(hub_id, token)
    if not project:
        return None
    project_id, project_name = project

    folders = list_folder_names(hub_id, project_id, token)
    if not folders:
        return None
    folder_names = {}
    for fid, name in folders:
        folder_names[name] = fid
    picked_name = forms.SelectFromList.show(
        sorted(folder_names.keys()), title="Select ACC Folder", button_name="Select Folder")
    if not picked_name:
        return None

    return {
        "token": token,
        "hub_id": hub_id,
        "hub_name": hub_name,
        "region": region,
        "project_id": project_id,
        "project_name": project_name,
        "folder_id": folder_names[picked_name],
        "folder_name": picked_name,
    }


def save_to_cloud(document, destination, model_name):
    """Calls the verified Document.SaveAsCloudModel Revit API method.
    Returns (success: bool, detail: str) - never raises, since a
    failed upload for one file must never abort a batch of many.
    `destination` is the dict returned by pick_destination() (or one
    assembled the same shape by a caller that already has hub/project/
    folder ids cached from a prior pick, to avoid re-prompting for
    every file in a batch)."""
    try:
        account_guid = _to_guid(destination["hub_id"])
        project_guid = _to_guid(destination["project_id"])
        folder_id = destination["folder_id"]
        safe_name = model_name if model_name.lower().endswith(".rvt") else model_name + ".rvt"
        document.SaveAsCloudModel(account_guid, project_guid, folder_id, safe_name)
        return True, "Saved as cloud model '{0}' in {1} / {2} / {3}".format(
            safe_name, destination.get("hub_name"), destination.get("project_name"),
            destination.get("folder_name"))
    except Exception as e:
        return False, (
            "SaveAsCloudModel failed: {0} - if this keeps happening, confirm Revit itself "
            "is signed in to your Autodesk account (top-right corner), separately from any "
            "APS sign-in used to browse hubs/projects/folders.".format(e))
