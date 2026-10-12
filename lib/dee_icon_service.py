# -*- coding: utf-8 -*-
"""
dee_icon_service
Backs DeeIcon (About) - brands the REAL file thumbnail Revit embeds in
every .rvt (the one shown in Windows Explorer AND Revit's own Recent
Files screen) with a Revit-version badge and an optional custom logo.

--------------------------------------------------------------------
Why this is NOT a raw file-format hack
--------------------------------------------------------------------
An earlier version of this tool directly rewrote Revit's own embedded
OLE-compound-file preview stream. That was abandoned: confirmed via
research that no one has ever published successfully WRITING that
stream (only reading it), its name is Revit-version-suffixed and
thinly documented, and Revit holds the .rvt file locked the entire
time it's open - a live rewrite could only ever happen after Close,
with real, hard-to-bound risk of corrupting the user's actual project
file for an undocumented operation. The user was asked and did not
consent to that risk.

Instead, this uses a confirmed, fully documented Revit API mechanism:
Revit's saved file thumbnail is a snapshot of a specific VIEW, and
which view is used is controlled by Document.GetDocumentPreviewSettings()
/ SetDocumentPreviewSettings() - DocumentPreviewSettings.PreviewViewId.
Point that at a small dedicated view containing the version badge +
logo, and Revit's own, completely unmodified native save process bakes
that view's image in as the file's real thumbnail. No binary file
access of any kind - everything here is standard, in-session Revit API
calls against the open Document, the same risk profile as any other
tool in this extension that creates a view or places an element.

NEEDS LIVE-REVIT VERIFICATION: the exact Get/Set calling convention for
DocumentPreviewSettings (this repo has never touched it before) - built
defensively (try/except) so a failure here can never raise past the
caller. ImageType/ImageInstance placement carries the same "needs a
live check" flag already noted where it was first used in this repo
(DeeAligner.pushbutton's _insert_image_on_sheet) - not a new risk
invented for DeeIcon.

--------------------------------------------------------------------
Why DocumentOpened AND DocumentSaved, not DocumentSaving
--------------------------------------------------------------------
DocumentPreviewSettings is PERSISTENT document state - once set, Revit
keeps using that view for every later native Save/Sync on its own, with
no further intervention needed per save. That's what actually makes
"automatic" achievable here: subscribing to DocumentSaving (which fires
BEFORE the write) was considered and dropped - Revit documented-ly
blocks changing the active view/starting new UI operations from inside
that event. DocumentOpened and DocumentSaved are both unrestricted,
POST-event hooks, safe to run a Transaction inside: DocumentOpened
brands a project the moment it's opened (best case), DocumentSaved
self-heals the same way if settings changed mid-session (a logo picked,
or the feature just turned on) without needing to reopen.

Same "run forever in the background, no click needed" architecture as
dee_prayer_service.py / dee_broadcast_service.py / dee_force_state.py -
module-level handlers, `-=` before `+=` so a pyRevit Reload can never
stack duplicate subscriptions (this module stays cached in sys.modules
for the life of the Revit process, which is what makes that work).

Never raises out of either handler - a failure here (no drafting-view
type in an oddly templated project, a missing logo file, an API call
that doesn't behave as expected) must never interrupt the open/save the
user just performed.
"""
import os

import deew_settings

_SETTINGS_TOOL_NAME = "dee_icon_settings"
DEFAULT_SETTINGS = {"enabled": False, "logo_path": "", "_applied_logo_path": ""}

_VIEW_NAME = "DeeIcon - Do Not Delete"


def load_settings():
    return deew_settings.load(_SETTINGS_TOOL_NAME, dict(DEFAULT_SETTINGS))


def save_settings(data):
    deew_settings.save(_SETTINGS_TOOL_NAME, data)


def _version_text(doc):
    try:
        return str(doc.Application.VersionNumber)
    except Exception:
        return ""


def _find_view_by_name(doc, name):
    from Autodesk.Revit.DB import FilteredElementCollector, View
    for v in FilteredElementCollector(doc).OfClass(View):
        try:
            if not v.IsTemplate and v.Name == name:
                return v
        except Exception:
            continue
    return None


