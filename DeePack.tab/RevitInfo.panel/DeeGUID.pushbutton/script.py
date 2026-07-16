# -*- coding: utf-8 -*-
from pyrevit import forms, script
import base64
import json

import acc_auth
import acc_api
import System

output = script.get_output()

uidoc = __revit__.ActiveUIDocument
if not uidoc:
    forms.alert("Open a cloud model via File > Open > Cloud Model, then run this.")
    script.exit()

doc = uidoc.Document
if not doc.IsModelInCloud:
    forms.alert("'{0}' is not a cloud model. Open one via File > Open > Cloud Model first.".format(doc.Title))
    script.exit()

model_path        = doc.GetCloudModelPath()
real_project_guid = model_path.GetProjectGUID()
real_model_guid   = model_path.GetModelGUID()
real_region       = model_path.Region
real_str          = str(real_model_guid).lower()

output.print_md("## Ground truth (from open Revit model)")
output.print_md("**Title:**       `{0}`".format(doc.Title))
output.print_md("**ProjectGUID:** `{0}`".format(real_project_guid))
output.print_md("**ModelGUID:**   `{0}`  ← target".format(real_model_guid))
output.print_md("**Region:**      `{0}`".format(real_region))
output.print_md("---")

token      = acc_auth.get_access_token()
origin_project_id = "b." + str(real_project_guid)
target_name = doc.Title if doc.Title.lower().endswith(".rvt") else doc.Title + ".rvt"

output.print_md("**Origin project_id** (from Revit): `{0}`".format(origin_project_id))
output.print_md("Searching ALL hubs/projects for `{0}` …".format(target_name))
output.print_md("---")

# ── discover hub and locate item ──────────────────────────────────────────────
# Strategy: enumerate every accessible hub + project and search folder trees.
# We do NOT assume the file lives in the origin project - it may be a cross-project
# reference that APPEARS in a different project's folder structure.

found_item_id  = None
found_in_proj  = None
found_hub_id   = None

try:
    all_hubs = acc_api.list_hubs(token)
except Exception as ex:
    output.print_md("list_hubs failed: {0}".format(ex))
    all_hubs = []

output.print_md("**Hubs found:** {0}".format(len(all_hubs)))

for hid, hname, hregion in all_hubs:
    if found_item_id:
        break
    output.print_md("Scanning hub `{0}` ({1})…".format(hname, hid))
    try:
        projects = acc_api.list_projects(hid, token)
    except Exception as ex:
        output.print_md("  list_projects failed: {0}".format(ex))
        continue

    for pid, pname in projects:
        if found_item_id:
            break
        output.print_md("  Project `{0}` ({1})".format(pname, pid))
        try:
            top    = acc_api.get_top_folders(hid, pid, token)
            stack  = [fid for fid, _ in top]
            vis    = 0
            while stack and not found_item_id and vis < 300:
                fid = stack.pop()
                vis += 1
                sub, items = acc_api.list_folder_contents(pid, fid, token)
                for sfid, _ in sub:
                    stack.append(sfid)
                for iid, iname in items:
                    if iname == target_name:
                        found_item_id = iid
                        found_in_proj = pid
                        found_hub_id  = hid
                        break
        except Exception:
            pass

if not found_item_id:
    output.print_md("Could not find `{0}` in any accessible project.".format(target_name))
    script.exit()

output.print_md("---")
output.print_md("**Found in project:** `{0}`".format(found_in_proj))
output.print_md("**Found in hub:**     `{0}`".format(found_hub_id))
output.print_md("**item_id:**          `{0}`".format(found_item_id))
output.print_md("---")

# ── lineage-decode strategies ─────────────────────────────────────────────────
output.print_md("## A: Lineage URN decode strategies")

def decode_v1(iid):
    enc = iid.split(":")[-1] + "=="
    raw = base64.urlsafe_b64decode(enc[:len(enc)-len(enc)%4 if len(enc)%4 else len(enc)])
    enc2 = iid.split(":")[-1]; enc2 += "=" * (-len(enc2) % 4)
    raw = base64.urlsafe_b64decode(enc2)
    return System.Guid(System.Array[System.Byte](bytearray(raw)))

def decode_v2(iid):
    enc = iid.split(":")[-1]; enc += "=" * (-len(enc) % 4)
    raw = bytearray(base64.urlsafe_b64decode(enc))
    h   = ''.join('{0:02x}'.format(b) for b in raw)
    return System.Guid('{0}-{1}-{2}-{3}-{4}'.format(h[0:8],h[8:12],h[12:16],h[16:20],h[20:32]))

