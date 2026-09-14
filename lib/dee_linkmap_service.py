# -*- coding: utf-8 -*-
"""
dee_linkmap_service
Pure-and-Revit-light logic for DeeLinkMAP: scans a batch of Revit
files (ACC project or local folder) and discovers each one's own
Revit-link references, producing a file-to-file relationship graph
exported as a single self-contained interactive HTML map (hand-rolled
SVG + vanilla JS force layout, no CDN - same "fully self-contained,
works offline" principle Dee3D's own viewer already uses, rather than
three.js/d3.js from a CDN).

--------------------------------------------------------------------
read_links_no_open() is NOT used by DeeLinkMAP anymore - confirmed unsafe
--------------------------------------------------------------------
Autodesk.Revit.DB.TransmissionData.ReadTransmissionData(ModelPath) was
meant to read a file's external references (including Revit links)
straight from disk, WITHOUT opening the document, per Autodesk's own
API docs and The Building Coder (jeremytammik.github.io/tbc/a/
0583_list_links.htm). Live testing found otherwise: it crashed Revit
immediately, before processing even the first file - a consistent,
reproducible failure, not an occasional bad file. Since that crash
happens at the native API level, no amount of Python try/except around
the call can catch or recover from it.

read_links_no_open() is kept here for reference/future investigation
only - DeePack.tab/Coordination.panel/DeeLinkMAP.pushbutton/script.py
no longer calls it. Every file is read via read_links_by_opening()
instead (an already-open document's FilteredElementCollector(doc)
.OfClass(RevitLinkType) - a standard, well-established pattern), with
the same dialog/failure handling and one-host-at-a-time discipline
DeeMAPLink/DeeSuperLINK already use, plus (added after a second live
crash, this time after several files processed successfully - pointing
at resource accumulation across many open/close cycles rather than a
single bad file) an explicit .NET GC pass after each close and a
time-budget safety net that stops the batch cleanly instead of pushing
through to another crash - both live in script.py's own run loop, not
here.
"""
import json
import os
import re


_THIS_DIR = os.path.dirname(__file__)
_DISCIPLINES_CONFIG_PATH = os.path.join(_THIS_DIR, ".dee_linkmap_disciplines.json")

# Starting set - user-editable/extensible via config.py (SHIFT+Click on
# DeeLinkMAP), which opens the JSON file directly rather than a custom
# editor UI, specifically so adding a brand new trade later is just
# adding one more "CODE": "Label" line, no code change needed.
DEFAULT_DISCIPLINES = {
    "AR": "Architecture",
    "ST": "Structure",
    "ME": "Mechanical",
    "PL": "Plumbing",
    "EL": "Electrical",
    "LS": "Landscape",
    "ID": "Interior Design",
}

UNKNOWN_LABEL = "Unknown"

# A generous, visually-distinct fixed palette - which color a given
# discipline CODE gets is computed (hash of the code string), not
# hand-assigned per discipline name, so a newly added code in the
# config file gets a stable, distinct color automatically with no
# extra configuration.
_PALETTE = [
    "#F2994D", "#4A90D9", "#66BB6A", "#26A69A", "#F0C419",
    "#AB47BC", "#EF5350", "#29B6F6", "#A1887F", "#EC407A",
    "#9CCC65", "#5C6BC0",
]
_UNKNOWN_COLOR = "#888888"


def discipline_config_path():
    """Public accessor for the discipline mapping file's path - used
    by config.py to open it directly for editing."""
    return _DISCIPLINES_CONFIG_PATH


def load_disciplines():
    """Returns the {code: label} mapping - defaults on first use,
    written to disk so the file exists and is directly editable
    afterward. Never raises."""
    try:
        if os.path.exists(_DISCIPLINES_CONFIG_PATH):
            with open(_DISCIPLINES_CONFIG_PATH, "r") as f:
                data = json.load(f)
            if isinstance(data, dict) and data:
                return dict((str(k).upper(), str(v)) for k, v in data.items())
    except Exception:
        pass
    save_disciplines(DEFAULT_DISCIPLINES)
    return dict(DEFAULT_DISCIPLINES)


def save_disciplines(mapping):
    try:
        with open(_DISCIPLINES_CONFIG_PATH, "w") as f:
            json.dump(mapping, f, indent=2, sort_keys=True)
    except Exception:
        pass


_SEGMENT_SPLIT = re.compile(r"[-_\s]+")


def detect_discipline(file_name, disciplines):
    """Matches a discipline code against a whole NAME SEGMENT (split on
    - / _ / whitespace), not a raw substring search - "AR" matches the
    "AR" segment in "KWG-NAG-Z1-C0A-01-MOD-AR-COR" but would not
    falsely match inside a longer segment like "ARCHIVE". Case-
    insensitive. Returns (code, label) - (None, UNKNOWN_LABEL) if
    nothing in the name matches any configured code."""
    segments = [s.upper() for s in _SEGMENT_SPLIT.split(file_name) if s]
    for code, label in disciplines.items():
        if code.upper() in segments:
            return code.upper(), label
    return None, UNKNOWN_LABEL


def discipline_color(code):
    if not code:
        return _UNKNOWN_COLOR
    idx = sum(ord(c) for c in code.upper()) % len(_PALETTE)
    return _PALETTE[idx]


class FileNode(object):
    """One scanned file: its display name, source-specific identity
    (local path, or (item_id, region, project_id) for ACC), and the
    RAW link target strings discovered for it. Edges are resolved
    later in build_graph(), once every selected file's own name is
    known - a link target string might point at a file style that was
    never included in this run's selection."""

    def __init__(self, name, source_key):
        self.name = name
        self.source_key = source_key
        self.raw_link_targets = []
        self.scan_method = None    # "no-open" | "opened" | None
        self.error = None


