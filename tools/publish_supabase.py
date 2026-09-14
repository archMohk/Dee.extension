# -*- coding: utf-8 -*-
"""Mirror the extension to Supabase Storage, file by file.

    python tools/publish_supabase.py            # publish
    python tools/publish_supabase.py --check    # report drift, upload nothing
    python tools/publish_supabase.py --force    # publish anyway (see below)

WHAT IS ON THE MIRROR IS WHAT IS ON GITHUB
------------------------------------------
That is the promise, and it is enforced rather than intended. Before
uploading anything this refuses to run if:

  * a TRACKED file is modified - otherwise the mirror would carry code
    that exists on nobody's machine but yours. Untracked files do not
    block: `git ls-files` cannot see them, so they are never uploaded
    and cannot breach the guarantee. They are listed as a warning in
    case one was meant to be committed;
  * HEAD is not pushed - otherwise the mirror would be AHEAD of GitHub,
    and a Supabase user would be running something a GitHub user cannot
    get.

So the mirror can only ever hold the content of a commit that is already
public. --force skips those checks; it exists for a genuine emergency
and prints what it is overriding.

The file list comes from `git ls-files`, so the mirror carries exactly
the set GitHub serves - by construction, not by intention. .gitignore
therefore protects the mirror too: the ACC token cache, the per-project
caches and the user's own acc_config.json are untracked and so can never
be uploaded.

CREDENTIALS
-----------
Uploading needs a SECRET Supabase key, which must never ship inside the
extension. Put it in tools/.supabase_publish.json (gitignored):

    {"url": "https://<project>.supabase.co",
     "bucket": "dee-extension",
     "service_key": "<secret key>"}

Only this script reads it. The extension itself only ever does public,
unauthenticated GETs.

INCREMENTAL
-----------
The remote manifest is read first, so only files whose sha256 actually
changed are uploaded - a normal release moves a handful, not 490.
"""
import argparse
import hashlib
import io
import json
import os
import zipfile
import subprocess
import sys
import time
import urllib.request
from urllib.error import HTTPError

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CONFIG = os.path.join(HERE, ".supabase_publish.json")
MANIFEST_NAME = "manifest.json"
# One-shot artifact for the INSTALLER only. Updates use the manifest.
ZIP_NAME = "Dee.extension.zip"

# Tracked by git, but of no use to someone running the extension. Kept
# off the mirror so a user's download is the tool, not the workshop.
SKIP_PREFIXES = ("tools/", "installer/", "dashboard/", ".claude/", ".github/")


def run(args):
    proc = subprocess.run(args, cwd=ROOT, capture_output=True, text=True)
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def git_state():
    """(commit, dirty, unpushed, untracked) - what the guarantee rests on.

    `dirty` deliberately ignores UNTRACKED files. The mirror's file list
    comes from `git ls-files`, so an untracked file can never be
    uploaded and therefore can never put anything on the mirror that
    GitHub does not have. Only a modified tracked file can do that.

    Untracked paths come back separately so they can be reported: one
    of them might be a new file nobody ever added, missing from GitHub
    and the mirror alike. That is an oversight rather than a breach of
    parity, so it warns instead of blocking."""
    _rc, commit, _e = run(["git", "rev-parse", "--short", "HEAD"])
    _rc, tracked, _e = run(["git", "status", "--porcelain",
                            "--untracked-files=no"])
    _rc, everything, _e = run(["git", "status", "--porcelain"])
    rc, ahead, _e = run(["git", "rev-list", "--count", "@{u}..HEAD"])
    unpushed = int(ahead) if rc == 0 and ahead.isdigit() else 0
    untracked = [line[3:].strip() for line in everything.splitlines()
                 if line.startswith("??")]
    return commit, bool(tracked.strip()), unpushed, untracked


