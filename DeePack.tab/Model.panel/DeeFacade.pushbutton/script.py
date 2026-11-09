# -*- coding: utf-8 -*-
"""
DeeFacade
Pick a wall, design a perforated facade pattern on it with live preview,
then choose how it is built:

1. Cut into the wall - every opening becomes an inner loop of the wall's
   own Edit Profile sketch (Wall.CreateProfileSketch + SketchEditScope,
   Revit 2022+). The wall itself gets the holes; Undo or Reset Profile
   brings the plain wall back.

2. Generic Model family - a new family built from Revit's own Generic
   Model template: one extrusion the shape of the wall's profile with the
   pattern as inner loops, a 'Facade Material' type parameter, door /
   window cut-outs as voids. Saved as .rfa, loaded, placed on the wall
   (replacing it, or as a screen in front of / behind it), and the
   original wall can be kept, hidden in the view or deleted.

Patterns (all with their own sliders): Voronoi, Hexagons, Diamonds,
Grid / slots, Circles. Shared settings: mullion width, border, corner
rounding, fade (solid-to-open gradient) with direction, smallest opening,
keep clear of doors / windows. The geometry lives in facade_patterns.py
(pure Python, tested outside Revit); this file is only the Revit side.
"""
import os
import sys
import math
import random
import datetime

import clr
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
clr.AddReference("WindowsBase")
clr.AddReference("System.Windows.Forms")
from System import TimeSpan
from System.Collections.Generic import List
from System.Windows import Visibility
from System.Windows.Media import Geometry
from System.Windows.Threading import DispatcherTimer
from System.Windows.Forms import FolderBrowserDialog, OpenFileDialog, DialogResult

from Autodesk.Revit.DB import (
    Arc, BuiltInParameter, CurveArray, CurveArrArray, CurveElement, ElementId,
    ElementTransformUtils, FailureProcessingResult, FailureSeverity,
    FilteredElementCollector, HostObjectUtils, IFailuresPreprocessor,
    IFamilyLoadOptions, FamilySource, Line, Material, Plane, SaveAsOptions,
    ShellLayerType, SketchPlane, Transaction, Wall, WallKind, XYZ,
)
from Autodesk.Revit.DB.Structure import StructuralType
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType
from Autodesk.Revit.Exceptions import OperationCanceledException

try:                                   # Revit 2022+
    from Autodesk.Revit.DB import SketchEditScope
except ImportError:
    SketchEditScope = None

from pyrevit import forms
import dee_branding
import dee_telemetry
import deew_settings
dee_telemetry.check_access("DeeFacade")

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)
import facade_patterns as FP

_XAML = os.path.join(_THIS_DIR, "ui.xaml")
_TITLE = "DeeFacade"
_MM = 304.8                         # mm per foot
_PATTERNS = ["voronoi", "hexagon", "diamond", "grid", "circles"]
_DIRS = ["top", "bottom", "left", "right", "edges", "centre"]
_MAT_PARAM = "Facade Material"

# slider name -> how its value reads (mm / % / plain number)
_SLIDERS = [
    ("vor_count_sl", "n"), ("vor_relax_sl", "n"), ("vor_density_sl", "%"),
    ("hex_size_sl", "mm"), ("dia_w_sl", "mm"), ("dia_h_sl", "mm"),
    ("grid_w_sl", "mm"), ("grid_h_sl", "mm"),
    ("circ_spacing_sl", "mm"), ("circ_dia_sl", "%"), ("circ_jitter_sl", "%"),
    ("mullion_sl", "mm"), ("border_sl", "mm"), ("round_sl", "%"), ("fade_sl", "%"),
    ("min_sl", "mm"), ("fam_thick_sl", "mm"), ("fam_gap_sl", "mm"),
]
_CHECKS = ["replace_cb", "grid_stagger_cb", "circ_stagger_cb", "keep_partial_cb",
           "avoid_cb", "fam_cut_cb", "hex_pointy_rb", "hex_flat_rb",
           "out_profile_rb", "out_family_rb"]
_COMBOS = ["pattern_cb", "dir_cb", "fam_pos_cb", "fam_wall_cb"]
_TEXTS = ["seed_tb", "fam_folder_tb"]


def _default_folder():
    return os.path.join(os.path.expanduser("~"), "Documents", "DeeFacade")


