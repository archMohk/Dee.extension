# -*- coding: utf-8 -*-
"""
dee_draft_import_service
Shared engine for DeeDWG2Draft and DeePDF2Draft: one Drafting View per
DWG file / per PDF page, with the file either IMPORTED (embedded in the
model) or LINKED (kept as an external reference).

--------------------------------------------------------------------
Revit API facts relied on here (checked against RevitAPI.dll 2024 and
2026 by reflection, not guessed)
--------------------------------------------------------------------
- ViewDrafting.Create(doc, viewFamilyTypeId) - the id is the
  ViewFamilyType whose ViewFamily is Drafting.
- DWG: Document.Import(path, DWGImportOptions, view, out ElementId) and
  Document.Link(path, DWGImportOptions, view, out ElementId). Both
  accept a drafting view. DWGImportOptions has Placement, Unit,
  ColorMode, VisibleLayersOnly, ThisViewOnly, CustomScale.
- PDF: there is NO PDFImportOptions class in the 2024 or 2026 API.
  Revit's own "Insert > PDF" is an ImageType whose ImageTypeOptions
  carries PageNumber and Resolution, so a PDF page is placed with
  ImageType.Create(doc, ImageTypeOptions(path, False, Import|Link)) and
  ImageInstance.Create(doc, view, imageTypeId, ImagePlacementOptions).
  ImageTypeSource has Link / Import / Internal.
- Each file is made in its OWN Transaction, so one bad file never rolls
  back the others.
- PDF page count is not exposed by the API: asking for a page beyond the
  end raises, which is how "all pages" knows where to stop.

--------------------------------------------------------------------
Removing the title block (PDF only)
--------------------------------------------------------------------
Revit cannot crop an image, so the title block has to be cut out of the
PDF itself first. crop_pdfs() hands that to lib/dee_pdf_crop_helper.py
run by a normal Python that has PyMuPDF (IronPython has no PDF library).
The cropped copies go to "<source folder>/_NoTitleblock" and are what get
placed. If no such Python is found the tool says so and does not guess.
"""
import glob
import math
import os
import re
import subprocess
import sys
import tempfile
import time

import clr
from Autodesk.Revit.DB import (
    FilteredElementCollector, Transaction, View, ViewFamily, ViewFamilyType,
    ViewDrafting, XYZ, ElementId, BoxPlacement,
    DWGImportOptions, ImportPlacement, ImportUnit, ImportColorMode,
    ImageType, ImageTypeOptions, ImageTypeSource, ImageInstance,
    ImagePlacementOptions, Line, Plane, ElementTransformUtils, TransactionGroup,
    ImageExportOptions, ExportRange, ZoomFitType, FitDirectionType, ImageFileType,
    ImageResolution,
)
from System.Collections.Generic import List

MODE_IMPORT = "import"
MODE_LINK = "link"

UNIT_CHOICES = [
    ("From the DWG file", ImportUnit.Default),
    ("Millimeters", ImportUnit.Millimeter),
    ("Centimeters", ImportUnit.Centimeter),
    ("Meters", ImportUnit.Meter),
    ("Inches", ImportUnit.Inch),
    ("Feet", ImportUnit.Foot),
]
COLOR_CHOICES = [
    ("Preserved", ImportColorMode.Preserved),
    ("Black and white", ImportColorMode.BlackAndWhite),
    ("Inverted", ImportColorMode.Inverted),
]

_BAD_NAME = re.compile(r'[\\{}\[\]|;<>?`~:]')
MAX_PDF_PAGES = 200


# ---------------------------------------------------------------------------
# Rows shown in the tool window
# ---------------------------------------------------------------------------
class FileRow(object):
    """One source file in the grid. `selected` is two-way bound."""

    def __init__(self, path):
        self.path = path
        self.selected = True
        self.file_name = os.path.basename(path)
        self.folder = os.path.dirname(path)
        self.view_name = clean_view_name(os.path.splitext(self.file_name)[0])
        self.status = ""


def clean_view_name(text):
    name = _BAD_NAME.sub("-", text or "").strip()
    return name or "Drafting View"


