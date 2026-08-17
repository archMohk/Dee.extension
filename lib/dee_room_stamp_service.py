# -*- coding: utf-8 -*-
"""
DeeRoomStamp business logic
Scans every real model element in the project, lets the user pick ONE
text parameter by name, and - for every element that has that
parameter - writes the name of the Room the element is physically
located in. Elements with no room match are left untouched (the
parameter is never blanked/cleared - explicit user choice).

--------------------------------------------------------------------
Why Room.IsPointInRoom needs a two-tier Z retry
--------------------------------------------------------------------
Room.IsPointInRoom(XYZ) tests both X/Y against the room's boundary AND
Z against that room's own computed vertical range (Level elevation +
Base Offset, up to its Upper Limit). An element's representative point
- especially a wall/duct/pipe's LocationCurve midpoint, or a ceiling-
mounted fixture's LocationPoint - can have a Z outside a room's
vertical range even while clearly, physically "in" that room in plan.

Tier 1: test the element's raw representative point as extracted -
handles the large majority of cases (furniture/fixtures near floor
height, walls/pipes whose midpoint Z sits inside a normal room's
range).
Tier 2 (only on Tier-1 failure): rebuild the SAME X/Y with Z replaced
by that specific candidate room's own floor (Level.Elevation +
BuiltInParameter.ROOM_LOWER_OFFSET + a small epsilon) - a point known,
by construction, to sit inside that room's vertical range. Falls back
to the room's own Location.Point.Z (the pattern already proven in
health_checks.py's overlapping_rooms check) if ROOM_LOWER_OFFSET
doesn't resolve. Both tiers reuse the same X/Y, so Tier 2 can only fix
a Z problem - it can never turn a genuine XY-miss into a false
positive.

--------------------------------------------------------------------
Why candidate rooms are narrowed by Level before testing
--------------------------------------------------------------------
A project can have thousands of model elements and dozens/hundreds of
rooms - naive O(elements x rooms) IsPointInRoom calls do not scale.
Each element's Level is resolved (el.LevelId first, then a fallback
chain of BuiltInParameter names - ported from
DeeRehoster.pushbutton/script.py's _LEVEL_PARAM_CANDIDATES, the
established cross-category "which level is this on" solution in this
codebase), then only rooms on that level +/- one level are tested
first (Tier A) - absorbing the classic "ceiling hosted one level up,
large negative offset" case without per-project tuning. If the
element's level can't be resolved at all, OR Tier A finds zero match,
Tier B retries against EVERY room in the project before concluding
"no room found" - a narrowing miss costs performance for one element,
never correctness.

--------------------------------------------------------------------
Why boundary-defining elements (walls) are matched directly, not by
point testing
--------------------------------------------------------------------
A wall that separates two rooms has its LocationCurve midpoint sitting
ON the line between them - an inherently ambiguous case for a point-
in-polygon test (IsPointInRoom can miss it on both sides, or behave
inconsistently right on the boundary). Revit already computes exactly
which elements bound each room's boundary via
Room.GetBoundarySegments() - each returned BoundarySegment carries the
ElementId of whatever bounds it (a Wall, a room separation line,
another room; already used the same way in
DeeFinisher.pushbutton/DeeViewAdjust.pushbutton's own boundary-curve
extraction). find_room_by_boundary() looks an element's Id up in an
index built from this once per scan and, if it bounds a Room, returns
that Room directly - skipping point-testing entirely for exactly the
element type where a point test is least reliable. Only elements not
found in this index (interior partitions with a room on just one side
they don't bound, unrelated categories, etc.) fall through to the
point-based Tier A/B matching.

--------------------------------------------------------------------
Why some elements get several candidate points, not just one
--------------------------------------------------------------------
A Floor/Ceiling/Roof (or any element with neither a LocationPoint nor
a LocationCurve) falls back to its bounding-box center as a single
representative point. For a non-convex plan shape - an L-shaped or
Y-shaped room's floor is a completely ordinary, common case - the
bbox center can land in the "notch" outside the actual room/floor
material entirely, even though the element is unambiguously "in" that
room. get_representative_points() samples the bbox center plus its
four quarter-points instead of just the center; find_room_for_points()
tries each in turn (full Tier A/B narrowing per point) and returns the
first match. LocationPoint/LocationCurve-based elements are unaffected
- they still get exactly their own one, authoritative point.

--------------------------------------------------------------------
NEEDS LIVE-REVIT VERIFICATION (flagged, not silently assumed correct -
same policy as this extension's other geometry-heavy tools)
--------------------------------------------------------------------
- BuiltInParameter.ROOM_LOWER_OFFSET - not referenced anywhere else in
  this codebase (unlike ROOM_LEVEL_ID/ROOM_UPPER_OFFSET, confirmed
  live in DeeReLevel.pushbutton/script.py). Defended by safe getattr +
  a sanity fallback to the room's own Location.Point.Z.
- Rooms/Spaces/Areas are assumed to report CategoryType.Internal (not
  Model), so the CategoryType.Model filter naturally excludes them
  from being scanned as "elements to stamp" - a well-known fact, not
  yet directly tested in THIS codebase.
- Element.LookupParameter is assumed instance-only (never resolving a
  type parameter) across every category exercised here - not
  previously exercised in this codebase for a WRITE path at this
  breadth. If a type parameter were ever returned, one Set() would
  silently overwrite it for every instance of that type.
- Level-narrowing coverage for categories not in
  _LEVEL_PARAM_CANDIDATES (e.g. cable trays/conduits) falls through
  to the all-rooms fallback automatically - correct but slower.
- Multiple room matches (overlapping rooms, a real condition per
  health_checks.overlapping_rooms) - first match wins, documented,
  not yet validated as the expected tie-break by an actual user.
- A wall bounding two rooms picks whichever room's GetBoundarySegments
  happens to be processed first while building the index - same
  first-match-wins policy as the point-based path, not yet validated
  as the expected tie-break for a shared wall specifically.
- Room.GetBoundarySegments() reflects Revit's LAST-COMPUTED boundary
  for that room - if a room's boundary is stale (walls moved since it
  was last redrawn, a known, ordinary Revit modeling situation), the
  boundary-element index built here will be stale in exactly the same
  way and in the same places Revit's own room boundary already is.
"""
import time

