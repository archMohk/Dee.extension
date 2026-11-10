# -*- coding: utf-8 -*-
"""
dee_icon_motion
"Live motion" ribbon icons (About -> Interface -> Icon -> Live motion).

Revit's ribbon cannot play an animated image, but a button's Image and
LargeImage can be swapped at will (dee_force_state does it for DeeForce's
on/off icon). So a motion is a short loop of FRAMES still images per icon,
pre-rendered once from the recoloured, restyled icon (lib/dee_accent.py)
and cached next to the accent icons, and a WPF DispatcherTimer on Revit's
UI thread that plays them on the button the mouse is over. Only the
hovered button moves (from its first frame, the moment the pointer
arrives); when the pointer leaves, its still icon comes back. Nothing is
painted while the DeePack tab is off screen.

Cost: the frames are rendered on a background thread (Revit's first
Idling only lists the buttons - it used to draw ~2,700 PNGs there and
froze Revit for 15-30 s per session), the WPF bitmaps are loaded the
first time a button is hovered, and the timer only ticks while the mouse
is over the ribbon (Autodesk.Windows RibbonControl MouseEnter/MouseLeave).

Who is hovered: Revit's ribbon is WPF, so System.Windows.Input.Mouse
.DirectlyOver gives the element under the pointer; its ancestors'
DataContext is the Autodesk.Windows.RibbonItem of the button, and each
Revit API item knows its Autodesk.Windows twin through the private
getRibbonItem method pyRevit itself relies on.

Motions (MOTIONS): pulse - the accent halo breathes; breathe - the icon
gently grows and shrinks; bob - it floats up and down; shimmer - a light
sweeps across it. Every one is built on the current colours and style, so
the Colours and Icon choices carry straight through.

Stored per PC with the colours ("motion" in lib/.dee_accent.json; None =
off, the default). Nothing here ever raises into a caller.
"""
import math
import os

import clr
clr.AddReference("System.Drawing")
clr.AddReference("WindowsBase")
from System import TimeSpan, Array  # noqa: E402
from System.Drawing import (Bitmap, Graphics, Rectangle, RectangleF,  # noqa: E402
                            PointF, GraphicsUnit)
from System.Drawing.Imaging import PixelFormat  # noqa: E402
from System.Drawing.Drawing2D import (CompositingMode, GraphicsPath,  # noqa: E402
                                      InterpolationMode)

import dee_accent

# (key, label, description)
MOTIONS = [
    ("none", "Off", "Still icons (the default)."),
    ("pulse", "Pulse", "Point at an icon and its accent halo breathes in and out."),
    ("breathe", "Breathe", "Point at an icon and it gently grows and shrinks."),
    ("bob", "Bob", "Point at an icon and it floats up and down."),
    ("shimmer", "Shimmer", "Point at an icon and a soft light sweeps across it."),
]
DEFAULT_MOTION = "none"
_KEYS = [k for k, _l, _d in MOTIONS]
FRAMES = 12
INTERVAL_MS = 90
_TAB_NAME = "DeePack"


# ---------------- the setting ----------------
def normalize(value):
    return value if value in _KEYS else DEFAULT_MOTION


def load_motion():
    return normalize(dee_accent._load_config().get("motion"))


def save_motion(kind):
    kind = normalize(kind)
    dee_accent.update_config({"motion": None if kind == DEFAULT_MOTION else kind})
    return kind


def is_off(kind):
    return normalize(kind) == DEFAULT_MOTION


