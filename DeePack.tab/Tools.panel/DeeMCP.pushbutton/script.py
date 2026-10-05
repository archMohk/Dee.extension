# -*- coding: utf-8 -*-
"""
DeeMCP
Starts/stops a local MCP (Model Context Protocol) server inside this
running Revit session - see lib/dee_mcp_server.py / lib/dee_mcp_service.py
/ lib/dee_mcp_tools.py for the actual implementation (lifecycle, JSON-RPC
dispatch, and tool registry respectively). This file is only the ribbon
entry point and status window.

--------------------------------------------------------------------
Why `engine: persistent: true` (same as DeePrinter/DeeFamily)
--------------------------------------------------------------------
This window is shown with Show() (modeless), not ShowDialog(), so Revit
stays interactive while the server runs - exactly DeeFamily.pushbutton's
own documented reasoning for the same choice (see that file's module
docstring). script.py returns almost immediately after Show() returns;
without `persistent: true`, pyRevit would tear down the IronPython
engine when script.py returns, and the background HTTP server thread
(started via lib/dee_mcp_server.start()) would have nothing live to
report back to - matching DeeFamily's ExternalEvent reasoning even
though this tool relies on pyRevit's OWN routes/ExternalEvent bridge
rather than a hand-rolled one (see lib/dee_mcp_server.py's docstring).

--------------------------------------------------------------------
Closing this window stops the server - deliberate, not a side effect
--------------------------------------------------------------------
Per the explicit security framing this tool was built with: there must
never be an invisible, still-listening server with no visible trace in
the UI. Closing this window (the X button or the Close button - both
raise the same Closing event) always stops the server first. Clicking
the ribbon button again while the server is NOT running opens a fresh
window exactly as today; clicking it while a server IS somehow already
running (e.g. a previous window's Stop call failed) re-attaches to that
existing state rather than silently starting a second one.
"""
import os

from pyrevit import forms
import dee_branding

import clr
clr.AddReference("WindowsBase")
from System import TimeSpan
from System.Windows.Threading import DispatcherTimer

import dee_mcp_server as mcp_server
import dee_telemetry
dee_telemetry.check_access("DeeMCP")


_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")

_STATUS_POLL_SECONDS = 2


class DeeMCPWindow(dee_branding.DeeBrandedWindow):
    def __init__(self, xaml_file):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self._timer = DispatcherTimer()
        self._timer.Interval = TimeSpan.FromSeconds(_STATUS_POLL_SECONDS)
        self._timer.Tick += self._on_tick
        self.Closing += self._on_closing
        self._refresh()
        if mcp_server.is_running():
            self._timer.Start()

    # ---------------- status display ----------------
    def _refresh(self):
        status = mcp_server.get_status()
        if status["running"]:
            self.status_tb.Text = "Running."
            self.url_tb.Text = status["url"] or ""
            self.token_tb.Text = status["token"] or ""
            self.count_tb.Text = "{0} request(s) served.".format(status["request_count"])
            self.start_b.IsEnabled = False
            self.stop_b.IsEnabled = True
        else:
            self.status_tb.Text = "Stopped."
            self.url_tb.Text = ""
            self.token_tb.Text = ""
            self.count_tb.Text = "0 request(s) served."
            self.start_b.IsEnabled = True
            self.stop_b.IsEnabled = False

    def _on_tick(self, sender, args):
        self._refresh()

    # ---------------- buttons ----------------
    def start_click(self, sender, args):
        ok, detail = mcp_server.start()
        self._refresh()
        if ok:
            self.log_tb.Text = detail
            self._timer.Start()
        else:
            self.log_tb.Text = "Error: " + detail
            forms.alert("Could not start the MCP server:\n{0}".format(detail))

    def stop_click(self, sender, args):
        ok, detail = mcp_server.stop()
        self._timer.Stop()
        self._refresh()
        self.log_tb.Text = detail if ok else "Error: " + detail

    def close_click(self, sender, args):
        self.Close()

    def _on_closing(self, sender, args):
        try:
            self._timer.Stop()
        except Exception:
            pass
        mcp_server.stop()


def main():
    window = DeeMCPWindow(_XAML_FILE)
    window.Show()


main()
