# -*- coding: utf-8 -*-
"""
DeeMono
Recolours a 3D view into a single-hue "monochrome presentation" theme
(the style in the reference image the user attached: flat, poster-like
colour with crisp dark line work) by driving the same View Graphic
Overrides "VV" opens - see lib/dee_mono_service.py for the Revit API
facts this relies on and exactly which ones were independently
confirmed versus reused from DeeSSelect's already-proven code.

--------------------------------------------------------------------
Why this tool has no custom window
--------------------------------------------------------------------
Every prompt here is a plain pyRevit or native Windows dialog
(CommandSwitchWindow, SelectFromList, System.Windows.Forms.ColorDialog)
shown one after another with nothing else open at the time - never a
custom WPF window with a nested modal inside it, which is the specific
shape that has crashed Revit elsewhere in this codebase. The colour
PICKER the user asked for ("let me select the colour") is exactly
System.Windows.Forms.ColorDialog with FullOpen=True, the identical
call DeeSSelect already uses live.

--------------------------------------------------------------------
Style
--------------------------------------------------------------------
After the colour, one more native dialog offers six named presets
(lib/dee_mono_service.py: PRESETS) that control how much categories
vary from each other and from the theme colour - how far lightness
spreads (Subtle/Balanced/Bold), a uniform light/dark bias (Pastel skews
everything light), how much each category's own hue drifts toward its
real-world material (wood-ish furniture, cool metal fixtures, blue-cyan
glazing, green planting), overall saturation, and line weight (thin for
Pastel, heavy for Sketch). Every preset still goes through the same
guaranteed contrast floor, so no combination of choices can make the
line work disappear against the fill.

--------------------------------------------------------------------
Restore
--------------------------------------------------------------------
Before anything is changed, the view's CURRENT category overrides,
display style and line weights are captured and saved to disk
(lib/.dee_mono/, one file per document+view, gitignored - same spirit
as lib/.deew_settings/). Running this tool again on a view that already
has a saved snapshot offers Restore as the first choice, so applying a
theme is never a one-way trip.
"""
import os
import traceback

import clr
clr.AddReference("System.Windows.Forms")
clr.AddReference("System.Drawing")
from System.Windows.Forms import ColorDialog, DialogResult
from System.Drawing import Color as DrawingColor

from pyrevit import forms, script

import dee_mono_service as core

output = script.get_output()

_TOOL = "DeeMono"

APPLY = "Apply a theme colour"
RESTORE = "Restore original graphics"
FLATTEN_YES = "Flatten shading to 'Consistent Colors' (recommended - matches the poster look)"
FLATTEN_NO = "Keep normal shaded-with-edges shading"

# Each preset's own description is baked straight into its button label
# (matching this codebase's own precedent, e.g. DeeOpener's open-mode
# list) rather than shown separately - CommandSwitchWindow has no
# established usage anywhere in this codebase of a per-button subtitle,
# so this is the proven-safe way to put both in front of the user in
# one native dialog.
_PRESET_LABELS = ["{0} - {1}".format(name, core.PRESETS[name]["description"])
                  for name in core.PRESET_ORDER]
_PRESET_BY_LABEL = dict(zip(_PRESET_LABELS, core.PRESET_ORDER))


def _pick_preset():
    chosen = forms.CommandSwitchWindow.show(
        _PRESET_LABELS, message="Style - how much should categories vary?")
    if not chosen:
        return None
    return _PRESET_BY_LABEL.get(chosen, core.DEFAULT_PRESET)


def _pick_view(doc, views, active_view):
    """SelectFromList, not a custom window - a single native pyRevit
    dialog with nothing else open. The active view's label is marked in
    the list (rather than pre-selected - this codebase has no proven
    usage anywhere of a `default=` kwarg on this call, and a new tool's
    very first prompt is the wrong place to find out the hard way
    whether that argument exists)."""
    active_label = None
    try:
        if active_view is not None:
            active_label = core.view_label(active_view)
    except Exception:
        active_label = None

    def label_of(v):
        text = core.view_label(v)
        return text + "  (active view)" if text == active_label else text

    labels = [label_of(v) for v in views]
    by_label = dict(zip(labels, views))

    picked = forms.SelectFromList.show(
        labels, title="Which 3D view?", button_name="Use this view",
        multiselect=False)
    if not picked:
        return None
    return by_label.get(picked)


def _pick_color(initial_rgb=(200, 120, 90)):
    dlg = ColorDialog()
    dlg.Color = DrawingColor.FromArgb(*initial_rgb)
    dlg.FullOpen = True
    dlg.AnyColor = True
    if dlg.ShowDialog() != DialogResult.OK:
        return None
    c = dlg.Color
    return (int(c.R), int(c.G), int(c.B))


