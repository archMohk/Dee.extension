# -*- coding: utf-8 -*-
"""
dee_sheet_renamer_service
Core engine for DeeSheet's "Sheet Renamer" tab - bulk-builds new Sheet
Number / Sheet Name values from a small token-template language, then
applies them to real ViewSheets in one Transaction.

Token syntax: {KIND} or {KIND:ARG} or {KIND:ARG|MOD|MOD...}. Literal
text outside {} is copied verbatim, which is what gives prefix/suffix
support for free - no separate prefix/suffix fields needed.

Recognized kinds:
    {SheetNumber}         current Sheet Number
    {SheetName}           current Sheet Name
    {Parameter:Name}      any instance parameter on the sheet, by name
                           (LookupParameter - works for built-in, shared
                           and project parameters alike)
    {Serial}              running sequence number for this batch
    {Alpha}                same sequence, rendered as letters
                           (1=A .. 26=Z, 27=AA, 28=AB, ...)
    {Date} / {Date:FMT}   today's date, FMT is a .NET date format
                           string (default "yyyyMMdd")

Modifiers (chainable with |, applied left to right):
    UPPER / LOWER / TITLE / TRIM
    PADn                  zero-pad to n characters (e.g. PAD3)
    FIRSTn / LASTn         first/last n characters
    MID:start:len          start is 1-based
    BEFORE:x / AFTER:x     text before/after the first occurrence of x
    BETWEEN:x:y             text between the first x and the following y
    SPLIT:delim:n           split on delim, take the n-th part - positive
                            n counts from the left (1 = first), negative
                            n counts from the right (-1 = last)
    NUM / ALPHA             keep only digits / only letters
    REPLACE:old:new
    DEFAULT:fallback        use fallback if the value so far is blank

An unrecognized token is left in the output exactly as written (with
its braces) rather than silently dropped, so a typo is obvious in the
Preview column instead of quietly producing a wrong sheet number.

Presets (saved rule templates) are stored via lib/deew_settings.py -
that module already gives every DeeW.Cloud tool a per-tool JSON file
under lib/.deew_settings/; reusing it here avoids a second, parallel
persistence implementation for what is exactly the same need (a named
dict of small settings, saved/loaded/deleted locally, never crashing
the caller on a missing/corrupt file).

Deliberately not built in this pass (kept out to stay maintainable -
see docs/pages/DeeSheet.html for the full list): Roman-numeral
sequences, a parameter-name aliasing/mapping layer (use
{Parameter:ActualName} directly instead), running a different rule per
group in a single pass (filter, apply, change the filter, apply again
covers this), a right-click context menu, a standalone Parameter
Inspector panel, and file-based logging (the pyRevit output window
already gives a full per-row report every run).
"""
import re

from pyrevit import script
from Autodesk.Revit.DB import FilteredElementCollector, ViewSheet, Transaction

import deew_settings

import System

output = script.get_output()

_TOKEN_RE = re.compile(r"\{([^{}]+)\}")
_PRESET_TOOL_NAME = "dee_sheet_renamer_presets"

STATUS_READY = "Ready"
STATUS_UNCHANGED = "Unchanged"
STATUS_EMPTY = "Empty"
STATUS_DUPLICATE = "Duplicate"
STATUS_INVALID = "Invalid"

_NOISE_PARAM_NAMES = set([
    "Sheet Number", "Sheet Name", "Sheet Issue Date", "Approved By",
    "Checked By", "Designed By", "Drawn By",
])

# Revit rejects these characters in Sheet Number/Sheet Name (and most
# other named elements) with a runtime exception - checked here so a
# bad rule shows up as an Invalid row in Preview instead of failing
# Apply partway through a batch.
_INVALID_NAME_CHARS = set("\\:{}[]|;<>?`~")


def _has_invalid_chars(text):
    return any(c in _INVALID_NAME_CHARS for c in (text or ""))


# --------------------------------------------------------------------------
# Scan
# --------------------------------------------------------------------------
class SheetRenameRow(object):
    def __init__(self, sheet):
        self.sheet = sheet
        self.original_number = sheet.SheetNumber or ""
        self.original_name = sheet.Name or ""
        self.new_number = self.original_number
        self.new_name = self.original_name
        self.selected = False
        self.status = ""

    @property
    def current_text(self):
        return "{0} - {1}".format(self.original_number, self.original_name)


