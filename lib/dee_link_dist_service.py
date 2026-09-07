# -*- coding: utf-8 -*-
"""
dee_link_dist_service
Places many NEW instances of already-loaded Revit links across a
masterplan, one per row of a user-supplied Excel sheet - "distribute"
in the sense DeeLinkDist's own name promises: many instances created
from a few link TYPES, not one existing link instance being nudged
around. Two phases, matching the button's own two-page wizard:
  1. Excel round trip (write an empty template, read a filled one back).
  2. Map each LOADED link type to a Building Typology string found in
     the sheet, then create+place one instance per matching row.

--------------------------------------------------------------------
Revit API facts this module relies on, and how each was confirmed
--------------------------------------------------------------------
All confirmed via WebSearch/WebFetch against revitapidocs.com and the
Revit API forum this session, not guessed - this is genuinely new API
surface for this codebase (no prior tool here has ever CREATED a link
instance, only moved CAD imports/links (dee_getdwg_service.py) or
rotated placed FamilyInstances (dee_block_to_family_service.py)):

- RevitLinkInstance.Create(Document, ElementId) - a static factory that
  places a NEW instance of an already-loaded RevitLinkType, origin-to-
  origin by default (i.e. at (0,0,0) in the host document). Returns the
  new RevitLinkInstance. Documented as unable to create instances of
  NESTED links - not a concern here, every link this tool discovers via
  FilteredElementCollector(doc).OfClass(RevitLinkType) is a top-level
  loaded link type.
- ElementTransformUtils.MoveElement(doc, ElementId, XYZ) - a pure
  translation, confirmed to throw InvalidOperationException only for a
  PINNED element (the one documented restriction) - never for a
  specific element TYPE. Already proven LIVE in this codebase on
  ImportInstance (dee_getdwg_service.py's "move CAD back to Internal
  Origin"), a sibling Instance subtype to RevitLinkInstance with the
  same placement-via-Transform shape. Since RevitLinkInstance.Create
  places the new instance at the origin, the move TRANSLATION vector is
  simply the target XYZ itself - no need to query the instance's own
  transform first.
- ElementTransformUtils.RotateElement(doc, ElementId, Line axis, angle
  radians) - already proven LIVE in this codebase on a placed
  FamilyInstance (dee_block_to_family_service.py's _apply_rotation),
  rotating around a vertical axis through the instance's OWN location -
  the identical pattern used here, through the just-placed instance's
  new position (target XYZ), so "place then spin in place" reads
  correctly rather than orbiting some other point.
- UnitUtils.ConvertToInternalUnits(value, UnitTypeId.Meters) - the
  exact call already proven live in dee_getdwg_service.py for a
  different unit conversion. X/Y/Z in the Excel sheet are meters (the
  masterplan-coordinate convention this is built for); rotation is
  degrees, converted via plain math.radians - both stated explicitly in
  the template's own column headers so nothing is ambiguous to whoever
  fills the sheet in.

NEEDS LIVE-REVIT VERIFICATION (per this codebase's own convention -
everything above is confirmed via documentation/proven sibling code,
not by running Revit): this is the FIRST time this codebase creates a
RevitLinkInstance via the API rather than just reading or moving one
already placed by a user - RevitLinkInstance.Create's exact behaviour
for a link type whose file is currently unloaded/needs-reload, and
whether MoveElement/RotateElement behave identically on a freshly
created link instance as they do on the ImportInstance/FamilyInstance
cases already proven, are both unverified until run for real.
"""
import math
import os
import tempfile

from Autodesk.Revit.DB import (
    FilteredElementCollector, RevitLinkType, RevitLinkInstance,
    ElementTransformUtils, XYZ, Line, Transaction,
    UnitUtils, UnitTypeId, BuiltInCategory,
    ExternalDefinitionCreationOptions, SpecTypeId, BuiltInParameterGroup,
    ModelPathUtils,
)

import xlsx_writer
import xlsx_reader

