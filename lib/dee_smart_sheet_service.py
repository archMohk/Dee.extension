# -*- coding: utf-8 -*-
"""
dee_smart_sheet_service
Core engine for DeeSheet's "SmartSheet" tab - mirrors the project's
current Sheet Browser Organization (Project Browser > Sheets, grouped
into folders by whichever parameters that Browser Organization uses)
into an editable tree, then lets the user:

  - Duplicate a bottom-level group (e.g. one Plot Number folder holding
    a Cover Sheet / Project Document / Setting Out Plan / Area
    Calculation set) into a brand new group - one new sheet per sheet
    in the template group, with the old group's identifying value
    swapped for a new one everywhere it appears in Sheet Number/Sheet
    Name, and (when the parameter driving that folder level could be
    identified) that parameter set to the new value too.
  - Add a single new sheet anywhere in the tree, with every ancestor
    folder's classification parameter pre-filled from the node that
    was clicked, and its Sheet Number suggested by continuing that
    group's own numbering sequence (see next_sequence_numbers).
  - Add the same new sheet(s) to EVERY group under a chosen scope in one
    pass (bulk_add_sheets) - each group gets its own next number(s),
    computed purely from its own existing sheets, so a group at 01..06
    gets 07/08... while a different group at 01..04 gets 05/06..., with
    no assumption that every group started at the same count.

Uses Autodesk.Revit.DB.BrowserOrganization.GetFolderItems(ElementId) to
read the ACTUAL folder path Revit itself would show that sheet under in
the Project Browser, under the current Browser Organization rules -
nothing here guesses at a grouping scheme independently. If that API
is unavailable, or the project's current Sheet Browser Organization has
no grouping rules at all, every sheet falls back to one flat list under
a single root node - SmartSheet still works for Add Sheet, there just
are no folders to Duplicate.

A folder's classification parameter (the thing Duplicate/Add Sheet
writes into) is found empirically by default - see
_find_classifying_parameter: the parameter whose displayed value equals
the folder's label on every sheet nested under it. This runs for EVERY
folder level, not just the bottom-level group, and it searches BOTH the
Sheet's own instance parameters AND its placed Title Block instance's
parameters (_iter_param_names_and_values) - plenty of projects
carry plot/villa metadata as instance parameters on a dedicated Title
Block placed per sheet rather than on the Sheet element itself, and
there is no way to tell which from the tree alone, so both are always
searched together, and writing (duplicate_group/add_sheet/
bulk_add_sheets, via _set_param_on_sheet_or_titleblock) tries the Sheet
first and falls back to its Title Block instance.

Auto-detection is still only a best guess though, and it can fail in
ways that are invisible until a new sheet shows up unclassified: a
level whose value is blank for every sheet in a branch renders as
Revit's own '???' placeholder, which cannot match any real parameter
value, so nothing can be auto-detected there; a level's parameter might
match ambiguously, or sit on a Title Block TYPE parameter (shared by
every sheet using that type, and deliberately never auto-written here -
mutating it would silently change every sibling sheet too) rather than
an instance parameter, and so on.

So auto-detection is a default, not the only option: scan_tree() takes
an optional `param_overrides` dict ({depth: parameter_name}) that pins a
specific parameter to a specific hierarchy level regardless of what
auto-detection would have guessed - this is what lets a user explicitly
confirm "Level 2 is the Mirror/Regular parameter" once, from
levels_info()'s list of the tree's actual levels and
list_sheet_parameter_names()'s list of candidates, rather than hoping
auto-detection got every level right. When a level has neither an
override nor a usable auto-detected guess, its classify_param stays
None and is simply skipped (the Sheet Number/Sheet Name substitution
still happens).
"""
import re

from Autodesk.Revit.DB import (
    FilteredElementCollector, BuiltInCategory, BuiltInParameter, ViewSheet,
    ElementId, StorageType, Transaction,
)

_TRAILING_DIGITS_RE = re.compile(r"^(.*?)(\d+)$")


