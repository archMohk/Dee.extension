# -*- coding: utf-8 -*-
"""
dee_ids - ElementId helpers shared by every Dee tool.

Revit 2024 renamed ElementId.IntegerValue to ElementId.Value (a 64-bit
long); Revit 2026 removed the old name altogether, so any bare
.IntegerValue access crashes there with
"'ElementId' object has no attribute 'IntegerValue'".
"""


def eid_value(element_id):
    """Integer id of an ElementId on any Revit version. Tries the current
    name (.Value, Revit 2024+) first and falls back to .IntegerValue on
    older releases. Returns None for a None id."""
    if element_id is None:
        return None
    try:
        return element_id.Value
    except AttributeError:
        return element_id.IntegerValue