def scan(doc):
    """Every real (non-placeholder) ViewSheet in the project, sorted by
    current Sheet Number. Placeholder sheets are skipped - they don't
    reliably expose the same parameters/behaviour as real sheets and
    are rare enough in most projects not to be worth the extra branching
    in this first version."""
    rows = []
    for sheet in FilteredElementCollector(doc).OfClass(ViewSheet):
        try:
            if getattr(sheet, "IsPlaceholder", False):
                continue
            rows.append(SheetRenameRow(sheet))
        except Exception:
            continue
    rows.sort(key=lambda r: r.original_number)
    return rows


def list_common_parameter_names(rows, max_sheets=40):
    """Union of instance-parameter names found across a sample of the
    scanned sheets - powers the "Insert Parameter Token" and
    group/sort-by pickers without the user having to remember exact
    parameter names."""
    names = set()
    for row in rows[:max_sheets]:
        try:
            for p in row.sheet.Parameters:
                try:
                    name = p.Definition.Name
                    if name and name not in _NOISE_PARAM_NAMES:
                        names.add(name)
                except Exception:
                    continue
        except Exception:
            continue
    return sorted(names)


def _read_param_value(sheet, name):
    if not name:
        return ""
    try:
        p = sheet.LookupParameter(name)
        if p is None:
            return ""
        val = p.AsString()
        if val:
            return val
        val = p.AsValueString()
        return val or ""
    except Exception:
        return ""


# --------------------------------------------------------------------------
# Token engine
# --------------------------------------------------------------------------
def _to_alpha(n):
    n = max(1, int(n))
    letters = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _split_pick(value, delim, index):
    """Splits value on delim and returns the piece at index (1-based,
    counting from the left for a positive index, or from the right for
    a negative one - -1 is the last piece, -2 the second-to-last, same
    convention as Python's own negative list indexing). Out-of-range or
    a zero index returns value unchanged rather than raising, since this
    also backs the free-typed {..|SPLIT:delim:n} token modifier where a
    bad index shouldn't blow up the whole rule."""
    try:
        bits = value.split(delim)
        if index > 0:
            return bits[index - 1] if index <= len(bits) else value
        if index < 0:
            return bits[index] if abs(index) <= len(bits) else value
        return value
    except Exception:
        return value


def _apply_modifier(value, mod):
    if not mod:
        return value
    upper_mod = mod.upper()
    try:
        if upper_mod == "UPPER":
            return value.upper()
        if upper_mod == "LOWER":
            return value.lower()
        if upper_mod == "TITLE":
            return value.title()
        if upper_mod == "TRIM":
            return value.strip()
        if upper_mod.startswith("PAD"):
            n = int(upper_mod[3:])
            return value.rjust(n, "0")
        if upper_mod.startswith("FIRST"):
            n = int(upper_mod[5:])
            return value[:n]
        if upper_mod.startswith("LAST"):
            n = int(upper_mod[4:])
            return value[-n:] if n > 0 else value
        if upper_mod.startswith("MID:"):
            _, start, length = mod.split(":")
            start = int(start)
            length = int(length)
            return value[start - 1:start - 1 + length]
        if upper_mod.startswith("BEFORE:"):
            delim = mod.split(":", 1)[1]
            return value.split(delim, 1)[0] if delim and delim in value else value
        if upper_mod.startswith("AFTER:"):
            delim = mod.split(":", 1)[1]
            return value.split(delim, 1)[1] if delim and delim in value else value
        if upper_mod.startswith("BETWEEN:"):
            _, d1, d2 = mod.split(":", 2)
            if d1 and d1 in value:
                after = value.split(d1, 1)[1]
                if d2 and d2 in after:
                    return after.split(d2, 1)[0]
            return value
        if upper_mod.startswith("SPLIT:"):
            _, delim, idx = mod.split(":", 2)
            return _split_pick(value, delim, int(idx))
        if upper_mod == "NUM":
            return "".join(c for c in value if c.isdigit())
        if upper_mod == "ALPHA":
            return "".join(c for c in value if c.isalpha())
        if upper_mod.startswith("REPLACE:"):
            _, old, new = mod.split(":", 2)
            return value.replace(old, new)
        if upper_mod.startswith("DEFAULT:"):
            fallback = mod.split(":", 1)[1]
            return value if value.strip() != "" else fallback
    except Exception:
        return value
    return value