def _report(view, base_rgb, result, mode, preset_name=None):
    colour_line = ""
    if base_rgb is not None:
        hexcode = "#{0:02X}{1:02X}{2:02X}".format(*base_rgb)
        swatch = ('<span style="display:inline-block;width:14px;height:14px;'
                 'background:{0};border:1px solid #888;vertical-align:middle;'
                 'margin-right:6px;"></span>'.format(hexcode))
        colour_line = "<br><b>Theme colour:</b> {0}{1}".format(swatch, hexcode)
    style_line = "<br><b>Style:</b> {0}".format(preset_name) if preset_name else ""
    html = '<h2 style="font-family:sans-serif;">DeeMono</h2>'
    html += ('<div style="font-family:sans-serif;font-size:12px;">'
             '<b>View:</b> {0}<br><b>Mode:</b> {1}{2}{3}</div>'.format(
                 core.view_label(view), mode, style_line, colour_line))
    bg = "#2e7d32" if not result.errors else "#8d6e19"
    html += ('<div style="margin-top:8px;padding:7px 11px;background:{0};color:#fff;'
             'border-radius:4px;font-family:monospace;font-size:12px;">'
             '{1} categor(y/ies) recoloured, {2} skipped, out of {3} in the view. '
             'Display style {4}. Background {5}.</div>'.format(
                 bg, result.applied, result.skipped, result.category_count,
                 "changed" if result.display_style_set else "unchanged",
                 "set to white" if result.background_set else "left as it was"))
    if result.errors:
        html += '<div style="font-family:sans-serif;font-size:11px;color:#a55;margin-top:6px;">'
        for e in result.errors[:12]:
            html += "&bull; {0}<br>".format(e)
        html += "</div>"
    if mode == "Apply" and not result.errors:
        html += ('<div style="margin-top:8px;padding:7px 11px;background:#37474f;color:#fff;'
                 'border-radius:4px;font-family:sans-serif;font-size:12px;">'
                 "This view's previous graphics were saved before anything changed. "
                 "Run DeeMono again on this view to restore them.</div>")
    output.print_html(html)


def main():
    uiapp = __revit__
    uidoc = uiapp.ActiveUIDocument
    if uidoc is None:
        forms.alert("Open a Revit project first.", title=_TOOL)
        return
    doc = uidoc.Document
    if doc.IsFamilyDocument:
        forms.alert("DeeMono works on a project's 3D view. It cannot be run inside the "
                    "Family Editor.", title=_TOOL)
        return

    views = core.list_3d_views(doc)
    if not views:
        forms.alert("This model has no 3D views. Create one and run DeeMono again.",
                    title=_TOOL)
        return

    active_view = None
    try:
        active_view = doc.ActiveView
    except Exception:
        active_view = None

    mode = APPLY
    if core.has_any_snapshot(doc.Title):
        chosen = forms.CommandSwitchWindow.show(
            [APPLY, RESTORE], message="DeeMono - what would you like to do?")
        if not chosen:
            return
        mode = chosen

    if mode == RESTORE:
        saved_ids = set(core.list_snapshot_view_ids(doc.Title))
        restorable = [v for v in views if v.UniqueId in saved_ids]
        if not restorable:
            forms.alert("No 3D view in this model has a saved DeeMono snapshot yet.",
                        title=_TOOL)
            return
        view = _pick_view(doc, restorable, active_view)
        if view is None:
            return
        try:
            with forms.ProgressBar(title="DeeMono - restoring original graphics...",
                                   indeterminate=True):
                result = core.restore_theme(doc, view)
        except Exception as e:
            forms.alert("DeeMono hit an error while restoring:\n\n{0}\n\n{1}".format(
                e, traceback.format_exc()[-900:]), title=_TOOL)
            return
        try:
            uidoc.RefreshActiveView()
        except Exception:
            pass
        _report(view, None, result, "Restore")
        return

    # ---------------- Apply ----------------
    view = _pick_view(doc, views, active_view)
    if view is None:
        return

    base_rgb = _pick_color()
    if base_rgb is None:
        return

    preset_name = _pick_preset()
    if preset_name is None:
        return

    flatten_choice = forms.CommandSwitchWindow.show(
        [FLATTEN_YES, FLATTEN_NO], message="Shading style for '{0}'?".format(
            core.view_label(view)))
    if not flatten_choice:
        return
    flatten = (flatten_choice == FLATTEN_YES)

    try:
        with forms.ProgressBar(title="DeeMono - applying theme to '{0}'...".format(
                core.view_label(view)), indeterminate=True):
            result = core.apply_theme(doc, view, base_rgb, preset_name=preset_name,
                                      flatten_shading=flatten)
    except Exception as e:
        forms.alert("DeeMono hit an error while applying the theme:\n\n{0}\n\n{1}".format(
            e, traceback.format_exc()[-900:]), title=_TOOL)
        return

    try:
        if view.Id == active_view.Id:
            uidoc.RefreshActiveView()
        elif forms.alert(
                "'{0}' is not the active view, so the change will not be visible until you "
                "open it. Open it now?".format(core.view_label(view)),
                title=_TOOL, yes=True, no=True):
            uidoc.ActiveView = view
    except Exception:
        pass

    _report(view, base_rgb, result, "Apply", preset_name=preset_name)


main()