from Autodesk.Revit.DB import (
    FilteredElementCollector, BuiltInCategory, BuiltInParameter, CategoryType,
    LocationPoint, LocationCurve, RevitLinkInstance, Transaction, StorageType,
    XYZ, Level, ElementId, SpatialElementBoundaryOptions, SpatialElementBoundaryLocation,
)
from Autodesk.Revit.DB.Architecture import Room

from pyrevit import script

output = script.get_output()

_Z_EPSILON = 0.1  # feet - lifted above a room's own floor for the Tier-2 retry

_LEVEL_PARAM_CANDIDATES = [
    "SCHEDULE_LEVEL_PARAM", "FAMILY_LEVEL_PARAM", "LEVEL_PARAM",
    "WALL_BASE_CONSTRAINT", "ROOF_BASE_LEVEL_PARAM", "ROOF_CONSTRAINT_LEVEL_PARAM",
    "STAIRS_BASE_LEVEL_PARAM", "RBS_START_LEVEL_PARAM",
]

_STATUS_NOT_FOUND = "not_found"
_STATUS_WRONG_TYPE = "wrong_type"
_STATUS_READ_ONLY = "read_only"
_STATUS_OK = "ok"


def _resolve_bip(name):
    return getattr(BuiltInParameter, name, None)


def _element_id_value(eid):
    """Dee.extension's own ElementId-across-Revit-versions compat
    helper (established convention across this codebase)."""
    if eid is None:
        return None
    try:
        return eid.Value
    except Exception:
        pass
    try:
        return eid.IntegerValue
    except Exception:
        return None


def _read_name(element):
    if element is None:
        return None
    try:
        n = element.Name
        return n if n else None
    except Exception:
        return None


def _read_room_name(room):
    """Room.Name is unreliable in IronPython for some rooms - confirmed
    live this session on a different tool (DeeViewAdjust). Falls back
    to the ROOM_NAME parameter directly, same proven pattern already
    established in DeeFinisher/DeeCleaner/DeeViewAdjust's own
    _read_room_name helpers."""
    try:
        n = room.Name
        if n:
            return n
    except Exception:
        pass
    try:
        p = room.get_Parameter(BuiltInParameter.ROOM_NAME)
        if p is not None:
            v = p.AsString()
            if v:
                return v
    except Exception:
        pass
    return "(unnamed)"


