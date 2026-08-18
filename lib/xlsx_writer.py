# -*- coding: utf-8 -*-
"""
xlsx_writer
Dependency-free (stdlib-only: zipfile) minimal .xlsx writer, themed to
match NAGA's LOD Drawings Index register (Arial, light-gray D9D9D9
header band, thin borders, bold centered title row) plus status-tinted
data rows (green/red/gray) matching this codebase's HTML-report color
language. No openpyxl or COM/Excel automation required, since neither
is reliably available from pyRevit's IronPython engine - this writes
the OOXML parts directly.

Usage:
    import xlsx_writer
    xlsx_writer.write_themed_xlsx(
        path, title, headers, col_widths,
        rows=[(["101", "Cover Sheet", "Created"], "ok"), ...])

`rows` is a list of (values, status) where status is one of
None / "ok" / "fail" / "skip", controlling that row's fill color.
"""
import re
import zipfile

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

_WORKBOOK = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""

_WORKBOOK_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="3">
<font><sz val="11"/><name val="Arial"/></font>
<font><b/><sz val="18"/><name val="Arial"/></font>
<font><b/><sz val="11"/><name val="Arial"/></font>
</fonts>
<fills count="6">
<fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFD9D9D9"/><bgColor indexed="64"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFC6E0B4"/><bgColor indexed="64"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFF4C7C3"/><bgColor indexed="64"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFE7E6E6"/><bgColor indexed="64"/></patternFill></fill>
</fills>
<borders count="2">
<border><left/><right/><top/><bottom/><diagonal/></border>
<border><left style="thin"><color indexed="64"/></left><right style="thin"><color indexed="64"/></right><top style="thin"><color indexed="64"/></top><bottom style="thin"><color indexed="64"/></bottom><diagonal/></border>
</borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="7">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
<xf numFmtId="0" fontId="2" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment horizontal="left" vertical="center"/></xf>
<xf numFmtId="0" fontId="0" fillId="3" borderId="1" xfId="0" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="left" vertical="center"/></xf>
<xf numFmtId="0" fontId="0" fillId="4" borderId="1" xfId="0" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="left" vertical="center"/></xf>
<xf numFmtId="0" fontId="0" fillId="5" borderId="1" xfId="0" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="left" vertical="center"/></xf>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""

_STATUS_STYLE = {None: 3, "ok": 4, "fail": 5, "skip": 6}


def _col_letter(n):
    result = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result = chr(ord('A') + rem) + result
    return result


def _escape(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def write_themed_xlsx(path, title, headers, col_widths, rows):
    """path: output file path.
    title: text for the merged, bold title row.
    headers: list of column header strings.
    col_widths: list of Excel column-width numbers, same length as headers.
    rows: list of (values, status) - values is a list matching headers'
          length, status is None/"ok"/"fail"/"skip" controlling row fill."""
    ncols = len(headers)
    last_col = _col_letter(ncols)

    cols_xml = "".join(
        '<col min="{0}" max="{0}" width="{1}" customWidth="1"/>'.format(i + 1, w)
        for i, w in enumerate(col_widths))

    title_cells = []
    for i in range(ncols):
        col = _col_letter(i + 1)
        if i == 0:
            title_cells.append(
                '<c r="{0}1" s="1" t="inlineStr"><is><t xml:space="preserve">{1}</t></is></c>'.format(
                    col, _escape(title)))
        else:
            title_cells.append('<c r="{0}1" s="1"/>'.format(col))
    rows_xml = ['<row r="1" ht="28" customHeight="1">' + "".join(title_cells) + "</row>"]

    header_cells = []
    for i, h in enumerate(headers):
        col = _col_letter(i + 1)
        header_cells.append(
            '<c r="{0}2" s="2" t="inlineStr"><is><t xml:space="preserve">{1}</t></is></c>'.format(
                col, _escape(h)))
    rows_xml.append('<row r="2">' + "".join(header_cells) + "</row>")

    r = 3
    for values, status in rows:
        style = _STATUS_STYLE.get(status, 3)
        cells = []
        for i, v in enumerate(values):
            col = _col_letter(i + 1)
            cells.append(
                '<c r="{0}{1}" s="{2}" t="inlineStr"><is><t xml:space="preserve">{3}</t></is></c>'.format(
                    col, r, style, _escape(str(v))))
        rows_xml.append('<row r="{0}">'.format(r) + "".join(cells) + "</row>")
        r += 1

    merge_xml = '<mergeCells count="1"><mergeCell ref="A1:{0}1"/></mergeCells>'.format(last_col)

    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<cols>{0}</cols><sheetData>{1}</sheetData>{2}</worksheet>'
    ).format(cols_xml, "".join(rows_xml), merge_xml)

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _ROOT_RELS)
        z.writestr("xl/workbook.xml", _WORKBOOK)
        z.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS)
        z.writestr("xl/styles.xml", _STYLES)
        z.writestr("xl/worksheets/sheet1.xml", sheet_xml)