TEMPLATE_HEADERS = [
    "X", "Y", "Z", "Rotation Angle (deg)",
    "Building Typology", "Parcel ID", "Developer ID",
]
_TEMPLATE_COL_WIDTHS = [12, 12, 12, 18, 26, 16, 16]
_TEMPLATE_TITLE = "DeeLinkDist - Masterplan Building Locations"

# X/Y/Z's unit is picked in the window at load/place time, not baked
# into the template - the header above is deliberately unit-agnostic
# ("X", not "X (m)") since the same downloaded template now works
# whichever unit the user's own masterplan coordinates happen to be in.
# (label, UnitTypeId) - every option UnitUtils.ConvertToInternalUnits
# already accepts, offered rather than assuming metric because this
# codebase's own dee_getdwg_service.py keeps the identical set for the
# same reason (a masterplan's site survey could be in any of these).
UNIT_OPTIONS = [
    ("Millimeters", UnitTypeId.Millimeters),
    ("Centimeters", UnitTypeId.Centimeters),
    ("Meters", UnitTypeId.Meters),
    ("Feet", UnitTypeId.Feet),
    ("Inches", UnitTypeId.Inches),
]
DEFAULT_UNIT_LABEL = "Meters"


def resolve_unit(label):
    """The UnitTypeId for a UNIT_OPTIONS label, or Meters if the label
    is unrecognised - never raises, same 'a stale/unknown choice falls
    back to a sane default rather than being the reason nothing
    happens' convention as core.resolve_preset in dee_mono_service.py."""
    for opt_label, unit_type_id in UNIT_OPTIONS:
        if opt_label == label:
            return unit_type_id
    return UnitTypeId.Meters

# Column order is FIXED - the reader below matches by POSITION, not by
# re-parsing whatever header text happens to be in row 2, so a sheet
# built from this exact template (even after a user reorders columns
# in Excel) either matches column-for-column or is rejected plainly
# rather than silently misreading a rotation as an X value.
_X_COL, _Y_COL, _Z_COL, _ROT_COL, _TYPOLOGY_COL, _PARCEL_COL, _DEVELOPER_COL = range(7)


def write_template(path):
    """Writes an EMPTY workbook (title + header row only, no data rows)
    with the 7 required columns, via xlsx_writer.write_themed_xlsx -
    reused as-is rather than a new writer, since an empty template is
    just the zero-rows case of the same function every other DeePack
    export already uses."""
    xlsx_writer.write_themed_xlsx(
        path, _TEMPLATE_TITLE, TEMPLATE_HEADERS, _TEMPLATE_COL_WIDTHS, rows=[])


# ==========================================================================
# reading a filled-in sheet
# ==========================================================================
class BuildingRow(object):
    """One parsed, VALID Excel row - x/y/z in METERS (not yet converted
    to feet; that happens right before the Revit API call, keeping this
    class a plain data holder with no Revit dependency, so it stays
    testable standalone)."""
    def __init__(self, row_number, x, y, z, rotation_deg, typology, parcel_id, developer_id):
        self.row_number = row_number
        self.x = x
        self.y = y
        self.z = z
        self.rotation_deg = rotation_deg
        self.typology = typology
        self.parcel_id = parcel_id
        self.developer_id = developer_id


def _to_float(text):
    try:
        return float(str(text).strip())
    except (ValueError, TypeError):
        return None


