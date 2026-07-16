# -*- coding: utf-8 -*-
"""
DeeHealth (HealthPack)
Opens with a built-in default rubric already loaded (mirroring the
user's own "Internal REVIT QC Tests.xlsx" - SectionName | SCORE |
TestName | SCORE | E | F | G | Description columns), so Run Health
Check works immediately with no external file needed. Import Rubric
from Excel still overrides it with a different/updated workbook.

Two tabs: Configuration (import, naming pattern, approval limit,
per-section and per-test Run checkboxes, editable Weight/E/F/G) and
Results (overall score vs. the Approval Limit, an auto-generated plain-
English summary, and the full color-coded per-test breakdown).

Architecture: lib/health_rubric.py parses the workbook and does the
Revit-API-free scoring math (unit-tested against a real such workbook -
45/45 test names mapped, weights sum to 1.0000), plus
infer_missing_thresholds() for the handful of tests that clearly follow
a scored sibling's "any occurrence is a defect" pattern but were left
blank in the source workbook. lib/health_checks.py holds the ~45
Revit-side check functions, one per check_id, each returning
(value, detail) or raising (caught here per-check, so one bad check
never blocks the rest of the run) - prepare_context() pre-collects the
handful of large, expensive collections several checks would otherwise
each re-gather independently (the main cost of a slow run on a big
project), and unchecking a test's Run box skips it entirely, which is
the other lever for a faster run when some checks aren't needed.

Several checks are explicitly approximate given real Revit API limits -
see health_checks.py's module docstring and each check's own detail
text: Purgeable Elements, Duplicate Modeled Elements, Unclosed/
overlapping Rooms and Spaces (per the rubric's own "same location as
another" description), and "largest family" (ranked by type+instance
count, not true file size). Nothing this tool does writes to the model
- every check only reads.
"""
import os
import csv

import clr
clr.AddReference("System.Windows.Forms")
clr.AddReference("WindowsBase")
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
from System.Windows.Forms import SaveFileDialog, OpenFileDialog, DialogResult, MessageBox
from System.Windows import Thickness
from System.Windows.Controls import CheckBox
from System.Windows.Media import SolidColorBrush, Color as MediaColor

from pyrevit import forms, script

import xlsx_reader
import xlsx_writer
import health_rubric
import health_checks

output = script.get_output()

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")

_DEFAULT_NAMING_PATTERN = r"^[A-Za-z0-9_\-\.\s]+$"
_DEFAULT_APPROVAL_LIMIT = 70.0

_BG_BY_STATUS = {"ok": "#2e7d32", "warn": "#f9a825", "fail": "#c62828"}


def _status_for_score(score):
    """Shared 3-tier grading used by both the on-screen report and the
    Excel export, so the two always agree on what counts as good/warn/fail."""
    if score is None:
        return None
    if score >= health_rubric.SCORE_STATUS_OK:
        return "ok"
    if score >= health_rubric.SCORE_STATUS_WARN:
        return "warn"
    return "fail"


