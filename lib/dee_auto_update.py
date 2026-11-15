# -*- coding: utf-8 -*-
"""Update DeePack every time Revit starts, before the ribbon is built.

startup.py calls run_at_startup(). It starts the update on a worker
thread and waits for it for a short time budget (default 8 s):

  * normally the check takes a second or two, so the new files are on
    disk BEFORE pyRevit builds the ribbon - new buttons appear in the
    same session, no extra Reload;
  * on a slow or missing network the budget runs out, Revit carries on
    with what is on disk, and the thread finishes in the background -
    the update is then in place for the next launch. Revit never hangs.

Which route is used depends on how this copy got here
(dee_update_source.detect_install_source):

  git clone (pyrevit extend)  -> `git fetch` + fast-forward merge, which
                                 is quick. Without git.exe on the PC it
                                 falls back to `pyrevit extensions update`.
                                 A copy with local changes is never
                                 touched (a fast-forward-only merge
                                 refuses) - a developer's work is safe.
  Supabase mirror install     -> fetch the small manifest; only when its
                                 commit differs, download the changed
                                 files (dee_update_source.apply_update).
  neither (hand-copied)       -> nothing to do.

Never raises. The last result is written to lib/.dee_auto_update.json
(gitignored) so "did it update?" can be answered after the fact.
"""
import datetime
import json
import os
import threading

import dee_update_source as upd

_STATUS_FILE = os.path.join("lib", ".dee_auto_update.json")
_GIT_TIMEOUT = 60          # seconds, per git call (the caller only waits the budget)
_CLI_TIMEOUT = 120


# ==========================================================================
# running programs (Revit: System.Diagnostics; CPython for the tests)
# ==========================================================================
def _run(args, cwd, timeout):
    """(exit_code, stdout, stderr); exit_code None on timeout / not found."""
    try:
        import clr
        clr.AddReference("System")
        from System.Diagnostics import Process, ProcessStartInfo
        psi = ProcessStartInfo()
        psi.FileName = args[0]
        psi.Arguments = u" ".join(u'"{0}"'.format(a) if u" " in a else a for a in args[1:])
        psi.WorkingDirectory = cwd
        psi.UseShellExecute = False
        psi.RedirectStandardOutput = True
        psi.RedirectStandardError = True
        psi.CreateNoWindow = True
        proc = Process.Start(psi)
        out = proc.StandardOutput.ReadToEnd()
        err = proc.StandardError.ReadToEnd()
        if not proc.WaitForExit(int(timeout * 1000)):
            try:
                proc.Kill()
            except Exception:
                pass
            return None, out, "timeout"
        return proc.ExitCode, out, err
    except ImportError:
        pass
    except Exception as e:
        return None, "", str(e)
    import subprocess
    try:
        p = subprocess.Popen(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = p.communicate()
        return p.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
    except Exception as e:
        return None, "", str(e)


def _find_git():
    """Full path of git.exe, or None."""
    candidates = []
    for folder in (os.environ.get("PATH") or "").split(os.pathsep):
        candidates.append(os.path.join(folder.strip('"'), "git.exe"))
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                 os.path.join(os.environ.get("LOCALAPPDATA") or "", "Programs")):
        if base:
            candidates.append(os.path.join(base, "Git", "cmd", "git.exe"))
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


# ==========================================================================
# the two routes
# ==========================================================================
def _update_git(root):
    git = _find_git()
    if git is None:
        code, out, err = _run(["pyrevit", "extensions", "update", "--all"], root, _CLI_TIMEOUT)
        return "pyrevit CLI update (no git.exe found) - exit {0}".format(code)
    code, branch, err = _run([git, "rev-parse", "--abbrev-ref", "HEAD"], root, _GIT_TIMEOUT)
    branch = (branch or "").strip()
    if code != 0 or not branch or branch == "HEAD":
        return "skipped - could not tell the branch ({0})".format((err or "").strip()[:120])
    code, out, err = _run([git, "fetch", "--quiet", "origin"], root, _GIT_TIMEOUT)
    if code != 0:
        return "fetch failed - {0}".format((err or "offline?").strip()[:120])
    code, before, _e = _run([git, "rev-parse", "--short", "HEAD"], root, _GIT_TIMEOUT)
    code, out, err = _run([git, "merge", "--ff-only", "origin/" + branch], root, _GIT_TIMEOUT)
    if code != 0:
        return "not updated - local changes or diverged history ({0})".format((err or out or "").strip()[:120])
    code, after, _e = _run([git, "rev-parse", "--short", "HEAD"], root, _GIT_TIMEOUT)
    before, after = (before or "").strip(), (after or "").strip()
    if before == after:
        return "already up to date ({0})".format(after)
    return "updated {0} -> {1}".format(before, after)


def _update_mirror(root):
    url, bucket = upd.load_mirror_config(root)
    manifest, detail = upd.fetch_manifest(url, bucket)
    if manifest is None:
        return "mirror not reachable - {0}".format(str(detail).splitlines()[0][:120])
    commit = manifest.get("commit")
    if commit and commit == upd.installed_commit(root):
        return "already up to date ({0})".format(commit)
    plan = upd.plan_update(root, manifest)
    if not plan["download"] and not plan["delete"]:
        return "already up to date ({0})".format(commit)
    result = upd.apply_update(root, url, bucket, manifest, plan=plan)
    failed = result.get("failed") if isinstance(result, dict) else None
    return "updated to {0} ({1} file(s){2})".format(
        commit, len(plan["download"]), ", {0} failed".format(len(failed)) if failed else "")


def update_now(root=None):
    """Runs the update for this copy. Returns a short status string."""
    if root is None:
        root = upd.extension_root()
    source = upd.detect_install_source(root)
    if source == upd.SOURCE_GITHUB:
        return _update_git(root)
    if source == upd.SOURCE_SUPABASE:
        return _update_mirror(root)
    return "nothing to update (not a git or mirror install)"


def _save_status(root, text):
    try:
        with open(os.path.join(root, _STATUS_FILE), "w") as handle:
            json.dump({"when": datetime.datetime.now().isoformat(), "result": text}, handle)
    except Exception:
        pass


def _worker(root):
    try:
        text = update_now(root)
    except Exception as e:
        text = "failed - {0}".format(e)
    _save_status(root, text)


def run_at_startup(root=None, budget_seconds=8):
    """Starts the update and waits at most `budget_seconds` for it."""
    try:
        if root is None:
            root = upd.extension_root()
        thread = threading.Thread(target=_worker, args=(root,))
        thread.daemon = True
        thread.start()
        thread.join(budget_seconds)
    except Exception:
        pass
