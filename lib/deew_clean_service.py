# -*- coding: utf-8 -*-
"""
deew_clean_service
Batch-cleaning operations for DeeW.Clean, run against a plain,
programmatically-opened Document (no active UIDocument needed) - the
same headless pattern already used by deew_document_manager.py's
open/close pipeline for DeeW.Sharing and DeeW.Batch Save to Cloud.

Three of the four checks are adapted directly from DeeCleaner's own
proven, already-shipped interactive scans (HealthPack.panel/
DeeCleaner.pushbutton/script.py) so this batch tool deletes exactly
the same things DeeCleaner would, using the same detection rules:
  - Zero-Area Rooms: Room.Area <= 0 (Unplaced or Not Enclosed)
  - Unused Groups: GroupType with zero placed Group instances anywhere

In-Place Families are intentionally NEVER auto-deleted here, matching
DeeCleaner's own restrained behavior (it only lists + isolates them for
manual review, never deletes). An in-place family is real modeled
geometry, not an "unused" element by any Revit definition - deleting
it automatically and unattended across a batch would be a categorically
more dangerous action than purging genuinely unused types/rooms/groups.
This check is REPORT-ONLY.

Purge Unused Elements uses Document.GetUnusedElements(ISet<ElementId>),
a real Revit API method verified against revitapidocs.com before
writing this - but it only exists starting Revit 2024. On Revit
2021-2023 the method is absent from Document entirely; detected here
via hasattr() and skipped with a clear per-file note rather than
raising, so a mixed-Revit-version batch degrades gracefully file by
file instead of aborting. Runs iteratively (bounded to max_passes)
since deleting one round of unused elements can free up others
(matching how Revit's own "Purge Unused" command behaves), rather than
a single shallow pass.
"""
from Autodesk.Revit.DB import (
    FilteredElementCollector, BuiltInCategory, Group, GroupType, FamilyInstance,
    Transaction, ElementId, View, ViewSheet, Viewport,
)
from Autodesk.Revit.DB.Architecture import Room
from System.Collections.Generic import HashSet


class CleanResult(object):
    """Per-document outcome - plain counters so the batch UI/report
    never needs scan internals, only totals and a human-readable
    summary line."""

    def __init__(self):
        self.purged_count = 0
        self.purge_supported = True
        self.zero_area_rooms_deleted = 0
        self.unused_groups_deleted = 0
        self.inplace_families_found = 0
        self.sheets_deleted = 0
        self.views_deleted = 0
        self.unused_views_deleted = 0
        self.errors = []

    def summary_text(self):
        parts = []
        if self.purge_supported:
            parts.append("{0} unused element(s) purged".format(self.purged_count))
        else:
            parts.append("Purge skipped (needs Revit 2024+)")
        parts.append("{0} zero-area room(s) deleted".format(self.zero_area_rooms_deleted))
        parts.append("{0} unused group(s) deleted".format(self.unused_groups_deleted))
        if self.sheets_deleted:
            parts.append("{0} sheet(s) deleted".format(self.sheets_deleted))
        if self.views_deleted:
            parts.append("{0} view(s) deleted".format(self.views_deleted))
        if self.unused_views_deleted:
            parts.append("{0} unused view(s) deleted".format(self.unused_views_deleted))
        if self.inplace_families_found:
            parts.append("{0} in-place famil{1} found (not deleted - review manually)".format(
                self.inplace_families_found, "y" if self.inplace_families_found == 1 else "ies"))
        if self.errors:
            parts.append("{0} step error(s)".format(len(self.errors)))
        return "; ".join(parts)


def _is_in_place(instance):
    try:
        return bool(instance.Symbol.Family.IsInPlace)
    except Exception:
        return False


