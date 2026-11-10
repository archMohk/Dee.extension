# -*- coding: utf-8 -*-
"""
dee_theme_motion
The motions behind the moving themes (lib/dee_themes.MOTION: Dynamic and
Aurora). Applied to every Dee window (DeeBrandedWindow) as it opens, and
to the ones already open when the user clicks Apply in About -> Interface.
(Ribbon icon motion is a separate thing: lib/dee_motion.py.)

Five motions, all WPF animations that run on the compositor, so a tool's
own code never sees them:

  glow      The window background becomes a slow-drifting gradient: its
            colours cycle and its angle sways. Dynamic tints the window
            colour with the user's accent (and a neighbouring hue), Aurora
            drifts through teal, violet and deep blue. Skipped while the
            Glass effect is on - that owns the background.
  breathe   The accent (DeeOrange: progress bars, the accent top line and
            title bar) breathes gently between the accent and a lighter
            tint.
  shimmer   A soft highlight sweeps along the website bar every few
            seconds.
  lift      Buttons (not check boxes or radio buttons) rise slightly under
            the mouse and settle back when it leaves. New buttons a script
            adds later are picked up by a light, throttled scan (the same
            idea as lib/dee_dark.py).
  glide     The content fades and scales in when the window opens (left
            to the Fade in effect when that is ticked).

stop(window) undoes everything, so the Interface window can switch a
theme on and off live. Nothing here raises into a caller: a motion that
can't be applied is skipped and the window looks as it always did.
"""
import colorsys

import clr
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
clr.AddReference("WindowsBase")
from System import TimeSpan  # noqa: E402
from System.Windows import Duration, Point, Window, DependencyPropertyHelper, BaseValueSource  # noqa: E402
from System.Windows.Controls import Button  # noqa: E402
from System.Windows.Media import (SolidColorBrush, LinearGradientBrush, GradientStop,  # noqa: E402
                                  Color, Colors, ScaleTransform, VisualTreeHelper, Visual)
from System.Windows import UIElement  # noqa: E402
from System.Windows.Media.Animation import (ColorAnimation, DoubleAnimation, PointAnimation,  # noqa: E402
                                            RepeatBehavior, SineEase, QuadraticEase, EasingMode)
from System.Windows.Threading import DispatcherTimer  # noqa: E402

import dee_themes

LIFT_SCALE = 1.06
BREATH_SECONDS = 2.8
SHIMMER_SECONDS = 4.2
GLIDE_MS = 280


