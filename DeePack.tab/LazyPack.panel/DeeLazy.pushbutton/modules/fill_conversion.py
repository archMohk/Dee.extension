# -*- coding: utf-8 -*-
"""
DeeLazy - Fill Pattern Conversion module
Batch-converts Fill Patterns between the two FillPattern.Target values
(Model / Drafting) by creating a NEW FillPatternElement with the
opposite Target and scaled grid geometry - the original pattern is
never mutated or deleted, so anything already using it (a Material's
surface pattern, a Filled Region Type, ...) is completely unaffected.

--------------------------------------------------------------------
The physics being converted (verified against revitapidocs.com/
Autodesk help before writing, not guessed)
--------------------------------------------------------------------
- A Model pattern's FillGrid Offset/Shift/segment lengths are
  REAL-WORLD units (feet) - the pattern scales WITH the view, so a
  1'-0" real spacing prints at 1/100 ft at 1:100 scale.
- A Drafting pattern's FillGrid Offset/Shift/segment lengths are
  PAPER-SPACE size directly - fixed regardless of view scale.
- There is no single "correct" conversion factor between the two -
  going from Model to Drafting means picking a reference scale to
  "lock in" (what should this pattern look like on paper at 1:X?).
  That is why Reference Scale is a per-row, user-editable value here,
  not a constant.
- Model -> Drafting: divide Offset/Shift/segment values by the
  reference scale ratio (paper size = real size / ratio).
- Drafting -> Model: multiply by the ratio (real size = paper size *
  ratio).
- FillGrid.Angle is copied unchanged - a direction is dimensionless
  with respect to this scaling operation.

--------------------------------------------------------------------
NEEDS LIVE-REVIT VERIFICATION (flagged, not silently assumed correct -
same policy as this module's sibling, view_cropping.py)
--------------------------------------------------------------------
- FillGrid.Origin is left UNSCALED here. If a live test shows a
  converted pattern's lines are offset/misaligned relative to the
  un-converted original, Origin likely needs the same scale factor
  applied - see _build_new_pattern's `factor` parameter, a single
  change point.
- Whether FillPattern.SetFillGrids() fully replaces the seed grid list
  from the constructor (rather than appending to it), and whether
  GridCount reflects the new list immediately afterward.
- Whether fill pattern name uniqueness is scoped per-Target or global
  across both targets - this module conservatively assumes GLOBAL
  (one shared name set for the duplicate check), which can only ever
  cause an extra safe skip, never a silent overwrite, if that
  assumption turns out to be wrong.

--------------------------------------------------------------------
Root-cause lesson from this module's sibling (view_cropping.py),
NOT to repeat here
--------------------------------------------------------------------
_THIS_DIR is computed directly in THIS file via
os.path.dirname(os.path.abspath(__file__)) - never through a shared
utils.py helper. A shared function's __file__ always resolves to that
function's OWN file, never the caller's; utils.module_dir() made
exactly this mistake and broke View Cropping's "Open" button outright
(it existed, was removed once this was found) until fixed.
"""
import os
import time

from pyrevit import forms, script
import dee_branding
from Autodesk.Revit.DB import (
    FilteredElementCollector, FillPatternElement, FillPattern, FillGrid,
    FillPatternTarget, Transaction,
)
from System.Collections.Generic import List

output = script.get_output()

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "FillConversion.xaml")

_DEFAULT_SCALE_TEXT = "100"
_DEFAULT_SUFFIX_MODEL = " (Model)"
_DEFAULT_SUFFIX_DRAFTING = " (Drafting)"
_MIN_SCALE_RATIO = 1e-6


def safe_float(text, default=0.0):
    try:
        return float(text)
    except Exception:
        return default


# ==========================================================================
# Scanning
# ==========================================================================
class FillPatternRow(object):
    def __init__(self, element):
        self.element = element
        self.id = element.Id
        self.selected = False
        pat = element.GetFillPattern()
        self.name = pat.Name
        self.target = pat.Target
        self.is_solid = pat.IsSolidFill
        self.grid_count = pat.GridCount
        self.scale_text = _DEFAULT_SCALE_TEXT
        self.new_name_text = ""

    @property
    def target_text(self):
        return "Model" if self.target == FillPatternTarget.Model else "Drafting"

    @property
    def solid_text(self):
        return "Yes" if self.is_solid else "No"

    @property
    def convertible(self):
        return not self.is_solid

    @property
    def convert_to_text(self):
        if self.is_solid:
            return "N/A (Solid Fill)"
        return "Drafting" if self.target == FillPatternTarget.Model else "Model"

    def refresh_new_name(self, suffix_model, suffix_drafting):
        if self.is_solid:
            self.new_name_text = "-"
            return
        suffix = suffix_drafting if self.target == FillPatternTarget.Model else suffix_model
        self.new_name_text = "{0}{1}".format(self.name, suffix)


