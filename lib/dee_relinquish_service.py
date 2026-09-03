# -*- coding: utf-8 -*-
"""
dee_relinquish_service
Scan / relinquish logic for DeeRelinquish - reports who owns what across an
ACC project's Revit models, and releases everything owned by the CURRENT
user in the models you pick.

--------------------------------------------------------------------
What the Revit API can and cannot do here - verified, not assumed
--------------------------------------------------------------------
You CANNOT force-relinquish another user's ownership. The official
documentation for WorksharingUtils.RelinquishOwnership says, verbatim:

    "Elements and worksets owned by other users are ignored."

No ACC/APS REST endpoint for it was found either. The only route is
Revit's own UI: Collaborate > Manage Model > Manage Cloud Models, which
needs the appropriate project permission. This module therefore REPORTS
other users' holds precisely (model / workset / owner) and never pretends
to release them. Confirmed with the user before this was built.

You CAN, and this module does:

  WorksharingUtils.GetUserWorksetInfo(ModelPath) -> IList<WorksetPreview>
      "Gets information about user worksets in a workshared model file,
      without fully opening the file."
      This is what makes the scan fast and risk-free - no model is opened,
      no local cache is created. WorksetPreview exposes .Name and .Owner
      (the owner is "" when nobody holds it).

  WorksharingUtils.RelinquishOwnership(Document, RelinquishOptions,
                                       TransactWithCentralOptions)
      -> RelinquishedItems
      Needs the model OPEN and ATTACHED to its central (a detached model
      has no central to relinquish to).

  RelinquishOptions(bool relinquishEverything), with five independent
      flags: CheckedOutElements, FamilyWorksets, StandardWorksets,
      UserWorksets, ViewWorksets.

--------------------------------------------------------------------
The unsynced-changes trap
--------------------------------------------------------------------
Also verbatim from the same documentation:

    "Only unmodified elements already in central will be relinquished.
    Newly added and modified elements cannot be relinquished until they
    have been synchronized with central."

So on a model holding unsynced local changes, relinquish alone leaves
those elements owned. Synchronising first fixes that but PUBLISHES that
work to the team, which is a decision the user has to make rather than a
tidy-up detail. It is therefore an explicit opt-in in the window, off by
default, and the report always says when a model was left partially held
because of it.

--------------------------------------------------------------------
NEEDS LIVE-REVIT VERIFICATION (flagged, not silently assumed correct)
--------------------------------------------------------------------
- GetUserWorksetInfo against a CLOUD ModelPath specifically. The method is
  documented for a workshared model file; every published example uses a
  file-server path. If a cloud path is rejected, the scan degrades to
  "could not read" per model rather than failing the run.
- Whether RelinquishedItems reports counts usefully across Revit versions;
  the code reads it defensively and falls back to "relinquished" without
  numbers.
- Element-level ownership is NOT scanned. GetCheckoutStatus needs the model
  open, and the user chose the fast no-open scan, so a borrowed element in
  a workset nobody owns will not appear in the report.
"""

CATEGORY_FLAGS = [
    ("CheckedOutElements", "Checked-out elements",
     "Individual elements you borrowed while editing."),
    ("UserWorksets", "User worksets",
     "The normal worksets people create for model content."),
    ("ViewWorksets", "View worksets",
     "One per view - held when you edit a view's properties."),
    ("FamilyWorksets", "Family worksets",
     "Held when you load or edit a family in the project."),
    ("StandardWorksets", "Project standards worksets",
     "Materials, line styles, browser organisation and similar."),
]


class OwnershipRow(object):
    """One workset in one model. Plain strings only - no Revit objects are
    held across the window's wait for the user."""

    def __init__(self, model_name, item_id, workset_name, owner, is_mine):
        self.model_name = model_name
        self.item_id = item_id
        self.workset_name = workset_name
        self.owner = owner
        self.is_mine = is_mine

    @property
    def owner_display(self):
        if not self.owner:
            return "(free)"
        return "{0}{1}".format(self.owner, "  <- you" if self.is_mine else "")

    @property
    def haystack(self):
        return u"{0} {1} {2}".format(self.model_name, self.workset_name, self.owner)


class ModelRow(object):
    """One Revit model in the ACC project, with its ownership summary."""

    def __init__(self, name, item_id):
        self.name = name
        self.item_id = item_id
        self.selected = False
        self.scanned = False
        self.scan_error = ""
        self.mine = 0
        self.others = 0
        self.free = 0
        self.other_owners = []
        self.result = ""

    @property
    def status(self):
        if self.scan_error:
            return "Could not read - {0}".format(self.scan_error)
        if not self.scanned:
            return "Not scanned"
        if self.mine and self.others:
            return "You hold {0}, others hold {1}".format(self.mine, self.others)
        if self.mine:
            return "You hold {0} workset(s)".format(self.mine)
        if self.others:
            return "Others hold {0} workset(s)".format(self.others)
        return "Nothing held"

    @property
    def owners_display(self):
        return ", ".join(self.other_owners) if self.other_owners else ""

    @property
    def haystack(self):
        return u"{0} {1}".format(self.name, self.owners_display)


