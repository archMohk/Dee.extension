# -*- coding: utf-8 -*-
"""
DeeNWCs
Batch-exports views to NWC, from either currently-open LOCAL documents
or an ACC/BIM360 project (browse hub/project, batch-open selected cloud
files) - a new options window (DeeNWCsOptions.xaml, this tool's first
ever WPF window) gathers the source, which views to export, and the
full set of Navisworks exporter settings, all remembered between runs
in .dee_nwc_settings.json (same folder-local pattern as this tool's own
.acc_file_cache.json).

The hub/project/file browsing and cloud-open plumbing is shared with
DeeOpener (BatchPack.panel) via lib/acc_file_browser.py - extracted from
DeeOpener's own proven logic without modifying that tool at all, so this
reuses the same fragile cloud-GUID resolution instead of re-deriving it.
The view-matching and NWC export logic is this tool's own.

Nothing is saved back to any model - this only reads view names and
exports NWC files, then optionally closes each document (never saves
it) once its matching view(s) are exported.

Deliberately sequential native/pyRevit dialogs for everything AFTER the
options window - this codebase's hard rule is never to call PickObject,
ShowDialog, or a pyrevit.forms dialog from inside an already-open WPF
window (see DeeCtotopo/DeeMoveMirror's own docstrings for the same
reasoning). DeeNWCsOptionsWindow.ShowDialog() always returns and the
window closes BEFORE any FolderBrowserDialog, forms.SelectFromList, or
the whole ACC pick-hub/pick-project/pick-files chain runs.

--------------------------------------------------------------------
NWC Export Options - what's real API vs best-effort
--------------------------------------------------------------------
Confirmed against the official Revit API docs (NavisworksExportOptions
Properties page, fetched directly - not from memory) and wired for
real: ConvertElementProperties, ConvertLights, ConvertLinkedCADFormats,
Coordinates (NavisworksCoordinates: Shared/Internal), DivideFileIntoLevels,
ExportElementIds, ExportLinks, ExportParts, ExportRoomAsAttribute,
ExportRoomGeometry, ExportUrls, FacetingFactor, FindMissingMaterials,
Parameters (NavisworksParameters: None/Elements/All), plus the
pre-existing ExportScope=View/ViewId.

Four checkboxes shown in a live NWC-export screenshot ("Embed Textures",
"Separate Custom properties", "Strict Sectioning", "Type properties on
Elements") have NO documented property on NavisworksExportOptions in
that same API research - they may only exist in Revit's own manual
Export UI dialog, not the scriptable API, or under a name this research
didn't surface. _build_nwc_options() applies them best-effort via
setattr wrapped in try/except against a short list of plausible
property-name guesses per option; a name Revit's object doesn't
recognise just skips that ONE setting (collected into
`best_effort_skipped`, reported in the results) rather than failing the
whole export.

--------------------------------------------------------------------
NEEDS LIVE-REVIT VERIFICATION (per this codebase's convention)
--------------------------------------------------------------------
1. NavisworksExportOptions / NavisworksExportScope / NavisworksCoordinates
   / NavisworksParameters class and enum names - confirmed against
   documentation, not yet exercised live inside Revit from this session.
2. The four best-effort property names in _BEST_EFFORT_PROPS are
   plausible guesses (EmbedTextures, SeparateCustomProperties,
   StrictSectioning, ExportPartsAsBuildingElements/TypePropertiesOnElements) -
   none of these exact names were found in the official API docs, so
   they may all silently no-op on every Revit version. Not a crash risk
   either way (wrapped in try/except per name), just an open question on
   whether they do anything at all.
3. The Local source mode (_pick_local_documents) and wrapping its export
   loop in the same afb.make_dialog_handler used by the ACC path are new
   in this revision - not exercised live yet.
"""
import os
import re
import json

from pyrevit import forms, script
from Autodesk.Revit.DB import (
    FilteredElementCollector, View, ViewType, NavisworksExportOptions,
    NavisworksExportScope, NavisworksCoordinates, NavisworksParameters,
)

import acc_auth
import acc_file_browser as afb
import dee_branding

import clr
clr.AddReference("System.Windows.Forms")
from System.Windows.Forms import FolderBrowserDialog, DialogResult
import dee_telemetry
dee_telemetry.check_access("DeeNWCs")


