# -*- coding: utf-8 -*-
"""
dee_rename_pipeline_dialog
Shared modal "Advanced Rename" dialog wrapping dee_rename_pipeline.py's
8-method engine (RegEx, Name, Replace, Case, Remove, Add, Auto Date,
Numbering) behind one reusable window, so any tool that wants the same
DeeBulkRename-style pipeline for its own naming feature can pop this
dialog instead of re-building the same ~40 controls itself.

Usage from a caller (e.g. DeeLevels, DeeAssemb, DeeView):
    import dee_rename_pipeline_dialog as rp_dialog
    methods = rp_dialog.show(sample_name=some_row.base_name,
                              initial_methods=self._naming_methods)
    if methods is not None:
        self._naming_methods = methods

`show()` returns the edited methods dict on OK, or None on Cancel/close.
The caller's own previously-stored config is left untouched when None
comes back, so callers should only overwrite their stored methods when
the return value is not None. This module never touches the caller's
document/model - it only edits a plain dict in memory.
"""
import os

from pyrevit import forms

import dee_branding
from dee_rename_pipeline import DEFAULT_METHODS, apply_methods

_THIS_DIR = os.path.dirname(__file__)
_XAML_FILE = os.path.join(_THIS_DIR, "DeeRenamePipelineDialog.xaml")


