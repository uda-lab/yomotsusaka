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

from pathlib import Path

from yomotsusaka.commit import commit
from yomotsusaka.redactor import Span, redact
from yomotsusaka.schemas import ArtifactHandle, DocumentManifest
from yomotsusaka.validator import Validator


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
        path or raw text in that field.
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
    """
    redacted_text, entities, private_dict = redact(raw_text, spans)

    manifest = DocumentManifest(
        doc_id=doc_id,
        source_ref=doc_id,
        redacted_text=redacted_text,
        entities=entities,
    )

    Validator().validate(manifest)

    return commit(manifest, private_dict, vault_root)