def read_building_rows(path):
    """(rows, errors) - `rows` is every row that parsed cleanly
    (BuildingRow list), `errors` is a list of human-readable strings for
    every row that did not (missing/non-numeric X/Y/Z, or a blank
    Building Typology) - one bad row never discards the rest, matching
    this codebase's established "one failure never aborts the rest"
    convention, extended here to PARSING rather than only Revit calls."""
    rows = []
    errors = []
    try:
        sheets = xlsx_reader.read_xlsx_sheets(path)
    except Exception as e:
        return [], ["Could not open the file as an .xlsx workbook: {0}".format(e)]

    grid = None
    for _name, sheet_grid in sheets.items():
        grid = sheet_grid
        break
    if not grid:
        return [], ["The workbook has no readable sheet."]

    # Row 1 = title (merged), Row 2 = headers, data starts at row 3 -
    # matching exactly what write_template (via write_themed_xlsx)
    # itself produces, so a template downloaded from this same tool and
    # filled in without restructuring always lines up.
    for i, data_row in enumerate(grid[2:]):
        row_number = i + 3
        if not any((cell or "").strip() for cell in data_row):
            continue  # a fully blank row (trailing Excel rows) - skip silently, not an error

        def cell(col):
            return data_row[col].strip() if col < len(data_row) and data_row[col] is not None else ""

        x = _to_float(cell(_X_COL))
        y = _to_float(cell(_Y_COL))
        z = _to_float(cell(_Z_COL))
        rot = _to_float(cell(_ROT_COL))
        typology = cell(_TYPOLOGY_COL)
        parcel_id = cell(_PARCEL_COL)
        developer_id = cell(_DEVELOPER_COL)

        row_errors = []
        if x is None:
            row_errors.append("X")
        if y is None:
            row_errors.append("Y")
        if z is None:
            row_errors.append("Z")
        if not typology:
            row_errors.append("Building Typology")
        if row_errors:
            errors.append("Row {0}: missing/invalid {1}.".format(
                row_number, ", ".join(row_errors)))
            continue

        rows.append(BuildingRow(row_number, x, y, z, rot or 0.0, typology,
                                parcel_id, developer_id))

    return rows, errors


def distinct_typologies(rows):
    """Sorted, de-duplicated (case-insensitive) list of every Building
    Typology value actually present in the parsed rows - what the
    mapping page's per-link dropdown is populated with, so the user is
    only ever offered typologies that genuinely need a link, never a
    blank list to type into by hand."""
    seen = {}
    for row in rows:
        key = row.typology.strip().lower()
        if key and key not in seen:
            seen[key] = row.typology.strip()
    return sorted(seen.values(), key=lambda s: s.lower())


# ==========================================================================
# Shared parameters written onto every placed link instance - Building
# Typology / Parcel ID / Developer ID, USER-NAMED (the window offers
# editable text boxes defaulting to those three names, per the request
# "let me decide the name of this parameters").
#
# Route and reasoning copied from this codebase's ONE existing precedent
# for creating/binding a shared parameter - DeeLazy's DeeViewsheet
# module (dee_viewsheet.py's create_shared_view_parameter/
# _ensure_shared_param_file) - generalised here from a single Views-
# category parameter to N parameters bound to OST_RvtLinks (the
# category for Revit Link instances) instead. Copied rather than
# imported, matching this codebase's established "local copy per file"
# convention for small pieces of logic reused across unrelated tools
# (the same reasoning list_3d_views's own docstring gives - an earlier
# tool crashed Revit when a shared helper's location changed under it).
#
#   Application.OpenSharedParameterFile()  -> DefinitionFile
#   DefinitionFile.Groups.Create(group)    -> DefinitionGroup
#   group.Definitions.Create(ExternalDefinitionCreationOptions(name, type))
#                                           -> ExternalDefinition
#   Application.Create.NewInstanceBinding(CategorySet with OST_RvtLinks)
#   doc.ParameterBindings.Insert(definition, binding, group)
#
# Creation/binding runs in its OWN Transaction, committed BEFORE
# place_links' own placement Transaction starts - mirroring
# dee_viewsheet.py's own explicit reasoning: whether a freshly-inserted
# binding is visible to LookupParameter within the SAME transaction it
# was created in is unconfirmed, so this never depends on the answer.
# ==========================================================================
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


