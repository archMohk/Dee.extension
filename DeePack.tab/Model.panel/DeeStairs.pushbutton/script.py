# -*- coding: utf-8 -*-
"""
DeeStairs
Create component stairs (Revit's "Stair by Component") with every option in one
window, a live plan preview, and a two-click placement in the plan view.

Shapes (the plan geometry is in stairs_layout.py, tested outside Revit):
  Straight, Straight with a landing, L-shape and U-shape on a landing, L-shape
  and U-shape with winders (a sketched run with fanned risers), and a Spiral
  (CreateSpiralRun - automatic sweep, or a fixed angle with the tread following).

Options: base / top level and offsets, number of risers, tread depth, run width,
location line (left edge / centre / right edge), begin / end with a riser,
extension below the base, first-flight split, landing depth, gap between flights,
winder treads, spiral radius; the stair type - or a customised COPY of it (max
riser, min tread, min width); FINISHES (stair_finishes.py): a preset such as cherry,
oak with white risers, timber on folded steel, steel carriage, floating timber,
marble, granite, concrete, glass and steel - or any mix - with a material for the
tread, riser, stringers and landing, the finished tread / riser thickness, nosing,
open or closed risers and the left / right / middle supports, shown in a live
section sketch; railings (type, on treads or on the stringer, both / left / right);
and a multistory repeat up to a higher level.

How it is built
---------------
One TransactionGroup (a single Undo step) holds: the type copy (if any - with
its run / landing / support types copied too, and the finish materials created
once as "Dee - <name>" and reused afterwards), a
StairsEditScope with the runs and landings, then the multistory and railing
steps. Each run is created at its own elevation (CreateStraightRun takes the
elevation from the line's Z), then Run.ActualRisersNumber is read back: if Revit
made a different number of risers than planned, the run is re-created with the
difference added to its length - so the stairs end up complete whatever Revit's
exact tread-counting rule is. Landings are Revit's automatic landings where it
allows them, a sketched landing of the exact planned shape where it does not.
Railings are added after the edit scope (Revit only allows that then).
"""
import os
import sys
import math

import clr
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
clr.AddReference("WindowsBase")
from System.Collections.Generic import List, HashSet
from System.Windows import Visibility, Point
from System.Windows.Media import (Geometry, GeometryGroup, EllipseGeometry, SolidColorBrush,
                                  Color as MediaColor)

from Autodesk.Revit.DB import (
    AppearanceAssetElement, Arc, BuiltInCategory, BuiltInParameter, Color as RevitColor, Curve,
    CurveLoop, ElementId, ElementTypeGroup, FailureProcessingResult, FailureSeverity,
    FilteredElementCollector, IFailuresPreprocessor, Level, Line, Material, StairsEditScope,
    Transaction, TransactionGroup, TransactionStatus, ViewType, XYZ,
)
from Autodesk.Revit.DB.Architecture import (
    MultistoryStairs, Railing, RailingPlacementPosition, RailingType, StairsLanding,
    StairsLandingType, StairsRun, StairsRunJustification, StairsRunType, StairsType,
)
from Autodesk.Revit.UI.Selection import ObjectSnapTypes
from Autodesk.Revit.Exceptions import OperationCanceledException

from pyrevit import forms
import dee_branding
import dee_telemetry
import deew_settings
from dee_ui import eid_int
dee_telemetry.check_access("DeeStairs")

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)
import stairs_layout as SL
import stair_finishes as SF

_XAML = os.path.join(_THIS_DIR, "ui.xaml")
_TITLE = "DeeStairs"
_MM = 304.8                          # mm per foot
_EPS = 1e-5                          # ft (0.003 mm): 'the user changed this value'

# same order as the Shape combo box
_SHAPES = ["straight", "straight_landing", "l_shape", "u_shape",
           "winder_l", "winder_u", "spiral"]
_JUST_SIDE = [1, 0, -1]              # left edge / centre / right edge
_PLAN_VIEWS = (ViewType.FloorPlan, ViewType.CeilingPlan, ViewType.EngineeringPlan, ViewType.AreaPlan)

_TEXTS = ["base_off_tb", "top_off_tb", "flight1_tb", "landing_tb", "gap_tb", "radius_tb",
          "sweep_tb", "tread_tb", "width_tb", "ext_tb", "angle_tb"]
# the four material combos of the Finishes tab: role -> (combo, swatch)
_MAT_ROLES = [("tread", "mat_tread_cb", "tread_sw"), ("riser", "mat_riser_cb", "riser_sw"),
              ("stringer", "mat_str_cb", "str_sw"), ("landing", "mat_land_cb", "land_sw")]
_CHECKS = ["begin_chk", "end_chk", "dir_chk", "multi_chk"]
_COMBOS = ["shape_cb", "turn_cb", "winders_cb", "sweep_cb", "just_cb", "rail_pos_cb", "rail_side_cb",
           "preset_cb"]


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def _pf(text):
    """Text box -> float, None when empty. Raises ValueError for anything else."""
    s = (text or u"").strip().replace(u",", u".")
    if not s:
        return None
    return float(s)


def _ename(el):
    try:
        return el.Name
    except Exception:
        return u"?"


def _same_id(a, b):
    return eid_int(a) == eid_int(b)


def _valid_id(eid):
    return eid is not None and eid_int(eid) > 0


def _setp(obj, name, value, notes, label):
    """Set a property only when it differs; a refusal becomes a note, not a crash."""
    try:
        cur = getattr(obj, name)
        if isinstance(value, float):
            if abs(cur - value) < 1e-9:
                return True
        elif cur == value:
            return True
        setattr(obj, name, value)
        return True
    except Exception as e:
        notes.append(u"{0}: {1}".format(label, e))
        return False


def _set_param(el, bip, value, notes, label):
    try:
        p = el.get_Parameter(bip)
        if p is None or p.IsReadOnly:
            notes.append(u"{0}: this parameter cannot be set.".format(label))
            return False
        if abs(p.AsDouble() - value) < 1e-9:
            return True
        p.Set(value)
        return True
    except Exception as e:
        notes.append(u"{0}: {1}".format(label, e))
        return False


# --------------------------------------------------------------------------
# what the document offers
# --------------------------------------------------------------------------
def _levels(doc):
    out = []
    for lv in FilteredElementCollector(doc).OfClass(Level):
        out.append((lv.Elevation, _ename(lv), lv.Id))
    out.sort(key=lambda t: t[0])
    return out


def _stairs_types(doc):
    out = []
    for t in FilteredElementCollector(doc).OfClass(StairsType):
        info = {"name": _ename(t), "id": t.Id, "el": t}
        for key, attr in (("max_riser", "MaxRiserHeight"), ("min_tread", "MinTreadDepth"),
                          ("min_width", "MinRunWidth")):
            try:
                info[key] = float(getattr(t, attr))
            except Exception:
                info[key] = None
        out.append(info)
    out.sort(key=lambda i: i["name"].lower())
    return out


def _railing_types(doc):
    out = []
    for t in FilteredElementCollector(doc).OfClass(RailingType):
        out.append((_ename(t), t.Id))
    out.sort(key=lambda i: i[0].lower())
    return out


def _support_types(doc):
    """Stair support (stringer / carriage) types, from either of the two
    categories Revit has used for them."""
    seen = set()
    out = []
    for cat in (BuiltInCategory.OST_StairsSupports, BuiltInCategory.OST_StairsStringerCarriage):
        try:
            for t in FilteredElementCollector(doc).OfCategory(cat).WhereElementIsElementType():
                if eid_int(t.Id) in seen:
                    continue
                seen.add(eid_int(t.Id))
                out.append((_ename(t), t.Id))
        except Exception:
            pass
    out.sort(key=lambda i: i[0].lower())
    return out


def _default_type_id(doc, group):
    try:
        eid = doc.GetDefaultElementTypeId(group)
        return eid if _valid_id(eid) else None
    except Exception:
        return None


def _view_level_index(doc, levels):
    try:
        lv = doc.ActiveView.GenLevel
    except Exception:
        lv = None
    if lv is not None:
        for i, l in enumerate(levels):
            if _same_id(l[2], lv.Id):
                return i
    return 0


# --------------------------------------------------------------------------
# building
# --------------------------------------------------------------------------
class _StairsFailures(IFailuresPreprocessor):
    """Warnings are dropped quietly; an error rolls the whole thing back and
    its text is kept so the user is told why."""

    def __init__(self):
        self.errors = []

    def PreprocessFailures(self, accessor):
        bad = False
        for f in accessor.GetFailureMessages():
            if f.GetSeverity() == FailureSeverity.Warning:
                try:
                    accessor.DeleteWarning(f)
                except Exception:
                    pass
            else:
                bad = True
                try:
                    self.errors.append(f.GetDescriptionText())
                except Exception:
                    pass
        if bad:
            return FailureProcessingResult.ProceedWithRollBack
        return FailureProcessingResult.Continue


def _attach(t, pre):
    try:
        opts = t.GetFailureHandlingOptions()
        opts.SetFailuresPreprocessor(pre)
        t.SetFailureHandlingOptions(opts)
    except Exception:
        pass


def _rollback(t):
    try:
        if t is not None and t.GetStatus() == TransactionStatus.Started:
            t.RollBack()
    except Exception:
        pass


def _unique_name(doc, cls, base):
    names = set(_ename(t) for t in FilteredElementCollector(doc).OfClass(cls))
    if base not in names:
        return base
    i = 2
    while u"{0} {1}".format(base, i) in names:
        i += 1
    return u"{0} {1}".format(base, i)


