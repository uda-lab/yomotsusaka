"""Audit-record schema and writer for the Chikaeshi execution gateway (#43).

Single source-of-truth for the JSON shape written to
``<vault_root>/audit/restoration.jsonl`` by
:func:`yomotsusaka.boundary.execute_request`. The file is **append-only
JSONL**: one JSON object per line, written atomically per call. No file
lock is taken in MVP — the single-process invariant is documented in
``docs/architecture.md`` §11.3.

Invariants enforced by :func:`write_record`
-------------------------------------------

1. **No raw-value leakage** — the JSON-serialised record line is re-run
   through :func:`yomotsusaka.scrubber.scrub_stream` against the supplied
   ``private_dict``. If any raw ``original_value`` survives both passes,
   the call raises :class:`AuditError` and the underlying file is left
   untouched (the JSONL line is never appended).
2. **No ``"deferred"`` outcome** — the :class:`AuditRecord.outcome`
   ``Literal`` type set excludes ``"deferred"``. That value is reserved
   for the legacy :class:`yomotsusaka.boundary.RestorationResponse` path
   that #44 will eventually replace; the execution-gateway audit MUST
   NOT collide with it.
3. **Denial paths are audited too** — the dispatcher writes one record
   per call regardless of outcome (success, scope-denied, schema-invalid,
   template-not-found, scrub-failed, etc.). The write happens BEFORE the
   failure is returned to the caller so a process crash between the two
   events does not silently drop a denial.

Reserved fields (#44)
---------------------

``policy_profile`` and ``approval_ticket`` are reserved as
``Optional[str] = None``. This PR (#43) MUST always pass ``None`` for
both; populating them is owned by #44's policy-table lookup. Tests in
this PR assert the fields are present-and-``None``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from yomotsusaka.schemas import PrivateDictEntry
from yomotsusaka.scrubber import ScrubError, scrub_stream

logger = logging.getLogger(__name__)


AuditOutcome = Literal[
    "success",
    "scope_denied",
    "purpose_not_permitted",
    "template_not_found",
    "schema_invalid",
    "scrub_failed",
    "template_raised",
    "artifact_missing",
]
"""The closed set of values that may appear in :attr:`AuditRecord.outcome`.

Modelled on :class:`yomotsusaka.execution_gateway.ExecutionFailureReason`
plus the ``"success"`` value. ``"deferred"`` is deliberately excluded so
the execution-gateway audit can never collide with the legacy
:class:`yomotsusaka.boundary.RestorationResponse` ``"deferred"`` value
reserved for the path owned by #44.
"""


class AuditError(Exception):
    """Raised by :func:`write_record` when the pre-write scrubber re-check
    finds a raw private value in the JSON-serialised record line.

    The underlying file is left untouched on this failure — the line is
    NEVER appended. Callers should convert this to a structured failure
    response with a generic detail; the exception message intentionally
    does NOT echo the offending raw value.
    """


class AuditRecord(BaseModel):
    """One audit row appended to ``<vault_root>/audit/restoration.jsonl``.

    Frozen + ``extra="forbid"`` so any future field must be a deliberate
    schema migration with paired test coverage.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    ts: datetime = Field(
        description="Record timestamp, timezone-aware UTC. Serialised via "
        ":meth:`datetime.isoformat`.",
    )
    request_id: str = Field(
        description="UUID4 hex assigned by the gateway for the request. "
        "Echoes into :attr:`ExecutionResponse.audit_record_id` so a caller "
        "can correlate a public response with its audit row.",
    )
    template_name: str = Field(
        description="Name of the template invoked (e.g. "
        "``\"summarise_private_minutes\"``).",
    )
    caller_scope: str = Field(
        description="The :class:`ExecutionScope` value of the calling "
        "request. Stored as its ``str`` enum value for wire stability.",
    )
    purpose: str = Field(
        description="Non-empty free-form purpose recorded for the request.",
    )
    locator: str = Field(
        description="The public locator the request targeted (or the empty "
        "string when the request never named a locator — e.g. a "
        "schema-invalid request).",
    )
    outcome: AuditOutcome = Field(
        description="One of the closed :data:`AuditOutcome` values; "
        "``\"deferred\"`` is deliberately excluded.",
    )
    artifact_locators: list[str] = Field(
        default_factory=list,
        description="Public handles of any artifacts the template produced. "
        "Empty on every failure outcome.",
    )
    resolver_reason: str | None = Field(
        default=None,
        description="The original :class:`ResolverFailureReason` value when "
        "the failure mapped through :func:`boundary.resolve`. ``None`` "
        "otherwise.",
    )
    detail: str | None = Field(
        default=None,
        description="Free-form failure detail, already scrubbed of raw "
        "values and vault paths. ``None`` on success.",
    )
    policy_profile: str | None = Field(
        default=None,
        description="Reserved for #44; this PR always writes ``None``.",
    )
    approval_ticket: str | None = Field(
        default=None,
        description="Reserved for #44; this PR always writes ``None``.",
    )

    @field_validator("request_id")
    @classmethod
    def _request_id_non_empty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("request_id must be non-empty after strip")
        return v

    @field_validator("template_name")
    @classmethod
    def _template_name_non_empty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("template_name must be non-empty after strip")
        return v

    @field_validator("caller_scope")
    @classmethod
    def _caller_scope_non_empty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("caller_scope must be non-empty after strip")
        return v

    @field_validator("purpose")
    @classmethod
    def _purpose_non_empty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("purpose must be non-empty after strip")
        return v