# ---------------- frame rendering ----------------
def _draw_frame(g, base, w, h, kind, t, accent_hex):
    """One frame of `kind` at phase t in [0, 1) from the styled icon."""
    s = max(1.0, w / 32.0)
    full = Rectangle(0, 0, w, h)
    if kind == "pulse":
        g.DrawImage(base, full, 0, 0, w, h, GraphicsUnit.Pixel)
        level = 0.5 - 0.5 * math.cos(2 * math.pi * t)       # 0 -> 1 -> 0
        for radius, a in ((1.0 * s, 0.28), (2.0 * s, 0.16), (3.0 * s, 0.08)):
            alpha = a * level
            if alpha < 0.01:
                continue
            for ang in range(0, 360, 45):
                dx = radius * math.cos(math.radians(ang))
                dy = radius * math.sin(math.radians(ang))
                dee_accent._draw_recolored(
                    g, base, w, h, dee_accent._silhouette_matrix(accent_hex, alpha), dx, dy)
        # a second pass of the icon keeps its lines crisp over the halo
        g.DrawImage(base, full, 0, 0, w, h, GraphicsUnit.Pixel)
    elif kind == "breathe":
        k = 1.0 - 0.07 * (0.5 - 0.5 * math.cos(2 * math.pi * t))
        dw, dh = w * k, h * k
        g.DrawImage(base, RectangleF((w - dw) / 2.0, (h - dh) / 2.0, dw, dh))
    elif kind == "bob":
        dy = -2.2 * s * math.sin(2 * math.pi * t)
        g.DrawImage(base, RectangleF(0.0, float(dy), float(w), float(h)))
    elif kind == "shimmer":
        g.DrawImage(base, full, 0, 0, w, h, GraphicsUnit.Pixel)
        # the band crosses the icon during the first 60% of the loop, then rests
        phase = t / 0.6
        if phase < 1.0:
            bw = 0.38 * w
            x0 = -bw + phase * (w + 2 * bw + h) - h / 2.0
            path = GraphicsPath()
            path.AddPolygon(Array[PointF]([PointF(x0, float(h)), PointF(x0 + bw, float(h)),
                                           PointF(x0 + bw + h, 0.0), PointF(x0 + h, 0.0)]))
            g.SetClip(path)
            dee_accent._draw_recolored(
                g, base, w, h, dee_accent._silhouette_matrix("#FFFFFF", 0.55))
            g.ResetClip()
            path.Dispose()
    else:
        g.DrawImage(base, full, 0, 0, w, h, GraphicsUnit.Pixel)


def _cache_root(accent_hex, line_hex, style, kind):
    return dee_accent._cache_root(accent_hex, line_hex, style) + "_motion-" + kind


try:
    _RENDERER_MTIME = os.path.getmtime(os.path.abspath(__file__))
except Exception:
    _RENDERER_MTIME = 0


def frame_paths(icon_path, accent_hex=None, line_hex=dee_accent._SAVED,
                style=dee_accent._SAVED, kind=None):
    """The FRAMES PNGs for icon_path in the given motion, built on demand
    and cached. [] when the motion is off or anything fails."""
    try:
        kind = normalize(kind or load_motion())
        if is_off(kind) or not icon_path or not os.path.isfile(icon_path):
            return []
        accent_hex = dee_accent.normalize_hex(accent_hex) or dee_accent.load_accent()
        line_hex = (dee_accent.load_line() if line_hex is dee_accent._SAVED
                    else dee_accent.normalize_hex(line_hex))
        style = (dee_accent.load_style() if style is dee_accent._SAVED
                 else dee_accent.normalize_style(style))
        rel = os.path.relpath(os.path.abspath(icon_path), dee_accent._EXT_ROOT)
        if rel.startswith(".."):
            rel = os.path.basename(icon_path)
        folder = os.path.join(_cache_root(accent_hex, line_hex, style, kind),
                              os.path.splitext(rel)[0])
        paths = [os.path.join(folder, "f{0:02d}.png".format(i)) for i in range(FRAMES)]
        newest = max(os.path.getmtime(icon_path), _RENDERER_MTIME,
                     dee_accent._RENDERER_MTIME)
        if all(os.path.isfile(p) and os.path.getmtime(p) >= newest for p in paths):
            return paths
        base = dee_accent.render_bitmap(icon_path, accent_hex, line_hex, style)
        try:
            w, h = base.Width, base.Height
            for i, dst in enumerate(paths):
                out = Bitmap(w, h, PixelFormat.Format32bppArgb)
                g = Graphics.FromImage(out)
                try:
                    g.CompositingMode = CompositingMode.SourceOver
                    g.InterpolationMode = InterpolationMode.HighQualityBicubic
                    _draw_frame(g, base, w, h, kind, i / float(FRAMES), accent_hex)
                    dee_accent.save_png(out, dst)
                finally:
                    g.Dispose()
                    out.Dispose()
        finally:
            base.Dispose()
        return paths
    except Exception:
        return []


# ---------------- the live ribbon ----------------
def _deepack_tab_active():
    """True when DeePack is the ribbon tab on screen (so a hidden ribbon
    costs nothing). True as well if the ribbon can't be asked."""
    try:
        clr.AddReference("AdWindows")
        from Autodesk.Windows import ComponentManager
        tab = ComponentManager.Ribbon.ActiveTab
        if tab is None:
            return False
        return _TAB_NAME in (tab.Title or "", tab.Id or "")
    except Exception:
        return True


