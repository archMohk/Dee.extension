# -*- coding: utf-8 -*-
"""
DeeCtotopo (Topography)
Converts a surface that came from Civil 3D into NATIVE Revit
Toposolid(s). The three shapes a Civil 3D surface actually arrives in:

  1. A linked or imported DWG (ImportInstance) carrying the TIN as one
     or more polyface MESHES - the classic "insert the corridor/EG
     surface DWG" workflow.
  2. "Link Topography" via Desktop Connector (BIM 360/ACC publish
     surface) - lands as a TopographySurface, whose points the API
     hands over directly via GetPoints().
  3. A legacy TopographySurface someone already generated.

Two FOOTPRINT modes (asked right after picking the source - live
feedback: a DWG of separate road strips converted into one giant blob
that bridged every gap between them with false terrain):

  A. "Respect the source boundaries" - each mesh in the DWG becomes its
     own Toposolid whose footprint is that mesh's TRUE outline, traced
     from its boundary edges (the edges belonging to exactly one
     triangle), concave bends and interior holes included. Separate
     road strips stay separate strips.
  B. "One combined outline" - every point from every mesh pooled, one
     Toposolid whose footprint is the convex hull of it all. Right for
     a single EG/site surface; wrong for disjoint corridor pieces.

  C. "Sub-divide an existing Toposolid" - no new terrain at all: the
     source's traced outlines become SUB-DIVISIONS on a host Toposolid
     the user picks (roads/pads drawn onto the terrain, each can then
     carry its own type/material). Outer rings only - a parcel 'hole'
     is precisely where the host must stay untouched. See
     _run_subdivide.

  D. "Join several surfaces into ONE Toposolid" - pick as many Civil
     sources as needed (one at a time, sequential native picks only -
     e.g. the EG surface DWG AND the road corridor DWG), trace every
     one's TRUE boundary exactly like mode A, then merge every kept
     outline into a SINGLE Toposolid element instead of one per
     outline. See _run_join.

Deliberately sequential native/pyRevit dialogs, NO custom WPF window -
this flow needs PickObject, and this codebase's hard rule is never to
call PickObject (or open a second WPF dialog) from inside an open WPF
window (see DeeMono's docstring for the crash this avoids).

Shared mechanics
----------------
- One Z per XY location (highest wins - a TIN is a top surface).
- Dense surfaces are thinned on a grid (highest point per cell kept, so
  ridges survive); the report states original vs used counts.
- The profile CurveLoop(s) are built FLAT at the target level's
  elevation; the 3D points shape the top through Toposolid.Create's
  points overload, with a SlabShapeEditor fallback.

--------------------------------------------------------------------
NEEDS LIVE-REVIT VERIFICATION (per this codebase's convention)
--------------------------------------------------------------------
  1. Toposolid.Create(profile, points, type, level) accepting boundary-
     coincident points (combined mode ran live and produced a solid -
     the points overload itself is confirmed working).
  2. Boundary-mode loop tracing against a real corridor DWG: welding
     tolerance (_WELD_XY) and non-manifold seam vertices are handled,
     but only a live file proves the chains close on real data.
"""
import math

from Autodesk.Revit.DB import (
    FilteredElementCollector, Options, ViewDetailLevel, GeometryInstance,
    Solid, Mesh, PolyLine, ImportInstance, DirectShape, CurveLoop, Line,
    XYZ, Transaction, Level, ElementId, Element, BuiltInParameter,
)
from Autodesk.Revit.UI.Selection import ObjectType, ISelectionFilter
from System.Collections.Generic import List

from pyrevit import forms, script
import dee_telemetry
dee_telemetry.check_access("DeeCtotopo")

_TOOL = "DeeCtotopo"
# Bumped by hand whenever this file changes meaningfully - shown in the
# mode-picker title so "did my last edit actually take effect?" has a
# one-glance answer instead of guessing about pyRevit's script caching.
_VERSION = "v5"
output = script.get_output()

_MAX_POINTS = 10000          # combined mode
_MAX_POINTS_PER_PIECE = 6000  # boundary mode, per created solid
_XY_KEY_DIGITS = 6      # feet - collapse identical XYs at seam precision
_WELD_XY = 3            # digits - boundary tracing welds XY at ~0.3 mm
_MIN_HULL_EDGE = 0.03   # feet (~1 cm) - drop degenerate outline edges
_MIN_LOOP_AREA = 1.0    # sq ft - ignore sliver loops
_MODE_BOUNDARY = "Respect the source boundaries (one Toposolid per surface piece)"
_MODE_COMBINED = "One combined outline around everything"
_MODE_SUBDIV = "Sub-divide an EXISTING Toposolid along the source edges"
_MODE_JOIN = "Join several surfaces into ONE Toposolid"

