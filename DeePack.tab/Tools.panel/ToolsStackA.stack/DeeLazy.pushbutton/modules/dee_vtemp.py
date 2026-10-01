# -*- coding: utf-8 -*-
"""
DeeLazy - DeeVTEMP module
Applies ONE change to MANY View Templates in the current project at
once. First action implemented (explicit user request): the Revit
Links "Display Settings" dropdown that normally has to be set one link
at a time, inside each View Template's own Visibility/Graphics
Overrides > Revit Links tab - DeeVTEMP sets it for every selected link,
in every selected View Template, in a single run.

Named generically ("View Template Bulk Editor" in the window title),
not "DeeVTEMP: Revit Links" - matching this package's established
one-module-per-concern-but-room-to-grow pattern (see view_cropping.py's
own "Scope for this first pass" docstring note): a future action button
for another view-template-wide setting belongs in THIS module, not a
new one, since it is the same "pick templates, pick a setting, apply to
all" shape.

--------------------------------------------------------------------
Revit API facts relied on here
--------------------------------------------------------------------
- A View Template is a plain View element with IsTemplate == True - it
  uses the exact same Visibility/Graphics Overrides API surface as a
  regular view (this is WHY a View Template's V/G dialog has a Revit
  Links tab at all), so View.GetLinkOverrides/SetLinkOverrides apply to
  it unchanged.
- View.SetLinkOverrides(ElementId linkInstanceId, RevitLinkGraphicsSettings
  overrides) / View.GetLinkOverrides(ElementId linkInstanceId) - the
  documented API for a view's per-link Display Settings override.
- RevitLinkGraphicsDisplayOptions enum: ByHostView / ByLinkView / Custom
  (only the first two are exposed here - see scope note below).

NEEDS LIVE-REVIT VERIFICATION (flagged, not silently assumed correct -
this module is the FIRST place in this codebase to touch
RevitLinkGraphicsSettings/SetLinkOverrides; a repo-wide search before
writing this confirmed no prior art to copy):
- The exact RevitLinkGraphicsSettings constructor shape. Revit API
  versions have been seen to expose this two different ways - a
  one-arg constructor taking the RevitLinkGraphicsDisplayOptions
  directly, or a no-arg constructor plus a settable .LinkVisibilityType
  property. _build_link_settings() below tries the constructor first
  and falls back to the property, so either shape works without
  guessing which one this project's Revit version uses.
- Whether SetLinkOverrides on a View Template specifically (as opposed
  to a regular view) needs anything extra - no Revit API documentation
  reviewed suggested a difference, but this is the first live exercise
  of that assumption.

--------------------------------------------------------------------
Scope for this first pass (explicit, not silently incomplete)
--------------------------------------------------------------------
Only "By Host View" and "By Linked View" are offered - both need just
the plain one-arg settings object. "Custom" (per-category visibility
overrides inside the link) is a materially different, much larger
feature (it means reproducing an entire V/G Overrides dialog for the
linked categories) and is deliberately left out of this first pass.
"""
import os
import time

import clr
clr.AddReference("System.Windows.Forms")

from pyrevit import forms, script
import dee_branding
from Autodesk.Revit.DB import (
    FilteredElementCollector, View, RevitLinkInstance,
    RevitLinkGraphicsSettings, RevitLinkGraphicsDisplayOptions,
    Transaction, ModelPathUtils,
)
from System.Windows.Forms import SaveFileDialog, DialogResult, MessageBox

import utils
import deew_progress_service as progsvc
import xlsx_writer

output = script.get_output()

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "DeeVTemp.xaml")

_REPORT_HEADERS = ["View Template", "Link", "Result", "Detail"]
_REPORT_COL_WIDTHS = [30, 30, 12, 40]


# ==========================================================================
# Naming - local copy of lib/dee_sheet_links_service.py's own
# _link_display_name (same reasoning given there: only the pure
# name-resolution logic is needed here, not that module's much larger
# import surface, and RevitLinkType.Name alone is not trustworthy - a
# live-reported bug on a real project, already fixed in that module).
# ==========================================================================
def _link_display_name(link_type):
    try:
        ref = link_type.GetExternalFileReference()
        if ref is not None:
            model_path = ref.GetPath()
            if model_path is not None:
                visible_path = ModelPathUtils.ConvertModelPathToUserVisiblePath(model_path)
                if visible_path:
                    return os.path.basename(visible_path)
    except Exception:
        pass
    try:
        if link_type.Name:
            return link_type.Name
    except Exception:
        pass
    return "(unnamed link)"