# --------------------------------------------------------------------------
# the wall, in its own elevation coordinates (u along the wall, v = world Z)
# --------------------------------------------------------------------------
class WallData(object):
    def __init__(self, doc, wall):
        self.doc = doc
        self.wall = wall
        self.kind = wall.WallType.Kind
        if self.kind == WallKind.Curtain:
            raise ValueError(u"DeeFacade works on Basic / Stacked walls - '{0}' is a curtain wall."
                             .format(wall.Name))
        loc = wall.Location
        crv = getattr(loc, "Curve", None)
        if not isinstance(crv, Line):
            raise ValueError(u"DeeFacade works on straight walls - this one is curved "
                             u"(or has no location line).")
        p0 = crv.GetEndPoint(0)
        p1 = crv.GetEndPoint(1)
        d = XYZ(p1.X - p0.X, p1.Y - p0.Y, 0)
        self.length = d.GetLength()
        self.dir = d.Normalize()
        self.origin = XYZ(p0.X, p0.Y, 0)
        o = wall.Orientation
        self.ext = XYZ(o.X, o.Y, 0).Normalize()
        # seen from outside, does u run left -> right?
        self.flip = self.dir.DotProduct(XYZ.BasisZ.CrossProduct(self.ext)) < 0
        try:
            self.width = wall.Width
        except Exception:
            self.width = wall.WallType.Width
        self.tol = max(doc.Application.ShortCurveTolerance * 1.5, 2.0 / _MM)
        self.sketch = self._sketch()
        self.outer, self.inner = self._profile()
        self.inserts = self._inserts()
        self.d_ext, self.d_int = self._faces()

    # -- coordinates
    def uv(self, p):
        return ((p.X - self.origin.X) * self.dir.X + (p.Y - self.origin.Y) * self.dir.Y, p.Z)

    def world(self, u, v):
        return XYZ(self.origin.X + self.dir.X * u, self.origin.Y + self.dir.Y * u, v)

    # -- reading the wall
    def _sketch(self):
        try:
            sid = self.wall.SketchId
        except Exception:
            return None
        if sid is None or sid == ElementId.InvalidElementId:
            return None
        return self.doc.GetElement(sid)

    def _chain(self, curves):
        segs = []
        for c in curves:
            segs.append([self.uv(p) for p in c.Tessellate()])
        if not segs:
            return []
        for first_rev in (False, True):
            pool = [list(s) for s in segs]
            poly = list(reversed(pool[0])) if first_rev else list(pool[0])
            pool.pop(0)
            ok = True
            while pool:
                end = poly[-1]
                hit = None
                for i, s in enumerate(pool):
                    if _d2(s[0], end) < 1e-8:
                        hit = (i, s)
                        break
                    if _d2(s[-1], end) < 1e-8:
                        hit = (i, list(reversed(s)))
                        break
                if hit is None:
                    ok = False
                    break
                pool.pop(hit[0])
                poly.extend(hit[1][1:])
            if ok:
                if _d2(poly[0], poly[-1]) < 1e-8:
                    poly.pop()
                return poly
        return []

    def _profile(self):
        if self.sketch is not None:
            loops = []
            try:
                for carr in self.sketch.Profile:
                    poly = self._chain([c for c in carr])
                    if len(poly) >= 3:
                        loops.append(poly)
            except Exception:
                loops = []
            if loops:
                loops.sort(key=lambda p: -abs(FP.area(p)))
                return loops[0], loops[1:]
        bb = self.wall.get_BoundingBox(None)
        z0 = bb.Min.Z
        z1 = bb.Max.Z
        try:
            h = self.wall.get_Parameter(BuiltInParameter.WALL_USER_HEIGHT_PARAM).AsDouble()
            if h > 0:
                z1 = z0 + h
        except Exception:
            pass
        return [(0.0, z0), (self.length, z0), (self.length, z1), (0.0, z1)], []

    def _inserts(self):
        rects = []
        try:
            ids = self.wall.FindInserts(True, False, True, True)
        except Exception:
            return rects
        for eid in ids:
            el = self.doc.GetElement(eid)
            bb = el.get_BoundingBox(None) if el is not None else None
            if bb is None:
                continue
            pts = []
            for x in (bb.Min.X, bb.Max.X):
                for y in (bb.Min.Y, bb.Max.Y):
                    for z in (bb.Min.Z, bb.Max.Z):
                        pts.append(self.uv(bb.Transform.OfPoint(XYZ(x, y, z))))
            rects.append((min(p[0] for p in pts), min(p[1] for p in pts),
                          max(p[0] for p in pts), max(p[1] for p in pts)))
        return rects

    def _faces(self):
        """Signed distance of the exterior / interior faces from the
        location line, measured along the exterior normal."""
        out = []
        for side in (ShellLayerType.Exterior, ShellLayerType.Interior):
            try:
                ref = list(HostObjectUtils.GetSideFaces(self.wall, side))[0]
                face = self.wall.GetGeometryObjectFromReference(ref)
                q = face.Origin
                out.append((q.X - self.origin.X) * self.ext.X + (q.Y - self.origin.Y) * self.ext.Y)
            except Exception:
                out.append(None)
        if out[0] is None or out[1] is None or abs(out[0] - out[1]) < 1e-6:
            return self.width / 2.0, -self.width / 2.0
        return out[0], out[1]

    def bounds(self):
        b = FP.bbox(self.outer)
        return b


