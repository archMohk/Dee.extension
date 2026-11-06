# -*- coding: utf-8 -*-
"""
DeeAccount (Cloud)
Lets the user sign in to TWO separate, independently-cached Autodesk
accounts - "Work / Company" and "Personal" - and pick which one is
ACTIVE. Every other cloud tool in this extension calls
lib/acc_auth.py's get_access_token() with no account argument, which
now resolves to whichever account is active here - so switching the
active account here changes what every tool uses, with no changes
needed in any of those 19+ call sites. See lib/acc_auth.py's own
"Named account slots" docstring section for the full mechanism.

This does NOT implement SSO/SAML/OIDC federation (Microsoft Entra ID,
Okta, Google Workspace, etc.) itself - every Sign In here opens
Autodesk's own hosted login page exactly as every cloud tool already
did before this existed. Whether that redirects to a company identity
provider depends entirely on how the signing-in account's Autodesk
organization is configured on Autodesk's own side - an IT/Autodesk
Admin Console setting, outside anything this extension controls.
"""
import os

import clr
clr.AddReference("WindowsBase")
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")

import dee_branding
import acc_auth
import dee_telemetry
dee_telemetry.check_access("DeeAccount")

_TOOL = "DeeAccount"
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")

_WORK = acc_auth.DEFAULT_ACCOUNT
_PERSONAL = "personal"


class DeeAccountWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self._ready = False
        active = acc_auth.get_active_account()
        # Set in code, not XAML - both RadioButtons have a wired Checked
        # handler, and setting IsChecked from XAML can fire it before
        # this window is ready (see feedback_wpf_xaml_early_event_fire).
        self.work_active_rb.IsChecked = (active == _WORK)
        self.personal_active_rb.IsChecked = (active == _PERSONAL)
        self._ready = True
        self._refresh_status()

    def _refresh_status(self):
        self.work_status_tb.Text = "Signed in." if acc_auth.account_status(_WORK) else "Not signed in."
        self.personal_status_tb.Text = (
            "Signed in." if acc_auth.account_status(_PERSONAL) else "Not signed in.")

    def active_changed(self, sender, args):
        if not self._ready:
            return
        account = _WORK if self.work_active_rb.IsChecked else _PERSONAL
        acc_auth.set_active_account(account)

    def _sign_in(self, account, status_tb):
        status_tb.Text = "Opening Autodesk sign-in in your browser..."
        try:
            acc_auth.get_access_token(force_login=True, account=account)
            status_tb.Text = "Signed in."
        except Exception as e:
            status_tb.Text = "Could not sign in - {0}".format(e)

    def work_signin_click(self, sender, args):
        self._sign_in(_WORK, self.work_status_tb)

    def personal_signin_click(self, sender, args):
        self._sign_in(_PERSONAL, self.personal_status_tb)

    def work_signout_click(self, sender, args):
        acc_auth.sign_out(_WORK)
        self._refresh_status()

    def personal_signout_click(self, sender, args):
        acc_auth.sign_out(_PERSONAL)
        self._refresh_status()

    def close_click(self, sender, args):
        self.Close()


def main():
    window = DeeAccountWindow(_XAML_FILE)
    window.ShowDialog()


main()
