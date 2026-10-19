# -*- coding: utf-8 -*-
"""
dee_smart_sheet_dialogs
Two small modal dialogs for DeeSheet's "SmartSheet" tab:

  show_duplicate(node) - confirms the new identifying value for a
  Duplicate Group action and previews the resulting Sheet Number /
  Sheet Name for every sheet in the template group before anything is
  created. Returns the new value string on Duplicate, or None on
  Cancel/close.

  show_add_sheet(chain, titleblock_names) - collects Sheet Number /
  Sheet Name / Title Block for a single new sheet, with the folder
  chain leading to the clicked tree node shown read-only for context.
  Returns (number, name, titleblock_label) on Add Sheet, or None on
  Cancel/close.

Same show()-returns-None-or-result pattern as dee_rename_pipeline_dialog,
so callers never need to distinguish "cancelled" from "returned empty".
"""
import os

import dee_branding

_THIS_DIR = os.path.dirname(__file__)
_DUP_XAML = os.path.join(_THIS_DIR, "DeeSmartSheetDuplicateDialog.xaml")
_ADD_XAML = os.path.join(_THIS_DIR, "DeeSmartSheetAddDialog.xaml")

_NONE_TITLEBLOCK_LABEL = "(None - no title block)"


class _DuplicateDialog(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, node):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self._node = node
        self._result = None
        self.old_value_tb.Text = node.label
        self.param_tb.Text = node.classify_param or "(not identified - Number/Name text only)"
        self._refresh_preview()

    def _refresh_preview(self):
        new_value = self.new_value_tb.Text or ""
        old_value = self._node.label
        rows = []
        for sheet in self._node.group_sheets:
            try:
                old_number = sheet.SheetNumber or ""
            except Exception:
                old_number = ""
            try:
                old_name = sheet.Name or ""
            except Exception:
                old_name = ""
            if new_value and (old_value in old_number or old_value in old_name):
                new_number = old_number.replace(old_value, new_value)
                new_name = old_name.replace(old_value, new_value)
                rows.append("{0} - {1}   ->   {2} - {3}".format(old_number, old_name, new_number, new_name))
            else:
                rows.append("{0} - {1}   (unchanged - value not found)".format(old_number, old_name))
        self.preview_lb.ItemsSource = rows

    def new_value_changed(self, sender, args):
        self._refresh_preview()

    def ok_click(self, sender, args):
        self._result = self.new_value_tb.Text or ""
        self.Close()

    def cancel_click(self, sender, args):
        self._result = None
        self.Close()


class _AddSheetDialog(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, chain, titleblock_names):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self._result = None
        self.chain_tb.Text = " / ".join(label for label, _param in chain) if chain else "(top level)"
        names = [_NONE_TITLEBLOCK_LABEL] + list(titleblock_names or [])
        self.titleblock_cb.ItemsSource = names
        self.titleblock_cb.SelectedIndex = 0

    def ok_click(self, sender, args):
        number = (self.number_tb.Text or "").strip()
        name = (self.name_tb.Text or "").strip()
        tb_label = self.titleblock_cb.SelectedItem
        self._result = (number, name, tb_label if tb_label != _NONE_TITLEBLOCK_LABEL else None)
        self.Close()

    def cancel_click(self, sender, args):
        self._result = None
        self.Close()


def show_duplicate(node):
    """node: a SmartSheetNode with node.is_group True. Returns the new
    value string the user entered, or None on Cancel/close (callers
    should treat an empty-but-not-None result as "nothing entered" and
    re-prompt/alert, same as other DeeSheet confirmation flows)."""
    dlg = _DuplicateDialog(_DUP_XAML, node)
    dlg.ShowDialog()
    return dlg._result


def show_add_sheet(chain, titleblock_names):
    """chain: [(folder_label, classify_param_or_None), ...] from root to
    the clicked node, as returned by dee_smart_sheet_service.classify_chain.
    Returns (number, name, titleblock_label_or_None) on Add Sheet, or
    None on Cancel/close."""
    dlg = _AddSheetDialog(_ADD_XAML, chain, titleblock_names)
    dlg.ShowDialog()
    return dlg._result
