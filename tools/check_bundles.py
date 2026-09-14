# -*- coding: utf-8 -*-
"""Parse every bundle.yaml in the extension and report the broken ones.

Why this exists
---------------
A bundle.yaml that does not parse takes its button out of the ribbon, and
the only sign is a pyRevit error window at startup naming a line number.
That happened on 2026-09-14 to DeePubCheck: a tooltip was reworded to
"...file open: nothing is opened...", and YAML reads that colon-space as
the start of a nested mapping inside a plain scalar. The text looked
completely ordinary.

Prose tooltips are the risk, because the things that break YAML are
things prose contains naturally:

    colon-space      "Works with NO file open: nothing is opened"
    leading quote    a value that starts with " or '
    trailing colon   a word ending in ':' at the end of a line

Run this after editing any bundle.yaml:

    python tools/check_bundles.py

Exit code is non-zero if anything is broken, so it can gate a commit.
"""
import io
import os
import sys

try:
    import yaml
except ImportError:
    print("PyYAML is not installed - pip install pyyaml")
    sys.exit(2)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Not errors, just the shapes most likely to become one after an edit.
RISKY_KEYS = ("tooltip", "title", "description")


def find_bundles(root):
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        for name in files:
            if name.lower() in ("bundle.yaml", "bundle.yml"):
                yield os.path.join(dirpath, name)


def main():
    broken = []
    warned = []
    checked = 0

    for path in find_bundles(ROOT):
        checked += 1
        rel = os.path.relpath(path, ROOT)
        text = io.open(path, encoding="utf-8").read()
        try:
            data = yaml.safe_load(text)
        except Exception as e:
            broken.append((rel, str(e).replace("\n", " ")[:200]))
            continue

        # A parse can succeed and still have swallowed something: a plain
        # scalar containing ": " parses fine when the rest of the line
        # cannot be read as a mapping, and then the tooltip is silently
        # truncated at the colon rather than erroring.
        if isinstance(data, dict):
            for key in RISKY_KEYS:
                value = data.get(key)
                if isinstance(value, str) and ": " in value:
                    warned.append((rel, key))

    print("bundle files checked : {0}".format(checked))
    if broken:
        print("BROKEN ({0}):".format(len(broken)))
        for rel, err in broken:
            print("  {0}\n      {1}".format(rel, err))
    else:
        print("BROKEN               : none")

    if warned:
        print("colon-space inside a quoted value (parses, but fragile - "
              "prefer ' - '):")
        for rel, key in warned:
            print("  {0}  [{1}]".format(rel, key))

    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