def scan_local_folder(folder, recursive, progress_cb=None):
    """Returns {display_name: file_path} for every .rvt file found -
    no worksharing filter, unlike DeeMAPLink's own local scan:
    DeeLinkMAP maps ANY Revit file's links, workshared or not."""
    import deew_model_scanner as scanner
    results = scanner.scan_folder(folder, recursive, progress_cb)
    out = {}
    for r in results:
        base = os.path.splitext(os.path.basename(r.file_path))[0]
        name = base
        i = 2
        while name in out:
            name = "{0} ({1})".format(base, i)
            i += 1
        out[name] = r.file_path
    return out


def matches_search(display_name, query):
    if not query:
        return True
    haystack = display_name.lower()
    return all(term in haystack for term in query.lower().split())


def read_links_no_open(model_path):
    """UNUSED by DeeLinkMAP - see this module's own docstring. Kept
    for reference/future investigation only; confirmed live to crash
    Revit immediately and consistently, a failure mode no Python-side
    error handling can catch or prevent.

    Fast, no-open Revit-link discovery via TransmissionData. Returns a
    LIST of raw path strings (possibly EMPTY - a file with genuinely
    zero Revit links is a normal, valid result), or None if the read
    itself failed, meaning the caller should fall back to opening the
    file. Never raises (for the Python-catchable failure modes only -
    see above)."""
    try:
        from Autodesk.Revit.DB import (
            TransmissionData, ExternalFileReferenceType, ModelPathUtils)
    except Exception:
        return None
    try:
        trans_data = TransmissionData.ReadTransmissionData(model_path)
    except Exception:
        return None
    if trans_data is None:
        return None
    try:
        ref_ids = list(trans_data.GetAllExternalFileReferenceIds())
    except Exception:
        return None
    targets = []
    for ref_id in ref_ids:
        try:
            ref = trans_data.GetLastSavedReferenceData(ref_id)
            if ref.ExternalFileReferenceType != ExternalFileReferenceType.RevitLink:
                continue
            path = ref.GetPath()
            try:
                display = ModelPathUtils.ConvertModelPathToUserVisiblePath(path)
            except Exception:
                display = str(path)
            targets.append(display)
        except Exception:
            continue
    return targets


def read_links_by_opening(doc):
    """Fallback used only when read_links_no_open() returns None.
    Queries an ALREADY-OPEN document - this function never opens or
    closes anything itself, the caller owns that explicitly (matching
    every other batch tool in this codebase). Never raises."""
    try:
        from Autodesk.Revit.DB import (
            FilteredElementCollector, RevitLinkType, ModelPathUtils)
    except Exception:
        return []
    targets = []
    try:
        collector = FilteredElementCollector(doc).OfClass(RevitLinkType)
    except Exception:
        return []
    for link_type in collector:
        try:
            ext_ref = link_type.GetExternalFileReference()
            path = ext_ref.GetPath()
            try:
                display = ModelPathUtils.ConvertModelPathToUserVisiblePath(path)
            except Exception:
                display = link_type.Name
            targets.append(display)
        except Exception:
            try:
                targets.append(link_type.Name)
            except Exception:
                continue
    return targets


def common_affix_segments(names):
    """How many leading and trailing name parts every name shares.

    Returns (head, tail). Both are 0 unless at least two names are given
    - a single file shares nothing with anything, and trimming it would
    just hide its name for no gain. At least one part is always left in
    the middle, so a set of names that differ only in their shared
    sections can never collapse to empty labels."""
    parts = [name_segments(n) for n in names if n]
    parts = [p for p in parts if p]
    if len(parts) < 2:
        return 0, 0
    shortest = min(len(p) for p in parts)

    head = 0
    while head < shortest - 1:
        value = parts[0][head]
        if any(p[head] != value for p in parts):
            break
        head += 1

    tail = 0
    while tail < shortest - head - 1:
        value = parts[0][-1 - tail]
        if any(p[-1 - tail] != value for p in parts):
            break
        tail += 1
    return head, tail


def shorten_name(display_name, head, tail):
    """The distinguishing middle of a name, given the shared head/tail
    part counts from common_affix_segments(). Falls back to the name
    itself whenever trimming would leave nothing useful."""
    if not display_name:
        return display_name
    parts = name_segments(display_name)
    if not parts or (head == 0 and tail == 0):
        return display_name
    end = len(parts) - tail
    if end <= head:
        return display_name
    middle = parts[head:end]
    return "-".join(middle) if middle else display_name


def closed_workset_note(doc):
    """"" if every user workset is open, otherwise a note naming how
    many were closed.

    Read AFTER a worksets-closed open, to qualify what the link list can
    be trusted to mean. The workset TABLE is readable either way - it is
    the elements on closed worksets that are not loaded - so this
    question can always be answered honestly.

    Returns "" for a non-workshared model too: nothing was closed, so
    there is nothing to qualify."""
    try:
        from Autodesk.Revit.DB import (
            FilteredWorksetCollector, WorksetKind)
    except Exception:
        return ""
    try:
        if not doc.IsWorkshared:
            return ""
        worksets = list(FilteredWorksetCollector(doc).OfKind(WorksetKind.UserWorkset))
    except Exception:
        return ""
    closed = 0
    for ws in worksets:
        try:
            if not ws.IsOpen:
                closed += 1
        except Exception:
            continue
    if not closed:
        return ""
    return ("read with {0} of {1} worksets closed - a link placed only on a "
            "closed workset may be missing from this list".format(
                closed, len(worksets)))


