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
  message - text, an optional image, and a choice of auto-dismiss vs.
  stays-until-Close - that shows up as a toast on every user's PC via
  lib/dee_broadcast_service.py. A non-admin sees an explanatory message
  instead of the compose box - gated first by the locally cached access
  status (instant, no network), then re-verified live right before an
  actual send so a stale cache can never let a since-demoted admin send.

Prayer Times and DeeCall's own "notification style" choice (auto-dismiss
vs. requires a Close click) both go through lib/dee_toast.py's
requires_ack - see that module's docstring for how the toast itself
renders each style.
"""
import os
import datetime

import clr
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
clr.AddReference("WindowsBase")
clr.AddReference("System.Windows.Forms")
clr.AddReference("System.Drawing")
from System import Convert
from System.IO import MemoryStream
from System.Windows import Visibility, Thickness
from System.Windows.Input import Cursors
from System.Windows.Media import SolidColorBrush, Color, Brushes
from System.Windows.Media.Imaging import BitmapImage, BitmapCacheOption
from System.Windows.Forms import OpenFileDialog, DialogResult
from System.Drawing import Bitmap, Graphics, Image as DrawingImage
from System.Drawing.Drawing2D import InterpolationMode
from System.Drawing.Imaging import ImageFormat

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

# Same brand orange DeeLazy's own hover cards use (dee_branding's footer
# bar color) - reused here so a hovered card in this window reads as
# part of the same extension rather than a generic Windows highlight.
_HOVER_ACCENT = Color.FromRgb(0xF2, 0x99, 0x4D)
_HOVER_BORDER = SolidColorBrush(_HOVER_ACCENT)
_HOVER_FILL = SolidColorBrush(Color.FromArgb(0x28, 0xF2, 0x99, 0x4D))
_IDLE_BORDER = Brushes.Gray

_MAX_IMAGE_DIM = 480
# ~260KB decoded - generous for a resized photo while keeping the
# Supabase row (and every recipient's download) small; the RPC enforces
# the same cap server-side.
_MAX_IMAGE_B64_CHARS = 350000
_IMAGE_FILE_FILTER = "Image Files (*.png;*.jpg;*.jpeg;*.bmp;*.gif)|*.png;*.jpg;*.jpeg;*.bmp;*.gif"


def _resize_and_encode_image(path):
    """Loads an image file, downscales it to fit _MAX_IMAGE_DIM (keeps
    aspect ratio, never upscales a smaller source), re-encodes as JPEG.
    Returns (base64_str, jpeg_bytes) - both needed, one for the RPC
    body, one to build the local preview from without decoding twice.
    Raises on failure - the caller shows the actual error, since a bad
    file here is a one-off user mistake worth surfacing, not something
    to silently swallow the way a toast's own decode does."""
    original = DrawingImage.FromFile(path)
    try:
        w, h = original.Width, original.Height
        scale = min(1.0, float(_MAX_IMAGE_DIM) / max(w, h))
        new_w, new_h = max(1, int(w * scale)), max(1, int(h * scale))
        resized = Bitmap(new_w, new_h)
        try:
            g = Graphics.FromImage(resized)
            try:
                g.InterpolationMode = InterpolationMode.HighQualityBicubic
                g.DrawImage(original, 0, 0, new_w, new_h)
            finally:
                g.Dispose()
            stream = MemoryStream()
            try:
                resized.Save(stream, ImageFormat.Jpeg)
                data = stream.ToArray()
            finally:
                stream.Dispose()
        finally:
            resized.Dispose()
    finally:
        original.Dispose()
    return Convert.ToBase64String(data), data

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
        self._wire_card(self.prayer_card_b, self.prayer_card_click)
        self._wire_card(self.deecall_card_b, self.deecall_card_click)
        self._refresh()

    def _wire_card(self, border, on_click):
        """Whole-card hover highlight + click, same pattern as DeeLazy's
        module launcher cards (DeeLazy.pushbutton/controller.py) - the
        Border IS the button, no separate Click-only control."""
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

        border.MouseEnter += on_enter
        border.MouseLeave += on_leave
        border.MouseLeftButtonUp += on_click

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

        requires_ack = bool(prayer_settings.get("requires_ack", False))
        self.prayer_ack_required_rb.IsChecked = requires_ack
        self.prayer_ack_auto_rb.IsChecked = not requires_ack

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

    def prayer_ack_changed(self, sender, args):
        settings = dee_prayer_service.load_settings()
        settings["requires_ack"] = bool(self.prayer_ack_required_rb.IsChecked)
        dee_prayer_service.save_settings(settings)

    def close_click(self, sender, args):
        self.Close()


class DeeCallWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self._image_base64 = None
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

    def attach_image_click(self, sender, args):
        dlg = OpenFileDialog()
        dlg.Filter = _IMAGE_FILE_FILTER
        dlg.Title = "Pick an Image"
        if dlg.ShowDialog() != DialogResult.OK:
            return
        try:
            b64, jpeg_bytes = _resize_and_encode_image(dlg.FileName)
        except Exception as e:
            forms.alert(u"Could not read that image:\n{0}".format(e))
            return
        if len(b64) > _MAX_IMAGE_B64_CHARS:
            forms.alert(u"That image is still too large after resizing - "
                        u"try a simpler picture or a smaller source file.")
            return

        self._image_base64 = b64
        try:
            bmp = BitmapImage()
            bmp.BeginInit()
            bmp.CacheOption = BitmapCacheOption.OnLoad
            bmp.StreamSource = MemoryStream(jpeg_bytes)
            bmp.EndInit()
            bmp.Freeze()
            self.image_preview_img.Source = bmp
        except Exception:
            pass
        self.image_preview_border.Visibility = Visibility.Visible
        self.remove_image_b.Visibility = Visibility.Visible
        self.image_status_tb.Text = u"{0} KB attached".format(len(jpeg_bytes) // 1024 + 1)

    def remove_image_click(self, sender, args):
        self._image_base64 = None
        self.image_preview_img.Source = None
        self.image_preview_border.Visibility = Visibility.Collapsed
        self.remove_image_b.Visibility = Visibility.Collapsed
        self.image_status_tb.Text = u"No image attached"

    def send_click(self, sender, args):
        text = (self.message_tb.Text or u"").strip()
        if not text:
            forms.alert(u"Type a message first.", title="Dee.extension - DeeCall")
            return
        requires_ack = bool(self.deecall_ack_required_rb.IsChecked)
        image_base64 = self._image_base64
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

        ok, reason = dee_broadcast_service.send_message(
            email, text, requires_ack=requires_ack, image_base64=image_base64)
        if ok:
            self.message_tb.Text = ""
            self.remove_image_click(sender, args)
            self.send_status_tb.Text = u"Sent - everyone, including you, will see it as a toast."
        else:
            self.send_status_tb.Text = u"Could not send - {0}".format(reason)

    def close_click(self, sender, args):
        self.Close()


window = NotificationCenterWindow(_HUB_XAML)
window.ShowDialog()
