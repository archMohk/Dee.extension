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
clr.AddReference("WindowsBase")
import System
from System.Windows import Thickness, VerticalAlignment, FontStyles, Visibility
from System.Windows.Controls import (
    StackPanel, TextBlock, TextBox, ComboBox, Button, CheckBox, Orientation,
)
from System.Windows.Threading import DispatcherPriority

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


def _safe_scale(text):
    """Blank/invalid/zero -> None (auto-fit); a positive integer -> a
    fixed scale denominator (1:N)."""
    try:
        v = int(str(text).strip())
        return v if v > 0 else None
    except Exception:
        return None


def _safe_range_value(text):
    """Blank/invalid -> None (leave that View Range plane untouched);
    a number -> that offset (can be negative, e.g. a Bottom clip plane
    below its Level)."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except Exception:
        return None


class _InlineProgress(object):
    """A progress bar EMBEDDED in this window's own layout, never a
    second window/dialog - forms.ProgressBar (pyRevit's own) tries to
    set Window.TaskbarItemInfo on its host, which throws
    NotImplementedException under Remote Desktop/Terminal Services or a
    custom shell without a taskbar (live-confirmed on this exact error
    from two different DeeSheetLinks actions earlier), so it showed NO
    progress feedback at all in that environment - not a crash any
    more (that was already fixed), just silently nothing to look at.
    This sidesteps the whole problem: it is a plain <ProgressBar>
    control already living in ui.xaml, updated directly, so it never
    touches TaskbarItemInfo or opens anything new.

    WPF does not repaint mid-loop on its own - a long synchronous
    Python loop blocks the same thread WPF would use to redraw, so
    every update() call pumps the Dispatcher's Background-priority
    queue (the same mechanism forms.ProgressBar itself relies on
    internally) to force the bar/text to actually appear before the
    loop continues."""
    def __init__(self, window, cancellable=False):
        self._window = window
        self._cancellable = cancellable

    def start(self, status, indeterminate=True, maximum=100):
        w = self._window
        w.progress_bar.IsIndeterminate = indeterminate
        w.progress_bar.Minimum = 0
        w.progress_bar.Maximum = maximum if maximum > 0 else 1
        w.progress_bar.Value = 0
        w.cancel_b.Visibility = Visibility.Visible if self._cancellable else Visibility.Collapsed
        w.progress_row.Visibility = Visibility.Visible
        w._cancel_requested = False
        w.status_tb.Text = status
        self._pump()

    def update(self, value, status=None):
        w = self._window
        try:
            w.progress_bar.Value = value
        except Exception:
            pass
        if status is not None:
            w.status_tb.Text = status
        self._pump()
        return self._cancellable and w._cancel_requested

    def stop(self):
        w = self._window
        w.progress_row.Visibility = Visibility.Collapsed
        self._pump()

    def _pump(self):
        try:
            self._window.Dispatcher.Invoke(
                System.Action(lambda: None), DispatcherPriority.Background)
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, tb):
        self.stop()
        return False


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
    def __init__(self, sheet_type, panel, name_tb, level_cb, template_cb,
                 include_view_cb, scale_tb, view_family_cb,
                 range_top_tb, range_cut_tb, range_bottom_tb, range_depth_tb):
        self.sheet_type = sheet_type
        self.panel = panel
        self.name_tb = name_tb
        self.level_cb = level_cb
        self.template_cb = template_cb
        self.include_view_cb = include_view_cb
        self.scale_tb = scale_tb
        self.view_family_cb = view_family_cb
        self.range_top_tb = range_top_tb
        self.range_cut_tb = range_cut_tb
        self.range_bottom_tb = range_bottom_tb
        self.range_depth_tb = range_depth_tb


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
        self._view_family_types = []
        self._cancel_requested = False

        for label, _unit_type_id in core.UNIT_OPTIONS:
            self.offset_unit_cb.Items.Add(label)
        self.offset_unit_cb.SelectedIndex = [l for l, _u in core.UNIT_OPTIONS].index(
            core.DEFAULT_UNIT_LABEL)

        self._levels = core.list_levels(self.doc)
        self._templates = core.list_view_templates(self.doc)
        self._view_family_types = core.list_floor_plan_view_family_types(self.doc)
        self._refresh_titleblocks()
        self._refresh_naming_presets()
        self._active_naming_tb = self.name_template_tb

        self._ready = True
        self._guard(self._scan_links)

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
    def _scan_links(self):
        """_InlineProgress is a plain control on THIS window's own
        already-constructed layout (not a second window), so - unlike
        the old forms.ProgressBar-based approach - it is safe to use
        even from __init__, before ShowDialog() has actually shown the
        window."""
        progress = _InlineProgress(self)
        with progress:
            progress.start("Scanning links...")
            self._link_rows = core.list_link_instances(self.doc, self._selected_param_names())
        self._apply_default_selection()
        self.links_grid.ItemsSource = None
        self.links_grid.ItemsSource = self._link_rows
        with_data = sum(1 for r in self._link_rows if r.has_typology)
        self.link_status_tb.Text = "{0} link(s) found, {1} with Typology data.".format(
            len(self._link_rows), with_data)
        self.status_tb.Text = self.link_status_tb.Text

    def scan_links_click(self, sender, args):
        self._guard(self._scan_links)

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

    def _view_family_type_names(self):
        return [name for _id, name in self._view_family_types] or ["(none found)"]

    def add_sheet_type_click(self, sender, args):
        def run():
            sheet_type = core.SheetType(name="Sheet Type {0}".format(len(self._sheet_type_rows) + 1))
            if self._levels:
                sheet_type.level_id, sheet_type.level_name = self._levels[0]
            self._add_sheet_type_row(sheet_type)
            self._update_sheet_type_status()
        self._guard(run)

    def _range_field(self, value):
        tb = TextBox()
        tb.Width = 55
        tb.Height = 24
        tb.Margin = Thickness(4, 0, 0, 0)
        tb.VerticalContentAlignment = VerticalAlignment.Center
        tb.Text = "" if value is None else str(value)
        return tb

    def _add_sheet_type_row(self, sheet_type):
        """Three lines per Sheet Type (name/level/remove, then view
        type/template/include-view/scale, then View Range offsets) -
        keeps every control readable at the window's own width rather
        than one very long horizontal row."""
        outer = StackPanel()
        outer.Orientation = Orientation.Vertical
        outer.Margin = Thickness(0, 0, 0, 12)

        row1 = StackPanel()
        row1.Orientation = Orientation.Horizontal
        row1.Margin = Thickness(0, 0, 0, 4)

        name_tb = TextBox()
        name_tb.Text = sheet_type.name
        name_tb.Width = 220
        name_tb.Height = 26
        name_tb.VerticalContentAlignment = VerticalAlignment.Center
        row1.Children.Add(name_tb)

        level_label = TextBlock()
        level_label.Text = "  Level:"
        level_label.VerticalAlignment = VerticalAlignment.Center
        row1.Children.Add(level_label)

        level_cb = ComboBox()
        level_cb.Width = 150
        level_cb.Height = 26
        level_cb.Margin = Thickness(4, 0, 0, 0)
        for name in self._level_names():
            level_cb.Items.Add(name)
        level_cb.SelectedIndex = 0
        row1.Children.Add(level_cb)

        remove_b = Button()
        remove_b.Content = "Remove"
        remove_b.Width = 70
        remove_b.Height = 26
        remove_b.Margin = Thickness(10, 0, 0, 0)
        row1.Children.Add(remove_b)

        row2 = StackPanel()
        row2.Orientation = Orientation.Horizontal

        view_family_label = TextBlock()
        view_family_label.Text = "View Type:"
        view_family_label.Width = 100
        view_family_label.VerticalAlignment = VerticalAlignment.Center
        row2.Children.Add(view_family_label)

        view_family_cb = ComboBox()
        view_family_cb.Width = 160
        view_family_cb.Height = 26
        view_family_cb.ToolTip = "Which Floor Plan ViewFamilyType this Sheet Type's views use."
        for name in self._view_family_type_names():
            view_family_cb.Items.Add(name)
        view_family_cb.SelectedIndex = 0
        row2.Children.Add(view_family_cb)

        new_type_label = TextBlock()
        new_type_label.Text = "  New type name:"
        new_type_label.VerticalAlignment = VerticalAlignment.Center
        row2.Children.Add(new_type_label)

        new_type_name_tb = TextBox()
        new_type_name_tb.Width = 130
        new_type_name_tb.Height = 24
        new_type_name_tb.Margin = Thickness(4, 0, 0, 0)
        new_type_name_tb.VerticalContentAlignment = VerticalAlignment.Center
        new_type_name_tb.ToolTip = "Type a name, then click Duplicate - creates a NEW View Type " \
                                   "from a copy of whatever is picked above and selects it here."
        row2.Children.Add(new_type_name_tb)

        new_type_b = Button()
        new_type_b.Content = "Duplicate"
        new_type_b.Width = 80
        new_type_b.Height = 24
        new_type_b.Margin = Thickness(4, 0, 0, 0)
        row2.Children.Add(new_type_b)

        row2b = StackPanel()
        row2b.Orientation = Orientation.Horizontal
        row2b.Margin = Thickness(0, 4, 0, 0)

        template_label = TextBlock()
        template_label.Text = "View Template:"
        template_label.Width = 100
        template_label.VerticalAlignment = VerticalAlignment.Center
        row2b.Children.Add(template_label)

        template_cb = ComboBox()
        template_cb.Width = 180
        template_cb.Height = 26
        for name in self._template_names():
            template_cb.Items.Add(name)
        template_cb.SelectedIndex = 0
        row2b.Children.Add(template_cb)

        include_view_cb = CheckBox()
        include_view_cb.Content = "Create a View (uncheck for sheet only, no view)"
        include_view_cb.IsChecked = sheet_type.include_view
        include_view_cb.VerticalAlignment = VerticalAlignment.Center
        include_view_cb.Margin = Thickness(24, 0, 0, 0)
        include_view_cb.ToolTip = ("Unchecked: this Sheet Type creates a plain Sheet only - "
                                   "no cropped view, no crop, no view parameters at all.")
        row2b.Children.Add(include_view_cb)

        scale_label = TextBlock()
        scale_label.Text = "  Scale 1:"
        scale_label.VerticalAlignment = VerticalAlignment.Center
        scale_label.Margin = Thickness(24, 0, 0, 0)
        row2b.Children.Add(scale_label)

        scale_tb = TextBox()
        scale_tb.Width = 60
        scale_tb.Height = 24
        scale_tb.Margin = Thickness(4, 0, 0, 0)
        scale_tb.VerticalContentAlignment = VerticalAlignment.Center
        scale_tb.Text = str(sheet_type.fixed_scale) if sheet_type.fixed_scale else ""
        scale_tb.ToolTip = "Blank = auto-fit the view to the sheet"
        row2b.Children.Add(scale_tb)

        row3 = StackPanel()
        row3.Orientation = Orientation.Horizontal
        row3.Margin = Thickness(0, 4, 0, 0)

        range_label = TextBlock()
        range_label.Text = "View Range (blank = leave as-is) - Top:"
        range_label.VerticalAlignment = VerticalAlignment.Center
        row3.Children.Add(range_label)
        range_top_tb = self._range_field(sheet_type.view_range_top)
        row3.Children.Add(range_top_tb)

        cut_label = TextBlock()
        cut_label.Text = "  Cut Plane:"
        cut_label.VerticalAlignment = VerticalAlignment.Center
        row3.Children.Add(cut_label)
        range_cut_tb = self._range_field(sheet_type.view_range_cut)
        row3.Children.Add(range_cut_tb)

        bottom_label = TextBlock()
        bottom_label.Text = "  Bottom:"
        bottom_label.VerticalAlignment = VerticalAlignment.Center
        row3.Children.Add(bottom_label)
        range_bottom_tb = self._range_field(sheet_type.view_range_bottom)
        row3.Children.Add(range_bottom_tb)

        depth_label = TextBlock()
        depth_label.Text = "  View Depth:"
        depth_label.VerticalAlignment = VerticalAlignment.Center
        row3.Children.Add(depth_label)
        range_depth_tb = self._range_field(sheet_type.view_range_depth)
        row3.Children.Add(range_depth_tb)

        range_unit_tb = TextBlock()
        range_unit_tb.Text = "  (offset from this row's own Level, in the Crop offset unit below)"
        range_unit_tb.VerticalAlignment = VerticalAlignment.Center
        range_unit_tb.FontStyle = FontStyles.Italic
        row3.Children.Add(range_unit_tb)

        outer.Children.Add(row1)
        outer.Children.Add(row2)
        outer.Children.Add(row2b)
        outer.Children.Add(row3)

        ui_row = SheetTypeUIRow(sheet_type, outer, name_tb, level_cb, template_cb,
                                include_view_cb, scale_tb, view_family_cb,
                                range_top_tb, range_cut_tb, range_bottom_tb, range_depth_tb)
        remove_b.Click += self._make_remove_handler(ui_row)
        new_type_b.Click += self._make_duplicate_view_type_handler(ui_row, new_type_name_tb)

        self._sheet_type_rows.append(ui_row)
        self.sheet_types_panel.Children.Add(outer)

    def _make_remove_handler(self, ui_row):
        def handler(sender, args):
            self.sheet_types_panel.Children.Remove(ui_row.panel)
            self._sheet_type_rows.remove(ui_row)
            self._update_sheet_type_status()
        return handler

    def _make_duplicate_view_type_handler(self, ui_row, new_type_name_tb):
        def handler(sender, args):
            def run():
                source_i = ui_row.view_family_cb.SelectedIndex
                if not (0 <= source_i < len(self._view_family_types)):
                    forms.alert("Pick a View Type to duplicate from first.", title=_TOOL)
                    return
                source_id = self._view_family_types[source_i][0]
                new_name = (new_type_name_tb.Text or "").strip()
                new_id, detail = core.duplicate_view_family_type(self.doc, source_id, new_name)
                if new_id is None:
                    forms.alert("Could not duplicate the View Type: {0}".format(detail), title=_TOOL)
                    return
                # every OTHER row's ComboBox is about to be cleared and
                # rebuilt too (the new type must appear everywhere, not
                # just on the row that created it) - capture each row's
                # currently-selected ElementId first so it can be
                # re-selected by id afterwards, since Items.Clear()
                # resets SelectedIndex to -1.
                previous_ids = {}
                old_types = list(self._view_family_types)
                for row in self._sheet_type_rows:
                    i = row.view_family_cb.SelectedIndex
                    previous_ids[row] = old_types[i][0] if 0 <= i < len(old_types) else None

                self._view_family_types.append((new_id, new_name))
                self._view_family_types.sort(key=lambda t: t[1].lower())
                names = self._view_family_type_names()
                for row in self._sheet_type_rows:
                    row.view_family_cb.Items.Clear()
                    for name in names:
                        row.view_family_cb.Items.Add(name)
                    target_id = new_id if row is ui_row else previous_ids.get(row)
                    match = [i for i, (eid, _n) in enumerate(self._view_family_types)
                             if eid == target_id]
                    row.view_family_cb.SelectedIndex = match[0] if match else 0
                new_type_name_tb.Text = ""
                self.status_tb.Text = "Duplicated View Type '{0}'.".format(new_name)
            self._guard(run)
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
            st.include_view = bool(ui_row.include_view_cb.IsChecked)
            st.fixed_scale = _safe_scale(ui_row.scale_tb.Text)
            vft_i = ui_row.view_family_cb.SelectedIndex
            if 0 <= vft_i < len(self._view_family_types):
                st.view_family_type_id, st.view_family_type_name = self._view_family_types[vft_i]
            st.view_range_top = _safe_range_value(ui_row.range_top_tb.Text)
            st.view_range_cut = _safe_range_value(ui_row.range_cut_tb.Text)
            st.view_range_bottom = _safe_range_value(ui_row.range_bottom_tb.Text)
            st.view_range_depth = _safe_range_value(ui_row.range_depth_tb.Text)

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

    # ---------------- naming sets (presets) ----------------
    def _refresh_naming_presets(self, select_name=None):
        names = core.list_naming_presets()
        self.naming_preset_cb.ItemsSource = None
        self.naming_preset_cb.ItemsSource = names
        if select_name:
            self.naming_preset_cb.Text = select_name
        elif names:
            self.naming_preset_cb.SelectedIndex = 0

    def load_preset_click(self, sender, args):
        def run():
            name = (self.naming_preset_cb.Text or "").strip()
            if not name:
                forms.alert("Pick or type a Naming Set name first.", title=_TOOL)
                return
            preset = core.load_naming_preset(name)
            if preset is None:
                forms.alert("No Naming Set called '{0}'.".format(name), title=_TOOL)
                return
            self.number_template_tb.Text = preset.get("number_template", "")
            self.name_template_tb.Text = preset.get("name_template", "")
            if preset.get("view_name_template"):
                self.view_name_template_tb.Text = preset.get("view_name_template")
            self.status_tb.Text = "Loaded Naming Set '{0}'.".format(name)
        self._guard(run)

    def save_preset_click(self, sender, args):
        def run():
            name = (self.naming_preset_cb.Text or "").strip()
            if not name:
                forms.alert("Type a name for this Naming Set in the box first.", title=_TOOL)
                return
            core.save_naming_preset(name, self.number_template_tb.Text or "",
                                    self.name_template_tb.Text or "",
                                    self.view_name_template_tb.Text or "")
            self._refresh_naming_presets(select_name=name)
            self.status_tb.Text = "Saved Naming Set '{0}'.".format(name)
        self._guard(run)

    def delete_preset_click(self, sender, args):
        def run():
            name = (self.naming_preset_cb.Text or "").strip()
            if not name:
                return
            if not forms.alert("Delete the Naming Set '{0}'?".format(name), title=_TOOL,
                               yes=True, no=True):
                return
            core.delete_naming_preset(name)
            self.naming_preset_cb.Text = ""
            self._refresh_naming_presets()
            self.status_tb.Text = "Deleted Naming Set '{0}'.".format(name)
        self._guard(run)

    # ---------------- token insert buttons ----------------
    def naming_field_focused(self, sender, args):
        self._active_naming_tb = sender

    def insert_token_click(self, sender, args):
        tb = getattr(self, "_active_naming_tb", None) or self.name_template_tb
        token_text = "{" + str(sender.Tag) + "}"
        text = tb.Text or ""
        caret = tb.CaretIndex
        if caret < 0 or caret > len(text):
            caret = len(text)
        tb.Text = text[:caret] + token_text + text[caret:]
        tb.CaretIndex = caret + len(token_text)
        tb.Focus()

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
            view_name_template = (self.view_name_template_tb.Text or "").strip() or core.DEFAULT_VIEW_NAME_TEMPLATE
            existing_numbers = core.existing_sheet_numbers(self.doc)
            existing_view_names = core.existing_view_names(self.doc)
            progress = _InlineProgress(self)
            with progress:
                progress.start("Building preview...")
                self._plan = core.build_plan(selected_links, sheet_types, number_template,
                                             name_template, existing_numbers,
                                             view_name_template, existing_view_names)
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
            # the grid lets Sheet Number/Name/View Name be hand-edited
            # after Preview - re-check duplicate/blank/invalid against
            # whatever is CURRENTLY in the grid before trusting Ready.
            core.revalidate_plan(self._plan, core.existing_sheet_numbers(self.doc),
                                 core.existing_view_names(self.doc))
            self.plan_grid.Items.Refresh()
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
            progress = _InlineProgress(self, cancellable=True)
            with progress:
                progress.start("Creating sheets...", indeterminate=False, maximum=len(ready))

                def progress_cb(i, total, row):
                    return progress.update(i, "Creating {0}/{1}: {2}".format(
                        i + 1, total, row.sheet_number))
                result = core.create_sheets_and_views(
                    self.doc, ready, vft_id, titleblock_id,
                    offset_display=offset_display, unit_label=self._selected_offset_unit(),
                    param_names=self._selected_param_names(),
                    show_crop_boundary=bool(self.show_crop_cb.IsChecked),
                    active_view=self.doc.ActiveView,
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

    def cancel_click(self, sender, args):
        self._cancel_requested = True

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
