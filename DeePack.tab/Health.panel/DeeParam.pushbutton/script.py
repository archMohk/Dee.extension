# -*- coding: utf-8 -*-
"""
DeeParam (Health panel)
One window that audits every shared and project parameter in the active
model and lets the user act on them - the standalone parameter tool
this extension never had. Before it, the pieces were scattered:
DeeParaValue (DeeLazy) lists bindings but only across a batch of files
and only the ones common to all of them; DeeTransmit deletes bindings
but only as one step of a transmit run; four tools create/bind shared
parameters for their own needs; nothing at all saw an ORPHANED shared
parameter (loaded into the model, bound to nothing) or compared the
model against the shared parameter file.

Every Revit call is in lib/dee_param_service.py; this file is the
window only (DeeCleaner / DeeLinkDist shape: DeeBrandedWindow, flat row
view-models, _guard around every handler, SafeProgress for the scan).

--------------------------------------------------------------------
Three groups of actions, all on the one grid
--------------------------------------------------------------------
Scan        - lists bound parameters (Shared / Project, Instance / Type,
              categories, storage, data type, group, GUID, element /
              used / distinct counts) plus unbound orphans. Usage is
              counted over at most N elements per parameter (the probe
              box) so a big model stays scannable - the Used column
              says "x of y probed" when it hit the cap.
Delete      - Remove Binding (checked) and Delete Orphaned Shared
              (checked). Both run the service's dry run first, show the
              result, and ask before the real transaction.
Rebind      - Load Shared Parameter File fills the File Status column
              (In file / Not in file / Name differs). Extend Checked
              adds the selected categories to an existing binding,
              keeping its Instance/Type kind. Bind Checked Orphans and
              Bind File-Only Parameters create a new binding from the
              file's definition, Instance or Type per the radio.

Row colours: orange = orphan, red = shared but not in the file,
yellow = name differs from the file, grey = bound but unused.

Rules kept from the rest of the codebase: no second WPF window and no
PickObject from inside this one (native Save dialog and pyRevit's
SelectFromList only); nothing holds a Definition / ParameterElement
across a write - the service re-fetches by name or GUID each time
(DeeTransmit's documented crash, see dee_transmit_service).
"""
import os
import traceback

import clr
clr.AddReference("System.Windows.Forms")
clr.AddReference("PresentationFramework")
from System.Windows.Forms import SaveFileDialog, DialogResult
from System.Windows.Data import CollectionViewSource, PropertyGroupDescription

from pyrevit import forms, script

import dee_branding
import dee_telemetry
from dee_ui import SafeProgress as _SafeProgress, require_doc
import dee_param_service as core
dee_telemetry.check_access("DeeParam")


output = script.get_output()

_TOOL = "DeeParam"
_XAML_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.xaml")

_GROUP_FIELDS = {
    "Kind": "kind",
    "Scope": "scope",
    "Group": "group",
    "File status": "file_status_text",
}


class ParamRow(object):
    """Flat view-model over one service info dict."""
    def __init__(self, info):
        self.info = info
        self.selected = False
        self.name = info["name"]
        self.kind = info["kind"]
        self.scope = info["scope"]
        self.categories_text = info["categories_text"]
        self.storage = info["storage"]
        self.datatype = info["datatype"]
        self.group = info["group"]
        self.guid = info["guid"]
        self.elements = info["elements"]
        self.distinct = info["distinct"]
        self.is_bound = info["is_bound"]
        self.is_shared = info["is_shared"]
        self.is_unused = bool(info["is_bound"] and info["elements"] > 0 and info["used"] == 0)
        if not info["is_bound"]:
            self.used_text = ""
        elif info["probed"] < info["elements"]:
            self.used_text = "{0} of {1} probed".format(info["used"], info["probed"])
        else:
            self.used_text = "{0}".format(info["used"])
        self.refresh_file_status()

    def refresh_file_status(self):
        info = self.info
        self.file_status = info.get("file_status") or ""
        if self.file_status == core.FILE_NAME_DIFFERS:
            self.file_status_text = "Name differs (file: {0})".format(info.get("file_name", ""))
        else:
            self.file_status_text = self.file_status
        if not self.is_bound:
            self.highlight = "orphan"
        elif self.file_status == core.FILE_MISSING:
            self.highlight = "missing"
        elif self.file_status == core.FILE_NAME_DIFFERS:
            self.highlight = "differs"
        elif self.is_unused:
            self.highlight = "unused"
        else:
            self.highlight = ""


