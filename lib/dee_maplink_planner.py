# -*- coding: utf-8 -*-
"""A visual planner for DeeMAPLink: draw the links, then run them.

DeeMAPLink's two-list "tick sources, tick targets, Add Match" flow works,
but it makes you hold the whole picture in your head - which file goes
into which - while reading two identical-looking lists of forty-character
names. DeeLinkMAP already draws exactly that picture, just read-only.
This is the same picture, made editable: drag from one box to another to
say "this model gets linked into that one", and the arrow you drew IS the
match.

HOW THE PLAN GETS BACK INTO REVIT
---------------------------------
The page is a local HTML file opened in the default browser, so it cannot
call back into Revit. It does not need to: the plan is a short list of
name pairs, so the page puts it on the clipboard as JSON and the tool
reads the clipboard back. That is a deliberate choice over the
alternatives:

  - a local web server just to receive a few dozen pairs is a lot of
    moving parts, and a listening socket, for a text transfer;
  - WPF's WebBrowser control is IE-based, and betting this on IE's SVG
    behaviour is not a bet worth making;
  - a file the page downloads means picking a folder and finding it
    again.

The JSON is also shown in a text box on the page, so if the browser
blocks clipboard access (file:// pages sometimes do) it can still be
selected and copied by hand. Nothing is lost either way.

The round trip is lossless: matches already in the tool are drawn as
arrows when the page opens, so planning can be picked up where it was
left rather than started again.

Templating here uses plain marker replacement, NOT str.format(). The
DeeLinkMAP template uses format() and therefore has to double every
literal brace in its CSS and JavaScript, which is a standing trap in a
file that is mostly CSS and JavaScript.
"""
import json
import os

import dee_linkmap_service as lms


def plan_payload(file_names, matches, disciplines=None):
    """The data the page needs: every file with its discipline colour,
    and the matches to draw as arrows on open."""
    if disciplines is None:
        disciplines = lms.load_disciplines()

    detected = {}
    for name in file_names:
        detected[name] = lms.detect_discipline(name, disciplines)

    labels = sorted(set(label for _code, label in detected.values()))
    colour_by_label = {}
    index = 0
    for label in labels:
        if label == lms.UNKNOWN_LABEL:
            colour_by_label[label] = lms._UNKNOWN_COLOR
        else:
            colour_by_label[label] = lms._PALETTE[index % len(lms._PALETTE)]
            index += 1

    nodes = []
    for name in sorted(file_names):
        code, label = detected[name]
        nodes.append({
            "id": name,
            "discipline_code": code,
            "discipline_label": label,
            "discipline_color": colour_by_label[label],
            "segments": lms.name_segments(name),
        })
    return {
        "nodes": nodes,
        "matches": [{"source": s, "target": t} for s, t in matches],
    }


def parse_plan(text):
    """Reads a plan back off the clipboard.

    Returns (matches, detail). matches is [(source, target), ...];
    detail explains a refusal. Accepts the page's own JSON object and a
    bare list of pairs, and ignores anything malformed inside an
    otherwise valid plan rather than throwing the whole thing away -
    a half-pasted clipboard should cost you the bad rows, not the plan.
    """
    if not text or not text.strip():
        return [], "clipboard is empty"
    try:
        data = json.loads(text)
    except Exception:
        return [], ("clipboard does not contain a DeeMAPLink plan "
                    "(it is not valid JSON)")

    raw = None
    if isinstance(data, dict):
        raw = data.get("matches")
        if raw is None and data.get("tool") and "plan" in data:
            raw = data.get("plan")
    elif isinstance(data, list):
        raw = data
    if raw is None:
        return [], "clipboard JSON has no 'matches' list"

    matches = []
    seen = set()
    dropped = 0
    for entry in raw:
        source = target = None
        if isinstance(entry, dict):
            source = entry.get("source")
            target = entry.get("target")
        elif isinstance(entry, (list, tuple)) and len(entry) == 2:
            source, target = entry[0], entry[1]
        if not source or not target or source == target:
            dropped += 1
            continue
        pair = (source, target)
        if pair in seen:
            continue
        seen.add(pair)
        matches.append(pair)

    if not matches:
        return [], "no usable matches in the pasted plan"
    detail = "{0} match(es)".format(len(matches))
    if dropped:
        detail += ", {0} unusable entr(ies) ignored".format(dropped)
    return matches, detail


def export_plan_html(file_names, matches, output_path, title="DeeMAPLink",
                     disciplines=None):
    """Writes the planner page. Never raises - returns (ok, detail)."""
    try:
        payload = plan_payload(file_names, matches, disciplines)
        html = (_TEMPLATE
                .replace("__TITLE__", _escape(title))
                .replace("__DATA__", json.dumps(payload)))
        folder = os.path.dirname(output_path)
        if folder and not os.path.isdir(folder):
            os.makedirs(folder)
        with open(output_path, "wb") as fh:
            fh.write(html.encode("utf-8"))
        return True, output_path
    except Exception as e:
        return False, str(e)


