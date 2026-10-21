# -*- coding: utf-8 -*-
"""
DeeLazy - Links Into Views module
Pick a view that contains Revit Links, scan it, tick the links to
process, then create one isolated duplicate view per ticked link: a
copy of the original view (same type/scale/crop/view-range/template -
whatever View.Duplicate() natively carries over) with every OTHER link
found in the scan hidden, so the copy ends up dedicated to that one
link. Host-model elements are never touched - only link visibility
changes. The new view's name is built from a chosen naming pattern
(link name alone, or combined with the original view's name) plus an
optional user prefix/suffix, sanitized against Revit's prohibited
name characters, and collision-safe suffixed if that name is taken.

Each created view is also set to Detail Level Fine with all annotation
categories hidden (both optional, on by default). A view template can then
be applied to the created views - an existing one picked from a list, or a
NEW one created from the first created view (so it carries Fine detail and
hidden annotations) with the Revit Links settings (V/G Overrides RVT Links)
excluded from what it controls, then applied to every created view. A
template that gets applied controls its own settings, so it can override
Fine / hidden annotations. Note View.Duplicate keeps the SOURCE view's own
template; if that template controls Detail Level or annotations, Revit
refuses those two defaults and the row says so.

Two grouping modes, switchable without re-scanning (live-reported need:
the same link FILE is often placed many times across a project - e.g.
one villa/unit type repeated across a masterplan - so "link count"
genuinely means two different things depending on the task):
- By Instance (default): one isolated view per PLACEMENT - if a file is
  linked 29 times, that is 29 views.
- By Link Type: one isolated view per distinct linked FILE - every
  placement of that file is kept visible together, only OTHER files'
  placements are hidden - so 29 placements across 7 distinct files
  becomes 7 views.

--------------------------------------------------------------------
Revit API facts relied on here (matched against real precedent already
proven elsewhere in this codebase, not guessed)
--------------------------------------------------------------------
- FilteredElementCollector(doc, view.Id).OfClass(RevitLinkInstance) -
  scopes the collector to links actually present in ONE view, same
  pattern as lib/dee_3d_export_service.py's collect_link_sources.
- RevitLinkType.Name alone is known-unreliable (frequently blank on a
  real project - documented in lib/dee_link_dist_service.py). The
  reliable name is the linked FILE's own name, via
  RevitLinkType.GetExternalFileReference() -> ExternalFileReference.
  GetPath() -> ModelPathUtils.ConvertModelPathToUserVisiblePath() ->
  os.path.basename(). This works whether or not the link is currently
  loaded - it does not require RevitLinkInstance.GetLinkDocument(), so
  an unloaded link is still fully processable here.
- View.Duplicate(ViewDuplicateOption.Duplicate) -> ElementId, then
  doc.GetElement(new_id) for the new View - precedent in
  DeeVSDupl.pushbutton/script.py and DeeView.pushbutton/script.py.
- View.HideElements(ICollection<ElementId>) - a PERMANENT per-element
  hide saved with the view (distinct from the temporary isolate/hide
  API family used elsewhere in this codebase) - sole existing
  precedent: DeeRehoster.pushbutton/script.py. Batched into one call
  per new view here (not per-element) since every "other link" id is
  known upfront and homogeneous.
- A link's "Design Option" (RevitLinkType.DesignOption / RevitLinkInstance
  .DesignOption) is a READ-ONLY, project-wide property of the link
  element ITSELF (confirmed by direct .NET reflection against the
  installed RevitAPI.dll - Revit 2026 - same verification method already
  used for RevitLinkGraphicsSettings in modules/dee_vtemp.py and
  lib/dee_mcp_tools.py). Since View.Duplicate() creates a new VIEW, not a
  new copy of the link element, the new view points at the exact same
  RevitLinkInstance - its Design Option membership is therefore already
  identical to the original ("father") view by construction; there is
  nothing to copy for this specific property.
- What IS real and per-VIEW (not per-element) is View.GetLinkOverrides
  (ElementId linkId) / View.SetLinkOverrides(ElementId, RevitLinkGraphics
  Settings) - confirmed, via the same reflection pass, to cover Phase,
  Phase Filter, Visibility Type (By Host View/By Linked View/Custom),
  Linked View, View Detail Level, Discipline, Nested Links, Object
  Styles, Color Fill, and View Range - NOT Design Option (that member
  does not exist on RevitLinkGraphicsSettings in this Revit version).
  Explicitly copied here, from the original view onto the new view, for
  the one link being kept - so every per-link display setting Revit
  actually exposes matches the father view, not just whatever
  View.Duplicate() happens to carry over on its own.

--------------------------------------------------------------------
NEEDS LIVE-REVIT VERIFICATION (flagged, not silently assumed correct)
--------------------------------------------------------------------
- Whether every supported ViewType (FloorPlan, CeilingPlan, Elevation,
  Section, ThreeD, AreaPlan, EngineeringPlan) behaves identically for
  Duplicate()+HideElements() in every Revit version - each step is
  wrapped in its own try/except and reported per-row rather than
  assumed, but not yet exercised live for every type.
- GetLinkOverrides/SetLinkOverrides member list was reflected directly
  off Revit 2026's installed RevitAPI.dll on this machine (not web-
  guessed), but the actual runtime COPY has not been exercised live yet.
- ModelPath.CloudPath/GetProjectGUID()/GetModelGUID() (used by
  _link_identity() for By Link Type grouping) were reflected the same
  way, off the same installed RevitAPI.dll - the member list is
  confirmed real, but grouping real ACC-hosted links by the resulting
  GUID pair has not yet been exercised live.

--------------------------------------------------------------------
Live-reported bug fixed here: prohibited characters in link names
--------------------------------------------------------------------
On a real project, RevitLinkType.GetExternalFileReference()/Name both
came back empty for every link in a view (cloud/ACC-hosted link
types), so every link fell back to RevitLinkInstance's own Name -
which Revit auto-generates as "LinkedFile.rvt : <copy number>" when a
link is placed more than once. The colon in that string is one of
Revit's prohibited element-name characters (\\:{}[]|;<>?`~), so
new_view.Name = that string raised "Name cannot include prohibited
characters" - and because View.Duplicate() had already succeeded
before the rename was attempted, the failed rows were left behind in
the project under Revit's own auto-generated "<Original View> Copy N"
name instead of being cleaned up. Fixed by (1) stripping the
" : <number>" copy suffix and any other prohibited character before
ever assigning a view name, and (2) deleting the duplicated view if
the rename still fails for any other reason, so a failed row never
leaves orphan "Copy N" views behind.

--------------------------------------------------------------------
Root cause of the 29-vs-7 count: NESTED ("layer 2") links
--------------------------------------------------------------------
The real explanation was not grouping at all. A diagnostic column plus
the Project Browser names of the views already generated showed that
the 29 RevitLinkInstance elements in the view were 7 top-level links
plus 22 NESTED links living inside those 7 (instance names are a chain:
"Top.rvt : Nested.rvt : 47 : location <Not Shared>"). Only top-level
links are listed now (RevitLinkType.IsNestedLink, confirmed by
reflection); each nested instance is matched to its root link by the
first segment of its instance name and rides along in that row's ids,
so isolating one link keeps ITS nested content and hides everyone
else's. The grouping work below (cloud GUID identity etc.) is still
used by By Link Type but was not the cause.

--------------------------------------------------------------------
Earlier attempts at the count: By Link Type grouping (29 -> 21 -> 28)
--------------------------------------------------------------------
Grouping by RevitLinkType.Id did not collapse every placement of the
same file into one row - on the real project, Revit's own Project
Browser confirmed only 7 distinct link files exist, but grouping by
Type.Id produced 21 rows. A follow-up fix that grouped by the resolved
file NAME (stripping the per-instance copy suffix) only reached 28,
barely better - because for these cloud/ACC-hosted links, the human-
readable path/name resolution chain (GetExternalFileReference ->
ConvertModelPathToUserVisiblePath) was ALSO failing, same root cause as
the earlier name-resolution bug. Fixed by _link_identity(): for a cloud
path, ModelPath.GetProjectGUID()/GetModelGUID() give the model's own
GUID identity straight from the path's serialized data - no network
call or string conversion involved - so it stays available exactly
when the human-readable path does not. That is now the PRIMARY grouping
key, with the file path and then the stripped name kept only as
fallbacks for non-cloud links or a reference that cannot be read at
all.
"""
import os
import re
import time