# ---------------------------------------------------------------------------
# Multi-sheet writer (DeeScheduleXL): one sheet per exported Schedule, a
# hidden row 1 carrying an import-compatibility fingerprint, a hidden
# ElementId column, and REAL Excel cell locking (sheetProtection) so
# calculated/non-settable fields can't be accidentally hand-edited -
# matching the reference tool's "locked cell" behavior rather than just a
# visual cue.
# ---------------------------------------------------------------------------
_MS_STYLES_TEMPLATE = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2">
<font><sz val="11"/><name val="Arial"/></font>
<font><b/><sz val="11"/><name val="Arial"/></font>
</fonts>
<fills count="3">
<fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFD9D9D9"/><bgColor indexed="64"/></patternFill></fill>
</fills>
<borders count="2">
<border><left/><right/><top/><bottom/><diagonal/></border>
<border><left style="thin"><color indexed="64"/></left><right style="thin"><color indexed="64"/></right><top style="thin"><color indexed="64"/></top><bottom style="thin"><color indexed="64"/></bottom><diagonal/></border>
</borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="4">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1"/>
<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1"/>
<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyProtection="1"><protection locked="0"/></xf>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""

_MS_STYLE_HEADER = 1
_MS_STYLE_LOCKED = 2
_MS_STYLE_UNLOCKED = 3


def _ms_sheet_xml(sheet):
    headers = sheet["headers"]
    rows = sheet["rows"]
    hidden_cols = sheet.get("hidden_cols", set())
    locked_cols = sheet.get("locked_cols", set())
    ncols = len(headers)

    cols_xml_parts = []
    for i, w in enumerate(sheet.get("col_widths") or [12] * ncols):
        attrs = 'min="{0}" max="{0}" width="{1}" customWidth="1"'.format(i + 1, w)
        if i in hidden_cols:
            attrs += ' hidden="1"'
        cols_xml_parts.append("<col {0}/>".format(attrs))
    cols_xml = "".join(cols_xml_parts)

    rows_xml = ['<row r="1" hidden="1"><c r="A1" t="inlineStr"><is><t xml:space="preserve">{0}</t></is></c></row>'
                .format(_escape(sheet.get("fingerprint", "")))]

    header_cells = []
    for i, h in enumerate(headers):
        col = _col_letter(i + 1)
        header_cells.append(
            '<c r="{0}2" s="{1}" t="inlineStr"><is><t xml:space="preserve">{2}</t></is></c>'.format(
                col, _MS_STYLE_HEADER, _escape(h)))
    rows_xml.append('<row r="2">' + "".join(header_cells) + "</row>")

    r = 3
    for values in rows:
        cells = []
        for i, v in enumerate(values):
            col = _col_letter(i + 1)
            style = _MS_STYLE_LOCKED if i in locked_cols else _MS_STYLE_UNLOCKED
            cells.append(
                '<c r="{0}{1}" s="{2}" t="inlineStr"><is><t xml:space="preserve">{3}</t></is></c>'.format(
                    col, r, style, _escape(str(v))))
        rows_xml.append('<row r="{0}">'.format(r) + "".join(cells) + "</row>")
        r += 1

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<cols>{0}</cols><sheetData>{1}</sheetData>'
        '<sheetProtection sheet="1" objects="1" scenarios="1"/>'
        '</worksheet>'
    ).format(cols_xml, "".join(rows_xml))


def _safe_sheet_name(name, used_names):
    # Excel sheet names: max 31 chars, no : \ / ? * [ ]
    cleaned = re.sub(r'[:\\/?*\[\]]', "_", name)[:31] or "Sheet"
    candidate = cleaned
    n = 1
    while candidate in used_names:
        suffix = " ({0})".format(n)
        candidate = cleaned[:31 - len(suffix)] + suffix
        n += 1
    used_names.add(candidate)
    return candidate