class DeeParamWindow(dee_branding.DeeBrandedWindow):
    _ready = False

    def __init__(self, xaml_file, doc, app):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.doc = doc
        self.app = app
        self._rows = []
        self._shown = []
        self._file_defs = None
        self._categories = core.bindable_categories(doc)
        self._fill_categories("")
        self._ready = True

    # ---------------- helpers ----------------
    def _guard(self, fn, *args):
        try:
            return fn(*args)
        except Exception as e:
            self.status_tb.Text = "ERROR: {0}".format(e)
            forms.alert("DeeParam hit an error:\n\n{0}\n\n{1}".format(
                e, traceback.format_exc()[-900:]), title=_TOOL)

    def _log(self, text):
        self.log_tb.AppendText(text + "\n")
        self.log_tb.ScrollToEnd()

    def _log_results(self, title, results):
        ok_count = sum(1 for _k, ok, _d in results if ok)
        self._log("{0}: {1} ok, {2} failed".format(title, ok_count, len(results) - ok_count))
        for key, ok, detail in results:
            self._log("  {0} {1} - {2}".format("OK " if ok else "ERR", key, detail))

    def _probe_limit(self):
        try:
            v = int((self.probe_tb.Text or "").strip())
            return max(1, v)
        except Exception:
            return core.DEFAULT_PROBE_LIMIT

    def _checked(self):
        return [r for r in self._shown if r.selected]

    def _selected_category_ids(self):
        ids = []
        for item in self.cat_lb.SelectedItems:
            try:
                ids.append(self._cat_ids_by_name[str(item)])
            except Exception:
                continue
        return ids

    def _fill_categories(self, filter_text):
        self._cat_ids_by_name = dict((n, i) for n, i in self._categories)
        self.cat_lb.Items.Clear()
        ft = (filter_text or "").strip().lower()
        for name, _id in self._categories:
            if not ft or ft in name.lower():
                self.cat_lb.Items.Add(name)

    def cat_filter_changed(self, sender, args):
        if not self._ready:
            return
        self._guard(self._fill_categories, self.cat_filter_tb.Text)

    def _combo_text(self, combo):
        try:
            item = combo.SelectedItem
            return str(item.Content) if item is not None else ""
        except Exception:
            return ""

    # ---------------- scan / view ----------------
    def scan_click(self, sender, args):
        def run():
            limit = self._probe_limit()
            with _SafeProgress(title="DeeParam - scanning parameters...", indeterminate=True):
                infos = core.scan_parameters(self.doc, probe_limit=limit)
            if self._file_defs is not None:
                core.compare_to_file(infos, self._file_defs)
            self._rows = [ParamRow(i) for i in infos]
            bound = sum(1 for r in self._rows if r.is_bound)
            orphans = len(self._rows) - bound
            unused = sum(1 for r in self._rows if r.is_unused)
            self.status_tb.Text = "{0} bound parameter(s), {1} orphan(s), {2} unused.".format(
                bound, orphans, unused)
            self._log("Scanned: {0} bound, {1} orphaned, {2} unused (probe limit {3}).".format(
                bound, orphans, unused, limit))
            self._apply_view()
        self._guard(run)

    def _apply_view(self):
        ft = (self.filter_tb.Text or "").strip().lower()
        kind = self._combo_text(self.kind_cb)
        shown = []
        for r in self._rows:
            if kind == "Shared" and not (r.is_shared and r.is_bound):
                continue
            if kind == "Project" and (r.is_shared or not r.is_bound):
                continue
            if kind == "Unbound" and r.is_bound:
                continue
            if kind == "Unused" and not r.is_unused:
                continue
            if ft and not (ft in r.name.lower() or ft in r.categories_text.lower()
                           or ft in r.guid.lower() or ft in r.group.lower()):
                continue
            shown.append(r)
        self._shown = shown
        self.grid.ItemsSource = None
        self.grid.ItemsSource = shown
        try:
            view = CollectionViewSource.GetDefaultView(self.grid.ItemsSource)
            if view is not None:
                view.GroupDescriptions.Clear()
                field = _GROUP_FIELDS.get(self._combo_text(self.group_cb))
                if field:
                    view.GroupDescriptions.Add(PropertyGroupDescription(field))
                view.Refresh()
        except Exception:
            pass

    def _refresh_grid(self):
        self.grid.ItemsSource = None
        self.grid.ItemsSource = self._shown
        self._apply_view()

    def filter_changed(self, sender, args):
        if not self._ready:
            return
        self._guard(self._apply_view)

    def grid_double_click(self, sender, args):
        def run():
            row = self.grid.SelectedItem
            if row is None:
                return
            row.selected = not row.selected
            self._refresh_grid()
        self._guard(run)

    # ---------------- checks ----------------
    def check_all_click(self, sender, args):
        for r in self._shown:
            r.selected = True
        self._refresh_grid()

    def check_none_click(self, sender, args):
        for r in self._rows:
            r.selected = False
        self._refresh_grid()

    def check_unused_click(self, sender, args):
        for r in self._shown:
            r.selected = r.is_unused
        self._refresh_grid()

    def check_orphans_click(self, sender, args):
        for r in self._shown:
            r.selected = not r.is_bound
        self._refresh_grid()

    # ---------------- delete / purge ----------------
    def _preview_then_run(self, title, preview, commit, warning):
        """Dry run -> show -> confirm -> real run -> log. Returns the
        real results or None when cancelled."""
        results = preview()
        self._log_results(title + " (preview)", results)
        would = [k for k, ok, _d in results if ok]
        if not would:
            forms.alert("Nothing to do - see the log for why.", title=_TOOL)
            return None
        names = "\n".join(would[:20]) + ("\n..." if len(would) > 20 else "")
        if not forms.alert(
                "{0}\n\n{1} item(s):\n{2}\n\nRun it now?".format(warning, len(would), names),
                title="DeeParam - Confirm", yes=True, no=True):
            self._log("Cancelled.")
            return None
        with _SafeProgress(title="DeeParam - {0}...".format(title), indeterminate=True):
            real = commit()
        self._log_results(title, real)
        return real

    def remove_binding_click(self, sender, args):
        def run():
            names = [r.name for r in self._checked() if r.is_bound]
            if not names:
                forms.alert("Check at least one bound parameter.", title=_TOOL)
                return
            real = self._preview_then_run(
                "Remove bindings",
                lambda: core.remove_bindings(self.doc, names, dry_run=True),
                lambda: core.remove_bindings(self.doc, names, dry_run=False),
                "Removing a binding discards every value stored in that parameter.")
            if real is not None:
                self.status_tb.Text = "Bindings removed. Scan again to refresh."
        self._guard(run)

    def delete_orphans_click(self, sender, args):
        def run():
            guids = [r.guid for r in self._checked() if not r.is_bound and r.is_shared and r.guid]
            if not guids:
                forms.alert("Check at least one orphaned shared parameter.", title=_TOOL)
                return
            real = self._preview_then_run(
                "Delete orphaned shared parameters",
                lambda: core.delete_orphaned_shared(self.doc, guids, dry_run=True),
                lambda: core.delete_orphaned_shared(self.doc, guids, dry_run=False),
                "Deleting a shared parameter element removes it from the model. "
                "It can be reloaded from the shared parameter file later.")
            if real is not None:
                self.status_tb.Text = "Orphans deleted. Scan again to refresh."
        self._guard(run)

    # ---------------- rebind / repair ----------------
    def load_file_click(self, sender, args):
        def run():
            defs, detail = core.read_shared_param_file(self.app)
            self._file_defs = defs
            self.file_tb.Text = detail
            self._log("Shared parameter file: {0}".format(detail))
            if defs is None:
                forms.alert(detail, title=_TOOL)
                return
            if self._rows:
                core.compare_to_file([r.info for r in self._rows], defs)
                for r in self._rows:
                    r.refresh_file_status()
                self._refresh_grid()
        self._guard(run)

    def _as_type(self):
        try:
            return bool(self.type_rb.IsChecked)
        except Exception:
            return False

    def extend_click(self, sender, args):
        def run():
            names = [r.name for r in self._checked() if r.is_bound]
            cat_ids = self._selected_category_ids()
            if not names:
                forms.alert("Check at least one bound parameter.", title=_TOOL)
                return
            if not cat_ids:
                forms.alert("Select at least one category in the list.", title=_TOOL)
                return
            real = self._preview_then_run(
                "Extend bindings",
                lambda: core.extend_bindings(self.doc, names, cat_ids, dry_run=True),
                lambda: core.extend_bindings(self.doc, names, cat_ids, dry_run=False),
                "Each checked parameter's binding will also cover the selected categories.")
            if real is not None:
                self.status_tb.Text = "Bindings extended. Scan again to refresh."
        self._guard(run)

    def _bind_guids(self, guids):
        cat_ids = self._selected_category_ids()
        if not cat_ids:
            forms.alert("Select at least one category in the list.", title=_TOOL)
            return
        as_type = self._as_type()
        real = self._preview_then_run(
            "Bind from shared parameter file",
            lambda: core.bind_from_file(self.doc, self.app, guids, cat_ids, as_type, dry_run=True),
            lambda: core.bind_from_file(self.doc, self.app, guids, cat_ids, as_type, dry_run=False),
            "Each parameter will be bound to the selected categories as {0} parameters.".format(
                "type" if as_type else "instance"))
        if real is not None:
            self.status_tb.Text = "Parameters bound. Scan again to refresh."

    def bind_file_click(self, sender, args):
        def run():
            if self._file_defs is None:
                forms.alert("Load the shared parameter file first.", title=_TOOL)
                return
            guids = [r.guid for r in self._checked() if not r.is_bound and r.is_shared and r.guid]
            if not guids:
                forms.alert("Check at least one unbound shared parameter.", title=_TOOL)
                return
            self._bind_guids(guids)
        self._guard(run)

    def bind_missing_click(self, sender, args):
        def run():
            if self._file_defs is None:
                forms.alert("Load the shared parameter file first.", title=_TOOL)
                return
            if not self._selected_category_ids():
                forms.alert("Select at least one category in the list first.", title=_TOOL)
                return
            entries = core.file_only_definitions([r.info for r in self._rows], self._file_defs)
            if not entries:
                forms.alert("Every parameter in the shared parameter file is already in this model.",
                            title=_TOOL)
                return
            labels = ["{0}  [{1}]  {2}".format(e["name"], e["group"], e["datatype"]) for e in entries]
            by_label = dict(zip(labels, entries))
            picked = forms.SelectFromList.show(
                labels, title="DeeParam - parameters in the file but not in the model",
                multiselect=True, button_name="Bind Selected")
            if not picked:
                return
            guids = [by_label[p]["guid"] for p in picked if p in by_label]
            self._bind_guids(guids)
        self._guard(run)

    # ---------------- export ----------------
    def export_click(self, sender, args):
        def run():
            if not self._rows:
                forms.alert("Scan first.", title=_TOOL)
                return
            dlg = SaveFileDialog()
            dlg.Filter = "Excel Workbook (*.xlsx)|*.xlsx"
            dlg.FileName = "DeeParam_Audit.xlsx"
            if dlg.ShowDialog() != DialogResult.OK:
                return
            count = core.export_xlsx(dlg.FileName, [r.info for r in self._shown])
            self._log("Exported {0} row(s) to {1}".format(count, dlg.FileName))
            self.status_tb.Text = "Exported {0} row(s).".format(count)
        self._guard(run)

    def close_click(self, sender, args):
        self.Close()


def main():
    doc = require_doc(_TOOL, __revit__, allow_family=False)
    window = DeeParamWindow(_XAML_FILE, doc, __revit__.Application)
    window.ShowDialog()


main()