def _eid(element_id):
    """Plain int from an ElementId, tolerant of both the current API
    (.Value, a 64-bit long) and the older one (.IntegerValue) - same
    normalization dee_transmit_service.py already relies on for safe
    ElementId dict keys/comparisons across Revit API versions."""
    if element_id is None:
        return -1
    try:
        return int(element_id.Value)
    except Exception:
        pass
    try:
        return int(element_id.IntegerValue)
    except Exception:
        return -1

_NOISE_PARAM_NAMES = set([
    "Sheet Number", "Sheet Name", "Sheet Issue Date", "Approved By",
    "Checked By", "Designed By", "Drawn By",
])


def _sheet_name(sheet):
    try:
        name = sheet.Name
        if name:
            return name
    except Exception:
        pass
    try:
        p = sheet.get_Parameter(BuiltInParameter.SHEET_NAME)
        if p is not None:
            v = p.AsString()
            if v:
                return v
    except Exception:
        pass
    return ""


def _param_value_str(p):
    try:
        if p.StorageType == StorageType.String:
            v = p.AsString()
        else:
            v = p.AsValueString()
        return v if v is not None else ""
    except Exception:
        return ""


def _set_param_value(param, value):
    try:
        st = param.StorageType
        if st == StorageType.Integer:
            param.Set(int(value))
        elif st == StorageType.Double:
            param.Set(float(value))
        else:
            param.Set(value)
    except Exception:
        try:
            param.Set(value)
        except Exception:
            pass


def _placed_titleblock(doc, sheet):
    """The single Title Block instance placed on `sheet`, or None - used
    sparingly (duplicate_group/bulk_add_sheets, once per operation) since
    it is one collector call per sheet. Bulk detection work instead uses
    _titleblock_instances_by_sheet, one collector call for the whole
    project."""
    try:
        tbs = list(FilteredElementCollector(doc, sheet.Id)
                   .OfCategory(BuiltInCategory.OST_TitleBlocks)
                   .WhereElementIsNotElementType())
        if tbs:
            return tbs[0]
    except Exception:
        pass
    return None


def _existing_titleblock_type_id(doc, sheet):
    tb = _placed_titleblock(doc, sheet)
    if tb is None:
        return None
    try:
        return tb.GetTypeId()
    except Exception:
        return None


def _titleblock_instances_by_sheet(doc):
    """{_eid(sheet.Id): title_block_instance} for every sheet that has
    one placed - a single collector call covering the whole project,
    used by scan_tree/list_sheet_parameter_names so detecting the
    classification parameter for hundreds of sheets doesn't cost one
    collector call each. Keyed by plain int (_eid) rather than the
    ElementId object itself, sidestepping any cross-API-version
    hashing/equality quirks."""
    result = {}
    try:
        for tb in (FilteredElementCollector(doc)
                   .OfCategory(BuiltInCategory.OST_TitleBlocks)
                   .WhereElementIsNotElementType()):
            try:
                owner = _eid(tb.OwnerViewId)
                if owner >= 0 and owner not in result:
                    result[owner] = tb
            except Exception:
                continue
    except Exception:
        pass
    return result


def _iter_param_names_and_values(sheet, tb):
    """Yields (name, value_str) for every instance parameter on `sheet`
    and, if given, on its placed Title Block instance `tb` too -
    Browser Organization can group sheets by a parameter that actually
    lives on the Title Block rather than the Sheet itself, and from the
    tree alone there's no way to tell which, so both are always
    searched together for both detection and the Configure Parameters
    dialog's candidate list."""
    for source in (sheet, tb):
        if source is None:
            continue
        try:
            for p in source.Parameters:
                try:
                    name = p.Definition.Name
                except Exception:
                    continue
                if not name or name in _NOISE_PARAM_NAMES:
                    continue
                yield name, _param_value_str(p)
        except Exception:
            continue


