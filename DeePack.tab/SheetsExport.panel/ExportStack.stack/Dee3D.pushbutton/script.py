# -*- coding: utf-8 -*-
"""
Dee3D
Export one Revit 3D view to a SINGLE self-contained .html file that
opens the BIM model in 3D in any browser - Windows, Android or iPhone -
with no internet, no viewer app and no login.

Save it wherever you like (PC, network drive, or a OneDrive folder so it
picks up a share link), then send that one file to whoever needs it.

All of the geometry work lives in lib/dee_3d_export_service.py, and the
viewer itself is lib/dee3d_viewer.html - see those two files for the
design decisions and for what still needs verifying against live Revit.

This window makes no model changes of any kind: it only reads geometry
and writes a file outside the project. There is no Transaction anywhere
in this tool.
"""
import os
import traceback

import clr
clr.AddReference("System.Windows.Forms")
from System.Windows.Forms import SaveFileDialog, DialogResult, MessageBox
from System.Diagnostics import Process
from System import DateTime

from pyrevit import forms, script

import dee_branding
import deew_settings
import dee_3d_export_service as core

output = script.get_output()

_TOOL = "Dee3D"
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_FILE = os.path.join(_THIS_DIR, "ui.xaml")
_SETTINGS = "dee3d"

_DEFAULTS = {
    "quality": 1,           # index into core.QUALITY_PRESETS
    "color_mode": 0,
    "include_links": False,
    "include_params": False,
    "open_after": True,
    "max_triangles": core.DEFAULT_MAX_TRIANGLES,
    "last_folder": "",
}


# ==========================================================================
# grid row objects (plain, so WPF binds to them by name)
# ==========================================================================
class ViewRow(object):
    def __init__(self, view):
        self.view = view
        self.label = core.view_label(view)
        try:
            self.detail = str(view.DetailLevel)
        except Exception:
            self.detail = ""


class CategoryRow(object):
    def __init__(self, name, count):
        self.name = name
        self.count = count
        self.included = True


class _SafeProgress(object):
    """forms.ProgressBar tries to set Window.TaskbarItemInfo on its host
    window - a genuine WPF-level bug (not this extension's code):
    Window.TaskbarItemInfo throws NotImplementedException whenever the
    underlying ITaskbarList::HrInit COM call fails, which is documented
    to happen specifically under Remote Desktop/Terminal Services or a
    custom shell without a taskbar (live-confirmed in DeeSheetLinks).

    Wraps the real forms.ProgressBar and falls back to running with NO
    progress UI at all if entering it fails, so the tool degrades
    gracefully under RDP instead of crashing - everyone else still gets
    the real progress bar exactly as before. `pb.update_progress(...)`/
    `pb.cancelled` are safe no-ops in the fallback case, so callers never
    need an extra branch."""
    def __init__(self, **kwargs):
        self._kwargs = kwargs
        self._real = None

    def __enter__(self):
        try:
            self._real = forms.ProgressBar(**self._kwargs)
            return self._real.__enter__()
        except Exception:
            self._real = None
            return self

    def __exit__(self, exc_type, exc_value, tb):
        if self._real is not None:
            return self._real.__exit__(exc_type, exc_value, tb)
        return False

    @property
    def cancelled(self):
        return False

    def update_progress(self, i, total):
        pass


