# -*- coding: utf-8 -*-
"""
dee_rename_pipeline
The 8-method Advanced-Renamer-style rename pipeline (RegEx, Name,
Replace, Case, Remove, Add, Auto Date, Numbering) - extracted from
DeeLazy's own DeeBulkRename module (its first home) so every tool with
a naming/renaming feature can share the exact same, already-tested
engine instead of each carrying its own copy.

Each method has its own "enabled" flag in its config dict and runs in
FIXED order 1-8 on the result of the one before it - see apply_methods()
for the exact order and DEFAULT_METHODS() for a ready-to-use, all-
neutral starting config (every method disabled except "Name" set to
"keep", which is a no-op).

Deliberately plain Python string manipulation - no Revit API call
anywhere in this module, so it is safe to import and use from ANY
pushbutton/module regardless of what it renames (Views, Sheets, Levels,
Grids, families, files on disk, ...). The caller decides what "old_name"
means and what to do with the returned new name.

Per-method try/except inside apply_methods(): a single method's own bad
input (an invalid regex, a From/To range outside the name's length)
degrades that ONE step rather than raising - matching this codebase's
established fail-safe convention. Two different callers wanting DIFFERENT
tokens seeded into the name (e.g. DeeAssemb's {assembly}/{type}/{index})
should resolve those tokens into `old_name` themselves BEFORE calling
apply_methods() - this module only ever sees plain strings, never a
template language of its own.
"""
import re
import string
import datetime

_SYMBOL_CHARS = set(c for c in string.punctuation)


def DEFAULT_METHODS():
    """A fresh, independent (never shared/mutated) all-neutral config -
    every method disabled, so apply_methods(name, idx, DEFAULT_METHODS())
    returns `name` unchanged. Callers should copy this and flip on/fill
    in whichever methods they want enabled by default."""
    return {
        "regex": {"enabled": False, "match": u"", "replace": u"", "case_sensitive": False},
        "name": {"enabled": False, "mode": "keep", "fixed_text": u""},
        "replace": {"enabled": False, "find": u"", "with": u"", "match_case": False, "first_only": False},
        "case": {"enabled": False, "mode": "same", "exceptions": u""},
        "remove": {
            "enabled": False, "first_n": 0, "last_n": 0, "from_pos": 0, "to_pos": 0,
            "crop_before": u"", "crop_after": u"",
            "remove_digits": False, "remove_symbols": False, "trim": False,
        },
        "add": {"enabled": False, "prefix": u"", "suffix": u"", "insert_text": u"", "insert_pos": 0},
        "auto_date": {"enabled": False, "position": "suffix", "format": "dmy", "separator": u"-"},
        "numbering": {
            "enabled": False, "position": "suffix", "start": 1, "increment": 1,
            "pad": 0, "separator": u"-",
        },
    }


def _regex_method(name, cfg):
    if not cfg["match"]:
        return name
    try:
        flags = 0 if cfg["case_sensitive"] else re.IGNORECASE
        return re.sub(cfg["match"], cfg["replace"], name, flags=flags)
    except Exception:
        return name


def _name_method(name, cfg):
    mode = cfg["mode"]
    if mode == "remove":
        return u""
    if mode == "fixed":
        return cfg["fixed_text"]
    return name


def _replace_method(name, cfg):
    find = cfg["find"]
    if not find:
        return name
    withtext = cfg["with"]
    if cfg["match_case"]:
        if cfg["first_only"]:
            return name.replace(find, withtext, 1)
        return name.replace(find, withtext)
    # Case-insensitive replace, preserving the rest of the string exactly.
    pattern = re.escape(find)
    count = 1 if cfg["first_only"] else 0
    try:
        return re.sub(pattern, lambda m: withtext, name, count=count, flags=re.IGNORECASE)
    except Exception:
        return name


def _apply_case(name, mode):
    if mode == "upper":
        return name.upper()
    if mode == "lower":
        return name.lower()
    if mode == "title":
        return name.title()
    if mode == "sentence":
        stripped = name.strip()
        if not stripped:
            return name
        lowered = name.lower()
        idx = len(name) - len(name.lstrip())
        return name[:idx] + lowered[idx:idx + 1].upper() + lowered[idx + 1:]
    return name


