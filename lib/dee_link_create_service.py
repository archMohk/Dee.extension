# -*- coding: utf-8 -*-
"""
dee_link_create_service
The one Revit API sequence shared by every tool that creates a NEW link
(RevitLinkType + RevitLinkInstance): DeeSuperLINK, DeeLINK, and DeeMAPLink
each had their own independent, near-identical copy of this -
lib/dee_maplink_service.py's own docstring already admits its version was
"ported from DeeSuperLINK's _link_once". Extracted here the moment one
feature (Attachment vs Overlay) would otherwise mean the same edit
repeated three times.

Deliberately does NOT own the Transaction - each of the three existing
callers already wraps its own (with its own naming and, for two of the
three, an attached deew_failure_handler preprocessor and retry/fallback
logic that differs between them), so this only does the part that is
genuinely identical: create the link type, set its AttachmentType, create
one instance of it. The caller's own transaction/rollback handling is
unchanged.
"""
from Autodesk.Revit.DB import RevitLinkType, RevitLinkOptions, RevitLinkInstance, AttachmentType


def create_link(doc, model_path, placement, attachment=AttachmentType.Overlay):
    """Must be called from inside an already-started Transaction on doc.
    Returns (ok, detail, instance_or_None) - never raises, matching every
    existing caller's own contract, so a caller's own except/rollback
    logic around this needs no change.

    attachment defaults to Overlay - Revit's own default for a new link,
    and the safer choice (an Attachment link propagates into further
    nested links if the host is itself linked into a third model, which
    should be something a user opts into, not a silent behavior change
    for tools that worked one way before this parameter existed)."""
    try:
        result = RevitLinkType.Create(doc, model_path, RevitLinkOptions(False))
    except Exception as e:
        return False, str(e), None

    try:
        bad = result.ElementId.Value < 0
    except Exception:
        try:
            bad = result.ElementId.IntegerValue < 0
        except Exception:
            bad = False
    if bad:
        return False, "link type could not be created", None

    try:
        result.AttachmentType = attachment
    except Exception:
        # Not fatal - the link still gets created, it just keeps whatever
        # Revit's own default AttachmentType is instead of the one
        # requested. A cosmetic/organizational property is not worth
        # failing the whole link over.
        pass

    try:
        instance = RevitLinkInstance.Create(doc, result.ElementId, placement)
    except Exception as e:
        return False, str(e), None

    return True, "linked", instance
