# -*- coding: utf-8 -*-
"""
DeeLazy - DeeParaValue module
Scans a batch of Revit files - local and/or ACC cloud - for ONE shared
parameter's current value, shows it per file, and lets the user type
corrected values before writing them all back in one run. Built for the
"audit many files, fix the ones that drifted" workflow, by explicit user
request and two explicit scope choices made when asked:
  - the parameter can be bound to ANY category (not just Project
    Information), so a file can have many bound elements, not one clean
    value;
  - editing is an EDITABLE GRID (current value shown, a separate New
    Value typed per file), not a single "push one value to every file"
    bulk overwrite.

--------------------------------------------------------------------
Resolving "any category" vs. "one editable cell per file"
--------------------------------------------------------------------
Two columns per file, not one dual-purpose cell: Current Value
(read-only - the shared value if every bound element/type in that file
agrees, "(N different values)" if they don't) and New Value (editable,
BLANK BY DEFAULT - always, even when Current Value is already clean).
Blank New Value means "skip this file" on Save, unconditionally - this
removes any ambiguity about whether an untouched cell means "confirmed
correct" or "never looked at". Saving a non-blank New Value overwrites
EVERY bound element/type in that file to that one value - a deliberate
"consolidate to one value" operation.

--------------------------------------------------------------------
Revit API facts relied on here - VERIFIED, not guessed
--------------------------------------------------------------------
Nothing in this codebase read or wrote shared-parameter values before
this module (confirmed by a repo-wide search) - every API surface below
was checked directly against the installed RevitAPI.dll via .NET
reflection (System.Reflection.MetadataLoadContext) before writing a
single line of code that depends on it, specifically because this
session was burned twice already by confidently-wrong Revit API names
(e.g. assuming "RevitLinkGraphicsDisplayOptions" existed when the real
enum was "LinkVisibility", only caught by a live crash):
  - Autodesk.Revit.DB.ExternalDefinition (GUID, Name properties) is the
    real runtime type of a Definition bound via doc.ParameterBindings
    when that definition is a SHARED parameter - an ordinary project
    parameter's Definition is an InternalDefinition instead. This is
    the exact, confirmed test used by discover_shared_parameters/
    _find_shared_definition below: isinstance(definition, ExternalDefinition).
  - InstanceBinding and TypeBinding both subclass ElementBinding and
    both expose .Categories (a CategorySet) - confirmed.
  - Parameter.IsShared, Parameter.StorageType, Parameter.SetValueString(str)
    -> bool, and Element.get_Parameter(System.Guid) -> Parameter (a
    distinct overload from get_Parameter(BuiltInParameter) and
    get_Parameter(Definition)) - all confirmed present with these exact
    signatures.
The doc.ParameterBindings.ForwardIterator()/.MoveNext()/.Key/.Current
iteration shape itself is not a new guess either - lib/
dee_shared_param_service.py already proves it live (DeeLinkDist/
DeeSheetLinks' shared-parameter creation/binding code).

What reflection CANNOT confirm is runtime BEHAVIOR (e.g. whether
SetValueString round-trips correctly for every real-world parameter
type/unit combination) - that needs a live Revit test, watched closely
on one or two files before trusting a large batch, same as this
codebase's standing practice for any first-time API surface.

--------------------------------------------------------------------
Scope limits for this first pass (not silently incomplete)
--------------------------------------------------------------------
- Only shared parameters bound as PROJECT PARAMETERS (present in
  doc.ParameterBindings) are discoverable/targetable - one embedded
  only inside a loaded family's own definitions, never bound at the
  document level, is out of reach (no bounded way to know which
  elements might carry it).
- The parameter picker lists only the ACTIVE document's bound shared
  parameters - targeting one absent there means typing its exact name
  (scanning every target file twice - once for names, once for values -
  isn't a reasonable cost for a convenience dropdown).
- One parameter per run - a second parameter means a fresh Load Values.
- Blank New Value always means "skip" - there is no way in this pass to
  explicitly set a String parameter to empty text.
- ElementId-storage parameters are read-only here - shown for
  visibility, never editable/writable.
- Type-bound parameters count/report Types, not instances - correct per
  Revit's own binding model, not a bug.
- Always opens with every workset open (matches DeeFUpdate) so closed
  worksets never hide bound elements - not user-configurable yet.
- Local files AND ACC/BIM360 cloud models are both in scope (explicit
  user correction during planning) - reuses acc_file_browser.py/
  deew_cloud_service.py exactly as dee_fupdate.py already does, no new
  cloud-access code.
- Works with NO active document open - this tool manages its own batch
  of target files independently; an active document is only ever an
  optional convenience source for the Parameter dropdown
  (discover_shared_parameters), never required. With nothing open, type
  the exact parameter name instead of picking it from the list.
"""
import os
import datetime

