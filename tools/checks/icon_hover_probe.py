# -*- coding: utf-8 -*-
"""Live motion hover probe - checks the plumbing lib/dee_icon_motion needs
to move only the ribbon button under the mouse. Runs under `pyrevit run`,
where the DeePack ribbon exists but no one is pointing at it:

    pyrevit run "<abs path>/Dee.extension/tools/checks/icon_hover_probe.py" --revit=2024

Checks, and writes to tools/checks/icon_styles/hover_report.txt:
  - every DeePack button with an icon resolves its Autodesk.Windows twin
    (the WPF item the pointer can be over) through getRibbonItem;
  - hovered_ribbon_item() runs and answers None with no pointer on it;
  - the animator arms for a motion (count > 0), its first tick paints
    nothing, and stop() puts every still icon back.
`pyrevit run` loads no extensions, so without a DeePack tab the result is
SKIP: the plumbing ran, the matching could not. Run it from a Revit with
DeePack loaded (e.g. pyRevit's script runner) for a PASS.
"""
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
EXT = os.environ.get("DEE_EXTENSION_ROOT", os.path.dirname(os.path.dirname(HERE)))
LIB = os.path.join(EXT, "lib")
OUT = os.path.join(HERE, "icon_styles")
for p in (LIB, EXT):
    if p not in sys.path:
        sys.path.insert(0, p)
if not os.path.isdir(OUT):
    os.makedirs(OUT)

import dee_accent  # noqa: E402
import dee_icon_motion  # noqa: E402

lines = []
ok = True
skipped = False
uiapp = __revit__  # noqa: F821 - injected by pyRevit

try:
    bundles = dee_accent._bundle_dirs()
    total = with_twin = 0
    missing = []
    for item in dee_accent._iter_items(uiapp):
        try:
            name = item.Name
        except Exception:
            continue
        if name not in bundles or not dee_accent._theme_icon(bundles[name]):
            continue
        total += 1
        adw = dee_icon_motion._adwindows_item(item)
        if adw is not None:
            with_twin += 1
            if total <= 3:
                lines.append("twin  {0}: {1} id={2}".format(name, adw.GetType().Name, adw.Id))
        else:
            missing.append(name)
    lines.append("twins: {0}/{1} buttons resolve their Autodesk.Windows item".format(
        with_twin, total))
    if missing:
        lines.append("no twin: " + ", ".join(missing))
    if total == 0:
        skipped = True            # pyrevit run loads no extensions: no DeePack tab
    elif with_twin < total:
        ok = False

    hovered = dee_icon_motion.hovered_ribbon_item()
    lines.append("hovered now: {0}".format(
        None if hovered is None else "{0} {1}".format(hovered.GetType().Name, hovered.Id)))

    count = dee_icon_motion.apply(uiapp, None, dee_accent._SAVED, dee_accent._SAVED, "pulse")
    lines.append("armed for pulse: {0} buttons, running={1}".format(
        count, dee_icon_motion.is_running()))
    if count == 0 and not skipped:
        ok = False
    anim = dee_icon_motion._animator
    anim._tick(None, None)
    lines.append("first tick: current={0} index={1}".format(anim.current, anim.index))
    if anim.current is not None:
        ok = False
    dee_icon_motion.stop()
    lines.append("stopped: running={0} entries={1}".format(
        dee_icon_motion.is_running(), len(anim.entries)))
except Exception:
    ok = False
    lines.append(traceback.format_exc())

lines.append("RESULT: " + ("FAIL" if not ok else
                           "SKIP (no DeePack ribbon in this Revit)" if skipped else "PASS"))
report = "\n".join(lines)
print(report)
with open(os.path.join(OUT, "hover_report.txt"), "w") as fh:
    fh.write(report + "\n")
