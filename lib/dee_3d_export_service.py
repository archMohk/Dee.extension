# -*- coding: utf-8 -*-
"""
dee_3d_export_service
Turns one Revit 3D view into a SINGLE self-contained .html file that
opens the model in 3D on any phone, tablet or PC with a browser - no
internet, no viewer app, no account, no server.

Why a hand-written WebGL viewer instead of a library
----------------------------------------------------
The whole value of this tool is that the exported file can be emailed,
WhatsApp'd, AirDropped or dropped in OneDrive and simply opened by
someone on site. A viewer that pulls three.js (or anything else) from a
CDN stops working the moment there is no signal, and stops working
permanently if that CDN URL ever moves. So lib/dee3d_viewer.html carries
a small purpose-built WebGL 1.0 renderer inside it, and this module
injects the model data into it. The output has zero external references.

What ends up in the file
------------------------
- The heavy fields (positions, parts, element boxes) are raw-DEFLATE
  compressed behind their base64 (~3x smaller file), inflated by the
  viewer at load - see deflate_b64().
- Elements are exported BIGGEST-FIRST and small elements get an
  automatically reduced triangulation LOD (_adaptive_lod), so a
  triangle budget cuts fittings and hardware, never walls and floors.
- Triangles, quantised to 16 bits per axis against the model's own
  bounding box. For a 200 m building that is ~3 mm of positional
  detail - far finer than anything visible on a phone - and it halves
  the file against float32. Vertices are NOT indexed: once each face is
  triangulated separately, BIM meshes share very few vertices between
  faces, so an index buffer would cost more than it saved and would
  drag in the OES_element_index_uint extension.
- Normals are NOT written - the viewer recomputes them from the
  triangles at load. A few milliseconds of JavaScript against several
  megabytes of file size is an easy trade when the file is going to be
  sent over a phone connection.
- One "part" per (element, colour) pair, so a curtain wall keeps its
  glass separate from its mullions.
- Per-element category / family / type / level / id through a shared
  string table (these strings repeat enormously across a model, so the
  table typically shrinks this section by 20x or more).
- Optional per-element instance parameters, off by default because it
  is the one setting that can multiply the file size.

Units: everything geometric is written in METRES. Revit's internal unit
is feet; the conversion happens once, here.

Precision: the first vertex encountered becomes a local origin that
every other vertex is measured from before being stored as float32.
Without this, a model set out on real survey coordinates (easting in
the hundreds of thousands of metres) would lose all its detail to
float32 rounding before quantisation ever ran.

--------------------------------------------------------------------
NEEDS LIVE-REVIT VERIFICATION (written without Revit access - flagged
per this codebase's own convention)
--------------------------------------------------------------------
  1. Face.Triangulate(double) accepting a 0..1 level-of-detail value,
     and Mesh.Vertices / MeshTriangle.get_Index() being available to
     read a triangulated face by index rather than fetching each XYZ
     three times over. The by-index path is used because it roughly
     halves the number of interop calls, which is the single biggest
     cost of this whole export.
  2. Material.Transparency being the documented 0..100 integer, and
     Material.Color.IsValid actually guarding the invalid-colour case.
  3. RevitLinkInstance.GetTotalTransform() being the right transform to
     put link geometry into host coordinates (as opposed to
     GetTransform()), for links that have been moved or rotated.
  4. Whether FilteredElementCollector(doc, view.Id) drops elements cut
     away by an active section box or merely those hidden by visibility
     settings. Either way the exported model is never CUT at the section
     box - whole elements come through - which is why the viewer carries
     its own section-box sliders.
"""
import array
import base64
import io
import json
import os
import re
import sys

from Autodesk.Revit.DB import (
    FilteredElementCollector, View3D, ViewDetailLevel, Options,
    GeometryInstance, Solid, Mesh, Element, BuiltInParameter, CategoryType,
    RevitLinkInstance, StorageType,
)

# IronPython 2.7 is what Revit runs; the pure-logic half of this module
# is also imported by the standalone tests under CPython 3.
try:
    TEXT = unicode  # noqa: F821
    UCHR = unichr   # noqa: F821
    NUMS = (int, long, float)  # noqa: F821
except NameError:
    TEXT = str
    UCHR = chr
    NUMS = (int, float)

FEET_TO_M = 0.3048
QUANT_MAX = 65535.0

# StorageType.None cannot be written literally - None is a keyword in
# Python, so the attribute has to be fetched by name.
_STORAGE_NONE = getattr(StorageType, "None", None)

# (label, ViewDetailLevel member, Face.Triangulate level 0..1)
# Coarse leads because the usual reason to reach for this tool is "send
# it to someone on their phone", and curved geometry (pipes, fittings,
# furniture) is where triangle counts explode.
QUALITY_PRESETS = [
    ("Coarse - smallest file, best for phones", "Coarse", 0.12),
    ("Normal - balanced (recommended)", "Medium", 0.35),
    ("Fine - most detail, largest file", "Fine", 0.72),
]

COLOR_BY_MATERIAL = "Material (falls back to category colour)"
COLOR_BY_CATEGORY = "Category"
COLOR_MODES = [COLOR_BY_MATERIAL, COLOR_BY_CATEGORY]

# (label shown in the export dialog, mode key the viewer understands).
# The file ALWAYS carries material colours - the viewer derives the
# Category / Plain / Hatch styles itself and can switch between all four
# live, so this choice only sets which style the file OPENS in.
VIEW_MODES = [
    ("Material colours (recommended)", "material"),
    ("Category colours", "category"),
    ("Plain white (clay model)", "normal"),
    ("Hatch (sketch look)", "hatch"),
]

