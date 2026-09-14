# -*- coding: utf-8 -*-
"""Create the mirror's Storage bucket, once.

    python tools/setup_mirror_bucket.py           # create it if missing
    python tools/setup_mirror_bucket.py --check   # report only

The mirror needs one public bucket to exist before anything can be
published into it. Making it by hand in the dashboard is three clicks,
but the one that matters is easy to miss: the bucket MUST be public.
The extension reads it with no credentials at all - that is the whole
security model, and a private bucket makes every update fail with a
404 that looks like a missing file rather than a permissions problem.

So this does it over the API instead, and verifies the public flag
rather than trusting that the right checkbox was ticked.

Idempotent: run it twice and the second run reports "already correct".
It only ever creates the ONE bucket named in the publish config, and
never deletes or empties anything.

Credentials come from tools/.supabase_publish.json, the same file
publish_supabase.py uses. The key is never printed.
"""
import argparse
import json
import os
import sys
import urllib.request
from urllib.error import HTTPError

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CONFIG = os.path.join(HERE, ".supabase_publish.json")


def load_config():
    if not os.path.exists(CONFIG):
        raise SystemExit(
            "No {0}.\n\nRun tools/link_publish_key.py first, or create the "
            "file by hand.".format(os.path.relpath(CONFIG, ROOT)))
    with open(CONFIG, "r", encoding="utf-8") as handle:
        cfg = json.load(handle)
    for field in ("url", "bucket", "service_key"):
        if not cfg.get(field):
            raise SystemExit(
                "{0} has no '{1}'.\n\nFor the key, run "
                "tools/link_publish_key.py.".format(
                    os.path.relpath(CONFIG, ROOT), field))
    return cfg


def api(cfg, method, path, payload=None):
    """(parsed_json_or_None, error_or_None). The key is never echoed."""
    url = "{0}/storage/v1/{1}".format(cfg["url"].rstrip("/"), path)
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("apikey", cfg["service_key"])
    request.add_header("Authorization", "Bearer " + cfg["service_key"])
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        raw = urllib.request.urlopen(request, timeout=60).read()
        return (json.loads(raw.decode("utf-8")) if raw else {}), None
    except HTTPError as e:
        detail = e.read()[:400].decode("utf-8", "replace")
        return None, "HTTP {0}: {1}".format(e.code, detail)
    except Exception as e:
        return None, str(e)


def find_bucket(cfg):
    buckets, err = api(cfg, "GET", "bucket")
    if err:
        return None, err
    for entry in buckets or []:
        if entry.get("name") == cfg["bucket"] or entry.get("id") == cfg["bucket"]:
            return entry, None
    return None, None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="report the bucket's state, change nothing")
    args = parser.parse_args()

    cfg = load_config()
    print("project : {0}".format(cfg["url"]))
    print("bucket  : {0}".format(cfg["bucket"]))

    existing, err = find_bucket(cfg)
    if err:
        raise SystemExit(
            "\nCould not list buckets: {0}\n\n"
            "If that says the key is invalid, the one in the publish config "
            "is not a SECRET key - an anon/publishable key cannot administer "
            "storage.".format(err))

    if existing is None:
        if args.check:
            print("\nstate   : DOES NOT EXIST - nothing can be published yet")
            return 1
        print("\ncreating it (public) ...")
        _data, err = api(cfg, "POST", "bucket",
                         {"id": cfg["bucket"], "name": cfg["bucket"],
                          "public": True})
        if err:
            raise SystemExit("Create failed: {0}".format(err))
        existing, err = find_bucket(cfg)
        if err or existing is None:
            raise SystemExit(
                "Created, but it is not in the bucket list afterwards. "
                "Check the dashboard before publishing.")

    if existing.get("public"):
        print("\nstate   : ready - the bucket exists and is public")
        return 0

    print("\nstate   : exists but is PRIVATE")
    if args.check:
        print("          the extension reads with no credentials, so every "
              "update would fail")
        return 1
    print("making it public ...")
    _data, err = api(cfg, "PUT", "bucket/" + cfg["bucket"], {"public": True})
    if err:
        raise SystemExit("Could not make it public: {0}".format(err))
    existing, _err = find_bucket(cfg)
    if not (existing or {}).get("public"):
        raise SystemExit(
            "It still reports private. Fix it in the dashboard "
            "(Storage > the bucket > Settings > Public) before publishing.")
    print("\nstate   : ready - the bucket exists and is public")
    return 0


if __name__ == "__main__":
    sys.exit(main())