def _escape(text):
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;"))


_TEMPLATE = u"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8"/>
<title>__TITLE__</title>
<style>
  html, body { margin:0; padding:0; height:100%; background:#1e1e1e;
    font-family: Segoe UI, Arial, sans-serif; overflow:hidden; color:#ddd; }
  #graph { width:100%; height:100%; display:block; cursor:grab; }
  #graph.linking { cursor:crosshair; }
  .node rect { cursor:crosshair; }
  .node text { pointer-events:none; }
  .node.source rect { stroke-width:3.5px; }
  .arrow { stroke:#F2994D; stroke-width:2.2px; cursor:pointer; }
  .arrow:hover { stroke:#ffc38a; stroke-width:3.4px; }
  .arrow_hit { stroke:transparent; stroke-width:14px; cursor:pointer; }
  .rubber { stroke:#F2994D; stroke-width:2px; stroke-dasharray:5 4;
    pointer-events:none; }
  #title_bar { position:fixed; left:14px; top:12px; color:#F2994D;
    font-size:14px; font-weight:600; }
  #hint { position:fixed; left:14px; bottom:12px; color:#888; font-size:11px; }
  #panel { position:fixed; top:0; right:0; width:330px; height:100%;
    background:#262626; box-sizing:border-box; padding:14px;
    box-shadow:-2px 0 8px rgba(0,0,0,0.4); display:flex;
    flex-direction:column; }
  #panel h2 { color:#F2994D; font-size:14px; margin:0 0 8px 0;
    text-transform:uppercase; letter-spacing:.6px; }
  #plan_list { flex:1; overflow-y:auto; font-size:11px; margin:0;
    padding:0; list-style:none; }
  #plan_list li { background:#1e1e1e; border-radius:4px; padding:6px 8px;
    margin-bottom:5px; word-break:break-all; position:relative;
    padding-right:24px; }
  #plan_list .into { color:#888; }
  #plan_list .kill { position:absolute; right:6px; top:5px; color:#777;
    cursor:pointer; font-size:13px; }
  #plan_list .kill:hover { color:#e57373; }
  #plan_json { width:100%; height:86px; box-sizing:border-box;
    background:#1e1e1e; color:#9fb8a0; border:1px solid #444;
    border-radius:4px; font-family:Consolas, monospace; font-size:10px;
    margin-top:8px; }
  .btn { background:#333; color:#ddd; border:1px solid #555;
    border-radius:4px; padding:6px 10px; font-size:11px; cursor:pointer;
    font-family:inherit; }
  .btn:hover { background:#3d3d3d; color:#fff; }
  .btn.primary { background:#F2994D; color:#1e1e1e; border-color:#F2994D;
    font-weight:600; }
  .btn.primary:hover { background:#ffb066; }
  #panel_actions { display:flex; gap:6px; margin-top:8px; flex-wrap:wrap; }
  #copy_state { font-size:11px; color:#8fbf7f; margin-top:6px; min-height:14px; }
  #controls { position:fixed; left:14px; top:40px; width:250px;
    background:rgba(38,38,38,0.94); border-radius:6px; font-size:11px;
    box-shadow:0 2px 10px rgba(0,0,0,0.45); }
  #controls_head { padding:8px 12px; font-size:10px; letter-spacing:.6px;
    text-transform:uppercase; color:#F2994D; cursor:pointer;
    display:flex; justify-content:space-between; user-select:none; }
  #controls_body { padding:0 12px 10px 12px; max-height:52vh; overflow-y:auto; }
  #controls.collapsed #controls_body { display:none; }
  #controls input[type=text], #controls select { width:100%;
    box-sizing:border-box; background:#1e1e1e; color:#ddd;
    border:1px solid #444; border-radius:4px; padding:4px 6px;
    font-size:11px; font-family:inherit; }
  .f_row { margin:8px 0 0 0; }
  .f_row label { display:block; color:#888; font-size:10px;
    text-transform:uppercase; letter-spacing:.4px; margin-bottom:3px; }
  .group_frame { fill:rgba(255,255,255,0.035); stroke:#555;
    stroke-dasharray:6 5; stroke-width:1px; }
  .group_label { fill:#F2994D; font-size:13px; font-weight:600;
    pointer-events:none; }
</style>
</head>
<body>
<svg id="graph">
  <defs>
    <marker id="head" viewBox="0 0 10 10" refX="9" refY="5"
            markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M 0 0 L 10 5 L 0 10 z" fill="#F2994D"/>
    </marker>
  </defs>