def write_multisheet_xlsx(path, sheets):
    """sheets: list of dicts, each:
        name          - sheet tab name (sanitized/de-duplicated automatically)
        fingerprint   - text stashed in a hidden row 1 (import compatibility check)
        headers       - list of column header strings
        col_widths    - list of Excel column-width numbers (optional)
        hidden_cols   - set of 0-based column indices to hide (e.g. ElementId)
        locked_cols   - set of 0-based column indices that are read-only in Excel
        rows          - list of value-lists, each same length as headers
    """
    used_names = set()
    sheet_ids = []
    content_overrides = []
    workbook_rels = []
    workbook_sheets = []
    files = {}

    for i, sheet in enumerate(sheets):
        idx = i + 1
        safe_name = _safe_sheet_name(sheet["name"], used_names)
        sheet_ids.append(idx)
        files["xl/worksheets/sheet{0}.xml".format(idx)] = _ms_sheet_xml(sheet)
        content_overrides.append(
            '<Override PartName="/xl/worksheets/sheet{0}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            .format(idx))
        workbook_rels.append(
            '<Relationship Id="rId{0}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="worksheets/sheet{0}.xml"/>'.format(idx))
        workbook_sheets.append(
            '<sheet name="{0}" sheetId="{1}" r:id="rId{1}"/>'.format(_escape(safe_name), idx))

    styles_rid = len(sheets) + 1
    workbook_rels.append(
        '<Relationship Id="rId{0}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'.format(styles_rid))

    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        + "".join(content_overrides) + "</Types>")

    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets>' + "".join(workbook_sheets) + '</sheets></workbook>')

    workbook_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(workbook_rels) + "</Relationships>")

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", _ROOT_RELS)
        z.writestr("xl/workbook.xml", workbook_xml)
        z.writestr("xl/_rels/workbook.xml.rels", workbook_rels_xml)
        z.writestr("xl/styles.xml", _MS_STYLES_TEMPLATE)
        for part_name, xml in files.items():
            z.writestr(part_name, xml)


# ---------------------------------------------------------------------------
# Health report writer (DeeHealth): a 2-sheet workbook - "Summary" (overall
# score + one row per section) and "Detailed" (every test, color-coded by
# status) - same Arial/border/status-tint visual language as
# write_themed_xlsx, extended to a 4-tier status (green/amber/red/gray)
# since DeeHealth scores are graded, not just pass/fail.
# ---------------------------------------------------------------------------
_HR_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="4">
<font><sz val="11"/><name val="Arial"/></font>
<font><b/><sz val="18"/><name val="Arial"/></font>
<font><b/><sz val="11"/><name val="Arial"/></font>
<font><b/><sz val="28"/><name val="Arial"/></font>
</fonts>
<fills count="7">
<fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFD9D9D9"/><bgColor indexed="64"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFC6E0B4"/><bgColor indexed="64"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFFFE699"/><bgColor indexed="64"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFF4C7C3"/><bgColor indexed="64"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFE7E6E6"/><bgColor indexed="64"/></patternFill></fill>
</fills>
<borders count="2">
<border><left/><right/><top/><bottom/><diagonal/></border>
<border><left style="thin"><color indexed="64"/></left><right style="thin"><color indexed="64"/></right><top style="thin"><color indexed="64"/></top><bottom style="thin"><color indexed="64"/></bottom><diagonal/></border>
</borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="9">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1" applyAlignment="1"><alignment horizontal="left" vertical="center"/></xf>
<xf numFmtId="0" fontId="2" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment horizontal="left" vertical="center" wrapText="1"/></xf>
<xf numFmtId="0" fontId="0" fillId="3" borderId="1" xfId="0" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="left" vertical="center" wrapText="1"/></xf>
<xf numFmtId="0" fontId="0" fillId="4" borderId="1" xfId="0" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="left" vertical="center" wrapText="1"/></xf>
<xf numFmtId="0" fontId="0" fillId="5" borderId="1" xfId="0" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="left" vertical="center" wrapText="1"/></xf>
<xf numFmtId="0" fontId="0" fillId="6" borderId="1" xfId="0" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="left" vertical="center" wrapText="1"/></xf>
<xf numFmtId="0" fontId="3" fillId="0" borderId="0" xfId="0" applyFont="1" applyAlignment="1"><alignment horizontal="left" vertical="center"/></xf>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""

_HR_STYLE_TITLE = 1
_HR_STYLE_HEADER = 2
_HR_STATUS_STYLE = {None: 3, "ok": 4, "warn": 5, "fail": 6, "skip": 7}
_HR_STYLE_BIG_SCORE = 8


def _hr_status_for_score(score):
    if score is None:
        return "skip"
    if score >= 0.8:
        return "ok"
    if score >= 0.4:
        return "warn"
    return "fail"


