# -*- coding: utf-8 -*-
"""
dee_prayer_service
A prayer-time reminder that shows a small toast, bottom-right of the
screen, on its own - no button click - at each of the 5 daily prayer
times while Revit is open. On by default for every user, self-service
(each person can turn it off or change how long it stays on screen from
the User Info window - UserInfo.pushbutton), purely local to their own
PC. Wired in from startup.py, not a pushbutton itself.

--------------------------------------------------------------------
The one genuinely new mechanism in this repo: running continuously in
the background, with no click at all
--------------------------------------------------------------------
Every other tool here is click-triggered. This subscribes a handler to
UIApplication.Idling (from startup.py, which is already proven in this
repo to run once automatically on every Revit launch/Reload - it
already drives the auto-update check) - Idling is Revit's own standard
"call me back periodically, only when it's safe" event, confirmed via
Autodesk's API docs, and it fires even with zero documents open, which
fits: prayer time has nothing to do with any project.

HONEST RISK, not hidden: whether a `+=` subscription made from a
pyRevit startup.py script survives and keeps firing for an entire Revit
session - as opposed to being torn down the way per-click script state
was proven to reset (see feedback_pyrevit_persistent_engine_globals.md)
- has no confirmed pyRevit-specific precedent. The reasoning it should
work: a .NET event subscription is a reference held by the long-lived
UIApplication object itself, not something that depends on any script's
own execution context staying alive - a different situation from the
per-click Python-globals issue. But "should work" is not "confirmed" -
this needs real live testing across a genuinely long idle stretch, not
just a quick check right after Reload.

start_watching() unsubscribes before subscribing (`-=` then `+=`) so
calling it again after a Reload can never stack up duplicate
subscriptions (which would fire the same notification multiple times),
regardless of whether this module happened to be freshly re-imported or
was still cached from before.

Idling can fire many times a second while Revit sits idle - the handler
throttles itself to at most once per ~30s (a module-level last-checked
timestamp) and never does real work on every tick.

--------------------------------------------------------------------
Prayer times: Aladhan (free, no key) + a maintained timezone lookup
--------------------------------------------------------------------
Aladhan needs an IANA timezone name ("Africa/Cairo") plus coordinates,
but .NET only exposes TimeZoneInfo.Local.Id as a *Windows* zone ID
("Egypt Standard Time") - there is no reliable Windows-to-IANA
conversion available on Revit's .NET Framework runtime. Same shape as
DeeFamily's Discipline map this session: a maintained, best-effort
lookup table, not a system API value. A PC whose zone isn't in the
table gets no notifications at all - never a guessed/wrong location.

Fetched once per calendar day (cached in memory, re-fetched on date
rollover), with a short HTTP timeout so a dead network can't stall
Revit's UI thread inside the Idling handler - only the network call
happens here, no Revit API access at all, so no threading concerns
apply (contrast lib/dee_telemetry.py's earlier bug, which was about
calling Revit API - not plain HTTP - off the main thread).
"""
import datetime
import json

import clr
clr.AddReference("System")
clr.AddReference("System.Net.Http")
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
clr.AddReference("WindowsBase")

from System import TimeSpan, TimeZoneInfo
from System.Net.Http import HttpClient, HttpRequestMessage, HttpMethod
from System.Windows import (
    Window, WindowStyle, ResizeMode, Thickness, CornerRadius,
    SystemParameters, FontWeights)
from System.Windows.Controls import StackPanel, TextBlock, Border
from System.Windows.Media import SolidColorBrush, Color, Brushes
from System.Windows.Threading import DispatcherTimer

import deew_settings

TOOL_NAME = "DeePrayer"
DEFAULT_SETTINGS = {"enabled": True, "duration_sec": 10}

_CHECK_INTERVAL = datetime.timedelta(seconds=30)
_NOTIFY_WINDOW = datetime.timedelta(minutes=2)
_PRAYER_NAMES = ["Fajr", "Dhuhr", "Asr", "Maghrib", "Isha"]