def _find_classifying_parameter(sheets, label, tb_by_sheet):
    """The parameter name (Sheet instance OR its placed Title Block
    instance - see _iter_param_names_and_values) whose displayed value
    equals `label` on EVERY sheet in this group - intersected across all
    of them so a parameter that only coincidentally matches on one sheet
    can't win. tb_by_sheet: _titleblock_instances_by_sheet(doc)'s result."""
    if not sheets or not label:
        return None
    common = None
    for sheet in sheets:
        tb = tb_by_sheet.get(_eid(sheet.Id))
        matches = set(name for name, value in _iter_param_names_and_values(sheet, tb) if value == label)
        common = matches if common is None else (common & matches)
        if not common:
            return None
    return sorted(common)[0] if common else None


def _set_param_on_sheet_or_titleblock(doc, sheet, param_name, value):
    """Sets `param_name` to `value` on `sheet` itself if it has that
    parameter; otherwise tries its placed Title Block instance - mirrors
    _find_classifying_parameter's search scope exactly, so whichever
    element a value was detected (or configured) from is also where it
    gets written on a newly created sheet. Never touches a Title Block
    TYPE parameter - only the instance placed on this specific sheet, so
    one sheet's values can never bleed into a sibling sheet that happens
    to share the same Title Block type."""
    try:
        p = sheet.LookupParameter(param_name)
        if p is not None and not p.IsReadOnly:
            _set_param_value(p, value)
            return
    except Exception:
        pass
    tb = _placed_titleblock(doc, sheet)
    if tb is None:
        return
    try:
        p = tb.LookupParameter(param_name)
        if p is not None and not p.IsReadOnly:
            _set_param_value(p, value)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Tree model
# --------------------------------------------------------------------------
class SmartSheetNode(object):
    """One node in the SmartSheet tree. A folder node has `children` and
    `sheet is None`; a leaf node has `sheet` set and no children. `label`
    is the folder name (for folders) or the sheet's Name (for leaves) -
    `display_name` is what the TreeView actually shows. `parent` lets any
    node found by the TreeView walk straight back up to the root without
    a separate tree search - see ancestor_chain()."""

    def __init__(self, label, sheet=None, parent=None):
        self.label = label or ""
        self.sheet = sheet
        self.parent = parent
        self.children = []
        self.classify_param = None
        self.group_sheets = []

    @property
    def display_name(self):
        if self.sheet is not None:
            number = ""
            try:
                number = self.sheet.SheetNumber or ""
            except Exception:
                pass
            return "{0} - {1}".format(number, self.label) if number else self.label
        return self.label

    @property
    def is_group(self):
        """A bottom-level folder: holds sheets directly, no sub-folders -
        the only kind of node Duplicate Group can act on."""
        return (self.sheet is None and bool(self.children) and
                all(c.sheet is not None for c in self.children))

    @property
    def is_folder(self):
        return self.sheet is None


def _get_browser_organization(doc):
    try:
        from Autodesk.Revit.DB import BrowserOrganization
        return BrowserOrganization.GetCurrentBrowserOrganizationForSheets(doc)
    except Exception:
        return None


def _folder_path(bo, sheet):
    if bo is None:
        return []
    try:
        items = bo.GetFolderItems(sheet.Id)
        names = []
        for fi in items:
            try:
                name = fi.Name
            except Exception:
                name = None
            names.append(name if name else "")
        return names
    except Exception:
        return []


def scan_tree(doc, param_overrides=None):
    """Builds the SmartSheet tree for every real (non-placeholder) sheet
    in the project, mirroring the project's current Sheet Browser
    Organization. param_overrides, if given, is {depth: parameter_name}
    (depth 1 = the first real classification level, i.e. root's direct
    children) - see levels_info() and the module docstring. Returns the
    single root SmartSheetNode."""
    sheets = []
    for sheet in FilteredElementCollector(doc).OfClass(ViewSheet):
        try:
            if getattr(sheet, "IsPlaceholder", False):
                continue
            sheets.append(sheet)
        except Exception:
            continue

    bo = _get_browser_organization(doc)
    root_label = u"All Sheets"
    try:
        if bo is not None and bo.Name:
            root_label = bo.Name
    except Exception:
        pass
    root = SmartSheetNode(root_label)

    entries = [(_folder_path(bo, sheet), sheet) for sheet in sheets]
    entries.sort(key=lambda e: (e[0], e[1].SheetNumber or ""))

    folder_cache = {(): root}
    for path, sheet in entries:
        parent = root
        for depth, label in enumerate(path):
            key = tuple(path[:depth + 1])
            node = folder_cache.get(key)
            if node is None:
                node = SmartSheetNode(label, parent=parent)
                parent.children.append(node)
                folder_cache[key] = node
            parent = node
        parent.group_sheets.append(sheet)
        parent.children.append(SmartSheetNode(_sheet_name(sheet), sheet=sheet, parent=parent))

    tb_by_sheet = _titleblock_instances_by_sheet(doc)
    _assign_classify_params(root, 0, param_overrides or {}, tb_by_sheet)
    return root


