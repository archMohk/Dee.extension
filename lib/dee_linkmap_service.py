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
        for raw in node.raw_link_targets:
            stem = target_stem(raw)
            target_name = stem_to_name.get(stem)
            # Short display name shown directly on the node's own box,
            # whether or not it resolved to another file in this scan -
            # the whole point of the box is showing links at a glance,
            # not just the ones that happen to also be selected.
            link_names.append(target_name if target_name else
                              os.path.basename(raw.replace("\\", "/")))
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
      <label for="f_arrange">Arrangement</label>
      <select id="f_arrange">
        <option value="force">Force &mdash; clusters related files</option>
        <option value="hierarchy">Hierarchy &mdash; follows the link flow</option>
      </select>
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


  // ---- second arrangement: hierarchy by link flow ------------------
  // An edge here means "source links target", so target sits BELOW
  // source and every arrow reads downward. A model's row is the longest
  // link chain reaching it, which puts the shared reference models
  // everything pulls in along the bottom where they belong.
  var ARRANGE = "force";

  function visibleNodes() {
    return nodes.filter(function(n) { return !n.hidden; });
  }

  function rankNodes(list) {
    var rank = {};
    list.forEach(function(n) { rank[n.id] = 0; });
    var live = edges.filter(function(e) {
      return !e.source.hidden && !e.target.hidden &&
             rank.hasOwnProperty(e.source.id) && rank.hasOwnProperty(e.target.id);
    });
    // Relaxation rather than a topological sort: link graphs really do
    // contain cycles (two models linking each other is legal and
    // happens), and a topological sort has nothing to say about those.
    // Bounded by the node count, so a cycle costs a few extra passes
    // instead of spinning.
    var limit = Math.min(list.length, 60), changed = true, pass = 0;
    while (changed && pass < limit) {
      changed = false; pass++;
      live.forEach(function(e) {
        var want = rank[e.source.id] + 1;
        if (want > rank[e.target.id]) { rank[e.target.id] = want; changed = true; }
      });
    }
    return rank;
  }

  function layoutHierarchy() {
    var list = visibleNodes();
    if (!list.length) return;
    var rank = rankNodes(list);

    var rows = {};
    var maxRank = 0;
    list.forEach(function(n) {
      var r = rank[n.id] || 0;
      if (r > maxRank) maxRank = r;
      (rows[r] = rows[r] || []).push(n);
    });

    // Start each row in a stable order so the result does not jump
    // around between runs of the same scan.
    for (var r = 0; r <= maxRank; r++) {
      if (rows[r]) rows[r].sort(function(a, b) { return a.id < b.id ? -1 : 1; });
    }

    // Barycentre ordering: put each node near the average position of
    // the nodes it connects to on the row above, then sweep back up.
    // Four sweeps is where this stops paying for itself on maps this
    // size.
    var index = {};
    function reindex() {
      for (var rr = 0; rr <= maxRank; rr++) {
        (rows[rr] || []).forEach(function(n, i) { index[n.id] = i; });
      }
    }
    reindex();
    var neighboursUp = {}, neighboursDown = {};
    edges.forEach(function(e) {
      if (e.source.hidden || e.target.hidden) return;
      (neighboursUp[e.target.id] = neighboursUp[e.target.id] || []).push(e.source.id);
      (neighboursDown[e.source.id] = neighboursDown[e.source.id] || []).push(e.target.id);
    });
    function sweep(useUp) {
      var order = [];
      for (var rr = 0; rr <= maxRank; rr++) order.push(rr);
      if (!useUp) order.reverse();
      order.forEach(function(rr) {
        var row = rows[rr];
        if (!row || row.length < 2) return;
        var table = useUp ? neighboursUp : neighboursDown;
        row.forEach(function(n) {
          var near = (table[n.id] || []).filter(function(id) {
            return index.hasOwnProperty(id);
          });
          n._bary = near.length
            ? near.reduce(function(a, id) { return a + index[id]; }, 0) / near.length
            : index[n.id];
        });
        row.sort(function(a, b) { return a._bary - b._bary; });
        reindex();
      });
    }
    for (var s = 0; s < 4; s++) sweep(s % 2 === 0);

    // Place them. Rows are spaced by the tallest box in the row above,
    // columns by each box's own width, so nothing collides and the
    // gaps stay even whatever the names are.
    var ROW_GAP = 90, COL_GAP = 34, WRAP_GAP = 26;
    // A rank with 28 files in it would otherwise be one row about
    // 9,600px wide, which fits on screen only at a zoom where nothing is
    // readable. Wide ranks wrap into stacked sub-rows instead. Arrows
    // still all point downward: a rank's sub-rows are laid out before
    // the next rank starts, so nothing in rank r+1 ever sits above
    // anything in rank r.
    var MAX_ROW_W = 2400;
    var y = 0;
    for (var rr = 0; rr <= maxRank; rr++) {
      var row = rows[rr] || [];
      if (!row.length) continue;

      var chunks = [], current = [], currentW = 0;
      row.forEach(function(n) {
        var w = (n._w || 190) + COL_GAP;
        if (current.length && currentW + w > MAX_ROW_W) {
          chunks.push(current); current = []; currentW = 0;
        }
        current.push(n); currentW += w;
      });
      if (current.length) chunks.push(current);

      chunks.forEach(function(chunk, chunkIndex) {
        var tallest = 0, total = 0;
        chunk.forEach(function(n) {
          tallest = Math.max(tallest, n._h || 70);
          total += (n._w || 190) + COL_GAP;
        });
        total -= COL_GAP;
        var x = -total / 2;
        chunk.forEach(function(n) {
          var w = n._w || 190;
          n.x = x + w / 2;
          n.y = y + tallest / 2;
          n.vx = 0; n.vy = 0;
          x += w + COL_GAP;
        });
        y += tallest + (chunkIndex < chunks.length - 1 ? WRAP_GAP : ROW_GAP);
      });
    }
    // Centre the whole stack on the canvas the force layout uses.
    var cx = W / 2, cy = H / 2 - y / 2;
    list.forEach(function(n) { n.x += cx; n.y += cy; });
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

    var arrangeSel = document.getElementById("f_arrange");
    arrangeSel.addEventListener("change", function() {
      ARRANGE = arrangeSel.value;
      // Grouping and hierarchy both decide where a box goes, so they
      // cannot both be in charge. Hierarchy wins while it is selected,
      // and the group control is disabled rather than silently ignored.
      groupSel.disabled = (ARRANGE === "hierarchy");
      if (ARRANGE === "hierarchy") {
        GROUPS.active = false;
        drawGroupFrames();
      } else if (groupSel.value !== "") {
        GROUPS.active = true;
        GROUPS.key = (groupSel.value === "discipline")
                   ? "discipline" : parseInt(groupSel.value, 10);
        layoutGroups();
      }
      restartLayout();
    });
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
      groupSel.value = ""; groupSel.disabled = false;
      arrangeSel.value = "force"; ARRANGE = "force";
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
    // Sized from the boxes that actually have to fit, not a constant.
    // Boxes are as wide as their file names now (~310px here, and a
    // long name can reach 560), so a cell tuned for 190px-wide boxes
    // left groups too cramped to separate and they overlapped.
    var wSum = 0, wCount = 0;
    nodes.forEach(function(n) {
      if (n.hidden) return;
      wSum += (n._w || 190); wCount++;
    });
    var avgW = wCount ? wSum / wCount : 190;
    var cell = Math.max(700, 1.5 * avgW * Math.sqrt(busiest));
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
        // Scaled by how wide these two boxes actually are: a fixed
        // constant was tuned for one fixed width, and now that a box can
        // be three times wider than another, the wide ones would sit on
        // top of each other.
        var span = ((n1._w || 190) + (n2._w || 190)) / 2;
        force = Math.min(55000 * (span / 190) / (dist*dist), 16 * (span / 190));
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
        // Rest length follows the boxes too, so an edge between two
        // wide boxes does not pull them into each other.
        var rest = 200 + ((e.source._w || 190) + (e.target._w || 190)) / 2;
        var pull = (dist - rest) * 0.02;
        var ux = dx/dist, uy = dy/dist;
        if (!e.source.fixed) {{ e.source.vx += ux*pull; e.source.vy += uy*pull; }}
        if (!e.target.fixed) {{ e.target.vx -= ux*pull; e.target.vy -= uy*pull; }}
      }}
    }});
    nodes.forEach(function(n) {{
      if (n.fixed || n.hidden) return;
      n.x += n.vx; n.y += n.vy;
    }});

    // Hard separation. Repulsion alone is a suggestion, and grouping
    // pulls hard enough to overrule it - with boxes now sized to their
    // own text, that showed up as 12 overlapping pairs on a 43-file
    // map. This pass is not a force: any two boxes still overlapping
    // after the forces have run are simply moved apart, along whichever
    // axis needs the least movement, so text never lands on text.
    // Iterates to convergence rather than a fixed number of sweeps:
    // pushing one pair apart can push another pair together, so a fixed
    // count left overlaps behind (7 of them, grouped). Stops as soon as
    // a whole sweep moves nothing, which is the common case, and is
    // bounded so it can never spin.
    var moved = true, pass = 0;
    while (moved && pass < 24) {{
    moved = false; pass++;
    for (i = 0; i < nodes.length; i++) {{
      n1 = nodes[i];
      if (n1.hidden) continue;
      for (j = i + 1; j < nodes.length; j++) {{
        n2 = nodes[j];
        if (n2.hidden) continue;
        var needX = ((n1._w || 190) + (n2._w || 190)) / 2 + 14;
        var needY = ((n1._h || 70) + (n2._h || 70)) / 2 + 12;
        var sepX = n2.x - n1.x, sepY = n2.y - n1.y;
        var overX = needX - Math.abs(sepX), overY = needY - Math.abs(sepY);
        if (overX <= 0 || overY <= 0) continue;
        moved = true;
        if (overX < overY) {{
          var pushX = (sepX < 0 ? -1 : 1) * overX / 2;
          if (!n1.fixed) n1.x -= pushX;
          if (!n2.fixed) n2.x += pushX;
        }} else {{
          var pushY = (sepY < 0 ? -1 : 1) * overY / 2;
          if (!n1.fixed) n1.y -= pushY;
          if (!n2.fixed) n2.y += pushY;
        }}
      }}
    }}
    }}
  }}

  var ticks = 0, settling = false;
  function settle() {{
    // Switching to hierarchy while a force run is still in flight used
    // to leave the old loop running: it kept calling step() and undoing
    // the computed rows, so the "rows" came out as a scatter. The loop
    // checks on every frame whether it is still the arrangement in
    // charge, and stands down if not.
    if (typeof ARRANGE !== "undefined" && ARRANGE === "hierarchy") {{
      settling = false;
      return;
    }}
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
    if (typeof ARRANGE !== "undefined" && ARRANGE === "hierarchy") {{
      // Hierarchy is computed, not simulated - there is nothing for the
      // force loop to do, and letting it run would drag the rows apart.
      layoutHierarchy();
      render();
      fitView();
      return;
    }}
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

  // Boxes are sized to their own text now, between these bounds.
  var BOX_MIN_W = 190, BOX_MAX_W = 560;
  var HEADER_H = 22, LINE_H = 14, MAX_LINES = 5;

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
    // In the DOM before anything is measured - getComputedTextLength()
    // returns 0 for an element that has never been laid out.
    gNodes.appendChild(g);

    // FULL names. They are what the file is actually called, and the
    // box is sized to them below rather than the other way round.
    var names = n.link_names || [];
    var shown = names.slice(0, MAX_LINES);
    var extra = names.length - shown.length;
    var bodyLines = shown.length ? shown.length + (extra > 0 ? 1 : 0) : 1;
    var TAG_H = 15;
    var boxH = HEADER_H + TAG_H + bodyLines * LINE_H + 10;
    var boxColor = n.discipline_color || "#888888";

    var hover = document.createElementNS(NS, "title");
    hover.textContent = n.label;
    g.appendChild(hover);

    var texts = [];
    function addText(y, text, fill, size, weight) {{
      var el = svgText(0, y, text, fill, size, weight);
      g.appendChild(el);
      texts.push(el);
      return el;
    }}

    addText(-boxH / 2 + 15, n.label, "#eee", 12, "bold");
    addText(-boxH / 2 + HEADER_H + 10, n.discipline_label || "Unknown",
            boxColor, 10, "bold");

    var y = -boxH / 2 + HEADER_H + TAG_H + 10;
    if (!shown.length) {{
      addText(y, n.error ? "(scan issue)" : "(no links)",
              n.error ? "#e57373" : "#888", 10);
    }} else {{
      shown.forEach(function(name, i) {{
        addText(y + i * LINE_H, name, "#ccc", 10);
      }});
      if (extra > 0) {{
        addText(y + shown.length * LINE_H,
                "+" + extra + " more - click for full list", "#888", 9);
      }}
    }}

    // Measure what was actually rendered, then fit the box to it.
    var widest = 0;
    texts.forEach(function(el) {{
      var w = 0;
      try {{ w = el.getComputedTextLength(); }} catch (e) {{ w = 0; }}
      if (w > widest) widest = w;
    }});
    var boxW = Math.max(BOX_MIN_W, Math.min(BOX_MAX_W, widest + 24));

    // A name past the ceiling is the only case that still gets cut, and
    // it is cut to the box that exists rather than to a guessed count.
    if (widest + 24 > BOX_MAX_W) {{
      texts.forEach(function(el) {{
        var full = el.textContent;
        var guard = 0;
        while (guard < 200) {{
          var w = 0;
          try {{ w = el.getComputedTextLength(); }} catch (e) {{ break; }}
          if (w <= boxW - 20 || el.textContent.length < 5) break;
          el.textContent = el.textContent.slice(0, -2) + "\u2026";
          el.textContent = el.textContent.replace("\u2026\u2026", "\u2026");
          guard++;
        }}
        if (el.textContent !== full) {{
          var tip = document.createElementNS(NS, "title");
          tip.textContent = full;
          el.appendChild(tip);
        }}
      }});
    }}

    n._w = boxW; n._h = boxH;

    var rect = document.createElementNS(NS, "rect");
    rect.setAttribute("x", -boxW / 2); rect.setAttribute("y", -boxH / 2);
    rect.setAttribute("width", boxW); rect.setAttribute("height", boxH);
    rect.setAttribute("rx", 7);
    rect.setAttribute("fill", "#2b2b2b");
    rect.setAttribute("stroke", n.error ? "#e57373" : boxColor);
    rect.setAttribute("stroke-width", n.error ? "2.2" : "2");
    // Behind the text, but after the <title> so hovering still works.
    g.insertBefore(rect, hover.nextSibling);

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