DEFAULT_MAX_TRIANGLES = 1500000
# The "Unlimited" checkbox's actual budget - a ceiling far past any real
# building model (2 billion triangles is ~24 GB of positions alone), so
# in practice nothing is ever cut, while build_scene's budget arithmetic
# stays intact rather than growing a special no-limit branch.
UNLIMITED_TRIANGLES = 2000000000
# Past this a mid-range phone starts to struggle with the buffers even
# though the file itself still opens. Reported, never silently enforced.
PHONE_COMFORT_TRIANGLES = 600000

_PARAM_LIMIT = 40  # per element, when parameters are requested
_TOKEN = "__DEE3D_PAYLOAD__"
BACKSLASH = chr(92)
_INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')


# ==========================================================================
# pure helpers - no Revit API, directly unit-testable
# ==========================================================================
def sanitize_filename(name):
    cleaned = _INVALID_FILENAME_CHARS.sub("_", name or "").strip()
    cleaned = cleaned.rstrip(". ")
    return cleaned or "Model"


def default_file_name(model_name, view_name):
    base = "{0}_{1}".format(sanitize_filename(model_name), sanitize_filename(view_name))
    return base[:120] + ".html"


def human_size(num_bytes):
    if num_bytes < 1024:
        return "{0} B".format(num_bytes)
    if num_bytes < 1024 * 1024:
        return "{0:.0f} KB".format(num_bytes / 1024.0)
    return "{0:.1f} MB".format(num_bytes / (1024.0 * 1024.0))


def category_palette_color(name):
    """A stable, readable colour per category name, used wherever an
    element has no usable material. Deterministic, so the same category
    is the same colour in every export, and biased light/desaturated so
    a whole model of them still reads as a building rather than a bag of
    sweets."""
    h = 0
    for ch in (name or "?"):
        h = (h * 131 + ord(ch)) & 0x7FFFFFFF
    hue = (h % 360) / 360.0
    sat = 0.26 + ((h >> 9) % 17) / 100.0
    val = 0.68 + ((h >> 17) % 20) / 100.0
    i = int(hue * 6.0)
    f = hue * 6.0 - i
    p = val * (1.0 - sat)
    q = val * (1.0 - f * sat)
    t = val * (1.0 - (1.0 - f) * sat)
    rgb = [(val, t, p), (q, val, p), (p, val, t),
           (p, q, val), (t, p, val), (val, p, q)][i % 6]
    return (int(rgb[0] * 255), int(rgb[1] * 255), int(rgb[2] * 255), 255)


def to_text(value):
    """Force anything into a real unicode string. EVERY string that
    reaches the JSON payload goes through here.

    This normalises genuine BYTE strings, which do turn up. It is NOT
    what protects json from non-ASCII: under IronPython every Revit
    string already satisfies the isinstance check below and passes
    through untouched, so _ascii_script_safe() does that job instead.

    The order matters: genuine UTF-8 bytes decode correctly first, and
    only then does it fall back to rebuilding from code points, which
    cannot fail but would mangle real UTF-8 if it ran too early.
    """
    if value is None:
        return u""
    if isinstance(value, TEXT):
        # NOTE: in IronPython 2.7 this matches EVERY Revit string, because
        # str and unicode are the SAME TYPE there (.NET strings are already
        # Unicode). So this guard cannot be what keeps non-ASCII away from
        # json - an earlier version of this module assumed it could, and
        # crashed in exactly the same place twice. The real protection is
        # _ascii_script_safe() below.
        return value
    for encoding in ("utf-8", "latin-1"):
        try:
            return value.decode(encoding)
        except Exception:
            continue
    try:
        return u"".join([UCHR(ord(c)) for c in value])
    except Exception:
        pass
    try:
        return TEXT(value)
    except Exception:
        return u""


def sanitize_payload(obj):
    """Deep copy with every string forced through to_text(). Only used
    as a retry after json refuses the fast path, so the usual export
    never pays for the walk."""
    if isinstance(obj, dict):
        return dict((to_text(k), sanitize_payload(v)) for k, v in obj.items())
    if isinstance(obj, (list, tuple)):
        return [sanitize_payload(v) for v in obj]
    if obj is None or isinstance(obj, bool) or isinstance(obj, NUMS):
        return obj
    return to_text(obj)


def _arr_bytes(arr):
    # tobytes() on newer builds, tostring() on IronPython 2.7.
    try:
        return arr.tobytes()
    except AttributeError:
        return arr.tostring()


def b64_array(arr):
    """base64 of a typed array, always little-endian. Every device that
    will ever open the output is little-endian too, but the viewer
    checks and swaps, so the two halves stay honest about it."""
    if sys.byteorder != "little":
        arr.byteswap()
    return base64.b64encode(_arr_bytes(arr)).decode("ascii")


def deflate_b64(b64_str):
    """Raw-DEFLATE the bytes behind a base64 string, return them as a new
    (much shorter) base64 string. Quantised vertex data is highly
    repetitive, so this shrinks the heavy payload fields ~3x - the
    difference between an "Unlimited" export that opens and one that
    doesn't (live report: unlimited file too large to open).

    Under IronPython the work happens in .NET (Convert/DeflateStream),
    entered VIA the base64 string on purpose: Convert.FromBase64String
    is a single native call, where converting a Python byte string to a
    .NET byte[] element-by-element would crawl through millions of
    items. Under CPython (the standalone tests) zlib provides the same
    raw stream by stripping the zlib header and checksum. Both sides
    produce headerless DEFLATE - exactly what the viewer's
    DecompressionStream("deflate-raw") (or its embedded fallback
    inflater) expects."""
    try:
        from System import Convert
        from System.IO import MemoryStream
        from System.IO.Compression import DeflateStream, CompressionMode
        raw = Convert.FromBase64String(b64_str)
        ms = MemoryStream()
        ds = DeflateStream(ms, CompressionMode.Compress)
        ds.Write(raw, 0, raw.Length)
        ds.Dispose()          # flushes the final block
        out = Convert.ToBase64String(ms.ToArray())
        ms.Dispose()
        return out
    except ImportError:
        import zlib
        raw = base64.b64decode(b64_str)
        compressed = zlib.compress(raw, 9)[2:-4]  # strip header + adler32
        return base64.b64encode(compressed).decode("ascii")