def _apply_modifiers(value, mods):
    for m in mods:
        value = _apply_modifier(value, m)
    return value


def _clean_modifier(mod):
    """Only trims whitespace that can't be a meaningful delimiter: a
    leading space after the | (so "{Serial| PAD3}" typed for
    readability still matches), and a trailing space only for
    argument-less keywords like UPPER/TRIM. A colon-argument modifier
    (BEFORE:x, SPLIT:d:n, REPLACE:old:new, ...) keeps everything after
    its first colon exactly as typed, since the argument itself might
    legitimately be a space (e.g. BEFORE: to split on a literal
    space)."""
    mod = mod.lstrip()
    if ":" not in mod:
        mod = mod.rstrip()
    return mod


def _resolve_token(kind, arg, mods, ctx):
    k = (kind or "").strip().upper()
    if k == "SHEETNUMBER":
        value = ctx["original_number"]
    elif k == "SHEETNAME":
        value = ctx["original_name"]
    elif k == "PARAMETER":
        value = _read_param_value(ctx["sheet"], (arg or "").strip())
    elif k == "SERIAL":
        value = str(ctx["serial_value"])
    elif k == "ALPHA":
        value = _to_alpha(ctx["serial_value"])
    elif k == "DATE":
        fmt = (arg or "").strip() or "yyyyMMdd"
        try:
            value = System.DateTime.Now.ToString(fmt)
        except Exception:
            value = System.DateTime.Now.ToString("yyyyMMdd")
    else:
        return "{" + kind + (":" + arg if arg else "") + "}"
    return _apply_modifiers(value, mods)


def render_template(template, ctx):
    if not template:
        return ""

    def _repl(m):
        body = m.group(1)
        parts = body.split("|")
        head = parts[0].strip()
        mods = [_clean_modifier(p) for p in parts[1:]]
        if ":" in head:
            kind, arg = head.split(":", 1)
        else:
            kind, arg = head, None
        return _resolve_token(kind, arg, mods, ctx)

    try:
        return _TOKEN_RE.sub(_repl, template)
    except Exception:
        return template


# --------------------------------------------------------------------------
# Sequencing
# --------------------------------------------------------------------------
def _sort_key(row, key_name):
    if not key_name or key_name == "Sheet Number":
        return row.original_number
    if key_name == "Sheet Name":
        return row.original_name
    return _read_param_value(row.sheet, key_name)


def compute_serials(rows, start, step, reset_param_name, sort_key_name):
    """Numbers `rows` in order, restarting at `start` every time
    `reset_param_name`'s value changes. When a reset field is set, rows
    are sorted by (that field, then sort_key_name) rather than by
    sort_key_name alone - otherwise same-group rows that aren't already
    contiguous under the chosen sort would reset over and over instead
    of once per group (e.g. Architectural/Structural sheets
    interleaved by Sheet Number would each flip the group back and
    forth). Grouping first guarantees one contiguous block per group
    regardless of how the sheets are numbered today.

    Each row's group value and sort key are read from the Revit
    parameter exactly once and cached here - the previous version read
    them twice per row (once to sort, once to detect a group change),
    doubling the Element.LookupParameter calls for no reason when
    either field is a real parameter rather than Sheet Number/Name."""
    group_cache = {}
    sort_cache = {}
    for r in rows:
        group_cache[r] = _read_param_value(r.sheet, reset_param_name) if reset_param_name else None
        sort_cache[r] = _sort_key(r, sort_key_name)

    if reset_param_name:
        ordered = sorted(rows, key=lambda r: (group_cache[r], sort_cache[r]))
    else:
        ordered = sorted(rows, key=lambda r: sort_cache[r])

    serials = {}
    counter = start
    last_group = None
    first = True
    for r in ordered:
        if reset_param_name:
            group_val = group_cache[r]
            if first or group_val != last_group:
                counter = start
                last_group = group_val
        serials[r] = counter
        counter += step
        first = False
    return serials