def name_segments(display_name):
    """The dash-separated parts of a file name, extension dropped.

    "KWG-NAG-Z1-C0A-01-MOD-AR-TY1-00000-00.rvt" ->
        ["KWG","NAG","Z1","C0A","01","MOD","AR","TY1","00000","00"]

    Underscores count as separators too, since some teams mix them in.
    Position is what carries the meaning in this convention, so the
    parts are returned in order and never sorted or de-duplicated."""
    if not display_name:
        return []
    base = display_name
    if "." in base:
        base = base.rsplit(".", 1)[0]
    parts = []
    for chunk in base.replace("_", "-").split("-"):
        chunk = chunk.strip()
        if chunk:
            parts.append(chunk)
    return parts


def target_stem(raw_target):
    """Reduces a raw link-target path/string to a bare filename stem,
    for matching against the set of selected files' own display
    names - link targets and the scanned file list come from
    different code paths, so matching by full path is too fragile;
    the bare stem is what both sides can agree on."""
    try:
        text = raw_target.replace("\\", "/")
        base = text.rsplit("/", 1)[-1]
    except Exception:
        base = str(raw_target)
    stem = base.rsplit(".", 1)[0] if "." in base else base
    return stem.strip().lower()


def build_graph(file_nodes, disciplines=None):
    """file_nodes: dict of {display_name: FileNode}. `disciplines`
    defaults to load_disciplines() if not given (tests pass an
    explicit dict for determinism). Returns (nodes, edges): nodes is a
    list of dicts (id, label, error, link_count, link_names,
    external_links, discipline_code, discipline_label,
    discipline_color); edges is a list of dicts (source, target). An
    edge is only drawn between two files BOTH present in this scanned
    set; a link to a file outside the selection is still counted
    (link_count) and listed (external_links) on that node, just not
    drawn as an edge (nothing in the graph to draw it to)."""
    if disciplines is None:
        disciplines = load_disciplines()

    stem_to_name = {}
    for name in file_nodes:
        stem_to_name[target_stem(name)] = name

    # Detected first, in one pass, so colors can be assigned collision-
    # free among only the disciplines actually PRESENT in this scan -
    # discipline_color()'s own per-code hash can and does collide for
    # some real code pairs (confirmed live: LS and AR hashed to the
    # same palette slot), which would defeat the entire point of
    # color-coding for exactly the files it matters most for. Assigning
    # palette slots in sorted-label order across only this map's own
    # disciplines guarantees no two DIFFERENT disciplines in the SAME
    # map ever share a color, at the (acceptable) cost of a given
    # discipline's color no longer being stable across different maps
    # with a different discipline mix.
    # What every file name in THIS map has in common. Shared parts carry
    # no information here, so they come off the labels drawn on the
    # boxes - the full name is still one click (or one hover) away.
    head_n, tail_n = common_affix_segments(list(file_nodes.keys()))

    detected = dict((name, detect_discipline(name, disciplines)) for name in file_nodes)
    unique_labels = sorted(set(label for _c, label in detected.values()))
    color_by_label = {}
    palette_i = 0
    for label in unique_labels:
        if label == UNKNOWN_LABEL:
            color_by_label[label] = _UNKNOWN_COLOR
        else:
            color_by_label[label] = _PALETTE[palette_i % len(_PALETTE)]
            palette_i += 1

    nodes = []
    edges = []
    seen_edges = set()
    for name, node in sorted(file_nodes.items()):
        external = []
        link_names = []
        short_link_names = []
        for raw in node.raw_link_targets:
            stem = target_stem(raw)
            target_name = stem_to_name.get(stem)
            # Short display name shown directly on the node's own box,
            # whether or not it resolved to another file in this scan -
            # the whole point of the box is showing links at a glance,
            # not just the ones that happen to also be selected.
            full_link = (target_name if target_name else
                         os.path.basename(raw.replace("\\", "/")))
            link_names.append(full_link)
            short_link_names.append(shorten_name(full_link, head_n, tail_n))
            if target_name is None or target_name == name:
                external.append(raw)
                continue
            edge_key = (name, target_name)
            if edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)
            edges.append({"source": name, "target": target_name})
        code, label = detected[name]
        nodes.append({
            "id": name,
            "label": name,
            "error": node.error,
            "scan_method": node.scan_method,
            "link_count": len(node.raw_link_targets),
            "link_names": link_names,
            # What the box draws: the same list with the shared head and
            # tail removed, so it fits instead of ending in an ellipsis.
            "short_link_names": short_link_names,
            "short_label": shorten_name(name, head_n, tail_n),
            "external_links": external,
            "discipline_code": code,
            "discipline_label": label,
            "discipline_color": color_by_label[label],
            # The name's own parts, in order - what the HTML map groups
            # and filters on. Computed here rather than in JavaScript so
            # the rule lives with the rest of the name handling.
            "segments": name_segments(name),
        })
    return nodes, edges


# ==========================================================================
# HTML export - hand-rolled SVG + vanilla JS force layout, no CDN
# ==========================================================================
_FILTER_CSS = u"""
  /* ---- filter + grouping panel ---- */
  #controls { position:fixed; left:14px; top:40px; width:250px;
    background:rgba(38,38,38,0.94); border-radius:6px; color:#ccc;
    font-size:11px; box-shadow:0 2px 10px rgba(0,0,0,0.45); }
  #controls_head { padding:8px 12px; font-size:10px; letter-spacing:.6px;
    text-transform:uppercase; color:#F2994D; cursor:pointer;
    display:flex; justify-content:space-between; user-select:none; }
  #controls_body { padding:0 12px 10px 12px; max-height:62vh; overflow-y:auto; }
  #controls.collapsed #controls_body { display:none; }
  #controls input[type=text], #controls select {
    width:100%; box-sizing:border-box; background:#1e1e1e; color:#ddd;
    border:1px solid #444; border-radius:4px; padding:4px 6px;
    font-size:11px; font-family:inherit; }
  #controls input[type=text]:focus, #controls select:focus {
    outline:none; border-color:#F2994D; }
  .f_row { margin:8px 0 0 0; }
  .f_row label { display:block; color:#888; font-size:10px;
    text-transform:uppercase; letter-spacing:.4px; margin-bottom:3px; }
  .f_foot { margin-top:10px; display:flex; align-items:center;
    justify-content:space-between; }
  #f_reset, #f_fit { background:#333; color:#ccc; border:1px solid #555;
    border-radius:4px; padding:3px 10px; font-size:11px; cursor:pointer;
    font-family:inherit; }
  #f_reset:hover, #f_fit:hover { background:#3d3d3d; color:#fff; }
  #f_fit { margin-left:6px; }
  #f_count { color:#888; }
  .group_frame { fill:rgba(255,255,255,0.035); stroke:#555;
    stroke-dasharray:6 5; stroke-width:1px; }
  .group_label { fill:#F2994D; font-size:13px; font-weight:600;
    font-family:Segoe UI, Arial, sans-serif; pointer-events:none; }
  /* the legend moves down so the filter panel can own the top-left */
  #legend { top:auto; bottom:34px; }
"""