import clr
clr.AddReference("System.Windows.Forms")
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
from System.Windows.Forms import FolderBrowserDialog, OpenFileDialog, SaveFileDialog, DialogResult, MessageBox
from System.Windows import Visibility

from pyrevit import forms
import dee_branding

from Autodesk.Revit.DB import (
    FilteredElementCollector, ExternalDefinition, InstanceBinding, TypeBinding,
    StorageType, Transaction,
)

import deew_logger
import deew_model_scanner as scanner
import deew_document_manager as docmgr
import deew_failure_handler as ffh
import deew_progress_service as progsvc
import acc_file_browser as afb
import deew_cloud_service as cloudsvc
import xlsx_writer

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "DeeParaValue.xaml")
_TOOL_NAME = "DeeParaValue"
_TOOL_TITLE = "DeeParaValue"
_CACHE_FILE = os.path.join(_THIS_DIR, ".deeparavalue_acc_cache.json")

_REPORT_HEADERS = ["File", "Source", "New Value", "Updated", "Skipped", "Save Status", "Errors"]
_REPORT_COL_WIDTHS = [30, 10, 20, 12, 12, 20, 40]


def _revit_version_text(app):
    try:
        return str(app.VersionNumber)
    except Exception:
        return "Unknown"


class _SafeProgress(object):
    """forms.ProgressBar tries to set Window.TaskbarItemInfo on its host
    window - throws NotImplementedException under Remote Desktop/no
    taskbar (live-confirmed in DeeSheetLinks). Falls back to no progress
    UI at all rather than crashing."""
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
# Pure discovery/read/write functions - no WPF, testable independent of it
# ==========================================================================
def discover_shared_parameters(doc):
    """Sorted list of distinct names of every shared parameter bound
    (doc.ParameterBindings) in this document - see module docstring for
    the isinstance(definition, ExternalDefinition) test this relies on.
    doc=None (no active document) is a real, expected case - returns an
    empty list rather than raising, not just an accidental side effect
    of the try/except below."""
    if doc is None:
        return []
    names = []
    try:
        it = doc.ParameterBindings.ForwardIterator()
        while it.MoveNext():
            definition = it.Key
            if isinstance(definition, ExternalDefinition):
                names.append(definition.Name)
    except Exception:
        pass
    return sorted(set(names))


def find_shared_definition(doc, param_name):
    """Returns (definition, binding) for the shared parameter named
    param_name as currently bound in doc, or (None, None) if it isn't
    bound there at all. Matches by NAME (not GUID) deliberately - the
    same shared parameter re-bound across different files/sessions
    keeps its GUID, but the caller only ever has a typed/picked NAME to
    go on, never a GUID handed in from elsewhere."""
    try:
        it = doc.ParameterBindings.ForwardIterator()
        while it.MoveNext():
            definition = it.Key
            if isinstance(definition, ExternalDefinition) and definition.Name == param_name:
                return definition, it.Current
    except Exception:
        pass
    return None, None


def collect_bound_elements(doc, binding):
    """Returns (elements, is_type_scope) - every element (or type, if
    this is a TypeBinding) in doc belonging to any category the binding
    covers. Never raises; a category that fails to collect is skipped,
    not fatal to the others."""
    elements = []
    is_type_scope = isinstance(binding, TypeBinding)
    try:
        for cat in binding.Categories:
            try:
                collector = FilteredElementCollector(doc).OfCategoryId(cat.Id)
                collector = (collector.WhereElementIsElementType() if is_type_scope
                             else collector.WhereElementIsNotElementType())
                elements.extend(collector.ToElements())
            except Exception:
                continue
    except Exception:
        pass
    return elements, is_type_scope