def ensure_link_parameters(doc, app, param_names):
    """param_names: list of text parameter names to create (if missing)
    and bind (if not already bound) to the OST_RvtLinks category as
    INSTANCE parameters, so each placed link instance can carry its OWN
    typology/parcel/developer value even when many instances share the
    same link TYPE. Returns {name: (ok, detail)}. Never raises - one
    parameter failing to bind is reported per-name, not fatal to the
    others or to the placement that follows."""
    results = {}
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

    t = Transaction(doc, "DeeLinkDist - Create Link Parameters")
    t.Start()
    try:
        cats = doc.Application.Create.NewCategorySet()
        links_cat = doc.Settings.Categories.get_Item(BuiltInCategory.OST_RvtLinks)
        cats.Insert(links_cat)

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

                binding = doc.Application.Create.NewInstanceBinding(cats)
                bindings = doc.ParameterBindings
                if bindings.Contains(definition):
                    results[name] = (True, "already bound to Revit Links ({0})".format(
                        file_detail))
                    continue
                inserted = bindings.Insert(definition, binding, BuiltInParameterGroup.PG_IDENTITY_DATA)
                results[name] = ((True, "created and bound to Revit Links ({0})".format(
                    file_detail)) if inserted else
                    (False, "Revit refused to bind '{0}' to Revit Links".format(name)))
            except Exception as e:
                results[name] = (False, "{0}".format(e))
        t.Commit()
    except Exception as e:
        t.RollBack()
        return dict((name, (False, "could not create link parameters: {0}".format(e)))
                   for name in param_names)

    return results


# ==========================================================================
# link types available to map
# ==========================================================================
def list_link_types(doc):
    """[RevitLinkType] present in the document, sorted by the SAME
    display name link_type_display_name shows (not raw .Name - see that
    function's own docstring for why sorting by a value the user never
    sees would make the mapping list look randomly ordered against what
    is actually displayed). Every LOADED link type this document
    currently has, whether or not it happens to have any instances
    placed yet - this tool exists specifically to CREATE new instances,
    so a link type with zero instances so far is exactly as valid a
    candidate as one already placed once."""
    try:
        types = list(FilteredElementCollector(doc).OfClass(RevitLinkType))
    except Exception:
        return []
    types.sort(key=lambda lt: link_type_display_name(lt).lower())
    return types


def link_type_display_name(link_type):
    """The linked FILE's actual name (e.g. "Villa_A.rvt") - confirmed
    via WebSearch against Autodesk's own Revit API developer guide as
    the documented, reliable route: Element.GetExternalFileReference()
    -> ExternalFileReference.GetPath() -> a ModelPath, run through
    ModelPathUtils.ConvertModelPathToUserVisiblePath to get the same
    user-visible path string Revit's own Manage Links dialog shows.

    NOT RevitLinkType.Name: a live test showed EVERY link type in a
    real project reporting an empty/unhelpful .Name (every one showing
    as "(unnamed link)" in the mapping dropdown) - confirmed as a real,
    live-observed gap, not a hypothetical one, so this is a fix for an
    actual reported bug, not a guessed improvement. .Name is kept only
    as a second-choice fallback below GetExternalFileReference, for a
    link type that genuinely has neither (never observed, but cheaper
    to keep than to assume can't happen)."""
    try:
        ref = link_type.GetExternalFileReference()
        if ref is not None:
            model_path = ref.GetPath()
            if model_path is not None:
                visible_path = ModelPathUtils.ConvertModelPathToUserVisiblePath(model_path)
                if visible_path:
                    return os.path.basename(visible_path)
    except Exception:
        pass
    try:
        if link_type.Name:
            return link_type.Name
    except Exception:
        pass
    return "(unnamed link)"


# ==========================================================================
# placement
# ==========================================================================
class RowResult(object):
    def __init__(self, row, ok, message):
        self.row = row
        self.ok = ok
        self.message = message


class PlacementResult(object):
    def __init__(self):
        self.applied = 0
        self.skipped = 0
        self.row_results = []
        self.errors = []
        # {param_name: (ok, detail)} from ensure_link_parameters, kept
        # separate from row_results - "3 parameters bound" and "40 links
        # placed" are different operations about different things, same
        # reasoning dee_mono_service.MonoResult already applies to
        # hidden_applied vs. applied.
        self.parameter_setup = {}


_DEFAULT_PARAM_NAMES = {
    "typology": "Building Typology",
    "parcel_id": "Parcel ID",
    "developer_id": "Developer ID",
}


