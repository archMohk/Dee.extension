# -*- coding: utf-8 -*-
"""
DeeForce (Models)
A toggle, not a one-shot action: click turns background auto-sync ON
or OFF. While ON, every open workshared model gets synchronized with
central on the configured interval (SHIFT+Click this button to set
it - see config.py), with a visible countdown starting 30 seconds
before each sync. All the actual timer/state logic lives in
lib/dee_force_state.py - see that module's own docstring for why this
needed a fundamentally different mechanism (UIApplication.Idling) from
every other Dee tool.
"""
from pyrevit import forms
import dee_force_state as state
import dee_telemetry
dee_telemetry.check_access("DeeForce")


def main():
    uiapp = __revit__

    if state.is_active():
        state.disable(uiapp)
        forms.alert(
            "DeeForce is now OFF.\n\nBackground auto-sync has stopped.",
            title="Dee.extension - DeeForce")
        return

    state.enable(uiapp)
    minutes = state.load_interval_minutes()
    forms.alert(
        "DeeForce is now ON.\n\n"
        "Every open workshared model will be synchronized with central "
        "every {0} minute(s). A countdown appears {1} seconds before "
        "each sync.\n\n"
        "Click DeeForce again to turn it off. SHIFT+Click to change the "
        "interval.".format(minutes, state.WARNING_SECONDS),
        title="Dee.extension - DeeForce")


main()