def _room_label(room):
    name = _read_room_name(room)
    number_text = ""
    try:
        p = room.get_Parameter(BuiltInParameter.ROOM_NUMBER)
        number_text = p.AsString() if p is not None else ""
    except Exception:
        number_text = ""
    if number_text:
        return "{0} - {1}".format(number_text, name)
    return name


# ==========================================================================
# Representative point for an arbitrary element
# ==========================================================================
def _get_model_bounding_box(doc, element):
    try:
        bbox = element.get_BoundingBox(None)
        if bbox is not None:
            return bbox
    except Exception:
        pass
    try:
        view = doc.ActiveView
        if view is not None:
            return element.get_BoundingBox(view)
    except Exception:
        pass
    return None


def _sample_bbox_points(bbox):
    """Center plus the four quarter-points of a bounding box's
    footprint, all at the bbox's own mid-height - see module docstring
    ("Why some elements get several candidate points"). Z barely
    matters here: whichever candidate room ends up tested against
    these points gets its Z corrected by the Tier-2 retry regardless -
    what matters is giving XY more than one chance to land on actual
    material for a non-convex plan shape."""
    try:
        minp, maxp = bbox.Min, bbox.Max
    except Exception:
        return []
    midz = (minp.Z + maxp.Z) * 0.5
    fractions = ((0.5, 0.5), (0.25, 0.25), (0.75, 0.25), (0.25, 0.75), (0.75, 0.75))
    pts = []
    for fx, fy in fractions:
        try:
            pts.append(XYZ(
                minp.X + (maxp.X - minp.X) * fx,
                minp.Y + (maxp.Y - minp.Y) * fy,
                midz))
        except Exception:
            continue
    return pts


def get_representative_points(doc, element):
    """Returns a list of candidate XYZ points to test this element
    against room boundaries. LocationPoint/LocationCurve-based
    elements get exactly their own one, authoritative point;
    everything else (Floors, Ceilings, Roofs, and any element with
    neither location type) gets several bbox samples instead of just
    the center - see module docstring."""
    try:
        loc = element.Location
    except Exception:
        loc = None
    if isinstance(loc, LocationPoint):
        try:
            return [loc.Point]
        except Exception:
            pass
    if isinstance(loc, LocationCurve):
        try:
            return [loc.Curve.Evaluate(0.5, True)]
        except Exception:
            pass
    bbox = _get_model_bounding_box(doc, element)
    if bbox is not None:
        pts = _sample_bbox_points(bbox)
        if pts:
            return pts
    return []


# ==========================================================================
# Level resolution for arbitrary categories (ported candidate list from
# DeeRehoster.pushbutton/script.py's _LEVEL_PARAM_CANDIDATES)
# ==========================================================================
def _resolve_element_level_id(el):
    try:
        lid = el.LevelId
        if lid is not None and lid != ElementId.InvalidElementId:
            return lid
    except Exception:
        pass
    for name in _LEVEL_PARAM_CANDIDATES:
        bip = _resolve_bip(name)
        if bip is None:
            continue
        try:
            p = el.get_Parameter(bip)
            if p is not None and p.StorageType == StorageType.ElementId:
                lid = p.AsElementId()
                if lid is not None and lid != ElementId.InvalidElementId:
                    return lid
        except Exception:
            continue
    return None


# ==========================================================================
# Room index - grouped by level, sorted level list, safe-Z helper per room
# ==========================================================================
class RoomIndex(object):
    def __init__(self):
        self.rooms_by_level_id = {}      # {level_id_value: [Room, ...]}
        self.sorted_level_ids = []       # level id values, sorted by elevation
        self.level_name_by_id = {}       # {level_id_value: level name}
        self.all_rooms = []
        self.boundary_element_to_rooms = {}   # {element_id_value: [Room, ...]}


def _room_safe_z(room):
    """A Z value known, by construction, to sit inside `room`'s own
    vertical range - used for the Tier-2 retry."""
    try:
        lvl = room.Level
        base_z = lvl.Elevation if lvl is not None else 0.0
    except Exception:
        base_z = 0.0
    try:
        p = room.get_Parameter(BuiltInParameter.ROOM_LOWER_OFFSET)
        offset = p.AsDouble() if p is not None else 0.0
    except Exception:
        offset = 0.0
    z = base_z + offset + _Z_EPSILON
    try:
        loc = room.Location
        if isinstance(loc, LocationPoint) and abs(z - loc.Point.Z) > 1000.0:
            # computed Z looks unreasonable - the room's own location
            # point (proven live via health_checks.overlapping_rooms)
            # is a safer bet than a bad offset read.
            return loc.Point.Z + _Z_EPSILON
    except Exception:
        pass
    return z