def pack_u16(values):
    return b64_array(array.array("H", values))


def pack_u32(values):
    return b64_array(array.array("I", values))


def quantize(value, lo, scale):
    if scale <= 0:
        return 0
    q = int(round((value - lo) / scale))
    if q < 0:
        return 0
    if q > 65535:
        return 65535
    return q


def quant_params(lo, hi):
    """(origin, scale) per axis for the 16-bit encoding. A degenerate
    axis (a perfectly flat model, or a single planar element) would
    divide by zero, so it is given an arbitrary non-zero span."""
    origin, scale = [], []
    for a in range(3):
        span = hi[a] - lo[a]
        if span <= 0:
            span = 1e-4
        origin.append(lo[a])
        scale.append(span / QUANT_MAX)
    return origin, scale


class StringTable(object):
    """Category / family / type / level names repeat across thousands of
    elements. Storing indices instead of the strings themselves is the
    single biggest saving in the metadata half of the file."""

    def __init__(self):
        self.items = []
        self._index = {}

    def add(self, text):
        text = to_text(text)
        got = self._index.get(text)
        if got is None:
            got = len(self.items)
            self._index[text] = got
            self.items.append(text)
        return got


# ==========================================================================
# Revit reads
# ==========================================================================
def _eid(element_id):
    """ElementId.Value (Revit 2024+, 64-bit) with pre-2024 IntegerValue
    as the fallback."""
    if element_id is None:
        return 0
    try:
        return int(element_id.Value)
    except Exception:
        pass
    try:
        return int(element_id.IntegerValue)
    except Exception:
        return 0


def element_name(element):
    if element is None:
        return ""
    try:
        return element.Name
    except Exception:
        pass
    try:
        return Element.Name.GetValue(element)
    except Exception:
        return ""


def list_3d_views(doc):
    """Every non-template 3D view, name-sorted. Perspective views are
    included - the export only reads geometry, so the camera type of the
    source view makes no difference to the result."""
    views = []
    try:
        collector = FilteredElementCollector(doc).OfClass(View3D)
    except Exception:
        return views
    for v in collector:
        try:
            if v.IsTemplate:
                continue
            views.append(v)
        except Exception:
            continue
    views.sort(key=lambda x: (element_name(x) or "").lower())
    return views


def view_label(view):
    extra = ""
    try:
        if view.IsPerspective:
            extra += " (perspective)"
    except Exception:
        pass
    try:
        if view.IsSectionBoxActive:
            extra += " (section box on)"
    except Exception:
        pass
    return "{0}{1}".format(element_name(view), extra)


def display_units(doc):
    """(metres -> display multiplier, unit label). Only used for the
    'model size' readout in the viewer, so a failure here is cosmetic."""
    try:
        from Autodesk.Revit.DB import UnitUtils, SpecTypeId, LabelUtils
        fmt = doc.GetUnits().GetFormatOptions(SpecTypeId.Length)
        unit_id = fmt.GetUnitTypeId()
        per_foot = UnitUtils.ConvertFromInternalUnits(1.0, unit_id)
        return per_foot / FEET_TO_M, to_text(LabelUtils.GetLabelForUnit(unit_id))
    except Exception:
        return 1000.0, "mm"


NO_CATEGORY = "(No category)"


def _category_name(element):
    """Grouping name for the category checklist and the per-part colour.
    Never raises and never returns empty - an element without a usable
    category still has to survive the allow-list filter in build_scene,
    which matches on this exact string."""
    try:
        name = element.Category.Name
        if name:
            return name
    except Exception:
        pass
    return NO_CATEGORY


def _is_model_element(element):
    """Deliberately permissive: anything that is not view-specific (2D
    tags, dimensions, detail items) gets a chance. The old version also
    required Category.CategoryType == Model, which silently dropped
    whole classes of real geometry - imported CAD, DirectShapes and
    generic elements with no category at all, plus the odd non-Model
    category that still owns solids. The geometry walk is the real
    filter now: an element that tessellates to nothing is discarded
    there anyway, so admitting more candidates costs iteration time,
    never file size. RevitLinkInstance is the one explicit exclusion -
    its geometry is the whole linked model, which the 'Include linked
    models' path already handles properly (with the link's transform);
    letting it through here would draw every link even when that option
    is off, and draw it twice when it is on."""
    try:
        if element.ViewSpecific:
            return False
    except Exception:
        pass
    if isinstance(element, RevitLinkInstance):
        return False
    try:
        cat = element.Category
    except Exception:
        cat = None
    if cat is None:
        return True
    try:
        # Annotation categories can never carry exportable 3D solids,
        # and skipping them keeps levels/grids/reference planes from
        # cluttering the category checklist with rows that export nothing.
        return cat.CategoryType != CategoryType.Annotation
    except Exception:
        return True