import clr
clr.AddReference("WindowsBase")
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")

from pyrevit import forms, script
import dee_branding
from Autodesk.Revit.DB import (
    FilteredElementCollector, View, ViewType, RevitLinkInstance, Element,
    Transaction, ElementId, ViewDuplicateOption, ModelPathUtils,
    ViewDetailLevel, ViewSchedule, BuiltInParameter,
)
from System.Collections.Generic import List

import utils

output = script.get_output()

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "DeeLinksIntoViews.xaml")

_ALLOWED_VIEW_TYPES = set([
    ViewType.FloorPlan, ViewType.CeilingPlan, ViewType.Elevation, ViewType.Section,
    ViewType.ThreeD, ViewType.AreaPlan, ViewType.EngineeringPlan,
])


# ==========================================================================
# View scanning - eligible views (real model-space views where links get
# placed and where Duplicate()+HideElements() make sense)
# ==========================================================================
def _is_eligible_view(view):
    try:
        if view is None or not isinstance(view, View):
            return False
        if view.IsTemplate:
            return False
        if view.ViewType not in _ALLOWED_VIEW_TYPES:
            return False
    except Exception:
        return False
    return True


def eligible_views(doc):
    views = [v for v in FilteredElementCollector(doc).OfClass(View) if _is_eligible_view(v)]
    views.sort(key=lambda v: (utils.read_name(v) or "").lower())
    return views


def view_label(view):
    return utils.read_name(view) or "(unnamed view)"


class ViewRow(object):
    """One tickable source view in the view picker."""

    def __init__(self, view):
        self.view = view
        self.selected = False
        self.name = view_label(view)
        try:
            self.view_type = str(view.ViewType)
        except Exception:
            self.view_type = ""


# ==========================================================================
# Link scanning (view-scoped) - local name-resolution copy, matching this
# codebase's own established precedent (dee_sheet_links_service.py
# duplicates the same helper rather than importing the much larger
# dee_link_dist_service.py for one pure function).
# ==========================================================================
def _link_model_path(link_type):
    """The ModelPath behind a link TYPE's external file reference, trying
    GetAbsolutePath() (the resolved path) before the raw GetPath() - for
    a cloud/ACC-hosted reference the absolute form has been observed to
    resolve more often. Returns None if no reference/path exists at all."""
    try:
        ref = link_type.GetExternalFileReference()
    except Exception:
        return None
    if ref is None:
        return None
    for getter_name in ("GetAbsolutePath", "GetPath"):
        try:
            model_path = getattr(ref, getter_name)()
            if model_path is not None:
                return model_path
        except Exception:
            continue
    return None


