# -*- coding: utf-8 -*-
"""Catch a check_access("X") call whose X was never added to
dee_telemetry.TOOL_CATEGORIES.

Why this exists
----------------
TOOL_CATEGORIES drives the per-category access gate (see
lib/dee_telemetry.py). A tool_name missing from it is NOT blocked for
users - it just skips category gating entirely (see check_access()'s own
handling: failing a user closed because a developer forgot a dict entry
would be a nasty, invisible footgun, so a gap fails safe for them).

But "invisible to users" must not mean "invisible to the developer"
too - a forgotten entry silently means that tool can never be
individually restricted from the dashboard, which is easy to not notice
for months. This makes the gap loud instead.

    python tools/check_tool_categories.py

Exits non-zero if any check_access("X") call site's X is not a key in
TOOL_CATEGORIES. Present with value None (explicitly excluded, e.g.
About.panel's meta tools) is NOT an error - only a genuinely missing key
is.
"""
import ast
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TELEMETRY = os.path.join(ROOT, "lib", "dee_telemetry.py")
TAB = os.path.join(ROOT, "DeePack.tab")


def load_tool_categories():
    """The TOOL_CATEGORIES dict, read WITHOUT importing dee_telemetry.py.

    That module does an unconditional `import clr` (IronPython/.NET only -
    verified: plain CPython here raises ModuleNotFoundError on it), unlike
    lib/dee_update_source.py's own deliberate urllib fallback. Importing
    it directly would crash this checker outside of pyRevit. Parsing the
    literal dict out of the source AST needs nothing but the standard
    library and never executes any of the file's IronPython-only code."""
    tree = ast.parse(io.open(TELEMETRY, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "TOOL_CATEGORIES"
                for t in node.targets):
            return ast.literal_eval(node.value)
    raise SystemExit("TOOL_CATEGORIES not found in " +
                     os.path.relpath(TELEMETRY, ROOT))


def python_files(root):
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        for name in files:
            if name in ("script.py", "config.py"):
                yield os.path.join(dirpath, name)


def find_check_access_calls(tree):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_check_access = (
            (isinstance(func, ast.Name) and func.id == "check_access") or
            (isinstance(func, ast.Attribute) and func.attr == "check_access"))
        if not is_check_access or not node.args:
            continue
        arg = node.args[0]
        # py3 ast.Constant / legacy ast.Str, either way
        value = getattr(arg, "value", None) if isinstance(arg, ast.Constant) \
            else getattr(arg, "s", None)
        if isinstance(value, str):
            yield node.lineno, value


def main():
    tool_categories = load_tool_categories()
    missing = []
    checked = 0
    for path in python_files(TAB):
        rel = os.path.relpath(path, ROOT)
        try:
            tree = ast.parse(io.open(path, encoding="utf-8").read())
        except Exception:
            continue
        for lineno, tool_name in find_check_access_calls(tree):
            checked += 1
            if tool_name not in tool_categories:
                missing.append((rel, lineno, tool_name))

    print("check_access() call sites checked : {0}".format(checked))
    if missing:
        print("NOT IN TOOL_CATEGORIES ({0}):".format(len(missing)))
        for rel, lineno, tool_name in missing:
            print('  {0}:{1}\n      "{2}" - add it to TOOL_CATEGORIES in '
                  'lib/dee_telemetry.py (or explicitly map it to None if '
                  'it should never be category-gated)'
                  .format(rel, lineno, tool_name))
        return 1
    print("NOT IN TOOL_CATEGORIES             : none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
