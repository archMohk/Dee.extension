# -*- coding: utf-8 -*-
"""
DeeLazy - a growing collection of bulk productivity tools.
This entry point only opens the card-based launcher (controller.py) -
each tool owns its own window/logic under modules/, see
modules/__init__.py for how to add a new one.
"""
import os

import controller

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")

uiapp = __revit__

window = controller.DeeLazyHomeWindow(_XAML_FILE, uiapp)
window.ShowDialog()