def category_names_text(binding):
    names = []
    try:
        for cat in binding.Categories:
            try:
                names.append(cat.Name)
            except Exception:
                continue
    except Exception:
        pass
    return ", ".join(sorted(names)) or "(none)"


def _read_value_text(parameter):
    """Display string for one parameter's current value. Never raises."""
    try:
        if not parameter.HasValue:
            return ""
        text = parameter.AsValueString()
        if text is not None and text != "":
            return text
        as_string = parameter.AsString()
        return as_string if as_string is not None else ""
    except Exception:
        return "(unreadable)"


def read_value_summary(guid, elements):
    """Returns (storage_type_or_None, display_text) for the shared
    parameter identified by guid, read from every element in `elements`
    that actually carries it. "(no elements)" if none do (e.g. a bound
    category with nothing placed); the shared value if every element
    that has it agrees; "(N different values)" otherwise."""
    values = []
    storage_type = None
    for el in elements:
        try:
            p = el.get_Parameter(guid)
        except Exception:
            p = None
        if p is None:
            continue
        if storage_type is None:
            try:
                storage_type = p.StorageType
            except Exception:
                pass
        values.append(_read_value_text(p))
    if not values:
        return storage_type, "(no elements)"
    distinct = sorted(set(values))
    if len(distinct) == 1:
        return storage_type, distinct[0]
    return storage_type, "({0} different values)".format(len(distinct))


def storage_type_text(storage_type):
    if storage_type is None:
        return "?"
    return str(storage_type)


_YES_WORDS = ("yes", "true", "1")
_NO_WORDS = ("no", "false", "0")


def apply_value_to_parameter(parameter, raw_text):
    """Sets one parameter's value from user-typed raw_text, StorageType-
    aware. Never raises - returns (ok, detail)."""
    try:
        if parameter.IsReadOnly:
            return False, "read-only on this element"
    except Exception:
        pass

    # Preferred for every StorageType: parses DISPLAY-formatted text the
    # same way the Properties palette would - critical for Double/length
    # parameters, where the display unit (e.g. mm) differs from the
    # internal storage unit (feet); a naive float(text) would silently
    # apply the wrong magnitude.
    try:
        if parameter.SetValueString(raw_text):
            return True, "set"
    except Exception:
        pass

    try:
        st = parameter.StorageType
        if st == StorageType.String:
            parameter.Set(raw_text)
            return True, "set (string)"
        if st == StorageType.Integer:
            text = raw_text.strip().lower()
            if text in _YES_WORDS:
                value = 1
            elif text in _NO_WORDS:
                value = 0
            else:
                value = int(raw_text.strip())
            parameter.Set(value)
            return True, "set (integer)"
        if st == StorageType.Double:
            parameter.Set(float(raw_text.strip()))
            return True, "set (double, internal units - SetValueString was unavailable)"
        if st == StorageType.ElementId:
            return False, "ElementId parameters are not supported"
        return False, "unrecognized storage type"
    except Exception as e:
        return False, str(e)


# ==========================================================================
# Data rows
# ==========================================================================
class CloudItem(object):
    """One row added via "Add Cloud Models..." - same shape as
    DeeFUpdate's own CloudFUpdateItem, matching acc_file_browser's
    pick_hub/pick_project/list_project_files return values directly."""

    def __init__(self, hub_id, hub_name, region, project_id, project_name, item_id, display_name, token):
        self.selected = False
        self.hub_id = hub_id
        self.hub_name = hub_name
        self.region = region
        self.project_id = project_id
        self.project_name = project_name
        self.item_id = item_id
        self.display_name = display_name
        self.token = token
        self.status = "Ready"


class ValueRow(object):
    """One row per target file in the Values grid - plain data holder,
    no live Document/Element handles retained between Load Values and
    Save (this codebase's established "hold nothing stale" rule)."""

    def __init__(self, target, source, file_name):
        self.target = target  # the ScannedModel or CloudItem this came from
        self.source = source  # "Local" or "Cloud"
        self.file_name = file_name
        self.selected = False
        self.status = "Not scanned"
        self.categories_text = ""
        self.scope_text = ""
        self.element_count = 0
        self.storage_type_text = ""
        self.current_value_display = ""
        self.new_value = ""