def build_room_index(doc):
    index = RoomIndex()
    level_elev = {}
    for lvl in FilteredElementCollector(doc).OfClass(Level):
        key = _element_id_value(lvl.Id)
        if key is None:
            continue
        try:
            level_elev[key] = lvl.Elevation
        except Exception:
            level_elev[key] = 0.0
        index.level_name_by_id[key] = _read_name(lvl) or "(unnamed level)"

    boundary_opts = SpatialElementBoundaryOptions()
    try:
        boundary_opts.SpatialElementBoundaryLocation = SpatialElementBoundaryLocation.Finish
    except Exception:
        pass

    for r in FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_Rooms).WhereElementIsNotElementType():
        try:
            if r.Area <= 0 or r.Location is None:
                continue
        except Exception:
            continue
        index.all_rooms.append(r)
        try:
            key = _element_id_value(r.LevelId)
        except Exception:
            key = None
        index.rooms_by_level_id.setdefault(key, []).append(r)

        # boundary-element association (walls, room separation lines,
        # etc.) - see module docstring, used to match a wall aligned
        # with the room boundary directly, without any point test.
        try:
            loops = r.GetBoundarySegments(boundary_opts)
        except Exception:
            loops = None
        if loops:
            for loop in loops:
                for seg in loop:
                    try:
                        seg_id = seg.ElementId
                    except Exception:
                        continue
                    if seg_id is None or seg_id == ElementId.InvalidElementId:
                        continue
                    seg_key = _element_id_value(seg_id)
                    if seg_key is None:
                        continue
                    index.boundary_element_to_rooms.setdefault(seg_key, []).append(r)

    index.sorted_level_ids = sorted(
        [k for k in index.rooms_by_level_id.keys() if k is not None],
        key=lambda k: level_elev.get(k, 0.0))
    return index


# ==========================================================================
# Room matching - Tier A/B narrowing, Tier 1/2 Z retry
# ==========================================================================
def _tier_a_candidates(element_level_id, room_index):
    key = _element_id_value(element_level_id) if element_level_id is not None else None
    if key is None or key not in room_index.sorted_level_ids:
        return []
    idx = room_index.sorted_level_ids.index(key)
    band = room_index.sorted_level_ids[max(0, idx - 1): idx + 2]
    out = []
    for k in band:
        out.extend(room_index.rooms_by_level_id.get(k, []))
    return out


def _test_candidates(pt, rooms):
    for room in rooms:
        try:
            if room.IsPointInRoom(pt):
                return room
        except Exception:
            continue
    # Tier 2 - retry with a Z known to sit inside each room's own range
    for room in rooms:
        try:
            safe_pt = XYZ(pt.X, pt.Y, _room_safe_z(room))
            if room.IsPointInRoom(safe_pt):
                return room
        except Exception:
            continue
    return None


class MatchDiagnostics(object):
    """Populated by find_room_for_points on a miss, so a "no room found"
    row can say WHY instead of leaving it a black box - level
    resolution failure, an empty narrowed candidate set, and a genuine
    geometry miss against every room in the project all look identical
    from the outside otherwise, and each points to a different fix."""
    def __init__(self):
        self.point_missing = False
        self.level_resolved = False
        self.level_has_nearby_rooms = False
        self.tier_a_count = 0
        self.tier_b_count = 0
        self.used_full_fallback = False

    @property
    def reason_text(self):
        if self.point_missing:
            return "No room found - could not determine this element's location"
        if not self.level_resolved:
            return "No room found - element's Level could not be resolved ({0} room(s) checked project-wide)".format(
                self.tier_b_count)
        if not self.level_has_nearby_rooms:
            return "No room found - no Rooms exist near this element's Level ({0} room(s) checked project-wide)".format(
                self.tier_b_count)
        if self.used_full_fallback:
            return "No room found - checked {0} nearby + {1} project-wide room(s), no match".format(
                self.tier_a_count, self.tier_b_count)
        return "No room found - checked {0} nearby room(s), no match".format(self.tier_a_count)


