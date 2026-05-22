"""Tests for Pydantic schemas."""

import pytest
from pydantic import ValidationError

from yomotsusaka.schemas import (
    ArtifactHandle,
    BatchState,
    BatchStatus,
    DocumentManifest,
    EntityKind,
    EntityRecord,
    PrivateDictEntry,
)


# ---------------------------------------------------------------------------
# EntityRecord
# ---------------------------------------------------------------------------

def test_entity_record_valid():
    rec = EntityRecord(
        kind=EntityKind.PERSON,
        redacted_key="<PERSON_abc12345>",
        start_char=0,
        end_char=5,
    )
    assert rec.kind == EntityKind.PERSON
    assert rec.confidence == 1.0


def test_entity_record_confidence_out_of_range():
    with pytest.raises(ValidationError):
        EntityRecord(
            kind=EntityKind.PERSON,
            redacted_key="<PERSON_abc12345>",
            start_char=0,
            end_char=5,
            confidence=1.5,
        )


def test_entity_record_is_immutable():
    rec = EntityRecord(
        kind=EntityKind.PERSON,
        redacted_key="<PERSON_abc12345>",
        start_char=0,
        end_char=5,
    )
    with pytest.raises(Exception):  # frozen model raises on assignment
        rec.redacted_key = "<PERSON_other>"  # type: ignore[misc]


def test_entity_record_rejects_original_value_field():
    with pytest.raises(ValidationError):
        EntityRecord(
            kind=EntityKind.PERSON,
            original_value="Alice",
            redacted_key="<PERSON_abc12345>",
            start_char=0,
            end_char=5,
        )


# ---------------------------------------------------------------------------
# PrivateDictEntry
# ---------------------------------------------------------------------------

def test_private_dict_entry_valid():
    entry = PrivateDictEntry(
        key="<PERSON_abc12345>",
        original_value="Alice",
        kind=EntityKind.PERSON,
    )
    assert entry.original_value == "Alice"


# ---------------------------------------------------------------------------
# DocumentManifest
# ---------------------------------------------------------------------------

def test_document_manifest_defaults():
    manifest = DocumentManifest(
        source_ref="sha256:deadbeef",
        redacted_text="Hello, <PERSON_abc12345>.",
    )
    assert manifest.entities == []
    assert manifest.labels == []
    assert manifest.summary == ""
    assert manifest.doc_id  # auto-generated


def test_document_manifest_with_entities():
    rec = EntityRecord(
        kind=EntityKind.PERSON,
        redacted_key="<PERSON_abc12345>",
        start_char=7,
        end_char=24,
    )
    manifest = DocumentManifest(
        source_ref="sha256:deadbeef",
        redacted_text="Hello, <PERSON_abc12345>.",
        entities=[rec],
        labels=["hr", "confidential"],
        summary="A greeting addressed to a redacted person.",
    )
    assert len(manifest.entities) == 1
    assert "hr" in manifest.labels


# ---------------------------------------------------------------------------
# ArtifactHandle
# ---------------------------------------------------------------------------

def test_artifact_handle_fields():
    handle = ArtifactHandle(doc_id="doc-001", vault_path=".vault/private/doc-001.json")
    assert handle.doc_id == "doc-001"
    assert handle.handle_id  # auto-generated UUID


# ---------------------------------------------------------------------------
# BatchState
# ---------------------------------------------------------------------------

def test_batch_state_initial():
    state = BatchState(doc_refs=["a.txt", "b.txt"])
    assert state.status == BatchStatus.PENDING
    assert state.batch_id
    assert state.started_at is None


def test_batch_state_mutable():
    state = BatchState(doc_refs=["a.txt"])
    state.status = BatchStatus.RUNNING  # BatchState is NOT frozen
    assert state.status == BatchStatus.RUNNING
