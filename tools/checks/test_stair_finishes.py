# -*- coding: utf-8 -*-
"""Tests for DeeStairs' finish catalogue and section sketch (Model.panel/
DeeStairs.pushbutton/stair_finishes.py). No Revit needed:

    python tools/checks/test_stair_finishes.py

The catalogue must be self-consistent (every preset names materials that exist,
no character Revit forbids in a name, sensible thicknesses) and the section
sketch must put each part where the detail drawings put it.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "DeePack.tab", "Model.panel", "DeeStairs.pushbutton"))
import stair_finishes as SF  # noqa: E402

FORBIDDEN = u"{}[]|;<>?`~"


def bounds(poly):
    xs = [p[0] for p in poly]
    zs = [p[1] for p in poly]
    return min(xs), min(zs), max(xs), max(zs)


def area(poly):
    a = 0.0
    for i in range(len(poly)):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % len(poly)]
        a += x0 * y1 - x1 * y0
    return abs(a) / 2.0


class CatalogueTests(unittest.TestCase):
    def test_material_keys_unique_and_complete(self):
        keys = [m["key"] for m in SF.MATERIALS]
        self.assertEqual(len(keys), len(set(keys)))
        for m in SF.MATERIALS:
            self.assertTrue(m["name"].strip())
            self.assertEqual(len(m["rgb"]), 3)
            self.assertTrue(all(0 <= c <= 255 for c in m["rgb"]))
            self.assertTrue(m["cls"])
            self.assertIsInstance(m["find"], tuple)
            self.assertTrue(0 <= m.get("transparency", 0) <= 100)

    def test_revit_forbidden_characters(self):
        for m in SF.MATERIALS:
            for ch in SF.material_name(m):
                self.assertNotIn(ch, FORBIDDEN, m["name"])
        for p in SF.PRESETS:
            for ch in p["name"] + p["short"]:
                self.assertNotIn(ch, FORBIDDEN, p["name"])

    def test_names_are_unique(self):
        names = [SF.material_name(m) for m in SF.MATERIALS]
        self.assertEqual(len(names), len(set(names)))
        pn = [p["name"] for p in SF.PRESETS]
        self.assertEqual(len(pn), len(set(pn)))
        pk = [p["key"] for p in SF.PRESETS]
        self.assertEqual(len(pk), len(set(pk)))
        ps = [p["short"] for p in SF.PRESETS]
        self.assertEqual(len(ps), len(set(ps)))
        for p in SF.PRESETS:
            self.assertTrue(p["short"].strip())

    def test_presets_refer_to_real_materials(self):
        for p in SF.PRESETS:
            for role in ("tread", "riser", "stringer", "landing"):
                v = p[role]
                self.assertTrue(v is None or v in SF.MATERIAL_BY_KEY, (p["key"], role, v))
            self.assertIsNotNone(p["tread"])
            self.assertTrue(p["info"])

    def test_preset_dimensions_are_sensible(self):
        for p in SF.PRESETS:
            self.assertTrue(10 <= p["tread_thk"] <= 100, p["key"])
            self.assertGreaterEqual(p["nosing"], 0)
            self.assertLessEqual(p["nosing"], 50)
            if p["open"]:
                self.assertIsNone(p["riser_thk"], p["key"])
            else:
                self.assertTrue(10 <= p["riser_thk"] <= 60, p["key"])
            for side in ("left", "right", "mid"):
                self.assertIn(p[side], ("closed", "open", "carriage", "none"), (p["key"], side))
            if p["mid"] != "none":
                self.assertGreaterEqual(p.get("midn", 0), 1)
            if p.get("stringer_w") is not None:
                self.assertTrue(5 <= p["stringer_w"] <= 300)

    def test_a_preset_with_stringers_has_a_stringer_material(self):
        for p in SF.PRESETS:
            if "none" != p["left"] or "none" != p["right"] or "none" != p["mid"]:
                self.assertIsNotNone(p["stringer"], p["key"])

    def test_glass_is_transparent_and_the_rest_is_not(self):
        self.assertGreater(SF.MATERIAL_BY_KEY["glass"].get("transparency", 0), 0)
        for k, m in SF.MATERIAL_BY_KEY.items():
            if k != "glass":
                self.assertEqual(m.get("transparency", 0), 0)

    def test_the_reference_stairs_are_all_there(self):
        keys = set(p["key"] for p in SF.PRESETS)
        for k in ("cherry", "oak_white", "timber_steel", "carriage", "floating", "marble"):
            self.assertIn(k, keys)

    def test_rgb_hex(self):
        self.assertEqual(SF.rgb_hex((0, 0, 0)), u"#000000")
        self.assertEqual(SF.rgb_hex((255, 128, 1)), u"#FF8001")


class MatchingTests(unittest.TestCase):
    def test_whole_words_only_for_short_words(self):
        self.assertTrue(SF.name_matches(u"Wood - Ash", ("ash",)))
        self.assertFalse(SF.name_matches(u"Washed concrete", ("ash",)))
        self.assertFalse(SF.name_matches(u"Ashlar stone", ("ash",)))

    def test_longer_words_match_prefixes(self):
        self.assertTrue(SF.name_matches(u"Marble, Carrara", ("marble",)))
        self.assertTrue(SF.name_matches(u"Cherry wood finish", ("cherry",)))
        self.assertTrue(SF.name_matches(u"Cherries", ("cherry",)) is False)
        self.assertFalse(SF.name_matches(u"", ("oak",)))
        self.assertFalse(SF.name_matches(None, ("oak",)))

    def test_best_asset_is_the_plainest(self):
        names = [u"Wood - Oak, Rustic Finish", u"Oak", u"Floor - Oak Plank", u"Concrete"]
        self.assertEqual(SF.best_asset_name(names, ("oak",)), u"Oak")
        self.assertIsNone(SF.best_asset_name(names, ("marble",)))
        self.assertIsNone(SF.best_asset_name([], ("oak",)))

    def test_avoid_words_keep_the_wrong_metal_out(self):
        names = [u"Metal - Stainless Steel", u"Metal - Steel, Painted"]
        self.assertEqual(SF.best_asset_name(names, ("steel",), ("stainless",)), u"Metal - Steel, Painted")
        self.assertIsNone(SF.best_asset_name([u"Stainless Steel"], ("steel",), ("stainless",)))
        spec = SF.MATERIAL_BY_KEY["steel"]
        self.assertIn("stainless", spec["avoid"])
        self.assertEqual(SF.MATERIAL_BY_KEY["white"]["find"], ())
        self.assertIsNone(SF.best_asset_name([u"White Oak"], SF.MATERIAL_BY_KEY["white"]["find"]))

    def test_support_style(self):
        self.assertEqual(SF.support_style(u"Stringer - Closed"), "closed")
        self.assertEqual(SF.support_style(u"Stringer - Open"), "open")
        self.assertEqual(SF.support_style(u"Carriage - Open"), "carriage")
        self.assertEqual(SF.support_style(u"Stringer 200x50"), "closed")
        self.assertIsNone(SF.support_style(u"Something else"))
        self.assertIsNone(SF.support_style(None))

    def test_match_support(self):
        names = [u"Carriage - Open", u"Stringer - Closed", u"Stringer - Open"]
        self.assertEqual(SF.match_support(names, "carriage"), 0)
        self.assertEqual(SF.match_support(names, "closed"), 1)
        self.assertEqual(SF.match_support(names, "open"), 2)
        self.assertIsNone(SF.match_support([u"Plain"], "closed"))
        self.assertIsNone(SF.match_support([], "open"))

    def test_section_stringer(self):
        self.assertEqual(SF.section_stringer("closed", "none", "none"), "closed")
        self.assertEqual(SF.section_stringer("none", "open", "none"), "open")
        self.assertEqual(SF.section_stringer("none", "none", "carriage"), "carriage")
        self.assertEqual(SF.section_stringer("none", "none", "closed"), "carriage")
        self.assertEqual(SF.section_stringer(None, None, None), "none")
        self.assertEqual(SF.section_stringer("none", "none", "none"), "none")


class SectionTests(unittest.TestCase):
    R, T = 178.0, 280.0

    def sec(self, **kw):
        a = dict(R=self.R, T=self.T, tread_thk=38.0, riser_thk=19.0, nosing=19.0,
                 has_riser=True, stringer="none", steps=4)
        a.update(kw)
        return SF.section(**a)

    def test_counts(self):
        s = self.sec()
        self.assertEqual(len(s["treads"]), 4)
        self.assertEqual(len(s["risers"]), 4)
        self.assertEqual(s["stringer"], [])
        self.assertEqual(len(s["floor"]), 1)
        self.assertEqual(len(self.sec(steps=7)["treads"]), 7)

    def test_tread_sits_on_its_riser_and_overhangs_it(self):
        s = self.sec()
        for i, (tr, ri) in enumerate(zip(s["treads"], s["risers"])):
            tx0, tz0, tx1, tz1 = bounds(tr)
            rx0, rz0, rx1, rz1 = bounds(ri)
            self.assertAlmostEqual(tz1, (i + 1) * self.R)                 # top of the step
            self.assertAlmostEqual(tz1 - tz0, 38.0)                       # finished thickness
            self.assertAlmostEqual(rz1, tz0)                              # riser meets the tread's underside
            self.assertAlmostEqual(rz0, i * self.R)                       # riser stands on the step below
            self.assertAlmostEqual(rx0 - tx0, 19.0)                       # tread overhangs by the nosing
            self.assertAlmostEqual(rx1 - rx0, 19.0)                       # riser thickness
            self.assertAlmostEqual(tx0, i * self.T)                       # going

    def test_each_tread_runs_under_the_next_riser(self):
        s = self.sec()
        for i in range(3):
            tx1 = bounds(s["treads"][i])[2]
            nx0, _, nx1, _ = bounds(s["risers"][i + 1])
            self.assertGreaterEqual(tx1 + 1e-9, nx1)

    def test_open_risers_have_no_riser_boards(self):
        s = self.sec(has_riser=False, riser_thk=None, nosing=0.0, tread_thk=70.0)
        self.assertEqual(s["risers"], [])
        self.assertEqual(len(s["treads"]), 4)
        for i, tr in enumerate(s["treads"]):
            x0, z0, x1, z1 = bounds(tr)
            self.assertAlmostEqual(z1 - z0, 70.0)
            self.assertAlmostEqual(x0, i * self.T)

    def test_zero_thickness_still_draws_and_thickness_is_capped(self):
        s = self.sec(tread_thk=0.0)
        self.assertGreater(bounds(s["treads"][0])[3] - bounds(s["treads"][0])[1], 0.0)
        s = self.sec(tread_thk=5000.0)
        h = bounds(s["treads"][0])[3] - bounds(s["treads"][0])[1]
        self.assertLess(h, self.R)

    def test_closed_stringer_follows_the_pitch(self):
        s = self.sec(stringer="closed")
        self.assertEqual(len(s["stringer"]), 1)
        (x0, z0), (x1, z1), (x2, z2), (x3, z3) = s["stringer"][0]
        self.assertAlmostEqual((z1 - z0) / (x1 - x0), self.R / self.T, places=6)
        self.assertAlmostEqual(z0, self.R)                                # through the first nosing
        self.assertAlmostEqual(z1, 4 * self.R)                            # through the last nosing
        self.assertGreater(area(s["stringer"][0]), 0)

    def test_open_stringer_is_a_zigzag_band(self):
        s = self.sec(stringer="open")
        poly = s["stringer"][0]
        self.assertEqual(len(poly), 4 * 2 * 2)
        self.assertGreater(area(poly), 0)
        zs = sorted(set(round(p[1], 6) for p in poly))
        self.assertGreater(len(zs), 4)

    def test_carriage_is_slimmer_than_closed(self):
        a = area(self.sec(stringer="closed")["stringer"][0])
        b = area(self.sec(stringer="carriage")["stringer"][0])
        self.assertLess(b, a)

    def test_bbox_contains_everything(self):
        for st in ("none", "closed", "open", "carriage"):
            s = self.sec(stringer=st)
            x0, z0, x1, z1 = s["bbox"]
            for k in ("treads", "risers", "stringer", "floor"):
                for poly in s[k]:
                    for x, z in poly:
                        self.assertTrue(x0 - 1e-9 <= x <= x1 + 1e-9 and z0 - 1e-9 <= z <= z1 + 1e-9)

    def test_stringer_never_goes_far_below_the_floor(self):
        for R, T in ((120.0, 300.0), (178.0, 280.0), (220.0, 230.0)):
            s = self.sec(R=R, T=T, stringer="closed")
            self.assertGreaterEqual(s["bbox"][1], -60.0 - 1e-9)

    def test_every_preset_draws(self):
        for p in SF.PRESETS:
            st = SF.section_stringer(p["left"], p["right"], p["mid"])
            s = SF.section(self.R, self.T, p["tread_thk"], p["riser_thk"], p["nosing"], not p["open"], st)
            self.assertEqual(len(s["treads"]), 4)
            self.assertEqual(len(s["risers"]), 0 if p["open"] else 4)
            self.assertEqual(bool(s["stringer"]), st != "none")
            x0, z0, x1, z1 = s["bbox"]
            self.assertTrue(all(abs(v) < 1e9 for v in (x0, z0, x1, z1)))


if __name__ == "__main__":
    unittest.main(verbosity=1)
