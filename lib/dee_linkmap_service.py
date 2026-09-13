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
        })
    return nodes, edges


# ==========================================================================
# HTML export - hand-rolled SVG + vanilla JS force layout, no CDN
# ==========================================================================
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
  #panel li {{ margin:2px 0; }}
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
</style>
</head>
<body>
<svg id="graph"></svg>
<div id="title_bar">{title}</div>
<div id="legend"></div>
<div id="hint">Drag nodes &middot; scroll to zoom &middot; drag background to pan &middot; click a node for details</div>
<div id="panel">
  <span id="close_panel">&#10005;</span>
  <h2 id="panel_title"></h2>
  <div class="row"><div class="label">Discipline</div>
    <div id="panel_discipline"></div></div>
  <div class="row"><div class="label">Revit links (in this file)</div>
    <div id="panel_count"></div></div>
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
      if (n1.fixed) continue;
      var fx = (W/2 - n1.x) * 0.002, fy = (H/2 - n1.y) * 0.002;
      for (j = 0; j < nodes.length; j++) {{
        if (i === j) continue;
        n2 = nodes[j];
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
      if (n.fixed) return;
      n.x += n.vx; n.y += n.vy;
    }});
  }}

  var ticks = 0;
  function settle() {{
    step();
    ticks++;
    render();
    if (ticks < 220) requestAnimationFrame(settle);
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

  var BOX_W = 190, HEADER_H = 22, LINE_H = 14, MAX_LINES = 5;

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

    var names = n.link_names || [];
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

    g.appendChild(svgText(0, -boxH / 2 + 15, truncate(n.label, 26),
      "#eee", 12, "bold"));
    g.appendChild(svgText(0, -boxH / 2 + HEADER_H + 10,
      n.discipline_label || "Unknown", boxColor, 10, "bold"));

    var y = -boxH / 2 + HEADER_H + TAG_H + 10;
    if (!shown.length) {{
      g.appendChild(svgText(0, y, n.error ? "(scan issue)" : "(no links)",
        n.error ? "#e57373" : "#888", 10));
    }} else {{
      shown.forEach(function(name, i) {{
        g.appendChild(svgText(0, y + i * LINE_H, truncate(name, 30), "#ccc", 10));
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
      l.setAttribute("x1", e.source.x); l.setAttribute("y1", e.source.y);
      l.setAttribute("x2", e.target.x); l.setAttribute("y2", e.target.y);
    }});
    nodeEls.forEach(function(g, i) {{
      g.setAttribute("transform", "translate(" + nodes[i].x + "," + nodes[i].y + ")");
    }});
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

  settle();
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
        html = _HTML_TEMPLATE.format(title=title, data_json=payload)
        folder = os.path.dirname(output_path)
        if folder and not os.path.isdir(folder):
            os.makedirs(folder)
        with open(output_path, "wb") as f:
            f.write(html.encode("utf-8"))
        return True, output_path
    except Exception as e:
        return False, str(e)
