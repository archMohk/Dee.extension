# -*- coding: utf-8 -*-
"""
DeeLazy - DeeISSUE module
Exports Issues, RFIs and Submittals from Autodesk Construction Cloud for
every project you tick - the Revit-side twin of the "ACC Projects
Dashboard" Chrome extension's full-data export.

NO MODEL NEEDED: this tool never touches a Revit document. Open Revit
(empty session is fine), click DeeISSUE, sign in to Autodesk, pick a hub
and the projects, export. Sign-in is the same APS PKCE flow every other
Dee ACC tool uses (acc_auth.get_access_token - the token is cached, so
after the first time there is no browser prompt).

Output is the SAME shape as the extension / acc_fetcher.py:
    {"generated_at": "YYYY-MM-DD", "projects": [names],
     "issues": [...], "rfi": [...], "submittals": [...]}
plus, optionally, one flat .csv (opens straight in Excel).

Endpoints (same as the extension):
    /construction/issues/v1/projects/{id}/issues  (+ issue-types)
    /construction/rfis/v3/... (falls back to v2)
    /construction/submittals/v2/projects/{id}/items
A project whose module is switched off simply returns an error for that
module - it is logged and skipped, never fatal.
"""
import io
import os
import json
import datetime

import clr
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")

from pyrevit import forms

import dee_branding
import acc_auth
import acc_api
from dee_ui import SafeProgress, pump

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "DeeISSUE.xaml")
_BASE = acc_api.BASE_URL


# ==========================================================================
# data helpers (pure Python - no WPF, no Revit)
# ==========================================================================
def _d10(value):
    """'2026-03-01T10:00:00Z' -> '2026-03-01'; falsy -> None."""
    return value[:10] if value else None


def _days_between(date_str, now):
    try:
        d = datetime.datetime.strptime(date_str[:10], "%Y-%m-%d")
        return (d - now).days
    except Exception:
        return None


def _due_status(due, now):
    if not due:
        return "no-date"
    days = _days_between(due, now)
    if days is None:
        return "no-date"
    if days < 0:
        return "overdue"
    if days <= 7:
        return "due-soon"
    return "ok"


def _days_open(created, now):
    d = _days_between(created, now) if created else None
    return None if d is None else max(0, -d)


def _name_of(obj):
    if isinstance(obj, dict):
        return obj.get("name") or obj.get("displayName") or ""
    return ""


def _paginate(token, url, limit=200):
    """offset/limit paging used by the construction APIs."""
    sep = "&" if "?" in url else "?"
    out = []
    offset = 0
    while True:
        data = acc_api.api_get("{0}{1}limit={2}&offset={3}".format(url, sep, limit, offset), token)
        items = data.get("results") or data.get("data") or data.get("items") or []
        out.extend(items)
        offset += len(items)
        total = (data.get("pagination") or {}).get("totalResults")
        if not items or (total is not None and offset >= total) or (total is None and len(items) < limit):
            break
    return out


def list_hub_projects(hub_id, token):
    """All projects in a hub as [(clean_id, name)] - follows page links
    (acc_api.list_projects only returns the first page)."""
    out = []
    url = "{0}/project/v1/hubs/{1}/projects?page[limit]=200".format(_BASE, hub_id)
    while url:
        data = acc_api.api_get(url, token)
        for p in data.get("data", []):
            raw = p.get("id", "")
            clean = raw[2:] if raw.startswith("b.") else raw
            out.append((clean, (p.get("attributes") or {}).get("name") or clean))
        nxt = data.get("links", {}).get("next") if isinstance(data.get("links"), dict) else None
        url = nxt.get("href") if isinstance(nxt, dict) else None
    return out


def fetch_issues(token, pid, pname, now):
    types = {}
    try:
        data = acc_api.api_get("{0}/construction/issues/v1/projects/{1}/issue-types?include=subtypes"
                               .format(_BASE, pid), token)
        for it in data.get("results", []):
            types[it.get("id")] = it.get("title") or "General"
            for st in it.get("subtypes", []) or []:
                types[st.get("id")] = u"{0} / {1}".format(it.get("title") or "", st.get("title") or "")
    except Exception:
        pass
    raw = _paginate(token, "{0}/construction/issues/v1/projects/{1}/issues".format(_BASE, pid), 200)
    rows = []
    for r in raw:
        due = _d10(r.get("dueDate"))
        created = _d10(r.get("createdAt") or r.get("assignedAt"))
        rows.append({
            "id": unicode(r.get("displayId") or r.get("id") or ""),
            "title": r.get("title") or "",
            "assignedTo": r.get("assignedTo") or "",
            "createdDate": created, "dueDate": due,
            "closedDate": _d10(r.get("closedAt")),
            "discipline": types.get(r.get("issueSubtypeId")) or types.get(r.get("issueTypeId")) or "General",
            "issueStatus": r.get("status") or "",
            "project": pname,
            "status": _due_status(due, now), "open": _days_open(created, now),
            "type": "issue",
        })
    return rows