def _unique_like(doc, el, base):
    """A name no other type of the same kind has; by the element's own class, and
    by its id when Revit will not filter on that class."""
    try:
        return _unique_name(doc, el.__class__, base)
    except Exception:
        return u"{0} {1}".format(base, eid_int(el.Id))


# -- materials ---------------------------------------------------------------
# A material "reference" is None (keep the type's), ("cat", key) - a finish of the
# catalogue, made once as "Dee - <name>" - or ("id", ElementId) - a material that
# is already in the project.
_P_TREAD = "STAIRS_TRISERTYPE_TREAD_MATERIAL"
_P_RISER = "STAIRS_TRISERTYPE_RISER_MATERIAL"
_P_RUN_MONO = "STAIRS_RUNTYPE_RUN_MATERIAL"
_P_LANDING_MONO = "STAIRS_LANDINGTYPE_LANDING_MATERIAL"
_P_SUPPORT = "STAIRS_SUPPORTTYPE_MATERIAL"
_P_SUPPORT_W = "STAIRS_SUPPORTTYPE_WIDTH"
_SUPPORT_ATTRS = ("LeftSideSupportType", "RightSideSupportType", "MiddleSupportType")


def _param(el, name):
    """The built-in parameter `name` of an element, or None (not in this Revit
    version, or not on this element)."""
    bip = getattr(BuiltInParameter, name, None)
    if bip is None or el is None:
        return None
    try:
        return el.get_Parameter(bip)
    except Exception:
        return None


def _param_eid(el, name):
    p = _param(el, name)
    if p is None:
        return None
    try:
        return p.AsElementId()
    except Exception:
        return None


def _param_double(el, name):
    p = _param(el, name)
    if p is None:
        return None
    try:
        return p.AsDouble()
    except Exception:
        return None


def _set_eid(el, name, eid, notes, label):
    p = _param(el, name)
    if p is None or p.IsReadOnly:
        notes.append(u"{0}: this type has no such setting.".format(label))
        return False
    try:
        cur = p.AsElementId()
        if cur is not None and eid_int(cur) == eid_int(eid):
            return True
        p.Set(eid)
        return True
    except Exception as e:
        notes.append(u"{0}: {1}".format(label, e))
        return False


def _material_rgb(m):
    try:
        c = m.Color
        if c is not None and c.IsValid:
            return (int(c.Red), int(c.Green), int(c.Blue))
    except Exception:
        pass
    return None


def _project_materials(doc):
    """[(name, id, rgb or None)] of the project's materials, by name."""
    out = []
    for m in FilteredElementCollector(doc).OfClass(Material):
        out.append((_ename(m), m.Id, _material_rgb(m)))
    out.sort(key=lambda t: t[0].lower())
    return out


def _find_material(doc, name):
    for m in FilteredElementCollector(doc).OfClass(Material):
        if _ename(m) == name:
            return m
    return None


def _ref_key(ref):
    return (ref[0], ref[1] if ref[0] == "cat" else eid_int(ref[1]))


def _existing_material_id(doc, ref):
    """The project's material for a reference, None when it is a catalogue
    finish that has not been made yet."""
    if ref[0] == "id":
        return ref[1]
    m = _find_material(doc, SF.material_name(SF.MATERIAL_BY_KEY[ref[1]]))
    return m.Id if m is not None else None


def _ref_differs(doc, ref, current):
    """Would applying `ref` change a parameter that now holds `current`?"""
    if ref is None or current is None:
        return False
    want = _existing_material_id(doc, ref)
    return want is None or eid_int(want) != eid_int(current)


def _borrow_appearance(doc, material, spec):
    """Give a new material the render appearance of a similar material the
    project already has (a copy of it, so editing one never changes the other)."""
    try:
        assets = {}
        for a in FilteredElementCollector(doc).OfClass(AppearanceAssetElement):
            assets[_ename(a)] = a
        best = SF.best_asset_name(list(assets.keys()), spec["find"], spec.get("avoid", ()))
        if best is None:
            return
        dup = assets[best].Duplicate(_unique_name(doc, AppearanceAssetElement,
                                                  SF.material_name(spec) + u" appearance"))
        material.AppearanceAssetId = dup.Id
        material.UseRenderAppearanceForShading = True
    except Exception:
        pass


def _make_material(doc, key, info):
    spec = SF.MATERIAL_BY_KEY[key]
    name = SF.material_name(spec)
    mid = Material.Create(doc, name)
    m = doc.GetElement(mid)
    r, g, b = spec["rgb"]
    for attr, value in (("Color", RevitColor(r, g, b)), ("MaterialClass", spec["cls"]),
                        ("Transparency", int(spec.get("transparency", 0)))):
        try:
            setattr(m, attr, value)
        except Exception:
            pass
    _borrow_appearance(doc, m, spec)
    info.append(u"new material '{0}'".format(name))
    return mid


def _resolve_materials(doc, refs, info):
    """{ref key: ElementId} for every reference, making the missing finishes."""
    out = {}
    for ref in refs:
        if ref is None:
            continue
        k = _ref_key(ref)
        if k in out:
            continue
        mid = _existing_material_id(doc, ref)
        out[k] = mid if mid is not None else _make_material(doc, ref[1], info)
    return out


# -- the stair type (a copy, only when something was changed) --------------
def _type_parts(doc, base):
    """The elements a stair type is built from, and which material setting each
    of them has."""
    parts = {"run": None, "landing": None, "mono_run": False, "mono_landing": False,
             "tread_param": _P_TREAD}
    try:
        parts["run"] = doc.GetElement(base.RunType)
    except Exception:
        pass
    try:
        parts["landing"] = doc.GetElement(base.LandingType)
    except Exception:
        pass
    if parts["run"] is not None and _attr(parts["run"], "IsMonolithic"):
        parts["mono_run"] = True
        parts["tread_param"] = _P_RUN_MONO
    if parts["landing"] is not None and _attr(parts["landing"], "IsMonolithic"):
        parts["mono_landing"] = True
    return parts


def _supports_in_use(doc, base, type_changes):
    """[(attribute, support type element)] of the supports the (changed) stair
    type will have."""
    out = []
    has_mid = type_changes.get("HasMiddleSupports", _attr(base, "HasMiddleSupports"))
    for attr in _SUPPORT_ATTRS:
        if attr == "MiddleSupportType" and not has_mid:
            continue
        eid = type_changes.get(attr, _attr(base, attr))
        el = doc.GetElement(eid) if _valid_id(eid) else None
        if el is not None:
            out.append((attr, el))
    return out


def _type_changes(doc, base, spec):
    """What differs between the dialog and the original type, grouped by the
    element it lives on: the stairs type, its run type, its landing type and its
    support types; plus the materials to apply."""
    ch = {"type": {}, "run": {}, "landing": {}, "run_el": None, "landing_el": None,
          "run_mats": [], "landing_mats": [], "support_edits": [], "support_attrs": [],
          "notes": []}
    for key, attr in (("max_riser", "MaxRiserHeight"), ("min_tread", "MinTreadDepth"),
                      ("min_width", "MinRunWidth"), ("left_off", "LeftLateralOffset"),
                      ("right_off", "RightLateralOffset")):
        v = spec.get(key)
        if v is None:
            continue
        try:
            if abs(getattr(base, attr) - v) > _EPS:
                ch["type"][attr] = v
        except Exception:
            pass
    for key, attr in (("left", "LeftSideSupportType"), ("right", "RightSideSupportType"),
                      ("mid", "MiddleSupportType")):
        v = spec.get(key)
        if v is None:
            continue
        target = ElementId.InvalidElementId if v == "none" else v
        try:
            if eid_int(getattr(base, attr)) != eid_int(target):
                ch["type"][attr] = target
                if key == "mid":
                    ch["type"]["HasMiddleSupports"] = (v != "none")
        except Exception:
            pass
    if spec.get("midn") is not None:
        try:
            if base.MiddleSupportsNumber != spec["midn"]:
                ch["type"]["MiddleSupportsNumber"] = spec["midn"]
        except Exception:
            pass

    mat = spec.get("mat") or {}
    parts = _type_parts(doc, base)
    rt = parts["run"]
    if rt is not None:
        ch["run_el"] = rt
        try:
            if spec.get("open") is not None and bool(rt.HasRisers) == bool(spec["open"]):
                ch["run"]["HasRisers"] = not spec["open"]
        except Exception:
            pass
        for key, attr in (("tread_thk", "TreadThickness"), ("riser_thk", "RiserThickness"),
                          ("nosing", "NosingLength")):
            v = spec.get(key)
            if v is None:
                continue
            try:
                if abs(getattr(rt, attr) - v) > _EPS:
                    ch["run"][attr] = v
            except Exception:
                pass
        roles = [("tread", parts["tread_param"])]
        if not parts["mono_run"]:
            roles.append(("riser", _P_RISER))
        for role, pname in roles:
            ref = mat.get(role)
            if ref is not None and _ref_differs(doc, ref, _param_eid(rt, pname)):
                ch["run_mats"].append((pname, ref))

    lt = parts["landing"]
    if lt is not None:
        ch["landing_el"] = lt
        if spec.get("land_thk") is not None:
            try:
                if abs(lt.Thickness - spec["land_thk"]) > _EPS:
                    ch["landing"]["Thickness"] = spec["land_thk"]
            except Exception:
                pass
        ref = mat.get("landing")
        if ref is not None:
            if parts["mono_landing"]:
                if _ref_differs(doc, ref, _param_eid(lt, _P_LANDING_MONO)):
                    ch["landing_mats"].append((_P_LANDING_MONO, ref))
            else:
                ch["notes"].append(u"The landing is built from treads and risers, so it follows the run's "
                                   u"materials - the landing material was not set separately.")

    # the support types the stairs will end up with: a copy of each, changed
    sref = mat.get("stringer")
    want_w = spec.get("stringer_w")
    seen = set()
    for attr, el in _supports_in_use(doc, base, ch["type"]):
        ch["support_attrs"].append((attr, el))
        if eid_int(el.Id) in seen:
            continue
        seen.add(eid_int(el.Id))
        edit = {"el": el, "mat": None, "w": None}
        if sref is not None and _ref_differs(doc, sref, _param_eid(el, _P_SUPPORT)):
            edit["mat"] = sref
        cur_w = _param_double(el, _P_SUPPORT_W)
        if want_w is not None and cur_w is not None and abs(cur_w - want_w) > _EPS:
            edit["w"] = want_w
        if edit["mat"] is not None or edit["w"] is not None:
            ch["support_edits"].append(edit)
    return ch