def _hr_row_xml(row_num, cells_with_styles):
    cells = []
    for i, (value, style) in enumerate(cells_with_styles):
        col = _col_letter(i + 1)
        cells.append(
            '<c r="{0}{1}" s="{2}" t="inlineStr"><is><t xml:space="preserve">{3}</t></is></c>'.format(
                col, row_num, style, _escape(str(value))))
    return '<row r="{0}">'.format(row_num) + "".join(cells) + "</row>"


def _hr_sheet_xml(ncols, col_widths, rows_xml, merges=None):
    last_col = _col_letter(ncols)
    cols_xml = "".join(
        '<col min="{0}" max="{0}" width="{1}" customWidth="1"/>'.format(i + 1, w)
        for i, w in enumerate(col_widths))
    merge_xml = ""
    if merges:
        merge_xml = '<mergeCells count="{0}">{1}</mergeCells>'.format(
            len(merges), "".join('<mergeCell ref="{0}"/>'.format(m) for m in merges))
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<cols>{0}</cols><sheetData>{1}</sheetData>{2}</worksheet>'
    ).format(cols_xml, "".join(rows_xml), merge_xml)


def write_health_report_xlsx(path, overall_score, section_rows, detail_rows):
    """path: output file path.
    overall_score: 0.0-1.0 float, or None if nothing was scorable.
    section_rows: list of (section_name, weight_text, weighted_score_pct_text, status)
                  - status is None/"ok"/"warn"/"fail"/"skip".
    detail_rows: list of (values, status) - values is
                 [Section, Test, Implemented, Weight, Value, Score, E, F, G, Description],
                 status is None/"ok"/"warn"/"fail"/"skip".
    """
    # -- Summary sheet --
    summary_rows = [
        '<row r="1" ht="26" customHeight="1">' +
        '<c r="A1" s="{0}" t="inlineStr"><is><t xml:space="preserve">{1}</t></is></c></row>'.format(
            _HR_STYLE_TITLE, _escape("DeeHealth - Model Health Report")),
    ]
    score_text = "N/A" if overall_score is None else "{0:.0f}%".format(overall_score * 100)
    score_status = _hr_status_for_score(overall_score) if overall_score is not None else "skip"
    summary_rows.append(
        '<row r="3">'
        '<c r="A3" s="{0}" t="inlineStr"><is><t xml:space="preserve">Overall Score</t></is></c>'
        '<c r="B3" s="{1}" t="inlineStr"><is><t xml:space="preserve">{2}</t></is></c></row>'.format(
            _HR_STYLE_TITLE, _HR_STYLE_BIG_SCORE, score_text))

    header_cells = [("Section", _HR_STYLE_HEADER), ("Weight", _HR_STYLE_HEADER),
                    ("Weighted Score", _HR_STYLE_HEADER), ("Status", _HR_STYLE_HEADER)]
    summary_rows.append(_hr_row_xml(5, header_cells))
    r = 6
    for name, weight_text, score_pct_text, status in section_rows:
        style = _HR_STATUS_STYLE.get(status, 3)
        status_label = {"ok": "Good", "warn": "Warning", "fail": "Fail", "skip": "N/A", None: "N/A"}.get(status, "N/A")
        summary_rows.append(_hr_row_xml(r, [
            (name, style), (weight_text, style), (score_pct_text, style), (status_label, style)]))
        r += 1
    summary_xml = _hr_sheet_xml(4, [40, 14, 16, 12], summary_rows, merges=["A1:D1"])

    # -- Detailed sheet --
    detail_headers = ["Section", "Test", "Implemented", "Weight", "Value", "Score",
                       "E (Good)", "F (Warn)", "G (Fail)", "Description"]
    detail_xml_rows = [
        '<row r="1" ht="24" customHeight="1">' +
        '<c r="A1" s="{0}" t="inlineStr"><is><t xml:space="preserve">{1}</t></is></c></row>'.format(
            _HR_STYLE_TITLE, _escape("Detailed Test Results")),
        _hr_row_xml(2, [(h, _HR_STYLE_HEADER) for h in detail_headers]),
    ]
    r = 3
    for values, status in detail_rows:
        style = _HR_STATUS_STYLE.get(status, 3)
        detail_xml_rows.append(_hr_row_xml(r, [(v, style) for v in values]))
        r += 1
    detail_col_widths = [22, 26, 12, 10, 12, 10, 10, 10, 10, 60]
    detail_xml = _hr_sheet_xml(len(detail_headers), detail_col_widths, detail_xml_rows,
                                merges=["A1:{0}1".format(_col_letter(len(detail_headers)))])

    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '</Types>')
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets>'
        '<sheet name="Summary" sheetId="1" r:id="rId1"/>'
        '<sheet name="Detailed" sheetId="2" r:id="rId2"/>'
        '</sheets></workbook>')
    workbook_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>'
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        '</Relationships>')

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", _ROOT_RELS)
        z.writestr("xl/workbook.xml", workbook_xml)
        z.writestr("xl/_rels/workbook.xml.rels", workbook_rels_xml)
        z.writestr("xl/styles.xml", _HR_STYLES)
        z.writestr("xl/worksheets/sheet1.xml", summary_xml)
        z.writestr("xl/worksheets/sheet2.xml", detail_xml)


