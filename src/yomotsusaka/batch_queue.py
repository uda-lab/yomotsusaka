"""
Batch queue — manages the lifecycle of a processing batch.

Documents move through PENDING → RUNNING → DONE / FAILED.
In the MVP this is an in-process queue; a durable task queue (e.g. Redis,
SQLite-backed) can be swapped in behind the same interface.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from yomotsusaka.schemas import BatchState, BatchStatus, DocumentManifest

logger = logging.getLogger(__name__)


class BatchQueue:
    """Simple in-memory batch processor."""

    def __init__(self) -> None:
        self._batches: dict[str, BatchState] = {}

    def submit(self, doc_refs: list[str]) -> BatchState:
        """Create a new batch and return its initial state."""
        batch = BatchState(doc_refs=doc_refs)
        self._batches[batch.batch_id] = batch
        logger.info("Submitted batch %s with %d docs", batch.batch_id, len(doc_refs))
        return batch

    def start(self, batch_id: str) -> None:
        batch = self._get(batch_id)
        batch.status = BatchStatus.RUNNING
        batch.started_at = datetime.now(timezone.utc)

    def complete(self, batch_id: str, manifests: list[DocumentManifest]) -> None:
        batch = self._get(batch_id)
        batch.manifests = manifests
        batch.status = BatchStatus.DONE
        batch.finished_at = datetime.now(timezone.utc)
        logger.info("Batch %s completed with %d manifests", batch_id, len(manifests))

    def fail(self, batch_id: str, error: str) -> None:
        batch = self._get(batch_id)
        batch.errors.append(error)
        batch.status = BatchStatus.FAILED
        batch.finished_at = datetime.now(timezone.utc)
        logger.error("Batch %s failed: %s", batch_id, error)

    def get(self, batch_id: str) -> BatchState:
        return self._get(batch_id)

    def _get(self, batch_id: str) -> BatchState:
        if batch_id not in self._batches:
            raise KeyError(f"Unknown batch: {batch_id}")
        return self._batches[batch_id]
