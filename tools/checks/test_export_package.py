# -*- coding: utf-8 -*-
"""Tests for lib/dee_export_package.py (DeePrinter's "keep each sheet's
images with its DWG/DXF" option). No Revit needed:

    python tools/checks/test_export_package.py

The "Revit export" is a stand-in function that writes files the way Revit
does: <name>.dwg plus whatever image files the sheet uses, into the folder
it is given.
"""
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "lib"))
import dee_export_package as pkg  # noqa: E402


def fake_export(name, extras=(), ext=".dwg"):
    """What Revit leaves in the target folder for one sheet."""
    def _do(target):
        for fname in [name + ext] + list(extras):
            with open(os.path.join(target, fname), "wb") as f:
                f.write(b"x")
    return _do


class ExportPackageTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="dee_pkg_test_")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def listing(self):
        out = []
        for r, _d, files in os.walk(self.root):
            for f in files:
                out.append(os.path.relpath(os.path.join(r, f), self.root).replace("\\", "/"))
        return sorted(out)

    def test_off_exports_straight_into_folder(self):
        images, note = pkg.export_with_images(
            fake_export("A101", ["logo.png"]), self.root, "A101", package=False)
        self.assertEqual((images, note), ([], ""))
        self.assertEqual(self.listing(), ["A101.dwg", "logo.png"])

    def test_sheet_with_images_gets_its_own_folder(self):
        images, note = pkg.export_with_images(
            fake_export("A101", ["logo.png", "pic.JPG"]), self.root, "A101")
        self.assertEqual(images, ["logo.png", "pic.JPG"])
        self.assertEqual(note, "")
        self.assertEqual(self.listing(),
                         ["A101/A101.dwg", "A101/logo.png", "A101/pic.JPG"])

    def test_sheet_without_images_lands_flat_and_leaves_no_folder(self):
        images, _ = pkg.export_with_images(fake_export("A102"), self.root, "A102")
        self.assertEqual(images, [])
        self.assertEqual(self.listing(), ["A102.dwg"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "A102")))

    def test_non_image_extras_move_up_with_the_dwg(self):
        pkg.export_with_images(fake_export("A103", ["A103.pcp"]), self.root, "A103")
        self.assertEqual(self.listing(), ["A103.dwg", "A103.pcp"])

    def test_dxf_works_the_same(self):
        images, _ = pkg.export_with_images(
            fake_export("A104", ["logo.bmp"], ext=".dxf"), self.root, "A104")
        self.assertEqual(images, ["logo.bmp"])
        self.assertEqual(self.listing(), ["A104/A104.dxf", "A104/logo.bmp"])

    def test_two_sheets_sharing_a_logo_each_get_a_copy(self):
        pkg.export_with_images(fake_export("A1", ["logo.png"]), self.root, "A1")
        pkg.export_with_images(fake_export("A2", ["logo.png"]), self.root, "A2")
        self.assertEqual(self.listing(), [
            "A1/A1.dwg", "A1/logo.png", "A2/A2.dwg", "A2/logo.png"])

    def test_reexport_keeps_it_packaged(self):
        pkg.export_with_images(fake_export("A1", ["logo.png"]), self.root, "A1")
        images, _ = pkg.export_with_images(fake_export("A1", ["logo.png"]), self.root, "A1")
        self.assertEqual(images, ["logo.png"])
        self.assertEqual(self.listing(), ["A1/A1.dwg", "A1/logo.png"])

    def test_flattening_replaces_an_older_flat_dwg(self):
        with open(os.path.join(self.root, "A5.dwg"), "wb") as f:
            f.write(b"old-old-old")
        pkg.export_with_images(fake_export("A5"), self.root, "A5")
        self.assertEqual(self.listing(), ["A5.dwg"])
        self.assertEqual(os.path.getsize(os.path.join(self.root, "A5.dwg")), 1)

    def test_failed_export_removes_the_empty_folder_and_reraises(self):
        def boom(_target):
            raise RuntimeError("Document.Export returned False")
        with self.assertRaises(RuntimeError):
            pkg.export_with_images(boom, self.root, "A6")
        self.assertFalse(os.path.exists(os.path.join(self.root, "A6")))

    def test_failed_export_keeps_a_folder_that_already_had_files(self):
        os.makedirs(os.path.join(self.root, "A7"))
        with open(os.path.join(self.root, "A7", "old.png"), "wb") as f:
            f.write(b"x")

        def boom(_target):
            raise RuntimeError("no")
        with self.assertRaises(RuntimeError):
            pkg.export_with_images(boom, self.root, "A7")
        self.assertEqual(self.listing(), ["A7/old.png"])

    def test_unrelated_file_in_existing_folder_is_not_touched(self):
        os.makedirs(os.path.join(self.root, "A8"))
        with open(os.path.join(self.root, "A8", "notes.txt"), "wb") as f:
            f.write(b"keep me")
        pkg.export_with_images(fake_export("A8"), self.root, "A8")
        self.assertEqual(self.listing(), ["A8.dwg", "A8/notes.txt"])

    def test_too_long_path_falls_back_to_flat_with_a_note(self):
        name = "N" * 120
        images, note = pkg.export_with_images(
            fake_export(name, ["logo.png"]), self.root, name)
        self.assertEqual(images, [])
        self.assertIn("too long", note)
        self.assertEqual(self.listing(), [name + ".dwg", "logo.png"])

    def test_file_with_the_folder_name_falls_back_to_flat_with_a_note(self):
        with open(os.path.join(self.root, "A9"), "wb") as f:
            f.write(b"x")
        images, note = pkg.export_with_images(
            fake_export("A9", ["logo.png"]), self.root, "A9")
        self.assertEqual(images, [])
        self.assertIn("already in the folder", note)
        self.assertEqual(self.listing(), ["A9", "A9.dwg", "logo.png"])

    def test_is_image(self):
        for n in ("a.PNG", "b.jpeg", "c.tif", "d.TIFF", "e.bmp", "f.gif"):
            self.assertTrue(pkg.is_image(n), n)
        for n in ("a.dwg", "b.dxf", "c.pcp", "d.pdf", "png"):
            self.assertFalse(pkg.is_image(n), n)


if __name__ == "__main__":
    unittest.main(verbosity=2)
