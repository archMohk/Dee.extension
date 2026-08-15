# -*- coding: utf-8 -*-
"""Distribute Horizontal - sorts the selection by X center, keeps the
first and last fixed, and spaces the rest equally by center X. All
real logic lives in lib/dee_align_service.py - this script only wires
the button to it."""
import dee_align_service as core

uidoc = __revit__.ActiveUIDocument
doc = uidoc.Document

result = core.distribute_elements(doc, uidoc, "Distribute Horizontal", axis="x")
core.show_summary(result)
