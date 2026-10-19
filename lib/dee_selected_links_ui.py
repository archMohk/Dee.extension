# -*- coding: utf-8 -*-
"""
dee_selected_links_ui
Shared "Selected Links" tab for every linking tool: a per-match
Link Type/Attachment override grid, read at Run time by each tool's own
_effective_link_options-equivalent (falls back to that tool's global
Link Setup controls for any match with no override). Originally a
hand-built StackPanel of rows in DeeInitiator (unreadable headers/combos
- no Foreground ever set on them) with no multi-select at all; this
module replaces that with a real DataGrid (SelectionMode="Extended"),
matching the plain-POCO + wholesale "ItemsSource = None; ItemsSource =
<new list>" convention this repo already uses for link_matches_grid/
report_grid - no INotifyPropertyChanged anywhere in this codebase, and
no need to introduce it here.

self._match_options stays owned by the calling tool - it only ever
holds OVERRIDES, shaped {(source, target): {"placement_label": str,
"attachment_label": str}}. A pair absent from it (or with a key
missing) falls back to that tool's own global default.
"""


class SelectedLinkRow(object):
    """One grid row, plain POCO - rebuilt wholesale on every refresh,
    never mutated in place."""
    def __init__(self, source, target, placement_label, attachment_label):
        self.source = source
        self.target = target
        self.match_label = u"{0}  ->  {1}".format(source, target)
        self.placement_label = placement_label
        self.attachment_label = attachment_label


def build_rows(matches, match_options, default_placement_label, default_attachment_label):
    """[(s,t), ...] + {(s,t): {...}} -> [SelectedLinkRow, ...], same
    override-falls-back-to-default resolution every tool's own
    _effective_link_options already applies at Run time."""
    rows = []
    for source, target in matches:
        override = match_options.get((source, target), {}) or {}
        placement_label = override.get("placement_label") or default_placement_label
        attachment_label = override.get("attachment_label") or default_attachment_label
        rows.append(SelectedLinkRow(source, target, placement_label, attachment_label))
    return rows


def refresh_grid(grid, matches, match_options, default_placement_label, default_attachment_label):
    """The one approved way to force the DataGrid to repaint, per this
    repo's existing convention (never mutate ItemsSource's list in
    place)."""
    grid.ItemsSource = None
    grid.ItemsSource = build_rows(matches, match_options, default_placement_label, default_attachment_label)


def row_edit_committed(grid, args, match_options, on_changed):
    """Wired as the grid's RowEditEnding. Reading args.Row.Item before
    the grid finishes committing the edit throws - the same
    Dispatcher.BeginInvoke deferral already used by this repo's other
    RowEditEnding handlers (DeeQs/DeeSheet) is required here too, so this
    function assumes it is itself called from inside that deferred
    callback (see each tool's own *_row_edit_ending wrapper)."""
    try:
        row = args.Row.Item
    except Exception:
        return
    if row is None or not getattr(row, "source", None):
        return
    pair = (row.source, row.target)
    entry = match_options.setdefault(pair, {})
    entry["placement_label"] = row.placement_label
    entry["attachment_label"] = row.attachment_label
    if on_changed is not None:
        try:
            on_changed()
        except Exception:
            pass


def apply_to_selection(grid, match_options, placement_label=None, attachment_label=None, on_changed=None):
    """Bulk-edit entry point for a "Set <field> for selected" + Apply
    control pair. Snapshots SelectedItems FIRST, since this function has
    no matches/default-label context of its own to call refresh_grid()
    with - the caller's on_changed is expected to do that repaint itself
    (e.g. by calling refresh_grid again), same as every other "matches
    changed" hook in these tools. Writes the given override(s) into
    match_options for every selected row's (source, target), then calls
    on_changed() exactly once, not once per row."""
    rows = list(grid.SelectedItems)
    if not rows:
        return 0
    for row in rows:
        pair = (row.source, row.target)
        entry = match_options.setdefault(pair, {})
        if placement_label:
            entry["placement_label"] = placement_label
        if attachment_label:
            entry["attachment_label"] = attachment_label
    if on_changed is not None:
        try:
            on_changed()
        except Exception:
            pass
    return len(rows)
