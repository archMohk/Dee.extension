# -*- coding: utf-8 -*-
"""
DeeLinkManagement (Coordination)
Scans every Revit link (RevitLinkType) in the open file and lists, per
link: its Reference Type (Overlay / Attachment), load status, source
(local file / Revit Server / cloud), path type, and how many instances
are placed. From that list the user can:
- switch the Reference Type of the ticked links (Overlay <-> Attachment)
- Reload the ticked links, or Reload All links in one click
- batch ADD new links: from ACC (hub -> project -> tick any number of
  models) or from local .rvt files, with a chosen positioning method and
  Reference Type, each file linked in its own Transaction. The hub /
  project / file pickers and the progress/result output reuse the same
  shared ACC browser (lib/acc_file_browser.py) and link-create service
  (lib/dee_link_create_service.py) as DeeLINK. Those pickers open their
  own dialogs, so Add closes this window, runs, and reopens it (rescanned)
  rather than nesting dialogs inside a modal window.
Nested ("layer 2") links - links living inside another linked model -
are not listed; they follow their parent link (same rule as
DeeLazy's Links Into Views).

--------------------------------------------------------------------
Revit API facts relied on here (members and enum values confirmed by
.NET reflection against the installed RevitAPI.dll, Revit 2026)
--------------------------------------------------------------------
- RevitLinkType.AttachmentType (settable; AttachmentType.Overlay /
  Attachment) - this is the "Reference Type" column of Revit's own
  Manage Links dialog. Changing it needs a Transaction; the same set is
  already used when creating links (lib/dee_link_create_service.py).
- RevitLinkType.GetLinkedFileStatus() -> LinkedFileStatus (Invalid,
  Loaded, Unloaded, Imported, NotFound, CanBeUpgraded, InClosedWorkset,
  LocallyUnloaded); IsNestedLink; IsFromLocalPath(); IsFromRevitServer();
  PathType (Relative / Absolute / Server / Content).
- RevitLinkType.Reload() / Load() / RevertLocalUnloadStatus() return a
  LinkLoadResult whose .LoadResult is a LinkLoadResultType; LinkLoaded
  means success (same check DeeRelink already uses for LoadFrom). These
  must run OUTSIDE a Transaction, so reloading never opens one.
- RevitLinkType.GetExternalFileReference() deliberately throws for
  cloud/ACC-hosted links (documented in DeeRelink), so no file path is
  shown - the Source column says where the link comes from instead.
- ElementId.IntegerValue no longer exists on Revit 2026 (replaced by
  .Value) - _id_value() below handles both.

--------------------------------------------------------------------
NEEDS LIVE-REVIT VERIFICATION (flagged, not silently assumed correct)
--------------------------------------------------------------------
- That setting AttachmentType on an EXISTING, loaded link type is
  accepted by Revit (it is proven on a freshly created one). Guarded:
  after the Transaction the real value is re-read, and a row whose type
  did not change is reported as "Revit did not apply the change".
- Load()/RevertLocalUnloadStatus() for unloaded links, and the Source
  column's classification (Local / Revit Server / Cloud) for ACC links.
"""
import os

import clr
clr.AddReference("WindowsBase")
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")

from pyrevit import forms, script
import dee_branding
import dee_telemetry
dee_telemetry.check_access("DeeLinkManagement")

import acc_auth
import acc_file_browser as afb
import dee_link_create_service as lcs

from Autodesk.Revit.DB import (
    FilteredElementCollector, RevitLinkType, RevitLinkInstance, AttachmentType,
    Transaction, LinkLoadResultType, Element, ModelPathUtils, ImportPlacement,
)

output = script.get_output()

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")
_CACHE_FILE = os.path.join(_THIS_DIR, ".acc_file_cache.json")

REFERENCE_CHOICES = [("Overlay", AttachmentType.Overlay), ("Attachment", AttachmentType.Attachment)]

PLACEMENT_CHOICES = [
    ("Auto - Center to Center",                         ImportPlacement.Centered),
    ("Auto - Internal Origin to Internal Origin",       ImportPlacement.Origin),
    ("Auto - By Shared Coordinates",                    ImportPlacement.Shared),
    ("Auto - Project Base Point to Project Base Point", ImportPlacement.Site),
]


# ==========================================================================
# Small helpers
# ==========================================================================
def _id_value(element_id):
    """ElementId.IntegerValue was removed in Revit 2026 (.Value replaces it)."""
    try:
        return element_id.IntegerValue
    except AttributeError:
        return element_id.Value


def _element_name(element):
    """Element.Name sometimes fails to resolve under IronPython's interop;
    Element.Name.GetValue(element) is the workaround already used in
    lib/dee_3d_export_service.py."""
    if element is None:
        return None
    try:
        n = element.Name
        if n:
            return n
    except Exception:
        pass
    try:
        n = Element.Name.GetValue(element)
        if n:
            return n
    except Exception:
        pass
    return None