def tracked_files():
    rc, out, err = run(["git", "ls-files"])
    if rc != 0:
        raise SystemExit("git ls-files failed: {0}".format(err))
    files = []
    for line in out.splitlines():
        rel = line.strip().replace("\\", "/")
        if not rel or rel.startswith(SKIP_PREFIXES):
            continue
        if not os.path.exists(os.path.join(ROOT, rel.replace("/", os.sep))):
            continue          # tracked but deleted in the working tree
        files.append(rel)
    return sorted(files)


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def load_config():
    """Credentials from the ENVIRONMENT first, then the local file.

    A CI runner has no config file and must never have one: the key
    reaches it as a repository secret in the environment. A developer's
    PC is the other way round - nothing in the environment, the key in a
    gitignored file. Environment wins so a runner cannot pick up a stale
    file if one is ever committed by mistake."""
    env_key = os.environ.get("SUPABASE_SERVICE_KEY")
    if env_key:
        cfg = {"url": os.environ.get("SUPABASE_URL", "").strip(),
               "bucket": os.environ.get("SUPABASE_BUCKET", "").strip(),
               "service_key": env_key.strip()}
        missing = [name for name in ("url", "bucket") if not cfg[name]]
        if missing:
            raise SystemExit(
                "SUPABASE_SERVICE_KEY is set, so the environment is being "
                "used for credentials, but {0} missing.".format(
                    " and ".join("SUPABASE_" + n.upper() for n in missing)))
        print("config    : environment")
        return cfg

    if not os.path.exists(CONFIG):
        raise SystemExit(
            "No {0}.\n\nCreate it with:\n"
            '  {{"url": "https://<project>.supabase.co",\n'
            '   "bucket": "dee-extension",\n'
            '   "service_key": "<secret key from Supabase > Settings > API>"}}\n\n'
            "It is gitignored - the secret key must never ship in the "
            "extension.".format(os.path.relpath(CONFIG, ROOT)))
    with open(CONFIG, "r", encoding="utf-8") as handle:
        cfg = json.load(handle)
    for field in ("url", "bucket", "service_key"):
        if not cfg.get(field):
            raise SystemExit("{0} is missing '{1}'".format(CONFIG, field))
    print("config    : {0}".format(os.path.relpath(CONFIG, ROOT)))
    return cfg


def request(cfg, method, path, data=None, content_type=None, public=False):
    url = "{0}/storage/v1/{1}".format(cfg["url"].rstrip("/"), path)
    req = urllib.request.Request(url, data=data, method=method)
    if not public:
        req.add_header("apikey", cfg["service_key"])
        req.add_header("Authorization", "Bearer " + cfg["service_key"])
    if content_type:
        req.add_header("Content-Type", content_type)
    try:
        return urllib.request.urlopen(req, timeout=120).read(), None
    except HTTPError as e:
        return None, "HTTP {0}: {1}".format(
            e.code, e.read()[:300].decode("utf-8", "replace"))
    except Exception as e:
        return None, str(e)


def guess_type(rel):
    ext = os.path.splitext(rel)[1].lower()
    return {
        ".py": "text/x-python", ".json": "application/json",
        ".xaml": "application/xml", ".yaml": "text/yaml",
        ".html": "text/html", ".png": "image/png", ".md": "text/markdown",
        ".txt": "text/plain", ".iss": "text/plain",
    }.get(ext, "application/octet-stream")


def remote_manifest(cfg):
    url = "{0}/storage/v1/object/public/{1}/{2}".format(
        cfg["url"].rstrip("/"), cfg["bucket"], MANIFEST_NAME)
    try:
        data = urllib.request.urlopen(url, timeout=60).read()
        return json.loads(data.decode("utf-8"))
    except Exception:
        return {"files": {}}


