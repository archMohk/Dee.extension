# -*- coding: utf-8 -*-
"""Align Top - moves every other selected element so its BoundingBox
Max.Y matches the first selected element's Max.Y. All real logic lives
in lib/dee_align_service.py - this script only wires the button to it."""
import dee_align_service as core
import dee_telemetry
dee_telemetry.check_access("AlignTop")


uidoc = __revit__.ActiveUIDocument
doc = uidoc.Document

result = core.align_elements(doc, uidoc, "Align Top", mode="top")
core.show_summary(result)