def _adwindows_item(item):
    """The Autodesk.Windows.RibbonItem behind a Revit API ribbon item - the
    WPF side, the one the mouse can be over. None when it can't be had."""
    try:
        from System.Reflection import BindingFlags
        method = item.GetType().GetMethod(
            "getRibbonItem", BindingFlags.NonPublic | BindingFlags.Instance)
        if method is not None:
            return method.Invoke(item, None)
    except Exception:
        pass
    return None


def _same_item(a, b):
    if a is None or b is None:
        return False
    try:
        ia, ib = (a.Id or ""), (b.Id or "")
        if ia and ib:
            return ia == ib
    except Exception:
        pass
    try:
        from System import Object
        return Object.ReferenceEquals(a, b)
    except Exception:
        return a is b


def hovered_ribbon_item():
    """The Autodesk.Windows.RibbonItem under the mouse pointer, or None
    (pointer elsewhere, nothing hovered, or the ribbon can't be asked)."""
    try:
        clr.AddReference("PresentationCore")
        clr.AddReference("PresentationFramework")
        clr.AddReference("AdWindows")
        from System.Windows import FrameworkElement, FrameworkContentElement
        from System.Windows.Input import Mouse
        from System.Windows.Media import VisualTreeHelper
        from Autodesk.Windows import RibbonItem
        el = Mouse.DirectlyOver
        depth = 0
        while el is not None and depth < 48:
            ctx = None
            try:
                if isinstance(el, (FrameworkElement, FrameworkContentElement)):
                    ctx = el.DataContext
            except Exception:
                ctx = None
            if isinstance(ctx, RibbonItem):
                return ctx
            try:
                ctx = el.RibbonItem        # some Autodesk item controls expose it
                if isinstance(ctx, RibbonItem):
                    return ctx
            except Exception:
                pass
            parent = None
            try:
                parent = VisualTreeHelper.GetParent(el)
            except Exception:
                parent = None
            if parent is None:
                try:
                    parent = el.Parent
                except Exception:
                    parent = None
            el = parent
            depth += 1
    except Exception:
        pass
    return None


class _Entry(object):
    """One ribbon button that can move. The PNG paths are produced on a
    worker thread; the WPF bitmaps behind frames/still are only created
    (on the UI thread) the first time the pointer reaches the button."""
    __slots__ = ("item", "adw", "name", "src", "on_src",
                 "frame_paths", "still_path", "on_frame_paths", "on_still_path",
                 "frames", "still", "on_frames", "on_still")

    def __init__(self, item, adw, name, src, on_src=None):
        self.item = item
        self.adw = adw
        self.name = name
        self.src = src
        self.on_src = on_src    # DeeForce: the icon while auto-sync is on
        self.frame_paths = []
        self.still_path = None
        self.on_frame_paths = []
        self.on_still_path = None
        self.frames = None      # [(small, large)] per motion frame, lazy
        self.still = None       # (small, large) of the plain styled icon, lazy
        self.on_frames = None
        self.on_still = None


def _icons(path):
    from pyrevit.coreutils.ribbon import ButtonIcons
    icons = ButtonIcons(path)
    return (icons.small_bitmap, icons.large_bitmap)