def fetch_rfis(token, pid, pname, now):
    try:
        raw = _paginate(token, "{0}/construction/rfis/v3/projects/{1}/rfis".format(_BASE, pid), 100)
    except Exception:
        raw = _paginate(token, "{0}/construction/rfis/v2/projects/{1}/rfis".format(_BASE, pid), 100)
    rows = []
    for r in raw:
        due = _d10(r.get("dueDate"))
        created = _d10(r.get("createdAt"))
        rows.append({
            "id": unicode(r.get("displayId") or r.get("number") or r.get("id") or ""),
            "title": r.get("title") or r.get("subject") or "",
            "assignedTo": _name_of(r.get("createdBy")),
            "bic": _name_of(r.get("ballInCourt")),
            "createdDate": created, "dueDate": due,
            "closedDate": _d10(r.get("closedAt") or r.get("answeredAt")),
            "discipline": r.get("discipline") or "General",
            "issueStatus": r.get("status") or r.get("statusLabel") or "",
            "project": pname,
            "status": _due_status(due, now), "open": _days_open(created, now),
            "type": "rfi",
        })
    return rows


def fetch_submittals(token, pid, pname, now):
    raw = _paginate(token, "{0}/construction/submittals/v2/projects/{1}/items".format(_BASE, pid), 200)
    rows = []
    for r in raw:
        due = _d10(r.get("dueDate") or r.get("managerDueDate"))
        created = _d10(r.get("createdAt"))
        specs = r.get("specSections") or []
        spec = specs[0] if specs else None
        disc = ""
        if isinstance(spec, dict):
            disc = spec.get("title") or spec.get("code") or ""
        rows.append({
            "id": unicode(r.get("customIdentifierHumanReadable") or r.get("customIdentifier") or r.get("id") or ""),
            "title": r.get("title") or "",
            "assignedTo": _name_of(r.get("responsibleContractor")),
            "bic": _name_of(r.get("ballInCourt")),
            "createdDate": created, "dueDate": due,
            "closedDate": _d10(r.get("publishedAt") or r.get("closedAt")),
            "discipline": disc or "General",
            "issueStatus": r.get("status") or "",
            "project": pname,
            "status": _due_status(due, now), "open": _days_open(created, now),
            "type": "submittal",
        })
    return rows


_CSV_COLS = ["type", "project", "id", "title", "issueStatus", "discipline", "assignedTo", "bic",
             "createdDate", "dueDate", "closedDate", "status", "open"]


def _csv_cell(v):
    if v is None:
        v = u""
    if not isinstance(v, basestring):
        v = unicode(v)
    return u'"{0}"'.format(v.replace(u'"', u'""'))


def write_outputs(path_json, payload, also_csv):
    """Returns list of files written."""
    written = []
    with io.open(path_json, "w", encoding="utf-8") as f:
        f.write(unicode(json.dumps(payload, indent=2, ensure_ascii=False)))
    written.append(path_json)
    if also_csv:
        path_csv = os.path.splitext(path_json)[0] + ".csv"
        lines = [u",".join(_CSV_COLS)]
        for key in ("issues", "rfi", "submittals"):
            for row in payload[key]:
                lines.append(u",".join(_csv_cell(row.get(c)) for c in _CSV_COLS))
        with io.open(path_csv, "w", encoding="utf-8-sig") as f:   # BOM -> Excel reads UTF-8
            f.write(u"\r\n".join(lines) + u"\r\n")
        written.append(path_csv)
    return written


class ProjectRow(object):
    def __init__(self, pid, name):
        self.pid = pid
        self.name = name
        self.selected = False


