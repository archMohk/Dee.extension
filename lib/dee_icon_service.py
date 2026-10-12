# -*- coding: utf-8 -*-
"""
dee_icon_service
Backs DeeIcon (About) - automatically creates a small branded preview
image, "<rvt file>.DeeIcon.png" next to the .rvt, every time the file is
saved: a snapshot of the active view with a Revit-version badge (e.g.
"2024") in the top-left corner and an optional user-chosen logo in the
bottom-right. Never touches Revit's own internal file thumbnail - there
is no documented API for that (see DeeIcon.pushbutton/script.py's own
docstring for the full scope decision); this is a separate, plain PNG
file saved beside the model.

--------------------------------------------------------------------
Architecture - same "run forever in the background, no click needed"
pattern as dee_prayer_service.py / dee_broadcast_service.py / dee_force_state.py
--------------------------------------------------------------------
The one difference from those three: this subscribes to
ControlledApplication.DocumentSaved instead of UIApplication.Idling -
the first use of that specific event in this codebase, though the
subscribe/unsubscribe shape (module-level handler, `-=` before `+=` so a
pyRevit Reload can never stack duplicate subscriptions) is identical to
theirs, for the exact same reason: this module stays cached in
sys.modules for the life of the Revit process, which is what makes `-=`
from a later call actually remove what `+=` added from an earlier one.

NEEDS LIVE-REVIT VERIFICATION, same as the three existing watchers:
whether a subscription made once from startup.py keeps firing for a
whole session, across closing/reopening documents. Also new/unverified
here specifically: Document.ExportImage's exact output-filename
behavior (some Revit versions append a view-name suffix to FilePath
rather than using it exactly) - handled defensively by
_resolve_exported_file() below, which looks for whatever file
ExportImage actually produced instead of assuming the exact name.

Never raises out of the DocumentSaved handler - a failure here (a
locked output file, a view that can't export, a missing logo file) must
never interrupt the save the user just performed.
"""
import os

import clr
clr.AddReference("System.Drawing")
from System.Drawing import (
    Bitmap, Graphics, Image as DrawingImage, Color as DrawingColor,
    Font, FontStyle, SolidBrush, PointF)
from System.Drawing.Imaging import ImageFormat
from System.Drawing.Drawing2D import InterpolationMode

import deew_settings

_SETTINGS_TOOL_NAME = "dee_icon_settings"
DEFAULT_SETTINGS = {"enabled": False, "logo_path": ""}

_OUTPUT_SUFFIX = ".DeeIcon.png"
_TEMP_PREFIX = "~DeeIcon_"


def load_settings():
    return deew_settings.load(_SETTINGS_TOOL_NAME, dict(DEFAULT_SETTINGS))


def save_settings(data):
    deew_settings.save(_SETTINGS_TOOL_NAME, data)


def _version_text(doc):
    try:
        return str(doc.Application.VersionNumber)
    except Exception:
        return ""


def _resolve_exported_file(file_path_no_ext):
    """Document.ExportImage's FilePath is a *base* path - some Revit
    versions write exactly "<base>.png", others append a view-name
    suffix first. Rather than assume either, look for whatever file
    ExportImage actually produced with that base as a prefix, picking
    the most recently modified match so a stale leftover from an
    earlier failed run can never be picked over the fresh one."""
    exact = file_path_no_ext + ".png"
    if os.path.isfile(exact):
        return exact
    folder = os.path.dirname(file_path_no_ext) or "."
    prefix = os.path.basename(file_path_no_ext).lower()
    candidates = []
    try:
        for name in os.listdir(folder):
            if name.lower().startswith(prefix) and name.lower().endswith(".png"):
                candidates.append(os.path.join(folder, name))
    except Exception:
        pass
    if not candidates:
        return None
    candidates.sort(key=os.path.getmtime, reverse=True)
    return candidates[0]


def _export_active_view(doc, file_path_no_ext):
    from Autodesk.Revit.DB import (
        ImageExportOptions, ExportRange, ZoomFitType, ImageResolution, ImageFileType)
    view = doc.ActiveView
    if view is None:
        raise Exception("No active view to capture.")
    options = ImageExportOptions()
    options.ZoomType = ZoomFitType.FitToPage
    options.PixelSize = 800
    options.ImageResolution = ImageResolution.DPI_150
    options.ExportRange = ExportRange.CurrentView
    options.HLRandWFViewsFileType = ImageFileType.PNG
    options.FilePath = file_path_no_ext
    doc.ExportImage(options)
    exported = _resolve_exported_file(file_path_no_ext)
    if not exported:
        raise Exception("Revit did not produce an image file for the active view.")
    return exported


