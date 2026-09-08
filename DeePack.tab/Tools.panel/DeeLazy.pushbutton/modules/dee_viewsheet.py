# -*- coding: utf-8 -*-
"""
DeeLazy - DeeViewsheet module
Scans every view placed on a sheet and writes that sheet's Number or
Name into a text parameter on the view itself - either a parameter you
already have, or a new Shared parameter this tool creates and binds to
the Views category for you.

Why you'd want it: Revit gives a view no built-in "which sheet am I
on?" field you can schedule, tag, or put in a view title. Copying the
sheet number onto the view makes view lists, view titles and QA
schedules possible.

--------------------------------------------------------------------
Multi-sheet views are SKIPPED, deliberately
--------------------------------------------------------------------
A legend (and sometimes a schedule) is routinely placed on many
sheets. There is no single correct sheet number for such a view, so
rather than silently picking one, this tool leaves those views
untouched and lists them in the report for a human decision. Confirmed
with the user before building. utils.build_view_to_sheets_map() exists
specifically so this case is visible at all - utils' older
build_view_to_sheet_map() keeps only one sheet per view and would have
hidden it.

--------------------------------------------------------------------
Creating the parameter - the one genuinely new piece of API surface
--------------------------------------------------------------------
Nothing else in this extension creates or binds parameters, so this is
new ground rather than a reuse of a proven path. The route used is the
standard, documented one:

  Application.OpenSharedParameterFile()  -> DefinitionFile
  DefinitionFile.Groups.Create(group)    -> DefinitionGroup
  group.Definitions.Create(ExternalDefinitionCreationOptions(name, type))
                                          -> ExternalDefinition
  Application.Create.NewInstanceBinding(CategorySet with OST_Views)
  doc.ParameterBindings.Insert(definition, binding, group)

Notes that matter in practice:
- A shared parameter file is a Revit-application-level setting, not a
  document one. If the user has none set, one is created next to the
  project file rather than silently hijacking some other location, and
  the tool says so. If the user DOES have one, it is appended to - an
  existing shared parameter file is never overwritten or replaced.
- Insert() is skipped when the definition is already bound; rebinding
  an existing binding is what raises, not what silently updates.
- Everything runs inside one Transaction, and the whole creation step
  is separate from the writing step so a binding failure cannot leave
  half the views written.

NEEDS LIVE-REVIT VERIFICATION (flagged, not assumed):
- ExternalDefinitionCreationOptions is the Revit 2017+ replacement for
  the old Definitions.Create(name, type) overload. Written against the
  current API; the exact SpecTypeId.String.Text spelling for a text
  parameter should be confirmed on a live 2024/2026 session.
- Whether the freshly-inserted binding is visible to LookupParameter
  within the SAME transaction, or needs the transaction committed
  first. This tool commits the creation transaction BEFORE the writing
  transaction specifically so it does not depend on that answer.
"""
import os

from pyrevit import forms, script

import dee_branding
import utils

from Autodesk.Revit.DB import (
    FilteredElementCollector, View, ViewSheet, Transaction, BuiltInCategory,
    CategorySet, ExternalDefinitionCreationOptions, SpecTypeId,
    BuiltInParameterGroup, ElementId, StorageType,
)
from System.Collections.Generic import List

output = script.get_output()

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "DeeViewsheet.xaml")

_SP_GROUP_NAME = "DeePack"
_DEFAULT_PARAM_NAME = "Sheet Number"

SOURCE_NUMBER = "Sheet Number"
SOURCE_NAME = "Sheet Name"
SOURCE_BOTH = "Sheet Number - Sheet Name"


