# -*- coding: utf-8 -*-
"""Align Center - moves every other selected element so its BoundingBox
horizontal center matches the first selected element's center. All real
logic lives in lib/dee_align_service.py - this script only wires the
button to it."""
import dee_align_service as core

uidoc = __revit__.ActiveUIDocument
doc = uidoc.Document

result = core.align_elements(doc, uidoc, "Align Center", mode="center_x")
core.show_summary(result)
