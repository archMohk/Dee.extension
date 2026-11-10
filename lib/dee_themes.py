# -*- coding: utf-8 -*-
"""
dee_themes
The themes picked in About -> Interface -> Theme & shape. One entry per
theme; everything else (lib/dee_branding.py, lib/dee_dark.py,
lib/dee_theme_motion.py, lib/dee_interface_dialog.py) reads this table.

A theme is a base (light or dark), a palette (the colours DeeTheme.xaml
is loaded with) and, for the moving ones, a motion set (lib/dee_theme_motion.py):

  light     The light windows every tool always had.
  dark      Dark windows (lib/dee_dark.py turns each tool's own written-in
            colours dark too).
  paper     Light, warm and sepia-tinted, like a drawing sheet.
  midnight  Deep blue-black windows with a cool blue accent.
  dynamic   Light windows that move: a slow colour drift across the
            background (tinted from the accent), the accent breathes, the
            website bar shimmers, buttons lift as you hover and the content
            glides in when a window opens.
  aurora    Dark windows under slow northern lights - a drifting teal and
            violet glow - with the same motions as Dynamic.

Dark-based themes get every dark treatment (DeeThemeDark.xaml, the XAML
colour transform, the live colour scanner) with their own palette swapped
in; light-based ones only swap the palette. A palette lists DeeTheme.xaml
keys; anything it leaves out keeps the base's value.
"""

# (key, label, description) in the order the Theme & shape tab shows them.
THEMES = [
    ("light", "Light", "Light windows (as before)."),
    ("dark", "Dark", "Dark windows, easier on the eyes next to Revit's dark theme."),
    ("paper", "Paper", "Warm, sepia-tinted light windows, like a drawing sheet."),
    ("midnight", "Midnight", "Deep blue-black windows with a cool blue accent."),
    ("dynamic", "Dynamic",
     "Light windows that move: the background drifts through tints of your accent, "
     "the accent breathes, the website bar shimmers, buttons lift under the mouse "
     "and content glides in."),
    ("aurora", "Aurora",
     "Dark windows under slow northern lights - a drifting teal and violet glow - "
     "with the same motions as Dynamic."),
]
KEYS = [t[0] for t in THEMES]
DEFAULT = "light"

# DeeTheme.xaml's own (light) values, so a card or a tool can ask for the
# full palette of any theme.
LIGHT = {
    "DeeAccentColor": "#1F6FB2",
    "DeeAccentDarkColor": "#185A90",
    "DeeWindowBg": "#FAFAFA",
    "DeeSurface": "#FFFFFF",
    "DeeBorder": "#D0D0D0",
    "DeeBorderStrong": "#A0A0A0",
    "DeeText": "#222222",
    "DeeTextMuted": "#777777",
    "DeeButtonBg": "#F2F2F2",
    "DeeButtonHover": "#E4E4E4",
    "DeeButtonPressed": "#D6D6D6",
    "DeeStatusOk": "#C6E0B4",
    "DeeStatusWarn": "#FFE699",
    "DeeStatusFail": "#F4C7C3",
    "DeeStatusIgnored": "#E0E0E0",
}

_BASE = {
    "light": "light", "dark": "dark", "paper": "light",
    "midnight": "dark", "dynamic": "light", "aurora": "dark",
}

# Palette overrides on top of the base (light: LIGHT, dark: dee_dark.PALETTE).
_PALETTES = {
    "paper": {
        "DeeAccentColor": "#9A6B3A",
        "DeeAccentDarkColor": "#7E5530",
        "DeeWindowBg": "#F6F0E4",
        "DeeSurface": "#FCF8F0",
        "DeeBorder": "#D9CDB8",
        "DeeBorderStrong": "#B09C7E",
        "DeeText": "#3A3028",
        "DeeTextMuted": "#8A7A66",
        "DeeButtonBg": "#EFE6D6",
        "DeeButtonHover": "#E4D8C2",
        "DeeButtonPressed": "#D8C9AE",
        "DeeStatusOk": "#D6E3BE",
        "DeeStatusWarn": "#F5E2A0",
        "DeeStatusFail": "#F0CBC0",
        "DeeStatusIgnored": "#E6DFD2",
    },
    "midnight": {
        "DeeAccentColor": "#4C8DE0",
        "DeeAccentDarkColor": "#3A74C2",
        "DeeWindowBg": "#151A26",
        "DeeSurface": "#1E2536",
        "DeeBorder": "#2C3650",
        "DeeBorderStrong": "#4A5878",
        "DeeText": "#E3E8F2",
        "DeeTextMuted": "#93A0BA",
        "DeeButtonBg": "#232C40",
        "DeeButtonHover": "#2C3750",
        "DeeButtonPressed": "#364260",
        "DeeStatusOk": "#233F33",
        "DeeStatusWarn": "#4E4420",
        "DeeStatusFail": "#52282E",
        "DeeStatusIgnored": "#2A3042",
    },
    "aurora": {
        "DeeAccentColor": "#3FB8A8",
        "DeeAccentDarkColor": "#2E9686",
        "DeeWindowBg": "#0F1A20",
        "DeeSurface": "#16242C",
        "DeeBorder": "#243640",
        "DeeBorderStrong": "#3C5664",
        "DeeText": "#E2EEF0",
        "DeeTextMuted": "#8FAAB2",
        "DeeButtonBg": "#1C2E38",
        "DeeButtonHover": "#243A46",
        "DeeButtonPressed": "#2C4654",
        "DeeStatusOk": "#1F4A3A",
        "DeeStatusWarn": "#4E4A1E",
        "DeeStatusFail": "#522A36",
        "DeeStatusIgnored": "#26343C",
    },
}

# The moving themes and how they move (read by lib/dee_theme_motion.py).
#   glow     colours the background drifts through; None = tints of the
#            user's accent on the theme's window colour
#   blend    how far the window colour leans towards each glow colour
#   period   seconds for one drift across the background
MOTION = {
    "dynamic": {"glow": None, "blend": 0.14, "period": 9.0},
    "aurora": {"glow": ["#1F8F86", "#5B3FA8", "#1E4F8A"], "blend": 0.42, "period": 14.0},
}


def normalize(key):
    return key if key in KEYS else DEFAULT


def label(key):
    return dict((k, l) for k, l, _d in THEMES).get(normalize(key), "Light")


def description(key):
    return dict((k, d) for k, _l, d in THEMES).get(normalize(key), "")


def base(key):
    return _BASE.get(normalize(key), "light")


def is_dark(key):
    return base(key) == "dark"


def has_motion(key):
    return normalize(key) in MOTION


def motion(key):
    return MOTION.get(normalize(key))


def overrides(key):
    """The theme's own colours only (DeeTheme.xaml key -> hex)."""
    return dict(_PALETTES.get(normalize(key), {}))


def palette(key):
    """The palette DeeTheme.xaml is loaded with for this theme, or None
    when the file is right as it is (the light theme)."""
    key = normalize(key)
    if is_dark(key):
        import dee_dark
        pal = dict(dee_dark.PALETTE)
        pal.update(_PALETTES.get(key, {}))
        return pal
    return _PALETTES.get(key) and dict(_PALETTES[key]) or None


def colors(key):
    """Every DeeTheme.xaml colour of a theme (for drawing a preview)."""
    pal = dict(LIGHT)
    pal.update(palette(key) or {})
    return pal
