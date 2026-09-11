# -*- coding: utf-8 -*-
"""
DeeControl (About)
The Control Panel: greys out any DeePack button, dropdown item or panel
you do not want, with each one's own tooltip shown as its brief. All the
scan / state / bundle-rewriting logic lives in
lib/dee_control_panel_service.py; this file only wires the WPF window to
it.

Switched-off buttons are FADED, never hidden - they stay exactly where
they are on the ribbon, greyed and unclickable, keeping their tooltips.
That is done by writing a never-true `context:` rule into the button's
bundle.yaml; Revit greys out any command whose availability check returns
false. See the service module's docstring for the pyRevit source that
confirms the `rule:` passthrough, and for why the rule is a logical
contradiction rather than a clever combination of tokens.

Switching a PANEL off greys every button inside it. Nothing is ever
removed from a layout, so ribbon order is never touched by this tool.

context: zero-doc, because rearranging your own ribbon has nothing to do
with whether a project happens to be open.
"""
import os

from pyrevit import forms, script
import dee_branding

import dee_control_panel_service as core
import dee_telemetry
dee_telemetry.check_access("DeeControl")


output = script.get_output()

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")

# script.py -> DeeControl.pushbutton -> About.panel -> DeePack.tab
_TAB_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))
_EXTENSION_ROOT = os.path.dirname(_TAB_ROOT)
_CONFIG_PATH = os.path.join(_EXTENSION_ROOT, "dee_control_panel.json")
# Personal, per-machine favorites list for DeePack's ribbon "View"
# dropdown's Favorite category (lib/dee_ribbon_mode.py) - deliberately
# NOT dee_control_panel.json (that one is team-wide, tracked in git).
_FAVORITES_PATH = os.path.join(_EXTENSION_ROOT, "lib", ".dee_favorites.json")


def matches(node, query):
    if not query:
        return True
    haystack = u"{0} {1} {2}".format(node.title, node.kind, node.brief).lower()
    return all(term in haystack for term in query.lower().split())


class _SafeProgress(object):
    """forms.ProgressBar tries to set Window.TaskbarItemInfo on its host
    window - a genuine WPF-level bug (not this extension's code):
    Window.TaskbarItemInfo throws NotImplementedException whenever the
    underlying ITaskbarList::HrInit COM call fails, which is documented
    to happen specifically under Remote Desktop/Terminal Services or a
    custom shell without a taskbar (live-confirmed in DeeSheetLinks).

    Wraps the real forms.ProgressBar and falls back to running with NO
    progress UI at all if entering it fails, so the tool degrades
    gracefully under RDP instead of crashing - everyone else still gets
    the real progress bar exactly as before. `pb.update_progress(...)`/
    `pb.cancelled` are safe no-ops in the fallback case, so callers never
    need an extra branch."""
    def __init__(self, **kwargs):
        self._kwargs = kwargs
        self._real = None

    def __enter__(self):
        try:
            self._real = forms.ProgressBar(**self._kwargs)
            return self._real.__enter__()
        except Exception:
            self._real = None
            return self

    def __exit__(self, exc_type, exc_value, tb):
        if self._real is not None:
            return self._real.__exit__(exc_type, exc_value, tb)
        return False

    @property
    def cancelled(self):
        return False

    def update_progress(self, i, total):
        pass


