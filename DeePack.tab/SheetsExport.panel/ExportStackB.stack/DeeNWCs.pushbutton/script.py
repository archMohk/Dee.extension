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
Super Config - an auto-managed export view instead of pre-named ones
--------------------------------------------------------------------
Live report: a plain Floor stayed flat instead of following a sloped
Toposolid in a DIFFERENT tool this session (DeeDropToSurface) - the
same underlying idea shows up here as its own request: instead of
relying on the modeler having pre-named a view "NAVIS" (or whatever the
View Selection filter says), Super Config finds-or-creates ONE dedicated
View3D per document (_get_or_create_export_view) and exports exactly
that view, ignoring the View Selection filter entirely while enabled.

Every Super Config run (re-)applies the FULL current config to the view,
whether it was just created or already existed from a previous run -
so editing settings and re-running with "reuse" always reflects the
latest config rather than whatever the view happened to be set up with
the first time:
  - Levels and Grids are always hidden (the original request).
  - "Also hide clutter" additionally hides Scope Boxes, Reference Planes
    (OST_CLines) and Sun Path (OST_SunPath) - each tried individually via
    getattr+try/except, so a BuiltInCategory name that doesn't exist on
    a given Revit version (or a category CanCategoryBeHidden refuses)
    just never gets hidden, rather than raising.
  - "Include linked models" toggles the OST_RvtLinks category's
    visibility in that one view - a stronger, simpler exclusion than the
    separate NWC ExportLinks setting (nothing to convert if nothing is
    visible), independent of whatever ExportLinks itself is set to in
    the main NWC Export Options panel.
  - Discipline is a genuine ComboBox (ViewDiscipline is a fixed enum,
    not project-specific) - Phase is NOT (Phases differ project to
    project, and the options window is shown before any document is
    even opened for the ACC path), so instead of naming a specific
    phase, the choice is a STRATEGY: "last phase in the project"
    (highest PHASE_SEQUENCE_NUMBER, found per-document at export time -
    the most inclusive choice for a federation export) or "leave
    default" (don't touch it at all).
  - view_mode "reuse" keeps the view around for next time (updating its
    settings each run); "delete" removes it in its own Transaction
    right after that document's export, so a delete failure (reported,
    not fatal) can never roll back an export that already succeeded.

--------------------------------------------------------------------
Folder & File Naming - parsing the Revit file name into labeled parts
--------------------------------------------------------------------
Live request: fold the export destination and file name around the
project's own file-naming convention (e.g. "type of building and
discipline" encoded as delimited parts of the file name), generically -
this tool has no way to know any specific company's convention, so
instead of guessing at one, the user labels their OWN convention once:
a delimiter (default "-") and a naming TEMPLATE using that same
delimiter, e.g. "Project-Discipline-Level-Type" - each word becomes a
label. _parse_segments() then zips any real file name split on the same
delimiter against those labels, so "ABC-AR-L02-DR001" resolves to
{Project: ABC, Discipline: AR, Level: L02, Type: DR001}.

Two more templates consume those labels via {Label} tokens
(_resolve_tokens, a plain regex substitution - an unrecognised {Key} is
left LITERALLY in the output rather than silently dropped, so a typo is
visible instead of invisible):
  - Folder structure: label names separated by "/", e.g.
    "Discipline/Level" nests output into <export folder>/AR/L02/.
  - Exported file name: defaults to "{RevitFileName}_{ViewName}" -
    exactly reproducing this tool's ORIGINAL, pre-this-revision naming
    (base_name + "_" + view.Name) - plus {RevitFileName}/{ViewName} are
    always available even with no naming template set, so nothing
    changes for a user who never touches this section.