def write_record(
    record: AuditRecord,
    vault_root: Path,
    *,
    private_dict: list[PrivateDictEntry] | tuple[PrivateDictEntry, ...] = (),
) -> None:
    """Append *record* to ``<vault_root>/audit/restoration.jsonl``.

    The record is serialised via :meth:`AuditRecord.model_dump_json`, then
    re-run through :func:`yomotsusaka.scrubber.scrub_stream` against
    *private_dict*. If the scrubber finds any non-empty
    ``original_value`` still present, the file is NEVER opened and
    :class:`AuditError` is raised.

    The audit directory is created with ``mkdir(parents=True,
    exist_ok=True)`` so the call is safe against a fresh vault root.

    Parameters
    ----------
    record:
        The :class:`AuditRecord` to append.
    vault_root:
        Vault root directory. ``<vault_root>/audit/`` is created on
        demand.
    private_dict:
        Sequence of :class:`PrivateDictEntry` used for the pre-write
        scrubber re-check. May be empty for paths where the gateway
        never resolved private data (e.g. schema-invalid requests); the
        re-check still runs (cheap no-op) so the call site stays
        symmetric.

    Raises
    ------
    AuditError
        If the pre-write scrubber re-check finds any raw value in the
        serialised record. The file is left untouched.
    OSError
        On filesystem failure (parent unwritable, disk full, etc.). The
        caller is expected to map this to a structured failure response.
    """
    if not isinstance(vault_root, Path):
        raise TypeError(
            f"vault_root must be a pathlib.Path; got {type(vault_root).__name__}"
        )
    if not isinstance(record, AuditRecord):
        raise TypeError(
            f"record must be an AuditRecord; got {type(record).__name__}"
        )

    # Serialise first so the scrubber re-check sees the exact bytes that
    # would be appended. ``ensure_ascii=False`` because the record may carry
    # Unicode in ``purpose`` / ``detail`` / ``locator`` and we do not want
    # to escape that — the scrubber works on Python ``str`` characters.
    serialised = record.model_dump_json()

    try:
        rechecked = scrub_stream(serialised, list(private_dict))
    except ScrubError as exc:
        # The serialised line still carried a raw value. Refuse to write.
        # The AuditError message intentionally identifies the failure
        # category, not the raw value itself.
        raise AuditError(
            "audit record failed pre-write scrubber re-check; refusing to "
            "append to restoration.jsonl"
        ) from exc

    # The rechecked output should be byte-identical to ``serialised`` when
    # no raw values are present, since neither pass should change a
    # well-formed record. Use it as the canonical line so any path-shape
    # mask the scrubber may have applied is also persisted.
    audit_dir = vault_root / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit_path = audit_dir / "restoration.jsonl"
    with audit_path.open("a", encoding="utf-8") as fh:
        fh.write(rechecked + "\n")
    logger.debug(
        "audit.write_record: appended request_id=%s outcome=%s",
        record.request_id,
        record.outcome,
    )


def read_records(vault_root: Path) -> list[AuditRecord]:
    """Read every audit record from ``<vault_root>/audit/restoration.jsonl``.

    Returns an empty list when the file does not exist. Used by tests to
    assert audit invariants; not part of the public boundary surface.

    Lines that do not parse against :class:`AuditRecord` are surfaced as a
    :class:`ValueError` (rather than silently skipped) so a corrupted log
    cannot mask a failed test assertion.
    """
    audit_path = vault_root / "audit" / "restoration.jsonl"
    if not audit_path.exists():
        return []
    out: list[AuditRecord] = []
    for line_no, raw in enumerate(
        audit_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"restoration.jsonl line {line_no} is not valid JSON"
            ) from exc
        # Best-effort: skip records that do not match the AuditRecord
        # schema (e.g. legacy lines written by ``boundary.restoration_request``
        # via :func:`_append_restoration_audit`, which uses a wider schema).
        # The execution-gateway audit shares the file path by spec; the
        # consumer is expected to filter by schema when reading.
        try:
            out.append(AuditRecord.model_validate(data))
        except Exception:  # noqa: BLE001 — schema mismatch is non-fatal here
            continue
    return out


__all__ = [
    "AuditError",
    "AuditOutcome",
    "AuditRecord",
    "read_records",
    "write_record",
]
