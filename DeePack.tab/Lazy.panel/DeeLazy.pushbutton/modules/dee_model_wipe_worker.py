# -*- coding: utf-8 -*-
"""
DeeLazy - Model Wipe: background worker function.

Runs inside a second Revit through lib/dee_background (see that module
for the mechanics: job folders, stub, log, results, stop file). One call
per model:

    process_model(ctx, item) -> {"ok", "detail", "saved", "save_detail", "items", "target"}

item / ctx.options are documented in dee_model_wipe.process_item.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def process_model(ctx, item):
    import dee_model_wipe
    return dee_model_wipe.process_item(ctx, item)