def _link_type_display_name(link_type):
    """File-reference based name for the link TYPE. Returns None (never
    the final sentinel) when not resolvable, so callers can still fall
    back further - this chain alone has been observed, on a real
    project, to fail for every link in a view (GetExternalFileReference
    and link_type.Name both coming back empty, e.g. for cloud/ACC-hosted
    link types), which is exactly why _element_name() below exists as a
    second, independent fallback rather than being treated as optional."""
    model_path = _link_model_path(link_type)
    if model_path is not None:
        try:
            visible_path = ModelPathUtils.ConvertModelPathToUserVisiblePath(model_path)
            if visible_path:
                return os.path.basename(visible_path)
        except Exception:
            pass
    try:
        if link_type.Name:
            return link_type.Name
    except Exception:
        pass
    return None


def _link_file_path(link_type):
    model_path = _link_model_path(link_type)
    if model_path is None:
        return ""
    try:
        visible_path = ModelPathUtils.ConvertModelPathToUserVisiblePath(model_path)
        if visible_path:
            return visible_path
    except Exception:
        pass
    return ""


def _link_identity(link_type):
    """A robust identity for "the same linked FILE", independent of how
    many separate RevitLinkType elements Revit may have created for it -
    live-reported: a cloud/ACC-hosted file linked multiple times ended up
    split across several RevitLinkType elements even though every one
    points at the exact same model, so grouping on Type.Id (and later,
    the human-readable path/name, which was ALSO failing to resolve for
    these links) both under-collapsed. ModelPath.GetProjectGUID()/
    GetModelGUID() are embedded directly in the path's own serialized
    data - no network call or user-visible-path conversion needed - so
    they stay available even when ConvertModelPathToUserVisiblePath
    comes back empty for a cloud path. Confirmed via .NET reflection
    against the installed RevitAPI.dll (Revit 2026): ModelPath exposes
    CloudPath (bool), GetProjectGUID() and GetModelGUID(). Returns None
    if no identity could be resolved at all (caller falls back further)."""
    model_path = _link_model_path(link_type)
    if model_path is None:
        return None
    try:
        if model_path.CloudPath:
            return ("cloud", str(model_path.GetProjectGUID()), str(model_path.GetModelGUID()))
    except Exception:
        pass
    try:
        visible_path = ModelPathUtils.ConvertModelPathToUserVisiblePath(model_path)
        if visible_path:
            return ("path", visible_path)
    except Exception:
        pass
    return None


def _element_name(element):
    """Element.Name sometimes fails to resolve on a RevitLinkInstance
    under IronPython's interop (a known ambiguity between the instance
    property and an interface member of the same name) -
    Element.Name.GetValue(element) is the documented workaround, already
    proven in lib/dee_3d_export_service.py's own element_name(). Used
    here as the fallback when the link TYPE's own name can't be
    resolved - Revit almost always auto-populates a link INSTANCE's name
    from its source file even when the type-level lookup comes up empty."""
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


_INSTANCE_COPY_SUFFIX_RE = re.compile(r"\s*:\s*\d+\s*$")
_PROHIBITED_NAME_CHARS_RE = re.compile(r"[\\:{}\[\]|;<>?`~]")
_COLLAPSE_DASHES_RE = re.compile(r"\s*-\s*-\s*")


def _sanitize_view_name(name):
    """Revit view/element names cannot contain \\:{}[]|;<>?`~ - a real,
    live-reported failure: a RevitLinkInstance's own Name often carries a
    Revit-generated " : <copy number>" suffix (e.g. "File.rvt : 24")
    whose colon alone is enough to make View.Name raise "Name cannot
    include prohibited characters." Strips that suffix first (cosmetic
    noise, not meaningful in a view name), then replaces any other
    prohibited character defensively, and finally collapses/trims stray
    separators left behind so the result still reads cleanly."""
    if not name:
        return name
    cleaned = _INSTANCE_COPY_SUFFIX_RE.sub("", name)
    cleaned = _PROHIBITED_NAME_CHARS_RE.sub("-", cleaned)
    cleaned = _COLLAPSE_DASHES_RE.sub(" - ", cleaned)
    cleaned = cleaned.strip(" -")
    return cleaned or "Link"


def _instance_root_name(instance):
    """First segment of a link INSTANCE's own name. Live data from a real
    project shows instance names are a path-like chain:
    "Top.rvt : Nested.rvt : 47 : location <Not Shared>" for a nested
    link and "Top.rvt : 21 : location <Not Shared>" for a top-level one -
    so the first " : " segment is always the ROOT (top-level) file."""
    name = _element_name(instance) or ""
    return name.split(" : ")[0].strip()


def _resolve_link_name(instance, link_type):
    name = _link_type_display_name(link_type) if link_type is not None else None
    if not name:
        name = _instance_root_name(instance)
    return name or "(unnamed link)"


