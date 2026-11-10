# -*- coding: utf-8 -*-
"""
dee_param_service
All the Revit API work behind DeeParam (Health panel): scan every shared
and project parameter in the active model, compare them with the shared
parameter file, remove bindings, delete orphaned shared parameters,
extend a binding to more categories, and bind a parameter from the
shared parameter file. No UI in here - the window lives in the
pushbutton's script.py and only ever calls these functions.

--------------------------------------------------------------------
Where the pieces come from
--------------------------------------------------------------------
Discovery walks doc.ParameterBindings the same way DeeLazy's
dee_para_value.discover_all_parameter_values does, then ALSO collects
SharedParameterElement / ParameterElement directly, which no other tool
in this codebase did - that is the only way to see a shared parameter
that is loaded into the model but bound to nothing (an orphan, left
behind when a binding was removed or a family carrying it was purged).

Removal copies the names-only pattern from dee_transmit_service: an
earlier DeeTransmit build held ~200 live Definition objects across
~200 removals and Revit crashed natively, with no exception to catch
(journal.0166). So nothing in this module keeps a Definition,
SharedParameterElement or Binding across a loop that mutates the
model - every write re-fetches its target by NAME or GUID immediately
before touching it, and the scan returns plain Python dicts only.

Extending a binding reuses dee_shared_param_service.extend_binding_
categories (the ReInsert route - Insert fails for a parameter that was
EVER bound, Jeremy Tammik's "Adding a Category to a Shared Parameter").
Unlike ensure_shared_parameters, which only ever makes instance
bindings, DeeParam keeps whatever kind the binding already has: a Type
binding stays a Type binding.

Revit 2026: no BuiltInParameterGroup (GroupTypeId.IdentityData is the
default group), no ElementId.IntegerValue (dee_ui.eid_int everywhere).

--------------------------------------------------------------------
Shared or project? - decided from the model, not from the class name
--------------------------------------------------------------------
The Definition that ParameterBindings hands back is an
InternalDefinition for BOTH shared and project parameters in current
Revit versions, so isinstance(ExternalDefinition) alone would call
everything "Project". The reliable test is to resolve the definition's
Id to its ParameterElement: a SharedParameterElement means shared (and
carries the GUID); anything else is a project parameter. The
isinstance check is kept as a first, cheap path for the older shape.
"""
from dee_ui import eid_int
from Autodesk.Revit.DB import (
    FilteredElementCollector, Transaction, StorageType, TypeBinding,
    ExternalDefinition, InternalDefinition, SharedParameterElement,
    ParameterElement, GlobalParameter, GroupTypeId, LabelUtils, ElementId,
)
from System import Guid

DEFAULT_PROBE_LIMIT = 5000

KIND_SHARED = "Shared"
KIND_PROJECT = "Project"
KIND_SHARED_UNBOUND = "Shared (unbound)"
KIND_PROJECT_UNBOUND = "Project (unbound)"

FILE_IN = "In file"
FILE_MISSING = "Not in file"
FILE_NAME_DIFFERS = "Name differs"
FILE_NA = "N/A"
FILE_UNKNOWN = ""


# ==========================================================================
# small, never-raising readers
# ==========================================================================
def _guid_text(guid):
    try:
        return str(guid) if guid is not None else ""
    except Exception:
        return ""


def _label_for_group(group_type_id):
    try:
        return LabelUtils.GetLabelForGroup(group_type_id)
    except Exception:
        try:
            return group_type_id.TypeId.split(":")[-1].split("-")[0]
        except Exception:
            return ""


def _label_for_spec(spec_type_id):
    try:
        return LabelUtils.GetLabelForSpec(spec_type_id)
    except Exception:
        try:
            tail = spec_type_id.TypeId.split(":")[-1]
            return tail.split("-")[0]
        except Exception:
            return ""


