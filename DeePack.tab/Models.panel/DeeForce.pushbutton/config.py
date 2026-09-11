# -*- coding: utf-8 -*-
"""
DeeForce config (SHIFT+Click) - sets the interval between auto-syncs.
pyRevit's own convention: a bundle's config.py runs instead of
script.py when the button is SHIFT+Clicked (confirmed via pyRevit's
own extensions/__init__.py - DEFAULT_CONFIG_NAME = 'config',
SHIFT_CLICK_PARAM = '__shiftclick__').

Deliberately a single ask_for_string loop rather than a full WPF
window - one number, with validation and a chance to re-enter on a
bad value, is all this needs.
"""
from pyrevit import forms
import dee_force_state as state
import dee_telemetry
dee_telemetry.check_access("DeeForce")


def main():
    current = state.load_interval_minutes()
    while True:
        entered = forms.ask_for_string(
            default=str(current),
            prompt="Minutes between each auto-sync ({0}-{1}):".format(
                state.MIN_INTERVAL_MINUTES, state.MAX_INTERVAL_MINUTES),
            title="Dee.extension - DeeForce Configuration"
        )
        if entered is None:
            return
        try:
            minutes = float(entered)
        except Exception:
            forms.alert("Enter a number.", title="Dee.extension - DeeForce Configuration")
            continue
        if not (state.MIN_INTERVAL_MINUTES <= minutes <= state.MAX_INTERVAL_MINUTES):
            forms.alert(
                "Enter a value between {0} and {1}.".format(
                    state.MIN_INTERVAL_MINUTES, state.MAX_INTERVAL_MINUTES),
                title="Dee.extension - DeeForce Configuration")
            continue
        state.save_interval_minutes(minutes)
        note = ""
        if state.is_active():
            note = ("\n\nDeeForce is currently ON - the new interval takes "
                     "effect starting from the next sync.")
        forms.alert(
            "Saved - auto-sync will run every {0} minute(s).{1}".format(minutes, note),
            title="Dee.extension - DeeForce Configuration")
        return


main()
