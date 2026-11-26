# Super DWG - makes a Revit-exported DWG self-contained, using AutoCAD's
# headless engine (accoreconsole.exe: no window, no clipboard, no plugins).
#
# Called by lib/dee_super_dwg.py (see its docstring for the why):
#   powershell -Sta -File dee_super_dwg.ps1 -JobsFile jobs.json -ResultFile results.json
#
# jobs.json    [{ "dwg": "<exported drawing>", "out": "<where to save the result>" }, ...]
# results.json [{ "out", "ok", "embedded", "xrefs_bound", "left_linked", "message" }, ...]
#
# How a drawing is made self-contained
#   pass 1  AutoCAD reads each drawing and lists its raster images (where they
#           sit, their size and rotation, the file they link to).
#   here    every image file is turned into a set of solid-colour rectangles
#           (neighbouring pixels of one colour are merged; fully transparent
#           pixels are left out).
#   pass 2  AutoCAD draws those rectangles as SOLID fills on the image's own
#           layer, in the image's place, deletes the image and its link, binds
#           every external reference (xref) into the drawing, and saves in
#           the same DWG version as the input.
# An image that cannot be handled (file missing, clipped to a part of the
# picture, inside a block) is left linked and counted in left_linked, so the
# caller knows the drawing still needs its image files.
#
# AutoCAD only ever sees plain-ASCII copies in a temp folder, so project
# names in any language cannot upset its script reader.

param(
    [Parameter(Mandatory = $true)][string]$JobsFile,
    [Parameter(Mandatory = $true)][string]$ResultFile
)
$ErrorActionPreference = 'Stop'

# Write-Host, never Write-Output: a function's output is its return value.
function Log([string]$m) { Write-Host ('[{0}] {1}' -f (Get-Date -Format 'HH:mm:ss'), $m) }

Add-Type -ReferencedAssemblies System.Drawing -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;

public class DeeRasterResult {
    public List<string> Lines = new List<string>();
    public int Width, Height, Step, Bits;
}

public static class DeeRaster {
    public static string Hash(string path) {
        using (MD5 md5 = MD5.Create())
        using (FileStream fs = File.OpenRead(path)) {
            byte[] h = md5.ComputeHash(fs);
            StringBuilder sb = new StringBuilder();
            foreach (byte b in h) sb.Append(b.ToString("x2"));
            return sb.ToString();
        }
    }

    static int[] LoadArgb(string path, out int w, out int h) {
        using (Bitmap src = new Bitmap(path)) {
            w = src.Width; h = src.Height;
            BitmapData data = src.LockBits(new Rectangle(0, 0, w, h), ImageLockMode.ReadOnly, PixelFormat.Format32bppArgb);
            try {
                int[] px = new int[w * h];
                for (int y = 0; y < h; y++)
                    Marshal.Copy(IntPtr.Add(data.Scan0, y * data.Stride), px, y * w, w);
                return px;
            } finally { src.UnlockBits(data); }
        }
    }

    // Detail levels, finest first: block size in pixels, bits per colour channel.
    static readonly int[][] Levels = new int[][] {
        new int[] {1, 6}, new int[] {1, 5}, new int[] {2, 5}, new int[] {2, 4},
        new int[] {4, 4}, new int[] {8, 3}, new int[] {16, 3} };

    static int Quant(double v, int bits) {
        int levels = (1 << bits) - 1;
        return (int)Math.Round(Math.Round(v * levels / 255.0) * 255.0 / levels);
    }

    // Rectangles "c0 v0 c1 v1 rgb" in pixel units (v counted up from the picture's
    // bottom edge). The finest level that stays within maxRects wins, so a flat
    // logo keeps its exact edges and a photograph degrades instead of exploding.
    public static DeeRasterResult Build(string path, int maxRects) {
        DeeRasterResult res = new DeeRasterResult();
        int w, h;
        int[] px = LoadArgb(path, out w, out h);
        res.Width = w; res.Height = h;
        for (int li = 0; li < Levels.Length; li++) {
            int step = Levels[li][0], bits = Levels[li][1];
            List<string> lines = Merge(px, w, h, step, bits);
            res.Lines = lines; res.Step = step; res.Bits = bits;
            if (lines.Count <= maxRects) break;
        }
        return res;
    }

