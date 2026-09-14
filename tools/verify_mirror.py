# -*- coding: utf-8 -*-
"""Check what the mirror actually holds, not what the uploader believed.

    python tools/verify_mirror.py            # sample check
    python tools/verify_mirror.py --all      # verify every file

Every request here is public and unauthenticated - no key is used or
needed. That is the point: the publisher can only report what it thinks
it sent, which is precisely the thing worth not trusting.

It reads the ORIGIN rather than whatever an edge has cached (see the
cache-busting note below), so it answers "did the publish land", not
"what would a user get this second". Those differ for a short while
after every publish, and conflating them once reported a perfectly good
mirror as broken.

What it verifies:

  * the manifest is reachable and parses;
  * its commit matches this checkout's HEAD - so a publish that half
    failed, or never ran, cannot pass silently;
  * sampled files download and their sha256 matches the manifest;
  * the installer zip downloads, matches its recorded sha256, has intact
    CRCs, and has every entry under Dee.extension/ - if that prefix is
    wrong the installer extracts to the wrong layout and pyRevit finds
    nothing.

Exits non-zero on any failure, so CI fails the run rather than reporting
a green publish over a broken mirror.
"""
import argparse
import hashlib
import io
import json
import os
import random
import subprocess
import sys
import time
import uuid
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "lib"))

import dee_update_source as upd  # noqa: E402

SAMPLE = 12
# Supabase serves public objects through a CDN. Run straight after a
# publish, this read the PREVIOUS version and reported the mirror as
# broken when it was fine - a cached answer, not a wrong upload. Every
# request here therefore carries a unique query string, which the CDN
# treats as a different object and so fetches from origin.
ATTEMPTS = 5
WAIT = 8


def head_commit():
    proc = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                          cwd=ROOT, capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def fresh(url, token):
    """Same object, a cache key nothing has seen before."""
    return url + ("&" if "?" in url else "?") + "cb=" + token


def get(url, bucket, rel, token):
    return upd._get_bytes(fresh(upd.public_url(url, bucket, rel), token))


def fetch_manifest_fresh(url, bucket, expect_commit):
    """The manifest as the ORIGIN has it, not as an edge remembers it.

    Retries while the commit is behind: a publish and this check can run
    within the same second, and propagation is not instant even past the
    cache. Returns (manifest_or_None, detail)."""
    last = "no attempt made"
    for attempt in range(1, ATTEMPTS + 1):
        data, detail = get(url, bucket, upd.MANIFEST_NAME, uuid.uuid4().hex)
        if data is None:
            last = detail
        else:
            try:
                manifest = json.loads(data.decode("utf-8"))
            except ValueError as e:
                last = "manifest is not valid JSON: {0}".format(e)
                manifest = None
            if manifest is not None:
                if not expect_commit or manifest.get("commit") == expect_commit:
                    return manifest, "ok"
                last = "mirror still at {0}, waiting for {1}".format(
                    manifest.get("commit"), expect_commit)
        if attempt < ATTEMPTS:
            print("  ({0}) {1} - retrying in {2}s".format(attempt, last, WAIT))
            time.sleep(WAIT)
    return None, last


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true",
                        help="verify every file, not a sample")
    args = parser.parse_args()

    url = os.environ.get("SUPABASE_URL") or upd.DEFAULT_SUPABASE_URL
    bucket = os.environ.get("SUPABASE_BUCKET") or upd.DEFAULT_BUCKET
    print("project : {0}".format(url))
    print("bucket  : {0}".format(bucket))

    head = head_commit()
    manifest, detail = fetch_manifest_fresh(url, bucket, head)
    if not manifest:
        print("\nFAIL - could not read the mirror:\n{0}".format(detail))
        return 1

    token = uuid.uuid4().hex
    files = manifest.get("files") or {}
    print("commit  : {0}".format(manifest.get("commit")))
    print("built   : {0}".format(manifest.get("built")))
    print("files   : {0}".format(len(files)))

    failures = []

    if head and head != manifest.get("commit"):
        failures.append(
            "mirror is at {0} but HEAD is {1} - the publish did not "
            "complete, or did not run".format(manifest.get("commit"), head))
    elif head:
        print("HEAD    : {0} - matches".format(head))

    if not files:
        failures.append("the manifest lists no files")

    # --- sampled (or full) file checks ---
    names = sorted(files)
    if not args.all and len(names) > SAMPLE:
        random.seed(manifest.get("commit") or "")
        names = random.sample(names, SAMPLE)
    print("\nchecking {0} file(s):".format(len(names)))
    for rel in names:
        data, detail = get(url, bucket, rel, token)
        if data is None:
            failures.append("{0}: download failed ({1})".format(rel, detail))
            print("  FAIL  {0}".format(rel))
            continue
        if hashlib.sha256(data).hexdigest() != files[rel]["sha256"]:
            failures.append("{0}: sha256 does not match the manifest".format(rel))
            print("  FAIL  {0}".format(rel))
            continue
        print("  ok    {0}".format(rel))

    # --- the installer's zip ---
    record = manifest.get("zip") or {}
    if not record.get("name"):
        failures.append("the manifest records no installer zip")
    else:
        print("\nzip: {0}".format(record["name"]))
        data, detail = get(url, bucket, record["name"], token)
        if data is None:
            failures.append("zip download failed ({0})".format(detail))
        elif hashlib.sha256(data).hexdigest() != record.get("sha256"):
            failures.append("zip sha256 does not match the manifest")
        else:
            print("  sha256 ok, {0:.2f} MB".format(len(data) / 1048576.0))
            try:
                archive = zipfile.ZipFile(io.BytesIO(data))
                broken = archive.testzip()
                if broken:
                    failures.append("zip has a bad CRC at {0}".format(broken))
                entries = archive.namelist()
                stray = [n for n in entries
                         if not n.startswith("Dee.extension/")][:3]
                if stray:
                    failures.append(
                        "zip entries are not all under Dee.extension/ "
                        "(e.g. {0}) - it would extract to the wrong layout"
                        .format(", ".join(stray)))
                else:
                    print("  {0} entries, all under Dee.extension/".format(
                        len(entries)))
            except zipfile.BadZipFile as e:
                failures.append("zip is not readable: {0}".format(e))

    print()
    if failures:
        print("FAILED - {0} problem(s):".format(len(failures)))
        for line in failures:
            print("  * {0}".format(line))
        return 1
    print("Mirror verified: what is being served matches what the manifest "
          "claims.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