def decode_v3(iid):
    enc = iid.split(":")[-1]; enc += "=" * (-len(enc) % 4)
    raw = bytearray(base64.urlsafe_b64decode(enc))
    raw[0:4] = raw[0:4][::-1]; raw[4:6] = raw[4:6][::-1]; raw[6:8] = raw[6:8][::-1]
    return System.Guid(System.Array[System.Byte](raw))

for label, fn in [("v1 byte[]", decode_v1), ("v2 hex-str", decode_v2), ("v3 byte-swap", decode_v3)]:
    try:
        g   = fn(found_item_id)
        tag = "**MATCH**" if str(g).lower() == real_str else "mismatch"
        output.print_md("- **{0}:** `{1}` → {2}".format(label, g, tag))
    except Exception as ex:
        output.print_md("- **{0}:** ERROR {1}".format(label, ex))

output.print_md("---")

# ── item extension.data ───────────────────────────────────────────────────────
output.print_md("## B: Item extension.data")
item_ext_data = {}
try:
    resp     = acc_api._get(
        acc_api.BASE_URL + "/data/v1/projects/{0}/items/{1}".format(found_in_proj, found_item_id),
        token
    )
    item_ext = resp.get("data", {}).get("attributes", {}).get("extension", {})
    output.print_md("**ext.type:** `{0}`".format(item_ext.get("type", "(none)")))
    item_ext_data = item_ext.get("data", {})
    if item_ext_data:
        for k, v in item_ext_data.items():
            tag = " ← **MATCH model**" if str(v).lower() == real_str else (
                  " ← **MATCH project**" if str(v).lower() == str(real_project_guid).lower() else "")
            output.print_md("  - `{0}` = `{1}`{2}".format(k, v, tag))
    else:
        output.print_md("  (empty)")
except Exception as ex:
    output.print_md("Item fetch error: {0}".format(ex))

output.print_md("---")

# ── tip version extension.data ────────────────────────────────────────────────
output.print_md("## C: Tip version extension.data")
tip_version_id = None
tip_ext_data   = {}
try:
    tip_resp = acc_api._get(
        acc_api.BASE_URL + "/data/v1/projects/{0}/items/{1}/tip".format(found_in_proj, found_item_id),
        token
    )
    tip_d          = tip_resp.get("data", {})
    tip_version_id = tip_d.get("id", "")
    output.print_md("**version id:** `{0}`".format(tip_version_id))
    tip_ext      = tip_d.get("attributes", {}).get("extension", {})
    output.print_md("**ext.type:** `{0}`".format(tip_ext.get("type", "(none)")))
    tip_ext_data = tip_ext.get("data", {})
    if tip_ext_data:
        for k, v in tip_ext_data.items():
            tag = " ← **MATCH model**" if str(v).lower() == real_str else (
                  " ← **MATCH project**" if str(v).lower() == str(real_project_guid).lower() else "")
            output.print_md("  - `{0}` = `{1}`{2}".format(k, v, tag))
    else:
        output.print_md("  (empty)")
except Exception as ex:
    output.print_md("Tip version fetch error: {0}".format(ex))

output.print_md("---")

# ── version URN decode ────────────────────────────────────────────────────────
output.print_md("## D: Version URN (vf.) decode")
if tip_version_id and "vf." in tip_version_id:
    try:
        vf  = tip_version_id.split("vf.")[-1].split("?")[0]
        enc = vf + "=" * (-len(vf) % 4)
        raw = bytearray(base64.urlsafe_b64decode(enc))
        h   = ''.join('{0:02x}'.format(b) for b in raw)
        vg  = System.Guid('{0}-{1}-{2}-{3}-{4}'.format(h[0:8],h[8:12],h[12:16],h[16:20],h[20:32]))
        tag = "**MATCH**" if str(vg).lower() == real_str else "mismatch"
        output.print_md("- Decoded: `{0}` → {1}".format(vg, tag))
    except Exception as ex:
        output.print_md("Error: {0}".format(ex))
else:
    output.print_md("(no vf. version URN available)")

output.print_md("---")
output.print_md("## Summary")
output.print_md("Target ModelGUID = `{0}`  |  Target ProjectGUID = `{1}`".format(
    real_model_guid, real_project_guid))
