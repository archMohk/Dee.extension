# -*- coding: utf-8 -*-
"""Align Middle - moves every other selected element so its BoundingBox
vertical center matches the first selected element's vertical center.
All real logic lives in lib/dee_align_service.py - this script only
wires the button to it."""
import dee_align_service as core

uidoc = __revit__.ActiveUIDocument
doc = uidoc.Document

result = core.align_elements(doc, uidoc, "Align Middle", mode="middle")
core.show_summary(result)
