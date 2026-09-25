# -*- coding: utf-8 -*-
"""
Dee3DView
A small hub for the 3D-view helpers, launched from one ribbon button:

- DeeMono: the existing single-hue presentation tool. Not duplicated -
  the hub closes itself and then EXECUTES DeeMono's own script.py (with
  the same globals pyRevit would give it), so there is exactly one
  DeeMono implementation and this hub can never drift out of date with
  it. The hub closes FIRST because this codebase's hard rule is never
  to open a second WPF ShowDialog from inside an already-open one (see
  DeeMono's own docstring) - the dispatch happens after this window's
  ShowDialog() has returned.

- DeeClear3D (new, lives in this bundle): tick any of the project's 3D
  views and hide every NON-MODEL category from them - annotation and
  analytical categories both (levels, grids, reference planes, scope
  boxes, analytical members...), which is everything that isn't actual
  building geometry. Done per view via View.SetCategoryHidden, the same
  switch VV's Annotation Categories tab flips, so it is fully reversible
  - the "Show Them Again" button runs the exact same loop with
  hidden=False. Categories a view refuses (CanCategoryBeHidden False,
  or Visibility/Graphics controlled by a view template) are skipped and
  the view is reported rather than failing the whole run.
"""
import io
import os

import clr
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
clr.AddReference("WindowsBase")
from System.Windows import Thickness
from System.Windows.Input import Cursors
from System.Windows.Media import SolidColorBrush, Color, Brushes

from Autodesk.Revit.DB import (
    FilteredElementCollector, View3D, CategoryType, Transaction, ElementId,
)

from pyrevit import forms
import dee_branding
import dee_telemetry
dee_telemetry.check_access("Dee3DView")

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_HUB_XAML = os.path.join(_THIS_DIR, "ui.xaml")
_CLEAR_XAML = os.path.join(_THIS_DIR, "clear3d.xaml")
_MONO_SCRIPT = os.path.join(os.path.dirname(_THIS_DIR),
                            "ViewStack.stack", "DeeMono.pushbutton", "script.py")

# Same hover treatment as the Notification Center hub's cards.
_HOVER_ACCENT = Color.FromRgb(0xF2, 0x99, 0x4D)
_HOVER_BORDER = SolidColorBrush(_HOVER_ACCENT)
_HOVER_FILL = SolidColorBrush(Color.FromArgb(0x28, 0xF2, 0x99, 0x4D))
_IDLE_BORDER = Brushes.Gray


def _view_name(view):
    try:
        return view.Name
    except Exception:
        return ""


