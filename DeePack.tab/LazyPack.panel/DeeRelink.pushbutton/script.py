# -*- coding: utf-8 -*-
"""
DeeRelink (LazyPack)
Scans every Revit Link (RevitLinkType) in each currently-open, non-
linked document and lets you batch-update their file paths - one tab
per open document (tab header = that document's own title), since more
than one project file can be open at once in the same Revit session.
Each tab has its own grid (File Name / Current Path / New Path /
Status) and its own Rescan / Browse for New Folder (Highlighted) /
Apply buttons - Apply only ever touches THAT tab's document.

--------------------------------------------------------------------
Revit API facts relied on here (verified against revitapidocs.com and
the actual community-documented workaround before writing, not guessed)
--------------------------------------------------------------------
- RevitLinkType.GetExternalFileReference() -> ExternalFileReference;
  .GetAbsolutePath() -> ModelPath (the resolved absolute path, per its
  own docs) works ONLY for LOCAL-file-hosted links.
- CLOUD-hosted (ACC/BIM 360) links deliberately throw "This Element
  does not represent an external file reference" from
  GetExternalFileReference() - this is documented, by-design Autodesk
  behavior (confirmed via the Autodesk API forum before assuming it
  was a bug in this code), not something fixable by calling it
  differently. The real API for those is a completely different path:
  RevitLinkType.GetExternalResourceReferences() -> a map of
  ExternalResourceReference objects; each one's
  GetReferenceInformation() exposes a dictionary of Forge/ACC info
  (documented to include the ACC Project GUID and Model GUID, exact
  key names unconfirmed - see NEEDS LIVE VERIFICATION below, handled
  defensively by trying several plausible key spellings).
- ModelPathUtils.ConvertModelPathToUserVisiblePath(ModelPath) -> str,
  and the reverse ConvertUserVisiblePathToModelPath(str) -> ModelPath,
  already used elsewhere in this extension for local files.
- ModelPathUtils.ConvertCloudGUIDsToCloudPath(region, projectGuid,
  modelGuid) -> ModelPath - already proven in this extension's
  acc_file_browser.open_cloud_file(), reused here unchanged (via
  acc_file_browser.get_cloud_path_guids()) to build the ModelPath for
  a cloud relink target picked through the ACC browser.
- RevitLinkType.LoadFrom(ModelPath, WorksetConfiguration) -> the exact
  API behind Revit's own "Manage Links > Reload From..." command,
  accepting EITHER a local/server ModelPath or a cloud one built via
  ConvertCloudGUIDsToCloudPath - same call either way. Passing None for
  the WorksetConfiguration is explicitly documented as valid ("loads
  the previously-used worksets"). Returns a RevitLinkLoadResult whose
  .LoadResult property (a LinkLoadResultType) equals
  LinkLoadResultType.LinkLoaded on success.
- CRITICAL, DIFFERENT FROM EVERY OTHER TOOL IN THIS EXTENSION:
  LoadFrom must be called OUTSIDE any open Transaction - "all
  transaction phases... must be finished prior to calling this
  method" (documented explicitly). So unlike every other Revit-
  mutating call in Dee.extension, _apply_relinks() below deliberately
  does NOT wrap anything in a Transaction.
- Document.IsLinked (bool) / Document.Title (str) - used to build one
  tab per genuinely-open project document, excluding Document objects
  that only exist in Application.Documents because they're loaded AS
  a link inside another open document.
- Reuses this repo's existing, already-proven ACC infrastructure
  (acc_auth.get_access_token, acc_file_browser.pick_hub/pick_project/
  get_cloud_path_guids - the same modules DeeOpener/DeeNWCs/DeeW.Cloud
  already use) for the "Pick from ACC Cloud..." button, rather than
  re-deriving hub/project/model browsing. The APS OAuth login only
  triggers when that button is actually clicked, not during the
  automatic scan-on-open, so opening this tool never forces a surprise
  sign-in prompt just to look at the link list.
- The ACC Hub/Project is picked ONCE and remembered (via deew_settings,
  the same gitignored per-tool JSON persistence DeeW.Cloud's tools
  already use) rather than re-prompted on every "Pick from ACC
  Cloud..." click - per explicit user request ("no need to open the
  full ACC every time, just open the project I work on"). A "Change
  ACC Project..." button lets you switch it deliberately. This is
  shared window-wide state (one Autodesk/ACC project context, not
  per-document-tab), since it's the same real-world ACC project
  regardless of which open Revit document's links you're fixing.
- Browsing WITHIN that remembered project uses a folder-by-folder
  drill-down (acc_file_browser.browse_and_pick_cloud_file - a new
  sibling to that module's existing pick_folder(), reusing the exact
  same acc_api.get_top_folders/list_folder_contents primitives) with
  an explicit "Scan This Folder for Revit Files" action at whatever
  level you navigate to, INSTEAD OF the old list_project_files() call
  (a full recursive BFS scan of the entire project's folder tree) -
  per explicit user request ("do not scan all the Revit files, just
  let me navigate between folders until I reach the requested folder,
  then let me click scan"). Each folder's scan result is cached per
  the same request ("save the scan, not every time scan all the
  files") - revisiting the same folder offers "Use Cached" instead of
  hitting the API again.

--------------------------------------------------------------------
NEEDS LIVE-REVIT VERIFICATION (flagged, not silently assumed correct)
--------------------------------------------------------------------
- The exact key names GetReferenceInformation() uses for the ACC
  Project GUID / Model GUID / display name are not fully documented -
  several plausible spellings are tried defensively (_dict_lookup),
  falling back to a generic "(Cloud Model)" label if none match,
  rather than crashing or showing a raw exception to the user.
- Whether a successful LoadFrom keeps the same RevitLinkType ElementId
  or creates a new one - unclear from documentation alone. Handled
  defensively: after Apply, the whole tab is rescanned from Revit's
  current state rather than trying to patch the existing row objects
  in place, so the grid always reflects ground truth regardless of
  which way this actually behaves. The apply run's own result (per-row
  success/failure and detail) is still shown via the report output
  before the rescan replaces the rows.
- GetAbsolutePath() behavior on a currently-UNLOADED local link (a
  common real case - exactly the "broken/moved link" scenario this
  tool targets) is expected to still report the last-known path (that
  metadata should persist independent of load state) but hasn't been
  exercised live.

Since this window's tab count varies with how many documents happen
to be open, the tabs/grids/buttons are built directly in Python via
WPF constructors rather than static XAML with x:Name bindings (the
pattern used by every other tool in this extension) - ui.xaml here is
just an empty shell (a TabControl placeholder + a Close button).
"""
import os