def upload(cfg, rel, body):
    """POST creates, PUT replaces - try create, fall back to replace."""
    path = "object/{0}/{1}".format(cfg["bucket"], rel)
    ctype = guess_type(rel)
    _data, err = request(cfg, "POST", path, body, ctype)
    if err is None:
        return None
    _data, err2 = request(cfg, "PUT", path, body, ctype)
    return err2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="report what would change, upload nothing")
    parser.add_argument("--force", action="store_true",
                        help="publish even with a dirty tree or unpushed commits")
    args = parser.parse_args()

    commit, dirty, unpushed, untracked = git_state()
    print("HEAD      : {0}".format(commit))
    print("working   : {0}".format("DIRTY" if dirty else "clean"))
    if os.environ.get("CI"):
        # A CI run is triggered BY the push, so the checked-out commit is
        # on GitHub by construction. There is usually no upstream ref to
        # compare against, and the count would be a meaningless 0 - say
        # which of the two it is rather than letting it read as proof.
        print("unpushed  : n/a (CI - this commit IS the pushed one)")
    else:
        print("unpushed  : {0}".format(unpushed))

    if untracked:
        # Not a blocker - see git_state - but if one of these was meant
        # to be part of the release, it is missing from GitHub too.
        print("\nuntracked (not mirrored, and not on GitHub either):")
        for path in untracked[:10]:
            print("   ? {0}".format(path))
        if len(untracked) > 10:
            print("   ... and {0} more".format(len(untracked) - 10))
        print("   If any of those belong in the release, commit and push "
              "them first.")

    if (dirty or unpushed) and not args.check:
        message = []
        if dirty:
            message.append("the working tree has uncommitted changes")
        if unpushed:
            message.append("{0} commit(s) are not pushed to GitHub".format(unpushed))
        detail = " and ".join(message)
        if not args.force:
            raise SystemExit(
                "\nRefusing to publish: {0}.\n\n"
                "The mirror must never hold code GitHub does not have - that is\n"
                "the whole guarantee. Commit and push first, then re-run.\n"
                "(--force overrides this.)".format(detail))
        print("\n!! --force: publishing anyway, though {0}.".format(detail))
        print("!! The mirror will NOT match GitHub until you push.\n")

    files = tracked_files()
    print("mirroring : {0} file(s)".format(len(files)))

    manifest = {"commit": commit,
                "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "files": {}}
    local = {}
    for rel in files:
        full = os.path.join(ROOT, rel.replace("/", os.sep))
        sha = digest(full)
        local[rel] = sha
        manifest["files"][rel] = {"sha256": sha, "size": os.path.getsize(full)}

    if args.check:
        cfg = load_config() if os.path.exists(CONFIG) else None
        if cfg is None:
            print("\n(no publish config - cannot compare with the mirror)")
            return 0
        remote = remote_manifest(cfg).get("files", {})
        changed = [r for r, s in local.items()
                   if (remote.get(r) or {}).get("sha256") != s]
        gone = [r for r in remote if r not in local]
        print("\nmirror commit : {0}".format(
            remote_manifest(cfg).get("commit", "(none)")))
        print("would upload  : {0}".format(len(changed)))
        print("would remove  : {0}".format(len(gone)))
        for rel in changed[:15]:
            print("   + {0}".format(rel))
        if len(changed) > 15:
            print("   ... and {0} more".format(len(changed) - 15))
        return 1 if (changed or gone) else 0

    cfg = load_config()
    remote = remote_manifest(cfg).get("files", {})
    changed = [r for r, s in local.items()
               if (remote.get(r) or {}).get("sha256") != s]
    gone = [r for r in remote if r not in local]

    print("changed   : {0}".format(len(changed)))
    print("removed   : {0}".format(len(gone)))
    if not changed and not gone:
        # Not an early exit. The FILES are already right, but this is a
        # different commit, and the manifest is what records which commit
        # the mirror reflects. Leaving it behind would look like drift
        # while nothing is actually wrong - which happens on any push
        # that only touched tools/ or installer/, both excluded here.
        print("\nFiles already match - refreshing the manifest only.")

    failed = []
    for index, rel in enumerate(changed, 1):
        full = os.path.join(ROOT, rel.replace("/", os.sep))
        with open(full, "rb") as handle:
            body = handle.read()
        err = upload(cfg, rel, body)
        status = "ok" if err is None else "FAILED"
        if err is not None:
            failed.append((rel, err))
        print("  [{0}/{1}] {2:<6} {3}".format(index, len(changed), status, rel))

    for rel in gone:
        _d, err = request(cfg, "DELETE", "object/{0}/{1}".format(cfg["bucket"], rel))
        print("  removed {0}{1}".format(rel, "" if err is None else " FAILED " + err))

    if failed:
        print("\n{0} file(s) failed - manifest NOT updated, so the mirror still\n"
              "advertises the previous version rather than a half-published one."
              .format(len(failed)))
        for rel, err in failed[:10]:
            print("   {0}: {1}".format(rel, err))
        return 1

    # The installer's single-download artifact. Built from the same
    # file list, in the same run, so it cannot drift from the loose
    # files beside it.
    print("\nbuilding {0} ...".format(ZIP_NAME))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for rel in files:
            full = os.path.join(ROOT, rel.replace("/", os.sep))
            # Inside the zip everything sits under Dee.extension/, so
            # extracting into an extensions folder produces exactly the
            # layout pyRevit expects.
            archive.write(full, "Dee.extension/" + rel)
    body = buffer.getvalue()
    manifest["zip"] = {"name": ZIP_NAME,
                       "sha256": hashlib.sha256(body).hexdigest(),
                       "size": len(body)}
    print("  {0:.1f} MB".format(len(body) / 1048576.0))
    err = upload(cfg, ZIP_NAME, body)
    if err:
        print("\nZip upload failed: {0}".format(err))
        print("Manifest NOT updated - the mirror still advertises the "
              "previous version rather than a half-published one.")
        return 1

    # The manifest goes LAST, on purpose: until it does, clients keep
    # seeing the previous complete version. A half-published mirror is
    # never advertised.
    err = upload(cfg, MANIFEST_NAME,
                 json.dumps(manifest, indent=1, sort_keys=True).encode("utf-8"))
    if err:
        print("\nFiles uploaded but the manifest failed: {0}".format(err))
        print("Clients will keep using the previous version. Re-run to finish.")
        return 1

    print("\nPublished. Mirror is now at commit {0} - {1} file(s) plus "
          "{2} for the installer.".format(commit, len(files), ZIP_NAME))
    return 0


if __name__ == "__main__":
    sys.exit(main())