def _definition_identity(doc, definition):
    """Returns (is_shared, guid_text, param_element_id_int). Resolves
    through the model's ParameterElement so a shared parameter is
    recognised whatever class the binding iterator hands back."""
    try:
        if isinstance(definition, ExternalDefinition):
            return True, _guid_text(definition.GUID), -1
    except Exception:
        pass
    try:
        if isinstance(definition, InternalDefinition):
            pe = doc.GetElement(definition.Id)
            if isinstance(pe, SharedParameterElement):
                return True, _guid_text(pe.GuidValue), eid_int(pe.Id)
            if pe is not None:
                return False, "", eid_int(pe.Id)
    except Exception:
        pass
    return False, "", -1


def _definition_group_text(definition):
    try:
        return _label_for_group(definition.GetGroupTypeId())
    except Exception:
        return ""


def _definition_datatype_text(definition):
    try:
        return _label_for_spec(definition.GetDataType())
    except Exception:
        return ""


def _category_names(binding):
    names = []
    try:
        for cat in binding.Categories:
            try:
                names.append(cat.Name)
            except Exception:
                continue
    except Exception:
        pass
    return sorted(names)


def _value_text(parameter):
    """Display text of a non-empty value, or "" when empty. Never raises."""
    try:
        if not parameter.HasValue:
            return ""
        st = parameter.StorageType
        if st == StorageType.String:
            return (parameter.AsString() or "").strip()
        if st == StorageType.Integer:
            v = parameter.AsInteger()
            text = parameter.AsValueString()
            return (text or str(v)) if v else ""
        if st == StorageType.Double:
            v = parameter.AsDouble()
            text = parameter.AsValueString()
            return (text or str(v)) if v else ""
        if st == StorageType.ElementId:
            eid = parameter.AsElementId()
            if eid is None or eid_int(eid) <= 0:
                return ""
            text = parameter.AsValueString()
            return text or str(eid_int(eid))
    except Exception:
        return ""
    return ""


def _get_parameter(element, is_shared, guid_text, name):
    try:
        if is_shared and guid_text:
            return element.get_Parameter(Guid(guid_text))
        return element.LookupParameter(name)
    except Exception:
        return None


def _probe_usage(doc, binding, is_shared, guid_text, name, probe_limit):
    """Returns (elements_total, probed, used, distinct_count,
    storage_text). Walks every element of every bound category, up to
    probe_limit elements in total - a parameter bound to Walls in a big
    model would otherwise cost a full-model walk per parameter."""
    is_type = isinstance(binding, TypeBinding)
    total = 0
    probed = 0
    used = 0
    values = set()
    storage_text = ""
    try:
        for cat in binding.Categories:
            try:
                collector = FilteredElementCollector(doc).OfCategoryId(cat.Id)
                collector = (collector.WhereElementIsElementType() if is_type
                             else collector.WhereElementIsNotElementType())
                for element in collector:
                    total += 1
                    if probed >= probe_limit:
                        continue
                    probed += 1
                    p = _get_parameter(element, is_shared, guid_text, name)
                    if p is None:
                        continue
                    if not storage_text:
                        try:
                            storage_text = str(p.StorageType)
                        except Exception:
                            pass
                    text = _value_text(p)
                    if text:
                        used += 1
                        if len(values) < 500:
                            values.add(text)
            except Exception:
                continue
    except Exception:
        pass
    return total, probed, used, len(values), storage_text


# ==========================================================================
# scan
# ==========================================================================
def _new_info(name):
    return {
        "name": name,
        "kind": "",
        "scope": "",
        "categories": [],
        "categories_text": "",
        "storage": "",
        "datatype": "",
        "group": "",
        "guid": "",
        "element_id": -1,
        "elements": 0,
        "probed": 0,
        "used": 0,
        "distinct": 0,
        "is_bound": False,
        "is_shared": False,
        "file_status": FILE_UNKNOWN,
        "file_name": "",
    }