def _is_nested_link(instance, link_type):
    """True for a "layer 2" link - a link that lives INSIDE another linked
    model. RevitLinkType.IsNestedLink is the real property (confirmed via
    .NET reflection against the installed RevitAPI.dll); when the type is
    unavailable, falls back to the instance-name chain containing more
    than one .rvt segment (see _instance_root_name)."""
    try:
        if link_type is not None:
            return bool(link_type.IsNestedLink)
    except Exception:
        pass
    name = _element_name(instance) or ""
    return len([s for s in name.split(" : ") if s.strip().lower().endswith(".rvt")]) > 1


class LinkRow(object):
    """Represents either ONE link instance (By Instance mode) or EVERY
    instance of the same link TYPE placed in the view (By Link Type
    mode) - ids holds every ElementId this row stands for, so hiding
    "every other row" and copying link-display-overrides both work
    identically in either mode by just iterating ids."""

    def __init__(self, ids, name, file_path, count=None, view=None):
        self.view = view
        self.view_name = view_label(view) if view is not None else ""
        self.ids = ids
        self.count = count if count is not None else len(ids)
        self.selected = False
        self.file_path = file_path or ""
        self.name = name
        self.safe_name = _sanitize_view_name(self.name)
        self.preview_name = self.safe_name
        self.validation = ""


def scan_raw_links(doc, view):
    """Links actually placed in THIS view, not the whole document - top
    level only, no nested-link recursion (only links directly visible
    in the picked view matter here). Returns [(RevitLinkInstance,
    RevitLinkType_or_None), ...] so the caller can group them (by
    instance or by type) without re-querying Revit."""
    raw = []
    try:
        collector = FilteredElementCollector(doc, view.Id).OfClass(RevitLinkInstance)
    except Exception:
        return raw
    for inst in collector:
        try:
            link_type = doc.GetElement(inst.GetTypeId())
        except Exception:
            link_type = None
        raw.append((inst, link_type))
    return raw


def _link_group_key(link_type, name, file_path):
    """Identifies "the same linked file" for grouping purposes. NOT the
    link TYPE's ElementId - live-reported: Revit can create a SEPARATE
    RevitLinkType per link operation even when every one of them points
    at the exact same file (e.g. the same villa/unit linked in 29 times
    across a masterplan), so grouping by Type.Id under-collapsed.
    Prefers _link_identity()'s cloud-GUID/path identity (robust even when
    the human-readable path can't be resolved); when THAT also fails
    (no external file reference at all), falls back to the resolved file
    path string, then to the name with its per-instance copy-number
    suffix stripped, so every placement still collapses to one group."""
    identity = _link_identity(link_type) if link_type is not None else None
    if identity is not None:
        return identity
    if file_path:
        return ("path", file_path)
    return ("name", _INSTANCE_COPY_SUFFIX_RE.sub("", name).strip() or name)


def count_nested(raw_instances):
    return sum(1 for inst, link_type in raw_instances if _is_nested_link(inst, link_type))


def build_rows(raw_instances, group_by_type, view=None):
    """Turns ONE view's scan_raw_links() output into LinkRow objects, each
    remembering its source view (several views can be scanned at once;
    the caller concatenates the per-view results).

    Only TOP-LEVEL links get a row - nested ("layer 2") links, i.e. links
    living inside another linked model, are not listed (live-reported:
    a view showed 29 link instances where Revit's own Project Browser and
    Properties palette count only 7 - the other 22 were nested links
    inside those 7). Each nested instance is still folded into its ROOT
    link's row ids (matched by the first segment of its instance name,
    see _instance_root_name), so hiding "every other row" also hides a
    different link's nested content, while the chosen link keeps its own.

    One row per top-level instance (group_by_type=False), or one per
    distinct linked FILE with all its top-level instances folded in
    (group_by_type=True), per _link_group_key()'s identity rather than
    the link Type element. count is the number of TOP-LEVEL instances a
    row stands for (nested ids ride along in ids but are not counted)."""
    nested_ids_by_root = {}
    top_level = []
    for inst, link_type in raw_instances:
        if _is_nested_link(inst, link_type):
            nested_ids_by_root.setdefault(_instance_root_name(inst).lower(), []).append(inst.Id)
        else:
            top_level.append((inst, link_type))

    def _nested_for(inst):
        return nested_ids_by_root.get(_instance_root_name(inst).lower(), [])

    if not group_by_type:
        rows = []
        for inst, link_type in top_level:
            ids = [inst.Id] + list(_nested_for(inst))
            rows.append(LinkRow(ids, _resolve_link_name(inst, link_type),
                                 _link_file_path(link_type) if link_type is not None else "",
                                 count=1, view=view))
    else:
        groups = {}
        order = []
        for inst, link_type in top_level:
            name = _resolve_link_name(inst, link_type)
            file_path = _link_file_path(link_type) if link_type is not None else ""
            try:
                key = _link_group_key(link_type, name, file_path)
            except Exception:
                key = ("error", utils.id_value(inst.Id))
            if key not in groups:
                groups[key] = {"ids": [], "count": 0, "name": name, "file_path": file_path, "roots": set()}
                order.append(key)
            group = groups[key]
            group["ids"].append(inst.Id)
            group["count"] += 1
            root = _instance_root_name(inst).lower()
            if root not in group["roots"]:
                group["roots"].add(root)
                group["ids"].extend(_nested_for(inst))
        rows = [LinkRow(groups[key]["ids"], groups[key]["name"], groups[key]["file_path"],
                         count=groups[key]["count"], view=view) for key in order]
    rows.sort(key=lambda r: r.name.lower())
    return rows