def _drafting_view_family_type_id(doc):
    from Autodesk.Revit.DB import FilteredElementCollector, ViewFamilyType, ViewFamily
    for vft in FilteredElementCollector(doc).OfClass(ViewFamilyType):
        try:
            if vft.ViewFamily == ViewFamily.Drafting:
                return vft.Id
        except Exception:
            continue
    return None


def _ensure_branding_view(doc):
    from Autodesk.Revit.DB import ViewDrafting
    view = _find_view_by_name(doc, _VIEW_NAME)
    if view is not None:
        return view
    vft_id = _drafting_view_family_type_id(doc)
    if vft_id is None:
        raise Exception("No Drafting view type found in this project.")
    view = ViewDrafting.Create(doc, vft_id)
    try:
        view.Name = _VIEW_NAME
    except Exception:
        pass
    return view


def _find_text_note(doc, view):
    from Autodesk.Revit.DB import FilteredElementCollector, TextNote
    for ele in FilteredElementCollector(doc, view.Id).OfClass(TextNote):
        return ele
    return None


def _text_note_type_id(doc, view):
    from Autodesk.Revit.DB import FilteredElementCollector, TextNoteType
    existing = _find_text_note(doc, view)
    if existing is not None:
        return existing.GetTypeId()
    for tnt in FilteredElementCollector(doc).OfClass(TextNoteType):
        return tnt.Id
    return None


def _ensure_text_note(doc, view, text):
    """Matches the TextNoteType lookup/fallback already proven in
    DeeLazy.pushbutton/modules/dee_sselect.py: reuse a type already
    present in the view, else the first TextNoteType found anywhere in
    the project."""
    from Autodesk.Revit.DB import TextNote, XYZ
    existing = _find_text_note(doc, view)
    if existing is not None:
        try:
            if existing.Text != text:
                existing.Text = text
        except Exception:
            pass
        return existing
    type_id = _text_note_type_id(doc, view)
    if type_id is None:
        raise Exception("No text note type found in this project.")
    return TextNote.Create(doc, view.Id, XYZ(0, 0, 0), text, type_id)


def _find_image_instance(doc, view):
    from Autodesk.Revit.DB import FilteredElementCollector, ImageInstance
    for ele in FilteredElementCollector(doc, view.Id).OfClass(ImageInstance):
        return ele
    return None


def _ensure_logo_image(doc, view, logo_path):
    """Same ImageType/ImageInstance placement already built (and
    flagged there as needing a live Revit check - same flag applies
    here) in DeeAligner.pushbutton's _insert_image_on_sheet, just
    targeting a Drafting View instead of a ViewSheet. Replaces any
    previously placed logo rather than stacking duplicates."""
    from Autodesk.Revit.DB import (
        ImageType, ImageTypeOptions, ImageTypeSource, ImagePlacementOptions,
        BoxPlacement, XYZ)
    existing = _find_image_instance(doc, view)
    if existing is not None:
        try:
            doc.Delete(existing.Id)
        except Exception:
            pass
    if not logo_path or not os.path.isfile(logo_path):
        return None
    options = ImageTypeOptions(logo_path, False, ImageTypeSource.Import)
    image_type = ImageType.Create(doc, options)
    placement = ImagePlacementOptions(XYZ(3.0, 0, 0), BoxPlacement.Center)
    return ImageInstance.Create(doc, view, image_type.Id, placement)


def _current_preview_view_id(doc):
    try:
        return doc.GetDocumentPreviewSettings().PreviewViewId
    except Exception:
        return None


def _apply_preview_settings(doc, view):
    """Returns (True, None) or (False, error_text) - the caller surfaces
    error_text verbatim since this exact API call has never been used
    in this repo before and its calling convention needs live-Revit
    confirmation."""
    try:
        settings = doc.GetDocumentPreviewSettings()
    except Exception as e:
        return False, "GetDocumentPreviewSettings() - {0}".format(e)
    try:
        settings.PreviewViewId = view.Id
    except Exception as e:
        return False, "setting PreviewViewId - {0}".format(e)
    try:
        doc.SetDocumentPreviewSettings(settings)
    except Exception as e:
        return False, "SetDocumentPreviewSettings() - {0}".format(e)
    return True, None