_FILTER_UI = u"""<div id="controls">
  <div id="controls_head"><span>Filters &amp; grouping</span><span id="controls_caret">&#9662;</span></div>
  <div id="controls_body">
    <div class="f_row">
      <label for="f_search">Search</label>
      <input id="f_search" type="text" placeholder="part of a file name..."/>
    </div>
    <div class="f_row">
      <label for="f_group">Group by</label>
      <select id="f_group"></select>
    </div>
    <div id="f_segments"></div>
    <div class="f_foot">
      <button id="f_reset">Reset</button>
      <button id="f_fit">Fit</button>
      <span id="f_count"></span>
    </div>
  </div>
</div>
"""

_FILTER_JS = u"""
  // ---- filtering + grouping on the name's own parts ----------------
  // The file naming is positional, so part 3 is always the zone, part 7
  // always the discipline, and so on. Every part with more than one
  // distinct value across the scan becomes both a filter and a
  // group-by option; parts that are identical everywhere (the project
  // code, usually) are skipped because filtering on them does nothing.

  var GROUPS = { active: false, key: null, centres: {}, order: [] };
  var segSelects = [];
  var gGroups = document.createElementNS(NS, "g");
  gRoot.insertBefore(gGroups, gEdges);

  function segOf(n, i) {
    var parts = n.segments || [];
    return (i >= 0 && i < parts.length) ? parts[i] : "";
  }

  function distinctSeg(i) {
    var seen = {}, out = [];
    nodes.forEach(function(n) {
      var v = segOf(n, i);
      if (v && !seen[v]) { seen[v] = true; out.push(v); }
    });
    out.sort();
    return out;
  }

  function groupKeyOf(n) {
    if (!GROUPS.active) return null;
    if (GROUPS.key === "discipline") return n.discipline_label || "Unknown";
    var v = segOf(n, GROUPS.key);
    return v === "" ? "(blank)" : v;
  }

  function option(value, text) {
    var o = document.createElement("option");
    o.value = value; o.textContent = text;
    return o;
  }

  function maxSegments() {
    var m = 0;
    nodes.forEach(function(n) {
      var len = (n.segments || []).length;
      if (len > m) m = len;
    });
    return m;
  }

  function buildControls() {
    var total = maxSegments();
    var groupSel = document.getElementById("f_group");
    groupSel.appendChild(option("", "Nothing (free layout)"));
    groupSel.appendChild(option("discipline", "Discipline"));

    var segHost = document.getElementById("f_segments");
    for (var i = 0; i < total; i++) {
      var values = distinctSeg(i);
      if (values.length < 2) continue;   // same everywhere: useless as a filter
      var preview = values.slice(0, 3).join(", ");
      if (values.length > 3) preview += ", ...";
      var label = "Part " + (i + 1) + "  (" + preview + ")";
      groupSel.appendChild(option(String(i), label));

      var row = document.createElement("div");
      row.className = "f_row";
      var lab = document.createElement("label");
      lab.textContent = "Part " + (i + 1);
      row.appendChild(lab);
      var sel = document.createElement("select");
      sel.appendChild(option("", "All (" + values.length + ")"));
      values.forEach(function(v) { sel.appendChild(option(v, v)); });
      sel.setAttribute("data-seg", String(i));
      sel.addEventListener("change", applyFilters);
      row.appendChild(sel);
      segHost.appendChild(row);
      segSelects.push(sel);
    }

    groupSel.addEventListener("change", function() {
      var v = groupSel.value;
      GROUPS.active = (v !== "");
      GROUPS.key = (v === "" || v === "discipline") ? v : parseInt(v, 10);
      layoutGroups();
      restartLayout();
    });
    document.getElementById("f_search").addEventListener("input", applyFilters);
    document.getElementById("f_reset").addEventListener("click", function() {
      document.getElementById("f_search").value = "";
      segSelects.forEach(function(s) { s.value = ""; });
      groupSel.value = "";
      GROUPS.active = false; GROUPS.key = null;
      applyFilters();
    });
    document.getElementById("f_fit").addEventListener("click", fitView);
    var head = document.getElementById("controls_head");
    head.addEventListener("click", function() {
      var box = document.getElementById("controls");
      var collapsed = box.className === "collapsed";
      box.className = collapsed ? "" : "collapsed";
      document.getElementById("controls_caret").innerHTML =
        collapsed ? "&#9662;" : "&#9656;";
    });
  }

  function matchesFilters(n) {
    var q = (document.getElementById("f_search").value || "")
              .toLowerCase().split(" ");
    var name = (n.label || "").toLowerCase();
    for (var i = 0; i < q.length; i++) {
      if (q[i] && name.indexOf(q[i]) === -1) return false;
    }
    for (var s = 0; s < segSelects.length; s++) {
      var want = segSelects[s].value;
      if (!want) continue;
      if (segOf(n, parseInt(segSelects[s].getAttribute("data-seg"), 10)) !== want) {
        return false;
      }
    }
    return true;
  }

  function applyFilters() {
    var shown = 0;
    nodes.forEach(function(n) {
      n.hidden = !matchesFilters(n);
      if (!n.hidden) shown++;
    });
    document.getElementById("f_count").textContent =
      shown + " of " + nodes.length + " shown";
    layoutGroups();
    restartLayout();
  }

  function layoutGroups() {
    GROUPS.centres = {}; GROUPS.order = [];
    if (!GROUPS.active) return;
    var seen = {};
    nodes.forEach(function(n) {
      if (n.hidden) return;
      var k = groupKeyOf(n);
      if (!seen[k]) { seen[k] = true; GROUPS.order.push(k); }
    });
    GROUPS.order.sort();
    var count = GROUPS.order.length || 1;
    var cols = Math.ceil(Math.sqrt(count));
    var rows = Math.ceil(count / cols);
    // Cells scale with how many nodes land in the busiest group, so a
    // lopsided split (one huge zone, several small ones) still separates.
    var busiest = 1, tally = {};
    nodes.forEach(function(n) {
      if (n.hidden) return;
      var k = groupKeyOf(n);
      tally[k] = (tally[k] || 0) + 1;
      if (tally[k] > busiest) busiest = tally[k];
    });
    var cell = Math.max(620, 230 * Math.sqrt(busiest));
    GROUPS.order.forEach(function(k, i) {
      var c = i % cols, r = Math.floor(i / cols);
      GROUPS.centres[k] = {
        x: W / 2 + (c - (cols - 1) / 2) * cell,
        y: H / 2 + (r - (rows - 1) / 2) * cell * 0.8
      };
    });
  }

  function drawGroupFrames() {
    while (gGroups.firstChild) gGroups.removeChild(gGroups.firstChild);
    if (!GROUPS.active) return;
    var boxes = {};
    nodes.forEach(function(n) {
      if (n.hidden) return;
      var k = groupKeyOf(n);
      var b = boxes[k];
      if (!b) { boxes[k] = { x0: n.x, y0: n.y, x1: n.x, y1: n.y }; return; }
      if (n.x < b.x0) b.x0 = n.x;
      if (n.y < b.y0) b.y0 = n.y;
      if (n.x > b.x1) b.x1 = n.x;
      if (n.y > b.y1) b.y1 = n.y;
    });
    GROUPS.order.forEach(function(k) {
      var b = boxes[k];
      if (!b) return;
      var padX = 118, padY = 86;
      var rect = document.createElementNS(NS, "rect");
      rect.setAttribute("class", "group_frame");
      rect.setAttribute("x", b.x0 - padX);
      rect.setAttribute("y", b.y0 - padY);
      rect.setAttribute("width", (b.x1 - b.x0) + padX * 2);
      rect.setAttribute("height", (b.y1 - b.y0) + padY * 2);
      rect.setAttribute("rx", 14);
      gGroups.appendChild(rect);
      var t = document.createElementNS(NS, "text");
      t.setAttribute("class", "group_label");
      t.setAttribute("x", b.x0 - padX + 14);
      t.setAttribute("y", b.y0 - padY + 22);
      t.textContent = k;
      gGroups.appendChild(t);
    });
  }


  // Fit whatever is currently visible into the window. Called when a
  // layout finishes settling, so filtering or regrouping never leaves
  // the content parked off-screen.
  function fitView() {
    var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity, any = false;
    nodes.forEach(function(n) {
      if (n.hidden) return;
      any = true;
      if (n.x < minX) minX = n.x;
      if (n.y < minY) minY = n.y;
      if (n.x > maxX) maxX = n.x;
      if (n.y > maxY) maxY = n.y;
    });
    if (!any) return;
    // Node boxes are drawn centred on x,y, so pad by roughly half a box
    // plus a margin or the outer ones get clipped at the edges.
    var padX = 150, padY = 120;
    minX -= padX; maxX += padX; minY -= padY; maxY += padY;
    var w = Math.max(1, maxX - minX), h = Math.max(1, maxY - minY);
    var k = Math.min(W / w, H / h);
    k = Math.max(0.08, Math.min(k, 1.4));   // never zoom past readable
    view.k = k;
    view.x = (W - w * k) / 2 - minX * k;
    view.y = (H - h * k) / 2 - minY * k;
    applyView();
  }

  function afterSettle() {
    if (pendingFit) { pendingFit = false; fitView(); }
  }

  buildControls();
  applyFilters();
"""