def scan_parameters(doc, probe_limit=DEFAULT_PROBE_LIMIT, progress=None):
    """Every shared and project parameter in the model as a list of plain
    dicts (see _new_info for the keys), bound ones first, then the
    orphans. Never raises; a parameter that fails to read is skipped.
    progress(i, total, name) is called per bound parameter when given."""
    if doc is None:
        return []
    infos = []
    seen_element_ids = set()
    seen_guids = set()

    names = []
    try:
        it = doc.ParameterBindings.ForwardIterator()
        it.Reset()
        while it.MoveNext():
            try:
                if it.Key is not None and it.Key.Name:
                    names.append(it.Key.Name)
            except Exception:
                continue
    except Exception:
        pass

    for i, name in enumerate(names):
        if progress is not None:
            try:
                progress(i, len(names), name)
            except Exception:
                pass
        definition, binding = _find_binding(doc, name)
        if definition is None:
            continue
        try:
            info = _new_info(name)
            is_shared, guid_text, pe_id = _definition_identity(doc, definition)
            info["is_bound"] = True
            info["is_shared"] = is_shared
            info["kind"] = KIND_SHARED if is_shared else KIND_PROJECT
            info["scope"] = "Type" if isinstance(binding, TypeBinding) else "Instance"
            info["categories"] = _category_names(binding)
            info["categories_text"] = ", ".join(info["categories"]) or "(none)"
            info["group"] = _definition_group_text(definition)
            info["datatype"] = _definition_datatype_text(definition)
            info["guid"] = guid_text
            info["element_id"] = pe_id
            total, probed, used, distinct, storage = _probe_usage(
                doc, binding, is_shared, guid_text, name, probe_limit)
            info["elements"] = total
            info["probed"] = probed
            info["used"] = used
            info["distinct"] = distinct
            info["storage"] = storage
            infos.append(info)
            if pe_id > 0:
                seen_element_ids.add(pe_id)
            if guid_text:
                seen_guids.add(guid_text.lower())
        except Exception:
            continue
        definition = None
        binding = None

    # orphans: in the model, bound to nothing
    try:
        for pe in FilteredElementCollector(doc).OfClass(ParameterElement):
            try:
                if isinstance(pe, GlobalParameter):
                    continue
                pe_id = eid_int(pe.Id)
                if pe_id in seen_element_ids:
                    continue
                is_shared = isinstance(pe, SharedParameterElement)
                guid_text = _guid_text(pe.GuidValue) if is_shared else ""
                if guid_text and guid_text.lower() in seen_guids:
                    continue
                definition = pe.GetDefinition()
                name = pe.Name
                try:
                    if definition is not None and definition.Name:
                        name = definition.Name
                except Exception:
                    pass
                if not is_shared:
                    # Built-in / system definitions also appear as
                    # ParameterElements; only user project parameters
                    # (no BuiltInParameter) are worth listing.
                    try:
                        if definition is not None and \
                                str(definition.BuiltInParameter) != "INVALID":
                            continue
                    except Exception:
                        pass
                info = _new_info(name)
                info["is_bound"] = False
                info["is_shared"] = is_shared
                info["kind"] = KIND_SHARED_UNBOUND if is_shared else KIND_PROJECT_UNBOUND
                info["scope"] = ""
                info["categories_text"] = "(not bound)"
                info["group"] = _definition_group_text(definition) if definition else ""
                info["datatype"] = _definition_datatype_text(definition) if definition else ""
                info["guid"] = guid_text
                info["element_id"] = pe_id
                infos.append(info)
            except Exception:
                continue
    except Exception:
        pass

    return infos


def _find_binding(doc, name):
    """A FRESH (definition, binding) for `name`, or (None, None)."""
    try:
        it = doc.ParameterBindings.ForwardIterator()
        it.Reset()
        while it.MoveNext():
            try:
                definition = it.Key
                if definition is not None and definition.Name == name:
                    return definition, it.Current
            except Exception:
                continue
    except Exception:
        pass
    return None, None


# ==========================================================================
# shared parameter file
# ==========================================================================
def shared_param_file_path(app):
    try:
        return app.SharedParametersFilename or ""
    except Exception:
        return ""