# ---------------------------------------------------------------------------
# BOQ writer (DeeQs) - a title block (Project/Project No/Client/Consultant/
# Date), a Section-grouped Item No/Description/Unit/Quantity/Rate/Amount
# table with a bold-filled group-header row per Section, a top-bordered
# Subtotal row per Section, and a double-top-bordered Grand Total row.
# None of the 3 writers above have a currency number format or grouped-
# header/subtotal styling - this is a new style palette (_BOQ_STYLES),
# built the same raw-OOXML way as every other writer in this module.
# ---------------------------------------------------------------------------
_BOQ_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<numFmts count="1">
<numFmt numFmtId="164" formatCode="#,##0.00"/>
</numFmts>
<fonts count="3">
<font><sz val="11"/><name val="Arial"/></font>
<font><b/><sz val="16"/><name val="Arial"/></font>
<font><b/><sz val="11"/><name val="Arial"/></font>
</fonts>
<fills count="5">
<fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFD9D9D9"/><bgColor indexed="64"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFDDEBF7"/><bgColor indexed="64"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFBFBFBF"/><bgColor indexed="64"/></patternFill></fill>
</fills>
<borders count="4">
<border><left/><right/><top/><bottom/><diagonal/></border>
<border><left style="thin"><color indexed="64"/></left><right style="thin"><color indexed="64"/></right><top style="thin"><color indexed="64"/></top><bottom style="thin"><color indexed="64"/></bottom><diagonal/></border>
<border><left/><right/><top style="thin"><color indexed="64"/></top><bottom/><diagonal/></border>
<border><left/><right/><top style="double"><color indexed="64"/></top><bottom style="double"><color indexed="64"/></bottom><diagonal/></border>
</borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="12">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1" applyAlignment="1"><alignment horizontal="left" vertical="center"/></xf>
<xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1" applyAlignment="1"><alignment horizontal="left" vertical="center"/></xf>
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment horizontal="left" vertical="center"/></xf>
<xf numFmtId="0" fontId="2" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
<xf numFmtId="0" fontId="2" fillId="3" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="left" vertical="center"/></xf>
<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment horizontal="left" vertical="center"/></xf>
<xf numFmtId="164" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1" applyAlignment="1"><alignment horizontal="right" vertical="center"/></xf>
<xf numFmtId="0" fontId="2" fillId="0" borderId="2" xfId="0" applyFont="1" applyBorder="1" applyAlignment="1"><alignment horizontal="right" vertical="center"/></xf>
<xf numFmtId="164" fontId="2" fillId="0" borderId="2" xfId="0" applyNumberFormat="1" applyFont="1" applyBorder="1" applyAlignment="1"><alignment horizontal="right" vertical="center"/></xf>
<xf numFmtId="0" fontId="2" fillId="4" borderId="3" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="right" vertical="center"/></xf>
<xf numFmtId="164" fontId="2" fillId="4" borderId="3" xfId="0" applyNumberFormat="1" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="right" vertical="center"/></xf>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""

_BOQ_STYLE_TITLE = 1
_BOQ_STYLE_LABEL = 2
_BOQ_STYLE_VALUE = 3
_BOQ_STYLE_HEADER = 4
_BOQ_STYLE_SECTION = 5
_BOQ_STYLE_ITEM_TEXT = 6
_BOQ_STYLE_ITEM_NUM = 7
_BOQ_STYLE_SUBTOTAL_LABEL = 8
_BOQ_STYLE_SUBTOTAL_NUM = 9
_BOQ_STYLE_GRAND_LABEL = 10
_BOQ_STYLE_GRAND_NUM = 11


def _boq_cell(col, row, style, value, is_number=False):
    ref = "{0}{1}".format(col, row)
    if is_number:
        return '<c r="{0}" s="{1}"><v>{2}</v></c>'.format(ref, style, value)
    return '<c r="{0}" s="{1}" t="inlineStr"><is><t xml:space="preserve">{2}</t></is></c>'.format(
        ref, style, _escape("" if value is None else str(value)))