class DeeControlWindow(dee_branding.DeeBrandedWindow):
    # Must exist BEFORE the base class loads the XAML - loading it fires
    # search_changed, at which point no instance attribute exists yet.
    _ready = False

    def __init__(self, xaml_file):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)

        with _SafeProgress(title="DeeControl - reading the ribbon...",
                               indeterminate=True):
            self.root = core.scan(_TAB_ROOT)
            self.nodes = core.flatten(self.root)
            # load_state supplies each button's ORIGINAL context. Without it a
            # button that is currently switched off cannot be restored, because
            # its file now holds the disable rule rather than what it used to
            # say.
            core.load_state(_CONFIG_PATH, self.root, self.nodes)
            core.load_favorites(_FAVORITES_PATH, self.nodes)

        self._baseline = dict((n.rel_key, n.enabled) for n in self.nodes)
        self._refresh()
        self._ready = True

    # ---------------- grid ----------------
    def _shown(self):
        query = ""
        try:
            query = self.search_tb.Text or ""
        except Exception:
            pass
        buttons_only = self.buttons_only_cb.IsChecked is True
        return [n for n in self.nodes
                if matches(n, query) and not (buttons_only and n.is_container)]

    def _update_counts(self):
        """Text-only update. Deliberately does NOT rebuild the grid, so ticking
        a box does not reset your scroll position mid-pass."""
        live, total, faded = core.summarize(self.nodes)
        shown_count = len(self._shown())
        self.status_tb.Text = (
            "{0} of {1} button(s) active, {2} greyed out. Showing {3} row(s).".format(
                live, total, faded, shown_count))
        changed = [n for n in self.nodes
                   if self._baseline.get(n.rel_key) != n.enabled]
        self.pending_tb.Text = (
            "" if not changed else
            "{0} unapplied change(s). Click Apply Changes, then reload "
            "pyRevit.".format(len(changed)))

    def _refresh(self):
        core.refresh_notes(self.nodes)
        shown = self._shown()
        self.items_grid.ItemsSource = None
        self.items_grid.ItemsSource = shown
        self._update_counts()
        return shown

    def search_changed(self, sender, args):
        if not self._ready:
            return
        self._guard(self._refresh)

    def _guard(self, fn, *args):
        """WPF swallows exceptions raised inside an event handler, which would
        make a real bug look exactly like 'the button does nothing'. Every
        handler goes through here so a failure is visible instead."""
        try:
            return fn(*args)
        except Exception as e:
            import traceback
            self.status_tb.Text = "ERROR: {0}".format(e)
            forms.alert("DeeControl hit an error:\n\n{0}\n\n{1}".format(
                e, traceback.format_exc()[-900:]), title="DeeControl")

    def row_toggle_click(self, sender, args):
        """The row checkbox. The model is set from the CheckBox's own state
        rather than trusting the TwoWay binding to have written it back, so
        this works regardless of how WPF resolved the binding."""
        def run():
            node = sender.DataContext
            if node is None:
                return
            if node.locked:
                sender.IsChecked = True
                node.enabled = True
                self.status_tb.Text = "'{0}' is always on - it is how you get back here.".format(node.title)
                return
            node.enabled = sender.IsChecked is True
            core.refresh_notes(self.nodes)
            # Only a CONTAINER toggle changes what other rows show (it greys
            # everything beneath it), so only that needs a rebuild. Rebuilding
            # on every tick would reset the scroll position of an 89-row list.
            if node.is_container:
                self._refresh()
            else:
                self._update_counts()
        self._guard(run)

    def row_favorite_click(self, sender, args):
        """The Fav checkbox. Mirrors row_toggle_click's approach of
        trusting the CheckBox's own IsChecked over the binding, for the
        same reason. Unlike on/off, favoriting never cascades to other
        rows and never touches bundle.yaml, so there is nothing to
        refresh or grey - just the row's own state."""
        def run():
            node = sender.DataContext
            if node is None or not node.is_favoritable:
                return
            node.favorite = sender.IsChecked is True
        self._guard(run)

    def save_favorites_click(self, sender, args):
        def run():
            core.save_favorites(_FAVORITES_PATH, self.nodes)
            count = len([n for n in self.nodes if n.favorite and n.is_favoritable])
            self.status_tb.Text = (
                "{0} favorite(s) saved. Pick Favorite in DeePack's ribbon "
                "'View' dropdown to see them - no reload needed.".format(count))
        self._guard(run)

    # ---------------- bulk toggles ----------------
    def _set_all(self, nodes, value):
        touched = 0
        for n in nodes:
            if not n.locked:
                n.enabled = value
                touched += 1
        self._refresh()
        self.status_tb.Text = "{0} row(s) switched {1}. {2}".format(
            touched, "on" if value else "off", self.status_tb.Text)

    def all_on_click(self, sender, args):
        self._guard(self._set_all, self.nodes, True)

    def all_off_click(self, sender, args):
        self._guard(self._set_all, self.nodes, False)

    def invert_click(self, sender, args):
        def run():
            for n in self.nodes:
                if not n.locked:
                    n.enabled = not n.enabled
            self._refresh()
        self._guard(run)

    def shown_on_click(self, sender, args):
        self._guard(self._set_all, self._shown(), True)

    def shown_off_click(self, sender, args):
        self._guard(self._set_all, self._shown(), False)

    def reset_click(self, sender, args):
        if not forms.alert("Turn every DeePack button back on?",
                           title="DeeControl", yes=True, no=True):
            return
        self._set_all(self.nodes, True)
        self.status_tb.Text = ("Everything is switched on. Click Apply Changes to "
                               "write it, then reload pyRevit.")

    # ---------------- apply ----------------
    def apply_click(self, sender, args):
        self._guard(self._apply)

    def _apply(self):
        live, total, faded = core.summarize(self.nodes)
        message = ("Apply this to the ribbon?\n\n"
                   "{0} of {1} button(s) will be greyed out - they stay on the "
                   "ribbon, visibly faded and unclickable, keeping their "
                   "tooltips.\n\n"
                   "Nothing is hidden, moved or deleted. This writes a "
                   "never-true context rule into each button's bundle.yaml; "
                   "those files are tracked in git, so the change is reviewable "
                   "and revertible.".format(faded, total))
        if not forms.alert(message, title="DeeControl", yes=True, no=True):
            return

        try:
            with _SafeProgress(title="DeeControl - writing bundle files...",
                                   indeterminate=True):
                results = core.apply_state(self.root)
                core.save_state(_CONFIG_PATH, self.root, self.nodes)
        except Exception as e:
            forms.alert("Could not write the layout files:\n\n{0}\n\n"
                        "Nothing was reloaded, so your ribbon is unchanged.".format(e),
                        title="DeeControl")
            return

        self._baseline = dict((n.rel_key, n.enabled) for n in self.nodes)
        self._refresh()

        html = ('<h2 style="font-family:sans-serif;color:#ddd;">DeeControl</h2>'
                '<div style="font-family:sans-serif;color:#bbb;font-size:13px;">'
                '{0} bundle file(s) rewritten.</div>'.format(len(results)))
        for key, detail in results:
            html += ('<div style="padding:3px 12px;color:#9e9e9e;'
                     'font-family:monospace;font-size:12px;">{0} &mdash; {1}</div>'.format(
                         key, detail))
        if not results:
            html += ('<div style="padding:6px 12px;color:#9e9e9e;font-family:monospace;'
                     'font-size:12px;">Nothing needed changing.</div>')
        output.print_html(html)

        self.status_tb.Text = ("Applied - {0} button file(s) rewritten. Reload "
                               "pyRevit to see it.".format(len(results)))
        forms.alert("Applied.\n\n{0} bundle file(s) were rewritten.\n\n"
                    "The ribbon does not change until pyRevit reloads. Use the "
                    "Reload pyRevit Now button, or the Reload button on the "
                    "pyRevit tab whenever suits you.".format(len(results)),
                    title="DeeControl")

    def reload_click(self, sender, args):
        """Deliberately a button the user presses, never something Apply does
        on its own - a reload tears down and rebuilds every loaded extension,
        which is not something to spring on someone mid-task."""
        if not forms.alert("Reload pyRevit now?\n\nThis rebuilds the whole ribbon "
                           "and closes this window. Any unapplied ticks are lost, "
                           "so Apply first if you have not.",
                           title="DeeControl", yes=True, no=True):
            return
        try:
            from pyrevit.loader import sessionmgr
        except Exception as e:
            forms.alert("Could not reach pyRevit's session manager:\n{0}\n\n"
                        "Use the Reload button on the pyRevit tab instead.".format(e),
                        title="DeeControl")
            return
        self.Close()
        sessionmgr.reload_pyrevit()

    def close_click(self, sender, args):
        self.Close()


window = DeeControlWindow(_XAML_FILE)
window.ShowDialog()
