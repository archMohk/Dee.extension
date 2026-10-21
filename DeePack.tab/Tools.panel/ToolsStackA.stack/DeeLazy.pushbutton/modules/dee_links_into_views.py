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
Live-reported bug fixed here: By Link Type undercounted (29 -> 21, not 7)
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


def _resolve_link_name(instance, link_type):
    name = _link_type_display_name(link_type) if link_type is not None else None
    if not name:
        name = _element_name(instance)
    return name or "(unnamed link)"


class LinkRow(object):
    """Represents either ONE link instance (By Instance mode) or EVERY
    instance of the same link TYPE placed in the view (By Link Type
    mode) - ids holds every ElementId this row stands for, so hiding
    "every other row" and copying link-display-overrides both work
    identically in either mode by just iterating ids."""

    def __init__(self, ids, name, file_path):
        self.ids = ids
        self.count = len(ids)
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


def build_rows(raw_instances, group_by_type):
    """Turns scan_raw_links()'s output into LinkRow objects - one per
    instance (group_by_type=False), or one per distinct linked FILE with
    every one of its instances' ids folded in (group_by_type=True), per
    _link_group_key()'s identity rather than the link Type element."""
    if not group_by_type:
        rows = [LinkRow([inst.Id], _resolve_link_name(inst, link_type),
                         _link_file_path(link_type) if link_type is not None else "")
                for inst, link_type in raw_instances]
    else:
        groups = {}
        order = []
        for inst, link_type in raw_instances:
            name = _resolve_link_name(inst, link_type)
            file_path = _link_file_path(link_type) if link_type is not None else ""
            key = _link_group_key(link_type, name, file_path)
            if key not in groups:
                groups[key] = {"ids": [], "name": name, "file_path": file_path}
                order.append(key)
            groups[key]["ids"].append(inst.Id)
        rows = [LinkRow(groups[key]["ids"], groups[key]["name"], groups[key]["file_path"]) for key in order]
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


def compute_preview(rows, view_name, template, prefix, suffix, existing_names):
    """Pure, read-only preview of the final view name + a validation note
    for each row, given the CURRENT naming pattern/prefix/suffix -
    mutates each row's preview_name/validation in place, no Revit calls.
    Mirrors build_view_base_name/_resolve_unique_name exactly, claiming
    names in the same sequential order create_isolated_view_for_link
    will use at run time, so a collision between two of THESE rows shows
    up here too, not just against already-existing views."""
    taken = set(existing_names)
    for row in rows:
        notes = []
        if row.name in ("(unnamed link)", "(unknown link)"):
            notes.append("Unnamed link")
        if row.safe_name != row.name:
            notes.append("Name sanitized")
        base_name = build_view_base_name(template, view_name, row.safe_name, prefix, suffix)
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


def create_isolated_view_for_link(doc, original_view, chosen_row, all_rows, taken_names,
                                   template, prefix, suffix):
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

    other_ids = [i for r in all_rows if r is not chosen_row for i in r.ids]
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
    warning = " ({0} link display setting(s) not copied)".format(override_failures) if override_failures else ""

    return ProcessResult(chosen_row.name, True, "Created '{0}'{1}".format(final_name, warning), final_name)


def run_batch(doc, original_view, all_rows, checked_rows, template, prefix, suffix):
    """One Transaction for the whole batch - per-row failures are caught
    inside create_isolated_view_for_link and recorded, never raised, so
    the transaction always commits and successful rows are never lost
    to one bad row (same house style as view_cropping.py's _run_action)."""
    taken_names = _all_taken_view_names(doc)
    results = []
    t = Transaction(doc, "DeeLazy - Links Into Views")
    t.Start()
    try:
        for row in checked_rows:
            try:
                result = create_isolated_view_for_link(
                    doc, original_view, row, all_rows, taken_names, template, prefix, suffix)
            except Exception as e:
                result = ProcessResult(row.name, False, str(e))
            results.append(result)
    finally:
        t.Commit()
    return results


