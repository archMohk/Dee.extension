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
