# -*- coding: utf-8 -*-
"""
DeeSheetLinks (Masterplan)
For every already-placed link copy (every "villa" DeeLinkDist placed -
see lib/dee_sheet_links_service.py for the Revit API facts this relies
on), creates one Sheet + one cropped Floor Plan View PER Sheet Type you
define - a generic, user-managed list (name + Level + optional View
Template), not a hardcoded Ground/First/Roof set. The view's Crop
Region is set to that villa's own footprint (+ your offset), and the
SAME Building Typology/Parcel ID/Developer ID parameters DeeLinkDist
wrote onto the link are copied onto the view too.

--------------------------------------------------------------------
Three-page wizard, same TabControl shape as DeeLinkDist/DeeW.Transmit
--------------------------------------------------------------------
Page 1 scans every RevitLinkInstance in the model into a DataGrid with
a per-row Include checkbox (defaulted from whether the link has
Typology data, per the "only include links with data" toggle - never
hidden, just default-unchecked, so a link the user genuinely wants can
still be ticked back in).

Page 2's Sheet Type list is built in CODE like DeeLinkDist's own
mapping rows - one row per Sheet Type, "Add Sheet Type" growing the
list with no fixed count (this is what makes "Number of Sheets" fully
generic rather than a hardcoded spinner, per explicit request).

Page 3 renders the (villa x sheet type) plan via the token engine
BEFORE anything is created (Preview), flags duplicate/blank/invalid
Sheet Numbers in the grid, and only Ready rows are ever handed to
Create - matching this codebase's "report before action, always" rule.
"""
import os
import traceback

import clr
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
from System.Windows import Thickness, VerticalAlignment
from System.Windows.Controls import StackPanel, TextBlock, TextBox, ComboBox, Button, Orientation

from pyrevit import forms, script

import dee_branding
import dee_sheet_links_service as core

output = script.get_output()

_TOOL = "DeeSheetLinks"
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")
_NONE_TEMPLATE = "(None)"


def _safe_float(text, default=0.0):
    try:
        return float(text)
    except Exception:
        return default


class _SafeProgress(object):
    """forms.ProgressBar tries to set Window.TaskbarItemInfo on its host
    window - a genuine WPF-level bug (not this extension's code):
    Window.TaskbarItemInfo throws NotImplementedException whenever the
    underlying ITaskbarList::HrInit COM call fails, which is documented
    to happen specifically under Remote Desktop/Terminal Services or a
    custom shell without a taskbar - live-confirmed on this exact error
    from TWO different DeeSheetLinks actions (the initial scan, then
    Preview), so this is an environment condition, not a one-off.

    Wraps the real forms.ProgressBar and falls back to running with NO
    progress UI at all if entering it fails, so the tool degrades
    gracefully under RDP instead of crashing - everyone else still gets
    the real progress bar exactly as before. `pb.update_progress(...)`/
    `pb.cancelled` are safe no-ops in the fallback case, so callers never
    need an extra branch."""
    def __init__(self, **kwargs):
        self._kwargs = kwargs
        self._real = None

    def __enter__(self):
        try:
            self._real = forms.ProgressBar(**self._kwargs)
            return self._real.__enter__()
        except Exception:
            self._real = None
            return self

    def __exit__(self, exc_type, exc_value, tb):
        if self._real is not None:
            return self._real.__exit__(exc_type, exc_value, tb)
        return False

    @property
    def cancelled(self):
        return False

    def update_progress(self, i, total):
        pass


class BuildReportRow(object):
    """Flat view-model for report_grid - a plain object with top-level
    attributes, the proven DataGrid-binding shape already used
    everywhere else in this codebase."""
    def __init__(self, row_result):
        self.sheet_number = row_result.sheet_number
        self.sheet_name = row_result.sheet_name
        self.sheet_type_name = row_result.sheet_type_name
        self.result = "Created" if row_result.ok else "Skipped"
        self.message = row_result.message


class SheetTypeUIRow(object):
    """One Sheet Type's live widgets, plus the SheetType object they
    write back into just before Preview/Create - the widgets are the
    source of truth while the window is open, not the SheetType's own
    fields (which only get synced on demand)."""
    def __init__(self, sheet_type, panel, name_tb, level_cb, template_cb):
        self.sheet_type = sheet_type
        self.panel = panel
        self.name_tb = name_tb
        self.level_cb = level_cb
        self.template_cb = template_cb