def read_shared_param_file(app):
    """Returns (defs, detail) where defs is {guid_lower: {"name", "group",
    "datatype", "guid"}} or None when no file is set / it will not open."""
    try:
        def_file = app.OpenSharedParameterFile()
    except Exception as e:
        return None, "could not open the shared parameter file: {0}".format(e)
    if def_file is None:
        return None, "no shared parameter file is set (Manage > Shared Parameters)"
    defs = {}
    try:
        for group in def_file.Groups:
            try:
                group_name = group.Name
            except Exception:
                group_name = ""
            try:
                for d in group.Definitions:
                    try:
                        guid_text = _guid_text(d.GUID)
                        defs[guid_text.lower()] = {
                            "name": d.Name,
                            "group": group_name,
                            "datatype": _definition_datatype_text(d),
                            "guid": guid_text,
                        }
                    except Exception:
                        continue
            except Exception:
                continue
    except Exception as e:
        return None, "could not read the shared parameter file: {0}".format(e)
    return defs, "{0} definition(s) in {1}".format(len(defs), shared_param_file_path(app))


def compare_to_file(infos, file_defs):
    """Fills file_status / file_name on every info in place."""
    for info in infos:
        if not info.get("is_shared"):
            info["file_status"] = FILE_NA
            info["file_name"] = ""
            continue
        if file_defs is None:
            info["file_status"] = FILE_UNKNOWN
            continue
        entry = file_defs.get((info.get("guid") or "").lower())
        if entry is None:
            info["file_status"] = FILE_MISSING
            info["file_name"] = ""
        elif entry["name"] != info["name"]:
            info["file_status"] = FILE_NAME_DIFFERS
            info["file_name"] = entry["name"]
        else:
            info["file_status"] = FILE_IN
            info["file_name"] = entry["name"]
    return infos


def file_only_definitions(infos, file_defs):
    """Definitions present in the shared parameter file but not in the
    model at all (neither bound nor orphaned) - candidates for 'Bind
    from file'. Returns a list of file entries."""
    if not file_defs:
        return []
    model_guids = set((i.get("guid") or "").lower() for i in infos if i.get("guid"))
    return [entry for guid, entry in sorted(file_defs.items(), key=lambda kv: kv[1]["name"].lower())
            if guid not in model_guids]


# ==========================================================================
# categories
# ==========================================================================
def bindable_categories(doc):
    """Sorted list of (name, id_int) for every category a parameter can
    be bound to."""
    out = []
    try:
        for cat in doc.Settings.Categories:
            try:
                if cat.AllowsBoundParameters:
                    out.append((cat.Name, eid_int(cat.Id)))
            except Exception:
                continue
    except Exception:
        pass
    return sorted(out, key=lambda t: t[0].lower())


def _category_by_id(doc, id_int):
    try:
        for cat in doc.Settings.Categories:
            try:
                if eid_int(cat.Id) == id_int:
                    return cat
            except Exception:
                continue
    except Exception:
        pass
    return None


# ==========================================================================
# writes - every one dry-run aware, names/GUIDs only, fresh lookups
# ==========================================================================
def _start(doc, name):
    t = Transaction(doc, name)
    t.Start()
    return t


def _finish(t, ok):
    try:
        if t is None:
            return
        if ok and t.HasStarted() and not t.HasEnded():
            t.Commit()
        elif t.HasStarted() and not t.HasEnded():
            t.RollBack()
    except Exception:
        pass


def remove_bindings(doc, names, dry_run=True):
    """Removes the project/shared parameter binding for each name.
    Returns [(name, ok, detail)]. dry_run only reports what would go."""
    results = []
    if not names:
        return results
    if dry_run:
        for name in names:
            definition, binding = _find_binding(doc, name)
            if definition is None:
                results.append((name, False, "no binding with this name"))
            else:
                results.append((name, True, "would remove the binding ({0})".format(
                    ", ".join(_category_names(binding)) or "no categories")))
            definition = None
            binding = None
        return results

    t = None
    try:
        t = _start(doc, "DeeParam - Remove Parameter Bindings")
        for name in names:
            definition, _binding = _find_binding(doc, name)
            _binding = None
            if definition is None:
                results.append((name, False, "no binding with this name"))
                continue
            try:
                removed = doc.ParameterBindings.Remove(definition)
                results.append((name, bool(removed),
                                "binding removed" if removed else "Revit refused to remove it"))
            except Exception as e:
                results.append((name, False, "{0}".format(e)))
            definition = None
        _finish(t, True)
    except Exception as e:
        _finish(t, False)
        results.append(("(transaction)", False, "rolled back: {0}".format(e)))
    return results


