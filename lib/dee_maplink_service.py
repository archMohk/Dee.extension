# -*- coding: utf-8 -*-
"""
dee_maplink_service
Pure-and-Revit-light logic behind DeeMAPLink, kept out of script.py (unlike
DeeSuperLINK's all-inline style) specifically so the matching/grouping/
estimate math can run under a standalone test - see scratchpad/test_dee_maplink.py.

DeeMAPLink generalizes DeeSuperLINK (links ACC cloud models into each other
with no file open) two ways: the source can be an ACC project OR a local
folder, and instead of a fixed ONE<->MANY relationship the user builds an
arbitrary many-to-many match set between two lists of the same scanned
files, then every host in that set is opened headless, linked to, and
synchronized back.

Reused rather than reinvented (all already proven elsewhere in this
extension - see each function's docstring for exactly which one):
  - lib/acc_file_browser.py       - ACC hub/project/file browsing, cloud
                                     ModelPath resolution, headless attached
                                     cloud-document opening.
  - lib/deew_model_scanner.py     - closed-file worksharing classification
                                     for the Local branch (BasicFileInfo,
                                     never opens the file).
  - lib/deew_document_manager.py  - headless attached LOCAL document
                                     opening, SynchronizeWithCentral, close.
  - lib/deew_failure_handler.py   - native-dialog auto-resolution +
                                     transaction failure preprocessing -
                                     the direct answer to "handle all the
                                     messages."
  - lib/deew_logger.py            - structured log this module's own
                                     issues_from_log() reads back from.

--------------------------------------------------------------------
NEEDS LIVE-REVIT VERIFICATION (flagged, not assumed - matching
DeeSuperLINK's own documented practice for the same category of risk)
--------------------------------------------------------------------
- RevitLinkType.Create accepting a LOCAL ModelPath (from
  ModelPathUtils.ConvertUserVisiblePathToModelPath) called against a
  headlessly-opened host document. DeeSuperLINK proves this works for a
  CLOUD ModelPath; a local one has not been exercised the same way.
- deew_document_manager.open_document_no_detach / synchronize_with_central
  against a real LOCAL central-model file - the only other consumer of
  this exact pair is DeeW.Clean, itself still unverified live per its own
  module docstring.
- The EST_OPEN_SECONDS / EST_LINK_SECONDS / EST_SYNC_SECONDS constants
  below are a rough guess, not a measurement - there is no historical
  timing data anywhere in this codebase to build a real estimator from.
  Documented as "rough" everywhere they reach the UI; never presented as
  a precise figure.
- A model cannot link itself (filtered out in build_matches), but a
  circular A->B->A link is not detected - same documented gap as
  DeeSuperLINK.
"""
import os

try:
    from collections import OrderedDict
except ImportError:  # pragma: no cover - always available in practice
    OrderedDict = dict

from Autodesk.Revit.DB import (
    RevitLinkType, ImportPlacement, AttachmentType, Transaction, FilteredElementCollector,
)

import deew_model_scanner as scanner
import deew_failure_handler as ffh
import dee_link_create_service as lcs


# ==========================================================================
# placement ("Link Type" in this tool's own UI wording, per the user's
# explicit clarification that they mean positioning, not Overlay/Attachment)
# ==========================================================================
PLACEMENT_OPTIONS = [
    ("Internal Origin to Internal Origin", ImportPlacement.Origin),
    ("Project Base Point to Project Base Point", ImportPlacement.Site),
    ("By Shared Coordinates", ImportPlacement.Shared),
    ("Center to Center", ImportPlacement.Centered),
]


def placement_label(placement):
    for label, value in PLACEMENT_OPTIONS:
        if value == placement:
            return label
    return str(placement)


# ==========================================================================
# search / formatting - ported from DeeSuperLINK verbatim
# ==========================================================================
def matches_search(name, query):
    """Case-insensitive AND-of-terms match - every space-separated word
    must appear somewhere in the name, in any order."""
    if not query:
        return True
    haystack = (name or "").lower()
    return all(term in haystack for term in query.lower().split())