def scan(doc):
    rows = []
    for fp in FilteredElementCollector(doc).OfClass(FillPatternElement):
        try:
            rows.append(FillPatternRow(fp))
        except Exception:
            continue
    rows.sort(key=lambda r: (r.target_text, r.name.lower()))
    return rows


# ==========================================================================
# Conversion
# ==========================================================================
def _scale_factor(old_target, ratio):
    """Model -> Drafting: divide (paper size = real size / ratio).
    Drafting -> Model: multiply (real size = paper size * ratio)."""
    if old_target == FillPatternTarget.Model:
        return 1.0 / ratio
    return ratio


def _new_target(old_target):
    return FillPatternTarget.Drafting if old_target == FillPatternTarget.Model else FillPatternTarget.Model


def _build_new_pattern(old_pattern, new_target, new_name, factor):
    """Builds an in-memory FillPattern with the same grid geometry as
    old_pattern, scaled by `factor`, under new_target/new_name. The
    5-arg constructor is only used to obtain a valid seed FillPattern
    instance (there is no parameterless "just give me an empty
    pattern" constructor) - its angle/spacing are placeholders,
    entirely superseded by the SetFillGrids() call below."""
    old_grids = list(old_pattern.GetFillGrids())
    seed_angle = old_grids[0].Angle if old_grids else 0.0
    seed_spacing = old_grids[0].Offset if (old_grids and old_grids[0].Offset > 1e-9) else (1.0 / 12.0)
    new_pattern = FillPattern(new_name, new_target, old_pattern.HostOrientation, seed_angle, seed_spacing)

    new_grids = List[FillGrid]()
    for g in old_grids:
        ng = FillGrid()
        ng.Angle = g.Angle
        ng.Origin = g.Origin  # NOT scaled - see module docstring's "NEEDS LIVE VERIFICATION"
        ng.Offset = g.Offset * factor
        ng.Shift = g.Shift * factor
        segs = list(g.GetSegments())
        if segs:
            ng.SetSegments(List[float]([s * factor for s in segs]))
        new_grids.Add(ng)

    new_pattern.SetFillGrids(new_grids)
    return new_pattern


def _collect_existing_names(doc):
    names = set()
    for fp in FilteredElementCollector(doc).OfClass(FillPatternElement):
        try:
            names.add(fp.GetFillPattern().Name)
        except Exception:
            continue
    return names


class ConvertResult(object):
    def __init__(self):
        self.created_count = 0
        self.skipped = []  # list of (label, reason)
        self.elapsed_seconds = 0.0

    def add_skip(self, label, reason):
        self.skipped.append((label, reason))


def convert_selected(doc, rows, suffix_model=_DEFAULT_SUFFIX_MODEL, suffix_drafting=_DEFAULT_SUFFIX_DRAFTING):
    """One Transaction, per-row try/except - a bad row (invalid scale,
    solid fill, name collision, or an unexpected API failure) is
    skipped and reported, never aborts the rest of the batch."""
    start = time.time()
    result = ConvertResult()
    existing_names = _collect_existing_names(doc)

    t = Transaction(doc, "DeeLazy - Fill Pattern Conversion")
    t.Start()
    try:
        for row in rows:
            label = "{0} ({1})".format(row.name, row.target_text)

            if row.is_solid:
                result.add_skip(label, "Solid fill pattern has no grid geometry to scale - skipped")
                continue

            ratio = safe_float(row.scale_text, -1.0)
            if ratio <= _MIN_SCALE_RATIO:
                result.add_skip(label, "Reference Scale '{0}' is not a valid positive number - skipped".format(
                    row.scale_text))
                continue

            try:
                old_pattern = row.element.GetFillPattern()
                new_target = _new_target(row.target)
                suffix = suffix_drafting if row.target == FillPatternTarget.Model else suffix_model
                new_name = "{0}{1}".format(row.name, suffix)

                if new_name in existing_names:
                    result.add_skip(label, "'{0}' already exists - skipped".format(new_name))
                    continue

                factor = _scale_factor(row.target, ratio)
                new_pattern = _build_new_pattern(old_pattern, new_target, new_name, factor)
                FillPatternElement.Create(doc, new_pattern)

                existing_names.add(new_name)
                result.created_count += 1
            except Exception as e:
                result.add_skip(label, "Conversion failed: {0}".format(e))
        t.Commit()
    except Exception:
        t.RollBack()
        raise

    result.elapsed_seconds = time.time() - start
    return result


