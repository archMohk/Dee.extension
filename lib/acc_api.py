# -*- coding: utf-8 -*-
"""Minimal Autodesk Platform Services Data Management API client,
just enough to browse ACC/BIM360 hubs > projects > folders and find
Revit (.rvt) files."""
import json

import clr
clr.AddReference("System")
clr.AddReference("System.Net.Http")
from System.Net.Http import HttpClient, HttpRequestMessage, HttpMethod
from System.Net.Http.Headers import AuthenticationHeaderValue

BASE_URL = "https://developer.api.autodesk.com"

# Reuse a single HttpClient (thread-safe) instead of creating one per
# request - avoids re-doing TCP/TLS handshakes on every call, which is
# what was making folder scans so slow.
_client = HttpClient()


def _get(url, token):
    request = HttpRequestMessage(HttpMethod.Get, url)
    request.Headers.Authorization = AuthenticationHeaderValue("Bearer", token)
    response = _client.SendAsync(request).Result
    body = response.Content.ReadAsStringAsync().Result
    if not response.IsSuccessStatusCode:
        raise Exception("GET {0} failed ({1}): {2}".format(url, response.StatusCode, body))
    return json.loads(body)


def list_hubs(token):
    """Returns (hub_id, name, region) tuples. The hub's own "region" field
    (US/EMEA/etc.) is the reliable source for cloud GUID path conversion -
    projects don't reliably expose it themselves."""
    data = _get(BASE_URL + "/project/v1/hubs", token)
    return [
        (h["id"], h["attributes"]["name"], h["attributes"].get("region", "US"))
        for h in data.get("data", [])
    ]


def list_projects(hub_id, token):
    data = _get(BASE_URL + "/project/v1/hubs/{0}/projects".format(hub_id), token)
    return [(p["id"], p["attributes"]["name"]) for p in data.get("data", [])]


def search_cloud_models(project_id, token):
    """
    Use the Data Management v2 search API to find every C4R cloud model in
    the project in one (paginated) query — far faster and more complete than
    walking the folder tree.  Returns [(item_id, display_name)].
    """
    items = []
    url = (BASE_URL +
           "/data/v2/projects/{0}/search"
           "?filter[attributes.extension.type]=items:autodesk.bim360:C4RModel"
           "&page[limit]=200").format(project_id)
    while url:
        try:
            data = _get(url, token)
        except Exception:
            break
        for entry in data.get("data", []):
            name = entry.get("attributes", {}).get("displayName", "")
            if name.lower().endswith(".rvt"):
                items.append((entry["id"], name))
        raw_next = data.get("links", {}).get("next")
        if isinstance(raw_next, dict):
            url = raw_next.get("href")
        elif isinstance(raw_next, str):
            url = raw_next
        else:
            url = None
    return items


def get_top_folders(hub_id, project_id, token):
    data = _get(
        BASE_URL + "/project/v1/hubs/{0}/projects/{1}/topFolders".format(hub_id, project_id),
        token
    )
    return [(f["id"], f["attributes"]["displayName"]) for f in data.get("data", [])]


def _patch(url, token, body_dict):
    """PATCH request — used for version state changes like publish."""
    try:
        from urllib import quote as _quote
    except ImportError:
        from urllib.parse import quote as _quote

    body_str = json.dumps(body_dict)
    request = HttpRequestMessage(HttpMethod("PATCH"), url)
    request.Headers.Authorization = AuthenticationHeaderValue("Bearer", token)

    from System.Net.Http import StringContent
    from System.Net.Http.Headers import MediaTypeHeaderValue
    content = StringContent(body_str)
    content.Headers.ContentType = MediaTypeHeaderValue("application/vnd.api+json")
    request.Content = content

    response = _client.SendAsync(request).Result
    body = response.Content.ReadAsStringAsync().Result
    if not response.IsSuccessStatusCode:
        raise Exception("PATCH {0} failed ({1}): {2}".format(url, response.StatusCode, body))
    return json.loads(body) if body.strip() else {}


def _post(url, token, body_dict=None, content_type="application/json"):
    """POST request."""
    from System.Net.Http import StringContent
    from System.Net.Http.Headers import MediaTypeHeaderValue

    request = HttpRequestMessage(HttpMethod.Post, url)
    request.Headers.Authorization = AuthenticationHeaderValue("Bearer", token)
    if body_dict is not None:
        body_str = json.dumps(body_dict)
        content = StringContent(body_str)
        content.Headers.ContentType = MediaTypeHeaderValue(content_type)
        request.Content = content

    response = _client.SendAsync(request).Result
    body = response.Content.ReadAsStringAsync().Result
    if not response.IsSuccessStatusCode:
        raise Exception("POST {0} failed ({1}): {2}".format(url, response.StatusCode, body))
    return json.loads(body) if body.strip() else {}


def publish_item(project_id, item_id, without_links, token):
    """
    Trigger ACC/BIM360 C4R model publish via the Commands API.
    POST /data/v1/projects/{id}/commands with C4RModelPublish — the same
    action the ACC Docs "Publish" button invokes.

    without_links=True  →  "Publish without Links"
    without_links=False →  "Normal Publish"

    Returns (success: bool, detail: str).
    """
    body = {
        "jsonapi": {"version": "1.0"},
        "data": {
            "type": "commands",
            "attributes": {
                "extension": {
                    "type": "commands:autodesk.bim360:C4RModelPublish",
                    "version": "1.0.0",
                    "data": {
                        "publishWithoutLinks": without_links
                    }
                }
            },
            "relationships": {
                "resources": {
                    "data": [
                        {"type": "items", "id": item_id}
                    ]
                }
            }
        }
    }

    url = BASE_URL + "/data/v1/projects/{0}/commands".format(project_id)

    try:
        _post(url, token, body, content_type="application/vnd.api+json")
        label = "without links" if without_links else "with links"
        return True, "queued ({0})".format(label)
    except Exception as e:
        return False, str(e)


def list_folder_contents(project_id, folder_id, token):
    """Returns (subfolders, rvt_items) as lists of (id, name) tuples.
    Any page failure raises an exception so the caller can retry the whole
    folder from page 1 — we never silently return partial results."""
    folders = []
    items = []
    url = BASE_URL + "/data/v1/projects/{0}/folders/{1}/contents?page[limit]=200".format(project_id, folder_id)

    while url:
        data = _get(url, token)  # let exceptions propagate; caller will retry
        for entry in data.get("data", []):
            entry_type = entry.get("type", "")
            name = entry.get("attributes", {}).get("displayName", "")
            if not name:
                continue
            if entry_type.startswith("folders"):
                folders.append((entry["id"], name))
            elif entry_type.startswith("items") and name.lower().endswith(".rvt"):
                items.append((entry["id"], name))

        raw_next = data.get("links", {}).get("next")
        if isinstance(raw_next, dict):
            url = raw_next.get("href")
        elif isinstance(raw_next, str):
            url = raw_next
        else:
            url = None

    return folders, items