output = script.get_output()

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "DeeNWCsOptions.xaml")
_CACHE_FILE = os.path.join(_THIS_DIR, ".acc_file_cache.json")
_SETTINGS_FILE = os.path.join(_THIS_DIR, ".dee_nwc_settings.json")

_INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')

_NON_GRAPHICAL_VIEW_TYPE_NAMES = (
    "Schedule", "Legend", "DrawingSheet", "ProjectBrowser", "SystemBrowser",
    "Internal", "Undefined", "ColumnSchedule", "PanelSchedule",
)

# "None" is a Python keyword - can't write NavisworksParameters.None, so
# this enum map is built once with getattr for that one entry.
_PARAM_ENUM = {
    "None": getattr(NavisworksParameters, "None"),
    "Elements": NavisworksParameters.Elements,
    "All": NavisworksParameters.All,
}

# Candidate property names to try, in order, for the four settings with
# no documented API property - see module docstring's own section on
# this. First one that setattr accepts wins; if none do, the setting is
# recorded as skipped rather than raised.
_BEST_EFFORT_PROPS = {
    "embed_textures": ("EmbedTextures",),
    "separate_custom_props": ("SeparateCustomProperties",),
    "strict_sectioning": ("StrictSectioning",),
    "type_properties": ("TypePropertiesOnElements", "ExportPartsAsBuildingElements"),
}

_DEFAULT_SETTINGS = {
    "source": "local",
    "view_contains": "NAVIS",
    "opts": {
        "convert_element_properties": False,
        "convert_lights": False,
        "convert_linked_cad": True,
        "export_links": False,
        "export_parts": False,
        "export_element_ids": True,
        "export_room_attribute": True,
        "export_urls": True,
        "divide_levels": True,
        "export_room_geometry": True,
        "find_missing_materials": True,
        "embed_textures": False,
        "separate_custom_props": False,
        "strict_sectioning": False,
        "type_properties": False,
        "coordinates": "Shared",
        "parameters": "All",
        "faceting_factor": 1.0,
    },
}


def _load_settings():
    """Merged onto _DEFAULT_SETTINGS key-by-key (not a bare json.load
    result) so a settings file saved by an OLDER version of this tool -
    missing a key a newer version added - never KeyErrors; it just uses
    the default for whatever's missing."""
    try:
        with open(_SETTINGS_FILE, "r") as f:
            data = json.load(f)
    except Exception:
        data = {}
    merged = dict(_DEFAULT_SETTINGS)
    merged["opts"] = dict(_DEFAULT_SETTINGS["opts"])
    for key, value in data.items():
        if key != "opts":
            merged[key] = value
    merged["opts"].update(data.get("opts", {}) or {})
    return merged


def _save_settings(settings):
    try:
        with open(_SETTINGS_FILE, "w") as f:
            json.dump(settings, f, indent=2)
    except Exception:
        pass


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


def _find_matching_views(doc, contains_text):
    """Every non-template, graphical view whose Name contains
    `contains_text` (case-insensitive) - generalized from the original
    hardcoded "NAVIS" substring; now set via the options window's View
    Selection field and saved between runs."""
    views = []
    needle = (contains_text or "").strip().lower()
    if not needle:
        return views
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
            if name and needle in name.lower():
                views.append(v)
        except Exception:
            continue
    return views


def _build_nwc_options(config, view_id, best_effort_skipped):
    """One fully-configured NavisworksExportOptions for a single export
    call - see module docstring for which properties are confirmed real
    API vs best-effort. `best_effort_skipped` is a set shared across the
    whole run; a property name this Revit's object doesn't accept adds
    its config key here instead of raising, so one unsupported setting
    never aborts an export."""
    o = config["opts"]
    opts = NavisworksExportOptions()
    opts.ExportScope = NavisworksExportScope.View
    opts.ViewId = view_id
    opts.ConvertElementProperties = bool(o["convert_element_properties"])
    opts.ConvertLights = bool(o["convert_lights"])
    opts.ConvertLinkedCADFormats = bool(o["convert_linked_cad"])
    opts.ExportLinks = bool(o["export_links"])
    opts.ExportParts = bool(o["export_parts"])
    opts.ExportElementIds = bool(o["export_element_ids"])
    opts.ExportRoomAsAttribute = bool(o["export_room_attribute"])
    opts.ExportUrls = bool(o["export_urls"])
    opts.DivideFileIntoLevels = bool(o["divide_levels"])
    opts.ExportRoomGeometry = bool(o["export_room_geometry"])
    opts.FindMissingMaterials = bool(o["find_missing_materials"])
    opts.FacetingFactor = float(o["faceting_factor"])
    opts.Coordinates = (NavisworksCoordinates.Internal
                        if o["coordinates"] == "Internal"
                        else NavisworksCoordinates.Shared)
    opts.Parameters = _PARAM_ENUM.get(o["parameters"], _PARAM_ENUM["All"])

    for key, candidate_names in _BEST_EFFORT_PROPS.items():
        if not o.get(key):
            continue
        applied = False
        for prop_name in candidate_names:
            try:
                setattr(opts, prop_name, True)
                applied = True
                break
            except Exception:
                continue
        if not applied:
            best_effort_skipped.add(key)
    return opts


