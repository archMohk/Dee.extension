# -*- coding: utf-8 -*-
"""
dee_control_panel_service
Scan / state / apply logic for DeeControl - the Control Panel that turns
individual DeePack buttons, dropdown items and whole panels on and off.

--------------------------------------------------------------------
How a button is actually switched off
--------------------------------------------------------------------
Via the `layout:` list in each container's bundle.yaml. This is not a
guess - it is confirmed in pyRevit's own source, in
pyrevitlib/pyrevit/extensions/genericcomps.py, GenericUIContainer:

    def __iter__(self):
        # if item is not listed in layout, it will not be created
        if self.layout_items:
            ...only components matching a layout item are returned...
        else:
            return self.components

Two consequences drive the whole design here:

1. An item missing from a non-empty `layout:` is never created. That is
   the off switch, and it needs no folder renaming - which matters,
   because Revit holds locks on __pycache__ while it is running and a
   folder rename can simply fail mid-session.

2. An EMPTY or ABSENT layout means "show everything" (the `else` branch
   above). So writing an empty layout list to hide a whole panel would do
   the exact opposite. A container with nothing left enabled is therefore
   removed from ITS parent's layout instead - handled by
   effective_enabled() below, which is why disabling every button in a
   panel makes the panel itself disappear.

--------------------------------------------------------------------
Ordering is preserved deliberately
--------------------------------------------------------------------
The ribbon order lives only in those layout lists, so switching a button
off and back on must not shuffle its neighbours. The canonical order of
every container is captured into the config file the first time it is
seen and reused from then on; folders that appear later (a new button)
are appended. Nothing is ever reordered as a side effect of a toggle.

--------------------------------------------------------------------
Editing bundle.yaml
--------------------------------------------------------------------
Rewritten line-by-line rather than through a YAML round-trip: these files
are hand-written and carry comments, quoting and long single-line
tooltips that a dump/reload would reformat wholesale. Only the `layout:`
block is touched; every other line is passed through byte-for-byte. A
survey of all 28 container bundles confirmed they use plain
`key: value` lines with no block scalars, so this is safe here.

NOTE: these bundle.yaml files are tracked in git, so toggling buttons
produces real repository changes. That is expected, not a bug - it is
also what makes the state reviewable and revertible with git.
"""
import io
import json
import os

TAB_FOLDER_SUFFIX = ".tab"
KIND_SUFFIXES = [
    (".panel", "Panel"),
    (".stack", "Group"),
    (".pulldown", "Dropdown"),
    (".pushbutton", "Button"),
]
CONTAINER_KINDS = set(["Tab", "Panel", "Group", "Dropdown"])

# Never switchable - turning these off would remove the only way back in.
LOCKED_NAMES = set(["About", "DeeControl"])


def _kind_of(folder_name):
    for suffix, kind in KIND_SUFFIXES:
        if folder_name.endswith(suffix):
            return kind, folder_name[:-len(suffix)]
    return None, None


