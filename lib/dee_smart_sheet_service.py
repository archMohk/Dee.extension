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
writes into) is found empirically rather than read off the Browser
Organization's rules directly (no public API exposes those rules) - see
_find_classifying_parameter: the parameter whose displayed value equals
the folder's label on every sheet nested under it. This runs for EVERY
folder level, not just the bottom-level group - a new sheet needs its
Villa Type, Mirror/Regular AND Plot Number (or whatever levels a given
project uses) all set, not just the deepest one, or it shows up
unclassified ('???') in the Project Browser instead of alongside its
siblings. When no single parameter matches on every sheet at a given
level, that level's classify_param stays None and is simply skipped
(the Sheet Number/Sheet Name substitution still happens).
"""
import re

from Autodesk.Revit.DB import (
    FilteredElementCollector, BuiltInCategory, BuiltInParameter, ViewSheet,
    ElementId, StorageType, Transaction,
)

_TRAILING_DIGITS_RE = re.compile(r"^(.*?)(\d+)$")

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


def _existing_titleblock_type_id(doc, sheet):
    try:
        tbs = list(FilteredElementCollector(doc, sheet.Id)
                   .OfCategory(BuiltInCategory.OST_TitleBlocks)
                   .WhereElementIsNotElementType())
        if tbs:
            return tbs[0].GetTypeId()
    except Exception:
        pass
    return None


def _find_classifying_parameter(sheets, label):
    """The instance parameter name whose displayed value equals `label`
    on EVERY sheet in this group - intersected across all of them so a
    parameter that only coincidentally matches on one sheet can't win."""
    if not sheets or not label:
        return None
    common = None
    for sheet in sheets:
        matches = set()
        try:
            for p in sheet.Parameters:
                try:
                    name = p.Definition.Name
                except Exception:
                    continue
                if not name or name in _NOISE_PARAM_NAMES:
                    continue
                if _param_value_str(p) == label:
                    matches.add(name)
        except Exception:
            continue
        common = matches if common is None else (common & matches)
        if not common:
            return None
    return sorted(common)[0] if common else None


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


def scan_tree(doc):
    """Builds the SmartSheet tree for every real (non-placeholder) sheet
    in the project, mirroring the project's current Sheet Browser
    Organization. Returns the single root SmartSheetNode."""
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

    _assign_classify_params(root)
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


def _assign_classify_params(node):
    """Identifies classify_param for EVERY folder level, not just the
    bottom-level group - a new sheet needs ALL of them set (Villa Type,
    Mirror/Regular, Plot Number, ...) to land in the exact same Sheet
    Browser folder as its siblings, not just the deepest one."""
    for child in node.children:
        _assign_classify_params(child)
    if node.is_folder and node.label:
        node.classify_param = _find_classifying_parameter(_collect_sheets(node), node.label)


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
                    if not param_name:
                        continue
                    try:
                        p = new_sheet.LookupParameter(param_name)
                        if p is not None and not p.IsReadOnly:
                            _set_param_value(p, label)
                    except Exception:
                        pass
                if node.classify_param:
                    try:
                        p = new_sheet.LookupParameter(node.classify_param)
                        if p is not None and not p.IsReadOnly:
                            _set_param_value(p, new_value)
                    except Exception:
                        pass
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
            if not param_name:
                continue
            try:
                p = sheet.LookupParameter(param_name)
                if p is not None and not p.IsReadOnly:
                    _set_param_value(p, label)
            except Exception:
                pass
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
                        if not param_name:
                            continue
                        try:
                            p = new_sheet.LookupParameter(param_name)
                            if p is not None and not p.IsReadOnly:
                                _set_param_value(p, label)
                        except Exception:
                            pass
                    existing_numbers.add(number)
                    results.append((True, number, "Created '{0}' in group '{1}'".format(name, node.label)))
                except Exception as e:
                    results.append((False, number, "FAILED: {0}".format(e)))
        t.Commit()
    except Exception as e:
        t.RollBack()
        results.append((False, "-", "Aborted: {0}".format(e)))
    return results
