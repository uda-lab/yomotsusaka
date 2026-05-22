"""BatchQueue lifecycle tests over the canonical fixture.

Per AGENTS.md, raw private values must not appear in agent-facing returns,
manifests, or test artifacts *except* inside private-dictionary assertions.
This module keeps the canonical raw strings as locally-scoped literals
inside the round-trip helper and asserts that job/batch state never
exposes those raw values (it carries only manifest ids and redacted text).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from yomotsusaka.batch_queue import BatchQueue
from yomotsusaka.pipeline import process_document
from yomotsusaka.redactor import Span
from yomotsusaka.schemas import (
    BatchStatus,
    DocumentManifest,
    EntityKind,
)


# Canonical raw text per the issue spec. Confined to this module's scope
# and only used to drive process_document; raw values must never reach
# batch state assertions below.
_CANONICAL_RAW_TEXT = "Alice Tan works at Acme Corp. Patient ID: 12345."
_CANONICAL_RAW_VALUES: tuple[str, ...] = ("Alice Tan", "Acme Corp", "12345")


@dataclass(frozen=True)
class _SpanSpec:
    start: int
    end: int
    kind: EntityKind


_CANONICAL_SPAN_SPECS: tuple[_SpanSpec, ...] = (
    _SpanSpec(start=0, end=9, kind=EntityKind.PERSON),
    _SpanSpec(start=19, end=28, kind=EntityKind.ORG),
    _SpanSpec(start=42, end=47, kind=EntityKind.ID_NUMBER),
)


def _canonical_spans() -> list[Span]:
    return [Span(start=s.start, end=s.end, kind=s.kind) for s in _CANONICAL_SPAN_SPECS]


def _process_canonical(tmp_path: Path, doc_id: str = "canonical-fixture-001") -> tuple[str, DocumentManifest]:
    """Drive the canonical fixture through the pipeline and read the manifest.

    Returns the manifest id (doc_id) and the loaded DocumentManifest so
    tests can assert the manifest is the redacted, agent-safe form.
    """
    vault_root = tmp_path / "vault"
    handle = process_document(
        doc_id=doc_id,
        raw_text=_CANONICAL_RAW_TEXT,
        spans=_canonical_spans(),
        vault_root=vault_root,
    )
    manifest_path = vault_root / "manifests" / f"{handle.doc_id}.json"
    manifest = DocumentManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    return handle.doc_id, manifest


def _assert_no_raw_values(blob: str) -> None:
    for raw in _CANONICAL_RAW_VALUES:
        assert raw not in blob, f"raw private value {raw!r} leaked into batch state"


# ---------------------------------------------------------------------------
# Lifecycle: PENDING -> RUNNING -> DONE
# ---------------------------------------------------------------------------

def test_submit_yields_pending_job(tmp_path: Path) -> None:
    manifest_id, _ = _process_canonical(tmp_path)
    queue = BatchQueue()

    batch = queue.submit([manifest_id])

    assert batch.status is BatchStatus.PENDING
    assert batch.doc_refs == [manifest_id]
    assert batch.manifests == []
    assert batch.started_at is None
    assert batch.finished_at is None


def test_start_transitions_pending_to_running(tmp_path: Path) -> None:
    manifest_id, _ = _process_canonical(tmp_path)
    queue = BatchQueue()
    batch = queue.submit([manifest_id])

    queue.start(batch.batch_id)

    current = queue.get(batch.batch_id)
    assert current.status is BatchStatus.RUNNING
    assert current.started_at is not None
    assert current.finished_at is None


def test_complete_transitions_running_to_done(tmp_path: Path) -> None:
    manifest_id, manifest = _process_canonical(tmp_path)
    queue = BatchQueue()
    batch = queue.submit([manifest_id])
    queue.start(batch.batch_id)

    queue.complete(batch.batch_id, [manifest])

    current = queue.get(batch.batch_id)
    assert current.status is BatchStatus.DONE
    assert current.finished_at is not None
    assert [m.doc_id for m in current.manifests] == [manifest_id]


# ---------------------------------------------------------------------------
# Lifecycle: PENDING -> RUNNING -> FAILED (fresh job)
# ---------------------------------------------------------------------------

def test_fail_transitions_running_to_failed(tmp_path: Path) -> None:
    manifest_id, _ = _process_canonical(tmp_path)
    queue = BatchQueue()
    batch = queue.submit([manifest_id])
    queue.start(batch.batch_id)

    queue.fail(batch.batch_id, "synthetic failure")

    current = queue.get(batch.batch_id)
    assert current.status is BatchStatus.FAILED
    assert current.errors == ["synthetic failure"]
    assert current.finished_at is not None


# ---------------------------------------------------------------------------
# Illegal transitions
# ---------------------------------------------------------------------------

def test_start_on_done_job_raises(tmp_path: Path) -> None:
    manifest_id, manifest = _process_canonical(tmp_path)
    queue = BatchQueue()
    batch = queue.submit([manifest_id])
    queue.start(batch.batch_id)
    queue.complete(batch.batch_id, [manifest])

    with pytest.raises(ValueError):
        queue.start(batch.batch_id)


def test_complete_on_pending_job_raises(tmp_path: Path) -> None:
    manifest_id, manifest = _process_canonical(tmp_path)
    queue = BatchQueue()
    batch = queue.submit([manifest_id])

    with pytest.raises(ValueError):
        queue.complete(batch.batch_id, [manifest])


def test_fail_on_pending_job_raises(tmp_path: Path) -> None:
    manifest_id, _ = _process_canonical(tmp_path)
    queue = BatchQueue()
    batch = queue.submit([manifest_id])

    with pytest.raises(ValueError):
        queue.fail(batch.batch_id, "premature failure")


def test_start_on_running_job_raises(tmp_path: Path) -> None:
    manifest_id, _ = _process_canonical(tmp_path)
    queue = BatchQueue()
    batch = queue.submit([manifest_id])
    queue.start(batch.batch_id)

    with pytest.raises(ValueError):
        queue.start(batch.batch_id)


# ---------------------------------------------------------------------------
# Privacy boundary: job state stores only manifest id / state, no raw values
# ---------------------------------------------------------------------------

def test_job_record_stores_only_manifest_id_no_raw_values(tmp_path: Path) -> None:
    manifest_id, manifest = _process_canonical(tmp_path)
    queue = BatchQueue()
    batch = queue.submit([manifest_id])

    # After submit: doc_refs holds the manifest id, not raw text.
    pending = queue.get(batch.batch_id)
    assert pending.doc_refs == [manifest_id]
    _assert_no_raw_values(pending.model_dump_json())

    queue.start(batch.batch_id)
    running = queue.get(batch.batch_id)
    _assert_no_raw_values(running.model_dump_json())

    queue.complete(batch.batch_id, [manifest])
    done = queue.get(batch.batch_id)

    # Manifest attached to the batch references the doc by id and carries
    # only the redacted form — no raw values leak through batch state.
    assert [m.doc_id for m in done.manifests] == [manifest_id]
    assert all(m.source_ref == manifest_id for m in done.manifests)
    serialized = done.model_dump_json()
    _assert_no_raw_values(serialized)

    # Sanity: serialized batch state is round-trippable JSON (not just str).
    json.loads(serialized)
