# -*- coding: utf-8 -*-
"""
DeeLazy - Model Wipe
Strip Revit models back to what you need - typically architects' files
that must lose their sheets, views and model elements before you start
modelling the facade.

Flow
    1. Add models: .rvt files / a folder, ACC files, or the model open in
       this Revit - as many as you like.
    2. Choose what happens to the original: DETACH and save a COPY (the
       original file / ACC model is never touched), or wipe the ORIGINAL
       itself (saved / synchronized in place - asks for a typed
       confirmation).
    3. Scan one model: its wipeable content is listed with counts - sheets,
       views by type, view templates, model categories, annotation
       categories, Revit / CAD links, images, an optional purge. The ticks
       are RULES: they apply to every ticked model, whatever it contains.
    4. Wipe. Each item is deleted ONE BY ONE in its own transaction - one
       that fails never stops the others. Run it in this Revit, or in one
       or more BACKGROUND Revits (lib/dee_background) so this session
       stays free.

Presets: Facade Prep (keep only the building envelope), Sheets & Views,
Everything, None. Levels and grids are never ticked by "Everything".
"""
import os
import re
import time
import datetime
import traceback

import clr
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
import System
from System import GC
from System.Collections.Generic import List, HashSet

from pyrevit import forms
from Autodesk.Revit.DB import (
    FilteredElementCollector, ElementId, ElementCategoryFilter, BuiltInCategory,
    Transaction, SubTransaction, TransactionStatus, CategoryType,
    View, ViewSheet, ViewType, RevitLinkType, CADLinkType, ImageType,
)

import dee_branding
import dee_background as bg
import acc_auth
import acc_file_browser as afb
import deew_document_manager as docmgr
import deew_failure_handler as failures
from dee_ui import SafeProgress


_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "DeeModelWipe.xaml")
_CACHE_FILE = os.path.join(_THIS_DIR, ".deemodelwipe_acc_cache.json")
_BACKUP_RE = re.compile(r"\.\d{4}\.rvt$", re.IGNORECASE)
_SYNC_COMMENT = "DeeLazy - Model Wipe"
_LOG_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "DeeLazy", "model_wipe_logs")

# ---- group names (also the on-screen order) ----
G_SHEETS = "1  Sheets"
G_VIEWS = "2  Views"
G_MODEL = "3  Model elements"
G_ANNO = "4  Annotation / view-specific"
G_LINKS = "5  Links & imports"
G_DATA = "6  Project data"

# ---- what each row does ----
K_SHEETS, K_VIEWTYPE, K_TEMPLATES, K_CAT = "sheets", "viewtype", "templates", "cat"
K_RVTLINKS, K_CADLINKS, K_IMAGES, K_PURGE = "rvtlinks", "cadlinks", "images", "purge"

# view type -> label shown in the list (one row per label)
_VIEW_LABELS = [
    ("Floor plans", ["FloorPlan"]),
    ("Ceiling plans", ["CeilingPlan"]),
    ("Structural / area plans", ["EngineeringPlan", "AreaPlan"]),
    ("Sections & details", ["Section", "Detail"]),
    ("Elevations", ["Elevation"]),
    ("3D views, walkthroughs, renderings", ["ThreeD", "Walkthrough", "Rendering"]),
    ("Drafting views", ["DraftingView"]),
    ("Legends", ["Legend"]),
    ("Schedules & reports", ["Schedule", "PanelSchedule", "ColumnSchedule", "CostReport",
                             "LoadsReport", "PresureLossReport"]),
]

# categories that must never be offered (own rows, sheet furniture, cannot be deleted)
_SKIP_CATEGORIES = [
    "OST_Views", "OST_Sheets", "OST_TitleBlocks", "OST_Viewports", "OST_ScheduleGraphics",
    "OST_RvtLinks", "OST_ProjectBasePoint", "OST_SharedBasePoint", "OST_Cameras",
    "OST_ProjectInformation", "OST_SunStudy", "OST_Constraints", "OST_Revisions",
    "OST_RevisionClouds_Obsolete",
]

# Facade Prep: model categories that survive (envelope). Names are BuiltInCategory names;
# anything starting with a prefix in _KEEP_PREFIXES is kept too.
_FACADE_KEEP = set([
    "OST_Walls", "OST_Windows", "OST_Doors", "OST_Roofs", "OST_Floors",
    "OST_Levels", "OST_Grids", "OST_Fascia", "OST_Gutter", "OST_RoofSoffit",
    "OST_StackedWalls", "OST_Reveals",
])
_FACADE_KEEP_PREFIXES = ("OST_Curtain",)
# never ticked by the "everything" style presets - they anchor the model
_ANCHORS = set(["OST_Levels", "OST_Grids"])

ST_PENDING = ""
CHUNK = 400


# ==========================================================================
# helpers
# ==========================================================================
def _eid(element_id):
    try:
        return int(element_id.Value)
    except Exception:
        pass
    try:
        return int(element_id.IntegerValue)
    except Exception:
        return 0


def _norm(path):
    return os.path.normcase(os.path.normpath(path or ""))


def _release():
    try:
        GC.Collect()
        GC.WaitForPendingFinalizers()
        GC.Collect()
    except Exception:
        pass


def _bic_name(cat_id_int):
    try:
        return str(System.Enum.ToObject(BuiltInCategory, cat_id_int))
    except Exception:
        return str(cat_id_int)


def _skip_ids():
    ids = set()
    for name in _SKIP_CATEGORIES:
        try:
            ids.add(int(getattr(BuiltInCategory, name)))
        except Exception:
            continue
    return ids


def _view_type_name(view):
    try:
        return str(view.ViewType).split(".")[-1]
    except Exception:
        return ""


class WipeRow(object):
    """One line in the list. `selected` / `group` / `name` / `count` / `status`
    are bound to the grid."""
    def __init__(self, kind, group, name, count, key=None, bic="", selected=False):
        self.kind = kind
        self.group = group
        self.name = name
        self.count = count
        self.key = key          # K_VIEWTYPE: list of ViewType names; K_CAT: (ElementId, view_specific)
        self.bic = bic          # BuiltInCategory name for K_CAT rows
        self.selected = selected
        self.status = ST_PENDING
        self.rule_key = u""      # stable across models, see rules_from_rows


