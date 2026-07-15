# -*- coding: utf-8 -*-
"""
DeeHealth (HealthPack)
Imports a QC-test-style Excel rubric (SectionName | SCORE | TestName |
SCORE | E | F | G | Description columns - matching firm QC workbooks
like "Internal REVIT QC Tests.xlsx") into an editable grid, runs every
test that maps to a known check against the current model, and computes
an overall weighted health score.

Architecture: lib/health_rubric.py parses the workbook and does the
Revit-API-free scoring math (unit-tested against a real such workbook -
45/45 test names mapped, weights sum to 1.0000). lib/health_checks.py
holds the ~45 Revit-side check functions, one per check_id, each
returning (value, detail) or raising (caught here per-check, so one bad
check never blocks the rest of the run). This script only wires the two
together with the WPF UI.

Weight/E/F/G stay editable in the grid after import, so the scoring
rubric can be tuned without re-editing the source Excel. Tests with no
Weight are informational only (still run and reported, never affect the
overall score). A test whose name in the workbook doesn't match any
known check_id is marked "Implemented: No" and skipped, rather than
guessed at or silently dropped.

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

from pyrevit import forms, script

import xlsx_reader
import health_rubric
import health_checks

output = script.get_output()

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")

_DEFAULT_NAMING_PATTERN = r"^[A-Za-z0-9_\-\.\s]+$"


class DeeHealthWindow(forms.WPFWindow):
    def __init__(self, xaml_file, doc):
        forms.WPFWindow.__init__(self, xaml_file)
        self.doc = doc
        self._tests = []
        self.naming_pattern_tb.Text = _DEFAULT_NAMING_PATTERN

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
            tests = health_rubric.parse_rubric_grid(grid)
        except Exception as e:
            forms.alert("Could not parse rubric: {0}".format(e))
            return
        if not tests:
            forms.alert("No tests found - check the workbook matches the expected "
                        "SectionName/SCORE/TestName/SCORE/E/F/G/Description layout.")
            return

        for t in tests:
            t.implemented = t.check_id in health_checks.CHECKS

        self._tests = tests
        self.rubric_grid.ItemsSource = None
        self.rubric_grid.ItemsSource = tests

        mapped = sum(1 for t in tests if t.implemented)
        self.rubric_summary_tb.Text = "{0} test(s) loaded from '{1}' ({2} implemented, {3} not implemented).".format(
            len(tests), os.path.basename(dlg.FileName), mapped, len(tests) - mapped)
        self.overall_score_tb.Text = "Overall Score: --"

    def run_health_click(self, sender, args):
        if not self._tests:
            forms.alert("Import a rubric first.")
            return

        ctx = {"naming_pattern": self.naming_pattern_tb.Text or _DEFAULT_NAMING_PATTERN}

        results = []
        with forms.ProgressBar(title="DeeHealth — running checks...", cancellable=True) as pb:
            total = len(self._tests)
            for i, t in enumerate(self._tests):
                if pb.cancelled:
                    break
                pb.update_progress(i, total)
                if not t.implemented or t.check_id is None:
                    t.result_value = None
                    t.result_detail = "Not implemented - no matching check for this test name"
                    t.result_score = None
                    results.append((None, t.name, t.result_detail))
                    continue
                check_fn = health_checks.CHECKS.get(t.check_id)
                try:
                    value, detail = check_fn(self.doc, ctx)
                    t.result_value = value
                    t.result_detail = detail
                    t.result_score = health_rubric.score_test(value, t.e_raw, t.g_raw) if t.is_scored else None
                    results.append((True, t.name, detail))
                except Exception as e:
                    t.result_value = None
                    t.result_detail = "FAILED: {0}".format(e)
                    t.result_score = None
                    results.append((False, t.name, t.result_detail))

        self.rubric_grid.Items.Refresh()

        overall = health_rubric.compute_overall_score(self._tests)
        if overall is None:
            self.overall_score_tb.Text = "Overall Score: -- (nothing scorable)"
        else:
            self.overall_score_tb.Text = "Overall Score: {0:.0f}%".format(overall * 100)

        html = ['<h2 style="font-family:sans-serif;color:#ddd;">DeeHealth Results</h2>']
        if overall is not None:
            html.append('<p style="color:#ddd;font-size:16px;"><b>Overall weighted score: {0:.1f}%</b></p>'
                        .format(overall * 100))
        current_section = None
        for t in self._tests:
            if t.section != current_section:
                current_section = t.section
                html.append('<h3 style="color:#ccc;margin-top:14px;">{0}</h3>'.format(current_section))
            if t.result_score is not None:
                bg = "#2e7d32" if t.result_score >= 0.8 else ("#f9a825" if t.result_score >= 0.4 else "#c62828")
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
                writer.writerow(["Section", "Test", "Implemented", "Weight", "E", "F", "G",
                                  "Value", "Score", "Description"])
                for t in self._tests:
                    writer.writerow([
                        t.section, t.name, t.implemented_text, t.weight_text,
                        t.e_text, t.f_text, t.g_text,
                        t.result_value_text, t.result_score_text, t.description])
        except Exception as e:
            forms.alert("Could not export: {0}".format(e))
            return
        MessageBox.Show("Exported {0} test result(s) to:\n{1}".format(len(self._tests), dlg.FileName),
                        "DeeHealth")

    def close_click(self, sender, args):
        self.Close()


def main():
    doc = __revit__.ActiveUIDocument.Document
    window = DeeHealthWindow(_XAML_FILE, doc)
    window.ShowDialog()


main()
