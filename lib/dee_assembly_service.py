# -*- coding: utf-8 -*-
"""
dee_assembly_service
All scan / layout / create logic for DeeAssemb - the batch
assembly-views-and-sheet builder. The pushbutton's script.py only wires
the WPF window to this module (same split already used by
DeeRoomStamp -> dee_room_stamp_service and DeeQs -> dee_qs_service).

Revit API surface used here, verified against the official signatures
before writing rather than guessed:

  AssemblyViewUtils.Create3DOrthographic(Document, ElementId)                     -> View3D
  AssemblyViewUtils.Create3DOrthographic(Document, ElementId, ElementId, bool)    -> View3D
  AssemblyViewUtils.CreateDetailSection(Document, ElementId,
                                        AssemblyDetailViewOrientation)            -> ViewSection
  AssemblyViewUtils.CreateDetailSection(Document, ElementId,
                                        AssemblyDetailViewOrientation,
                                        ElementId, bool)                          -> ViewSection
  AssemblyViewUtils.CreatePartList(Document, ElementId[, ElementId, bool])        -> ViewSchedule
  AssemblyViewUtils.CreateMaterialTakeoff(Document, ElementId[, ElementId, bool]) -> ViewSchedule
  AssemblyViewUtils.CreateSingleCategorySchedule(Document, ElementId, ElementId
                                                 [, ElementId, bool])             -> ViewSchedule
  AssemblyViewUtils.CreateSheet(Document, ElementId, ElementId titleBlockId)      -> ViewSheet
  AssemblyDetailViewOrientation: 9 members - DetailSectionA, DetailSectionB,
      ElevationBack, ElevationBottom, ElevationFront, ElevationLeft,
      ElevationRight, ElevationTop, HorizontalDetail.
  View.AssociatedAssemblyInstanceId (read-only ElementId) - how an assembly
      view/sheet is tied back to its AssemblyInstance.
  Viewport.Create / CanAddViewToSheet / GetBoxOutline / SetBoxCenter
      (already proven live in DeeAligner + DeeView).
  ScheduleSheetInstance.Create(Document, ElementId sheetId,
      ElementId scheduleId, XYZ origin) - schedules are NOT Viewports and
      must be placed with this instead; position is its .Point property.

The isAssigned=False overload argument matters: passing True ASSIGNS the
view template to the created view, which locks View.Scale and therefore
makes the auto-fit scaling below a no-op. The window exposes that as an
explicit choice rather than silently picking one.

NEEDS LIVE-REVIT VERIFICATION (flagged, not silently assumed correct):
  - That changing View.Scale AFTER its Viewport exists updates the
    viewport box on regeneration (this is what the auto-fit loop relies
    on; it re-measures rather than trusting the arithmetic, so a wrong
    assumption degrades to "left at the probe scale", not a crash).
  - ScheduleSheetInstance.Point's anchor corner is not documented. The
    code here never assumes one: it places, measures the resulting
    bounding box, and corrects by the measured delta - the same
    anchor-agnostic trick DeeAligner already uses for Image resizing.
  - Whether AssemblyViewUtils.CreateSheet auto-assigns a sheet number
    that collides with an existing sheet in a busy project (the rename
    is wrapped so a collision keeps Revit's own number instead of
    failing the whole assembly).
"""
from Autodesk.Revit.DB import (
    FilteredElementCollector, BuiltInCategory, ElementId, XYZ,
    Transaction, View, ViewSheet, ViewSchedule, Viewport, ScheduleSheetInstance,
    AssemblyInstance, AssemblyViewUtils, AssemblyDetailViewOrientation,
    UnitUtils, UnitTypeId,
)


# --------------------------------------------------------------------------
# view kinds
# --------------------------------------------------------------------------
# (key, label, kind) - kind drives BOTH how the view is created and how it
# is placed on the sheet ("schedule" -> ScheduleSheetInstance, everything
# else -> Viewport).
VIEW_KINDS = [
    ("3d",              "3D Orthographic",          "3d"),
    ("elev_front",      "Elevation Front",          "section"),
    ("elev_back",       "Elevation Back",           "section"),
    ("elev_left",       "Elevation Left",           "section"),
    ("elev_right",      "Elevation Right",          "section"),
    ("elev_top",        "Elevation Top",            "section"),
    ("elev_bottom",     "Elevation Bottom",         "section"),
    ("horiz_detail",    "Horizontal Detail",        "section"),
    ("detail_a",        "Detail Section A",         "section"),
    ("detail_b",        "Detail Section B",         "section"),
    ("part_list",       "Part List",                "schedule"),
    ("takeoff",         "Material Takeoff",         "schedule"),
    ("cat_schedule",    "Single-Category Schedule", "schedule"),
]

VIEW_KIND_LABELS = dict((k, lbl) for k, lbl, _ in VIEW_KINDS)
VIEW_KIND_TYPES = dict((k, t) for k, _, t in VIEW_KINDS)


def _orientation_map():
    """Built lazily-by-name so a Revit version missing one member degrades to
    "that orientation is unavailable" instead of an import-time crash."""
    names = {
        "elev_front": "ElevationFront",
        "elev_back": "ElevationBack",
        "elev_left": "ElevationLeft",
        "elev_right": "ElevationRight",
        "elev_top": "ElevationTop",
        "elev_bottom": "ElevationBottom",
        "horiz_detail": "HorizontalDetail",
        "detail_a": "DetailSectionA",
        "detail_b": "DetailSectionB",
    }
    out = {}
    for key in names:
        try:
            out[key] = getattr(AssemblyDetailViewOrientation, names[key])
        except AttributeError:
            continue
    return out


