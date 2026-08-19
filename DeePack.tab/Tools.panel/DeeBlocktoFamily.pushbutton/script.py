# -*- coding: utf-8 -*-
"""
DeeBlocktoFamily
Pick a linked or imported CAD (DWG) file already in the project, click
one visible occurrence of a block directly in an open Revit view, and
place a chosen Revit Family instance at every OTHER occurrence of that
same block found in the file - matching position and rotation. The
CAD/DWG geometry itself is never touched, deleted, or modified.

Click-to-identify rather than a named list: real AutoCAD block names
are not reliably readable via Revit's public API (researched this
session - see lib/dee_block_to_family_service.py's module docstring
for sources) - so the user visually identifies the block themselves by
clicking it in their own real CAD content, and the tool matches every
other occurrence by comparing geometry, not a name.

All scan/geometry/matching/placement logic lives in
lib/dee_block_to_family_service.py; this file only wires the WPF
window to it, matching this stack's established thin-shell convention
(DeeGetDWG.pushbutton/script.py is the closest shape).

Window/view-interaction handoff: pick_block_click calls self.Hide(),
then uidoc.Selection.PickObject(...) (any exception - including a
plain user Escape-cancel - is swallowed silently, matching common
pyRevit pick-wrapper convention, since a cancel isn't a fault worth
alerting about), then self.Show() in a finally block so the window can
never be left stranded hidden. This is NOT the same pattern that
crashed Revit live in DeeQs this session (forms.ask_for_string opening
a SECOND, separate ShowDialog()/message-loop from inside an
already-modal window) - Hide()/Show() toggle visibility on the SAME
window instance already inside its original ShowDialog() call, and
calling Selection.PickObject from within an open modal WPF window's
event handler is a standard, Revit-API-supported pattern (it hands
control to Revit's own selection/highlighting UI, not a second WPF
dialog). Still flagged NEEDS LIVE-REVIT VERIFICATION per
dee_block_to_family_service.py's own docstring, since this exact
sequence has no prior precedent anywhere in this codebase - if it
proves unstable, the documented fallback is a two-phase flow (pick the
block via a lightweight PickObject call BEFORE the wizard window even
opens, then launch the wizard pre-seeded with the result).
"""
import math
import os

from pyrevit import forms, script
import dee_branding
import dee_block_to_family_service as core

from Autodesk.Revit.UI.Selection import ObjectType, ISelectionFilter

output = script.get_output()

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")


class _SingleElementFilter(ISelectionFilter):
    """Restricts PickObject to only the chosen ImportInstance - Revit
    itself refuses clicks on anything else, so no post-pick "did they
    click the right element" validation is needed."""
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
        origin, angle, _scale = core.extract_position_and_rotation(occurrence.world_transform)
        self.x_text = "{0:.2f}".format(core.internal_to_display(doc, origin.X))
        self.y_text = "{0:.2f}".format(core.internal_to_display(doc, origin.Y))
        self.z_text = "{0:.2f}".format(core.internal_to_display(doc, origin.Z))
        self.rotation_text = "{0:.1f}".format(math.degrees(angle))


class DeeBlocktoFamilyWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, doc, uidoc):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.doc = doc
        self.uidoc = uidoc
        self._cad_rows = []
        self._selected_cad = None
        self._occurrences = []
        self._clicked = None
        self._matches = []
        self._type_index = {}
        self._family_index = {}
        self._selected_symbol = None
        self._levels = []
        self._level_by_name = {}

        self._rescan_cad()

        self._levels = core.list_levels(self.doc)
        self._level_by_name = {}
        for lvl in self._levels:
            try:
                self._level_by_name[lvl.Name] = lvl
            except Exception:
                continue
        self.level_cb.ItemsSource = sorted(self._level_by_name.keys())

        with forms.ProgressBar(title="DeeBlocktoFamily - scanning loaded families...", cancellable=True) as pb:
            self._type_index, self._family_index = core.build_family_index(self.doc, self._progress_cb(pb))
        self.category_cb.ItemsSource = sorted(self._family_index.keys())

        self.matches_grid.ItemsSource = []
        self.status_tb.Text = "Ready. Pick a CAD file on Tab 1."

    def _progress_cb(self, pb):
        def cb(i, total):
            if i % 20 == 0 or i == total - 1:
                try:
                    pb.update_progress(i, total)
                except Exception:
                    pass
            return pb.cancelled
        return cb

    # ======================================================================
    # Tab 1: Pick CAD File
    # ======================================================================
    def _rescan_cad(self):
        self._cad_rows = core.scan_cad_instances(self.doc)
        self.cad_grid.ItemsSource = None
        self.cad_grid.ItemsSource = self._cad_rows
        self.status_tb.Text = "{0} CAD import/link(s) found.".format(len(self._cad_rows))

    def rescan_click(self, sender, args):
        self._rescan_cad()

    def cad_grid_selection_changed(self, sender, args):
        self._selected_cad = self.cad_grid.SelectedItem
        self._occurrences = []
        self._clicked = None
        self._matches = []
        self._refresh_matches_grid()
        if self._selected_cad is not None:
            self.status_tb.Text = "Selected: {0}. Go to Tab 2 and click 'Pick Block in View'.".format(
                self._selected_cad.file_name)

    # ======================================================================
    # Tab 2: Pick Block
    # ======================================================================
    def _refresh_matches_grid(self):
        rows = [MatchDisplayRow(self.doc, i + 1, occ) for i, occ in enumerate(self._matches)]
        self.matches_grid.ItemsSource = None
        self.matches_grid.ItemsSource = rows
        if self._matches:
            self.match_summary_tb.Text = "Found {0} matching occurrence(s).".format(len(self._matches))
        else:
            self.match_summary_tb.Text = "No block picked yet."

    def pick_block_click(self, sender, args):
        if self._selected_cad is None:
            forms.alert("Pick a CAD file on Tab 1 first.")
            return

        self.Hide()
        try:
            sel_filter = _SingleElementFilter(self._selected_cad.id)
            try:
                ref = self.uidoc.Selection.PickObject(
                    ObjectType.PointOnElement, sel_filter,
                    "Click one visible occurrence of the block to replace")
            except Exception:
                return  # cancelled (Escape) or nothing pickable - not a fault, no alert needed

            with forms.ProgressBar(title="DeeBlocktoFamily - scanning CAD geometry...", indeterminate=True):
                self._occurrences = core.walk_import_instance(self.doc, self._selected_cad.element)

            world_point = None
            try:
                world_point = ref.GlobalPoint
            except Exception:
                world_point = None
            if world_point is None:
                forms.alert("Could not resolve a 3D point from that click - "
                             "try clicking directly on a visible line/edge of the block.")
                return

            self._clicked = core.resolve_clicked_occurrence(self._occurrences, world_point)
            if self._clicked is None:
                forms.alert("Could not find any block near that click - try again.")
                return
            self._matches = core.find_matching_occurrences(self._occurrences, self._clicked)
        finally:
            self.Show()

        self._refresh_matches_grid()
        self._default_level_from_matches()
        self.status_tb.Text = "{0} matching occurrence(s) found. Pick a Family and Level.".format(
            len(self._matches))
        if self._matches:
            self.main_tabs.SelectedIndex = 2

    def _default_level_from_matches(self):
        if not self._matches or not self._levels:
            return
        avg_z = sum(occ.world_transform.Origin.Z for occ in self._matches) / float(len(self._matches))
        nearest = core.nearest_level(self._levels, avg_z)
        if nearest is not None:
            try:
                self.level_cb.SelectedItem = nearest.Name
            except Exception:
                pass

    # ======================================================================
    # Tab 3: Family & Place
    # ======================================================================
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
        cat = self.category_cb.SelectedItem
        fam = self.family_cb.SelectedItem
        typ = self.type_cb.SelectedItem
        self._selected_symbol = None
        if cat and fam and typ:
            self._selected_symbol = self._type_index.get(cat, {}).get(fam, {}).get(typ)
        if self._selected_symbol is not None and not core.is_placement_supported(self._selected_symbol):
            self.placement_warning_tb.Text = (
                "'{0}' is a {1} family - only point-placed (OneLevelBased) families like Generic "
                "Models/Furniture/Planting are supported by this tool. Pick a different Type.").format(
                    typ, core.placement_kind_text(self._selected_symbol))
        else:
            self.placement_warning_tb.Text = ""

    def place_click(self, sender, args):
        if not self._matches:
            forms.alert("Pick a block on Tab 2 first.")
            return
        if self._selected_symbol is None:
            forms.alert("Pick a Category, Family, and Type first.")
            return
        if not core.is_placement_supported(self._selected_symbol):
            forms.alert("This family's placement type is not supported by this tool - pick a different Type.")
            return
        level_name = self.level_cb.SelectedItem
        level = self._level_by_name.get(level_name) if level_name else None
        if level is None:
            forms.alert("Pick a Level first.")
            return

        if not forms.alert(
                "Place '{0}' at {1} matched occurrence(s)?".format(self.type_cb.SelectedItem, len(self._matches)),
                title="DeeBlocktoFamily - Confirm", yes=True, no=True):
            return

        with forms.ProgressBar(title="DeeBlocktoFamily - placing families...", indeterminate=True):
            result = core.place_matches(self.doc, self._selected_symbol, self._matches, level)

        core.print_report(result, len(self._matches), self.type_cb.SelectedItem)
        self.status_tb.Text = "Placed {0}, skipped {1}. See the pyRevit output window for details.".format(
            result.placed_count, len(result.skipped))

    def close_click(self, sender, args):
        self.Close()


uiapp = __revit__
if uiapp.ActiveUIDocument is None:
    forms.alert("Open a Revit project first.")
else:
    window = DeeBlocktoFamilyWindow(_XAML_FILE, uiapp.ActiveUIDocument.Document, uiapp.ActiveUIDocument)
    window.ShowDialog()