import clr
clr.AddReference("WindowsBase")
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
clr.AddReference("System.Windows.Forms")

from pyrevit import forms, script
from Autodesk.Revit.DB import (
    FilteredElementCollector, RevitLinkType, ModelPathUtils, LinkLoadResultType,
    BuiltInParameter,
)
from System.Windows import Thickness, FontWeights, FontStyles
from System.Windows.Controls import (
    TabItem, DockPanel, StackPanel, Button, TextBlock, DataGrid, DataGridTextColumn,
    Orientation, Dock, DataGridSelectionMode, DataGridSelectionUnit,
    DataGridLength, DataGridLengthUnitType,
)
from System.Windows.Data import Binding, BindingMode, UpdateSourceTrigger
from System.Windows.Forms import FolderBrowserDialog, DialogResult

import acc_auth
import acc_file_browser as afb
import deew_settings

output = script.get_output()

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")
_ACC_CACHE_FILE = os.path.join(_THIS_DIR, ".acc_file_cache.json")

_ACC_SETTINGS_TOOL = "DeeRelink"
_ACC_SETTINGS_DEFAULTS = {
    "hub_id": None, "hub_name": None, "region": None,
    "project_id": None, "project_name": None,
}


def _pick_new_acc_project(acc_context):
    """Prompts Hub then Project (the two ACC pickers, only ever shown
    here - never re-shown just to browse folders/files within an
    already-remembered project) and saves the choice to disk so future
    DeeRelink sessions start with it already set. Mutates `acc_context`
    in place (a plain dict shared by the window and every tab's
    controller) and returns True on success, False if cancelled or
    failed."""
    try:
        token = acc_auth.get_access_token()
        hub = afb.pick_hub(token)
        if not hub:
            return False
        hub_id, region, hub_name = hub
        project = afb.pick_project(hub_id, token)
        if not project:
            return False
        project_id, project_name = project
    except Exception as e:
        forms.alert("Could not load ACC hubs/projects: {0}".format(e))
        return False
    acc_context.update({
        "hub_id": hub_id, "hub_name": hub_name, "region": region,
        "project_id": project_id, "project_name": project_name,
    })
    deew_settings.save(_ACC_SETTINGS_TOOL, acc_context)
    return True


