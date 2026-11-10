# -*- coding: utf-8 -*-
"""Text to Lines - a DeeLazy module launched straight from the ribbon.
The tool itself lives in DeeLazy.pushbutton/modules/; this stub only
routes to it so the DeeLazy dropdown can list every module."""
import os
import sys

_HOME = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "DeeLazy.pushbutton")
if _HOME not in sys.path:
    sys.path.insert(0, _HOME)

import dee_lazy_launch
dee_lazy_launch.run_module("dee_text_to_lines", __revit__)