# ---------------- colours ----------------
def hex_color(value):
    value = value.lstrip("#")
    return Color.FromRgb(int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def mix(a, b, t):
    return Color.FromRgb(int(round(a.R + (b.R - a.R) * t)),
                         int(round(a.G + (b.G - a.G) * t)),
                         int(round(a.B + (b.B - a.B) * t)))


def lighten(c, t):
    return mix(c, Colors.White, t)


def shift_hue(c, degrees):
    h, l, s = colorsys.rgb_to_hls(c.R / 255.0, c.G / 255.0, c.B / 255.0)
    h = (h + degrees / 360.0) % 1.0
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return Color.FromRgb(int(round(r * 255)), int(round(g * 255)), int(round(b * 255)))


def _resource_color(window, key, fallback):
    try:
        res = window.TryFindResource(key)
        if isinstance(res, Color):
            return res
        if isinstance(res, SolidColorBrush):
            return res.Color
    except Exception:
        pass
    return fallback


def _ease():
    e = SineEase()
    e.EasingMode = EasingMode.EaseInOut
    return e


def _seconds(s):
    return Duration(TimeSpan.FromSeconds(s))


def _forever(anim, begin=0.0):
    anim.AutoReverse = True
    anim.RepeatBehavior = RepeatBehavior.Forever
    anim.EasingFunction = _ease()
    if begin:
        anim.BeginTime = TimeSpan.FromSeconds(begin)
    return anim


# ---------------- the brushes ----------------
def gradient_brush(colors, period):
    """A gradient whose stops drift through `colors` (each stop cycles to
    the next colour) while its angle sways. Used for the window glow and
    the theme cards in the Interface window."""
    brush = LinearGradientBrush()
    brush.StartPoint = Point(0, 0)
    brush.EndPoint = Point(1, 1)
    n = len(colors)
    for i, c in enumerate(colors):
        stop = GradientStop(c, i / float(max(1, n - 1)))
        nxt = colors[(i + 1) % n]
        stop.BeginAnimation(GradientStop.ColorProperty,
                            _forever(ColorAnimation(c, nxt, _seconds(period)), i * period / 3.0))
        brush.GradientStops.Add(stop)
    brush.BeginAnimation(LinearGradientBrush.StartPointProperty,
                         _forever(PointAnimation(Point(0, 0), Point(0.35, 0), _seconds(period * 1.6))))
    brush.BeginAnimation(LinearGradientBrush.EndPointProperty,
                         _forever(PointAnimation(Point(1, 1), Point(0.65, 1), _seconds(period * 1.6)),
                                  period * 0.4))
    return brush


def glow_colors(key, bg, accent):
    """The colours a theme's glow drifts through, from the window colour
    `bg` and the user's accent (both Colors)."""
    spec = dee_themes.motion(key) or {}
    blend = spec.get("blend", 0.14)
    if spec.get("glow"):
        return [mix(bg, hex_color(h), blend) for h in spec["glow"]]
    return [mix(bg, accent, blend), bg, mix(bg, shift_hue(accent, 70), blend)]


def _window_glow_colors(window, key):
    bg = _resource_color(window, "DeeWindowBg", hex_color(dee_themes.colors(key)["DeeWindowBg"]))
    accent = _resource_color(window, "DeeOrangeColor", hex_color("#CC7020"))
    return glow_colors(key, bg, accent)


def shimmer_brush(color):
    """The website bar with a soft highlight sweeping along it."""
    brush = LinearGradientBrush()
    brush.StartPoint = Point(0, 0.5)
    brush.EndPoint = Point(1, 0.5)
    light = lighten(color, 0.38)
    brush.GradientStops.Add(GradientStop(color, 0.0))
    for shift in (-0.18, 0.0, 0.18):
        stop = GradientStop(light if shift == 0.0 else color, -0.4 + shift)
        anim = DoubleAnimation(-0.4 + shift, 1.4 + shift, _seconds(SHIMMER_SECONDS))
        anim.RepeatBehavior = RepeatBehavior.Forever
        anim.EasingFunction = _ease()
        stop.BeginAnimation(GradientStop.OffsetProperty, anim)
        brush.GradientStops.Add(stop)
    brush.GradientStops.Add(GradientStop(color, 1.0))
    return brush


# ---------------- the motions ----------------
def _state(window):
    st = getattr(window, "_dee_motion", None)
    if st is None:
        st = {"key": None, "brushes": [], "lifted": [], "timer": None,
              "bg": None, "bar": None, "handlers": []}
        try:
            window._dee_motion = st
        except Exception:
            return None
    return st


def _glow(window, st, key):
    try:
        src = DependencyPropertyHelper.GetValueSource(window, Window.BackgroundProperty)
        local = src.BaseValueSource == BaseValueSource.Local
    except Exception:
        local = False
    had = window.Resources.Contains("DeeWindowBg")
    st["bg"] = (local, window.Background if local else None,
                window.Resources["DeeWindowBg"] if had else None)
    period = (dee_themes.motion(key) or {}).get("period", 9.0)
    brush = gradient_brush(_window_glow_colors(window, key), period)
    window.Resources["DeeWindowBg"] = brush      # shaped windows paint their body with it
    if not getattr(window, "_dee_rounded", False):
        window.Background = brush


def _unglow(window, st):
    saved = st.get("bg")
    if saved is None:
        return
    local, background, resource = saved
    try:
        if window.Resources.Contains("DeeWindowBg"):
            window.Resources.Remove("DeeWindowBg")
        if resource is not None:
            window.Resources["DeeWindowBg"] = resource
    except Exception:
        pass
    try:
        if not getattr(window, "_dee_rounded", False):
            if local:
                window.Background = background
            else:
                window.ClearValue(Window.BackgroundProperty)
    except Exception:
        pass
    st["bg"] = None


def _breathe(window, st):
    # A brush stored in a ResourceDictionary gets frozen, so the stored
    # one can't be animated: a fresh brush is animated first (an animated
    # brush can't freeze) and then put in the window's own Resources,
    # where every DynamicResource user picks it up.
    for key in ("DeeOrange", "DeeAccent"):
        try:
            current = window.TryFindResource(key)
            if not isinstance(current, SolidColorBrush):
                continue
            c = current.Color
            had = window.Resources.Contains(key)
            brush = SolidColorBrush(c)
            brush.BeginAnimation(SolidColorBrush.ColorProperty,
                                 _forever(ColorAnimation(c, lighten(c, 0.24), _seconds(BREATH_SECONDS))))
            window.Resources[key] = brush
            st["brushes"].append((key, c, had))
        except Exception:
            pass


def _unbreathe(window, st):
    for key, c, had in st["brushes"]:
        try:
            if had:
                window.Resources[key] = SolidColorBrush(c)
            elif window.Resources.Contains(key):
                window.Resources.Remove(key)
        except Exception:
            pass
    st["brushes"] = []


def _shimmer(window, st):
    color = _resource_color(window, "DeeBarColor", None)
    if color is None:
        return
    st["bar"] = color
    window.Resources["DeeBar"] = shimmer_brush(color)


def _unshimmer(window, st):
    color = st.get("bar")
    if color is None:
        return
    try:
        window.Resources["DeeBar"] = SolidColorBrush(color)
    except Exception:
        pass
    st["bar"] = None


def _lift_button(button, st):
    try:
        rt = button.RenderTransform
        if rt is not None and not rt.Value.IsIdentity:
            return      # the tool animates / transforms it itself
        scale = ScaleTransform(1.0, 1.0)
        button.RenderTransformOrigin = Point(0.5, 0.5)
        button.RenderTransform = scale

        def _to(value, ms):
            ease = QuadraticEase()
            ease.EasingMode = EasingMode.EaseOut
            for prop in (ScaleTransform.ScaleXProperty, ScaleTransform.ScaleYProperty):
                anim = DoubleAnimation(value, Duration(TimeSpan.FromMilliseconds(ms)))
                anim.EasingFunction = ease
                scale.BeginAnimation(prop, anim)

        def _enter(sender, args):
            _to(LIFT_SCALE, 140)

        def _leave(sender, args):
            _to(1.0, 200)

        button.MouseEnter += _enter
        button.MouseLeave += _leave
        st["lifted"].append((button, scale, _enter, _leave))
    except Exception:
        pass


def _scan_buttons(window, st):
    done = set(id(b) for b, _s, _e, _l in st["lifted"])
    stack = [window]
    while stack:
        obj = stack.pop()
        if isinstance(obj, Button) and id(obj) not in done:
            _lift_button(obj, st)
            done.add(id(obj))
        try:
            if isinstance(obj, Visual):
                for i in range(VisualTreeHelper.GetChildrenCount(obj)):
                    stack.append(VisualTreeHelper.GetChild(obj, i))
        except Exception:
            pass


def _lift(window, st):
    _scan_buttons(window, st)
    timer = DispatcherTimer()
    timer.Interval = TimeSpan.FromMilliseconds(700)

    def _tick(sender, args):
        timer.Stop()
        try:
            _scan_buttons(window, st)
        except Exception:
            pass

    def _changed(sender, args):
        if not timer.IsEnabled:
            timer.Start()

    timer.Tick += _tick
    window.LayoutUpdated += _changed
    st["timer"] = timer
    st["handlers"].append(("LayoutUpdated", _changed))


def _unlift(window, st):
    timer = st.get("timer")
    if timer is not None:
        try:
            timer.Stop()
        except Exception:
            pass
    for name, handler in st["handlers"]:
        try:
            if name == "LayoutUpdated":
                window.LayoutUpdated -= handler
        except Exception:
            pass
    st["handlers"] = []
    st["timer"] = None
    for button, scale, enter, leave in st["lifted"]:
        try:
            button.MouseEnter -= enter
            button.MouseLeave -= leave
            scale.BeginAnimation(ScaleTransform.ScaleXProperty, None)
            scale.BeginAnimation(ScaleTransform.ScaleYProperty, None)
            if button.RenderTransform is scale:
                button.ClearValue(UIElement.RenderTransformProperty)
        except Exception:
            pass
    st["lifted"] = []


def _glide(window):
    try:
        content = window.Content
        if content is None:
            return
        rt = content.RenderTransform
        if rt is not None and not rt.Value.IsIdentity:
            return      # the Fade in effect (or the tool) moves it already
        scale = ScaleTransform(0.97, 0.97)
        content.RenderTransformOrigin = Point(0.5, 0.5)
        content.RenderTransform = scale
        ease = QuadraticEase()
        ease.EasingMode = EasingMode.EaseOut
        fade = DoubleAnimation(0.0, 1.0, Duration(TimeSpan.FromMilliseconds(GLIDE_MS)))
        fade.EasingFunction = ease
        content.BeginAnimation(UIElement.OpacityProperty, fade)
        for prop in (ScaleTransform.ScaleXProperty, ScaleTransform.ScaleYProperty):
            grow = DoubleAnimation(0.97, 1.0, Duration(TimeSpan.FromMilliseconds(GLIDE_MS)))
            grow.EasingFunction = ease
            scale.BeginAnimation(prop, grow)
    except Exception:
        pass


# ---------------- entry points ----------------
def _effects():
    try:
        import dee_effects
        return dee_effects.load_effects()
    except Exception:
        return {}


def apply(window, key, effects=None, glide=False):
    """Starts the theme's motions on a window that is on screen (stopping
    any that ran before). A key without motion = stop."""
    key = dee_themes.normalize(key)
    st = _state(window)
    if st is None:
        return
    stop(window)
    if not dee_themes.has_motion(key):
        return
    effects = effects if effects is not None else _effects()
    st["key"] = key
    for step in (lambda: not effects.get("glass") and _glow(window, st, key),
                 lambda: _breathe(window, st),
                 lambda: _shimmer(window, st),
                 lambda: _lift(window, st),
                 lambda: glide and not effects.get("fade") and _glide(window)):
        try:
            step()
        except Exception:
            pass


def stop(window):
    """Removes every motion and puts the window's colours back."""
    st = getattr(window, "_dee_motion", None)
    if st is None or st.get("key") is None:
        return
    for step in (lambda: _unlift(window, st),
                 lambda: _unshimmer(window, st),
                 lambda: _unbreathe(window, st),
                 lambda: _unglow(window, st)):
        try:
            step()
        except Exception:
            pass
    st["key"] = None


def attach(window, key):
    """Called once per new Dee window: starts the motions as the window
    is shown, with the glide-in."""
    key = dee_themes.normalize(key)
    if not dee_themes.has_motion(key):
        return

    def _loaded(sender, args):
        try:
            apply(window, key, glide=True)
        except Exception:
            pass

    def _closed(sender, args):
        try:
            stop(window)
        except Exception:
            pass

    try:
        window.Loaded += _loaded
        window.Closed += _closed
    except Exception:
        pass
