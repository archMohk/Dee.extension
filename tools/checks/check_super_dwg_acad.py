# -*- coding: utf-8 -*-
"""End-to-end check of Super DWG against a real AutoCAD. Not part of the
automated tests: it starts AutoCAD's headless engine, takes a minute, and
needs Pillow for the test pictures.

    python tools/checks/check_super_dwg_acad.py [keep]

Builds a drawing with an opaque logo, the same logo rotated 30 degrees, a
logo with a transparent background, a large photograph and an xref; runs
lib/dee_super_dwg on it; then opens the RESULT FROM A FOLDER THAT HOLDS NO
IMAGE OR XREF FILES and checks, picture by picture, that the drawing
carries everything itself. Pass "keep" to leave the work folder in place.
"""
import io
import json
import math
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "lib"))
import dee_super_dwg as sd  # noqa: E402

FAILS = []


def check(ok, what):
    print(("  PASS  " if ok else "  FAIL  ") + what)
    if not ok:
        FAILS.append(what)


def lisp_path(p):
    return p.replace("\\", "/")


def run_core(exe, scr, dwg=None, marker=None, timeout=180):
    """Runs one AutoCAD console script (on a blank drawing unless `dwg`).
    With `marker`, the script is finished once it has written that file: after
    a long LISP loop the console may not get to its own QUIT line, so it is
    given a few seconds and then ended (as the converter itself does)."""
    cmd = [exe] + (["/i", dwg] if dwg else []) + ["/s", scr, "/l", "en-US"]
    with open(scr + ".log", "wb") as out:
        p = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT)
        t0, seen = time.time(), None
        while p.poll() is None:
            if time.time() - t0 > timeout:
                p.kill()
                raise RuntimeError("AutoCAD console timed out on " + scr)
            if marker and os.path.exists(marker):
                seen = seen or time.time()
                if time.time() - seen > 6:
                    p.kill()
                    break
            time.sleep(0.3)
    if marker and not os.path.exists(marker):
        raise RuntimeError("AutoCAD console did not finish " + scr)


def write(path, text):
    with open(path, "w") as f:
        f.write(text)