def list_files(folder, ext, recursive=False):
    ext = ext.lower()
    found = []
    if recursive:
        for dirpath, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if not d.startswith("_NoTitleblock")]
            for f in files:
                if f.lower().endswith(ext):
                    found.append(os.path.join(dirpath, f))
    else:
        for f in os.listdir(folder):
            if f.lower().endswith(ext):
                found.append(os.path.join(folder, f))
    return sorted(found, key=lambda p: os.path.basename(p).lower())


# ---------------------------------------------------------------------------
# Drafting views
# ---------------------------------------------------------------------------
def drafting_view_type_id(doc):
    for vft in FilteredElementCollector(doc).OfClass(ViewFamilyType):
        if vft.ViewFamily == ViewFamily.Drafting:
            return vft.Id
    return None


def existing_view_names(doc):
    names = set()
    for v in FilteredElementCollector(doc).OfClass(View):
        try:
            if not v.IsTemplate:
                names.add(v.Name)
        except Exception:
            pass
    return names


def unique_name(base, taken, when_exists):
    """Name to use, or None to skip. when_exists: 'skip' | 'suffix'."""
    if base not in taken:
        return base
    if when_exists == "skip":
        return None
    n = 2
    while "{0} ({1})".format(base, n) in taken:
        n += 1
    return "{0} ({1})".format(base, n)


def make_drafting_view(doc, name, scale):
    view = ViewDrafting.Create(doc, drafting_view_type_id(doc))
    view.Name = name
    try:
        if scale and scale > 0:
            view.Scale = int(scale)
    except Exception:
        pass
    return view


# ---------------------------------------------------------------------------
# DWG
# ---------------------------------------------------------------------------
def place_dwg(doc, view, path, mode, unit, color_mode, visible_only):
    opts = DWGImportOptions()
    opts.Placement = ImportPlacement.Origin
    opts.Unit = unit
    opts.ColorMode = color_mode
    opts.VisibleLayersOnly = bool(visible_only)
    opts.ThisViewOnly = True
    opts.OrientToView = False
    ref = clr.Reference[ElementId]()
    if mode == MODE_LINK:
        ok = doc.Link(path, opts, view, ref)
    else:
        ok = doc.Import(path, opts, view, ref)
    if not ok:
        raise Exception("Revit did not accept the file.")
    return ref.Value


def turn_element(doc, view, element_id, rot, mirror):
    """Mirrors (left-right) then turns `rot` degrees clockwise about the
    element's centre in the view. Does nothing for 0 / no mirror."""
    if not rot and not mirror:
        return
    doc.Regenerate()
    bb = doc.GetElement(element_id).get_BoundingBox(view)
    if bb is None:
        return
    ctr = XYZ((bb.Min.X + bb.Max.X) / 2.0, (bb.Min.Y + bb.Max.Y) / 2.0, 0.0)
    if mirror:
        ElementTransformUtils.MirrorElement(
            doc, element_id, Plane.CreateByNormalAndOrigin(XYZ.BasisX, ctr))
    if rot:
        axis = Line.CreateBound(ctr, XYZ(ctr.X, ctr.Y, 1.0))
        ElementTransformUtils.RotateElement(doc, element_id, axis, -math.radians(rot))