# ==========================================================================
# Scanning
# ==========================================================================
class TemplateRow(object):
    def __init__(self, view):
        self.view = view
        self.id = view.Id
        self.selected = False
        self.name = utils.read_name(view) or "(unnamed template)"
        try:
            self.view_type = str(view.ViewType)
        except Exception:
            self.view_type = "(unknown)"


class LinkRow(object):
    def __init__(self, instance, name):
        self.instance = instance
        self.id = instance.Id
        self.selected = True
        self.name = name


def scan_view_templates(doc):
    rows = []
    for v in FilteredElementCollector(doc).OfClass(View):
        try:
            if utils.is_view_template(v):
                rows.append(TemplateRow(v))
        except Exception:
            continue
    return rows


def scan_link_instances(doc):
    rows = []
    for inst in FilteredElementCollector(doc).OfClass(RevitLinkInstance):
        try:
            link_type = doc.GetElement(inst.GetTypeId())
            name = _link_display_name(link_type) if link_type is not None else "(unresolved link)"
        except Exception:
            name = "(unresolved link)"
        rows.append(LinkRow(inst, name))
    return rows


# ==========================================================================
# Action
# ==========================================================================
def _option_label(display_option):
    if display_option == RevitLinkGraphicsDisplayOptions.ByHostView:
        return "By Host View"
    if display_option == RevitLinkGraphicsDisplayOptions.ByLinkView:
        return "By Linked View"
    return str(display_option)


def _build_link_settings(display_option):
    """See module docstring's NEEDS LIVE-REVIT VERIFICATION note - tries
    the one-arg constructor first, falls back to the no-arg constructor
    plus the .LinkVisibilityType property, so either Revit API shape
    for this class works without guessing which one applies here."""
    try:
        return RevitLinkGraphicsSettings(display_option)
    except Exception:
        settings = RevitLinkGraphicsSettings()
        settings.LinkVisibilityType = display_option
        return settings


def apply_link_display_option(view_template, link_instance_id, display_option):
    """Sets ONE link's Display Settings override on ONE View Template.
    Never raises - returns (ok, detail)."""
    try:
        settings = _build_link_settings(display_option)
        view_template.SetLinkOverrides(link_instance_id, settings)
        return True, "Set to {0}".format(_option_label(display_option))
    except Exception as e:
        return False, str(e)


# ==========================================================================
# Report
# ==========================================================================
class ReportRow(object):
    def __init__(self, template_name, link_name, ok, detail):
        self.template_name = template_name
        self.link_name = link_name
        self.result = "OK" if ok else "Failed"
        self.detail = detail

    def to_list(self):
        return [self.template_name, self.link_name, self.result, self.detail]

    def status_tag(self):
        return "ok" if self.result == "OK" else "fail"


def _report_html(option_label, rows, elapsed_seconds):
    ok_count = sum(1 for r in rows if r.result == "OK")
    fail_count = len(rows) - ok_count
    html = [
        '<h2 style="font-family:sans-serif;color:#ddd;">DeeLazy - DeeVTEMP: Revit Links -&gt; {0}</h2>'.format(option_label),
        '<p style="color:#ddd;">{0} template/link pair(s) processed - {1} succeeded, {2} failed - {3:.1f}s.</p>'.format(
            len(rows), ok_count, fail_count, elapsed_seconds),
    ]
    for r in rows:
        bg = "#2e7d32" if r.result == "OK" else "#c62828"
        icon = "&#10003;" if r.result == "OK" else "&#10007;"
        html.append(
            '<div style="padding:4px 10px;margin:2px 0;background:{0};color:#fff;'
            'border-radius:4px;font-family:monospace;font-size:12px;">'
            '{1}&nbsp; <b>{2}</b> &rarr; {3} &mdash; {4}</div>'.format(
                bg, icon, r.template_name, r.link_name, r.detail))
    output.print_html("".join(html))


def export_report(path, title, rows):
    xlsx_rows = [(r.to_list(), r.status_tag()) for r in rows]
    xlsx_writer.write_themed_xlsx(path, title, _REPORT_HEADERS, _REPORT_COL_WIDTHS, xlsx_rows)