def _d2(a, b):
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2


# --------------------------------------------------------------------------
# Revit helpers
# --------------------------------------------------------------------------
class _WallFilter(ISelectionFilter):
    def AllowElement(self, element):
        return isinstance(element, Wall)

    def AllowReference(self, reference, position):
        return False


class _QuietFailures(IFailuresPreprocessor):
    def PreprocessFailures(self, accessor):
        for f in accessor.GetFailureMessages():
            if f.GetSeverity() == FailureSeverity.Warning:
                accessor.DeleteWarning(f)
        return FailureProcessingResult.Continue


class _LoadOptions(IFamilyLoadOptions):
    def OnFamilyFound(self, familyInUse, overwriteParameterValues):
        overwriteParameterValues.Value = True
        return True

    def OnSharedFamilyFound(self, sharedFamily, familyInUse, source, overwriteParameterValues):
        source.Value = FamilySource.Family
        overwriteParameterValues.Value = True
        return True


def _seg_curve(seg, P):
    """One facade_patterns segment -> a Revit Line / Arc via point map P."""
    if seg[0] == "L":
        return Line.CreateBound(P(seg[1]), P(seg[2]))
    return Arc.Create(P(seg[1]), P(seg[2]), P(seg[3]))


def find_generic_template(app):
    """Revit's own 'Metric Generic Model.rft' (or 'Generic Model.rft')."""
    roots = []
    try:
        base = app.FamilyTemplatePath
        if base:
            roots += [base, os.path.dirname(base.rstrip("\\/"))]
    except Exception:
        pass
    pd = os.environ.get("ProgramData") or r"C:\ProgramData"
    try:
        roots.append(os.path.join(pd, "Autodesk", "RVT " + str(app.VersionNumber), "Family Templates"))
    except Exception:
        pass
    skip = ("face", "wall", "ceiling", "floor", "roof", "line", "adaptive", "pattern",
            "two level", "baluster", "hosted", "pattern")
    found = []
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                lf = f.lower()
                if lf.endswith(".rft") and "generic model" in lf and not any(k in lf for k in skip):
                    found.append(os.path.join(dirpath, f))
        if found:
            break

    def rank(p):
        lf = os.path.basename(p).lower()
        return (0 if lf.startswith("metric") else 1, 0 if "english" in p.lower() else 1, len(lf))
    found.sort(key=rank)
    return found[0] if found else None


def _add_material_param(fm):
    try:
        from Autodesk.Revit.DB import GroupTypeId, SpecTypeId
        return fm.AddParameter(_MAT_PARAM, GroupTypeId.Materials, SpecTypeId.Reference.Material, False)
    except Exception:
        pass
    from Autodesk.Revit.DB import BuiltInParameterGroup, ParameterType
    return fm.AddParameter(_MAT_PARAM, BuiltInParameterGroup.PG_MATERIALS, ParameterType.Material, False)