def collect_view_elements(doc, view):
    """Elements the view actually shows. The view-scoped collector is
    what makes 'hide this in the 3D view, then export' behave the way
    anyone would expect."""
    out = []
    try:
        collector = FilteredElementCollector(doc, view.Id).WhereElementIsNotElementType()
    except Exception:
        return out
    for el in collector:
        if _is_model_element(el):
            out.append(el)
    return out


def categories_of(elements):
    """[(name, count)], name-sorted - drives the pre-export category
    filter, so it must not tessellate anything."""
    counts = {}
    for el in elements:
        name = _category_name(el)
        counts[name] = counts.get(name, 0) + 1
    return sorted(counts.items(), key=lambda kv: kv[0].lower())


def transform_tuple(xf):
    """Flatten a Revit Transform into 12 plain floats, with the ORIGIN
    ALREADY CONVERTED TO METRES so it can be applied to metre-space
    vertices directly. Doing the arithmetic in Python beats calling
    Transform.OfPoint once per vertex by a wide margin - that call would
    otherwise run millions of times."""
    if xf is None:
        return None
    try:
        bx, by, bz, o = xf.BasisX, xf.BasisY, xf.BasisZ, xf.Origin
        return (bx.X, by.X, bz.X, o.X * FEET_TO_M,
                bx.Y, by.Y, bz.Y, o.Y * FEET_TO_M,
                bx.Z, by.Z, bz.Z, o.Z * FEET_TO_M)
    except Exception:
        return None


def apply_transform(m, x, y, z):
    """m is a transform_tuple; x/y/z are metres."""
    if m is None:
        return x, y, z
    return (m[0] * x + m[1] * y + m[2] * z + m[3],
            m[4] * x + m[5] * y + m[6] * z + m[7],
            m[8] * x + m[9] * y + m[10] * z + m[11])


def compose_transform(parent, child):
    """parent AFTER child: a point in the child's own local space is
    put into the child's owner's space by `child`, then into the
    ultimate host's space by `parent`. Needed for a link nested inside
    another link (see collect_link_sources) - GetTotalTransform() on a
    link instance found INSIDE another link's document is relative to
    that PARENT link's own coordinate system, not the host's, so it has
    to be composed one level at a time rather than used as-is."""
    if parent is None:
        return child
    if child is None:
        return parent
    pr = ((parent[0], parent[1], parent[2]), (parent[4], parent[5], parent[6]),
          (parent[8], parent[9], parent[10]))
    pt = (parent[3], parent[7], parent[11])
    cr = ((child[0], child[1], child[2]), (child[4], child[5], child[6]),
          (child[8], child[9], child[10]))
    ct = (child[3], child[7], child[11])
    r = [[sum(pr[i][k] * cr[k][j] for k in range(3)) for j in range(3)] for i in range(3)]
    t = [sum(pr[i][k] * ct[k] for k in range(3)) + pt[i] for i in range(3)]
    return (r[0][0], r[0][1], r[0][2], t[0],
            r[1][0], r[1][1], r[1][2], t[1],
            r[2][0], r[2][1], r[2][2], t[2])


def collect_link_sources(doc, view, _parent_xf=None, _seen=None, _depth=0):
    """[(link_name, link_doc, transform_tuple)] for every loaded link
    visible in the view, INCLUDING links nested inside another link (a
    link loaded inside a linked document, not the host) - the normal
    shape of a federated coordination model where MEP/structural links
    are attached to one architectural link rather than all being linked
    directly into the host. Unloaded links are skipped silently - there
    is nothing to read from them.

    NEEDS LIVE-REVIT VERIFICATION: the nested-recursion branch (_depth
    > 0) and compose_transform() above it. GetTotalTransform() on a
    directly-hosted link is already relied on elsewhere in this file
    (see the module docstring); whether it behaves the same way one
    level down, inside another link's own document, has not been
    confirmed against a real nested-link file - written from the
    documented Transform-composition pattern, not tested live. A
    circular or self-referencing link chain is guarded against with
    _seen (by document identity) and a depth cap, so a bad file can at
    worst return an incomplete list, never loop forever."""
    out = []
    if _seen is None:
        _seen = set()
    if _depth > 6:
        return out
    try:
        if _depth == 0:
            collector = FilteredElementCollector(doc, view.Id).OfClass(RevitLinkInstance)
        else:
            collector = FilteredElementCollector(doc).OfClass(RevitLinkInstance)
    except Exception:
        return out
    for li in collector:
        try:
            link_doc = li.GetLinkDocument()
            if link_doc is None:
                continue
            key = id(link_doc)
            if key in _seen:
                continue
            _seen.add(key)
            local_xf = transform_tuple(li.GetTotalTransform())
            combined_xf = compose_transform(_parent_xf, local_xf)
            out.append((element_name(li), link_doc, combined_xf))
            out.extend(collect_link_sources(link_doc, None, combined_xf, _seen, _depth + 1))
        except Exception:
            continue
    return out


def collect_link_elements(doc, view):
    """Every model element build_scene() would tessellate from linked
    documents, pooled flat across all visible links. Exists so the
    pre-export category checklist can be built from the SAME set the
    actual export uses - previously it only ever scanned the host model,
    so any category that only existed in a link (a common case: a
    structural/MEP link with categories the architectural host does not
    have) was silently excluded from the allow-list and the whole link
    could vanish from the export with no warning."""
    out = []
    for _name, link_doc, _xf in collect_link_sources(doc, view):
        try:
            for el in FilteredElementCollector(link_doc).WhereElementIsNotElementType():
                if _is_model_element(el):
                    out.append(el)
        except Exception:
            continue
    return out


