# -*- coding: utf-8 -*-
"""
dee_matchset_ui
Shared "Matrix" tab UI for every linking tool built around an arbitrary
many-to-many match list (self._matches = [(source, target), ...]):
DeeInitiator, DeeMAPLink, and DeeSuperLINK. Before this module existed,
DeeMAPLink's own Matrix tab was an almost line-for-line hand copy of
DeeInitiator's - this is the one copy all three now call, each supplying
its own data via a Grid control reference plus small callbacks, since
none of them share a data model (DeeInitiator: per-party
self._link_files; DeeMAPLink/DeeSuperLINK: a single self._all_items
pool) - only the (source, target) match-list shape is common.

Cells are plain Border/TextBlock placed into a Grid's dynamically-added
rows/columns, never a DataGrid - matches every linking tool's own prior
choice (a DataGrid's column set isn't meant to be regenerated per a
runtime-variable file count).

Row/column axes are independent lists (names_rows / names_cols) so a
caller can filter each one separately (e.g. one dropdown per axis
instead of one shared filter) - "diagonal" (a file cannot be linked
into itself) is therefore computed by NAME EQUALITY (row_name ==
col_name), never by matching index, since the two axes can hold
different subsets or orders of the same underlying pool.

Separators are a purely visual grouping aid the user adds themselves
(right-click a header -> Insert/Remove separator here) - stored as a
sorted list of insertion INDICES into names_rows/names_cols on the
shared MatrixState, not as literal persisted Grid rows, since the whole
grid is already fully rebuilt on every filter/search change (the
existing cost model this module inherits unchanged) - adding/removing
a separator just re-runs that same rebuild. A caller that wants
separators remembered across Save Set/Load Set persists
state.row_separators/state.col_separators itself (see
dee_matchset_io.separators_to_json/separators_from_json) - this module
only ever holds them in memory.

Shift-click range selection: MatrixState.last_clicked remembers the
most recently clicked cell. A plain click toggles one pair. A
shift-click computes the rectangle between the last-clicked cell and
the one just clicked (by row/col INDEX into the current names_rows/
names_cols - not by name, since a rectangle is a positional concept),
and applies the just-clicked cell's own resulting new state to every
non-diagonal pair in that rectangle in a single on_toggle_range(pairs,
new_value) call - never once per cell - so a large shift-selected
rectangle costs exactly one "matches changed" refresh, not many.
"""
import clr
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
from System.Windows import Thickness, HorizontalAlignment, VerticalAlignment, TextTrimming, GridLength
from System.Windows.Controls import (Grid, RowDefinition, ColumnDefinition, Border, TextBlock,
                                      ContextMenu, MenuItem)
from System.Windows.Media import SolidColorBrush, Color, Brushes, RotateTransform
from System.Windows.Input import Cursors, Keyboard, ModifierKeys

import dee_maplink_service as dms

WIRE_BRUSH = SolidColorBrush(Color.FromRgb(0xF2, 0x99, 0x4D))
ROW_BG = SolidColorBrush(Color.FromRgb(0x2B, 0x2B, 0x2B))
ROW_ARMED = SolidColorBrush(Color.FromRgb(0x4A, 0x3A, 0x22))
SEPARATOR_BRUSH = SolidColorBrush(Color.FromRgb(0xF2, 0x99, 0x4D))
_DIAGONAL_BG = SolidColorBrush(Color.FromRgb(0x3A, 0x3A, 0x3A))
_CELL_SIZE = 26
_SEPARATOR_SIZE = 6
_HEADER_ROW_HEIGHT = 140
_HEADER_COL_WIDTH = 190


class MatrixState(object):
    """One instance per tool, created once and kept on self (e.g.
    self._matrix = MatrixState()). Everything on it is plain data -
    no WPF objects except the live `cells` Border references needed for
    refresh_ticks()."""
    def __init__(self):
        self.built = False
        self.cells = {}            # (row_name, col_name) -> Border, non-diagonal only
        self.names_rows = []
        self.names_cols = []
        self.last_clicked = None   # (row_name, col_name), shift-select anchor
        self.row_separators = []   # sorted insertion indices into names_rows
        self.col_separators = []   # sorted insertion indices into names_cols


