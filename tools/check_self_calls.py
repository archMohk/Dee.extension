# -*- coding: utf-8 -*-
"""Catch self.method(...) calls that do not match the method's def.

Why this exists
---------------
Twice now a call and its definition drifted apart and nothing noticed
until Revit did:

  * self._guard(...) was called in DeeMAPLink, which had no _guard at all
    - copied from a sibling tool that did.
  * self._map_build(keep_positions=False) survived a rewrite that dropped
    the parameter. It raised TypeError inside a try/except, so the Wire
    Map simply came up empty with no error shown anywhere.

ast.parse cannot see either: both are syntactically perfect. Python only
finds out at the moment of the call, which in a WPF tool means after a
user has clicked something.

So this compares every self.X(...) call against the def of X in the same
class: does the method exist, are there too many positional arguments,
and is every keyword one the def actually accepts.

Deliberately conservative. Anything it cannot resolve - a method from a
base class, *args or **kwargs in the def, a name assigned dynamically -
is left alone rather than guessed at. A checker that reports things that
are fine gets ignored, and an ignored checker is worse than none.

    python tools/check_self_calls.py

Exits non-zero if a call cannot possibly work.
"""
import ast
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def python_files(root):
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        for name in files:
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


def method_table(cls):
    """name -> (positional names, has *args, has **kwargs, defaults count)"""
    table = {}
    for node in cls.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        a = node.args
        # A @staticmethod called through self gets NO implicit first
        # argument, so counting one costs two false positives per call.
        # Both of the checker's first findings were exactly this.
        static = any(isinstance(d, ast.Name) and d.id == "staticmethod"
                     for d in node.decorator_list)
        positional = [p.arg for p in a.args]
        kwonly = [p.arg for p in a.kwonlyargs]
        table[node.name] = {
            "positional": positional,
            "kwonly": kwonly,
            "defaults": len(a.defaults),
            "star": a.vararg is not None,
            "starstar": a.kwarg is not None,
            "implicit": 0 if static else 1,
            "line": node.lineno,
        }
    return table


def check_class(rel, cls, problems):
    table = method_table(cls)
    # A class with a base we cannot see may inherit anything; only flag
    # missing methods when the class stands alone.
    standalone = not cls.bases or all(
        isinstance(b, ast.Name) and b.id == "object" for b in cls.bases)

    for node in ast.walk(cls):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "self"):
            continue
        name = func.attr
        info = table.get(name)
        if info is None:
            if standalone and not any(
                    isinstance(n, ast.Attribute) and n.attr == name
                    for n in ast.walk(cls) if isinstance(n, ast.Attribute)
                    and isinstance(getattr(n, "ctx", None), ast.Store)):
                problems.append((rel, node.lineno,
                                 "self.{0}(...) - no such method in {1}".format(
                                     name, cls.name)))
            continue

        if info["star"] or info["starstar"]:
            continue                      # takes anything; nothing to check

        implicit = info["implicit"]
        given = len(node.args) + implicit
        most = len(info["positional"])
        least = most - info["defaults"]
        if given > most:
            problems.append((rel, node.lineno,
                             "self.{0}() given {1} positional arg(s), def takes "
                             "at most {2} (line {3})".format(
                                 name, given - implicit, most - implicit,
                                 info["line"])))
        accepted = set(info["positional"][implicit:]) | set(info["kwonly"])
        for kw in node.keywords:
            if kw.arg is None:
                continue                  # **something; unknowable
            if kw.arg not in accepted:
                problems.append((rel, node.lineno,
                                 "self.{0}({1}=...) - def has no such parameter "
                                 "(line {2})".format(name, kw.arg, info["line"])))
        supplied = given - implicit + len([k for k in node.keywords if k.arg])
        if not node.keywords and supplied < least - implicit:
            problems.append((rel, node.lineno,
                             "self.{0}() given {1} arg(s), def needs at least "
                             "{2} (line {3})".format(
                                 name, supplied, least - implicit, info["line"])))


def main():
    problems = []
    classes = 0
    for path in python_files(ROOT):
        rel = os.path.relpath(path, ROOT)
        try:
            tree = ast.parse(io.open(path, encoding="utf-8").read())
        except Exception:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                classes += 1
                check_class(rel, node, problems)

    print("classes checked : {0}".format(classes))
    if problems:
        print("MISMATCHED CALLS ({0}):".format(len(problems)))
        for rel, line, message in problems:
            print("  {0}:{1}\n      {2}".format(rel, line, message))
        return 1
    print("MISMATCHED CALLS: none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
