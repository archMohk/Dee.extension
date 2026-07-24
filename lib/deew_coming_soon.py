# -*- coding: utf-8 -*-
"""
deew_coming_soon
Reusable "Coming Soon" WPF dialog shared by every not-yet-implemented
DeeW.Cloud tool (Publish, Consume, Transfer, Batch Upgrade, Audit,
Clean, Sync, Package) so none of them need their own placeholder
window - one XAML file, one launcher function.

Runs under pyRevit's CPython 3 engine (this whole DeeW.Cloud package
does - see the "#! python3" hashbang at the top of every tool's
script.py), unlike the rest of Dee.extension which stays on pyRevit's
default IronPython 2 engine. pyrevit.forms.WPFWindow itself is
engine-agnostic pyRevit library code, so this pattern is expected to
work the same under either engine - flagged here as a general
DeeW.Cloud assumption needing live-Revit verification, same as every
other tool in this extension.
"""
import os

from pyrevit import forms

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "deew_coming_soon.xaml")


class _ComingSoonWindow(forms.WPFWindow):
    def __init__(self, xaml_file, tool_name, description):
        forms.WPFWindow.__init__(self, xaml_file)
        self.tool_name_tb.Text = tool_name
        if description:
            self.description_tb.Text = description

    def close_click(self, sender, args):
        self.Close()


def show_coming_soon(tool_name, description=""):
    """Call this as the entire body of a placeholder tool's script.py.
    Never raises - a placeholder button failing to even show a dialog
    would be a worse experience than just silently doing nothing, so
    any unexpected UI failure is swallowed defensively."""
    try:
        window = _ComingSoonWindow(_XAML_FILE, tool_name, description)
        window.ShowDialog()
    except Exception:
        pass
