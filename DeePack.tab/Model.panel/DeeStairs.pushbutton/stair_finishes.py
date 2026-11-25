# -*- coding: utf-8 -*-
"""
DeeStairs - finishes (pure Python: no Revit, no WPF, so it is tested outside Revit).

  MATERIALS   the finishes DeeStairs can make as Revit materials: name, shading
              colour, material class, transparency, and the words to look for in the
              project's own appearance assets (so a new material can borrow a
              realistic look when the project has one).
  PRESETS     complete stair build-ups taken from real stairs: which material goes on
              the tread, the riser, the stringers and the landing, plus the finished
              thicknesses, the nosing, open or closed risers and the kind of support.
  support_style / match_support    map the words "closed", "open", "carriage" onto the
              names of the project's stair support types.
  section()   the side-view sketch of a build-up (treads, risers, nosing, stringer)
              that the window draws, like the detail sections it is based on.

All dimensions here are millimetres, in the plan/section frame x = going, z = up.
The script converts to Revit's feet.
"""
import math
import re

MATERIAL_PREFIX = u"Dee - "

# the keys are what PRESETS refer to; the order is the order of the combo boxes
MATERIALS = [
    {"key": "cherry",    "name": u"Cherry wood",        "rgb": (146, 78, 52),   "cls": u"Wood",     "find": ("cherry",)},
    {"key": "oak",       "name": u"Oak",                "rgb": (198, 156, 108), "cls": u"Wood",     "find": ("oak",)},
    {"key": "walnut",    "name": u"Walnut",             "rgb": (94, 66, 50),    "cls": u"Wood",     "find": ("walnut",)},
    {"key": "ash",       "name": u"Ash (light timber)", "rgb": (226, 205, 165), "cls": u"Wood",     "find": ("ash", "maple", "birch")},
    {"key": "white",     "name": u"White painted",      "rgb": (244, 244, 240), "cls": u"Generic",  "find": ()},
    {"key": "marble",    "name": u"Marble (Carrara)",   "rgb": (226, 228, 226), "cls": u"Stone",    "find": ("marble",)},
    {"key": "granite",   "name": u"Granite",            "rgb": (112, 114, 120), "cls": u"Stone",    "find": ("granite",)},
    {"key": "concrete",  "name": u"Concrete",           "rgb": (168, 168, 164), "cls": u"Concrete", "find": ("concrete",)},
    {"key": "steel",     "name": u"Dark steel",         "rgb": (58, 60, 64),    "cls": u"Metal",    "find": ("steel", "metal"),
     "avoid": ("stainless", "chrome", "polished", "galvanized", "aluminum", "aluminium")},
    {"key": "stainless", "name": u"Stainless steel",    "rgb": (196, 200, 204), "cls": u"Metal",    "find": ("stainless", "chrome")},
    {"key": "brass",     "name": u"Brass",              "rgb": (181, 148, 72),  "cls": u"Metal",    "find": ("brass", "bronze")},
    {"key": "glass",     "name": u"Glass",              "rgb": (186, 220, 230), "cls": u"Glass",    "find": ("glass",),
     "transparency": 60},
]
MATERIAL_BY_KEY = dict((m["key"], m) for m in MATERIALS)