# ==========================================================================
# Window
# ==========================================================================
class DeeVTempWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, uiapp):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.uiapp = uiapp
        self.doc = uiapp.ActiveUIDocument.Document
        self._template_rows = []
        self._link_rows = []
        self._report_rows = []

        self._refresh_templates()
        self._refresh_links()
        self._log("Ready. Check View Template(s) and Link(s), pick a Display Setting, then Apply.")

    # ---------------- logging ----------------
    def _log(self, message):
        self.status_tb.Text = message

    # ---------------- View Templates ----------------
    def refresh_templates_click(self, sender, args):
        self._refresh_templates()

    def _refresh_templates(self):
        self._template_rows = scan_view_templates(self.doc)
        self.templates_grid.ItemsSource = None
        self.templates_grid.ItemsSource = self._template_rows

    def templates_select_all_click(self, sender, args):
        for r in self._template_rows:
            r.selected = True
        self.templates_grid.Items.Refresh()

    def templates_select_none_click(self, sender, args):
        for r in self._template_rows:
            r.selected = False
        self.templates_grid.Items.Refresh()

    # ---------------- Revit Links ----------------
    def refresh_links_click(self, sender, args):
        self._refresh_links()

    def _refresh_links(self):
        self._link_rows = scan_link_instances(self.doc)
        self.links_grid.ItemsSource = None
        self.links_grid.ItemsSource = self._link_rows

    def links_select_all_click(self, sender, args):
        for r in self._link_rows:
            r.selected = True
        self.links_grid.Items.Refresh()

    def links_select_none_click(self, sender, args):
        for r in self._link_rows:
            r.selected = False
        self.links_grid.Items.Refresh()

    # ---------------- Apply ----------------
    def apply_click(self, sender, args):
        templates = [r for r in self._template_rows if r.selected]
        links = [r for r in self._link_rows if r.selected]
        if not templates:
            forms.alert("Check at least one View Template first.")
            return
        if not links:
            forms.alert("Check at least one Revit Link first.")
            return

        display_option = (RevitLinkGraphicsDisplayOptions.ByLinkView
                           if bool(self.option_linked_rb.IsChecked)
                           else RevitLinkGraphicsDisplayOptions.ByHostView)
        option_label = _option_label(display_option)

        total = len(templates) * len(links)
        if not forms.alert(
                "Set {0} link(s) to '{1}' in {2} View Template(s) ({3} total changes)?".format(
                    len(links), option_label, len(templates), total),
                title="DeeVTEMP - confirm", yes=True, no=True):
            return

        start = time.time()
        report_rows = []
        t = Transaction(self.doc, "DeeVTEMP - Set Revit Links Display Setting")
        t.Start()
        try:
            with progsvc.DeeWProgressService("DeeLazy - DeeVTEMP", total) as prog:
                for tmpl in templates:
                    for link in links:
                        if prog.cancelled:
                            break
                        prog.step(tmpl.name, "Setting '{0}'".format(link.name))
                        ok, detail = apply_link_display_option(tmpl.view, link.id, display_option)
                        report_rows.append(ReportRow(tmpl.name, link.name, ok, detail))
                        prog.finish_file("success" if ok else "failed")
                    if prog.cancelled:
                        self._log("Cancelled by user.")
                        break
        finally:
            t.Commit()

        elapsed = time.time() - start
        self._report_rows = report_rows
        self.report_grid.ItemsSource = None
        self.report_grid.ItemsSource = report_rows
        _report_html(option_label, report_rows, elapsed)

        ok_count = sum(1 for r in report_rows if r.result == "OK")
        fail_count = len(report_rows) - ok_count
        self._log("Applied '{0}' - {1} change(s), {2} succeeded, {3} failed, {4:.1f}s. See the report.".format(
            option_label, len(report_rows), ok_count, fail_count, elapsed))

    # ---------------- Report / Close ----------------
    def export_report_click(self, sender, args):
        if not self._report_rows:
            forms.alert("Run Apply first - nothing to export yet.")
            return
        dlg = SaveFileDialog()
        dlg.Filter = "Excel Workbook (*.xlsx)|*.xlsx"
        dlg.FileName = "DeeVTEMP_Report.xlsx"
        if dlg.ShowDialog() != DialogResult.OK:
            return
        try:
            export_report(dlg.FileName, "DeeLazy - DeeVTEMP Report", self._report_rows)
        except Exception as e:
            forms.alert("Could not export report: {0}".format(e))
            return
        MessageBox.Show("Report exported to:\n{0}".format(dlg.FileName), "DeeLazy - DeeVTEMP")

    def close_click(self, sender, args):
        self.Close()


# ==========================================================================
# Launch entry point (called by the DeeLazy home window)
# ==========================================================================
def launch(uiapp):
    if uiapp.ActiveUIDocument is None:
        forms.alert("Open a Revit project first.")
        return
    window = DeeVTempWindow(_XAML_FILE, uiapp)
    window.ShowDialog()


TOOL_INFO = {
    "id": "dee_vtemp",
    "title": "DeeVTEMP",
    "description": "Apply one change to many View Templates at once. First action: set the Revit Links Display Setting (By Host View / By Linked View) across every selected View Template and link in one run.",
    "launch": launch,
}
