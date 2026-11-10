# -*- coding: utf-8 -*-
"""Updater - a DeeLazy module launched straight from the ribbon.
The tool itself lives in DeeLazy.pushbutton/modules/; this stub only
routes to it so the category dropdowns in Lazy.panel can list every module."""
import os
import sys

# script.py -> X.pushbutton -> Y.pulldown -> LazyStack.stack -> Lazy.panel
_PANEL = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_HOME = os.path.join(_PANEL, "DeeLazy.pushbutton")   # Lazy.panel/DeeLazy.pushbutton
if _HOME not in sys.path:
    sys.path.insert(0, _HOME)

import dee_lazy_launch
dee_lazy_launch.run_module("updater", __revit__)