# Support styles: "closed" (stringer along the side of the run), "open" (sawtooth
# stringer under the treads), "carriage" (in the middle), "none".
PRESETS = [
    {"key": "cherry", "short": u"Cherry", "name": u"Cherry wood - tread, riser and stringers",
     "info": u"Cherry on the tread, the riser and the closed stringers, as in the cherry-finish detail: "
             u"38 mm tread, 19 mm riser, 19 mm nosing.",
     "tread": "cherry", "riser": "cherry", "stringer": "cherry", "landing": "cherry",
     "tread_thk": 38, "riser_thk": 19, "nosing": 19, "open": False,
     "left": "closed", "right": "closed", "mid": "none"},

    {"key": "oak_white", "short": u"Oak and white", "name": u"Oak treads, white painted risers and stringers",
     "info": u"Warm oak treads on white risers and white closed stringers - the lit timber-and-white stair: "
             u"40 mm treads, 20 mm risers, 25 mm nosing.",
     "tread": "oak", "riser": "white", "stringer": "white", "landing": "oak",
     "tread_thk": 40, "riser_thk": 20, "nosing": 25, "open": False,
     "left": "closed", "right": "closed", "mid": "none"},

    {"key": "timber_steel", "short": u"Timber on steel", "name": u"Timber treads on folded steel stringers",
     "info": u"Timber treads and risers on thin dark-steel stringers that follow the steps (the folded-plate stair): "
             u"45 mm treads, 20 mm risers, 10 mm steel.",
     "tread": "oak", "riser": "oak", "stringer": "steel", "landing": "oak",
     "tread_thk": 45, "riser_thk": 20, "nosing": 0, "open": False,
     "left": "open", "right": "open", "mid": "none", "stringer_w": 10},

    {"key": "carriage", "short": u"Steel carriage", "name": u"Timber treads on a steel carriage, open risers",
     "info": u"Thick walnut treads carried by one dark-steel carriage down the middle, no riser boards and no "
             u"side stringers: 50 mm treads.",
     "tread": "walnut", "riser": "walnut", "stringer": "steel", "landing": "walnut",
     "tread_thk": 50, "riser_thk": None, "nosing": 0, "open": True,
     "left": "none", "right": "none", "mid": "carriage", "midn": 1},

    {"key": "floating", "short": u"Floating timber", "name": u"Floating timber treads, no risers or stringers",
     "info": u"Thick slab treads that look as if they float from the wall: 70 mm oak, open risers, no nosing, "
             u"no stringers (fix the treads to the wall in the model).",
     "tread": "oak", "riser": "oak", "stringer": None, "landing": "oak",
     "tread_thk": 70, "riser_thk": None, "nosing": 0, "open": True,
     "left": "none", "right": "none", "mid": "none"},

    {"key": "marble", "short": u"Marble", "name": u"Marble - tread, riser and stringers",
     "info": u"Marble on every part, with heavy closed marble stringers on both sides: 30 mm treads, 20 mm risers, "
             u"20 mm nosing, 100 mm flanks.",
     "tread": "marble", "riser": "marble", "stringer": "marble", "landing": "marble",
     "tread_thk": 30, "riser_thk": 20, "nosing": 20, "open": False,
     "left": "closed", "right": "closed", "mid": "none", "stringer_w": 100},

    {"key": "granite", "short": u"Granite", "name": u"Granite - tread, riser and stringers",
     "info": u"Granite on every part: 40 mm treads, 25 mm risers, 25 mm nosing, 100 mm closed stringers.",
     "tread": "granite", "riser": "granite", "stringer": "granite", "landing": "granite",
     "tread_thk": 40, "riser_thk": 25, "nosing": 25, "open": False,
     "left": "closed", "right": "closed", "mid": "none", "stringer_w": 100},

    {"key": "concrete", "short": u"Concrete", "name": u"Exposed concrete",
     "info": u"Concrete treads, risers and closed stringers: 60 mm treads, 40 mm risers, no nosing. A monolithic "
             u"stair type takes the tread material for the whole run.",
     "tread": "concrete", "riser": "concrete", "stringer": "concrete", "landing": "concrete",
     "tread_thk": 60, "riser_thk": 40, "nosing": 0, "open": False,
     "left": "closed", "right": "closed", "mid": "none", "stringer_w": 120},

    {"key": "glass_steel", "short": u"Glass and steel", "name": u"Glass treads on steel stringers, open risers",
     "info": u"Glass treads (36 mm) between two thin dark-steel closed stringers, open risers, no nosing.",
     "tread": "glass", "riser": "glass", "stringer": "steel", "landing": "glass",
     "tread_thk": 36, "riser_thk": None, "nosing": 0, "open": True,
     "left": "closed", "right": "closed", "mid": "none", "stringer_w": 15},

    {"key": "white_paint", "short": u"White painted", "name": u"White painted - tread, riser and stringers",
     "info": u"One painted finish on every part: 38 mm treads, 19 mm risers, 19 mm nosing, closed stringers.",
     "tread": "white", "riser": "white", "stringer": "white", "landing": "white",
     "tread_thk": 38, "riser_thk": 19, "nosing": 19, "open": False,
     "left": "closed", "right": "closed", "mid": "none"},
]
PRESET_BY_KEY = dict((p["key"], p) for p in PRESETS)

NEUTRAL_RGB = (176, 176, 176)


# --------------------------------------------------------------------------
# materials
# --------------------------------------------------------------------------
def material_name(spec):
    """The Revit material name for a catalogue material."""
    return MATERIAL_PREFIX + spec["name"]


def rgb_hex(rgb):
    return u"#{0:02X}{1:02X}{2:02X}".format(int(rgb[0]), int(rgb[1]), int(rgb[2]))