def _export_view_to_nwc(doc, view, folder, base_name, config, best_effort_skipped):
    file_name = _sanitize_filename("{0}_{1}".format(base_name, view.Name))
    try:
        opts = _build_nwc_options(config, view.Id, best_effort_skipped)
        doc.Export(folder, file_name, opts)
        return True, "Exported '{0}.nwc'".format(file_name)
    except Exception as e:
        return False, "FAILED: {0}".format(e)


class _SafeProgress(object):
    """forms.ProgressBar tries to set Window.TaskbarItemInfo on its host
    window - a genuine WPF-level bug (not this extension's code):
    Window.TaskbarItemInfo throws NotImplementedException whenever the
    underlying ITaskbarList::HrInit COM call fails, which is documented
    to happen specifically under Remote Desktop/Terminal Services or a
    custom shell without a taskbar (live-confirmed in DeeSheetLinks).

    Wraps the real forms.ProgressBar and falls back to running with NO
    progress UI at all if entering it fails, so the tool degrades
    gracefully under RDP instead of crashing - everyone else still gets
    the real progress bar exactly as before. `pb.update_progress(...)`/
    `pb.cancelled` are safe no-ops in the fallback case, so callers never
    need an extra branch."""
    def __init__(self, **kwargs):
        self._kwargs = kwargs
        self._real = None

    def __enter__(self):
        try:
            self._real = forms.ProgressBar(**self._kwargs)
            return self._real.__enter__()
        except Exception:
            self._real = None
            return self

    def __exit__(self, exc_type, exc_value, tb):
        if self._real is not None:
            return self._real.__exit__(exc_type, exc_value, tb)
        return False

    @property
    def cancelled(self):
        return False

    def update_progress(self, i, total):
        pass