# --------------------------------------------------------------------------
# option 1: cut the openings into the wall's profile sketch
# --------------------------------------------------------------------------
def apply_profile(wd, holes, replace):
    doc = wd.doc
    wall = wd.wall
    if SketchEditScope is None or not hasattr(wall, "CreateProfileSketch"):
        raise ValueError(u"Editing a wall's profile from a tool needs Revit 2022 or newer.\n"
                         u"Use 'Build a Generic Model family' instead.")
    if wd.kind != WallKind.Basic:
        raise ValueError(u"Only Basic walls have an editable profile (this is a stacked wall).\n"
                         u"Use 'Build a Generic Model family' instead.")

    sketch = wd.sketch
    if sketch is None:
        try:
            if not wall.CanHaveProfileSketch():
                raise ValueError(u"Revit does not allow a profile sketch on this wall.")
        except AttributeError:
            pass
        t = Transaction(doc, "DeeFacade - edit wall profile")
        t.Start()
        try:
            sketch = wall.CreateProfileSketch()
            t.Commit()
        except Exception:
            t.RollBack()
            raise
        if sketch is None:
            sketch = doc.GetElement(wall.SketchId)

    loops = []
    for carr in sketch.Profile:
        cs = [c for c in carr]
        loops.append((abs(FP.area(wd._chain(cs)) or 0.0) if cs else 0.0, cs))
    loops.sort(key=lambda x: -x[0])
    outer_curves = loops[0][1] if loops else []

    plane = sketch.SketchPlane.GetPlane()
    n = plane.Normal
    O = plane.Origin

    def P(uv):
        w = wd.world(uv[0], uv[1])
        k = (w - O).DotProduct(n)
        return w - n.Multiply(k)

    scope = SketchEditScope(doc, "DeeFacade - pattern openings")
    scope.Start(sketch.Id)
    made = 0
    try:
        t = Transaction(doc, "DeeFacade openings")
        t.Start()
        try:
            if replace:
                dels = List[ElementId]()
                for eid in sketch.GetAllElements():
                    el = doc.GetElement(eid)
                    if not isinstance(el, CurveElement):
                        continue
                    m = el.GeometryCurve.Evaluate(0.5, True)
                    if not any(oc.Distance(m) < 1e-4 for oc in outer_curves):
                        dels.Add(eid)
                if dels.Count:
                    doc.Delete(dels)
            sp = sketch.SketchPlane
            for h in holes:
                for seg in FP.hole_segments(h, wd.tol):
                    doc.Create.NewModelCurve(_seg_curve(seg, P), sp)
                    made += 1
            t.Commit()
        except Exception:
            t.RollBack()
            raise
        scope.Commit(_QuietFailures())
    except Exception:
        try:
            if scope.IsActive:
                scope.Cancel()
        except Exception:
            pass
        raise
    return made