ORIENTATIONS = _orientation_map()


# --------------------------------------------------------------------------
# pure layout math (no Revit) - unit-tested at the bottom of this file
# --------------------------------------------------------------------------
STANDARD_SCALES = [1, 2, 5, 10, 20, 25, 50, 75, 100, 125, 150, 200, 250,
                   500, 1000, 2000, 5000]


def next_standard_scale(value):
    """Smallest standard Revit scale denominator >= value. Rounding the
    denominator UP always makes the drawing SMALLER, so a fitted view can
    never overflow its cell because of the rounding itself."""
    if value is None or value <= 0:
        return STANDARD_SCALES[0]
    for s in STANDARD_SCALES:
        if s >= value - 1e-9:
            return s
    return STANDARD_SCALES[-1]


def compute_cells(bounds, rows, cols, margin, spacing_h, spacing_v):
    """bounds: (min_x, max_x, min_y, max_y) of the sheet, in feet.
    Returns cells[row][col] = (min_x, max_x, min_y, max_y), row 0 at the TOP
    (matching how the layout designer draws it and how people read a sheet).
    Returns [] if the requested grid cannot fit in the space left over."""
    rows = int(rows)
    cols = int(cols)
    if rows < 1 or cols < 1:
        return []
    min_x, max_x, min_y, max_y = bounds
    avail_w = (max_x - min_x) - 2.0 * margin - spacing_h * (cols - 1)
    avail_h = (max_y - min_y) - 2.0 * margin - spacing_v * (rows - 1)
    if avail_w <= 0 or avail_h <= 0:
        return []
    cell_w = avail_w / cols
    cell_h = avail_h / rows
    cells = []
    for r in range(rows):
        row_cells = []
        top = max_y - margin - r * (cell_h + spacing_v)
        for c in range(cols):
            left = min_x + margin + c * (cell_w + spacing_h)
            row_cells.append((left, left + cell_w, top - cell_h, top))
        cells.append(row_cells)
    return cells


def slot_bounds(cells, row, col, row_span=1, col_span=1):
    """Merged bounds of a slot spanning several cells. The span is clamped to
    the grid, so a slot left over from a bigger grid never raises."""
    if not cells:
        return None
    rows = len(cells)
    cols = len(cells[0])
    if row < 0 or col < 0 or row >= rows or col >= cols:
        return None
    r1 = min(rows - 1, row + max(1, int(row_span)) - 1)
    c1 = min(cols - 1, col + max(1, int(col_span)) - 1)
    x0 = cells[row][col][0]
    x1 = cells[row][c1][1]
    y1 = cells[row][col][3]
    y0 = cells[r1][c1][2]
    return (x0, x1, y0, y1)


