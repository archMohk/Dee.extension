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
Four tabs over ONE scan
--------------------------------------------------------------------
Scan Parameters (top bar) walks the model once; every tab is a view of
that same list of info dicts, so switching tabs never re-reads Revit.

Parameters  - the audit grid: bound parameters (Shared / Project,
              Instance / Type, categories, storage, data type, group,
              GUID, element / used / distinct counts) plus unbound
              orphans. Delete / Purge and Rebind / Repair live here.
By Category - pick a category, see the parameters bound to it, take
              checked ones OFF this category (ReInsert with the reduced
              CategorySet, or Remove when it was the last category), or
              add the parameters checked on the Parameters tab to it.
Validation  - a small dashboard: for every bound parameter, how many
              probed elements carry it, how many hold a value (filled)
              and how many do not (empty), as counts, a percentage and
              a two-colour bar; six summary tiles on top; Select Empty
              Elements in Revit hands the empties to the selection so
              they can be filled from the Properties palette.
Add Parameter - name, data type, group, Instance/Type, categories.
              Revit's API cannot create a NON-shared project parameter,
              so this writes the definition to the shared parameter
              file (group of the user's choosing) and binds it.

Row colours on the Parameters tab: orange = orphan, red = shared but
not in the file, yellow = name differs from the file, grey = bound but
unused.

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
from System.Windows import GridLength, GridUnitType
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


def _used_text(info):
    if not info["is_bound"]:
        return ""
    if info["probed"] < info["elements"]:
        return "{0} of {1} probed".format(info["used"], info["probed"])
    return "{0}".format(info["used"])


class ParamRow(object):
    """Flat view-model over one service info dict (Parameters tab)."""
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
        self.used_text = _used_text(info)
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


class CatRow(object):
    def __init__(self, name, id_int, count):
        self.name = name
        self.id_int = id_int
        self.count = count


class CatParamRow(object):
    """One parameter as seen from one category (By Category tab)."""
    def __init__(self, info, cat_name):
        self.info = info
        self.selected = False
        self.name = info["name"]
        self.kind = info["kind"]
        self.scope = info["scope"]
        self.datatype = info["datatype"]
        self.group = info["group"]
        self.used_text = _used_text(info)
        others = [c for c in info.get("categories") or [] if c != cat_name]
        self.other_categories_text = ", ".join(others) or "(only this category)"


class ValRow(object):
    """One parameter's fill state (Validation tab)."""
    def __init__(self, info, filled, empty, percent):
        self.info = info
        self.name = info["name"]
        self.kind = info["kind"]
        self.scope = info["scope"]
        self.categories_text = info["categories_text"]
        self.categories = info.get("categories") or []
        self.carrying = info.get("carrying", 0)
        self.filled = filled
        self.empty = empty
        self.percent = percent
        self.percent_text = "{0}%".format(percent)
        # Star-sized grid columns draw the bar; a zero star collapses
        # that half, so a never-filled parameter is all red.
        self.filled_star = GridLength(float(filled), GridUnitType.Star)
        self.empty_star = GridLength(float(empty), GridUnitType.Star)


class DeeParamWindow(dee_branding.DeeBrandedWindow):
    _ready = False

    def __init__(self, xaml_file, uidoc, app):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.uidoc = uidoc
        self.doc = uidoc.Document
        self.app = app
        self._rows = []
        self._shown = []
        self._file_defs = None
        self._cat_rows = []
        self._cat_param_rows = []
        self._current_cat = None
        self._val_rows = []
        self._categories = core.bindable_categories(self.doc)
        self._cat_ids_by_name = dict((n, i) for n, i in self._categories)
        self._fill_listbox(self.cat_lb, "")
        self._fill_listbox(self.add_cat_lb, "")
        self._type_opts = core.data_type_options()
        self._group_opts = core.group_options()
        for label, _spec in self._type_opts:
            self.add_type_cb.Items.Add(label)
        if self._type_opts:
            self.add_type_cb.SelectedIndex = 0
        for label, _gid in self._group_opts:
            self.add_group_cb.Items.Add(label)
        if self._group_opts:
            self.add_group_cb.SelectedIndex = 0
        self.val_cat_cb.Items.Add("(all)")
        self.val_cat_cb.SelectedIndex = 0
        self._refresh_cat_rows()
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

    def _infos(self):
        return [r.info for r in self._rows]

    def _fill_listbox(self, listbox, filter_text):
        listbox.Items.Clear()
        ft = (filter_text or "").strip().lower()
        for name, _id in self._categories:
            if not ft or ft in name.lower():
                listbox.Items.Add(name)

    def _listbox_ids(self, listbox):
        ids = []
        for item in listbox.SelectedItems:
            try:
                ids.append(self._cat_ids_by_name[str(item)])
            except Exception:
                continue
        return ids

    def _selected_category_ids(self):
        return self._listbox_ids(self.cat_lb)

    def cat_filter_changed(self, sender, args):
        if not self._ready:
            return
        self._guard(self._fill_listbox, self.cat_lb, self.cat_filter_tb.Text)

    def _combo_text(self, combo):
        try:
            item = combo.SelectedItem
            if item is None:
                return ""
            return str(getattr(item, "Content", item))
        except Exception:
            return ""

    def _after_change(self, note):
        self.status_tb.Text = note + " Scan again to refresh."

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
            self._refresh_cat_rows()
            self._refresh_validation()
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

    # ---------------- preview -> confirm -> run ----------------
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

    # ---------------- delete / purge ----------------
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
                self._after_change("Bindings removed.")
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
                self._after_change("Orphans deleted.")
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
                core.compare_to_file(self._infos(), defs)
                for r in self._rows:
                    r.refresh_file_status()
                self._refresh_grid()
        self._guard(run)

    def _as_type(self):
        try:
            return bool(self.type_rb.IsChecked)
        except Exception:
            return False

    def _extend(self, names, cat_ids, note):
        real = self._preview_then_run(
            "Extend bindings",
            lambda: core.extend_bindings(self.doc, names, cat_ids, dry_run=True),
            lambda: core.extend_bindings(self.doc, names, cat_ids, dry_run=False),
            note)
        if real is not None:
            self._after_change("Bindings extended.")

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
            self._extend(names, cat_ids,
                         "Each checked parameter's binding will also cover the selected categories.")
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
            self._after_change("Parameters bound.")

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
            entries = core.file_only_definitions(self._infos(), self._file_defs)
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

    # ---------------- By Category tab ----------------
    def _refresh_cat_rows(self):
        summary = core.category_summary(self._infos(), self._categories)
        ft = (self.bycat_filter_tb.Text or "").strip().lower()
        only_bound = bool(self.bycat_only_bound_cb.IsChecked)
        rows = []
        for name, id_int, count in summary:
            if only_bound and count == 0:
                continue
            if ft and ft not in name.lower():
                continue
            rows.append(CatRow(name, id_int, count))
        self._cat_rows = rows
        self.bycat_grid.ItemsSource = None
        self.bycat_grid.ItemsSource = rows
        # keep the current category's parameter list in step with a rescan
        if self._current_cat is not None:
            self._show_category(self._current_cat)

    def bycat_filter_changed(self, sender, args):
        if not self._ready:
            return
        self._guard(self._refresh_cat_rows)

    def bycat_selected(self, sender, args):
        def run():
            row = self.bycat_grid.SelectedItem
            if row is None:
                return
            self._show_category(row.name)
        self._guard(run)

    def _show_category(self, cat_name):
        self._current_cat = cat_name
        infos = core.infos_for_category(self._infos(), cat_name)
        self._cat_param_rows = [CatParamRow(i, cat_name) for i in infos]
        self.bycat_params_grid.ItemsSource = None
        self.bycat_params_grid.ItemsSource = self._cat_param_rows
        self.bycat_title_tb.Text = "{0} - {1} parameter(s) bound".format(cat_name, len(infos))

    def _refresh_cat_params(self):
        self.bycat_params_grid.ItemsSource = None
        self.bycat_params_grid.ItemsSource = self._cat_param_rows

    def bycat_check_all_click(self, sender, args):
        for r in self._cat_param_rows:
            r.selected = True
        self._refresh_cat_params()

    def bycat_check_none_click(self, sender, args):
        for r in self._cat_param_rows:
            r.selected = False
        self._refresh_cat_params()

    def bycat_unbind_click(self, sender, args):
        def run():
            if self._current_cat is None:
                forms.alert("Pick a category first.", title=_TOOL)
                return
            names = [r.name for r in self._cat_param_rows if r.selected]
            if not names:
                forms.alert("Check at least one parameter.", title=_TOOL)
                return
            cat_id = self._cat_ids_by_name.get(self._current_cat)
            real = self._preview_then_run(
                "Unbind from {0}".format(self._current_cat),
                lambda: core.unbind_from_category(self.doc, names, cat_id, dry_run=True),
                lambda: core.unbind_from_category(self.doc, names, cat_id, dry_run=False),
                "Each checked parameter will no longer cover {0}. Values stored on {0} "
                "elements are discarded. A parameter whose only category this was loses "
                "its whole binding.".format(self._current_cat))
            if real is not None:
                self._after_change("Category unbound.")
        self._guard(run)

    def bycat_add_click(self, sender, args):
        def run():
            if self._current_cat is None:
                forms.alert("Pick a category first.", title=_TOOL)
                return
            names = [r.name for r in self._rows if r.selected and r.is_bound]
            if not names:
                forms.alert("Check at least one bound parameter on the Parameters tab first.",
                            title=_TOOL)
                return
            cat_id = self._cat_ids_by_name.get(self._current_cat)
            self._extend(names, [cat_id],
                         "Each parameter checked on the Parameters tab will also cover {0}.".format(
                             self._current_cat))
        self._guard(run)

    # ---------------- Validation tab ----------------
    def _refresh_validation(self):
        rows = core.validation_rows(self._infos())
        self._val_rows = [ValRow(*r) for r in rows]
        params, filled, empty, percent, full, none = core.validation_totals(rows)
        self.val_params_tb.Text = "{0}".format(params)
        self.val_filled_tb.Text = "{0}".format(filled)
        self.val_empty_tb.Text = "{0}".format(empty)
        self.val_percent_tb.Text = "{0}%".format(percent)
        self.val_percent_pb.Value = percent
        self.val_full_tb.Text = "{0}".format(full)
        self.val_none_tb.Text = "{0}".format(none)
        capped = sum(1 for r in self._val_rows if r.info.get("probed", 0) < r.info.get("elements", 0))
        self.val_note_tb.Text = ("{0} parameter(s) hit the probe limit - raise it for exact counts."
                                 .format(capped) if capped else "")
        # category combo: only categories that appear in the rows
        current = self._combo_text(self.val_cat_cb)
        names = sorted(set(c for r in self._val_rows for c in r.categories), key=lambda s: s.lower())
        self.val_cat_cb.Items.Clear()
        self.val_cat_cb.Items.Add("(all)")
        for n in names:
            self.val_cat_cb.Items.Add(n)
        idx = 0
        for i in range(self.val_cat_cb.Items.Count):
            if str(self.val_cat_cb.Items[i]) == current:
                idx = i
                break
        self.val_cat_cb.SelectedIndex = idx
        self._apply_val_view()

    def _apply_val_view(self):
        show = self._combo_text(self.val_show_cb)
        cat = self._combo_text(self.val_cat_cb)
        shown = []
        for r in self._val_rows:
            if show == "Below 100%" and r.percent >= 100:
                continue
            if show == "Below 50%" and r.percent >= 50:
                continue
            if show == "Never filled" and r.filled > 0:
                continue
            if cat and cat != "(all)" and cat not in r.categories:
                continue
            shown.append(r)
        self._val_shown = shown
        self.val_grid.ItemsSource = None
        self.val_grid.ItemsSource = shown

    def val_filter_changed(self, sender, args):
        if not self._ready:
            return
        self._guard(self._apply_val_view)

    def val_select_click(self, sender, args):
        def run():
            row = self.val_grid.SelectedItem
            if row is None:
                forms.alert("Highlight a parameter row first.", title=_TOOL)
                return
            with _SafeProgress(title="DeeParam - finding empty elements...", indeterminate=True):
                ids = core.empty_element_ids(self.doc, row.name, limit=self._probe_limit())
            if ids.Count == 0:
                forms.alert("No empty elements found for '{0}'.".format(row.name), title=_TOOL)
                return
            self.uidoc.Selection.SetElementIds(ids)
            self._log("Selected {0} element(s) with '{1}' empty.".format(ids.Count, row.name))
            self.status_tb.Text = "{0} element(s) selected in Revit.".format(ids.Count)
        self._guard(run)

    def val_export_click(self, sender, args):
        def run():
            rows = getattr(self, "_val_shown", None) or []
            if not rows:
                forms.alert("Scan first.", title=_TOOL)
                return
            dlg = SaveFileDialog()
            dlg.Filter = "Excel Workbook (*.xlsx)|*.xlsx"
            dlg.FileName = "DeeParam_Validation.xlsx"
            if dlg.ShowDialog() != DialogResult.OK:
                return
            from xlsx_writer import write_themed_xlsx
            headers = ["Name", "Kind", "Scope", "Categories", "Carrying", "Filled", "Empty", "Filled %"]
            widths = [34, 12, 10, 48, 10, 10, 10, 10]
            data = []
            for r in rows:
                status = "ok" if r.empty == 0 else ("fail" if r.filled == 0 else None)
                data.append(([r.name, r.kind, r.scope, r.categories_text, r.carrying,
                              r.filled, r.empty, r.percent], status))
            write_themed_xlsx(dlg.FileName, "DeeParam - Validation", headers, widths, data)
            self._log("Exported {0} validation row(s) to {1}".format(len(data), dlg.FileName))
        self._guard(run)

    # ---------------- Add Parameter tab ----------------
    def add_cat_filter_changed(self, sender, args):
        if not self._ready:
            return
        self._guard(self._fill_listbox, self.add_cat_lb, self.add_cat_filter_tb.Text)

    def add_cat_selection_changed(self, sender, args):
        if not self._ready:
            return
        try:
            self.add_cat_count_tb.Text = "{0} selected".format(self.add_cat_lb.SelectedItems.Count)
        except Exception:
            pass

    def add_cat_all_click(self, sender, args):
        self.add_cat_lb.SelectAll()

    def add_cat_none_click(self, sender, args):
        self.add_cat_lb.UnselectAll()

    def add_create_click(self, sender, args):
        def run():
            name = (self.add_name_tb.Text or "").strip()
            cat_ids = self._listbox_ids(self.add_cat_lb)
            ti = self.add_type_cb.SelectedIndex
            gi = self.add_group_cb.SelectedIndex
            if not name:
                forms.alert("Type a parameter name.", title=_TOOL)
                return
            if not cat_ids:
                forms.alert("Select at least one category.", title=_TOOL)
                return
            if ti < 0 or gi < 0:
                forms.alert("Pick a data type and a group.", title=_TOOL)
                return
            spec = self._type_opts[ti][1]
            group_id = self._group_opts[gi][1]
            as_type = bool(self.add_type_rb.IsChecked)
            vary = bool(self.add_vary_cb.IsChecked)
            file_group = (self.add_file_group_tb.Text or "").strip() or "DeePack"

            ok, detail = core.create_and_bind(self.doc, self.app, name, spec, group_id, cat_ids,
                                              as_type=as_type, file_group=file_group,
                                              vary_by_group=vary, dry_run=True)
            self._log("Add parameter (preview): {0}".format(detail))
            if not ok:
                self.add_status_tb.Text = detail
                forms.alert(detail, title=_TOOL)
                return
            if not forms.alert("{0}.\n\nCreate it now?".format(detail),
                               title="DeeParam - Confirm", yes=True, no=True):
                self._log("Cancelled.")
                return
            with _SafeProgress(title="DeeParam - creating parameter...", indeterminate=True):
                ok, detail = core.create_and_bind(self.doc, self.app, name, spec, group_id, cat_ids,
                                                  as_type=as_type, file_group=file_group,
                                                  vary_by_group=vary, dry_run=False)
            self._log("Add parameter: {0} {1}".format("OK" if ok else "ERR", detail))
            self.add_status_tb.Text = detail
            if ok:
                self.add_name_tb.Text = ""
                self._after_change("Parameter '{0}' created.".format(name))
            else:
                forms.alert(detail, title=_TOOL)
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
    require_doc(_TOOL, __revit__, allow_family=False)
    window = DeeParamWindow(_XAML_FILE, __revit__.ActiveUIDocument, __revit__.Application)
    window.ShowDialog()


main()