# ==========================================================================
# colours
# ==========================================================================
class ColorBook(object):
    def __init__(self, doc, mode):
        self.doc = doc
        self.mode = mode
        self.colors = []
        self._index = {}
        self._mat_cache = {}

    def intern(self, rgba):
        key = tuple(rgba)
        got = self._index.get(key)
        if got is None:
            got = len(self.colors)
            self._index[key] = got
            self.colors.append(list(key))
        return got

    def _from_material(self, mat_id):
        key = _eid(mat_id)
        if key in self._mat_cache:
            return self._mat_cache[key]
        rgba = None
        try:
            mat = self.doc.GetElement(mat_id)
            if mat is not None:
                col = mat.Color
                if col is not None and col.IsValid:
                    try:
                        pct = max(0, min(100, int(mat.Transparency)))
                    except Exception:
                        pct = 0
                    # Fully transparent glass would be invisible AND
                    # unpickable, so the alpha floor keeps it selectable.
                    alpha = max(28, int(round(255.0 * (1.0 - pct / 100.0))))
                    rgba = (int(col.Red), int(col.Green), int(col.Blue), alpha)
        except Exception:
            rgba = None
        self._mat_cache[key] = rgba
        return rgba

    def for_face(self, element, face_material_id, category_name):
        if self.mode == COLOR_BY_CATEGORY:
            return self.intern(category_palette_color(category_name))
        if face_material_id is not None and _eid(face_material_id) > 0:
            rgba = self._from_material(face_material_id)
            if rgba:
                return self.intern(rgba)
        try:
            cat = element.Category
            if cat is not None and cat.Material is not None:
                rgba = self._from_material(cat.Material.Id)
                if rgba:
                    return self.intern(rgba)
        except Exception:
            pass
        return self.intern(category_palette_color(category_name))


# ==========================================================================
# scene building
# ==========================================================================
class Scene(object):
    def __init__(self):
        self.strings = StringTable()
        self.book = None
        self.els = []            # [catS, famS, typeS, lvlS, id, srcS]
        self.ebox = []           # 6 floats per element (metres, origin-local)
        self.parts_opaque = []   # [elem, color, triStart, triCount]
        self.parts_trans = []
        self.f_opaque = array.array("f")
        self.f_trans = array.array("f")
        self.params = {}
        self.tri_opaque = 0
        self.tri_trans = 0
        self.origin = None       # metres, subtracted from every vertex
        self.bounds = [1e30, 1e30, 1e30, -1e30, -1e30, -1e30]
        self.notes = []
        self.hit_budget = False
        self.cancelled = False
        self.sources = []        # [label, elements found, elements exported] per source

    @property
    def triangles(self):
        return self.tri_opaque + self.tri_trans

    def grow_bounds(self, box):
        for a in range(3):
            if box[a] < self.bounds[a]:
                self.bounds[a] = box[a]
            if box[a + 3] > self.bounds[a + 3]:
                self.bounds[a + 3] = box[a + 3]


def _element_meta(doc, element, scene, source_label, cat_name):
    fam_name, type_name = "", ""
    try:
        etype = doc.GetElement(element.GetTypeId())
        if etype is not None:
            type_name = element_name(etype)
            try:
                fam_name = etype.FamilyName or ""
            except Exception:
                fam_name = ""
    except Exception:
        pass

    level_name = ""
    try:
        lvl = doc.GetElement(element.LevelId)
        if lvl is not None:
            level_name = element_name(lvl)
    except Exception:
        pass
    if not level_name:
        for bip in (BuiltInParameter.SCHEDULE_LEVEL_PARAM,
                    BuiltInParameter.FAMILY_LEVEL_PARAM,
                    BuiltInParameter.LEVEL_PARAM):
            try:
                p = element.get_Parameter(bip)
                if p is not None:
                    lvl = doc.GetElement(p.AsElementId())
                    if lvl is not None:
                        level_name = element_name(lvl)
                        break
            except Exception:
                continue

    add = scene.strings.add
    return [add(cat_name), add(fam_name), add(type_name), add(level_name),
            _eid(element.Id), add(source_label)]


def _element_params(element, scene):
    """Instance parameters as display strings. Deliberately capped -
    this is the one option that can multiply the file size, and nobody
    reads the 200th parameter on a phone."""
    out = []
    try:
        params = element.Parameters
    except Exception:
        return out
    add = scene.strings.add
    for p in params:
        if len(out) >= _PARAM_LIMIT:
            break
        try:
            if _STORAGE_NONE is not None and p.StorageType == _STORAGE_NONE:
                continue
            value = p.AsValueString()
            if value is None:
                value = p.AsString()
            if value is None and p.StorageType == StorageType.Integer:
                value = str(p.AsInteger())
            if not value:
                continue
            out.append([add(p.Definition.Name), add(value)])
        except Exception:
            continue
    return out


def _element_diag_ft(element):
    """Rough bounding-box diagonal in FEET (model-wide bbox, view-
    independent). 0.0 when no box is available - such elements sort
    last and get the smallest LOD, which is the right default for the
    geometry-less stragglers that produce nothing anyway."""
    try:
        bb = element.get_BoundingBox(None)
        if bb is None:
            return 0.0
        dx = bb.Max.X - bb.Min.X
        dy = bb.Max.Y - bb.Min.Y
        dz = bb.Max.Z - bb.Min.Z
        return (dx * dx + dy * dy + dz * dz) ** 0.5
    except Exception:
        return 0.0