</svg>
<div id="title_bar">__TITLE__ &mdash; link planner</div>
<div id="controls">
  <div id="controls_head"><span>Filters &amp; grouping</span><span id="controls_caret">&#9662;</span></div>
  <div id="controls_body">
    <div class="f_row"><label for="f_search">Search</label>
      <input id="f_search" type="text" placeholder="part of a file name..."/></div>
    <div class="f_row"><label for="f_arrange">Arrangement</label>
      <select id="f_arrange">
        <option value="force">Force &mdash; clusters related files</option>
        <option value="hierarchy">Hierarchy &mdash; what goes into what</option>
      </select></div>
    <div class="f_row"><label for="f_group">Group by</label>
      <select id="f_group"></select></div>
    <div id="f_segments"></div>
    <div class="f_row"><button class="btn" id="f_reset">Reset</button>
      <button class="btn" id="f_fit">Fit</button>
      <span id="f_count" style="color:#888;margin-left:6px;"></span></div>
  </div>
</div>
<div id="hint">Drag from one box to another to link it in &middot; click an arrow to remove it &middot; SHIFT+drag moves a box &middot; scroll to zoom &middot; drag background to pan</div>
<div id="panel">
  <h2>Plan &mdash; <span id="plan_count">0</span> link(s)</h2>
  <ul id="plan_list"></ul>
  <div id="panel_actions">
    <button class="btn primary" id="copy_btn">Copy plan for Revit</button>
    <button class="btn" id="clear_btn">Clear all</button>
  </div>
  <div id="copy_state"></div>
  <textarea id="plan_json" readonly
    title="If the browser blocks the clipboard, select this and copy it by hand."></textarea>
</div>
<script>
var DATA = __DATA__;

