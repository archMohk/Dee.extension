# -*- coding: utf-8 -*-
"""
dee_shared_param_service
Generic "create (if missing) and bind (if not already bound) a set of
TEXT Shared Parameters to a category, as instance parameters" - extracted
into its own lib/ module the moment a SECOND tool needed it (DeeSheetLinks
writing the SAME parameter names DeeLinkDist already bound to Revit
Links, now onto Views too), rather than duplicated a second time. Unlike
this codebase's usual "local copy per file" convention (born from
importing across DIFFERENT PUSHBUTTON folders, which pyRevit does NOT
put on sys.path), this lives in lib/ alongside xlsx_writer.py/
dee_branding.py/utils.py - lib/ IS on sys.path for every pushbutton, so
importing it here carries none of that risk.

Route and reasoning originally from DeeLazy's DeeViewsheet module
(dee_viewsheet.py's create_shared_view_parameter/_ensure_shared_param_file),
generalised first by DeeLinkDist (N parameters, OST_RvtLinks, user-named)
and now moved here and generalised again - any category, and EXTENDING
an already-bound parameter to also cover a SECOND category instead of
skipping it:

  Application.OpenSharedParameterFile()  -> DefinitionFile
  DefinitionFile.Groups.Create(group)    -> DefinitionGroup
  group.Definitions.Create(ExternalDefinitionCreationOptions(name, type))
                                          -> ExternalDefinition
  Application.Create.NewInstanceBinding(CategorySet)
  doc.ParameterBindings.Insert(definition, binding, group)   - first bind
  doc.ParameterBindings.ReInsert(definition, binding, group) - EXTEND an
      existing binding to also cover a category it did not before

Insert() unconditionally fails once a parameter has EVER been bound at
all (confirmed via the documented "Adding a Category to a Shared
Parameter" pattern - Jeremy Tammik / Autodesk AEC blog: "if param has
EVER been bound in a project...Insert always fails!? Must use .ReInsert
in that case"). So extending which categories a parameter covers means
reading the EXISTING binding's CategorySet via
DefinitionBindingMap.ForwardIterator(), inserting the new category into
a COPY of it, and calling ReInsert with the enlarged set - a second
Insert would raise.

Runs in its own Transaction, committed before the caller's own
placement/write Transaction starts - mirrors dee_viewsheet.py's own
explicit reasoning: whether a freshly-inserted/extended binding is
visible to LookupParameter within the SAME transaction is unconfirmed,
so nothing here ever depends on the answer.

NEEDS LIVE-REVIT VERIFICATION: the "brand new parameter, first category"
path is exactly DeeLinkDist's own already-live-confirmed code, just
moved here unchanged. The ReInsert "extend an existing binding to a
SECOND category" path (first exercised by DeeSheetLinks, giving Views
the same parameter DeeLinkDist already bound to Revit Links) is new
ground - never run live before.
"""
import os
import tempfile

from Autodesk.Revit.DB import (
    Transaction, ExternalDefinitionCreationOptions, SpecTypeId,
    BuiltInParameterGroup,
)

_SP_GROUP_NAME = "DeePack"


def _ensure_shared_param_file(app, doc):
    """Returns (DefinitionFile, detail). Uses the existing shared
    parameter file when one is already set; otherwise creates a new one
    next to the project file (or in the user's temp folder for an
    unsaved project) rather than writing to an arbitrary location. An
    existing shared parameter file is never overwritten or replaced."""
    try:
        existing = app.OpenSharedParameterFile()
        if existing is not None:
            return existing, "using your existing shared parameter file"
    except Exception:
        pass

    try:
        base = ""
        try:
            if doc.PathName:
                base = os.path.dirname(doc.PathName)
        except Exception:
            base = ""
        if not base or not os.path.isdir(base):
            base = tempfile.gettempdir()
        path = os.path.join(base, "DeePack_SharedParameters.txt")
        if not os.path.exists(path):
            with open(path, "w") as fh:
                fh.write("")
        app.SharedParametersFilename = path
        created = app.OpenSharedParameterFile()
        if created is None:
            return None, "Revit would not open the new shared parameter file at {0}".format(path)
        return created, "created a new shared parameter file at {0}".format(path)
    except Exception as e:
        return None, "could not prepare a shared parameter file: {0}".format(e)