# ==========================================================================
# window
# ==========================================================================
class DeeIssueWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, uiapp):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.uiapp = uiapp
        self._token = None
        self._hubs = []        # [(hub_id, name, region)]
        self._rows = []
        self._ready = True
        self._log("Ready. Click 'Connect to ACC' - no Revit model is needed.")
        try:
            if acc_auth.is_configured() and acc_auth.account_status(acc_auth.get_active_account()):
                self.connect_click(None, None)
        except Exception:
            pass

    def _log(self, message):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        try:
            self.log_tb.AppendText(u"[{0}] {1}\n".format(ts, message))
            self.log_tb.ScrollToEnd()
        except Exception:
            pass
        pump(self)

    def close_click(self, sender, args):
        self.Close()

    # ---------------- connect / hub / projects ----------------
    def connect_click(self, sender, args):
        try:
            self._log("Signing in to Autodesk (a browser tab opens the first time)...")
            self._token = acc_auth.get_access_token()
            self._hubs = acc_api.list_hubs(self._token)
        except Exception as e:
            self._log(u"! Could not connect: {0}".format(e))
            forms.alert(u"Could not connect to ACC:\n{0}".format(e))
            return
        self.hub_cb.ItemsSource = [h[1] for h in self._hubs]
        self._log("Connected - {0} hub(s).".format(len(self._hubs)))
        if self._hubs:
            self.hub_cb.SelectedIndex = 0

    def hub_changed(self, sender, args):
        if not getattr(self, "_ready", False) or self._token is None:
            return
        i = self.hub_cb.SelectedIndex
        if i < 0 or i >= len(self._hubs):
            return
        hub_id, name, _region = self._hubs[i]
        self._log(u"Loading projects of '{0}'...".format(name))
        try:
            projects = list_hub_projects(hub_id, self._token)
        except Exception as e:
            self._log(u"! Could not list projects: {0}".format(e))
            return
        self._rows = [ProjectRow(pid, n) for pid, n in sorted(projects, key=lambda p: p[1].lower())]
        self._refresh()
        self.export_b.IsEnabled = bool(self._rows)
        self._log("{0} project(s).".format(len(self._rows)))

    def _visible(self):
        text = (self.search_tb.Text or "").strip().lower()
        if not text:
            return list(self._rows)
        return [r for r in self._rows if text in r.name.lower()]

    def _refresh(self):
        self.proj_grid.ItemsSource = None
        self.proj_grid.ItemsSource = self._visible()
        self.count_tb.Text = "{0} of {1} ticked".format(len([r for r in self._rows if r.selected]), len(self._rows))

    def search_changed(self, sender, args):
        if getattr(self, "_ready", False):
            self._refresh()

    def all_click(self, sender, args):
        for r in self._visible():
            r.selected = True
        self._refresh()

    def none_click(self, sender, args):
        for r in self._rows:
            r.selected = False
        self._refresh()

    # ---------------- export ----------------
    def export_click(self, sender, args):
        picked = [r for r in self._rows if r.selected]
        if not picked:
            forms.alert("Tick at least one project.")
            return
        want_i = bool(self.issues_cb.IsChecked)
        want_r = bool(self.rfi_cb.IsChecked)
        want_s = bool(self.sub_cb.IsChecked)
        if not (want_i or want_r or want_s):
            forms.alert("Tick at least one of Issues / RFIs / Submittals.")
            return
        stamp = datetime.date.today().strftime("%Y-%m-%d")
        path = forms.save_file(file_ext="json", default_name="acc_data_{0}".format(stamp))
        if not path:
            return

        now = datetime.datetime.now()
        issues, rfis, subs = [], [], []
        total = len(picked)
        with SafeProgress(window=self, title="DeeISSUE {value} of {max_value}", cancellable=True) as pb:
            for n, row in enumerate(picked):
                pb.update_progress(n, total)
                self._log(u"[{0}/{1}] {2}".format(n + 1, total, row.name))
                for wanted, label, fn, bucket in (
                        (want_i, "Issues", fetch_issues, issues),
                        (want_r, "RFIs", fetch_rfis, rfis),
                        (want_s, "Submittals", fetch_submittals, subs)):
                    if not wanted:
                        continue
                    try:
                        items = fn(self._token, row.pid, row.name, now)
                        bucket.extend(items)
                        self._log(u"    {0}: {1}".format(label, len(items)))
                    except Exception as e:
                        # module switched off for this project, or no access
                        self._log(u"    {0}: skipped ({1})".format(label, str(e)[:110]))
            pb.update_progress(total, total)

        payload = {
            "generated_at": stamp,
            "projects": [r.name for r in picked],
            "issues": issues, "rfi": rfis, "submittals": subs,
        }
        try:
            files = write_outputs(path, payload, bool(self.csv_cb.IsChecked))
        except Exception as e:
            self._log(u"! Could not write file: {0}".format(e))
            forms.alert(u"Could not write the file:\n{0}".format(e))
            return
        summary = u"{0} issues, {1} RFIs, {2} submittals across {3} project(s).".format(
            len(issues), len(rfis), len(subs), total)
        self._log(u"Done - " + summary)
        for f in files:
            self._log(u"  saved: " + f)
        forms.alert(u"Done - {0}\n\n{1}".format(summary, u"\n".join(files)))


def launch(uiapp):
    window = DeeIssueWindow(_XAML_FILE, uiapp)
    window.ShowDialog()


TOOL_INFO = {
    "id": "issue",
    "title": "DeeISSUE",
    "description": "Export ACC Issues, RFIs and Submittals for the projects you pick (JSON + Excel-friendly CSV) - works with no model open.",
    "icon": u"\U0001F4CB",  # clipboard
    "category": "Files, Links & ACC",
    "launch": launch,
}