def matches(haystack, query):
    """AND-of-terms - the same filter rule every other DeePack list uses."""
    if not query:
        return True
    low = haystack.lower()
    return all(term in low for term in query.lower().split())


def same_user(owner, current_user):
    """Revit usernames come back in mixed case and can carry surrounding
    whitespace, so compare defensively rather than with ==."""
    if not owner or not current_user:
        return False
    return owner.strip().lower() == current_user.strip().lower()


def summarize_worksets(previews, current_user):
    """(mine, others, free, sorted_other_owners) from a list of objects with
    .Owner. Pure logic - unit-tested with plain stand-ins."""
    mine = others = free = 0
    owners = set()
    for preview in previews:
        try:
            owner = preview.Owner or ""
        except Exception:
            owner = ""
        if not owner.strip():
            free += 1
        elif same_user(owner, current_user):
            mine += 1
        else:
            others += 1
            owners.add(owner.strip())
    return mine, others, free, sorted(owners)


def build_relinquish_options(flags):
    """flags: {property_name: bool}. Built with relinquishEverything=False and
    then set explicitly, so an unticked category is never released by
    accident."""
    from Autodesk.Revit.DB import RelinquishOptions
    options = RelinquishOptions(False)
    for name, _label, _help in CATEGORY_FLAGS:
        try:
            setattr(options, name, bool(flags.get(name, False)))
        except Exception:
            continue
    return options


def any_flag_set(flags):
    return any(bool(flags.get(name, False)) for name, _l, _h in CATEGORY_FLAGS)


def scan_model(model_path, current_user):
    """Reads workset ownership WITHOUT opening the model.
    Returns (previews_or_None, error_detail)."""
    from Autodesk.Revit.DB import WorksharingUtils
    try:
        previews = list(WorksharingUtils.GetUserWorksetInfo(model_path))
        return previews, ""
    except Exception as e:
        return None, str(e)


def synchronize_only(doc, comment=""):
    """Synchronizes WITHOUT relinquishing anything.

    Deliberately not deew_document_manager.synchronize_with_central: that one
    passes RelinquishOptions(True), i.e. "release everything", which is right
    for its callers but would make DeeRelinquish's per-category choice
    meaningless - ticking only "User worksets" would still hand back your view
    and family worksets as a side effect of the sync.

    Here the sync exists purely to satisfy the documented precondition that an
    element must already be in central before it can be relinquished; the
    actual release is then done separately with exactly the categories the
    user ticked. Never raises - returns (ok, detail)."""
    from Autodesk.Revit.DB import (
        RelinquishOptions, SynchronizeWithCentralOptions,
        TransactWithCentralOptions,
    )
    try:
        transact = TransactWithCentralOptions()
        sync = SynchronizeWithCentralOptions()
        sync.Comment = comment or "DeeRelinquish - sync before relinquishing"
        sync.Compact = False
        sync.SaveLocalBefore = True
        sync.SaveLocalAfter = True
        sync.SetRelinquishOptions(RelinquishOptions(False))
        doc.SynchronizeWithCentral(transact, sync)
        return True, "synchronized"
    except Exception as e:
        return False, str(e)


def relinquish_document(doc, flags):
    """Releases the current user's ownership in an OPEN, ATTACHED document.
    Never raises - returns (ok, detail)."""
    from Autodesk.Revit.DB import TransactWithCentralOptions, WorksharingUtils
    try:
        options = build_relinquish_options(flags)
        transact = TransactWithCentralOptions()
        items = WorksharingUtils.RelinquishOwnership(doc, options, transact)
    except Exception as e:
        return False, str(e)

    detail = "relinquished"
    try:
        counts = []
        for attr, label in (("Elements", "element"), ("UserWorksets", "user workset"),
                            ("ViewWorksets", "view workset"),
                            ("FamilyWorksets", "family workset"),
                            ("StandardWorksets", "standards workset")):
            try:
                value = len(list(getattr(items, attr)))
            except Exception:
                continue
            if value:
                counts.append("{0} {1}(s)".format(value, label))
        if counts:
            detail = "relinquished " + ", ".join(counts)
    except Exception:
        pass
    return True, detail