def print_report(result):
    html = ['<h2 style="font-family:sans-serif;color:#ddd;">DeeLazy - Fill Pattern Conversion Results</h2>',
            '<p style="color:#ddd;">{0} pattern(s) created, {1} skipped - {2:.2f}s.</p>'.format(
                result.created_count, len(result.skipped), result.elapsed_seconds)]
    for label, reason in result.skipped:
        html.append(
            '<div style="padding:4px 10px;margin:2px 0;background:#c62828;color:#fff;'
            'border-radius:4px;font-family:monospace;font-size:12px;">'
            '&#10007;&nbsp; <b>{0}</b> &mdash; {1}</div>'.format(label, reason))
    output.print_html("".join(html))


# ==========================================================================
# Window - UI wiring only; all real work happens in the plain functions
# above (same separation as view_cropping.py)
# ==========================================================================
class FillConversionWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, uiapp):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.uiapp = uiapp
        self.doc = uiapp.ActiveUIDocument.Document
        self._rows = []
        self.scan_click(None, None)

    def scan_click(self, sender, args):
        self._rows = scan(self.doc)
        self._refresh_grid()

    def _refresh_grid(self):
        suffix_model = self.suffix_model_tb.Text or _DEFAULT_SUFFIX_MODEL
        suffix_drafting = self.suffix_drafting_tb.Text or _DEFAULT_SUFFIX_DRAFTING
        for r in self._rows:
            r.refresh_new_name(suffix_model, suffix_drafting)
        self.patterns_grid.ItemsSource = None
        self.patterns_grid.ItemsSource = self._rows
        sel_count = sum(1 for r in self._rows if r.selected)
        self.status_tb.Text = "{0} pattern(s) found, {1} selected.".format(len(self._rows), sel_count)

    def suffix_changed(self, sender, args):
        self._refresh_grid()

    # ---------------- selection ----------------
    def select_all_click(self, sender, args):
        for r in self._rows:
            if r.convertible:
                r.selected = True
        self.patterns_grid.Items.Refresh()

    def select_none_click(self, sender, args):
        for r in self._rows:
            r.selected = False
        self.patterns_grid.Items.Refresh()

    def invert_selection_click(self, sender, args):
        for r in self._rows:
            if r.convertible:
                r.selected = not r.selected
        self.patterns_grid.Items.Refresh()

    # ---------------- convert ----------------
    def convert_click(self, sender, args):
        selected = [r for r in self._rows if r.selected]
        if not selected:
            forms.alert("Check at least one fill pattern first.")
            return
        if not forms.alert(
                "This will create {0} new fill pattern(s) with the opposite Target. "
                "Existing patterns are never changed. Continue?".format(len(selected)),
                yes=True, no=True):
            return

        suffix_model = self.suffix_model_tb.Text or _DEFAULT_SUFFIX_MODEL
        suffix_drafting = self.suffix_drafting_tb.Text or _DEFAULT_SUFFIX_DRAFTING
        result = convert_selected(self.doc, selected, suffix_model, suffix_drafting)
        print_report(result)
        self.scan_click(None, None)
        self.status_tb.Text = "Created {0}, skipped {1}. See the pyRevit output window for details.".format(
            result.created_count, len(result.skipped))

    def close_click(self, sender, args):
        self.Close()


# ==========================================================================
# Launch entry point (called by the DeeLazy home window)
# ==========================================================================
def launch(uiapp):
    if uiapp.ActiveUIDocument is None:
        forms.alert("Open a Revit project first.")
        return
    window = FillConversionWindow(_XAML_FILE, uiapp)
    window.ShowDialog()


TOOL_INFO = {
    "id": "fill_conversion",
    "title": "Fill Pattern Conversion",
    "description": "Convert Fill Patterns between Model and Drafting Target - creates a new, geometry-scaled pattern at a reference scale you choose; originals are never touched.",
    "launch": launch,
}