# Adaptive detail: an element's own size decides how finely its curved
# faces are triangulated. A door handle at Fine quality can cost more
# triangles than the whole wall it sits on, and nobody zooms a phone
# into a valve - so small elements get a fraction of the chosen LOD.
# Thresholds in metres of bounding-box diagonal.
_LOD_SMALL_M, _LOD_SMALL_FACTOR = 0.6, 0.35
_LOD_MEDIUM_M, _LOD_MEDIUM_FACTOR = 2.5, 0.65


def _adaptive_lod(base_lod, diag_ft):
    diag_m = diag_ft * FEET_TO_M
    if diag_m < _LOD_SMALL_M:
        return max(0.05, base_lod * _LOD_SMALL_FACTOR)
    if diag_m < _LOD_MEDIUM_M:
        return max(0.05, base_lod * _LOD_MEDIUM_FACTOR)
    return base_lod


def _geometry_options(detail_name):
    opts = Options()
    opts.ComputeReferences = False
    opts.IncludeNonVisibleObjects = False
    # The view is deliberately NOT assigned to Options: doing so takes
    # the detail level from the view and makes the quality setting in
    # this tool a lie. View-specific graphic OVERRIDES are lost as a
    # result; element visibility is not, because the collector is
    # already view-scoped.
    try:
        opts.DetailLevel = getattr(ViewDetailLevel, detail_name)
    except Exception:
        pass
    return opts


def _emit_mesh(mesh, xform, scene, arr, box):
    """Appends triangles to `arr` (a flat float array) in metres relative
    to the scene origin, growing `box`. Returns the triangle count."""
    try:
        count = int(mesh.NumTriangles)
    except Exception:
        return 0
    if count <= 0:
        return 0

    # Read each vertex once by index rather than three times per
    # triangle - this is the hot loop of the whole export.
    try:
        pts = [(p.X * FEET_TO_M, p.Y * FEET_TO_M, p.Z * FEET_TO_M) for p in mesh.Vertices]
    except Exception:
        return 0
    if not pts:
        return 0

    if xform is not None:
        pts = [apply_transform(xform, x, y, z) for (x, y, z) in pts]

    if scene.origin is None:
        scene.origin = pts[0]
    ox, oy, oz = scene.origin
    pts = [(x - ox, y - oy, z - oz) for (x, y, z) in pts]
    limit = len(pts)

    written = 0
    for i in range(count):
        try:
            tri = mesh.get_Triangle(i)
            idx = (int(tri.get_Index(0)), int(tri.get_Index(1)), int(tri.get_Index(2)))
        except Exception:
            continue
        if idx[0] >= limit or idx[1] >= limit or idx[2] >= limit:
            continue
        if idx[0] < 0 or idx[1] < 0 or idx[2] < 0:
            continue
        for k in idx:
            x, y, z = pts[k]
            arr.append(x)
            arr.append(y)
            arr.append(z)
            if x < box[0]:
                box[0] = x
            if y < box[1]:
                box[1] = y
            if z < box[2]:
                box[2] = z
            if x > box[3]:
                box[3] = x
            if y > box[4]:
                box[4] = y
            if z > box[5]:
                box[5] = z
        written += 1
    return written


def _emit(mesh, xform, color_index, scene, buckets, box):
    alpha = scene.book.colors[color_index][3]
    bucket = buckets["trans" if alpha < 250 else "opaque"]
    start = bucket["tris"]
    written = _emit_mesh(mesh, xform, scene, bucket["arr"], box)
    if written <= 0:
        return
    bucket["tris"] = start + written
    bucket["parts"].append([bucket["elem"], color_index, start, written])


def _emit_solid(solid, xform, element, scene, cat_name, lod, buckets, box):
    try:
        faces = solid.Faces
        if faces is None or faces.Size == 0:
            return
    except Exception:
        return
    for face in faces:
        mesh = None
        try:
            mesh = face.Triangulate(lod)
        except Exception:
            try:
                mesh = face.Triangulate()
            except Exception:
                mesh = None
        if mesh is None:
            continue
        try:
            mat_id = face.MaterialElementId
        except Exception:
            mat_id = None
        _emit(mesh, xform, scene.book.for_face(element, mat_id, cat_name),
              scene, buckets, box)


def _walk_geometry(geom, xform, element, scene, cat_name, lod, buckets, box, depth=0):
    """Recurses into GeometryInstance (family geometry). GetInstance-
    Geometry() already returns geometry positioned in the element's own
    coordinates, so the instance transform must NOT be applied again on
    top - only a link transform (`xform`) is ever applied here."""
    if geom is None or depth > 8:
        return
    for obj in geom:
        try:
            if isinstance(obj, GeometryInstance):
                _walk_geometry(obj.GetInstanceGeometry(), xform, element, scene,
                               cat_name, lod, buckets, box, depth + 1)
            elif isinstance(obj, Solid):
                _emit_solid(obj, xform, element, scene, cat_name, lod, buckets, box)
            elif isinstance(obj, Mesh):
                _emit(obj, xform, scene.book.for_face(element, None, cat_name),
                      scene, buckets, box)
        except Exception:
            continue


