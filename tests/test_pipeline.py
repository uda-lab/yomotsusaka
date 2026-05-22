"""End-to-end pipeline integration test for the canonical fixture."""

from __future__ import annotations

import json
from pathlib import Path

from yomotsusaka.pipeline import process_document
from yomotsusaka.redactor import Span
from yomotsusaka.restoration_api import restore
from yomotsusaka.schemas import ArtifactHandle, EntityKind

CANONICAL_TEXT = "Alice Tan works at Acme Corp. Patient ID: 12345."
CANONICAL_SPANS = [
    Span(start=0, end=9, kind=EntityKind.PERSON),     # "Alice Tan"
    Span(start=19, end=28, kind=EntityKind.ORG),      # "Acme Corp"
    Span(start=42, end=47, kind=EntityKind.ID_NUMBER),  # "12345"
]
CANONICAL_ORIGINALS = {
    EntityKind.PERSON: "Alice Tan",
    EntityKind.ORG: "Acme Corp",
    EntityKind.ID_NUMBER: "12345",
}


def test_canonical_fixture_round_trip(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    doc_id = "canonical-fixture-001"

    handle = process_document(
        doc_id=doc_id,
        raw_text=CANONICAL_TEXT,
        spans=CANONICAL_SPANS,
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
    for original in CANONICAL_ORIGINALS.values():
        assert original not in redacted_text

    assert "<PERSON_" in redacted_text
    assert "<ORG_" in redacted_text
    assert "<ID_NUMBER_" in redacted_text

    # source_ref carries the opaque doc_id, never the raw text or file path.
    assert manifest_data["source_ref"] == doc_id
    assert manifest_data["doc_id"] == doc_id

    # Restoration recovers all three originals keyed by placeholder.
    restored = restore(handle, vault_root=vault_root)
    recovered = {entry.kind: entry.original_value for entry in restored}
    assert recovered == CANONICAL_ORIGINALS

    # Each placeholder in the redacted text maps to its original via the
    # restored private dictionary.
    by_key = {entry.key: entry.original_value for entry in restored}
    for entry in restored:
        assert entry.key in redacted_text
        assert by_key[entry.key] == CANONICAL_ORIGINALS[entry.kind]