def filtered_names(all_names, query, filter_value, name_to_filter_value):
    """Sorted, search-filtered, optionally filter_value-filtered name
    list - shared replacement for the identical loop every tool's own
    _matrix_build used to hand-roll. filter_value=None (an "(all)"
    selection) means no filtering; name_to_filter_value(name) -> label
    is only ever called when filter_value is not None. Called once per
    axis per rebuild, with independent filter_value/name_to_filter_value
    arguments, so the row axis and column axis can be filtered
    differently."""
    names = []
    for name in sorted(all_names):
        if not dms.matches_search(name, query):
            continue
        if filter_value and name_to_filter_value and name_to_filter_value(name) != filter_value:
            continue
        names.append(name)
    return names


def add_row_separator(state, before_index):
    if before_index not in state.row_separators:
        state.row_separators.append(before_index)
        state.row_separators.sort()


def remove_row_separator(state, index):
    if index in state.row_separators:
        state.row_separators.remove(index)


def add_col_separator(state, before_index):
    if before_index not in state.col_separators:
        state.col_separators.append(before_index)
        state.col_separators.sort()


def remove_col_separator(state, index):
    if index in state.col_separators:
        state.col_separators.remove(index)


def _default_row_header(name):
    text = TextBlock()
    text.Text = name
    text.Foreground = Brushes.WhiteSmoke
    text.FontSize = 10
    text.TextTrimming = TextTrimming.CharacterEllipsis
    text.VerticalAlignment = VerticalAlignment.Center
    text.Margin = Thickness(4, 0, 6, 0)
    text.ToolTip = name
    return text


def _default_col_header(name):
    # Vertical text (rotated -90deg) rather than truncated horizontal
    # text, so the full file name is readable without a tooltip.
    text = TextBlock()
    text.Text = name
    text.Foreground = Brushes.WhiteSmoke
    text.FontSize = 9
    text.VerticalAlignment = VerticalAlignment.Bottom
    text.HorizontalAlignment = HorizontalAlignment.Left
    text.Margin = Thickness(2, 4, 2, 4)
    text.ToolTip = name
    text.LayoutTransform = RotateTransform(-90)

    border = Border()
    border.Child = text
    border.Background = ROW_BG
    border.BorderBrush = Brushes.Gray
    border.BorderThickness = Thickness(0, 0, 1, 1)
    border.VerticalAlignment = VerticalAlignment.Bottom
    border.HorizontalAlignment = HorizontalAlignment.Center
    return border


def _attach_separator_menu(element, on_insert, on_remove):
    menu = ContextMenu()
    insert_item = MenuItem()
    insert_item.Header = "Insert separator here"

    def _do_insert(sender, args):
        on_insert()
    insert_item.Click += _do_insert
    menu.Items.Add(insert_item)

    if on_remove is not None:
        remove_item = MenuItem()
        remove_item.Header = "Remove separator"

        def _do_remove(sender, args):
            on_remove()
        remove_item.Click += _do_remove
        menu.Items.Add(remove_item)

    element.ContextMenu = menu


def _build_axis(definitions, count, separators, make_definition, cell_size, sep_size):
    """Populates `definitions` (a Grid's RowDefinitions or
    ColumnDefinitions) with one definition per name plus one thin
    definition per separator. Returns (index_map, sep_grid_indices):
    index_map is {position: grid_index} (1-based - 0 is the header
    row/column) for every name's own cell definition; sep_grid_indices
    is the list of grid indices holding a separator's thin definition,
    used afterward to draw the visible divider bar across it."""
    index_map = {}
    sep_grid_indices = []
    grid_index = 1
    for i in range(count):
        if i in separators:
            definitions.Add(make_definition(sep_size))
            sep_grid_indices.append(grid_index)
            grid_index += 1
        definitions.Add(make_definition(cell_size))
        index_map[i] = grid_index
        grid_index += 1
    if count in separators:
        definitions.Add(make_definition(sep_size))
        sep_grid_indices.append(grid_index)
        grid_index += 1
    return index_map, sep_grid_indices


