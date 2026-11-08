# -*- coding: utf-8 -*-
"""
deew_background_ui
The "Run by: this session / Background Revit x N" controls and the
host-side glue for the DeeW.Cloud windows, on top of lib/dee_background.

Every DeeW tool window has the same button row (run_b, Export Report,
Save/Load Settings, Cancel), a _log(message) method, a report_grid and
a summary_tb. add_run_controls(window) drops a "Run by" picker, a
"Revits" picker and a hidden Stop button into that row - built in code
like dee_known_dialogs.add_error_checkbox so every tool gets the same
control and wording.

Then, inside run_click, once the tool has validated its inputs:

    if deew_background_ui.is_background(self):
        deew_background_ui.launch(self, tool="DeeWTransmit",
            worker_module="deew_transmit_pipeline", worker_func="worker",
            items=items, options=options, extra_paths=[_THIS_DIR],
            row_class=reportgen.CleanReportRow, status_field="save_status",
            success_values=("Copy saved",), on_done=self._background_done)
        return

launch() starts the Revits, locks the run button, tails the logs into
window._log and, when every Revit has finished, rebuilds the report rows
from the result dicts (deew_background_worker.row_to_result on the
other side) in the order the items were given, fills report_grid and
calls on_done(rows, results) so the tool can write its own summary.
"""
import dee_background

RUN_LABELS = ["This Revit session", "Background Revit (this session stays free)"]
_TIP_RUN = ("This Revit session: the models are opened here and this window waits.\n"
            "Background Revit: one or more second Revits are started by the pyRevit CLI and do the job "
            "while you keep working here; they close themselves when done. Models open in this session "
            "cannot be handed to another Revit and stay here.")
_TIP_N = ("How many background Revits run at the same time. Each needs its own memory - 2 or 3 is the "
          "usual sweet spot on a workstation.")


def add_run_controls(window, anchor="run_b"):
    """Adds the controls after `anchor`'s row siblings. Never raises."""
    try:
        from System.Windows import Thickness, VerticalAlignment, Visibility
        from System.Windows.Controls import ComboBox, ComboBoxItem, TextBlock, Button, Panel
        button = window.FindName(anchor)
        parent = button.Parent if button is not None else None
        if not isinstance(parent, Panel):
            return False

        def _label(text, tip=None):
            tb = TextBlock()
            tb.Text = text
            tb.VerticalAlignment = VerticalAlignment.Center
            tb.Margin = Thickness(14, 0, 6, 0)
            if tip:
                tb.ToolTip = tip
            return tb

        run_cb = ComboBox()
        run_cb.Width = 250
        run_cb.Height = 26
        run_cb.ToolTip = _TIP_RUN
        run_cb.VerticalAlignment = VerticalAlignment.Center
        for text in RUN_LABELS:
            it = ComboBoxItem()
            it.Content = text
            run_cb.Items.Add(it)
        run_cb.SelectedIndex = 0

        n_cb = ComboBox()
        n_cb.Width = 50
        n_cb.Height = 26
        n_cb.ToolTip = _TIP_N
        n_cb.VerticalAlignment = VerticalAlignment.Center
        for text in dee_background.INSTANCE_CHOICES:
            it = ComboBoxItem()
            it.Content = text
            n_cb.Items.Add(it)
        n_cb.SelectedIndex = 1
        n_cb.IsEnabled = False

        stop_b = Button()
        stop_b.Content = "Stop Background"
        stop_b.Width = 130
        stop_b.Height = 26
        stop_b.Margin = Thickness(10, 0, 0, 0)
        stop_b.Visibility = Visibility.Collapsed
        stop_b.ToolTip = "Each background Revit finishes the model it is on, then skips the rest."

        def _run_changed(sender, args):
            n_cb.IsEnabled = run_cb.SelectedIndex == 1

        def _stop(sender, args):
            job = getattr(window, "dee_bg_job", None)
            if job is not None:
                job.stop()
                _log(window, "Stop requested - each background Revit finishes its current model, then skips the rest.")

        run_cb.SelectionChanged += _run_changed
        stop_b.Click += _stop

        children = parent.Children
        children.Add(_label("Run by:"))
        children.Add(run_cb)
        children.Add(_label("Revits:", _TIP_N))
        children.Add(n_cb)
        children.Add(stop_b)
        window.dee_bg_run_cb = run_cb
        window.dee_bg_n_cb = n_cb
        window.dee_bg_stop_b = stop_b
        window.dee_bg_job = None
        return True
    except Exception:
        return False


def is_background(window):
    try:
        return window.dee_bg_run_cb.SelectedIndex == 1
    except Exception:
        return False


def instances(window):
    try:
        return dee_background.instances_from_combo(window.dee_bg_n_cb, 1)
    except Exception:
        return 1


def _log(window, message):
    try:
        window._log(message)
    except Exception:
        pass