def preview_dwg(doc, path, unit, color_mode, visible_only, rot, mirror, out_png, px=700):
    """Renders the DWG as it would sit in a drafting view (with the chosen
    turn) by building it in a throw-away view, exporting that view to a png,
    and rolling everything back. Returns (ok, error text)."""
    tmp = tempfile.mkdtemp(prefix="dee_dwgprev_")
    tg = TransactionGroup(doc, "Dee DWG preview")
    tg.Start()
    try:
        t = Transaction(doc, "Dee preview view")
        t.Start()
        view = make_drafting_view(doc, "zz_dee_preview_{0}".format(int(time.time())), 1)
        iid = place_dwg(doc, view, path, MODE_IMPORT, unit, color_mode, visible_only)
        t.Commit()
        t = Transaction(doc, "Dee preview turn")
        t.Start()
        turn_element(doc, view, iid, rot, mirror)
        t.Commit()
        opts = ImageExportOptions()
        opts.ExportRange = ExportRange.SetOfViews
        ids = List[ElementId]()
        ids.Add(view.Id)
        opts.SetViewsAndSheets(ids)
        opts.FilePath = os.path.join(tmp, "dwg")
        opts.HLRandWFViewsFileType = ImageFileType.PNG
        opts.ZoomType = ZoomFitType.FitToPage
        opts.FitDirection = FitDirectionType.Horizontal
        opts.ImageResolution = ImageResolution.DPI_150
        opts.PixelSize = int(px)
        doc.ExportImage(opts)
        found = glob.glob(os.path.join(tmp, "*.png"))
        if not found:
            return False, "Revit produced no image."
        with open(found[0], "rb") as src, open(out_png, "wb") as dst:
            dst.write(src.read())
        return True, ""
    except Exception as ex:
        return False, str(ex)
    finally:
        try:
            tg.RollBack()
        except Exception:
            pass
        try:
            for f in glob.glob(os.path.join(tmp, "*")):
                os.remove(f)
            os.rmdir(tmp)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
def place_pdf_page(doc, view, path, page, mode, dpi):
    source = ImageTypeSource.Link if mode == MODE_LINK else ImageTypeSource.Import
    opts = ImageTypeOptions(path, False, source)
    opts.PageNumber = int(page)
    opts.Resolution = float(dpi)
    image_type = ImageType.Create(doc, opts)
    inst = ImageInstance.Create(
        doc, view, image_type.Id, ImagePlacementOptions(XYZ.Zero, BoxPlacement.Center))
    return inst.Id


# ---------------------------------------------------------------------------
# Title-block removal (PDF)
# ---------------------------------------------------------------------------
_PYTHON_CACHE = {}


def find_pymupdf_python():
    """argv prefix of a Python that can `import pymupdf`, or None."""
    if "py" in _PYTHON_CACHE:
        return _PYTHON_CACHE["py"]
    found = None
    for prefix in (["py", "-3"], ["python"], ["python3"]):
        try:
            rc = subprocess.call(prefix + ["-I", "-c", "import pymupdf"],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 creationflags=0x08000000)  # CREATE_NO_WINDOW
            if rc == 0:
                found = prefix
                break
        except Exception:
            continue
    _PYTHON_CACHE["py"] = found
    return found


def install_pymupdf():
    """pip-installs PyMuPDF for the user (only called after they said yes).
    Returns (ok, message)."""
    last = "no Python found (install Python 3 from python.org)"
    for prefix in (["py", "-3"], ["python"], ["python3"]):
        try:
            p = subprocess.Popen(prefix + ["-m", "pip", "install", "--user", "pymupdf"],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 creationflags=0x08000000)
            out, err = p.communicate()
            if p.returncode == 0:
                _PYTHON_CACHE.pop("py", None)
                return find_pymupdf_python() is not None, "installed"
            last = (err or out or "").strip()
        except Exception as ex:
            last = str(ex)
    return False, last


def crop_pdfs(paths, left, top, right, bottom, out_folder_name="_NoTitleblock"):
    """Writes cropped copies; returns ({source: cropped}, error_text).

    left/top/right/bottom are margins in PDF points (1/72 in) trimmed from
    each page edge. Nothing is returned for a file that failed."""
    py = find_pymupdf_python()
    if py is None:
        return {}, ("No Python with PyMuPDF was found, so the title block cannot be "
                    "cut out. Install it once with:  py -m pip install pymupdf")
    helper = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dee_pdf_crop_helper.py")
    result, errors = {}, []
    for path in paths:
        out_dir = os.path.join(os.path.dirname(path), out_folder_name)
        out_path = os.path.join(out_dir, os.path.basename(path))
        cmd = py + ["-I", helper, path, out_path,
                    str(left), str(top), str(right), str(bottom)]
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 creationflags=0x08000000)
            _, err = p.communicate()
            if p.returncode == 0 and os.path.isfile(out_path):
                result[path] = out_path
            else:
                errors.append("{0}: {1}".format(os.path.basename(path), (err or "").strip()))
        except Exception as ex:
            errors.append("{0}: {1}".format(os.path.basename(path), ex))
    return result, "; ".join(errors)


