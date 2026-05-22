"""
Pipeline — local orchestrator that walks raw text through redact → validate → commit.

This module wires the existing primitives (:mod:`yomotsusaka.redactor`,
:mod:`yomotsusaka.validator`, :mod:`yomotsusaka.commit`) into a single entry
point so callers can drive the canonical fixture end-to-end without
re-implementing orchestration glue.

The validator enforces the MVP privacy invariants; on failure a
:class:`~yomotsusaka.validator.ValidationError` propagates out and no
public/private artifacts are written for the offending document.
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

# Windows reserved device names (case-insensitive, with or without extension).
# Writing to e.g. ``private/NUL.json`` on Windows opens the NUL device instead
# of a regular file, so reject these up front for cross-platform safety.
# Reference: https://learn.microsoft.com/en-us/windows/win32/fileio/naming-a-file
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def _validate_doc_id(doc_id: str) -> None:
    if not isinstance(doc_id, str) or not _DOC_ID_PATTERN.fullmatch(doc_id):
        raise ValueError(
            "doc_id must match [A-Za-z0-9._-]{1,128}; "
            "path separators and traversal segments are not allowed"
        )
    if doc_id in {".", ".."}:
        raise ValueError("doc_id must not be a path traversal segment")
    # Windows-reserved device names apply to the basename (the portion before
    # the first dot), case-insensitively, with or without an extension.
    stem = doc_id.split(".", 1)[0].upper()
    if stem in _WINDOWS_RESERVED_NAMES:
        raise ValueError(
            f"doc_id stem {stem!r} collides with a Windows reserved device name; "
            "choose a different identifier"
        )


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
    yomotsusaka.validator.ValidationError
        If the redacted manifest fails any MVP privacy check.  The vault
        is left untouched in this case (no artifacts are written).
    """
    _validate_doc_id(doc_id)

    redacted_text, entities, private_dict = redact(raw_text, spans)

    manifest = DocumentManifest(
        doc_id=doc_id,
        source_ref=doc_id,
        redacted_text=redacted_text,
        entities=entities,
    )

    # Run before commit so a ValidationError leaves the vault untouched.
    Validator().validate(manifest, private_dict)

    return commit(manifest, private_dict, vault_root)
