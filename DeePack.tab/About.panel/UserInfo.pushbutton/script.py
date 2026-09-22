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
import datetime

import clr
clr.AddReference("PresentationCore")
from System.Windows.Media import SolidColorBrush, Color

from pyrevit import forms
import dee_branding
import dee_telemetry
import dee_ribbon_mode
import dee_prayer_service

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
_TIME_COLOR = Color.FromRgb(0x22, 0x22, 0x22)
_TIME_DIM_COLOR = Color.FromRgb(0xAA, 0xAA, 0xAA)

_PRAYER_ORDER = ["Fajr", "Dhuhr", "Asr", "Maghrib", "Isha"]
_PRAYER_TB_NAMES = {
    "Fajr": "prayer_time_fajr",
    "Dhuhr": "prayer_time_dhuhr",
    "Asr": "prayer_time_asr",
    "Maghrib": "prayer_time_maghrib",
    "Isha": "prayer_time_isha",
}


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

        # Loaded here in code, not set via XAML attributes on
        # prayer_enabled_cb - a Checked/Unchecked handler wired in XAML
        # can fire the moment IsChecked is set there, before the rest of
        # this window is ready (see feedback_wpf_xaml_early_event_fire).
        prayer_settings = dee_prayer_service.load_settings()
        self.prayer_enabled_cb.IsChecked = bool(prayer_settings.get("enabled", True))
        self.prayer_duration_tb.Text = str(prayer_settings.get(
            "duration_sec", dee_prayer_service.DEFAULT_SETTINGS["duration_sec"]))

        try:
            city, times = dee_prayer_service.get_today_times()
        except Exception:
            city, times = None, None

        now_time = datetime.datetime.now().time()
        next_name = None
        if times:
            upcoming = [n for n in _PRAYER_ORDER if n in times and times[n] > now_time]
            next_name = upcoming[0] if upcoming else None
            self.prayer_location_tb.Text = u"Today's times for {0}".format(city)
        else:
            self.prayer_location_tb.Text = (
                u"Prayer times not available - either this PC's time zone "
                u"isn't recognized, or there's no internet connection.")

        for name in _PRAYER_ORDER:
            tb = getattr(self, _PRAYER_TB_NAMES[name])
            if times and name in times:
                tb.Text = times[name].strftime("%H:%M")
                if name == next_name:
                    tb.Foreground = SolidColorBrush(_ACTIVE_COLOR)
                elif times[name] <= now_time:
                    tb.Foreground = SolidColorBrush(_TIME_DIM_COLOR)
                else:
                    tb.Foreground = SolidColorBrush(_TIME_COLOR)
            else:
                tb.Text = u"—"
                tb.Foreground = SolidColorBrush(_TIME_DIM_COLOR)

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

    def prayer_setting_changed(self, sender, args):
        settings = dee_prayer_service.load_settings()
        settings["enabled"] = bool(self.prayer_enabled_cb.IsChecked)
        dee_prayer_service.save_settings(settings)

    def prayer_duration_changed(self, sender, args):
        settings = dee_prayer_service.load_settings()
        try:
            seconds = int(float(self.prayer_duration_tb.Text))
        except Exception:
            seconds = dee_prayer_service.DEFAULT_SETTINGS["duration_sec"]
        seconds = max(1, min(seconds, 120))
        self.prayer_duration_tb.Text = str(seconds)
        settings["duration_sec"] = seconds
        dee_prayer_service.save_settings(settings)

    def close_click(self, sender, args):
        self.Close()


window = UserInfoWindow(_XAML_FILE)
window.ShowDialog()