def _needs_copy(ch):
    return bool(ch["type"] or ch["run"] or ch["landing"] or ch["run_mats"] or ch["landing_mats"]
                or ch["support_edits"])


def _prepare_type(doc, o, notes, info):
    base = doc.GetElement(o["type_id"])
    if base is None:
        raise ValueError(u"The stair type is no longer in the project.")
    spec = o.get("custom")
    if not spec:
        return base.Id
    ch = _type_changes(doc, base, spec)
    if not _needs_copy(ch):
        return base.Id                    # nothing differs: use the original
    notes.extend(ch["notes"])
    t = Transaction(doc, u"DeeStairs - stair type")
    t.Start()
    try:
        name = _unique_name(doc, StairsType, spec.get("name") or u"{0} - DeeStairs".format(_ename(base)))
        new = base.Duplicate(name)
        refs = [r for _, r in ch["run_mats"]] + [r for _, r in ch["landing_mats"]]
        refs += [e["mat"] for e in ch["support_edits"]]
        mats = _resolve_materials(doc, refs, info)

        if ch["run"] or ch["run_mats"]:
            rt2 = ch["run_el"].Duplicate(_unique_name(
                doc, StairsRunType, u"{0} - DeeStairs".format(_ename(ch["run_el"]))))
            for attr, v in ch["run"].items():
                _setp(rt2, attr, v, notes, u"Run type " + attr)
            for pname, ref in ch["run_mats"]:
                _set_eid(rt2, pname, mats[_ref_key(ref)], notes, u"Run material")
            new.RunType = rt2.Id
        if ch["landing"] or ch["landing_mats"]:
            lt2 = ch["landing_el"].Duplicate(_unique_name(
                doc, StairsLandingType, u"{0} - DeeStairs".format(_ename(ch["landing_el"]))))
            for attr, v in ch["landing"].items():
                _setp(lt2, attr, v, notes, u"Landing type " + attr)
            for pname, ref in ch["landing_mats"]:
                _set_eid(lt2, pname, mats[_ref_key(ref)], notes, u"Landing material")
            new.LandingType = lt2.Id
        for attr, v in ch["type"].items():
            _setp(new, attr, v, notes, u"Stair type " + attr)

        # support types: one changed copy per distinct support in use
        copies = {}
        for edit in ch["support_edits"]:
            el = edit["el"]
            dup = el.Duplicate(_unique_like(doc, el, u"{0} - {1}".format(
                _ename(el), spec.get("finish") or u"DeeStairs")))
            if edit["mat"] is not None:
                _set_eid(dup, _P_SUPPORT, mats[_ref_key(edit["mat"])], notes, u"Stringer material")
            if edit["w"] is not None:
                p = _param(dup, _P_SUPPORT_W)
                try:
                    p.Set(edit["w"])
                except Exception as e:
                    notes.append(u"Stringer width: {0}".format(e))
            copies[eid_int(el.Id)] = dup.Id
        for attr, el in ch["support_attrs"]:
            if eid_int(el.Id) in copies:
                _setp(new, attr, copies[eid_int(el.Id)], notes, u"Stair type " + attr)

        if spec.get("finish"):
            p = _param(new, "ALL_MODEL_TYPE_COMMENTS")
            try:
                if p is not None and not p.IsReadOnly:
                    p.Set(u"DeeStairs finish: {0}".format(spec["finish"]))
            except Exception:
                pass
        t.Commit()
    except Exception:
        _rollback(t)
        raise
    info.append(u"new stair type '{0}'".format(name))
    return new.Id


# -- runs and landings -------------------------------------------------------
class _Ctx(object):
    pass


def _w(ctx, p):
    return SL.to_world(p, ctx.origin, ctx.angle)


def _xyz(ctx, p, z):
    w = _w(ctx, p)
    return XYZ(w[0], w[1], z)


def _curve(ctx, seg, z):
    if seg[0] == "L":
        return Line.CreateBound(_xyz(ctx, seg[1], z), _xyz(ctx, seg[2], z))
    return Arc.Create(_xyz(ctx, seg[1], z), _xyz(ctx, seg[3], z), _xyz(ctx, seg[2], z))


def _create_run(ctx, plan, rp, k):
    """One Revit run from a planned one. The caller reads the risers back."""
    z = ctx.z0 + rp.step0 * ctx.R
    if rp.kind == "straight":
        q0, q1 = SL.shifted_line(rp.p0, rp.p1, ctx.side * ctx.W / 2.0)
        run = StairsRun.CreateStraightRun(ctx.doc, ctx.stairs_id,
                                          Line.CreateBound(_xyz(ctx, q0, z), _xyz(ctx, q1, z)),
                                          ctx.just)
    elif rp.kind == "spiral":
        sp = rp.spiral
        c = _w(ctx, sp["center"])
        r_path = SL.spiral_path_radius(sp["rc"], ctx.W, ctx.side, sp["ccw"])
        run = StairsRun.CreateSpiralRun(ctx.doc, ctx.stairs_id, XYZ(c[0], c[1], z), r_path,
                                        sp["a0"] + ctx.angle, sp["sweep"], not sp["ccw"], ctx.just)
    else:
        sk = rp.sketch
        bnd = List[Curve]()
        for seg in sk["left"] + sk["right"]:
            bnd.Add(_curve(ctx, seg, z))
        ris = List[Curve]()
        for seg in sk["risers"]:
            ris.Add(_curve(ctx, seg, z))
        pth = List[Curve]()
        for seg in sk["path"]:
            pth.Add(_curve(ctx, seg, z))
        run = StairsRun.CreateSketchedRun(ctx.doc, ctx.stairs_id, 0.0, bnd, ris, pth)

    if rp.kind != "sketched":
        _setp(run, "ActualRunWidth", ctx.W, ctx.notes, u"Run width")
        _setp(run, "BeginsWithRiser", bool(rp.begin), ctx.notes, u"Begin with riser")
        _setp(run, "EndsWithRiser", bool(rp.end), ctx.notes, u"End with riser")
        if k == 0 and ctx.ext > 1e-9:
            _setp(run, "ExtensionBelowRiserBase" if rp.begin else "ExtensionBelowTreadBase",
                  -ctx.ext, ctx.notes, u"Extension below base")
    return run


def _make_landing(ctx, lp, run_a, run_b):
    """Revit's automatic landing where it allows one, else the planned shape."""
    doc = ctx.doc
    try:
        if StairsLanding.CanCreateAutomaticLanding(doc, run_a.Id, run_b.Id):
            StairsLanding.CreateAutomaticLanding(doc, run_a.Id, run_b.Id)
            return True
    except Exception as e:
        ctx.notes.append(u"Automatic landing refused ({0}) - drew the planned one.".format(e))
    last = None
    for zz in (ctx.z0 + lp.step * ctx.R, ctx.z0):
        try:
            loop = CurveLoop()
            n = len(lp.polygon)
            for i in range(n):
                loop.Append(Line.CreateBound(_xyz(ctx, lp.polygon[i], zz),
                                             _xyz(ctx, lp.polygon[(i + 1) % n], zz)))
            StairsLanding.CreateSketchedLanding(doc, ctx.stairs_id, loop, lp.step * ctx.R)
            return True
        except Exception as e:
            last = e
    raise ValueError(u"Revit would not create the landing: {0}".format(last))


class _TypeChangeError(ValueError):
    """Revit would not switch the new stairs to the chosen type while editing."""


def _set_default_stairs_type(doc, type_id):
    """Make `type_id` the project's default stair type - the one a new stairs
    edit scope starts with. Returns the previous default (or None)."""
    prev = _default_type_id(doc, ElementTypeGroup.StairsType)
    t = Transaction(doc, u"DeeStairs - default stair type")
    t.Start()
    try:
        doc.SetDefaultElementTypeId(ElementTypeGroup.StairsType, type_id)
        t.Commit()
    except Exception:
        _rollback(t)
        raise
    return prev