# ---------------------------------------------------------------------------
# Batch drivers
# ---------------------------------------------------------------------------
class BatchResult(object):
    def __init__(self):
        self.created = []      # (view name, source file)
        self.skipped = []      # (label, reason)
        self.notes = []        # free-text lines shown in the report
        self.seconds = 0.0


def _run_one(doc, label, make, result):
    t = Transaction(doc, "Dee - " + label)
    t.Start()
    try:
        name = make()
        t.Commit()
        result.created.append((name, label))
    except Exception as ex:
        try:
            t.RollBack()
        except Exception:
            pass
        result.skipped.append((label, str(ex)))


def run_dwg_batch(doc, rows, mode, unit, color_mode, visible_only, scale,
                  when_exists, rot=0, mirror=False, progress=None):
    result = BatchResult()
    start = time.time()
    taken = existing_view_names(doc)
    total = len(rows)
    for i, row in enumerate(rows):
        if progress is not None:
            progress.update_progress(i, total)
        name = unique_name(row.view_name, taken, when_exists)
        if name is None:
            result.skipped.append((row.file_name, "a view named '{0}' already exists".format(row.view_name)))
            continue

        def make(name=name, row=row):
            view = make_drafting_view(doc, name, scale)
            iid = place_dwg(doc, view, row.path, mode, unit, color_mode, visible_only)
            turn_element(doc, view, iid, rot, mirror)
            taken.add(name)
            return name
        _run_one(doc, row.file_name, make, result)
    result.seconds = time.time() - start
    return result


def run_pdf_batch(doc, rows, mode, dpi, all_pages, scale, when_exists,
                  crop=None, progress=None):
    """crop: None, or (left, top, right, bottom) points."""
    result = BatchResult()
    start = time.time()
    taken = existing_view_names(doc)

    source_for = {}
    if crop is not None:
        cropped, err = crop_pdfs([r.path for r in rows], *crop)
        source_for = cropped
        if err:
            result.skipped.append(("Title block removal", err))
        # Never place an uncropped sheet when cropping was asked for.
        rows = [r for r in rows if r.path in cropped]

    total = len(rows)
    for i, row in enumerate(rows):
        if progress is not None:
            progress.update_progress(i, total)
        path = source_for.get(row.path, row.path)
        page = 1
        while page <= (MAX_PDF_PAGES if all_pages else 1):
            base = row.view_name if page == 1 else "{0} - p{1}".format(row.view_name, page)
            name = unique_name(base, taken, when_exists)
            if name is None:
                result.skipped.append((row.file_name, "a view named '{0}' already exists".format(base)))
                if not all_pages:
                    break
                page += 1
                continue

            def make(name=name, page=page, path=path):
                view = make_drafting_view(doc, name, scale)
                place_pdf_page(doc, view, path, page, mode, dpi)
                taken.add(name)
                return name
            before = len(result.skipped)
            _run_one(doc, row.file_name if page == 1 else "{0} (p{1})".format(row.file_name, page), make, result)
            if len(result.skipped) > before:
                # Past the last page is the normal way "all pages" ends.
                if page > 1:
                    result.skipped.pop()
                break
            page += 1
    result.seconds = time.time() - start
    return result


def print_report(output, title, result):
    html = [
        '<h2 style="font-family:sans-serif;color:#ddd;">{0}</h2>'.format(title),
        '<p style="color:#ddd;">Created {0} drafting view(s) - skipped {1} - {2:.1f}s.</p>'.format(
            len(result.created), len(result.skipped), result.seconds),
    ]
    for note in getattr(result, "notes", []):
        html.append('<div style="color:#bbb;font-family:monospace;font-size:12px;">{0}</div>'.format(note))
    for label, reason in result.skipped:
        html.append(
            '<div style="padding:4px 10px;margin:2px 0;background:#c62828;color:#fff;'
            'border-radius:4px;font-family:monospace;font-size:12px;">'
            '&#10007;&nbsp; <b>{0}</b> &mdash; {1}</div>'.format(label, reason))
    output.print_html("".join(html))
