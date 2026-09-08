# -*- coding: utf-8 -*-
"""
DeeRoomStamp
Scans every real model element in the project, lets the user pick one
text parameter by name, and - for every element that has that
parameter - writes the name of the Room the element is physically
located in. All scan/matching/apply logic lives in
lib/dee_room_stamp_service.py; this file only wires the WPF window to
it (matching this stack's own DeeRoomXYD.pushbutton/script.py shape).
"""
import os

from pyrevit import forms
import dee_branding

import dee_room_stamp_service as core

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")


class _SafeProgress(object):
    """forms.ProgressBar tries to set Window.TaskbarItemInfo on its host
    window - a genuine WPF-level bug (not this extension's code):
    Window.TaskbarItemInfo throws NotImplementedException whenever the
    underlying ITaskbarList::HrInit COM call fails, which is documented
    to happen specifically under Remote Desktop/Terminal Services or a
    custom shell without a taskbar (live-confirmed in DeeSheetLinks).

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


class DeeRoomStampWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, doc, uidoc):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.doc = doc
        self.uidoc = uidoc
        self._rows = []
        self._param_universe = []
        self._cached_elements = []
        self._room_index = None

        with _SafeProgress(title="DeeRoomStamp - scanning project elements...", cancellable=True) as pb:
            self._param_universe, self._cached_elements = core.collect_string_param_universe_and_elements(
                self.doc, self._progress_cb(pb))
        with _SafeProgress(title="DeeRoomStamp - indexing rooms...", indeterminate=True):
            self._room_index = core.build_room_index(self.doc)

        self.param_suggestions_lb.ItemsSource = self._param_universe
        self.preview_grid.ItemsSource = self._rows
        self.status_tb.Text = "{0} element(s) scanned, {1} distinct text parameter name(s) found.".format(
            len(self._cached_elements), len(self._param_universe))

    def _progress_cb(self, pb):
        def cb(i, total):
            if i % 200 == 0 or i == total - 1:
                try:
                    pb.update_progress(i, total)
                except Exception:
                    pass
            return pb.cancelled
        return cb

    # ---------------- parameter picker ----------------
    def param_search_changed(self, sender, args):
        filt = self.param_search_tb.Text.lower()
        if filt:
            self.param_suggestions_lb.ItemsSource = [n for n in self._param_universe if filt in n.lower()]
        else:
            self.param_suggestions_lb.ItemsSource = self._param_universe

    def param_suggestion_selected(self, sender, args):
        picked = self.param_suggestions_lb.SelectedItem
        if picked:
            self.param_search_tb.Text = picked

    # ---------------- scan ----------------
    def scan_click(self, sender, args):
        param_name = (self.param_search_tb.Text or "").strip()
        if not param_name:
            forms.alert("Type or pick a parameter name first.")
            return

        with _SafeProgress(title="DeeRoomStamp - matching elements to rooms...", cancellable=True) as pb:
            self._rows = core.scan_for_parameter(
                param_name, self._cached_elements, self._room_index, self._progress_cb(pb))

        self.preview_grid.ItemsSource = None
        self.preview_grid.ItemsSource = self._rows
        will_update = sum(1 for r in self._rows if r.status_key == "will_update")
        self.status_tb.Text = "{0} row(s) found for '{1}' - {2} will update.".format(
            len(self._rows), param_name, will_update)

    # ---------------- selection ----------------
    def check_all_click(self, sender, args):
        for r in self._rows:
            r.checked = True
        self.preview_grid.Items.Refresh()

    def check_none_click(self, sender, args):
        for r in self._rows:
            r.checked = False
        self.preview_grid.Items.Refresh()

    # ---------------- apply ----------------
    def apply_click(self, sender, args):
        param_name = (self.param_search_tb.Text or "").strip()
        selected = [r for r in self._rows if r.checked]
        if not param_name or not selected:
            forms.alert("Scan a parameter and check at least one row first.")
            return

        if not forms.alert(
                "Set '{0}' on {1} element(s) to their found Room name?".format(param_name, len(selected)),
                title="DeeRoomStamp - Confirm", yes=True, no=True):
            return

        with _SafeProgress(title="DeeRoomStamp - applying...", indeterminate=True):
            result = core.apply_rows(self.doc, selected, param_name)
            view_name = None
            view_error = None
            if result.updated_ids:
                try:
                    view_name = core.isolate_updated_elements(self.doc, result.updated_ids)
                except Exception as e:
                    view_error = str(e)

        core.print_report(result, view_name=view_name, view_error=view_error)
        self.status_tb.Text = "Applied: {0} updated, {1} skipped. See the pyRevit output window for details.".format(
            result.ok_count, len(result.skipped))

    def close_click(self, sender, args):
        self.Close()


uiapp = __revit__
if uiapp.ActiveUIDocument is None:
    forms.alert("Open a Revit project first.")
else:
    window = DeeRoomStampWindow(_XAML_FILE, uiapp.ActiveUIDocument.Document, uiapp.ActiveUIDocument)
    window.ShowDialog()