def _instance_root_name(instance):
    """A link INSTANCE's name is a chain ("Top.rvt : 21 : location ..."):
    the first segment is the link's own file name."""
    return (_element_name(instance) or "").split(" : ")[0].strip()


def _type_name(link_type, instances):
    name = None
    try:
        name = link_type.Name
    except Exception:
        pass
    if not name:
        name = _element_name(link_type)
    if not name and instances:
        name = _instance_root_name(instances[0])
    return name or "(unnamed link)"


def _status_raw(link_type):
    try:
        return str(link_type.GetLinkedFileStatus())
    except Exception:
        return "Unknown"


def _reference_text(link_type):
    try:
        return str(link_type.AttachmentType)
    except Exception:
        return "?"


def _path_type_text(link_type):
    try:
        return str(link_type.PathType)
    except Exception:
        return ""


def _source_text(link_type):
    local = server = None
    try:
        local = bool(link_type.IsFromLocalPath())
    except Exception:
        pass
    try:
        server = bool(link_type.IsFromRevitServer())
    except Exception:
        pass
    if local:
        return "Local file"
    if server:
        return "Revit Server"
    if local is False and server is False:
        return "Cloud (ACC)"
    return "Unknown"


# ==========================================================================
# Scanning
# ==========================================================================
class LinkRow(object):
    def __init__(self, link_type, instances):
        self.link_type = link_type
        self.instance_count = len(instances)
        self.selected = False
        self.last_result = ""
        self.name = _type_name(link_type, instances)
        self.refresh()

    def refresh(self):
        self.reference_type = _reference_text(self.link_type)
        self.status_raw = _status_raw(self.link_type)
        self.status = self.status_raw
        self.source = _source_text(self.link_type)
        self.path_type = _path_type_text(self.link_type)


def scan_links(doc):
    instances_by_type = {}
    for inst in FilteredElementCollector(doc).OfClass(RevitLinkInstance):
        try:
            instances_by_type.setdefault(_id_value(inst.GetTypeId()), []).append(inst)
        except Exception:
            continue
    rows = []
    nested = 0
    for link_type in FilteredElementCollector(doc).OfClass(RevitLinkType):
        try:
            if link_type.IsNestedLink:
                nested += 1
                continue
        except Exception:
            pass
        rows.append(LinkRow(link_type, instances_by_type.get(_id_value(link_type.Id), [])))
    rows.sort(key=lambda r: r.name.lower())
    return rows, nested


# ==========================================================================
# Actions
# ==========================================================================
def set_reference_type(doc, rows, label, target):
    """One Transaction for the batch; each row is tried on its own so one
    link Revit refuses never blocks the others. The real value is
    re-read afterwards, so a change Revit silently rejected is reported
    as such instead of claimed as done."""
    t = Transaction(doc, "DeeLinkManagement - Set Reference Type")
    t.Start()
    try:
        for row in rows:
            try:
                if row.link_type.AttachmentType == target:
                    row.last_result = "Already {0}".format(label)
                else:
                    row.link_type.AttachmentType = target
                    row.last_result = "Changed to {0}".format(label)
            except Exception as e:
                row.last_result = "Failed: {0}".format(e)
    finally:
        t.Commit()
    for row in rows:
        row.refresh()
        if row.last_result.startswith("Changed") and row.reference_type != label:
            row.last_result = "Revit did not apply the change"


def reload_link(row):
    """Reload (or load, if currently unloaded) one link type. Must run
    OUTSIDE a Transaction. Returns (ok, text)."""
    link_type = row.link_type
    try:
        if row.status_raw == "Unloaded":
            result = link_type.Load()
        elif row.status_raw == "LocallyUnloaded":
            link_type.RevertLocalUnloadStatus()
            result = link_type.Load()
        else:
            result = link_type.Reload()
    except Exception as e:
        return False, "Failed: {0}".format(e)
    try:
        outcome = result.LoadResult
        if outcome == LinkLoadResultType.LinkLoaded:
            return True, "Reloaded"
        return False, "Not loaded: {0}".format(outcome)
    except Exception:
        return True, "Reloaded"