def _read_name(element):
    """Same defensive Name-then-BuiltInParameter-fallback pattern used
    throughout this extension (e.g. DeeAligner) - a plain .Name getter
    can come back empty for some element types/states, so the fallback
    parameters matter, not just a nicety."""
    if element is None:
        return None
    try:
        n = element.Name
        if n:
            return n
    except Exception:
        pass
    for bip in (BuiltInParameter.SYMBOL_NAME_PARAM, BuiltInParameter.ALL_MODEL_TYPE_NAME,
                BuiltInParameter.DATUM_TEXT):
        try:
            p = element.get_Parameter(bip)
            if p is not None:
                val = p.AsString()
                if val:
                    return val
        except Exception:
            continue
    return None


def _dict_lookup(net_dict, keys):
    """Best-effort read from a .NET IDictionary<string,string> (as
    returned by ExternalResourceReference.GetReferenceInformation())
    trying several plausible key spellings, since the exact keys aren't
    fully documented - see module docstring."""
    if net_dict is None:
        return None
    for k in keys:
        try:
            if net_dict.ContainsKey(k):
                return net_dict[k]
        except Exception:
            continue
    return None


class LinkRow(object):
    """One row per RevitLinkType in a document. current_path/file_name
    are read once at scan time (never mutated afterward - a fresh scan
    replaces the whole row list instead, see module docstring re: why).
    new_path is what's shown/typed in the grid; cloud_target (set only
    via "Pick from ACC Cloud...") takes priority over new_path's text
    at Apply time if both are somehow set."""

    def __init__(self, link_type):
        self.link_type = link_type
        self.id = link_type.Id
        self.is_cloud = False
        self.current_path = ""
        self.new_path = ""
        self.status = "Not Applied"
        self.cloud_target = None  # (region, project_id, item_id, token, label) once picked from ACC
        self.file_name = ""

        try:
            ext_ref = link_type.GetExternalFileReference()
            model_path = ext_ref.GetAbsolutePath()
            self.current_path = ModelPathUtils.ConvertModelPathToUserVisiblePath(model_path)
            if self.current_path:
                self.file_name = os.path.basename(self.current_path)
        except Exception:
            # Cloud-hosted links throw here BY DESIGN (verified, not a
            # bug) - GetExternalFileReference() only works for local/
            # server-hosted links. Fall back to the cloud-specific path.
            self._read_cloud_info()

        if not self.file_name:
            self.file_name = _read_name(link_type) or "(unnamed link)"

    def _read_cloud_info(self):
        self.is_cloud = True
        try:
            ref_map = self.link_type.GetExternalResourceReferences()
            for key in ref_map.Keys:
                ref = ref_map[key]
                try:
                    info = ref.GetReferenceInformation()
                except Exception:
                    continue
                project_guid = _dict_lookup(info, ("ProjectGUID", "ProjectId", "projectGuid", "project_guid"))
                model_guid = _dict_lookup(info, ("ModelGUID", "ModelId", "modelGuid", "model_guid"))
                model_name = _dict_lookup(info, ("ModelName", "DisplayName", "FileName", "Name"))
                if model_name:
                    self.file_name = model_name
                if project_guid and model_guid:
                    self.current_path = "[ACC Cloud Model] Project {0} / Model {1}".format(
                        project_guid, model_guid)
                    return
        except Exception:
            pass
        if not self.current_path:
            self.current_path = "(Cloud Model - path not resolvable here; see Revit's Manage Links dialog)"