class ReportRow(object):
    def __init__(self, file_name, source, new_value, elements_updated, elements_skipped, save_status, errors=""):
        self.file_name = file_name
        self.source = source
        self.new_value = new_value
        self.elements_updated = elements_updated
        self.elements_skipped = elements_skipped
        self.save_status = save_status
        self.errors = errors

    def to_list(self):
        return [self.file_name, self.source, self.new_value, self.elements_updated,
                self.elements_skipped, self.save_status, self.errors]

    def status_tag(self):
        if "fail" in self.save_status.lower() or self.errors:
            return "fail"
        if "skip" in self.save_status.lower():
            return "skip"
        return "ok"


def export_report(path, title, rows):
    xlsx_rows = [(r.to_list(), r.status_tag()) for r in rows]
    xlsx_writer.write_themed_xlsx(path, title, _REPORT_HEADERS, _REPORT_COL_WIDTHS, xlsx_rows)


# ==========================================================================
# Pipeline - per-file scan (read-only) and apply (write), local + cloud
# ==========================================================================
class DeeParaValuePipeline(object):
    def __init__(self, application, param_name, logger):
        self.application = application
        self.param_name = param_name
        self.logger = logger

    def _scan_document(self, doc, row):
        definition, binding = find_shared_definition(doc, self.param_name)
        if definition is None:
            row.status = "Not present"
            return
        elements, is_type_scope = collect_bound_elements(doc, binding)
        row.categories_text = category_names_text(binding)
        row.scope_text = "Type" if is_type_scope else "Instance"
        row.element_count = len(elements)
        storage, display = read_value_summary(definition.GUID, elements)
        row.storage_type_text = storage_type_text(storage)
        row.current_value_display = display
        row.status = "OK" if elements else "No elements"
        if storage == StorageType.ElementId:
            row.status = "Unsupported (ElementId)"

    def scan_local(self, scanned_model):
        row = ValueRow(scanned_model, "Local", scanned_model.file_name)
        if scanned_model.model_type in (scanner.MODEL_TYPE_CORRUPTED, scanner.MODEL_TYPE_READ_ONLY):
            row.status = "Skipped - {0}".format(scanned_model.status)
            return row
        document = None
        try:
            document, err = docmgr.open_document_no_detach(
                self.application, scanned_model.file_path, open_all_worksets=True, logger=self.logger)
            if document is None:
                row.status = "Failed - could not open ({0})".format(err)
                return row
            self._scan_document(document, row)
        except Exception as e:
            row.status = "Failed - unexpected error"
            self.logger.exception("Unexpected error scanning file", e, file=scanned_model.file_name)
        finally:
            if document is not None:
                docmgr.close_document(document, save_modified=False, logger=self.logger)
        return row

    def scan_cloud(self, item, uiapp):
        row = ValueRow(item, "Cloud", item.display_name)
        ui_doc = None
        try:
            ui_doc, detail = afb.open_cloud_file(
                uiapp, item.region, item.project_id, item.item_id, item.token, close_worksets=False)
            if ui_doc is None:
                row.status = "Failed - could not open ({0})".format(detail)
                return row
            self._scan_document(ui_doc.Document, row)
        except Exception as e:
            row.status = "Failed - unexpected error"
            self.logger.exception("Unexpected error scanning cloud model", e, file=item.display_name)
        finally:
            if ui_doc is not None:
                try:
                    docmgr.close_document(ui_doc.Document, save_modified=False, logger=self.logger)
                except Exception:
                    pass
        return row

    def _apply_to_document(self, doc, raw_text, report):
        definition, binding = find_shared_definition(doc, self.param_name)
        if definition is None:
            report.save_status = "Skipped - parameter no longer present"
            return False
        elements, _is_type_scope = collect_bound_elements(doc, binding)
        if not elements:
            report.save_status = "Skipped - no bound elements"
            return False

        updated = 0
        skipped = 0
        errors = []
        t = Transaction(doc, "DeeParaValue - Set '{0}'".format(self.param_name))
        t.Start()
        try:
            for el in elements:
                try:
                    p = el.get_Parameter(definition.GUID)
                except Exception:
                    p = None
                if p is None:
                    skipped += 1
                    continue
                ok, detail = apply_value_to_parameter(p, raw_text)
                if ok:
                    updated += 1
                else:
                    skipped += 1
                    errors.append(detail)
            t.Commit()
        except Exception as e:
            t.RollBack()
            report.save_status = "Failed - transaction rolled back"
            report.errors = str(e)
            return False

        report.elements_updated = updated
        report.elements_skipped = skipped
        if errors:
            # Dedupe repeated identical messages (e.g. "read-only on this
            # element" from 300 instances) so the report stays readable.
            distinct = sorted(set(errors))
            report.errors = "; ".join(distinct[:5]) + ("; ..." if len(distinct) > 5 else "")
        return True

    def apply_local(self, scanned_model, raw_text):
        report = ReportRow(scanned_model.file_name, "Local", raw_text, 0, 0, "")
        document = None
        try:
            document, err = docmgr.open_document_no_detach(
                self.application, scanned_model.file_path, open_all_worksets=True, logger=self.logger)
            if document is None:
                report.save_status = "Failed - could not open"
                report.errors = err
                return report
            committed = self._apply_to_document(document, raw_text, report)
            if not committed:
                return report
            if docmgr.is_workshared(document):
                ok, detail = docmgr.synchronize_with_central(
                    document, comment="DeeParaValue - set {0}".format(self.param_name), logger=self.logger)
                report.save_status = "Synchronized with Central" if ok else "Failed - sync error"
                if not ok:
                    report.errors = (report.errors + "; " + detail) if report.errors else detail
            else:
                ok, detail = docmgr.save_standalone(document, logger=self.logger)
                report.save_status = "Saved" if ok else "Failed - save error"
                if not ok:
                    report.errors = (report.errors + "; " + detail) if report.errors else detail
        except Exception as e:
            report.save_status = "Failed - unexpected error"
            report.errors = str(e)
            self.logger.exception("Unexpected error applying value to file", e, file=scanned_model.file_name)
        finally:
            if document is not None:
                docmgr.close_document(document, save_modified=False, logger=self.logger)
        return report

    def apply_cloud(self, item, uiapp, raw_text):
        report = ReportRow(item.display_name, "Cloud", raw_text, 0, 0, "")
        ui_doc = None
        try:
            ui_doc, detail = afb.open_cloud_file(
                uiapp, item.region, item.project_id, item.item_id, item.token, close_worksets=False)
            if ui_doc is None:
                report.save_status = "Failed - could not open"
                report.errors = detail
                return report
            document = ui_doc.Document
            committed = self._apply_to_document(document, raw_text, report)
            if not committed:
                return report
            ok, sync_detail = docmgr.synchronize_with_central(
                document, comment="DeeParaValue - set {0}".format(self.param_name), logger=self.logger)
            report.save_status = "Synchronized with Central" if ok else "Failed - sync error"
            if not ok:
                report.errors = (report.errors + "; " + sync_detail) if report.errors else sync_detail
        except Exception as e:
            report.save_status = "Failed - unexpected error"
            report.errors = str(e)
            self.logger.exception("Unexpected error applying value to cloud model", e, file=item.display_name)
        finally:
            if ui_doc is not None:
                try:
                    docmgr.close_document(ui_doc.Document, save_modified=False, logger=self.logger)
                except Exception:
                    pass
        return report