try:
    from Autodesk.Revit.DB import Toposolid, ToposolidType
    from Autodesk.Revit.DB.Architecture import TopographySurface
    _HAS_TOPOSOLID = True
except ImportError:
    Toposolid = ToposolidType = None
    try:
        from Autodesk.Revit.DB.Architecture import TopographySurface
    except ImportError:
        TopographySurface = None
    _HAS_TOPOSOLID = False


# ==========================================================================
# geometry reads
# ==========================================================================
def _geometry_of(element):
    opts = Options()
    opts.ComputeReferences = False
    opts.IncludeNonVisibleObjects = False
    try:
        opts.DetailLevel = ViewDetailLevel.Fine
    except Exception:
        pass
    try:
        return element.get_Geometry(opts)
    except Exception:
        return None


def _walk_points(geom, out, depth=0):
    """Every 3D vertex any geometry exposes - meshes, solid faces, and
    3D polylines (the contours-only DWG fallback)."""
    if geom is None or depth > 8:
        return
    for obj in geom:
        try:
            if isinstance(obj, GeometryInstance):
                _walk_points(obj.GetInstanceGeometry(), out, depth + 1)
            elif isinstance(obj, Mesh):
                for p in obj.Vertices:
                    out.append((p.X, p.Y, p.Z))
            elif isinstance(obj, Solid):
                for face in obj.Faces:
                    try:
                        mesh = face.Triangulate()
                        if mesh is not None:
                            for p in mesh.Vertices:
                                out.append((p.X, p.Y, p.Z))
                    except Exception:
                        continue
            elif isinstance(obj, PolyLine):
                for p in obj.GetCoordinates():
                    out.append((p.X, p.Y, p.Z))
        except Exception:
            continue


def _walk_meshes(geom, out, depth=0):
    """[(verts, tris)] per Mesh - verts as (x,y,z) tuples, tris as index
    triples. Solid faces are triangulated and count as meshes too, so a
    DWG that arrived as solids still gets true boundaries."""
    if geom is None or depth > 8:
        return
    for obj in geom:
        try:
            if isinstance(obj, GeometryInstance):
                _walk_meshes(obj.GetInstanceGeometry(), out, depth + 1)
            elif isinstance(obj, Mesh):
                out.append(_read_mesh(obj))
            elif isinstance(obj, Solid):
                for face in obj.Faces:
                    try:
                        mesh = face.Triangulate()
                        if mesh is not None:
                            out.append(_read_mesh(mesh))
                    except Exception:
                        continue
        except Exception:
            continue


def _read_mesh(mesh):
    verts = [(p.X, p.Y, p.Z) for p in mesh.Vertices]
    tris = []
    for i in range(mesh.NumTriangles):
        try:
            t = mesh.get_Triangle(i)
            tris.append((int(t.get_Index(0)), int(t.get_Index(1)), int(t.get_Index(2))))
        except Exception:
            continue
    return verts, tris


def _extract_points(element):
    """All 3D points of the picked source, in host feet."""
    if TopographySurface is not None and isinstance(element, TopographySurface):
        try:
            return [(p.X, p.Y, p.Z) for p in element.GetPoints()]
        except Exception:
            pass
    out = []
    _walk_points(_geometry_of(element), out)
    return out


# ==========================================================================
# point conditioning
# ==========================================================================
def _collapse_xy(points):
    best = {}
    for x, y, z in points:
        key = (round(x, _XY_KEY_DIGITS), round(y, _XY_KEY_DIGITS))
        old = best.get(key)
        if old is None or z > old:
            best[key] = z
    return [(k[0], k[1], z) for k, z in best.items()]


def _thin(points, limit):
    if len(points) <= limit:
        return points
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    span_x = max(xs) - min(xs) or 1e-6
    span_y = max(ys) - min(ys) or 1e-6
    cell = math.sqrt(span_x * span_y / float(limit))
    x0, y0 = min(xs), min(ys)
    best = {}
    for x, y, z in points:
        key = (int((x - x0) / cell), int((y - y0) / cell))
        old = best.get(key)
        if old is None or z > old[2]:
            best[key] = (x, y, z)
    return list(best.values())


def _convex_hull_xy(points):
    pts = sorted(set((round(p[0], 6), round(p[1], 6)) for p in points))
    if len(pts) < 3:
        return []

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return _clean_ring(lower[:-1] + upper[:-1])