def _boq_title_and_info_rows(title_text, project_info, last_col):
    """Shared title + Project/Project No/Client/Consultant/Date block
    used at the top of every BOQ sheet variant. Returns (rows_xml list,
    merges list, next_free_row)."""
    rows_xml = ['<row r="1" ht="26" customHeight="1">' +
                _boq_cell("A", 1, _BOQ_STYLE_TITLE, title_text) + "</row>"]
    merges = ["A1:{0}1".format(last_col)]

    info_pairs = [
        ("Project", project_info.get("project_name", "")),
        ("Project No", project_info.get("project_no", "")),
        ("Client", project_info.get("client", "")),
        ("Consultant", project_info.get("consultant", "")),
        ("Date", project_info.get("date", "")),
    ]
    r = 2
    for label, value in info_pairs:
        rows_xml.append(
            '<row r="{0}">'.format(r) +
            _boq_cell("A", r, _BOQ_STYLE_LABEL, label) +
            _boq_cell("B", r, _BOQ_STYLE_VALUE, value) + "</row>")
        merges.append("B{0}:{1}{0}".format(r, last_col))
        r += 1
    return rows_xml, merges, r + 1  # +1 blank spacer row


def _boq_detail_table_rows(sections, currency_symbol, start_row):
    """The Item No/Description/Unit/Quantity/Rate/Amount table with a
    Section group-header row, a Subtotal row per Section, and a Grand
    Total row - the same body write_boq_xlsx has always produced,
    factored out so the Advanced export's Detailed sheet can reuse it
    exactly instead of re-deriving it. Returns (rows_xml, merges,
    grand_total, next_free_row)."""
    headers = ["Item No", "Description", "Unit", "Quantity",
               "Rate ({0})".format(currency_symbol), "Amount ({0})".format(currency_symbol)]
    ncols = len(headers)
    rows_xml = []
    merges = []
    last_col = _col_letter(ncols)

    r = start_row
    header_row = "".join(
        _boq_cell(_col_letter(i + 1), r, _BOQ_STYLE_HEADER, h) for i, h in enumerate(headers))
    rows_xml.append('<row r="{0}">'.format(r) + header_row + "</row>")
    r += 1

    grand_total = 0.0
    for section in sections:
        section_label = "{0}  {1}".format(getattr(section, "section_no", ""), section.title).strip()
        section_cells = _boq_cell("A", r, _BOQ_STYLE_SECTION, section_label)
        section_cells += "".join(
            _boq_cell(_col_letter(i + 2), r, _BOQ_STYLE_SECTION, "") for i in range(ncols - 1))
        rows_xml.append('<row r="{0}">'.format(r) + section_cells + "</row>")
        merges.append("A{0}:{1}{0}".format(r, last_col))
        r += 1

        for item in section.items:
            amount = item.quantity * item.rate
            rows_xml.append(
                '<row r="{0}">'.format(r) +
                _boq_cell("A", r, _BOQ_STYLE_ITEM_TEXT, getattr(item, "item_no", "")) +
                _boq_cell("B", r, _BOQ_STYLE_ITEM_TEXT, item.description) +
                _boq_cell("C", r, _BOQ_STYLE_ITEM_TEXT, item.unit) +
                _boq_cell("D", r, _BOQ_STYLE_ITEM_NUM, round(item.quantity, 2), is_number=True) +
                _boq_cell("E", r, _BOQ_STYLE_ITEM_NUM, round(item.rate, 2), is_number=True) +
                _boq_cell("F", r, _BOQ_STYLE_ITEM_NUM, round(amount, 2), is_number=True) +
                "</row>")
            r += 1

        subtotal = sum(item.quantity * item.rate for item in section.items)
        grand_total += subtotal
        rows_xml.append(
            '<row r="{0}">'.format(r) +
            _boq_cell("A", r, _BOQ_STYLE_SUBTOTAL_LABEL, "") +
            _boq_cell("B", r, _BOQ_STYLE_SUBTOTAL_LABEL, "") +
            _boq_cell("C", r, _BOQ_STYLE_SUBTOTAL_LABEL, "") +
            _boq_cell("D", r, _BOQ_STYLE_SUBTOTAL_LABEL, "") +
            _boq_cell("E", r, _BOQ_STYLE_SUBTOTAL_LABEL, "Subtotal") +
            _boq_cell("F", r, _BOQ_STYLE_SUBTOTAL_NUM, round(subtotal, 2), is_number=True) +
            "</row>")
        r += 1

    grand_cells = "".join(_boq_cell(_col_letter(i + 1), r, _BOQ_STYLE_GRAND_LABEL, "") for i in range(4))
    grand_cells += _boq_cell("E", r, _BOQ_STYLE_GRAND_LABEL, "Grand Total")
    grand_cells += _boq_cell("F", r, _BOQ_STYLE_GRAND_NUM, round(grand_total, 2), is_number=True)
    rows_xml.append('<row r="{0}">'.format(r) + grand_cells + "</row>")
    r += 1

    return rows_xml, merges, grand_total, r


