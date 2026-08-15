# -*- coding: utf-8 -*-
"""
DeeNWCs
Browse an ACC/BIM360 project, batch-open selected cloud-hosted Revit
files, and export every view whose name contains "NAVIS" (case
insensitive, any graphical view type) to its own NWC file.

The hub/project/file browsing and cloud-open plumbing is shared with
DeeOpener (BatchPack.panel) via lib/acc_file_browser.py - extracted from
DeeOpener's own proven logic without modifying that tool at all, so this
reuses the same fragile cloud-GUID resolution instead of re-deriving it.
The NAVIS-view detection and NWC export is new.

Nothing is saved back to any model - this only reads view names and
exports NWC files, then optionally closes each document (never saves
it) once its NAVIS view(s) are exported.

Highest-risk area (cannot be validated without live-testing inside
Revit): NavisworksExportOptions / NavisworksExportScope class and enum
names, and Document.Export's exact 3-argument (folder, file name,
NavisworksExportOptions) overload used here for NWC specifically - this
is a different Export overload shape than the ViewSet-based ones used
for DWG/PDF/IFC export elsewhere.
"""
import os
import re

from pyrevit import forms, script
from Autodesk.Revit.DB import (
    FilteredElementCollector, View, ViewType, NavisworksExportOptions, NavisworksExportScope
)

import acc_auth
import acc_file_browser as afb

import clr
clr.AddReference("System.Windows.Forms")
from System.Windows.Forms import FolderBrowserDialog, DialogResult

output = script.get_output()

_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".acc_file_cache.json")

_INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')

_NON_GRAPHICAL_VIEW_TYPE_NAMES = (
    "Schedule", "Legend", "DrawingSheet", "ProjectBrowser", "SystemBrowser",
    "Internal", "Undefined", "ColumnSchedule", "PanelSchedule",
)


def _sanitize_filename(name):
    cleaned = _INVALID_FILENAME_CHARS.sub("_", name or "").strip()
    return cleaned or "Unnamed"


def _non_graphical_view_types():
    excluded = set()
    for name in _NON_GRAPHICAL_VIEW_TYPE_NAMES:
        try:
            excluded.add(getattr(ViewType, name))
        except Exception:
            continue
    return excluded


def _find_navis_views(doc):
    """Every non-template view whose Name contains 'NAVIS' (case
    insensitive), excluding non-graphical view types (schedules, legends,
    sheets, etc.) that have no 3D geometry to export."""
    views = []
    excluded_types = _non_graphical_view_types()
    try:
        collector = FilteredElementCollector(doc).OfClass(View)
    except Exception:
        return views
    for v in collector:
        try:
            if v.IsTemplate:
                continue
            if v.ViewType in excluded_types:
                continue
            name = v.Name
            if name and "navis" in name.lower():
                views.append(v)
        except Exception:
            continue
    return views


def _export_view_to_nwc(doc, view, folder, base_name):
    file_name = _sanitize_filename("{0}_{1}".format(base_name, view.Name))
    try:
        opts = NavisworksExportOptions()
        opts.ExportScope = NavisworksExportScope.View
        opts.ViewId = view.Id
        doc.Export(folder, file_name, opts)
        return True, "Exported '{0}.nwc'".format(file_name)
    except Exception as e:
        return False, "FAILED: {0}".format(e)


def main():
    try:
        token = acc_auth.get_access_token()
    except Exception as e:
        forms.alert("Authentication failed:\n{0}".format(e))
        return

    hub_result = afb.pick_hub(token)
    if hub_result is None:
        return
    hub_id, region, _hub_name = hub_result

    project_result = afb.pick_project(hub_id, token)
    if project_result is None:
        return
    project_id, _project_name = project_result

    all_items = afb.list_project_files(hub_id, project_id, token, _CACHE_FILE)
    if not all_items:
        return

    selected_names = afb.pick_files_to_open(
        all_items, title="Select Files to Batch-Export NWC", button_name="Export Selected")
    if not selected_names:
        return

    folder_dlg = FolderBrowserDialog()
    folder_dlg.Description = "Choose a folder to save the exported NWC files"
    if folder_dlg.ShowDialog() != DialogResult.OK:
        return
    export_folder = folder_dlg.SelectedPath

    close_after = forms.alert(
        "Close each document after exporting its NAVIS view(s)? Recommended for large "
        "batches, to avoid running out of memory with many documents left open.",
        title="DeeNWCs", yes=True, no=True)

    uiapp = __revit__
    dismissed_log = []
    dialog_handler = afb.make_dialog_handler(dismissed_log)
    uiapp.DialogBoxShowing += dialog_handler

    results = []  # (ok, file_name, view_name, detail) - ok True/False/None (success/fail/skip)
    try:
        total = len(selected_names)
        with forms.ProgressBar(title="DeeNWCs — batch exporting NWC...", cancellable=True) as pb:
            for i, name in enumerate(selected_names):
                if pb.cancelled:
                    results.append((None, name, "-", "Cancelled"))
                    break
                pb.update_progress(i, total)

                item_id = all_items[name]
                before_count = len(dismissed_log)
                ui_doc, detail = afb.open_cloud_file(
                    uiapp, region, project_id, item_id, token, close_worksets=False)
                for msg, _sev in dismissed_log[before_count:]:
                    results.append((True, name, "-", msg))

                if ui_doc is None:
                    results.append((False, name, "-", "Could not open: {0}".format(detail)))
                    continue

                doc = ui_doc.Document
                navis_views = _find_navis_views(doc)
                if not navis_views:
                    results.append((None, name, "-", "No view with 'NAVIS' in its name - skipped"))
                else:
                    base_name = _sanitize_filename(name)
                    for view in navis_views:
                        ok, msg = _export_view_to_nwc(doc, view, export_folder, base_name)
                        results.append((ok, name, view.Name, msg))

                if close_after:
                    try:
                        doc.Close(False)
                    except Exception:
                        pass
    finally:
        uiapp.DialogBoxShowing -= dialog_handler

    html = '<h2 style="font-family:sans-serif;color:#ddd;">DeeNWCs Results</h2>'
    for ok, name, view_name, detail in results:
        bg = "#2e7d32" if ok else ("#455a64" if ok is None else "#c62828")
        icon = "&#10003;" if ok else ("&#9888;" if ok is None else "&#10007;")
        html += (
            '<div style="padding:6px 12px;margin:2px 0;background:{0};color:#fff;'
            'border-radius:4px;font-family:monospace;font-size:12px;">'
            '<b>{1}</b> [{2}] &nbsp;{3}&nbsp; {4}'
            '</div>'.format(bg, name, view_name, icon, detail))
    ok_count = sum(1 for r in results if r[0] is True)
    fail_count = sum(1 for r in results if r[0] is False)
    skip_count = sum(1 for r in results if r[0] is None)
    html += (
        '<hr><b style="font-family:sans-serif;">{0} view(s) exported, {1} failed, {2} '
        'skipped/cancelled (out of {3} file(s) processed).</b>'.format(
            ok_count, fail_count, skip_count, len(selected_names)))
    output.print_html(html)


main()