def _clean_ring(ring):
    """Drops near-coincident neighbours and collinear midpoints so no
    zero-length or redundant profile lines get created."""
    out = []
    for p in ring:
        if not out or math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) > _MIN_HULL_EDGE:
            out.append(p)
    if len(out) >= 2 and math.hypot(out[0][0] - out[-1][0],
                                    out[0][1] - out[-1][1]) <= _MIN_HULL_EDGE:
        out.pop()
    if len(out) < 3:
        return out
    slim = []
    n = len(out)
    for i in range(n):
        a, b, c = out[i - 1], out[i], out[(i + 1) % n]
        cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
        if abs(cross) > 1e-6:
            slim.append(b)
    return slim if len(slim) >= 3 else out


def _ring_area(ring):
    area = 0.0
    n = len(ring)
    for i in range(n):
        x0, y0 = ring[i]
        x1, y1 = ring[(i + 1) % n]
        area += x0 * y1 - x1 * y0
    return area / 2.0


def _point_in_ring(pt, ring):
    x, y = pt
    inside = False
    n = len(ring)
    for i in range(n):
        x0, y0 = ring[i]
        x1, y1 = ring[(i + 1) % n]
        if (y0 > y) != (y1 > y):
            xin = x0 + (y - y0) * (x1 - x0) / ((y1 - y0) or 1e-12)
            if x < xin:
                inside = not inside
    return inside


def _on_ring(pt, ring, tol=0.02):
    """True when pt sits ON the ring's outline (within tol feet).
    Boundary vertices carry real elevations - a road edge climbing a
    hill - so they must count as part of a region even though the
    ray-cast above treats them as outside."""
    x, y = pt
    n = len(ring)
    for i in range(n):
        x0, y0 = ring[i]
        x1, y1 = ring[(i + 1) % n]
        dx, dy = x1 - x0, y1 - y0
        seg_len2 = dx * dx + dy * dy
        if seg_len2 <= 0:
            continue
        t = ((x - x0) * dx + (y - y0) * dy) / seg_len2
        t = 0.0 if t < 0 else (1.0 if t > 1 else t)
        ex, ey = x0 + dx * t, y0 + dy * t
        if (x - ex) * (x - ex) + (y - ey) * (y - ey) <= tol * tol:
            return True
    return False


# ==========================================================================
# true boundary tracing (mode A)
# ==========================================================================
def _mesh_regions(verts, tris, stats=None):
    """One (outer_ring, [hole_rings], region_points) per closed outline
    found in this mesh. Boundary edges are the ones used by exactly ONE
    triangle; welding XY first makes seam duplicates share an index so
    interior seams don't read as boundaries."""
    weld_index = {}
    remap = []
    welded = []   # (x, y, z) - highest z wins per welded XY
    for x, y, z in verts:
        key = (round(x, _WELD_XY), round(y, _WELD_XY))
        idx = weld_index.get(key)
        if idx is None:
            idx = len(welded)
            weld_index[key] = idx
            welded.append((x, y, z))
        elif z > welded[idx][2]:
            welded[idx] = (welded[idx][0], welded[idx][1], z)
        remap.append(idx)

    edge_count = {}
    directed = {}   # undirected key -> (a, b) as wound in its triangle
    for a, b, c in tris:
        wa, wb, wc = remap[a], remap[b], remap[c]
        if wa == wb or wb == wc or wa == wc:
            continue
        for e in ((wa, wb), (wb, wc), (wc, wa)):
            key = (min(e), max(e))
            edge_count[key] = edge_count.get(key, 0) + 1
            directed[key] = e

    # DIRECTED-edge loop walking - the planar face-tracing rule. Each
    # boundary edge keeps the direction its (single) triangle wound it
    # with, so every loop is a consistent directed cycle; at a junction
    # vertex where several loops touch (a parcel corner meeting two road
    # edges - the exact spot the old vertex-visited walk broke and lost
    # the parcel holes, live-caught as "one blob over everything"), the
    # walk leaves along the sharpest CLOCKWISE turn from the reversed
    # incoming direction, which is what keeps two loops sharing a vertex
    # from merging into a zero-area figure-8.
    out_edges = {}
    for key, count in edge_count.items():
        if count == 1:
            a, b = directed[key]
            out_edges.setdefault(a, []).append(b)

    def _angle(a, b):
        return math.atan2(welded[b][1] - welded[a][1],
                          welded[b][0] - welded[a][0])

    rings = []
    open_chains = 0
    starts = sorted(out_edges)
    for start in starts:
        while out_edges.get(start):
            b0 = out_edges[start].pop()
            chain = [start, b0]
            prev, cur = start, b0
            closed = False
            for _guard in range(len(edge_count) + 8):
                if cur == start:
                    closed = True
                    break
                options = out_edges.get(cur) or []
                if not options:
                    break                 # open chain - not a loop, drop it
                if len(options) == 1:
                    nxt = options.pop()
                else:
                    back = _angle(cur, prev)
                    nxt = min(options, key=lambda n:
                              (back - _angle(cur, n)) % (2 * math.pi))
                    options.remove(nxt)
                chain.append(nxt)
                prev, cur = cur, nxt
            if closed and len(chain) >= 4:    # chain ends back at start
                ring = _clean_ring([(welded[i][0], welded[i][1])
                                    for i in chain[:-1]])
                if len(ring) >= 3 and abs(_ring_area(ring)) >= _MIN_LOOP_AREA:
                    rings.append(ring)
            elif not closed:
                open_chains += 1
    if stats is not None and open_chains:
        stats[0] = stats[0] + open_chains

    if not rings:
        return []

    # largest first; each smaller ring inside an outer becomes its hole
    rings.sort(key=lambda r: -abs(_ring_area(r)))
    regions = []   # [outer, holes]
    for ring in rings:
        probe = ring[0]
        placed = False
        for region in regions:
            if _point_in_ring(probe, region[0]):
                region[1].append(ring)
                placed = True
                break
        if not placed:
            regions.append([ring, []])

    out = []
    all_pts = [(p[0], p[1], p[2]) for p in welded]
    for outer, holes in regions:
        pts = []
        for p in all_pts:
            xy = (p[0], p[1])
            if not (_point_in_ring(xy, outer) or _on_ring(xy, outer)):
                continue
            # a point ON a hole's edge belongs to the region; strictly
            # INSIDE a hole it does not
            if any(_point_in_ring(xy, h) and not _on_ring(xy, h) for h in holes):
                continue
            pts.append(p)
        out.append((outer, holes, pts))
    return out


