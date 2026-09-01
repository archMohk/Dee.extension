# -*- coding: utf-8 -*-
"""
DeeControl (About)
The Control Panel: switches every DeePack button, dropdown item and panel
on or off, with each one's own tooltip shown as its brief. All the scan /
state / bundle-rewriting logic lives in lib/dee_control_panel_service.py;
this file only wires the WPF window to it.

The off switch is the `layout:` list in each container's bundle.yaml -
confirmed in pyRevit's own source, not guessed. See the service module's
docstring for the exact quote and for why an EMPTY layout would do the
opposite of what you want.

context: zero-doc, because rearranging your own ribbon has nothing to do
with whether a project happens to be open.
"""
import os

from pyrevit import forms, script
import dee_branding

import dee_control_panel_service as core

output = script.get_output()

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")

# script.py -> DeeControl.pushbutton -> About.panel -> DeePack.tab
_TAB_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))
_EXTENSION_ROOT = os.path.dirname(_TAB_ROOT)
_CONFIG_PATH = os.path.join(_EXTENSION_ROOT, "dee_control_panel.json")


def matches(node, query):
    if not query:
        return True
    haystack = u"{0} {1} {2}".format(node.title, node.kind, node.brief).lower()
    return all(term in haystack for term in query.lower().split())


class DeeControlWindow(dee_branding.DeeBrandedWindow):
    # Must exist BEFORE the base class loads the XAML - loading it fires
    # search_changed, at which point no instance attribute exists yet.
    _ready = False

    def __init__(self, xaml_file):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)

        with forms.ProgressBar(title="DeeControl - reading the ribbon...",
                               indeterminate=True):
            self.root = core.scan(_TAB_ROOT)
            self.nodes = core.flatten(self.root)
            core.load_state(_CONFIG_PATH, self.root, self.nodes)
            # flatten again: load_state may have re-ordered children back to
            # their saved ribbon order.
            self.nodes = core.flatten(self.root)

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

    def _refresh(self):
        core.refresh_notes(self.nodes)
        shown = self._shown()
        self.items_grid.ItemsSource = None
        self.items_grid.ItemsSource = shown

        on, total, hidden = core.summarize(self.nodes)
        self.status_tb.Text = (
            "{0} of {1} button(s) on. {2} panel/group(s) will be hidden because "
            "nothing inside them is on. Showing {3} row(s).".format(
                on, total, hidden, len(shown)))

        changed = [n for n in self.nodes
                   if self._baseline.get(n.rel_key) != n.enabled]
        self.pending_tb.Text = (
            "" if not changed else
            "{0} unapplied change(s). Click Apply Changes, then reload "
            "pyRevit.".format(len(changed)))
        return shown

    def search_changed(self, sender, args):
        if not self._ready:
            return
        self._refresh()

    # ---------------- bulk toggles ----------------
    def _set_all(self, nodes, value):
        for n in nodes:
            if not n.locked:
                n.enabled = value
        self._refresh()

    def all_on_click(self, sender, args):
        self._set_all(self.nodes, True)

    def all_off_click(self, sender, args):
        self._set_all(self.nodes, False)

    def invert_click(self, sender, args):
        for n in self.nodes:
            if not n.locked:
                n.enabled = not n.enabled
        self._refresh()

    def shown_on_click(self, sender, args):
        self._set_all(self._shown(), True)

    def shown_off_click(self, sender, args):
        self._set_all(self._shown(), False)

    def reset_click(self, sender, args):
        if not forms.alert("Turn every DeePack button back on?",
                           title="DeeControl", yes=True, no=True):
            return
        self._set_all(self.nodes, True)
        self.status_tb.Text = ("Everything is switched on. Click Apply Changes to "
                               "write it, then reload pyRevit.")

    # ---------------- apply ----------------
    def apply_click(self, sender, args):
        off_buttons = [n for n in self.nodes if not n.is_container and not n.enabled]
        hidden_containers = [n for n in self.nodes
                             if n.is_container and not core.effective_enabled(n)]
        message = ("Write this layout to the extension?\n\n"
                   "{0} button(s) will be switched OFF.\n"
                   "{1} panel/group(s) will disappear because nothing inside "
                   "them is on.\n\n"
                   "This edits the layout list in each panel's bundle.yaml. "
                   "Those files are tracked in git, so the change is reviewable "
                   "and revertible.".format(len(off_buttons), len(hidden_containers)))
        if not forms.alert(message, title="DeeControl", yes=True, no=True):
            return

        try:
            with forms.ProgressBar(title="DeeControl - writing bundle files...",
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

        self.status_tb.Text = ("Applied - {0} file(s) rewritten. Reload pyRevit to "
                               "rebuild the ribbon.".format(len(results)))
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