def find_room_by_boundary(element, room_index):
    """Direct match via Revit's own room-boundary computation - see
    module docstring ("Why boundary-defining elements are matched
    directly, not by point testing"). Returns the first Room this
    element bounds, or None if it doesn't bound any room (an interior
    partition with a room on only one side it doesn't bound, an
    exterior wall, an unrelated category, etc.) - callers fall through
    to point-based matching in that case."""
    try:
        key = _element_id_value(element.Id)
    except Exception:
        return None
    rooms = room_index.boundary_element_to_rooms.get(key)
    if not rooms:
        return None
    return rooms[0]


def find_room_for_points(points, element_level_id, room_index):
    """Returns (room_or_None, MatchDiagnostics). Tries each candidate
    point in `points` in turn against the Tier A candidate set first,
    then - only if none of them matched - against every room in the
    project (Tier B). See module docstring for the Tier A/B narrowing,
    Tier 1/2 Z-retry, and multi-point rationale."""
    diag = MatchDiagnostics()
    if not points:
        diag.point_missing = True
        return None, diag

    key = _element_id_value(element_level_id) if element_level_id is not None else None
    diag.level_resolved = key is not None
    candidates = _tier_a_candidates(element_level_id, room_index)
    diag.tier_a_count = len(candidates)
    diag.level_has_nearby_rooms = len(candidates) > 0

    if candidates:
        for pt in points:
            match = _test_candidates(pt, candidates)
            if match is not None:
                return match, diag

    # Tier B - element level unresolved, Tier A had no candidates, or
    # none of Tier A's candidates matched any candidate point - a
    # narrowing miss must never cost correctness, only performance for
    # this one element.
    diag.used_full_fallback = True
    diag.tier_b_count = len(room_index.all_rooms)
    for pt in points:
        match = _test_candidates(pt, room_index.all_rooms)
        if match is not None:
            return match, diag
    return None, diag


# ==========================================================================
# Parameter validation
# ==========================================================================
def validate_parameter(element, param_name):
    try:
        p = element.LookupParameter(param_name)
    except Exception:
        p = None
    if p is None:
        return _STATUS_NOT_FOUND, None
    try:
        if p.StorageType != StorageType.String:
            return _STATUS_WRONG_TYPE, p
    except Exception:
        return _STATUS_WRONG_TYPE, p
    try:
        if p.IsReadOnly:
            return _STATUS_READ_ONLY, p
    except Exception:
        pass
    return _STATUS_OK, p


# ==========================================================================
# Pass 1 - project-wide element cache + distinct String-parameter names
# ==========================================================================
class _CachedElement(object):
    def __init__(self, element, points, level_id, category_name):
        self.element = element
        self.points = points
        self.level_id = level_id
        self.category_name = category_name


def collect_string_param_universe_and_elements(doc, progress_cb=None):
    """One upfront full-project pass (DeeRehoster's own scan skeleton:
    WhereElementIsNotElementType, skip RevitLinkInstance, skip anything
    whose Category isn't CategoryType.Model - this last filter is also
    what naturally excludes Rooms/Spaces/Areas from being scanned as
    "elements to stamp"). For every surviving element, caches its
    representative point + resolved level id once (reused regardless
    of which parameter gets picked later), and collects every
    StorageType.String parameter name seen on any element (readable or
    not - a read-only one a user later picks still surfaces an honest
    per-element skip reason instead of silently never appearing in the
    picker). Returns (sorted_name_list, cached_elements)."""
    collector = list(FilteredElementCollector(doc).WhereElementIsNotElementType())
    total = len(collector)
    names = set()
    cached = []
    for i, el in enumerate(collector):
        if progress_cb is not None and progress_cb(i, total):
            break
        try:
            if isinstance(el, RevitLinkInstance):
                continue
            cat = el.Category
            if cat is None:
                continue
            try:
                if cat.CategoryType != CategoryType.Model:
                    continue
            except Exception:
                continue

            pts = get_representative_points(doc, el)
            level_id = _resolve_element_level_id(el)
            cat_name = _read_name(cat) or "(unknown category)"
            cached.append(_CachedElement(el, pts, level_id, cat_name))

            for p in el.Parameters:
                try:
                    if p.StorageType == StorageType.String:
                        pname = p.Definition.Name
                        if pname:
                            names.add(pname)
                except Exception:
                    continue
        except Exception:
            continue
    return sorted(names), cached


