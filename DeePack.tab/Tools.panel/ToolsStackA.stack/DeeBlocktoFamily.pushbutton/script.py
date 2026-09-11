# -*- coding: utf-8 -*-
"""
DeeBlocktoFamily
Pick a linked or imported CAD (DWG) file, click one visible occurrence
of a block directly in an open Revit view, and place a chosen Revit
Family instance at every OTHER occurrence of that same block found in
the file - matching position and rotation. The CAD/DWG geometry itself
is never touched, deleted, or modified.

Click-to-identify rather than a named list: real AutoCAD block names
are not reliably readable via Revit's public API (researched - see
lib/dee_block_to_family_service.py's module docstring for sources), so
the user visually identifies the block by clicking it in their own CAD
content and the tool matches every other occurrence by geometry.

--------------------------------------------------------------------
TWO-PHASE FLOW - why this file is shaped the way it is
--------------------------------------------------------------------
Phase 1 (this module's main(), NO window open at any point):
    pick the CAD file -> Selection.PickObject -> walk the geometry ->
    resolve the click -> find matching occurrences -> reduce everything
    to plain Python numbers.
Phase 2 (the WPF window): choose Family/Type/Level and place. The
    window makes NO Revit API calls except the final placement
    Transaction.

The earlier design ran PickObject from a button inside the wizard
window, calling self.Hide() first and self.Show() after. That kept
crashing Revit outright ("An unrecoverable error has occurred") at
whatever the user touched next. Hide() does NOT end a modal dialog -
ShowDialog() is still blocking further up the stack, so Revit was
being driven from inside a nested modal message loop, which is a
documented no-go for Selection.PickObject specifically. The pick
itself appeared to succeed (317 matches were found and displayed), but
it left the host in a state that died at the next interaction - which
is exactly why the crash point kept sliding around as other things
were fixed, and why no try/except ever caught it.

So the pick now happens with no window in play at all. This is the
fallback the tool's own plan named for exactly this outcome, and it
also matches every other tool in this extension: they open a modal
window and never hand control back to the Revit view while it's up.

Trade-off, accepted deliberately: choosing a different block now means
re-running the tool rather than clicking "Re-pick" in the window. That
is a small, honest cost for not crashing.

A step-by-step trace is appended to DeeBlocktoFamily_trace.log in the
system TEMP folder (each line flushed and the file closed immediately,
so the trail survives even a hard process kill).
If anything still goes wrong, that file says exactly which step was
last reached.
"""
import math
import os
import tempfile
import traceback

import System
import clr
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
from System.Windows import Visibility
from System.Windows.Media import SolidColorBrush, Color

from pyrevit import forms, script
import dee_branding
import dee_block_to_family_service as core

from Autodesk.Revit.UI.Selection import ObjectType, ISelectionFilter
import dee_telemetry
dee_telemetry.check_access("DeeBlocktoFamily")


output = script.get_output()

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")

_ERROR_BRUSH = SolidColorBrush(Color.FromRgb(0xC6, 0x28, 0x28))   # red - a real blocker
_NOTE_BRUSH = SolidColorBrush(Color.FromRgb(0x2E, 0x7D, 0x32))    # green - informational

_LOG_PATH = os.path.join(tempfile.gettempdir(), "DeeBlocktoFamily_trace.log")


