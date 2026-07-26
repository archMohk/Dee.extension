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
Revit API facts relied on here (verified against revitapidocs.com
before writing, not guessed)
--------------------------------------------------------------------
- RevitLinkType.GetExternalFileReference() -> ExternalFileReference;
  .GetAbsolutePath() -> ModelPath (the resolved absolute path, per its
  own docs - "ExternalFileReferences taken from a closed document
  report absolute path as of the last save").
- ModelPathUtils.ConvertModelPathToUserVisiblePath(ModelPath) -> str,
  and the reverse ConvertUserVisiblePathToModelPath(str) -> ModelPath,
  already used elsewhere in this extension (deew_document_manager.py
  etc.) for local files.
- RevitLinkType.LoadFrom(ModelPath, WorksetConfiguration) -> the exact
  API behind Revit's own "Manage Links > Reload From..." command.
  Passing None for the WorksetConfiguration is explicitly documented as
  valid ("loads the previously-used worksets"). Returns a
  RevitLinkLoadResult whose .LoadResult property (a LinkLoadResultType)
  equals LinkLoadResultType.LinkLoaded on success.
- CRITICAL, DIFFERENT FROM EVERY OTHER TOOL IN THIS EXTENSION:
  LoadFrom must be called OUTSIDE any open Transaction - "all
  transaction phases... must be finished prior to calling this
  method" (documented explicitly). So unlike every other Revit-
  mutating call in Dee.extension, _apply_relinks() below deliberately
  does NOT wrap anything in a Transaction.
- ModelPathUtils has no "IsCloudPath" helper (checked the documented
  method list before assuming one existed - only
  ConvertCloudGUIDsToCloudPath / ConvertModelPathToUserVisiblePath /
  ConvertUserVisiblePathToModelPath / IsValidUserVisibleFullServerPath
  are documented). ModelPath's cloud-hosted derived type is named
  "CloudPath" (per its own docs: "To create a ModelPath, use the
  derived classes FilePath, ServerPath and CloudPath") - detected here
  via .NET reflection (GetType().Name == "CloudPath") rather than
  importing that derived class directly, so this degrades gracefully
  if the exact type name ever differs by Revit version.
- Document.IsLinked (bool) / Document.Title (str) - used to build one
  tab per genuinely-open project document, excluding Document objects
  that only exist in Application.Documents because they're loaded AS
  a link inside another open document.

--------------------------------------------------------------------
NEEDS LIVE-REVIT VERIFICATION (flagged, not silently assumed correct)
--------------------------------------------------------------------
- Whether a successful LoadFrom keeps the same RevitLinkType ElementId
  or creates a new one - unclear from documentation alone. Handled
  defensively: after Apply, the whole tab is rescanned from Revit's
  current state rather than trying to patch the existing row objects
  in place, so the grid always reflects ground truth regardless of
  which way this actually behaves. The apply run's own result (per-row
  success/failure and detail) is still shown via the report output
  before the rescan replaces the rows.
- GetAbsolutePath() behavior on a currently-UNLOADED link (a very
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
)
from System.Windows import Thickness, FontWeights, FontStyles
from System.Windows.Controls import (
    TabItem, DockPanel, StackPanel, Button, TextBlock, DataGrid, DataGridTextColumn,
    Orientation, Dock, DataGridSelectionMode, DataGridSelectionUnit,
    DataGridLength, DataGridLengthUnitType,
)
from System.Windows.Data import Binding, BindingMode, UpdateSourceTrigger
from System.Windows.Forms import FolderBrowserDialog, DialogResult

output = script.get_output()

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")


def _read_name(element):
    if element is None:
        return None
    try:
        n = element.Name
        if n:
            return n
    except Exception:
        pass
    return None


def _is_cloud_model_path(model_path):
    """See module docstring - ModelPathUtils has no IsCloudPath helper;
    detected via .NET reflection on ModelPath's derived-type name
    instead of assuming an API surface that doesn't exist."""
    try:
        return model_path.GetType().Name == "CloudPath"
    except Exception:
        return False


class LinkRow(object):
    """One row per RevitLinkType in a document. current_path/file_name
    are read once at scan time (never mutated afterward - a fresh scan
    replaces the whole row list instead, see module docstring re: why).
    new_path is the only field the user edits; empty means "no change
    requested" for that row."""

    def __init__(self, link_type):
        self.link_type = link_type
        self.id = link_type.Id
        self.is_cloud = False
        self.current_path = ""
        self.new_path = ""
        self.status = "Not Applied"

        file_name = None
        try:
            ext_ref = link_type.GetExternalFileReference()
            model_path = ext_ref.GetAbsolutePath()
            if _is_cloud_model_path(model_path):
                self.is_cloud = True
                self.current_path = "(Cloud Model - not editable here)"
            else:
                self.current_path = ModelPathUtils.ConvertModelPathToUserVisiblePath(model_path)
                if self.current_path:
                    file_name = os.path.basename(self.current_path)
        except Exception as e:
            self.current_path = "(Could not read path: {0})".format(e)

        self.file_name = file_name or _read_name(link_type) or "(unnamed link)"


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
    """Calls RevitLinkType.LoadFrom for every row with a non-empty
    New Path - deliberately NOT wrapped in a Transaction (see module
    docstring: LoadFrom requires no open transaction). Returns a list
    of (success, file_name, detail) for reporting; never raises."""
    results = []
    for row in pending_rows:
        new_text = (row.new_path or "").strip()
        if not new_text:
            continue
        if row.is_cloud:
            results.append((False, row.file_name, "Cloud-hosted link - not supported here"))
            continue
        try:
            new_model_path = ModelPathUtils.ConvertUserVisiblePathToModelPath(new_text)
            result = row.link_type.LoadFrom(new_model_path, None)
            if result is not None and result.LoadResult == LinkLoadResultType.LinkLoaded:
                results.append((True, row.file_name, "Relinked to '{0}'".format(new_text)))
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

    def __init__(self, document):
        self.document = document
        self.rows = []
        self.grid = None
        self.status_tb = None

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
        highlighted = list(self.grid.SelectedItems)
        if not highlighted:
            forms.alert("Click a row (Shift-click or Ctrl-click for more) to highlight rows first.")
            return
        dlg = FolderBrowserDialog()
        dlg.Description = "Pick the new folder for the highlighted link(s)"
        if dlg.ShowDialog() != DialogResult.OK:
            return
        folder = dlg.SelectedPath
        changed = 0
        for row in highlighted:
            if row.is_cloud:
                continue
            row.new_path = os.path.join(folder, row.file_name)
            changed += 1
        self.grid.Items.Refresh()
        if changed == 0:
            forms.alert("None of the highlighted rows can be relinked here (all cloud-hosted).")

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
        self._build_tabs()

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
            controller = DocTabController(doc)
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