# ==========================================================================
# options window
# ==========================================================================
class DeeNWCsOptionsWindow(dee_branding.DeeBrandedWindow):
    _ready = False

    def __init__(self, xaml_file):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        # Set by continue_click(), read by main() AFTER ShowDialog()
        # returns - never acted on from inside this still-open window
        # (this codebase's hard rule - see module docstring).
        self.result = None
        self.config = None
        self._apply_settings(_load_settings())
        self._ready = True

    def _apply_settings(self, s):
        if s.get("source") == "acc":
            self.source_acc_rb.IsChecked = True
        else:
            self.source_local_rb.IsChecked = True
        self.view_contains_tb.Text = s.get("view_contains", "NAVIS")
        o = s.get("opts", {})
        self.opt_convert_element_properties.IsChecked = bool(o.get("convert_element_properties", False))
        self.opt_convert_lights.IsChecked = bool(o.get("convert_lights", False))
        self.opt_convert_linked_cad.IsChecked = bool(o.get("convert_linked_cad", True))
        self.opt_export_links.IsChecked = bool(o.get("export_links", False))
        self.opt_export_parts.IsChecked = bool(o.get("export_parts", False))
        self.opt_export_element_ids.IsChecked = bool(o.get("export_element_ids", True))
        self.opt_export_room_attribute.IsChecked = bool(o.get("export_room_attribute", True))
        self.opt_export_urls.IsChecked = bool(o.get("export_urls", True))
        self.opt_divide_levels.IsChecked = bool(o.get("divide_levels", True))
        self.opt_export_room_geometry.IsChecked = bool(o.get("export_room_geometry", True))
        self.opt_find_missing_materials.IsChecked = bool(o.get("find_missing_materials", True))
        self.opt_embed_textures.IsChecked = bool(o.get("embed_textures", False))
        self.opt_separate_custom_props.IsChecked = bool(o.get("separate_custom_props", False))
        self.opt_strict_sectioning.IsChecked = bool(o.get("strict_sectioning", False))
        self.opt_type_properties.IsChecked = bool(o.get("type_properties", False))
        self.opt_coordinates_cb.SelectedIndex = 1 if o.get("coordinates") == "Internal" else 0
        self.opt_parameters_cb.SelectedIndex = {"None": 0, "Elements": 1, "All": 2}.get(
            o.get("parameters", "All"), 2)
        try:
            self.opt_faceting_factor_tb.Text = str(o.get("faceting_factor", 1.0))
        except Exception:
            pass

    def _gather_config(self):
        """Returns the config dict, or None if the Faceting Factor field
        isn't a valid number - the one field here that can't just be
        coerced to a safe default, since a silently-wrong faceting value
        would change every exported file's geometry quality without the
        user noticing."""
        try:
            faceting = float(self.opt_faceting_factor_tb.Text)
        except Exception:
            return None
        opts = {
            "convert_element_properties": self.opt_convert_element_properties.IsChecked is True,
            "convert_lights": self.opt_convert_lights.IsChecked is True,
            "convert_linked_cad": self.opt_convert_linked_cad.IsChecked is True,
            "export_links": self.opt_export_links.IsChecked is True,
            "export_parts": self.opt_export_parts.IsChecked is True,
            "export_element_ids": self.opt_export_element_ids.IsChecked is True,
            "export_room_attribute": self.opt_export_room_attribute.IsChecked is True,
            "export_urls": self.opt_export_urls.IsChecked is True,
            "divide_levels": self.opt_divide_levels.IsChecked is True,
            "export_room_geometry": self.opt_export_room_geometry.IsChecked is True,
            "find_missing_materials": self.opt_find_missing_materials.IsChecked is True,
            "embed_textures": self.opt_embed_textures.IsChecked is True,
            "separate_custom_props": self.opt_separate_custom_props.IsChecked is True,
            "strict_sectioning": self.opt_strict_sectioning.IsChecked is True,
            "type_properties": self.opt_type_properties.IsChecked is True,
            "coordinates": "Internal" if self.opt_coordinates_cb.SelectedIndex == 1 else "Shared",
            "parameters": ["None", "Elements", "All"][self.opt_parameters_cb.SelectedIndex],
            "faceting_factor": faceting,
        }
        source = "acc" if self.source_acc_rb.IsChecked is True else "local"
        view_contains = (self.view_contains_tb.Text or "").strip()
        return {"source": source, "view_contains": view_contains, "opts": opts}

    def continue_click(self, sender, args):
        config = self._gather_config()
        if config is None:
            self.status_tb.Text = "Faceting Factor must be a number (e.g. 1 or 0.5)."
            return
        if not config["view_contains"]:
            self.status_tb.Text = "Enter text the view name should contain."
            return
        _save_settings(config)
        self.config = config
        self.result = "run"
        self.Close()

    def close_click(self, sender, args):
        self.Close()


def _pick_local_documents(uiapp):
    """Every currently open, non-linked document - if more than one is
    open, lets the user pick which ones to batch-export via pyRevit's
    own SelectFromList, called AFTER the options window has already
    closed (this codebase's hard rule - see module docstring)."""
    docs = []
    try:
        for d in uiapp.Application.Documents:
            try:
                if d.IsLinked:
                    continue
            except Exception:
                pass
            docs.append(d)
    except Exception:
        pass
    if not docs:
        forms.alert("No open documents found.", title="DeeNWCs")
        return []
    if len(docs) == 1:
        return docs
    by_title = {}
    for d in docs:
        try:
            by_title[d.Title] = d
        except Exception:
            continue
    selected = forms.SelectFromList.show(
        sorted(by_title.keys()),
        title="DeeNWCs - Select open document(s) to export",
        multiselect=True,
        button_name="Export Selected")
    if not selected:
        return []
    return [by_title[name] for name in selected]