def _scan_revit_links(doc):
    rows = []
    for link_type in FilteredElementCollector(doc).OfClass(RevitLinkType):
        try:
            rows.append(LinkRow(link_type))
        except Exception:
            continue
    rows.sort(key=lambda r: r.file_name.lower())
    return rows


def _apply_relinks(pending_rows):
    """Calls RevitLinkType.LoadFrom for every row with a cloud_target or
    a non-empty New Path - deliberately NOT wrapped in a Transaction
    (see module docstring: LoadFrom requires no open transaction).
    Returns a list of (success, file_name, detail) for reporting;
    never raises."""
    results = []
    for row in pending_rows:
        new_text = (row.new_path or "").strip()
        if row.cloud_target is None and not new_text:
            continue
        try:
            if row.cloud_target is not None:
                region, project_id, item_id, token, label = row.cloud_target
                project_guid, model_guid, _src = afb.get_cloud_path_guids(project_id, item_id, token)
                new_model_path = ModelPathUtils.ConvertCloudGUIDsToCloudPath(region, project_guid, model_guid)
                target_desc = label
            else:
                new_model_path = ModelPathUtils.ConvertUserVisiblePathToModelPath(new_text)
                target_desc = new_text
            result = row.link_type.LoadFrom(new_model_path, None)
            if result is not None and result.LoadResult == LinkLoadResultType.LinkLoaded:
                results.append((True, row.file_name, "Relinked to '{0}'".format(target_desc)))
            else:
                load_result_text = str(result.LoadResult) if result is not None else "Unknown"
                results.append((False, row.file_name, "LoadFrom result: {0}".format(load_result_text)))
        except Exception as e:
            results.append((False, row.file_name, str(e)))
    return results


def _report_results(doc_title, results):
    ok_count = sum(1 for ok, _n, _d in results if ok)
    fail_count = len(results) - ok_count
    html = ['<h2 style="font-family:sans-serif;color:#ddd;">DeeRelink - {0} Results</h2>'.format(doc_title),
            '<p style="color:#ddd;">{0} succeeded, {1} failed.</p>'.format(ok_count, fail_count)]
    for ok, name, detail in results:
        bg = "#2e7d32" if ok else "#c62828"
        icon = "&#10003;" if ok else "&#10007;"
        html.append(
            '<div style="padding:4px 10px;margin:2px 0;background:{0};color:#fff;'
            'border-radius:4px;font-family:monospace;font-size:12px;">'
            '{1}&nbsp; <b>{2}</b> &mdash; {3}</div>'.format(bg, icon, name, detail))
    output.print_html("".join(html))


