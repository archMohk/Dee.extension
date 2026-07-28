# -*- coding: utf-8 -*-
"""Align Left - moves every other selected element so its BoundingBox
Min.X matches the first selected element's Min.X. All real logic lives
in lib/dee_align_service.py - this script only wires the button to it."""
import dee_align_service as core

uidoc = __revit__.ActiveUIDocument
doc = uidoc.Document

result = core.align_elements(doc, uidoc, "Align Left", mode="left")
core.show_summary(result)