# ==========================================================================
# View duplication + collision-safe naming
# ==========================================================================
def _all_taken_view_names(doc):
    names = set()
    for v in FilteredElementCollector(doc).OfClass(View):
        try:
            n = utils.read_name(v)
            if n:
                names.add(n)
        except Exception:
            continue
    return names


def _resolve_unique_name(base_name, taken_names):
    if base_name not in taken_names:
        return base_name
    n = 2
    while True:
        candidate = "{0} ({1})".format(base_name, n)
        if candidate not in taken_names:
            return candidate
        n += 1


# ==========================================================================
# Naming presets - pattern + optional prefix/suffix, all sanitized
# together into a final Revit-safe view name.
# ==========================================================================
NAME_PATTERNS = [
    ("Link name only", u"{link}"),
    ("Original View - Link", u"{view} - {link}"),
    ("Link - Original View", u"{link} - {view}"),
    ("Original View_Link", u"{view}_{link}"),
    ("Link_Original View", u"{link}_{view}"),
]


def pattern_labels():
    return [label for label, _ in NAME_PATTERNS]


def pattern_template(index):
    if index is None or index < 0 or index >= len(NAME_PATTERNS):
        return NAME_PATTERNS[0][1]
    return NAME_PATTERNS[index][1]


def build_view_base_name(template, view_name, link_safe_name, prefix, suffix):
    try:
        body = template.format(view=view_name, link=link_safe_name)
    except Exception:
        body = link_safe_name
    base = u"{0}{1}{2}".format(prefix or "", body, suffix or "")
    return _sanitize_view_name(base)


def compute_preview(rows, template, prefix, suffix, existing_names):
    """Pure, read-only preview of the final view name + a validation note
    for each row, given the CURRENT naming pattern/prefix/suffix -
    mutates each row's preview_name/validation in place, no Revit calls.
    Each row uses its OWN source view's name for the {view} placeholder
    (rows can come from several scanned views). Mirrors
    build_view_base_name/_resolve_unique_name exactly, claiming names in
    the same sequential order run_batch will use, so a collision between
    two of THESE rows shows up here too (e.g. "Link name only" for the
    same link in two source views), not just against existing views."""
    taken = set(existing_names)
    for row in rows:
        notes = []
        if row.name in ("(unnamed link)", "(unknown link)"):
            notes.append("Unnamed link")
        if row.safe_name != row.name:
            notes.append("Name sanitized")
        base_name = build_view_base_name(template, row.view_name, row.safe_name, prefix, suffix)
        final_name = _resolve_unique_name(base_name, taken)
        if final_name != base_name:
            notes.append("Duplicate - will suffix")
        taken.add(final_name)
        row.preview_name = final_name
        row.validation = "; ".join(notes) if notes else "OK"


class ProcessResult(object):
    def __init__(self, link_name, ok, detail, new_view_name=""):
        self.link_name = link_name
        self.ok = ok
        self.result = "OK" if ok else "Skipped/Failed"
        self.detail = detail
        self.new_view_name = new_view_name
        self.source_view = ""
        self.new_view = None
        self.is_summary = False


TEMPLATE_NONE = "none"
TEMPLATE_APPLY = "apply"
TEMPLATE_CREATE = "create"


class RunOptions(object):
    """What to do to each created view beyond isolating its link."""

    def __init__(self, fine=True, hide_annotations=True, template_mode=TEMPLATE_NONE,
                 template_id=None, new_template_name=""):
        self.fine = fine
        self.hide_annotations = hide_annotations
        self.template_mode = template_mode
        self.template_id = template_id
        self.new_template_name = new_template_name


def list_view_templates(doc):
    """[(name, ElementId)] of every view template that can be applied to a
    model view (schedule templates are left out), name-sorted."""
    out = []
    for v in FilteredElementCollector(doc).OfClass(View):
        try:
            if not v.IsTemplate or isinstance(v, ViewSchedule):
                continue
            out.append((utils.read_name(v) or "(unnamed template)", v.Id))
        except Exception:
            continue
    out.sort(key=lambda t: t[0].lower())
    return out


def _has_template(view):
    try:
        return utils.id_value(view.ViewTemplateId) > 0
    except Exception:
        return False


def _apply_view_defaults(view, options):
    """Detail Level Fine and all annotation categories hidden. These are
    controlled by a view template when one is applied (and View.Duplicate
    keeps the SOURCE view's template), in which case Revit refuses the
    change - reported as a note instead of failing the view."""
    notes = []
    if options.fine:
        try:
            view.DetailLevel = ViewDetailLevel.Fine
        except Exception as e:
            notes.append("detail level not set ({0})".format(
                "controlled by the view template it inherited" if _has_template(view) else e))
    if options.hide_annotations:
        try:
            view.AreAnnotationCategoriesHidden = True
        except Exception as e:
            notes.append("annotations not hidden ({0})".format(
                "controlled by the view template it inherited" if _has_template(view) else e))
    return notes


def _exclude_links_from_template(template):
    """Stops the template controlling the Revit Links (V/G Overrides RVT
    Links) settings, so applying it never overwrites a view's per-link
    display settings. Returns a short text for the report."""
    param_id = ElementId(BuiltInParameter.VIS_GRAPHICS_RVT_LINKS)
    key = utils.id_value(param_id)
    try:
        if not any(utils.id_value(i) == key for i in template.GetTemplateParameterIds()):
            return "Revit Links setting does not apply to this template type"
    except Exception:
        pass
    non_controlled = list(template.GetNonControlledTemplateParameterIds())
    if not any(utils.id_value(i) == key for i in non_controlled):
        non_controlled.append(param_id)
        template.SetNonControlledTemplateParameterIds(List[ElementId](non_controlled))
    return "Revit Links settings excluded from the template"