# ==========================================================================
# scanning (read-only)
# ==========================================================================
def scan_document(doc, progress=None):
    """Returns a list of WipeRow for everything wipeable in `doc`."""
    rows = []

    # ---- sheets / views / templates ----
    sheets = 0
    templates = 0
    by_label = {}
    label_of = {}
    for label, types in _VIEW_LABELS:
        for t in types:
            label_of[t] = label
    for v in FilteredElementCollector(doc).OfClass(View):
        try:
            if v.IsTemplate:
                templates += 1
                continue
            if isinstance(v, ViewSheet):
                sheets += 1
                continue
            label = label_of.get(_view_type_name(v))
            if not label:
                continue
            try:
                if v.IsTitleblockRevisionSchedule:
                    continue
            except Exception:
                pass
            by_label[label] = by_label.get(label, 0) + 1
        except Exception:
            continue
    if sheets:
        rows.append(WipeRow(K_SHEETS, G_SHEETS, "All sheets (their viewports go with them)", sheets, selected=True))
    for label, types in _VIEW_LABELS:
        if by_label.get(label):
            rows.append(WipeRow(K_VIEWTYPE, G_VIEWS, label, by_label[label], key=types, selected=True))
    if templates:
        rows.append(WipeRow(K_TEMPLATES, G_VIEWS, "View templates", templates))

    # ---- categories (model vs view-specific) ----
    skip = _skip_ids()
    stats = {}   # (cat int id, view_specific) -> [name, count, ElementId]
    n = 0
    for e in FilteredElementCollector(doc).WhereElementIsNotElementType():
        n += 1
        if progress is not None and n % 2000 == 0:
            try:
                progress.update_progress(0, 0)
            except Exception:
                pass
        try:
            cat = e.Category
            if cat is None:
                continue
            if cat.CategoryType not in (CategoryType.Model, CategoryType.Annotation):
                continue
            cid = _eid(cat.Id)
            if cid in skip:
                continue
            vs = bool(e.ViewSpecific)
            key = (cid, vs)
            rec = stats.get(key)
            if rec is None:
                stats[key] = [cat.Name, 1, cat.Id]
            else:
                rec[1] += 1
        except Exception:
            continue
    for (cid, vs), (name, count, cat_id) in sorted(stats.items(), key=lambda kv: (kv[0][1], kv[1][0].lower())):
        bic = _bic_name(cid)
        row = WipeRow(K_CAT, G_ANNO if vs else G_MODEL, name, count, key=(cat_id, vs), bic=bic, selected=False)
        row.rule_key = u"{0}|{1}".format(bic if bic.startswith("OST_") else u"name:" + name, 1 if vs else 0)
        rows.append(row)

    # ---- links & imports ----
    rvt = len(list(FilteredElementCollector(doc).OfClass(RevitLinkType)))
    cad = len(list(FilteredElementCollector(doc).OfClass(CADLinkType)))
    img = len(list(FilteredElementCollector(doc).OfClass(ImageType)))
    if rvt:
        rows.append(WipeRow(K_RVTLINKS, G_LINKS, "Revit links (type + instances)", rvt, selected=True))
    if cad:
        rows.append(WipeRow(K_CADLINKS, G_LINKS, "CAD links & imports (DWG, DXF, DGN...)", cad, selected=True))
    if img:
        rows.append(WipeRow(K_IMAGES, G_LINKS, "Raster images", img))

    # ---- project data ----
    if hasattr(doc, "GetUnusedElements"):
        rows.append(WipeRow(K_PURGE, G_DATA, "Purge unused types, materials, families (up to 5 passes)", "-"))
    return rows


# ==========================================================================
# deleting
# ==========================================================================
def _collect_elements(doc, row):
    """Fresh elements for a row (re-read at run time - earlier rows may
    already have deleted some of them with their views / hosts)."""
    out = []
    if row.kind == K_SHEETS:
        for v in FilteredElementCollector(doc).OfClass(ViewSheet):
            out.append(v)
    elif row.kind == K_VIEWTYPE:
        wanted = set(row.key)
        for v in FilteredElementCollector(doc).OfClass(View):
            try:
                if v.IsTemplate or isinstance(v, ViewSheet):
                    continue
                if _view_type_name(v) in wanted:
                    try:
                        if v.IsTitleblockRevisionSchedule:
                            continue
                    except Exception:
                        pass
                    out.append(v)
            except Exception:
                continue
    elif row.kind == K_TEMPLATES:
        for v in FilteredElementCollector(doc).OfClass(View):
            try:
                if v.IsTemplate:
                    out.append(v)
            except Exception:
                continue
    elif row.kind == K_CAT:
        cat_id, vs = row.key
        for e in FilteredElementCollector(doc).WherePasses(ElementCategoryFilter(cat_id)).WhereElementIsNotElementType():
            try:
                if bool(e.ViewSpecific) == vs:
                    out.append(e)
            except Exception:
                continue
    elif row.kind == K_RVTLINKS:
        out = list(FilteredElementCollector(doc).OfClass(RevitLinkType))
    elif row.kind == K_CADLINKS:
        out = list(FilteredElementCollector(doc).OfClass(CADLinkType))
    elif row.kind == K_IMAGES:
        out = list(FilteredElementCollector(doc).OfClass(ImageType))
    return out


def _run_in_transaction(doc, label, work):
    """Runs work() in a transaction with failure suppression; returns
    (ok, result_or_error)."""
    t = Transaction(doc, "DeeLazy Model Wipe - " + label)
    try:
        t.Start()
        try:
            failures.apply_to_transaction(t)
        except Exception:
            pass
        result = work()
        if t.Commit() != TransactionStatus.Committed:
            return False, "rolled back by Revit"
        return True, result
    except Exception as e:
        try:
            if t.GetStatus() == TransactionStatus.Started:
                t.RollBack()
        except Exception:
            pass
        return False, str(e)