def _run_local(uiapp, config, export_folder, best_effort_skipped):
    docs = _pick_local_documents(uiapp)
    if not docs:
        return []
    results = []
    dismissed_log = []
    dialog_handler = afb.make_dialog_handler(dismissed_log)
    uiapp.DialogBoxShowing += dialog_handler
    try:
        total = len(docs)
        with _SafeProgress(title="DeeNWCs — exporting NWC...", cancellable=True) as pb:
            for i, doc in enumerate(docs):
                if pb.cancelled:
                    results.append((None, doc.Title, "-", "Cancelled"))
                    break
                pb.update_progress(i, total)
                before_count = len(dismissed_log)
                matching_views = _find_matching_views(doc, config["view_contains"])
                for msg, _sev in dismissed_log[before_count:]:
                    results.append((True, doc.Title, "-", msg))
                if not matching_views:
                    results.append((None, doc.Title, "-",
                        u"No view containing '{0}' - skipped".format(config["view_contains"])))
                    continue
                base_name = _sanitize_filename(doc.Title)
                for view in matching_views:
                    ok, msg = _export_view_to_nwc(
                        doc, view, export_folder, base_name, config, best_effort_skipped)
                    results.append((ok, doc.Title, view.Name, msg))
    finally:
        uiapp.DialogBoxShowing -= dialog_handler
    return results


def _run_acc(uiapp, config, export_folder, best_effort_skipped):
    try:
        token = acc_auth.get_access_token()
    except Exception as e:
        forms.alert("Authentication failed:\n{0}".format(e))
        return []

    hub_result = afb.pick_hub(token)
    if hub_result is None:
        return []
    hub_id, region, _hub_name = hub_result

    project_result = afb.pick_project(hub_id, token)
    if project_result is None:
        return []
    project_id, _project_name = project_result

    all_items = afb.list_project_files(hub_id, project_id, token, _CACHE_FILE)
    if not all_items:
        return []

    selected_names = afb.pick_files_to_open(
        all_items, title="Select Files to Batch-Export NWC", button_name="Export Selected")
    if not selected_names:
        return []

    close_after = forms.alert(
        "Close each document after exporting its matching view(s)? Recommended for "
        "large batches, to avoid running out of memory with many documents left open.",
        title="DeeNWCs", yes=True, no=True)

    results = []
    dismissed_log = []
    dialog_handler = afb.make_dialog_handler(dismissed_log)
    uiapp.DialogBoxShowing += dialog_handler
    try:
        total = len(selected_names)
        with _SafeProgress(title="DeeNWCs — batch exporting NWC...", cancellable=True) as pb:
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
                matching_views = _find_matching_views(doc, config["view_contains"])
                if not matching_views:
                    results.append((None, name, "-",
                        u"No view containing '{0}' - skipped".format(config["view_contains"])))
                else:
                    base_name = _sanitize_filename(name)
                    for view in matching_views:
                        ok, msg = _export_view_to_nwc(
                            doc, view, export_folder, base_name, config, best_effort_skipped)
                        results.append((ok, name, view.Name, msg))

                if close_after:
                    try:
                        doc.Close(False)
                    except Exception:
                        pass
    finally:
        uiapp.DialogBoxShowing -= dialog_handler
    return results


def main():
    uiapp = __revit__

    window = DeeNWCsOptionsWindow(_XAML_FILE)
    window.ShowDialog()
    if window.result != "run" or window.config is None:
        return
    config = window.config

    folder_dlg = FolderBrowserDialog()
    folder_dlg.Description = "Choose a folder to save the exported NWC files"
    if folder_dlg.ShowDialog() != DialogResult.OK:
        return
    export_folder = folder_dlg.SelectedPath

    best_effort_skipped = set()
    if config["source"] == "local":
        results = _run_local(uiapp, config, export_folder, best_effort_skipped)
    else:
        results = _run_acc(uiapp, config, export_folder, best_effort_skipped)

    if not results:
        return

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
        'skipped/cancelled.</b>'.format(ok_count, fail_count, skip_count))
    if best_effort_skipped:
        html += (
            '<p style="font-family:sans-serif;color:#888;font-size:12px;">Note: {0} '
            'best-effort NWC setting(s) had no matching property on this Revit '
            'version\'s exporter and were skipped: {1}.</p>'.format(
                len(best_effort_skipped), ", ".join(sorted(best_effort_skipped))))
    output.print_html(html)


main()