def _collect_sheets(node, out=None):
    """Every ViewSheet nested anywhere under `node` - not just its direct
    children. A folder several levels above a group (e.g. 'Villa Type')
    has no sheets of its own in group_sheets (only a bottom-level group
    does), but its classification parameter still needs to be identified
    from the full set of sheets that actually live under it."""
    if out is None:
        out = []
    if node.sheet is not None:
        out.append(node.sheet)
    for child in node.children:
        _collect_sheets(child, out)
    return out


def _assign_classify_params(node, depth, overrides, tb_by_sheet):
    """Identifies classify_param for EVERY folder level, not just the
    bottom-level group - a new sheet needs ALL of them set (Villa Type,
    Mirror/Regular, Plot Number, ...) to land in the exact same Sheet
    Browser folder as its siblings, not just the deepest one. `depth` is
    this node's own level (root's direct children are depth 1); a node's
    sheet-leaf children share its own depth rather than incrementing,
    since a leaf isn't a classification level. `overrides` pins a
    specific parameter name to a given depth, bypassing auto-detection
    entirely - the fix for a level whose value is blank everywhere
    ('???' in the tree) and so can never be auto-detected, or where
    auto-detection simply guessed wrong."""
    for child in node.children:
        child_depth = depth + 1 if child.is_folder else depth
        _assign_classify_params(child, child_depth, overrides, tb_by_sheet)
    if node.is_folder and node.label:
        override = overrides.get(depth)
        node.classify_param = override if override else _find_classifying_parameter(
            _collect_sheets(node), node.label, tb_by_sheet)


def ancestor_chain(node):
    """[(folder_label, classify_param_or_None), ...] from the root down
    to `node` itself (inclusive, if node is a folder) or down to its
    parent folder (if node is a sheet leaf) - every parameter a new
    sheet needs set to land in exactly this spot in the Sheet Browser.
    Walks `parent` pointers rather than searching the tree, so it works
    directly on whatever node the TreeView hands back."""
    start = node if node.is_folder else node.parent
    chain = []
    n = start
    while n is not None:
        if n.label:
            chain.append((n.label, n.classify_param))
        n = n.parent
    chain.reverse()
    return chain


def levels_info(root):
    """[(depth, sample_label_or_None, classify_param_or_None), ...] for
    every real classification level found in the tree (depth 1 = root's
    direct children, depth 2 the level below that, and so on - root
    itself, depth 0, is just the Browser Organization's rule-set name,
    never a configurable level). One entry per depth, with a sample
    label for context (e.g. depth 1 -> '4 Bedroom Villa') and whatever
    classify_param currently resolves to there (auto-detected or
    already overridden) - this is exactly what the Configure
    Classification Parameters dialog needs to show the user, and what
    list_sheet_parameter_names() supplies the dropdown choices for.
    Prefers a non-'???' sample where one exists, since '???' doesn't
    tell the user anything about what the level actually represents."""
    best = {}
    def walk(node, depth):
        if node.is_folder and depth > 0 and node.label:
            placeholder = (node.label == u"???")
            current = best.get(depth)
            if current is None or (current[2] and not placeholder):
                best[depth] = (node.label, node.classify_param, placeholder)
        for child in node.children:
            walk(child, depth + 1 if child.is_folder else depth)
    walk(root, 0)
    return [(depth, label, param) for depth, (label, param, _placeholder) in sorted(best.items())]


