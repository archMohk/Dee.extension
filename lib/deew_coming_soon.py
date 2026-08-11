# -*- coding: utf-8 -*-
"""
deew_coming_soon
Reusable "Coming Soon" WPF dialog shared by every not-yet-implemented
DeeW.Cloud tool (Publish, Consume, Transfer, Batch Upgrade, Audit,
Clean, Sync, Package) so none of them need their own placeholder
window - one XAML file, one launcher function.

Runs on pyRevit's default IronPython 2 engine, same as every other
tool in Dee.extension. DeeW.Cloud was originally built against
pyRevit's CPython 3 engine, but live testing found that engine crashes
at the .NET level before any script runs in this environment - see
DeeWSharing.pushbutton/script.py's module docstring for the full
story - so the whole package now uses IronPython 2 instead.
"""
import os

from pyrevit import forms
import dee_branding

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "deew_coming_soon.xaml")


class _ComingSoonWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, tool_name, description):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
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