# ==========================================================================
# Scan
# ==========================================================================
class ViewRow(object):
    """One placed view. Holds the ElementId, never the View object -
    this list survives an open window and a Transaction, and holding
    live Revit elements across that is what crashed DeeBlocktoFamily
    earlier in this codebase's history."""

    def __init__(self, view, sheets):
        self.view_id = view.Id
        self.selected = True
        self.view_name = utils.read_name(view) or "(unnamed)"
        try:
            self.view_type = str(view.ViewType)
        except Exception:
            self.view_type = ""
        self.sheet_count = len(sheets)
        self.multi = self.sheet_count > 1
        if sheets:
            self.sheet_number = getattr(sheets[0], "SheetNumber", "") or ""
            self.sheet_name = utils.read_name(sheets[0]) or ""
        else:
            self.sheet_number = ""
            self.sheet_name = ""
        self.all_sheets_text = ", ".join(
            (getattr(s, "SheetNumber", "") or "?") for s in sheets)
        try:
            tid = view.ViewTemplateId
            self.template_id = tid if (tid is not None and tid != ElementId.InvalidElementId) else None
        except Exception:
            self.template_id = None
        self.template_name = ""
        self.locked = False
        self.current_value = ""
        self.new_value = ""
        self.status = "Multiple sheets - will be skipped" if self.multi else "Ready"

    @property
    def sheets_text(self):
        return self.all_sheets_text

    def value_for(self, source):
        if source == SOURCE_NAME:
            return self.sheet_name
        if source == SOURCE_BOTH:
            return "{0} - {1}".format(self.sheet_number, self.sheet_name).strip(" -")
        return self.sheet_number


def scan_placed_views(doc):
    """Every non-template view that is placed on at least one sheet.
    Sheets themselves are excluded - a sheet is not 'a view on a
    sheet'."""
    view_to_sheets = utils.build_view_to_sheets_map(doc)
    rows = []
    for view in FilteredElementCollector(doc).OfClass(View):
        try:
            if utils.is_view_template(view):
                continue
            if isinstance(view, ViewSheet):
                continue
            sheets = view_to_sheets.get(view.Id.IntegerValue)
            if not sheets:
                continue
            rows.append(ViewRow(view, sheets))
        except Exception:
            continue
    rows.sort(key=lambda r: (r.sheet_number, r.view_name))
    return rows


# ==========================================================================
# Existing text parameters available on views
# ==========================================================================
def list_view_text_parameters(doc, rows, limit=400):
    """Distinct TEXT parameter names found on the scanned views. Built
    from the views themselves rather than the document's bindings, so
    the picker offers exactly what exists on real views.

    Read-only parameters are deliberately NOT filtered out here. A
    parameter controlled by a view template reports IsReadOnly=True on
    every view using that template - filtering on it would hide the
    very parameter the user is trying to unlock, which is the whole
    point of the template-exclusion feature. Genuinely read-only
    parameters still get caught and reported per view by apply_rows."""
    names = set()
    for row in rows[:limit]:
        view = doc.GetElement(row.view_id)
        if view is None:
            continue
        try:
            for p in view.Parameters:
                try:
                    if p.StorageType != StorageType.String:
                        continue
                    nm = p.Definition.Name
                    if nm:
                        names.add(nm)
                except Exception:
                    continue
        except Exception:
            continue
    return sorted(names)


# ==========================================================================
# Shared parameter creation - see module docstring
# ==========================================================================
def _ensure_shared_param_file(app, doc):
    """Returns (DefinitionFile, detail). Uses the existing shared
    parameter file when one is set; otherwise creates a new one next to
    the project file (or in the user's temp folder for an unsaved
    project) rather than writing to an arbitrary location."""
    try:
        existing = app.OpenSharedParameterFile()
        if existing is not None:
            return existing, "using your existing shared parameter file"
    except Exception:
        pass

    try:
        base = ""
        try:
            if doc.PathName:
                base = os.path.dirname(doc.PathName)
        except Exception:
            base = ""
        if not base or not os.path.isdir(base):
            import tempfile
            base = tempfile.gettempdir()
        path = os.path.join(base, "DeePack_SharedParameters.txt")
        if not os.path.exists(path):
            with open(path, "w") as fh:
                # Revit needs the file to exist; it writes its own
                # header/content on first use.
                fh.write("")
        app.SharedParametersFilename = path
        created = app.OpenSharedParameterFile()
        if created is None:
            return None, "Revit would not open the new shared parameter file at {0}".format(path)
        return created, "created a new shared parameter file at {0}".format(path)
    except Exception as e:
        return None, "could not prepare a shared parameter file: {0}".format(e)


