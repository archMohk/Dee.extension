# -*- coding: utf-8 -*-
"""Runs under a normal Python (needs PyMuPDF), NOT inside Revit.

    py -I dee_pdf_crop_helper.py <in.pdf> <out.pdf> <left> <top> <right> <bottom>

Writes <out.pdf>: every page of <in.pdf> cut down to the area left after
trimming the four margins (PDF points) from the page edges, as a fresh
page the size of that area - so nothing outside it survives, including
text and linework underneath the removed strip. Used by
lib/dee_draft_import_service.py to drop the sheet border and title block
before a PDF is placed in a Drafting View.
"""
import os
import sys


def main(argv):
    src, dst = argv[1], argv[2]
    left, top, right, bottom = [float(v) for v in argv[3:7]]
    import pymupdf
    doc = pymupdf.open(src)
    out = pymupdf.open()
    for page in doc:
        r = page.rect
        clip = pymupdf.Rect(r.x0 + left, r.y0 + top, r.x1 - right, r.y1 - bottom)
        if clip.is_empty or clip.width < 10 or clip.height < 10:
            raise ValueError("margins leave nothing of page {0}".format(page.number + 1))
        new = out.new_page(width=clip.width, height=clip.height)
        new.show_pdf_page(new.rect, doc, page.number, clip=clip)
    folder = os.path.dirname(dst)
    if folder and not os.path.isdir(folder):
        os.makedirs(folder)
    out.save(dst)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception as ex:
        sys.stderr.write(str(ex))
        sys.exit(1)
