# -*- coding: utf-8 -*-
"""
xlsx_reader
Dependency-free (stdlib-only: zipfile + xml.etree.ElementTree) minimal
.xlsx reader, the counterpart to xlsx_writer.py's multi-sheet writer.

Unlike the writer (which always emits inlineStr cells, so it never needs
a shared-strings table), this reader has to tolerate whatever a real
round trip through Microsoft Excel/LibreOffice produces after a user
edits and saves an exported file - that almost always rewrites the
internals to use xl/sharedStrings.xml instead of inline strings, so both
forms (and plain numeric/boolean cells) are handled here.

Usage:
    import xlsx_reader
    sheets = xlsx_reader.read_xlsx_sheets(path)
    # sheets: {sheet_name: [[cell_text, ...], ...]} - a 2D grid per sheet,
    # 0-indexed, gaps filled with "".
"""
import re
import zipfile
from xml.etree import ElementTree as ET

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_R_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_CELL_REF_RE = re.compile(r"([A-Za-z]+)([0-9]+)")


def _col_to_index(col_letters):
    idx = 0
    for ch in col_letters:
        idx = idx * 26 + (ord(ch.upper()) - ord('A') + 1)
    return idx - 1


def _split_cell_ref(ref):
    m = _CELL_REF_RE.match(ref)
    if not m:
        return 0, 0
    return _col_to_index(m.group(1)), int(m.group(2)) - 1


def _text_of(elem):
    if elem is None:
        return ""
    return "".join(t.text or "" for t in elem.iter(_NS + "t"))


def _read_shared_strings(z):
    if "xl/sharedStrings.xml" not in z.namelist():
        return []
    root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    return [_text_of(si) for si in root.findall(_NS + "si")]


def _read_sheet_name_targets(z):
    """Returns [(sheet_name, target_path), ...] in workbook order."""
    wb_root = ET.fromstring(z.read("xl/workbook.xml"))
    rels_root = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    rid_to_target = {rel.get("Id"): rel.get("Target") for rel in rels_root}

    sheets_el = wb_root.find(_NS + "sheets")
    result = []
    if sheets_el is None:
        return result
    for sheet_el in sheets_el.findall(_NS + "sheet"):
        name = sheet_el.get("name")
        rid = sheet_el.get(_R_NS + "id")
        target = rid_to_target.get(rid)
        if not target:
            continue
        if not target.startswith("xl/") and not target.startswith("/"):
            target = "xl/" + target
        target = target.lstrip("/")
        result.append((name, target))
    return result


def _cell_text(c_el, shared):
    ctype = c_el.get("t")
    if ctype == "s":
        v_el = c_el.find(_NS + "v")
        if v_el is not None and v_el.text is not None:
            try:
                idx = int(v_el.text)
            except ValueError:
                return ""
            if 0 <= idx < len(shared):
                return shared[idx]
        return ""
    if ctype == "inlineStr":
        return _text_of(c_el.find(_NS + "is"))
    v_el = c_el.find(_NS + "v")
    return v_el.text if v_el is not None and v_el.text is not None else ""


def _read_sheet_grid(z, target, shared):
    root = ET.fromstring(z.read(target))
    sheet_data = root.find(_NS + "sheetData")
    if sheet_data is None:
        return []
    grid = []
    for row_el in sheet_data.findall(_NS + "row"):
        row_cells = {}
        max_col = -1
        for c_el in row_el.findall(_NS + "c"):
            ref = c_el.get("r")
            col_idx = _split_cell_ref(ref)[0] if ref else (max_col + 1)
            row_cells[col_idx] = _cell_text(c_el, shared)
            if col_idx > max_col:
                max_col = col_idx
        grid.append([row_cells.get(i, "") for i in range(max_col + 1)])
    return grid


def read_xlsx_sheets(path):
    """Returns {sheet_name: [[cell_text, ...], ...]} for every sheet."""
    sheets = {}
    with zipfile.ZipFile(path, "r") as z:
        shared = _read_shared_strings(z)
        for name, target in _read_sheet_name_targets(z):
            try:
                sheets[name] = _read_sheet_grid(z, target, shared)
            except Exception:
                sheets[name] = []
    return sheets