class DeeSheetLinksWindow(dee_branding.DeeBrandedWindow):
    _ready = False

    def __init__(self, xaml_file, doc):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.doc = doc
        self._link_rows = []
        self._sheet_type_rows = []
        self._plan = []
        self._levels = []
        self._templates = []

        for label, _unit_type_id in core.UNIT_OPTIONS:
            self.offset_unit_cb.Items.Add(label)
        self.offset_unit_cb.SelectedIndex = [l for l, _u in core.UNIT_OPTIONS].index(
            core.DEFAULT_UNIT_LABEL)

        self._levels = core.list_levels(self.doc)
        self._templates = core.list_view_templates(self.doc)
        self._refresh_titleblocks()

        self._ready = True
        self._guard(self._scan_links, False)

    def _guard(self, fn, *args):
        try:
            return fn(*args)
        except Exception as e:
            self.status_tb.Text = "ERROR: {0}".format(e)
            forms.alert("DeeSheetLinks hit an error:\n\n{0}\n\n{1}".format(
                e, traceback.format_exc()[-900:]), title=_TOOL)

    def _selected_offset_unit(self):
        i = self.offset_unit_cb.SelectedIndex
        options = core.UNIT_OPTIONS
        if 0 <= i < len(options):
            return options[i][0]
        return core.DEFAULT_UNIT_LABEL

    def _selected_param_names(self):
        return {
            "typology": (self.param_typology_tb.Text or "").strip() or "Building Typology",
            "parcel_id": (self.param_parcel_tb.Text or "").strip() or "Parcel ID",
            "developer_id": (self.param_developer_tb.Text or "").strip() or "Developer ID",
        }

    # ---------------- page 1: scan links ----------------
    def _scan_links(self, with_progress):
        """with_progress=False is used ONLY from __init__ - forms.
        ProgressBar attaches itself to the host window's TaskbarItemInfo,
        which throws NotImplementedError when the window has not been
        shown yet (ShowDialog() has not run, so it has no HWND) - a
        live-confirmed crash, fixed by skipping the progress bar for the
        one scan that happens before the window is visible, matching
        DeeLinkDist's own _refresh_link_types (called plain from its own
        __init__, only wrapped in ProgressBar from button clicks that
        run after the window is already shown)."""
        def do_scan():
            self._link_rows = core.list_link_instances(self.doc, self._selected_param_names())
        if with_progress:
            with _SafeProgress(title="DeeSheetLinks - scanning links...", indeterminate=True):
                do_scan()
        else:
            do_scan()
        self._apply_default_selection()
        self.links_grid.ItemsSource = None
        self.links_grid.ItemsSource = self._link_rows
        with_data = sum(1 for r in self._link_rows if r.has_typology)
        self.link_status_tb.Text = "{0} link(s) found, {1} with Typology data.".format(
            len(self._link_rows), with_data)
        self.status_tb.Text = self.link_status_tb.Text

    def scan_links_click(self, sender, args):
        self._guard(self._scan_links, True)

    def _apply_default_selection(self):
        only_with_data = bool(self.only_with_data_cb.IsChecked)
        for row in self._link_rows:
            row.selected = row.has_typology if only_with_data else True

    def only_with_data_changed(self, sender, args):
        if not self._ready or not self._link_rows:
            return
        self._apply_default_selection()
        self.links_grid.Items.Refresh()

    # ---------------- page 2: sheet types ----------------
    def _level_names(self):
        return [name for _id, name in self._levels] or ["(no levels in project)"]

    def _template_names(self):
        return [_NONE_TEMPLATE] + [name for _id, name in self._templates]

    def add_sheet_type_click(self, sender, args):
        def run():
            sheet_type = core.SheetType(name="Sheet Type {0}".format(len(self._sheet_type_rows) + 1))
            if self._levels:
                sheet_type.level_id, sheet_type.level_name = self._levels[0]
            self._add_sheet_type_row(sheet_type)
            self._update_sheet_type_status()
        self._guard(run)

    def _add_sheet_type_row(self, sheet_type):
        row_panel = StackPanel()
        row_panel.Orientation = Orientation.Horizontal
        row_panel.Margin = Thickness(0, 0, 0, 8)

        name_tb = TextBox()
        name_tb.Text = sheet_type.name
        name_tb.Width = 220
        name_tb.Height = 26
        name_tb.VerticalContentAlignment = VerticalAlignment.Center
        row_panel.Children.Add(name_tb)

        level_label = TextBlock()
        level_label.Text = "  Level:"
        level_label.VerticalAlignment = VerticalAlignment.Center
        row_panel.Children.Add(level_label)

        level_cb = ComboBox()
        level_cb.Width = 150
        level_cb.Height = 26
        level_cb.Margin = Thickness(4, 0, 0, 0)
        for name in self._level_names():
            level_cb.Items.Add(name)
        level_cb.SelectedIndex = 0
        row_panel.Children.Add(level_cb)

        template_label = TextBlock()
        template_label.Text = "  View Template:"
        template_label.VerticalAlignment = VerticalAlignment.Center
        row_panel.Children.Add(template_label)

        template_cb = ComboBox()
        template_cb.Width = 180
        template_cb.Height = 26
        template_cb.Margin = Thickness(4, 0, 0, 0)
        for name in self._template_names():
            template_cb.Items.Add(name)
        template_cb.SelectedIndex = 0
        row_panel.Children.Add(template_cb)

        remove_b = Button()
        remove_b.Content = "Remove"
        remove_b.Width = 70
        remove_b.Height = 26
        remove_b.Margin = Thickness(10, 0, 0, 0)
        row_panel.Children.Add(remove_b)

        ui_row = SheetTypeUIRow(sheet_type, row_panel, name_tb, level_cb, template_cb)
        remove_b.Click += self._make_remove_handler(ui_row)

        self._sheet_type_rows.append(ui_row)
        self.sheet_types_panel.Children.Add(row_panel)

    def _make_remove_handler(self, ui_row):
        def handler(sender, args):
            self.sheet_types_panel.Children.Remove(ui_row.panel)
            self._sheet_type_rows.remove(ui_row)
            self._update_sheet_type_status()
        return handler

    def _update_sheet_type_status(self):
        self.sheet_type_status_tb.Text = "{0} Sheet Type(s) defined.".format(len(self._sheet_type_rows))
        self.status_tb.Text = self.sheet_type_status_tb.Text

    def _sync_sheet_types_from_ui(self):
        for ui_row in self._sheet_type_rows:
            st = ui_row.sheet_type
            st.name = (ui_row.name_tb.Text or "").strip() or st.name
            level_i = ui_row.level_cb.SelectedIndex
            if 0 <= level_i < len(self._levels):
                st.level_id, st.level_name = self._levels[level_i]
            template_i = ui_row.template_cb.SelectedIndex
            if template_i <= 0:
                st.view_template_id, st.view_template_name = None, _NONE_TEMPLATE
            elif (template_i - 1) < len(self._templates):
                st.view_template_id, st.view_template_name = self._templates[template_i - 1]

    def _sheet_types(self):
        return [ui_row.sheet_type for ui_row in self._sheet_type_rows]

    # ---------------- page 3: title blocks / naming / preview / create ----------------
    def _refresh_titleblocks(self):
        self._titleblocks = core.list_title_blocks(self.doc)
        self.titleblock_cb.ItemsSource = None
        self.titleblock_cb.ItemsSource = [label for _id, label in self._titleblocks]
        if self._titleblocks:
            self.titleblock_cb.SelectedIndex = 0

    def _selected_titleblock_id(self):
        i = self.titleblock_cb.SelectedIndex
        if 0 <= i < len(self._titleblocks):
            return self._titleblocks[i][0]
        return None

    def preview_click(self, sender, args):
        def run():
            self._sync_sheet_types_from_ui()
            selected_links = [r for r in self._link_rows if r.selected]
            if not selected_links:
                forms.alert("Check at least one link to include first (page 1).", title=_TOOL)
                return
            sheet_types = self._sheet_types()
            if not sheet_types:
                forms.alert("Add at least one Sheet Type first (page 2).", title=_TOOL)
                return
            number_template = (self.number_template_tb.Text or "").strip() or core.DEFAULT_NUMBER_TEMPLATE
            name_template = (self.name_template_tb.Text or "").strip() or core.DEFAULT_NAME_TEMPLATE
            existing = core.existing_sheet_numbers(self.doc)
            with _SafeProgress(title="DeeSheetLinks - building preview...", indeterminate=True):
                self._plan = core.build_plan(selected_links, sheet_types, number_template,
                                             name_template, existing)
            self.plan_grid.ItemsSource = None
            self.plan_grid.ItemsSource = self._plan
            ready = sum(1 for r in self._plan if r.status == core.STATUS_READY)
            self.plan_status_tb.Text = "{0} planned ({1} villa(s) x {2} sheet type(s)), {3} Ready.".format(
                len(self._plan), len(selected_links), len(sheet_types), ready)
            self.status_tb.Text = self.plan_status_tb.Text
        self._guard(run)

    def create_click(self, sender, args):
        def run():
            if not self._plan:
                forms.alert("Run Preview first (this page).", title=_TOOL)
                return
            ready = [r for r in self._plan if r.status == core.STATUS_READY]
            not_ready = len(self._plan) - len(ready)
            if not ready:
                forms.alert("Nothing is Ready to create - fix the naming templates "
                            "(check for duplicate/blank/invalid Sheet Numbers) and Preview again.",
                            title=_TOOL)
                return
            titleblock_id = self._selected_titleblock_id()
            if titleblock_id is None:
                forms.alert("Pick a Title Block first, or add one to the project.", title=_TOOL)
                return
            vft_id = core.find_floor_plan_view_family_type(self.doc)
            if vft_id is None:
                forms.alert("This project has no Floor Plan view type - cannot create views.",
                            title=_TOOL)
                return

            msg = "Create {0} sheet(s) + cropped view(s)?".format(len(ready))
            if not_ready:
                msg += "\n\n{0} row(s) are not Ready and will be skipped.".format(not_ready)
            if not forms.alert(msg, title=_TOOL, yes=True, no=True):
                return

            offset_display = _safe_float(self.offset_tb.Text, 0.0)
            with _SafeProgress(title="DeeSheetLinks - creating sheets...", cancellable=True) as pb:
                def progress_cb(i, total, row):
                    pb.update_progress(i, total)
                    return pb.cancelled
                result = core.create_sheets_and_views(
                    self.doc, ready, vft_id, titleblock_id,
                    offset_display=offset_display, unit_label=self._selected_offset_unit(),
                    param_names=self._selected_param_names(),
                    show_crop_boundary=bool(self.show_crop_cb.IsChecked),
                    progress_cb=progress_cb)

            report_rows = [BuildReportRow(r) for r in result.row_results]
            self.report_grid.ItemsSource = None
            self.report_grid.ItemsSource = report_rows

            self.status_tb.Text = "Created {0}, skipped {1}, out of {2} Ready row(s).".format(
                result.applied, result.skipped, len(ready))
            self._report(result, len(ready))
        self._guard(run)

    def _report(self, result, ready_count):
        html = '<h2 style="font-family:sans-serif;">DeeSheetLinks</h2>'
        if result.parameter_setup:
            parts = []
            for name, (ok, detail) in sorted(result.parameter_setup.items()):
                parts.append("{0}: {1}".format(name, detail if ok else "FAILED - " + detail))
            html += ('<div style="font-family:sans-serif;font-size:12px;">'
                     '<b>View parameters:</b> ' + "; ".join(parts) + "</div>")
        bg = "#2e7d32" if not result.errors else "#8d6e19"
        html += ('<div style="margin-top:8px;padding:7px 11px;background:{0};color:#fff;'
                 'border-radius:4px;font-family:monospace;font-size:12px;">'
                 '{1} sheet(s) created, {2} skipped, out of {3} Ready row(s).</div>'.format(
                     bg, result.applied, result.skipped, ready_count))
        if result.errors:
            html += '<div style="font-family:sans-serif;font-size:11px;color:#a55;margin-top:6px;">'
            for e in result.errors[:12]:
                html += "&bull; {0}<br>".format(e)
            html += "</div>"
        skipped = [r for r in result.row_results if not r.ok]
        if skipped:
            html += ('<div style="font-family:sans-serif;font-size:11px;color:#a55;'
                     'margin-top:6px;">')
            for r in skipped[:20]:
                html += "&bull; {0} ({1}): {2}<br>".format(
                    r.sheet_number, r.sheet_type_name, r.message)
            html += "</div>"
        output.print_html(html)

    def close_click(self, sender, args):
        self.Close()


def main():
    uiapp = __revit__
    uidoc = uiapp.ActiveUIDocument
    if uidoc is None:
        forms.alert("Open a Revit project first.", title=_TOOL)
        return
    doc = uidoc.Document
    if doc.IsFamilyDocument:
        forms.alert("DeeSheetLinks works on a project document. It cannot be run inside the "
                    "Family Editor.", title=_TOOL)
        return

    window = DeeSheetLinksWindow(_XAML_FILE, doc)
    window.ShowDialog()


main()