def name_matches(name, words):
    """True when one of `words` is a word of `name` (whole word, or a prefix of a
    word for the longer search words: 'marble' finds 'Marble - Carrara')."""
    tokens = re.findall(r"[a-z0-9]+", (name or u"").lower())
    for w in words:
        for t in tokens:
            if t == w or (len(w) >= 5 and t.startswith(w)):
                return True
    return False


def best_asset_name(names, words, avoid=()):
    """The shortest of `names` that matches one of the words and none of the
    `avoid` words (the plainest one), ties broken alphabetically; None when
    nothing matches."""
    hits = [n for n in names if name_matches(n, words) and not name_matches(n, avoid)]
    if not hits:
        return None
    hits.sort(key=lambda n: (len(n), n.lower()))
    return hits[0]


# --------------------------------------------------------------------------
# supports
# --------------------------------------------------------------------------
def support_style(name):
    """'closed' | 'open' | 'carriage' | None for a support type's name."""
    n = (name or u"").lower()
    if "carriage" in n:
        return "carriage"
    if "closed" in n:
        return "closed"
    if "open" in n:
        return "open"
    if "stringer" in n:
        return "closed"
    return None


def match_support(names, style):
    """Index of the first support type name of the given style, else None.
    'open' means an open stringer (not an open carriage)."""
    for i, n in enumerate(names):
        if support_style(n) == style:
            return i
    return None


def section_stringer(left, right, mid):
    """The one stringer style to sketch from the three supports' styles
    (each 'closed' / 'open' / 'carriage' / 'none' / None)."""
    for s in (left, right):
        if s in ("closed", "open", "carriage"):
            return s
    if mid in ("closed", "open", "carriage"):
        return "carriage"
    return "none"


# --------------------------------------------------------------------------
# the section sketch
# --------------------------------------------------------------------------
def _rect(x0, z0, x1, z1):
    return [(x0, z0), (x1, z0), (x1, z1), (x0, z1)]


def section(R, T, tread_thk, riser_thk, nosing, has_riser, stringer="none", steps=4):
    """Side-view sketch of `steps` steps: riser height R, going T, finished tread
    thickness, riser thickness, nosing overhang (all mm). Each tread sits on its
    riser, overhangs it by the nosing and runs back under the next riser. Returns
    {'treads','risers','stringer','floor': [polygons of (x, z)], 'bbox': (x0, z0, x1, z1)}."""
    R = float(R)
    T = float(T)
    steps = max(1, int(steps))
    tt = max(0.0, float(tread_thk or 0.0))
    tt = min(tt, R * 0.9)
    rt = max(0.0, float(riser_thk or 0.0)) if has_riser else 0.0
    ns = max(0.0, float(nosing or 0.0))
    out = {"treads": [], "risers": [], "stringer": [], "floor": []}
    tt_draw = tt if tt > 0.0 else max(4.0, R * 0.04)       # a zero thickness still shows

    for i in range(steps):
        x = i * T
        top = (i + 1) * R
        out["treads"].append(_rect(x, top - tt_draw, x + T + ns + rt, top))
        if has_riser and rt > 0.0:
            out["risers"].append(_rect(x + ns, i * R, x + ns + rt, top - tt_draw))

    floor_end = (ns + rt) if has_riser else 0.0
    out["floor"].append(_rect(-1.2 * T, -60.0, floor_end, 0.0))

    if stringer in ("closed", "carriage"):
        depth = 150.0 if stringer == "closed" else 120.0
        th = math.atan2(R, T)
        dv = depth / math.cos(th)
        x_e = (steps - 1) * T
        pts = [(0.0, R), (x_e, steps * R), (x_e, steps * R - dv), (0.0, R - dv)]
        out["stringer"].append([(x, max(z, -60.0)) for (x, z) in pts])
    elif stringer == "open":
        ts = 40.0
        path = []
        for i in range(steps):
            z = (i + 1) * R - tt_draw
            path.append((i * T + ns, z))
            path.append(((i + 1) * T + ns, z))
        low = [(x + ts, z - ts) for (x, z) in path]
        out["stringer"].append(path + low[::-1])

    xs = [p[0] for k in ("treads", "risers", "stringer", "floor") for poly in out[k] for p in poly]
    zs = [p[1] for k in ("treads", "risers", "stringer", "floor") for poly in out[k] for p in poly]
    out["bbox"] = (min(xs), min(zs), max(xs), max(zs))
    return out
