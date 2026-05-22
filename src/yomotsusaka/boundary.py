"""
Boundary — opaque public surface for agent-facing operations.

Frozen URI grammar
(``private://<exposure_class>/<artifact_kind>/<opaque_id>[#<fragment>]``),
:class:`PublicHandle`, :class:`SpanSpec`, plus the fail-closed local
:func:`resolve` contract that maps a locator to private-side state.
Request/response models for the five MVP-2 operations land in the next
commit.
"""

from __future__ import annotations

import json
import re
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from yomotsusaka.redactor import Span
from yomotsusaka.schemas import EntityKind, PrivateDictEntry


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
        raw_entries = json.loads(private_dict_path.read_text(encoding="utf-8"))
        private_entries = [PrivateDictEntry(**item) for item in raw_entries]
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
]
