# -*- coding: utf-8 -*-
"""
dee_matchset_io
Shared JSON I/O for the "matches / per-match overrides / Matrix
separators" slice of a Save Set / Load Set file. Every linking tool
(DeeInitiator, DeeMAPLink, DeeSuperLINK) keeps its OWN save_set_click/
load_set_click - the surrounding context (Delivery Parties + ACC
destinations for DeeInitiator; ACC-or-local scan context for DeeMAPLink;
ACC-only scan context for DeeSuperLINK) differs too much per tool to
share - but all three delegate this one common slice here instead of
hand-rolling the same JSON shape three times.

Every function is tolerant of missing/malformed input (a hand-edited or
older-format file) and never raises - a Set file failing to parse
cleanly should degrade to "that part came back empty", never crash the
loader partway through.
"""
import json
import io


def matches_to_json(matches):
    return [[s, t] for s, t in matches]


def matches_from_json(data):
    return [tuple(pair) for pair in (data or [])
            if isinstance(pair, (list, tuple)) and len(pair) == 2]


def match_options_to_json(match_options):
    """{(s,t): {"placement_label":.., "attachment_label":..}} -> a list,
    since JSON object keys can't be tuples."""
    out = []
    for pair, options in (match_options or {}).items():
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        source, target = pair
        entry = {"source": source, "target": target}
        entry.update(options or {})
        out.append(entry)
    return out


def match_options_from_json(data):
    out = {}
    for entry in (data or []):
        if not isinstance(entry, dict):
            continue
        source = entry.get("source")
        target = entry.get("target")
        if not source or not target:
            continue
        options = dict((k, v) for k, v in entry.items() if k not in ("source", "target"))
        out[(source, target)] = options
    return out


def separators_to_json(state):
    if state is None:
        return {"row": [], "col": []}
    return {"row": list(state.row_separators), "col": list(state.col_separators)}


def separators_from_json(data, state):
    """Restores separator positions onto an existing MatrixState in
    place (the tool creates the MatrixState itself at window construction
    - this only ever repopulates its two separator lists). Tolerates a
    missing/malformed "matrix_separators" block from an older Set file."""
    data = data or {}
    try:
        state.row_separators = sorted(int(i) for i in (data.get("row") or []))
    except Exception:
        state.row_separators = []
    try:
        state.col_separators = sorted(int(i) for i in (data.get("col") or []))
    except Exception:
        state.col_separators = []


def write_set(path, payload_dict):
    """json.dumps's own default ensure_ascii=True is used deliberately -
    an accented file/folder name comes out as plain \\uXXXX escapes, a
    guaranteed-ASCII str regardless of IronPython 2.7's str/unicode
    unification not matching CPython 2 exactly (documented at this
    pattern's original site, DeeInitiator's own save_set_click) -
    sidesteps that whole class of bug rather than risk a mis-encoded
    file."""
    with open(path, "w") as f:
        f.write(json.dumps(payload_dict, indent=2))


def read_set(path, format_key="format"):
    """Returns (data, error_message_or_None). `data` is a plain dict on
    success; on any failure (unreadable file, invalid JSON, not a dict,
    or missing format_key) returns (None, "human-readable reason")."""
    try:
        with io.open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception as e:
        return None, "Could not read that file: {0}".format(e)
    if not isinstance(data, dict) or format_key not in data:
        return None, "That doesn't look like a Set file."
    return data, None