def _same_category(cat_a, cat_b):
    try:
        return cat_a.Id.IntegerValue == cat_b.Id.IntegerValue
    except Exception:
        return cat_a == cat_b


def ensure_shared_parameters(doc, app, param_names, category_bic, category_label,
                              transaction_name="DeePack - Create/Extend Shared Parameters"):
    """param_names: iterable of text parameter names to create (if
    missing) and bind (if not already bound) - or EXTEND to also cover,
    if already bound to a DIFFERENT category - as INSTANCE parameters on
    the category named by category_bic (a BuiltInCategory). category_label
    is only used for the human-readable detail strings (kept separate
    from the Revit-reported Category.Name, which is not guaranteed to
    read naturally in a sentence - e.g. the Revit Links category's own
    display name is "RVT Links", not "Revit Links").

    Returns {name: (ok, detail)}. Never raises - one parameter failing
    is reported per-name, never fatal to the others or to whatever the
    caller does next with the result."""
    def_file, file_detail = _ensure_shared_param_file(app, doc)
    if def_file is None:
        return dict((name, (False, file_detail)) for name in param_names)

    try:
        group = None
        for g in def_file.Groups:
            if g.Name == _SP_GROUP_NAME:
                group = g
                break
        if group is None:
            group = def_file.Groups.Create(_SP_GROUP_NAME)
    except Exception as e:
        detail = "could not open/create the '{0}' definition group: {1}".format(
            _SP_GROUP_NAME, e)
        return dict((name, (False, detail)) for name in param_names)

    results = {}
    t = Transaction(doc, transaction_name)
    t.Start()
    try:
        target_cat = doc.Settings.Categories.get_Item(category_bic)

        for name in param_names:
            try:
                definition = None
                for d in group.Definitions:
                    if d.Name == name:
                        definition = d
                        break
                if definition is None:
                    opts = ExternalDefinitionCreationOptions(name, SpecTypeId.String.Text)
                    definition = group.Definitions.Create(opts)

                bindings = doc.ParameterBindings
                existing_binding = None
                if bindings.Contains(definition):
                    it = bindings.ForwardIterator()
                    while it.MoveNext():
                        if it.Key.Name == name:
                            existing_binding = it.Current
                            break

                if existing_binding is None:
                    cats = doc.Application.Create.NewCategorySet()
                    cats.Insert(target_cat)
                    binding = doc.Application.Create.NewInstanceBinding(cats)
                    inserted = bindings.Insert(definition, binding, BuiltInParameterGroup.PG_IDENTITY_DATA)
                    results[name] = ((True, "created and bound to {0} ({1})".format(
                        category_label, file_detail)) if inserted else
                        (False, "Revit refused to bind '{0}' to {1}".format(name, category_label)))
                    continue

                already_has_category = any(
                    _same_category(c, target_cat) for c in existing_binding.Categories)
                if already_has_category:
                    results[name] = (True, "already bound to {0} ({1})".format(
                        category_label, file_detail))
                    continue

                cats = doc.Application.Create.NewCategorySet()
                for c in existing_binding.Categories:
                    cats.Insert(c)
                cats.Insert(target_cat)
                new_binding = doc.Application.Create.NewInstanceBinding(cats)
                reinserted = bindings.ReInsert(definition, new_binding, BuiltInParameterGroup.PG_IDENTITY_DATA)
                results[name] = ((True, "extended the existing binding to also cover {0} ({1})".format(
                    category_label, file_detail)) if reinserted else
                    (False, "Revit refused to extend '{0}' to also cover {1}".format(name, category_label)))
            except Exception as e:
                results[name] = (False, "{0}".format(e))
        t.Commit()
    except Exception as e:
        t.RollBack()
        return dict((name, (False, "could not create/bind parameters: {0}".format(e)))
                   for name in param_names)

    return results
