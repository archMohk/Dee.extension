# -*- coding: utf-8 -*-
"""What is known about each cloud model's publishing, and what to do about it.

WHAT THIS ANSWERS, AND WHAT IT DOES NOT
---------------------------------------
It would be nice to ask ACC "which models have changes that were synced
but never published?". These APIs do not expose that, and the first cut
of this module pretended otherwise - worth writing down so it is not
tried again the same way.

C4RModelGetPublishJob was the obvious candidate and it is the wrong
question. It reports a publish JOB, not a model's publish state. Run live
against a 155-model project it answered:

    63 models  ->  a job with status "complete"
    92 models  ->  {"jsonapi": {...}, "data": null}   (no job on record)
     0 models  ->  anything resembling "needs publishing"

Zero, across every model in the project - because that state is not
something this command reports. "complete" only means a publish job
finished at some point; it says nothing about syncs made since. Reporting
that as "Published" told the user their model was current when the API
had not said so.

Neither does any per-model RCM endpoint: models/{guid},
models/{guid}/status, published-versions/{id} and
versions/{id}/publish-state were all probed live and all 404.

And publishStatus inside the RCM linked-files response is per
RELATIONSHIP, not per model - the same model came back "NotPublished" as
a link of one host and "Published" as a host itself, in the same minute.
It records the state of the version a host referenced when that host was
published, which is a historical fact about the link, not a current fact
about the model.

SO THIS MODULE REPORTS TWO SEPARATE, HONEST THINGS
--------------------------------------------------
1. JOB STATUS - what C4RModelGetPublishJob actually said, named for what
   it is: a job completed, a job is running now, or no job on record.
   Useful for "is a publish still churning", not for "is this current".

2. PUBLISH TYPE - from the item's own tip version, verified live during
   the DeeLinkMAP work. This is the column worth acting on:

     NoZipFile     published the current way. ACC can report this model's
                   Revit links, so DeeLinkMAP reads it without opening it.
     WithoutLinks  published with its links stripped. ACC holds no link
                   data, DeeLinkMAP has to open the model, and opening is
                   what has been crashing Revit. Re-publishing it normally
                   is a real, concrete fix.

So "which ones should I publish?" gets a truthful answer - the
old-workflow ones - even though "which ones are out of date?" cannot be
answered from here at all.
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
import acc_links_service as _links

BASE_URL = acc_api.BASE_URL

# Publish JOB states - what C4RModelGetPublishJob reports. Named so
# that nobody reads them as "this model is up to date", which is a
# different question these APIs do not answer (see the module docstring).
JOB_DONE = "Job complete"
JOB_RUNNING = "Publishing now"
JOB_NONE = "No job on record"
JOB_UNKNOWN = "Unrecognised"
ERROR = "Error"

# Publish TYPE - the column worth acting on.
TYPE_CURRENT = "Current (links readable)"
TYPE_OLD = "Old publish (re-publish)"
TYPE_UNKNOWN = "Unknown"

# Raw status text (lower-cased, substring match) -> our status. Extend
# this as live runs reveal real values; anything absent stays UNKNOWN and
# is reported verbatim rather than guessed at.
_STATUS_MAP = (
    ("failed", ERROR),
    ("error", ERROR),
    ("queued", JOB_RUNNING),
    ("inprogress", JOB_RUNNING),
    ("processing", JOB_RUNNING),
    ("extracting", JOB_RUNNING),
    ("running", JOB_RUNNING),
    ("scheduled", JOB_RUNNING),
    ("pending", JOB_RUNNING),
    ("complete", JOB_DONE),
    ("success", JOB_DONE),
    ("finished", JOB_DONE),
    ("published", JOB_DONE),
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
        # "data": null - ACC has no publish job for this model at all.
        # That is an ANSWER, not a gap, so it gets its own state rather
        # than being lumped in with wording we failed to parse.
        return JOB_NONE, ""
    text = str(raw_text).strip()
    # Spaces are stripped too, not just _ and -: without that,
    # "NOT PUBLISHED" failed to match the "not published" needle and
    # fell through to the "published" one, reporting a stale model as
    # safely published. Caught by the substring-ordering test.
    low = text.lower().replace("_", "").replace("-", "").replace(" ", "")
    for needle, status in _STATUS_MAP:
        if needle.replace(" ", "") in low:
            return status, text
    return JOB_UNKNOWN, text


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
                                   "status": JOB_UNKNOWN,
                                   "detail": "not checked - stopped early"}
                else:
                    status, text, payload = get_publish_job(project_id, item_id, token)
                    row = {"item_id": item_id, "name": name,
                           "status": status, "detail": text,
                           "publish_type": None,
                           "publish_type_label": TYPE_UNKNOWN,
                           "version_number": None,
                           "last_published": None}
                    # The item's own tip version carries the publish TYPE,
                    # which is the part of this report worth acting on.
                    # A failure here must not lose the job status we
                    # already have, so it degrades to "unknown type".
                    try:
                        info = _links.get_item_tip(project_id, item_id, token)
                        row["publish_type"] = info.get("publish_type")
                        row["publish_type_label"] = classify_publish_type(
                            info.get("publish_type"))
                        row["version_number"] = info.get("version_number")
                        row["last_published"] = info.get("created_time")
                    except Exception:
                        pass
                    rows[index] = row
                    if status == JOB_UNKNOWN:
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
    """Counts for BOTH columns, kept apart on purpose: the job states and
    the publish types answer different questions and must not be added
    together into one misleading total."""
    counts = {JOB_DONE: 0, JOB_RUNNING: 0, JOB_NONE: 0,
              JOB_UNKNOWN: 0, ERROR: 0}
    types = {TYPE_CURRENT: 0, TYPE_OLD: 0, TYPE_UNKNOWN: 0}
    for row in rows:
        status = row.get("status")
        if status in counts:
            counts[status] += 1
        label = row.get("publish_type_label")
        if label in types:
            types[label] += 1
    counts["types"] = types
    return counts


def classify_publish_type(publish_type):
    """The actionable column. Verified live: a version published as
    "NoZipFile" can have its Revit links read straight from ACC; one
    published as "WithoutLinks" cannot, and has to be opened in Revit
    instead - which is the slow, crash-prone path. Anything else is
    reported as unknown rather than guessed into one bucket."""
    if not publish_type:
        return TYPE_UNKNOWN
    if publish_type == "NoZipFile":
        return TYPE_CURRENT
    if publish_type == "WithoutLinks":
        return TYPE_OLD
    return TYPE_UNKNOWN


def worth_publishing(rows):
    """Models a re-publish would actually improve: the ones published the
    old way, whose links ACC therefore cannot report. Never includes
    unknowns - publishing on a guess is what this module exists to
    avoid. Anything else can still be ticked by hand."""
    return [r for r in rows if r.get("publish_type_label") == TYPE_OLD]