def _delete_elements(doc, elements, label, protect):
    """Deletes `elements`; returns (deleted_count, failed_count, note).
    Bulk delete in chunks first; a chunk Revit refuses is retried element
    by element so one stubborn element cannot save the rest."""
    deleted = 0
    failed = 0
    notes = []
    protect = protect or set()
    todo = []
    for e in elements:
        try:
            if _eid(e.Id) in protect:
                failed += 1
                notes.append("kept the active view")
                continue
            todo.append(e)
        except Exception:
            continue

    # unpin pinned elements (a pinned element refuses deletion)
    def _unpin():
        for e in todo:
            try:
                if e.Pinned:
                    e.Pinned = False
            except Exception:
                continue
    _run_in_transaction(doc, label + " (unpin)", _unpin)

    for start in range(0, len(todo), CHUNK):
        chunk = todo[start:start + CHUNK]
        ids = List[ElementId]()
        for e in chunk:
            try:
                ids.Add(e.Id)
            except Exception:
                continue
        # skip ids already gone (deleted as a dependent of an earlier chunk)
        live = List[ElementId]()
        for i in ids:
            if doc.GetElement(i) is not None:
                live.Add(i)
        if live.Count == 0:
            continue

        ok, res = _run_in_transaction(doc, label, lambda: doc.Delete(live))
        if ok:
            deleted += res.Count if res is not None else live.Count
            continue

        # retry one by one
        def _one_by_one():
            done = [0, 0]
            for i in live:
                if doc.GetElement(i) is None:
                    continue
                st = SubTransaction(doc)
                try:
                    st.Start()
                    r = doc.Delete(i)
                    st.Commit()
                    done[0] += r.Count if r is not None else 1
                except Exception as ex:
                    done[1] += 1
                    if len(notes) < 3:
                        notes.append(str(ex).split("\n")[0][:90])
                    try:
                        st.RollBack()
                    except Exception:
                        pass
            return done
        ok2, res2 = _run_in_transaction(doc, label + " (one by one)", _one_by_one)
        if ok2:
            deleted += res2[0]
            failed += res2[1]
        else:
            failed += live.Count
            if len(notes) < 3:
                notes.append(str(res2).split("\n")[0][:90])
    return deleted, failed, "; ".join(sorted(set(notes)))


def _purge_unused(doc, log):
    total = 0
    for p in range(5):
        try:
            unused = doc.GetUnusedElements(HashSet[ElementId]())
        except Exception as e:
            log("  purge not available: {0}".format(e))
            break
        if unused is None or unused.Count == 0:
            break
        ids = List[ElementId](unused)
        before = total
        ok, res = _run_in_transaction(doc, "purge pass {0}".format(p + 1), lambda: doc.Delete(ids))
        if ok:
            total += res.Count if res is not None else ids.Count
        else:
            # retry one by one so a single protected type does not block the pass
            def _each():
                n = 0
                for i in ids:
                    if doc.GetElement(i) is None:
                        continue
                    st = SubTransaction(doc)
                    try:
                        st.Start()
                        r = doc.Delete(i)
                        st.Commit()
                        n += r.Count if r is not None else 1
                    except Exception:
                        try:
                            st.RollBack()
                        except Exception:
                            pass
                return n
            ok2, n = _run_in_transaction(doc, "purge pass {0} (one by one)".format(p + 1), _each)
            if ok2:
                total += n
        log("  purge pass {0}: {1} element(s)".format(p + 1, total - before))
        if total == before:
            break
    return total


def wipe_row(doc, row, protect, log):
    """Executes one row. Returns a short status string."""
    if row.kind == K_PURGE:
        n = _purge_unused(doc, log)
        return "Purged {0}".format(n)
    elements = _collect_elements(doc, row)
    if not elements:
        return "Nothing left"
    deleted, failed, note = _delete_elements(doc, elements, row.name, protect)
    if note:
        log(u"  ! {0}: {1}".format(row.name, note))
    if failed and not deleted:
        return "Failed ({0})".format(failed)
    if failed:
        return "Deleted {0}, {1} failed".format(deleted, failed)
    return "Deleted {0}".format(deleted)


# ==========================================================================
# rules: what to wipe, independent of any one model
# ==========================================================================
def rules_from_rows(rows, others, keep_envelope):
    """The ticks of the scanned model as a JSON-safe dict that can be
    applied to ANY model (also in a background Revit)."""
    rules = {"sheets": False, "templates": False, "rvtlinks": False, "cadlinks": False,
             "images": False, "purge": False, "viewtypes": [], "cats_wipe": [], "cats_keep": [],
             "others": bool(others), "keep_envelope": bool(keep_envelope)}
    for r in rows:
        if r.kind == K_VIEWTYPE:
            if r.selected:
                rules["viewtypes"].append(r.name)
        elif r.kind == K_CAT:
            rules["cats_wipe" if r.selected else "cats_keep"].append(r.rule_key)
        elif r.kind in rules:
            rules[r.kind] = bool(r.selected)
    return rules


def apply_rules(rows, rules):
    """Sets .selected on a model's own freshly scanned rows from `rules`."""
    wipe = set(rules.get("cats_wipe") or [])
    keep = set(rules.get("cats_keep") or [])
    views = set(rules.get("viewtypes") or [])
    for r in rows:
        if r.kind == K_VIEWTYPE:
            r.selected = r.name in views
        elif r.kind == K_CAT:
            if r.rule_key in wipe:
                r.selected = True
            elif r.rule_key in keep:
                r.selected = False
            else:
                r.selected = bool(rules.get("others"))
                if r.bic in _ANCHORS:
                    r.selected = False
                if rules.get("keep_envelope") and r.group == G_MODEL and \
                        (r.bic in _FACADE_KEEP or r.bic.startswith(_FACADE_KEEP_PREFIXES)):
                    r.selected = False
        else:
            r.selected = bool(rules.get(r.kind))