def build_scene(doc, view, options, progress=None):
    """options keys:
        detail          ViewDetailLevel member name
        lod             0..1 for Face.Triangulate
        view_mode       a VIEW_MODES key - only recorded in the payload
                        (colours are always baked by material)
        categories      set of allowed category names, or None for all
        include_links   bool
        include_params  bool
        max_triangles   int
    progress(done, total, label) -> return False to cancel.
    """
    scene = Scene()
    # Material colours are always what gets baked, whatever display style
    # the export dialog picked - the viewer needs them to offer its
    # Material style at all, and it derives Category/Plain/Hatch from the
    # category names it already carries.
    scene.book = ColorBook(doc, COLOR_BY_MATERIAL)

    allowed = options.get("categories")
    lod = float(options.get("lod", 0.35))
    max_tris = int(options.get("max_triangles", DEFAULT_MAX_TRIANGLES))
    geom_opts = _geometry_options(options.get("detail", "Medium"))

    sources = [("Host model", doc, None, collect_view_elements(doc, view))]
    if options.get("include_links"):
        for link_name, link_doc, xf in collect_link_sources(doc, view):
            link_elements = []
            try:
                for el in FilteredElementCollector(link_doc).WhereElementIsNotElementType():
                    if _is_model_element(el):
                        link_elements.append(el)
            except Exception:
                link_elements = []
            # Kept even when empty - a link contributing genuinely
            # nothing (unloaded, or no model geometry) is exactly what
            # the per-source counts below need to be able to report; a
            # link silently dropped here instead of showing "0 found"
            # is indistinguishable from one that was never found at all.
            sources.append((link_name, link_doc, xf, link_elements))

    total = sum(len(s[3]) for s in sources) or 1
    done = 0

    # [label, elements found, elements actually exported] per source,
    # updated live (not just at the end) so a cancelled or budget-cut
    # export still reports accurate partial counts. This is the one
    # thing that turns "a link's elements silently did not show up"
    # from a mystery into something visible in the export report - a
    # source sitting at 0 exported despite N found points straight at
    # category filtering; 0 found at all points at the link itself
    # (unloaded, or genuinely empty of model geometry).
    scene.sources = []
    source_row = {}
    for source_label, _doc, _xf, elements in sources:
        source_row[source_label] = [source_label, len(elements), 0]
        scene.sources.append(source_row[source_label])

    for source_label, source_doc, xform, elements in sources:
        # Biggest elements first (bounding-box diagonal, one cheap native
        # call each). When the triangle budget cuts an export short, it's
        # now bolts and fittings that fall off the end - never the walls,
        # floors and roofs that make the model recognizable. (Live report:
        # a budget-cut export looked like "too many elements not shown"
        # because the cut order used to be whatever the collector felt
        # like.) The same size feeds the adaptive LOD below.
        sized = [(el, _element_diag_ft(el)) for el in elements]
        sized.sort(key=lambda pair: -pair[1])

        for element, diag_ft in sized:
            done += 1
            if progress is not None and (done % 25 == 0 or done == total):
                if progress(done, total, source_label) is False:
                    scene.cancelled = True
                    scene.notes.append(
                        "Cancelled after {0} of {1} elements.".format(done, total))
                    return scene

            if scene.triangles >= max_tris:
                scene.hit_budget = True
                scene.notes.append(
                    "Stopped at the {0:,} triangle limit - {1:,} of {2:,} elements made "
                    "it in. Elements are exported biggest-first, so what was left out "
                    "is the smallest detail (fittings, hardware), not walls or floors. "
                    "Raise the limit, lower the mesh quality, or untick categories to "
                    "fit more.".format(max_tris, done, total))
                return scene

            cat_name = _category_name(element)
            if allowed is not None and cat_name not in allowed:
                continue

            try:
                geom = element.get_Geometry(geom_opts)
            except Exception:
                continue
            if geom is None:
                continue

            elem_index = len(scene.els)
            box = [1e30, 1e30, 1e30, -1e30, -1e30, -1e30]
            buckets = {
                "opaque": {"arr": scene.f_opaque, "parts": scene.parts_opaque,
                           "tris": scene.tri_opaque, "elem": elem_index},
                "trans": {"arr": scene.f_trans, "parts": scene.parts_trans,
                          "tris": scene.tri_trans, "elem": elem_index},
            }

            _walk_geometry(geom, xform, element, scene, cat_name,
                           _adaptive_lod(lod, diag_ft), buckets, box)

            scene.tri_opaque = buckets["opaque"]["tris"]
            scene.tri_trans = buckets["trans"]["tris"]

            if box[0] > box[3]:
                # Nothing tessellated - a line-based or otherwise
                # geometry-free element that got past the model filter.
                # No parts were appended for it either (an empty mesh
                # never reaches bucket["parts"]), so the element index is
                # still free for whatever comes next.
                continue

            scene.els.append(_element_meta(source_doc, element, scene, source_label, cat_name))
            scene.ebox.extend(box)
            scene.grow_bounds(box)
            source_row[source_label][2] += 1
            if options.get("include_params"):
                params = _element_params(element, scene)
                if params:
                    scene.params[str(elem_index)] = params

    return scene