class _RenamePipelineDialog(dee_branding.DeeBrandedWindow):
    _ready = False

    def __init__(self, xaml_file, sample_name, initial_methods):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self._sample_name = sample_name if sample_name else u"Sample Name"
        self._result = None

        self._load_methods(initial_methods or DEFAULT_METHODS())
        self._ready = True
        self._refresh_preview()

    # ---------------- load/read ----------------
    def _load_methods(self, m):
        try:
            self.m_regex_en_cb.IsChecked = m["regex"]["enabled"]
            self.m_regex_match_tb.Text = m["regex"]["match"]
            self.m_regex_replace_tb.Text = m["regex"]["replace"]
            self.m_regex_case_cb.IsChecked = m["regex"]["case_sensitive"]

            self.m_name_en_cb.IsChecked = m["name"]["enabled"]
            self.m_name_mode_cb.SelectedIndex = {"keep": 0, "remove": 1, "fixed": 2}.get(m["name"]["mode"], 0)
            self.m_name_fixed_tb.Text = m["name"]["fixed_text"]

            self.m_replace_en_cb.IsChecked = m["replace"]["enabled"]
            self.m_replace_find_tb.Text = m["replace"]["find"]
            self.m_replace_with_tb.Text = m["replace"]["with"]
            self.m_replace_matchcase_cb.IsChecked = m["replace"]["match_case"]
            self.m_replace_first_cb.IsChecked = m["replace"]["first_only"]

            self.m_case_en_cb.IsChecked = m["case"]["enabled"]
            self.m_case_mode_cb.SelectedIndex = ["same", "upper", "lower", "title", "sentence"].index(
                m["case"]["mode"]) if m["case"]["mode"] in ["same", "upper", "lower", "title", "sentence"] else 0
            self.m_case_exceptions_tb.Text = m["case"]["exceptions"]

            self.m_remove_en_cb.IsChecked = m["remove"]["enabled"]
            self.m_remove_firstn_tb.Text = unicode(m["remove"]["first_n"])
            self.m_remove_lastn_tb.Text = unicode(m["remove"]["last_n"])
            self.m_remove_from_tb.Text = unicode(m["remove"]["from_pos"])
            self.m_remove_to_tb.Text = unicode(m["remove"]["to_pos"])
            self.m_remove_cropbefore_tb.Text = m["remove"]["crop_before"]
            self.m_remove_cropafter_tb.Text = m["remove"]["crop_after"]
            self.m_remove_digits_cb.IsChecked = m["remove"]["remove_digits"]
            self.m_remove_symbols_cb.IsChecked = m["remove"]["remove_symbols"]
            self.m_remove_trim_cb.IsChecked = m["remove"]["trim"]

            self.m_add_en_cb.IsChecked = m["add"]["enabled"]
            self.m_add_prefix_tb.Text = m["add"]["prefix"]
            self.m_add_suffix_tb.Text = m["add"]["suffix"]
            self.m_add_insert_tb.Text = m["add"]["insert_text"]
            self.m_add_insertpos_tb.Text = unicode(m["add"]["insert_pos"])

            self.m_date_en_cb.IsChecked = m["auto_date"]["enabled"]
            self.m_date_pos_cb.SelectedIndex = 0 if m["auto_date"]["position"] == "prefix" else 1
            self.m_date_fmt_cb.SelectedIndex = ["dmy", "mdy", "ymd"].index(m["auto_date"]["format"]) \
                if m["auto_date"]["format"] in ["dmy", "mdy", "ymd"] else 0
            self.m_date_sep_tb.Text = m["auto_date"]["separator"]

            self.m_numbering_en_cb.IsChecked = m["numbering"]["enabled"]
            self.m_numbering_pos_cb.SelectedIndex = 0 if m["numbering"]["position"] == "prefix" else 1
            self.m_numbering_start_tb.Text = unicode(m["numbering"]["start"])
            self.m_numbering_incr_tb.Text = unicode(m["numbering"]["increment"])
            self.m_numbering_pad_tb.Text = unicode(m["numbering"]["pad"])
            self.m_numbering_sep_tb.Text = m["numbering"]["separator"]
        except Exception:
            pass

    def _read_int(self, textbox, default=0):
        try:
            return int((textbox.Text or "").strip())
        except Exception:
            return default

    def _read_methods(self):
        return {
            "regex": {
                "enabled": self.m_regex_en_cb.IsChecked is True,
                "match": self.m_regex_match_tb.Text or u"",
                "replace": self.m_regex_replace_tb.Text or u"",
                "case_sensitive": self.m_regex_case_cb.IsChecked is True,
            },
            "name": {
                "enabled": self.m_name_en_cb.IsChecked is True,
                "mode": ("remove" if self.m_name_mode_cb.SelectedIndex == 1
                         else "fixed" if self.m_name_mode_cb.SelectedIndex == 2 else "keep"),
                "fixed_text": self.m_name_fixed_tb.Text or u"",
            },
            "replace": {
                "enabled": self.m_replace_en_cb.IsChecked is True,
                "find": self.m_replace_find_tb.Text or u"",
                "with": self.m_replace_with_tb.Text or u"",
                "match_case": self.m_replace_matchcase_cb.IsChecked is True,
                "first_only": self.m_replace_first_cb.IsChecked is True,
            },
            "case": {
                "enabled": self.m_case_en_cb.IsChecked is True,
                "mode": ["same", "upper", "lower", "title", "sentence"][self.m_case_mode_cb.SelectedIndex],
                "exceptions": self.m_case_exceptions_tb.Text or u"",
            },
            "remove": {
                "enabled": self.m_remove_en_cb.IsChecked is True,
                "first_n": self._read_int(self.m_remove_firstn_tb, 0),
                "last_n": self._read_int(self.m_remove_lastn_tb, 0),
                "from_pos": self._read_int(self.m_remove_from_tb, 0),
                "to_pos": self._read_int(self.m_remove_to_tb, 0),
                "crop_before": self.m_remove_cropbefore_tb.Text or u"",
                "crop_after": self.m_remove_cropafter_tb.Text or u"",
                "remove_digits": self.m_remove_digits_cb.IsChecked is True,
                "remove_symbols": self.m_remove_symbols_cb.IsChecked is True,
                "trim": self.m_remove_trim_cb.IsChecked is True,
            },
            "add": {
                "enabled": self.m_add_en_cb.IsChecked is True,
                "prefix": self.m_add_prefix_tb.Text or u"",
                "suffix": self.m_add_suffix_tb.Text or u"",
                "insert_text": self.m_add_insert_tb.Text or u"",
                "insert_pos": self._read_int(self.m_add_insertpos_tb, 0),
            },
            "auto_date": {
                "enabled": self.m_date_en_cb.IsChecked is True,
                "position": "prefix" if self.m_date_pos_cb.SelectedIndex == 0 else "suffix",
                "format": ["dmy", "mdy", "ymd"][self.m_date_fmt_cb.SelectedIndex],
                "separator": self.m_date_sep_tb.Text or u"",
            },
            "numbering": {
                "enabled": self.m_numbering_en_cb.IsChecked is True,
                "position": "prefix" if self.m_numbering_pos_cb.SelectedIndex == 0 else "suffix",
                "start": self._read_int(self.m_numbering_start_tb, 1),
                "increment": self._read_int(self.m_numbering_incr_tb, 1) or 1,
                "pad": self._read_int(self.m_numbering_pad_tb, 0),
                "separator": self.m_numbering_sep_tb.Text or u"",
            },
        }

    # ---------------- events ----------------
    def method_changed(self, sender, args):
        if not self._ready:
            return
        self._refresh_preview()

    def _refresh_preview(self):
        try:
            methods = self._read_methods()
            new_name = apply_methods(self._sample_name, 0, methods)
            self.preview_tb.Text = u"{0}  ->  {1}".format(self._sample_name, new_name)
        except Exception:
            self.preview_tb.Text = self._sample_name

    def ok_click(self, sender, args):
        self._result = self._read_methods()
        self.Close()

    def cancel_click(self, sender, args):
        self._result = None
        self.Close()


def show(sample_name=u"", initial_methods=None):
    """Opens the Advanced Rename dialog modally. Returns the edited
    methods dict on OK, or None on Cancel/close (in which case the
    caller should leave its own stored config untouched)."""
    dlg = _RenamePipelineDialog(_XAML_FILE, sample_name, initial_methods)
    dlg.ShowDialog()
    return dlg._result
