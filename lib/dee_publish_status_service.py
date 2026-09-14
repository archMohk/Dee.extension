# -*- coding: utf-8 -*-
"""Which cloud models in an ACC project still need publishing.

The two publish tools that already exist both publish BLIND: DeeS.Publish
publishes every file in a project, DeePublisher publishes whatever you
tick. Neither asks ACC what actually needs publishing, so both spend real
time (and ACC processing) re-publishing models that were already up to
date. This module answers the question first, so only the models that
need it get published.

HOW THE STATUS IS READ
----------------------
Autodesk's own mechanism: the Data Management Commands API, command
`commands:autodesk.bim360:C4RModelGetPublishJob`. It reports the publish
job for a cloud-workshared model - that is, whether the model has changes
synced to the cloud that have not been published to Docs yet. It is a
read: the command that actually publishes is C4RModelPublish, which
acc_api.publish_item() already wraps and which this module never calls.

A WARNING ABOUT THE PARSING BELOW
---------------------------------
Unlike acc_links_service, whose every response shape was captured live
before a line was written, this command's exact response shape has NOT
been verified against the live service yet - the access token had expired
at the time of writing and refreshing it means a login in Revit. The APS
reference pages render as an empty single-page-app shell and cannot be
read either.

So the extraction here is deliberately defensive and deliberately
LOUD about it:

  - it looks for a status string in every place the JSON:API envelope
    could plausibly carry one, rather than assuming one path;
  - anything it does not recognise becomes UNKNOWN carrying the RAW text,
    which the tool shows verbatim and writes to the log;
  - it NEVER silently maps an unrecognised value onto "Published".

That last point is the one that matters. Guessing "published" for a
status nobody has seen would tell someone their model is safe when it may
not be. Guessing "needs publishing" would have them re-publish something
needlessly. Saying "unknown, here is what ACC said" is the only honest
answer, and the first live run turns every unknown into a known - the
same way the dialog handler's unrecognised-dialog logging did.
"""
import threading
import time

try:
    import Queue as queue          # IronPython 2.7
except ImportError:
    import queue                   # Python 3 (tests)

try:
    from urllib import quote as _quote
except ImportError:
    from urllib.parse import quote as _quote

import acc_api

BASE_URL = acc_api.BASE_URL

PUBLISHED = "Published"
NEEDS_PUBLISH = "Needs publishing"
IN_PROGRESS = "Publishing now"
UNKNOWN = "Unknown"
ERROR = "Error"

# Raw status text (lower-cased, substring match) -> our status. Extend
# this as live runs reveal real values; anything absent stays UNKNOWN and
# is reported verbatim rather than guessed at.
_STATUS_MAP = (
    ("notpublished", NEEDS_PUBLISH),
    ("not published", NEEDS_PUBLISH),
    ("needspublish", NEEDS_PUBLISH),
    ("outofdate", NEEDS_PUBLISH),
    ("out of date", NEEDS_PUBLISH),
    ("pending", NEEDS_PUBLISH),
    ("queued", IN_PROGRESS),
    ("inprogress", IN_PROGRESS),
    ("in progress", IN_PROGRESS),
    ("processing", IN_PROGRESS),
    ("extracting", IN_PROGRESS),
    ("running", IN_PROGRESS),
    ("scheduled", IN_PROGRESS),
    ("published", PUBLISHED),      # AFTER "notpublished"/"not published"
    ("uptodate", PUBLISHED),
    ("up to date", PUBLISHED),
    ("complete", PUBLISHED),
    ("success", PUBLISHED),
    ("finished", PUBLISHED),
)

_PERMANENT_MARKERS = ("Unauthorized", "Forbidden", "401", "403")
_MAX_ATTEMPTS = 3
_SCAN_TIME_BUDGET_SECONDS = 600

# Keys worth inspecting when hunting for the status in the response.
_STATUS_KEYS = ("status", "publishstatus", "publishjobstatus", "jobstatus",
                "state", "processstate", "publishstate")


def _is_permanent(text):
    return any(m in text for m in _PERMANENT_MARKERS)


def _post_with_retry(url, token, body):
    last = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            return acc_api.api_post(url, token, body,
                                    content_type="application/vnd.api+json")
        except Exception as e:
            last = str(e)
            if _is_permanent(last):
                break
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(2 * (attempt + 1))
    raise Exception(last or "request failed")


def classify(raw_text):
    """Maps ACC's own status wording onto one of our statuses.

    Order matters: "notpublished" has to be tested before "published",
    since the second is a substring of the first. Returns (status,
    raw_text) - the raw text travels with it so the UI can show exactly
    what ACC said, which is the only way an UNKNOWN ever becomes known."""
    if raw_text is None:
        return UNKNOWN, ""
    text = str(raw_text).strip()
    # Spaces are stripped too, not just _ and -: without that,
    # "NOT PUBLISHED" failed to match the "not published" needle and
    # fell through to the "published" one, reporting a stale model as
    # safely published. Caught by the substring-ordering test.
    low = text.lower().replace("_", "").replace("-", "").replace(" ", "")
    for needle, status in _STATUS_MAP:
        if needle.replace(" ", "") in low:
            return status, text
    return UNKNOWN, text