def _make_cell(state, row_name, col_name, is_matched, on_toggle_range):
    text = TextBlock()
    text.FontSize = 13
    text.HorizontalAlignment = HorizontalAlignment.Center
    text.VerticalAlignment = VerticalAlignment.Center
    text.IsHitTestVisible = False

    cell = Border()
    cell.BorderBrush = Brushes.Gray
    cell.BorderThickness = Thickness(0, 0, 1, 1)
    cell.Child = text

    if row_name == col_name:
        # A file cannot be linked into itself - disabled up front (never
        # wired to a click handler) rather than relying on the run
        # pipeline's own self-pair skip, so the user sees it's invalid
        # instead of clicking a cell that silently does nothing.
        cell.Background = _DIAGONAL_BG
        text.Text = u"—"
        text.Foreground = Brushes.Gray
        return cell, False

    cell.Background = ROW_BG
    cell.Cursor = Cursors.Hand
    cell.Tag = (row_name, col_name)
    cell.ToolTip = "{0}\nlinked into\n{1}".format(row_name, col_name)
    matched = is_matched(row_name, col_name)
    text.Text = u"✓" if matched else ""
    text.Foreground = WIRE_BRUSH
    if matched:
        cell.Background = ROW_ARMED

    def _handler(sender, args, _state=state, _is_matched=is_matched, _on_toggle=on_toggle_range):
        cell_mouse_down(_state, sender, args, _is_matched, _on_toggle)
    cell.MouseLeftButtonDown += _handler
    return cell, True


def build(grid, state, names_rows, names_cols, is_matched, on_toggle_range,
          row_header_factory=None, col_header_factory=None):
    """Clears and rebuilds `grid` as a len(names_cols) x len(names_rows)
    matrix (plus one thin extra Row/ColumnDefinition per separator).

    is_matched(row_name, col_name) -> bool - the tool's own self._matches
        lookup; this module never reads or writes self._matches itself.
    on_toggle_range(pairs, new_value) - called once per click/shift-click
        gesture with the full list of (row_name, col_name) pairs to set
        to `new_value`. The tool's callback is the ONLY thing that
        mutates self._matches/self._match_options and calls the tool's
        own post-change refresh hook (e.g. redrawing its Wire Map) - this
        module stays ignorant of what "matches changed" means downstream.
    row_header_factory(name)/col_header_factory(name) -> UIElement -
        default to a plain TextBlock / a -90-degree-rotated TextBlock in
        a Border, matching DeeInitiator's original convention.
    """
    row_header_factory = row_header_factory or _default_row_header
    col_header_factory = col_header_factory or _default_col_header

    state.names_rows = list(names_rows)
    state.names_cols = list(names_cols)
    # A separator past the current (possibly filtered/shrunk) name count
    # is simply dropped rather than erroring - it was placed against a
    # larger/different name list and reappears once the filter widens
    # back out, since Save Set persists these as plain indices (see
    # dee_matchset_io), never as literal Grid state.
    state.row_separators = sorted(s for s in state.row_separators if 0 <= s <= len(state.names_rows))
    state.col_separators = sorted(s for s in state.col_separators if 0 <= s <= len(state.names_cols))

    grid.Children.Clear()
    grid.RowDefinitions.Clear()
    grid.ColumnDefinitions.Clear()
    state.cells = {}

    if not state.names_rows or not state.names_cols:
        return

    grid.RowDefinitions.Add(RowDefinition(Height=GridLength(_HEADER_ROW_HEIGHT)))
    grid.ColumnDefinitions.Add(ColumnDefinition(Width=GridLength(_HEADER_COL_WIDTH)))

    row_index_map, row_sep_indices = _build_axis(
        grid.RowDefinitions, len(state.names_rows), state.row_separators,
        lambda size: RowDefinition(Height=GridLength(size)), _CELL_SIZE, _SEPARATOR_SIZE)
    col_index_map, col_sep_indices = _build_axis(
        grid.ColumnDefinitions, len(state.names_cols), state.col_separators,
        lambda size: ColumnDefinition(Width=GridLength(size)), _CELL_SIZE, _SEPARATOR_SIZE)

    total_rows = grid.RowDefinitions.Count
    total_cols = grid.ColumnDefinitions.Count

    def _rebuild():
        build(grid, state, state.names_rows, state.names_cols, is_matched, on_toggle_range,
              row_header_factory, col_header_factory)

    corner = TextBlock()
    Grid.SetRow(corner, 0)
    Grid.SetColumn(corner, 0)
    grid.Children.Add(corner)

    for j, col_name in enumerate(state.names_cols):
        header = col_header_factory(col_name)
        Grid.SetRow(header, 0)
        Grid.SetColumn(header, col_index_map[j])

        def _insert_col(j=j):
            add_col_separator(state, j)
            _rebuild()
        _remove_col = None
        if j in state.col_separators:
            def _remove_col(j=j):
                remove_col_separator(state, j)
                _rebuild()
        _attach_separator_menu(header, _insert_col, _remove_col)
        grid.Children.Add(header)

    for i, row_name in enumerate(state.names_rows):
        row_header = row_header_factory(row_name)
        Grid.SetRow(row_header, row_index_map[i])
        Grid.SetColumn(row_header, 0)

        def _insert_row(i=i):
            add_row_separator(state, i)
            _rebuild()
        _remove_row = None
        if i in state.row_separators:
            def _remove_row(i=i):
                remove_row_separator(state, i)
                _rebuild()
        _attach_separator_menu(row_header, _insert_row, _remove_row)
        grid.Children.Add(row_header)

        for j, col_name in enumerate(state.names_cols):
            cell, clickable = _make_cell(state, row_name, col_name, is_matched, on_toggle_range)
            Grid.SetRow(cell, row_index_map[i])
            Grid.SetColumn(cell, col_index_map[j])
            grid.Children.Add(cell)
            if clickable:
                state.cells[(row_name, col_name)] = cell

    for r in row_sep_indices:
        bar = Border()
        bar.Background = SEPARATOR_BRUSH
        Grid.SetRow(bar, r)
        Grid.SetColumn(bar, 0)
        Grid.SetColumnSpan(bar, total_cols)
        grid.Children.Add(bar)
    for c in col_sep_indices:
        bar = Border()
        bar.Background = SEPARATOR_BRUSH
        Grid.SetColumn(bar, c)
        Grid.SetRow(bar, 0)
        Grid.SetRowSpan(bar, total_rows)
        grid.Children.Add(bar)

    state.built = True