# Windows Time Zone ID -> (IANA name, latitude, longitude, display city).
# Best-effort, maintained lookup - see module docstring for why this
# can't just be derived from .NET. Add more zones here by hand as real
# users hit gaps (a genuinely unmapped zone silently gets no
# notifications - see below - rather than a wrong guess). The display
# city is shown in the User Info window so a user can sanity-check the
# location their times are being computed for.
_TZ_TABLE = {
    "Egypt Standard Time": ("Africa/Cairo", 30.0444, 31.2357, "Cairo"),
    "Arabian Standard Time": ("Asia/Dubai", 25.2048, 55.2708, "Dubai"),
    "Arab Standard Time": ("Asia/Riyadh", 24.7136, 46.6753, "Riyadh"),
    "Jordan Standard Time": ("Asia/Amman", 31.9454, 35.9284, "Amman"),
    "Syria Standard Time": ("Asia/Damascus", 33.5138, 36.2765, "Damascus"),
    "Israel Standard Time": ("Asia/Jerusalem", 31.7683, 35.2137, "Jerusalem"),
    "Turkey Standard Time": ("Europe/Istanbul", 41.0082, 28.9784, "Istanbul"),
    "GTB Standard Time": ("Europe/Bucharest", 44.4268, 26.1025, "Bucharest"),
    "GMT Standard Time": ("Europe/London", 51.5074, -0.1278, "London"),
    "W. Europe Standard Time": ("Europe/Berlin", 52.5200, 13.4050, "Berlin"),
    "Central Europe Standard Time": ("Europe/Warsaw", 52.2297, 21.0122, "Warsaw"),
    "Romance Standard Time": ("Europe/Paris", 48.8566, 2.3522, "Paris"),
    "Eastern Standard Time": ("America/New_York", 40.7128, -74.0060, "New York"),
    "Central Standard Time": ("America/Chicago", 41.8781, -87.6298, "Chicago"),
    "Mountain Standard Time": ("America/Denver", 39.7392, -104.9903, "Denver"),
    "Pacific Standard Time": ("America/Los_Angeles", 34.0522, -118.2437, "Los Angeles"),
    "India Standard Time": ("Asia/Kolkata", 28.6139, 77.2090, "Delhi"),
    "Pakistan Standard Time": ("Asia/Karachi", 24.8607, 67.0011, "Karachi"),
    "Bangladesh Standard Time": ("Asia/Dhaka", 23.8103, 90.4125, "Dhaka"),
    "SE Asia Standard Time": ("Asia/Jakarta", -6.2088, 106.8456, "Jakarta"),
    "Singapore Standard Time": ("Asia/Kuala_Lumpur", 3.1390, 101.6869, "Kuala Lumpur"),
    "China Standard Time": ("Asia/Shanghai", 31.2304, 121.4737, "Shanghai"),
    "AUS Eastern Standard Time": ("Australia/Sydney", -33.8688, 151.2093, "Sydney"),
    "Morocco Standard Time": ("Africa/Casablanca", 33.5731, -7.5898, "Casablanca"),
    "W. Central Africa Standard Time": ("Africa/Lagos", 6.5244, 3.3792, "Lagos"),
}

_client = HttpClient()
_client.Timeout = TimeSpan.FromSeconds(8)

_state = {"last_check": None, "date": None, "times": None, "notified": set()}


def load_settings():
    return deew_settings.load(TOOL_NAME, dict(DEFAULT_SETTINGS))


def save_settings(settings):
    deew_settings.save(TOOL_NAME, settings)


def _location():
    try:
        tz_id = TimeZoneInfo.Local.Id
    except Exception:
        return None
    return _TZ_TABLE.get(tz_id)


def _fetch_today_times(lat, lon, tz_name):
    date_str = datetime.date.today().strftime("%d-%m-%Y")
    url = ("https://api.aladhan.com/v1/timings/{0}"
           "?latitude={1}&longitude={2}&timezonestring={3}&method=3"
           .format(date_str, lat, lon, tz_name))
    try:
        request = HttpRequestMessage(HttpMethod.Get, url)
        response = _client.SendAsync(request).Result
        if not response.IsSuccessStatusCode:
            return None
        body = response.Content.ReadAsStringAsync().Result
        data = json.loads(body)
        timings = (data.get("data") or {}).get("timings") or {}
        result = {}
        for name in _PRAYER_NAMES:
            raw = timings.get(name)
            if not raw:
                continue
            hhmm = raw.split(" ")[0]
            h, m = hhmm.split(":")
            result[name] = datetime.time(int(h), int(m))
        return result or None
    except Exception:
        return None


