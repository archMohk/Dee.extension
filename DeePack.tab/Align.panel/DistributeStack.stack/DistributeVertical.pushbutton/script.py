# -*- coding: utf-8 -*-
"""Distribute Vertical - sorts the selection by Y center, keeps the
top and bottom fixed, and spaces the rest equally by center Y. All
real logic lives in lib/dee_align_service.py - this script only wires
the button to it."""
import dee_align_service as core
import dee_telemetry
dee_telemetry.check_access("DistributeVertical")


uidoc = __revit__.ActiveUIDocument
doc = uidoc.Document

result = core.distribute_elements(doc, uidoc, "Distribute Vertical", axis="y")
core.show_summary(result)