# ==========================================================================
# Revit builds
# ==========================================================================
def _profile_loop(ring, z):
    loop = CurveLoop()
    n = len(ring)
    for i in range(n):
        a, b = ring[i], ring[(i + 1) % n]
        loop.Append(Line.CreateBound(XYZ(a[0], a[1], z), XYZ(b[0], b[1], z)))
    return loop


def _create_toposolid(doc, rings, points, topo_type, level):
    """rings: [outer] + holes. Tries the points overload first, falls
    back to flat-create + SlabShapeEditor. Returns the new element."""
    loops = List[CurveLoop]()
    for ring in rings:
        loops.Add(_profile_loop(ring, level.Elevation))
    xyz_points = List[XYZ]()
    for x, y, z in points:
        xyz_points.Add(XYZ(x, y, z))
    try:
        return Toposolid.Create(doc, loops, xyz_points, topo_type.Id, level.Id)
    except Exception:
        created = Toposolid.Create(doc, loops, topo_type.Id, level.Id)
        editor = created.GetSlabShapeEditor()
        editor.Enable()
        for x, y, z in points:
            try:
                editor.DrawPoint(XYZ(x, y, z))
            except Exception:
                continue
        return created


# ==========================================================================
# pick + choose
# ==========================================================================
class _SourceFilter(ISelectionFilter):
    def AllowElement(self, element):
        if isinstance(element, (ImportInstance, DirectShape)):
            return True
        if TopographySurface is not None and isinstance(element, TopographySurface):
            return True
        return False

    def AllowReference(self, reference, position):
        return False


def _pick_source(uidoc):
    doc = uidoc.Document
    try:
        sel_ids = list(uidoc.Selection.GetElementIds())
    except Exception:
        sel_ids = []
    if len(sel_ids) == 1:
        el = doc.GetElement(sel_ids[0])
        if el is not None and _SourceFilter().AllowElement(el):
            return el
    try:
        ref = uidoc.Selection.PickObject(
            ObjectType.Element, _SourceFilter(),
            "Pick the Civil 3D surface (a linked/imported DWG, linked "
            "topography, or a toposurface)")
        return doc.GetElement(ref.ElementId)
    except Exception:
        return None  # user pressed Esc