def delete_orphaned_shared(doc, guids, dry_run=True):
    """Deletes the SharedParameterElement for each GUID, each one looked
    up fresh right before its own delete. Returns [(guid, ok, detail)]."""
    results = []
    if not guids:
        return results

    def lookup(guid_text):
        try:
            return SharedParameterElement.Lookup(doc, Guid(guid_text))
        except Exception:
            return None

    if dry_run:
        for guid_text in guids:
            pe = lookup(guid_text)
            if pe is None:
                results.append((guid_text, False, "not found in the model"))
            else:
                try:
                    name = pe.Name
                except Exception:
                    name = guid_text
                results.append((guid_text, True, "would delete '{0}'".format(name)))
            pe = None
        return results

    t = None
    try:
        t = _start(doc, "DeeParam - Delete Orphaned Shared Parameters")
        for guid_text in guids:
            pe = lookup(guid_text)
            if pe is None:
                results.append((guid_text, False, "not found in the model"))
                continue
            try:
                name = pe.Name
            except Exception:
                name = guid_text
            try:
                deleted = doc.Delete(pe.Id)
                count = 0
                try:
                    count = deleted.Count
                except Exception:
                    count = 1 if deleted else 0
                results.append((guid_text, count > 0,
                                "deleted '{0}'".format(name) if count > 0
                                else "Revit deleted nothing for '{0}'".format(name)))
            except Exception as e:
                results.append((guid_text, False, "'{0}': {1}".format(name, e)))
            pe = None
        _finish(t, True)
    except Exception as e:
        _finish(t, False)
        results.append(("(transaction)", False, "rolled back: {0}".format(e)))
    return results


def extend_bindings(doc, names, category_ids, dry_run=True):
    """Extends each named parameter's EXISTING binding to also cover the
    categories in category_ids (ints), keeping its Instance/Type kind.
    Returns [(name, ok, detail)]."""
    import dee_shared_param_service as sps
    results = []
    if not names or not category_ids:
        return results
    cats = [c for c in (_category_by_id(doc, i) for i in category_ids) if c is not None]
    if not cats:
        return [(n, False, "none of the chosen categories could be resolved") for n in names]
    cat_label = ", ".join(c.Name for c in cats)

    if dry_run:
        for name in names:
            definition, binding = _find_binding(doc, name)
            if definition is None:
                results.append((name, False, "no binding with this name"))
            else:
                have = set(_category_names(binding))
                new = [c.Name for c in cats if c.Name not in have]
                results.append((name, bool(new),
                                "would add {0}".format(", ".join(new)) if new
                                else "already covers {0}".format(cat_label)))
            definition = None
            binding = None
        return results

    t = None
    try:
        t = _start(doc, "DeeParam - Extend Parameter Bindings")
        for name in names:
            definition, binding = _find_binding(doc, name)
            if definition is None:
                results.append((name, False, "no binding with this name"))
                continue
            try:
                ok, detail = sps.extend_binding_categories(doc, definition, binding, cats)
                results.append((name, ok, detail))
            except Exception as e:
                results.append((name, False, "{0}".format(e)))
            definition = None
            binding = None
        _finish(t, True)
    except Exception as e:
        _finish(t, False)
        results.append(("(transaction)", False, "rolled back: {0}".format(e)))
    return results


def _file_definition(app, guid_text):
    """The ExternalDefinition with this GUID from the shared parameter
    file, fetched fresh. None when not found."""
    try:
        def_file = app.OpenSharedParameterFile()
        if def_file is None:
            return None
        target = guid_text.lower()
        for group in def_file.Groups:
            for d in group.Definitions:
                try:
                    if _guid_text(d.GUID).lower() == target:
                        return d
                except Exception:
                    continue
    except Exception:
        pass
    return None