def list_sheet_parameter_names(doc, max_sheets=60):
    """Every instance parameter name found on a sample of the project's
    real sheets AND their placed Title Block instances (same
    noise-filtering as the Sheet Renamer tab's own parameter picker) -
    the candidate list for the Configure Classification Parameters
    dialog's dropdowns. Title Block names are included because a level's
    actual classification parameter may live there rather than on the
    Sheet (see the module docstring) - _set_param_on_sheet_or_titleblock
    tries both when writing, so either kind of name works as a choice."""
    names = set()
    tb_by_sheet = _titleblock_instances_by_sheet(doc)
    count = 0
    for sheet in FilteredElementCollector(doc).OfClass(ViewSheet):
        try:
            if getattr(sheet, "IsPlaceholder", False):
                continue
        except Exception:
            pass
        count += 1
        if count > max_sheets:
            break
        tb = tb_by_sheet.get(_eid(sheet.Id))
        for name, _value in _iter_param_names_and_values(sheet, tb):
            names.add(name)
    return sorted(names)


def containing_group(node):
    """The bottom-level group `node` itself is, or that its parent folder
    is, or None if neither qualifies (e.g. a higher folder like 'Mirror'
    with no sheets directly in it) - used to know whose Sheet Number
    sequence to continue when suggesting a new number."""
    if node.is_group:
        return node
    if node.sheet is not None and node.parent is not None and node.parent.is_group:
        return node.parent
    return None


def suggest_titleblock_type_id(doc, node):
    """The ElementId of the Title Block type an existing sheet in `node`'s
    own group (or node's nearest containing group) already uses - a
    sensible default for the Add Sheet dialog's Title Block dropdown.
    Any classification data that lives on the Title Block TYPE (rather
    than an instance parameter _set_param_on_sheet_or_titleblock can
    write) can only ever reach a new sheet by it being assigned the SAME
    type as its siblings, since type parameters are never written here -
    so the single-sheet Add flow needs this default just as much as
    Duplicate Group/Add Sheet(s) to All Groups, which already reuse a
    template sheet's Title Block automatically. Returns None if there's
    no group context or no sheet in it has a Title Block placed."""
    group = node if node.is_group else containing_group(node)
    if group is None or not group.group_sheets:
        return None
    return _existing_titleblock_type_id(doc, group.group_sheets[-1])


def count_sheets(node):
    total = 1 if node.sheet is not None else 0
    for child in node.children:
        total += count_sheets(child)
    return total


def count_groups(node):
    total = 1 if node.is_group else 0
    if not node.is_group:
        for child in node.children:
            total += count_groups(child)
    return total


# --------------------------------------------------------------------------
# Sequence detection - generic to any numbering convention
# --------------------------------------------------------------------------
def _split_trailing_number(number):
    """(prefix, digits) split on the TRAILING run of digits in a Sheet
    Number, e.g. '6853597-01' -> ('6853597-', '01'), 'A101' -> ('A', '101').
    None if the number has no trailing digits at all - nothing to safely
    continue a sequence from. Deliberately generic: no assumption about a
    dash or any other specific delimiter, so it fits any project's own
    numbering convention."""
    if not number:
        return None
    m = _TRAILING_DIGITS_RE.match(number)
    if not m:
        return None
    return m.group(1), m.group(2)


def detect_group_sequence(node):
    """Looks at every sheet Number already in this group and finds the
    one common prefix + zero-padded trailing number they all share, e.g.
    '6853597-01'..'6853597-06' -> prefix '6853597-', pad width 2, next
    sequence value 7. Returns (prefix, pad_width, next_n), or None when
    the group has no sheets yet or their numbers don't share one common
    prefix (nothing safe to continue)."""
    prefixes = set()
    max_n = 0
    pad_width = 0
    found_any = False
    for sheet in node.group_sheets:
        try:
            number = sheet.SheetNumber or ""
        except Exception:
            continue
        parsed = _split_trailing_number(number)
        if parsed is None:
            return None
        prefix, digits = parsed
        prefixes.add(prefix)
        found_any = True
        max_n = max(max_n, int(digits))
        pad_width = max(pad_width, len(digits))
    if not found_any or len(prefixes) != 1:
        return None
    return next(iter(prefixes)), pad_width, max_n + 1


