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
"""
import os

from pyrevit import forms
import dee_branding
import dee_telemetry

try:
    import acc_auth
except Exception:
    acc_auth = None

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")


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

    def _refresh(self):
        email = dee_telemetry.get_cached_identity()
        self.email_tb.Text = email or "(not set yet - open any Dee tool once)"

        status = dee_telemetry.load_cached_status()
        summary = dee_telemetry.status_summary()
        self.status_tb.Text = summary or "Not checked yet - open any Dee tool once first."
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