class _Animator(object):
    """Startup cost on Revit's UI thread is one pass over the ribbon items
    (no drawing). The FRAMES PNGs per icon are rendered on a background
    thread, then the hover timer starts; the timer only ticks while the
    mouse is over Revit's ribbon, so an idle Revit pays nothing."""

    def __init__(self):
        self.timer = None
        self.entries = []
        self.current = None     # the _Entry under the mouse, if any
        self.index = 0
        self.generation = 0     # bumped by stop(); stale renders are dropped
        self.ribbon = None      # Autodesk.Windows RibbonControl hooked for enter/leave
        self._hooked = False

    @property
    def items(self):
        return self.entries

    def stop(self):
        self.generation += 1
        try:
            if self.timer is not None:
                self.timer.Stop()
        except Exception:
            pass
        self.timer = None
        self._unhook_ribbon()
        self._rest(self.current)
        self.current = None
        self.entries = []

    # ---- ribbon enter/leave gate ----
    def _hook_ribbon(self):
        try:
            clr.AddReference("AdWindows")
            from Autodesk.Windows import ComponentManager
            ribbon = ComponentManager.Ribbon
            if ribbon is None:
                return False
            ribbon.MouseEnter += self._on_ribbon_enter
            ribbon.MouseLeave += self._on_ribbon_leave
            self.ribbon = ribbon
            self._hooked = True
            return True
        except Exception:
            self.ribbon = None
            self._hooked = False
            return False

    def _unhook_ribbon(self):
        if self.ribbon is not None and self._hooked:
            try:
                self.ribbon.MouseEnter -= self._on_ribbon_enter
            except Exception:
                pass
            try:
                self.ribbon.MouseLeave -= self._on_ribbon_leave
            except Exception:
                pass
        self.ribbon = None
        self._hooked = False

    def _on_ribbon_enter(self, sender, args):
        try:
            if self.timer is not None and not self.timer.IsEnabled:
                self.timer.Start()
        except Exception:
            pass

    def _on_ribbon_leave(self, sender, args):
        try:
            if self.timer is not None:
                self.timer.Stop()
        except Exception:
            pass
        try:
            self._rest(self.current)
            self.current = None
            self.index = 0
        except Exception:
            pass

    def _ribbon_has_mouse(self):
        try:
            return bool(self.ribbon.IsMouseOver)
        except Exception:
            return True

    # ---- surviving a pyRevit Reload ----
    # A Reload builds a fresh engine and a fresh _Animator, but the old
    # DispatcherTimer and ribbon hooks keep running in the old one. Each
    # load leaves a stop callback in the AppDomain so the next can retire it.
    _DOMAIN_KEY = "Dee.IconMotion.Stop"

    def _retire_previous(self):
        try:
            from System import AppDomain
            previous = AppDomain.CurrentDomain.GetData(self._DOMAIN_KEY)
            AppDomain.CurrentDomain.SetData(self._DOMAIN_KEY, None)
            if previous is not None:
                previous.Invoke()
        except Exception:
            pass

    def _publish_stop(self):
        try:
            from System import AppDomain, Action
            AppDomain.CurrentDomain.SetData(self._DOMAIN_KEY, Action(self.stop))
        except Exception:
            pass

    # ---- start: collect on the UI thread, render on a worker ----
    def start(self, uiapp, accent_hex, line_hex, style, kind):
        self._retire_previous()
        self.stop()
        generation = self.generation
        bundles = dee_accent._bundle_dirs()
        pending = []
        for item in dee_accent._iter_items(uiapp):
            try:
                name = item.Name
            except Exception:
                continue
            bundle_dir = bundles.get(name)
            if not bundle_dir:
                continue
            src = dee_accent._theme_icon(bundle_dir)
            if not src:
                continue
            adw = _adwindows_item(item)
            if adw is None:
                continue                      # no WPF twin, so no hover
            on_src = None
            if name == "DeeForce":
                candidate = os.path.join(bundle_dir, "icon.on.png")
                if os.path.isfile(candidate):
                    on_src = candidate
            pending.append(_Entry(item, adw, name, src, on_src))
        if not pending:
            return 0

        from System.Windows.Threading import Dispatcher
        dispatcher = Dispatcher.CurrentDispatcher

        def _render():
            ready = []
            for entry in pending:
                if generation != self.generation:
                    return
                try:
                    entry.frame_paths = frame_paths(
                        entry.src, accent_hex, line_hex, style, kind)
                    if not entry.frame_paths:
                        continue
                    entry.still_path = dee_accent.resolve(
                        entry.src, accent_hex, line_hex, style)
                    if entry.on_src:
                        entry.on_frame_paths = frame_paths(
                            entry.on_src, accent_hex, line_hex, style, kind)
                        entry.on_still_path = dee_accent.resolve(
                            entry.on_src, accent_hex, line_hex, style)
                    ready.append(entry)
                except Exception:
                    continue
            if generation != self.generation or not ready:
                return
            try:
                from System import Action
                dispatcher.BeginInvoke(Action(lambda: self._arm(generation, ready)))
            except Exception:
                pass

        import threading
        worker = threading.Thread(target=_render)
        worker.daemon = True
        worker.start()
        return len(pending)

    def _arm(self, generation, ready):
        """UI thread, after the worker: keep the entries and start the
        hover timer (gated by the ribbon's MouseEnter/MouseLeave)."""
        try:
            if generation != self.generation:
                return
            self.entries = ready
            from System.Windows.Threading import DispatcherTimer
            self.timer = DispatcherTimer()
            self.timer.Interval = TimeSpan.FromMilliseconds(INTERVAL_MS)
            self.timer.Tick += self._tick
            if not self._hook_ribbon() or self._ribbon_has_mouse():
                self.timer.Start()        # no gate available: tick as before
            self._publish_stop()
        except Exception:
            pass

    @staticmethod
    def _force_on():
        try:
            import dee_force_state
            return bool(dee_force_state.is_active())
        except Exception:
            return False

    @staticmethod
    def _paint(entry, pair):
        if pair is None:
            return
        small, large = pair
        try:
            entry.item.Image = small
        except Exception:
            pass
        try:
            entry.item.LargeImage = large
        except Exception:
            pass

    @staticmethod
    def _load(entry):
        """UI thread: the WPF bitmaps for an entry, built once from its
        PNG paths the first time the pointer reaches it."""
        if entry.frames is None:
            try:
                entry.frames = [_icons(p) for p in entry.frame_paths]
            except Exception:
                entry.frames = []
        if entry.still is None and entry.still_path:
            try:
                entry.still = _icons(entry.still_path)
            except Exception:
                entry.still = None
        if entry.on_src and entry.on_frames is None:
            try:
                entry.on_frames = [_icons(p) for p in entry.on_frame_paths]
                if entry.on_still_path:
                    entry.on_still = _icons(entry.on_still_path)
            except Exception:
                entry.on_frames = []

    def _rest(self, entry):
        """Puts the still icon back on a button the pointer has left."""
        if entry is None:
            return
        self._load(entry)
        still = entry.still
        if entry.on_still is not None and self._force_on():
            still = entry.on_still
        self._paint(entry, still)

    def _find(self, adw):
        if adw is None:
            return None
        for entry in self.entries:
            if _same_item(entry.adw, adw):
                return entry
        return None

    def _tick(self, sender, args):
        try:
            if self._hooked and not self._ribbon_has_mouse():
                self._on_ribbon_leave(sender, args)   # missed MouseLeave: go quiet
                return
            hovered = self._find(hovered_ribbon_item()) if _deepack_tab_active() else None
            if hovered is not self.current:
                self._rest(self.current)
                self.current = hovered
                self.index = 0                # every hover starts the loop afresh
            if hovered is None:
                return
            self._load(hovered)
            frames = hovered.frames
            if hovered.on_frames and self._force_on():
                frames = hovered.on_frames
            if not frames:
                return
            self.index = (self.index + 1) % FRAMES
            self._paint(hovered, frames[self.index % len(frames)])
        except Exception:
            pass


