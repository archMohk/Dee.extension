# -*- coding: utf-8 -*-
"""
DeeSchedule
Tracks every scheduled export task of every tool: run, resume, pause,
stop, delete, edit, and move the whole list to another PC.

Tasks are created from inside each export tool (its Schedule button). The
window is lib/dee_schedule_ui.py's ManagerWindow; storage and timing are
lib/dee_schedule_core.py; the runs are lib/dee_schedule_engine.py.
"""
import os

import dee_telemetry
dee_telemetry.check_access("DeeSchedule")

import dee_schedule_ui

_XAML_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.xaml")

dee_schedule_ui.ManagerWindow(_XAML_FILE, __revit__).ShowDialog()