class Hub(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.launch = None
        self._wire_card(self.mono_card_b, "mono")
        self._wire_card(self.clear_card_b, "clear")

    def _wire_card(self, border, key):
        border.Cursor = Cursors.Hand
        border.BorderBrush = _IDLE_BORDER
        border.BorderThickness = Thickness(1)

        def on_enter(sender, args):
            sender.BorderBrush = _HOVER_BORDER
            sender.BorderThickness = Thickness(2)
            sender.Background = _HOVER_FILL

        def on_leave(sender, args):
            sender.BorderBrush = _IDLE_BORDER
            sender.BorderThickness = Thickness(1)
            sender.Background = Brushes.Transparent

        def on_click(sender, args):
            self.launch = key
            self.Close()

        border.MouseEnter += on_enter
        border.MouseLeave += on_leave
        border.MouseLeftButtonUp += on_click

    def close_click(self, sender, args):
        self.Close()


class ViewRow(object):
    def __init__(self, view):
        self.view = view
        self.state = False
        extra = ""
        try:
            if view.IsPerspective:
                extra = "  (perspective)"
        except Exception:
            pass
        self.name = u"{0}{1}".format(_view_name(view), extra)


def _hide_non_model(doc, views, hide):
    """Flips every non-Model category in each view. Returns
    [(view_name, changed_count, note_or_None)]. One transaction for the
    whole run - either every checked view changes or none do."""
    results = []
    t = Transaction(doc, "DeeClear3D" if hide else "DeeClear3D - restore")
    t.Start()
    try:
        for view in views:
            changed = 0
            for cat in doc.Settings.Categories:
                try:
                    if cat.CategoryType == CategoryType.Model:
                        continue
                    if not view.CanCategoryBeHidden(cat.Id):
                        continue
                    view.SetCategoryHidden(cat.Id, hide)
                    changed += 1
                except Exception:
                    continue
            note = None
            if changed == 0:
                has_template = False
                try:
                    has_template = view.ViewTemplateId != ElementId.InvalidElementId
                except Exception:
                    pass
                note = (u"visibility is controlled by a view template"
                        if has_template else u"nothing to change")
            results.append((_view_name(view), changed, note))
        t.Commit()
    except Exception:
        t.RollBack()
        raise
    return results


class Clear3DWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file, doc):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.doc = doc
        self._rows = [ViewRow(v) for v in self._collect_3d_views()]
        self._rows.sort(key=lambda r: r.name.lower())
        self._refresh()

    def _collect_3d_views(self):
        out = []
        try:
            for v in FilteredElementCollector(self.doc).OfClass(View3D):
                try:
                    if not v.IsTemplate:
                        out.append(v)
                except Exception:
                    continue
        except Exception:
            pass
        return out

    def _shown(self):
        needle = (self.filter_tb.Text or u"").strip().lower()
        if not needle:
            return list(self._rows)
        return [r for r in self._rows if needle in r.name.lower()]

    def _refresh(self):
        self.views_lb.ItemsSource = None
        self.views_lb.ItemsSource = self._shown()

    def filter_changed(self, sender, args):
        self._refresh()

    def _set_all(self, value):
        for r in self._shown():
            r.state = value
        self._refresh()

    def check_all_click(self, sender, args):
        self._set_all(True)

    def uncheck_all_click(self, sender, args):
        self._set_all(False)

    def _run(self, hide):
        checked = [r.view for r in self._rows if r.state]
        if not checked:
            forms.alert(u"Tick at least one 3D view first.", title="Dee3DView")
            return
        try:
            results = _hide_non_model(self.doc, checked, hide)
        except Exception as e:
            self.status_tb.Text = u"FAILED - nothing was changed: {0}".format(e)
            return
        done = [r for r in results if r[1] > 0]
        skipped = [r for r in results if r[1] == 0]
        verb = u"Hidden in" if hide else u"Restored in"
        msg = u"{0} {1} view(s).".format(verb, len(done))
        if skipped:
            msg += u"  Skipped: " + u"; ".join(
                u"{0} ({1})".format(nm, note) for nm, _c, note in skipped)
        self.status_tb.Text = msg

    def apply_click(self, sender, args):
        self._run(True)

    def restore_click(self, sender, args):
        self._run(False)

    def close_click(self, sender, args):
        self.Close()


def _run_mono():
    """Executes DeeMono's own script.py in a fresh globals dict carrying
    the same names pyRevit itself provides - one implementation, zero
    duplication. Raises to the caller on a missing file so the error is
    visible rather than a silent nothing-happens."""
    with io.open(_MONO_SCRIPT, "r", encoding="utf-8") as fh:
        source = fh.read()
    code = compile(source, _MONO_SCRIPT, "exec")
    module_globals = {
        "__revit__": __revit__,
        "__file__": _MONO_SCRIPT,
        "__name__": "__main__",
        "__builtins__": __builtins__,
    }
    exec(code, module_globals)


def main():
    doc = __revit__.ActiveUIDocument.Document if __revit__.ActiveUIDocument else None
    if doc is None:
        forms.alert("Open a Revit project first.", title="Dee3DView")
        return
    if doc.IsFamilyDocument:
        forms.alert("Dee3DView works on a project's 3D views. It cannot be run "
                    "inside the Family Editor.", title="Dee3DView")
        return

    hub = Hub(_HUB_XAML)
    hub.ShowDialog()

    # Dispatch AFTER the hub has fully closed - never a nested ShowDialog.
    if hub.launch == "mono":
        try:
            _run_mono()
        except Exception as e:
            forms.alert(u"Could not open DeeMono:\n{0}".format(e), title="Dee3DView")
    elif hub.launch == "clear":
        try:
            Clear3DWindow(_CLEAR_XAML, doc).ShowDialog()
        except Exception as e:
            forms.alert(u"Could not open DeeClear3D:\n{0}".format(e), title="Dee3DView")


main()