# ==========================================================================
# Report
# ==========================================================================
def _report_html(results, elapsed_seconds):
    ok_count = sum(1 for r in results if r.ok)
    fail_count = len(results) - ok_count
    html = [
        '<h2 style="font-family:sans-serif;color:#ddd;">DeeLazy - Links Into Views</h2>',
        '<p style="color:#ddd;">{0} link(s) processed - {1} view(s) created, {2} skipped/failed - {3:.1f}s.</p>'.format(
            len(results), ok_count, fail_count, elapsed_seconds),
    ]
    for r in results:
        bg = "#2e7d32" if r.ok else "#c62828"
        icon = "&#10003;" if r.ok else "&#10007;"
        html.append(
            '<div style="padding:4px 10px;margin:2px 0;background:{0};color:#fff;'
            'border-radius:4px;font-family:monospace;font-size:12px;">'
            '{1}&nbsp; <b>{2}</b> &mdash; {3}</div>'.format(bg, icon, r.link_name, r.detail))
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
        self._view = None
        self._raw_instances = []
        self._link_rows = []
        self._results = []

        self._views = eligible_views(self.doc)
        labels = [view_label(v) for v in self._views]
        self.view_cb.ItemsSource = labels
        if labels:
            default_index = 0
            active = self.doc.ActiveView
            if active is not None:
                for i, v in enumerate(self._views):
                    if v.Id == active.Id:
                        default_index = i
                        break
            self.view_cb.SelectedIndex = default_index
            self._log("Pick a view, then click 'Scan for Links'.")
        else:
            self._log("No eligible views (Floor Plan/Ceiling Plan/Elevation/Section/3D/"
                       "Area Plan/Engineering Plan) found in this project.")

        self.pattern_cb.ItemsSource = pattern_labels()
        self.pattern_cb.SelectedIndex = 0

    # ---------------- logging/status ----------------
    def _log(self, message):
        self.status_tb.Text = message

    def _selected_view(self):
        idx = self.view_cb.SelectedIndex
        if idx is None or idx < 0 or idx >= len(self._views):
            return None
        return self._views[idx]

    # ---------------- naming preview ----------------
    def _refresh_preview(self):
        """Recomputes preview_name/validation for every scanned link row
        against the CURRENT pattern/prefix/suffix - pure string work, no
        Revit calls, safe to run on every keystroke. A no-op until a view
        has actually been scanned."""
        if self._view is None or not self._link_rows:
            return
        template = pattern_template(self.pattern_cb.SelectedIndex)
        prefix = self.prefix_tb.Text or ""
        suffix = self.suffix_tb.Text or ""
        existing_names = _all_taken_view_names(self.doc)
        compute_preview(self._link_rows, view_label(self._view), template, prefix, suffix, existing_names)
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
        self._link_rows = build_rows(self._raw_instances, self._group_by_type())
        self._results = []
        self.report_grid.ItemsSource = None
        self.links_grid.ItemsSource = None
        self.links_grid.ItemsSource = self._link_rows
        self._refresh_preview()

    def group_mode_changed(self, sender, args):
        """The RadioButton's XAML-declared IsChecked="True" fires this
        Checked event during window load, BEFORE __init__'s own body has
        run (a known hazard, already documented in dee_bulk_rename.py) -
        getattr(..., None) avoids an AttributeError on self._raw_instances
        in that case rather than relying on it already existing."""
        if not getattr(self, "_raw_instances", None):
            return
        self._rebuild_rows()
        mode = "link type" if self._group_by_type() else "instance"
        self._log("Grouped by {0} - {1} row(s).".format(mode, len(self._link_rows)))

    def scan_click(self, sender, args):
        view = self._selected_view()
        if view is None:
            forms.alert("Pick a view first.")
            return
        self._view = view
        self._raw_instances = scan_raw_links(self.doc, view)
        if not self._raw_instances:
            self._link_rows = []
            self.report_grid.ItemsSource = None
            self.links_grid.ItemsSource = None
            forms.alert("No Revit Links found in '{0}'.".format(view_label(view)))
            self._log("0 links found in '{0}'.".format(view_label(view)))
            return
        self._rebuild_rows()
        instance_count = len(self._raw_instances)
        self._log("{0} link instance(s) found in '{1}' ({2} row(s) shown). Tick the ones to isolate, "
                   "then click 'Create Isolated Views'.".format(
                       instance_count, view_label(view), len(self._link_rows)))

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
        if self._view is None:
            forms.alert("Scan a view first.")
            return
        checked = [r for r in self._link_rows if r.selected]
        if not checked:
            forms.alert("Tick at least one link first.")
            return
        if not forms.alert(
                "Create {0} isolated view(s) from '{1}'?".format(len(checked), view_label(self._view)),
                yes=True, no=True):
            return

        template = pattern_template(self.pattern_cb.SelectedIndex)
        prefix = self.prefix_tb.Text or ""
        suffix = self.suffix_tb.Text or ""

        start = time.time()
        results = run_batch(self.doc, self._view, self._link_rows, checked, template, prefix, suffix)
        elapsed = time.time() - start

        self._results = results
        self.report_grid.ItemsSource = None
        self.report_grid.ItemsSource = results
        _report_html(results, elapsed)
        ok_count = sum(1 for r in results if r.ok)
        self._log("{0} of {1} isolated view(s) created - {2:.1f}s. See the report.".format(
            ok_count, len(results), elapsed))

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
