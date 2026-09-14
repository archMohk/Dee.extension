# -*- coding: utf-8 -*-
"""Copy the Supabase secret key already on this PC into the publish config.

    python tools/link_publish_key.py

The telemetry dashboard (dashboard/server.py, gitignored) already holds
a SECRET key for this same Supabase project. The publisher needs the
same key, in tools/.supabase_publish.json. Rather than have anyone
re-fetch it from the dashboard, reveal it on screen and paste it around
- every one of which is a chance for it to end up somewhere it should
not - this moves it directly between two gitignored files on this
machine.

The key is never printed, never logged, and never leaves this PC. The
script reports only its length and the last four characters, which is
enough to confirm the right one landed without disclosing it.

Both files are gitignored, so neither can be committed by accident.
Refuses to run if either one is not - that check is the point, not a
formality.
"""
import io
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SOURCE = os.path.join(ROOT, "dashboard", "server.py")
CONFIG = os.path.join(HERE, ".supabase_publish.json")

# Supabase secret keys: the current sb_secret_ form, and the older
# service_role JWT.
PATTERNS = (re.compile(r"sb_secret_[A-Za-z0-9_\-]{10,}"),
            re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"
                       r"\.[A-Za-z0-9_\-]{10,}"))
PROJECT = re.compile(r"https://([a-z0-9]+)\.supabase\.co")


def must_be_gitignored(path):
    rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
    result = subprocess.run(["git", "check-ignore", "-q", rel],
                            cwd=ROOT, capture_output=True)
    if result.returncode != 0:
        raise SystemExit(
            "REFUSING: {0} is not gitignored.\n\n"
            "A secret key must never be in a file git can see. Add it to "
            ".gitignore first.".format(rel))


def main():
    if not os.path.exists(SOURCE):
        raise SystemExit(
            "No {0} - nothing to copy from. Put the key into {1} by hand."
            .format(os.path.relpath(SOURCE, ROOT),
                    os.path.relpath(CONFIG, ROOT)))
    if not os.path.exists(CONFIG):
        raise SystemExit(
            "No {0}. Create it first (url, bucket, service_key)."
            .format(os.path.relpath(CONFIG, ROOT)))

    must_be_gitignored(SOURCE)
    must_be_gitignored(CONFIG)

    text = io.open(SOURCE, encoding="utf-8", errors="replace").read()
    key = None
    for pattern in PATTERNS:
        match = pattern.search(text)
        if match:
            key = match.group(0)
            break
    if not key:
        raise SystemExit(
            "No Supabase secret key found in {0}."
            .format(os.path.relpath(SOURCE, ROOT)))

    with io.open(CONFIG, encoding="utf-8") as handle:
        cfg = json.load(handle)

    # Both files must point at the SAME project, or the key administers
    # something other than the mirror.
    source_projects = set(PROJECT.findall(text))
    target_project = set(PROJECT.findall(cfg.get("url", "")))
    if source_projects and target_project and not (source_projects & target_project):
        raise SystemExit(
            "REFUSING: {0} is for project {1}, but the publish config "
            "targets {2}. That key would administer the wrong project."
            .format(os.path.relpath(SOURCE, ROOT),
                    "/".join(sorted(source_projects)),
                    "/".join(sorted(target_project))))

    if cfg.get("service_key") == key:
        print("Already linked - the publish config has this key.")
    else:
        cfg["service_key"] = key
        tmp = CONFIG + ".tmp"
        with io.open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(cfg, indent=2))
        os.replace(tmp, CONFIG)
        print("Key copied into {0}.".format(os.path.relpath(CONFIG, ROOT)))

    print("  project : {0}".format(cfg.get("url")))
    print("  bucket  : {0}".format(cfg.get("bucket")))
    print("  key     : present, {0} characters".format(len(key)))
    print("\nNeither file is visible to git. Next: "
          "python tools/setup_mirror_bucket.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