def make_pictures(work):
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (480, 200), (255, 255, 255))
    d = ImageDraw.Draw(im)
    d.rectangle([10, 10, 230, 190], fill=(200, 30, 30))
    d.ellipse([250, 20, 450, 180], fill=(20, 90, 200))
    im.save(os.path.join(work, "logo.png"))
    bd = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    ImageDraw.Draw(bd).ellipse([20, 20, 180, 180], fill=(30, 160, 60, 255))
    bd.save(os.path.join(work, "badge.png"))
    random.seed(7)
    ph = Image.new("RGB", (1600, 1000))
    px = ph.load()
    for y in range(1000):
        for x in range(1600):
            px[x, y] = (((x * 255) // 1600 + random.randint(-25, 25)) & 255,
                        ((y * 255) // 1000 + random.randint(-25, 25)) & 255,
                        (((x + y) * 255) // 2600 + random.randint(-25, 25)) & 255)
    ph.save(os.path.join(work, "photo.jpg"), quality=90)


# (file, label, origin, width in units, rotation deg) - as attached by make_drawing
PLACEMENTS = [
    ("logo.png", "logo", (1000, 500), 240, 0),
    ("logo.png", "logo rotated 30", (2000, 500), 240, 30),
    ("badge.png", "transparent badge", (1000, 1000), 100, 0),
    ("photo.jpg", "photo", (3000, 500), 320, 0),
]


def make_drawings(exe, work):
    w = lisp_path(work)
    write(os.path.join(work, "ref.scr"), "\n".join([
        '(entmake \'((0 . "CIRCLE") (10 0.0 0.0 0.0) (40 . 50.0)))',
        '_.SAVEAS 2013 "%s/ref.dwg"' % w, "_.QUIT _Y"]) + "\n")
    run_core(exe, os.path.join(work, "ref.scr"))
    lines = ['(entmake \'((0 . "LINE") (10 0.0 0.0 0.0) (11 5000.0 0.0 0.0)))',
             '(entmake \'((0 . "LINE") (10 0.0 0.0 0.0) (11 0.0 2000.0 0.0)))']
    for name, _label, (x, y), width, rot in PLACEMENTS:
        lines.append('_.-IMAGE _A "%s/%s" %d,%d %d %d' % (w, name, x, y, width, rot))
    lines += ['_.-XREF _A "%s/ref.dwg" 3000,0 1 1 0' % w,
              '_.SAVEAS 2013 "%s/main.dwg"' % w, "_.QUIT _Y"]
    write(os.path.join(work, "make.scr"), "\n".join(lines) + "\n")
    run_core(exe, os.path.join(work, "make.scr"))


VERIFY = r'''(setq dee-out "%s/verify.txt")
(defun dee-cnt (flt / ss) (if (setq ss (ssget "_X" flt)) (sslength ss) 0))
(defun dee-verify ( / f ss rec k dict c b e)
  (setq f (open dee-out "w"))
  (write-line (strcat "DWG|" (getvar "DWGNAME")) f)
  (write-line (strcat "IMAGES|" (itoa (dee-cnt '((0 . "IMAGE"))))) f)
  (write-line (strcat "LINES|" (itoa (dee-cnt '((0 . "LINE"))))) f)
  (setq k 0)
  (if (setq dict (dictsearch (namedobjdict) "ACAD_IMAGE_DICT")) (foreach it dict (if (= (car it) 350) (setq k (1+ k)))))
  (write-line (strcat "IMAGEDEFS|" (itoa k)) f)
  (setq k 0 rec (tblnext "BLOCK" T))
  (while rec (if (/= 0 (logand 4 (cdr (assoc 70 rec)))) (setq k (1+ k))) (setq rec (tblnext "BLOCK")))
  (write-line (strcat "XREFBLOCKS|" (itoa k)) f)
  (setq c 0 b (tblnext "BLOCK" T))
  (while b
    (if (and (= 0 (logand 4 (cdr (assoc 70 b)))) (not (wcmatch (cdr (assoc 2 b)) "`**")))
      (progn (setq e (cdr (assoc -2 b)))
             (while e (if (= (cdr (assoc 0 (entget e))) "CIRCLE") (setq c (1+ c))) (setq e (entnext e)))))
    (setq b (tblnext "BLOCK")))
  (write-line (strcat "CIRCLES_IN_BLOCKS|" (itoa c)) f)
  (close f)
  (princ))
(dee-verify)
_.SAVEAS _DXF 8 "%s/res.dxf"
(close (open "%s/done.txt" "w"))
_.QUIT _Y
'''


def read_verify(path):
    """(counts the LISP wrote, solids [(4 corner points A B D C, rgb)] from the DXF)."""
    info = {}
    with io.open(path, "r", encoding="cp1252") as f:
        for line in f:
            line = line.rstrip("\r\n")
            if "|" in line:
                k, v = line.split("|", 1)
                info[k] = v
    solids = []
    with io.open(os.path.join(os.path.dirname(path), "res.dxf"), "r", encoding="cp1252") as f:
        lines = f.read().split("\n")
    cur = None
    for i in range(0, len(lines) - 1, 2):
        code, val = lines[i].strip(), lines[i + 1].strip()
        if code == "0":
            if cur is not None and cur.get("type") == "SOLID":
                solids.append(([(cur[10], cur[20]), (cur[11], cur[21]), (cur[12], cur[22]), (cur[13], cur[23])], cur[420]))
            cur = {"type": val}
        elif cur is not None and cur.get("type") == "SOLID" and code in ("10", "20", "11", "21", "12", "22", "13", "23"):
            cur[int(code)] = float(val)
        elif cur is not None and cur.get("type") == "SOLID" and code == "420":
            cur[420] = int(val)
    if cur is not None and cur.get("type") == "SOLID":
        solids.append(([(cur[10], cur[20]), (cur[11], cur[21]), (cur[12], cur[22]), (cur[13], cur[23])], cur[420]))
    return info, solids


def render_back(solids, origin, width, rot_deg, size):
    """Rebuild one placed picture from the solids that fall inside it:
    ({(col, row counted from the bottom): rgb}, how many solids)."""
    W, H = size
    u_len = width / float(W)
    r = math.radians(rot_deg)
    u = (u_len * math.cos(r), u_len * math.sin(r))
    v = (-u_len * math.sin(r), u_len * math.cos(r))
    det = u[0] * v[1] - u[1] * v[0]

    def local(p):
        dx, dy = p[0] - origin[0], p[1] - origin[1]
        return ((dx * v[1] - dy * v[0]) / det, (-dx * u[1] + dy * u[0]) / det)

    grid, count = {}, 0
    for pts, rgb in solids:
        la, lc = local(pts[0]), local(pts[3])        # entmake order: A(bottom-left) B D C(top-right)
        if not (-0.01 <= la[0] <= W + 0.01 and -0.01 <= lc[0] <= W + 0.01 and
                -0.01 <= la[1] <= H + 0.01 and -0.01 <= lc[1] <= H + 0.01):
            continue
        count += 1
        col = ((rgb >> 16) & 255, (rgb >> 8) & 255, rgb & 255)
        for vv in range(int(round(la[1])), int(round(lc[1]))):
            for cc in range(int(round(la[0])), int(round(lc[0]))):
                grid[(cc, vv)] = col
    return grid, count


def compare(png, grid, count, label, name):
    W, H = png.size
    px = png.load()
    uncovered = bad = wanted_empty = filled_wrongly = covered = 0
    total_err = 0
    sum_exp = [0, 0, 0]
    sum_got = [0, 0, 0]
    for r in range(H):
        for c in range(W):
            exp = px[c, r]
            got = grid.get((c, H - 1 - r))
            if exp[3] < 128:
                wanted_empty += 1
                filled_wrongly += got is not None
                continue
            if got is None:
                uncovered += 1
                continue
            covered += 1
            err = max(abs(got[i] - exp[i]) for i in range(3))
            total_err += err
            bad += err > 4
            for i in range(3):
                sum_exp[i] += exp[i]
                sum_got[i] += got[i]
    if name == "photo.jpg":
        # Pure random noise on a gradient: simplifying must average the noise away, so a
        # per-pixel error of 20-30 is expected. What must hold: every pixel covered and the
        # picture's overall colour unchanged.
        mean = total_err / float(max(1, covered))
        drift = max(abs(sum_exp[i] - sum_got[i]) for i in range(3)) / float(max(1, covered))
        check(uncovered == 0 and mean < 40 and drift < 4,
              "%s: fully covered, overall colour drift %.2f, simplified to %d fills for %d pixels (per-pixel error %.0f is the noise it averaged out)"
              % (label, drift, count, W * H, mean))
    elif name == "badge.png":
        check(uncovered == 0 and filled_wrongly == 0 and bad == 0 and wanted_empty > 0,
              "%s: transparent corners left empty (%d px), opaque disc exact" % (label, wanted_empty))
    else:
        check(uncovered == 0 and bad == 0, "%s: all %d pixels reproduced (%d fills)" % (label, W * H, count))


def main():
    # DEE_ACAD_CORE pins one accoreconsole.exe (here and in the converter itself).
    exe = os.environ.get("DEE_ACAD_CORE") or sd.find_autocad()
    if not exe:
        print("No full AutoCAD found - nothing to check.")
        return 0
    from PIL import Image
    base = os.path.join(os.environ.get("ProgramData", "C:\\ProgramData"), "DeePack")
    if not os.path.isdir(base):
        os.makedirs(base)
    work = tempfile.mkdtemp(prefix="check_super_", dir=base)       # ASCII path: AutoCAD scripts need it
    started = time.time()
    try:
        print("work folder:", work)
        make_pictures(work)
        print("building the test drawing with AutoCAD ...")
        make_drawings(exe, work)
        main_dwg = os.path.join(work, "main.dwg")
        check(os.path.isfile(main_dwg), "test drawing was built")
        if not os.path.isfile(main_dwg):
            return 1

        out_dwg = os.path.join(work, "main_super.dwg")
        print("running Super DWG ...")
        t0 = time.time()
        res = sd.run_batch([{"dwg": main_dwg, "out": out_dwg}], timeout=400)[0]
        print("  ->", json.dumps(res), " (%.1f s)" % (time.time() - t0))
        check(res["ok"], "Super DWG reported success")
        check(res["embedded"] == 4, "all 4 pictures embedded (got %s)" % res["embedded"])
        check(res["xrefs_bound"] == 1, "the xref was bound (got %s)" % res["xrefs_bound"])
        check(res["left_linked"] == 0, "nothing left linked (got %s)" % res["left_linked"])
        check("simplified" in res["message"], "the big photo was simplified to keep the file light")
        if not os.path.isfile(out_dwg):
            return 1

        iso = os.path.join(work, "isolated")       # a folder with NO images and NO xref file
        os.makedirs(iso)
        iso_dwg = os.path.join(iso, "result.dwg")
        shutil.copy(out_dwg, iso_dwg)
        write(os.path.join(iso, "verify.scr"), VERIFY % (lisp_path(iso), lisp_path(iso), lisp_path(iso)))
        run_core(exe, os.path.join(iso, "verify.scr"), dwg=iso_dwg, marker=os.path.join(iso, "done.txt"))
        info, solids = read_verify(os.path.join(iso, "verify.txt"))
        print("the result, opened on its own, contains:", info, "- %d solids" % len(solids))
        check(info.get("DWG", "").lower() == "result.dwg", "verified the result drawing itself")
        check(info.get("IMAGES") == "0", "no raster images remain")
        check(info.get("IMAGEDEFS") == "0", "no image links remain")
        check(info.get("XREFBLOCKS") == "0", "no external references remain")
        check(info.get("LINES") == "2", "the original lines are still there")
        check(info.get("CIRCLES_IN_BLOCKS") == "1", "the xref's circle is now inside the drawing")
        with open(iso_dwg, "rb") as f:
            check(f.read(6) == b"AC1027", "same DWG version as the input (2013)")
        check(len(solids) > 0, "the pictures are drawn as solid fills (%d)" % len(solids))

        for name, label, origin, width, rot in PLACEMENTS:
            png = Image.open(os.path.join(work, name)).convert("RGBA")
            grid, n = render_back(solids, origin, width, rot, png.size)
            compare(png, grid, n, label, name)
        print("\n%s  (%.0f s)" % ("ALL CHECKS PASSED" if not FAILS else "%d CHECK(S) FAILED" % len(FAILS), time.time() - started))
        return 0 if not FAILS else 1
    finally:
        if "keep" in sys.argv:
            print("kept:", work)
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
