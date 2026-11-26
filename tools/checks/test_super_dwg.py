# -*- coding: utf-8 -*-
"""Tests for lib/dee_super_dwg.py (DeePrinter's Super DWG: finding AutoCAD
and running the batch). No Revit and no AutoCAD needed:

    python tools/checks/test_super_dwg.py

The AutoCAD step itself (lib/dee_super_dwg.ps1) is swapped for tiny stub
PowerShell scripts that write the result file the real one would. The real
thing is exercised by tools/checks/check_super_dwg_acad.py (needs AutoCAD).
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "lib"))
import dee_super_dwg as sd  # noqa: E402

STUB_OK = r'''
param([string]$JobsFile, [string]$ResultFile)
$jobs = Get-Content -Raw $JobsFile | ConvertFrom-Json
$out = @()
foreach ($j in @($jobs)) {
    $out += @{ out = $j.out; ok = $true; embedded = 2; xrefs_bound = 1; left_linked = 0; message = "stub" }
}
ConvertTo-Json -InputObject @($out) | Set-Content -Encoding UTF8 $ResultFile
'''

STUB_PARTIAL = r'''
param([string]$JobsFile, [string]$ResultFile)
$jobs = Get-Content -Raw $JobsFile | ConvertFrom-Json
$r = @(@{ out = $jobs[0].out; ok = $true; embedded = 1; xrefs_bound = 0; left_linked = 0; message = "first only" })
ConvertTo-Json -InputObject $r | Set-Content -Encoding UTF8 $ResultFile
'''

STUB_CRASH = r'''
param([string]$JobsFile, [string]$ResultFile)
Write-Output "boom: AutoCAD is not installed"
exit 3
'''

STUB_HANG = r'''
param([string]$JobsFile, [string]$ResultFile)
Start-Sleep -Seconds 120
'''


class SuperDwgTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="dee_super_test_")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def stub(self, body):
        p = os.path.join(self.root, "stub.ps1")
        with open(p, "w") as f:
            f.write(body)
        return p

    def jobs(self, n=2):
        return [{"dwg": os.path.join(self.root, "in%d.dwg" % i),
                 "out": os.path.join(self.root, "out%d.dwg" % i)} for i in range(n)]

    def test_no_jobs_returns_nothing_without_starting_anything(self):
        self.assertEqual(sd.run_batch([], script=self.stub(STUB_CRASH)), [])

    def test_results_come_back_in_job_order(self):
        jobs = self.jobs(3)
        res = sd.run_batch(jobs, script=self.stub(STUB_OK), timeout=60)
        self.assertEqual([r["out"] for r in res], [j["out"] for j in jobs])
        for r in res:
            self.assertTrue(r["ok"])
            self.assertEqual((r["embedded"], r["xrefs_bound"], r["left_linked"]), (2, 1, 0))

    def test_a_job_with_no_result_is_reported_failed_not_dropped(self):
        jobs = self.jobs(2)
        res = sd.run_batch(jobs, script=self.stub(STUB_PARTIAL), timeout=60)
        self.assertEqual(len(res), 2)
        self.assertTrue(res[0]["ok"])
        self.assertFalse(res[1]["ok"])
        self.assertIn("no result", res[1]["message"])

    def test_script_that_dies_fails_every_job_with_its_output(self):
        res = sd.run_batch(self.jobs(2), script=self.stub(STUB_CRASH), timeout=60)
        self.assertEqual(len(res), 2)
        for r in res:
            self.assertFalse(r["ok"])
            self.assertIn("boom", r["message"])

    def test_hung_script_is_killed_at_the_timeout(self):
        res = sd.run_batch(self.jobs(1), script=self.stub(STUB_HANG), timeout=3)
        self.assertFalse(res[0]["ok"])
        self.assertIn("did not finish", res[0]["message"])

    def test_temp_work_folder_is_cleaned_up(self):
        before = set(n for n in os.listdir(tempfile.gettempdir()) if n.startswith("dee_superdwg_"))
        sd.run_batch(self.jobs(1), script=self.stub(STUB_OK), timeout=60)
        after = set(n for n in os.listdir(tempfile.gettempdir()) if n.startswith("dee_superdwg_"))
        self.assertEqual(after - before, set())

    def test_find_autocad_picks_newest_full_autocad_and_ignores_lt(self):
        pf = os.path.join(self.root, "pf")
        for name in ("AutoCAD 2024", "AutoCAD 2026", "AutoCAD LT 2027", "AutoCAD 2025"):
            d = os.path.join(pf, "Autodesk", name)
            os.makedirs(d)
            open(os.path.join(d, "accoreconsole.exe"), "w").close()
        os.makedirs(os.path.join(pf, "Autodesk", "AutoCAD 2027"))   # folder but no accoreconsole.exe
        saved = dict((k, os.environ.get(k)) for k in
                     ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"))
        try:
            os.environ["ProgramFiles"] = pf
            os.environ.pop("ProgramW6432", None)
            os.environ.pop("ProgramFiles(x86)", None)
            self.assertEqual(sd.find_autocad(),
                             os.path.join(pf, "Autodesk", "AutoCAD 2026", "accoreconsole.exe"))
            shutil.rmtree(os.path.join(pf, "Autodesk"))
            self.assertIsNone(sd.find_autocad())
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main(verbosity=2)
