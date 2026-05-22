"""
Deterministic span-based redactor.

The redactor receives a document and a list of (start, end, kind) spans —
either from a rule-based detector or from the inference backend — and
replaces each span with a stable, opaque key of the form ``<KIND_hex>``.

Private values are captured in :class:`~yomotsusaka.schemas.PrivateDictEntry`
objects which must be stored in the vault by the caller; they are *not*
persisted here.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from yomotsusaka.schemas import EntityKind, EntityRecord, PrivateDictEntry


@dataclass(frozen=True)
class Span:
    """A detected entity span within raw text."""
    start: int
    end: int
    kind: EntityKind


def _make_key(kind: EntityKind, value: str) -> str:
    """Return a deterministic, opaque redaction key for *value*."""
    digest = hashlib.sha256(value.encode()).hexdigest()[:8]
    return f"<{kind.value}_{digest}>"


def redact(
    text: str,
    spans: list[Span],
) -> tuple[str, list[EntityRecord], list[PrivateDictEntry]]:
    """
    Replace each span in *text* with an opaque key.

    Parameters
    ----------
    text:
        Raw document text.
    spans:
        Non-overlapping detected entity spans, sorted by start position.
        Overlapping or out-of-order spans are silently ignored.

    Returns
    -------
    redacted_text:
        The document with private spans replaced by keys.
    entity_records:
        :class:`~yomotsusaka.schemas.EntityRecord` objects describing each
        replacement (suitable for inclusion in a
        :class:`~yomotsusaka.schemas.DocumentManifest`).
    private_dict:
        :class:`~yomotsusaka.schemas.PrivateDictEntry` objects mapping each
        key back to the original value.  **Must be stored in the vault.**
    """
    # Sort and de-overlap spans
    sorted_spans = sorted(spans, key=lambda s: s.start)

    entity_records: list[EntityRecord] = []
    private_dict: list[PrivateDictEntry] = []
    parts: list[str] = []
    cursor = 0
    offset = 0  # tracks how redacted text length differs from original

    for span in sorted_spans:
        if span.start < cursor:
            # Overlapping span — skip
            continue
        if span.start > len(text) or span.end > len(text):
            continue

        original_value = text[span.start : span.end]
        key = _make_key(span.kind, original_value)

        # Accumulate non-private text before this span
        parts.append(text[cursor : span.start])
        parts.append(key)

        redacted_start = span.start + offset
        redacted_end = redacted_start + len(key)
        offset += len(key) - len(original_value)

        entity_records.append(
            EntityRecord(
                kind=span.kind,
                redacted_key=key,
                start_char=redacted_start,
                end_char=redacted_end,
            )
        )
        private_dict.append(
            PrivateDictEntry(
                key=key,
                original_value=original_value,
                kind=span.kind,
            )
        )
        cursor = span.end

    parts.append(text[cursor:])
    redacted_text = "".join(parts)
    return redacted_text, entity_records, private_dict