A missing/blank naming template disables all of this - every file
exports flat, named the original way, exactly as before this feature
existed. _show_preview_and_confirm() prints every selected file's
resolved folder + naming pattern to the output window and asks to
proceed BEFORE any file is opened or any export runs, so a wrong
delimiter/template mapping is caught before wasting a whole batch on it
- the real per-view file name (using the view's actual Name, not the
preview's "<view>" placeholder) can only be confirmed once each
document is actually open, so the preview covers the FOLDER mapping
precisely (already fully knowable from the file name alone) and shows
the naming PATTERN rather than every exact final name.

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
4. Super Config in its entirety (view creation/reuse/deletion, category
   hiding, discipline/phase assignment) is brand new and untested live -
   in particular, whether OST_CLines and OST_SunPath are the correct
   BuiltInCategory names for "Reference Planes" and "Sun Path" on every
   Revit version, and whether CreateIsometric + immediate category/
   discipline/phase edits inside one Transaction behaves as expected.
5. Folder/naming template parsing (_parse_segments/_resolve_tokens/
   _build_export_folder) and the preview step are new and untested
   live - in particular, os.makedirs() being called for every export
   (needed since a folder-structure rule can name a subfolder that
   doesn't exist yet) assumes Document.Export itself does NOT need the
   folder to pre-exist beyond that; not verified against a real Export
   call from this session.
"""
import os
import re
import json

from pyrevit import forms, script
from Autodesk.Revit.DB import (
    FilteredElementCollector, View, View3D, ViewType, ViewFamilyType, ViewFamily,
    ViewDiscipline, Category, BuiltInCategory, BuiltInParameter, Phase,
    Transaction, NavisworksExportOptions, NavisworksExportScope,
    NavisworksCoordinates, NavisworksParameters,
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
    "super_config": {
        "enabled": False,
        "view_name": "NWC Export View",
        "with_links": False,
        "view_mode": "reuse",       # "reuse" or "delete"
        "hide_clutter": False,
        "discipline": "Coordination",
        "phase": "last",           # "last" or "default"
    },
    "naming": {
        "delimiter": "-",
        "template": "",                            # e.g. "Project-Discipline-Level-Type"
        "folder_structure": "",                     # e.g. "Discipline/Level" - blank = flat
        "output_template": "{RevitFileName}_{ViewName}",
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
    merged["super_config"] = dict(_DEFAULT_SETTINGS["super_config"])
    merged["naming"] = dict(_DEFAULT_SETTINGS["naming"])
    for key, value in data.items():
        if key not in ("opts", "super_config", "naming"):
            merged[key] = value
    merged["opts"].update(data.get("opts", {}) or {})
    merged["super_config"].update(data.get("super_config", {}) or {})
    merged["naming"].update(data.get("naming", {}) or {})
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


# ==========================================================================
# Super Config - dedicated, auto-managed export view
# ==========================================================================
# Category BuiltInCategory names to hide unconditionally (Levels, Grids)
# and, if "hide_clutter" is on, the extra clutter categories too. Tried
# individually via getattr + try/except - a name that doesn't exist on a
# given Revit version (or a category the view can't hide) just never
# gets added/hidden rather than raising, matching this codebase's
# established fail-safe convention for this kind of per-item lookup
# (see e.g. DeeNWCs' own _non_graphical_view_types or DeeDropToSurface's
# category-hiding loop in an earlier, unrelated tool).
_ALWAYS_HIDE_CATEGORIES = ("OST_Levels", "OST_Grids")
_CLUTTER_CATEGORIES = ("OST_ScopeBoxes", "OST_CLines", "OST_SunPath")
_LINKS_CATEGORY = "OST_RvtLinks"

_DISCIPLINE_ENUM = {
    "Coordination": ViewDiscipline.Coordination,
    "Architectural": ViewDiscipline.Architectural,
    "Structural": ViewDiscipline.Structural,
    "Mechanical": ViewDiscipline.Mechanical,
    "Electrical": ViewDiscipline.Electrical,
    "Plumbing": ViewDiscipline.Plumbing,
}


def _hide_category_if_possible(doc, view, bic_name):
    try:
        bic = getattr(BuiltInCategory, bic_name)
        cat = Category.GetCategory(doc, bic)
        if cat is not None and view.CanCategoryBeHidden(cat.Id):
            view.SetCategoryHidden(cat.Id, True)
    except Exception:
        pass


def _set_category_visibility_if_possible(doc, view, bic_name, hidden):
    try:
        bic = getattr(BuiltInCategory, bic_name)
        cat = Category.GetCategory(doc, bic)
        if cat is not None and view.CanCategoryBeHidden(cat.Id):
            view.SetCategoryHidden(cat.Id, hidden)
    except Exception:
        pass


def _find_view_by_name(doc, name):
    try:
        for v in FilteredElementCollector(doc).OfClass(View3D):
            if not v.IsTemplate and v.Name == name:
                return v
    except Exception:
        pass
    return None


def _create_export_view3d(doc):
    vft_id = None
    for vft in FilteredElementCollector(doc).OfClass(ViewFamilyType):
        if vft.ViewFamily == ViewFamily.ThreeDimensional:
            vft_id = vft.Id
            break
    if vft_id is None:
        return None
    return View3D.CreateIsometric(doc, vft_id)


def _last_phase(doc):
    last = None
    try:
        for p in FilteredElementCollector(doc).OfClass(Phase):
            if last is None or p.get_Parameter(BuiltInParameter.PHASE_SEQUENCE_NUMBER).AsInteger() \
                    > last.get_Parameter(BuiltInParameter.PHASE_SEQUENCE_NUMBER).AsInteger():
                last = p
    except Exception:
        pass
    return last


def _apply_super_config_settings(doc, view, sc):
    """Applies every Super Config setting to `view` - called for BOTH a
    freshly-created view and a reused one, so a reused view always
    reflects the CURRENT config rather than whatever it was set up with
    the first time it was created."""
    for bic_name in _ALWAYS_HIDE_CATEGORIES:
        _hide_category_if_possible(doc, view, bic_name)
    if sc.get("hide_clutter"):
        for bic_name in _CLUTTER_CATEGORIES:
            _hide_category_if_possible(doc, view, bic_name)
    _set_category_visibility_if_possible(
        doc, view, _LINKS_CATEGORY, hidden=not sc.get("with_links"))
    try:
        view.Discipline = _DISCIPLINE_ENUM.get(sc.get("discipline"), ViewDiscipline.Coordination)
    except Exception:
        pass
    if sc.get("phase") == "last":
        phase = _last_phase(doc)
        if phase is not None:
            try:
                phase_param = view.get_Parameter(BuiltInParameter.VIEW_PHASE)
                if phase_param is not None and not phase_param.IsReadOnly:
                    phase_param.Set(phase.Id)
            except Exception:
                pass


def _get_or_create_export_view(doc, sc):
    """Returns (view, created_new) per Super Config's view_name/view_mode -
    finds an existing view by that exact name first (reused regardless of
    view_mode, since "delete" mode simply means none should normally be
    left over from a PRIOR run - if one somehow still exists, reusing it
    is still safer than silently creating a second, confusingly-similar
    view), creating a fresh View3D only if none exists. Settings are
    (re-)applied either way. Returns (None, False) if no 3D ViewFamilyType
    exists in this document's templates at all."""
    name = (sc.get("view_name") or "NWC Export View").strip() or "NWC Export View"
    view = _find_view_by_name(doc, name)
    created_new = False
    if view is None:
        view = _create_export_view3d(doc)
        if view is None:
            return None, False
        try:
            view.Name = name
        except Exception:
            pass
        created_new = True
    _apply_super_config_settings(doc, view, sc)
    return view, created_new


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


def _split_template(template, delimiter):
    """The naming template's own labels, in order - e.g. "Project-
    Discipline-Level" with delimiter "-" gives ["Project", "Discipline",
    "Level"]. Empty parts (a doubled delimiter) are dropped rather than
    producing a blank label name."""
    if not template or not delimiter:
        return []
    return [seg.strip() for seg in template.split(delimiter) if seg.strip()]


def _parse_segments(file_label, delimiter, labels):
    """{label: value}, zipping file_label.split(delimiter) against
    `labels` (the naming template's own labels, same delimiter). A file
    with FEWER parts than the template has maps the missing labels to
    '' rather than raising; extra parts beyond the template's own labels
    are ignored (there's nothing to name them)."""
    if not delimiter or not labels:
        return {}
    parts = file_label.split(delimiter)
    return dict((label, parts[i].strip() if i < len(parts) else "")
                for i, label in enumerate(labels))


def _resolve_tokens(template, tokens):
    """Replaces every {Key} in `template` with tokens.get(Key). A {Key}
    with no matching token is left LITERALLY in the output rather than
    silently dropped or raising - a typo'd label name shows up plainly
    in the exported file name / preview instead of vanishing."""
    def repl(match):
        key = match.group(1)
        return tokens[key] if key in tokens else match.group(0)
    return re.sub(r"\{(\w+)\}", repl, template or "")


def _build_export_folder(base_folder, folder_structure, segments):
    """base_folder, with one subfolder per label named in
    `folder_structure` (label names separated by "/") - e.g.
    "Discipline/Level" with segments {"Discipline": "AR", "Level": "L02"}
    gives base_folder/AR/L02. A label with no resolved value (missing
    from the template, or the file had fewer parts than expected) uses
    "Unspecified" as its folder name rather than collapsing the path or
    raising, so a mapping mistake is visible as a folder name instead of
    silently merging unrelated files together."""
    if not folder_structure:
        return base_folder
    sub = base_folder
    for label in (p.strip() for p in folder_structure.split("/") if p.strip()):
        value = _sanitize_filename(segments.get(label) or "Unspecified")
        sub = os.path.join(sub, value)
    return sub


def _resolve_output(doc_label, view_name, config, export_folder):
    """(output_folder, output_file_name) for one view of one document -
    shared by the preview (view_name="<view>" as a placeholder, since
    the real view names aren't known until the document is open - see
    module docstring) and the real export (the view's actual Name)."""
    nm = config.get("naming", {})
    labels = _split_template(nm.get("template", ""), nm.get("delimiter", "-"))
    segments = _parse_segments(doc_label, nm.get("delimiter", "-"), labels)
    tokens = dict(segments)
    tokens["RevitFileName"] = _sanitize_filename(doc_label)
    tokens["ViewName"] = view_name
    out_folder = _build_export_folder(
        export_folder, nm.get("folder_structure", ""), segments)
    out_name = _resolve_tokens(
        nm.get("output_template") or "{RevitFileName}_{ViewName}", tokens)
    return out_folder, _sanitize_filename(out_name)


def _export_view_to_nwc(doc, view, output_folder, output_name, config, best_effort_skipped):
    file_name = _sanitize_filename(output_name)
    try:
        if not os.path.isdir(output_folder):
            os.makedirs(output_folder)
        opts = _build_nwc_options(config, view.Id, best_effort_skipped)
        doc.Export(output_folder, file_name, opts)
        return True, u"Exported '{0}.nwc' to {1}".format(file_name, output_folder)
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

        sc = s.get("super_config", {})
        self.super_config_cb.IsChecked = bool(sc.get("enabled", False))
        self.super_config_panel.IsEnabled = bool(sc.get("enabled", False))
        self.sc_view_name_tb.Text = sc.get("view_name", "NWC Export View")
        self.sc_with_links_cb.IsChecked = bool(sc.get("with_links", False))
        if sc.get("view_mode") == "delete":
            self.sc_delete_rb.IsChecked = True
        else:
            self.sc_reuse_rb.IsChecked = True
        self.sc_hide_clutter_cb.IsChecked = bool(sc.get("hide_clutter", False))
        disciplines = ["Coordination", "Architectural", "Structural", "Mechanical", "Electrical", "Plumbing"]
        try:
            self.sc_discipline_cb.SelectedIndex = disciplines.index(sc.get("discipline", "Coordination"))
        except ValueError:
            self.sc_discipline_cb.SelectedIndex = 0
        self.sc_phase_cb.SelectedIndex = 1 if sc.get("phase") == "default" else 0

        nm = s.get("naming", {})
        self.name_delimiter_tb.Text = nm.get("delimiter", "-")
        self.name_template_tb.Text = nm.get("template", "")
        self.name_folder_structure_tb.Text = nm.get("folder_structure", "")
        self.name_output_template_tb.Text = nm.get("output_template", "{RevitFileName}_{ViewName}")

    def super_config_toggled(self, sender, args):
        if not self._ready:
            return
        self.super_config_panel.IsEnabled = self.super_config_cb.IsChecked is True

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
        disciplines = ["Coordination", "Architectural", "Structural", "Mechanical", "Electrical", "Plumbing"]
        super_config = {
            "enabled": self.super_config_cb.IsChecked is True,
            "view_name": (self.sc_view_name_tb.Text or "").strip(),
            "with_links": self.sc_with_links_cb.IsChecked is True,
            "view_mode": "delete" if self.sc_delete_rb.IsChecked is True else "reuse",
            "hide_clutter": self.sc_hide_clutter_cb.IsChecked is True,
            "discipline": disciplines[self.sc_discipline_cb.SelectedIndex],
            "phase": "default" if self.sc_phase_cb.SelectedIndex == 1 else "last",
        }
        naming = {
            "delimiter": (self.name_delimiter_tb.Text or "-"),
            "template": (self.name_template_tb.Text or "").strip(),
            "folder_structure": (self.name_folder_structure_tb.Text or "").strip(),
            "output_template": (self.name_output_template_tb.Text or "").strip()
                                or "{RevitFileName}_{ViewName}",
        }
        return {"source": source, "view_contains": view_contains, "opts": opts,
                "super_config": super_config, "naming": naming}

    def continue_click(self, sender, args):
        config = self._gather_config()
        if config is None:
            self.status_tb.Text = "Faceting Factor must be a number (e.g. 1 or 0.5)."
            return
        if config["super_config"]["enabled"]:
            if not config["super_config"]["view_name"]:
                self.status_tb.Text = "Enter a name for the Super Config created view."
                return
        elif not config["view_contains"]:
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


def _export_for_document(doc, doc_label, config, export_folder, best_effort_skipped):
    """One document's worth of export results (list of (ok, doc_label,
    view_name, detail) tuples) - shared by both _run_local and _run_acc
    so the Super Config vs plain-filter branch only needs writing once.

    Super Config: get-or-create the dedicated view (inside its own
    Transaction - creating/renaming/hiding categories all modify the
    document), export exactly that one view, then delete it afterward
    if view_mode is "delete" (its own separate Transaction, so a delete
    failure can never roll back a successful export that already
    happened). Otherwise: unchanged plain "every view whose name
    contains X" behaviour."""
    sc = config.get("super_config", {})
    if sc.get("enabled"):
        t = Transaction(doc, "DeeNWCs - prepare export view")
        t.Start()
        try:
            view, _created = _get_or_create_export_view(doc, sc)
            t.Commit()
        except Exception as e:
            t.RollBack()
            return [(False, doc_label, "-", "Could not prepare export view: {0}".format(e))]
        if view is None:
            return [(False, doc_label, "-",
                    "This document has no 3D view type available - can't create the export view.")]

        out_folder, out_name = _resolve_output(doc_label, view.Name, config, export_folder)
        ok, msg = _export_view_to_nwc(doc, view, out_folder, out_name, config, best_effort_skipped)
        results = [(ok, doc_label, view.Name, msg)]

        if sc.get("view_mode") == "delete":
            t2 = Transaction(doc, "DeeNWCs - remove export view")
            t2.Start()
            try:
                doc.Delete(view.Id)
                t2.Commit()
            except Exception as e:
                t2.RollBack()
                results.append((None, doc_label, view.Name,
                                u"Export view could not be deleted afterward: {0}".format(e)))
        return results

    matching_views = _find_matching_views(doc, config["view_contains"])
    if not matching_views:
        return [(None, doc_label, "-",
                u"No view containing '{0}' - skipped".format(config["view_contains"]))]
    results = []
    for view in matching_views:
        out_folder, out_name = _resolve_output(doc_label, view.Name, config, export_folder)
        ok, msg = _export_view_to_nwc(doc, view, out_folder, out_name, config, best_effort_skipped)
        results.append((ok, doc_label, view.Name, msg))
    return results


def _run_local(uiapp, docs, config, export_folder, best_effort_skipped):
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
                doc_results = _export_for_document(
                    doc, doc.Title, config, export_folder, best_effort_skipped)
                for msg, _sev in dismissed_log[before_count:]:
                    results.append((True, doc.Title, "-", msg))
                results.extend(doc_results)
    finally:
        uiapp.DialogBoxShowing -= dialog_handler
    return results


def _pick_acc_files(uiapp):
    """Everything ACC-side that must happen BEFORE the preview step -
    auth, hub/project/file pick, and the close-after-export prompt.
    Returns a dict for _run_acc, or None if the user backed out anywhere
    along the chain."""
    try:
        token = acc_auth.get_access_token()
    except Exception as e:
        forms.alert("Authentication failed:\n{0}".format(e))
        return None

    hub_result = afb.pick_hub(token)
    if hub_result is None:
        return None
    hub_id, region, _hub_name = hub_result

    project_result = afb.pick_project(hub_id, token)
    if project_result is None:
        return None
    project_id, _project_name = project_result

    all_items = afb.list_project_files(hub_id, project_id, token, _CACHE_FILE)
    if not all_items:
        return None

    selected_names = afb.pick_files_to_open(
        all_items, title="Select Files to Batch-Export NWC", button_name="Export Selected")
    if not selected_names:
        return None

    close_after = forms.alert(
        "Close each document after exporting its matching view(s)? Recommended for "
        "large batches, to avoid running out of memory with many documents left open.",
        title="DeeNWCs", yes=True, no=True)

    return {
        "token": token, "region": region, "project_id": project_id,
        "all_items": all_items, "selected_names": selected_names,
        "close_after": close_after,
    }


def _run_acc(uiapp, acc_ctx, config, export_folder, best_effort_skipped):
    region = acc_ctx["region"]
    project_id = acc_ctx["project_id"]
    all_items = acc_ctx["all_items"]
    selected_names = acc_ctx["selected_names"]
    token = acc_ctx["token"]
    close_after = acc_ctx["close_after"]

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
                results.extend(_export_for_document(
                    doc, name, config, export_folder, best_effort_skipped))

                if close_after:
                    try:
                        doc.Close(False)
                    except Exception:
                        pass
    finally:
        uiapp.DialogBoxShowing -= dialog_handler
    return results


def _show_preview_and_confirm(file_labels, config, export_folder):
    """Prints a per-file preview (destination folder + parsed naming
    segments) to the output window, then asks to proceed. The exported
    file's VIEW NAME isn't known yet for ACC sources (the file isn't
    open until the real export loop runs) - shown as the literal
    placeholder "<view>" here, since only the FOLDER/segment mapping
    (the thing a naming-convention mistake would get wrong) needs to be
    accurate before committing to a whole batch; the exact per-view file
    name is confirmed in the results afterward instead."""
    nm = config.get("naming", {})
    labels = _split_template(nm.get("template", ""), nm.get("delimiter", "-"))
    lines = [u"**DeeNWCs — Preview**", u"",
             u"- Destination base folder: `{0}`".format(export_folder)]
    if labels:
        lines.append(u"- Naming template labels: {0}".format(", ".join(labels)))
    else:
        lines.append(u"- No naming template set - every file exports flat into the "
                     u"base folder with the default `{RevitFileName}_{ViewName}` naming.")
    lines.append(u"")
    for name in file_labels:
        segments = _parse_segments(name, nm.get("delimiter", "-"), labels)
        out_folder, out_name = _resolve_output(name, u"<view>", config, export_folder)
        seg_note = (u", ".join(u"{0}={1}".format(k, v or "(missing)") for k, v in segments.items())
                    if segments else u"(none)")
        lines.append(u"- **{0}**".format(name))
        lines.append(u"  - folder: `{0}`".format(out_folder))
        lines.append(u"  - file name pattern: `{0}`".format(out_name))
        lines.append(u"  - segments: {0}".format(seg_note))
    output.print_md(u"\n".join(lines))
    return forms.alert(
        u"Preview printed to the output window above ({0:,} file(s)). Proceed with export?"
        .format(len(file_labels)), title="DeeNWCs - Preview", yes=True, no=True)


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

    docs = None
    acc_ctx = None
    if config["source"] == "local":
        docs = _pick_local_documents(uiapp)
        if not docs:
            return
        file_labels = [d.Title for d in docs]
    else:
        acc_ctx = _pick_acc_files(uiapp)
        if acc_ctx is None:
            return
        file_labels = acc_ctx["selected_names"]

    if not _show_preview_and_confirm(file_labels, config, export_folder):
        return

    best_effort_skipped = set()
    if config["source"] == "local":
        results = _run_local(uiapp, docs, config, export_folder, best_effort_skipped)
    else:
        results = _run_acc(uiapp, acc_ctx, config, export_folder, best_effort_skipped)

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