def _element_name(element):
    """ElementTYPES (ToposolidType here) don't expose .Name through the
    instance property under IronPython - it comes back empty, which is
    exactly how the type-picker once showed five blank rows labelled
    only by the dedup counter (live-caught). The static Element.Name
    getter and the type-name parameter both do return it."""
    try:
        if element.Name:
            return element.Name
    except Exception:
        pass
    try:
        name = Element.Name.GetValue(element)
        if name:
            return name
    except Exception:
        pass
    for bip in (BuiltInParameter.ALL_MODEL_TYPE_NAME,
                BuiltInParameter.SYMBOL_NAME_PARAM):
        try:
            p = element.get_Parameter(bip)
            if p is not None and p.AsString():
                return p.AsString()
        except Exception:
            continue
    return ""


def _choose(items, label_of, title):
    labels, by_label = [], {}
    for item in items:
        label = label_of(item)
        if not label:
            try:
                label = u"<unnamed>  (id {0})".format(item.Id)
            except Exception:
                label = u"<unnamed>"
        base, n = label, 2
        while label in by_label:
            label = u"{0} ({1})".format(base, n)
            n += 1
        labels.append(label)
        by_label[label] = item
    picked = forms.SelectFromList.show(labels, title=title, button_name="Use this")
    return by_label.get(picked) if picked else None


def _trace_source_regions(source):
    """(meshes, regions, stats) - the boundary tracing shared by
    'respect boundaries' and 'sub-divide' modes."""
    meshes = []
    _walk_meshes(_geometry_of(source), meshes)
    regions = []
    stats = [0]
    for verts, tris in meshes:
        if len(verts) >= 3 and tris:
            regions.extend(_mesh_regions(verts, tris, stats))
    return meshes, regions, stats


def _pick_regions(regions, verb, tags=None):
    """Region checklist (largest first) - lets the user drop outlines
    they don't want. `tags`, if given, is a same-length list of source
    labels shown per row (join mode, where regions come from several
    picked elements). Returns the kept list, or None on cancel."""
    order = sorted(range(len(regions)), key=lambda i: -abs(_ring_area(regions[i][0])))
    regions = [regions[i] for i in order]
    if tags is not None:
        tags = [tags[i] for i in order]
    if len(regions) <= 1:
        return regions
    labels, by_label = [], {}
    for i, (outer, holes, pts) in enumerate(regions):
        area_m2 = abs(_ring_area(outer)) * 0.09290304
        prefix = u"[{0}]  ".format(tags[i]) if tags else u""
        label = u"{0}Outline {1}:  {2:,.0f} m2,  {3} hole(s),  {4:,} points".format(
            prefix, i + 1, area_m2, len(holes), len(pts))
        labels.append(label)
        by_label[label] = regions[i]
    picked = forms.SelectFromList.show(
        labels, multiselect=True, button_name=verb,
        title="{0} surface outlines found - untick any to skip".format(len(regions)))
    if not picked:
        return None
    return [by_label[l] for l in picked]


def _create_subdivision(doc, host, loops):
    """Toposolid sub-division - tries the documented instance signature
    first, then the static spelling, so an API rename between point
    releases degrades to a reported error rather than a hard crash.
    NEEDS LIVE-REVIT VERIFICATION: written from the 2024 API docs."""
    try:
        return host.CreateSubDivision(doc, loops)
    except (AttributeError, TypeError):
        pass
    return Toposolid.CreateSubDivision(doc, host.Id, loops)


def _choose_type_and_level(doc, min_z):
    topo_types = list(FilteredElementCollector(doc).OfClass(ToposolidType))
    if not topo_types:
        forms.alert("This project has no Toposolid types - load or create one "
                    "first (a default template usually has 'Grass').", title=_TOOL)
        return None, None
    topo_type = topo_types[0] if len(topo_types) == 1 else _choose(
        topo_types, _element_name, "Toposolid type")
    if topo_type is None:
        return None, None

    levels = sorted(FilteredElementCollector(doc).OfClass(Level),
                    key=lambda lv: lv.Elevation)
    if not levels:
        forms.alert("This project has no levels.", title=_TOOL)
        return None, None
    default = None
    for lv in levels:
        if lv.Elevation <= min_z:
            default = lv
    level = _choose(
        levels,
        lambda lv: u"{0}{1}".format(_element_name(lv),
                                    u"   (suggested)" if lv is default else u""),
        "Base level for the Toposolid")
    return topo_type, level


class _ToposolidFilter(ISelectionFilter):
    """Only host Toposolids - an existing sub-division (a Toposolid
    whose HostTopoId points at another) can't be sub-divided again."""
    def AllowElement(self, element):
        if Toposolid is None or not isinstance(element, Toposolid):
            return False
        try:
            host_id = element.HostTopoId
            if host_id is not None and host_id != ElementId.InvalidElementId:
                return False
        except Exception:
            pass
        return True

    def AllowReference(self, reference, position):
        return False


