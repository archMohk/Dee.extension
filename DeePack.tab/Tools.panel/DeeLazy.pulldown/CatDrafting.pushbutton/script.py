# -*- coding: utf-8 -*-
"""Opens the DeeLazy launcher showing only the Drafting & Annotation tools."""
import os
import sys

_HOME = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "DeeLazy.pushbutton")
if _HOME not in sys.path:
    sys.path.insert(0, _HOME)

import dee_lazy_launch
dee_lazy_launch.run_category(u"Drafting & Annotation", __revit__)
