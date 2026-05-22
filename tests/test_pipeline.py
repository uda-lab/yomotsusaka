"""End-to-end pipeline integration test for the canonical fixture.

Per AGENTS.md, raw private values must not appear in agent-facing returns,
manifests, or test artifacts *except* inside private-dictionary assertions.
This module therefore keeps the canonical raw strings as locally-scoped
literals inside the round-trip test and uses them only when asserting
recovered private-dictionary contents.  Shared module-level fixtures
expose offsets, kinds, and placeholder prefixes only — never raw values.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from yomotsusaka.pipeline import process_document
from yomotsusaka.redactor import Span
from yomotsusaka.restoration_api import restore
from yomotsusaka.schemas import ArtifactHandle, EntityKind


@dataclass(frozen=True)
class _SpanSpec:
    start: int
    end: int
    kind: EntityKind
    placeholder_prefix: str  # e.g. "<PERSON_"; raw value lives elsewhere


# Offsets and kinds for the canonical fixture; no raw values here so the
# module-level constants do not carry private literals.
_CANONICAL_SPAN_SPECS: tuple[_SpanSpec, ...] = (
    _SpanSpec(start=0, end=9, kind=EntityKind.PERSON, placeholder_prefix="<PERSON_"),
    _SpanSpec(start=19, end=28, kind=EntityKind.ORG, placeholder_prefix="<ORG_"),
    _SpanSpec(start=42, end=47, kind=EntityKind.ID_NUMBER, placeholder_prefix="<ID_NUMBER_"),
)


def _canonical_spans() -> list[Span]:
    return [Span(start=s.start, end=s.end, kind=s.kind) for s in _CANONICAL_SPAN_SPECS]


def test_canonical_fixture_round_trip(tmp_path: Path) -> None:
    # Raw private literals are confined to this test's local scope and are
    # only consumed inside the private-dictionary assertion block below.
    raw_text = "Alice Tan works at Acme Corp. Patient ID: 12345."
    expected_private = {
        EntityKind.PERSON: "Alice Tan",
        EntityKind.ORG: "Acme Corp",
        EntityKind.ID_NUMBER: "12345",
    }

    vault_root = tmp_path / "vault"
    doc_id = "canonical-fixture-001"

    handle = process_document(
        doc_id=doc_id,
        raw_text=raw_text,
        spans=_canonical_spans(),
        vault_root=vault_root,
    )

    # The handle is a real one produced by commit (not a stand-in).
    assert isinstance(handle, ArtifactHandle)
    assert handle.doc_id == doc_id
    assert handle.vault_path.endswith(f"private/{doc_id}.json")

    # Inspect the committed manifest from disk.
    manifest_path = vault_root / "manifests" / f"{doc_id}.json"
    assert manifest_path.exists()
    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))

    redacted_text = manifest_data["redacted_text"]
    # Public-side assertion: each expected placeholder prefix appears, and
    # the redacted text differs from the raw input (length check avoids
    # naming any private literal in a public assertion).
    for spec in _CANONICAL_SPAN_SPECS:
        assert spec.placeholder_prefix in redacted_text
    assert redacted_text != raw_text
    assert len(manifest_data["entities"]) == len(_CANONICAL_SPAN_SPECS)

    # source_ref carries the opaque doc_id, never the raw text or file path.
    assert manifest_data["source_ref"] == doc_id
    assert manifest_data["doc_id"] == doc_id

    # ----- private-dictionary assertions (raw values permitted here) -----
    restored = restore(handle, vault_root=vault_root)
    recovered = {entry.kind: entry.original_value for entry in restored}
    assert recovered == expected_private

    by_key = {entry.key: entry.original_value for entry in restored}
    for entry in restored:
        # Placeholder routing: every restored key shows up in the redacted
        # text and resolves back to the expected raw value for its kind.
        assert entry.key in redacted_text
        assert by_key[entry.key] == expected_private[entry.kind]


@pytest.mark.parametrize(
    "bad_doc_id",
    [
        "../escape",
        "nested/path",
        "back\\slash",
        "..",
        ".",
        "",
        "a" * 129,
        "has space",
        "has\x00null",
        # Windows-reserved device names (case-insensitive, with/without ext).
        "CON",
        "nul",
        "PRN.json",
        "aux.txt",
        "COM1",
        "lpt9",
    ],
)
def test_process_document_rejects_unsafe_doc_id(
    tmp_path: Path, bad_doc_id: str
) -> None:
    """doc_id flows into vault paths; reject anything that could escape the
    vault or collide with a Windows reserved device name."""
    vault_root = tmp_path / "vault"

    with pytest.raises(ValueError):
        process_document(
            doc_id=bad_doc_id,
            raw_text="placeholder text with no private spans",
            spans=[],
            vault_root=vault_root,
        )

    # Nothing must have been written to the vault for a rejected doc_id.
    assert not (vault_root / "manifests").exists()
    assert not (vault_root / "private").exists()