def _fill_stairs(doc, stairs_id, o, type_id, origin, angle, notes):
    """Everything that happens inside the stairs edit scope."""
    stairs = doc.GetElement(stairs_id)
    if not _same_id(stairs.GetTypeId(), type_id):
        try:
            stairs.ChangeTypeId(type_id)
        except Exception as e:
            raise _TypeChangeError(u"Revit would not apply the stair type: {0}".format(e))
        stairs = doc.GetElement(stairs_id)
    _set_param(stairs, BuiltInParameter.STAIRS_BASE_OFFSET, o["base_off"], notes, u"Base offset")
    _set_param(stairs, BuiltInParameter.STAIRS_TOP_OFFSET, o["top_off"], notes, u"Top offset")
    try:
        stairs.DesiredRisersNumber = o["n"]
    except Exception as e:
        raise ValueError(u"Revit would not take {0} risers: {1}".format(o["n"], e))

    pa = o["plan_args"]
    plan0 = SL.make_plan(**pa)
    if not plan0.ok:
        raise ValueError(u"; ".join(plan0.errors))
    _setp(stairs, "ActualTreadDepth", plan0.tread, notes, u"Tread depth")
    doc.Regenerate()

    ctx = _Ctx()
    ctx.doc, ctx.stairs_id, ctx.origin, ctx.angle, ctx.notes = doc, stairs_id, origin, angle, notes
    ctx.W, ctx.side, ctx.ext = pa["W"], o["side"], o["ext"]
    ctx.just = (StairsRunJustification.Left, StairsRunJustification.Center,
                StairsRunJustification.Right)[1 - o["side"]]
    ctx.z0 = stairs.BaseElevation
    ctx.R = stairs.ActualRiserHeight
    if ctx.R < 1e-6:
        ctx.R = stairs.Height / float(o["n"])
    if abs(stairs.Height - o["H"]) > 1.0 / _MM:
        notes.append(u"Revit measures the height as {0:.0f} mm, not {1:.0f} mm."
                     .format(stairs.Height * _MM, o["H"] * _MM))

    nruns = len(plan0.runs)
    extras = [0] * max(nruns, 4)
    elems = []
    for k in range(nruns):
        el = None
        for attempt in range(3):
            plan = SL.make_plan(extras=extras, **pa)
            rp = plan.runs[k]
            try:
                el = _create_run(ctx, plan, rp, k)
            except Exception as e:
                raise ValueError(u"Revit would not create run {0} of {1} ({2}): {3}"
                                 .format(k + 1, nruns, rp.kind, e))
            if rp.kind == "sketched":
                break
            got = el.ActualRisersNumber
            if got == rp.risers:
                break
            if attempt == 2:
                notes.append(u"Run {0} has {1} risers, {2} were planned.".format(k + 1, got, rp.risers))
                break
            extras[k] += rp.risers - got
            doc.Delete(el.Id)
            el = None
        elems.append(el)

    plan = SL.make_plan(extras=extras, **pa)
    for li, lp in enumerate(plan.landings):
        _make_landing(ctx, lp, elems[li], elems[li + 1])
    doc.Regenerate()
    stairs = doc.GetElement(stairs_id)
    if stairs.ActualRisersNumber != o["n"]:
        notes.append(u"The stairs have {0} of {1} risers - they do not reach the top yet."
                     .format(stairs.ActualRisersNumber, o["n"]))
    return plan


def _build_stairs(doc, o, type_id, origin, angle, pre, notes):
    scope = StairsEditScope(doc, u"DeeStairs")
    if not scope.IsPermitted:
        raise ValueError(u"Revit cannot start editing stairs right now "
                         u"(another edit mode is active, or the model is read-only).")
    stairs_id = scope.Start(o["base_level_id"], o["top_level_id"])
    t = None
    plan = None
    try:
        t = Transaction(doc, u"DeeStairs - runs and landings")
        t.Start()
        _attach(t, pre)
        plan = _fill_stairs(doc, stairs_id, o, type_id, origin, angle, notes)
        t.Commit()
        scope.Commit(pre)
    except Exception as err:
        _rollback(t)
        try:
            if scope.IsActive:
                scope.Cancel()
        except Exception:
            pass
        if pre.errors:
            raise ValueError(u"Revit rejected the stairs: " + u"; ".join(pre.errors))
        raise err                        # (a bare raise could re-raise the Cancel error in Python 2)
    if doc.GetElement(stairs_id) is None:
        raise ValueError(u"Revit rejected the stairs: " + (u"; ".join(pre.errors) or u"it rolled them back."))
    return stairs_id, plan


# -- after the edit scope -----------------------------------------------------
def _multistory(doc, stairs_id, o, notes, info):
    if not o["multi_levels"]:
        return
    t = Transaction(doc, u"DeeStairs - multistory")
    t.Start()
    try:
        stairs = doc.GetElement(stairs_id)
        if not MultistoryStairs.IsAcceptableForMultistoryStairs(stairs):
            _rollback(t)
            notes.append(u"Revit cannot make these stairs multistory.")
            return
        ms = MultistoryStairs.Create(stairs)
        ids = HashSet[ElementId]()
        for lid in o["multi_levels"]:
            ids.Add(lid)
        ms.ConnectLevels(ids)
        t.Commit()
        info.append(u"repeated on {0} more level{1}".format(
            len(o["multi_levels"]), u"" if len(o["multi_levels"]) == 1 else u"s"))
    except Exception as e:
        _rollback(t)
        notes.append(u"Multistory: {0}".format(e))


def _side_on(p, polys):
    """+1 when point p lies to the left of the nearest run's direction of
    travel, -1 to the right, 0 when it is beyond the ends of the stairs."""
    best = None
    for poly in polys:
        n = len(poly) - 1
        for i in range(n):
            a, b = poly[i], poly[i + 1]
            dx, dy = b[0] - a[0], b[1] - a[1]
            l2 = dx * dx + dy * dy
            if l2 < 1e-12:
                continue
            t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2
            tc = max(0.0, min(1.0, t))
            d = math.hypot(p[0] - (a[0] + tc * dx), p[1] - (a[1] + tc * dy))
            clamped = (i == 0 and t <= 0.0) or (i == n - 1 and t >= 1.0)
            cross = dx * (p[1] - a[1]) - dy * (p[0] - a[0])
            if best is None or d < best[0]:
                best = (d, clamped, cross)
    if best is None or best[1]:
        return 0
    return 1 if best[2] > 0 else -1


def _railing_side(rail, polys):
    try:
        loop = rail.GetPath()
    except Exception:
        return 0
    total = 0.0
    for c in loop:
        try:
            pts = list(c.Tessellate())
        except Exception:
            continue
        for i in range(len(pts) - 1):
            a, b = pts[i], pts[i + 1]
            mid = ((a.X + b.X) / 2.0, (a.Y + b.Y) / 2.0)
            total += _side_on(mid, polys) * math.hypot(b.X - a.X, b.Y - a.Y)
    return 1 if total > 1e-9 else (-1 if total < -1e-9 else 0)


def _railings(doc, stairs_id, o, plan, origin, angle, notes):
    t = Transaction(doc, u"DeeStairs - railings")
    t.Start()
    try:
        stairs = doc.GetElement(stairs_id)
        existing = list(stairs.GetAssociatedRailings())
        if existing:                      # Revit may have made its own: ours replace them
            doc.Delete(List[ElementId](existing))
        count = 0
        if o["rail_type_id"] is not None:
            if not Railing.IsValidHostForNewRailing(doc, stairs_id):
                notes.append(u"Revit will not host new railings on these stairs.")
            else:
                made = list(Railing.Create(doc, stairs_id, o["rail_type_id"], o["rail_pos"]))
                count = len(made)
                if o["rail_side"] != 0 and made:
                    polys = [[SL.to_world(p, origin, angle) for p in r.center] for r in plan.runs]
                    drop = []
                    for rid in made:
                        rail = doc.GetElement(rid)
                        s = _railing_side(rail, polys) if rail is not None else 0
                        if s != 0 and s != o["rail_side"]:
                            drop.append(rid)
                    if drop:
                        doc.Delete(List[ElementId](drop))
                        count -= len(drop)
        t.Commit()
        return count
    except Exception as e:
        _rollback(t)
        notes.append(u"Railings: {0}".format(e))
        return 0


def create_stairs(doc, uidoc, o, origin, angle):
    """Build the stairs for options `o` at `origin` (feet, x/y) heading
    `angle` (radians). One Undo step. Returns a dict for the summary."""
    notes = []
    info = []
    pre = _StairsFailures()
    tg = TransactionGroup(doc, u"DeeStairs")
    tg.Start()
    try:
        type_id = _prepare_type(doc, o, notes, info)
        mark = len(notes)
        try:
            stairs_id, plan = _build_stairs(doc, o, type_id, origin, angle, pre, notes)
        except _TypeChangeError:
            # Revit refuses to switch the type of stairs being edited: start the
            # scope with this type as the project default instead, then put the
            # previous default back.
            del notes[mark:]
            pre.errors = []
            prev = _set_default_stairs_type(doc, type_id)
            try:
                stairs_id, plan = _build_stairs(doc, o, type_id, origin, angle, pre, notes)
            finally:
                if prev is not None:
                    try:
                        _set_default_stairs_type(doc, prev)
                    except Exception as e:
                        notes.append(u"Could not restore the default stair type: {0}".format(e))
        _multistory(doc, stairs_id, o, notes, info)
        rails = _railings(doc, stairs_id, o, plan, origin, angle, notes)
        tg.Assimilate()
    except Exception as err:
        try:
            tg.RollBack()
        except Exception:
            pass
        raise err
    stairs = doc.GetElement(stairs_id)
    res = {"notes": notes, "info": info, "railings": rails, "plan": plan, "stairs_id": stairs_id}
    try:
        res["risers"] = stairs.ActualRisersNumber
        res["riser_h"] = stairs.ActualRiserHeight
        res["tread"] = stairs.ActualTreadDepth
        res["runs"] = len(list(stairs.GetStairsRuns()))
        res["landings"] = len(list(stairs.GetStairsLandings()))
    except Exception:
        pass
    try:
        uidoc.Selection.SetElementIds(List[ElementId]([stairs_id]))
    except Exception:
        pass
    return res


def _summary(res, o):
    if "risers" not in res:
        return u"Stairs created."
    s = u"Stairs created - {0} risers x {1:.0f} mm, tread {2:.0f} mm, {3} run{4}".format(
        res["risers"], res["riser_h"] * _MM, res["tread"] * _MM, res["runs"],
        u"" if res["runs"] == 1 else u"s")
    if res.get("landings"):
        s += u", {0} landing{1}".format(res["landings"], u"" if res["landings"] == 1 else u"s")
    if o["rail_type_id"] is not None:
        s += u", {0} railing{1}".format(res["railings"], u"" if res["railings"] == 1 else u"s")
    if res.get("info"):
        s += u" (" + u"; ".join(res["info"]) + u")"
    return s + u"."