def bind_from_file(doc, app, guids, category_ids, as_type=False, dry_run=True):
    """Binds each shared parameter (by GUID, from the shared parameter
    file) to the given categories as Instance (default) or Type. A
    parameter already bound is extended instead (ReInsert). Returns
    [(guid, ok, detail)]."""
    import dee_shared_param_service as sps
    results = []
    if not guids or not category_ids:
        return results
    cats = [c for c in (_category_by_id(doc, i) for i in category_ids) if c is not None]
    if not cats:
        return [(g, False, "none of the chosen categories could be resolved") for g in guids]
    cat_label = ", ".join(c.Name for c in cats)
    kind_label = "Type" if as_type else "Instance"

    if dry_run:
        for guid_text in guids:
            d = _file_definition(app, guid_text)
            if d is None:
                results.append((guid_text, False, "not in the shared parameter file"))
            else:
                results.append((guid_text, True, "would bind '{0}' to {1} as {2}".format(
                    d.Name, cat_label, kind_label)))
            d = None
        return results

    t = None
    try:
        t = _start(doc, "DeeParam - Bind Shared Parameters from File")
        for guid_text in guids:
            d = _file_definition(app, guid_text)
            if d is None:
                results.append((guid_text, False, "not in the shared parameter file"))
                continue
            try:
                name = d.Name
                bindings = doc.ParameterBindings
                if bindings.Contains(d):
                    existing_def, existing_binding = _find_binding(doc, name)
                    ok, detail = sps.extend_binding_categories(
                        doc, existing_def if existing_def is not None else d,
                        existing_binding, cats)
                    existing_def = None
                    existing_binding = None
                    results.append((guid_text, ok, "'{0}': {1}".format(name, detail)))
                    continue
                cat_set = app.Create.NewCategorySet()
                for c in cats:
                    cat_set.Insert(c)
                binding = (app.Create.NewTypeBinding(cat_set) if as_type
                           else app.Create.NewInstanceBinding(cat_set))
                inserted = bindings.Insert(d, binding, GroupTypeId.IdentityData)
                results.append((guid_text, bool(inserted),
                                "bound '{0}' to {1} as {2}".format(name, cat_label, kind_label)
                                if inserted else "Revit refused to bind '{0}'".format(name)))
            except Exception as e:
                results.append((guid_text, False, "{0}".format(e)))
            d = None
        _finish(t, True)
    except Exception as e:
        _finish(t, False)
        results.append(("(transaction)", False, "rolled back: {0}".format(e)))
    return results


# ==========================================================================
# export
# ==========================================================================
EXPORT_HEADERS = ["Name", "Kind", "Scope", "Categories", "Storage", "Data Type",
                  "Group", "GUID", "Elements", "Probed", "Used", "Distinct Values",
                  "File Status", "Name in File"]
EXPORT_WIDTHS = [34, 18, 10, 48, 12, 16, 20, 40, 10, 10, 10, 14, 14, 30]


def export_xlsx(path, infos, title="DeeParam - Parameter Audit"):
    from xlsx_writer import write_themed_xlsx
    rows = []
    for i in infos:
        status = None
        if not i.get("is_bound"):
            status = "skip"
        elif i.get("file_status") in (FILE_MISSING, FILE_NAME_DIFFERS):
            status = "fail"
        elif i.get("is_bound") and i.get("used", 0) == 0:
            status = "skip"
        rows.append(([i.get("name", ""), i.get("kind", ""), i.get("scope", ""),
                      i.get("categories_text", ""), i.get("storage", ""),
                      i.get("datatype", ""), i.get("group", ""), i.get("guid", ""),
                      i.get("elements", 0), i.get("probed", 0), i.get("used", 0),
                      i.get("distinct", 0), i.get("file_status", ""),
                      i.get("file_name", "")], status))
    write_themed_xlsx(path, title, EXPORT_HEADERS, EXPORT_WIDTHS, rows)
    return len(rows)