class DeeHealthWindow(forms.WPFWindow):
    def __init__(self, xaml_file, doc):
        forms.WPFWindow.__init__(self, xaml_file)
        self.doc = doc
        self._tests = []
        self._last_overall = None
        self.naming_pattern_tb.Text = _DEFAULT_NAMING_PATTERN
        self.approval_limit_tb.Text = "{0:.0f}".format(_DEFAULT_APPROVAL_LIMIT)
        self._apply_tests(health_rubric.get_default_tests(), "the built-in default rubric")

    def _apply_tests(self, tests, source_label):
        for t in tests:
            t.implemented = t.check_id in health_checks.CHECKS

        self._tests = tests
        self._last_overall = None
        self.config_grid.ItemsSource = None
        self.config_grid.ItemsSource = tests
        self.results_grid.ItemsSource = None
        self.results_grid.ItemsSource = tests
        self._rebuild_section_checks()

        mapped = sum(1 for t in tests if t.implemented)
        self.rubric_summary_tb.Text = "{0} test(s) loaded from {1} ({2} implemented, {3} not implemented).".format(
            len(tests), source_label, mapped, len(tests) - mapped)
        self.overall_score_tb.Text = "Overall Score: --"
        self.approval_status_tb.Text = ""
        self.summary_tb.Text = ""

    # -- section group checkboxes -------------------------------------------
    def _rebuild_section_checks(self):
        panel = self.section_checks_panel
        panel.Children.Clear()
        sections = []
        seen = set()
        for t in self._tests:
            if t.section not in seen:
                seen.add(t.section)
                sections.append(t.section)
        for section in sections:
            cb = CheckBox()
            cb.Content = section
            cb.IsChecked = True
            cb.Margin = _thickness(0, 0, 20, 6)
            cb.Tag = section
            cb.Checked += self._section_check_changed
            cb.Unchecked += self._section_check_changed
            panel.Children.Add(cb)

    def _section_check_changed(self, sender, args):
        section = sender.Tag
        enabled = bool(sender.IsChecked)
        for t in self._tests:
            if t.section == section:
                t.run_enabled = enabled
        self.config_grid.Items.Refresh()

    # -- import ---------------------------------------------------------------
    def import_rubric_click(self, sender, args):
        dlg = OpenFileDialog()
        dlg.Filter = "Excel files (*.xlsx)|*.xlsx"
        if dlg.ShowDialog() != DialogResult.OK:
            return
        try:
            sheets = xlsx_reader.read_xlsx_sheets(dlg.FileName)
        except Exception as e:
            forms.alert("Could not read file: {0}".format(e))
            return
        if not sheets:
            forms.alert("No sheets found in that workbook.")
            return
        grid = list(sheets.values())[0]
        try:
            tests = health_rubric.infer_missing_thresholds(health_rubric.parse_rubric_grid(grid))
        except Exception as e:
            forms.alert("Could not parse rubric: {0}".format(e))
            return
        if not tests:
            forms.alert("No tests found - check the workbook matches the expected "
                        "SectionName/SCORE/TestName/SCORE/E/F/G/Description layout.")
            return

        self._apply_tests(tests, "'{0}'".format(os.path.basename(dlg.FileName)))

    # -- run --------------------------------------------------------------------
    def run_health_click(self, sender, args):
        if not self._tests:
            forms.alert("Import a rubric first.")
            return

        base_ctx = {"naming_pattern": self.naming_pattern_tb.Text or _DEFAULT_NAMING_PATTERN}
        with forms.ProgressBar(title="DeeHealth — preparing...", cancellable=True):
            ctx = health_checks.prepare_context(self.doc, base_ctx)

        with forms.ProgressBar(title="DeeHealth — running checks...", cancellable=True) as pb:
            total = len(self._tests)
            for i, t in enumerate(self._tests):
                if pb.cancelled:
                    break
                pb.update_progress(i, total)
                t.has_run = True
                if not t.run_enabled:
                    t.result_value = None
                    t.result_detail = "Skipped (Run unchecked)"
                    t.result_score = None
                    continue
                if not t.implemented or t.check_id is None:
                    t.result_value = None
                    t.result_detail = "Not implemented - no matching check for this test name"
                    t.result_score = None
                    continue
                check_fn = health_checks.CHECKS.get(t.check_id)
                try:
                    value, detail = check_fn(self.doc, ctx)
                    t.result_value = value
                    t.result_detail = detail
                    t.result_score = health_rubric.score_test(value, t.e_raw, t.g_raw) if t.is_scored else None
                except Exception as e:
                    t.result_value = None
                    t.result_detail = "FAILED: {0}".format(e)
                    t.result_score = None

        self.config_grid.Items.Refresh()
        self.results_grid.Items.Refresh()

        overall = health_rubric.compute_overall_score(self._tests)
        self._last_overall = overall
        try:
            approval_limit = float(self.approval_limit_tb.Text)
        except (ValueError, TypeError):
            approval_limit = _DEFAULT_APPROVAL_LIMIT

        if overall is None:
            self.overall_score_tb.Text = "Overall Score: -- (nothing scorable)"
            self.approval_status_tb.Text = ""
        else:
            self.overall_score_tb.Text = "Overall Score: {0:.0f}%".format(overall * 100)
            passed = (overall * 100) >= approval_limit
            self.approval_status_tb.Text = "{0} (Approval Limit: {1:.0f}%)".format(
                "PASSED" if passed else "FAILED", approval_limit)
            self.approval_status_tb.Foreground = _brush("#2e7d32" if passed else "#c62828")

        self.summary_tb.Text = self._build_summary_text(approval_limit)
        self._print_html_report(overall)
        self.main_tabs.SelectedIndex = 1

    def _build_summary_text(self, approval_limit):
        ran = [t for t in self._tests if t.has_run and t.run_enabled and t.implemented]
        ignored = [t for t in self._tests if not t.run_enabled]
        not_impl = [t for t in self._tests if t.run_enabled and not t.implemented]
        scored = [t for t in ran if t.result_score is not None]
        ok = [t for t in scored if t.status_key == "ok"]
        warn = [t for t in scored if t.status_key == "warn"]
        fail = [t for t in scored if t.status_key == "fail"]

        lines = []
        lines.append("{0} test(s) run, {1} scored, {2} informational, {3} ignored, {4} not implemented.".format(
            len(ran), len(scored), len(ran) - len(scored), len(ignored), len(not_impl)))
        lines.append("Good: {0}   Warning: {1}   Fail: {2}".format(len(ok), len(warn), len(fail)))
        if fail:
            worst = sorted(fail, key=lambda t: t.result_score)[:5]
            lines.append("Key issues (lowest scoring): " + "; ".join(
                "{0} ({1})".format(t.name, t.result_score_text) for t in worst))
        elif warn:
            worst = sorted(warn, key=lambda t: t.result_score)[:5]
            lines.append("Worth a look (warnings): " + "; ".join(
                "{0} ({1})".format(t.name, t.result_score_text) for t in worst))
        else:
            lines.append("No failing or warning tests among what was scored.")
        return "\n".join(lines)

    def _print_html_report(self, overall):
        html = ['<h2 style="font-family:sans-serif;color:#ddd;">DeeHealth Results</h2>']
        if overall is not None:
            html.append('<p style="color:#ddd;font-size:16px;"><b>Overall weighted score: {0:.1f}%</b></p>'
                        .format(overall * 100))
        current_section = None
        for t in self._tests:
            if t.section != current_section:
                current_section = t.section
                html.append('<h3 style="color:#ccc;margin-top:14px;">{0}</h3>'.format(current_section))
            status = _status_for_score(t.result_score)
            if status is not None:
                bg = _BG_BY_STATUS[status]
            elif not t.run_enabled:
                bg = "#9e9e9e"
            elif t.implemented:
                bg = "#455a64"
            else:
                bg = "#616161"
            score_txt = t.result_score_text or ("n/a" if not t.implemented else "not scored")
            html.append(
                '<div style="padding:5px 10px;margin:2px 0;background:{0};color:#fff;'
                'border-radius:4px;font-family:monospace;font-size:12px;">'
                '<b>{1}</b> &mdash; Value: {2} | Score: {3}<br/>{4}</div>'.format(
                    bg, t.name, t.result_value_text or "-", score_txt,
                    (t.result_detail or "").replace("<", "&lt;").replace(">", "&gt;")))
        output.print_html("".join(html))

    # -- export -----------------------------------------------------------------
    def export_csv_click(self, sender, args):
        if not self._tests:
            forms.alert("Nothing to export - import a rubric and run the health check first.")
            return
        dlg = SaveFileDialog()
        dlg.Filter = "CSV files (*.csv)|*.csv"
        dlg.FileName = "DeeHealth_Results.csv"
        if dlg.ShowDialog() != DialogResult.OK:
            return
        try:
            with open(dlg.FileName, "w") as f:
                writer = csv.writer(f)
                writer.writerow(["Section", "Test", "Implemented", "Run", "Status", "Weight", "E", "F", "G",
                                  "Value", "Score", "Description"])
                for t in self._tests:
                    writer.writerow([
                        t.section, t.name, t.implemented_text, "Yes" if t.run_enabled else "No",
                        t.status_label, t.weight_text, t.e_text, t.f_text, t.g_text,
                        t.result_value_text, t.result_score_text, t.description])
        except Exception as e:
            forms.alert("Could not export: {0}".format(e))
            return
        MessageBox.Show("Exported {0} test result(s) to:\n{1}".format(len(self._tests), dlg.FileName),
                        "DeeHealth")

    def export_report_click(self, sender, args):
        if not self._tests:
            forms.alert("Nothing to export - import a rubric and run the health check first.")
            return

        dlg = SaveFileDialog()
        dlg.Filter = "Excel files (*.xlsx)|*.xlsx"
        dlg.FileName = "DeeHealth_Report.xlsx"
        if dlg.ShowDialog() != DialogResult.OK:
            return

        sections = []
        seen_sections = set()
        for t in self._tests:
            if t.section not in seen_sections:
                seen_sections.add(t.section)
                sections.append(t.section)

        section_rows = []
        for section in sections:
            section_tests = [t for t in self._tests if t.section == section and t.is_scored
                              and t.result_score is not None]
            total_weight = sum(t.weight for t in section_tests)
            if total_weight > 0:
                section_score = sum(t.weight * t.result_score for t in section_tests) / total_weight
                status = _status_for_score(section_score)
                score_text = "{0:.0f}%".format(section_score * 100)
            else:
                status = None
                score_text = "N/A"
            any_weight = sum(t.weight for t in self._tests if t.section == section and t.weight is not None)
            section_rows.append((section, "{0:.4f}".format(any_weight), score_text, status))

        detail_rows = []
        for t in self._tests:
            status = _status_for_score(t.result_score)
            if status is None:
                status = "skip" if (not t.implemented or not t.run_enabled) else None
            detail_rows.append(([
                t.section, t.name, t.implemented_text, t.weight_text,
                t.result_value_text, t.result_score_text,
                t.e_text, t.f_text, t.g_text, t.description,
            ], status))

        try:
            xlsx_writer.write_health_report_xlsx(dlg.FileName, self._last_overall, section_rows, detail_rows)
        except Exception as e:
            forms.alert("Could not export report: {0}".format(e))
            return
        MessageBox.Show("Exported health report to:\n{0}".format(dlg.FileName), "DeeHealth")

    def close_click(self, sender, args):
        self.Close()


def _thickness(left, top, right, bottom):
    return Thickness(left, top, right, bottom)


def _brush(hex_color):
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return SolidColorBrush(MediaColor.FromRgb(r, g, b))


def main():
    doc = __revit__.ActiveUIDocument.Document
    window = DeeHealthWindow(_XAML_FILE, doc)
    window.ShowDialog()


main()