def _lock(window, running):
    try:
        from System.Windows import Visibility
        window.FindName("run_b").IsEnabled = not running
        window.dee_bg_stop_b.Visibility = Visibility.Visible if running else Visibility.Collapsed
    except Exception:
        pass


def local_item(scanned_model):
    """Serializes a deew_model_scanner.ScannedModel for a worker."""
    return {"key": scanned_model.file_path, "kind": "local", "name": scanned_model.file_name,
            "path": scanned_model.file_path}


def cloud_item(item):
    """Serializes a tool's Cloud*Item (hub/region/project/item ids)."""
    return {"key": item.item_id, "kind": "cloud", "name": item.display_name,
            "hub_id": item.hub_id, "hub_name": item.hub_name, "region": item.region,
            "project_id": item.project_id, "project_name": item.project_name,
            "item_id": item.item_id, "display_name": item.display_name}


def launch(window, tool, worker_module, worker_func, items, options, extra_paths,
           row_class, status_field, success_values, on_done, title=None):
    """Starts the background Revits for `items`. Returns True when started."""
    from pyrevit import forms
    if getattr(window, "dee_bg_job", None) is not None:
        forms.alert("A background job is still running - wait for it to finish (or stop it) first.")
        return False
    if not items:
        forms.alert("Nothing can be sent to a background Revit (models open in this session stay here).")
        return False
    options = dict(options or {})
    try:
        import dee_known_dialogs
        options["delete_errors"] = bool(dee_known_dialogs.delete_errors_enabled())
    except Exception:
        pass
    try:
        app = window.uiapp.Application
        job = dee_background.BackgroundJob.start(
            tool, worker_module, worker_func, items, options, str(app.VersionNumber),
            instances=instances(window), extra_paths=extra_paths, log=lambda m: _log(window, m))
    except Exception as e:
        forms.alert("{0}\n\nSwitch 'Run by' to this Revit session instead.".format(e), title=title or tool)
        return False
    window.dee_bg_job = job
    _lock(window, True)
    _log(window, "{0} background Revit(s) starting (about a minute each); {1} model(s) in total. "
                 "Logs under {2}".format(len(job.workers), len(items), job.root))
    order = [it.get("key") for it in items]
    names = dict((it.get("key"), it.get("name")) for it in items)

    def on_line(tag, line):
        _log(window, u"[{0}] {1}".format(tag, line))

    def on_finish(job, how):
        window.dee_bg_job = None
        _lock(window, False)
        results = job.results()
        rows = []
        for key in order:
            res = results.get(key)
            row = result_to_row(row_class, res, names.get(key), status_field)
            rows.append(row)
        if how == "failed":
            _log(window, "A background Revit crashed - see the log above and the logs under {0}".format(job.root))
        elif how == "exited":
            _log(window, "A background Revit ended without reporting - see the logs under {0}".format(job.root))
        try:
            window.report_grid.ItemsSource = None
            window.report_grid.ItemsSource = rows
        except Exception:
            pass
        try:
            on_done(rows, results, how)
        except Exception as e:
            _log(window, "Could not finish the report: {0}".format(e))

    window.dee_bg_timer = dee_background.start_timer(job, on_line, on_finish)
    return True


def result_to_row(row_class, result, name, status_field):
    """Rebuilds a report row from a worker's result dict (see
    deew_background_worker.row_to_result). A missing result means the
    Revit never got to that item."""
    row = row_class.__new__(row_class)
    if result and result.get("row"):
        row.__dict__.update(result["row"])
        return row
    # minimal row: the class constructors differ, so fill the common fields
    try:
        import datetime
        import os
        row.file_name = name or ""
        row.warnings = ""
        row.errors = (result or {}).get("detail", "") or ((result or {}).get("trace") and "worker error") or ""
        row.processing_time_seconds = 0.0
        row.date_text = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        row.revit_version = ""
        row.user = os.environ.get("USERNAME", "Unknown")
        for f in ("location", "original_location", "source", "model_type", "original_model_type",
                  "cloud_model_name", "cloud_guid", "acc_hub", "acc_project", "acc_folder", "worksharing_enabled"):
            if not hasattr(row, f):
                setattr(row, f, "")
        for f in ("purged_count", "zero_area_rooms_deleted", "unused_groups_deleted", "inplace_families_found",
                  "sheets_deleted", "views_deleted", "unused_views_deleted"):
            if not hasattr(row, f):
                setattr(row, f, 0)
        setattr(row, status_field, "Failed - not processed" if result is None
                else "Failed - {0}".format(result.get("detail") or "worker error"))
    except Exception:
        pass
    return row


def count(rows, status_field, success_values):
    ok = fail = skip = 0
    for r in rows:
        st = (getattr(r, status_field, "") or "")
        if st in success_values:
            ok += 1
        elif "skip" in st.lower():
            skip += 1
        elif "fail" in st.lower():
            fail += 1
    return ok, fail, skip