def _run_template_step(doc, results, options, taken_names):
    """After every view exists: apply an existing template, or create a new
    one FROM THE FIRST created view (so it carries Fine detail and hidden
    annotations), exclude the Revit Links settings from it, and apply it to
    every created view. A template problem is appended to that row's detail;
    it never turns a successfully created view into a failure."""
    if options.template_mode == TEMPLATE_NONE:
        return
    created = [r for r in results if r.ok and r.new_view is not None]
    if not created:
        return

    template = None
    if options.template_mode == TEMPLATE_APPLY:
        template = doc.GetElement(options.template_id) if options.template_id is not None else None
        if template is None:
            for r in created:
                r.detail += " | template not found"
            return
    else:
        source = created[0].new_view
        try:
            if not source.IsViewValidForTemplateCreation():
                raise Exception("this view type cannot be used to create a template")
            template = source.CreateViewTemplate()
            wanted = _sanitize_view_name(options.new_template_name) or "Links Into Views"
            name = _resolve_unique_name(wanted, taken_names)
            template.Name = name
            taken_names.add(name)
            links_note = _exclude_links_from_template(template)
            summary = ProcessResult("(view template)", True,
                                    "Created view template '{0}' from '{1}' - {2}".format(
                                        name, created[0].new_view_name, links_note), name)
            summary.source_view = created[0].source_view
            summary.is_summary = True
            results.append(summary)
        except Exception as e:
            for r in created:
                r.detail += " | new view template FAILED: {0}".format(e)
            return

    template_name = utils.read_name(template) or "template"
    for r in created:
        try:
            if not r.new_view.IsValidViewTemplate(template.Id):
                r.detail += " | template '{0}' is not valid for this view type".format(template_name)
                continue
            r.new_view.ViewTemplateId = template.Id
            r.detail += " | template '{0}' applied".format(template_name)
        except Exception as e:
            r.detail += " | template not applied ({0})".format(e)


def create_isolated_view_for_link(doc, original_view, chosen_row, all_rows, taken_names,
                                   template, prefix, suffix, options):
    """One link -> one new view. Mutates taken_names (claims the name it
    just used) so later rows in the same batch don't collide with each
    other, not just with pre-existing views. Never raises - any failure
    is returned as a failed ProcessResult so the batch loop can continue."""
    try:
        new_id = original_view.Duplicate(ViewDuplicateOption.Duplicate)
        new_view = doc.GetElement(new_id)
    except Exception as e:
        return ProcessResult(chosen_row.name, False, "Could not duplicate view: {0}".format(e))

    base_name = build_view_base_name(template, view_label(original_view), chosen_row.safe_name, prefix, suffix)
    final_name = _resolve_unique_name(base_name, taken_names)
    try:
        new_view.Name = final_name
    except Exception as e:
        try:
            doc.Delete(new_view.Id)
        except Exception:
            pass
        return ProcessResult(chosen_row.name, False, "Could not rename (duplicate removed): {0}".format(e))
    taken_names.add(final_name)

    chosen_keys = set(utils.id_value(i) for i in chosen_row.ids)
    other_ids = []
    seen = set()
    for r in all_rows:
        if r is chosen_row:
            continue
        for i in r.ids:
            key = utils.id_value(i)
            if key in chosen_keys or key in seen:
                continue
            seen.add(key)
            other_ids.append(i)
    if other_ids:
        try:
            new_view.HideElements(List[ElementId](other_ids))
        except Exception as e:
            return ProcessResult(
                chosen_row.name, False,
                "Created '{0}' but could not hide other links: {1}".format(final_name, e), final_name)

    override_failures = 0
    for link_id in chosen_row.ids:
        try:
            overrides = original_view.GetLinkOverrides(link_id)
            if overrides is not None:
                new_view.SetLinkOverrides(link_id, overrides)
        except Exception:
            override_failures += 1
    notes = _apply_view_defaults(new_view, options)
    if override_failures:
        notes.append("{0} link display setting(s) not copied".format(override_failures))
    warning = " ({0})".format("; ".join(notes)) if notes else ""

    result = ProcessResult(chosen_row.name, True, "Created '{0}'{1}".format(final_name, warning), final_name)
    result.new_view = new_view
    return result


def run_batch(doc, all_rows, checked_rows, template, prefix, suffix, options):
    """One Transaction for the whole batch - per-row failures are caught
    inside create_isolated_view_for_link and recorded, never raised, so
    the transaction always commits and successful rows are never lost
    to one bad row (same house style as view_cropping.py's _run_action).

    all_rows spans EVERY scanned source view; each checked row is
    isolated against the rows of its OWN source view only - "every other
    link" means every other link in that same view, never links that
    only exist in a different one."""
    rows_by_view = {}
    for r in all_rows:
        rows_by_view.setdefault(utils.id_value(r.view.Id), []).append(r)
    taken_names = _all_taken_view_names(doc)
    results = []
    t = Transaction(doc, "DeeLazy - Links Into Views")
    t.Start()
    try:
        for row in checked_rows:
            try:
                result = create_isolated_view_for_link(
                    doc, row.view, row, rows_by_view.get(utils.id_value(row.view.Id), [row]),
                    taken_names, template, prefix, suffix, options)
            except Exception as e:
                result = ProcessResult(row.name, False, str(e))
            result.source_view = row.view_name
            results.append(result)
        try:
            _run_template_step(doc, results, options, taken_names)
        except Exception as e:
            results.append(ProcessResult("(view template)", False, "Template step failed: {0}".format(e)))
    finally:
        t.Commit()
    return results