# ==========================================================================
# window
# ==========================================================================
class Dee3DWindow(dee_branding.DeeBrandedWindow):
    # Exists before the base class loads the XAML, because loading the
    # XAML fires SelectionChanged before __init__ has finished.
    _ready = False

    def __init__(self, xaml_file, doc, view_rows):
        dee_branding.DeeBrandedWindow.__init__(self, xaml_file)
        self.doc = doc
        self._all_views = view_rows
        self._cat_rows = []
        self._settings = deew_settings.load(_SETTINGS, _DEFAULTS)

        for label, _detail, _lod in core.QUALITY_PRESETS:
            self.quality_cb.Items.Add(label)
        for mode in core.COLOR_MODES:
            self.color_cb.Items.Add(mode)

        # Assigned here rather than in the XAML: a selection set in the
        # markup fires the handler before this object has its fields.
        self.quality_cb.SelectedIndex = self._clamp(
            self._settings.get("quality", 1), len(core.QUALITY_PRESETS))
        self.color_cb.SelectedIndex = self._clamp(
            self._settings.get("color_mode", 0), len(core.COLOR_MODES))
        self.links_cb.IsChecked = bool(self._settings.get("include_links"))
        self.params_cb.IsChecked = bool(self._settings.get("include_params"))
        self.open_cb.IsChecked = bool(self._settings.get("open_after", True))
        self.limit_tb.Text = str(self._settings.get("max_triangles", core.DEFAULT_MAX_TRIANGLES))

        self._ready = True
        self._refresh_views()
        if self.views_grid.Items.Count:
            self.views_grid.SelectedIndex = 0

    @staticmethod
    def _clamp(value, length):
        try:
            value = int(value)
        except Exception:
            return 0
        return max(0, min(length - 1, value))

    def _guard(self, fn, *args):
        """WPF swallows exceptions raised inside an event handler, which
        would make a real bug look exactly like 'the button does
        nothing'. Every handler goes through here so a failure is
        visible instead."""
        try:
            return fn(*args)
        except Exception as e:
            self.status_tb.Text = "ERROR: {0}".format(e)
            forms.alert("Dee3D hit an error:\n\n{0}\n\n{1}".format(
                e, traceback.format_exc()[-900:]), title=_TOOL)

    # ---------------- views ----------------
    def _shown_views(self):
        needle = (self.view_filter_tb.Text or "").strip().lower()
        if not needle:
            return list(self._all_views)
        return [r for r in self._all_views if needle in r.label.lower()]

    def _refresh_views(self):
        rows = self._shown_views()
        self.views_grid.ItemsSource = None
        self.views_grid.ItemsSource = rows
        return rows

    def view_filter_changed(self, sender, args):
        if not self._ready:
            return
        self._guard(self._refresh_views)

    def views_selection_changed(self, sender, args):
        if not self._ready:
            return
        self._guard(self._load_categories)

    def _selected_view(self):
        row = self.views_grid.SelectedItem
        return row.view if row is not None else None

    # ---------------- categories ----------------
    def _load_categories(self):
        view = self._selected_view()
        if view is None:
            self.cats_grid.ItemsSource = None
            self._cat_rows = []
            self._update_counts()
            return
        self.status_tb.Text = "Reading what '{0}' shows...".format(core.element_name(view))
        elements = core.collect_view_elements(self.doc, view)
        self._cat_rows = [CategoryRow(name, count)
                          for name, count in core.categories_of(elements)]
        self.cats_grid.ItemsSource = None
        self.cats_grid.ItemsSource = self._cat_rows
        self._update_counts()
        self.status_tb.Text = "'{0}' shows {1} model element(s) across {2} categor(y/ies).".format(
            core.element_name(view), len(elements), len(self._cat_rows))

    def _update_counts(self):
        on = [r for r in self._cat_rows if r.included]
        total = sum(r.count for r in on)
        self.cats_count_tb.Text = "{0} of {1} on".format(len(on), len(self._cat_rows))
        self.estimate_tb.Text = (
            "{0} element(s) will be exported.".format(total) if total
            else "Nothing selected to export.")

    def cat_toggle_click(self, sender, args):
        """The model is set from the CheckBox's own state rather than
        trusting the TwoWay binding to have written it back, so this
        works regardless of how WPF resolved the binding."""
        def run():
            row = sender.DataContext
            if row is None:
                return
            row.included = sender.IsChecked is True
            self._update_counts()
        self._guard(run)

    def _set_all_cats(self, value):
        for r in self._cat_rows:
            r.included = value
        self.cats_grid.ItemsSource = None
        self.cats_grid.ItemsSource = self._cat_rows
        self._update_counts()

    def cats_all_click(self, sender, args):
        self._guard(self._set_all_cats, True)

    def cats_none_click(self, sender, args):
        self._guard(self._set_all_cats, False)

    # ---------------- export ----------------
    def _read_options(self):
        q_index = max(0, self.quality_cb.SelectedIndex)
        _label, detail, lod = core.QUALITY_PRESETS[q_index]
        try:
            max_tris = int(str(self.limit_tb.Text).replace(",", "").strip())
        except Exception:
            max_tris = core.DEFAULT_MAX_TRIANGLES
        if max_tris < 1000:
            max_tris = 1000
        included = set(r.name for r in self._cat_rows if r.included)
        return {
            "detail": detail,
            "lod": lod,
            "color_mode": core.COLOR_MODES[max(0, self.color_cb.SelectedIndex)],
            "categories": included,
            "include_links": self.links_cb.IsChecked is True,
            "include_params": self.params_cb.IsChecked is True,
            "max_triangles": max_tris,
        }

    def _save_settings(self):
        try:
            deew_settings.save(_SETTINGS, {
                "quality": self.quality_cb.SelectedIndex,
                "color_mode": self.color_cb.SelectedIndex,
                "include_links": self.links_cb.IsChecked is True,
                "include_params": self.params_cb.IsChecked is True,
                "open_after": self.open_cb.IsChecked is True,
                "max_triangles": self._read_options()["max_triangles"],
                "last_folder": self._settings.get("last_folder", ""),
            })
        except Exception:
            pass

    def _ask_path(self, model_name, view_name):
        dlg = SaveFileDialog()
        dlg.Title = "Save the 3D model as a web page"
        dlg.Filter = "Web page (*.html)|*.html"
        dlg.DefaultExt = "html"
        dlg.AddExtension = True
        dlg.FileName = core.default_file_name(model_name, view_name)
        start = self._settings.get("last_folder") or _onedrive_folder()
        if start and os.path.isdir(start):
            dlg.InitialDirectory = start
        if dlg.ShowDialog() != DialogResult.OK:
            return None
        return dlg.FileName

    def export_click(self, sender, args):
        self._guard(self._export)

    def _export(self):
        view = self._selected_view()
        if view is None:
            forms.alert("Pick a 3D view first.", title=_TOOL)
            return
        options = self._read_options()
        if not options["categories"]:
            forms.alert("Every category is switched off, so there would be nothing "
                        "in the file. Tick at least one.", title=_TOOL)
            return

        model_name = _model_name(self.doc)
        view_name = core.element_name(view)
        out_path = self._ask_path(model_name, view_name)
        if not out_path:
            return

        self._settings["last_folder"] = os.path.dirname(out_path)
        self._save_settings()

        scene = None
        with _SafeProgress(title="Dee3D - reading geometry from '{0}'...".format(view_name),
                               cancellable=True) as pb:
            def progress(done, total, label):
                if pb.cancelled:
                    return False
                pb.update_progress(done, total)
                return True
            scene = core.build_scene(self.doc, view, options, progress)

        if scene.cancelled:
            self.status_tb.Text = "Cancelled - nothing was written."
            forms.alert("Cancelled. No file was written.", title=_TOOL)
            return

        if scene.triangles <= 0:
            forms.alert(
                "This view produced no 3D geometry, so there is nothing to export.\n\n"
                "Check that the view is not empty, that its section box is not cutting "
                "everything away, and that the categories you need are ticked.",
                title=_TOOL)
            self.status_tb.Text = "No geometry found in '{0}'.".format(view_name)
            return

        with _SafeProgress(title="Dee3D - writing the web page...", indeterminate=True):
            payload = core.build_payload(self.doc, scene, {
                "model": model_name,
                "view": view_name,
                "date": DateTime.Now.ToString("yyyy-MM-dd HH:mm"),
            })
            size = core.write_html(payload, out_path)

        self._report(out_path, size, scene, options, view_name)

        if self.open_cb.IsChecked is True:
            _open_file(out_path)

    def _report(self, out_path, size, scene, options, view_name):
        warn = []
        if scene.hit_budget:
            warn.append(scene.notes[-1] if scene.notes else "The triangle limit was reached.")
        if scene.triangles > core.PHONE_COMFORT_TRIANGLES:
            warn.append(
                "At {0:,} triangles this will open on a PC but may be slow or run out of "
                "memory on an older phone. Re-exporting at Coarse quality, or with fewer "
                "categories, gives a much lighter file.".format(scene.triangles))

        self.status_tb.Text = "Exported {0} - {1}, {2} elements, {3} triangles.".format(
            os.path.basename(out_path), core.human_size(size),
            len(scene.els), scene.triangles)

        rows = [
            ("View", view_name),
            ("Saved to", out_path),
            ("File size", core.human_size(size)),
            ("Elements", "{0:,}".format(len(scene.els))),
            ("Triangles", "{0:,}".format(scene.triangles)),
            ("Colours", "{0}".format(len(scene.book.colors))),
            ("Quality", core.QUALITY_PRESETS[max(0, self.quality_cb.SelectedIndex)][0]),
            ("Linked models", "included" if options["include_links"] else "not included"),
            ("Element parameters", "included" if options["include_params"] else "not included"),
        ]
        html = '<h2 style="font-family:sans-serif;">Dee3D - export finished</h2><table style="font-family:sans-serif;font-size:12px;border-collapse:collapse;">'
        for key, value in rows:
            html += ('<tr><td style="padding:3px 14px 3px 0;color:#888;">{0}</td>'
                     '<td style="padding:3px 0;"><b>{1}</b></td></tr>'.format(key, value))
        html += "</table>"
        for w in warn:
            html += ('<div style="margin-top:8px;padding:7px 11px;background:#8d6e19;color:#fff;'
                     'border-radius:4px;font-family:sans-serif;font-size:12px;">{0}</div>'.format(w))
        html += (
            '<div style="margin-top:10px;padding:9px 12px;background:#2e7d32;color:#fff;'
            'border-radius:4px;font-family:sans-serif;font-size:12px;line-height:1.6;">'
            '<b>Sharing it</b><br>'
            'This one file is the whole model - there is nothing else to send with it.'
            '<br>&bull; <b>OneDrive:</b> save it into a OneDrive folder, then right-click the '
            'file in File Explorer and choose Share to get a link that opens on any phone.'
            '<br>&bull; <b>Email / WhatsApp / Teams:</b> attach the file as it is. The person '
            'receiving it just opens it - Android, iPhone and Windows all open it in their browser.'
            '<br>&bull; No internet is needed to VIEW it, so it still works on site with no signal.'
            "</div>")
        output.print_html(html)

        MessageBox.Show(
            "Exported '{0}'\n\n{1}  -  {2:,} elements  -  {3:,} triangles\n\n"
            "Send this single file to anyone - it opens in the browser on Windows, "
            "Android and iPhone, with no app and no internet.".format(
                os.path.basename(out_path), core.human_size(size),
                len(scene.els), scene.triangles),
            _TOOL)

    def close_click(self, sender, args):
        self._save_settings()
        self.Close()