def next_sequence_numbers(node, count):
    """`count` consecutive new Sheet Numbers continuing this group's own
    detected sequence (e.g. 07, 08 after 01..06), or None if no common
    pattern could be detected in the group's current sheets."""
    seq = detect_group_sequence(node)
    if seq is None:
        return None
    prefix, pad_width, start_n = seq
    return [u"{0}{1}".format(prefix, str(start_n + i).zfill(pad_width)) for i in range(count)]


def all_groups(node, out=None):
    """Every bottom-level duplicable group under `node` (node included),
    depth-first - the set Add Sheets to All Groups operates on."""
    if out is None:
        out = []
    if node.is_group:
        out.append(node)
    else:
        for child in node.children:
            all_groups(child, out)
    return out


# --------------------------------------------------------------------------
# Duplicate Group
# --------------------------------------------------------------------------
def duplicate_group(doc, node, new_value, ancestor_chain=None):
    """node must be a bottom-level group (node.is_group). Creates one new
    sheet per sheet in node.group_sheets, substituting node.label with
    new_value wherever it appears in Sheet Number/Sheet Name, carrying
    over each template sheet's own Title Block, setting node.classify_param
    (if identified) to new_value, and - crucially - also setting every
    parameter in `ancestor_chain` (the folder levels ABOVE this group,
    e.g. Villa Type, Mirror/Regular) to their UNCHANGED label, since a
    freshly created sheet starts with blank parameters and would
    otherwise show up unclassified ('???') in the Sheet Browser instead
    of alongside the group it was duplicated from. Pass the result of
    ancestor_chain(node.parent) (or [] if node has no parent folder).
    Returns a list of (ok, label, detail) tuples, same shape as the rest
    of DeeSheet's results."""
    old_value = node.label
    ancestor_chain = ancestor_chain or []
    results = []
    if not old_value:
        return [(False, "-", "This group has no identifying value to substitute.")]
    if not new_value or new_value == old_value:
        return [(False, "-", "Enter a new value different from '{0}'.".format(old_value))]

    existing_numbers = set()
    for vs in FilteredElementCollector(doc).OfClass(ViewSheet):
        try:
            existing_numbers.add(vs.SheetNumber)
        except Exception:
            continue

    t = Transaction(doc, "DeeSheet - SmartSheet Duplicate Group")
    t.Start()
    try:
        for sheet in node.group_sheets:
            try:
                old_number = sheet.SheetNumber or ""
                old_name = _sheet_name(sheet)
                if old_value not in old_number and old_value not in old_name:
                    results.append((False, old_number,
                                     "Skipped - '{0}' not found in Number or Name".format(old_value)))
                    continue
                new_number = old_number.replace(old_value, new_value)
                new_name = old_name.replace(old_value, new_value)
                if new_number in existing_numbers:
                    results.append((False, new_number, "Skipped - Sheet Number already exists"))
                    continue
                tb_id = _existing_titleblock_type_id(doc, sheet)
                new_sheet = ViewSheet.Create(doc, tb_id if tb_id is not None else ElementId.InvalidElementId)
                new_sheet.SheetNumber = new_number
                new_sheet.Name = new_name
                for label, param_name in ancestor_chain:
                    if param_name:
                        _set_param_on_sheet_or_titleblock(doc, new_sheet, param_name, label)
                if node.classify_param:
                    _set_param_on_sheet_or_titleblock(doc, new_sheet, node.classify_param, new_value)
                existing_numbers.add(new_number)
                results.append((True, new_number, "Created '{0}'".format(new_name)))
            except Exception as e:
                results.append((False, sheet.SheetNumber, "FAILED: {0}".format(e)))
        t.Commit()
    except Exception as e:
        t.RollBack()
        results.append((False, "-", "Aborted: {0}".format(e)))
    return results


