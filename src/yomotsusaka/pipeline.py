"""
Pipeline — local orchestrator that walks raw text through redact → validate → commit.

This module wires the existing primitives (:mod:`yomotsusaka.redactor`,
:mod:`yomotsusaka.validator`, :mod:`yomotsusaka.commit`) into a single entry
point so callers can drive the canonical fixture end-to-end without
re-implementing orchestration glue.

The validator currently runs as a no-op stub; tightening its behaviour is
tracked under issue #9.
"""

from __future__ import annotations

import re
from pathlib import Path

from yomotsusaka.commit import commit
from yomotsusaka.redactor import Span, redact
from yomotsusaka.schemas import ArtifactHandle, DocumentManifest
from yomotsusaka.validator import Validator

# doc_id is interpolated into vault filesystem paths by commit()
# (`<vault_root>/manifests/<doc_id>.json` and `<vault_root>/private/<doc_id>.json`).
# Restrict it to an opaque, filesystem-safe charset so callers cannot smuggle
# path components ("..", "/", "\\", NUL) that would escape the vault subtree
# or break restoration via restoration_api.restore.
_DOC_ID_PATTERN = re.compile(r"\A[A-Za-z0-9._-]{1,128}\Z")


def _validate_doc_id(doc_id: str) -> None:
    if not isinstance(doc_id, str) or not _DOC_ID_PATTERN.fullmatch(doc_id):
        raise ValueError(
            "doc_id must match [A-Za-z0-9._-]{1,128}; "
            "path separators and traversal segments are not allowed"
        )
    if doc_id in {".", ".."}:
        raise ValueError("doc_id must not be a path traversal segment")


def process_document(
    doc_id: str,
    raw_text: str,
    spans: list[Span],
    vault_root: Path,
) -> ArtifactHandle:
    """
    Drive *raw_text* through redaction, validation, and commit.

    Parameters
    ----------
    doc_id:
        Caller-supplied document identifier.  Used as both the manifest
        ``doc_id`` and ``source_ref`` so the manifest carries no raw file
        path or raw text in that field.  Must match ``[A-Za-z0-9._-]{1,128}``
        and must not be ``.`` or ``..``; the value is interpolated into
        vault filesystem paths by :func:`~yomotsusaka.commit.commit`, so
        path separators and traversal segments are rejected up front.
    raw_text:
        Source document text.  Stays inside this function; only the
        redacted form is persisted to the manifest.
    spans:
        Pre-detected, non-overlapping :class:`~yomotsusaka.redactor.Span`
        objects identifying private entities to redact.
    vault_root:
        Root directory of the local vault used by
        :func:`~yomotsusaka.commit.commit`.

    Returns
    -------
    ArtifactHandle
        Opaque handle produced by :func:`~yomotsusaka.commit.commit`.
        Use :func:`~yomotsusaka.restoration_api.restore` to recover the
        private dictionary.

    Raises
    ------
    ValueError
        If ``doc_id`` contains characters outside the allowed charset or
        equals a path-traversal segment.
    """
    _validate_doc_id(doc_id)

    redacted_text, entities, private_dict = redact(raw_text, spans)

    manifest = DocumentManifest(
        doc_id=doc_id,
        source_ref=doc_id,
        redacted_text=redacted_text,
        entities=entities,
    )

    Validator().validate(manifest)

    return commit(manifest, private_dict, vault_root)