def read_bundle_meta(bundle_path):
    """Minimal reader for the four keys these bundles actually use.
    Splits on the FIRST colon only, because a tooltip legitimately
    contains colons and parentheses."""
    meta = {"title": None, "tooltip": None, "layout": []}
    if not os.path.isfile(bundle_path):
        return meta
    try:
        with io.open(bundle_path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except Exception:
        return meta
    in_layout = False
    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        if in_layout:
            if stripped.startswith("-"):
                meta["layout"].append(stripped[1:].strip().strip('"').strip("'"))
                continue
            in_layout = False
        if line.startswith("layout:"):
            in_layout = True
            continue
        if ":" in line and not line.startswith(" "):
            key, value = line.split(":", 1)
            key = key.strip()
            if key in ("title", "tooltip"):
                meta[key] = value.strip().strip('"').strip("'")
    return meta


class BundleNode(object):
    """Holds paths and plain strings only - this is filesystem state, never
    a Revit element."""

    def __init__(self, path, name, kind, title, brief, rel_key, depth):
        self.path = path
        self.name = name          # folder stem, i.e. what a layout list holds
        self.kind = kind
        self.title = title or name
        self.brief = brief or ""
        self.rel_key = rel_key
        self.depth = depth
        self.children = []
        self.parent = None
        self.enabled = True
        self.locked = name in LOCKED_NAMES
        self.note = "always on" if self.locked else ""

    @property
    def display_name(self):
        """Indented by depth so the grid reads as the ribbon hierarchy without
        needing a TreeView (which cannot be filtered by a search box nearly as
        simply)."""
        return u"{0}{1}".format(u"        " * max(0, self.depth), self.title)

    @property
    def is_container(self):
        return self.kind in CONTAINER_KINDS

    @property
    def bundle_path(self):
        return os.path.join(self.path, "bundle.yaml")


def _scan_children(parent_node, tab_root):
    try:
        entries = sorted(os.listdir(parent_node.path))
    except Exception:
        return
    found = {}
    for entry in entries:
        full = os.path.join(parent_node.path, entry)
        if not os.path.isdir(full):
            continue
        kind, name = _kind_of(entry)
        if kind is None:
            continue
        meta = read_bundle_meta(os.path.join(full, "bundle.yaml"))
        title = meta["title"] or name
        # A title of "." is a deliberate icon-only button; the folder name
        # is the useful label in a list like this.
        if not title.strip() or title.strip() == ".":
            title = name
        rel = os.path.relpath(full, tab_root).replace("\\", "/")
        node = BundleNode(full, name, kind, title, meta["tooltip"], rel,
                          parent_node.depth + 1)
        node.parent = parent_node
        found[name] = node
        if node.is_container:
            _scan_children(node, tab_root)

    # Canonical order: what the layout already says, then anything on disk
    # that the layout does not mention (a newly added button), alphabetically.
    meta = read_bundle_meta(parent_node.bundle_path)
    ordered = []
    for name in meta["layout"]:
        if name in found and found[name] not in ordered:
            ordered.append(found[name])
    for name in sorted(found.keys()):
        if found[name] not in ordered:
            ordered.append(found[name])
    parent_node.children = ordered


def scan(tab_root):
    """Returns the root node for the .tab folder, with the whole tree under
    it. The root itself is never toggled."""
    folder = os.path.basename(tab_root.rstrip("\\/"))
    name = folder[:-len(TAB_FOLDER_SUFFIX)] if folder.endswith(TAB_FOLDER_SUFFIX) else folder
    meta = read_bundle_meta(os.path.join(tab_root, "bundle.yaml"))
    root = BundleNode(tab_root, name, "Tab", meta["title"] or name, "", "", -1)
    root.locked = True
    _scan_children(root, tab_root)
    return root


def flatten(node, out=None):
    """Depth-first, in canonical order - the order the ribbon is built in,
    which is the order that makes sense in the list."""
    if out is None:
        out = []
    for child in node.children:
        out.append(child)
        flatten(child, out)
    return out


def effective_enabled(node):
    """A container is only really on if it is ticked AND has at least one
    descendant that is on. This is what makes a panel vanish once its last
    button is switched off - and it must, because an EMPTY layout list means
    "show everything" to pyRevit, not "show nothing"."""
    if node.locked:
        return True
    if not node.enabled:
        return False
    if not node.is_container:
        return True
    return any(effective_enabled(c) for c in node.children)


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------
def _container_key(node):
    return node.rel_key or "."


def collect_order(root):
    """{container_key: [child names in canonical order]} for the whole tree."""
    orders = {}

    def walk(node):
        if node.is_container or node.kind == "Tab":
            orders[_container_key(node)] = [c.name for c in node.children]
            for child in node.children:
                walk(child)

    walk(root)
    return orders


def apply_order(root, orders):
    """Re-sorts each container's children to the SAVED order.

    This is what stops a disabled button drifting to the end of the ribbon.
    A disabled button is absent from the live layout: list, so re-deriving
    order from that file alone would append it on re-enable instead of
    returning it to its slot. The saved order is therefore authoritative,
    and anything not in it (a button added since) keeps its scanned position
    at the end."""
    def walk(node):
        if node.is_container or node.kind == "Tab":
            saved = orders.get(_container_key(node))
            if saved:
                index = dict((name, i) for i, name in enumerate(saved))
                fallback = len(saved)
                node.children.sort(
                    key=lambda c, _i=index, _f=fallback: _i.get(c.name, _f))
            for child in node.children:
                walk(child)

    walk(root)


def load_state(config_path, root, nodes):
    """Applies the saved disabled-set AND the saved ordering onto a freshly
    scanned tree. Anything not mentioned stays on, so a newly added button is
    enabled by default."""
    if not os.path.isfile(config_path):
        return False
    try:
        with io.open(config_path, encoding="utf-8") as fh:
            data = json.loads(fh.read())
    except Exception:
        return False
    apply_order(root, data.get("order", {}))
    disabled = set(data.get("disabled", []))
    for node in nodes:
        if node.rel_key in disabled and not node.locked:
            node.enabled = False
    return True


def save_state(config_path, root, nodes):
    data = {
        "_comment": ("Written by DeeControl. 'disabled' lists the bundles kept "
                     "out of their parent's layout: list in bundle.yaml, and "
                     "'order' remembers the real ribbon order so a button "
                     "switched back on returns to its original slot. Delete "
                     "this file and re-apply to turn everything back on."),
        "disabled": sorted(n.rel_key for n in nodes if not n.enabled and not n.locked),
        "order": collect_order(root),
    }
    folder = os.path.dirname(config_path)
    if folder and not os.path.isdir(folder):
        os.makedirs(folder)
    # json.dumps returns str on IronPython 2 and unicode on 3; io.open with an
    # encoding demands unicode, so normalise rather than assume either.
    text = json.dumps(data, indent=2, sort_keys=True)
    if not isinstance(text, type(u"")):
        text = text.decode("utf-8")
    with io.open(config_path, "w", encoding="utf-8") as fh:
        fh.write(text)


# --------------------------------------------------------------------------
# writing bundle.yaml
# --------------------------------------------------------------------------
def build_layout_block(names):
    lines = ["layout:"]
    for name in names:
        lines.append("  - {0}".format(name))
    return lines


def rewrite_layout(bundle_path, names):
    """Replaces ONLY the layout: block, leaving every other line untouched.
    Returns (changed, detail).

    Never writes an empty list: pyRevit treats an empty/absent layout as
    "show every child", so an empty one would do the opposite of what the
    caller means. A container with nothing enabled is dropped by its PARENT
    instead, and its own file is left alone."""
    if not names:
        return False, "nothing enabled - handled by the parent, file left alone"

    new_block = build_layout_block(names)
    if not os.path.isfile(bundle_path):
        io.open(bundle_path, "w", encoding="utf-8").write(
            u"\n".join(new_block) + u"\n")
        return True, "created with a layout"

    with io.open(bundle_path, encoding="utf-8") as fh:
        original = fh.read()
    lines = original.splitlines()
    out = []
    i = 0
    replaced = False
    while i < len(lines):
        line = lines[i]
        if line.startswith("layout:") and not replaced:
            out.extend(new_block)
            replaced = True
            i += 1
            # Swallow the old list items (and blank lines between them).
            while i < len(lines):
                nxt = lines[i]
                if nxt.strip().startswith("-") or not nxt.strip():
                    i += 1
                    continue
                break
            continue
        out.append(line)
        i += 1

    if not replaced:
        if out and out[-1].strip():
            out.append("")
        out.extend(new_block)

    new_text = u"\n".join(out).rstrip() + u"\n"
    if new_text == original.rstrip() + u"\n":
        return False, "already correct"
    with io.open(bundle_path, "w", encoding="utf-8") as fh:
        fh.write(new_text)
    return True, "layout updated"


def apply_state(root):
    """Walks every container and rewrites its layout to the enabled children,
    in canonical order. Returns a list of (rel_key, detail) for the report."""
    results = []

    def walk(node):
        if node.is_container or node.kind == "Tab":
            names = [c.name for c in node.children if effective_enabled(c)]
            changed, detail = rewrite_layout(node.bundle_path, names)
            if changed:
                results.append((node.rel_key or node.name, detail))
            for child in node.children:
                walk(child)

    walk(root)
    return results


def refresh_notes(nodes):
    """Explains, per row, why something will not appear even though its own box
    is ticked - a button inside a switched-off panel, or a container whose last
    child was switched off. Without this the grid would show a ticked button
    that silently never appears."""
    for node in nodes:
        if node.locked:
            node.note = "always on"
            continue
        if not node.enabled:
            node.note = "OFF"
            continue
        if node.is_container and not effective_enabled(node):
            node.note = "hidden - nothing inside it is on"
            continue
        parent = node.parent
        while parent is not None and parent.kind != "Tab":
            if not effective_enabled(parent):
                node.note = "hidden - '{0}' is off".format(parent.title)
                break
            parent = parent.parent
        else:
            node.note = ""


def summarize(nodes):
    """(buttons_on, buttons_total, hidden_containers) for the status line."""
    buttons = [n for n in nodes if not n.is_container]
    on = len([n for n in buttons if n.enabled])
    hidden_containers = len([n for n in nodes
                             if n.is_container and not effective_enabled(n)])
    return on, len(buttons), hidden_containers


if __name__ == "__main__":
    import shutil
    import tempfile
    import unittest

    def make_tree(root):
        """A miniature DeePack: one panel with a stack of two buttons and one
        loose button, plus a locked About panel."""
        def mk(path, text):
            os.makedirs(path)
            with io.open(os.path.join(path, "bundle.yaml"), "w",
                         encoding="utf-8") as fh:
                fh.write(text)

        tab = os.path.join(root, "DeePack.tab")
        mk(tab, u'title: "DeePack"\nlayout:\n  - Cloud\n  - About\n')
        cloud = os.path.join(tab, "Cloud.panel")
        mk(cloud, u'title: "Cloud"\nlayout:\n  - Solo\n  - CloudStack\n')
        mk(os.path.join(cloud, "Solo.pushbutton"), u'title: Solo\ntooltip: a lone button\n')
        stack = os.path.join(cloud, "CloudStack.stack")
        mk(stack, u'layout:\n  - Alpha\n  - Beta\n')
        mk(os.path.join(stack, "Alpha.pushbutton"), u'title: Alpha\ntooltip: first\n')
        mk(os.path.join(stack, "Beta.pushbutton"), u'title: Beta\ntooltip: second\n')
        about = os.path.join(tab, "About.panel")
        mk(about, u'title: "About"\nlayout:\n  - DeeControl\n')
        mk(os.path.join(about, "DeeControl.pushbutton"), u'title: DeeControl\ntooltip: this tool\n')
        return tab

    class Base(unittest.TestCase):
        def setUp(self):
            self.tmp = tempfile.mkdtemp()
            self.tab = make_tree(self.tmp)
            self.root = scan(self.tab)
            self.nodes = flatten(self.root)
            self.by = dict((n.name, n) for n in self.nodes)

        def tearDown(self):
            shutil.rmtree(self.tmp, ignore_errors=True)

        def layout_of(self, rel):
            return read_bundle_meta(
                os.path.join(self.tab, rel, "bundle.yaml"))["layout"]

    class ScanTests(Base):
        def test_finds_every_bundle(self):
            self.assertEqual(
                sorted(self.by.keys()),
                ["About", "Alpha", "Beta", "Cloud", "CloudStack", "DeeControl", "Solo"])

        def test_kinds(self):
            self.assertEqual(self.by["Cloud"].kind, "Panel")
            self.assertEqual(self.by["CloudStack"].kind, "Group")
            self.assertEqual(self.by["Alpha"].kind, "Button")

        def test_brief_comes_from_tooltip(self):
            self.assertEqual(self.by["Solo"].brief, "a lone button")

        def test_canonical_order_follows_existing_layout(self):
            self.assertEqual([c.name for c in self.by["Cloud"].children],
                             ["Solo", "CloudStack"])

        def test_about_and_control_are_locked(self):
            self.assertTrue(self.by["About"].locked)
            self.assertTrue(self.by["DeeControl"].locked)
            self.assertFalse(self.by["Solo"].locked)

    class EffectiveTests(Base):
        def test_disabled_button_is_off(self):
            self.by["Alpha"].enabled = False
            self.assertFalse(effective_enabled(self.by["Alpha"]))

        def test_container_dies_when_all_children_off(self):
            self.by["Alpha"].enabled = False
            self.by["Beta"].enabled = False
            self.assertFalse(effective_enabled(self.by["CloudStack"]))

        def test_container_survives_one_child(self):
            self.by["Alpha"].enabled = False
            self.assertTrue(effective_enabled(self.by["CloudStack"]))

        def test_locked_stays_on_even_if_unticked(self):
            self.by["DeeControl"].enabled = False
            self.assertTrue(effective_enabled(self.by["DeeControl"]))

    class ApplyTests(Base):
        def test_disabling_removes_only_that_name(self):
            self.by["Alpha"].enabled = False
            apply_state(self.root)
            self.assertEqual(self.layout_of("Cloud.panel/CloudStack.stack"), ["Beta"])

        def test_order_is_preserved_on_re_enable(self):
            """The regression that motivated persisting `order`: once Alpha is
            switched off it vanishes from the layout file, so re-deriving order
            from that file alone would append it AFTER Beta on re-enable."""
            cfg = os.path.join(self.tmp, "cfg.json")
            self.by["Alpha"].enabled = False
            save_state(cfg, self.root, self.nodes)
            apply_state(self.root)
            self.assertEqual(self.layout_of("Cloud.panel/CloudStack.stack"), ["Beta"])

            fresh = scan(self.tab)
            nodes = flatten(fresh)
            load_state(cfg, fresh, nodes)
            by = dict((n.name, n) for n in nodes)
            by["Alpha"].enabled = True
            apply_state(fresh)
            self.assertEqual(self.layout_of("Cloud.panel/CloudStack.stack"),
                             ["Alpha", "Beta"])

        def test_without_saved_order_it_would_drift(self):
            """Documents exactly why the saved order is needed - without it the
            scan can only see the pruned layout."""
            self.by["Alpha"].enabled = False
            apply_state(self.root)
            fresh = scan(self.tab)
            by = dict((n.name, n) for n in flatten(fresh))
            self.assertEqual([c.name for c in by["CloudStack"].children],
                             ["Beta", "Alpha"])

        def test_empty_container_is_dropped_by_parent(self):
            self.by["Alpha"].enabled = False
            self.by["Beta"].enabled = False
            apply_state(self.root)
            self.assertEqual(self.layout_of("Cloud.panel"), ["Solo"])

        def test_never_writes_an_empty_layout(self):
            for n in self.nodes:
                if not n.locked:
                    n.enabled = False
            apply_state(self.root)
            # Cloud has nothing left, so the TAB drops it - and Cloud's own
            # file must NOT have become an empty layout (that means "show all").
            self.assertEqual(self.layout_of(""), ["About"])
            self.assertTrue(len(self.layout_of("Cloud.panel")) > 0)

        def test_other_keys_survive_a_rewrite(self):
            self.by["Solo"].enabled = False
            apply_state(self.root)
            with io.open(os.path.join(self.tab, "Cloud.panel", "bundle.yaml"),
                         encoding="utf-8") as fh:
                text = fh.read()
            self.assertIn('title: "Cloud"', text)

        def test_layout_added_when_absent(self):
            path = os.path.join(self.tab, "Cloud.panel", "CloudStack.stack",
                                "Alpha.pushbutton", "bundle.yaml")
            changed, _ = rewrite_layout(path, ["X"])
            self.assertTrue(changed)
            meta = read_bundle_meta(path)
            self.assertEqual(meta["layout"], ["X"])
            self.assertEqual(meta["title"], "Alpha")
            self.assertEqual(meta["tooltip"], "first")

    class ConfigTests(Base):
        def test_round_trip(self):
            self.by["Alpha"].enabled = False
            self.by["Solo"].enabled = False
            cfg = os.path.join(self.tmp, "cfg.json")
            save_state(cfg, self.root, self.nodes)
            fresh = scan(self.tab)
            nodes = flatten(fresh)
            load_state(cfg, fresh, nodes)
            by = dict((n.name, n) for n in nodes)
            self.assertFalse(by["Alpha"].enabled)
            self.assertFalse(by["Solo"].enabled)
            self.assertTrue(by["Beta"].enabled)

        def test_missing_config_leaves_everything_on(self):
            fresh = scan(self.tab)
            nodes = flatten(fresh)
            self.assertFalse(
                load_state(os.path.join(self.tmp, "nope.json"), fresh, nodes))
            self.assertTrue(all(n.enabled for n in nodes))

        def test_order_map_covers_every_container(self):
            orders = collect_order(self.root)
            self.assertIn(".", orders)
            self.assertIn("Cloud.panel", orders)
            self.assertIn("Cloud.panel/CloudStack.stack", orders)
            self.assertEqual(orders["Cloud.panel"], ["Solo", "CloudStack"])

        def test_apply_order_puts_a_stray_child_last(self):
            apply_order(self.root, {"Cloud.panel/CloudStack.stack": ["Beta"]})
            by = dict((n.name, n) for n in flatten(self.root))
            self.assertEqual([c.name for c in by["CloudStack"].children],
                             ["Beta", "Alpha"])

    class SummaryTests(Base):
        def test_counts(self):
            on, total, hidden = summarize(self.nodes)
            self.assertEqual(total, 4)     # Solo, Alpha, Beta, DeeControl
            self.assertEqual(on, 4)
            self.assertEqual(hidden, 0)

        def test_hidden_container_counted(self):
            self.by["Alpha"].enabled = False
            self.by["Beta"].enabled = False
            _on, _total, hidden = summarize(self.nodes)
            self.assertEqual(hidden, 1)

    unittest.main(verbosity=2)
