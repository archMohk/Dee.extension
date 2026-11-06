# -*- coding: utf-8 -*-
"""
Interface (About)
Opens the Interface window: icon / window colours and window effects.
All the work lives in lib/dee_interface_dialog.py, lib/dee_accent.py and
lib/dee_effects.py. Works with no Revit file open.
"""
import dee_telemetry
dee_telemetry.check_access("DeeInterface")

import dee_interface_dialog  # noqa: E402

dee_interface_dialog.show(__revit__)  # noqa: F821 - pyRevit global