def wipe_document(doc, rules, protect, log):
    """Scans `doc`, applies the rules and wipes. Returns [[label, status], ...]."""
    rows = scan_document(doc)
    apply_rules(rows, rules)
    picked = sorted([r for r in rows if r.selected], key=lambda r: r.group)
    out = []
    log("  wiping {0} item(s) ...".format(len(picked)))
    for row in picked:
        try:
            status = wipe_row(doc, row, protect, log)
        except Exception as e:
            status = "Failed"
            log(u"  ✗ {0}: {1}".format(row.name, e))
        log(u"  {0} / {1} -> {2}".format(row.group[3:], row.name, status))
        out.append([u"{0} / {1}".format(row.group[3:], row.name), status])
    return out


# ==========================================================================
# one model (runs in this Revit OR in a background Revit)
# ==========================================================================
def _save_in_place(doc):
    """Sync a workshared (attached) model; SaveCloudModel for a
    non-workshared cloud model; plain Save otherwise."""
    if docmgr.is_workshared(doc) and not docmgr.is_detached(doc):
        return docmgr.synchronize_with_central(doc, comment=_SYNC_COMMENT)
    if docmgr.is_cloud_model(doc) and not docmgr.is_detached(doc):
        try:
            doc.SaveCloudModel()
            return True, "Saved to cloud"
        except Exception as e:
            return False, str(e)
    return docmgr.save_standalone(doc)


def process_item(ctx, item):
    """item = {"key","name","source": "local"|"acc","path" | "project_guid"/"model_guid"/"region",
              "output": "copy"|"overwrite", "target": copy path, "compact": bool}
    ctx.options["rules"] = the wipe rules. Returns a result dict."""
    res = {"ok": False, "detail": "", "saved": False, "save_detail": "", "items": [], "target": ""}
    copy_mode = item.get("output") == "copy"
    if item.get("source") == "acc":
        doc, detail = bg.open_cloud_model(ctx, item["region"], item["project_guid"], item["model_guid"],
                                          detach=copy_mode)
    else:
        doc, detail = bg.open_local_model(ctx, item["path"], detach=copy_mode)
    if doc is None:
        res["detail"] = detail
        ctx.log(u"  ✗ could not open - " + detail)
        return res
    res["ok"] = True
    res["detail"] = "opened " + detail
    ctx.log("  opened " + detail)
    try:
        res["items"] = wipe_document(doc, ctx.options["rules"], set(), ctx.log)
        if copy_mode:
            if docmgr.is_workshared(doc) and not docmgr.is_detached(doc):
                # SaveAs here would repoint the shared model - never do that
                ok, sdetail = False, "could not detach from central - copy NOT saved, original untouched"
            else:
                ok, sdetail = docmgr.save_copy_as(doc, item["target"], compact=bool(item.get("compact")),
                                                  overwrite=False)
                if ok:
                    res["target"] = item["target"]
        else:
            ok, sdetail = _save_in_place(doc)
        res["saved"], res["save_detail"] = bool(ok), sdetail
        ctx.log(u"  ✓ " + sdetail if ok else u"  ✗ save failed: " + sdetail)
    finally:
        bg.close_model(ctx, doc)
    return res


def _summary(res):
    if res is None:
        return "Not processed"
    if not res.get("ok"):
        return "Open failed"
    done = len([i for i in res.get("items", []) if i[1].startswith("Deleted") or i[1].startswith("Purged")])
    if not res.get("saved"):
        return "Wiped {0} item(s) - NOT saved".format(done)
    if res.get("target"):
        return u"Wiped {0} item(s) - copy saved".format(done)
    return u"Wiped {0} item(s) - saved".format(done)


# ==========================================================================
# window
# ==========================================================================
SRC_LOCAL, SRC_ACC, SRC_OPEN = "local", "acc", "open"
_SRC_LABEL = {SRC_LOCAL: "Local", SRC_ACC: "ACC", SRC_OPEN: "Open model"}
RUN_BACKGROUND, RUN_SESSION = 0, 1
ST_QUEUED = "Queued (background)"


class ModelRow(object):
    """One model in the list."""
    def __init__(self, name, key, source, folder="", acc=None):
        self.name = name
        self.key = key              # local full path / ACC item id / "ACTIVE:<title>"
        self.source = source
        self.source_label = _SRC_LABEL[source]
        self.folder = folder
        self.acc = acc              # ACC source dict (project_id, region, ...)
        self.selected = True
        self.status = ""


def _strip_rvt(name):
    name = (name or "").strip()
    return name[:-4] if name.lower().endswith(".rvt") else name


class ModelWipeWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, uiapp):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.uiapp = uiapp
        self.application = uiapp.Application
        self._models = []
        self._rows = []             # wipe list of the scanned model
        self._facade = False        # Facade Prep clicked last (its envelope is spared when "others" is on)
        self._acc_guids = {}        # item_id -> (project Guid, model Guid)
        self._log_lines = []
        self._job = None
        self._job_ctx = None
        self._ready = True
        self.Closing += self._on_closing
        self._mode_changed_ui()
        self._log("Ready. Add models, scan one of them, tick what to wipe.")

    # ---------------- shared ----------------
    def _log(self, message):
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        line = u"{0}  {1}".format(stamp, message)
        self._log_lines.append(line)
        try:
            self.log_tb.AppendText(line + u"\n")
            self.log_tb.ScrollToEnd()
        except Exception:
            pass

    def _write_log_file(self):
        try:
            if not os.path.isdir(_LOG_DIR):
                os.makedirs(_LOG_DIR)
            path = os.path.join(_LOG_DIR, datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + ".txt")
            with open(path, "wb") as fh:
                fh.write(u"\r\n".join(self._log_lines).encode("utf-8"))
            self._log("Log saved: {0}".format(path))
        except Exception as e:
            self._log("(could not write the log file: {0})".format(e))

    def _with_dialog_handler(self, fn):
        dialog_log = []
        handler = afb.make_dialog_handler(dialog_log)
        self.uiapp.DialogBoxShowing += handler
        try:
            return fn()
        finally:
            try:
                self.uiapp.DialogBoxShowing -= handler
            except Exception:
                pass
            for text, _ok in dialog_log:
                self._log("  (auto-answered) " + text)

    def _commit(self):
        for g in (self.models_grid, self.items_grid):
            try:
                g.CommitEdit()
            except Exception:
                pass

    def _make_ctx(self):
        ctx = bg.Context(self.uiapp, self.application, self._log, {}, _LOG_DIR)
        ctx.activated = None
        ctx.pending_close = None
        return ctx

    def _open_doc_info(self):
        titles, paths = set(), set()
        try:
            for d in self.application.Documents:
                try:
                    titles.add(_strip_rvt(d.Title).lower())
                    if d.PathName:
                        paths.add(os.path.normcase(os.path.normpath(d.PathName)))
                except Exception:
                    continue
        except Exception:
            pass
        return titles, paths

    def _is_open_here(self, row, titles, paths):
        if row.source == SRC_LOCAL:
            return os.path.normcase(os.path.normpath(row.key)) in paths
        if row.source == SRC_ACC:
            return _strip_rvt(row.name).lower() in titles
        return False

    # ---------------- window state ----------------
    def _on_closing(self, sender, args):
        if self._job is not None:
            if not forms.alert("A background job is still running. Closing this window does not stop the "
                               "background Revit(s), but the results will not be shown here.\n\nLog folders under:\n{0}"
                               "\n\nClose anyway?".format(self._job.root), yes=True, no=True):
                args.Cancel = True
                return
            self._job_stop_timer()

    def close_click(self, sender, args):
        self.Close()

    def _copy_mode(self):
        return bool(self.mode_copy_rb.IsChecked)

    def _run_mode(self):
        try:
            return RUN_SESSION if self.run_cb.SelectedIndex == 1 else RUN_BACKGROUND
        except Exception:
            return RUN_BACKGROUND

    def _mode_changed_ui(self):
        if not getattr(self, "_ready", False):
            return
        self.copy_panel.IsEnabled = self._copy_mode()
        self.copy_panel.Opacity = 1.0 if self._copy_mode() else 0.4

    def mode_changed(self, sender, args):
        self._mode_changed_ui()

    def run_changed(self, sender, args):
        if getattr(self, "_ready", False):
            self.par_cb.IsEnabled = self._run_mode() == RUN_BACKGROUND

    def out_browse_click(self, sender, args):
        folder = forms.pick_folder()
        if folder:
            self.out_tb.Text = folder

    # ---------------- model list ----------------
    def _refresh_models(self):
        self.models_grid.ItemsSource = None
        self.models_grid.ItemsSource = self._models
        self._update_buttons()

    def _update_buttons(self):
        self.wipe_b.IsEnabled = bool(self._models) and bool(self._rows) and self._job is None

    def _known_keys(self):
        return set((m.source, os.path.normcase(m.key)) for m in self._models)

    def _add_local(self, paths):
        known = self._known_keys()
        added = 0
        for p in paths:
            if not p or not p.lower().endswith(".rvt") or _BACKUP_RE.search(p):
                continue
            p = os.path.normpath(p)
            if (SRC_LOCAL, os.path.normcase(p)) in known:
                continue
            known.add((SRC_LOCAL, os.path.normcase(p)))
            self._models.append(ModelRow(os.path.basename(p), p, SRC_LOCAL, os.path.dirname(p)))
            added += 1
        self._log("Added {0} file(s).".format(added))
        self._refresh_models()

    def add_files_click(self, sender, args):
        picked = forms.pick_file(file_ext="rvt", multi_file=True)
        if picked:
            self._add_local(picked if isinstance(picked, list) else [picked])

    def add_folder_click(self, sender, args):
        folder = forms.pick_folder()
        if not folder:
            return
        sub = forms.CommandSwitchWindow.show(["This Folder Only", "Include Subfolders"],
                                             message="Scan '{0}':".format(folder))
        if not sub:
            return
        found = []
        if sub == "Include Subfolders":
            for root, dirs, files in os.walk(folder):
                dirs[:] = [d for d in dirs if not d.lower().endswith("_backup")]
                found += [os.path.join(root, f) for f in files]
        else:
            found = [os.path.join(folder, f) for f in os.listdir(folder)]
        self._add_local(found)

    def add_open_click(self, sender, args):
        uidoc = self.uiapp.ActiveUIDocument
        if uidoc is None or uidoc.Document.IsFamilyDocument:
            forms.alert("Open a project model first.")
            return
        doc = uidoc.Document
        key = "ACTIVE:" + doc.Title
        if (SRC_OPEN, os.path.normcase(key)) in self._known_keys():
            return
        self._models.append(ModelRow(doc.Title, key, SRC_OPEN, doc.PathName or "(not saved)"))
        self._log("Added the open model '{0}' - it is wiped in place and not saved.".format(doc.Title))
        self._refresh_models()

    def add_acc_click(self, sender, args):
        try:
            token = acc_auth.get_access_token()
        except Exception as e:
            forms.alert("Could not sign in to Autodesk:\n{0}".format(e))
            return
        if not token:
            return
        hub = afb.pick_hub(token)
        if not hub:
            return
        hub_id, region, hub_name = hub
        project = afb.pick_project(hub_id, token)
        if not project:
            return
        project_id, project_name = project
        how = forms.CommandSwitchWindow.show(["Whole project", "One folder"],
                                             message="Scan which part of '{0}'?".format(project_name))
        if not how:
            return
        where = project_name
        if how == "Whole project":
            items = afb.list_project_files(hub_id, project_id, token, _CACHE_FILE)
        else:
            folder = afb.pick_folder(hub_id, project_id, token)
            if not folder:
                return
            folder_id, folder_path = folder
            sub = forms.CommandSwitchWindow.show(["This Folder Only", "Include Subfolders"],
                                                 message="Scan '{0}':".format(folder_path))
            if not sub:
                return
            items = afb._scan_one_folder(project_id, folder_id, token, sub == "Include Subfolders", _CACHE_FILE)
            where = "{0} / {1}".format(project_name, folder_path)
        if not items:
            self._log("Scan found no Revit files.")
            return
        names = sorted(items.keys())
        picked = forms.SelectFromList.show(names, title="ACC files in {0}".format(where), multiselect=True,
                                           button_name="Add to Model Wipe")
        if not picked:
            return
        source = {"token": token, "region": region, "project_id": project_id, "project_name": project_name}
        known = self._known_keys()
        added = 0
        for name in picked:
            item_id = items[name]
            if (SRC_ACC, os.path.normcase(item_id)) in known:
                continue
            self._models.append(ModelRow(name, item_id, SRC_ACC, u"{0} / {1}".format(hub_name, where), acc=source))
            added += 1
        self._log("Added {0} ACC file(s) from {1}.".format(added, where))
        self._refresh_models()

    def remove_models_click(self, sender, args):
        row = self.models_grid.SelectedItem
        if row is not None and row in self._models:
            self._models.remove(row)
            self._refresh_models()

    def clear_models_click(self, sender, args):
        self._models = []
        self._refresh_models()

    def models_all_click(self, sender, args):
        for m in self._models:
            m.selected = True
        self.models_grid.Items.Refresh()

    def models_none_click(self, sender, args):
        for m in self._models:
            m.selected = False
        self.models_grid.Items.Refresh()

    # ---------------- ACC ids ----------------
    def _fresh_token(self, row):
        try:
            token = acc_auth.get_access_token()
        except Exception as e:
            self._log("Autodesk sign-in failed: {0}".format(e))
            return None
        if token and row.acc is not None:
            row.acc["token"] = token
        return token

    def _acc_ids(self, row):
        """((project Guid, model Guid), detail). A guessed answer
        ("lineage") is never used - see dee_remove_links._acc_cloud_path."""
        ids = self._acc_guids.get(row.key)
        if ids is not None:
            return ids, "ok"
        label = ""
        for attempt in range(3):
            token = self._fresh_token(row)
            if not token:
                return None, "not signed in to Autodesk"
            try:
                pg, mg, label = afb.get_cloud_path_guids(row.acc["project_id"], row.key, token)
            except Exception as e:
                label = "error: {0}".format(e)
            if label and not label.startswith("lineage") and not label.startswith("error"):
                ids = (pg, mg)
                self._acc_guids[row.key] = ids
                return ids, "ok"
            time.sleep(2 * (attempt + 1))
        return None, ("ACC did not return this model's cloud IDs ({0}) - the Autodesk sign-in may have "
                      "expired or ACC is busy. Sign in again and retry.".format(label or "no answer"))

    # ---------------- scan ----------------
    def scan_click(self, sender, args):
        self._commit()
        row = self.models_grid.SelectedItem
        if row is None:
            ticked = [m for m in self._models if m.selected]
            row = ticked[0] if ticked else (self._models[0] if self._models else None)
        if row is None:
            forms.alert("Add a model first, then scan it.")
            return
        titles, paths = self._open_doc_info()
        if self._is_open_here(row, titles, paths):
            forms.alert("'{0}' is open in this Revit.\n\nUse 'Add Open Model' for it, or close it first.".format(row.name))
            return
        self._rows = []
        self.items_grid.ItemsSource = None
        self._facade = False
        self._with_dialog_handler(lambda: self._scan_row(row))
        self._refresh_items()

    def _scan_row(self, row):
        self._log("Scanning '{0}' ...".format(row.name))
        ctx = self._make_ctx()
        doc, own = None, True
        try:
            if row.source == SRC_OPEN:
                uidoc = self.uiapp.ActiveUIDocument
                if uidoc is None or ("ACTIVE:" + uidoc.Document.Title) != row.key:
                    self._log(u"  ✗ the open model changed - remove it and add it again")
                    return
                doc, own = uidoc.Document, False
            elif row.source == SRC_LOCAL:
                doc, detail = bg.open_local_model(ctx, row.key, detach=True)
            else:
                ids, why = self._acc_ids(row)
                if ids is None:
                    self._log(u"  ✗ " + why)
                    return
                doc, detail = bg.open_cloud_model(ctx, row.acc["region"], str(ids[0]), str(ids[1]), detach=True)
            if doc is None:
                self._log(u"  ✗ could not open - " + detail)
                forms.alert("Could not open the model:\n\n" + detail)
                return
            with SafeProgress(window=self, title="Scanning the model...", cancellable=False) as pb:
                try:
                    self._rows = scan_document(doc, pb)
                except Exception:
                    self._log(u"  ✗ scan failed:\n" + traceback.format_exc())
                    self._rows = []
        finally:
            if doc is not None and own:
                bg.close_model(ctx, doc)
                bg._close_pending(ctx)
                _release()

    def _refresh_items(self):
        self._refresh_grid()
        if self._rows:
            self.info_tb.Text = "{0} item(s) found. Tick what to wipe - or use a preset. The ticks apply to every ticked model.".format(len(self._rows))
            self._log("Found {0} wipeable item(s).".format(len(self._rows)))
        else:
            self.info_tb.Text = "Nothing to wipe was found."
        self._update_buttons()

    # ---------------- wipe list / ticks ----------------
    def _shown(self):
        text = (self.search_tb.Text or "").strip().lower()
        if not text:
            return list(self._rows)
        return [r for r in self._rows if all(w in (r.group + " " + r.name).lower() for w in text.split())]

    def _refresh_grid(self):
        self.items_grid.ItemsSource = None
        self.items_grid.ItemsSource = self._shown()

    def search_changed(self, sender, args):
        if getattr(self, "_ready", False):
            self._commit()
            self._refresh_grid()

    def _set_shown(self, value):
        self._commit()
        for r in self._shown():
            r.selected = value
        self.items_grid.Items.Refresh()

    def tick_shown_click(self, sender, args):
        self._set_shown(True)

    def untick_shown_click(self, sender, args):
        self._set_shown(False)

    def _apply_preset(self, picker, facade=False, others=None):
        self._commit()
        for r in self._rows:
            r.selected = bool(picker(r))
        self._facade = facade
        if others is not None:
            self.others_cb.IsChecked = others
        self.items_grid.Items.Refresh()

    @staticmethod
    def _keeps_envelope(row):
        return row.bic in _FACADE_KEEP or row.bic.startswith(_FACADE_KEEP_PREFIXES)

    def preset_facade_click(self, sender, args):
        def pick(r):
            if r.kind == K_CAT:
                return r.group == G_ANNO or not self._keeps_envelope(r)
            return r.kind != K_PURGE and r.kind != K_IMAGES
        self._apply_preset(pick, facade=True, others=True)
        self._log("Preset Facade Prep: envelope categories (walls, curtain wall, windows, doors, roofs, floors, levels, grids) are kept.")

    def preset_views_click(self, sender, args):
        self._apply_preset(lambda r: r.kind in (K_SHEETS, K_VIEWTYPE), others=False)

    def preset_all_click(self, sender, args):
        self._apply_preset(lambda r: r.kind != K_PURGE and r.bic not in _ANCHORS, others=True)

    def preset_none_click(self, sender, args):
        self._apply_preset(lambda r: False, others=False)

    # ---------------- wipe ----------------
    def _rules(self):
        return rules_from_rows(self._rows, bool(self.others_cb.IsChecked), self._facade)

    def _target_for(self, row, used):
        """Unique copy path for a model, or (None, why)."""
        folder = (self.out_tb.Text or "").strip().strip('"')
        suffix = self.suffix_tb.Text or ""
        if not folder:
            if row.source != SRC_LOCAL:
                return None, "set the 'Save copies in' folder - ACC models cannot be saved beside the original"
            folder = row.folder
        base = _strip_rvt(row.name)
        n = 1
        while True:
            name = u"{0}{1}{2}.rvt".format(base, suffix, "" if n == 1 else " ({0})".format(n))
            cand = os.path.join(folder, name)
            key = os.path.normcase(os.path.normpath(cand))
            same_as_source = row.source == SRC_LOCAL and key == os.path.normcase(os.path.normpath(row.key))
            if not os.path.exists(cand) and key not in used and not same_as_source:
                used.add(key)
                return cand, ""
            n += 1

    def wipe_click(self, sender, args):
        self._commit()
        picked = [m for m in self._models if m.selected]
        if not picked:
            forms.alert("Tick at least one model.")
            return
        rules = self._rules()
        n_items = len([r for r in self._rows if r.selected])
        if not n_items and not rules["others"]:
            forms.alert("Tick at least one item to wipe.")
            return
        copy_mode = self._copy_mode()
        if not copy_mode:
            real = [m for m in picked if m.source != SRC_OPEN]
            if real:
                answer = forms.ask_for_string(
                    default="", title="Model Wipe - wipe the ORIGINALS",
                    prompt="{0} original model(s) will be wiped and SAVED over - local files are overwritten, "
                           "ACC models are synchronized (their version history stays on ACC).\n\n"
                           "Type WIPE to continue:".format(len(real)))
                if (answer or "").strip().upper() != "WIPE":
                    self._log("Cancelled - nothing was changed.")
                    return
        else:
            used = set()
            for m in picked:
                if m.source == SRC_OPEN:
                    continue
                target, why = self._target_for(m, used)
                if target is None:
                    forms.alert(u"{0}: {1}".format(m.name, why))
                    return
        names = u"\n".join(u"  - {0}  ({1})".format(m.name, m.source_label) for m in picked[:12])
        if len(picked) > 12:
            names += u"\n  ... and {0} more".format(len(picked) - 12)
        where = ("Each model is detached and saved as a NEW copy - the originals stay untouched."
                 if copy_mode else "The ORIGINALS are changed and saved. This cannot be undone.")
        if any(m.source == SRC_OPEN for m in picked):
            where += "\nThe open model is changed in place and NOT saved."
        if not forms.alert("Wipe {0} model(s) with {1} ticked item(s)?\n\n{2}\n\n{3}".format(
                len(picked), n_items, names, where), title="Model Wipe", yes=True, no=True):
            return
        for m in self._models:
            m.status = ""
        self._log_lines = self._log_lines[-200:]
        self._start_run(picked, rules, copy_mode)

    def _items_for(self, models, rules, copy_mode):
        """Builds the worker items for local / ACC models (resolving ACC ids).
        Returns (items, rows_by_key). Models that cannot be queued get a status."""
        titles, paths = self._open_doc_info()
        used = set()
        items, by_key = [], {}
        with SafeProgress(window=self, title="Preparing models...", cancellable=True) as pb:
            for i, m in enumerate(models):
                pb.update_progress(i, len(models))
                if pb.cancelled:
                    self._log("Cancelled.")
                    return [], {}
                if self._is_open_here(m, titles, paths):
                    m.status = "Skipped - open in Revit"
                    self._log(u"  ✗ {0}: skipped - open in this Revit; use 'Add Open Model' for it".format(m.name))
                    continue
                item = {"key": m.key, "name": m.name, "source": m.source,
                        "output": "copy" if copy_mode else "overwrite",
                        "compact": bool(self.compact_cb.IsChecked)}
                if m.source == SRC_ACC:
                    ids, why = self._acc_ids(m)
                    if ids is None:
                        m.status = "Open failed"
                        self._log(u"  ✗ {0}: {1}".format(m.name, why))
                        continue
                    item.update({"project_guid": str(ids[0]), "model_guid": str(ids[1]), "region": m.acc["region"]})
                else:
                    item["path"] = m.key
                if copy_mode:
                    target, why = self._target_for(m, used)
                    if target is None:
                        m.status = "No target"
                        self._log(u"  ✗ {0}: {1}".format(m.name, why))
                        continue
                    item["target"] = target
                items.append(item)
                by_key[m.key] = m
            pb.update_progress(len(models), len(models))
        return items, by_key

    def _start_run(self, picked, rules, copy_mode):
        started = time.time()
        # 1) the open model is always wiped here
        for m in [m for m in picked if m.source == SRC_OPEN]:
            self._with_dialog_handler(lambda m=m: self._wipe_open(m, rules))
        # 2) everything else
        rest = [m for m in picked if m.source != SRC_OPEN]
        if rest:
            items, by_key = self._items_for(rest, rules, copy_mode)
            if items:
                if self._run_mode() == RUN_BACKGROUND:
                    if self._start_background(items, by_key, rules):
                        self.models_grid.Items.Refresh()
                        return
                else:
                    self._with_dialog_handler(lambda: self._run_session(items, by_key, rules))
        self.models_grid.Items.Refresh()
        self._log("Finished in {0:.0f}s.".format(time.time() - started))
        self._write_log_file()

    def _wipe_open(self, m, rules):
        uidoc = self.uiapp.ActiveUIDocument
        if uidoc is None or ("ACTIVE:" + uidoc.Document.Title) != m.key:
            m.status = "Not active any more"
            self._log(u"  ✗ {0}: it is no longer the open model".format(m.name))
            return
        self._log("{0} (open model):".format(m.name))
        protect = set()
        try:
            protect.add(_eid(uidoc.ActiveView.Id))
        except Exception:
            pass
        try:
            items = wipe_document(uidoc.Document, rules, protect, self._log)
            m.status = _summary({"ok": True, "items": items, "saved": False}).replace("NOT saved", "not saved")
            self._log("  the open model is changed but NOT saved.")
        except Exception as e:
            m.status = "Failed"
            self._log(u"  ✗ " + str(e))

    def _run_session(self, items, by_key, rules):
        ctx = self._make_ctx()
        ctx.options = {"rules": rules}
        with SafeProgress(window=self, title="Wiping models...", cancellable=True) as pb:
            for i, item in enumerate(items):
                if pb.cancelled:
                    self._log("Cancelled - the models not reached are untouched.")
                    break
                pb.update_progress(i, len(items))
                pb.title = "{0} ({1}/{2})".format(item["name"], i + 1, len(items))
                self._log(u"{0} ({1}/{2}):".format(item["name"], i + 1, len(items)))
                try:
                    res = process_item(ctx, item)
                except Exception as e:
                    res = {"ok": False, "detail": str(e)}
                    self._log(u"  ✗ " + str(e))
                by_key[item["key"]].status = _summary(res)
                bg._close_pending(ctx)
                _release()
                self.models_grid.Items.Refresh()
            pb.update_progress(len(items), len(items))

    # ---------------- background ----------------
    def _start_background(self, items, by_key, rules):
        for it in items:
            by_key[it["key"]].status = ST_QUEUED
        try:
            job = bg.BackgroundJob.start(
                "model_wipe", "dee_model_wipe_worker", "process_model", items,
                {"rules": rules}, str(self.application.VersionNumber),
                instances=bg.instances_from_combo(self.par_cb, 1),
                extra_paths=[_THIS_DIR], log=self._log)
        except Exception as e:
            for it in items:
                by_key[it["key"]].status = ""
            forms.alert("{0}\n\nSwitch 'Run in' to this Revit session instead.".format(e))
            return False
        self._job = job
        self._job_ctx = (by_key, None)
        self._log("{0} background Revit(s) starting (this takes a minute each). {1} model(s) in total."
                  .format(len(job.workers), len(items)))
        self._set_job_ui(True)
        timer = bg.start_timer(job, lambda tag, line: self._log(u"[{0}] {1}".format(tag, line)),
                               self._on_job_finished)
        self._job_ctx = (by_key, timer)
        return True

    def _set_job_ui(self, running):
        for b in (self.wipe_b, self.scan_b):
            b.IsEnabled = not running
        self.job_stop_b.Visibility = System.Windows.Visibility.Visible if running else System.Windows.Visibility.Collapsed

    def _job_stop_timer(self):
        job, ctx = self._job, self._job_ctx
        self._job = self._job_ctx = None
        if ctx and ctx[1] is not None:
            try:
                ctx[1].Stop()
            except Exception:
                pass
        self._set_job_ui(False)
        self._update_buttons()
        return job, ctx

    def job_stop_click(self, sender, args):
        if self._job is not None:
            self._job.stop()
            self._log("Stop requested - each background Revit finishes its current model, then skips the rest.")

    def _on_job_finished(self, job, how):
        _job, ctx = self._job_stop_timer()
        if ctx is None:
            return
        by_key = ctx[0]
        results = job.results()
        for key, m in by_key.items():
            if m.status == ST_QUEUED:
                m.status = _summary(results.get(key))
        self.models_grid.Items.Refresh()
        if how == "exited":
            self._log("A background Revit ended without reporting - see the logs under {0}".format(job.root))
        elif how == "failed":
            self._log("A background job crashed - see the log above and the logs under {0}".format(job.root))
        done = len([m for m in by_key.values() if m.status.startswith("Wiped")])
        self._log("Background job finished: {0} of {1} model(s) wiped.".format(done, len(by_key)))
        self._write_log_file()


def launch(uiapp):
    try:
        window = ModelWipeWindow(_XAML_FILE, uiapp)
        window.ShowDialog()
    except Exception:
        forms.alert("Model Wipe failed to start:\n\n" + traceback.format_exc())


TOOL_INFO = {
    "id": "model_wipe",
    "title": "Model Wipe",
    "description": "Strip many Revit models (files, ACC or the open one) back to what you need - wipe sheets, views, model categories, annotations, links and unused data one item at a time. Keep the original (detached copy) or wipe it in place; run in background Revits. Facade Prep keeps only the envelope.",
    "icon": u"\U0001F9F9",  # broom
    "category": "Files, Links & ACC",
    "launch": launch,
}