def create_shared_view_parameter(doc, app, param_name):
    """Creates (if needed) a text Shared parameter and binds it to the
    Views category as an instance parameter. Returns (ok, detail).

    Runs in its own Transaction, committed before any view is written,
    so a binding failure can never leave a half-written batch."""
    def_file, file_detail = _ensure_shared_param_file(app, doc)
    if def_file is None:
        return False, file_detail

    try:
        group = None
        for g in def_file.Groups:
            if g.Name == _SP_GROUP_NAME:
                group = g
                break
        if group is None:
            group = def_file.Groups.Create(_SP_GROUP_NAME)

        definition = None
        for d in group.Definitions:
            if d.Name == param_name:
                definition = d
                break
        if definition is None:
            opts = ExternalDefinitionCreationOptions(param_name, SpecTypeId.String.Text)
            definition = group.Definitions.Create(opts)
    except Exception as e:
        return False, "could not create the shared parameter definition: {0}".format(e)

    t = Transaction(doc, "DeeViewsheet - Create View Parameter")
    t.Start()
    try:
        cats = doc.Application.Create.NewCategorySet()
        views_cat = doc.Settings.Categories.get_Item(BuiltInCategory.OST_Views)
        cats.Insert(views_cat)
        binding = doc.Application.Create.NewInstanceBinding(cats)

        bindings = doc.ParameterBindings
        if bindings.Contains(definition):
            t.Commit()
            return True, "'{0}' was already bound to Views ({1})".format(param_name, file_detail)
        inserted = bindings.Insert(definition, binding, BuiltInParameterGroup.PG_IDENTITY_DATA)
        t.Commit()
        if not inserted:
            return False, "Revit refused to bind '{0}' to the Views category".format(param_name)
        return True, "created '{0}' and bound it to Views ({1})".format(param_name, file_detail)
    except Exception as e:
        t.RollBack()
        return False, "could not bind the parameter: {0}".format(e)


# ==========================================================================
# View Templates
#
# A parameter that a View Template CONTROLS is read-only on every view
# using that template - Revit greys it out, and p.Set() cannot write to
# it. So for templated views this tool is useless unless the parameter
# is first excluded from template control.
#
# The API (confirmed against revitapidocs + a working published sample
# before writing, because the naming is genuinely confusing - see the
# open "SetNonControlledTemplateParameterIds not working as intended"
# thread where the semantics tripped someone up):
#   template.GetTemplateParameterIds()            -> everything the
#       template is CAPABLE of controlling.
#   template.GetNonControlledTemplateParameterIds() -> the subset
#       currently EXCLUDED (unticked in the template's Include column).
#   template.SetNonControlledTemplateParameterIds(ICollection<ElementId>)
#       -> REPLACES that excluded set.
#
# Two things that matter and are easy to get wrong:
#   1. Set() replaces rather than appends, so the new id must be added
#      to the EXISTING excluded ids or every other exclusion the user
#      had configured is silently wiped out.
#   2. The collection must be a subset of GetTemplateParameterIds(), so
#      a parameter the template cannot control is skipped rather than
#      inserted.
# ==========================================================================
def get_param_element_id(doc, rows, param_name):
    """ElementId of the named parameter, found from a real view that
    has it. Shared/project parameters expose their ParameterElement id
    via Parameter.Id."""
    for row in rows:
        view = doc.GetElement(row.view_id)
        if view is None:
            continue
        p = utils.find_param_by_name(view, param_name)
        if p is not None:
            try:
                return p.Id
            except Exception:
                continue
    return None


def template_controls_param(template, param_id):
    """True when the template governs this parameter, i.e. it is
    controllable AND not in the excluded set."""
    if template is None or param_id is None:
        return False
    try:
        controllable = [i.IntegerValue for i in template.GetTemplateParameterIds()]
        if param_id.IntegerValue not in controllable:
            return False
        excluded = [i.IntegerValue for i in template.GetNonControlledTemplateParameterIds()]
        return param_id.IntegerValue not in excluded
    except Exception:
        return False