def _case_method(name, cfg):
    result = _apply_case(name, cfg["mode"])
    exceptions = [e.strip() for e in (cfg["exceptions"] or u"").split(",") if e.strip()]
    for word in exceptions:
        try:
            result = re.sub(re.escape(word), word, result, flags=re.IGNORECASE)
        except Exception:
            continue
    return result


def _remove_method(name, cfg):
    result = name
    first_n = cfg["first_n"]
    if first_n > 0:
        result = result[first_n:]
    last_n = cfg["last_n"]
    if last_n > 0:
        result = result[:-last_n] if last_n < len(result) else u""
    frm, to = cfg["from_pos"], cfg["to_pos"]
    if frm > 0 and to >= frm:
        result = result[:frm - 1] + result[to:]
    crop_before = cfg["crop_before"]
    if crop_before:
        idx = result.find(crop_before)
        if idx != -1:
            result = result[idx + len(crop_before):]
    crop_after = cfg["crop_after"]
    if crop_after:
        idx = result.find(crop_after)
        if idx != -1:
            result = result[:idx]
    if cfg["remove_digits"]:
        result = u"".join(c for c in result if not c.isdigit())
    if cfg["remove_symbols"]:
        result = u"".join(c for c in result if c not in _SYMBOL_CHARS)
    if cfg["trim"]:
        result = result.strip()
    return result


def _add_method(name, cfg):
    result = name
    insert_text = cfg["insert_text"]
    if insert_text:
        pos = max(0, min(cfg["insert_pos"], len(result)))
        result = result[:pos] + insert_text + result[pos:]
    return u"{0}{1}{2}".format(cfg["prefix"], result, cfg["suffix"])


def _format_date(fmt):
    today = datetime.date.today()
    d, m, y = u"{0:02d}".format(today.day), u"{0:02d}".format(today.month), u"{0:04d}".format(today.year)
    if fmt == "mdy":
        return u"{0}{1}{2}".format(m, d, y)
    if fmt == "ymd":
        return u"{0}{1}{2}".format(y, m, d)
    return u"{0}{1}{2}".format(d, m, y)


def _date_method(name, cfg):
    date_str = _format_date(cfg["format"])
    sep = cfg["separator"] or u""
    if cfg["position"] == "prefix":
        return u"{0}{1}{2}".format(date_str, sep, name)
    return u"{0}{1}{2}".format(name, sep, date_str)


def _numbering_method(name, idx, cfg):
    value = cfg["start"] + idx * cfg["increment"]
    pad = cfg["pad"]
    num_str = unicode(value).zfill(pad) if pad > 0 else unicode(value)
    sep = cfg["separator"] or u""
    if cfg["position"] == "prefix":
        return u"{0}{1}{2}".format(num_str, sep, name)
    return u"{0}{1}{2}".format(name, sep, num_str)


def apply_methods(old_name, idx, methods):
    """Runs every ENABLED method in fixed order (1-8) on `old_name`,
    returning the final new name. `idx` is the caller's own 0-based
    position for whatever sequence Numbering should count over (e.g.
    "this row's position among the checked/ticked items") - it means
    nothing to any OTHER method. Wrapped per-method in try/except so one
    bad method's config (e.g. Remove's From/To outside the name's
    length) degrades that one step rather than aborting the whole
    result."""
    name = old_name
    steps = (
        ("regex", _regex_method),
        ("name", _name_method),
        ("replace", _replace_method),
        ("case", _case_method),
        ("remove", _remove_method),
        ("add", _add_method),
        ("auto_date", _date_method),
    )
    for key, fn in steps:
        cfg = methods.get(key)
        if not cfg or not cfg.get("enabled"):
            continue
        try:
            name = fn(name, cfg)
        except Exception:
            continue
    numbering = methods.get("numbering")
    if numbering and numbering.get("enabled"):
        try:
            name = _numbering_method(name, idx, numbering)
        except Exception:
            pass
    return name