# ==========================================================================
# Window
# ==========================================================================
class DeeParaValueWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, uiapp):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.uiapp = uiapp
        self.application = uiapp.Application
        # No active document is a real, supported case here - this tool
        # manages its OWN batch of target files independently; the
        # active document is only ever used as a convenience source for
        # the Parameter dropdown (see discover_shared_parameters/
        # refresh_params_click, both already None-safe), never required.
        self.doc = uiapp.ActiveUIDocument.Document if uiapp.ActiveUIDocument is not None else None
        self.logger = deew_logger.DeeWLogger(_TOOL_NAME)
        self._models = []
        self._cloud_items = []
        self._value_rows = []
        self._report_rows = []
        self._dialog_handler = None

        self.parameter_cb.ItemsSource = discover_shared_parameters(self.doc)
        if self.doc is None:
            self._log("Ready - no document open. Type the exact shared parameter name on the "
                      "Parameter tab (the dropdown only lists an active document's parameters), "
                      "add target files, then Load Values.")
        else:
            self._log("Ready. Add target files, pick a shared parameter, then Load Values.")

    # ---------------- logging ----------------
    def _log(self, message):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self.status_tb.Text = (self.status_tb.Text + "\n" if self.status_tb.Text else "") + "[{0}] {1}".format(ts, message)
        self.status_tb.ScrollToEnd()

    # ---------------- Source mode (Local / ACC) ----------------
    def source_mode_changed(self, sender, args):
        """Toggles which input section is visible - purely a decluttering
        toggle, not a data constraint: files already added to EITHER list
        stay there and still get processed by Load Values regardless of
        which radio is currently selected, so switching back and forth to
        add both local and cloud files to the same run still works."""
        is_acc = bool(self.source_acc_rb.IsChecked)
        self.local_section.Visibility = Visibility.Collapsed if is_acc else Visibility.Visible
        self.cloud_section.Visibility = Visibility.Visible if is_acc else Visibility.Collapsed

    # ---------------- Local files ----------------
    def browse_click(self, sender, args):
        dlg = FolderBrowserDialog()
        if self.source_folder_tb.Text and os.path.isdir(self.source_folder_tb.Text):
            dlg.SelectedPath = self.source_folder_tb.Text
        if dlg.ShowDialog() == DialogResult.OK:
            self.source_folder_tb.Text = dlg.SelectedPath

    def scan_click(self, sender, args):
        folder = self.source_folder_tb.Text
        if not folder or not os.path.isdir(folder):
            forms.alert("Pick a valid source folder first.")
            return
        self._log("Scanning '{0}' for RVT files...".format(folder))
        recursive = bool(self.recursive_cb.IsChecked)
        with progsvc.DeeWProgressService(_TOOL_TITLE, 1) as prog:
            def progress_cb(i, total, name):
                prog.total_files = max(total, 1)
                prog.step(name, "Scanning", index=i)
            models = scanner.scan_folder(folder, recursive=recursive, progress_cb=progress_cb)
        self._models = models
        self._refresh_models_grid()
        self.scan_status_tb.Text = "{0} RVT file(s) found.".format(len(models))
        self._log("Scan complete: {0} file(s).".format(len(models)))

    def add_files_click(self, sender, args):
        dlg = OpenFileDialog()
        dlg.Filter = "Revit Files (*.rvt)|*.rvt"
        dlg.Multiselect = True
        dlg.Title = "Add individual Revit files"
        if dlg.ShowDialog() != DialogResult.OK:
            return
        existing_paths = set(m.file_path for m in self._models)
        added = 0
        for path in dlg.FileNames:
            if path in existing_paths:
                continue
            self._models.append(scanner.scan_file(path))
            existing_paths.add(path)
            added += 1
        self._refresh_models_grid()
        self.scan_status_tb.Text = "{0} RVT file(s) in list.".format(len(self._models))
        self._log("Added {0} file(s) individually.".format(added))

    def select_all_click(self, sender, args):
        for m in self._models:
            m.selected = True
        self._refresh_models_grid()

    def select_none_click(self, sender, args):
        for m in self._models:
            m.selected = False
        self._refresh_models_grid()

    def _refresh_models_grid(self):
        self.models_grid.ItemsSource = None
        self.models_grid.ItemsSource = self._models

    # ---------------- Cloud models ----------------
    def add_cloud_click(self, sender, args):
        try:
            token = cloudsvc.get_token()
            hub = afb.pick_hub(token)
            if not hub:
                return
            hub_id, region, hub_name = hub
            project = afb.pick_project(hub_id, token)
            if not project:
                return
            project_id, project_name = project
            self._log("Loading cloud model list for '{0}'...".format(project_name))
            with _SafeProgress(title="DeeParaValue - loading cloud model list...", indeterminate=True):
                all_items = afb.list_project_files(hub_id, project_id, token, _CACHE_FILE)
            if not all_items:
                return
            picked_names = afb.pick_files_to_open(
                all_items, title="Select Cloud Models", button_name="Add Selected")
            if not picked_names:
                return
        except Exception as e:
            forms.alert("Could not load ACC cloud models: {0}".format(e))
            return

        existing_ids = set(i.item_id for i in self._cloud_items)
        added = 0
        for name in picked_names:
            item_id = all_items[name]
            if item_id in existing_ids:
                continue
            self._cloud_items.append(
                CloudItem(hub_id, hub_name, region, project_id, project_name, item_id, name, token))
            existing_ids.add(item_id)
            added += 1
        self._refresh_cloud_grid()
        self._log("Added {0} cloud model(s) ({1} already in list).".format(added, len(picked_names) - added))

    def select_all_cloud_click(self, sender, args):
        for i in self._cloud_items:
            i.selected = True
        self._refresh_cloud_grid()

    def select_none_cloud_click(self, sender, args):
        for i in self._cloud_items:
            i.selected = False
        self._refresh_cloud_grid()

    def _refresh_cloud_grid(self):
        self.cloud_grid.ItemsSource = None
        self.cloud_grid.ItemsSource = self._cloud_items

    # ---------------- Parameter ----------------
    def refresh_params_click(self, sender, args):
        self.parameter_cb.ItemsSource = discover_shared_parameters(self.doc)
        if self.doc is None:
            self.parameter_info_tb.Text = "No document is open - type the exact shared parameter name; it'll be checked against each target file instead."
            return
        name = (self.parameter_cb.Text or "").strip()
        if not name:
            self.parameter_info_tb.Text = "No parameter checked against the active document yet."
            return
        definition, binding = find_shared_definition(self.doc, name)
        if definition is None:
            self.parameter_info_tb.Text = "'{0}' is not bound in the active document - you can still type it exactly and use it against your target files.".format(name)
            return
        self.parameter_info_tb.Text = "Found in active document - Categories: {0} | Scope: {1}".format(
            category_names_text(binding), "Type" if isinstance(binding, TypeBinding) else "Instance")

    # ---------------- Values ----------------
    def load_values_click(self, sender, args):
        param_name = (self.parameter_cb.Text or "").strip()
        if not param_name:
            forms.alert("Enter or pick a shared parameter name first (Parameter tab).")
            return
        selected_local = [m for m in self._models if m.selected]
        selected_cloud = [i for i in self._cloud_items if i.selected]
        if not selected_local and not selected_cloud:
            forms.alert("Select at least one local file or cloud model first (Target Files tab).")
            return

        pipeline = DeeParaValuePipeline(self.application, param_name, self.logger)
        total = len(selected_local) + len(selected_cloud)
        rows = []
        with progsvc.DeeWProgressService(_TOOL_TITLE, total) as prog:
            for model in selected_local:
                if prog.cancelled:
                    self._log("Cancelled by user.")
                    break
                prog.step(model.file_name, "Scanning")
                row = pipeline.scan_local(model)
                rows.append(row)
                prog.finish_file("success" if row.status in ("OK", "No elements") else "failed")
                self._log("'{0}': {1}".format(row.file_name, row.status))
            if not prog.cancelled:
                for item in selected_cloud:
                    if prog.cancelled:
                        self._log("Cancelled by user.")
                        break
                    prog.step(item.display_name, "Scanning")
                    row = pipeline.scan_cloud(item, self.uiapp)
                    rows.append(row)
                    prog.finish_file("success" if row.status in ("OK", "No elements") else "failed")
                    self._log("'{0}': {1}".format(row.file_name, row.status))

        self._value_rows = rows
        self._refresh_values_grid()
        self.values_status_tb.Text = "{0} file(s) loaded for '{1}'.".format(len(rows), param_name)
        self._log(self.values_status_tb.Text)

    def _refresh_values_grid(self):
        self.values_grid.ItemsSource = None
        self.values_grid.ItemsSource = self._value_rows

    def select_all_values_click(self, sender, args):
        for r in self._value_rows:
            r.selected = True
        self._refresh_values_grid()

    def select_none_values_click(self, sender, args):
        for r in self._value_rows:
            r.selected = False
        self._refresh_values_grid()

    def clear_new_values_click(self, sender, args):
        for r in self._value_rows:
            r.new_value = ""
        self._refresh_values_grid()

    def fill_selected_click(self, sender, args):
        text = self.fill_value_tb.Text or ""
        count = 0
        for r in self._value_rows:
            if r.selected:
                r.new_value = text
                count += 1
        self._refresh_values_grid()
        self._log("Filled New Value for {0} checked row(s).".format(count))

    # ---------------- Save ----------------
    def save_click(self, sender, args):
        param_name = (self.parameter_cb.Text or "").strip()
        to_apply = [r for r in self._value_rows if (r.new_value or "").strip()]
        if not to_apply:
            forms.alert("No row has a New Value typed in - nothing to save. Blank New Value always means 'skip'.")
            return
        unsupported = [r for r in to_apply if "ElementId" in r.status]
        if unsupported:
            forms.alert("{0} file(s) are ElementId-typed (unsupported) and will be skipped automatically.".format(
                len(unsupported)))

        if not forms.alert(
                "Write '{0}' into {1} file(s)? Every bound element in each of those files will be set to its "
                "typed New Value. This is saved/synchronized back to the real files - it cannot be undone from here.".format(
                    param_name, len(to_apply)),
                title=_TOOL_TITLE + " - confirm", yes=True, no=True):
            return

        options_auto_resolve = True
        if options_auto_resolve:
            self._dialog_handler = ffh.make_dialog_handler(self.logger)
            try:
                self.uiapp.DialogBoxShowing += self._dialog_handler
            except Exception as e:
                self.logger.exception("Could not attach dialog handler", e)

        pipeline = DeeParaValuePipeline(self.application, param_name, self.logger)
        report_rows = []
        try:
            with progsvc.DeeWProgressService(_TOOL_TITLE, len(to_apply)) as prog:
                for row in to_apply:
                    if prog.cancelled:
                        self._log("Cancelled by user.")
                        break
                    prog.step(row.file_name, "Applying")
                    if "ElementId" in row.status:
                        rr = ReportRow(row.file_name, row.source, row.new_value, 0, 0, "Skipped - ElementId unsupported")
                    elif row.source == "Cloud":
                        rr = pipeline.apply_cloud(row.target, self.uiapp, row.new_value)
                    else:
                        rr = pipeline.apply_local(row.target, row.new_value)
                    report_rows.append(rr)
                    outcome = "success" if rr.save_status in ("Saved", "Synchronized with Central") else (
                        "skipped" if "skip" in rr.save_status.lower() else "failed")
                    prog.finish_file(outcome)
                    self._log("'{0}': {1}".format(rr.file_name, rr.save_status))
        finally:
            if self._dialog_handler is not None:
                try:
                    self.uiapp.DialogBoxShowing -= self._dialog_handler
                except Exception:
                    pass
                self._dialog_handler = None

        self._report_rows = report_rows
        self.report_grid.ItemsSource = None
        self.report_grid.ItemsSource = report_rows

        succeeded = sum(1 for r in report_rows if r.save_status in ("Saved", "Synchronized with Central"))
        failed = sum(1 for r in report_rows if "fail" in r.save_status.lower())
        skipped = sum(1 for r in report_rows if "skip" in r.save_status.lower())
        self.summary_tb.Text = "{0} processed: {1} succeeded, {2} failed, {3} skipped.".format(
            len(report_rows), succeeded, failed, skipped)
        self._log(self.summary_tb.Text)
        forms.alert(self.summary_tb.Text, title=_TOOL_TITLE)

    # ---------------- Export / Close ----------------
    def export_report_click(self, sender, args):
        if not self._report_rows:
            forms.alert("Run Save first - nothing to export yet.")
            return
        dlg = SaveFileDialog()
        dlg.Filter = "Excel Workbook (*.xlsx)|*.xlsx"
        dlg.FileName = "DeeParaValue_Report.xlsx"
        if dlg.ShowDialog() != DialogResult.OK:
            return
        try:
            export_report(dlg.FileName, "DeeParaValue - Batch Parameter Value Report", self._report_rows)
        except Exception as e:
            forms.alert("Could not export report: {0}".format(e))
            return
        MessageBox.Show("Report exported to:\n{0}".format(dlg.FileName), _TOOL_TITLE)

    def close_click(self, sender, args):
        self.Close()


# ==========================================================================
# Launch entry point (called by the DeeLazy home window)
# ==========================================================================
def launch(uiapp):
    """No active-document gate, deliberately - this tool manages its own
    batch of target files independently of whatever (if anything) is
    currently open; see DeeParaValueWindow.__init__ for how the active
    document is used only as an optional convenience, never required."""
    window = DeeParaValueWindow(_XAML_FILE, uiapp)
    window.ShowDialog()


TOOL_INFO = {
    "id": "dee_para_value",
    "title": "DeeParaValue",
    "description": "Batch-audit and edit a shared parameter's value across many Revit files (local and/or ACC cloud) - see each file's current value, type corrections, save them all in one run.",
    "launch": launch,
}
