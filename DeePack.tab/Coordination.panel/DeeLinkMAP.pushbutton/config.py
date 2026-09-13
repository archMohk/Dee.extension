# -*- coding: utf-8 -*-
"""
DeeLinkMAP config (SHIFT+Click) - edits the discipline code mapping
DeeLinkMAP uses to color-code and label each file's box on the map
(e.g. "AR" -> "Architecture"). pyRevit's own convention: a bundle's
config.py runs instead of script.py when the button is SHIFT+Clicked.

Deliberately opens the raw JSON file in the user's default text/JSON
editor rather than building a custom add/edit/remove table UI - the
mapping is meant to be generic and freely extensible (a new trade is
just one more "CODE": "Label" line), and a plain JSON file already
does that with no UI to maintain.
"""
import os

from pyrevit import forms
import dee_linkmap_service as svc
import dee_telemetry
dee_telemetry.check_access("DeeLinkMAP")


def main():
    # load_disciplines() writes the default file on first use, so this
    # is guaranteed to exist and be populated by the time it opens.
    svc.load_disciplines()

    forms.alert(
        "Opening the discipline code mapping for editing.\n\n"
        "Each line maps a code DeeLinkMAP looks for as its OWN name "
        "segment (between - or _) to a discipline label, e.g.:\n\n"
        '  "AR": "Architecture"\n\n'
        "Add, edit, or remove lines for any trade - a file whose name "
        "does not match any code shows as \"Unknown\" on the map. "
        "Save the file and close your editor when done; changes apply "
        "the next time you run DeeLinkMAP.",
        title="Dee.extension - DeeLinkMAP Discipline Codes")

    path = svc.discipline_config_path()
    try:
        os.startfile(path)
    except Exception as e:
        forms.alert(
            "Could not open the file automatically:\n{0}\n\n"
            "Edit it directly at:\n{1}".format(e, path),
            title="Dee.extension - DeeLinkMAP Discipline Codes")


main()