def refresh_ticks(state, is_matched):
    """Repaints every cell against the current matches without a full
    rebuild - used after an external change (e.g. a Wire Map edit)."""
    for pair, cell in state.cells.items():
        matched = is_matched(pair[0], pair[1])
        cell.Child.Text = u"✓" if matched else ""
        cell.Background = ROW_ARMED if matched else ROW_BG


def cell_mouse_down(state, sender, args, is_matched, on_toggle_range):
    """Wired (indirectly, via build()'s own closure) to every clickable
    cell's MouseLeftButtonDown. A plain click toggles just this one pair.
    A shift-click computes the rectangle between state.last_clicked and
    this cell (by index into the CURRENT names_rows/names_cols - a
    positional rectangle, same convention as Excel/Explorer shift-select)
    and applies this cell's own resulting new state to every non-diagonal
    pair in it, in one on_toggle_range call."""
    row_name, col_name = sender.Tag
    new_value = not is_matched(row_name, col_name)
    pairs = [(row_name, col_name)]

    shift = False
    try:
        shift = bool(Keyboard.Modifiers & ModifierKeys.Shift)
    except Exception:
        shift = False

    if shift and state.last_clicked:
        try:
            anchor_row, anchor_col = state.last_clicked
            r0 = state.names_rows.index(anchor_row)
            r1 = state.names_rows.index(row_name)
            c0 = state.names_cols.index(anchor_col)
            c1 = state.names_cols.index(col_name)
            if r0 > r1:
                r0, r1 = r1, r0
            if c0 > c1:
                c0, c1 = c1, c0
            row_range = state.names_rows[r0:r1 + 1]
            col_range = state.names_cols[c0:c1 + 1]
            pairs = [(r, c) for r in row_range for c in col_range if r != c]
        except Exception:
            pairs = [(row_name, col_name)]

    state.last_clicked = (row_name, col_name)
    on_toggle_range(pairs, new_value)
    refresh_ticks(state, is_matched)
    try:
        args.Handled = True
    except Exception:
        pass
