# -*- coding: utf-8 -*-
"""
dee_native_dialogs
Answers plain Windows dialogs that Revit's DialogBoxShowing event never
reports, so a batch is not left waiting for somebody to press OK.

Why
    The Navisworks exporter (nwexportrevit.dll) shows its own native box,
    titled "Navisworks NWC Exporter", e.g. "No suitable geometry found.
    Please check what part you want to export in the "Navisworks settings"
    menu." with a single OK button. It is not a Revit TaskDialog, so
    lib/dee_known_dialogs never sees it: in the user's Revit the export
    stops until OK is pressed, in a background Revit (lib/dee_background,
    no one watching) it stops for ever - and Revit then adds a "Journal
    Error" box on top.

How
    In a background Revit started by `pyrevit run` there is a second effect:
    while such a box is open, Revit plays the journal's next line (Quit)
    behind it, fails, and shows "Journal Error - The journal file could not
    be run to completion" with a single "Enter interactive mode" button,
    which also waits for ever. JOURNAL_RULES answers that one too (use it in
    background workers only - it never appears in the user's own Revit),
    and the worker then has to close Revit itself because the journal's
    Quit line is gone (dee_background.exit_when_done()).

    A Watcher thread looks, a few times a second, for visible top-level
    dialogs of THIS process whose title matches a rule, reads their text,
    presses the wanted button (BM_CLICK) and remembers what they said, so
    the tool can put the sentence in its report:

        watcher = Watcher(NWC_RULES)
        watcher.start()
        try:
            doc.Export(...)
        finally:
            watcher.stop()
        said = watcher.messages()      # ["No suitable geometry found. ..."]

    Only dialogs of this process are touched, and only those named in the
    rules. Never raises: with no ctypes (or no Windows) it simply does
    nothing.
"""
import os
import threading

try:
    import ctypes
    from ctypes import c_void_p, c_int, c_uint, c_ulong, create_unicode_buffer, byref
    _user32 = ctypes.windll.user32
    _ENUM = ctypes.WINFUNCTYPE(c_int, c_void_p, c_void_p)
except Exception:           # not Windows / no ctypes
    ctypes = None
    _user32 = None

BM_CLICK = 0x00F5

# (lower-case part of the dialog title, button texts in order of preference)
NWC_RULES = [
    ("navisworks nwc exporter", ("ok", "close", "yes")),
]
JOURNAL_RULES = [
    ("journal error", ("enter interactive mode", "ok")),
]


def _text(hwnd, size=2048):
    buf = create_unicode_buffer(size)
    _user32.GetWindowTextW(hwnd, buf, size)
    return buf.value


def _class(hwnd):
    buf = create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _children(hwnd):
    found = []

    def visit(child, _l):
        found.append((child, _class(child), _text(child)))
        return 1
    _user32.EnumChildWindows(hwnd, _ENUM(visit), None)
    return found


def _my_dialogs(rules):
    """[(hwnd, title, rule)] for visible dialogs of this process."""
    pid = os.getpid()
    found = []

    def visit(hwnd, _l):
        try:
            owner = c_uint(0)
            _user32.GetWindowThreadProcessId(hwnd, byref(owner))
            if owner.value == pid and _user32.IsWindowVisible(hwnd):
                title = _text(hwnd, 512)
                low = title.lower()
                for rule in rules:
                    if rule[0] in low and _class(hwnd) == "#32770":
                        found.append((hwnd, title, rule))
                        break
        except Exception:
            pass
        return 1
    _user32.EnumWindows(_ENUM(visit), None)
    return found


def _answer(hwnd, preferred):
    """Presses the preferred button; returns the dialog's message text."""
    kids = _children(hwnd)
    message = u" ".join(t.strip() for _h, c, t in kids if c == "Static" and t.strip())
    buttons = [(h, t.replace("&", "").strip().lower()) for h, c, t in kids if c == "Button" and t.strip()]
    chosen = None
    for want in preferred:
        for h, t in buttons:
            if t == want:
                chosen = h
                break
        if chosen:
            break
    if chosen is None and buttons:
        chosen = buttons[0][0]
    if chosen is not None:
        _user32.PostMessageW(chosen, BM_CLICK, 0, 0)
    return message, chosen is not None


class Watcher(object):
    def __init__(self, rules=None, interval=0.3, on_caught=None):
        self.rules = list(rules if rules is not None else NWC_RULES)
        self.interval = interval
        self.on_caught = on_caught      # called with (title, message) per answered box
        self.caught = []            # [(title, message)]
        self._seen = set()
        self._stop = threading.Event()
        self._thread = None

    def _run(self):
        while not self._stop.is_set():
            self.poll()
            self._stop.wait(self.interval)

    def poll(self):
        """One look; also usable without the thread."""
        if _user32 is None:
            return
        try:
            now = set()
            for hwnd, title, rule in _my_dialogs(self.rules):
                now.add(hwnd)
                if hwnd in self._seen:
                    continue
                self._seen.add(hwnd)
                message, pressed = _answer(hwnd, rule[1])
                if pressed:
                    self.caught.append((title, message))
                    if self.on_caught is not None:
                        try:
                            self.on_caught(title, message)
                        except Exception:
                            pass
            self._seen &= now              # a closed dialog's handle may be reused
        except Exception:
            pass

    def start(self):
        if _user32 is None or self._thread is not None:
            return self
        self._thread = threading.Thread(target=self._run)
        self._thread.daemon = True
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(2.0)
        # the dialog may have appeared in the last moments
        self.poll()

    def messages(self):
        return [m for _t, m in self.caught if m] or [t for t, _m in self.caught]

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc, tb):
        self.stop()
        return False