def _boq_sheet_xml(col_widths, rows_xml, merges):
    ncols = len(col_widths)
    cols_xml = "".join(
        '<col min="{0}" max="{0}" width="{1}" customWidth="1"/>'.format(i + 1, w)
        for i, w in enumerate(col_widths))
    merge_xml = ""
    if merges:
        merge_xml = '<mergeCells count="{0}">{1}</mergeCells>'.format(
            len(merges), "".join('<mergeCell ref="{0}"/>'.format(m) for m in merges))
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<cols>{0}</cols><sheetData>{1}</sheetData>{2}</worksheet>'
    ).format(cols_xml, "".join(rows_xml), merge_xml)


def write_boq_xlsx(path, project_info, sections, currency_symbol="$"):
    """path: output file path.
    project_info: dict with optional keys project_name/project_no/
                  client/consultant/date.
    sections: list of section-like objects, each with .title/
              .section_no and .items (item-like objects with
              .item_no/.description/.unit/.quantity/.rate) - matches
              dee_qs_service's BoqSection/BoqItem shape but only reads
              these attributes, so any duck-typed equivalent works.
    currency_symbol: shown in the Rate/Amount column headers only -
                     Excel itself just sees plain numbers.
    """
    col_widths = [10, 46, 10, 14, 14, 16]
    last_col = _col_letter(len(col_widths))

    title_rows, title_merges, next_row = _boq_title_and_info_rows("Bill of Quantities", project_info, last_col)
    detail_rows, detail_merges, _grand_total, _next_row = _boq_detail_table_rows(
        sections, currency_symbol, next_row)

    sheet_xml = _boq_sheet_xml(col_widths, title_rows + detail_rows, title_merges + detail_merges)

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _ROOT_RELS)
        z.writestr("xl/workbook.xml", _WORKBOOK)
        z.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS)
        z.writestr("xl/styles.xml", _BOQ_STYLES)
        z.writestr("xl/worksheets/sheet1.xml", sheet_xml)


# ---------------------------------------------------------------------------
# 3-sheet Advanced BOQ export (DeeQs Phase 2) - Summary (one row per
# Section, subtotal only), Detailed (the exact same section/item table
# write_boq_xlsx produces, via the shared helpers above), and a hidden
# Data sheet (every individual scanned element's raw category/
# segregation-value/quantity/unit - not aggregated - for traceability/
# audit back to the source scan). Reuses _BOQ_STYLES and every helper
# above rather than duplicating the section/item-table logic.
# ---------------------------------------------------------------------------
def _boq_summary_table_rows(sections, currency_symbol, start_row):
    headers = ["Section No", "Section", "Subtotal ({0})".format(currency_symbol)]
    ncols = len(headers)
    last_col = _col_letter(ncols)
    rows_xml = []

    r = start_row
    header_row = "".join(
        _boq_cell(_col_letter(i + 1), r, _BOQ_STYLE_HEADER, h) for i, h in enumerate(headers))
    rows_xml.append('<row r="{0}">'.format(r) + header_row + "</row>")
    r += 1

    grand_total = 0.0
    for section in sections:
        subtotal = sum(item.quantity * item.rate for item in section.items)
        grand_total += subtotal
        rows_xml.append(
            '<row r="{0}">'.format(r) +
            _boq_cell("A", r, _BOQ_STYLE_ITEM_TEXT, getattr(section, "section_no", "")) +
            _boq_cell("B", r, _BOQ_STYLE_ITEM_TEXT, section.title) +
            _boq_cell("C", r, _BOQ_STYLE_ITEM_NUM, round(subtotal, 2), is_number=True) +
            "</row>")
        r += 1

    grand_cells = (
        _boq_cell("A", r, _BOQ_STYLE_GRAND_LABEL, "") +
        _boq_cell("B", r, _BOQ_STYLE_GRAND_LABEL, "Grand Total") +
        _boq_cell("C", r, _BOQ_STYLE_GRAND_NUM, round(grand_total, 2), is_number=True))
    rows_xml.append('<row r="{0}">'.format(r) + grand_cells + "</row>")
    r += 1

    return rows_xml, [], r, [10, 40, 18], last_col