def format_duration(seconds):
    seconds = int(max(0, seconds))
    if seconds < 60:
        return "{0}s".format(seconds)
    if seconds < 3600:
        return "{0}m {1:02d}s".format(seconds // 60, seconds % 60)
    return "{0}h {1:02d}m".format(seconds // 3600, (seconds % 3600) // 60)


# ==========================================================================
# local folder scan - the "get all the Worksharing Revit Files" mechanism
# for the Local branch. (The ACC branch needs no equivalent filter: every
# cloud model acc_file_browser.list_project_files returns is inherently
# workshared/central by construction - ACC does not host non-workshared
# cloud models.)
# ==========================================================================
def scan_local_folder(folder_path, recursive=False, progress_cb=None):
    """Returns {display_name: file_path} for every LOCAL .rvt under
    folder_path that deew_model_scanner classifies as workshared
    (Central or Local Copy - Standalone/Corrupted/Read-only excluded).
    Name collisions get a short path-based suffix, the same convention
    acc_file_browser's own _add() helpers use for cloud item-id
    collisions."""
    items = {}
    models = scanner.scan_folder(folder_path, recursive=recursive, progress_cb=progress_cb)
    for m in models:
        if m.worksharing_status != "Enabled":
            continue
        key = m.file_name
        if key in items and items[key] != m.file_path:
            key = "{0}  [{1}]".format(m.file_name, os.path.basename(os.path.dirname(m.file_path)))
        items[key] = m.file_path
    return items


# ==========================================================================
# matching - List 1 (sources) x List 2 (targets), confirmed mechanic:
# tick rows in both lists, Add Match adds every ticked-source x
# ticked-target pair.
# ==========================================================================
def build_matches(existing_matches, ticked_sources, ticked_targets):
    """Returns (new_matches_list, added_count, skipped_self_count).
    new_matches_list is existing_matches plus every NEW (source, target)
    pair from the ticked_sources x ticked_targets cross product - a file
    can never be matched into itself, and an already-present pair is not
    duplicated. Order is preserved: existing matches first, then newly
    added ones in (source, target) iteration order."""
    result = list(existing_matches)
    seen = set(result)
    added = 0
    skipped_self = 0
    for s in ticked_sources:
        for t in ticked_targets:
            if s == t:
                skipped_self += 1
                continue
            pair = (s, t)
            if pair in seen:
                continue
            seen.add(pair)
            result.append(pair)
            added += 1
    return result, added, skipped_self


def group_by_target(matches):
    """[(source, target), ...] -> OrderedDict target -> [source, ...],
    insertion-ordered so the run loop and the report process/list hosts
    in a predictable, repeatable order."""
    groups = OrderedDict()
    for source, target in matches:
        groups.setdefault(target, [])
        if source not in groups[target]:
            groups[target].append(source)
    return groups


# ==========================================================================
# pre-run analysis + rough time estimate
# ==========================================================================
EST_OPEN_SECONDS = 25    # opening one host, headless and attached
EST_LINK_SECONDS = 8     # creating one link inside an already-open host
EST_SYNC_SECONDS = 20    # synchronizing one host back to its central


def estimate_seconds(groups):
    """A ROUGH heuristic, not a measurement (see module docstring) -
    hosts*OPEN + total_links*LINK + hosts*SYNC."""
    hosts = len(groups)
    total_links = sum(len(sources) for sources in groups.values())
    return hosts * EST_OPEN_SECONDS + total_links * EST_LINK_SECONDS + hosts * EST_SYNC_SECONDS


def analysis_text(groups, estimated_seconds):
    """The multi-line preview shown in the Analysis panel and folded into
    the confirm-before-run dialog - this IS the "analysis of the whole
    process before you start" the tool was asked for."""
    hosts = len(groups)
    total_links = sum(len(sources) for sources in groups.values())
    all_sources = set()
    for sources in groups.values():
        all_sources.update(sources)

    if not groups:
        return "No matches yet. Tick files in both lists and press Add Match."

    lines = [
        "{0} source file(s) -> {1} target host(s), {2} link operation(s) total.".format(
            len(all_sources), hosts, total_links),
        "",
        "Per host:",
    ]
    for target, sources in groups.items():
        lines.append("  {0}  <-  {1} ({2} link(s))".format(
            target, ", ".join(sources), len(sources)))
    lines.append("")
    lines.append("Estimated time: ~{0} (rough estimate - actual time depends on model size, "
                 "network speed, and how many messages Revit needs to auto-resolve per file)"
                 .format(format_duration(estimated_seconds)))
    return "\n".join(lines)


# ==========================================================================
# already-linked skip check - ported from DeeSuperLINK verbatim
# ==========================================================================
def existing_link_names(doc):
    """Lowercased names of RevitLinkTypes already in the host, so a
    re-run does not stack duplicate links."""
    names = set()
    try:
        for lt in FilteredElementCollector(doc).OfClass(RevitLinkType):
            try:
                n = lt.Name
                if n:
                    names.add(n.lower())
            except Exception:
                continue
    except Exception:
        pass
    return names


def already_linked(existing, link_name):
    """Revit link type names usually carry the .rvt extension, but not
    always - compare on the stem so both spellings match."""
    stem = os.path.splitext(link_name)[0].lower()
    for n in existing:
        if os.path.splitext(n)[0] == stem:
            return True
    return False


# ==========================================================================
# the link itself - ported from DeeSuperLINK's _link_once/link_into,
# extended to attach deew_failure_handler's transaction failure
# preprocessor (the one thing DeeSuperLINK itself does not yet do).
# ==========================================================================
def _link_once(doc, link_name, model_path, placement, attachment=AttachmentType.Overlay, logger=None):
    """A single attempt. Returns (ok, detail, instance_or_None). The
    actual Create-type/set-AttachmentType/Create-instance sequence now
    lives in lib/dee_link_create_service.py, shared with DeeSuperLINK and
    DeeLINK - this function keeps owning its own Transaction/failure-
    preprocessor."""
    t = Transaction(doc, "DeeMAPLink: link {0}".format(link_name))
    t.Start()
    try:
        ffh.apply_to_transaction(t, logger)
        ok, detail, instance = lcs.create_link(doc, model_path, placement, attachment)
        if not ok:
            t.RollBack()
            return False, detail, None
        t.Commit()
        return True, detail, instance
    except Exception as e:
        if t.HasStarted() and not t.HasEnded():
            t.RollBack()
        return False, str(e), None


def _shared_coordinates_look_unestablished(instance):
    """After a Shared placement, an identity transform means the link
    landed exactly origin-on-origin - what Revit does when the two
    models have no shared-coordinate relationship to honour. A SIGNAL,
    not proof (a genuinely shared model whose position happens to
    coincide with the host's origin looks the same) - reported as a
    note, never used to silently re-place anything."""
    try:
        return bool(instance.GetTotalTransform().IsIdentity)
    except Exception:
        return False


def link_into(doc, link_name, model_path, placement, fallback=None, attachment=AttachmentType.Overlay, logger=None):
    """Creates the link, falling back to `fallback` placement if the
    requested one is rejected outright (Revit can either REFUSE a Shared
    placement, caught here and retried with the fallback, or ACCEPT it
    and quietly place origin-to-origin, checked and reported rather than
    pretended otherwise). Returns (ok, detail) - detail names the
    placement actually used."""
    ok, detail, instance = _link_once(doc, link_name, model_path, placement, attachment, logger)
    if ok:
        note = placement_label(placement)
        if placement == ImportPlacement.Shared and _shared_coordinates_look_unestablished(instance):
            note += " (no shared-coordinate relationship found - Revit placed it origin-to-origin)"
        return True, "linked - {0}".format(note)

    if fallback is None or fallback == placement:
        return False, detail

    ok2, detail2, _inst = _link_once(doc, link_name, model_path, fallback, attachment, logger)
    if ok2:
        return True, "linked - {0} (requested {1} was rejected: {2})".format(
            placement_label(fallback), placement_label(placement), detail)
    return False, "{0} failed ({1}); {2} also failed ({3})".format(
        placement_label(placement), detail, placement_label(fallback), detail2)


# ==========================================================================
# per-file issue attribution - "handle all the messages ... inform me in
# the final report about the issue in this file"
# ==========================================================================
def issues_from_log(logger, start_index, end_index=None):
    """The slice of logger.entries recorded while one host was open,
    filtered to what is actually worth telling the user about: WARNING/
    ERROR entries, and any dialog auto-resolution the dialog handler
    logged (make_dialog_handler calls logger.debug("Auto-resolved
    dialog", ...) - a DEBUG entry, so it would not otherwise surface
    here). Returns a single semicolon-joined string, "" if nothing
    notable happened for this host."""
    if logger is None:
        return ""
    entries = logger.entries[start_index:end_index]
    parts = []
    for e in entries:
        level = e.get("level")
        message = e.get("message", "")
        if level in ("WARNING", "ERROR", "CRITICAL"):
            parts.append("{0}: {1}".format(level, message))
        elif "dialog" in message.lower():
            dialog_id = e.get("dialog_id", "")
            parts.append("Auto-resolved dialog{0}".format(
                " ({0})".format(dialog_id) if dialog_id else ""))
    return "; ".join(parts)
