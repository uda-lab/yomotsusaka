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
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError as PydanticValidationError, model_validator

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
        if not isinstance(fragment, str) or not _FRAGMENT_PATTERN.fullmatch(fragment):
            raise ValueError(
                "fragment must match [A-Za-z0-9._-]{1,64}; "
                f"got {fragment!r}"
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
    return ParsedLocator(
        exposure_class=exposure,
        artifact_kind=kind,
        opaque_id=opaque,
        fragment=match.group("fragment"),
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
            private_entries = [PrivateDictEntry(**item) for item in raw_entries]
        except (OSError, ValueError, PydanticValidationError):
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


class RestorationRequest(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid")
    locator: str
    purpose: str


class RestorationResponse(BaseModel, frozen=True):
    """Shape-only restoration response.

    MVP-2 always emits ``outcome="deferred"`` because the real restoration
    flow with audit logging is scoped to #27.
    """

    model_config = ConfigDict(extra="forbid")
    outcome: Literal["deferred"] = "deferred"
    locator: str
    detail: str = "restoration flow deferred to issue #27"


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
    """
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
    except (OSError, ValueError, PydanticValidationError):
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


def restoration_request(
    request: RestorationRequest,
    *,
    vault_root: Path,  # noqa: ARG001  # accepted for future #27 parity
) -> RestorationResponse | ResolverFailure:
    """Shape-only restoration entry point.

    The real flow (audit logging, scope/purpose enforcement, raw-value
    return to PRIVATE_BOUNDARY callers) is scoped to #27. MVP-2 validates
    inputs and returns a structured ``RestorationResponse(outcome="deferred")``
    without invoking :func:`restoration_api.restore`. Malformed locator
    inputs return :class:`ResolverFailure` so callers can branch uniformly.
    """
    parsed = parse_locator(request.locator)
    if parsed is None:
        return ResolverFailure(
            locator=request.locator,
            reason=ResolverFailureReason.MalformedLocator,
            detail="locator does not match the public URI grammar",
        )
    if not request.purpose.strip():
        return ResolverFailure(
            locator=request.locator,
            reason=ResolverFailureReason.PurposeNotPermitted,
            detail="purpose must be a non-empty string",
        )
    return RestorationResponse(locator=request.locator)


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
    "StatusReportRequest",
    "StatusReportResponse",
    # Boundary entry points
    "process_document_request",
    "inspect_request",
    "search_request",
    "restoration_request",
    "status_report_request",
]