# --------------------------------------------------------------------------
# option 2: a Generic Model family the shape of the wall
# --------------------------------------------------------------------------
def apply_family(wd, holes, o):
    doc = wd.doc
    app = doc.Application
    template = find_generic_template(app)
    if not template:
        dlg = OpenFileDialog()
        dlg.Filter = "Family template (*.rft)|*.rft"
        dlg.Title = "DeeFacade - pick the 'Metric Generic Model.rft' family template"
        if dlg.ShowDialog() != DialogResult.OK:
            return None
        template = dlg.FileName

    name = o["fam_name"]
    folder = o["fam_folder"] or _default_folder()
    if not os.path.isdir(folder):
        os.makedirs(folder)
    path = os.path.join(folder, name + ".rfa")

    # panel depth range, measured along the wall's exterior normal
    if o["fam_pos"] == 0:
        y_lo, y_hi = sorted([wd.d_int, wd.d_ext])
    elif o["fam_pos"] == 1:
        y_lo = max(wd.d_ext, wd.d_int) + o["fam_gap"]
        y_hi = y_lo + o["fam_thick"]
    else:
        y_hi = min(wd.d_ext, wd.d_int) - o["fam_gap"]
        y_lo = y_hi - o["fam_thick"]
    # family +Y ends up along Z x dir once rotated onto the wall
    sign = 1.0 if wd.ext.DotProduct(XYZ.BasisZ.CrossProduct(wd.dir)) > 0 else -1.0
    ly0, ly1 = sorted([sign * y_lo, sign * y_hi])

    zb = FP.bbox(wd.outer)[1]

    def P(uv):
        return XYZ(uv[0], 0.0, uv[1] - zb)

    outer = FP.clean(wd.outer, wd.tol * 3) or wd.outer
    fdoc = None
    family = None
    try:
        fdoc = app.NewFamilyDocument(template)
        t = Transaction(fdoc, "DeeFacade panel")
        t.Start()
        try:
            sp = SketchPlane.Create(fdoc, Plane.CreateByNormalAndOrigin(XYZ.BasisY, XYZ.Zero))
            prof = CurveArrArray()
            ca = CurveArray()
            for i in range(len(outer)):
                ca.Append(Line.CreateBound(P(outer[i]), P(outer[(i + 1) % len(outer)])))
            prof.Append(ca)
            for h in holes:
                ca = CurveArray()
                for seg in FP.hole_segments(h, wd.tol):
                    ca.Append(_seg_curve(seg, P))
                prof.Append(ca)
            ext = fdoc.FamilyCreate.NewExtrusion(True, prof, sp, ly1 - ly0)
            fdoc.Regenerate()
            bb = ext.get_BoundingBox(None)
            dy = ly0 - bb.Min.Y
            if abs(dy) > 1e-9:
                ElementTransformUtils.MoveElement(fdoc, ext.Id, XYZ(0, dy, 0))

            # door / window cut-outs as voids (they may touch the edge)
            if o["fam_cut"]:
                for r in wd.inserts:
                    vca = CurveArray()
                    corners = [(r[0], r[1]), (r[2], r[1]), (r[2], r[3]), (r[0], r[3])]
                    for i in range(4):
                        vca.Append(Line.CreateBound(P(corners[i]), P(corners[(i + 1) % 4])))
                    varr = CurveArrArray()
                    varr.Append(vca)
                    void = fdoc.FamilyCreate.NewExtrusion(False, varr, sp, ly1 - ly0 + 1.0)
                    fdoc.Regenerate()
                    vb = void.get_BoundingBox(None)
                    ElementTransformUtils.MoveElement(fdoc, void.Id, XYZ(0, ly0 - 0.5 - vb.Min.Y, 0))
                    try:
                        from Autodesk.Revit.DB import SolidSolidCutUtils
                        SolidSolidCutUtils.AddCutBetweenSolids(fdoc, ext, void)
                    except Exception:
                        pass

            fm = fdoc.FamilyManager
            if fm.CurrentType is None:
                fm.NewType(name)
            try:
                fp = _add_material_param(fm)
                fm.AssociateElementParameterToFamilyParameter(
                    ext.get_Parameter(BuiltInParameter.MATERIAL_ID_PARAM), fp)
            except Exception:
                pass
            t.Commit()
        except Exception:
            t.RollBack()
            raise
        so = SaveAsOptions()
        so.OverwriteExistingFile = True
        fdoc.SaveAs(path, so)
        family = fdoc.LoadFamily(doc, _LoadOptions())
    finally:
        if fdoc is not None:
            try:
                fdoc.Close(False)
            except Exception:
                pass
    if family is None:
        raise ValueError(u"Revit did not load the new family.")

    t = Transaction(doc, "DeeFacade - place facade panel")
    t.Start()
    try:
        sym = doc.GetElement(list(family.GetFamilySymbolIds())[0])
        if not sym.IsActive:
            sym.Activate()
            doc.Regenerate()
        pt = XYZ(wd.origin.X, wd.origin.Y, zb)
        level = doc.GetElement(wd.wall.LevelId)
        inst = doc.Create.NewFamilyInstance(pt, sym, level, StructuralType.NonStructural)
        doc.Regenerate()
        lp = inst.Location.Point
        delta = pt - lp
        if delta.GetLength() > 1e-7:
            ElementTransformUtils.MoveElement(doc, inst.Id, delta)
        angle = math.atan2(wd.dir.Y, wd.dir.X)
        if abs(angle) > 1e-9:
            ElementTransformUtils.RotateElement(doc, inst.Id, Line.CreateBound(pt, pt + XYZ.BasisZ), angle)
        if o["fam_mat"] is not None:
            p = sym.LookupParameter(_MAT_PARAM)
            if p is not None and not p.IsReadOnly:
                p.Set(o["fam_mat"])
        wall_note = u""
        if o["fam_wall"] == 1:
            try:
                doc.ActiveView.HideElements(List[ElementId]([wd.wall.Id]))
                wall_note = u"Wall hidden in this view."
            except Exception as e:
                wall_note = u"Could not hide the wall here: {0}".format(e)
        elif o["fam_wall"] == 2:
            doc.Delete(wd.wall.Id)
            wall_note = u"Original wall deleted."
        t.Commit()
    except Exception:
        t.RollBack()
        raise
    return path, inst, wall_note


