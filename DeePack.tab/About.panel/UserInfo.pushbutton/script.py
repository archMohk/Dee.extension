# -*- coding: utf-8 -*-
"""
User Info (About)
Purely informational: shows the signed-in email, the cached access
status, and whether this PC is set up for ACC. Deliberately does NOT
call dee_telemetry.check_access() the way every other Dee tool does -
the whole point of this button is to let someone see their OWN status
even while blocked, rather than being gated by the very thing it is
showing. Everything shown is either cached/local (no network call on
open) or explicitly re-checked via the "Check Now" button.

Prayer time settings used to live in this window - moved out to the
new Notification Center (About.panel/NotificationCenter.pushbutton),
which is the general home for everything this extension pops up as a
toast, not just prayer times.
"""
import os

import clr
clr.AddReference("PresentationCore")
from System.Windows.Media import SolidColorBrush, Color

from pyrevit import forms
import dee_branding
import dee_telemetry
import dee_ribbon_mode

try:
    import acc_auth
except Exception:
    acc_auth = None

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")
# script.py -> UserInfo.pushbutton -> About.panel -> DeePack.tab -> Dee.extension
_EXTENSION_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_THIS_DIR)))
_TAB_ICON_PATH = os.path.join(_EXTENSION_ROOT, "icon.png")

# Same green/red pair used throughout this codebase's own coloured
# HTML reports (e.g. DeeSuperLINK/DeeMAPLink's _report()), reused here
# for consistency rather than picking new colours.
_ACTIVE_COLOR = Color.FromRgb(0x2E, 0x7D, 0x32)
_INACTIVE_COLOR = Color.FromRgb(0xC6, 0x28, 0x28)


def _expires_text(status):
    if not status:
        return "-"
    expires_at = status.get("expires_at")
    if not expires_at:
        return "Never"
    # Postgres timestamptz JSON is ISO-8601 ("2026-10-01T00:00:00+00:00") -
    # slicing off the date portion is simpler and safer than parsing it
    # (no timezone-math risk) for a purely-informational display.
    return expires_at.split("T")[0]


class UserInfoWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.contact_tb.Text = "Contact:\n" + dee_telemetry.CONTACT_INFO
        self._refresh()
        # Third attempt point for the experimental tab icon (see
        # dee_ribbon_mode.set_tab_icon's own docstring) - two combo-box
        # trigger points (__selfinit__, __cmb_on_change__) produced zero
        # evidence across 4 full reload/restart cycles. A plain
        # pushbutton is the one execution context proven reliable all
        # session (every other tool's edits have picked up on a normal
        # Reload without exception), so this rules module-caching/
        # execution-context oddities in or out cleanly.
        try:
            dee_ribbon_mode.set_tab_icon(__revit__, _TAB_ICON_PATH)
        except Exception:
            pass

    def _refresh(self):
        email = dee_telemetry.get_cached_identity()
        self.email_tb.Text = email or "(not set yet - open any Dee tool once)"

        status = dee_telemetry.load_cached_status()
        summary = dee_telemetry.status_summary()
        self.status_tb.Text = summary or "Not checked yet - open any Dee tool once first."
        is_active = bool(status and status.get("allowed"))
        self.status_tb.Foreground = SolidColorBrush(
            _ACTIVE_COLOR if is_active else _INACTIVE_COLOR)
        self.expires_tb.Text = _expires_text(status)

        if acc_auth is not None:
            try:
                ready = acc_auth.is_configured()
            except Exception:
                ready = False
            self.acc_tb.Text = ("Ready" if ready else
                                 "Not configured - see acc_config.example.json")
        else:
            self.acc_tb.Text = "Unknown"

    def check_now_click(self, sender, args):
        email = dee_telemetry.get_cached_identity()
        if not email:
            forms.alert(
                "No email on file yet - open any Dee tool once first, it "
                "will ask you.", title="Dee.extension - User Info")
            return
        try:
            dee_telemetry.refresh_status(email)
        except Exception as e:
            forms.alert("Could not check - {0}".format(e),
                        title="Dee.extension - User Info")
            return
        self._refresh()

    def reenter_click(self, sender, args):
        if not forms.alert(
                "Clear the saved email? The next Dee tool you open will "
                "ask for it again.",
                title="Dee.extension - User Info", yes=True, no=True):
            return
        dee_telemetry.clear_identity()
        self._refresh()

    def close_click(self, sender, args):
        self.Close()


window = UserInfoWindow(_XAML_FILE)
window.ShowDialog()