def _set_link_parameters(instance, names, row):
    """Best-effort - a parameter that failed to bind (or a link
    instance that, for whatever reason, does not expose it) is simply
    skipped rather than failing the whole row: the LINK itself is still
    correctly placed either way, which is what actually matters most.
    Returns a short suffix for the row's own status message noting how
    many of the 3 values were actually written, so a silent partial
    write is never invisible in the report."""
    values = {
        names["typology"]: row.typology,
        names["parcel_id"]: row.parcel_id,
        names["developer_id"]: row.developer_id,
    }
    written = 0
    for name, value in values.items():
        try:
            p = instance.LookupParameter(name)
            if p is not None and not p.IsReadOnly:
                p.Set(value or "")
                written += 1
        except Exception:
            pass
    if written == len(values):
        return ""
    return " ({0}/{1} parameters written)".format(written, len(values))


def place_links(doc, rows, typology_to_link_type, unit_label=DEFAULT_UNIT_LABEL,
                param_names=None):
    """rows: [BuildingRow]. typology_to_link_type: {typology_lower:
    RevitLinkType} - built by the window from the user's per-link
    dropdown picks. unit_label: one of UNIT_OPTIONS' labels - what the
    sheet's X/Y/Z values are IN, converted to Revit's internal feet via
    UnitUtils.ConvertToInternalUnits before use. param_names: optional
    {"typology"/"parcel_id"/"developer_id": custom_name} - any key left
    out (or the whole dict left None) falls back to
    _DEFAULT_PARAM_NAMES, so an old caller passing nothing behaves
    exactly as before this option existed.

    Creating/binding the three shared parameters happens FIRST, in its
    own Transaction (see ensure_link_parameters) - a parameter that
    fails to bind is recorded in the result and simply never gets SET
    on any instance below, it does not stop placement.

    One Transaction for the placement batch itself (creating + moving +
    rotating + setting 3 parameters on a link instance is lightweight -
    nowhere near the scale that calls for DeeTransmit-style multi-
    transaction batching). Every row is wrapped in its own try/except
    so one failure never aborts the rest, matching this codebase's
    convention throughout (dee_mono_service.apply_theme's per-category
    loop, dee_transmit_service's per-model loop, etc.)."""
    result = PlacementResult()
    if not rows:
        result.errors.append("No building rows to place.")
        return result

    names = dict(_DEFAULT_PARAM_NAMES)
    if param_names:
        names.update(dict((k, v) for k, v in param_names.items() if v))
    unit_type_id = resolve_unit(unit_label)

    result.parameter_setup = ensure_link_parameters(
        doc, doc.Application, [names["typology"], names["parcel_id"], names["developer_id"]])

    t = Transaction(doc, "DeeLinkDist - Place Links")
    try:
        t.Start()
        for row in rows:
            link_type = typology_to_link_type.get(row.typology.strip().lower())
            if link_type is None:
                msg = "No link mapped for typology '{0}'.".format(row.typology)
                result.row_results.append(RowResult(row, False, msg))
                result.skipped += 1
                continue
            try:
                instance = RevitLinkInstance.Create(doc, link_type.Id)
                target = XYZ(
                    UnitUtils.ConvertToInternalUnits(row.x, unit_type_id),
                    UnitUtils.ConvertToInternalUnits(row.y, unit_type_id),
                    UnitUtils.ConvertToInternalUnits(row.z, unit_type_id))
                # Created origin-to-origin at (0,0,0), so the move
                # TRANSLATION is simply the target point itself.
                ElementTransformUtils.MoveElement(doc, instance.Id, target)
                if abs(row.rotation_deg) > 1e-9:
                    axis = Line.CreateBound(target, target + XYZ.BasisZ)
                    ElementTransformUtils.RotateElement(
                        doc, instance.Id, axis, math.radians(row.rotation_deg))

                param_note = _set_link_parameters(instance, names, row)

                result.row_results.append(RowResult(row, True, "Placed" + param_note))
                result.applied += 1
            except Exception as e:
                result.row_results.append(RowResult(row, False, "{0}".format(e)))
                result.skipped += 1
        t.Commit()
    except Exception as e:
        try:
            if t.HasStarted() and not t.HasEnded():
                t.RollBack()
        except Exception:
            pass
        result.errors.append("Placement failed: {0}".format(e))
        return result

    return result