def _boq_data_table_rows(raw_rows, start_row):
    """raw_rows: list of (category_name, segregation_value, quantity,
    unit) tuples - one per scanned element (dee_qs_service.ScanRow, via
    dee_qs_service.pivot_display_rows-style flattening, but per-element
    rather than pre-summed) - or None to emit an empty Data sheet when
    the caller doesn't have per-element rows on hand."""
    headers = ["Category", "Segregation Value", "Quantity", "Unit"]
    rows_xml = []
    r = start_row
    header_row = "".join(
        _boq_cell(_col_letter(i + 1), r, _BOQ_STYLE_HEADER, h) for i, h in enumerate(headers))
    rows_xml.append('<row r="{0}">'.format(r) + header_row + "</row>")
    r += 1
    for category_name, segregation_value, quantity, unit in (raw_rows or []):
        rows_xml.append(
            '<row r="{0}">'.format(r) +
            _boq_cell("A", r, _BOQ_STYLE_ITEM_TEXT, category_name) +
            _boq_cell("B", r, _BOQ_STYLE_ITEM_TEXT, segregation_value) +
            _boq_cell("C", r, _BOQ_STYLE_ITEM_NUM, round(quantity, 2), is_number=True) +
            _boq_cell("D", r, _BOQ_STYLE_ITEM_TEXT, unit) +
            "</row>")
        r += 1
    return rows_xml, [16, 220, 14, 10], r


def write_boq_advanced_xlsx(path, project_info, sections, currency_symbol="$", raw_rows=None):
    """path: output file path.
    project_info/sections/currency_symbol: same as write_boq_xlsx.
    raw_rows: optional list of (category_name, segregation_value,
              quantity, unit) tuples - one per scanned element, for the
              hidden Data sheet. Omit for an empty Data sheet.
    Produces a 3-sheet workbook: Summary (Section/Subtotal only),
    Detailed (the full write_boq_xlsx table), Data (hidden, raw
    per-element scan rows for audit).
    """
    detail_col_widths = [10, 46, 10, 14, 14, 16]
    detail_last_col = _col_letter(len(detail_col_widths))

    summary_title_rows, summary_title_merges, summary_next = _boq_title_and_info_rows(
        "Bill of Quantities - Summary", project_info, _col_letter(3))
    summary_rows, summary_merges, _summary_next, summary_col_widths, _summary_last_col = \
        _boq_summary_table_rows(sections, currency_symbol, summary_next)
    summary_xml = _boq_sheet_xml(summary_col_widths, summary_title_rows + summary_rows,
                                  summary_title_merges + summary_merges)

    detail_title_rows, detail_title_merges, detail_next = _boq_title_and_info_rows(
        "Bill of Quantities - Detailed", project_info, detail_last_col)
    detail_rows, detail_merges, _grand_total, _detail_next = _boq_detail_table_rows(
        sections, currency_symbol, detail_next)
    detail_xml = _boq_sheet_xml(detail_col_widths, detail_title_rows + detail_rows,
                                 detail_title_merges + detail_merges)

    data_rows, data_col_widths, _data_next = _boq_data_table_rows(raw_rows, 1)
    data_xml = _boq_sheet_xml(data_col_widths, data_rows, [])

    sheet_names = ["Summary", "Detailed", "Data"]
    sheet_xmls = [summary_xml, detail_xml, data_xml]

    content_overrides = "".join(
        '<Override PartName="/xl/worksheets/sheet{0}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'.format(i + 1)
        for i in range(len(sheet_names)))
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        + content_overrides + '</Types>')

    workbook_sheets = "".join(
        '<sheet name="{0}" sheetId="{1}" r:id="rId{1}"{2}/>'.format(
            _escape(name), i + 1, ' state="hidden"' if name == "Data" else "")
        for i, name in enumerate(sheet_names))
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets>' + workbook_sheets + '</sheets></workbook>')

    styles_rid = len(sheet_names) + 1
    workbook_rels = "".join(
        '<Relationship Id="rId{0}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet{0}.xml"/>'.format(i + 1)
        for i in range(len(sheet_names)))
    workbook_rels += (
        '<Relationship Id="rId{0}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'.format(styles_rid))
    workbook_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + workbook_rels + '</Relationships>')

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", _ROOT_RELS)
        z.writestr("xl/workbook.xml", workbook_xml)
        z.writestr("xl/_rels/workbook.xml.rels", workbook_rels_xml)
        z.writestr("xl/styles.xml", _BOQ_STYLES)
        for i, sheet_xml in enumerate(sheet_xmls):
            z.writestr("xl/worksheets/sheet{0}.xml".format(i + 1), sheet_xml)