def apply_branding(doc, settings=None):
    """Does the actual work: find-or-create the dedicated branding
    view, find-or-update its version-badge text, find-or-replace its
    logo image if the logo changed, then point Revit's own thumbnail
    setting at that view. Only opens a Transaction if something
    actually needs to change - never marks an already-correct document
    as modified. Returns (True, message) / (False, reason) - never
    raises, so both the automatic hooks and a manual "Apply Now" button
    can treat this the same way.

    Each step after the view itself is wrapped in its OWN try/except
    and the transaction is still committed even if one step fails - a
    failure placing the logo image (the single most novel, least-
    verified piece here) must never wipe out an otherwise-successful
    view + version badge by rolling back the whole transaction. Problems
    are collected and reported back in plain text instead of being
    silently swallowed, so a live failure is actually diagnosable from
    one run instead of a blind guess."""
    if settings is None:
        settings = load_settings()
    if not settings.get("enabled"):
        return False, "DeeIcon is turned off."
    try:
        if doc is None:
            return False, "No document."
        if doc.IsFamilyDocument:
            return False, "DeeIcon does not run inside the Family Editor."
        if doc.IsLinked:
            return False, "Linked document - skipped."

        version_text = _version_text(doc)
        logo_path = settings.get("logo_path") or ""
        if logo_path and not os.path.isfile(logo_path):
            logo_path = ""
        applied_logo = settings.get("_applied_logo_path") or ""

        existing_view = _find_view_by_name(doc, _VIEW_NAME)
        existing_text = _find_text_note(doc, existing_view) if existing_view is not None else None
        needs_view = existing_view is None
        needs_text = existing_text is None or getattr(existing_text, "Text", None) != version_text
        needs_logo = applied_logo != logo_path
        needs_preview = (existing_view is None
                          or _current_preview_view_id(doc) != existing_view.Id)

        if not (needs_view or needs_text or needs_logo or needs_preview):
            return True, "Already up to date - the thumbnail branding is already applied."

        problems = []
        from Autodesk.Revit.DB import Transaction
        t = Transaction(doc, "DeeIcon - Update Thumbnail Branding")
        t.Start()
        try:
            view = _ensure_branding_view(doc)
        except Exception as e:
            t.RollBack()
            return False, "Could not create the branding view - {0}".format(e)

        try:
            _ensure_text_note(doc, view, version_text)
        except Exception as e:
            problems.append("version badge text - {0}".format(e))

        if needs_logo:
            try:
                _ensure_logo_image(doc, view, logo_path)
                settings["_applied_logo_path"] = logo_path
                save_settings(settings)
            except Exception as e:
                problems.append("logo image - {0}".format(e))

        preview_ok, preview_error = _apply_preview_settings(doc, view)
        if not preview_ok:
            problems.append("pointing Revit's thumbnail setting at the view - {0}".format(preview_error))

        t.Commit()
        if problems:
            return False, "Partly applied - the view was created, but: " + "; ".join(problems)
        return True, "Applied - the file's thumbnail will show the update next time it's saved."
    except Exception as e:
        return False, str(e)


def _on_document_opened(sender, args):
    try:
        apply_branding(args.Document)
    except Exception:
        pass


def _on_document_saved(sender, args):
    try:
        apply_branding(args.Document)
    except Exception:
        pass


def start_watching(uiapp):
    """Call once from startup.py. `-=` before `+=` makes this safe to
    call again on a Reload without stacking duplicate subscriptions -
    see module docstring."""
    try:
        app = uiapp.Application
    except Exception:
        return
    try:
        app.DocumentOpened -= _on_document_opened
    except Exception:
        pass
    try:
        app.DocumentOpened += _on_document_opened
    except Exception:
        pass
    try:
        app.DocumentSaved -= _on_document_saved
    except Exception:
        pass
    try:
        app.DocumentSaved += _on_document_saved
    except Exception:
        pass
