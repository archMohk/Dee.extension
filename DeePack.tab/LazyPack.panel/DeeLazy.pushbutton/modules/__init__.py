# -*- coding: utf-8 -*-
"""
DeeLazy.modules
Registry of available DeeLazy tool modules. This is the ONLY file the
home window (controller.py) reads to know what tools exist - adding a
future module (Sheets, Dimensions, Levels, Grids, ...) means:

  1. Create modules/your_module.py exposing a TOOL_INFO dict:
     TOOL_INFO = {
         "id": "your_module",           # unique, stable, used as a key
         "title": "Your Module",        # shown on the card
         "description": "One sentence for the card.",
         "launch": launch_function,     # def launch(uiapp): opens the
                                         # module's own window - owns
                                         # its own XAML/logic entirely
     }
  2. Add one line to REGISTERED_MODULES below.

No other file (script.py, controller.py, ui.xaml, utils.py, or any
OTHER module) ever needs to change to add a new tool - this is the
single seam the whole framework is built around, per the requirement
that future modules must not require modifying existing code.
"""
from modules import view_cropping
from modules import fill_conversion

REGISTERED_MODULES = [
    view_cropping.TOOL_INFO,
    fill_conversion.TOOL_INFO,
]
