# -*- coding: utf-8 -*-
"""
Notification Center (About)
Hub for everything this extension pops up as a toast. Three windows in
one bundle - not DeeLazy's growing-registry modules/ pattern, since
there are exactly 2 fixed sub-features here, not an open-ended list:

- NotificationCenterWindow: general appearance (position/size/how long
  it stays - lib/dee_toast.py's shared "DeeNotifications" settings,
  used by every kind of toast this extension shows) plus a Test
  Notification button to preview them, and 2 buttons opening the
  sub-windows below.
- PrayerSettingsWindow: moved here from the old User Info window
  (UserInfo.pushbutton no longer shows it) - same fields, same
  lib/dee_prayer_service.py logic, unchanged.
- DeeCallWindow: lets an admin (allowed_users.is_admin) broadcast a
  plain-text message that shows up as a toast on every user's PC via
  lib/dee_broadcast_service.py. A non-admin sees an explanatory message
  instead of the compose box - gated first by the locally cached access
  status (instant, no network), then re-verified live right before an
  actual send so a stale cache can never let a since-demoted admin send.
"""
import os
import datetime

import clr
clr.AddReference("PresentationCore")
from System.Windows import Visibility
from System.Windows.Media import SolidColorBrush, Color

from pyrevit import forms
import dee_branding
import dee_toast
import dee_prayer_service
import dee_broadcast_service
import dee_telemetry

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_HUB_XAML = os.path.join(_THIS_DIR, "notification_center.xaml")
_PRAYER_XAML = os.path.join(_THIS_DIR, "prayer_settings.xaml")
_DEECALL_XAML = os.path.join(_THIS_DIR, "deecall.xaml")

_ACTIVE_COLOR = Color.FromRgb(0x2E, 0x7D, 0x32)
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


class NotificationCenterWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.position_cb.ItemsSource = [label for _code, label in dee_toast.POSITIONS]
        self._refresh()

    def _refresh(self):
        settings = dee_toast.load_settings()
        codes = [code for code, _label in dee_toast.POSITIONS]
        try:
            self.position_cb.SelectedIndex = codes.index(
                settings.get("position", dee_toast.DEFAULT_SETTINGS["position"]))
        except ValueError:
            self.position_cb.SelectedIndex = 0
        self.width_tb.Text = str(settings.get("width", dee_toast.DEFAULT_SETTINGS["width"]))
        self.height_tb.Text = str(settings.get("height", dee_toast.DEFAULT_SETTINGS["height"]))
        self.duration_tb.Text = str(settings.get(
            "duration_sec", dee_toast.DEFAULT_SETTINGS["duration_sec"]))

    def appearance_changed(self, sender, args):
        settings = dee_toast.load_settings()
        codes = [code for code, _label in dee_toast.POSITIONS]
        idx = self.position_cb.SelectedIndex
        if 0 <= idx < len(codes):
            settings["position"] = codes[idx]
        try:
            settings["width"] = dee_toast.clamp_width(int(float(self.width_tb.Text)))
        except Exception:
            settings["width"] = dee_toast.DEFAULT_SETTINGS["width"]
        try:
            settings["height"] = dee_toast.clamp_height(int(float(self.height_tb.Text)))
        except Exception:
            settings["height"] = dee_toast.DEFAULT_SETTINGS["height"]
        try:
            settings["duration_sec"] = dee_toast.clamp_duration(int(float(self.duration_tb.Text)))
        except Exception:
            settings["duration_sec"] = dee_toast.DEFAULT_SETTINGS["duration_sec"]
        dee_toast.save_settings(settings)
        # Reflect any clamping back into the boxes so the field never
        # silently disagrees with what's actually saved.
        self.width_tb.Text = str(settings["width"])
        self.height_tb.Text = str(settings["height"])
        self.duration_tb.Text = str(settings["duration_sec"])

    def test_notification_click(self, sender, args):
        dee_toast.show_toast(
            u"TEST — DEE.EXTENSION", u"Sample Notification",
            u"This is what your notifications will look like.",
            _ACTIVE_COLOR)

    def prayer_card_click(self, sender, args):
        try:
            PrayerSettingsWindow(_PRAYER_XAML).ShowDialog()
        except Exception as e:
            forms.alert(u"Could not open Prayer Times:\n{0}".format(e))

    def deecall_card_click(self, sender, args):
        try:
            DeeCallWindow(_DEECALL_XAML).ShowDialog()
        except Exception as e:
            forms.alert(u"Could not open DeeCall:\n{0}".format(e))

    def close_click(self, sender, args):
        self.Close()


class PrayerSettingsWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self._refresh()

    def _refresh(self):
        # Loaded here in code, not set via XAML attributes - a
        # Checked/Unchecked handler wired in XAML can fire the moment
        # IsChecked is set there, before the rest of this window is
        # ready (see feedback_wpf_xaml_early_event_fire).
        prayer_settings = dee_prayer_service.load_settings()
        self.prayer_enabled_cb.IsChecked = bool(prayer_settings.get("enabled", True))
        self.prayer_reminder_cb.IsChecked = bool(prayer_settings.get("reminder_enabled", False))
        self.prayer_reminder_minutes_tb.Text = str(prayer_settings.get(
            "reminder_minutes", dee_prayer_service.DEFAULT_SETTINGS["reminder_minutes"]))
        is_12h = prayer_settings.get("time_format") == "12"
        self.prayer_format_12_rb.IsChecked = is_12h
        self.prayer_format_24_rb.IsChecked = not is_12h

        self._update_prayer_times_display(prayer_settings)

    def _update_prayer_times_display(self, prayer_settings):
        """Split out from _refresh() so prayer_format_changed can
        re-render just the times without touching the checkbox/radio
        controls themselves - those have Checked/Unchecked handlers
        wired in XAML, and re-setting their IsChecked from inside a
        handler they themselves trigger would recurse."""
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
                tb.Text = dee_prayer_service.format_time(times[name], prayer_settings)
                if name == next_name:
                    tb.Foreground = SolidColorBrush(_ACTIVE_COLOR)
                elif times[name] <= now_time:
                    tb.Foreground = SolidColorBrush(_TIME_DIM_COLOR)
                else:
                    tb.Foreground = SolidColorBrush(_TIME_COLOR)
            else:
                tb.Text = u"—"
                tb.Foreground = SolidColorBrush(_TIME_DIM_COLOR)

    def prayer_setting_changed(self, sender, args):
        settings = dee_prayer_service.load_settings()
        settings["enabled"] = bool(self.prayer_enabled_cb.IsChecked)
        dee_prayer_service.save_settings(settings)

    def prayer_reminder_changed(self, sender, args):
        settings = dee_prayer_service.load_settings()
        settings["reminder_enabled"] = bool(self.prayer_reminder_cb.IsChecked)
        dee_prayer_service.save_settings(settings)

    def prayer_reminder_minutes_changed(self, sender, args):
        settings = dee_prayer_service.load_settings()
        try:
            minutes = int(float(self.prayer_reminder_minutes_tb.Text))
        except Exception:
            minutes = dee_prayer_service.DEFAULT_SETTINGS["reminder_minutes"]
        minutes = max(1, min(minutes, 120))
        self.prayer_reminder_minutes_tb.Text = str(minutes)
        settings["reminder_minutes"] = minutes
        dee_prayer_service.save_settings(settings)

    def prayer_format_changed(self, sender, args):
        settings = dee_prayer_service.load_settings()
        settings["time_format"] = "12" if self.prayer_format_12_rb.IsChecked else "24"
        dee_prayer_service.save_settings(settings)
        self._update_prayer_times_display(settings)

    def close_click(self, sender, args):
        self.Close()


class DeeCallWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self._show_as_admin(self._check_is_admin())

    def _check_is_admin(self):
        """A live check, not the local cache - is_admin was only added
        to check_user_access()'s response partway through this feature's
        own build, so anyone whose LAST tool click predates that change
        has a cached status with no is_admin field at all, which read as
        "not admin" even for the real owner (live-caught: this happened
        to the owner's own account on first open). DeeCall is opened
        rarely and needs network access to actually send anyway, so
        paying for one live check on open - same cost as UserInfo's own
        "Check Now" - is worth it here, unlike UserInfo's own default
        no-network-on-open design. Falls back to the cached value only
        if the live check itself fails (e.g. no internet)."""
        email = dee_telemetry.get_cached_identity()
        if email:
            try:
                fresh = dee_telemetry.refresh_status(email)
                return bool(fresh.get("is_admin"))
            except Exception:
                pass
        status = dee_telemetry.load_cached_status()
        return bool(status and status.get("is_admin"))

    def _show_as_admin(self, is_admin):
        self.admin_panel.Visibility = Visibility.Visible if is_admin else Visibility.Collapsed
        self.not_admin_panel.Visibility = Visibility.Collapsed if is_admin else Visibility.Visible

    def send_click(self, sender, args):
        text = (self.message_tb.Text or u"").strip()
        if not text:
            forms.alert(u"Type a message first.", title="Dee.extension - DeeCall")
            return
        if not forms.alert(
                u"Send this to every Dee.extension user right now?\n\n{0}".format(text),
                title="Dee.extension - DeeCall", yes=True, no=True):
            return

        email = dee_telemetry.get_cached_identity()
        if not email:
            forms.alert(u"No email on file yet - open any Dee tool once first.",
                        title="Dee.extension - DeeCall")
            return

        # Re-verify live right before actually sending - the cached
        # status this window opened with could be stale (e.g. admin
        # access was revoked since the last check).
        try:
            fresh = dee_telemetry.refresh_status(email)
        except Exception as e:
            self.send_status_tb.Text = u"Could not verify access - {0}".format(e)
            return
        if not fresh.get("is_admin"):
            self.send_status_tb.Text = u"You're not authorized to send announcements."
            self._show_as_admin(False)
            return

        ok, reason = dee_broadcast_service.send_message(email, text)
        if ok:
            self.message_tb.Text = ""
            self.send_status_tb.Text = u"Sent - everyone, including you, will see it as a toast."
        else:
            self.send_status_tb.Text = u"Could not send - {0}".format(reason)

    def close_click(self, sender, args):
        self.Close()


window = NotificationCenterWindow(_HUB_XAML)
window.ShowDialog()