# --------------------------------------------------------------------------
# Preview-staging tools (all operate on the selected rows' new_number /
# new_name, so they compose: run the rule, then Find & Replace, then a
# quick case-convert, then Apply - each step just edits the same staged
# values further)
# --------------------------------------------------------------------------
def generate_preview(rows, number_template, name_template, start, step,
                      reset_param_name, sort_key_name):
    selected_rows = [r for r in rows if r.selected]
    serials = compute_serials(selected_rows, start, step, reset_param_name, sort_key_name)
    for r in selected_rows:
        ctx = {
            "sheet": r.sheet,
            "original_number": r.original_number,
            "original_name": r.original_name,
            "serial_value": serials.get(r, start),
        }
        if number_template:
            r.new_number = render_template(number_template, ctx)
        if name_template:
            r.new_name = render_template(name_template, ctx)


def reset_preview(rows):
    for r in rows:
        if r.selected:
            r.new_number = r.original_number
            r.new_name = r.original_name


def find_replace(rows, find_text, replace_text, target, case_sensitive, whole_word):
    if not find_text:
        return
    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = re.escape(find_text)
    if whole_word:
        pattern = r"\b" + pattern + r"\b"
    safe_replace = replace_text.replace("\\", "\\\\")
    for r in rows:
        if not r.selected:
            continue
        try:
            if target in ("number", "both"):
                r.new_number = re.sub(pattern, safe_replace, r.new_number, flags=flags)
            if target in ("name", "both"):
                r.new_name = re.sub(pattern, safe_replace, r.new_name, flags=flags)
        except Exception:
            continue


def add_text(rows, text, target, position):
    if not text:
        return
    for r in rows:
        if not r.selected:
            continue
        if target in ("number", "both"):
            r.new_number = (text + r.new_number) if position == "prefix" else (r.new_number + text)
        if target in ("name", "both"):
            r.new_name = (text + r.new_name) if position == "prefix" else (r.new_name + text)


def extract_segment(rows, delimiter, direction, segment_number, target):
    """Splits the staged value on delimiter and keeps one piece,
    counted from the Left (1st, 2nd, ...) or from the Right (1st,
    2nd, ... counting backward from the end) - the UI-friendly
    equivalent of hand-typing {..|SPLIT:delim:n} / {..|SPLIT:delim:-n}."""
    if not delimiter:
        return
    try:
        n = int(segment_number)
    except Exception:
        return
    if n <= 0:
        return
    signed_index = -n if direction == "right" else n
    for r in rows:
        if not r.selected:
            continue
        if target in ("number", "both"):
            r.new_number = _split_pick(r.new_number, delimiter, signed_index)
        if target in ("name", "both"):
            r.new_name = _split_pick(r.new_name, delimiter, signed_index)


def convert_case(rows, mode, target):
    for r in rows:
        if not r.selected:
            continue
        if target in ("number", "both"):
            r.new_number = _case_one(r.new_number, mode)
        if target in ("name", "both"):
            r.new_name = _case_one(r.new_name, mode)


def _case_one(value, mode):
    if mode == "upper":
        return value.upper()
    if mode == "lower":
        return value.lower()
    if mode == "title":
        return value.title()
    return value


# --------------------------------------------------------------------------
# Status / duplicate detection
# --------------------------------------------------------------------------
def compute_statuses(rows):
    """Every row (selected or not) contributes the Sheet Number it will
    hold after Apply - its new_number if selected, otherwise its
    unchanged original_number - into one pool. Any number held by more
    than one row is a real post-Apply collision, whether that's two
    proposed renames landing on the same value, a proposed rename
    landing on a number some untouched sheet already owns, or (the
    inverse) is naturally NOT flagged for a clean swap like
    A101<->A102, since after Apply each number still has exactly one
    owner - Apply itself stages through a temporary unique number per
    sheet first so Revit never sees the transient collision either.

    A row whose New Number or New Name contains a character Revit
    rejects outright ("\\:{}[]|;<>?`~") is marked Invalid rather than
    Ready - this is caught here, before Apply, specifically so a bad
    rule shows up red in Preview instead of failing partway through a
    real Transaction."""
    final_number_to_rows = {}
    for r in rows:
        held_number = r.new_number if r.selected else r.original_number
        final_number_to_rows.setdefault(held_number, []).append(r)

    for r in rows:
        if not r.selected:
            r.status = ""
            continue
        if not (r.new_number or "").strip() or not (r.new_name or "").strip():
            r.status = STATUS_EMPTY
            continue
        if _has_invalid_chars(r.new_number) or _has_invalid_chars(r.new_name):
            r.status = STATUS_INVALID
            continue
        if r.new_number == r.original_number and r.new_name == r.original_name:
            r.status = STATUS_UNCHANGED
            continue
        if len(final_number_to_rows.get(r.new_number, [])) > 1:
            r.status = STATUS_DUPLICATE
            continue
        r.status = STATUS_READY


