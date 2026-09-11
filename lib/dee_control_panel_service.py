# -*- coding: utf-8 -*-
"""
dee_control_panel_service
Scan / state / apply logic for DeeControl - the Control Panel that greys
out DeePack buttons you do not want, without removing them from the
ribbon.

--------------------------------------------------------------------
How a button is greyed out (NOT hidden)
--------------------------------------------------------------------
By giving it a `context:` rule that can never be satisfied. Revit greys
out - rather than hides - any command whose availability check returns
false, so the button stays exactly where it is, visibly faded and
unclickable, with its tooltip intact.

The rule used is a logical contradiction:

    context:
      rule: "(zero-doc)&!(zero-doc)"

pyRevit passes a `rule:` string straight through to the runtime
untouched. Confirmed in pyRevit's own source,
pyrevitlib/pyrevit/extensions/genericcomps.py,
GenericUICommand._parse_context_directives:

    elif isinstance(context, dict):
        if "rule" in context:
            return context["rule"]

and the grammar it must match is built a few lines above it - each rule
is wrapped as "({rule})", a negated one is prefixed "!", and multiple
rules are joined with "&" (MDATA_COMMAND_CONTEXT_ALL_SEP).

"P and not P" is used deliberately in preference to something like
"zero-doc & selection". The latter only works if you have reasoned
correctly about what both tokens mean at runtime; a contradiction is
false whatever `zero-doc` evaluates to, so it cannot be wrong for a
reason this code cannot see.

--------------------------------------------------------------------
Why not the layout: list
--------------------------------------------------------------------
Omitting a button from a container's `layout:` list also works, and the
first version of this tool did exactly that - but it makes the button
DISAPPEAR. That was the wrong behaviour: a missing button looks like a
broken install, whereas a faded one reads as "switched off on purpose".
Layout lists are therefore left completely alone now, which also means
ribbon ORDER is never touched by this tool.

--------------------------------------------------------------------
The original context must be remembered
--------------------------------------------------------------------
35 DeePack buttons already carry `context: zero-doc`, which is what lets
them run with no project open. Overwriting that to disable a button and
then "restoring" it by reading the file back would lose it forever - the
file now holds the disable rule, not the original. Originals are
therefore captured into the config the first time a button is seen and
are authoritative from then on. (This is the same class of bug as the
ordering one this module hit earlier: never re-derive a canonical value
from a file your own tool overwrites.)

--------------------------------------------------------------------
Editing bundle.yaml
--------------------------------------------------------------------
Rewritten line-by-line rather than through a YAML round-trip: these are
hand-written files with quoting and long single-line tooltips that a
dump/reload would reformat wholesale. Only the `context:` key is touched.
All 61 button bundles were surveyed first - every existing context is the
single line `context: zero-doc`, with no block scalars anywhere.

NOTE: these bundle.yaml files are tracked in git, so switching buttons
off produces real repository changes. That is intended - it makes the
state reviewable and revertible.
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

# Never switchable - greying these would remove the only way back in.
LOCKED_NAMES = set(["About", "DeeControl"])

# P and not P. See the module docstring for why this exact shape.
DISABLED_RULE = "(zero-doc)&!(zero-doc)"


def _kind_of(folder_name):
    for suffix, kind in KIND_SUFFIXES:
        if folder_name.endswith(suffix):
            return kind, folder_name[:-len(suffix)]
    return None, None


def read_bundle_meta(bundle_path):
    """Minimal reader for the keys these bundles actually use. Splits on the
    FIRST colon only, because a tooltip legitimately contains colons.

    `context` comes back as the single-line string value, or the special
    string DISABLED_RULE when the file holds our disable block."""
    meta = {"title": None, "tooltip": None, "layout": [], "context": None}
    if not os.path.isfile(bundle_path):
        return meta
    try:
        with io.open(bundle_path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except Exception:
        return meta

    in_layout = False
    in_context_block = False
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

        if in_context_block:
            if line.startswith(" "):
                if stripped.startswith("rule:"):
                    meta["context"] = stripped.split(":", 1)[1].strip().strip('"').strip("'")
                continue
            in_context_block = False

        if line.startswith("layout:"):
            in_layout = True
            continue
        if line.startswith("context:"):
            value = line.split(":", 1)[1].strip()
            if value:
                meta["context"] = value.strip('"').strip("'")
            else:
                in_context_block = True
            continue
        if ":" in line and not line.startswith(" "):
            key, value = line.split(":", 1)
            key = key.strip()
            if key in ("title", "tooltip"):
                meta[key] = value.strip().strip('"').strip("'")
    return meta


class BundleNode(object):
    """Holds paths and plain strings only - this is filesystem state, never a
    Revit element."""

    def __init__(self, path, name, kind, title, brief, rel_key, depth):
        self.path = path
        self.name = name
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
        # The context this button had before DeeControl ever touched it.
        self.original_context = None
        # DeePack.tab's ribbon "View" dropdown's Favorite category shows
        # only favorited buttons (see lib/dee_ribbon_mode.py) - a
        # personal, per-machine pick list, unrelated to the on/off
        # greying above (favoriting something never touches its
        # bundle.yaml, so it needs no Apply/reload to take effect).
        self.favorite = False

    @property
    def display_name(self):
        """Indented by depth so the grid reads as the ribbon hierarchy without
        needing a TreeView (which a search box cannot filter nearly as
        simply)."""
        return u"{0}{1}".format(u"        " * max(0, self.depth), self.title)

    @property
    def is_toggleable(self):
        """Bound to the row checkbox's IsEnabled, so a locked row is greyed out
        rather than accepting a click and silently snapping back."""
        return not self.locked

    @property
    def is_container(self):
        return self.kind in CONTAINER_KINDS

    @property
    def is_favoritable(self):
        """Only real buttons can be favorited, not panels/groups - a
        panel-level 'favorite' would just duplicate what the dropdown's
        other categories already do."""
        return not self.is_container

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
        # A title of "." is a deliberate icon-only button; the folder name is
        # the useful label in a list like this.
        if not title.strip() or title.strip() == ".":
            title = name
        rel = os.path.relpath(full, tab_root).replace("\\", "/")
        node = BundleNode(full, name, kind, title, meta["tooltip"], rel,
                          parent_node.depth + 1)
        node.parent = parent_node
        # A file already holding the disable rule means this button is off.
        # Its ORIGINAL context is unknowable from the file at that point, which
        # is exactly why the config remembers it - see load_state.
        if meta["context"] == DISABLED_RULE:
            node.enabled = False
        else:
            node.original_context = meta["context"]
        found[name] = node
        if node.is_container:
            _scan_children(node, tab_root)

    # Order follows the container's layout: list where it has one, so the grid
    # reads in ribbon order. This tool never WRITES layout lists.
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
    """Returns the root node for the .tab folder, with the whole tree under it.
    The root itself is never toggled."""
    folder = os.path.basename(tab_root.rstrip("\\/"))
    name = folder[:-len(TAB_FOLDER_SUFFIX)] if folder.endswith(TAB_FOLDER_SUFFIX) else folder
    meta = read_bundle_meta(os.path.join(tab_root, "bundle.yaml"))
    root = BundleNode(tab_root, name, "Tab", meta["title"] or name, "", "", -1)
    root.locked = True
    _scan_children(root, tab_root)
    return root


def flatten(node, out=None):
    """Depth-first, in ribbon order."""
    if out is None:
        out = []
    for child in node.children:
        out.append(child)
        flatten(child, out)
    return out


def effective_enabled(node):
    """A button is live only if it is on AND every container above it is on.

    Ancestor-based, not descendant-based: switching a Panel off greys every
    button inside it. Nothing is ever removed, so an empty container is not a
    case that needs handling any more."""
    if node.locked:
        return True
    if not node.enabled:
        return False
    parent = node.parent
    while parent is not None:
        if not parent.locked and not parent.enabled:
            return False
        parent = parent.parent
    return True


def refresh_notes(nodes):
    """Explains, per row, why a button will be faded even though its own box is
    ticked - because a panel above it is off.

    A row switched off by its OWN tick gets no note: the unticked box already
    says that, and leaving the note empty means toggling one button changes no
    other row, so the window can skip rebuilding the grid and keep your scroll
    position."""
    for node in nodes:
        if node.locked:
            node.note = "always on"
            continue
        if not node.enabled:
            node.note = ""
            continue
        blocker = None
        parent = node.parent
        while parent is not None:
            if not parent.locked and not parent.enabled:
                blocker = parent
                break
            parent = parent.parent
        node.note = ("faded - '{0}' is off".format(blocker.title) if blocker else "")


def summarize(nodes):
    """(live_buttons, total_buttons, faded_buttons) for the status line."""
    buttons = [n for n in nodes if not n.is_container]
    live = len([n for n in buttons if effective_enabled(n)])
    return live, len(buttons), len(buttons) - live


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------
def load_state(config_path, root, nodes):
    """Applies the saved disabled-set and the saved ORIGINAL contexts.

    The originals matter: once a button has been switched off its file holds
    the disable rule, so the file can no longer tell us what its context used
    to be. Only the config can."""
    if not os.path.isfile(config_path):
        return False
    try:
        with io.open(config_path, encoding="utf-8") as fh:
            data = json.loads(fh.read())
    except Exception:
        return False
    originals = data.get("original_contexts", {})
    disabled = set(data.get("disabled", []))
    for node in nodes:
        if node.rel_key in originals:
            node.original_context = originals[node.rel_key]
        if node.rel_key in disabled and not node.locked:
            node.enabled = False
    return True


def save_state(config_path, root, nodes):
    data = {
        "_comment": ("Written by DeeControl. 'disabled' lists the bundles given "
                     "a never-true context: rule, which makes Revit grey the "
                     "button out instead of hiding it. 'original_contexts' "
                     "remembers what each button's context was BEFORE it was "
                     "switched off, so turning it back on restores it exactly. "
                     "Delete this file only if every button is currently on."),
        "disabled": sorted(n.rel_key for n in nodes if not n.enabled and not n.locked),
        "original_contexts": dict(
            (n.rel_key, n.original_context) for n in nodes if not n.is_container),
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


def load_favorites(favorites_path, nodes):
    """Applies the saved favorite set. Separate file from load_state's
    config_path deliberately - favorites are a personal, per-machine
    pick list (gitignored), not the team-wide on/off state that file
    holds (tracked in git)."""
    if not os.path.isfile(favorites_path):
        return False
    try:
        with io.open(favorites_path, encoding="utf-8") as fh:
            data = json.loads(fh.read())
    except Exception:
        return False
    favorited = set(data.get("favorites", []))
    for node in nodes:
        if node.name in favorited:
            node.favorite = True
    return True


def save_favorites(favorites_path, nodes):
    """Stores bare button NAMES, not rel_key paths - that is what
    lib/dee_ribbon_mode.py needs to match against a live RibbonItem's
    own .Name at runtime, which exposes no path back to the bundle it
    came from. Names are unique across this whole tab in practice (one
    tool = one distinct name), so this is unambiguous."""
    data = {
        "_comment": ("Written by DeeControl. Personal, per-machine list of "
                     "tool names shown when DeePack's ribbon 'View' dropdown "
                     "is set to Favorite - not shared via git."),
        "favorites": sorted(set(
            n.name for n in nodes if n.favorite and n.is_favoritable)),
    }
    folder = os.path.dirname(favorites_path)
    if folder and not os.path.isdir(folder):
        os.makedirs(folder)
    text = json.dumps(data, indent=2, sort_keys=True)
    if not isinstance(text, type(u"")):
        text = text.decode("utf-8")
    with io.open(favorites_path, "w", encoding="utf-8") as fh:
        fh.write(text)


# --------------------------------------------------------------------------
# writing bundle.yaml
# --------------------------------------------------------------------------
def _strip_key(lines, key):
    """Removes `key:` and any indented continuation lines under it. Returns
    (remaining_lines, index_where_it_was_or_None)."""
    out = []
    found_at = None
    i = 0
    prefix = key + ":"
    while i < len(lines):
        line = lines[i]
        if line.startswith(prefix) and found_at is None:
            found_at = len(out)
            i += 1
            while i < len(lines) and lines[i].startswith(" "):
                i += 1
            continue
        out.append(line)
        i += 1
    return out, found_at


def context_lines(context_value):
    """[] for no context, one line for a plain value, a block for the disable
    rule (which must be a mapping, since that is the only shape pyRevit passes
    through untouched)."""
    if context_value is None:
        return []
    if context_value == DISABLED_RULE:
        return ["context:", '  rule: "{0}"'.format(DISABLED_RULE)]
    return ["context: {0}".format(context_value)]


def write_context(bundle_path, context_value):
    """Sets (or removes) the context: key, leaving every other line untouched.
    Returns (changed, detail)."""
    if not os.path.isfile(bundle_path):
        return False, "no bundle.yaml"
    with io.open(bundle_path, encoding="utf-8") as fh:
        original = fh.read()

    lines = original.splitlines()
    remaining, found_at = _strip_key(lines, "context")
    new_lines = context_lines(context_value)
    if new_lines:
        at = found_at if found_at is not None else len(remaining)
        remaining[at:at] = new_lines

    new_text = u"\n".join(remaining).rstrip() + u"\n"
    if new_text == original.rstrip() + u"\n":
        return False, "already correct"
    with io.open(bundle_path, "w", encoding="utf-8") as fh:
        fh.write(new_text)
    return True, ("greyed out" if context_value == DISABLED_RULE else
                  "restored to {0}".format(context_value or "no context"))


def apply_state(root):
    """Writes the disable rule onto every faded BUTTON and restores the
    original context on every live one. Containers are never written to - a
    container being off simply fades its buttons."""
    results = []

    def walk(node):
        if node.is_container or node.kind == "Tab":
            for child in node.children:
                walk(child)
            return
        wanted = node.original_context if effective_enabled(node) else DISABLED_RULE
        changed, detail = write_context(node.bundle_path, wanted)
        if changed:
            results.append((node.rel_key, detail))

    walk(root)
    return results


if __name__ == "__main__":
    import shutil
    import tempfile
    import unittest

    def make_tree(root):
        def mk(path, text):
            os.makedirs(path)
            with io.open(os.path.join(path, "bundle.yaml"), "w",
                         encoding="utf-8") as fh:
                fh.write(text)

        tab = os.path.join(root, "DeePack.tab")
        mk(tab, u'title: "DeePack"\nlayout:\n  - Cloud\n  - About\n')
        cloud = os.path.join(tab, "Cloud.panel")
        mk(cloud, u'title: "Cloud"\nlayout:\n  - Solo\n  - CloudStack\n')
        mk(os.path.join(cloud, "Solo.pushbutton"),
           u'title: Solo\ntooltip: a lone button\ncontext: zero-doc\n')
        stack = os.path.join(cloud, "CloudStack.stack")
        mk(stack, u'layout:\n  - Alpha\n  - Beta\n')
        mk(os.path.join(stack, "Alpha.pushbutton"), u'title: Alpha\ntooltip: first\n')
        mk(os.path.join(stack, "Beta.pushbutton"), u'title: Beta\ntooltip: second\n')
        about = os.path.join(tab, "About.panel")
        mk(about, u'title: "About"\nlayout:\n  - DeeControl\n')
        mk(os.path.join(about, "DeeControl.pushbutton"),
           u'title: DeeControl\ntooltip: this tool\ncontext: zero-doc\n')
        return tab

    class Base(unittest.TestCase):
        def setUp(self):
            self.tmp = tempfile.mkdtemp()
            self.tab = make_tree(self.tmp)
            self.cfg = os.path.join(self.tmp, "cfg.json")
            self.root = scan(self.tab)
            self.nodes = flatten(self.root)
            self.by = dict((n.name, n) for n in self.nodes)

        def tearDown(self):
            shutil.rmtree(self.tmp, ignore_errors=True)

        def meta_of(self, rel):
            return read_bundle_meta(os.path.join(self.tab, rel, "bundle.yaml"))

        def text_of(self, rel):
            with io.open(os.path.join(self.tab, rel, "bundle.yaml"),
                         encoding="utf-8") as fh:
                return fh.read()

    class ScanTests(Base):
        def test_finds_every_bundle(self):
            self.assertEqual(
                sorted(self.by.keys()),
                ["About", "Alpha", "Beta", "Cloud", "CloudStack", "DeeControl", "Solo"])

        def test_reads_existing_context(self):
            self.assertEqual(self.by["Solo"].original_context, "zero-doc")
            self.assertIsNone(self.by["Alpha"].original_context)

        def test_brief_comes_from_tooltip(self):
            self.assertEqual(self.by["Solo"].brief, "a lone button")

        def test_order_follows_layout(self):
            self.assertEqual([c.name for c in self.by["Cloud"].children],
                             ["Solo", "CloudStack"])

        def test_locking(self):
            self.assertTrue(self.by["DeeControl"].locked)
            self.assertFalse(self.by["DeeControl"].is_toggleable)
            self.assertTrue(self.by["Solo"].is_toggleable)

    class EffectiveTests(Base):
        def test_off_button_is_faded(self):
            self.by["Alpha"].enabled = False
            self.assertFalse(effective_enabled(self.by["Alpha"]))

        def test_panel_off_fades_its_descendants(self):
            self.by["Cloud"].enabled = False
            self.assertFalse(effective_enabled(self.by["Alpha"]))
            self.assertFalse(effective_enabled(self.by["Solo"]))

        def test_group_off_does_not_fade_a_sibling(self):
            self.by["CloudStack"].enabled = False
            self.assertFalse(effective_enabled(self.by["Alpha"]))
            self.assertTrue(effective_enabled(self.by["Solo"]))

        def test_all_children_off_does_not_fade_the_panel_itself(self):
            # Nothing is hidden any more, so a container with every child off
            # is still perfectly live itself.
            self.by["Alpha"].enabled = False
            self.by["Beta"].enabled = False
            self.assertTrue(effective_enabled(self.by["CloudStack"]))

        def test_locked_stays_live(self):
            self.by["About"].enabled = False
            self.by["DeeControl"].enabled = False
            self.assertTrue(effective_enabled(self.by["DeeControl"]))

    class ApplyTests(Base):
        def test_disabling_writes_the_contradiction_rule(self):
            self.by["Alpha"].enabled = False
            apply_state(self.root)
            self.assertEqual(self.meta_of("Cloud.panel/CloudStack.stack/Alpha.pushbutton")["context"],
                             DISABLED_RULE)

        def test_the_rule_is_a_contradiction(self):
            self.assertIn("!", DISABLED_RULE)
            token = DISABLED_RULE.split("&")[0]
            self.assertEqual(DISABLED_RULE, "{0}&!{1}".format(token, token))

        def test_layout_lists_are_never_touched(self):
            before = self.text_of("Cloud.panel")
            self.by["Alpha"].enabled = False
            self.by["Solo"].enabled = False
            apply_state(self.root)
            self.assertEqual(self.text_of("Cloud.panel"), before)

        def test_original_context_is_restored(self):
            self.by["Solo"].enabled = False
            apply_state(self.root)
            self.assertEqual(self.meta_of("Cloud.panel/Solo.pushbutton")["context"],
                             DISABLED_RULE)
            self.by["Solo"].enabled = True
            apply_state(self.root)
            self.assertEqual(self.meta_of("Cloud.panel/Solo.pushbutton")["context"],
                             "zero-doc")

        def test_button_with_no_context_gets_none_back(self):
            self.by["Alpha"].enabled = False
            apply_state(self.root)
            self.by["Alpha"].enabled = True
            apply_state(self.root)
            self.assertIsNone(self.meta_of("Cloud.panel/CloudStack.stack/Alpha.pushbutton")["context"])

        def test_other_keys_survive(self):
            self.by["Solo"].enabled = False
            apply_state(self.root)
            text = self.text_of("Cloud.panel/Solo.pushbutton")
            self.assertIn("title: Solo", text)
            self.assertIn("tooltip: a lone button", text)

        def test_panel_off_greys_children_not_the_panel_file(self):
            before = self.text_of("Cloud.panel")
            self.by["Cloud"].enabled = False
            apply_state(self.root)
            self.assertEqual(self.text_of("Cloud.panel"), before)
            self.assertEqual(self.meta_of("Cloud.panel/Solo.pushbutton")["context"],
                             DISABLED_RULE)

        def test_locked_button_never_greyed(self):
            self.by["DeeControl"].enabled = False
            apply_state(self.root)
            self.assertEqual(self.meta_of("About.panel/DeeControl.pushbutton")["context"],
                             "zero-doc")

    class ConfigTests(Base):
        def test_original_context_survives_a_reload(self):
            """The regression this config exists to prevent: once Solo is off,
            its file says DISABLED_RULE, so only the config knows it was
            zero-doc."""
            self.by["Solo"].enabled = False
            save_state(self.cfg, self.root, self.nodes)
            apply_state(self.root)

            fresh = scan(self.tab)
            nodes = flatten(fresh)
            by = dict((n.name, n) for n in nodes)
            self.assertFalse(by["Solo"].enabled)      # read back off from the file
            self.assertIsNone(by["Solo"].original_context)   # file cannot know it
            load_state(self.cfg, fresh, nodes)
            self.assertEqual(by["Solo"].original_context, "zero-doc")

            by["Solo"].enabled = True
            apply_state(fresh)
            self.assertEqual(self.meta_of("Cloud.panel/Solo.pushbutton")["context"],
                             "zero-doc")

        def test_disabled_set_round_trip(self):
            self.by["Alpha"].enabled = False
            save_state(self.cfg, self.root, self.nodes)
            fresh = scan(self.tab)
            nodes = flatten(fresh)
            load_state(self.cfg, fresh, nodes)
            by = dict((n.name, n) for n in nodes)
            self.assertFalse(by["Alpha"].enabled)
            self.assertTrue(by["Beta"].enabled)

        def test_missing_config_leaves_everything_on(self):
            fresh = scan(self.tab)
            nodes = flatten(fresh)
            self.assertFalse(load_state(os.path.join(self.tmp, "nope.json"),
                                        fresh, nodes))
            self.assertTrue(all(n.enabled for n in nodes))

    class MetaTests(Base):
        def test_reads_back_a_written_rule_block(self):
            path = os.path.join(self.tab, "Cloud.panel", "Solo.pushbutton",
                                "bundle.yaml")
            write_context(path, DISABLED_RULE)
            meta = read_bundle_meta(path)
            self.assertEqual(meta["context"], DISABLED_RULE)
            self.assertEqual(meta["title"], "Solo")
            self.assertEqual(meta["tooltip"], "a lone button")

        def test_tooltip_with_colons_survives(self):
            path = os.path.join(self.tab, "Cloud.panel", "Solo.pushbutton",
                                "bundle.yaml")
            with io.open(path, "w", encoding="utf-8") as fh:
                fh.write(u"title: Solo\ntooltip: does a: thing (e.g. this: that)\n")
            self.assertEqual(read_bundle_meta(path)["tooltip"],
                             "does a: thing (e.g. this: that)")

    class SummaryTests(Base):
        def test_counts(self):
            live, total, faded = summarize(self.nodes)
            self.assertEqual((live, total, faded), (4, 4, 0))

        def test_notes_only_flag_inherited_fading(self):
            self.by["Alpha"].enabled = False
            refresh_notes(self.nodes)
            # its own unticked box says it; no note, so no other row changed
            self.assertEqual(self.by["Alpha"].note, "")
            self.assertEqual(self.by["Beta"].note, "")

        def test_notes_name_the_blocking_container(self):
            self.by["Cloud"].enabled = False
            refresh_notes(self.nodes)
            self.assertIn("Cloud", self.by["Solo"].note)
            self.assertIn("Cloud", self.by["Alpha"].note)
            self.assertEqual(self.by["DeeControl"].note, "always on")

        def test_panel_off_counts_its_buttons_as_faded(self):
            self.by["Cloud"].enabled = False
            live, total, faded = summarize(self.nodes)
            self.assertEqual(total, 4)
            self.assertEqual(faded, 3)   # Solo, Alpha, Beta
            self.assertEqual(live, 1)    # DeeControl

    class FavoritesTests(Base):
        def test_containers_are_not_favoritable(self):
            self.assertFalse(self.by["Cloud"].is_favoritable)
            self.assertTrue(self.by["Solo"].is_favoritable)

        def test_save_then_load_roundtrip(self):
            path = os.path.join(self.tmp, "favs.json")
            self.by["Solo"].favorite = True
            self.by["Alpha"].favorite = True
            save_favorites(path, self.nodes)

            fresh_root = scan(self.tab)
            fresh_nodes = flatten(fresh_root)
            fresh_by = dict((n.name, n) for n in fresh_nodes)
            load_favorites(path, fresh_nodes)

            self.assertTrue(fresh_by["Solo"].favorite)
            self.assertTrue(fresh_by["Alpha"].favorite)
            self.assertFalse(fresh_by["Beta"].favorite)
            self.assertFalse(fresh_by["Cloud"].favorite)

        def test_containers_are_never_persisted_even_if_flagged(self):
            # Defensive: is_favoritable gates the UI, but save_favorites
            # itself must also never write a container's name, in case
            # something else ever sets .favorite on one directly.
            path = os.path.join(self.tmp, "favs.json")
            self.by["Cloud"].favorite = True
            save_favorites(path, self.nodes)
            with io.open(path, encoding="utf-8") as fh:
                data = json.loads(fh.read())
            self.assertNotIn("Cloud", data["favorites"])

        def test_load_missing_file_is_a_safe_noop(self):
            path = os.path.join(self.tmp, "does_not_exist.json")
            self.assertFalse(load_favorites(path, self.nodes))
            self.assertFalse(self.by["Solo"].favorite)

    unittest.main(verbosity=2)