class DocTabController(object):
    """Owns one tab's worth of state/behavior for ONE open Document.
    Click handlers are bound directly to these methods via += when the
    tab's WPF controls are built in code (no XAML Click= binding is
    possible here since the number of tabs varies at runtime)."""

    def __init__(self, document, acc_context, on_acc_context_changed=None):
        self.document = document
        self.rows = []
        self.grid = None
        self.status_tb = None
        self.acc_context = acc_context  # shared dict - same ACC project across all tabs
        self.on_acc_context_changed = on_acc_context_changed

    def scan(self):
        self.rows = _scan_revit_links(self.document)
        self._refresh_grid()

    def _refresh_grid(self):
        self.grid.ItemsSource = None
        self.grid.ItemsSource = self.rows
        self.status_tb.Text = "{0} Revit link(s) found.".format(len(self.rows))

    def rescan_click(self, sender, args):
        self.scan()

    def browse_folder_click(self, sender, args):
        """Local-file relink target: applies to ANY highlighted row
        (regardless of whether it's currently cloud-hosted or local -
        relinking a cloud-sourced link to a new local file is just as
        valid a scenario as the reverse)."""
        highlighted = list(self.grid.SelectedItems)
        if not highlighted:
            forms.alert("Click a row (Shift-click or Ctrl-click for more) to highlight rows first.")
            return
        dlg = FolderBrowserDialog()
        dlg.Description = "Pick the new folder for the highlighted link(s)"
        if dlg.ShowDialog() != DialogResult.OK:
            return
        folder = dlg.SelectedPath
        for row in highlighted:
            row.new_path = os.path.join(folder, row.file_name)
            row.cloud_target = None
        self.grid.Items.Refresh()

    def pick_cloud_click(self, sender, args):
        """ACC cloud relink target: reuses whatever Hub/Project is
        already remembered (self.acc_context) - only prompts Hub/
        Project pickers the very first time ever, or after "Change ACC
        Project..." - then browses INTO that project folder-by-folder
        (acc_file_browser.browse_and_pick_cloud_file), scanning only
        the one folder you navigate to and explicitly ask to scan, not
        the whole project. Applies the ONE picked model to every
        highlighted row. The APS sign-in only happens here, on explicit
        user action - never during the automatic scan-on-open."""
        highlighted = list(self.grid.SelectedItems)
        if not highlighted:
            forms.alert("Click a row (Shift-click or Ctrl-click for more) to highlight rows first.")
            return
        if not self.acc_context.get("project_id"):
            if not _pick_new_acc_project(self.acc_context):
                return
            if self.on_acc_context_changed:
                self.on_acc_context_changed()

        ctx = self.acc_context
        try:
            token = acc_auth.get_access_token()
            picked = afb.browse_and_pick_cloud_file(ctx["hub_id"], ctx["project_id"], token, _ACC_CACHE_FILE)
            if not picked:
                return
            item_id, picked_name = picked
        except Exception as e:
            forms.alert("Could not browse ACC folders: {0}".format(e))
            return

        label = "[ACC] {0} / {1} / {2}".format(ctx["hub_name"], ctx["project_name"], picked_name)
        for row in highlighted:
            row.cloud_target = (ctx["region"], ctx["project_id"], item_id, token, label)
            row.new_path = label
        self.grid.Items.Refresh()

    def apply_click(self, sender, args):
        pending = [r for r in self.rows if (r.new_path or "").strip()]
        if not pending:
            forms.alert("Nothing to apply - type or Browse a New Path for at least one row first.")
            return
        results = _apply_relinks(pending)
        self.scan()
        applied = sum(1 for ok, _n, _d in results if ok)
        failed = len(results) - applied
        self.status_tb.Text = "{0} Revit link(s) found. Last apply: {1} relinked, {2} failed.".format(
            len(self.rows), applied, failed)
        _report_results(self.document.Title, results)