def auto_arrange(keys, rows, cols):
    """Fills the grid left-to-right, top-to-bottom. Returns
    {key: (row, col, row_span, col_span)}. Keys past the last cell are left
    unplaced (reported, never silently dropped)."""
    placed = {}
    capacity = rows * cols
    for i, key in enumerate(keys):
        if i >= capacity:
            break
        placed[key] = (i // cols, i % cols, 1, 1)
    return placed


def normalize_slots(slots, keys, rows, cols):
    """Drops slots for unchecked keys, clamps every slot into the current
    grid, and auto-places any checked key that has no slot yet (into the
    first free cell, else the last cell). Guarantees the caller a slot for
    every checked key, so nothing silently fails to get placed."""
    out = {}
    for key in keys:
        s = slots.get(key)
        if s is None:
            continue
        r, c, rs, cs = s
        r = max(0, min(rows - 1, int(r)))
        c = max(0, min(cols - 1, int(c)))
        rs = max(1, min(rows - r, int(rs)))
        cs = max(1, min(cols - c, int(cs)))
        out[key] = (r, c, rs, cs)
    occupied = set()
    for slot in out.values():
        r, c, rs, cs = slot
        for rr in range(r, r + rs):
            for cc in range(c, c + cs):
                occupied.add((rr, cc))
    for key in keys:
        if key in out:
            continue
        spot = None
        for r in range(rows):
            for c in range(cols):
                if (r, c) not in occupied:
                    spot = (r, c)
                    break
            if spot is not None:
                break
        if spot is None:
            spot = (rows - 1, cols - 1)
        out[key] = (spot[0], spot[1], 1, 1)
        occupied.add(spot)
    return out


def cell_at_point(cells, x, y):
    """(row, col) of the cell containing the point, else None. Used by the
    layout designer to turn a click/drop on the preview canvas into a cell."""
    for r, row in enumerate(cells):
        for c, (x0, x1, y0, y1) in enumerate(row):
            if x0 <= x <= x1 and y0 <= y <= y1:
                return (r, c)
    return None


def mm_to_ft(mm):
    try:
        return UnitUtils.ConvertToInternalUnits(float(mm), UnitTypeId.Millimeters)
    except Exception:
        # 1 ft = 304.8 mm - only reached if UnitTypeId is unavailable.
        return float(mm) / 304.8


def ft_to_mm(ft):
    try:
        return UnitUtils.ConvertFromInternalUnits(float(ft), UnitTypeId.Millimeters)
    except Exception:
        return float(ft) * 304.8


# --------------------------------------------------------------------------
# scanning
# --------------------------------------------------------------------------
class AssemblyRow(object):
    """Holds ElementIds and plain strings ONLY - never a live Element. This
    tool opens a window and waits on the user between the scan and the run,
    which is exactly the situation where a cached Element wrapper goes stale
    and takes Revit down with an uncatchable native error."""

    def __init__(self, element_id, name, type_name, member_count,
                 view_count, sheet_count):
        self.element_id = element_id
        self.name = name
        self.type_name = type_name
        self.member_count = member_count
        self.view_count = view_count
        self.sheet_count = sheet_count
        self.selected = False

    @property
    def has_existing(self):
        return self.view_count > 0 or self.sheet_count > 0

    @property
    def is_ready(self):
        return not self.has_existing

    @property
    def status(self):
        if self.sheet_count > 0 and self.view_count > 0:
            return "Skip - has {0} view(s) and a sheet".format(self.view_count)
        if self.sheet_count > 0:
            return "Skip - already has a sheet"
        if self.view_count > 0:
            return "Skip - already has {0} view(s)".format(self.view_count)
        return "Ready"


def _safe_name(element):
    try:
        n = element.Name
        if n:
            return n
    except Exception:
        pass
    return "(unnamed)"


def index_assembly_views(doc):
    """One pass over every View in the document (ViewSheet is itself a View,
    so this catches both), bucketed by the assembly that owns it. View
    templates are excluded - a template is never an assembly view."""
    views_by_assembly = {}
    sheets_by_assembly = {}
    for v in FilteredElementCollector(doc).OfClass(View):
        try:
            if v.IsTemplate:
                continue
            aid = v.AssociatedAssemblyInstanceId
        except Exception:
            continue
        if aid is None or aid == ElementId.InvalidElementId:
            continue
        key = aid.IntegerValue
        if isinstance(v, ViewSheet):
            sheets_by_assembly.setdefault(key, []).append(v.Id)
        else:
            views_by_assembly.setdefault(key, []).append(v.Id)
    return views_by_assembly, sheets_by_assembly


def collect_assemblies(doc, progress_cb=None):
    """Returns a list of AssemblyRow, sorted by type then name.

    Collected via OfCategory(OST_Assemblies) rather than OfClass - the same
    defensive choice this codebase already learned the hard way with
    Room/Space/Area, where OfClass throws at runtime."""
    try:
        elements = list(FilteredElementCollector(doc)
                        .OfCategory(BuiltInCategory.OST_Assemblies)
                        .WhereElementIsNotElementType())
    except Exception:
        elements = list(FilteredElementCollector(doc).OfClass(AssemblyInstance))

    views_by_assembly, sheets_by_assembly = index_assembly_views(doc)

    rows = []
    total = max(len(elements), 1)
    for i, el in enumerate(elements):
        if progress_cb is not None and progress_cb(i, total):
            break
        if not isinstance(el, AssemblyInstance):
            continue
        try:
            key = el.Id.IntegerValue
            try:
                type_name = el.AssemblyTypeName or ""
            except Exception:
                type_name = ""
            try:
                member_count = len(list(el.GetMemberIds()))
            except Exception:
                member_count = 0
            rows.append(AssemblyRow(
                element_id=el.Id,
                name=_safe_name(el),
                type_name=type_name,
                member_count=member_count,
                view_count=len(views_by_assembly.get(key, [])),
                sheet_count=len(sheets_by_assembly.get(key, [])),
            ))
        except Exception:
            continue
    rows.sort(key=lambda r: (r.type_name.lower(), r.name.lower()))
    return rows


def _param_mm(element, name):
    try:
        p = element.LookupParameter(name)
        if p is None:
            return None
        return ft_to_mm(p.AsDouble())
    except Exception:
        return None


def collect_titleblocks(doc):
    """(ElementId, label, width_mm, height_mm) for every title block TYPE.
    Width/height come from the type's own "Sheet Width"/"Sheet Height"
    parameters and are used ONLY to draw the layout preview at the right
    aspect ratio - the real placement always measures the actual
    ViewSheet.Outline instead, so a missing parameter costs a nicer preview,
    never a wrong layout."""
    out = []
    for sym in (FilteredElementCollector(doc)
                .OfCategory(BuiltInCategory.OST_TitleBlocks)
                .WhereElementIsElementType()):
        try:
            fam = ""
            try:
                fam = sym.Family.Name
            except Exception:
                pass
            label = "{0} : {1}".format(fam, _safe_name(sym)) if fam else _safe_name(sym)
            out.append((sym.Id, label, _param_mm(sym, "Sheet Width"),
                        _param_mm(sym, "Sheet Height")))
        except Exception:
            continue
    out.sort(key=lambda t: t[1].lower())
    return out


def collect_view_templates(doc):
    """(ElementId, label) for view templates, split into the ones that apply
    to model views and the ones that apply to schedules - Revit rejects a
    schedule template on a 3D view and vice versa."""
    model_templates = []
    schedule_templates = []
    for v in FilteredElementCollector(doc).OfClass(View):
        try:
            if not v.IsTemplate:
                continue
        except Exception:
            continue
        entry = (v.Id, _safe_name(v))
        if isinstance(v, ViewSchedule):
            schedule_templates.append(entry)
        else:
            model_templates.append(entry)
    model_templates.sort(key=lambda t: t[1].lower())
    schedule_templates.sort(key=lambda t: t[1].lower())
    return model_templates, schedule_templates


def collect_schedulable_categories(doc):
    """Model categories that can carry parameters - the ones valid for
    AssemblyViewUtils.CreateSingleCategorySchedule, so the combo cannot
    offer something Revit will reject outright."""
    out = []
    try:
        for cat in doc.Settings.Categories:
            try:
                if not cat.AllowsBoundParameters:
                    continue
                if str(cat.CategoryType) != "Model":
                    continue
                out.append((cat.Id, cat.Name))
            except Exception:
                continue
    except Exception:
        pass
    out.sort(key=lambda t: t[1].lower())
    return out


# --------------------------------------------------------------------------
# creation
# --------------------------------------------------------------------------
class AssemblyResult(object):
    def __init__(self, name):
        self.name = name
        self.sheet_label = ""
        self.created = []      # (label, detail)
        self.warnings = []
        self.error = ""

    @property
    def ok(self):
        return not self.error


class BuildOptions(object):
    """Everything the run needs, resolved to plain values / ElementIds by the
    window before the transaction starts."""

    def __init__(self):
        self.view_keys = []
        self.slots = {}
        self.rows = 2
        self.cols = 2
        self.margin_mm = 10.0
        self.spacing_h_mm = 5.0
        self.spacing_v_mm = 5.0
        self.titleblock_id = None
        self.model_template_id = None
        self.schedule_template_id = None
        self.assign_template = False
        self.schedule_category_id = None
        self.sheet_number_rule = None    # NamingRule or None
        self.sheet_name_rule = None      # NamingRule or None
        # key -> fixed scale denominator, or None for auto-fit.
        self.scales = {}
        self.probe_scale = 50


def _create_view(doc, assembly_id, key, opts):
    """Returns (view_or_None, detail). Uses the 4-argument overloads only
    when a template was chosen, so the 2-argument path stays exactly what
    Revit's own Assembly UI does."""
    kind = VIEW_KIND_TYPES.get(key)
    tpl = opts.schedule_template_id if kind == "schedule" else opts.model_template_id
    assigned = bool(opts.assign_template)

    if key == "3d":
        if tpl is not None:
            return AssemblyViewUtils.Create3DOrthographic(doc, assembly_id, tpl, assigned), ""
        return AssemblyViewUtils.Create3DOrthographic(doc, assembly_id), ""

    if kind == "section":
        orientation = ORIENTATIONS.get(key)
        if orientation is None:
            return None, "orientation not available in this Revit version"
        if tpl is not None:
            return AssemblyViewUtils.CreateDetailSection(
                doc, assembly_id, orientation, tpl, assigned), ""
        return AssemblyViewUtils.CreateDetailSection(doc, assembly_id, orientation), ""

    if key == "part_list":
        if tpl is not None:
            return AssemblyViewUtils.CreatePartList(doc, assembly_id, tpl, assigned), ""
        return AssemblyViewUtils.CreatePartList(doc, assembly_id), ""

    if key == "takeoff":
        if tpl is not None:
            return AssemblyViewUtils.CreateMaterialTakeoff(doc, assembly_id, tpl, assigned), ""
        return AssemblyViewUtils.CreateMaterialTakeoff(doc, assembly_id), ""

    if key == "cat_schedule":
        if opts.schedule_category_id is None:
            return None, "no category chosen for the single-category schedule"
        if tpl is not None:
            return AssemblyViewUtils.CreateSingleCategorySchedule(
                doc, assembly_id, opts.schedule_category_id, tpl, assigned), ""
        return AssemblyViewUtils.CreateSingleCategorySchedule(
            doc, assembly_id, opts.schedule_category_id), ""

    return None, "unknown view kind '{0}'".format(key)


def _try_scale(view):
    try:
        return view.Scale
    except Exception:
        return "?"


def _fit_and_place_viewport(doc, sheet_id, view_id, cell, probe_scale, warnings,
                            label, fixed_scale=None):
    """Places the view and positions it in its cell.

    fixed_scale=None  -> auto-fit: measure the real viewport box, then pick the
                         largest standard scale that still fits, and re-measure.
    fixed_scale=<int> -> use exactly that scale. It is still measured, and an
                         overflow is reported rather than silently allowed, but
                         the scale the user asked for is never overridden.

    Measuring rather than computing from CropBox is deliberate: the viewport
    box includes the view title, and the crop box's paper-space units are
    ambiguous across Revit versions. What lands on the sheet is what gets
    measured."""
    view = doc.GetElement(view_id)
    cx = (cell[0] + cell[1]) / 2.0
    cy = (cell[2] + cell[3]) / 2.0
    cell_w = cell[1] - cell[0]
    cell_h = cell[3] - cell[2]

    try:
        if not Viewport.CanAddViewToSheet(doc, sheet_id, view_id):
            warnings.append("{0}: Revit will not allow this view on the sheet".format(label))
            return None
    except Exception:
        pass

    scale_locked = False
    wanted = int(fixed_scale) if fixed_scale else (int(probe_scale) if probe_scale else None)
    if wanted:
        try:
            view.Scale = wanted
        except Exception:
            scale_locked = True

    vp = Viewport.Create(doc, sheet_id, view_id, XYZ(cx, cy, 0))
    doc.Regenerate()

    def measured():
        o = vp.GetBoxOutline()
        return (o.MaximumPoint.X - o.MinimumPoint.X,
                o.MaximumPoint.Y - o.MinimumPoint.Y)

    w, h = measured()
    if fixed_scale and scale_locked:
        warnings.append(
            "{0}: could not set the requested 1:{1} - the view's scale is locked "
            "by an assigned view template".format(label, int(fixed_scale)))
    if (not fixed_scale and not scale_locked
            and w > 1e-9 and h > 1e-9 and cell_w > 0 and cell_h > 0):
        ratio = max(w / cell_w, h / cell_h)
        try:
            current = int(view.Scale)
        except Exception:
            current = int(probe_scale or 50)
        target = next_standard_scale(current * ratio)
        if target != current:
            try:
                view.Scale = target
                doc.Regenerate()
                w, h = measured()
            except Exception:
                scale_locked = True
        # The standard-scale list is coarse, so a view can still overflow
        # after a single jump - step down until it fits or we run out.
        guard = 0
        while (w > cell_w + 1e-9 or h > cell_h + 1e-9) and guard < 4 and not scale_locked:
            try:
                idx = STANDARD_SCALES.index(int(view.Scale))
            except Exception:
                break
            if idx >= len(STANDARD_SCALES) - 1:
                break
            try:
                view.Scale = STANDARD_SCALES[idx + 1]
                doc.Regenerate()
                w, h = measured()
            except Exception:
                break
            guard += 1

    if scale_locked and not fixed_scale:
        warnings.append(
            "{0}: view scale is locked (assigned view template) - placed at its "
            "template scale without auto-fit".format(label))
    elif w > cell_w + 1e-6 or h > cell_h + 1e-6:
        if fixed_scale:
            warnings.append(
                "{0}: does not fit its cell at the 1:{1} you chose - placed anyway "
                "and allowed to overflow (set it to Auto-fit, enlarge the cell, or "
                "give it a bigger span)".format(label, _try_scale(view)))
        else:
            warnings.append(
                "{0}: still larger than its cell at 1:{1} - even the coarsest "
                "standard scale was not enough".format(label, _try_scale(view)))

    vp.SetBoxCenter(XYZ(cx, cy, 0))
    return vp


def _place_schedule(doc, sheet_id, schedule_id, cell, warnings, label):
    """Schedules are not Viewports and cannot be scaled. Placed centred when
    they fit, top-left-anchored when they do not, so an oversized schedule
    spills downward off its cell instead of in every direction.

    The anchor corner of ScheduleSheetInstance.Point is undocumented, so this
    never assumes one - it places, measures, and corrects by the measured
    delta."""
    sheet = doc.GetElement(sheet_id)
    inst = ScheduleSheetInstance.Create(doc, sheet_id, schedule_id,
                                        XYZ(cell[0], cell[3], 0))
    doc.Regenerate()
    try:
        bbox = inst.get_BoundingBox(sheet)
    except Exception:
        bbox = None
    if bbox is None:
        warnings.append("{0}: placed, but its size could not be measured - left at "
                        "the cell's top-left corner".format(label))
        return inst

    w = bbox.Max.X - bbox.Min.X
    h = bbox.Max.Y - bbox.Min.Y
    cell_w = cell[1] - cell[0]
    cell_h = cell[3] - cell[2]
    pt = inst.Point

    if w <= cell_w + 1e-9 and h <= cell_h + 1e-9:
        target_cx = (cell[0] + cell[1]) / 2.0
        target_cy = (cell[2] + cell[3]) / 2.0
        cur_cx = (bbox.Min.X + bbox.Max.X) / 2.0
        cur_cy = (bbox.Min.Y + bbox.Max.Y) / 2.0
        inst.Point = XYZ(pt.X + (target_cx - cur_cx), pt.Y + (target_cy - cur_cy), pt.Z)
    else:
        inst.Point = XYZ(pt.X + (cell[0] - bbox.Min.X),
                         pt.Y + (cell[3] - bbox.Max.Y), pt.Z)
        warnings.append(
            "{0}: {1:.0f} x {2:.0f} mm does not fit its {3:.0f} x {4:.0f} mm cell - "
            "anchored to the cell's top-left corner and allowed to overflow "
            "(schedules cannot be scaled)".format(
                label, ft_to_mm(w), ft_to_mm(h), ft_to_mm(cell_w), ft_to_mm(cell_h)))
    return inst


def format_pattern(pattern, assembly_name, type_name, index, seq=""):
    """Token substitution used by both halves of a NamingRule. Returns None
    for an empty pattern."""
    if not pattern:
        return None
    out = pattern
    for token, value in (("{assembly}", assembly_name), ("{type}", type_name),
                         ("{index}", str(index)), ("{n}", str(index)),
                         ("{seq}", seq)):
        out = out.replace(token, value)
    return out


def alpha_label(n):
    """0 -> A, 25 -> Z, 26 -> AA, 27 -> AB ... the same bijective base-26
    sequence Excel uses for column letters. Negative input clamps to A."""
    n = int(n)
    if n < 0:
        n = 0
    out = ""
    while True:
        out = chr(ord("A") + (n % 26)) + out
        n = n // 26 - 1
        if n < 0:
            break
    return out


def alpha_index(text):
    """Inverse of alpha_label - "A" -> 0, "AA" -> 26. Non-letters are ignored,
    and an empty/garbage start value falls back to A rather than raising."""
    n = 0
    found = False
    for ch in str(text).upper():
        if not ("A" <= ch <= "Z"):
            continue
        found = True
        n = n * 26 + (ord(ch) - ord("A") + 1)
    if not found:
        return 0
    return n - 1


# NamingRule.mode
NAMING_NONE = "none"
NAMING_NUMBER = "number"
NAMING_ALPHA = "alpha"

NAMING_MODE_LABELS = [
    ("No sequence", NAMING_NONE),
    ("Sequential number (1, 2, 3...)", NAMING_NUMBER),
    ("Alphabetic (A, B, C... AA, AB)", NAMING_ALPHA),
]


class NamingRule(object):
    """Builds a sheet number or sheet name as  prefix + sequence + suffix.

    Prefix and suffix both accept the {assembly} / {type} / {index} / {seq}
    tokens, so a separator is just part of the prefix ("A-") rather than yet
    another field. An all-empty rule renders to None, which the builder reads
    as "leave whatever Revit assigned"."""

    def __init__(self, prefix="", suffix="", mode=NAMING_NONE, start="1",
                 step=1, pad=0):
        self.prefix = prefix or ""
        self.suffix = suffix or ""
        self.mode = mode or NAMING_NONE
        self.start = start if start not in (None, "") else "1"
        self.step = int(step) if step else 1
        self.pad = int(pad) if pad else 0

    def sequence_for(self, position):
        """position is 0-based within the batch."""
        if self.mode == NAMING_NUMBER:
            try:
                first = int(str(self.start).strip())
            except (TypeError, ValueError):
                first = 1
            value = first + position * self.step
            text = str(value)
            if self.pad > 0:
                negative = text.startswith("-")
                digits = text[1:] if negative else text
                text = ("-" if negative else "") + digits.rjust(self.pad, "0")
            return text
        if self.mode == NAMING_ALPHA:
            return alpha_label(alpha_index(self.start) + position * self.step)
        return ""

    def render(self, position, assembly_name, type_name):
        """Returns the finished string, or None when the rule produces
        nothing at all (so the caller leaves Revit's own value alone)."""
        seq = self.sequence_for(position)
        index = position + 1
        prefix = format_pattern(self.prefix, assembly_name, type_name, index, seq) or ""
        suffix = format_pattern(self.suffix, assembly_name, type_name, index, seq) or ""
        out = "{0}{1}{2}".format(prefix, seq, suffix)
        return out if out else None

    def preview(self, samples):
        """samples: list of (assembly_name, type_name). Returns the rendered
        strings so the window can show what the first few sheets will be
        called before anything is created."""
        out = []
        for i, (name, type_name) in enumerate(samples):
            out.append(self.render(i, name, type_name) or "(unchanged)")
        return out


def build_for_assembly(doc, row, opts, index):
    """Creates the sheet, the checked views, and places everything - all in
    ONE transaction per assembly, so a failure rolls back that assembly only
    and the rest of the batch carries on."""
    result = AssemblyResult(row.name)
    t = Transaction(doc, "DeeAssemb - {0}".format(row.name[:60]))
    try:
        t.Start()
    except Exception as e:
        result.error = "could not start a transaction: {0}".format(e)
        return result

    try:
        sheet = AssemblyViewUtils.CreateSheet(doc, row.element_id, opts.titleblock_id)
        if sheet is None:
            raise Exception("Revit returned no sheet")
        sheet_id = sheet.Id

        # index is 1-based for display; NamingRule counts positions from 0.
        position = max(0, index - 1)
        number = (opts.sheet_number_rule.render(position, row.name, row.type_name)
                  if opts.sheet_number_rule is not None else None)
        if number:
            try:
                sheet.SheetNumber = number
            except Exception as e:
                result.warnings.append(
                    "sheet number '{0}' rejected ({1}) - kept Revit's own number".format(
                        number, e))
        name = (opts.sheet_name_rule.render(position, row.name, row.type_name)
                if opts.sheet_name_rule is not None else None)
        if name:
            try:
                sheet.Name = name
            except Exception as e:
                result.warnings.append(
                    "sheet name '{0}' rejected ({1}) - kept Revit's own name".format(name, e))

        created = []      # (key, view_id, kind)
        for key in opts.view_keys:
            label = VIEW_KIND_LABELS.get(key, key)
            try:
                view, detail = _create_view(doc, row.element_id, key, opts)
            except Exception as e:
                result.warnings.append("{0}: not created - {1}".format(label, e))
                continue
            if view is None:
                result.warnings.append("{0}: not created - {1}".format(label, detail))
                continue
            created.append((key, view.Id, VIEW_KIND_TYPES.get(key)))

        doc.Regenerate()

        sheet = doc.GetElement(sheet_id)
        outline = sheet.Outline
        bounds = (outline.Min.U, outline.Max.U, outline.Min.V, outline.Max.V)
        cells = compute_cells(bounds, opts.rows, opts.cols,
                              mm_to_ft(opts.margin_mm),
                              mm_to_ft(opts.spacing_h_mm),
                              mm_to_ft(opts.spacing_v_mm))
        if not cells:
            raise Exception(
                "the margins/spacing leave no room for a {0} x {1} grid on this "
                "sheet".format(opts.rows, opts.cols))

        slots = normalize_slots(opts.slots, [c[0] for c in created],
                                opts.rows, opts.cols)

        for key, view_id, kind in created:
            label = VIEW_KIND_LABELS.get(key, key)
            slot = slots.get(key)
            cell = slot_bounds(cells, slot[0], slot[1], slot[2], slot[3]) if slot else None
            if cell is None:
                result.warnings.append("{0}: created but had no cell to go in".format(label))
                continue
            try:
                if kind == "schedule":
                    _place_schedule(doc, sheet_id, view_id, cell, result.warnings, label)
                    result.created.append((label, "schedule"))
                else:
                    _fit_and_place_viewport(doc, sheet_id, view_id, cell,
                                            opts.probe_scale, result.warnings, label,
                                            fixed_scale=opts.scales.get(key))
                    chosen = _try_scale(doc.GetElement(view_id))
                    result.created.append(
                        (label, "1:{0}{1}".format(
                            chosen, "" if opts.scales.get(key) else " auto")))
            except Exception as e:
                result.warnings.append("{0}: created but NOT placed - {1}".format(label, e))

        sheet = doc.GetElement(sheet_id)
        try:
            result.sheet_label = "{0} - {1}".format(sheet.SheetNumber, sheet.Name)
        except Exception:
            result.sheet_label = "(sheet)"

        t.Commit()
    except Exception as e:
        result.error = str(e)
        try:
            t.RollBack()
        except Exception:
            pass
    return result


def print_report(results, output):
    """Coloured per-assembly report in the pyRevit output window, matching the
    house style used by DeeView / DeeRoomStamp / DeeQs."""
    ok = [r for r in results if r.ok]
    bad = [r for r in results if not r.ok]
    warned = [r for r in results if r.ok and r.warnings]

    html = ('<h2 style="font-family:sans-serif;color:#ddd;">DeeAssemb Results</h2>'
            '<div style="font-family:sans-serif;color:#bbb;font-size:13px;'
            'padding:4px 0 10px 0;">{0} assembly(ies) built, {1} failed, '
            '{2} with warnings.</div>'.format(len(ok), len(bad), len(warned)))

    for r in results:
        bg = "#2e7d32" if r.ok else "#c62828"
        icon = "&#10003;" if r.ok else "&#10007;"
        head = r.sheet_label or r.name
        html += ('<div style="padding:6px 12px;margin:6px 0 0 0;background:{0};'
                 'color:#fff;border-radius:4px;font-family:monospace;font-size:13px;">'
                 '{1}&nbsp; <b>{2}</b> &mdash; {3}</div>'.format(
                     bg, icon, r.name,
                     r.error if r.error else "sheet '{0}', {1} view(s) placed".format(
                         head, len(r.created))))
        for label, detail in r.created:
            html += ('<div style="padding:2px 12px 2px 34px;color:#9e9e9e;'
                     'font-family:monospace;font-size:12px;">{0}{1}</div>'.format(
                         label, "  ({0})".format(detail) if detail else ""))
        for w in r.warnings:
            html += ('<div style="padding:2px 12px 2px 34px;color:#ffb300;'
                     'font-family:monospace;font-size:12px;">&#9888; {0}</div>'.format(w))
    output.print_html(html)


if __name__ == "__main__":
    # Only the Revit-free layout math is testable outside Revit; everything
    # touching the API is flagged NEEDS LIVE-REVIT VERIFICATION above.
    import unittest

    class ScaleTests(unittest.TestCase):
        def test_rounds_up_to_standard(self):
            self.assertEqual(next_standard_scale(37.0), 50)
            self.assertEqual(next_standard_scale(50.0), 50)
            self.assertEqual(next_standard_scale(0.4), 1)

        def test_clamps_to_last(self):
            self.assertEqual(next_standard_scale(999999), STANDARD_SCALES[-1])

    class CellTests(unittest.TestCase):
        def test_grid_shape_and_containment(self):
            bounds = (0.0, 100.0, 0.0, 60.0)
            cells = compute_cells(bounds, 2, 3, 2.0, 1.0, 1.0)
            self.assertEqual(len(cells), 2)
            self.assertEqual(len(cells[0]), 3)
            for row in cells:
                for cell in row:
                    self.assertGreaterEqual(cell[0], bounds[0] - 1e-9)
                    self.assertLessEqual(cell[1], bounds[1] + 1e-9)
                    self.assertGreaterEqual(cell[2], bounds[2] - 1e-9)
                    self.assertLessEqual(cell[3], bounds[3] + 1e-9)

        def test_row_zero_is_top(self):
            cells = compute_cells((0.0, 100.0, 0.0, 60.0), 2, 1, 0.0, 0.0, 0.0)
            self.assertGreater(cells[0][0][2], cells[1][0][3] - 1e-9)

        def test_no_room_returns_empty(self):
            self.assertEqual(compute_cells((0.0, 10.0, 0.0, 10.0), 2, 2, 6.0, 0.0, 0.0), [])

        def test_span_merges_cells(self):
            cells = compute_cells((0.0, 100.0, 0.0, 60.0), 2, 2, 0.0, 0.0, 0.0)
            full = slot_bounds(cells, 0, 0, 2, 2)
            self.assertAlmostEqual(full[0], 0.0)
            self.assertAlmostEqual(full[1], 100.0)
            self.assertAlmostEqual(full[2], 0.0)
            self.assertAlmostEqual(full[3], 60.0)

        def test_span_is_clamped(self):
            cells = compute_cells((0.0, 100.0, 0.0, 60.0), 2, 2, 0.0, 0.0, 0.0)
            self.assertIsNotNone(slot_bounds(cells, 1, 1, 5, 5))
            self.assertIsNone(slot_bounds(cells, 9, 9, 1, 1))

        def test_cell_at_point(self):
            cells = compute_cells((0.0, 100.0, 0.0, 60.0), 2, 2, 0.0, 0.0, 0.0)
            self.assertEqual(cell_at_point(cells, 25.0, 45.0), (0, 0))
            self.assertEqual(cell_at_point(cells, 75.0, 15.0), (1, 1))
            self.assertIsNone(cell_at_point(cells, 500.0, 500.0))

    class SlotTests(unittest.TestCase):
        def test_auto_arrange_order(self):
            got = auto_arrange(["a", "b", "c"], 2, 2)
            self.assertEqual(got["a"], (0, 0, 1, 1))
            self.assertEqual(got["b"], (0, 1, 1, 1))
            self.assertEqual(got["c"], (1, 0, 1, 1))

        def test_auto_arrange_drops_overflow(self):
            self.assertNotIn("e", auto_arrange(list("abcde"), 2, 2))

        def test_normalize_gives_every_key_a_slot(self):
            got = normalize_slots({"a": (0, 0, 1, 1)}, ["a", "b", "c"], 2, 2)
            self.assertEqual(sorted(got.keys()), ["a", "b", "c"])

        def test_normalize_drops_unchecked_and_clamps(self):
            got = normalize_slots({"a": (9, 9, 4, 4), "gone": (0, 0, 1, 1)}, ["a"], 2, 2)
            self.assertEqual(sorted(got.keys()), ["a"])
            self.assertEqual(got["a"], (1, 1, 1, 1))

        def test_normalize_does_not_double_book(self):
            got = normalize_slots({"a": (0, 0, 1, 1)}, ["a", "b"], 2, 2)
            self.assertNotEqual(got["a"][:2], got["b"][:2])

    class PatternTests(unittest.TestCase):
        def test_tokens(self):
            self.assertEqual(
                format_pattern("A-{index} {assembly} [{type}]", "PC-01", "Panel", 7),
                "A-7 PC-01 [Panel]")

        def test_empty_means_leave_alone(self):
            self.assertIsNone(format_pattern("", "x", "y", 1))

    class AlphaTests(unittest.TestCase):
        def test_single_letters(self):
            self.assertEqual(alpha_label(0), "A")
            self.assertEqual(alpha_label(25), "Z")

        def test_rolls_over_like_excel_columns(self):
            self.assertEqual(alpha_label(26), "AA")
            self.assertEqual(alpha_label(27), "AB")
            self.assertEqual(alpha_label(51), "AZ")
            self.assertEqual(alpha_label(52), "BA")

        def test_round_trip(self):
            for n in (0, 1, 25, 26, 27, 51, 52, 700):
                self.assertEqual(alpha_index(alpha_label(n)), n)

        def test_garbage_start_falls_back_to_a(self):
            self.assertEqual(alpha_index(""), 0)
            self.assertEqual(alpha_index("123"), 0)

        def test_negative_clamps(self):
            self.assertEqual(alpha_label(-5), "A")

    class NamingRuleTests(unittest.TestCase):
        def test_sequential_with_padding(self):
            r = NamingRule(prefix="A-", mode=NAMING_NUMBER, start="1", pad=3)
            self.assertEqual(r.render(0, "PC-01", "Panel"), "A-001")
            self.assertEqual(r.render(9, "PC-01", "Panel"), "A-010")

        def test_step_and_custom_start(self):
            r = NamingRule(mode=NAMING_NUMBER, start="100", step=5)
            self.assertEqual([r.sequence_for(i) for i in range(3)], ["100", "105", "110"])

        def test_alphabetic(self):
            r = NamingRule(prefix="SH-", mode=NAMING_ALPHA, start="A")
            self.assertEqual([r.render(i, "x", "y") for i in range(3)],
                             ["SH-A", "SH-B", "SH-C"])

        def test_alphabetic_custom_start(self):
            r = NamingRule(mode=NAMING_ALPHA, start="C")
            self.assertEqual(r.sequence_for(0), "C")
            self.assertEqual(r.sequence_for(1), "D")

        def test_prefix_suffix_tokens(self):
            r = NamingRule(prefix="{assembly} - ", suffix=" ({type} {seq})",
                           mode=NAMING_NUMBER, start="1")
            self.assertEqual(r.render(0, "PC-01", "Panel"), "PC-01 - 1 (Panel 1)")

        def test_no_sequence_is_prefix_plus_suffix(self):
            r = NamingRule(prefix="{assembly}", suffix=" Layout", mode=NAMING_NONE)
            self.assertEqual(r.render(3, "PC-04", "Panel"), "PC-04 Layout")

        def test_completely_empty_leaves_revits_value(self):
            self.assertIsNone(NamingRule().render(0, "x", "y"))

        def test_pad_keeps_negative_sign(self):
            r = NamingRule(mode=NAMING_NUMBER, start="-2", pad=3)
            self.assertEqual(r.sequence_for(0), "-002")

        def test_bad_start_falls_back_to_one(self):
            r = NamingRule(mode=NAMING_NUMBER, start="abc")
            self.assertEqual(r.sequence_for(0), "1")

        def test_preview_matches_render(self):
            r = NamingRule(prefix="A-", mode=NAMING_NUMBER, start="1", pad=2)
            self.assertEqual(r.preview([("a", "t"), ("b", "t")]), ["A-01", "A-02"])

    unittest.main(verbosity=2)