# ==========================================================================
# Batch add links
# ==========================================================================
def _link_one(doc, name, model_path, placement, attachment):
    """One link per Transaction, so a file Revit refuses (already the host,
    unreachable, ...) rolls back alone and never blocks the rest of the
    batch. The create-type / set-AttachmentType / create-instance sequence
    is lib/dee_link_create_service.py, shared with DeeLINK, DeeSuperLINK
    and DeeMAPLink. Returns (ok, detail)."""
    t = Transaction(doc, "DeeLinkManagement - Link: {0}".format(name))
    t.Start()
    try:
        ok, detail, _instance = lcs.create_link(doc, model_path, placement, attachment)
        if not ok:
            t.RollBack()
            return False, detail
        t.Commit()
        return True, detail
    except Exception as e:
        try:
            if t.HasStarted() and not t.HasEnded():
                t.RollBack()
        except Exception:
            pass
        return False, str(e)


def _acc_model_paths():
    """Hub -> project -> file picker, using the same shared ACC browser
    every other Dee link tool uses. Returns [(display_name, ModelPath or
    error string, is_error)] for the files the user ticked, or None if
    they cancelled or nothing could be listed."""
    try:
        token = acc_auth.get_access_token()
    except Exception as e:
        forms.alert("Authentication failed:\n{0}".format(e))
        return None
    hub = afb.pick_hub(token)
    if not hub:
        return None
    hub_id, region, _hub_name = hub
    project = afb.pick_project(hub_id, token)
    if not project:
        return None
    project_id, _project_name = project
    all_items = afb.list_project_files(hub_id, project_id, token, _CACHE_FILE)
    if not all_items:
        return None
    picked = afb.pick_files_to_open(
        all_items, title="Add Links - select files to link  ({0} available)".format(len(all_items)),
        button_name="Link Selected")
    if not picked:
        return None
    entries = []
    for name in picked:
        try:
            proj_guid, model_guid, _src = afb.get_cloud_path_guids(project_id, all_items[name], token)
            entries.append((name, ModelPathUtils.ConvertCloudGUIDsToCloudPath(region, proj_guid, model_guid), False))
        except Exception as e:
            entries.append((name, str(e), True))
    return entries


def _file_model_paths():
    paths = forms.pick_file(file_ext="rvt", multi_file=True, title="Select Revit files to link")
    if not paths:
        return None
    entries = []
    for path in paths:
        name = os.path.basename(path)
        try:
            entries.append((name, ModelPathUtils.ConvertUserVisiblePathToModelPath(path), False))
        except Exception as e:
            entries.append((name, str(e), True))
    return entries


def add_links(doc, source, placement_label, placement, attachment_label, attachment):
    """Picks files (source "acc" or "files") and links each one into the
    open project. Runs with no Dee window open (see main()), so the
    pickers' own dialogs are never nested inside another modal window.
    Prints a result row per file."""
    entries = _acc_model_paths() if source == "acc" else _file_model_paths()
    if not entries:
        return
    results = []
    total = len(entries)
    with afb._SafeProgress(title="Linking 0 / {0}...".format(total), cancellable=True) as pb:
        for i, (name, model_path, is_error) in enumerate(entries):
            if pb.cancelled:
                results.append((None, name, "Cancelled - {0} of {1} done".format(i, total)))
                break
            pb.title = "Linking {0} / {1}  -  {2}".format(i + 1, total, name)
            pb.update_progress(i + 1, total)
            if is_error:
                results.append((False, name, "FAILED: {0}".format(model_path)))
                continue
            ok, detail = _link_one(doc, name, model_path, placement, attachment)
            if ok:
                results.append((True, name, "Linked ({0}, {1})".format(placement_label, attachment_label)))
            else:
                results.append((False, name, "FAILED: {0}".format(detail)))

    ok_count = sum(1 for r in results if r[0])
    html = ['<h2 style="font-family:sans-serif;color:#ddd;">DeeLinkManagement - Add Links</h2>',
            '<p style="color:#ddd;">{0} of {1} file(s) linked.</p>'.format(ok_count, total)]
    for ok, name, detail in results:
        bg = "#1b5e20" if ok else ("#37474f" if ok is None else "#b71c1c")
        icon = "&#10003;" if ok else ("&#9888;" if ok is None else "&#10007;")
        html.append(
            '<div style="padding:6px 12px;margin:3px 0;background:{0};color:#fff;border-radius:4px;'
            'font-family:monospace;font-size:13px;"><b>{1}</b>&nbsp; {2} &mdash; {3}</div>'.format(
                bg, icon, name, detail))
    output.print_html("".join(html))