# ==========================================================================
# payload + file writing
# ==========================================================================
def build_payload(doc, scene, meta):
    """meta: dict with model / view / date, for the viewer's Model panel."""
    if scene.triangles <= 0:
        raise ValueError(
            "This view produced no 3D geometry. Check that the view is not empty, "
            "that its section box is not cutting everything away, and that at least "
            "one category is still ticked.")

    lo = scene.bounds[0:3]
    hi = scene.bounds[3:6]
    origin, scale = quant_params(lo, hi)
    hi = [origin[a] + scale[a] * QUANT_MAX for a in range(3)]

    inv = [1.0 / scale[a] for a in range(3)]
    quantised = array.array("H")
    push = quantised.append
    for arr in (scene.f_opaque, scene.f_trans):
        for i in range(0, len(arr), 3):
            for a in range(3):
                q = int((arr[i + a] - origin[a]) * inv[a] + 0.5)
                push(0 if q < 0 else (65535 if q > 65535 else q))

    ebox_q = array.array("H")
    for i in range(0, len(scene.ebox), 3):
        for a in range(3):
            ebox_q.append(quantize(scene.ebox[i + a], origin[a], scale[a]))

    # Transparent parts sit after every opaque one in the vertex buffer,
    # so the viewer can draw the model in two passes (opaque with depth
    # writes on, then blended) without sorting anything at run time.
    parts = array.array("I")
    for p in scene.parts_opaque:
        parts.extend([p[0], p[1], p[2], p[3]])
    for p in scene.parts_trans:
        parts.extend([p[0], p[1], p[2] + scene.tri_opaque, p[3]])

    disp_per_m, disp_unit = display_units(doc)

    payload = {
        "v": 2,
        # cmp=1: pos/ebox/parts are raw-DEFLATE compressed behind their
        # base64 (see deflate_b64) - the viewer inflates them at load.
        "cmp": 1,
        "model": to_text(meta.get("model", "")),
        "view": to_text(meta.get("view", "")),
        "date": to_text(meta.get("date", "")),
        "mode": to_text(meta.get("mode", "material")),
        "tris": scene.triangles,
        "qmin": origin,
        "qscale": scale,
        "bmin": list(origin),
        "bmax": hi,
        "dispPerM": disp_per_m,
        "dispUnit": to_text(disp_unit),
        "opaqueVerts": scene.tri_opaque * 3,
        "pos": deflate_b64(b64_array(quantised)),
        "ebox": deflate_b64(b64_array(ebox_q)),
        "parts": deflate_b64(b64_array(parts)),
        "colors": scene.book.colors,
        "strings": scene.strings.items,
        "els": scene.els,
    }
    if scene.params:
        payload["prm"] = scene.params
    return payload


def template_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "dee3d_viewer.html")


def render_html(payload, template_text):
    """Splitting this out from the file write is what lets the tests
    check the substitution without touching the disk."""
    occurrences = template_text.count(_TOKEN)
    if occurrences != 1:
        raise ValueError(
            "The viewer template is not usable: expected exactly one {0} "
            "placeholder, found {1}.".format(_TOKEN, occurrences))
    # The finished file is pure ASCII even when the model is full of
    # Arabic room names - _ascii_script_safe() turns every non-ASCII
    # character into an escape inside the JavaScript string, removing any
    # chance of the file arriving mojibaked on a phone that guessed the
    # wrong encoding at a file:// URL.
    try:
        # ensure_ascii is deliberately FALSE. With it True, json reacts
        # to a non-ASCII string by calling s.decode("utf-8") on it
        # (json/encoder.py, py_encode_basestring_ascii) - and under
        # IronPython that raises "'unknown' codec can't decode byte 0xb3",
        # a live crash this tool hit on a cubic-metre parameter value.
        # The escaping json would have done happens in
        # _ascii_script_safe() instead, where it cannot fail.
        blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except (UnicodeDecodeError, UnicodeEncodeError, TypeError):
        # Something reached the payload as a non-ASCII byte string
        # despite to_text() at every known source. IronPython raises
        # UnicodeDecodeError here; CPython 3 raises TypeError for the
        # same shape of problem. Rather than fail a whole export over one
        # stray parameter value, normalise the structure and try again -
        # an odd value showing up as text in the file is a far better
        # outcome for the person waiting on the export than no file.
        blob = json.dumps(sanitize_payload(payload), ensure_ascii=False,
                          separators=(",", ":"))
    return template_text.replace(_TOKEN, _ascii_script_safe(blob))


_NON_ASCII_RE = re.compile(u"[^\u0000-\u007f]")


def _escape_one(match):
    """A JSON u-escape is exactly four hex digits, so anything above the
    BMP has to become a surrogate PAIR. Under IronPython this branch
    never fires - .NET strings are UTF-16, so an astral character is
    already two units - but on a wide Python build ord() returns the
    whole code point and a naive five-digit escape would silently
    corrupt the file."""
    cp = ord(match.group(0))
    if cp > 0xFFFF:
        cp -= 0x10000
        return "{0}u{1:04x}{0}u{2:04x}".format(
            BACKSLASH, 0xD800 + (cp >> 10), 0xDC00 + (cp & 0x3FF))
    return "{0}u{1:04x}".format(BACKSLASH, cp)


def _ascii_script_safe(blob):
    """Make a JSON document pure ASCII and safe to paste inside a <script>
    block. Two problems, one pass:

    1. Non-ASCII. Doing it here instead of via ensure_ascii keeps json off
       its decode path entirely (see render_html above). A regex
       substitution scans at native speed and only calls back on real
       matches, which matters because the blob can be tens of megabytes of
       base64 that needs no work at all. A surrogate pair escapes as two
       units, which is exactly what JSON wants.
    2. '<' and '>'. json escapes quotes and backslashes but not these, so
       a Revit type name or comment containing a close-tag would end the
       script block early and leave a blank white page - the viewer
       silently gone, with no error anywhere.

    Both rewrites are lossless: the blob is JSON, so these characters can
    only occur inside string literals, where a u-escape means exactly the
    same thing.
    """
    blob = _NON_ASCII_RE.sub(_escape_one, blob)
    blob = blob.replace("<", "{0}u003c".format(BACKSLASH))
    return blob.replace(">", "{0}u003e".format(BACKSLASH))


def write_html(payload, out_path, template_file=None):
    template_file = template_file or template_path()
    with io.open(template_file, "r", encoding="utf-8") as fh:
        template = fh.read()
    html = render_html(payload, template)

    folder = os.path.dirname(out_path)
    if folder and not os.path.isdir(folder):
        os.makedirs(folder)
    with io.open(out_path, "w", encoding="utf-8", newline="") as fh:
        fh.write(html)
    return os.path.getsize(out_path)