# --------------------------------------------------------------------------
# Apply
# --------------------------------------------------------------------------
class RenameResult(object):
    def __init__(self, results):
        self.results = results
        self.ok_count = sum(1 for ok, _label, _detail in results if ok)


def apply_renames(doc, rows):
    """Renames every selected, Ready, actually-changed row in one
    Transaction. Sheet Number changes go through a temporary unique
    placeholder first (ZZDeeSheetTMP<ElementId> - letters and digits
    only, since Revit rejects "\\:{}[]|;<>?`~" in Sheet Number/Name and
    an earlier version of this placeholder used "~", which broke Apply
    outright) so a circular batch like A101->A102, A102->A101 never
    hits Revit's "duplicate Sheet Number" error no matter what order
    the loop processes rows in - every temp value is unique by
    construction (it's the element id), so phase 1 can never collide
    with itself or with any final value."""
    to_apply = [r for r in rows if r.selected and r.status == STATUS_READY]
    results = []
    if not to_apply:
        return RenameResult(results)

    t = Transaction(doc, "DeeSheet - Sheet Renamer")
    t.Start()
    try:
        failed = set()
        for r in to_apply:
            if r.new_number == r.original_number:
                continue
            temp_number = "ZZDeeSheetTMP{0}".format(r.sheet.Id.IntegerValue)
            try:
                r.sheet.SheetNumber = temp_number
            except Exception as e:
                failed.add(r)
                results.append((False, r.original_number, "FAILED (temp stage): {0}".format(e)))

        for r in to_apply:
            if r in failed:
                continue
            try:
                old_number, old_name = r.original_number, r.original_name
                if r.new_number != r.original_number:
                    r.sheet.SheetNumber = r.new_number
                if r.new_name != r.original_name:
                    r.sheet.Name = r.new_name
                results.append((True, r.new_number, "'{0} / {1}' -> '{2} / {3}'".format(
                    old_number, old_name, r.new_number, r.new_name)))
                r.original_number = r.new_number
                r.original_name = r.new_name
            except Exception as e:
                results.append((False, r.original_number, "FAILED: {0}".format(e)))
        t.Commit()
    except Exception:
        t.RollBack()
        raise
    return RenameResult(results)


def print_report(result):
    html = ['<h2 style="font-family:sans-serif;color:#ddd;">DeeSheet - Sheet Renamer Results</h2>']
    for ok, label, detail in result.results:
        bg = "#2e7d32" if ok else "#c62828"
        icon = "&#10003;" if ok else "&#10007;"
        html.append(
            '<div style="padding:6px 12px;margin:3px 0;background:{0};color:#fff;'
            'border-radius:4px;font-family:monospace;font-size:12px;">'
            '{1}&nbsp; <b>{2}</b> &mdash; {3}</div>'.format(bg, icon, label, detail))
    html.append('<hr><b style="font-family:sans-serif;">{0} / {1} renamed successfully.</b>'.format(
        result.ok_count, len(result.results)))
    output.print_html("".join(html))


# --------------------------------------------------------------------------
# Presets (saved rule templates) - reuses lib/deew_settings.py's generic
# per-tool JSON store rather than a second, parallel persistence layer.
# --------------------------------------------------------------------------
def list_presets():
    data = deew_settings.load(_PRESET_TOOL_NAME, {})
    return sorted(data.keys())


def load_preset(name):
    data = deew_settings.load(_PRESET_TOOL_NAME, {})
    return data.get(name)


def save_preset(name, preset_dict):
    data = deew_settings.load(_PRESET_TOOL_NAME, {})
    data[name] = preset_dict
    return deew_settings.save(_PRESET_TOOL_NAME, data)


def delete_preset(name):
    data = deew_settings.load(_PRESET_TOOL_NAME, {})
    if name in data:
        del data[name]
        return deew_settings.save(_PRESET_TOOL_NAME, data)
    return True