# ==========================================================================
# Window
# ==========================================================================
class DeeLinkManagementWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, doc):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.doc = doc
        self._all_rows = []
        self._nested = 0
        self.pending_request = None

        self.reference_cb.ItemsSource = [label for label, _ in REFERENCE_CHOICES]
        self.reference_cb.SelectedIndex = 0
        self.placement_cb.ItemsSource = [label for label, _ in PLACEMENT_CHOICES]
        self.placement_cb.SelectedIndex = 0
        self.new_reference_cb.ItemsSource = [label for label, _ in REFERENCE_CHOICES]
        self.new_reference_cb.SelectedIndex = 0
        self._scan()

    def _log(self, message):
        self.status_tb.Text = message

    def _scan(self):
        self._all_rows, self._nested = scan_links(self.doc)
        self._refresh_grid()

    def _visible_rows(self):
        text = (self.search_tb.Text or "").strip().lower()
        if not text:
            return self._all_rows
        return [r for r in self._all_rows if text in r.name.lower() or text in r.status.lower()]

    def _refresh_grid(self, message=None):
        visible = self._visible_rows()
        self.links_grid.ItemsSource = None
        self.links_grid.ItemsSource = visible
        if message:
            self._log(message)
        else:
            self._log("{0} link(s) found ({1} nested link(s) not listed - they follow their parent) - {2} shown.".format(
                len(self._all_rows), self._nested, len(visible)))

    def search_changed(self, sender, args):
        if not getattr(self, "_all_rows", None):
            return
        self._refresh_grid()

    def rescan_click(self, sender, args):
        self._scan()

    def select_all_click(self, sender, args):
        for r in self._visible_rows():
            r.selected = True
        self.links_grid.Items.Refresh()

    def select_none_click(self, sender, args):
        for r in self._visible_rows():
            r.selected = False
        self.links_grid.Items.Refresh()

    def _ticked(self):
        return [r for r in self._all_rows if r.selected]

    def apply_reference_click(self, sender, args):
        rows = self._ticked()
        if not rows:
            forms.alert("Tick at least one link first.")
            return
        idx = self.reference_cb.SelectedIndex
        if idx < 0 or idx >= len(REFERENCE_CHOICES):
            forms.alert("Pick a Reference Type first.")
            return
        label, target = REFERENCE_CHOICES[idx]
        if not forms.alert("Set {0} link(s) to {1}?".format(len(rows), label), yes=True, no=True):
            return
        set_reference_type(self.doc, rows, label, target)
        changed = sum(1 for r in rows if r.last_result.startswith("Changed"))
        failed = sum(1 for r in rows if r.last_result.startswith(("Failed", "Revit did not")))
        self._refresh_grid("{0} of {1} link(s) set to {2} ({3} failed).".format(changed, len(rows), label, failed))

    def _reload(self, rows, what):
        if not rows:
            forms.alert("No links to reload.")
            return
        ok_count = 0
        for row in rows:
            ok, text = reload_link(row)
            row.last_result = text
            row.refresh()
            if ok:
                ok_count += 1
        self._refresh_grid("{0}: {1} of {2} link(s) reloaded successfully.".format(what, ok_count, len(rows)))

    def reload_selected_click(self, sender, args):
        rows = self._ticked()
        if not rows:
            forms.alert("Tick at least one link first.")
            return
        self._reload(rows, "Reload Selected")

    def reload_all_click(self, sender, args):
        if not self._all_rows:
            forms.alert("No links to reload.")
            return
        if not forms.alert(
                "Reload all {0} link(s)? Revit shows its own progress and this can take a while.".format(
                    len(self._all_rows)), yes=True, no=True):
            return
        self._reload(list(self._all_rows), "Reload All")

    def _request_add(self, source):
        """The pickers used to add links open their own dialogs, so they
        must not run from inside this still-open modal window (the same
        rule DeeLazy's launcher follows). Record what was asked for and
        close; main() runs the add and then reopens this window."""
        p_idx = self.placement_cb.SelectedIndex
        r_idx = self.new_reference_cb.SelectedIndex
        if p_idx < 0 or p_idx >= len(PLACEMENT_CHOICES) or r_idx < 0 or r_idx >= len(REFERENCE_CHOICES):
            forms.alert("Pick the positioning method and the Reference Type for the new links first.")
            return
        placement_label, placement = PLACEMENT_CHOICES[p_idx]
        attachment_label, attachment = REFERENCE_CHOICES[r_idx]
        self.pending_request = (source, placement_label, placement, attachment_label, attachment)
        self.Close()

    def add_acc_click(self, sender, args):
        self._request_add("acc")

    def add_files_click(self, sender, args):
        self._request_add("files")

    def close_click(self, sender, args):
        self.Close()


def main():
    uiapp = __revit__
    uidoc = uiapp.ActiveUIDocument
    if uidoc is None:
        forms.alert("Open a Revit project first.")
        return
    doc = uidoc.Document
    if doc.IsFamilyDocument:
        forms.alert("Please open a project document first.")
        return
    while True:
        window = DeeLinkManagementWindow(_XAML_FILE, doc)
        window.ShowDialog()
        request = window.pending_request
        if request is None:
            break
        add_links(doc, *request)


main()