def _log(message):
    """Opened/closed per line on purpose - a buffered handle would lose
    the last (most interesting) lines if Revit dies hard."""
    try:
        with open(_LOG_PATH, "a") as fh:
            fh.write("{0}  {1}\n".format(
                System.DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss"), message))
    except Exception:
        pass


class _SingleElementFilter(ISelectionFilter):
    """Restricts PickObject to only the chosen ImportInstance, so Revit
    itself refuses clicks on anything else."""
    def __init__(self, target_id):
        self._target_id = target_id

    def AllowElement(self, elem):
        try:
            return elem.Id == self._target_id
        except Exception:
            return False

    def AllowReference(self, reference, point):
        return True


class MatchDisplayRow(object):
    def __init__(self, doc, index, occurrence):
        self.index = index
        x, y, z = occurrence.origin  # plain floats - no Revit objects retained
        self.x_text = "{0:.2f}".format(core.internal_to_display(doc, x))
        self.y_text = "{0:.2f}".format(core.internal_to_display(doc, y))
        self.z_text = "{0:.2f}".format(core.internal_to_display(doc, z))
        self.rotation_text = "{0:.1f}".format(math.degrees(occurrence.rotation_radians))


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
# Phase 2: the window (no Revit API calls except the final placement)
# ==========================================================================
class DeeBlocktoFamilyWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, doc, cad_label, matches, type_index, family_index, level_entries):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.doc = doc
        self._matches = matches
        self._type_index = type_index
        self._family_index = family_index
        self._level_entries = level_entries
        self._level_by_name = dict((e.name, e) for e in level_entries)
        self._selected_entry = None

        self.summary_tb.Text = "{0} matching block occurrence(s) found.".format(len(matches))
        self.cad_file_tb.Text = "CAD file: {0}".format(cad_label)

        self.category_cb.ItemsSource = sorted(self._family_index.keys())
        self.level_cb.ItemsSource = sorted(self._level_by_name.keys())
        self.fallback_unit_tb.Text = core.unit_abbreviation(doc)
        self._set_fallback_visible(False)

        rows = [MatchDisplayRow(doc, i + 1, occ) for i, occ in enumerate(matches)]
        self.matches_grid.ItemsSource = rows

        self._default_level_from_matches()
        self.status_tb.Text = "Pick a Category, Family, Type and Level, then Place."

    def _default_level_from_matches(self):
        if not self._matches or not self._level_entries:
            return
        avg_z = sum(occ.origin[2] for occ in self._matches) / float(len(self._matches))
        nearest = core.nearest_level_entry(self._level_entries, avg_z)
        if nearest is not None:
            try:
                self.level_cb.SelectedItem = nearest.name
            except Exception:
                pass

    # ---------------- family pickers (pure dict lookups, no Revit API) ----
    def category_cb_changed(self, sender, args):
        cat = self.category_cb.SelectedItem
        families = self._family_index.get(cat, {}) if cat else {}
        self.family_cb.ItemsSource = sorted(families.keys())
        self.family_cb.SelectedIndex = -1
        self.type_cb.ItemsSource = []
        self._update_symbol()

    def family_cb_changed(self, sender, args):
        cat = self.category_cb.SelectedItem
        fam = self.family_cb.SelectedItem
        types = self._type_index.get(cat, {}).get(fam, {}) if cat and fam else {}
        self.type_cb.ItemsSource = sorted(types.keys())
        self.type_cb.SelectedIndex = -1
        self._update_symbol()

    def type_cb_changed(self, sender, args):
        self._update_symbol()

    def _update_symbol(self):
        # 100% Revit-API-free: the index holds core.FamilyTypeEntry
        # objects (ElementId + a placement kind already read as a plain
        # string). The live FamilySymbol is only re-resolved inside
        # place_matches. See FamilyTypeEntry's docstring for why
        # touching a long-cached FamilySymbol here crashed Revit.
        cat = self.category_cb.SelectedItem
        fam = self.family_cb.SelectedItem
        typ = self.type_cb.SelectedItem
        self._selected_entry = None
        if cat and fam and typ:
            self._selected_entry = self._type_index.get(cat, {}).get(fam, {}).get(typ)
        entry = self._selected_entry
        self._set_fallback_visible(entry is not None and entry.needs_host_face)
        if entry is None:
            self._set_note("", ok=True)
        elif not entry.is_supported:
            self._set_note(
                "'{0}' can't be used here - it's {1}. Pick a different Type.".format(
                    entry.type_name, entry.unsupported_reason), ok=False)
        elif entry.needs_host_face:
            self._set_note(
                "'{0}' is a ceiling/face-hosted family. Each one will be hosted to the ceiling "
                "directly above its block; where there's no ceiling, it falls back to a reference "
                "plane at the height set above.".format(entry.type_name), ok=True)
        else:
            self._set_note("", ok=True)

    def _set_note(self, text, ok):
        """Red only for a real blocker - an informational note about
        face-hosting shouldn't look like an error."""
        self.placement_warning_tb.Text = text
        try:
            self.placement_warning_tb.Foreground = _NOTE_BRUSH if ok else _ERROR_BRUSH
        except Exception:
            pass

    def _set_fallback_visible(self, visible):
        vis = Visibility.Visible if visible else Visibility.Collapsed
        for ctrl in (self.fallback_label_tb, self.fallback_height_tb, self.fallback_unit_tb):
            try:
                ctrl.Visibility = vis
            except Exception:
                pass

    # ---------------- place ----------------
    def place_click(self, sender, args):
        entry = self._selected_entry
        if entry is None:
            forms.alert("Pick a Category, Family, and Type first.")
            return
        if not entry.is_supported:
            forms.alert("'{0}' can't be used here - it's {1}. Pick a different Type.".format(
                entry.type_name, entry.unsupported_reason))
            return
        level_name = self.level_cb.SelectedItem
        level_entry = self._level_by_name.get(level_name) if level_name else None
        if level_entry is None:
            forms.alert("Pick a Level first.")
            return

        fallback_internal = 0.0
        if entry.needs_host_face:
            try:
                fallback_display = float(self.fallback_height_tb.Text)
            except (TypeError, ValueError):
                forms.alert("Fallback height must be a number.")
                return
            fallback_internal = core.display_to_internal(self.doc, fallback_display)

        rotation_note = ("matching each block's rotation"
                          if self.apply_rotation_cb.IsChecked is True
                          else "all at the family's default orientation (rotation ignored)")
        if not forms.alert(
                "Place '{0}' at {1} matched occurrence(s), {2}?".format(
                    entry.type_name, len(self._matches), rotation_note),
                title="DeeBlocktoFamily - Confirm", yes=True, no=True):
            return

        # CheckBox.IsChecked is a Nullable<bool> - it can arrive as
        # None (indeterminate), so compare explicitly rather than
        # relying on truthiness.
        apply_rotation = (self.apply_rotation_cb.IsChecked is True)

        _log("PLACE start: type='{0}' kind={1} count={2} rotation={3}".format(
            entry.type_name, entry.placement_kind, len(self._matches), apply_rotation))
        with _SafeProgress(title="DeeBlocktoFamily - placing families...", indeterminate=True):
            result = core.place_matches(
                self.doc, entry, self._matches, level_entry, fallback_internal,
                apply_rotation=apply_rotation)
        _log("PLACE done: placed={0} skipped={1}".format(result.placed_count, len(result.skipped)))

        core.print_report(result, len(self._matches), entry.type_name)
        self.status_tb.Text = "Placed {0}, skipped {1}. See the pyRevit output window for details.".format(
            result.placed_count, len(result.skipped))

    def close_click(self, sender, args):
        self.Close()


# ==========================================================================
# Phase 1: everything that touches the Revit view/geometry, with NO
# window open - see the module docstring for why this ordering matters
# ==========================================================================
def _choose_cad_instance(cad_rows):
    if not cad_rows:
        return None
    if len(cad_rows) == 1:
        return cad_rows[0]
    labels = {}
    for row in cad_rows:
        labels[row.label] = row
    picked_label = forms.SelectFromList.show(
        sorted(labels.keys()), title="DeeBlocktoFamily - pick the CAD file", multiselect=False)
    if not picked_label:
        return None
    return labels.get(picked_label)


def main():
    uiapp = __revit__
    if uiapp.ActiveUIDocument is None:
        forms.alert("Open a Revit project first.")
        return
    uidoc = uiapp.ActiveUIDocument
    doc = uidoc.Document

    _log("=== DeeBlocktoFamily run start ===")

    cad_rows = core.scan_cad_instances(doc)
    _log("CAD instances found: {0}".format(len(cad_rows)))
    if not cad_rows:
        forms.alert("No CAD imports or links found in this project.")
        return

    selected_cad = _choose_cad_instance(cad_rows)
    if selected_cad is None:
        _log("cancelled at CAD file selection")
        return
    _log("CAD chosen: {0}".format(selected_cad.file_name))

    # --- the pick, with NO window open ---
    forms.alert(
        "Click one visible occurrence of the block you want to replace.\n\n"
        "Every other occurrence of that same block in '{0}' will be found automatically.".format(
            selected_cad.file_name),
        title="DeeBlocktoFamily")
    _log("PickObject: about to call")
    try:
        ref = uidoc.Selection.PickObject(
            ObjectType.PointOnElement,
            _SingleElementFilter(selected_cad.id),
            "Click one visible occurrence of the block to replace")
    except Exception:
        _log("PickObject: cancelled or failed")
        return  # user pressed Escape - not a fault worth alerting about
    _log("PickObject: returned OK")

    world_point = None
    try:
        world_point = ref.GlobalPoint
    except Exception:
        world_point = None
    if world_point is None:
        _log("GlobalPoint unavailable")
        forms.alert("Could not resolve a 3D point from that click - "
                     "try clicking directly on a visible line/edge of the block.")
        return
    _log("GlobalPoint OK")

    # Re-resolve the ImportInstance from its id rather than reusing the
    # object captured during the initial scan - PickObject sits between
    # the two, and holding an Element across another API operation is
    # the exact pattern that crashed this tool before.
    cad_element = doc.GetElement(selected_cad.id)
    if cad_element is None:
        _log("could not re-resolve the CAD instance")
        forms.alert("Could not re-read that CAD instance - try running the tool again.")
        return

    with _SafeProgress(title="DeeBlocktoFamily - scanning CAD geometry...", indeterminate=True):
        occurrences = core.walk_import_instance(doc, cad_element)
    _log("geometry walk done: {0} block occurrence(s)".format(len(occurrences)))

    clicked = core.resolve_clicked_occurrence(occurrences, world_point)
    if clicked is None:
        _log("click did not resolve to any block")
        forms.alert("Could not find any block near that click - try again.")
        return

    matches = core.find_matching_occurrences(occurrences, clicked)
    _log("matches: {0}".format(len(matches)))

    # Cross-check the computed positions against the CAD import's own
    # (authoritative, internal-units) bounding box before anything is
    # placed - see core.diagnose_occurrences.
    pos_ok, pos_msg = core.diagnose_occurrences(doc, cad_element, matches)
    _log("position check: {0}".format(pos_msg))
    for i, occ in enumerate(matches[:3]):
        _log("  sample match {0}: internal(ft)=({1:.4f}, {2:.4f}, {3:.4f})  display=({4:.2f}, "
             "{5:.2f}, {6:.2f}) {7}".format(
                 i + 1, occ.origin[0], occ.origin[1], occ.origin[2],
                 core.internal_to_display(doc, occ.origin[0]),
                 core.internal_to_display(doc, occ.origin[1]),
                 core.internal_to_display(doc, occ.origin[2]),
                 core.unit_abbreviation(doc)))
    if not pos_ok:
        if not forms.alert(
                "The block positions computed from this CAD file fall outside the file's own "
                "extents, which means they are almost certainly at the wrong scale.\n\n"
                "{0}\n\nPlacing now would put families in the wrong place. Continue anyway?".format(
                    pos_msg),
                title="DeeBlocktoFamily - position check FAILED", yes=True, no=True):
            _log("aborted by user after failed position check")
            return

    occurrences = None  # only `matches` is needed from here on

    if not matches:
        forms.alert("No matching blocks found.")
        return

    with _SafeProgress(title="DeeBlocktoFamily - scanning loaded families...", cancellable=True) as pb:
        def cb(i, total):
            if i % 20 == 0 or i == total - 1:
                try:
                    pb.update_progress(i, total)
                except Exception:
                    pass
            return pb.cancelled
        type_index, family_index = core.build_family_index(doc, cb)
    _log("family index built: {0} categor(y/ies)".format(len(family_index)))

    level_entries = core.list_level_entries(doc)
    _log("levels: {0}".format(len(level_entries)))

    # --- Phase 2: the window. Nothing Revit-owned is handed to it. ---
    _log("opening window")
    window = DeeBlocktoFamilyWindow(
        _XAML_FILE, doc, selected_cad.label, matches, type_index, family_index, level_entries)
    window.ShowDialog()
    _log("window closed - run end")


try:
    main()
except Exception:
    _log("UNHANDLED:\n" + traceback.format_exc())
    raise