# --------------------------------------------------------------------------
# Add Sheet
# --------------------------------------------------------------------------
def add_sheet(doc, chain, number, name, titleblock_id):
    """chain: as returned by ancestor_chain() for whichever node was
    clicked. Creates one sheet, then sets every (param_name, value) pair
    in chain that resolved to a real, writable parameter. Returns
    (ok, label, detail)."""
    number = (number or "").strip()
    name = (name or "").strip()
    if not number or not name:
        return False, "{0} / {1}".format(number, name), "Enter both a Sheet Number and a Sheet Name."

    for vs in FilteredElementCollector(doc).OfClass(ViewSheet):
        try:
            if vs.SheetNumber == number:
                return False, number, "Sheet Number already exists"
        except Exception:
            continue

    t = Transaction(doc, "DeeSheet - SmartSheet Add Sheet")
    t.Start()
    try:
        sheet = ViewSheet.Create(doc, titleblock_id if titleblock_id is not None else ElementId.InvalidElementId)
        sheet.SheetNumber = number
        sheet.Name = name
        for label, param_name in chain:
            if param_name:
                _set_param_on_sheet_or_titleblock(doc, sheet, param_name, label)
        t.Commit()
        return True, number, "Created '{0}'".format(name)
    except Exception as e:
        t.RollBack()
        return False, number, "FAILED: {0}".format(e)


# --------------------------------------------------------------------------
# Add Sheet(s) to All Groups
# --------------------------------------------------------------------------
def bulk_add_sheets(doc, groups, sheet_names):
    """Creates len(sheet_names) new sheets in EACH group in `groups`,
    continuing that group's own detected Sheet Number sequence
    independently - a group at 01..06 gets 07/08..., a different group at
    01..04 gets 05/06..., each purely from its own existing numbers (see
    next_sequence_numbers). Reuses each group's own last sheet's Title
    Block, and sets every classification parameter from ancestor_chain(node)
    (the group's own level AND every folder above it - Villa Type,
    Mirror/Regular, Plot Number, ...) so the new sheets land in exactly
    the same Sheet Browser spot as their siblings instead of showing up
    unclassified. A group whose sheets don't share one common numbering
    pattern is skipped and reported, never guessed at. Returns a list of
    (ok, label, detail) tuples."""
    results = []
    existing_numbers = set()
    for vs in FilteredElementCollector(doc).OfClass(ViewSheet):
        try:
            existing_numbers.add(vs.SheetNumber)
        except Exception:
            continue

    t = Transaction(doc, "DeeSheet - SmartSheet Add Sheets to All Groups")
    t.Start()
    try:
        for node in groups:
            numbers = next_sequence_numbers(node, len(sheet_names))
            if numbers is None:
                results.append((False, node.label,
                                 "Skipped - sheets in this group don't share one common Number pattern"))
                continue
            template_sheet = node.group_sheets[-1] if node.group_sheets else None
            tb_id = _existing_titleblock_type_id(doc, template_sheet) if template_sheet is not None else None
            chain = ancestor_chain(node)
            for number, name in zip(numbers, sheet_names):
                try:
                    if number in existing_numbers:
                        results.append((False, number, "Skipped - Sheet Number already exists"))
                        continue
                    new_sheet = ViewSheet.Create(doc, tb_id if tb_id is not None else ElementId.InvalidElementId)
                    new_sheet.SheetNumber = number
                    new_sheet.Name = name
                    for label, param_name in chain:
                        if param_name:
                            _set_param_on_sheet_or_titleblock(doc, new_sheet, param_name, label)
                    existing_numbers.add(number)
                    results.append((True, number, "Created '{0}' in group '{1}'".format(name, node.label)))
                except Exception as e:
                    results.append((False, number, "FAILED: {0}".format(e)))
        t.Commit()
    except Exception as e:
        t.RollBack()
        results.append((False, "-", "Aborted: {0}".format(e)))
    return results
