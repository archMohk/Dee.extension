# -*- coding: utf-8 -*-
"""
health_rubric
Revit-API-free parsing and scoring logic for DeeHealth's QC rubric
(a weighted, 3-tier-threshold Excel scoring sheet: SectionName | SCORE |
TestName | SCORE | E | F | G | Description, matching the structure of
firm QC-test workbooks like "Internal REVIT QC Tests.xlsx").

Kept separate from script.py (which needs the live Revit API for the
actual checks) so the parsing/scoring math can be unit-tested on its
own, same as distribution_patterns.py and host_level_tools.py.
"""
import re

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def normalize_test_name(name):
    """Collapses whitespace and lowercases, so 'family ', 'Family', and
    '  family  ' all match the same check_id - the source workbook has
    inconsistent spacing/casing in test names."""
    return " ".join((name or "").strip().lower().split())


# Maps a normalized test name (as found in the rubric workbook) to an
# internal check_id that lib/health_checks.py knows how to run. Test
# names that don't appear here are still parsed and shown, just marked
# "not implemented" rather than crashing or being silently dropped.
CHECK_ID_MAP = {
    "family": "families_naming",
    "worksets": "worksets_naming",
    "line style": "line_style_naming",
    "warnings": "warnings_count",
    "file size": "file_size",
    "purgeable elements": "purgeable_elements",
    "duplicate modeled elements": "duplicate_elements",
    "total elements": "total_elements",
    "model groups": "model_groups",
    "detail groups": "detail_groups",
    "model lines": "model_lines",
    "generic models": "generic_models",
    "masses": "masses",
    "largest family": "largest_family",
    "project organization": "project_organization",
    "model in place": "in_place_families",
    "imported skp files": "imported_skp",
    "imported cad files": "imported_cad",
    "linked cad files": "linked_cad",
    "linked cad file visible in all views": "linked_cad_visible_all_views",
    "linked revit files and their link method": "linked_revit_method",
    "linked revit files not pinned in place": "linked_revit_unpinned",
    "linked cad not pinned in place": "linked_cad_unpinned",
    "raster image": "raster_images",
    "project information": "project_information",
    "design options": "design_options",
    "project coordinates": "project_coordinates",
    "workset": "worksets_list",
    "phase elements": "phase_elements",
    "views": "views_count",
    "sheets": "sheets_count",
    "view with hidden element": "views_hidden_elements",
    "views not on sheets": "views_not_on_sheets",
    "views on sheets with no view template": "views_no_template",
    "levels": "levels_count",
    "grids": "grids_count",
    "unplaced rooms": "unplaced_rooms",
    "unclosed rooms": "overlapping_rooms",
    "unique room number": "duplicate_room_numbers",
    "unplaced spaces": "unplaced_spaces",
    "unenclosed spaces": "overlapping_spaces",
    "unique spaces number": "duplicate_space_numbers",
    "areas": "areas_not_placed",
    "host dependent elements hosted ?": "unhosted_elements",
    "elements on the correct host level": "mishosted_elements",
}


class HealthTest(object):
    """One row of the rubric. weight/e/f/g are None when the workbook left
    that cell blank (an informational/report-only row - still runs and
    reports a value, just never contributes to the overall score)."""
    def __init__(self, section, section_weight, name, weight, e_raw, f_raw, g_raw, description):
        self.section = section
        self.section_weight = section_weight
        self.name = name
        self.weight = weight
        self.e_raw = e_raw
        self.f_raw = f_raw
        self.g_raw = g_raw
        self.description = description
        self.check_id = CHECK_ID_MAP.get(normalize_test_name(name))
        self.implemented = False
        self.result_value = None
        self.result_detail = ""
        self.result_score = None

    @property
    def is_scored(self):
        return self.weight is not None and self.weight > 0

    @property
    def weight_text(self):
        return "" if self.weight is None else "{0:.4f}".format(self.weight)

    @property
    def result_value_text(self):
        return "" if self.result_value is None else str(self.result_value)

    @property
    def result_score_text(self):
        return "" if self.result_score is None else "{0:.0f}%".format(self.result_score * 100)


def _extract_number(raw):
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip()
    if not s:
        return None
    m = _NUM_RE.search(s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _parse_weight(raw):
    """Weight cells are sometimes non-numeric (e.g. '0%\\n(PBI Check)' for
    a section that's explicitly excluded from scoring) - only a cleanly
    parseable number counts as an actual weight."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def score_test(value, e_raw, g_raw):
    """Returns a 0.0-1.0 score given a raw check value and the test's E
    (first) and G (third) threshold cells, or None if either threshold
    or the value itself isn't usable. Auto-detects direction by
    comparing E and G: E < G means lower values are better (most counts
    - Warnings, File Size, ...); E > G means higher values are better
    (the naming-convention % tests, where E=0.7 is the good threshold
    and G=0.5 is the fail threshold). Linearly interpolates between the
    two for a graded score rather than a hard pass/fail."""
    if value is None:
        return None
    e = _extract_number(e_raw)
    g = _extract_number(g_raw)
    if e is None or g is None:
        return None
    if e == g:
        return 1.0 if value == e else 0.0
    if e < g:
        if value <= e:
            return 1.0
        if value >= g:
            return 0.0
        return 1.0 - (value - e) / (g - e)
    else:
        if value >= e:
            return 1.0
        if value <= g:
            return 0.0
        return (value - g) / (e - g)


def parse_rubric_grid(grid):
    """grid: a 2D list of cell text (row 0 = headers), as returned by
    xlsx_reader.read_xlsx_sheets()[sheet_name]. Returns a list of
    HealthTest objects in the workbook's own row order."""
    tests = []
    current_section = ""
    current_section_weight = None
    for row in grid[1:]:
        if not row or not any((c or "").strip() for c in row):
            continue
        padded = list(row) + [""] * max(0, 8 - len(row))
        a, b, c, d, e, f, g, h = padded[:8]
        if (a or "").strip():
            current_section = a.strip()
            current_section_weight = _parse_weight(b)
        test_name = (c or "").strip()
        if not test_name:
            continue
        tests.append(HealthTest(
            section=current_section,
            section_weight=current_section_weight,
            name=test_name,
            weight=_parse_weight(d),
            e_raw=e, f_raw=f, g_raw=g,
            description=(h or "").strip(),
        ))
    return tests


def compute_overall_score(tests):
    """Weighted average of every test that both HAS a weight and was
    actually scored (result_score is not None) - re-normalized against
    the weight of only what could be measured, so unmapped/failed
    checks don't silently drag the score down by being treated as 0.
    Returns None if nothing scorable was found."""
    total_weight = 0.0
    total_weighted = 0.0
    for t in tests:
        if t.weight is None or t.result_score is None:
            continue
        total_weight += t.weight
        total_weighted += t.weight * t.result_score
    if total_weight <= 0:
        return None
    return total_weighted / total_weight