def _composite_overlay(base_image_path, version_text, logo_path, output_path):
    """Mirrors NotificationCenter.pushbutton's own
    _resize_and_encode_image - same clr.AddReference("System.Drawing"),
    same Bitmap/Graphics.FromImage/DrawImage shape, same "dispose
    everything in a finally block" discipline. The one new piece beyond
    that proven function: drawing TEXT onto the bitmap (DrawString/Font/
    SolidBrush) - standard, well-documented GDI+ API, just not something
    any file in this repo had done before this."""
    base_img = DrawingImage.FromFile(base_image_path)
    try:
        bmp = Bitmap(base_img.Width, base_img.Height)
        try:
            g = Graphics.FromImage(bmp)
            try:
                g.InterpolationMode = InterpolationMode.HighQualityBicubic
                g.DrawImage(base_img, 0, 0, base_img.Width, base_img.Height)

                if version_text:
                    font = Font("Segoe UI", 13.0, FontStyle.Bold)
                    try:
                        text_size = g.MeasureString(version_text, font)
                        pad_x, pad_y, margin = 10.0, 6.0, 10.0
                        badge_w = text_size.Width + pad_x * 2
                        badge_h = text_size.Height + pad_y * 2
                        bg_brush = SolidBrush(DrawingColor.FromArgb(190, 0, 0, 0))
                        try:
                            g.FillRectangle(bg_brush, margin, margin, badge_w, badge_h)
                        finally:
                            bg_brush.Dispose()
                        text_brush = SolidBrush(DrawingColor.White)
                        try:
                            g.DrawString(version_text, font, text_brush,
                                          PointF(margin + pad_x, margin + pad_y))
                        finally:
                            text_brush.Dispose()
                    finally:
                        font.Dispose()

                if logo_path and os.path.isfile(logo_path):
                    logo_img = DrawingImage.FromFile(logo_path)
                    try:
                        max_dim = 110.0
                        scale = min(1.0, max_dim / max(logo_img.Width, logo_img.Height))
                        logo_w = max(1, int(logo_img.Width * scale))
                        logo_h = max(1, int(logo_img.Height * scale))
                        margin_px = 14
                        x = bmp.Width - logo_w - margin_px
                        y = bmp.Height - logo_h - margin_px
                        g.DrawImage(logo_img, x, y, logo_w, logo_h)
                    finally:
                        logo_img.Dispose()
            finally:
                g.Dispose()
            bmp.Save(output_path, ImageFormat.Png)
        finally:
            bmp.Dispose()
    finally:
        base_img.Dispose()


def generate_preview(doc, settings=None):
    """Does the actual work: export the active view, composite the
    version badge + logo, save next to the .rvt as "<file>.DeeIcon.png".
    Returns (True, output_path) or (False, reason) - never raises, so
    both the DocumentSaved handler and the "Generate Preview Now" button
    can treat this call the same way."""
    if settings is None:
        settings = load_settings()
    try:
        if doc is None:
            return False, "No document."
        try:
            path = doc.PathName
        except Exception:
            path = None
        if not path or not os.path.isfile(path):
            return False, "This document has not been saved to a file yet."

        folder = os.path.dirname(path)
        base_name = os.path.splitext(os.path.basename(path))[0]
        output_path = os.path.join(folder, base_name + _OUTPUT_SUFFIX)
        temp_base = os.path.join(folder, _TEMP_PREFIX + base_name)

        exported_path = _export_active_view(doc, temp_base)
        try:
            logo_path = settings.get("logo_path") or ""
            if logo_path and not os.path.isfile(logo_path):
                logo_path = ""
            _composite_overlay(exported_path, _version_text(doc), logo_path, output_path)
        finally:
            try:
                if os.path.isfile(exported_path):
                    os.remove(exported_path)
            except Exception:
                pass
        return True, output_path
    except Exception as e:
        return False, str(e)


def _on_document_saved(sender, args):
    try:
        settings = load_settings()
        if not settings.get("enabled"):
            return
        generate_preview(args.Document, settings)
    except Exception:
        pass


def start_watching(uiapp):
    """Call once from startup.py. `-=` before `+=` makes this safe to
    call again on a Reload without stacking duplicate subscriptions -
    see module docstring."""
    try:
        app = uiapp.Application
    except Exception:
        return
    try:
        app.DocumentSaved -= _on_document_saved
    except Exception:
        pass
    try:
        app.DocumentSaved += _on_document_saved
    except Exception:
        pass