def _show_toast(prayer_name, prayer_time, duration_sec):
    try:
        width, height, margin = 300.0, 86.0, 16.0

        window = Window()
        window.WindowStyle = getattr(WindowStyle, "None")
        window.ResizeMode = ResizeMode.NoResize
        window.ShowInTaskbar = False
        window.Topmost = True
        window.AllowsTransparency = True
        window.Background = Brushes.Transparent
        window.Width = width
        window.Height = height

        border = Border()
        border.Background = SolidColorBrush(Color.FromRgb(0x1E, 0x22, 0x2D))
        border.BorderBrush = SolidColorBrush(Color.FromRgb(0x3E, 0xCF, 0x8E))
        border.BorderThickness = Thickness(1)
        border.CornerRadius = CornerRadius(8)
        border.Padding = Thickness(14)

        stack = StackPanel()

        title = TextBlock()
        title.Text = u"Prayer Time — Dee.extension"
        title.Foreground = SolidColorBrush(Color.FromRgb(0x8B, 0x93, 0xA7))
        title.FontSize = 11
        stack.Children.Add(title)

        name_tb = TextBlock()
        name_tb.Text = prayer_name
        name_tb.Foreground = SolidColorBrush(Color.FromRgb(0x3E, 0xCF, 0x8E))
        name_tb.FontSize = 20
        name_tb.FontWeight = FontWeights.Bold
        name_tb.Margin = Thickness(0, 2, 0, 2)
        stack.Children.Add(name_tb)

        time_tb = TextBlock()
        time_tb.Text = prayer_time.strftime("%H:%M")
        time_tb.Foreground = SolidColorBrush(Color.FromRgb(0xE7, 0xE9, 0xEE))
        time_tb.FontSize = 14
        stack.Children.Add(time_tb)

        border.Child = stack
        window.Content = border

        work_area = SystemParameters.WorkArea
        window.Left = work_area.Right - width - margin
        window.Top = work_area.Bottom - height - margin

        window.Show()

        timer = DispatcherTimer()
        timer.Interval = TimeSpan.FromSeconds(max(1, duration_sec))

        def _on_tick(sender, args):
            try:
                timer.Stop()
            except Exception:
                pass
            try:
                window.Close()
            except Exception:
                pass

        timer.Tick += _on_tick
        timer.Start()
    except Exception:
        pass


def _ensure_today_cached(loc):
    """Shared by the Idling handler and get_today_times() - one cache,
    so a User Info window open doesn't force a redundant fetch on a day
    the watcher already fetched, and the watcher doesn't re-fetch just
    because the window happened to trigger the first fetch of the day."""
    tz_name, lat, lon, _city = loc
    today_str = datetime.date.today().strftime("%Y-%m-%d")
    if _state["date"] != today_str:
        _state["date"] = today_str
        _state["times"] = _fetch_today_times(lat, lon, tz_name)
        _state["notified"] = set()
    return _state["times"]


def get_today_times():
    """Best-effort, for display (e.g. the User Info window) - returns
    (city_label, {prayer_name: datetime.time}) or (None, None) if the
    PC's time zone isn't in _TZ_TABLE or the fetch fails. May make a
    network call the first time it's asked on a given day (short
    timeout already set on the shared HttpClient) - every later call
    that same day reuses the cache instantly, same as the watcher."""
    loc = _location()
    if loc is None:
        return None, None
    times = _ensure_today_cached(loc)
    if not times:
        return None, None
    return loc[3], times


def _check_and_notify():
    now = datetime.datetime.now()
    last = _state["last_check"]
    if last is not None and (now - last) < _CHECK_INTERVAL:
        return
    _state["last_check"] = now

    settings = load_settings()
    if not settings.get("enabled", True):
        return

    loc = _location()
    if loc is None:
        return

    times = _ensure_today_cached(loc)
    if not times:
        return

    now = datetime.datetime.now()
    for name, prayer_time in times.items():
        if name in _state["notified"]:
            continue
        prayer_dt = datetime.datetime.combine(now.date(), prayer_time)
        delta = now - prayer_dt
        if datetime.timedelta(0) <= delta <= _NOTIFY_WINDOW:
            _state["notified"].add(name)
            duration = settings.get("duration_sec", DEFAULT_SETTINGS["duration_sec"])
            _show_toast(name, prayer_time, duration)


def _on_idling(sender, args):
    try:
        _check_and_notify()
    except Exception:
        pass


def start_watching(uiapp):
    """Call once from startup.py. `-=` before `+=` makes this safe to
    call again on a Reload without stacking duplicate subscriptions -
    see module docstring."""
    try:
        uiapp.Idling -= _on_idling
    except Exception:
        pass
    try:
        uiapp.Idling += _on_idling
    except Exception:
        pass