class DeeRelinkWindow(forms.WPFWindow):
    def __init__(self, xaml_file, uiapp):
        forms.WPFWindow.__init__(self, xaml_file)
        self.uiapp = uiapp
        self._controllers = []
        self._acc_context = deew_settings.load(_ACC_SETTINGS_TOOL, _ACC_SETTINGS_DEFAULTS)
        self._refresh_acc_status()
        self._build_tabs()

    def _refresh_acc_status(self):
        ctx = self._acc_context
        if ctx.get("project_id"):
            self.acc_status_tb.Text = "ACC Project: {0} / {1}".format(
                ctx.get("hub_name", "?"), ctx.get("project_name", "?"))
        else:
            self.acc_status_tb.Text = "ACC Project: (none set yet - picked on first cloud use)"

    def change_project_click(self, sender, args):
        if _pick_new_acc_project(self._acc_context):
            self._refresh_acc_status()

    def _open_project_documents(self):
        docs = []
        try:
            for d in self.uiapp.Application.Documents:
                try:
                    if d.IsLinked:
                        continue
                except Exception:
                    pass
                docs.append(d)
        except Exception:
            pass
        return docs

    def _build_tabs(self):
        documents = self._open_project_documents()
        if not documents:
            forms.alert("No open Revit documents found.")
            return
        for doc in documents:
            controller = DocTabController(doc, self._acc_context, self._refresh_acc_status)
            tab_item = self._build_tab_item(doc, controller)
            self.main_tabs.Items.Add(tab_item)
            self._controllers.append(controller)
            controller.scan()

    def _build_tab_item(self, doc, controller):
        tab = TabItem()
        try:
            tab.Header = doc.Title or "(untitled)"
        except Exception:
            tab.Header = "(untitled)"

        root = DockPanel()
        root.Margin = Thickness(8)

        toolbar = StackPanel()
        toolbar.Orientation = Orientation.Horizontal
        toolbar.Margin = Thickness(0, 0, 0, 8)
        DockPanel.SetDock(toolbar, Dock.Top)

        rescan_b = Button()
        rescan_b.Content = "Rescan"
        rescan_b.Width = 90
        rescan_b.Height = 24
        rescan_b.Click += controller.rescan_click
        toolbar.Children.Add(rescan_b)

        browse_b = Button()
        browse_b.Content = "Browse for New Folder (Highlighted)..."
        browse_b.Width = 240
        browse_b.Height = 24
        browse_b.Margin = Thickness(8, 0, 0, 0)
        browse_b.Click += controller.browse_folder_click
        toolbar.Children.Add(browse_b)

        pick_cloud_b = Button()
        pick_cloud_b.Content = "Pick from ACC Cloud (Highlighted)..."
        pick_cloud_b.Width = 230
        pick_cloud_b.Height = 24
        pick_cloud_b.Margin = Thickness(8, 0, 0, 0)
        pick_cloud_b.Click += controller.pick_cloud_click
        toolbar.Children.Add(pick_cloud_b)

        apply_b = Button()
        apply_b.Content = "Apply"
        apply_b.Width = 90
        apply_b.Height = 26
        apply_b.Margin = Thickness(14, 0, 0, 0)
        apply_b.FontWeight = FontWeights.Bold
        apply_b.Click += controller.apply_click
        toolbar.Children.Add(apply_b)

        root.Children.Add(toolbar)

        status_tb = TextBlock()
        status_tb.Text = "Scanning..."
        status_tb.FontStyle = FontStyles.Italic
        status_tb.Margin = Thickness(0, 0, 0, 8)
        DockPanel.SetDock(status_tb, Dock.Top)
        root.Children.Add(status_tb)

        grid = DataGrid()
        grid.AutoGenerateColumns = False
        grid.CanUserAddRows = False
        grid.SelectionMode = DataGridSelectionMode.Extended
        grid.SelectionUnit = DataGridSelectionUnit.FullRow

        def _make_column(header, prop, width, editable=False, star=False):
            col = DataGridTextColumn()
            col.Header = header
            binding = Binding(prop)
            if editable:
                binding.Mode = BindingMode.TwoWay
                binding.UpdateSourceTrigger = UpdateSourceTrigger.PropertyChanged
            col.Binding = binding
            col.Width = (DataGridLength(1, DataGridLengthUnitType.Star)
                         if star else DataGridLength(width))
            col.IsReadOnly = not editable
            return col

        grid.Columns.Add(_make_column("File Name", "file_name", 200))
        grid.Columns.Add(_make_column("Current Path", "current_path", 260))
        grid.Columns.Add(_make_column("New Path", "new_path", 0, editable=True, star=True))
        grid.Columns.Add(_make_column("Status", "status", 150))

        root.Children.Add(grid)
        tab.Content = root

        controller.grid = grid
        controller.status_tb = status_tb
        return tab

    def close_click(self, sender, args):
        self.Close()


def main():
    uiapp = __revit__
    window = DeeRelinkWindow(_XAML_FILE, uiapp)
    window.ShowDialog()


main()