def exclude_param_from_templates(doc, template_ids, param_id):
    """Adds param_id to each template's excluded ("not included") set so
    the parameter becomes editable per view. Returns (changed, notes).
    Its own Transaction, committed before any value is written."""
    notes = []
    changed = 0
    if param_id is None:
        return 0, ["Could not resolve the parameter's ElementId - nothing changed."]

    t = Transaction(doc, "DeeViewsheet - Exclude Parameter from View Templates")
    t.Start()
    try:
        for tid in template_ids:
            template = doc.GetElement(tid)
            if template is None:
                continue
            name = utils.read_name(template) or "(unnamed template)"
            try:
                controllable = [i.IntegerValue for i in template.GetTemplateParameterIds()]
                if param_id.IntegerValue not in controllable:
                    notes.append("{0}: template cannot control this parameter - nothing to do".format(name))
                    continue
                existing = list(template.GetNonControlledTemplateParameterIds())
                if any(i.IntegerValue == param_id.IntegerValue for i in existing):
                    notes.append("{0}: already excluded".format(name))
                    continue
                # Rebuild the FULL excluded set - Set() replaces it.
                new_ids = List[ElementId]()
                for i in existing:
                    new_ids.Add(i)
                new_ids.Add(param_id)
                template.SetNonControlledTemplateParameterIds(new_ids)
                changed += 1
                notes.append("{0}: excluded".format(name))
            except Exception as e:
                notes.append("{0}: FAILED - {1}".format(name, e))
        t.Commit()
    except Exception:
        t.RollBack()
        raise
    return changed, notes


def templates_used_by(doc, rows):
    """Distinct view-template ElementIds applied to the given rows."""
    seen = {}
    for row in rows:
        view = doc.GetElement(row.view_id)
        if view is None:
            continue
        try:
            tid = view.ViewTemplateId
            if tid is not None and tid != ElementId.InvalidElementId:
                seen[tid.IntegerValue] = tid
        except Exception:
            continue
    return list(seen.values())


# ==========================================================================
# Apply
# ==========================================================================
class ApplyResult(object):
    def __init__(self):
        self.written = 0
        self.skipped = []      # (label, reason)
        self.param_name = ""
        self.source = ""
        self.template_notes = []


def apply_rows(doc, rows, param_name, source):
    """One Transaction. Multi-sheet views are skipped by rule; every
    other failure is collected per-view so one bad view never aborts
    the rest."""
    result = ApplyResult()
    result.param_name = param_name
    result.source = source

    t = Transaction(doc, "DeeViewsheet - Write Sheet Info to Views")
    t.Start()
    try:
        for row in rows:
            if not row.selected:
                continue
            label = "{0} ({1})".format(row.view_name, row.sheet_number or "?")
            if row.multi:
                result.skipped.append(
                    (label, "on {0} sheets ({1}) - skipped by design".format(
                        row.sheet_count, row.sheets_text)))
                continue
            view = doc.GetElement(row.view_id)
            if view is None:
                result.skipped.append((label, "view no longer exists"))
                continue
            p = utils.find_param_by_name(view, param_name)
            if p is None:
                result.skipped.append((label, "view has no parameter '{0}'".format(param_name)))
                continue
            if p.IsReadOnly:
                result.skipped.append((label, "'{0}' is read-only on this view".format(param_name)))
                continue
            try:
                p.Set(row.value_for(source))
                row.current_value = row.value_for(source)
                row.status = "Written"
                result.written += 1
            except Exception as e:
                result.skipped.append((label, "write failed: {0}".format(e)))
        t.Commit()
    except Exception:
        t.RollBack()
        raise
    return result