    static List<string> Merge(int[] px, int w, int h, int step, int bits) {
        int cw = (w + step - 1) / step, ch = (h + step - 1) / step;
        List<string> outLines = new List<string>();
        Dictionary<string, int[]> open = new Dictionary<string, int[]>();   // key -> {x0, x1, rgb, y0}
        int[] cell = new int[cw];
        for (int cy = 0; cy <= ch; cy++) {
            Dictionary<string, int[]> cur = new Dictionary<string, int[]>();
            if (cy < ch) {
                for (int cx = 0; cx < cw; cx++) {
                    double sr = 0, sg = 0, sb = 0, sa = 0; int n = 0;
                    int yEnd = Math.Min(h, (cy + 1) * step), xEnd = Math.Min(w, (cx + 1) * step);
                    for (int y = cy * step; y < yEnd; y++) {
                        for (int x = cx * step; x < xEnd; x++) {
                            int p = px[y * w + x];
                            double a = (p >> 24) & 255;
                            double k = a / 255.0;
                            sr += ((p >> 16) & 255) * k + 255 * (1 - k);     // flatten onto white
                            sg += ((p >> 8) & 255) * k + 255 * (1 - k);
                            sb += (p & 255) * k + 255 * (1 - k);
                            sa += a; n++;
                        }
                    }
                    if (n == 0 || sa / n < 128) { cell[cx] = -1; continue; }   // transparent: no fill
                    cell[cx] = (Quant(sr / n, bits) << 16) | (Quant(sg / n, bits) << 8) | Quant(sb / n, bits);
                }
                int i = 0;
                while (i < cw) {
                    if (cell[i] < 0) { i++; continue; }
                    int j = i;
                    while (j + 1 < cw && cell[j + 1] == cell[i]) j++;
                    string key = i + "," + (j + 1) + "," + cell[i];
                    int[] prev;
                    if (open.TryGetValue(key, out prev)) cur[key] = prev;                  // continues the rectangle above
                    else cur[key] = new int[] { i, j + 1, cell[i], cy };
                    i = j + 1;
                }
            }
            foreach (KeyValuePair<string, int[]> kv in open) {
                if (cur.ContainsKey(kv.Key)) continue;                                       // closed: emit
                int[] r = kv.Value;
                int top = r[3] * step, bottom = Math.Min(h, cy * step);
                outLines.Add((r[0] * step) + " " + (h - bottom) + " " + Math.Min(w, r[1] * step) + " " + (h - top) + " " + r[2]);
            }
            open = cur;
        }
        return outLines;
    }
}
'@

# ---- AutoLISP run inside accoreconsole -------------------------------------