# --------------------------------------------------------------------------
# window
# --------------------------------------------------------------------------
class FacadeWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, wd):
        self._ready = False
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.wd = wd
        self.action = None
        self.holes = []
        self.stats = {}
        self._timer = DispatcherTimer()
        self._timer.Interval = TimeSpan.FromMilliseconds(180)
        self._timer.Tick += self._on_tick
        self.Closed += self._on_closed

        self._mats = [(u"<By Category>", None)]
        try:
            ms = sorted(FilteredElementCollector(wd.doc).OfClass(Material),
                        key=lambda m: m.Name.lower())
            self._mats += [(m.Name, m.Id) for m in ms]
        except Exception:
            pass
        self.fam_mat_cb.ItemsSource = [m[0] for m in self._mats]
        self.fam_mat_cb.SelectedIndex = 0

        self._restore()
        self._new_family_name()
        self._show_wall_info()
        self._ready = True
        self._sync_panels()
        self._recompute()

    # -- settings
    def _restore(self):
        s = deew_settings.load(_TITLE, {})
        for name, _k in _SLIDERS:
            if name in s:
                try:
                    getattr(self, name).Value = float(s[name])
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
        for name in _TEXTS:
            if s.get(name):
                getattr(self, name).Text = s[name]
        if not self.fam_folder_tb.Text:
            self.fam_folder_tb.Text = _default_folder()
        mat = s.get("fam_mat")
        if mat:
            names = [m[0] for m in self._mats]
            if mat in names:
                self.fam_mat_cb.SelectedIndex = names.index(mat)

    def _save(self):
        s = {}
        for name, _k in _SLIDERS:
            s[name] = getattr(self, name).Value
        for name in _CHECKS:
            s[name] = bool(getattr(self, name).IsChecked)
        for name in _COMBOS:
            s[name] = getattr(self, name).SelectedIndex
        for name in _TEXTS:
            s[name] = getattr(self, name).Text
        s["fam_mat"] = self.fam_mat_cb.SelectedItem
        deew_settings.save(_TITLE, s)

    def _new_family_name(self):
        self.fam_name_tb.Text = u"DeeFacade_W{0}_{1}".format(
            self.wd.wall.Id.IntegerValue if hasattr(self.wd.wall.Id, "IntegerValue") else self.wd.wall.Id.Value,
            datetime.datetime.now().strftime("%y%m%d-%H%M%S"))

    def _show_wall_info(self):
        wd = self.wd
        b = FP.bbox(wd.outer)
        shape = u"" if len(wd.outer) == 4 else u"  (edited, non-rectangular profile)"
        extra = []
        if wd.inserts:
            extra.append(u"{0} door/window/opening(s)".format(len(wd.inserts)))
        if wd.inner:
            extra.append(u"{0} opening(s) already in its profile".format(len(wd.inner)))
        self.wall_info_tb.Text = u"Wall: {0}   ·   {1:.0f} x {2:.0f} mm{3}{4}".format(
            wd.wall.Name, (b[2] - b[0]) * _MM, (b[3] - b[1]) * _MM, shape,
            (u"   ·   " + u", ".join(extra)) if extra else u"")

    # -- events
    def option_changed(self, sender, args):
        if getattr(self, "_ready", False):
            self._schedule()

    def pattern_changed(self, sender, args):
        if getattr(self, "_ready", False):
            self._sync_panels()
            self._schedule()

    def output_changed(self, sender, args):
        if getattr(self, "_ready", False):
            self._sync_panels()
            self._schedule()

    def fam_pos_changed(self, sender, args):
        if getattr(self, "_ready", False):
            self._sync_panels()
            self._schedule()

    def shuffle_click(self, sender, args):
        self.seed_tb.Text = str(random.randint(1, 99999))

    def fam_browse_click(self, sender, args):
        dlg = FolderBrowserDialog()
        dlg.Description = "DeeFacade - folder for the facade .rfa files"
        if self.fam_folder_tb.Text and os.path.isdir(self.fam_folder_tb.Text):
            dlg.SelectedPath = self.fam_folder_tb.Text
        if dlg.ShowDialog() == DialogResult.OK:
            self.fam_folder_tb.Text = dlg.SelectedPath

    def repick_click(self, sender, args):
        self.action = "repick"
        self.Close()

    def close_click(self, sender, args):
        self.Close()

    def _on_closed(self, sender, args):
        try:
            self._timer.Stop()
            self._save()
        except Exception:
            pass

    def _schedule(self):
        self._timer.Stop()
        self._timer.Start()

    def _on_tick(self, sender, args):
        self._timer.Stop()
        self._recompute()

    def _sync_panels(self):
        idx = max(0, self.pattern_cb.SelectedIndex)
        for i, panel in enumerate([self.vor_panel, self.hex_panel, self.dia_panel,
                                   self.grid_panel, self.circ_panel]):
            panel.Visibility = Visibility.Visible if i == idx else Visibility.Collapsed
        fam = bool(self.out_family_rb.IsChecked)
        self.profile_panel.Visibility = Visibility.Collapsed if fam else Visibility.Visible
        self.family_panel.Visibility = Visibility.Visible if fam else Visibility.Collapsed
        replace_pos = self.fam_pos_cb.SelectedIndex == 0
        self.fam_thick_sl.IsEnabled = not replace_pos
        self.fam_gap_sl.IsEnabled = not replace_pos
        self.keep_partial_cb.IsEnabled = idx in (1, 2, 3)
        self.apply_b.Content = u"Build family" if fam else u"Cut into wall"

    # -- options
    def _read(self):
        for name, kind in _SLIDERS:
            v = getattr(self, name).Value
            tb = getattr(self, name[:-3] + "_tb")
            if kind == "mm":
                tb.Text = u"{0:.0f} mm".format(v)
            elif kind == "%":
                tb.Text = u"{0:.0f} %".format(v)
            else:
                tb.Text = u"{0:.0f}".format(v)
        if self.fam_pos_cb.SelectedIndex == 0:
            self.fam_thick_tb.Text = u"{0:.0f} mm".format(abs(self.wd.d_ext - self.wd.d_int) * _MM)
            self.fam_gap_tb.Text = u"-"

        def mm(name):
            return getattr(self, name).Value / _MM

        def pct(name):
            return getattr(self, name).Value / 100.0
        try:
            seed = int((self.seed_tb.Text or "1").strip())
        except ValueError:
            seed = sum(ord(c) for c in self.seed_tb.Text)
        direction = _DIRS[max(0, self.dir_cb.SelectedIndex)]
        if self.wd.flip and direction in ("left", "right"):
            direction = "right" if direction == "left" else "left"
        fam = bool(self.out_family_rb.IsChecked)
        o = {
            "pattern": _PATTERNS[max(0, self.pattern_cb.SelectedIndex)],
            "seed": seed,
            "vor_count": int(self.vor_count_sl.Value),
            "vor_relax": int(self.vor_relax_sl.Value),
            "vor_density": pct("vor_density_sl"),
            "hex_size": mm("hex_size_sl"),
            "hex_pointy": bool(self.hex_pointy_rb.IsChecked),
            "dia_w": mm("dia_w_sl"), "dia_h": mm("dia_h_sl"),
            "grid_w": mm("grid_w_sl"), "grid_h": mm("grid_h_sl"),
            "grid_stagger": bool(self.grid_stagger_cb.IsChecked),
            "circ_spacing": mm("circ_spacing_sl"), "circ_dia": pct("circ_dia_sl"),
            "circ_jitter": pct("circ_jitter_sl"), "circ_stagger": bool(self.circ_stagger_cb.IsChecked),
            "mullion": mm("mullion_sl"), "border": mm("border_sl"),
            "round": pct("round_sl"), "fade": pct("fade_sl"), "direction": direction,
            "min_open": mm("min_sl"), "keep_partial": bool(self.keep_partial_cb.IsChecked),
            # output
            "family": fam,
            "replace": bool(self.replace_cb.IsChecked),
            "fam_name": u"".join(c for c in (self.fam_name_tb.Text or u"").strip()
                                 if c not in u'\\/:*?"<>|') or u"DeeFacade",
            "fam_folder": (self.fam_folder_tb.Text or u"").strip(),
            "fam_pos": max(0, self.fam_pos_cb.SelectedIndex),
            "fam_thick": max(mm("fam_thick_sl"), 1.0 / _MM),
            "fam_gap": mm("fam_gap_sl"),
            "fam_wall": max(0, self.fam_wall_cb.SelectedIndex),
            "fam_cut": bool(self.fam_cut_cb.IsChecked),
            "fam_mat": self._mats[max(0, self.fam_mat_cb.SelectedIndex)][1],
        }
        return o

    def _obstacles(self, o):
        obs = []
        if bool(self.avoid_cb.IsChecked) or (o["family"] and o["fam_cut"]):
            obs += list(self.wd.inserts)
        if not o["family"] and not o["replace"]:
            obs += [FP.bbox(p) for p in self.wd.inner]
        return obs

    def _recompute(self):
        o = self._read()
        try:
            self.holes, self.stats = FP.build(o, self.wd.outer, self._obstacles(o), self.wd.tol)
        except Exception as e:
            self.holes, self.stats = [], {}
            self.info_tb.Text = u"Pattern error: {0}".format(e)
            return o
        self._draw(o)
        st = self.stats
        msg = u"{0} openings   ·   {1:.0f}% open".format(st.get("holes", 0), st.get("open_pct", 0.0))
        if st.get("dropped"):
            msg += u"   ·   {0} too small / blocked, left out".format(st["dropped"])
        if st.get("holes", 0) > 700:
            msg += u"\nThat is a lot of openings - Revit will take a while to build them."
        if not st.get("holes"):
            msg += u"\nNothing fits - lower the border, mullion or smallest-opening size."
        self.info_tb.Text = msg
        return o

    def _draw(self, o):
        wd = self.wd
        b = FP.bbox(wd.outer)
        W = max(b[2] - b[0], 1e-6)
        H = max(b[3] - b[1], 1e-6)

        def X(u):
            return (b[2] - u) if wd.flip else (u - b[0])

        def Y(v):
            return b[3] - v

        def ring(pts):
            s = u"M {0:.4f},{1:.4f}".format(X(pts[0][0]), Y(pts[0][1]))
            for p in pts[1:]:
                s += u" L {0:.4f},{1:.4f}".format(X(p[0]), Y(p[1]))
            return s + u" Z"

        parts = [u"F0", ring(wd.outer)]
        if not o["family"] and not o["replace"]:
            parts += [ring(p) for p in wd.inner]
        for h in self.holes:
            parts.append(ring(FP.tessellate(FP.hole_segments(h, wd.tol))))
        if o["family"] and o["fam_cut"]:
            for r in wd.inserts:
                parts.append(ring([(r[0], r[1]), (r[2], r[1]), (r[2], r[3]), (r[0], r[3])]))
        self.canvas_grid.Width = W
        self.canvas_grid.Height = H
        lw = max(W, H) / 700.0
        self.wall_path.StrokeThickness = lw
        self.obst_path.StrokeThickness = lw * 1.5
        try:
            self.wall_path.Data = Geometry.Parse(u" ".join(parts))
        except Exception:
            self.wall_path.Data = None
        obs = [ring([(r[0], r[1]), (r[2], r[1]), (r[2], r[3]), (r[0], r[3])]) for r in wd.inserts]
        self.obst_path.Data = Geometry.Parse(u" ".join(obs)) if obs else None

    # -- apply
    def apply_click(self, sender, args):
        self._timer.Stop()
        o = self._recompute()
        if not self.holes:
            forms.alert(u"The pattern has no openings with these settings.", title=_TITLE)
            return
        self.status_tb.Text = u"Building {0} openings...".format(len(self.holes))
        try:
            if o["family"]:
                res = apply_family(self.wd, self.holes, o)
                if res is None:
                    self.status_tb.Text = u"Cancelled."
                    return
                path, _inst, note = res
                self.status_tb.Text = u"Family placed: {0}   {1}".format(os.path.basename(path), note)
                if o["fam_wall"] == 2:
                    self.apply_b.IsEnabled = False
                    self.status_tb.Text += u"   Pick another wall to continue."
                self._new_family_name()
            else:
                n = apply_profile(self.wd, self.holes, o["replace"])
                self.status_tb.Text = u"Done - {0} openings ({1} lines / arcs) cut into the wall's profile.".format(
                    len(self.holes), n)
                # re-read the wall: it now has a profile sketch
                self.wd = WallData(self.wd.doc, self.wd.wall)
                self._show_wall_info()
                self._recompute()
        except Exception as e:
            self.status_tb.Text = u"FAILED - nothing was changed: {0}".format(e)
            forms.alert(u"DeeFacade could not build the facade:\n\n{0}".format(e), title=_TITLE)


