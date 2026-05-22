"""
Boundary — opaque public surface for agent-facing operations.

This module is the only surface ordinary agents are intended to import from
``yomotsusaka``. It provides:

* A frozen URI grammar for public artifact locators
  (``private://<exposure_class>/<artifact_kind>/<opaque_id>[#<fragment>]``).
* :class:`PublicHandle` — an opaque wrapper carrying only the locator string.
* :func:`build_locator` / :func:`parse_locator` — round-tripping helpers.
* :func:`resolve` — the fail-closed resolver contract that maps a locator to
  private-side state. Only callers that pass ``scope=ResolverScope.PRIVATE_BOUNDARY``
  ever receive the materialised :class:`PrivateState`; all other scopes get a
  :class:`ResolverSuccess` whose ``private_state`` is ``None``.
* Five MVP-2 request/response models (process / inspect / search /
  request-restore / report) and matching boundary entry points.

Private kernel modules (``pipeline``, ``commit``, ``restoration_api``,
``search_gateway``) remain importable at their original paths but are
classified as **private-side internal kernel** — see ``docs/scaffold-status.md``
and ``docs/architecture.md`` §5.7.1, §5.7.2.

No agent-facing return from this module carries raw private values, raw
file paths, or vault-side bytes. The single exception is
:class:`PrivateState`, which is intentionally restricted to
``scope=PRIVATE_BOUNDARY`` callers (e.g. the future #27 restoration flow);
its serialisation must never reach an ordinary-agent surface.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError as PydanticValidationError,
    field_validator,
    model_validator,
)

from yomotsusaka import restoration_api
from yomotsusaka.pipeline import process_document
from yomotsusaka.redactor import Span
from yomotsusaka.schemas import (
    ArtifactHandle,
    DocumentManifest,
    EntityKind,
    EntityRecord,
    PrivateDictEntry,
)
from yomotsusaka.search_gateway import SearchGateway

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Locator grammar
# ---------------------------------------------------------------------------

LOCATOR_SCHEME: Literal["private"] = "private"
"""Scheme constant for all public locators."""

EXPOSURE_CLASSES: frozenset[str] = frozenset(
    {"agent_public", "agent_redacted", "private", "restricted", "never_expose"}
)
"""Permitted ``<exposure_class>`` values; see metaplan Fork 1."""

ARTIFACT_KINDS: frozenset[str] = frozenset(
    {"manifest", "private_dict", "search_hit", "restoration_request", "status_report"}
)
"""Permitted ``<artifact_kind>`` values; see metaplan Fork 1."""

# Charset for opaque_id mirrors pipeline._validate_doc_id so any doc_id that
# survives the pipeline is also a syntactically valid locator opaque_id.
_OPAQUE_ID_PATTERN = re.compile(r"\A[A-Za-z0-9._-]{1,128}\Z")
_FRAGMENT_PATTERN = re.compile(r"\A[A-Za-z0-9._-]{1,64}\Z")

# Full locator pattern: private://<class>/<kind>/<opaque_id>[#<fragment>]
_LOCATOR_PATTERN = re.compile(
    r"\Aprivate://(?P<exposure>[A-Za-z_]+)/(?P<kind>[A-Za-z_]+)/"
    r"(?P<opaque>[A-Za-z0-9._-]{1,128})(?:#(?P<fragment>[A-Za-z0-9._-]{1,64}))?\Z"
)


class ParsedLocator(BaseModel, frozen=True):
    """Structured view of a parsed public locator. Internal to the boundary."""

    model_config = ConfigDict(extra="forbid")
    exposure_class: str
    artifact_kind: str
    opaque_id: str
    fragment: str | None = None


def build_locator(
    *,
    exposure_class: str,
    artifact_kind: str,
    opaque_id: str,
    fragment: str | None = None,
) -> str:
    """
    Construct a syntactically valid public locator string.

    Raises :class:`ValueError` on any invalid component. The output is the
    canonical on-the-wire form used by every public surface in this module.
    """
    if exposure_class not in EXPOSURE_CLASSES:
        raise ValueError(
            f"exposure_class must be one of {sorted(EXPOSURE_CLASSES)!r}; "
            f"got {exposure_class!r}"
        )
    if artifact_kind not in ARTIFACT_KINDS:
        raise ValueError(
            f"artifact_kind must be one of {sorted(ARTIFACT_KINDS)!r}; "
            f"got {artifact_kind!r}"
        )
    if (
        not isinstance(opaque_id, str)
        or not _OPAQUE_ID_PATTERN.fullmatch(opaque_id)
        or opaque_id in {".", ".."}
    ):
        raise ValueError(
            "opaque_id must match [A-Za-z0-9._-]{1,128} and must not be a "
            f"path traversal segment; got {opaque_id!r}"
        )
    if fragment is not None:
        if (
            not isinstance(fragment, str)
            or not _FRAGMENT_PATTERN.fullmatch(fragment)
            or fragment in {".", ".."}
        ):
            raise ValueError(
                "fragment must match [A-Za-z0-9._-]{1,64} and must not be a "
                f"path traversal segment; got {fragment!r}"
            )

    base = f"{LOCATOR_SCHEME}://{exposure_class}/{artifact_kind}/{opaque_id}"
    return f"{base}#{fragment}" if fragment is not None else base


def parse_locator(locator: str) -> ParsedLocator | None:
    """
    Return a :class:`ParsedLocator` for *locator* or ``None`` if invalid.

    Never raises; returns ``None`` on any malformed input so the resolver can
    distinguish parse failure from filesystem failure without touching disk.
    """
    if not isinstance(locator, str):
        return None
    match = _LOCATOR_PATTERN.fullmatch(locator)
    if match is None:
        return None
    exposure = match.group("exposure")
    kind = match.group("kind")
    if exposure not in EXPOSURE_CLASSES or kind not in ARTIFACT_KINDS:
        return None
    opaque = match.group("opaque")
    if opaque in {".", ".."}:
        return None
    fragment = match.group("fragment")
    if fragment in {".", ".."}:
        return None
    return ParsedLocator(
        exposure_class=exposure,
        artifact_kind=kind,
        opaque_id=opaque,
        fragment=fragment,
    )


# ---------------------------------------------------------------------------
# Public handle & span spec
# ---------------------------------------------------------------------------


class PublicHandle(BaseModel, frozen=True):
    """
    Opaque public reference to a committed artifact.

    Wraps only the locator string. Never carries vault paths, file system
    layout, or any private state. The mapping from internal
    :class:`~yomotsusaka.schemas.ArtifactHandle` to ``PublicHandle`` happens
    exclusively inside the boundary entry points.
    """

    model_config = ConfigDict(extra="forbid")
    locator: str = Field(
        description="Opaque public URI of the form "
        "private://<exposure_class>/<artifact_kind>/<opaque_id>[#<fragment>]",
    )


class SpanSpec(BaseModel, frozen=True):
    """Public-side description of a span to redact during processing.

    Mirrors :class:`yomotsusaka.redactor.Span` but as a Pydantic model so it
    can travel through the public request/response surface (which is
    schema-validated end to end).
    """

    model_config = ConfigDict(extra="forbid")
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    kind: EntityKind

    @model_validator(mode="after")
    def _check_range(self) -> "SpanSpec":
        # The kernel redactor silently drops out-of-range spans, which would
        # be observable to ordinary agents as "redaction silently did
        # nothing". Reject the obviously-malformed end < start case at the
        # public boundary so the failure is a clear client-side validation
        # error rather than a no-op.
        if self.end < self.start:
            raise ValueError(
                f"SpanSpec.end ({self.end}) must be >= start ({self.start})"
            )
        return self

    def to_internal(self) -> Span:
        """Project this spec into the kernel :class:`Span` dataclass."""
        return Span(start=self.start, end=self.end, kind=self.kind)


# ---------------------------------------------------------------------------
# Resolver contract
# ---------------------------------------------------------------------------


class ResolverScope(str, Enum):
    """Caller scope for :func:`resolve`.

    MVP-2 enforcement is shape-only: the resolver accepts all three values and
    only :data:`PRIVATE_BOUNDARY` populates :attr:`ResolverSuccess.private_state`.
    #27 will add the policy table that actually gates the values.
    """

    ORDINARY_AGENT = "ordinary_agent"
    PRIVATE_BOUNDARY = "private_boundary"
    AUDIT_REVIEWER = "audit_reviewer"


class ResolverFailureReason(str, Enum):
    """Enumerated reasons returned in :class:`ResolverFailure`.

    Values are stable wire identifiers; do not rename without coordinating
    with downstream consumers and the umbrella #29 contract tests.
    """

    MalformedLocator = "malformed_locator"
    UnknownArtifact = "unknown_artifact"
    ArtifactMissing = "artifact_missing"
    ScopeDenied = "scope_denied"
    PurposeNotPermitted = "purpose_not_permitted"


class ResolverError(Exception):
    """Raised for programmer errors only (e.g. ``scope=None``).

    Expected failure categories (malformed locator, missing artifact, scope
    denied, purpose not permitted) are returned as :class:`ResolverFailure`
    values, not raised. See metaplan Fork 6.
    """


class PrivateState(BaseModel, frozen=True):
    """Private-side payload. Never serialised to public outputs.

    Only populated when :func:`resolve` is called with
    ``scope=ResolverScope.PRIVATE_BOUNDARY``. Ordinary-agent and
    audit-reviewer callers always see ``private_state=None`` even on success.
    """

    model_config = ConfigDict(extra="forbid")
    manifest_path: Path
    private_dict_path: Path
    private_entries: list[PrivateDictEntry]


class ResolverFailure(BaseModel, frozen=True):
    """Structured failure report.

    ``detail`` MUST NOT contain raw private values, absolute paths, environment
    variable contents, or credentials. This invariant is enforced by tests.
    """

    model_config = ConfigDict(extra="forbid")
    outcome: Literal["failure"] = "failure"
    locator: str
    reason: ResolverFailureReason
    detail: str | None = None


class ResolverSuccess(BaseModel, frozen=True):
    """Structured success report.

    For ``scope`` other than :data:`ResolverScope.PRIVATE_BOUNDARY`,
    ``private_state`` is ``None`` even when the artifact exists on disk.
    """

    model_config = ConfigDict(extra="forbid")
    outcome: Literal["success"] = "success"
    locator: str
    exposure_class: str
    artifact_kind: str
    opaque_id: str
    fragment: str | None = None
    purpose: str
    private_state: PrivateState | None = None


def _vault_paths(vault_root: Path, opaque_id: str) -> tuple[Path, Path]:
    """Return the (manifest_path, private_dict_path) pair for *opaque_id*."""
    return (
        vault_root / "manifests" / f"{opaque_id}.json",
        vault_root / "private" / f"{opaque_id}.json",
    )


def resolve(
    locator: str,
    *,
    scope: ResolverScope,
    purpose: str,
    vault_root: Path,
) -> ResolverSuccess | ResolverFailure:
    """
    Resolve *locator* to a structured success or failure report.

    Fail-closed semantics:

    * Programmer errors (wrong types for ``scope`` / ``vault_root`` /
      ``locator``) raise :class:`ResolverError`.
    * Expected failure categories (malformed locator, unknown artifact kind,
      missing artifact, empty purpose) are returned as :class:`ResolverFailure`
      values.
    * The locator is parsed before any filesystem call. A malformed locator
      never reaches :meth:`Path.exists`.

    Parameters
    ----------
    locator:
        Public URI string.
    scope:
        Caller scope; only :data:`ResolverScope.PRIVATE_BOUNDARY` triggers
        materialisation of :class:`PrivateState`.
    purpose:
        Free-form, required, non-empty after ``.strip()``. Recorded on
        :attr:`ResolverSuccess.purpose` for audit. Empty/whitespace ⇒
        :class:`ResolverFailure(reason=PurposeNotPermitted)`.
    vault_root:
        Local vault root. Explicit dependency injection; no environment
        defaults.
    """
    # ---- programmer-error guardrails (these raise, never return) ----
    if not isinstance(scope, ResolverScope):
        raise ResolverError(
            f"scope must be a ResolverScope; got {type(scope).__name__}"
        )
    if not isinstance(vault_root, Path):
        raise ResolverError(
            f"vault_root must be a pathlib.Path; got {type(vault_root).__name__}"
        )
    if not isinstance(locator, str):
        raise ResolverError(
            f"locator must be a str; got {type(locator).__name__}"
        )
    if not isinstance(purpose, str):
        raise ResolverError(
            f"purpose must be a str; got {type(purpose).__name__}"
        )

    # ---- purpose check (returns, does not raise) ----
    if not purpose.strip():
        return ResolverFailure(
            locator=locator,
            reason=ResolverFailureReason.PurposeNotPermitted,
            detail="purpose must be a non-empty string",
        )

    # ---- locator parse: must succeed before any filesystem call ----
    parsed = parse_locator(locator)
    if parsed is None:
        return ResolverFailure(
            locator=locator,
            reason=ResolverFailureReason.MalformedLocator,
            detail="locator does not match the public URI grammar",
        )

    # ---- artifact-kind dispatch ----
    if parsed.artifact_kind != "manifest":
        # Grammar reserves other artifact_kinds; only "manifest" is wired in
        # MVP-2. Anything else is an unknown artifact for resolution purposes.
        return ResolverFailure(
            locator=locator,
            reason=ResolverFailureReason.UnknownArtifact,
            detail=f"artifact_kind {parsed.artifact_kind!r} is not resolvable in MVP-2",
        )

    # For ordinary-agent and audit-reviewer scopes the resolver does not
    # materialise private state. It still checks the artifact exists so
    # callers that ask for a missing locator under a non-private scope get
    # UnknownArtifact rather than an empty "success".
    manifest_path, private_dict_path = _vault_paths(vault_root, parsed.opaque_id)
    if not manifest_path.exists():
        return ResolverFailure(
            locator=locator,
            reason=ResolverFailureReason.UnknownArtifact,
            detail="no committed manifest for this locator",
        )

    private_state: PrivateState | None = None
    if scope is ResolverScope.PRIVATE_BOUNDARY:
        if not private_dict_path.exists():
            return ResolverFailure(
                locator=locator,
                reason=ResolverFailureReason.ArtifactMissing,
                detail="private dictionary file is missing for this artifact",
            )
        # A file that passed exists() but cannot be parsed (corrupt JSON, schema
        # drift, race-deleted between exists() and read_text()) is an
        # operational failure, not a programmer error. Per architecture
        # §5.7.2 every expected failure category is a returned ResolverFailure,
        # never a raised exception. Detail is intentionally generic so the
        # parse error message (which can contain raw bytes from the file) is
        # not propagated to public surfaces.
        try:
            raw_text = private_dict_path.read_text(encoding="utf-8")
            raw_entries = json.loads(raw_text)
            # The file must be a JSON array; reject every other top-level
            # shape (null, object, scalar) explicitly so a non-iterable or
            # mapping-keyed iteration cannot raise TypeError out of the
            # except below. Use model_validate (Pydantic v2 idiom) so a
            # non-mapping list item raises PydanticValidationError rather
            # than TypeError, keeping the except tuple consistent.
            if not isinstance(raw_entries, list):
                raise TypeError(
                    "private dictionary file must contain a JSON array"
                )
            private_entries = [
                PrivateDictEntry.model_validate(item) for item in raw_entries
            ]
        except (OSError, ValueError, TypeError, PydanticValidationError):
            return ResolverFailure(
                locator=locator,
                reason=ResolverFailureReason.ArtifactMissing,
                detail="private dictionary could not be read or parsed",
            )
        private_state = PrivateState(
            manifest_path=manifest_path,
            private_dict_path=private_dict_path,
            private_entries=private_entries,
        )

    return ResolverSuccess(
        locator=locator,
        exposure_class=parsed.exposure_class,
        artifact_kind=parsed.artifact_kind,
        opaque_id=parsed.opaque_id,
        fragment=parsed.fragment,
        purpose=purpose,
        private_state=private_state,
    )


# ---------------------------------------------------------------------------
# Public manifest projection
# ---------------------------------------------------------------------------


class PublicManifestView(BaseModel, frozen=True):
    """Agent-safe projection of a :class:`DocumentManifest`.

    Drops ``source_ref`` — the opaque internal correlation key that
    intentionally aliases ``doc_id`` in MVP-1 — and keeps only the fields
    ordinary agents should see.
    """

    model_config = ConfigDict(extra="forbid")
    doc_id: str
    redacted_text: str
    entities: list[EntityRecord] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    summary: str = ""
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_manifest(cls, manifest: DocumentManifest) -> "PublicManifestView":
        return cls(
            doc_id=manifest.doc_id,
            redacted_text=manifest.redacted_text,
            entities=list(manifest.entities),
            labels=list(manifest.labels),
            summary=manifest.summary,
            created_at=manifest.created_at,
            metadata=dict(manifest.metadata),
        )


# ---------------------------------------------------------------------------
# Request / response schemas (five MVP-2 operations)
# ---------------------------------------------------------------------------


class ProcessRequest(BaseModel, frozen=True):
    """Public-side request to drive raw_text through the kernel pipeline."""

    model_config = ConfigDict(extra="forbid")
    doc_id: str
    raw_text: str
    spans: list[SpanSpec] = Field(default_factory=list)


class ProcessResponse(BaseModel, frozen=True):
    """Public response carrying only the opaque handle."""

    model_config = ConfigDict(extra="forbid")
    handle: PublicHandle


class InspectRequest(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid")
    locator: str
    # Forwarded to resolve() so the caller's intent shows up on
    # ResolverSuccess.purpose for the future #27 audit log. Defaults to a
    # stable string so existing callers do not have to change.
    purpose: str = "boundary.inspect_request"


class InspectResponse(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid")
    manifest: PublicManifestView


class SearchRequest(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid")
    query: str
    top_k: int = Field(default=10, ge=1, le=1000)


class SearchHit(BaseModel, frozen=True):
    """One search result.

    Carries only the public handle, a redacted snippet, and the manifest's
    public labels. No ``DocumentManifest`` is returned to ordinary agents.
    """

    model_config = ConfigDict(extra="forbid")
    handle: PublicHandle
    redacted_snippet: str
    labels: list[str] = Field(default_factory=list)


class SearchResponse(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid")
    hits: list[SearchHit] = Field(default_factory=list)


class RestorationFailureReason(str, Enum):
    """Enumerated reasons returned in a failed :class:`RestorationResponse`.

    Values are stable wire identifiers; do not rename without coordinating
    with downstream consumers and the umbrella #29 contract tests.

    ``UnknownArtifact`` is grammar-reserved for future wiring (e.g. when the
    boundary learns to resolve artifact_kinds other than ``"manifest"`` and
    needs to distinguish "locator parses but addresses a kind not committed
    locally" from the schema-invalid case). MVP-2 never emits it; callers
    that exhaustively match this enum must handle it as an inert future
    value.
    """

    RequestSchemaInvalid = "request_schema_invalid"
    ScopeDenied = "scope_denied"
    UnknownArtifact = "unknown_artifact"  # reserved; not emitted in MVP-2
    ArtifactMissing = "artifact_missing"
    AuditWriteFailed = "audit_write_failed"
    KernelError = "kernel_error"


class RestorationRequest(BaseModel, frozen=True):
    """Public-side request to restore raw private values for a committed artifact.

    The request is a *public* schema: it carries the caller's intent (who,
    what, why, when) but never raw private values. The kernel call that
    actually materialises :class:`PrivateDictEntry` objects happens only
    after this request is validated, scope-checked, and audit-logged.

    Two-part field split:

    * **Required (validated)**: ``caller_label``, ``target_public_handle`` xor
      ``document_id``, at least one of ``requested_keys`` / ``requested_entity_kinds``,
      ``reason``, ``timestamp``.
    * **Reserved (accepted as-given, persisted unchanged)**:
      ``authorization_decision``, ``policy_profile``, ``approval_ticket``,
      ``production_scope``. These are recorded into the audit log but their
      semantics are not enforced in MVP-2.
    """

    model_config = ConfigDict(extra="forbid")

    caller_label: str = Field(
        description="Caller identity or task label. Free-form; non-empty after strip.",
    )
    target_public_handle: PublicHandle | None = Field(
        default=None,
        description="Public handle of the artifact to restore. Exactly one of "
        "this and ``document_id`` must be set.",
    )
    document_id: str | None = Field(
        default=None,
        description="Direct document id, when the caller already knows it. "
        "Exactly one of this and ``target_public_handle`` must be set.",
    )
    requested_keys: list[str] = Field(
        default_factory=list,
        description="Restrict the response to entries with these redacted keys.",
    )
    requested_entity_kinds: list[EntityKind] = Field(
        default_factory=list,
        description="Restrict the response to entries of these entity kinds.",
    )
    reason: str = Field(
        description="Free-form purpose for the restoration. Non-empty after strip.",
    )
    timestamp: datetime = Field(
        description="Request timestamp, timezone-aware UTC.",
    )
    # Reserved fields — persisted unchanged into the audit record and not
    # used as a policy gate in MVP-2. Their *types* still match the frozen
    # lite-spec: ``authorization_decision`` is intentionally constrained to
    # ``Literal["accept"] | None`` rather than ``str | None`` so that new
    # decision values (``"deny"``, ``"pending"``, etc.) have to land via an
    # explicit schema migration with paired audit-consumer changes — not by
    # ambient string drift. ``policy_profile`` / ``approval_ticket`` /
    # ``production_scope`` are free-form because their wire shape is not
    # yet pinned by any audit consumer.
    authorization_decision: Literal["accept"] | None = None
    policy_profile: str | None = None
    approval_ticket: str | None = None
    production_scope: str | None = None

    @field_validator("caller_label")
    @classmethod
    def _caller_label_non_empty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("caller_label must be non-empty after strip")
        return v

    @field_validator("reason")
    @classmethod
    def _reason_non_empty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("reason must be non-empty after strip")
        return v

    @field_validator("timestamp")
    @classmethod
    def _timestamp_utc(cls, v: datetime) -> datetime:
        # Python only treats a datetime as truly aware when ``utcoffset()``
        # is non-None. A custom ``tzinfo`` whose ``utcoffset()`` returns
        # None still satisfies ``tzinfo is not None`` but is naive by
        # Python's own definition (datetime docs §"aware and naive
        # objects"). Use the canonical idiom so such constructions are
        # rejected here rather than slipping into ``astimezone()`` later.
        if not isinstance(v, datetime) or v.utcoffset() is None:
            raise ValueError("timestamp must be a timezone-aware datetime")
        return v

    @field_validator("document_id")
    @classmethod
    def _document_id_opaque_id_charset(cls, v: str | None) -> str | None:
        # When supplied, ``document_id`` is interpolated into the vault path
        # ``<vault_root>/private/<document_id>.json``. Require the same
        # opaque-id charset and traversal exclusion the locator grammar
        # enforces (architecture §5.7.1) so path separators, traversal
        # segments, and out-of-range values are rejected at the public
        # boundary rather than at the kernel's relative_to() guard.
        if v is None:
            return v
        if not isinstance(v, str) or not _OPAQUE_ID_PATTERN.fullmatch(v) or v in {".", ".."}:
            raise ValueError(
                "document_id must match [A-Za-z0-9._-]{1,128} and must not be a "
                "path traversal segment"
            )
        return v

    @model_validator(mode="after")
    def _check_targets_and_filters(self) -> "RestorationRequest":
        has_handle = self.target_public_handle is not None
        has_doc_id = self.document_id is not None and self.document_id != ""
        if has_handle == has_doc_id:
            raise ValueError(
                "exactly one of target_public_handle and document_id must be set"
            )
        if not self.requested_keys and not self.requested_entity_kinds:
            raise ValueError(
                "at least one of requested_keys or requested_entity_kinds must be non-empty"
            )
        return self


class RestorationResponse(BaseModel, frozen=True):
    """Public response from :func:`restoration_request`.

    ``outcome`` is one of:

    * ``"accepted"`` — the kernel returned entries; ``private_entries`` is
      populated. This outcome is only reachable for
      :data:`ResolverScope.PRIVATE_BOUNDARY` callers.
    * ``"accepted_but_redacted"`` — reserved for post-MVP-2 use; never
      emitted in MVP-2. ``private_entries`` is ``None``.
    * ``"failed"`` — ``reason`` identifies the failure category and
      ``private_entries`` is ``None``.

    ``detail`` must never contain raw private values, absolute filesystem
    paths, or the vault root.
    """

    model_config = ConfigDict(extra="forbid")
    outcome: Literal["accepted", "accepted_but_redacted", "failed"]
    audit_record_id: str
    document_id: str | None = None
    reason: RestorationFailureReason | None = None
    detail: str | None = None
    private_entries: list[PrivateDictEntry] | None = None

    @model_validator(mode="after")
    def _check_outcome_invariants(self) -> "RestorationResponse":
        if self.outcome == "accepted":
            if self.private_entries is None:
                raise ValueError(
                    "accepted outcome must carry private_entries (may be an empty list)"
                )
            if self.reason is not None:
                raise ValueError("accepted outcome must not carry a failure reason")
        elif self.outcome == "failed":
            if self.reason is None:
                raise ValueError("failed outcome must carry a failure reason")
            if self.private_entries is not None:
                raise ValueError("failed outcome must not carry private_entries")
        else:  # accepted_but_redacted
            if self.private_entries is not None:
                raise ValueError(
                    "accepted_but_redacted outcome must not carry private_entries"
                )
        return self


class StatusReportRequest(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid")
    locator: str


class StatusReportResponse(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid")
    locator: str
    status: Literal["committed", "unknown"]


# ---------------------------------------------------------------------------
# Boundary entry points
# ---------------------------------------------------------------------------


def _public_handle_for(doc_id: str) -> PublicHandle:
    """Build the MVP-2 canonical public handle for a committed doc_id."""
    return PublicHandle(
        locator=build_locator(
            exposure_class="agent_redacted",
            artifact_kind="manifest",
            opaque_id=doc_id,
        )
    )


def process_document_request(
    request: ProcessRequest,
    *,
    vault_root: Path,
) -> ProcessResponse:
    """Drive *request* through the kernel and return a public-only handle.

    The internal :class:`ArtifactHandle` returned by the kernel is mapped to
    a :class:`PublicHandle` carrying the opaque locator only; ``vault_path``
    is discarded at the boundary and never reaches the response.

    The kernel's :func:`pipeline.process_document` validates *doc_id* against
    the same opaque-id charset the locator grammar uses (including the
    ``"."``/``".."`` exclusion), so an unsafe doc_id raises ``ValueError``
    *before* any vault write — there is no orphan-write risk between the
    kernel call and :func:`build_locator`. We also pre-validate the locator
    component here so the error path is symmetric whether the rejection
    happens kernel-side or boundary-side.
    """
    # Boundary-side validation mirrors the kernel guard. If the kernel
    # tightens or relaxes _validate_doc_id, this call surfaces the
    # difference here (build_locator raises ValueError) instead of leaving
    # a window where one side accepts and the other rejects.
    _ = build_locator(
        exposure_class="agent_redacted",
        artifact_kind="manifest",
        opaque_id=request.doc_id,
    )
    handle: ArtifactHandle = process_document(
        doc_id=request.doc_id,
        raw_text=request.raw_text,
        spans=[s.to_internal() for s in request.spans],
        vault_root=vault_root,
    )
    return ProcessResponse(handle=_public_handle_for(handle.doc_id))


def inspect_request(
    request: InspectRequest,
    *,
    vault_root: Path,
) -> InspectResponse | ResolverFailure:
    """Read a committed manifest and return its public view.

    Returns :class:`ResolverFailure` (not raises) on malformed locator,
    unknown artifact, or missing manifest. This keeps the public surface
    homogeneous: callers branch on ``isinstance(... , ResolverFailure)``.
    """
    outcome = resolve(
        request.locator,
        scope=ResolverScope.ORDINARY_AGENT,
        purpose=request.purpose,
        vault_root=vault_root,
    )
    if isinstance(outcome, ResolverFailure):
        return outcome
    manifest_path, _ = _vault_paths(vault_root, outcome.opaque_id)
    # The exists() check inside resolve() can race against a concurrent
    # delete, and a manifest file with schema drift would otherwise raise
    # pydantic.ValidationError. Both are operational failures, not
    # programmer errors — return ResolverFailure to preserve the
    # documented "homogeneous public surface" contract.
    try:
        manifest = DocumentManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError, TypeError, PydanticValidationError):
        return ResolverFailure(
            locator=request.locator,
            reason=ResolverFailureReason.ArtifactMissing,
            detail="manifest could not be read or parsed",
        )
    return InspectResponse(manifest=PublicManifestView.from_manifest(manifest))


def _redacted_snippet(text: str, query: str, *, window: int = 60) -> str:
    """Return a window of *text* around the first case-insensitive match of *query*.

    The window is taken from ``text`` only — which is already redacted on
    indexed manifests — so the result carries no raw private values.
    """
    if not query:
        return text[:window]
    haystack = text.lower()
    needle = query.lower()
    idx = haystack.find(needle)
    if idx == -1:
        return text[:window]
    start = max(0, idx - window // 2)
    end = min(len(text), idx + len(query) + window // 2)
    return text[start:end]


def search_request(
    request: SearchRequest,
    *,
    gateway: SearchGateway,
) -> SearchResponse:
    """Search redacted manifests and return public-only hits.

    The :class:`SearchGateway` only ever indexes redacted manifests, so any
    hit is by construction public-safe. The boundary still drops the raw
    :class:`DocumentManifest` reference and exposes only handle / snippet /
    labels.
    """
    raw_hits: list[DocumentManifest] = gateway.search(request.query, top_k=request.top_k)
    hits = [
        SearchHit(
            handle=_public_handle_for(m.doc_id),
            redacted_snippet=_redacted_snippet(m.redacted_text, request.query),
            labels=list(m.labels),
        )
        for m in raw_hits
    ]
    return SearchResponse(hits=hits)


def _append_restoration_audit(
    vault_root: Path,
    *,
    audit_record_id: str,
    scope: ResolverScope,
    request: RestorationRequest | dict[str, Any],
    outcome: Literal["accepted", "failed"],
    failure_reason: RestorationFailureReason | None,
    returned_entry_count: int | None,
) -> str:
    """Append one JSONL audit record to ``<vault_root>/audit/restoration.jsonl``.

    The boundary writes records in the following pattern per request:

    * **Schema-invalid / scope-denied / audit-only-failure paths**: one
      record with ``outcome="failed"`` and a non-null ``failure_reason``.
    * **Accepted path (kernel succeeds)**: two records sharing the same
      ``audit_record_id`` — an *intent* record
      (``outcome="accepted"``, ``returned_entry_count=None``) written
      before the kernel call, and a *result* record
      (``outcome="accepted"``, ``returned_entry_count=<int>``) written
      after the kernel returns.
    * **Kernel-error path (intent was written, then kernel failed)**: two
      records sharing the same ``audit_record_id`` — the original intent
      record (``outcome="accepted"``, ``returned_entry_count=None``) and a
      corrective record (``outcome="failed"`` with a non-null
      ``failure_reason``). The intent record is intentionally not deleted
      or rewritten — JSONL is append-only — so consumers reconstruct the
      final outcome by **taking the last record per ``audit_record_id``**.
      Counting raw "accepted" lines without correlating by
      ``audit_record_id`` will overcount accepted operations.

    Raises :class:`OSError` on filesystem failure. Returns the
    ``audit_record_id`` that was written (echo of the input).

    The record never contains :attr:`PrivateDictEntry.original_value` or
    any resolved filesystem path.
    """
    audit_dir = vault_root / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit_path = audit_dir / "restoration.jsonl"

    # Project the request into a JSON-safe shape. When the caller hands us a
    # raw dict (the schema-invalid path), persist whatever scalar fields are
    # safe to record; never persist non-stringifiable junk.
    if isinstance(request, RestorationRequest):
        if request.target_public_handle is not None:
            target: dict[str, Any] = {
                "public_handle": request.target_public_handle.locator,
            }
        else:
            target = {"document_id": request.document_id}
        record_request_timestamp: str | None = request.timestamp.astimezone(
            timezone.utc
        ).isoformat()
        caller_label = request.caller_label
        requested_keys = list(request.requested_keys)
        requested_entity_kinds = [k.value for k in request.requested_entity_kinds]
        reason = request.reason
        authorization_decision = request.authorization_decision
        policy_profile = request.policy_profile
        approval_ticket = request.approval_ticket
        production_scope = request.production_scope
    else:
        # Schema-invalid fallback: best-effort capture of whatever the caller
        # supplied, with values coerced to JSON-safe types so the audit line
        # is always parseable.
        raw = request
        target = {"raw_request": True}
        record_request_timestamp = None
        caller_label = str(raw.get("caller_label", "")) if isinstance(raw, dict) else ""
        requested_keys = []
        requested_entity_kinds = []
        reason = str(raw.get("reason", "")) if isinstance(raw, dict) else ""
        authorization_decision = None
        policy_profile = None
        approval_ticket = None
        production_scope = None

    record: dict[str, Any] = {
        "audit_record_id": audit_record_id,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "caller_label": caller_label,
        "scope": scope.name,
        "target": target,
        "requested_keys": requested_keys,
        "requested_entity_kinds": requested_entity_kinds,
        "reason": reason,
        "request_timestamp": record_request_timestamp,
        "authorization_decision": authorization_decision,
        "policy_profile": policy_profile,
        "approval_ticket": approval_ticket,
        "production_scope": production_scope,
        "outcome": outcome,
        "failure_reason": failure_reason.value if failure_reason is not None else None,
        "returned_entry_count": returned_entry_count,
    }
    line = json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"
    with audit_path.open("a", encoding="utf-8") as fh:
        fh.write(line)
    return audit_record_id


def _new_audit_id() -> str:
    return uuid.uuid4().hex


def _strip_vault_root(message: str, vault_root: Path) -> str:
    """Strip occurrences of ``vault_root`` (and its resolved form) from *message*.

    Used to scrub kernel error messages that may include the absolute path
    of the private dictionary file before they appear in
    :attr:`RestorationResponse.detail`.
    """
    cleaned = message.replace(str(vault_root), "<vault_root>")
    try:
        resolved = str(vault_root.resolve())
    except OSError:
        resolved = ""
    if resolved and resolved != str(vault_root):
        cleaned = cleaned.replace(resolved, "<vault_root>")
    return cleaned


def restoration_request(
    request: RestorationRequest,
    *,
    scope: ResolverScope,
    vault_root: Path,
) -> RestorationResponse:
    """Audit-logged, scope-gated restoration entry point.

    Every observable code path writes (or attempts to write) one or two
    audit records to ``<vault_root>/audit/restoration.jsonl`` before
    returning. The kernel :func:`restoration_api.restore` is only invoked
    when ``scope == ResolverScope.PRIVATE_BOUNDARY``, the request schema is
    valid, and the intent audit record has been written.

    Failure modes:

    * Schema-invalid input → :data:`RestorationFailureReason.RequestSchemaInvalid`.
      If the intent audit also fails, the response carries
      :data:`RestorationFailureReason.AuditWriteFailed` instead.
    * Non-``PRIVATE_BOUNDARY`` scope →
      :data:`RestorationFailureReason.ScopeDenied`. Audit written; kernel NOT
      called.
    * Audit-write failure on the accept path →
      :data:`RestorationFailureReason.AuditWriteFailed`. Kernel NOT called.
    * Unknown artifact (kernel raises "No private data found") →
      :data:`RestorationFailureReason.ArtifactMissing`.
    * Other kernel errors → :data:`RestorationFailureReason.KernelError`.
      ``detail`` strips ``vault_root`` substrings.
    """
    # ---- programmer-error guardrails (raise, never return) ----
    if not isinstance(scope, ResolverScope):
        raise ResolverError(
            f"scope must be a ResolverScope; got {type(scope).__name__}"
        )
    if not isinstance(vault_root, Path):
        raise ResolverError(
            f"vault_root must be a pathlib.Path; got {type(vault_root).__name__}"
        )

    # ---- step (a): Pydantic validation already happened by virtue of the
    # caller constructing a RestorationRequest. If they wanted to handle the
    # ValidationError themselves they did so upstream. If something is still
    # wrong here (e.g. a dict snuck in), reject it now.
    if not isinstance(request, RestorationRequest):
        audit_id = _new_audit_id()
        try:
            _append_restoration_audit(
                vault_root,
                audit_record_id=audit_id,
                scope=scope,
                request=request if isinstance(request, dict) else {},
                outcome="failed",
                failure_reason=RestorationFailureReason.RequestSchemaInvalid,
                returned_entry_count=None,
            )
        except OSError:
            return RestorationResponse(
                outcome="failed",
                audit_record_id=audit_id,
                reason=RestorationFailureReason.AuditWriteFailed,
                detail="audit log could not be written",
            )
        return RestorationResponse(
            outcome="failed",
            audit_record_id=audit_id,
            reason=RestorationFailureReason.RequestSchemaInvalid,
            detail="request must be a RestorationRequest instance",
        )

    # ---- step (b): resolve target_public_handle → document_id ----
    if request.target_public_handle is not None:
        parsed = parse_locator(request.target_public_handle.locator)
        if (
            parsed is None
            or parsed.artifact_kind != "manifest"
            or parsed.exposure_class not in EXPOSURE_CLASSES
        ):
            audit_id = _new_audit_id()
            try:
                _append_restoration_audit(
                    vault_root,
                    audit_record_id=audit_id,
                    scope=scope,
                    request=request,
                    outcome="failed",
                    failure_reason=RestorationFailureReason.RequestSchemaInvalid,
                    returned_entry_count=None,
                )
            except OSError:
                return RestorationResponse(
                    outcome="failed",
                    audit_record_id=audit_id,
                    reason=RestorationFailureReason.AuditWriteFailed,
                    detail="audit log could not be written",
                )
            return RestorationResponse(
                outcome="failed",
                audit_record_id=audit_id,
                reason=RestorationFailureReason.RequestSchemaInvalid,
                detail="target_public_handle does not parse as a manifest locator",
            )
        document_id = parsed.opaque_id
    else:
        # _check_targets_and_filters guarantees document_id is set here.
        assert request.document_id is not None
        document_id = request.document_id

    # ---- step (c): scope gate. Non-PRIVATE_BOUNDARY → audit + ScopeDenied,
    # kernel NOT called.
    if scope is not ResolverScope.PRIVATE_BOUNDARY:
        audit_id = _new_audit_id()
        try:
            _append_restoration_audit(
                vault_root,
                audit_record_id=audit_id,
                scope=scope,
                request=request,
                outcome="failed",
                failure_reason=RestorationFailureReason.ScopeDenied,
                returned_entry_count=None,
            )
        except OSError:
            return RestorationResponse(
                outcome="failed",
                audit_record_id=audit_id,
                reason=RestorationFailureReason.AuditWriteFailed,
                detail="audit log could not be written",
            )
        return RestorationResponse(
            outcome="failed",
            audit_record_id=audit_id,
            document_id=document_id,
            reason=RestorationFailureReason.ScopeDenied,
            detail="scope does not authorise restoration",
        )

    # ---- step (d): intent audit BEFORE kernel call. Failure here is a hard
    # stop — we never reach the kernel without a durable audit record.
    audit_id = _new_audit_id()
    try:
        _append_restoration_audit(
            vault_root,
            audit_record_id=audit_id,
            scope=scope,
            request=request,
            outcome="accepted",
            failure_reason=None,
            returned_entry_count=None,
        )
    except OSError:
        return RestorationResponse(
            outcome="failed",
            audit_record_id=audit_id,
            document_id=document_id,
            reason=RestorationFailureReason.AuditWriteFailed,
            detail="audit log could not be written",
        )

    # ---- step (e): kernel call ----
    # The kernel may raise either RestorationError (documented contract) or
    # an unexpected exception (corrupt JSON in the private dictionary file,
    # PrivateDictEntry validation failure, OSError on read, etc.). Both
    # cases must be classified as failed RestorationResponse values rather
    # than escaping as raw exceptions — otherwise the boundary's own
    # "every code path returns a structured response" contract is broken
    # and callers lose the audit-record-id pairing. Unexpected exceptions
    # map to KernelError with a generic detail string so the underlying
    # message (which may contain raw bytes from a malformed file) is not
    # propagated through the public surface.
    private_path = (vault_root / "private" / f"{document_id}.json").resolve()
    handle = ArtifactHandle(doc_id=document_id, vault_path=str(private_path))
    reason_code: RestorationFailureReason | None = None
    detail: str | None = None
    entries: list[PrivateDictEntry] | None = None
    try:
        entries = restoration_api.restore(handle, vault_root=vault_root)
    except restoration_api.RestorationError as exc:
        msg = str(exc)
        if "No private data found" in msg:
            reason_code = RestorationFailureReason.ArtifactMissing
            detail = "no private data is committed for this document_id"
        else:
            reason_code = RestorationFailureReason.KernelError
            detail = _strip_vault_root(msg, vault_root)
    except Exception:
        # Any non-RestorationError leak (corrupt JSON, schema drift,
        # OSError mid-read, etc.) is a kernel-side failure. Intentionally
        # do not echo the underlying exception message — it may contain
        # raw bytes from the private dictionary file.
        reason_code = RestorationFailureReason.KernelError
        detail = "kernel raised an unexpected exception while reading private data"

    if reason_code is not None:
        # Corrective audit record for the failure.
        try:
            _append_restoration_audit(
                vault_root,
                audit_record_id=audit_id,
                scope=scope,
                request=request,
                outcome="failed",
                failure_reason=reason_code,
                returned_entry_count=None,
            )
        except OSError:
            return RestorationResponse(
                outcome="failed",
                audit_record_id=audit_id,
                document_id=document_id,
                reason=RestorationFailureReason.AuditWriteFailed,
                detail="audit log could not be written",
            )
        return RestorationResponse(
            outcome="failed",
            audit_record_id=audit_id,
            document_id=document_id,
            reason=reason_code,
            detail=detail,
        )
    assert entries is not None  # narrow type after successful kernel call

    # ---- step (g): AND-filter on key + entity_kind. Both lists empty is
    # rejected by the request validator, so at least one filter is active.
    filtered: list[PrivateDictEntry] = []
    keys_filter = set(request.requested_keys)
    kinds_filter = set(request.requested_entity_kinds)
    for entry in entries:
        if keys_filter and entry.key not in keys_filter:
            continue
        if kinds_filter and entry.kind not in kinds_filter:
            continue
        filtered.append(entry)

    # ---- result audit record (sharing the intent's audit_record_id). ----
    try:
        _append_restoration_audit(
            vault_root,
            audit_record_id=audit_id,
            scope=scope,
            request=request,
            outcome="accepted",
            failure_reason=None,
            returned_entry_count=len(filtered),
        )
    except OSError:
        return RestorationResponse(
            outcome="failed",
            audit_record_id=audit_id,
            document_id=document_id,
            reason=RestorationFailureReason.AuditWriteFailed,
            detail="audit log could not be written",
        )

    return RestorationResponse(
        outcome="accepted",
        audit_record_id=audit_id,
        document_id=document_id,
        private_entries=filtered,
    )


def status_report_request(
    request: StatusReportRequest,
    *,
    vault_root: Path,
) -> StatusReportResponse:
    """Report whether *locator* corresponds to a committed artifact.

    Shape-only stub: returns ``"committed"`` if the manifest file exists at
    the canonical vault path, ``"unknown"`` otherwise. Malformed locators
    map to ``"unknown"`` because the public status response must always be
    well-typed; for stricter failure routing use :func:`resolve` directly.
    """
    parsed = parse_locator(request.locator)
    if parsed is None or parsed.artifact_kind != "manifest":
        return StatusReportResponse(locator=request.locator, status="unknown")
    manifest_path, _ = _vault_paths(vault_root, parsed.opaque_id)
    status: Literal["committed", "unknown"] = (
        "committed" if manifest_path.exists() else "unknown"
    )
    return StatusReportResponse(locator=request.locator, status=status)


__all__ = [
    # Locator grammar
    "LOCATOR_SCHEME",
    "EXPOSURE_CLASSES",
    "ARTIFACT_KINDS",
    "ParsedLocator",
    "build_locator",
    "parse_locator",
    "PublicHandle",
    "SpanSpec",
    # Resolver contract
    "ResolverScope",
    "ResolverFailureReason",
    "ResolverError",
    "PrivateState",
    "ResolverFailure",
    "ResolverSuccess",
    "resolve",
    # Public manifest projection
    "PublicManifestView",
    # Request / response models
    "ProcessRequest",
    "ProcessResponse",
    "InspectRequest",
    "InspectResponse",
    "SearchRequest",
    "SearchHit",
    "SearchResponse",
    "RestorationRequest",
    "RestorationResponse",
    "RestorationFailureReason",
    "StatusReportRequest",
    "StatusReportResponse",
    # Boundary entry points
    "process_document_request",
    "inspect_request",
    "search_request",
    "restoration_request",
    "status_report_request",
]