def purge_unused(doc, transaction_name="DeeW.Clean - Purge Unused Elements", max_passes=6):
    """Returns (total_deleted, supported: bool). supported=False means
    this Revit session's Document type has no GetUnusedElements method
    (pre-2024) - callers must not treat that as an error."""
    if not hasattr(doc, "GetUnusedElements"):
        return 0, False
    total_deleted = 0
    for _pass in range(max_passes):
        try:
            unused_ids = doc.GetUnusedElements(HashSet[ElementId]())
        except Exception:
            break
        if not unused_ids or unused_ids.Count == 0:
            break
        t = Transaction(doc, transaction_name)
        t.Start()
        try:
            doc.Delete(unused_ids)
            t.Commit()
            total_deleted += unused_ids.Count
        except Exception:
            t.RollBack()
            break
    return total_deleted, True


def delete_zero_area_rooms(doc, transaction_name="DeeW.Clean - Delete Zero-Area Rooms"):
    """Same detection rule as DeeCleaner's _scan_zero_area_rooms."""
    ids = []
    for r in FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_Rooms):
        if not isinstance(r, Room):
            continue
        try:
            if r.Area <= 0:
                ids.append(r.Id)
        except Exception:
            continue
    if not ids:
        return 0
    t = Transaction(doc, transaction_name)
    t.Start()
    deleted = 0
    for rid in ids:
        try:
            doc.Delete(rid)
            deleted += 1
        except Exception:
            continue
    t.Commit()
    return deleted


def delete_unused_groups(doc, transaction_name="DeeW.Clean - Delete Unused Groups"):
    """Same detection rule as DeeCleaner's _scan_unused_groups: a
    GroupType with zero placed Group instances anywhere in the
    project."""
    used_type_ids = set()
    try:
        for g in FilteredElementCollector(doc).OfClass(Group):
            try:
                used_type_ids.add(g.GetTypeId())
            except Exception:
                continue
    except Exception:
        pass

    ids = []
    for gt in FilteredElementCollector(doc).OfClass(GroupType):
        try:
            if gt.Id not in used_type_ids:
                ids.append(gt.Id)
        except Exception:
            continue
    if not ids:
        return 0
    t = Transaction(doc, transaction_name)
    t.Start()
    deleted = 0
    for gid in ids:
        try:
            doc.Delete(gid)
            deleted += 1
        except Exception:
            continue
    t.Commit()
    return deleted


# ---------------------------------------------------------------------------
# View / Sheet deletion
#
# These are categorically more destructive than the purge/rooms/groups
# checks above - those remove things Revit itself considers unused,
# whereas these throw away real drawing content someone authored. They
# exist for issuing stripped models out (an eTransmit-style workflow),
# and every one of them is OFF by default in both tools' UIs.
#
# Deliberate scoping decisions, so behaviour is predictable rather than
# surprising:
#   - View TEMPLATES are never deleted by any of these. A template is
#     not a drawing; deleting one silently changes how every view that
#     referenced it displays. Purge Unused already removes templates
#     nothing uses.
#   - Revit refuses to delete the last remaining view and the active
#     view, so a document always keeps at least one. That is Revit's
#     rule, not something this module works around - the per-element
#     try/except simply lets those refusals pass without aborting the
#     rest of the batch (this repo's standard convention).
#   - Deleting a sheet does NOT delete the views placed on it; it
#     removes the sheet and its viewports. Deleting a view that sits on
#     a sheet removes that viewport too. So the three options compose
#     predictably in any combination.
# ---------------------------------------------------------------------------
def _is_deletable_view(view):
    """A real, user-facing view - excludes sheets (handled separately),
    view templates (see module note), and the internal 'project browser'
    style views Revit exposes as View elements but will not delete."""
    try:
        if isinstance(view, ViewSheet):
            return False
        if view.IsTemplate:
            return False
    except Exception:
        return False
    return True


def _view_ids_placed_on_sheets(doc):
    """ViewIds that appear in any Viewport, i.e. are placed on a sheet.
    Uses Viewport rather than View.GetPlacementOnSheetStatus so the same
    code path works across the Revit versions this extension targets."""
    placed = set()
    try:
        for vp in FilteredElementCollector(doc).OfClass(Viewport):
            try:
                placed.add(vp.ViewId)
            except Exception:
                continue
    except Exception:
        pass
    return placed


