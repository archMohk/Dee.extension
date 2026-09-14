# -*- coding: utf-8 -*-
"""Catch the installer bugs that only a compiler would otherwise catch.

Inno Setup is not installed on the development PC, so installer/*.iss is
the one file in this repo that gets written and committed without ever
being compiled. A Pascal Script type error is not a runtime surprise
there - it stops the installer building at all, and nobody finds out
until someone tries to build a release.

This is not a Pascal parser and does not pretend to be. It checks the
two things that have actually gone wrong:

  1. Exec/ShellExec's last argument is `var ErrorCode: Integer`. Passing
     a Boolean - PrepareToInstall's own NeedsRestart is the tempting
     one, it is right there in scope - will not compile.

  2. begin/end balance per routine, which is what a sliced-out block
     leaves behind and what the eye is worst at.

    python tools/check_iss.py
"""
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# `function Name(params): Type;` / `procedure Name(params);`
ROUTINE = re.compile(r"^\s*(function|procedure)\s+(\w+)", re.IGNORECASE)
# The out-parameter is the last argument of either call.
OUT_PARAM = re.compile(r"\b(Exec|ShellExec|ExecAsOriginalUser)\s*\(", re.IGNORECASE)
# `Name, Other: Integer;` in a var block.
DECL = re.compile(r"^\s*([\w\s,]+?)\s*:\s*(\w+)\s*;")


def strip_comments(text):
    """Braces and (* *) are comments; ' ' is a string. Good enough here."""
    out = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "{":
            depth = 1
            i += 1
            while i < n and depth:
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                if text[i] == "\n":
                    out.append("\n")          # keep line numbers honest
                i += 1
            continue
        if ch == "'":
            out.append("'")
            i += 1
            while i < n and text[i] != "'":
                out.append(" " if text[i] != "\n" else "\n")
                i += 1
            i += 1
            out.append("'")
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def code_section(text):
    """Only [Code] is Pascal; the rest is ini-style and must be skipped."""
    lower = text.lower()
    start = lower.find("\n[code]")
    if start < 0:
        return "", 0
    start = text.index("\n", start + 1) + 1
    offset = text[:start].count("\n")
    rest = re.search(r"^\[\w+\]", text[start:], re.MULTILINE)
    return (text[start:start + rest.start()] if rest else text[start:]), offset


def split_args(inner):
    """Top-level comma split - nested calls and strings must not fool it."""
    args, depth, current = [], 0, []
    in_string = False
    for ch in inner:
        if ch == "'":
            in_string = not in_string
        if not in_string:
            if ch in "([":
                depth += 1
            elif ch in ")]":
                if depth == 0:
                    break
                depth -= 1
            elif ch == "," and depth == 0:
                args.append("".join(current).strip())
                current = []
                continue
        current.append(ch)
    args.append("".join(current).strip())
    return args


def balanced_inner(text, open_at):
    depth, i, in_string = 0, open_at, False
    while i < len(text):
        ch = text[i]
        if ch == "'":
            in_string = not in_string
        elif not in_string:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return text[open_at + 1:i]
        i += 1
    return ""


def routines(code):
    """[(name, start_line, body_text)] - one entry per routine."""
    lines = code.splitlines()
    marks = [(i, ROUTINE.match(line)) for i, line in enumerate(lines)]
    marks = [(i, m.group(2)) for i, m in marks if m]
    found = []
    for index, (line_no, name) in enumerate(marks):
        end = marks[index + 1][0] if index + 1 < len(marks) else len(lines)
        found.append((name, line_no, "\n".join(lines[line_no:end])))
    return found


def declared_types(body):
    """Identifier -> declared type, from the routine's var block AND its
    own parameter list. Both are places an Integer can come from."""
    types = {}
    head = body.split("begin", 1)[0]
    for raw in head.splitlines():
        match = DECL.match(raw)
        if not match:
            continue
        for name in match.group(1).split(","):
            name = name.strip().replace("var ", "").replace("const ", "")
            if name and " " not in name:
                types[name.lower()] = match.group(2).lower()
    # parameters, e.g. (var NeedsRestart: Boolean)
    paren = body.find("(")
    if 0 <= paren < body.find("begin"):
        for part in balanced_inner(body, paren).split(";"):
            if ":" in part:
                names, kind = part.rsplit(":", 1)
                for name in names.split(","):
                    name = re.sub(r"\b(var|const|out)\b", "", name).strip()
                    if name:
                        types[name.lower()] = kind.strip().lower()
    return types


def check(path):
    text = io.open(path, encoding="utf-8", errors="replace").read()
    code, offset = code_section(text)
    if not code:
        return []
    code = strip_comments(code)
    problems = []

    for name, line_no, body in routines(code):
        types = declared_types(body)
        lower = body.lower()
        opens = len(re.findall(r"\bbegin\b", lower))
        closes = len(re.findall(r"\bend\b", lower))
        if opens != closes:
            problems.append((offset + line_no + 1, name,
                             "{0} begin vs {1} end".format(opens, closes)))

        for match in OUT_PARAM.finditer(body):
            inner = balanced_inner(body, body.index("(", match.end() - 1))
            args = split_args(inner)
            if len(args) < 2:
                continue
            last = args[-1].strip()
            if not re.match(r"^\w+$", last):
                continue
            kind = types.get(last.lower())
            if kind and kind != "integer":
                problems.append((
                    offset + line_no + body[:match.start()].count("\n") + 1,
                    name,
                    "{0}'s out-parameter is {1} '{2}', must be Integer"
                    .format(match.group(1), kind, last)))
    return problems


def main():
    checked, bad = 0, 0
    for folder, _dirs, files in os.walk(ROOT):
        for name in files:
            if not name.lower().endswith(".iss"):
                continue
            path = os.path.join(folder, name)
            checked += 1
            for line_no, routine, detail in check(path):
                bad += 1
                print("{0}:{1}  {2}: {3}".format(
                    os.path.relpath(path, ROOT), line_no, routine, detail))
    print("iss files checked : {0}".format(checked))
    print("PROBLEMS          : {0}".format(bad or "none"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