def report_html(model_rows, other_rows, synced_note):
    """Coloured report in the pyRevit output window, matching the house style."""
    done = [r for r in model_rows if r.result.startswith("OK")]
    failed = [r for r in model_rows if r.result and not r.result.startswith("OK")]

    html = ('<h2 style="font-family:sans-serif;color:#ddd;">DeeRelinquish</h2>'
            '<div style="font-family:sans-serif;color:#bbb;font-size:13px;'
            'padding:4px 0 10px 0;">{0} model(s) released, {1} failed.{2}</div>'.format(
                len(done), len(failed), synced_note))

    for row in model_rows:
        if not row.result:
            continue
        ok = row.result.startswith("OK")
        html += ('<div style="padding:6px 12px;margin:4px 0 0 0;background:{0};'
                 'color:#fff;border-radius:4px;font-family:monospace;font-size:13px;">'
                 '{1}&nbsp; <b>{2}</b> &mdash; {3}</div>'.format(
                     "#2e7d32" if ok else "#c62828",
                     "&#10003;" if ok else "&#10007;", row.name, row.result))

    if other_rows:
        html += ('<h3 style="font-family:sans-serif;color:#ffb300;margin-top:16px;">'
                 'Held by other people &mdash; not touched</h3>'
                 '<div style="font-family:sans-serif;color:#bbb;font-size:12px;'
                 'padding:0 0 8px 0;">The Revit API cannot release another '
                 'user\'s ownership. Ask the owner to relinquish, or use '
                 'Collaborate &gt; Manage Model &gt; Manage Cloud Models.</div>')
        for row in other_rows:
            html += ('<div style="padding:3px 12px;color:#9e9e9e;font-family:monospace;'
                     'font-size:12px;">{0}  &mdash;  {1}  &mdash;  <b>{2}</b></div>'.format(
                         row.model_name, row.workset_name, row.owner))
    return html


if __name__ == "__main__":
    import unittest

    class FakePreview(object):
        def __init__(self, owner):
            self.Owner = owner

    class SameUserTests(unittest.TestCase):
        def test_exact(self):
            self.assertTrue(same_user("mohk", "mohk"))

        def test_case_and_whitespace_insensitive(self):
            self.assertTrue(same_user("  MohK ", "mohk"))

        def test_different(self):
            self.assertFalse(same_user("someone", "mohk"))

        def test_empty_is_never_me(self):
            self.assertFalse(same_user("", "mohk"))
            self.assertFalse(same_user("mohk", ""))

    class SummarizeTests(unittest.TestCase):
        def test_counts_split_three_ways(self):
            previews = [FakePreview("mohk"), FakePreview("ali"),
                        FakePreview(""), FakePreview("mohk"), FakePreview("  ")]
            mine, others, free, owners = summarize_worksets(previews, "mohk")
            self.assertEqual((mine, others, free), (2, 1, 2))
            self.assertEqual(owners, ["ali"])

        def test_owner_list_is_deduplicated_and_sorted(self):
            previews = [FakePreview("zoe"), FakePreview("ali"), FakePreview("zoe")]
            _m, _o, _f, owners = summarize_worksets(previews, "mohk")
            self.assertEqual(owners, ["ali", "zoe"])

        def test_empty_model(self):
            self.assertEqual(summarize_worksets([], "mohk"), (0, 0, 0, []))

        def test_a_preview_that_raises_counts_as_free(self):
            class Broken(object):
                @property
                def Owner(self):
                    raise Exception("no owner")
            mine, others, free, _o = summarize_worksets([Broken()], "mohk")
            self.assertEqual((mine, others, free), (0, 0, 1))

    class FlagTests(unittest.TestCase):
        def test_any_flag_set(self):
            self.assertFalse(any_flag_set({}))
            self.assertFalse(any_flag_set(dict((n, False) for n, _l, _h in CATEGORY_FLAGS)))
            self.assertTrue(any_flag_set({"UserWorksets": True}))

        def test_five_categories_exposed(self):
            self.assertEqual(len(CATEGORY_FLAGS), 5)
            names = [n for n, _l, _h in CATEGORY_FLAGS]
            self.assertEqual(sorted(names),
                             ["CheckedOutElements", "FamilyWorksets",
                              "StandardWorksets", "UserWorksets", "ViewWorksets"])

    class RowTests(unittest.TestCase):
        def test_model_status_wording(self):
            row = ModelRow("A.rvt", "id")
            self.assertEqual(row.status, "Not scanned")
            row.scanned = True
            self.assertEqual(row.status, "Nothing held")
            row.mine = 3
            self.assertIn("You hold 3", row.status)
            row.others = 2
            self.assertIn("others hold 2", row.status.lower())

        def test_scan_error_wins(self):
            row = ModelRow("A.rvt", "id")
            row.scanned = True
            row.scan_error = "not workshared"
            self.assertIn("Could not read", row.status)

        def test_owner_display_marks_you(self):
            self.assertEqual(OwnershipRow("m", "i", "w", "", False).owner_display, "(free)")
            self.assertIn("<- you", OwnershipRow("m", "i", "w", "mohk", True).owner_display)
            self.assertNotIn("<- you", OwnershipRow("m", "i", "w", "ali", False).owner_display)

    class MatchTests(unittest.TestCase):
        def test_and_of_terms(self):
            self.assertTrue(matches("Tower A Level 3", "tower 3"))
            self.assertFalse(matches("Tower A Level 3", "tower 9"))

        def test_empty_matches_everything(self):
            self.assertTrue(matches("anything", ""))

    unittest.main(verbosity=2)
