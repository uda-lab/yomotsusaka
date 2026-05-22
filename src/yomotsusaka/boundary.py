"""
Boundary — opaque public surface for agent-facing operations (locator grammar).

First commit in the #26+#28 series. Introduces the frozen URI grammar
(``private://<exposure_class>/<artifact_kind>/<opaque_id>[#<fragment>]``),
:class:`PublicHandle`, and :class:`SpanSpec`. The resolver contract and
request/response models are added in subsequent commits.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from yomotsusaka.redactor import Span
from yomotsusaka.schemas import EntityKind


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


__all__ = [
    "LOCATOR_SCHEME",
    "EXPOSURE_CLASSES",
    "ARTIFACT_KINDS",
    "ParsedLocator",
    "build_locator",
    "parse_locator",
    "PublicHandle",
    "SpanSpec",
]