_animator = _Animator()


def stop():
    _animator.stop()


def is_running():
    return _animator.timer is not None


def apply(uiapp, accent_hex=None, line_hex=dee_accent._SAVED,
          style=dee_accent._SAVED, kind=None):
    """(Re)starts the live motion for the given settings, or stops it when
    the motion is off. Call AFTER dee_accent.apply_to_ribbon, which puts
    the still icons back. Returns how many buttons will move when the
    pointer is over them."""
    try:
        kind = normalize(kind or load_motion())
        _animator.stop()
        if is_off(kind) or uiapp is None:
            return 0
        accent_hex = dee_accent.normalize_hex(accent_hex) or dee_accent.load_accent()
        line_hex = (dee_accent.load_line() if line_hex is dee_accent._SAVED
                    else dee_accent.normalize_hex(line_hex))
        style = (dee_accent.load_style() if style is dee_accent._SAVED
                 else dee_accent.normalize_style(style))
        return _animator.start(uiapp, accent_hex, line_hex, style, kind)
    except Exception:
        return 0


# ---------------- startup ----------------
def _on_idling(sender, args):
    try:
        sender.Idling -= _on_idling
    except Exception:
        pass
    try:
        apply(sender)
    except Exception:
        pass


def apply_on_startup(uiapp):
    """From startup.py (before the ribbon exists): starts the saved motion
    on Revit's first Idling. Nothing at all when motion is off."""
    try:
        if is_off(load_motion()):
            return
        try:
            uiapp.Idling -= _on_idling
        except Exception:
            pass
        uiapp.Idling += _on_idling
    except Exception:
        pass
