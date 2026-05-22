"""
Pydantic schemas shared across the yomotsusaka pipeline.

All models are immutable by default (frozen=True) to prevent accidental
mutation of vault-adjacent data structures.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _new_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class EntityKind(str, Enum):
    """Categories of private entities that may appear in documents."""
    PERSON = "PERSON"
    ORG = "ORG"
    LOCATION = "LOCATION"
    DATE = "DATE"
    ID_NUMBER = "ID_NUMBER"
    FINANCIAL = "FINANCIAL"
    HEALTH = "HEALTH"
    CUSTOM = "CUSTOM"


class BatchStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"


# ---------------------------------------------------------------------------
# Core schemas
# ---------------------------------------------------------------------------

class EntityRecord(BaseModel, frozen=True):
    """Agent-safe metadata for a single detected private entity."""
    model_config = ConfigDict(extra="forbid")
    entity_id: str = Field(default_factory=_new_id)
    kind: EntityKind
    redacted_key: str
    start_char: int
    end_char: int
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)


class PrivateDictEntry(BaseModel, frozen=True):
    """Mapping from a redacted key back to the original private value."""
    key: str
    original_value: str
    kind: EntityKind
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class DocumentManifest(BaseModel, frozen=True):
    """
    Agent-facing representation of a processed document.

    Contains only redacted text and metadata; no raw private values.
    """
    doc_id: str = Field(default_factory=_new_id)
    source_ref: str  # opaque handle — e.g. relative vault path hash
    redacted_text: str
    entities: list[EntityRecord] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    summary: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)


class ArtifactHandle(BaseModel, frozen=True):
    """
    Opaque reference returned to agents after a document is committed.

    Agents may use this handle to request restoration through the
    :mod:`restoration_api` module.
    """
    handle_id: str = Field(default_factory=_new_id)
    doc_id: str
    vault_path: str  # internal path; not exposed outside the boundary


class BatchState(BaseModel):
    """Mutable state object that travels through the batch queue."""
    batch_id: str = Field(default_factory=_new_id)
    status: BatchStatus = BatchStatus.PENDING
    doc_refs: list[str] = Field(default_factory=list)
    manifests: list[DocumentManifest] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None