def _run_subdivide(uidoc, doc, source):
    """Mode C: the source's traced outlines become SUB-DIVISIONS on an
    existing Toposolid the user picks - roads/pads drawn onto the
    terrain, each gettable its own material and thickness, instead of
    separate solids. The sub-division profile is the outer ring only:
    a parcel 'hole' in a road network is exactly where the host should
    stay untouched, and nested sub-division loops are not valid there
    anyway."""
    _meshes, regions, trace_stats = _trace_source_regions(source)
    if not regions:
        forms.alert(
            "No closed surface outlines could be traced in that element, so "
            "there are no edges to sub-divide along.", title=_TOOL)
        return
    regions = _pick_regions(regions, "Sub-divide with these")
    if not regions:
        return

    try:
        ref = uidoc.Selection.PickObject(
            ObjectType.Element, _ToposolidFilter(),
            "Pick the EXISTING Toposolid to sub-divide")
    except Exception:
        return  # Esc
    host = doc.GetElement(ref.ElementId)

    try:
        host_level = doc.GetElement(host.LevelId)
        z = host_level.Elevation if host_level is not None else 0.0
    except Exception:
        z = 0.0

    created, failed = [], []
    t = Transaction(doc, "DeeCtotopo - sub-divide Toposolid")
    t.Start()
    try:
        for outer, _holes, _pts in regions:
            loops = List[CurveLoop]()
            loops.Add(_profile_loop(outer, z))
            try:
                sub = _create_subdivision(doc, host, loops)
                created.append(sub)
            except Exception as e:
                failed.append(str(e))
        if created:
            t.Commit()
        else:
            t.RollBack()
    except Exception as e:
        t.RollBack()
        forms.alert(u"Could not sub-divide:\n{0}".format(e), title=_TOOL)
        return

    lines = [u"**DeeCtotopo — done (sub-divide mode)**", u""]
    lines.append(u"- Host Toposolid: id {0}".format(host.Id))
    lines.append(u"- Sub-divisions created: {0}".format(len(created)))
    if trace_stats[0]:
        lines.append(u"- WARNING: {0} boundary chain(s) did not close and "
                     u"were dropped.".format(trace_stats[0]))
    if failed:
        lines.append(u"- FAILED for {0} outline(s), first error: {1}".format(
            len(failed), failed[0]))
        lines.append(u"  (a sub-division profile must lie fully WITHIN the "
                     u"host's own footprint - an outline hanging over the "
                     u"host's edge is the usual cause)")
    output.print_md(u"\n".join(lines))
    forms.alert(
        u"{0} sub-division(s) created on the picked Toposolid.{1}\n\n"
        u"Select any of them to give it its own type/material "
        u"(e.g. asphalt for roads).".format(
            len(created),
            u"  {0} failed - see the output window.".format(len(failed))
            if failed else u""),
        title=_TOOL)