$script:Lisp = @'
(defun dee-num (x) (rtos x 2 12))
(defun dee-pt (p) (strcat (dee-num (car p)) "," (dee-num (cadr p)) "," (dee-num (if (caddr p) (caddr p) 0.0))))
(defun dee-opt (d code) (if (assoc code d) (itoa (cdr (assoc code d))) "-"))
(defun dee-dump ( / ss i e d def f rec)
  (setq f (open dee-out "w"))
  (write-line (strcat "DWG|" (getvar "DWGNAME")) f)
  (setq rec (tblnext "BLOCK" T))
  (while rec
    (if (/= 0 (logand 4 (cdr (assoc 70 rec))))
      (write-line (strcat "XR|" (cdr (assoc 2 rec)) "|" (if (assoc 1 rec) (cdr (assoc 1 rec)) "")) f))
    (setq rec (tblnext "BLOCK")))
  (if (setq ss (ssget "_X" '((0 . "IMAGE"))))
    (progn
      (setq i 0)
      (repeat (sslength ss)
        (setq e (ssname ss i) d (entget e))
        (setq def (if (assoc 340 d) (entget (cdr (assoc 340 d))) nil))
        (write-line
          (strcat "IMG|" (cdr (assoc 5 d))
                  "|O=" (dee-pt (cdr (assoc 10 d)))
                  "|U=" (dee-pt (cdr (assoc 11 d)))
                  "|V=" (dee-pt (cdr (assoc 12 d)))
                  "|PX=" (dee-num (cadr (assoc 13 d))) "," (dee-num (caddr (assoc 13 d)))
                  "|CLIP=" (dee-opt d 280) "," (dee-opt d 71) "," (dee-opt d 91)
                  "|DEF=" (if def (cdr (assoc 5 def)) "?")
                  "|FILE=" (if def (cdr (assoc 1 def)) "?"))
          f)
        (setq i (1+ i)))))
  (close f)
  (princ))
(defun dee-at (o u v c r)
  (list (+ (car o) (* c (car u)) (* r (car v)))
        (+ (cadr o) (* c (cadr u)) (* r (cadr v)))
        (+ (if (caddr o) (caddr o) 0.0) (* c (if (caddr u) (caddr u) 0.0)) (* r (if (caddr v) (caddr v) 0.0)))))
(defun dee-del-def (hdef / dict nm)
  (if (setq dict (dictsearch (namedobjdict) "ACAD_IMAGE_DICT"))
    (progn
      (setq nm nil)
      (foreach it dict
        (cond ((= (car it) 3) (setq nm (cdr it)))
              ((and (= (car it) 350) nm (= (strcase (cdr (assoc 5 (entget (cdr it))))) (strcase hdef)))
               (dictremove (cdr (assoc -1 dict)) nm)))))))
(defun dee-defs-left ( / dict n)
  (setq n 0)
  (if (setq dict (dictsearch (namedobjdict) "ACAD_IMAGE_DICT"))
    (foreach it dict (if (= (car it) 350) (setq n (1+ n)))))
  n)
(defun dee-xrefs ( / rec out)
  (setq out '() rec (tblnext "BLOCK" T))
  (while rec
    (if (/= 0 (logand 4 (cdr (assoc 70 rec)))) (setq out (cons (cdr (assoc 2 rec)) out)))
    (setq rec (tblnext "BLOCK")))
  out)
(defun dee-repoint ( / xf xl xv)
  (if (and dee-xrf (findfile dee-xrf))
    (progn
      (setq xf (open dee-xrf "r"))
      (while (setq xl (read-line xf))
        (setq xv (read (strcat "(" xl ")")))
        (command "_.-XREF" "_P" (car xv) (cadr xv))
        (command "_.-XREF" "_R" (car xv)))
      (close xf))))
(defun dee-build ( / f r line v img d o u w layer rf rl q rgb nsol nimg defs xr nb)
  (setq r (open dee-res "w"))
  (if (/= (strcase (getvar "DWGNAME")) (strcase dee-expect))
    (write-line (strcat "ERR|wrong drawing open: " (getvar "DWGNAME")) r)
    (progn
      (setvar "FILLMODE" 1)
      (setvar "CMDECHO" 0)
      (setq nsol 0 nimg 0 defs '())
      (setq f (open dee-ctl "r"))
      (while (setq line (read-line f))
        (setq v (read (strcat "(" line ")")))
        (setq img (handent (car v)))
        (if (and img (setq d (entget img)))
          (progn
            (setq o (cdr (assoc 10 d)) u (cdr (assoc 11 d)) w (cdr (assoc 12 d)) layer (cdr (assoc 8 d)))
            (setq rf (open (cadr v) "r"))
            (while (setq rl (read-line rf))
              (setq q (read (strcat "(" rl ")")))
              (entmake (list '(0 . "SOLID") '(100 . "AcDbEntity") (cons 8 layer) (cons 420 (nth 4 q))
                             '(100 . "AcDbTrace")
                             (cons 10 (dee-at o u w (nth 0 q) (nth 1 q)))
                             (cons 11 (dee-at o u w (nth 2 q) (nth 1 q)))
                             (cons 12 (dee-at o u w (nth 0 q) (nth 3 q)))
                             (cons 13 (dee-at o u w (nth 2 q) (nth 3 q)))))
              (setq nsol (1+ nsol)))
            (close rf)
            (entdel img)
            (setq defs (cons (caddr v) defs) nimg (1+ nimg)))))
      (close f)
      (foreach h defs (dee-del-def h))
      (setvar "BINDTYPE" 1)
      (dee-repoint)
      (setq xr (dee-xrefs) nb 0)
      (foreach nm xr (command "_.-XREF" "_B" nm))
      (setq nb (- (length xr) (length (dee-xrefs))))
      (write-line (strcat "OK|images=" (itoa nimg) "|solids=" (itoa nsol) "|xrefs=" (itoa (length xr))
                          "|xrefs_bound=" (itoa nb) "|defs_left=" (itoa (dee-defs-left))) r)
      (if (= dee-year "")
        (command "_.SAVEAS" "" dee-outdwg)
        (command "_.SAVEAS" dee-year dee-outdwg))))
  (close r)
  (princ))
(princ)
'@

# ---- helpers ---------------------------------------------------------------

function ConvertTo-LispPath([string]$p) { return ($p -replace '\\', '/') }

function Get-CoreExes {
    if ($env:DEE_ACAD_CORE -and (Test-Path -LiteralPath $env:DEE_ACAD_CORE)) { return , @($env:DEE_ACAD_CORE) }
    $found = New-Object System.Collections.ArrayList
    foreach ($root in @($env:ProgramFiles, $env:ProgramW6432) | Where-Object { $_ } | Select-Object -Unique) {
        $base = Join-Path $root 'Autodesk'
        if (-not (Test-Path -LiteralPath $base)) { continue }
        foreach ($d in Get-ChildItem -LiteralPath $base -Directory -ErrorAction SilentlyContinue) {
            if ($d.Name -match '^AutoCAD (\d{4})$') {
                $exe = Join-Path $d.FullName 'accoreconsole.exe'
                if (Test-Path -LiteralPath $exe) { [void]$found.Add([pscustomobject]@{ Year = [int]$Matches[1]; Exe = $exe }) }
            }
        }
    }
    return , @($found | Sort-Object Year -Descending | ForEach-Object { $_.Exe })
}

# DWG version code at the start of the file -> the SAVEAS format name, so the
# result opens wherever the input did.
function Get-SaveYear([string]$path) {
    try {
        $fs = [IO.File]::OpenRead($path)
        try { $buf = New-Object byte[] 6; [void]$fs.Read($buf, 0, 6) } finally { $fs.Dispose() }
        switch ([Text.Encoding]::ASCII.GetString($buf)) {
            'AC1015' { return '2000' }
            'AC1018' { return '2004' }
            'AC1021' { return '2007' }
            'AC1024' { return '2010' }
            'AC1027' { return '2013' }
            'AC1032' { return '2018' }
        }
    } catch { }
    return ''
}

# Runs accoreconsole on a script; the first drawing is opened with /i. It is
# finished when the script has written its marker file - the console does not
# always exit by itself with several drawings open - or when time runs out.
#
# Returns @{ finished; crashed }. A console that dies on its own before the marker
# has crashed; it leaves an "AutoCAD Error Report" window open, which is closed here
# so a broken AutoCAD cannot pile dialogs onto the user's screen.
function Invoke-Core([string]$exe, [string]$firstDwg, [string]$scr, [string]$log, [string]$marker, [int]$seconds) {
    if (Test-Path -LiteralPath $marker) { Remove-Item -LiteralPath $marker -Force }
    $started = Get-Date
    $argList = '/i "{0}" /s "{1}" /l en-US' -f $firstDwg, $scr
    $p = Start-Process -FilePath $exe -ArgumentList $argList -PassThru -NoNewWindow -RedirectStandardOutput $log
    $deadline = $started.AddSeconds($seconds)
    $sawMarker = $null
    while (-not $p.HasExited) {
        if ((Get-Date) -gt $deadline) { break }
        if (Test-Path -LiteralPath $marker) {
            if (-not $sawMarker) { $sawMarker = Get-Date }
            elseif (((Get-Date) - $sawMarker).TotalSeconds -gt 8) { break }   # done; it is just slow to quit
        }
        Start-Sleep -Milliseconds 300
    }
    $finished = Test-Path -LiteralPath $marker
    $crashed = ($p.HasExited -and -not $finished)
    if (-not $p.HasExited) { try { $p.Kill() } catch { } }
    if ($crashed) {
        Start-Sleep -Milliseconds 1500
        Get-Process cer_dialog -ErrorAction SilentlyContinue | Where-Object { $_.StartTime -ge $started.AddSeconds(-2) } |
            ForEach-Object { try { $_.Kill() } catch { } }
    }
    return @{ finished = $finished; crashed = $crashed }
}

# Which AutoCAD to use: the one that worked last time first, newest otherwise, and
# one that crashed while another worked is skipped from then on (until that
# AutoCAD's exe changes, e.g. it is updated). Kept in ProgramData, per PC.
$script:CoreStateFile = Join-Path $env:ProgramData 'DeePack\superdwg_core.json'
function Get-CoreKey([string]$exe) { return ('{0}|{1}' -f $exe, (Get-Item -LiteralPath $exe).LastWriteTimeUtc.Ticks) }
function Get-CoreState {
    try { return (Get-Content -Raw -Encoding UTF8 -LiteralPath $script:CoreStateFile | ConvertFrom-Json) } catch { return $null }
}
function Select-Cores($all) {
    $prev = Get-CoreState
    $bad = if ($prev) { @($prev.bad) } else { @() }
    $ordered = @($all)
    if ($prev -and $prev.good -and ($ordered -contains $prev.good)) {
        $ordered = @($prev.good) + @($ordered | Where-Object { $_ -ne $prev.good })
    }
    $usable = @($ordered | Where-Object { $bad -notcontains (Get-CoreKey $_) })
    if ($usable.Count -eq 0) { $usable = $ordered }     # never leave nothing to try
    return , $usable
}
function Save-CoreState([string]$good, $newBad) {
    try {
        $prev = Get-CoreState
        $keys = @(@($prev.bad) + @($newBad | ForEach-Object { Get-CoreKey $_ }) | Where-Object { $_ } | Select-Object -Unique)
        $keys = @($keys | Where-Object { $_ -ne (Get-CoreKey $good) })
        $dir = Split-Path -Parent $script:CoreStateFile
        if (-not (Test-Path -LiteralPath $dir)) { [void](New-Item -ItemType Directory -Path $dir -Force) }
        $json = ConvertTo-Json -InputObject ([ordered]@{ good = $good; bad = $keys })
        [IO.File]::WriteAllText($script:CoreStateFile, $json, (New-Object System.Text.UTF8Encoding($false)))
    } catch { }
}

# A real DWG starts with its version code (AC1015 ... AC1032). Anything else would
# only stall the AutoCAD session it is put in, so it is refused up front.
function Test-DwgHeader([string]$path) {
    try {
        $fs = [IO.File]::OpenRead($path)
        try { $buf = New-Object byte[] 6; [void]$fs.Read($buf, 0, 6) } finally { $fs.Dispose() }
        return ([Text.Encoding]::ASCII.GetString($buf) -match '^AC\d{4}$')
    } catch { return $false }
}

# One script that runs $perDrawing for each listed drawing in turn. Drawings are
# opened one after another and left open (the console cannot close a drawing it
# is not showing), which is why callers keep each script to a handful of them.
function New-Script($work, [int[]]$indices, [scriptblock]$perDrawing, [string]$markerName) {
    $sb = New-Object System.Text.StringBuilder
    for ($k = 0; $k -lt $indices.Count; $k++) {
        $i = $indices[$k]
        if ($k -gt 0) { [void]$sb.AppendLine(('_.OPEN "{0}/in_{1}.dwg"' -f (ConvertTo-LispPath $work), $i)) }
        [void]$sb.AppendLine($script:Lisp)
        [void]$sb.AppendLine((& $perDrawing $i))
    }
    [void]$sb.AppendLine(('(close (open "{0}/{1}" "w"))' -f (ConvertTo-LispPath $work), $markerName))
    [void]$sb.AppendLine('_.QUIT _Y')
    return $sb.ToString()
}

function Read-Lines([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { return @() }
    return @(Get-Content -LiteralPath $path -Encoding Default)
}

# ---- main ------------------------------------------------------------------

$jobs = @(Get-Content -Raw -Encoding UTF8 -LiteralPath $JobsFile | ConvertFrom-Json)
if ($jobs.Count -eq 1 -and $jobs[0] -is [array]) { $jobs = @($jobs[0]) }
$results = New-Object System.Collections.ArrayList
function Write-Results {
    $json = ConvertTo-Json -InputObject @($results) -Depth 4
    [IO.File]::WriteAllText($ResultFile, $json, (New-Object System.Text.UTF8Encoding($false)))
}
function New-Result($job) { return [ordered]@{ out = $job.out; ok = $false; embedded = 0; xrefs_bound = 0; left_linked = 0; message = '' } }

# A plain-ASCII work folder: C:\ProgramData is ASCII on every PC, whereas the
# user's temp folder carries the Windows user name (which may not be).
$work = $null
foreach ($base in @((Join-Path $env:ProgramData 'DeePack\superdwg'), (Join-Path ([IO.Path]::GetTempPath()) 'DeePack_superdwg'))) {
    try {
        $candidate = Join-Path $base ([Guid]::NewGuid().ToString('N').Substring(0, 10))
        [void](New-Item -ItemType Directory -Path $candidate -Force)
        if ($candidate -match '^[\x20-\x7e]+$') { $work = $candidate; break }
        Remove-Item -LiteralPath $candidate -Recurse -Force -ErrorAction SilentlyContinue
    } catch { }
}
if (-not $work) { throw 'could not create an ASCII-named work folder' }
$w = ConvertTo-LispPath $work
$state = @{}      # per drawing: its result record
$cache = @{}      # image content hash -> its rectangle file (a logo shared by every sheet is built once)
$GroupSize = 8    # drawings per AutoCAD session: bounds its memory

# Runs both passes for the drawings in $idx. Leaves res_<i>.txt / out_<i>.dwg for
# each one AutoCAD got through; the caller sees the rest as failed.
function Convert-Group([int[]]$idx) {
    foreach ($i in $idx) {
        $state[$i] = @{ res = (New-Result $jobs[$i]) }
        foreach ($f in "dump_$i.txt", "ctl_$i.txt", "res_$i.txt", "out_$i.dwg") {
            if (Test-Path -LiteralPath (Join-Path $work $f)) { Remove-Item -LiteralPath (Join-Path $work $f) -Force }
        }
    }
    $first = $idx[0]
    foreach ($core in $cores) {
        if ($script:coreCrashed -contains $core) { continue }
        Log ('using {0} for {1} drawing(s)' -f $core, $idx.Count)
        # ---- pass 1: list the images -------------------------------------
        $scan = New-Script $work $idx { param($i) "(setq dee-out `"$w/dump_$i.txt`")`r`n(dee-dump)" } "p1done_$first.txt"
        [IO.File]::WriteAllText((Join-Path $work "pass1_$first.scr"), $scan, (New-Object System.Text.UTF8Encoding($false)))
        $run = Invoke-Core $core (Join-Path $work "in_$first.dwg") (Join-Path $work "pass1_$first.scr") `
            (Join-Path $work "pass1_$first.log") (Join-Path $work "p1done_$first.txt") (180 + 60 * $idx.Count)
        $dumped = @($idx | Where-Object { Test-Path -LiteralPath (Join-Path $work "dump_$_.txt") }).Count
        if (-not $run.finished -or $dumped -eq 0) {
            Log 'pass 1 produced nothing with this AutoCAD; trying the next one'
            if ($run.crashed) { $script:coreCrashed += $core }
            continue
        }

        # ---- rasterise the images ----------------------------------------
        foreach ($i in $idx) {
            $res = $state[$i].res
            $dwgFolder = Split-Path -Parent $jobs[$i].dwg
            $ctl = New-Object System.Collections.ArrayList
            $xrefLines = New-Object System.Collections.ArrayList
            $xk = 0
            foreach ($line in (Read-Lines (Join-Path $work "dump_$i.txt"))) {
                if ($line.StartsWith('XR|')) {
                    # The drawing is processed from a copy in the work folder, so an xref stored
                    # with a relative path would not be found: give AutoCAD a copy to point at.
                    $xp = $line.Split('|')
                    $stored = $xp[2]
                    $found = @($stored, (Join-Path $dwgFolder $stored), (Join-Path $dwgFolder ([IO.Path]::GetFileName($stored)))) |
                        Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } | Select-Object -First 1
                    if ($found) {
                        $copy = Join-Path $work ('xr_{0}_{1}.dwg' -f $i, $xk); $xk++
                        Copy-Item -LiteralPath $found -Destination $copy -Force
                        [void]$xrefLines.Add(('"{0}" "{1}"' -f $xp[1], (ConvertTo-LispPath $copy)))
                    }
                    continue
                }
                if (-not $line.StartsWith('IMG|')) { continue }
                $f = @{}
                foreach ($part in $line.Split('|')) { $eq = $part.IndexOf('='); if ($eq -gt 0) { $f[$part.Substring(0, $eq)] = $part.Substring($eq + 1) } }
                $handle = $line.Split('|')[1]
                try {
                    $clip = $f['CLIP'].Split(',')      # state, type, vertex count
                    # default boundary = the whole picture = 2 vertices; anything else is a real crop
                    if ($clip[0] -eq '1' -and $clip[2] -ne '2') { throw 'image is clipped (cropped to part of the picture)' }
                    $file = $f['FILE']
                    if (-not (Test-Path -LiteralPath $file)) {
                        $alt = Join-Path $dwgFolder ([IO.Path]::GetFileName($file))
                        if (Test-Path -LiteralPath $alt) { $file = $alt } else { throw "image file not found: $file" }
                    }
                    $hash = [DeeRaster]::Hash($file)
                    if (-not $cache.ContainsKey($hash)) {
                        $r = [DeeRaster]::Build($file, 60000)
                        $rf = Join-Path $work ("rects_$hash.txt")
                        [IO.File]::WriteAllLines($rf, [string[]]$r.Lines, (New-Object System.Text.UTF8Encoding($false)))
                        $cache[$hash] = @{ file = $rf; step = $r.Step; bits = $r.Bits; count = $r.Lines.Count; name = [IO.Path]::GetFileName($file) }
                    }
                    $c = $cache[$hash]
                    [void]$ctl.Add(('"{0}" "{1}" "{2}"' -f $handle, (ConvertTo-LispPath $c.file), $f['DEF']))
                    if ($c.step -gt 1 -or $c.bits -lt 6) {
                        $res.message += ("; image '{0}' was simplified to {1} fills to keep the file light" -f $c.name, $c.count)
                    }
                } catch {
                    $res.left_linked++
                    $res.message += ("; image {0} left linked: {1}" -f $handle, $_.Exception.Message)
                }
            }
            [IO.File]::WriteAllLines((Join-Path $work "ctl_$i.txt"), [string[]]$ctl, (New-Object System.Text.UTF8Encoding($false)))
            [IO.File]::WriteAllLines((Join-Path $work "xrefs_$i.txt"), [string[]]$xrefLines, (New-Object System.Text.UTF8Encoding($false)))
        }

        # ---- pass 2: draw them, bind xrefs, save -------------------------
        $build = New-Script $work $idx {
            param($i)
            $year = Get-SaveYear $jobs[$i].dwg
            "(setq dee-ctl `"$w/ctl_$i.txt`" dee-xrf `"$w/xrefs_$i.txt`" dee-res `"$w/res_$i.txt`" dee-expect `"in_$i.dwg`" dee-year `"$year`" dee-outdwg `"$w/out_$i.dwg`")`r`n(dee-build)"
        } "p2done_$first.txt"
        [IO.File]::WriteAllText((Join-Path $work "pass2_$first.scr"), $build, (New-Object System.Text.UTF8Encoding($false)))
        [void](Invoke-Core $core (Join-Path $work "in_$first.dwg") (Join-Path $work "pass2_$first.scr") `
            (Join-Path $work "pass2_$first.log") (Join-Path $work "p2done_$first.txt") (240 + 90 * $idx.Count))
        $script:coreWorked = $core
        return
    }
    Log 'no AutoCAD could process this group'
}

try {
    $cores = Select-Cores (Get-CoreExes)
    if ($cores.Count -eq 0) { throw 'no AutoCAD found (AutoCAD LT cannot be used for Super DWG)' }
    $script:coreCrashed = @()
    $script:coreWorked = $null

    # ASCII-named copies, so AutoCAD never sees a project's own file names.
    # Anything that is not a DWG is refused here and takes no part in the sessions.
    $usable = New-Object System.Collections.ArrayList
    for ($i = 0; $i -lt $jobs.Count; $i++) {
        $state[$i] = @{ res = (New-Result $jobs[$i]) }
        if ((Test-Path -LiteralPath $jobs[$i].dwg) -and (Test-DwgHeader $jobs[$i].dwg)) {
            Copy-Item -LiteralPath $jobs[$i].dwg -Destination (Join-Path $work ('in_{0}.dwg' -f $i)) -Force
            [void]$usable.Add($i)
        } else {
            $state[$i].res.message = 'not a valid DWG file'
            $state[$i].invalid = $true
        }
    }

    for ($s = 0; $s -lt $usable.Count; $s += $GroupSize) {
        $idx = @($usable[$s..([Math]::Min($s + $GroupSize, $usable.Count) - 1)])
        Convert-Group $idx
        if ($idx.Count -gt 1) {
            # One unreadable drawing can stall its whole session: give the ones that
            # got no result a session of their own.
            foreach ($i in $idx) {
                if (-not (Test-Path -LiteralPath (Join-Path $work "res_$i.txt"))) {
                    Log ('retrying on its own: ' + $jobs[$i].dwg)
                    Convert-Group @($i)
                }
            }
        }
    }

    # A console that crashed only counts against that AutoCAD if another one then
    # worked - when nothing works the cause is elsewhere (licence, a broken install).
    if ($script:coreWorked) { Save-CoreState $script:coreWorked $script:coreCrashed }

    # ---- collect ---------------------------------------------------------
    for ($i = 0; $i -lt $jobs.Count; $i++) {
        $res = $state[$i].res
        $outFile = Join-Path $work "out_$i.dwg"
        $lines = Read-Lines (Join-Path $work "res_$i.txt")
        $ok = $lines | Where-Object { $_.StartsWith('OK|') } | Select-Object -First 1
        $err = $lines | Where-Object { $_.StartsWith('ERR|') } | Select-Object -First 1
        if ($ok -and (Test-Path -LiteralPath $outFile)) {
            $kv = @{}
            foreach ($p in $ok.Split('|')) { $eq = $p.IndexOf('='); if ($eq -gt 0) { $kv[$p.Substring(0, $eq)] = [int]$p.Substring($eq + 1) } }
            $res.embedded = $kv['images']
            $res.xrefs_bound = $kv['xrefs_bound']
            if ($kv['xrefs'] -gt $kv['xrefs_bound']) {
                $res.left_linked += ($kv['xrefs'] - $kv['xrefs_bound'])
                $res.message += ("; {0} xref(s) could not be bound" -f ($kv['xrefs'] - $kv['xrefs_bound']))
            }
            if ($kv['defs_left'] -gt $res.left_linked) {
                $res.message += ("; {0} image link(s) remain (images inside blocks, or not embeddable)" -f $kv['defs_left'])
                $res.left_linked = $kv['defs_left']
            }
            if (Test-Path -LiteralPath $jobs[$i].out) { Remove-Item -LiteralPath $jobs[$i].out -Force }
            Copy-Item -LiteralPath $outFile -Destination $jobs[$i].out -Force
            $res.ok = $true
            $res.message = ('embedded {0} image(s) as {1} fills, bound {2} xref(s)' -f $res.embedded, $kv['solids'], $res.xrefs_bound) + $res.message
        } elseif ($state[$i].invalid) {
            $res.ok = $false       # message already says why
        } else {
            $res.ok = $false
            $res.message = if ($err) { $err } else { 'AutoCAD could not process this drawing' + $res.message }
        }
        [void]$results.Add($res)
    }
    Write-Results
} catch {
    $why = $_.Exception.Message
    Log ('FAILED: ' + $why)
    $results.Clear()
    foreach ($job in $jobs) { $r = New-Result $job; $r.message = $why; [void]$results.Add($r) }
    Write-Results
} finally {
    # Support knob: DEE_KEEP_WORK=1 keeps the work folder (scripts, AutoCAD's console logs).
    if ($env:DEE_KEEP_WORK) { Log ('work folder kept: ' + $work) }
    else { Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue }
}