# --------------------------------------------------------------------------
def _preselected_wall(uidoc):
    try:
        ids = list(uidoc.Selection.GetElementIds())
    except Exception:
        return None
    if len(ids) != 1:
        return None
    el = uidoc.Document.GetElement(ids[0])
    return el if isinstance(el, Wall) else None


def main():
    uidoc = __revit__.ActiveUIDocument
    if uidoc is None:
        forms.alert(u"Open a Revit project first.", title=_TITLE)
        return
    doc = uidoc.Document
    if doc.IsFamilyDocument:
        forms.alert(u"DeeFacade works on walls in a project, not in the Family Editor.", title=_TITLE)
        return

    wall = _preselected_wall(uidoc)
    while True:
        if wall is None:
            try:
                ref = uidoc.Selection.PickObject(ObjectType.Element, _WallFilter(),
                                                 "DeeFacade: pick a straight wall (Esc to cancel)")
            except OperationCanceledException:
                return
            wall = doc.GetElement(ref.ElementId)
        try:
            wd = WallData(doc, wall)
        except ValueError as e:
            forms.alert(u"{0}".format(e), title=_TITLE)
            wall = None
            continue
        win = FacadeWindow(_XAML, wd)
        win.ShowDialog()
        if win.action == "repick":
            wall = None
            continue
        break


main()