def _run_join(uidoc, doc, first_source):
    """Mode D: pick as many Civil 3D surfaces as needed - one at a time,
    sequential native PickObject calls only, never a second WPF window
    (this codebase's hard rule - see the module docstring) - trace each
    one's TRUE boundary exactly like mode A, then merge every kept
    outline into a SINGLE Toposolid element instead of one per outline.
    `first_source` is the element main() already picked before showing
    the mode switcher, so the user is never asked to pick the same
    thing twice.

    --------------------------------------------------------------
    NEEDS LIVE-REVIT VERIFICATION (per this codebase's convention)
    --------------------------------------------------------------
    Toposolid.Create accepting several DISJOINT (non-nested, non-
    touching) outer loops as separate regions/pads of one element -
    standard Floor/Toposolid sketch behaviour in the Revit UI (several
    closed loops in one sketch = several pads on one element), but not
    yet confirmed against this exact API call. Two picked surfaces that
    TOUCH along a shared edge (rather than sitting apart) are the case
    most likely to need attention live - if Revit rejects a coincident
    edge between two loops, run them as separate DeeCtotopo passes
    (mode A) instead of joining them."""
    sources = [first_source]
    while forms.alert(
            u"Pick another surface to join into the same Toposolid?",
            title=_TOOL, yes=True, no=True):
        try:
            ref = uidoc.Selection.PickObject(
                ObjectType.Element, _SourceFilter(),
                "Pick another Civil 3D surface to join in")
        except Exception:
            break  # Esc - stop adding, keep what's picked so far
        el = doc.GetElement(ref.ElementId)
        if el is not None and el.Id not in [s.Id for s in sources]:
            sources.append(el)

    tagged = []   # [(label, outer, holes, pts)]
    total_open = 0
    for src in sources:
        label = _element_name(src) or src.GetType().Name
        _meshes, regions, stats = _trace_source_regions(src)
        total_open += stats[0]
        for outer, holes, pts in regions:
            tagged.append((label, outer, holes, pts))

    if not tagged:
        forms.alert(
            "No closed surface outlines could be traced in any of the "
            "picked elements - nothing to join.", title=_TOOL)
        return

    regions_only = [(o, h, p) for _l, o, h, p in tagged]
    tags_only = [l for l, _o, _h, _p in tagged]
    kept = _pick_regions(regions_only, "Join these", tags=tags_only)
    if not kept:
        return

    all_rings, all_points = [], []
    for outer, holes, pts in kept:
        all_rings.append(outer)
        all_rings.extend(holes)
        all_points.extend(pts)

    used = _thin(_collapse_xy(all_points), _MAX_POINTS)
    topo_type, level = _choose_type_and_level(doc, min(p[2] for p in used))
    if topo_type is None or level is None:
        return

    t = Transaction(doc, "DeeCtotopo - join surfaces into one Toposolid")
    t.Start()
    try:
        created = _create_toposolid(doc, all_rings, used, topo_type, level)
        t.Commit()
    except Exception as e:
        t.RollBack()
        forms.alert(
            u"Could not join these surfaces into one Toposolid:\n{0}\n\n"
            u"If two of them touch along a shared edge, try converting "
            u"them separately instead (mode A).".format(e), title=_TOOL)
        return

    lines = [u"**DeeCtotopo — done (join mode)**", u""]
    lines.append(u"- Sources picked: {0}".format(len(sources)))
    lines.append(u"- Outlines merged into one Toposolid: {0}".format(len(kept)))
    lines.append(u"- Points used: {0:,}".format(len(used)))
    if total_open:
        lines.append(u"- WARNING: {0} boundary chain(s) did not close and "
                     u"were dropped.".format(total_open))
    lines.append(u"- Toposolid: *{0}* on level *{1}* (id {2})".format(
        _element_name(topo_type), _element_name(level), created.Id))
    output.print_md(u"\n".join(lines))
    forms.alert(
        u"{0} surface(s), {1} outline(s) joined into ONE Toposolid "
        u"(id {2}).\n\nNone of the sources were modified.".format(
            len(sources), len(kept), created.Id),
        title=_TOOL)