def _find_status_text(node, depth=0):
    """Walks the JSON:API response looking for anything that reads like a
    status. Written as a search rather than a fixed path on purpose: the
    exact envelope this command returns is not yet confirmed, and a search
    that finds it in any shape beats a path that breaks in all but one."""
    if depth > 8 or node is None:
        return None
    if isinstance(node, dict):
        for key, value in node.items():
            if (isinstance(value, str) and value.strip()
                    and key.lower().replace("_", "") in _STATUS_KEYS):
                return value
        for value in node.values():
            found = _find_status_text(value, depth + 1)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_status_text(value, depth + 1)
            if found is not None:
                return found
    return None


def get_publish_job(project_id, item_id, token):
    """Asks ACC for one model's publish job. Returns (status, raw_text,
    raw_payload). Raises only for a transport/permission failure."""
    body = {
        "jsonapi": {"version": "1.0"},
        "data": {
            "type": "commands",
            "attributes": {
                "extension": {
                    "type": "commands:autodesk.bim360:C4RModelGetPublishJob",
                    "version": "1.0.0",
                }
            },
            "relationships": {
                "resources": {"data": [{"type": "items", "id": item_id}]}
            },
        },
    }
    url = BASE_URL + "/data/v1/projects/{0}/commands".format(project_id)
    payload = _post_with_retry(url, token, body)
    raw = _find_status_text(payload)
    status, text = classify(raw)
    return status, text, payload


def scan_statuses(project_id, items, token, max_workers=6, on_progress=None,
                  should_cancel=None, logger=None,
                  time_budget=_SCAN_TIME_BUDGET_SECONDS):
    """Publish status for many models at once.

    `items` is [(item_id, name), ...]; returns rows in that order. One
    pool of worker threads created ONCE - repeatedly spawning and
    discarding real .NET threads from IronPython is the pattern that was
    taking Revit down during ACC scans, and it is not repeated here."""
    total = len(items)
    rows = [None] * total
    q = queue.Queue()
    for index, entry in enumerate(items):
        q.put((index, entry))

    lock = threading.Lock()
    done = [0]
    cancelled = [False]
    started = time.time()
    unknown_samples = []

    def worker():
        while True:
            try:
                index, (item_id, name) = q.get(timeout=0.5)
            except queue.Empty:
                if q.unfinished_tasks == 0:
                    return
                continue
            try:
                if cancelled[0] or (time.time() - started) > time_budget:
                    rows[index] = {"item_id": item_id, "name": name,
                                   "status": UNKNOWN,
                                   "detail": "not checked - stopped early"}
                else:
                    status, text, payload = get_publish_job(project_id, item_id, token)
                    rows[index] = {"item_id": item_id, "name": name,
                                   "status": status, "detail": text}
                    if status == UNKNOWN:
                        with lock:
                            if len(unknown_samples) < 3:
                                unknown_samples.append((name, payload))
            except Exception as e:
                rows[index] = {"item_id": item_id, "name": name,
                               "status": ERROR, "detail": str(e)}
            finally:
                with lock:
                    done[0] += 1
                q.task_done()

    workers = []
    for _ in range(max(1, min(max_workers, total or 1))):
        t = threading.Thread(target=worker)
        t.daemon = True
        t.start()
        workers.append(t)

    while any(t.is_alive() for t in workers):
        if should_cancel is not None and should_cancel():
            cancelled[0] = True
        if on_progress is not None:
            try:
                on_progress(done[0], total)
            except Exception:
                pass
        for t in workers:
            t.join(0.3)

    if on_progress is not None:
        try:
            on_progress(done[0], total)
        except Exception:
            pass

    # Log a couple of whole payloads when the status could not be read.
    # This is what turns "unknown" into a one-line fix next time rather
    # than another round of guessing.
    if logger is not None and unknown_samples:
        import json as _json
        for name, payload in unknown_samples:
            try:
                logger.warning("Publish status not recognised - raw response follows",
                               file=name, raw=_json.dumps(payload)[:1500])
            except Exception:
                pass

    return [r for r in rows if r is not None]


def summarize(rows):
    counts = {PUBLISHED: 0, NEEDS_PUBLISH: 0, IN_PROGRESS: 0,
              UNKNOWN: 0, ERROR: 0}
    for row in rows:
        status = row.get("status")
        if status in counts:
            counts[status] += 1
    return counts


def needs_publishing(rows):
    """Only the models it is worth publishing. UNKNOWN is deliberately
    NOT included: publishing on a guess is the thing this tool exists to
    stop. The user can still tick an unknown by hand if they want it."""
    return [r for r in rows if r.get("status") == NEEDS_PUBLISH]
