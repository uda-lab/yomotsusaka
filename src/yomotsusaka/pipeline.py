"""Private-side internal kernel. Ordinary agents should use ``yomotsusaka.boundary`` instead.

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
from typing import TYPE_CHECKING

from yomotsusaka.commit import commit
from yomotsusaka.redactor import Span, redact
from yomotsusaka.schemas import ArtifactHandle, DocumentManifest
from yomotsusaka.validator import Validator

if TYPE_CHECKING:
    # Imported only for type hints; the runtime path treats *proposer* as a
    # duck-typed object with a ``.propose(raw_text) -> list[Span]`` method.
    # Keeping this in a TYPE_CHECKING block ensures importing
    # :mod:`yomotsusaka.pipeline` (and transitively :mod:`yomotsusaka.boundary`)
    # does NOT pull :mod:`yomotsusaka.span_proposer` — and therefore does not
    # transitively pull :mod:`yomotsusaka.inference_backend` — into the
    # boundary's import graph. Enforced by
    # ``tests/test_boundary_private_isolation.py``.
    from yomotsusaka.span_proposer import SpanProposer

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
    *,
    proposer: "SpanProposer | None" = None,
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
        objects identifying private entities to redact. Mutually
        exclusive with *proposer*; exactly one of the two MUST supply
        spans for this call.
    vault_root:
        Root directory of the local vault used by
        :func:`~yomotsusaka.commit.commit`.
    proposer:
        Optional :class:`~yomotsusaka.span_proposer.SpanProposer` that
        derives candidate spans from *raw_text*. When supplied, *spans*
        MUST be empty and the proposer's output is used as the redaction
        input. The proposer call runs BEFORE redaction so any failure
        leaves the vault untouched. Mutually exclusive with *spans*.

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
        equals a path-traversal segment, or if the single-source-of-spans
        invariant is violated (both *spans* and *proposer* supplied, or
        neither).
    yomotsusaka.validator.ValidationError
        If the redacted manifest fails any MVP privacy check.  The vault
        is left untouched in this case (no artifacts are written).
    yomotsusaka.span_proposer.SpanProposerError
        Propagated from a misbehaving inference-backed proposer.
    yomotsusaka.inference_backend.InferenceBackendError
        Propagated unwrapped from the proposer's backend layer (LLM
        outages, timeouts, etc.). The vault is left untouched.
    """
    _validate_doc_id(doc_id)

    spans_supplied = bool(spans)
    proposer_supplied = proposer is not None
    if spans_supplied == proposer_supplied:
        # Either both supplied or both empty; either way the call lacks a
        # single, unambiguous source of spans.
        raise ValueError(
            "process_document requires exactly one of 'spans' (non-empty) "
            "or 'proposer'; got "
            f"spans={'non-empty' if spans_supplied else 'empty'}, "
            f"proposer={'set' if proposer_supplied else 'None'}"
        )

    if proposer_supplied:
        # NOTE: the proposer call runs BEFORE redact() so any failure
        # (InferenceBackendError, SpanProposerError, ValueError, etc.)
        # leaves the vault untouched for this doc_id. The proposer is
        # responsible for not logging or persisting raw_text.
        assert proposer is not None  # for mypy
        spans = proposer.propose(raw_text)

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
