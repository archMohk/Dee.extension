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
from modules import dee_sselect
from modules import dee_viewsheet
from modules import dee_view_select
from modules import dee_draft_coper
from modules import dee_fupdate
from modules import dee_line_tag
from modules import dee_aselect

REGISTERED_MODULES = [
    view_cropping.TOOL_INFO,
    fill_conversion.TOOL_INFO,
    dee_sselect.TOOL_INFO,
    dee_viewsheet.TOOL_INFO,
    dee_view_select.TOOL_INFO,
    dee_draft_coper.TOOL_INFO,
    dee_fupdate.TOOL_INFO,
    dee_line_tag.TOOL_INFO,
    dee_aselect.TOOL_INFO,
]