# --------------------------------------------------------------------------
# window
# --------------------------------------------------------------------------
def _attr(obj, name):
    try:
        return getattr(obj, name)
    except Exception:
        return None


def _fmt_mm(v):
    """feet -> a short millimetre string for a text box."""
    if v is None:
        return u""
    s = u"{0:.3f}".format(v * _MM).rstrip(u"0").rstrip(u".")
    return s or u"0"


class StairsWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, doc, uidoc, status=u""):
        self._ready = False
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.doc = doc
        self.uidoc = uidoc
        self.action = None
        self.options = None
        self._levels = _levels(doc)
        self._types = _stairs_types(doc)
        self._rails = _railing_types(doc)
        self._supports = _support_types(doc)
        self._project_mats = _project_materials(doc)
        self._multi_ids = [[]]
        self._cur = {"rgb": {}, "styles": {}}          # what the chosen stair type has now
        self._auto_name = u""
        self._preset_note = u""
        self.Closed += self._on_closed

        try:
            vname = doc.ActiveView.Name
        except Exception:
            vname = u"?"
        self.head_tb.Text = u"Project: {0}   -   active view: {1}".format(doc.Title, vname)

        self._fill_levels()
        self._fill_types()
        self._fill_rails()
        self._fill_supports()
        self._fill_finishes()
        self._restore()
        self._fill_multi()
        self._prefill_custom()
        self._restore_finish()
        self._ready = True
        self._sync_panels()
        self._recompute()
        if status:
            self.status_tb.Text = status

    # -- filling the lists
    def _fill_levels(self):
        labels = [u"{0}   ({1:.0f} mm)".format(l[1], l[0] * _MM) for l in self._levels]
        self.base_level_cb.ItemsSource = labels
        self.top_level_cb.ItemsSource = labels
        bi = _view_level_index(self.doc, self._levels)
        if bi >= len(self._levels) - 1:
            bi = max(0, len(self._levels) - 2)
        self.base_level_cb.SelectedIndex = bi
        self.top_level_cb.SelectedIndex = min(bi + 1, len(self._levels) - 1)

    def _fill_types(self):
        self.type_cb.ItemsSource = [t["name"] for t in self._types]
        idx = 0
        default = _default_type_id(self.doc, ElementTypeGroup.StairsType)
        if default is not None:
            for i, t in enumerate(self._types):
                if _same_id(t["id"], default):
                    idx = i
        self.type_cb.SelectedIndex = idx

    def _fill_rails(self):
        self.rail_cb.ItemsSource = [u"<No railings>"] + [r[0] for r in self._rails]
        idx = 1 if self._rails else 0
        default = _default_type_id(self.doc, ElementTypeGroup.StairsRailingType)
        if default is not None:
            for i, r in enumerate(self._rails):
                if _same_id(r[1], default):
                    idx = i + 1
        self.rail_cb.SelectedIndex = idx

    def _fill_supports(self):
        items = [u"(unchanged)", u"(none)"] + [s[0] for s in self._supports]
        for cb in (self.t_left_cb, self.t_right_cb, self.t_mid_cb):
            cb.ItemsSource = items
            cb.SelectedIndex = 0

    def _fill_finishes(self):
        """The preset list and the four material lists of the Finishes tab."""
        self.preset_cb.ItemsSource = ([u"(none - keep the stair type as it is)",
                                       u"Custom - choose the materials and build-up below"]
                                      + [p["name"] for p in SF.PRESETS])
        self.preset_cb.SelectedIndex = 0
        labels = [u"(keep the type's material)"]
        refs = [None]
        rgbs = [None]
        for m in SF.MATERIALS:
            labels.append(u"{0}   (new material)".format(m["name"]))
            refs.append(("cat", m["key"]))
            rgbs.append(m["rgb"])
        catalog_names = set(SF.material_name(m) for m in SF.MATERIALS)
        for name, eid, rgb in self._project_mats:
            if name in catalog_names:
                continue
            labels.append(name)
            refs.append(("id", eid))
            rgbs.append(rgb)
        self._mat_labels, self._mat_refs, self._mat_rgbs = labels, refs, rgbs
        for role, cb_name, _sw in _MAT_ROLES:
            cb = getattr(self, cb_name)
            cb.ItemsSource = labels
            cb.SelectedIndex = 0

    def _fill_multi(self):
        ti = self.top_level_cb.SelectedIndex
        prev = self.multi_cb.SelectedItem
        labels = [u"<Single storey>"]
        self._multi_ids = [[]]
        if ti >= 0:
            acc = []
            for l in self._levels[ti + 1:]:
                acc = acc + [l[2]]
                labels.append(l[1])
                self._multi_ids.append(list(acc))
        self.multi_cb.ItemsSource = labels
        idx = 0
        if prev is not None and prev in labels:
            idx = labels.index(prev)
        self.multi_cb.SelectedIndex = idx

    # -- settings
    def _restore(self):
        s = deew_settings.load(_TITLE, {})
        for name in _TEXTS:
            if name in s:
                try:
                    getattr(self, name).Text = u"{0}".format(s[name])
                except Exception:
                    pass
        for name in _CHECKS:
            if name in s:
                try:
                    getattr(self, name).IsChecked = bool(s[name])
                except Exception:
                    pass
        for name in _COMBOS:
            if name in s:
                try:
                    getattr(self, name).SelectedIndex = int(s[name])
                except Exception:
                    pass

        def pick(cb, names, key):
            if s.get(key) in names:
                cb.SelectedIndex = names.index(s[key])
        self._saved_mats = dict((r, s.get("mat_" + r)) for r, _c, _w in _MAT_ROLES)
        pick(self.type_cb, [t["name"] for t in self._types], "type_name")
        pick(self.rail_cb, [u"<No railings>"] + [r[0] for r in self._rails], "rail_name")
        lnames = [l[1] for l in self._levels]
        if s.get("base_level") in lnames and s.get("top_level") in lnames:
            b, t = lnames.index(s["base_level"]), lnames.index(s["top_level"])
            if t > b:
                self.base_level_cb.SelectedIndex = b
                self.top_level_cb.SelectedIndex = t

    def _save(self):
        s = {}
        for name in _TEXTS:
            s[name] = getattr(self, name).Text
        for name in _CHECKS:
            s[name] = bool(getattr(self, name).IsChecked)
        for name in _COMBOS:
            s[name] = getattr(self, name).SelectedIndex
        yi = self.type_cb.SelectedIndex
        if yi >= 0:
            s["type_name"] = self._types[yi]["name"]
        ri = self.rail_cb.SelectedIndex
        if ri == 0:
            s["rail_name"] = u"<No railings>"
        elif ri > 0:
            s["rail_name"] = self._rails[ri - 1][0]
        bi, ti = self.base_level_cb.SelectedIndex, self.top_level_cb.SelectedIndex
        if bi >= 0 and ti >= 0:
            s["base_level"], s["top_level"] = self._levels[bi][1], self._levels[ti][1]
        for role, cb_name, _sw in _MAT_ROLES:
            i = getattr(self, cb_name).SelectedIndex
            if i >= 0:
                s["mat_" + role] = self._mat_labels[i]
        deew_settings.save(_TITLE, s)

    # -- events
    def option_changed(self, sender, args):
        if getattr(self, "_ready", False):
            self._recompute()

    def shape_changed(self, sender, args):
        if getattr(self, "_ready", False):
            self._sync_panels()
            self._recompute()

    def levels_changed(self, sender, args):
        if getattr(self, "_ready", False):
            self._fill_multi()
            self._recompute()

    def type_changed(self, sender, args):
        if getattr(self, "_ready", False):
            self._prefill_custom()
            self._pick_preset(self.preset_cb.SelectedIndex)
            self._sync_panels()
            self._recompute()

    def custom_changed(self, sender, args):
        if getattr(self, "_ready", False):
            self._sync_panels()
            self._recompute()

    def preset_changed(self, sender, args):
        if not getattr(self, "_ready", False):
            return
        self._pick_preset(self.preset_cb.SelectedIndex)
        self._sync_panels()
        self._recompute()

    def mat_changed(self, sender, args):
        if getattr(self, "_ready", False):
            self._recompute()

    def place_click(self, sender, args):
        o = self._read()
        if o["errors"]:
            self.status_tb.Text = u"Cannot place yet: " + o["errors"][0]
            return
        self.options = o
        self.action = "place"
        self.Close()

    def close_click(self, sender, args):
        self.Close()

    def _on_closed(self, sender, args):
        try:
            self._save()
        except Exception:
            pass

    # -- what is shown for the chosen shape
    def _sync_panels(self):
        idx = max(0, self.shape_cb.SelectedIndex)

        def show(panel, on):
            panel.Visibility = Visibility.Visible if on else Visibility.Collapsed
        show(self.turn_panel, idx >= 2)
        show(self.flight_panel, 1 <= idx <= 5)
        show(self.landing_panel, 1 <= idx <= 3)
        show(self.gap_panel, idx in (3, 5))
        show(self.winder_panel, idx in (4, 5))
        show(self.spiral_panel, idx == 6)
        self.flight_lbl.Text = u"Risers before the winders:" if idx in (4, 5) else u"Risers, first flight:"
        self.sweep_tb.IsEnabled = self.sweep_cb.SelectedIndex == 1
        winder = idx in (4, 5)
        self.begin_chk.IsEnabled = not winder
        self.end_chk.IsEnabled = not winder
        self.angle_tb.IsEnabled = not bool(self.dir_chk.IsChecked)
        show(self.custom_panel, bool(self.custom_chk.IsChecked))
        self.finish_panel.IsEnabled = self.preset_cb.SelectedIndex >= 1
        self.preset_info_tb.Text = self._preset_text()

    # -- the stair type
    def _type_info(self, tinfo):
        t = tinfo["el"]
        bits = []
        if tinfo["max_riser"] is not None:
            bits.append(u"max riser {0:.0f} mm".format(tinfo["max_riser"] * _MM))
        if tinfo["min_tread"] is not None:
            bits.append(u"min tread {0:.0f} mm".format(tinfo["min_tread"] * _MM))
        if tinfo["min_width"] is not None:
            bits.append(u"min run width {0:.0f} mm".format(tinfo["min_width"] * _MM))
        cm = _attr(t, "ConstructionMethod")
        if cm is not None:
            bits.append(u"{0}".format(cm))
        for label, attr in ((u"run type", "RunType"), (u"landing type", "LandingType")):
            eid = _attr(t, attr)
            el = self.doc.GetElement(eid) if _valid_id(eid) else None
            if el is not None:
                bits.append(u"{0}: {1}".format(label, _ename(el)))
        return u"   |   ".join(bits)

    def _current_type(self):
        yi = self.type_cb.SelectedIndex
        return self._types[yi] if yi >= 0 else None

    def _prefill_custom(self):
        """Every field that shows the chosen type's own values."""
        tinfo = self._current_type()
        if tinfo is None:
            return
        was = getattr(self, "_ready", False)
        self._ready = False
        try:
            self.type_info_tb.Text = self._type_info(tinfo)
            auto = u"{0} - DeeStairs".format(tinfo["name"])
            self.newname_tb.Text = auto
            self._auto_name = auto
            self.t_maxriser_tb.Text = _fmt_mm(tinfo["max_riser"])
            self.t_mintread_tb.Text = _fmt_mm(tinfo["min_tread"])
            self.t_minwidth_tb.Text = _fmt_mm(tinfo["min_width"])
            self._prefill_build(tinfo)
        finally:
            self._ready = was

    def _prefill_build(self, tinfo=None):
        """The Finishes tab's fields from the chosen type: thicknesses, risers,
        supports, and the materials it has now (shown on the swatches)."""
        tinfo = tinfo or self._current_type()
        if tinfo is None:
            return
        was = getattr(self, "_ready", False)
        self._ready = False
        try:
            t = tinfo["el"]
            self.t_leftoff_tb.Text = _fmt_mm(_attr(t, "LeftLateralOffset"))
            self.t_rightoff_tb.Text = _fmt_mm(_attr(t, "RightLateralOffset"))
            n = _attr(t, "MiddleSupportsNumber")
            self.t_midn_tb.Text = u"" if n is None else u"{0}".format(int(n))
            parts = _type_parts(self.doc, t)
            rt, lt = parts["run"], parts["landing"]
            self.t_open_chk.IsChecked = (rt is not None and _attr(rt, "HasRisers") is False)
            self.t_treadthk_tb.Text = _fmt_mm(_attr(rt, "TreadThickness")) if rt is not None else u""
            self.t_riserthk_tb.Text = _fmt_mm(_attr(rt, "RiserThickness")) if rt is not None else u""
            self.t_nosing_tb.Text = _fmt_mm(_attr(rt, "NosingLength")) if rt is not None else u""
            self.t_landthk_tb.Text = _fmt_mm(_attr(lt, "Thickness")) if lt is not None else u""
            for cb in (self.t_left_cb, self.t_right_cb, self.t_mid_cb):
                cb.SelectedIndex = 0
            for role, cb_name, _sw in _MAT_ROLES:
                getattr(self, cb_name).SelectedIndex = 0
            self.preset_note_clear()

            # what the type has now: material colours and support styles
            rgb = {}

            def mat_rgb(el, pname):
                eid = _param_eid(el, pname)
                m = self.doc.GetElement(eid) if _valid_id(eid) else None
                return _material_rgb(m) if m is not None else None
            if rt is not None:
                rgb["tread"] = mat_rgb(rt, parts["tread_param"])
                rgb["riser"] = None if parts["mono_run"] else mat_rgb(rt, _P_RISER)
                if parts["mono_run"]:
                    rgb["riser"] = rgb["tread"]
            if lt is not None:
                rgb["landing"] = (mat_rgb(lt, _P_LANDING_MONO) if parts["mono_landing"] else rgb.get("tread"))
            styles = {}
            widths = []
            for key, attr in (("left", "LeftSideSupportType"), ("right", "RightSideSupportType"),
                              ("mid", "MiddleSupportType")):
                eid = _attr(t, attr)
                el = self.doc.GetElement(eid) if _valid_id(eid) else None
                if key == "mid" and not _attr(t, "HasMiddleSupports"):
                    el = None
                styles[key] = (SF.support_style(_ename(el)) or "closed") if el is not None else "none"
                if el is not None:
                    if "stringer" not in rgb:
                        rgb["stringer"] = mat_rgb(el, _P_SUPPORT)
                    w = _param_double(el, _P_SUPPORT_W)
                    if w is not None:
                        widths.append(w)
            self.t_stringw_tb.Text = _fmt_mm(widths[0]) if widths else u""
            self._cur = {"rgb": rgb, "styles": styles}
        finally:
            self._ready = was

    def preset_note_clear(self):
        self._preset_note = u""

    # -- finishes
    def _preset_text(self):
        i = self.preset_cb.SelectedIndex
        if i <= 0:
            return (u"The stair keeps the materials and build-up of its type. Pick a finish to make a copy of "
                    u"the type with that finish.")
        if i == 1:
            return (u"Choose a material for each part and set the build-up below. The materials stay as they "
                    u"are in the type where you leave '(keep the type's material)'.")
        text = SF.PRESETS[i - 2]["info"]
        return text + (u"\n" + self._preset_note if self._preset_note else u"")

    def _mat_index(self, key):
        """Combo index of a catalogue material (0 = keep the type's)."""
        if key is None:
            return 0
        for i, ref in enumerate(self._mat_refs):
            if ref is not None and ref[0] == "cat" and ref[1] == key:
                return i
        return 0

    def _support_index(self, style):
        """Support combo index for a style word: (none) / the matching type /
        (unchanged) when the project has no such type."""
        if style == "none":
            return 1, None
        names = [x[0] for x in self._supports]
        i = SF.match_support(names, style)
        if i is None:
            return 0, style
        return i + 2, None

    def _pick_preset(self, idx):
        """A new entry in the preset combo: start again from the type's own values
        and lay the preset over them."""
        if idx == 1:
            return
        was = getattr(self, "_ready", False)
        self._ready = False
        try:
            tinfo = self._current_type()
            if tinfo is not None:
                self._prefill_build(tinfo)
            if idx >= 2:
                self._apply_preset(SF.PRESETS[idx - 2])
            elif idx <= 0 and tinfo is not None:
                self.newname_tb.Text = self._auto_name or u"{0} - DeeStairs".format(tinfo["name"])
        finally:
            self._ready = was

    def _apply_preset(self, p):
        tinfo = self._current_type()
        if tinfo is not None and (self.newname_tb.Text or u"").strip() in (u"", self._auto_name):
            auto = u"{0} - {1}".format(tinfo["name"], p["short"])
            self.newname_tb.Text = auto
            self._auto_name = auto
        for role, cb_name, _sw in _MAT_ROLES:
            getattr(self, cb_name).SelectedIndex = self._mat_index(p.get(role))
        self.t_treadthk_tb.Text = u"{0}".format(p["tread_thk"])
        if p["riser_thk"] is not None:
            self.t_riserthk_tb.Text = u"{0}".format(p["riser_thk"])
        self.t_nosing_tb.Text = u"{0}".format(p["nosing"])
        self.t_open_chk.IsChecked = bool(p["open"])
        missing = []
        for cb, style in ((self.t_left_cb, p["left"]), (self.t_right_cb, p["right"]),
                          (self.t_mid_cb, p["mid"])):
            i, lost = self._support_index(style)
            cb.SelectedIndex = i
            if lost and lost not in missing:
                missing.append(lost)
        if p.get("midn") and p["mid"] != "none":
            self.t_midn_tb.Text = u"{0}".format(p["midn"])
        if p.get("stringer_w") is not None:
            self.t_stringw_tb.Text = u"{0}".format(p["stringer_w"])
        self._preset_note = u""
        if missing:
            self._preset_note = (u"This project has no {0} stair support type, so that support is left as the "
                                 u"type has it - pick one in the lists below if you have it.".format(
                                     u" / ".join(missing)))

    def _restore_finish(self):
        """After the type's own values are in: the preset chosen last time, then
        the materials chosen last time."""
        was = getattr(self, "_ready", False)
        self._ready = False
        try:
            idx = self.preset_cb.SelectedIndex
            if idx >= 2:
                self._apply_preset(SF.PRESETS[idx - 2])
            for role, cb_name, _sw in _MAT_ROLES:
                label = (self._saved_mats or {}).get(role)
                if idx >= 1 and label in self._mat_labels:
                    getattr(self, cb_name).SelectedIndex = self._mat_labels.index(label)
        finally:
            self._ready = was

    def _mat_ref(self, role):
        cb = getattr(self, dict((r, c) for r, c, _w in _MAT_ROLES)[role])
        i = cb.SelectedIndex
        return self._mat_refs[i] if 0 < i < len(self._mat_refs) else None

    def _mat_color(self, role):
        """(r, g, b) of what a part is made of: the chosen material, else the type's."""
        cb = getattr(self, dict((r, c) for r, c, _w in _MAT_ROLES)[role])
        i = cb.SelectedIndex
        if i > 0 and self._mat_rgbs[i] is not None:
            return self._mat_rgbs[i]
        return self._cur["rgb"].get(role) or SF.NEUTRAL_RGB

    def _paint_swatches(self):
        for role, _cb, sw_name in _MAT_ROLES:
            r, g, b = self._mat_color(role)
            getattr(self, sw_name).Background = SolidColorBrush(MediaColor.FromRgb(r, g, b))

    def _support_choice(self, cb):
        i = cb.SelectedIndex
        if i <= 0:
            return None
        if i == 1:
            return "none"
        return self._supports[i - 2][1]

    def _style_of(self, cb, key):
        """'closed' / 'open' / 'carriage' / 'none' of one support combo."""
        i = cb.SelectedIndex
        if i == 1:
            return "none"
        if i >= 2:
            return SF.support_style(self._supports[i - 2][0]) or "closed"
        return self._cur["styles"].get(key, "none")

    def _type_spec(self, err):
        """The changes to make on a COPY of the stair type: the rules of the
        Stair type tab (when Customise is ticked) and the finish and build-up of
        the Finishes tab (when a finish is chosen). None = use the type as it is."""
        custom_on = bool(self.custom_chk.IsChecked)
        i = self.preset_cb.SelectedIndex
        finish_on = i >= 1
        if not (custom_on or finish_on):
            return None
        s = {"name": (self.newname_tb.Text or u"").strip(), "finish": None}

        def mm(tb, key, label):
            try:
                v = _pf(tb.Text)
            except ValueError:
                err.append(u"{0}: type a number.".format(label))
                return
            s[key] = None if v is None else v / _MM
        if custom_on:
            mm(self.t_maxriser_tb, "max_riser", u"Maximum riser height")
            mm(self.t_mintread_tb, "min_tread", u"Minimum tread depth")
            mm(self.t_minwidth_tb, "min_width", u"Minimum run width")
        if finish_on:
            s["finish"] = SF.PRESETS[i - 2]["short"] if i >= 2 else u"Custom finish"
            mm(self.t_treadthk_tb, "tread_thk", u"Tread thickness")
            mm(self.t_riserthk_tb, "riser_thk", u"Riser thickness")
            mm(self.t_nosing_tb, "nosing", u"Nosing length")
            mm(self.t_landthk_tb, "land_thk", u"Landing thickness")
            mm(self.t_leftoff_tb, "left_off", u"Left support offset")
            mm(self.t_rightoff_tb, "right_off", u"Right support offset")
            mm(self.t_stringw_tb, "stringer_w", u"Support width")
            s["open"] = bool(self.t_open_chk.IsChecked)
            s["left"] = self._support_choice(self.t_left_cb)
            s["right"] = self._support_choice(self.t_right_cb)
            s["mid"] = self._support_choice(self.t_mid_cb)
            try:
                n = _pf(self.t_midn_tb.Text)
                s["midn"] = None if n is None else int(round(n))
            except ValueError:
                err.append(u"Middle supports: type a whole number.")
            s["mat"] = dict((role, self._mat_ref(role)) for role, _c, _w in _MAT_ROLES)
        return s

    # -- the options
    def _read(self):
        """Controls -> one options dict (feet). o['errors'] stops Place."""
        o = {"errors": [], "warnings": [], "info": u"", "plan": None}
        err = o["errors"]
        warn = o["warnings"]

        def num(tb, label, scale=1.0 / _MM):
            try:
                v = _pf(tb.Text)
            except ValueError:
                err.append(u"{0}: type a number.".format(label))
                return None
            return None if v is None else v * scale

        bi, ti = self.base_level_cb.SelectedIndex, self.top_level_cb.SelectedIndex
        yi = self.type_cb.SelectedIndex
        if bi < 0 or ti < 0 or yi < 0:
            err.append(u"Pick the base level, the top level and a stair type.")
            return o
        base, top, tinfo = self._levels[bi], self._levels[ti], self._types[yi]
        base_off = num(self.base_off_tb, u"Base offset") or 0.0
        top_off = num(self.top_off_tb, u"Top offset") or 0.0
        H = (top[0] + top_off) - (base[0] + base_off)
        self.height_tb.Text = u"{0:.0f} mm".format(H * _MM)
        if H < 0.5:
            err.append(u"The top must be at least 150 mm above the base - choose a higher top level.")
        o.update({"base_level_id": base[2], "top_level_id": top[2], "base_off": base_off,
                  "top_off": top_off, "H": H, "type_id": tinfo["id"], "type_name": tinfo["name"]})

        # limits of the stair type (or of the customised copy)
        max_riser = tinfo["max_riser"] or (190.0 / _MM)
        min_tread = tinfo["min_tread"] or (250.0 / _MM)
        min_width = tinfo["min_width"] or (900.0 / _MM)
        custom = self._type_spec(err)
        if custom is not None:
            max_riser = custom.get("max_riser") or max_riser
            min_tread = custom.get("min_tread") or min_tread
            min_width = custom.get("min_width") or min_width
        o["custom"] = custom

        n_min = max(2, int(math.ceil(H / max_riser - 1e-6))) if H >= 0.5 else 2
        n_user = num(self.risers_tb, u"Number of risers", 1.0)
        n = n_min
        if n_user is not None:
            n = int(round(n_user))
            if n < n_min:
                warn.append(u"{0} risers would be higher than the {1:.0f} mm maximum - using {2}."
                            .format(n, max_riser * _MM, n_min))
                n = n_min
        T = min_tread
        t_user = num(self.tread_tb, u"Tread depth")
        if t_user:
            T = t_user
            if T < min_tread - 1e-9:
                warn.append(u"Tread depth raised to the type's minimum of {0:.0f} mm.".format(min_tread * _MM))
                T = min_tread
        W = num(self.width_tb, u"Run width") or min_width
        self.risers_info_tb.Text = u"automatic = {0}".format(n_min)
        self.tread_info_tb.Text = u"type minimum {0:.0f}".format(min_tread * _MM)
        self.width_info_tb.Text = u"type minimum {0:.0f}".format(min_width * _MM)

        idx = max(0, self.shape_cb.SelectedIndex)
        shape = _SHAPES[idx]
        turn = u"right" if self.turn_cb.SelectedIndex == 1 else u"left"
        side = _JUST_SIDE[max(0, self.just_cb.SelectedIndex)]
        winder = shape in ("winder_l", "winder_u")
        begin = True if winder else bool(self.begin_chk.IsChecked)
        end = True if winder else bool(self.end_chk.IsChecked)
        f1 = num(self.flight1_tb, u"First flight", 1.0)
        landing = num(self.landing_tb, u"Landing depth")
        gap = num(self.gap_tb, u"Gap between flights") or 0.0
        winders = max(0, self.winders_cb.SelectedIndex) + 2
        radius = None
        sweep = None
        if shape == "spiral":
            r_loc = num(self.radius_tb, u"Radius")
            if r_loc is None:
                err.append(u"Give the spiral a radius.")
            else:
                t_dir = 1.0 if turn == u"left" else -1.0
                radius = r_loc + side * t_dir * W / 2.0      # centre-line radius
            if self.sweep_cb.SelectedIndex == 1:
                deg = num(self.sweep_tb, u"Sweep", 1.0)
                if deg is None or deg <= 0:
                    err.append(u"Give the spiral a sweep angle in degrees.")
                else:
                    sweep = math.radians(deg)
        pa = {"shape": shape, "n": n, "T": T, "W": W, "turn": turn, "begin": begin, "end": end,
              "flight1": int(round(f1)) if f1 is not None else None, "landing": landing, "gap": gap,
              "winders": winders, "radius": radius, "sweep": sweep}
        plan = None
        if not err:
            try:
                plan = SL.make_plan(**pa)
            except Exception as e:
                err.append(u"Cannot plan these stairs: {0}".format(e))
        if plan is not None:
            err.extend(plan.errors)
            warn.extend(plan.warnings)
            if plan.ok and plan.tread < min_tread - 1e-6:
                warn.append(u"The tread comes out at {0:.0f} mm, below the type's minimum of {1:.0f} mm - "
                            u"Revit may refuse it.".format(plan.tread * _MM, min_tread * _MM))
        R = H / n
        o.update({"n": n, "R": R, "plan_args": pa, "plan": plan, "side": side,
                  "ext": abs(num(self.ext_tb, u"Extension below base") or 0.0)})
        tread = plan.tread if plan is not None and plan.ok else T
        comfort = (2 * R + tread) * _MM
        if comfort < 550 or comfort > 700:
            warn.append(u"2 x riser + tread = {0:.0f} mm; a comfortable stair is between 550 and 700 mm."
                        .format(comfort))
        info = u"{0} risers x {1:.0f} mm   |   tread {2:.0f} mm   |   width {3:.0f} mm".format(
            n, R * _MM, tread * _MM, W * _MM)
        if plan is not None and plan.ok:
            if len(plan.runs) > 1:
                info += u"   |   flights " + u" + ".join(u"{0}".format(f) for f in plan.flights())
            info += u"   |   walking line {0:.1f} m".format(plan.walk_length * 0.3048)
            if plan.notes:
                info += u"   |   " + u", ".join(plan.notes)
        o["info"] = info

        # railings, storeys, placement
        ri = self.rail_cb.SelectedIndex
        o["rail_type_id"] = self._rails[ri - 1][1] if ri > 0 else None
        o["rail_pos"] = (RailingPlacementPosition.Treads if self.rail_pos_cb.SelectedIndex == 0
                         else RailingPlacementPosition.Stringer)
        o["rail_side"] = (0, 1, -1)[max(0, self.rail_side_cb.SelectedIndex)]
        mi = self.multi_cb.SelectedIndex
        o["multi_levels"] = list(self._multi_ids[mi]) if 0 < mi < len(self._multi_ids) else []
        o["dir_pick"] = bool(self.dir_chk.IsChecked)
        o["angle"] = math.radians(num(self.angle_tb, u"Angle", 1.0) or 0.0)
        o["multi_place"] = bool(self.multi_chk.IsChecked)
        return o

    def _recompute(self):
        o = self._read()
        self._draw(o.get("plan"))
        self._paint_swatches()
        self._draw_section(o)
        self.info_tb.Text = o.get("info", u"")
        self.warn_tb.Text = u"\n".join(o["errors"] + o["warnings"])
        self.place_b.IsEnabled = not o["errors"]
        return o

    # -- section sketch of the build-up
    def _mm_field(self, tb):
        try:
            return _pf(tb.Text)
        except ValueError:
            return None

    def _clear_section(self):
        for p in (self.sec_floor_path, self.sec_stringer_path, self.sec_riser_path, self.sec_tread_path):
            p.Data = None
        self.sec_tb.Text = u""

    def _draw_section(self, o):
        """Three steps in side view: tread (finished thickness, nosing), riser board,
        stringer and floor, filled with the colour of their material."""
        plan = o.get("plan")
        if not o.get("R") or plan is None or not plan.ok:
            self._clear_section()
            return
        R, T = o["R"] * _MM, plan.tread * _MM
        tt = self._mm_field(self.t_treadthk_tb) or 0.0
        rt = self._mm_field(self.t_riserthk_tb) or 0.0
        ns = self._mm_field(self.t_nosing_tb) or 0.0
        has_riser = not bool(self.t_open_chk.IsChecked)
        style = SF.section_stringer(self._style_of(self.t_left_cb, "left"),
                                    self._style_of(self.t_right_cb, "right"),
                                    self._style_of(self.t_mid_cb, "mid"))
        try:
            sec = SF.section(R, T, tt, rt, ns, has_riser, style, steps=3)
        except Exception:
            self._clear_section()
            return
        x0, z0, x1, z1 = sec["bbox"]
        pad = max(x1 - x0, z1 - z0) * 0.05 + 10.0
        x0, z0, x1, z1 = x0 - pad, z0 - pad, x1 + pad, z1 + pad

        def ring(poly):
            pts = [u"{0:.2f},{1:.2f}".format(x - x0, z1 - z) for (x, z) in poly]
            return u"M " + u" L ".join(pts) + u" Z"
        self.sec_grid.Width = x1 - x0
        self.sec_grid.Height = z1 - z0
        lw = max(x1 - x0, z1 - z0) / 420.0
        fills = (("floor", self.sec_floor_path, (205, 205, 200)),
                 ("stringer", self.sec_stringer_path, self._mat_color("stringer")),
                 ("risers", self.sec_riser_path, self._mat_color("riser")),
                 ("treads", self.sec_tread_path, self._mat_color("tread")))
        try:
            for key, path, rgb in fills:
                path.StrokeThickness = lw
                path.Fill = SolidColorBrush(MediaColor.FromRgb(rgb[0], rgb[1], rgb[2]))
                polys = sec[key]
                path.Data = Geometry.Parse(u" ".join(ring(q) for q in polys)) if polys else None
        except Exception:
            self._clear_section()
            return
        self.sec_tb.Text = (u"Section sketch (3 steps) - riser {0:.0f} mm, going {1:.0f} mm  |  tread {2:.0f}, "
                            u"riser board {3}, nosing {4:.0f} mm  |  stringer: {5}".format(
                                R, T, tt, u"none (open)" if not has_riser else u"{0:.0f}".format(rt), ns, style))

    # -- preview
    def _clear_preview(self):
        for p in (self.landing_path, self.run_path, self.tread_path, self.walk_path,
                  self.start_path, self.end_path):
            p.Data = None

    def _draw(self, plan):
        if plan is None or not plan.ok or not plan.runs:
            self._clear_preview()
            return
        x0, y0, x1, y1 = plan.bbox
        pad = max(x1 - x0, y1 - y0) * 0.04 + 0.3
        x0, y0, x1, y1 = x0 - pad, y0 - pad, x1 + pad, y1 + pad
        w, h = x1 - x0, y1 - y0

        def X(x):
            return x - x0

        def Y(y):
            return y1 - y

        def ring(pts):
            s = u"M {0:.4f},{1:.4f}".format(X(pts[0][0]), Y(pts[0][1]))
            for p in pts[1:]:
                s += u" L {0:.4f},{1:.4f}".format(X(p[0]), Y(p[1]))
            return s + u" Z"

        def line(pts):
            s = u"M {0:.4f},{1:.4f}".format(X(pts[0][0]), Y(pts[0][1]))
            for p in pts[1:]:
                s += u" L {0:.4f},{1:.4f}".format(X(p[0]), Y(p[1]))
            return s

        self.canvas_grid.Width = w
        self.canvas_grid.Height = h
        lw = max(w, h) / 600.0
        for path, k in ((self.landing_path, 1.0), (self.run_path, 1.0),
                        (self.tread_path, 0.7), (self.walk_path, 2.2)):
            path.StrokeThickness = lw * k
        try:
            self.run_path.Data = Geometry.Parse(u" ".join(ring(r.outline) for r in plan.runs))
            self.landing_path.Data = (Geometry.Parse(u" ".join(ring(l.polygon) for l in plan.landings))
                                      if plan.landings else None)
            treads = []
            for r in plan.runs:
                for a, b in r.tread_lines:
                    treads.append(line([a, b]))
            self.tread_path.Data = Geometry.Parse(u" ".join(treads))
            self.walk_path.Data = Geometry.Parse(u" ".join(line(r.center) for r in plan.runs))
        except Exception:
            self._clear_preview()
            return
        dot = max(w, h) / 70.0
        starts = GeometryGroup()
        ends = GeometryGroup()
        first = plan.runs[0].center[0]
        starts.Children.Add(EllipseGeometry(Point(X(first[0]), Y(first[1])), dot, dot))
        last = plan.runs[-1].center[-1]
        ends.Children.Add(EllipseGeometry(Point(X(last[0]), Y(last[1])), dot, dot))
        self.start_path.Data = starts
        self.end_path.Data = ends


