# -*- coding: utf-8 -*-
"""Keeps a sheet's CAD export together with the image files Revit writes
beside it.

A DWG/DXF cannot hold raster images: Revit writes every image placed in
the exported views (title block logos, pasted pictures) as a separate file
in the export folder and the CAD file only links to it. Over a whole
project that scatters image files through the output folder. This groups
them: each sheet that has images is exported into its own folder
(<folder>\\<name>\\<name>.dwg plus its images), and a sheet with no images
lands exactly where it always did, as <folder>\\<name>.dwg.

The CAD file and its images are never separated, so the links keep
working wherever the folder is copied. The export itself is done by the
caller's function; this module only decides where its files end up, so it
has no Revit dependency (tools/checks/test_export_package.py runs it
without Revit). Works on IronPython 2.7 and CPython.
"""
import os
import shutil

# Raster formats a CAD image link can point at.
IMAGE_EXTENSIONS = frozenset([
    ".png", ".jpg", ".jpeg", ".jpe", ".bmp", ".dib", ".tif", ".tiff",
    ".gif", ".jp2", ".tga", ".pcx"])

# Revit/AutoCAD fail on paths past MAX_PATH (260); leave room for the
# extension and the terminating NUL.
_MAX_PATH_BUDGET = 248


def is_image(file_name):
    return os.path.splitext(file_name)[1].lower() in IMAGE_EXTENSIONS


def _snapshot(folder):
    """{file name: (modified, size)} of the files directly in `folder`."""
    found = {}
    if os.path.isdir(folder):
        for n in os.listdir(folder):
            p = os.path.join(folder, n)
            if os.path.isfile(p):
                try:
                    st = os.stat(p)
                    found[n] = (st.st_mtime, st.st_size)
                except OSError:
                    pass
    return found


def _move_over(src, dst_folder):
    dst = os.path.join(dst_folder, os.path.basename(src))
    if os.path.exists(dst):
        os.remove(dst)
    shutil.move(src, dst)


def export_with_images(export_fn, folder, name, package=True):
    """Runs `export_fn(target_folder)` (which does the Revit export of one
    sheet as <name>.<ext> into target_folder and raises on failure) and
    returns (images, note).

    package False: exports straight into `folder`, as before; images is [].
    package True: exports into <folder>\\<name>\\. If any image file is
    there afterwards the sheet stays packaged in that folder and `images`
    lists them; otherwise everything this export wrote is moved up into
    `folder` and the empty package folder is removed. `note` is a
    warning to show with the result ("" when there is nothing to say).
    """
    if not package:
        export_fn(folder)
        return [], ""

    pack = os.path.join(folder, name)
    if len(pack) + 1 + len(name) > _MAX_PATH_BUDGET:
        export_fn(folder)
        return [], (u"Not packaged: the path would be too long for Windows "
                    u"('{0}'). Exported next to the other files instead; "
                    u"its images, if any, are in the same folder.".format(name))
    if os.path.exists(pack) and not os.path.isdir(pack):
        export_fn(folder)
        return [], (u"Not packaged: a file named '{0}' is already in the "
                    u"folder. Exported next to the other files "
                    u"instead.".format(name))

    created = not os.path.isdir(pack)
    if created:
        os.makedirs(pack)
    before = _snapshot(pack)
    try:
        export_fn(pack)
    except Exception:
        if created:
            try:
                os.rmdir(pack)      # only succeeds while still empty
            except OSError:
                pass
        raise

    after = _snapshot(pack)
    images = sorted(n for n in after if is_image(n))
    if images:
        return images, ""

    # No images: put the sheet back where an unpackaged export would be.
    for n in after:
        if after[n] != before.get(n):
            _move_over(os.path.join(pack, n), folder)
    try:
        os.rmdir(pack)              # a leftover from an earlier run stays
    except OSError:
        pass
    return [], ""