# ==========================================================================
def main():
    uidoc = __revit__.ActiveUIDocument
    if uidoc is None:
        forms.alert("Open a Revit project first.", title=_TOOL)
        return
    doc = uidoc.Document
    if not _HAS_TOPOSOLID:
        forms.alert("This Revit version has no Toposolid API - DeeCtotopo needs "
                    "Revit 2024 or newer.", title=_TOOL)
        return

    source = _pick_source(uidoc)
    if source is None:
        return

    # ALWAYS offered, whatever the source type - a TopographySurface used
    # to skip straight to "combined" (a legacy toposurface's own points
    # can still have several disjoint islands, or the user may want to
    # sub-divide with them), which silently reproduced the pre-mode-
    # picker behaviour and looked exactly like "the update never
    # arrived" (live report). _VERSION in the title is a plain, visible
    # proof that this build is the one actually running - compare it
    # against DeeCtotopo's own script.py docstring/changelog if a future
    # report ever again looks like "my last change had no effect".
    mode = forms.CommandSwitchWindow.show(
        [_MODE_BOUNDARY, _MODE_COMBINED, _MODE_SUBDIV, _MODE_JOIN],
        message="DeeCtotopo {0} - what should be built from the source?".format(_VERSION))
    if not mode:
        return

    # ---- mode D: merge several picked surfaces into ONE Toposolid ------
    if mode == _MODE_JOIN:
        _run_join(uidoc, doc, source)
        return

    # ---- mode C: sub-divide an existing Toposolid along the edges ------
    if mode == _MODE_SUBDIV:
        _run_subdivide(uidoc, doc, source)
        return

    # ---- mode A: one Toposolid per real surface outline ---------------
    if mode == _MODE_BOUNDARY:
        meshes, regions, trace_stats = _trace_source_regions(source)
        if not regions:
            forms.alert(
                "No closed surface outlines could be traced in that element "
                "(it may carry only 3D polylines/contours, which have no "
                "boundary to respect).\n\nRun again and pick 'One combined "
                "outline' instead.", title=_TOOL)
            return
        regions = _pick_regions(regions, "Convert these")
        if not regions:
            return

        min_z = min(min(p[2] for p in pts) for _o, _h, pts in regions if pts)
        topo_type, level = _choose_type_and_level(doc, min_z)
        if topo_type is None or level is None:
            return

        created, failed = [], []
        t = Transaction(doc, "DeeCtotopo - Civil surfaces to Toposolids")
        t.Start()
        try:
            for outer, holes, pts in regions:
                used = _thin(_collapse_xy(pts), _MAX_POINTS_PER_PIECE)
                try:
                    solid = _create_toposolid(doc, [outer] + holes, used,
                                              topo_type, level)
                    created.append((solid, len(used), len(holes)))
                except Exception as e:
                    failed.append(str(e))
            if created:
                t.Commit()
            else:
                t.RollBack()
        except Exception as e:
            t.RollBack()
            forms.alert(u"Could not create the Toposolids:\n{0}".format(e),
                        title=_TOOL)
            return

        lines = [u"**DeeCtotopo — done (boundary mode)**", u""]
        lines.append(u"- Source: {0} (untouched)".format(
            _element_name(source) or source.GetType().Name))
        lines.append(u"- Meshes read: {0} ({1:,} triangles)".format(
            len(meshes), sum(len(t) for _v, t in meshes)))
        lines.append(u"- Surface outlines converted: {0}".format(len(regions)))
        if trace_stats[0]:
            lines.append(u"- WARNING: {0} boundary chain(s) did not close and "
                         u"were dropped - if part of a surface is missing, "
                         u"tell the developer this number.".format(trace_stats[0]))
        for solid, n_pts, n_holes in created:
            lines.append(u"- Toposolid id {0}: {1:,} points{2}".format(
                solid.Id, n_pts,
                u", {0} hole(s)".format(n_holes) if n_holes else u""))
        if failed:
            lines.append(u"- FAILED for {0} outline(s): {1}".format(
                len(failed), failed[0]))
        output.print_md(u"\n".join(lines))
        forms.alert(
            u"{0} Toposolid(s) created, one per surface outline.\n{1}"
            u"\nThe Civil 3D source was not modified.".format(
                len(created),
                u"{0} outline(s) failed - see the output window.\n".format(
                    len(failed)) if failed else u""),
            title=_TOOL)
        return

    # ---- mode B: one combined solid, convex-hull outline ---------------
    raw = _extract_points(source)
    if len(raw) < 3:
        forms.alert(
            "No 3D surface points could be read from that element.\n\n"
            "If it is a DWG, check it actually carries the surface MESH "
            "(a contours-only DWG needs its 3D polylines visible).", title=_TOOL)
        return

    points = _collapse_xy(raw)
    hull = _convex_hull_xy(points)
    used = _thin(points, _MAX_POINTS)
    if len(hull) < 3:
        forms.alert("The surface points are collinear - no footprint can be "
                    "built from them.", title=_TOOL)
        return

    topo_type, level = _choose_type_and_level(doc, min(p[2] for p in used))
    if topo_type is None or level is None:
        return

    t = Transaction(doc, "DeeCtotopo - Civil surface to Toposolid")
    t.Start()
    try:
        created = _create_toposolid(doc, [hull], used, topo_type, level)
        t.Commit()
    except Exception as e:
        t.RollBack()
        forms.alert(u"Could not create the Toposolid:\n{0}".format(e), title=_TOOL)
        return

    thinned_note = (u" (thinned from {0:,} - grid keeps the peaks)".format(len(points))
                    if len(used) < len(points) else u"")
    output.print_md(
        u"**DeeCtotopo — done (combined mode)**\n\n"
        u"- Source: {0} (untouched - hide it via VV when you are happy)\n"
        u"- Points used: {1:,}{2}\n"
        u"- Footprint: convex hull, {3} edges\n"
        u"- Toposolid: *{4}* on level *{5}* (id {6})".format(
            _element_name(source) or source.GetType().Name,
            len(used), thinned_note, len(hull),
            _element_name(topo_type), _element_name(level), created.Id))
    forms.alert(u"Toposolid created from {0:,} surface points.\n\n"
                u"The Civil 3D source was not modified - hide or remove it "
                u"once you are happy with the result.".format(len(used)),
                title=_TOOL)


main()