# --------------------------------------------------------------------------
# placing
# --------------------------------------------------------------------------
_SNAP = (ObjectSnapTypes.Endpoints | ObjectSnapTypes.Midpoints | ObjectSnapTypes.Intersections |
         ObjectSnapTypes.Centers | ObjectSnapTypes.Nearest)


def _place(uidoc, doc, o):
    """Ask for the start point and direction, build, repeat while 'place
    several' is on. Returns the line for the window's status bar."""
    if uidoc.ActiveView.ViewType not in _PLAN_VIEWS:
        forms.alert(u"Open a plan view (floor plan, ceiling plan, ...) first - the stairs are placed "
                    u"by clicking in the plan.", title=_TITLE)
        return u"Open a plan view to place the stairs."
    placed = 0
    status = u"Nothing placed."
    while True:
        try:
            p0 = uidoc.Selection.PickPoint(
                _SNAP, u"DeeStairs: click the START of the stairs (Esc = back to the options)")
        except OperationCanceledException:
            break
        ang = o["angle"]
        if o["dir_pick"]:
            try:
                p1 = uidoc.Selection.PickPoint(
                    _SNAP, u"DeeStairs: click a point the stairs head towards (Esc = skip this one)")
            except OperationCanceledException:
                if o["multi_place"]:
                    continue
                break
            if math.hypot(p1.X - p0.X, p1.Y - p0.Y) > 1e-4:
                ang = math.atan2(p1.Y - p0.Y, p1.X - p0.X)
        # the click is on the first run's location line - move to its centre line
        shift = o["side"] * o["plan_args"]["W"] / 2.0
        origin = (p0.X + math.sin(ang) * shift, p0.Y - math.cos(ang) * shift)
        try:
            res = create_stairs(doc, uidoc, o, origin, ang)
        except Exception as e:
            forms.alert(u"DeeStairs could not build the stairs - nothing was changed.\n\n{0}".format(e),
                        title=_TITLE)
            return u"FAILED - nothing was changed: {0}".format(e)
        placed += 1
        status = _summary(res, o)
        if placed > 1:
            status = u"{0} stairs placed. Last: ".format(placed) + status
        if res["notes"]:
            forms.alert(_summary(res, o) + u"\n\nPlease check:\n- " + u"\n- ".join(res["notes"]),
                        title=_TITLE)
            status += u"   (" + u"; ".join(res["notes"]) + u")"
        if not o["multi_place"]:
            break
    return status


def main():
    uidoc = __revit__.ActiveUIDocument
    if uidoc is None:
        forms.alert(u"Open a Revit project first.", title=_TITLE)
        return
    doc = uidoc.Document
    if doc.IsFamilyDocument:
        forms.alert(u"DeeStairs places stairs in a project, not in the Family Editor.", title=_TITLE)
        return
    if len(_levels(doc)) < 2:
        forms.alert(u"Stairs need two levels - this project has fewer.", title=_TITLE)
        return
    if not _stairs_types(doc):
        forms.alert(u"This project has no stair types (Stair by Component). Load or create one first.",
                    title=_TITLE)
        return
    status = u""
    while True:
        win = StairsWindow(_XAML, doc, uidoc, status)
        win.ShowDialog()
        if win.action != "place":
            return
        status = _place(uidoc, doc, win.options)


main()