(function() {
  var svg = document.getElementById("graph");
  var NS = "http://www.w3.org/2000/svg";
  var W = window.innerWidth, H = window.innerHeight;
  var HEADER_H = 22, TAG_H = 15, BOX_MIN_W = 190, BOX_MAX_W = 560;

  var spreadR = 240 + DATA.nodes.length * 12;
  var nodes = DATA.nodes.map(function(n, i) {
    var a = (i / DATA.nodes.length) * Math.PI * 2;
    return {
      id: n.id, label: n.id,
      discipline_label: n.discipline_label,
      discipline_color: n.discipline_color,
      segments: n.segments || [],
      x: W/2 + Math.cos(a) * spreadR + (Math.random()-0.5)*40,
      y: H/2 + Math.sin(a) * spreadR + (Math.random()-0.5)*40,
      vx:0, vy:0, fixed:false, hidden:false
    };
  });
  var byId = {};
  nodes.forEach(function(n) { byId[n.id] = n; });

  // The plan itself: source -> target means "source is linked INTO
  // target", the same direction the arrow points.
  var matches = [];
  (DATA.matches || []).forEach(function(m) {
    if (byId[m.source] && byId[m.target] && m.source !== m.target) {
      matches.push({ source: m.source, target: m.target });
    }
  });

  var gRoot = document.createElementNS(NS, "g");
  svg.appendChild(gRoot);
  var gGroups = document.createElementNS(NS, "g");
  var gArrows = document.createElementNS(NS, "g");
  var gNodes = document.createElementNS(NS, "g");
  gRoot.appendChild(gGroups);
  gRoot.appendChild(gArrows);
  gRoot.appendChild(gNodes);

  function svgText(x, y, text, fill, size, weight) {
    var t = document.createElementNS(NS, "text");
    t.setAttribute("x", x); t.setAttribute("y", y);
    t.setAttribute("text-anchor", "middle");
    t.setAttribute("fill", fill);
    t.setAttribute("font-size", size);
    if (weight) t.setAttribute("font-weight", weight);
    t.textContent = text;
    return t;
  }

  var nodeEls = nodes.map(function(n) {
    var g = document.createElementNS(NS, "g");
    g.setAttribute("class", "node");
    gNodes.appendChild(g);
    var boxH = HEADER_H + TAG_H + 8;
    var colour = n.discipline_color || "#888888";

    var title = document.createElementNS(NS, "title");
    title.textContent = n.label;
    g.appendChild(title);

    var texts = [];
    var t1 = svgText(0, -boxH/2 + 15, n.label, "#eee", 12, "bold");
    g.appendChild(t1); texts.push(t1);
    var t2 = svgText(0, -boxH/2 + HEADER_H + 10,
                     n.discipline_label || "Unknown", colour, 10, "bold");
    g.appendChild(t2); texts.push(t2);

    var widest = 0;
    texts.forEach(function(el) {
      var w = 0;
      try { w = el.getComputedTextLength(); } catch (e) { w = 0; }
      if (w > widest) widest = w;
    });
    var boxW = Math.max(BOX_MIN_W, Math.min(BOX_MAX_W, widest + 24));
    if (widest + 24 > BOX_MAX_W) {
      texts.forEach(function(el) {
        var guard = 0;
        while (guard++ < 200) {
          var w = 0;
          try { w = el.getComputedTextLength(); } catch (e) { break; }
          if (w <= boxW - 20 || el.textContent.length < 5) break;
          el.textContent = el.textContent.slice(0, -2) + "\\u2026";
        }
      });
    }
    n._w = boxW; n._h = boxH;

    var rect = document.createElementNS(NS, "rect");
    rect.setAttribute("x", -boxW/2); rect.setAttribute("y", -boxH/2);
    rect.setAttribute("width", boxW); rect.setAttribute("height", boxH);
    rect.setAttribute("rx", 7);
    rect.setAttribute("fill", "#2b2b2b");
    rect.setAttribute("stroke", colour);
    rect.setAttribute("stroke-width", "2");
    g.insertBefore(rect, title.nextSibling);
    return g;
  });

  // ---- layout ----------------------------------------------------
  function step() {
    var i, j, n1, n2, dx, dy, dist, force;
    for (i = 0; i < nodes.length; i++) {
      n1 = nodes[i];
      if (n1.fixed || n1.hidden) continue;
      var fx, fy;
      var centre = GROUPS.active ? GROUPS.centres[groupKeyOf(n1)] : null;
      if (centre) { fx = (centre.x - n1.x) * 0.02; fy = (centre.y - n1.y) * 0.02; }
      else { fx = (W/2 - n1.x) * 0.002; fy = (H/2 - n1.y) * 0.002; }
      for (j = 0; j < nodes.length; j++) {
        if (i === j) continue;
        n2 = nodes[j];
        if (n2.hidden) continue;
        dx = n1.x - n2.x; dy = n1.y - n2.y;
        dist = Math.sqrt(dx*dx + dy*dy) || 1;
        var span = ((n1._w || 190) + (n2._w || 190)) / 2;
        force = Math.min(55000 * (span/190) / (dist*dist), 16 * (span/190));
        fx += (dx/dist) * force; fy += (dy/dist) * force;
      }
      n1.vx = (n1.vx + fx) * 0.75;
      n1.vy = (n1.vy + fy) * 0.75;
    }
    matches.forEach(function(m) {
      var a = byId[m.source], b = byId[m.target];
      if (!a || !b || a.hidden || b.hidden) return;
      dx = b.x - a.x; dy = b.y - a.y;
      dist = Math.sqrt(dx*dx + dy*dy) || 1;
      var rest = 200 + ((a._w||190) + (b._w||190)) / 2;
      var pull = (dist - rest) * 0.02;
      var ux = dx/dist, uy = dy/dist;
      if (!a.fixed) { a.vx += ux*pull; a.vy += uy*pull; }
      if (!b.fixed) { b.vx -= ux*pull; b.vy -= uy*pull; }
    });
    nodes.forEach(function(n) {
      if (n.fixed || n.hidden) return;
      n.x += n.vx; n.y += n.vy;
    });
    // Boxes must never sit on top of each other - see DeeLinkMAP, where
    // forces alone left overlaps behind once grouping pulled inward.
    var moved = true, pass = 0;
    while (moved && pass < 24) {
      moved = false; pass++;
      for (i = 0; i < nodes.length; i++) {
        n1 = nodes[i];
        if (n1.hidden) continue;
        for (j = i + 1; j < nodes.length; j++) {
          n2 = nodes[j];
          if (n2.hidden) continue;
          var needX = ((n1._w||190) + (n2._w||190)) / 2 + 14;
          var needY = ((n1._h||70) + (n2._h||70)) / 2 + 12;
          var sx = n2.x - n1.x, sy = n2.y - n1.y;
          var ox = needX - Math.abs(sx), oy = needY - Math.abs(sy);
          if (ox <= 0 || oy <= 0) continue;
          moved = true;
          if (ox < oy) {
            var px = (sx < 0 ? -1 : 1) * ox / 2;
            if (!n1.fixed) n1.x -= px;
            if (!n2.fixed) n2.x += px;
          } else {
            var py = (sy < 0 ? -1 : 1) * oy / 2;
            if (!n1.fixed) n1.y -= py;
            if (!n2.fixed) n2.y += py;
          }
        }
      }
    }
  }

  var ticks = 0, settling = false, pendingFit = true;
  function settle() {
    // A force run still in flight would keep calling step() and undo the
    // computed rows, so it checks each frame whether it is still the
    // arrangement in charge (the map had exactly this bug).
    if (typeof ARRANGE !== "undefined" && ARRANGE === "hierarchy") {
      settling = false;
      return;
    }
    settling = true;
    step(); ticks++; render();
    if (ticks < 200) requestAnimationFrame(settle);
    else { settling = false; if (pendingFit) { pendingFit = false; fitView(); } }
  }
  function restartLayout() {
    if (typeof ARRANGE !== "undefined" && ARRANGE === "hierarchy") {
      // Computed, not simulated - there is nothing for the force loop
      // to do, and letting it run would pull the rows apart.
      layoutHierarchy();
      render();
      fitView();
      return;
    }
    ticks = 0; pendingFit = true;
    if (!settling) settle();
  }

  // ---- arrows ----------------------------------------------------
  function edgePoint(from, to) {
    // Where the line meets the box edge, so the arrowhead lands on the
    // border instead of being buried under the box.
    var dx = to.x - from.x, dy = to.y - from.y;
    if (!dx && !dy) return { x: from.x, y: from.y };
    var hw = (from._w || 190) / 2 + 2, hh = (from._h || 70) / 2 + 2;
    var scale = Math.min(
      hw / (Math.abs(dx) || 0.0001),
      hh / (Math.abs(dy) || 0.0001));
    return { x: from.x + dx * scale, y: from.y + dy * scale };
  }

  var arrowEls = [];
  function buildArrows() {
    arrowEls.forEach(function(pair) {
      gArrows.removeChild(pair.line);
      gArrows.removeChild(pair.hit);
    });
    arrowEls = [];
    matches.forEach(function(m, index) {
      var hit = document.createElementNS(NS, "line");
      hit.setAttribute("class", "arrow_hit");
      var line = document.createElementNS(NS, "line");
      line.setAttribute("class", "arrow");
      line.setAttribute("marker-end", "url(#head)");
      var t = document.createElementNS(NS, "title");
      t.textContent = m.source + "  linked into  " + m.target + "  (click to remove)";
      line.appendChild(t);
      gArrows.appendChild(hit);
      gArrows.appendChild(line);
      function remove(ev) { ev.stopPropagation(); removeMatch(index); }
      hit.addEventListener("click", remove);
      line.addEventListener("click", remove);
      arrowEls.push({ line: line, hit: hit, m: m });
    });
  }

  function removeMatch(index) {
    matches.splice(index, 1);
    refreshPlan();
  }

  function addMatch(sourceId, targetId) {
    if (!sourceId || !targetId || sourceId === targetId) return;
    for (var i = 0; i < matches.length; i++) {
      if (matches[i].source === sourceId && matches[i].target === targetId) return;
    }
    matches.push({ source: sourceId, target: targetId });
    refreshPlan();
  }

  function refreshPlan() {
    buildArrows();
    document.getElementById("plan_count").textContent = matches.length;
    var ul = document.getElementById("plan_list");
    ul.innerHTML = "";
    matches.forEach(function(m, index) {
      var li = document.createElement("li");
      var s = document.createElement("div"); s.textContent = m.source;
      var into = document.createElement("div");
      into.className = "into"; into.textContent = "linked into";
      var t = document.createElement("div"); t.textContent = m.target;
      var kill = document.createElement("span");
      kill.className = "kill"; kill.innerHTML = "&#10005;";
      kill.addEventListener("click", function() { removeMatch(index); });
      li.appendChild(s); li.appendChild(into); li.appendChild(t);
      li.appendChild(kill);
      ul.appendChild(li);
    });
    document.getElementById("plan_json").value = JSON.stringify({
      tool: "DeeMAPLink", matches: matches
    });
    document.getElementById("copy_state").textContent = "";
    // Drawing an arrow changes the dependency structure, so in
    // hierarchy the rows are recomputed - watching that structure form
    // is the whole reason to be in this view while planning.
    if (ARRANGE === "hierarchy") { restartLayout(); } else { render(); }
  }

  // ---- render ----------------------------------------------------
  function render() {
    arrowEls.forEach(function(pair) {
      var a = byId[pair.m.source], b = byId[pair.m.target];
      if (!a || !b || a.hidden || b.hidden) {
        pair.line.style.display = "none"; pair.hit.style.display = "none";
        return;
      }
      pair.line.style.display = ""; pair.hit.style.display = "";
      var p1 = edgePoint(a, b), p2 = edgePoint(b, a);
      [pair.line, pair.hit].forEach(function(el) {
        el.setAttribute("x1", p1.x); el.setAttribute("y1", p1.y);
        el.setAttribute("x2", p2.x); el.setAttribute("y2", p2.y);
      });
    });
    nodeEls.forEach(function(g, i) {
      if (nodes[i].hidden) { g.style.display = "none"; return; }
      g.style.display = "";
      g.setAttribute("transform", "translate(" + nodes[i].x + "," + nodes[i].y + ")");
    });
    drawGroupFrames();
  }

  // ---- pan / zoom -------------------------------------------------
  var view = { x:0, y:0, k:1 };
  function applyView() {
    gRoot.setAttribute("transform",
      "translate(" + view.x + "," + view.y + ") scale(" + view.k + ")");
  }
  function toWorld(clientX, clientY) {
    return { x: (clientX - view.x) / view.k, y: (clientY - view.y) / view.k };
  }
  function fitView() {
    var minX=Infinity, minY=Infinity, maxX=-Infinity, maxY=-Infinity, any=false;
    nodes.forEach(function(n) {
      if (n.hidden) return;
      any = true;
      if (n.x < minX) minX = n.x;
      if (n.y < minY) minY = n.y;
      if (n.x > maxX) maxX = n.x;
      if (n.y > maxY) maxY = n.y;
    });
    if (!any) return;
    minX -= 180; maxX += 180; minY -= 110; maxY += 110;
    var w = Math.max(1, maxX-minX), h = Math.max(1, maxY-minY);
    var k = Math.max(0.08, Math.min(Math.min(W/w, H/h), 1.4));
    view.k = k;
    view.x = (W - w*k)/2 - minX*k;
    view.y = (H - h*k)/2 - minY*k;
    applyView();
  }

  svg.addEventListener("wheel", function(ev) {
    ev.preventDefault();
    var before = toWorld(ev.clientX, ev.clientY);
    view.k *= (ev.deltaY < 0 ? 1.12 : 1/1.12);
    view.k = Math.max(0.05, Math.min(view.k, 3));
    var after = toWorld(ev.clientX, ev.clientY);
    view.x += (after.x - before.x) * view.k;
    view.y += (after.y - before.y) * view.k;
    applyView();
  }, { passive: false });

  // ---- dragging: link, or move with SHIFT -------------------------
  var drag = null;
  var rubber = document.createElementNS(NS, "line");
  rubber.setAttribute("class", "rubber");
  rubber.style.display = "none";
  gRoot.appendChild(rubber);

  function nodeIndexFromEvent(ev) {
    var el = ev.target;
    while (el && el !== svg) {
      var idx = nodeEls.indexOf(el);
      if (idx !== -1) return idx;
      el = el.parentNode;
    }
    return -1;
  }

  svg.addEventListener("mousedown", function(ev) {
    var idx = nodeIndexFromEvent(ev);
    var world = toWorld(ev.clientX, ev.clientY);
    if (idx !== -1 && !ev.shiftKey) {
      // Linking is the primary gesture - this page exists to draw links.
      drag = { mode:"link", from:idx };
      nodeEls[idx].classList.add("source");
      rubber.style.display = "";
      rubber.setAttribute("x1", nodes[idx].x);
      rubber.setAttribute("y1", nodes[idx].y);
      rubber.setAttribute("x2", world.x);
      rubber.setAttribute("y2", world.y);
      svg.classList.add("linking");
    } else if (idx !== -1) {
      drag = { mode:"move", index:idx };
      nodes[idx].fixed = true;
    } else {
      drag = { mode:"pan", x:ev.clientX - view.x, y:ev.clientY - view.y };
    }
  });

  svg.addEventListener("mousemove", function(ev) {
    if (!drag) return;
    var world = toWorld(ev.clientX, ev.clientY);
    if (drag.mode === "link") {
      rubber.setAttribute("x2", world.x);
      rubber.setAttribute("y2", world.y);
    } else if (drag.mode === "move") {
      nodes[drag.index].x = world.x;
      nodes[drag.index].y = world.y;
      render();
    } else {
      view.x = ev.clientX - drag.x;
      view.y = ev.clientY - drag.y;
      applyView();
    }
  });

  window.addEventListener("mouseup", function(ev) {
    if (!drag) return;
    if (drag.mode === "link") {
      var idx = nodeIndexFromEvent(ev);
      nodeEls[drag.from].classList.remove("source");
      rubber.style.display = "none";
      svg.classList.remove("linking");
      if (idx !== -1 && idx !== drag.from) {
        addMatch(nodes[drag.from].id, nodes[idx].id);
      }
    } else if (drag.mode === "move") {
      nodes[drag.index].fixed = false;
    }
    drag = null;
  });

  // ---- filtering + grouping (same parts as DeeLinkMAP) ------------
  var GROUPS = { active:false, key:null, centres:{}, order:[] };
  var segSelects = [];

  function segOf(n, i) {
    var p = n.segments || [];
    return (i >= 0 && i < p.length) ? p[i] : "";
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

  // ---- second arrangement: hierarchy of what goes into what --------
  // An arrow means "source is linked INTO target", so source is placed
  // ABOVE target and every arrow points down. Read top to bottom: each
  // row goes into the row below it, and the bottom row is the host that
  // ends up receiving everything.
  var ARRANGE = "force";

  function rankNodes(list) {
    var rank = {}, present = {};
    list.forEach(function(n) { rank[n.id] = 0; present[n.id] = true; });
    var live = matches.filter(function(m) {
      return present[m.source] && present[m.target];
    });
    // Relaxation, not a topological sort: a plan can easily contain a
    // cycle while it is being drawn (A into B, B into A), and a
    // topological sort has nothing to say about one. Bounded, so a cycle
    // costs a few passes instead of spinning.
    var limit = Math.min(list.length, 60), changed = true, pass = 0;
    while (changed && pass < limit) {
      changed = false; pass++;
      live.forEach(function(m) {
        var want = rank[m.source] + 1;
        if (want > rank[m.target]) { rank[m.target] = want; changed = true; }
      });
    }
    return rank;
  }

  function layoutHierarchy() {
    var list = nodes.filter(function(n) { return !n.hidden; });
    if (!list.length) return;
    var rank = rankNodes(list);

    var rows = {}, maxRank = 0;
    list.forEach(function(n) {
      var r = rank[n.id] || 0;
      if (r > maxRank) maxRank = r;
      (rows[r] = rows[r] || []).push(n);
    });
    for (var r0 = 0; r0 <= maxRank; r0++) {
      if (rows[r0]) rows[r0].sort(function(a, b) { return a.id < b.id ? -1 : 1; });
    }

    // Barycentre ordering - place each box near the average position of
    // what it connects to on the neighbouring row, so arrows stop
    // crossing each other.
    var index = {};
    function reindex() {
      for (var rr = 0; rr <= maxRank; rr++) {
        (rows[rr] || []).forEach(function(n, i) { index[n.id] = i; });
      }
    }
    reindex();
    var up = {}, down = {};
    matches.forEach(function(m) {
      var a = byId[m.source], b = byId[m.target];
      if (!a || !b || a.hidden || b.hidden) return;
      (up[m.target] = up[m.target] || []).push(m.source);
      (down[m.source] = down[m.source] || []).push(m.target);
    });
    function sweep(useUp) {
      var order = [];
      for (var rr = 0; rr <= maxRank; rr++) order.push(rr);
      if (!useUp) order.reverse();
      order.forEach(function(rr) {
        var row = rows[rr];
        if (!row || row.length < 2) return;
        var table = useUp ? up : down;
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

    // Wide rows wrap: a row of thirty boxes is ~10,000px across and
    // only fits on screen at a zoom where nothing can be read. Arrows
    // still all point down, because a rank's sub-rows are placed before
    // the next rank starts.
    var ROW_GAP = 90, COL_GAP = 34, WRAP_GAP = 26, MAX_ROW_W = 2400;
    var y = 0;
    for (var rr2 = 0; rr2 <= maxRank; rr2++) {
      var row2 = rows[rr2] || [];
      if (!row2.length) continue;
      var chunks = [], current = [], currentW = 0;
      row2.forEach(function(n) {
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
    var cx = W / 2, cy = H / 2 - y / 2;
    list.forEach(function(n) { n.x += cx; n.y += cy; });
  }

  function buildControls() {
    var total = 0;
    nodes.forEach(function(n) {
      if ((n.segments||[]).length > total) total = n.segments.length;
    });
    var gsel = document.getElementById("f_group");
    gsel.appendChild(option("", "Nothing (free layout)"));
    gsel.appendChild(option("discipline", "Discipline"));
    var host = document.getElementById("f_segments");
    for (var i = 0; i < total; i++) {
      var values = distinctSeg(i);
      if (values.length < 2) continue;
      var preview = values.slice(0,3).join(", ") + (values.length > 3 ? ", ..." : "");
      gsel.appendChild(option(String(i), "Part " + (i+1) + "  (" + preview + ")"));
      var row = document.createElement("div");
      row.className = "f_row";
      var lab = document.createElement("label");
      lab.textContent = "Part " + (i+1);
      row.appendChild(lab);
      var sel = document.createElement("select");
      sel.appendChild(option("", "All (" + values.length + ")"));
      values.forEach(function(v) { sel.appendChild(option(v, v)); });
      sel.setAttribute("data-seg", String(i));
      sel.addEventListener("change", applyFilters);
      row.appendChild(sel);
      host.appendChild(row);
      segSelects.push(sel);
    }
    var asel = document.getElementById("f_arrange");
    asel.addEventListener("change", function() {
      ARRANGE = asel.value;
      // Grouping and hierarchy both decide where a box goes, so only one
      // can be in charge. Hierarchy wins while selected, and the group
      // control is disabled rather than silently ignored.
      gsel.disabled = (ARRANGE === "hierarchy");
      if (ARRANGE === "hierarchy") {
        GROUPS.active = false;
        drawGroupFrames();
      } else if (gsel.value !== "") {
        GROUPS.active = true;
        GROUPS.key = (gsel.value === "discipline")
                   ? "discipline" : parseInt(gsel.value, 10);
        layoutGroups();
      }
      restartLayout();
    });
    gsel.addEventListener("change", function() {
      var v = gsel.value;
      GROUPS.active = (v !== "");
      GROUPS.key = (v === "" || v === "discipline") ? v : parseInt(v, 10);
      layoutGroups(); restartLayout();
    });
    document.getElementById("f_search").addEventListener("input", applyFilters);
    document.getElementById("f_reset").addEventListener("click", function() {
      document.getElementById("f_search").value = "";
      segSelects.forEach(function(s) { s.value = ""; });
      gsel.value = ""; gsel.disabled = false;
      asel.value = "force"; ARRANGE = "force";
      GROUPS.active = false; GROUPS.key = null;
      applyFilters();
    });
    document.getElementById("f_fit").addEventListener("click", fitView);
    document.getElementById("controls_head").addEventListener("click", function() {
      var box = document.getElementById("controls");
      var collapsed = box.className === "collapsed";
      box.className = collapsed ? "" : "collapsed";
      document.getElementById("controls_caret").innerHTML =
        collapsed ? "&#9662;" : "&#9656;";
    });
  }
  function matchesFilters(n) {
    var terms = (document.getElementById("f_search").value || "")
                  .toLowerCase().split(" ");
    var name = (n.label || "").toLowerCase();
    for (var i = 0; i < terms.length; i++) {
      if (terms[i] && name.indexOf(terms[i]) === -1) return false;
    }
    for (var s = 0; s < segSelects.length; s++) {
      var want = segSelects[s].value;
      if (!want) continue;
      if (segOf(n, parseInt(segSelects[s].getAttribute("data-seg"), 10)) !== want) return false;
    }
    return true;
  }
  function applyFilters() {
    var shown = 0;
    nodes.forEach(function(n) {
      n.hidden = !matchesFilters(n);
      if (!n.hidden) shown++;
    });
    document.getElementById("f_count").textContent = shown + " of " + nodes.length;
    layoutGroups(); restartLayout();
  }
  function layoutGroups() {
    GROUPS.centres = {}; GROUPS.order = [];
    if (!GROUPS.active) return;
    var seen = {}, tally = {}, busiest = 1;
    nodes.forEach(function(n) {
      if (n.hidden) return;
      var k = groupKeyOf(n);
      if (!seen[k]) { seen[k] = true; GROUPS.order.push(k); }
      tally[k] = (tally[k] || 0) + 1;
      if (tally[k] > busiest) busiest = tally[k];
    });
    GROUPS.order.sort();
    var count = GROUPS.order.length || 1;
    var cols = Math.ceil(Math.sqrt(count));
    var rows = Math.ceil(count / cols);
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
        x: W/2 + (c - (cols-1)/2) * cell,
        y: H/2 + (r - (rows-1)/2) * cell * 0.8
      };
    });
  }
  function drawGroupFrames() {
    while (gGroups.firstChild) gGroups.removeChild(gGroups.firstChild);
    if (!GROUPS.active) return;
    var boxes = {};
    nodes.forEach(function(n) {
      if (n.hidden) return;
      var k = groupKeyOf(n), b = boxes[k];
      if (!b) { boxes[k] = { x0:n.x, y0:n.y, x1:n.x, y1:n.y }; return; }
      if (n.x < b.x0) b.x0 = n.x;
      if (n.y < b.y0) b.y0 = n.y;
      if (n.x > b.x1) b.x1 = n.x;
      if (n.y > b.y1) b.y1 = n.y;
    });
    GROUPS.order.forEach(function(k) {
      var b = boxes[k];
      if (!b) return;
      var padX = 150, padY = 80;
      var rect = document.createElementNS(NS, "rect");
      rect.setAttribute("class", "group_frame");
      rect.setAttribute("x", b.x0-padX); rect.setAttribute("y", b.y0-padY);
      rect.setAttribute("width", (b.x1-b.x0)+padX*2);
      rect.setAttribute("height", (b.y1-b.y0)+padY*2);
      rect.setAttribute("rx", 14);
      gGroups.appendChild(rect);
      var t = document.createElementNS(NS, "text");
      t.setAttribute("class", "group_label");
      t.setAttribute("x", b.x0-padX+14);
      t.setAttribute("y", b.y0-padY+22);
      t.textContent = k;
      gGroups.appendChild(t);
    });
  }

  // ---- handing the plan back --------------------------------------
  document.getElementById("copy_btn").addEventListener("click", function() {
    var box = document.getElementById("plan_json");
    var state = document.getElementById("copy_state");
    function ok() {
      state.style.color = "#8fbf7f";
      state.textContent = "Copied. In Revit, press 'Paste Plan'.";
    }
    function fail() {
      state.style.color = "#e5a663";
      state.textContent = "Could not reach the clipboard - select the text below and copy it.";
      box.focus(); box.select();
    }
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(box.value).then(ok, function() {
          try {
            box.focus(); box.select();
            document.execCommand("copy") ? ok() : fail();
          } catch (e) { fail(); }
        });
        return;
      }
      box.focus(); box.select();
      document.execCommand("copy") ? ok() : fail();
    } catch (e) { fail(); }
  });

  document.getElementById("clear_btn").addEventListener("click", function() {
    if (!matches.length) return;
    matches.length = 0;
    refreshPlan();
  });

  window.addEventListener("resize", function() {
    W = window.innerWidth; H = window.innerHeight;
  });

  buildControls();
  applyFilters();
  refreshPlan();
})();
</script>
</body>
</html>
"""