_HTML_TEMPLATE = u"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8"/>
<title>{title}</title>
<style>
  html, body {{ margin:0; padding:0; height:100%; background:#1e1e1e;
    font-family: Segoe UI, Arial, sans-serif; overflow:hidden; }}
  #graph {{ width:100%; height:100%; display:block; cursor:grab; }}
  #graph:active {{ cursor:grabbing; }}
  .node rect {{ cursor:pointer; }}
  .node text {{ pointer-events:none; font-family:Segoe UI, Arial, sans-serif; }}
  .edge {{ stroke:#666; stroke-width:1.4px; }}
  #panel {{ position:fixed; top:0; right:0; width:300px; height:100%;
    background:#262626; color:#ddd; box-sizing:border-box; padding:16px;
    box-shadow:-2px 0 8px rgba(0,0,0,0.4); overflow-y:auto;
    transform:translateX(100%); transition:transform .15s ease; }}
  #panel.open {{ transform:translateX(0); }}
  #panel h2 {{ color:#F2994D; font-size:16px; margin:0 0 10px 0;
    word-break:break-all; }}
  #panel .row {{ margin:0 0 12px 0; font-size:12px; }}
  #panel .label {{ color:#999; text-transform:uppercase; font-size:10px;
    letter-spacing:.5px; margin-bottom:4px; }}
  #panel ul {{ margin:4px 0 0 0; padding-left:16px; }}
  #panel li {{ margin:2px 0; word-break:break-all; }}
  #panel .inscan {{ color:#8fbf7f; }}
  #panel .warn {{ color:#e57373; }}
  #close_panel {{ position:absolute; top:10px; right:12px; cursor:pointer;
    color:#999; font-size:16px; }}
  #hint {{ position:fixed; left:14px; bottom:12px; color:#888;
    font-size:11px; }}
  #title_bar {{ position:fixed; left:14px; top:12px; color:#F2994D;
    font-size:14px; font-weight:600; }}
  #legend {{ position:fixed; left:14px; top:40px; background:rgba(38,38,38,0.85);
    border-radius:6px; padding:8px 12px; font-size:11px; color:#ccc; }}
  #legend .item {{ display:flex; align-items:center; margin:3px 0; }}
  #legend .swatch {{ width:11px; height:11px; border-radius:3px; margin-right:7px;
    flex-shrink:0; }}
{filter_css}
</style>
</head>
<body>
<svg id="graph"></svg>
<div id="title_bar">{title}</div>
<div id="legend"></div>
{filter_ui}<div id="hint">Drag nodes &middot; scroll to zoom &middot; drag background to pan &middot; click a node for details &middot; filter and group from the panel on the left</div>
<div id="panel">
  <span id="close_panel">&#10005;</span>
  <h2 id="panel_title"></h2>
  <div class="row"><div class="label">Discipline</div>
    <div id="panel_discipline"></div></div>
  <div class="row"><div class="label">Revit links (in this file)</div>
    <div id="panel_count"></div>
    <ul id="panel_links"></ul></div>
  <div class="row"><div class="label">Linked into (by other files here)</div>
    <ul id="panel_in"></ul></div>
  <div class="row"><div class="label">Links not in this scan</div>
    <ul id="panel_ext"></ul></div>
  <div class="row" id="panel_error_row" style="display:none;">
    <div class="label warn">Scan issue</div>
    <div id="panel_error" class="warn"></div></div>