def print_report(result):
    html = [
        '<h2 style="font-family:sans-serif;color:#ddd;">DeeViewsheet - Results</h2>',
        '<p style="color:#ddd;">Wrote {0} into <b>{1}</b> on {2} view(s). {3} skipped.</p>'.format(
            result.source, result.param_name, result.written, len(result.skipped)),
    ]
    for note in getattr(result, "template_notes", []):
        colour = "#c62828" if "FAILED" in note else "#2e7d32"
        html.append(
            '<div style="padding:4px 10px;margin:2px 0;background:{0};color:#fff;'
            'border-radius:4px;font-family:monospace;font-size:12px;">'
            'View template &mdash; {1}</div>'.format(colour, note))
    for label, reason in result.skipped:
        colour = "#8d6e00" if "skipped by design" in reason else "#c62828"
        html.append(
            '<div style="padding:4px 10px;margin:2px 0;background:{0};color:#fff;'
            'border-radius:4px;font-family:monospace;font-size:12px;">'
            '<b>{1}</b> &mdash; {2}</div>'.format(colour, label, reason))
    output.print_html("".join(html))


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


# ==========================================================================
# Window
# ==========================================================================
class DeeViewsheetWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, uiapp):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.uiapp = uiapp
        self.doc = uiapp.ActiveUIDocument.Document
        self._rows = []

        self.source_cb.ItemsSource = [SOURCE_NUMBER, SOURCE_NAME, SOURCE_BOTH]
        self.source_cb.SelectedIndex = 0
        self.new_param_tb.Text = _DEFAULT_PARAM_NAME
        self.scan_click(None, None)

    # ---------------- scan ----------------
    def scan_click(self, sender, args):
        with _SafeProgress(title="DeeViewsheet - scanning sheets and views...", indeterminate=True):
            self._rows = scan_placed_views(self.doc)
            names = list_view_text_parameters(self.doc, self._rows)
        self.param_cb.ItemsSource = names
        if names:
            for preferred in (_DEFAULT_PARAM_NAME, "Sheet Name", "Comments"):
                if preferred in names:
                    self.param_cb.SelectedItem = preferred
                    break
            else:
                self.param_cb.SelectedIndex = 0
        self._refresh()

    def _refresh(self):
        self._recompute_preview()
        self.rows_grid.ItemsSource = None
        self.rows_grid.ItemsSource = self._rows
        multi = sum(1 for r in self._rows if r.multi)
        self.status_tb.Text = (
            "{0} view(s) placed on sheets. {1} on multiple sheets (skipped). "
            "{2} text parameter(s) available on views.".format(
                len(self._rows), multi, len(list(self.param_cb.ItemsSource or []))))

    def _recompute_preview(self):
        source = self.source_cb.SelectedItem or SOURCE_NUMBER
        param = self.param_cb.SelectedItem
        param_id = get_param_element_id(self.doc, self._rows, param) if param else None
        # One lookup per template, not per view - a project can have
        # hundreds of views sharing a handful of templates.
        lock_cache = {}
        for row in self._rows:
            row.new_value = "" if row.multi else row.value_for(source)
            row.locked = False
            row.template_name = ""
            if row.template_id is not None:
                template = self.doc.GetElement(row.template_id)
                row.template_name = utils.read_name(template) or ""
                key = row.template_id.IntegerValue
                if key not in lock_cache:
                    lock_cache[key] = template_controls_param(template, param_id)
                row.locked = lock_cache[key]
            if param:
                view = self.doc.GetElement(row.view_id)
                p = utils.find_param_by_name(view, param) if view is not None else None
                try:
                    row.current_value = (p.AsString() or "") if p is not None else "(no such parameter)"
                except Exception:
                    row.current_value = ""
            if row.multi:
                row.status = "Multiple sheets - will be skipped"
            elif row.locked:
                row.status = "Locked by view template"
            else:
                row.status = "Ready"

    def source_changed(self, sender, args):
        if self._rows:
            self._refresh()

    def param_changed(self, sender, args):
        if self._rows:
            self._refresh()

    # ---------------- selection ----------------
    def select_all_click(self, sender, args):
        for r in self._rows:
            r.selected = True
        self.rows_grid.Items.Refresh()

    def select_none_click(self, sender, args):
        for r in self._rows:
            r.selected = False
        self.rows_grid.Items.Refresh()

    # ---------------- create parameter ----------------
    def create_param_click(self, sender, args):
        name = (self.new_param_tb.Text or "").strip()
        if not name:
            forms.alert("Type a name for the new parameter first.")
            return
        if not forms.alert(
                "Create a Shared parameter called '{0}' and bind it to the Views category?\n\n"
                "This adds it to your shared parameter file and to this project.".format(name),
                title="DeeViewsheet - Create Parameter", yes=True, no=True):
            return
        with _SafeProgress(title="DeeViewsheet - creating parameter...", indeterminate=True):
            ok, detail = create_shared_view_parameter(self.doc, self.uiapp.Application, name)
        if not ok:
            forms.alert("Could not create the parameter:\n\n{0}".format(detail))
            return
        self.scan_click(None, None)
        try:
            if name in list(self.param_cb.ItemsSource or []):
                self.param_cb.SelectedItem = name
        except Exception:
            pass
        self._refresh()
        forms.alert("Parameter ready - {0}.".format(detail), title="DeeViewsheet")

    # ---------------- apply ----------------
    def apply_click(self, sender, args):
        param = self.param_cb.SelectedItem
        if not param:
            forms.alert("Pick the parameter to write into, or create one first.")
            return
        selected = [r for r in self._rows if r.selected and not r.multi]
        multi_selected = [r for r in self._rows if r.selected and r.multi]
        if not selected:
            forms.alert("Nothing to write - no single-sheet views are selected.")
            return
        source = self.source_cb.SelectedItem or SOURCE_NUMBER

        locked = [r for r in selected if r.locked]
        unlock = (self.unlock_templates_cb.IsChecked is True)
        template_ids = templates_used_by(self.doc, locked) if (locked and unlock) else []

        msg = "Write the {0} into '{1}' on {2} view(s)?".format(source, param, len(selected))
        if multi_selected:
            msg += "\n\n{0} selected view(s) sit on more than one sheet and will be skipped.".format(
                len(multi_selected))
        if locked:
            if unlock:
                msg += ("\n\n{0} selected view(s) have '{1}' controlled by a view template. "
                        "{2} template(s) will be edited to EXCLUDE that parameter, so it becomes "
                        "editable per view. This changes those templates for the whole "
                        "project.".format(len(locked), param, len(template_ids)))
            else:
                msg += ("\n\n{0} selected view(s) have '{1}' locked by a view template and will "
                        "FAIL to write. Tick 'Exclude from view templates' to fix that "
                        "automatically.".format(len(locked), param))
        if not forms.alert(msg, title="DeeViewsheet - Confirm", yes=True, no=True):
            return

        unlock_notes = []
        if template_ids:
            with _SafeProgress(title="DeeViewsheet - excluding parameter from view templates...",
                                    indeterminate=True):
                _changed, unlock_notes = exclude_param_from_templates(
                    self.doc, template_ids, get_param_element_id(self.doc, self._rows, param))

        with _SafeProgress(title="DeeViewsheet - writing...", indeterminate=True):
            result = apply_rows(self.doc, self._rows, param, source)
        result.template_notes = unlock_notes
        print_report(result)
        self._refresh()
        self.status_tb.Text = "Wrote {0}, skipped {1}. See the pyRevit output window for details.".format(
            result.written, len(result.skipped))

    def close_click(self, sender, args):
        self.Close()


# ==========================================================================
# Launch entry point (called by the DeeLazy home window)
# ==========================================================================
def launch(uiapp):
    if uiapp.ActiveUIDocument is None:
        forms.alert("Open a Revit project first.")
        return
    window = DeeViewsheetWindow(_XAML_FILE, uiapp)
    window.ShowDialog()


TOOL_INFO = {
    "id": "dee_viewsheet",
    "title": "DeeViewsheet",
    "description": "Write each sheet's Number/Name onto the views placed on it - pick an existing parameter or create a new shared one.",
    "launch": launch,
}