def _delete_ids(doc, ids, transaction_name):
    if not ids:
        return 0
    t = Transaction(doc, transaction_name)
    t.Start()
    deleted = 0
    for eid in ids:
        try:
            doc.Delete(eid)
            deleted += 1
        except Exception:
            # Revit refuses the last/active view, views owned by an open
            # UI tab, etc. - skip and keep going.
            continue
    t.Commit()
    return deleted


def delete_all_sheets(doc, transaction_name="DeeW - Delete All Sheets"):
    ids = []
    for s in FilteredElementCollector(doc).OfClass(ViewSheet):
        try:
            if s.IsTemplate:
                continue
            ids.append(s.Id)
        except Exception:
            continue
    return _delete_ids(doc, ids, transaction_name)


def delete_all_views(doc, transaction_name="DeeW - Delete All Views"):
    """Every deletable non-sheet, non-template view."""
    ids = []
    for v in FilteredElementCollector(doc).OfClass(View):
        try:
            if _is_deletable_view(v):
                ids.append(v.Id)
        except Exception:
            continue
    return _delete_ids(doc, ids, transaction_name)


def delete_unused_views(doc, transaction_name="DeeW - Delete Unused Views"):
    """Views not placed on any sheet - the usual meaning of an "unused"
    view, and the one thing Purge Unused notably does NOT do."""
    placed = _view_ids_placed_on_sheets(doc)
    ids = []
    for v in FilteredElementCollector(doc).OfClass(View):
        try:
            if not _is_deletable_view(v):
                continue
            if v.Id in placed:
                continue
            ids.append(v.Id)
        except Exception:
            continue
    return _delete_ids(doc, ids, transaction_name)


def count_inplace_families(doc):
    """Report-only, per module docstring - never deleted here."""
    count = 0
    for fi in FilteredElementCollector(doc).OfClass(FamilyInstance):
        try:
            if _is_in_place(fi):
                count += 1
        except Exception:
            continue
    return count


def clean_document(doc, options):
    """options: dict with bool keys purge_unused / delete_zero_area_rooms /
    delete_unused_groups / flag_inplace_families. Each step runs
    independently and is wrapped so one failing step can never abort
    the others - matching this repo's "one bad file/step never aborts
    the batch" convention. Returns a CleanResult."""
    result = CleanResult()
    # Sheets first, then views: deleting a sheet removes its viewports
    # but keeps the views, so doing it in this order means the later
    # view passes see a clean, predictable state either way. "All views"
    # runs before "unused views" - if both are ticked the second is
    # simply a no-op rather than a conflict.
    if options.get("delete_all_sheets"):
        try:
            result.sheets_deleted = delete_all_sheets(doc)
        except Exception as e:
            result.errors.append("Delete All Sheets failed: {0}".format(e))
    if options.get("delete_all_views"):
        try:
            result.views_deleted = delete_all_views(doc)
        except Exception as e:
            result.errors.append("Delete All Views failed: {0}".format(e))
    if options.get("delete_unused_views"):
        try:
            result.unused_views_deleted = delete_unused_views(doc)
        except Exception as e:
            result.errors.append("Delete Unused Views failed: {0}".format(e))
    if options.get("purge_unused"):
        try:
            result.purged_count, result.purge_supported = purge_unused(doc)
        except Exception as e:
            result.errors.append("Purge Unused failed: {0}".format(e))
    if options.get("delete_zero_area_rooms"):
        try:
            result.zero_area_rooms_deleted = delete_zero_area_rooms(doc)
        except Exception as e:
            result.errors.append("Zero-Area Rooms failed: {0}".format(e))
    if options.get("delete_unused_groups"):
        try:
            result.unused_groups_deleted = delete_unused_groups(doc)
        except Exception as e:
            result.errors.append("Unused Groups failed: {0}".format(e))
    if options.get("flag_inplace_families"):
        try:
            result.inplace_families_found = count_inplace_families(doc)
        except Exception as e:
            result.errors.append("In-Place Family scan failed: {0}".format(e))
    return result