# ==========================================================================
# helpers
# ==========================================================================
def _model_name(doc):
    try:
        title = doc.Title
        if title:
            return os.path.splitext(title)[0]
    except Exception:
        pass
    return "Model"


def _onedrive_folder():
    """A sensible starting folder, since 'save it on OneDrive so I can
    share it' is the main reason this tool exists."""
    for key in ("OneDriveCommercial", "OneDriveConsumer", "OneDrive"):
        path = os.environ.get(key)
        if path and os.path.isdir(path):
            return path
    return ""


def _open_file(path):
    try:
        Process.Start(path)
        return True
    except Exception:
        pass
    try:
        os.startfile(path)
        return True
    except Exception:
        return False


def main():
    doc = __revit__.ActiveUIDocument.Document if __revit__.ActiveUIDocument else None
    if doc is None:
        forms.alert("Open a Revit project first.", title=_TOOL)
        return
    if doc.IsFamilyDocument:
        forms.alert("Dee3D exports a project's 3D view. It cannot be run inside the "
                    "Family Editor.", title=_TOOL)
        return

    if not os.path.isfile(core.template_path()):
        forms.alert("The viewer template is missing:\n\n{0}\n\nThe exported file cannot be "
                    "built without it.".format(core.template_path()), title=_TOOL)
        return

    views = core.list_3d_views(doc)
    if not views:
        forms.alert("This model has no 3D views. Create one (or use the default {3D} view) "
                    "and run Dee3D again.", title=_TOOL)
        return

    window = Dee3DWindow(_XAML_FILE, doc, [ViewRow(v) for v in views])
    window.ShowDialog()


main()