</div>
<script>
var DATA = {data_json};

(function() {{
  var svg = document.getElementById("graph");
  var W = window.innerWidth, H = window.innerHeight;
  var NS = "http://www.w3.org/2000/svg";

  var spreadR = 220 + DATA.nodes.length * 12;
  var nodes = DATA.nodes.map(function(n, i) {{
    var angle = (i / DATA.nodes.length) * Math.PI * 2;
    return {{
      id: n.id, label: n.label, error: n.error, link_count: n.link_count,
      link_names: n.link_names, external_links: n.external_links,
      discipline_code: n.discipline_code, discipline_label: n.discipline_label,
      discipline_color: n.discipline_color,
      // Carried through explicitly like every other field - this map
      // builds fresh objects rather than spreading, so a field left out
      // here simply vanishes from the simulation. That is how the
      // grouping controls first came up empty.
      segments: n.segments || [],
      short_label: n.short_label || n.label,
      short_link_names: n.short_link_names || n.link_names || [],
      x: W/2 + Math.cos(angle) * spreadR + (Math.random()-0.5)*40,
      y: H/2 + Math.sin(angle) * spreadR + (Math.random()-0.5)*40,
      vx: 0, vy: 0, fixed: false
    }};
  }});
  var byId = {{}};
  nodes.forEach(function(n) {{ byId[n.id] = n; }});
  var edges = DATA.edges.map(function(e) {{
    return {{ source: byId[e.source], target: byId[e.target] }};
  }}).filter(function(e) {{ return e.source && e.target; }});

  var incoming = {{}};
  edges.forEach(function(e) {{
    (incoming[e.target.id] = incoming[e.target.id] || []).push(e.source.id);
  }});

  // ---- legend: only disciplines actually present in this map ----
  (function buildLegend() {{
    var seen = {{}};
    var entries = [];
    nodes.forEach(function(n) {{
      var key = n.discipline_label || "Unknown";
      if (seen[key]) return;
      seen[key] = true;
      entries.push({{ label: key, color: n.discipline_color || "#888888" }});
    }});
    entries.sort(function(a, b) {{ return a.label.localeCompare(b.label); }});
    var el = document.getElementById("legend");
    entries.forEach(function(e) {{
      var row = document.createElement("div");
      row.className = "item";
      var sw = document.createElement("div");
      sw.className = "swatch";
      sw.style.background = e.color;
      row.appendChild(sw);
      var lbl = document.createElement("span");
      lbl.textContent = e.label;
      row.appendChild(lbl);
      el.appendChild(row);
    }});
  }})();

  // ---- simple force layout: pairwise repulsion + spring edges + centering ----
  function step() {{
    var i, j, n1, n2, dx, dy, dist, force;
    for (i = 0; i < nodes.length; i++) {{
      n1 = nodes[i];
      if (n1.fixed || n1.hidden) continue;
      var fx, fy;
      var centre = (typeof GROUPS !== "undefined" && GROUPS.active)
                 ? GROUPS.centres[groupKeyOf(n1)] : null;
      if (centre) {{
        // Grouped: pull toward the group's own centre instead of the
        // middle of the canvas, firmly enough to beat the repulsion
        // between boxes and keep each group visibly separate.
        fx = (centre.x - n1.x) * 0.020;
        fy = (centre.y - n1.y) * 0.020;
      }} else {{
        fx = (W/2 - n1.x) * 0.002;
        fy = (H/2 - n1.y) * 0.002;
      }}
      for (j = 0; j < nodes.length; j++) {{
        if (i === j) continue;
        n2 = nodes[j];
        if (n2.hidden) continue;
        dx = n1.x - n2.x; dy = n1.y - n2.y;
        dist = Math.sqrt(dx*dx + dy*dy) || 1;
        // Boxes (~190px wide, variable height) need much more separation
        // than the small circles this replaced - tuned so a handful of
        // 5-link boxes don't overlap once settled.
        force = Math.min(55000 / (dist*dist), 16);
        fx += (dx/dist) * force; fy += (dy/dist) * force;
      }}
      n1.vx = (n1.vx + fx) * 0.75;
      n1.vy = (n1.vy + fy) * 0.75;
    }}
    edges.forEach(function(e) {{
      if (e.source.hidden || e.target.hidden) return;
      if (!e.source.fixed || !e.target.fixed) {{
        dx = e.target.x - e.source.x; dy = e.target.y - e.source.y;
        dist = Math.sqrt(dx*dx + dy*dy) || 1;
        var pull = (dist - 260) * 0.02;
        var ux = dx/dist, uy = dy/dist;
        if (!e.source.fixed) {{ e.source.vx += ux*pull; e.source.vy += uy*pull; }}
        if (!e.target.fixed) {{ e.target.vx -= ux*pull; e.target.vy -= uy*pull; }}
      }}
    }});
    nodes.forEach(function(n) {{
      if (n.fixed || n.hidden) return;
      n.x += n.vx; n.y += n.vy;
    }});
  }}

  var ticks = 0, settling = false;
  function settle() {{
    settling = true;
    step();
    ticks++;
    render();
    if (ticks < 220) requestAnimationFrame(settle);
    else {{
      settling = false;
      afterSettle();
    }}
  }}
  // Filtering and grouping both change where nodes belong, so the
  // simulation has to run again. Restarting a finished loop needs the
  // call; restarting a running one only needs the tick count reset, or
  // two loops would run at once and the layout would jitter.
  var pendingFit = true;
  function restartLayout() {{
    ticks = 0;
    pendingFit = true;
    if (!settling) settle();
  }}

  // ---- SVG build ----
  var gRoot = document.createElementNS(NS, "g");
  svg.appendChild(gRoot);
  var gEdges = document.createElementNS(NS, "g");
  var gNodes = document.createElementNS(NS, "g");
  gRoot.appendChild(gEdges);
  gRoot.appendChild(gNodes);

  var edgeEls = edges.map(function() {{
    var l = document.createElementNS(NS, "line");
    l.setAttribute("class", "edge");
    gEdges.appendChild(l);
    return l;
  }});

  var BOX_W = 230, HEADER_H = 22, LINE_H = 14, MAX_LINES = 5;

  function truncate(text, max) {{
    return text.length > max ? text.slice(0, max - 3) + "..." : text;
  }}

  function svgText(x, y, text, fill, size, weight) {{
    var t = document.createElementNS(NS, "text");
    t.setAttribute("x", x); t.setAttribute("y", y);
    t.setAttribute("text-anchor", "middle");
    t.setAttribute("fill", fill);
    t.setAttribute("font-size", size || 10);
    if (weight) t.setAttribute("font-weight", weight);
    t.textContent = text;
    return t;
  }}

  var nodeEls = nodes.map(function(n) {{
    var g = document.createElementNS(NS, "g");
    g.setAttribute("class", "node");

    // Shortened for drawing; the full names live in the panel and
    // in the hover tooltip added below.
    var names = n.short_link_names || n.link_names || [];
    var shown = names.slice(0, MAX_LINES);
    var extra = names.length - shown.length;
    var bodyLines = shown.length ? shown.length + (extra > 0 ? 1 : 0) : 1;
    var TAG_H = 15;
    var boxH = HEADER_H + TAG_H + bodyLines * LINE_H + 10;
    n._w = BOX_W; n._h = boxH;
    var boxColor = n.discipline_color || "#888888";

    var rect = document.createElementNS(NS, "rect");
    rect.setAttribute("x", -BOX_W / 2); rect.setAttribute("y", -boxH / 2);
    rect.setAttribute("width", BOX_W); rect.setAttribute("height", boxH);
    rect.setAttribute("rx", 7);
    rect.setAttribute("fill", "#2b2b2b");
    rect.setAttribute("stroke", n.error ? "#e57373" : boxColor);
    rect.setAttribute("stroke-width", n.error ? "2.2" : "2");
    g.appendChild(rect);

    var hover = document.createElementNS(NS, "title");
    hover.textContent = n.label;
    g.appendChild(hover);
    g.appendChild(svgText(0, -boxH / 2 + 15, truncate(n.short_label, 30),
      "#eee", 12, "bold"));
    g.appendChild(svgText(0, -boxH / 2 + HEADER_H + 10,
      n.discipline_label || "Unknown", boxColor, 10, "bold"));

    var y = -boxH / 2 + HEADER_H + TAG_H + 10;
    if (!shown.length) {{
      g.appendChild(svgText(0, y, n.error ? "(scan issue)" : "(no links)",
        n.error ? "#e57373" : "#888", 10));
    }} else {{
      shown.forEach(function(name, i) {{
        g.appendChild(svgText(0, y + i * LINE_H, truncate(name, 34), "#ccc", 10));
      }});
      if (extra > 0) {{
        g.appendChild(svgText(0, y + shown.length * LINE_H,
          "+" + extra + " more - click for full list", "#888", 9));
      }}
    }}

    gNodes.appendChild(g);
    g.addEventListener("click", function(ev) {{ ev.stopPropagation(); openPanel(n); }});
    var dragging = false, dx0 = 0, dy0 = 0;
    g.addEventListener("mousedown", function(ev) {{
      ev.stopPropagation();
      dragging = true; n.fixed = true;
      var p = toWorld(ev.clientX, ev.clientY);
      dx0 = p.x - n.x; dy0 = p.y - n.y;
    }});
    window.addEventListener("mousemove", function(ev) {{
      if (!dragging) return;
      var p = toWorld(ev.clientX, ev.clientY);
      n.x = p.x - dx0; n.y = p.y - dy0;
      render();
    }});
    window.addEventListener("mouseup", function() {{ dragging = false; }});
    return g;
  }});

  function render() {{
    edgeEls.forEach(function(l, i) {{
      var e = edges[i];
      // An edge to a filtered-out file would otherwise dangle, pointing
      // at nothing - hide it with either end.
      if (e.source.hidden || e.target.hidden) {{ l.style.display = "none"; return; }}
      l.style.display = "";
      l.setAttribute("x1", e.source.x); l.setAttribute("y1", e.source.y);
      l.setAttribute("x2", e.target.x); l.setAttribute("y2", e.target.y);
    }});
    nodeEls.forEach(function(g, i) {{
      if (nodes[i].hidden) {{ g.style.display = "none"; return; }}
      g.style.display = "";
      g.setAttribute("transform", "translate(" + nodes[i].x + "," + nodes[i].y + ")");
    }});
    drawGroupFrames();
  }}

  // ---- pan / zoom ----
  var view = {{ x:0, y:0, k:1 }};
  function applyView() {{
    gRoot.setAttribute("transform",
      "translate(" + view.x + "," + view.y + ") scale(" + view.k + ")");
  }}
  function toWorld(clientX, clientY) {{
    return {{ x: (clientX - view.x) / view.k, y: (clientY - view.y) / view.k }};
  }}
  var panning = false, panStart = null;
  svg.addEventListener("mousedown", function(ev) {{
    panning = true; panStart = {{ x: ev.clientX - view.x, y: ev.clientY - view.y }};
  }});
  window.addEventListener("mousemove", function(ev) {{
    if (!panning) return;
    view.x = ev.clientX - panStart.x; view.y = ev.clientY - panStart.y;
    applyView();
  }});
  window.addEventListener("mouseup", function() {{ panning = false; }});
  svg.addEventListener("wheel", function(ev) {{
    ev.preventDefault();
    var factor = ev.deltaY < 0 ? 1.1 : 0.9;
    view.k = Math.max(0.15, Math.min(4, view.k * factor));
    applyView();
  }}, {{ passive: false }});

  // ---- details panel ----
  var panel = document.getElementById("panel");
  document.getElementById("close_panel").addEventListener("click", function() {{
    panel.classList.remove("open");
  }});
  svg.addEventListener("click", function() {{ panel.classList.remove("open"); }});

  function openPanel(n) {{
    document.getElementById("panel_title").textContent = n.label;
    var discEl = document.getElementById("panel_discipline");
    discEl.textContent = n.discipline_label || "Unknown";
    discEl.style.color = n.discipline_color || "#888888";
    document.getElementById("panel_count").textContent = n.link_count + " link(s)";
    // The full names, untruncated. The box can only show five short
    // ones; this is where you actually read them.
    var linkUl = document.getElementById("panel_links");
    linkUl.innerHTML = "";
    var external = {{}};
    (n.external_links || []).forEach(function(x) {{ external[x] = true; }});
    (n.link_names || []).forEach(function(name) {{
      var li = document.createElement("li");
      li.textContent = name;
      // Green marks a link that resolved to another file in this scan,
      // so it can be told from one pointing outside it.
      if (!external[name] && byId[name]) li.className = "inscan";
      linkUl.appendChild(li);
    }});
    if (!(n.link_names || []).length) {{
      var liNone = document.createElement("li");
      liNone.textContent = n.error ? "(not read)" : "(none)";
      linkUl.appendChild(liNone);
    }}
    var inUl = document.getElementById("panel_in");
    inUl.innerHTML = "";
    (incoming[n.id] || []).forEach(function(src) {{
      var li = document.createElement("li"); li.textContent = src; inUl.appendChild(li);
    }});
    if (!(incoming[n.id] || []).length) {{
      var li0 = document.createElement("li"); li0.textContent = "(none)"; inUl.appendChild(li0);
    }}
    var extUl = document.getElementById("panel_ext");
    extUl.innerHTML = "";
    (n.external_links || []).forEach(function(x) {{
      var li = document.createElement("li"); li.textContent = x; extUl.appendChild(li);
    }});
    if (!(n.external_links || []).length) {{
      var li0 = document.createElement("li"); li0.textContent = "(none)"; extUl.appendChild(li0);
    }}
    var errRow = document.getElementById("panel_error_row");
    if (n.error) {{
      errRow.style.display = "";
      document.getElementById("panel_error").textContent = n.error;
    }} else {{
      errRow.style.display = "none";
    }}
    panel.classList.add("open");
  }}

  window.addEventListener("resize", function() {{
    W = window.innerWidth; H = window.innerHeight;
  }});

{filter_js}
}})();
</script>
</body>
</html>
"""


def export_html_map(nodes, edges, output_path, title="DeeLinkMAP"):
    """Writes the self-contained interactive HTML map. Never raises -
    returns (ok, detail)."""
    try:
        payload = json.dumps({"nodes": nodes, "edges": edges})
        html = _HTML_TEMPLATE.format(title=title, data_json=payload,
                                     filter_css=_FILTER_CSS,
                                     filter_ui=_FILTER_UI,
                                     filter_js=_FILTER_JS)
        folder = os.path.dirname(output_path)
        if folder and not os.path.isdir(folder):
            os.makedirs(folder)
        with open(output_path, "wb") as f:
            f.write(html.encode("utf-8"))
        return True, output_path
    except Exception as e:
        return False, str(e)