# ==========================================================================
# Report
# ==========================================================================
def _report_html(results, elapsed_seconds):
    view_results = [r for r in results if not r.is_summary]
    ok_count = sum(1 for r in view_results if r.ok)
    fail_count = len(view_results) - ok_count
    html = [
        '<h2 style="font-family:sans-serif;color:#ddd;">DeeLazy - Links Into Views</h2>',
        '<p style="color:#ddd;">{0} link(s) processed - {1} view(s) created, {2} skipped/failed - {3:.1f}s.</p>'.format(
            len(view_results), ok_count, fail_count, elapsed_seconds),
    ]
    for r in results:
        bg = "#2e7d32" if r.ok else "#c62828"
        icon = "&#10003;" if r.ok else "&#10007;"
        html.append(
            '<div style="padding:4px 10px;margin:2px 0;background:{0};color:#fff;'
            'border-radius:4px;font-family:monospace;font-size:12px;">'
            '{1}&nbsp; <b>{2}</b> (from {3}) &mdash; {4}</div>'.format(
                bg, icon, r.link_name, r.source_view, r.detail))
    output.print_html("".join(html))


# ==========================================================================
# Window - UI wiring only; all real work happens in the plain functions
# above (same separation as view_cropping.py)
# ==========================================================================
class DeeLinksIntoViewsWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, uiapp):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.uiapp = uiapp
        self.uidoc = uiapp.ActiveUIDocument
        self.doc = self.uidoc.Document
        self._scans = []
        self._link_rows = []
        self._results = []

        self._view_rows = [ViewRow(v) for v in eligible_views(self.doc)]
        active = self.doc.ActiveView
        if active is not None:
            for vr in self._view_rows:
                if vr.view.Id == active.Id:
                    vr.selected = True
                    break
        self.views_grid.ItemsSource = self._view_rows
        if self._view_rows:
            self._log("Tick one or more views, then click 'Scan Selected Views'.")
        else:
            self._log("No eligible views (Floor Plan/Ceiling Plan/Elevation/Section/3D/"
                       "Area Plan/Engineering Plan) found in this project.")

        self.pattern_cb.ItemsSource = pattern_labels()
        self.pattern_cb.SelectedIndex = 0

        self._templates = list_view_templates(self.doc)
        self.template_cb.ItemsSource = [name for name, _id in self._templates]
        if self._templates:
            self.template_cb.SelectedIndex = 0
        self.template_name_tb.Text = "Exported View Template"
        # Set in code (not XAML): the radios have a Checked handler, and an
        # XAML-declared IsChecked would fire it before this window is ready.
        self.template_none_rb.IsChecked = True
        self._template_ui_ready = True
        self._update_template_enables()

    def _update_template_enables(self):
        self.template_apply_rb.IsEnabled = bool(self._templates)
        self.template_cb.IsEnabled = bool(self.template_apply_rb.IsChecked) and bool(self._templates)
        self.template_name_tb.IsEnabled = bool(self.template_create_rb.IsChecked)

    def template_mode_changed(self, sender, args):
        if not getattr(self, "_template_ui_ready", False):
            return
        self._update_template_enables()

    # ---------------- logging/status ----------------
    def _log(self, message):
        self.status_tb.Text = message

    # ---------------- view picker ----------------
    def _visible_view_rows(self):
        text = ((self.view_search_tb.Text or "") if hasattr(self, "view_search_tb") else "").strip().lower()
        if not text:
            return self._view_rows
        return [r for r in self._view_rows if text in r.name.lower()]

    def view_search_changed(self, sender, args):
        if not getattr(self, "_view_rows", None):
            return
        self.views_grid.ItemsSource = None
        self.views_grid.ItemsSource = self._visible_view_rows()

    def view_select_all_click(self, sender, args):
        for r in self._visible_view_rows():
            r.selected = True
        self.views_grid.Items.Refresh()

    def view_select_none_click(self, sender, args):
        for r in self._visible_view_rows():
            r.selected = False
        self.views_grid.Items.Refresh()

    # ---------------- naming preview ----------------
    def _refresh_preview(self):
        """Recomputes preview_name/validation for every scanned link row
        against the CURRENT pattern/prefix/suffix - pure string work, no
        Revit calls, safe to run on every keystroke. A no-op until views
        have actually been scanned."""
        if not self._link_rows:
            return
        template = pattern_template(self.pattern_cb.SelectedIndex)
        prefix = self.prefix_tb.Text or ""
        suffix = self.suffix_tb.Text or ""
        existing_names = _all_taken_view_names(self.doc)
        compute_preview(self._link_rows, template, prefix, suffix, existing_names)
        self.links_grid.Items.Refresh()

    def pattern_changed(self, sender, args):
        self._refresh_preview()

    def prefix_changed(self, sender, args):
        self._refresh_preview()

    def suffix_changed(self, sender, args):
        self._refresh_preview()

    # ---------------- scan ----------------
    def _group_by_type(self):
        try:
            return bool(self.group_by_type_rb.IsChecked)
        except Exception:
            return False

    def _rebuild_rows(self):
        """Regroups the already-scanned instances (no Revit re-query) per
        the current By Instance / By Link Type toggle - lets the toggle
        switch instantly without a fresh scan."""
        group = self._group_by_type()
        rows = []
        for view, raw in self._scans:
            rows.extend(build_rows(raw, group, view))
        self._link_rows = rows
        self._results = []
        self.report_grid.ItemsSource = None
        self.links_grid.ItemsSource = None
        self.links_grid.ItemsSource = self._link_rows
        self._refresh_preview()

    def group_mode_changed(self, sender, args):
        """The RadioButton's XAML-declared IsChecked="True" fires this
        Checked event during window load, BEFORE __init__'s own body has
        run (a known hazard, already documented in dee_bulk_rename.py) -
        getattr(..., None) avoids an AttributeError on self._scans in that
        case rather than relying on it already existing."""
        if not getattr(self, "_scans", None):
            return
        self._rebuild_rows()
        mode = "link type" if self._group_by_type() else "instance"
        self._log("Grouped by {0} - {1} row(s).".format(mode, len(self._link_rows)))

    def scan_click(self, sender, args):
        ticked = [r for r in self._view_rows if r.selected]
        if not ticked:
            forms.alert("Tick at least one view first.")
            return
        self._scans = [(r.view, scan_raw_links(self.doc, r.view)) for r in ticked]
        if not any(raw for _, raw in self._scans):
            self._scans = []
            self._link_rows = []
            self.report_grid.ItemsSource = None
            self.links_grid.ItemsSource = None
            forms.alert("No Revit Links found in the {0} selected view(s).".format(len(ticked)))
            self._log("0 links found in {0} view(s).".format(len(ticked)))
            return
        self._rebuild_rows()
        nested_count = sum(count_nested(raw) for _, raw in self._scans)
        views_with_links = sum(1 for _, raw in self._scans if raw)
        self._log("{0} link row(s) found across {1} of {2} scanned view(s) ({3} nested link instance(s) "
                   "kept with their parent, not listed). Tick the ones to isolate, then click "
                   "'Create Isolated Views'.".format(
                       len(self._link_rows), views_with_links, len(ticked), nested_count))

    def select_all_click(self, sender, args):
        for r in self._link_rows:
            r.selected = True
        self.links_grid.Items.Refresh()

    def select_none_click(self, sender, args):
        for r in self._link_rows:
            r.selected = False
        self.links_grid.Items.Refresh()

    # ---------------- run ----------------
    def run_click(self, sender, args):
        if not self._link_rows:
            forms.alert("Scan one or more views first.")
            return
        checked = [r for r in self._link_rows if r.selected]
        if not checked:
            forms.alert("Tick at least one link first.")
            return
        template_mode = TEMPLATE_NONE
        template_id = None
        new_template_name = ""
        if bool(self.template_apply_rb.IsChecked):
            idx = self.template_cb.SelectedIndex
            if idx < 0 or idx >= len(self._templates):
                forms.alert("Pick the view template to apply, or choose 'None'.")
                return
            template_mode = TEMPLATE_APPLY
            template_id = self._templates[idx][1]
        elif bool(self.template_create_rb.IsChecked):
            new_template_name = (self.template_name_tb.Text or "").strip()
            if not new_template_name:
                forms.alert("Enter a name for the new view template, or choose 'None'.")
                return
            template_mode = TEMPLATE_CREATE
        options = RunOptions(
            fine=bool(self.fine_cb.IsChecked),
            hide_annotations=bool(self.annot_cb.IsChecked),
            template_mode=template_mode, template_id=template_id,
            new_template_name=new_template_name)

        source_count = len(set(utils.id_value(r.view.Id) for r in checked))
        if not forms.alert(
                "Create {0} isolated view(s) from {1} source view(s)?".format(len(checked), source_count),
                yes=True, no=True):
            return

        template = pattern_template(self.pattern_cb.SelectedIndex)
        prefix = self.prefix_tb.Text or ""
        suffix = self.suffix_tb.Text or ""

        start = time.time()
        results = run_batch(self.doc, self._link_rows, checked, template, prefix, suffix, options)
        elapsed = time.time() - start

        self._results = results
        self.report_grid.ItemsSource = None
        self.report_grid.ItemsSource = results
        _report_html(results, elapsed)
        view_results = [r for r in results if not r.is_summary]
        ok_count = sum(1 for r in view_results if r.ok)
        self._log("{0} of {1} isolated view(s) created - {2:.1f}s. See the report.".format(
            ok_count, len(view_results), elapsed))

    def close_click(self, sender, args):
        self.Close()


# ==========================================================================
# Launch entry point (called by the DeeLazy home window)
# ==========================================================================
def launch(uiapp):
    if uiapp.ActiveUIDocument is None:
        forms.alert("Open a Revit project first.")
        return
    window = DeeLinksIntoViewsWindow(_XAML_FILE, uiapp)
    window.ShowDialog()


TOOL_INFO = {
    "id": "links_into_views",
    "title": "Links Into Views",
    "description": "Pick a view with Revit Links, then create one isolated duplicate view per selected link (same view properties, only that link visible).",
    "icon": u"\U0001F517",  # link emoji
    "launch": launch,
}