# ==========================================================================
# Pass 2 - scan the cache for one chosen parameter, build preview rows
# ==========================================================================
class PreviewRow(object):
    _STATUS_TEXT = {
        "will_update": "Will update",
        "already_correct": "Already correct - no change",
        "no_room": "No room found",
        "not_string": "Parameter is not a text value",
        "read_only": "Parameter is read-only",
    }

    def __init__(self, cached, status_key, current_value, found_room_label, new_value, level_label,
                 status_detail=None):
        self.element = cached.element
        self.id_text = str(_element_id_value(cached.element.Id))
        self.checked = status_key == "will_update"
        self.category_name = cached.category_name
        self.level_label = level_label
        self.current_value = current_value or ""
        self.found_room_label = found_room_label or ""
        self.new_value = new_value or ""
        self.status_key = status_key
        self._status_detail = status_detail

    @property
    def status_text(self):
        if self._status_detail:
            return self._status_detail
        return self._STATUS_TEXT.get(self.status_key, self.status_key)


def scan_for_parameter(param_name, cached_elements, room_index, progress_cb=None):
    rows = []
    total = len(cached_elements)
    for i, cached in enumerate(cached_elements):
        if progress_cb is not None and progress_cb(i, total):
            break

        status, param = validate_parameter(cached.element, param_name)
        if status == _STATUS_NOT_FOUND:
            continue

        level_label = room_index.level_name_by_id.get(
            _element_id_value(cached.level_id), "(none)")

        current_value = ""
        if param is not None:
            try:
                current_value = param.AsString() or ""
            except Exception:
                current_value = ""

        if status == _STATUS_WRONG_TYPE:
            rows.append(PreviewRow(cached, "not_string", current_value, "", current_value, level_label))
            continue
        if status == _STATUS_READ_ONLY:
            rows.append(PreviewRow(cached, "read_only", current_value, "", current_value, level_label))
            continue

        room = find_room_by_boundary(cached.element, room_index)
        diag = None
        if room is None:
            room, diag = find_room_for_points(cached.points, cached.level_id, room_index)
        if room is None:
            rows.append(PreviewRow(cached, "no_room", current_value, "", current_value, level_label,
                                   status_detail=diag.reason_text if diag is not None else None))
            continue

        room_label = _room_label(room)
        if room_label == current_value:
            rows.append(PreviewRow(cached, "already_correct", current_value, room_label, room_label, level_label))
        else:
            rows.append(PreviewRow(cached, "will_update", current_value, room_label, room_label, level_label))
    return rows


# ==========================================================================
# Apply / report
# ==========================================================================
class ApplyResult(object):
    def __init__(self):
        self.ok_count = 0
        self.skipped = []  # list of (id_text, reason)
        self.elapsed_seconds = 0.0


def apply_rows(doc, rows, param_name):
    start = time.time()
    result = ApplyResult()
    t = Transaction(doc, "DeeRoomStamp - Apply Room Names")
    t.Start()
    try:
        for row in rows:
            try:
                p = row.element.LookupParameter(param_name)
                if p is None or p.IsReadOnly or p.StorageType != StorageType.String:
                    result.skipped.append((row.id_text, "Parameter no longer valid on this element"))
                    continue
                p.Set(row.new_value)
                result.ok_count += 1
            except Exception as e:
                result.skipped.append((row.id_text, str(e)))
        t.Commit()
    except Exception:
        t.RollBack()
        raise
    result.elapsed_seconds = time.time() - start
    return result


def print_report(result):
    html = ['<h2 style="font-family:sans-serif;color:#ddd;">DeeRoomStamp - Apply Results</h2>',
            '<p style="color:#ddd;">{0} updated, {1} skipped - {2:.2f}s.</p>'.format(
                result.ok_count, len(result.skipped), result.elapsed_seconds)]
    for label, reason in result.skipped:
        html.append(
            '<div style="padding:4px 10px;margin:2px 0;background:#c62828;color:#fff;'
            'border-radius:4px;font-family:monospace;font-size:12px;">'
            '&#10007;&nbsp; <b>{0}</b> &mdash; {1}</div>'.format(label, reason))
    output.print_html("".join(html))
