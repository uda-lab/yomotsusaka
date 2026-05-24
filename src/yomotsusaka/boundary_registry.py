"""
Operational trust-boundary field registry (issue #95, MVP-5 child 06).

A compact field-level boundary-crossing registry for the operational MVP
surfaces shipped in MVP-4 and contracted by MVP-5 children 01/02/03. This
module is **observational**: it does not change any behaviour, introduce
any new redaction rule, or invent a new exposure class. The five exposure
classes (``agent_public`` / ``agent_redacted`` / ``private`` /
``restricted`` / ``never_expose``) defined in
:doc:`architecture <../docs/architecture>` §"Capability and exposure
model" are pinned by :data:`yomotsusaka.boundary.EXPOSURE_CLASSES` and
mirrored here only by reuse.

Why a typed Python registry (rather than YAML or decorators)
------------------------------------------------------------

A YAML/TOML registry would require a runtime parser for the drift tests
and would lose ``mypy`` / ``ruff`` coverage over the symbol names. A
decorator-based discovery scheme would risk silent omission (a model that
forgets to register would never fail the drift test). A test-only fixture
would duplicate the source/test split and would not be citable from
``docs/architecture.md``.

The registry is the single source of truth that the failure taxonomy
(issue #93) and the operational report (issue #92, merged) classify by
boundary. Drift tests in :mod:`tests.test_boundary_registry_drift` verify
that:

1. Every :class:`BoundaryField` in :data:`REGISTRY` resolves to a real
   field (Pydantic ``model_fields``, ``dataclass.fields``, or a
   module-level constant).
2. Every public-facing field on an in-scope :class:`pydantic.BaseModel`
   has a registry row (the load-bearing acceptance signal — adding a new
   agent-facing field without registering it must fail CI).
3. ``never_expose`` and ``private`` field values never appear in the
   serialised form of any agent-facing response in
   :data:`EXPECTED_BOUNDARY_SYMBOLS`.

Markdown rendering
------------------

``python -m yomotsusaka.boundary_registry --render-markdown`` prints a
markdown table of the registry to stdout for cross-linking from
``docs/architecture.md``. The render path is **not** CI-gated; it exists
so a docs sweep (e.g. issue #96) can call it deterministically.

Future maintenance
------------------

* Renaming a registered field MUST be paired with the corresponding
  registry update (the drift test will name the offending row).
* Adding a new agent-facing :class:`BaseModel` field to an in-scope
  module MUST add a registry row classifying its exposure (the drift
  test will fail with the missing ``(module, qualname)``).
* Adding a new exposure class is **out of scope** for this registry and
  belongs in a separate docs-architecture issue (the closed five-element
  vocabulary is pinned by :data:`yomotsusaka.boundary.EXPOSURE_CLASSES`).
"""

from __future__ import annotations

import argparse
import sys
from typing import Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from yomotsusaka.boundary import EXPOSURE_CLASSES

__all__ = [
    "BoundaryField",
    "REGISTRY",
    "SCRUB_MECHANISMS",
    "IN_SCOPE_MODULES",
    "iter_registry",
    "render_markdown",
]


# ---------------------------------------------------------------------------
# Scrub-mechanism vocabulary (closed set)
# ---------------------------------------------------------------------------


SCRUB_MECHANISMS: frozenset[str] = frozenset(
    {
        "opaque_locator",
        "opaque_id",
        "redactor_keyed",
        "scrubber",
        "scope_gated_resolver",
        "enum_closed_set",
        "category_literal_only",
        "never_emitted",
        "stripped_at_PublicHandle",
        "hash_only",
        "audit_pre_write_scan",
    }
)
"""Closed vocabulary of values permitted in :attr:`BoundaryField.scrub_mechanism`.

Each token names the mechanism by which the field's value is rendered
public-safe at the boundary crossing:

* ``opaque_locator`` — value is wrapped in the ``private://`` locator
  grammar (only the opaque id portion survives).
* ``opaque_id`` — value is a generator-produced opaque id (UUID4 hex
  or equivalent) with no embedded private content.
* ``redactor_keyed`` — value is the output of
  :func:`yomotsusaka.redactor.redact` keyed by stable redaction keys.
* ``scrubber`` — value passes through
  :func:`yomotsusaka.scrubber.scrub_stream` before emission.
* ``scope_gated_resolver`` — value is only populated when the caller's
  scope (:class:`yomotsusaka.boundary.ResolverScope`) includes the
  private boundary; ``None`` otherwise.
* ``enum_closed_set`` — value is a closed enumeration; the closed set
  is itself classified at the type level.
* ``category_literal_only`` — value is one of a small frozen literal
  set (e.g. ``scripts.manage_runpod.PUBLIC_SAFE_CATEGORIES``).
* ``never_emitted`` — field never crosses the boundary; observable only
  inside the private kernel.
* ``stripped_at_PublicHandle`` — value is dropped during the
  internal-to-public projection (e.g. ``ArtifactHandle.vault_path``
  never appears on the corresponding :class:`PublicHandle`).
* ``hash_only`` — value is exposed as a stable hash rather than the
  raw token (e.g. tenant id hashed for cross-line correlation).
* ``audit_pre_write_scan`` — value is re-run through the scrubber
  before being persisted into the audit log (see
  :func:`yomotsusaka.audit.write_record`).
"""


# ---------------------------------------------------------------------------
# Frozen registry model
# ---------------------------------------------------------------------------


ExposureClass = Literal[
    "agent_public",
    "agent_redacted",
    "private",
    "restricted",
    "never_expose",
]
"""The five exposure classes pinned by
:data:`yomotsusaka.boundary.EXPOSURE_CLASSES`. Mirrored here as a
``Literal`` so a typo on a registry row fails ``mypy`` rather than
silently widening the public surface."""


class BoundaryField(BaseModel):
    """One field's boundary-crossing classification.

    Frozen + ``extra="forbid"`` so future additions are deliberate
    schema migrations rather than ambient drift.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    module: str = Field(
        description="Importable module path, e.g. ``\"yomotsusaka.boundary\"``.",
    )
    qualname: str = Field(
        description="Qualified name within ``module``. For a Pydantic /"
        " dataclass field, ``ClassName.field_name``. For a module-level"
        " constant, the bare attribute name.",
    )
    exposure: ExposureClass = Field(
        description="One of the five classes from"
        " :data:`yomotsusaka.boundary.EXPOSURE_CLASSES`. A typo here"
        " fails ``mypy``.",
    )
    scrub_mechanism: str = Field(
        description="The mechanism by which this field is rendered"
        " public-safe at the boundary. Must be a value of"
        " :data:`SCRUB_MECHANISMS`.",
    )
    note: str = Field(
        max_length=120,
        description="Short descriptive note (≤120 chars) citing the"
        " enforcement point. Not consumed by any drift test.",
    )

    @field_validator("module")
    @classmethod
    def _module_non_empty(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("module must be a non-empty string")
        return value

    @field_validator("qualname")
    @classmethod
    def _qualname_non_empty(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("qualname must be a non-empty string")
        return value

    @field_validator("scrub_mechanism")
    @classmethod
    def _scrub_in_closed_set(cls, value: str) -> str:
        if value not in SCRUB_MECHANISMS:
            raise ValueError(
                f"scrub_mechanism must be one of"
                f" {sorted(SCRUB_MECHANISMS)!r}; got {value!r}"
            )
        return value


# ---------------------------------------------------------------------------
# In-scope module roster
# ---------------------------------------------------------------------------


IN_SCOPE_MODULES: frozenset[str] = frozenset(
    {
        "yomotsusaka.boundary",
        "yomotsusaka.schemas",
        "yomotsusaka.execution_gateway",
        "yomotsusaka.audit",
        "yomotsusaka.policy",
        "yomotsusaka.runpod_lifecycle",
        "yomotsusaka.tenant",
        "yomotsusaka.operational_report",
    }
)
"""Modules whose public-facing fields the registry covers.

This is the closed set the drift tests iterate; private-side helpers,
test utilities, and resolver internals that do not cross a public seam
are deliberately excluded (per issue #95 §Scope)."""


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


REGISTRY: tuple[BoundaryField, ...] = (
    # -- yomotsusaka.boundary -----------------------------------------------
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="PublicHandle.locator",
        exposure="agent_public",
        scrub_mechanism="opaque_locator",
        note="opaque URI; build_locator() enforces grammar",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="ParsedLocator.exposure_class",
        exposure="agent_public",
        scrub_mechanism="enum_closed_set",
        note="member of boundary.EXPOSURE_CLASSES",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="ParsedLocator.artifact_kind",
        exposure="agent_public",
        scrub_mechanism="enum_closed_set",
        note="member of boundary.ARTIFACT_KINDS",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="ParsedLocator.opaque_id",
        exposure="agent_public",
        scrub_mechanism="opaque_id",
        note="OPAQUE_ID charset; mirrors pipeline._validate_doc_id",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="ParsedLocator.fragment",
        exposure="agent_public",
        scrub_mechanism="opaque_id",
        note="optional opaque fragment; FRAGMENT_PATTERN",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="SpanSpec.start",
        exposure="agent_public",
        scrub_mechanism="enum_closed_set",
        note="int >= 0; structural",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="SpanSpec.end",
        exposure="agent_public",
        scrub_mechanism="enum_closed_set",
        note="int >= start; structural",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="SpanSpec.kind",
        exposure="agent_public",
        scrub_mechanism="enum_closed_set",
        note="EntityKind enum",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="PrivateState.manifest_path",
        exposure="private",
        scrub_mechanism="scope_gated_resolver",
        note="only crosses under PRIVATE_BOUNDARY scope",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="PrivateState.private_dict_path",
        exposure="private",
        scrub_mechanism="scope_gated_resolver",
        note="only crosses under PRIVATE_BOUNDARY scope",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="PrivateState.private_entries",
        exposure="private",
        scrub_mechanism="scope_gated_resolver",
        note="materialised PrivateDictEntry list",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="ResolverFailure.outcome",
        exposure="agent_public",
        scrub_mechanism="enum_closed_set",
        note="Literal['failure']",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="ResolverFailure.locator",
        exposure="agent_public",
        scrub_mechanism="opaque_locator",
        note="echo of caller-supplied locator string",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="ResolverFailure.reason",
        exposure="agent_public",
        scrub_mechanism="enum_closed_set",
        note="ResolverFailureReason enum",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="ResolverFailure.detail",
        exposure="agent_redacted",
        scrub_mechanism="scrubber",
        note="docstring forbids raw values / paths / credentials",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="ResolverSuccess.outcome",
        exposure="agent_public",
        scrub_mechanism="enum_closed_set",
        note="Literal['success']",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="ResolverSuccess.locator",
        exposure="agent_public",
        scrub_mechanism="opaque_locator",
        note="echo of caller-supplied locator string",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="ResolverSuccess.exposure_class",
        exposure="agent_public",
        scrub_mechanism="enum_closed_set",
        note="member of boundary.EXPOSURE_CLASSES",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="ResolverSuccess.artifact_kind",
        exposure="agent_public",
        scrub_mechanism="enum_closed_set",
        note="member of boundary.ARTIFACT_KINDS",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="ResolverSuccess.opaque_id",
        exposure="agent_public",
        scrub_mechanism="opaque_id",
        note="generator-controlled opaque id",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="ResolverSuccess.fragment",
        exposure="agent_public",
        scrub_mechanism="opaque_id",
        note="optional opaque fragment",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="ResolverSuccess.purpose",
        exposure="agent_redacted",
        scrub_mechanism="scrubber",
        note="caller-supplied free-form; recorded in audit",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="ResolverSuccess.private_state",
        exposure="restricted",
        scrub_mechanism="scope_gated_resolver",
        note="None for non-PRIVATE_BOUNDARY scopes",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="PublicManifestView.doc_id",
        exposure="agent_public",
        scrub_mechanism="opaque_id",
        note="UUID4 hex; opaque id charset",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="PublicManifestView.redacted_text",
        exposure="agent_redacted",
        scrub_mechanism="redactor_keyed",
        note="redactor strips raw values, keys stable",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="PublicManifestView.entities",
        exposure="agent_redacted",
        scrub_mechanism="redactor_keyed",
        note="EntityRecord list; redacted_key only",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="PublicManifestView.labels",
        exposure="agent_public",
        scrub_mechanism="scrubber",
        note="caller-supplied labels (already public-safe)",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="PublicManifestView.summary",
        exposure="agent_redacted",
        scrub_mechanism="scrubber",
        note="pipeline-generated; redactor sweep applies",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="PublicManifestView.created_at",
        exposure="agent_public",
        scrub_mechanism="opaque_id",
        note="timezone-aware UTC timestamp",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="PublicManifestView.metadata",
        exposure="agent_redacted",
        scrub_mechanism="scrubber",
        note="dict[str, Any]; scrubber sweep at emission",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="ProcessRequest.doc_id",
        exposure="agent_public",
        scrub_mechanism="opaque_id",
        note="validated against opaque-id charset",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="ProcessRequest.raw_text",
        exposure="private",
        scrub_mechanism="never_emitted",
        note="caller-side private input; never echoed back",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="ProcessRequest.spans",
        exposure="agent_public",
        scrub_mechanism="enum_closed_set",
        note="SpanSpec list; integer offsets only",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="ProcessResponse.handle",
        exposure="agent_public",
        scrub_mechanism="opaque_locator",
        note="PublicHandle wrapper",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="InspectRequest.locator",
        exposure="agent_public",
        scrub_mechanism="opaque_locator",
        note="caller-supplied locator",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="InspectRequest.purpose",
        exposure="agent_redacted",
        scrub_mechanism="scrubber",
        note="free-form; forwarded to resolve()",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="InspectResponse.manifest",
        exposure="agent_redacted",
        scrub_mechanism="redactor_keyed",
        note="PublicManifestView projection",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="SearchRequest.query",
        exposure="private",
        scrub_mechanism="never_emitted",
        note="raw query may contain private terms; never echoed",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="SearchRequest.top_k",
        exposure="agent_public",
        scrub_mechanism="enum_closed_set",
        note="bounded int [1, 1000]",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="SearchHit.handle",
        exposure="agent_public",
        scrub_mechanism="opaque_locator",
        note="PublicHandle wrapper",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="SearchHit.redacted_snippet",
        exposure="agent_redacted",
        scrub_mechanism="redactor_keyed",
        note="window of redacted_text only",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="SearchHit.labels",
        exposure="agent_public",
        scrub_mechanism="scrubber",
        note="manifest labels",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="SearchResponse.hits",
        exposure="agent_redacted",
        scrub_mechanism="redactor_keyed",
        note="list of SearchHit",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="RestorationRequest.caller_label",
        exposure="agent_redacted",
        scrub_mechanism="scrubber",
        note="caller-supplied identity label",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="RestorationRequest.target_public_handle",
        exposure="agent_public",
        scrub_mechanism="opaque_locator",
        note="PublicHandle wrapper",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="RestorationRequest.document_id",
        exposure="agent_public",
        scrub_mechanism="opaque_id",
        note="opaque-id charset enforced at validator",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="RestorationRequest.requested_keys",
        exposure="agent_redacted",
        scrub_mechanism="redactor_keyed",
        note="redacted keys, not raw values",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="RestorationRequest.requested_entity_kinds",
        exposure="agent_public",
        scrub_mechanism="enum_closed_set",
        note="EntityKind enum",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="RestorationRequest.reason",
        exposure="agent_redacted",
        scrub_mechanism="scrubber",
        note="free-form purpose; persisted to audit",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="RestorationRequest.timestamp",
        exposure="agent_public",
        scrub_mechanism="opaque_id",
        note="timezone-aware UTC timestamp",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="RestorationRequest.authorization_decision",
        exposure="agent_public",
        scrub_mechanism="enum_closed_set",
        note="Literal['accept'] | None",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="RestorationRequest.policy_profile",
        exposure="restricted",
        scrub_mechanism="never_emitted",
        note="operator-supplied profile name; never echoed",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="RestorationRequest.approval_ticket",
        exposure="restricted",
        scrub_mechanism="never_emitted",
        note="operator-supplied approval token; never echoed",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="RestorationRequest.production_scope",
        exposure="restricted",
        scrub_mechanism="never_emitted",
        note="operator-supplied scope label; never echoed",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="RestorationResponse.outcome",
        exposure="agent_public",
        scrub_mechanism="enum_closed_set",
        note="closed Literal set",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="RestorationResponse.audit_record_id",
        exposure="agent_public",
        scrub_mechanism="opaque_id",
        note="UUID4 hex; correlates to vault audit",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="RestorationResponse.document_id",
        exposure="agent_public",
        scrub_mechanism="opaque_id",
        note="opaque doc id, optional",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="RestorationResponse.reason",
        exposure="agent_public",
        scrub_mechanism="enum_closed_set",
        note="RestorationFailureReason enum",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="RestorationResponse.detail",
        exposure="agent_redacted",
        scrub_mechanism="scrubber",
        note="vault_root substrings stripped",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="RestorationResponse.private_entries",
        exposure="restricted",
        scrub_mechanism="scope_gated_resolver",
        note="None unless caller is PRIVATE_BOUNDARY",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="StatusReportRequest.locator",
        exposure="agent_public",
        scrub_mechanism="opaque_locator",
        note="caller-supplied locator",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="StatusReportResponse.locator",
        exposure="agent_public",
        scrub_mechanism="opaque_locator",
        note="echo of request locator",
    ),
    BoundaryField(
        module="yomotsusaka.boundary",
        qualname="StatusReportResponse.status",
        exposure="agent_public",
        scrub_mechanism="enum_closed_set",
        note="Literal['committed', 'unknown']",
    ),
    # -- yomotsusaka.schemas ------------------------------------------------
    BoundaryField(
        module="yomotsusaka.schemas",
        qualname="EntityRecord.entity_id",
        exposure="agent_public",
        scrub_mechanism="opaque_id",
        note="UUID4 hex",
    ),
    BoundaryField(
        module="yomotsusaka.schemas",
        qualname="EntityRecord.kind",
        exposure="agent_public",
        scrub_mechanism="enum_closed_set",
        note="EntityKind enum",
    ),
    BoundaryField(
        module="yomotsusaka.schemas",
        qualname="EntityRecord.redacted_key",
        exposure="agent_redacted",
        scrub_mechanism="redactor_keyed",
        note="stable redaction key; never the raw value",
    ),
    BoundaryField(
        module="yomotsusaka.schemas",
        qualname="EntityRecord.start_char",
        exposure="agent_public",
        scrub_mechanism="enum_closed_set",
        note="int >= 0",
    ),
    BoundaryField(
        module="yomotsusaka.schemas",
        qualname="EntityRecord.end_char",
        exposure="agent_public",
        scrub_mechanism="enum_closed_set",
        note="int >= 0",
    ),
    BoundaryField(
        module="yomotsusaka.schemas",
        qualname="EntityRecord.confidence",
        exposure="agent_public",
        scrub_mechanism="enum_closed_set",
        note="float [0.0, 1.0]",
    ),
    BoundaryField(
        module="yomotsusaka.schemas",
        qualname="PrivateDictEntry.key",
        exposure="agent_redacted",
        scrub_mechanism="redactor_keyed",
        note="redacted-key side of the private dict mapping",
    ),
    BoundaryField(
        module="yomotsusaka.schemas",
        qualname="PrivateDictEntry.original_value",
        exposure="private",
        scrub_mechanism="never_emitted",
        note="raw private value; never crosses public boundary",
    ),
    BoundaryField(
        module="yomotsusaka.schemas",
        qualname="PrivateDictEntry.kind",
        exposure="agent_public",
        scrub_mechanism="enum_closed_set",
        note="EntityKind enum",
    ),
    BoundaryField(
        module="yomotsusaka.schemas",
        qualname="PrivateDictEntry.created_at",
        exposure="agent_public",
        scrub_mechanism="opaque_id",
        note="timezone-aware UTC timestamp",
    ),
    BoundaryField(
        module="yomotsusaka.schemas",
        qualname="DocumentManifest.doc_id",
        exposure="agent_public",
        scrub_mechanism="opaque_id",
        note="UUID4 hex; opaque",
    ),
    BoundaryField(
        module="yomotsusaka.schemas",
        qualname="DocumentManifest.source_ref",
        exposure="agent_public",
        scrub_mechanism="opaque_id",
        note="opaque correlation key (alias of doc_id in MVP-1)",
    ),
    BoundaryField(
        module="yomotsusaka.schemas",
        qualname="DocumentManifest.redacted_text",
        exposure="agent_redacted",
        scrub_mechanism="redactor_keyed",
        note="redactor output; PublicManifestView passes through",
    ),
    BoundaryField(
        module="yomotsusaka.schemas",
        qualname="DocumentManifest.entities",
        exposure="agent_redacted",
        scrub_mechanism="redactor_keyed",
        note="EntityRecord list",
    ),
    BoundaryField(
        module="yomotsusaka.schemas",
        qualname="DocumentManifest.labels",
        exposure="agent_public",
        scrub_mechanism="scrubber",
        note="caller-supplied labels",
    ),
    BoundaryField(
        module="yomotsusaka.schemas",
        qualname="DocumentManifest.summary",
        exposure="agent_redacted",
        scrub_mechanism="scrubber",
        note="pipeline-generated summary",
    ),
    BoundaryField(
        module="yomotsusaka.schemas",
        qualname="DocumentManifest.created_at",
        exposure="agent_public",
        scrub_mechanism="opaque_id",
        note="timezone-aware UTC timestamp",
    ),
    BoundaryField(
        module="yomotsusaka.schemas",
        qualname="DocumentManifest.metadata",
        exposure="agent_redacted",
        scrub_mechanism="scrubber",
        note="dict[str, Any]; scrubber sweep at emission",
    ),
    BoundaryField(
        module="yomotsusaka.schemas",
        qualname="ArtifactHandle.handle_id",
        exposure="agent_public",
        scrub_mechanism="opaque_id",
        note="UUID4 hex",
    ),
    BoundaryField(
        module="yomotsusaka.schemas",
        qualname="ArtifactHandle.doc_id",
        exposure="agent_public",
        scrub_mechanism="opaque_id",
        note="opaque doc id",
    ),
    BoundaryField(
        module="yomotsusaka.schemas",
        qualname="ArtifactHandle.vault_path",
        exposure="never_expose",
        scrub_mechanism="stripped_at_PublicHandle",
        note="internal path dropped by PublicHandle projection",
    ),
    BoundaryField(
        module="yomotsusaka.schemas",
        qualname="BatchState.batch_id",
        exposure="agent_public",
        scrub_mechanism="opaque_id",
        note="UUID4 hex",
    ),
    BoundaryField(
        module="yomotsusaka.schemas",
        qualname="BatchState.status",
        exposure="agent_public",
        scrub_mechanism="enum_closed_set",
        note="BatchStatus enum",
    ),
    BoundaryField(
        module="yomotsusaka.schemas",
        qualname="BatchState.doc_refs",
        exposure="agent_public",
        scrub_mechanism="opaque_id",
        note="caller-controlled opaque references",
    ),
    BoundaryField(
        module="yomotsusaka.schemas",
        qualname="BatchState.manifests",
        exposure="agent_redacted",
        scrub_mechanism="redactor_keyed",
        note="DocumentManifest list (redacted side)",
    ),
    BoundaryField(
        module="yomotsusaka.schemas",
        qualname="BatchState.errors",
        exposure="agent_redacted",
        scrub_mechanism="scrubber",
        note="scrubber MUST run pre-emission on each entry",
    ),
    BoundaryField(
        module="yomotsusaka.schemas",
        qualname="BatchState.started_at",
        exposure="agent_public",
        scrub_mechanism="opaque_id",
        note="optional timezone-aware UTC timestamp",
    ),
    BoundaryField(
        module="yomotsusaka.schemas",
        qualname="BatchState.finished_at",
        exposure="agent_public",
        scrub_mechanism="opaque_id",
        note="optional timezone-aware UTC timestamp",
    ),
    # -- yomotsusaka.execution_gateway --------------------------------------
    BoundaryField(
        module="yomotsusaka.execution_gateway",
        qualname="ExecutionRequest.job_name",
        exposure="agent_public",
        scrub_mechanism="enum_closed_set",
        note="registered template-job name",
    ),
    BoundaryField(
        module="yomotsusaka.execution_gateway",
        qualname="ExecutionRequest.purpose",
        exposure="agent_redacted",
        scrub_mechanism="scrubber",
        note="caller-supplied free-form; persisted to audit",
    ),
    BoundaryField(
        module="yomotsusaka.execution_gateway",
        qualname="ExecutionRequest.scope",
        exposure="agent_public",
        scrub_mechanism="enum_closed_set",
        note="ExecutionScope enum",
    ),
    BoundaryField(
        module="yomotsusaka.execution_gateway",
        qualname="ExecutionRequest.inputs",
        exposure="agent_redacted",
        scrub_mechanism="scrubber",
        note="opaque payloads; dispatcher type-checks per template",
    ),
    BoundaryField(
        module="yomotsusaka.execution_gateway",
        qualname="ExecutionResponse.audit_record_id",
        exposure="agent_public",
        scrub_mechanism="opaque_id",
        note="correlation id to audit log",
    ),
    BoundaryField(
        module="yomotsusaka.execution_gateway",
        qualname="ExecutionResponse.status",
        exposure="agent_public",
        scrub_mechanism="enum_closed_set",
        note="closed status string set",
    ),
    BoundaryField(
        module="yomotsusaka.execution_gateway",
        qualname="ExecutionResponse.artifacts",
        exposure="agent_public",
        scrub_mechanism="opaque_locator",
        note="list of PublicHandle",
    ),
    BoundaryField(
        module="yomotsusaka.execution_gateway",
        qualname="ExecutionResponse.scrubbed_stdout",
        exposure="agent_redacted",
        scrub_mechanism="scrubber",
        note="scrubber output; never raw stdout",
    ),
    BoundaryField(
        module="yomotsusaka.execution_gateway",
        qualname="ExecutionResponse.scrubbed_stderr",
        exposure="agent_redacted",
        scrub_mechanism="scrubber",
        note="scrubber output; never raw stderr",
    ),
    BoundaryField(
        module="yomotsusaka.execution_gateway",
        qualname="ExecutionResponse.reason",
        exposure="agent_public",
        scrub_mechanism="enum_closed_set",
        note="ExecutionFailureReason enum or None",
    ),
    BoundaryField(
        module="yomotsusaka.execution_gateway",
        qualname="ExecutionResponse.detail",
        exposure="agent_redacted",
        scrub_mechanism="scrubber",
        note="failure detail; scrubbed of raw values and paths",
    ),
    BoundaryField(
        module="yomotsusaka.execution_gateway",
        qualname="ExecutionFailureReason",
        exposure="agent_public",
        scrub_mechanism="enum_closed_set",
        note="closed failure enum (module-level)",
    ),
    BoundaryField(
        module="yomotsusaka.execution_gateway",
        qualname="ExecutionScope",
        exposure="agent_public",
        scrub_mechanism="enum_closed_set",
        note="closed scope enum (module-level)",
    ),
    # -- yomotsusaka.audit --------------------------------------------------
    BoundaryField(
        module="yomotsusaka.audit",
        qualname="AuditRecord.ts",
        exposure="agent_public",
        scrub_mechanism="opaque_id",
        note="timezone-aware UTC timestamp",
    ),
    BoundaryField(
        module="yomotsusaka.audit",
        qualname="AuditRecord.request_id",
        exposure="agent_public",
        scrub_mechanism="opaque_id",
        note="UUID4 hex; echoes into ExecutionResponse",
    ),
    BoundaryField(
        module="yomotsusaka.audit",
        qualname="AuditRecord.template_name",
        exposure="agent_public",
        scrub_mechanism="enum_closed_set",
        note="registered template name",
    ),
    BoundaryField(
        module="yomotsusaka.audit",
        qualname="AuditRecord.caller_scope",
        exposure="agent_public",
        scrub_mechanism="enum_closed_set",
        note="ExecutionScope value (stringified)",
    ),
    BoundaryField(
        module="yomotsusaka.audit",
        qualname="AuditRecord.purpose",
        exposure="agent_redacted",
        scrub_mechanism="audit_pre_write_scan",
        note="re-scrubbed via write_record() pre-write check",
    ),
    BoundaryField(
        module="yomotsusaka.audit",
        qualname="AuditRecord.locator",
        exposure="agent_public",
        scrub_mechanism="opaque_locator",
        note="public locator or empty string",
    ),
    BoundaryField(
        module="yomotsusaka.audit",
        qualname="AuditRecord.outcome",
        exposure="agent_public",
        scrub_mechanism="enum_closed_set",
        note="AuditOutcome closed Literal set",
    ),
    BoundaryField(
        module="yomotsusaka.audit",
        qualname="AuditRecord.artifact_locators",
        exposure="agent_public",
        scrub_mechanism="opaque_locator",
        note="list of opaque locators",
    ),
    BoundaryField(
        module="yomotsusaka.audit",
        qualname="AuditRecord.resolver_reason",
        exposure="agent_public",
        scrub_mechanism="enum_closed_set",
        note="ResolverFailureReason value or None",
    ),
    BoundaryField(
        module="yomotsusaka.audit",
        qualname="AuditRecord.detail",
        exposure="agent_redacted",
        scrub_mechanism="audit_pre_write_scan",
        note="re-scrubbed via write_record() pre-write check",
    ),
    BoundaryField(
        module="yomotsusaka.audit",
        qualname="AuditRecord.policy_profile",
        exposure="restricted",
        scrub_mechanism="never_emitted",
        note="reserved field; always None in current writer",
    ),
    BoundaryField(
        module="yomotsusaka.audit",
        qualname="AuditRecord.approval_ticket",
        exposure="restricted",
        scrub_mechanism="never_emitted",
        note="reserved field; always None in current writer",
    ),
    # -- yomotsusaka.policy -------------------------------------------------
    BoundaryField(
        module="yomotsusaka.policy",
        qualname="PolicyDecision.verdict",
        exposure="agent_public",
        scrub_mechanism="enum_closed_set",
        note="Literal['permit', 'deny']",
    ),
    BoundaryField(
        module="yomotsusaka.policy",
        qualname="PolicyDecision.matched_profile",
        exposure="agent_public",
        scrub_mechanism="enum_closed_set",
        note="profile name from a registered row",
    ),
    BoundaryField(
        module="yomotsusaka.policy",
        qualname="PolicyDecision.deny_reason",
        exposure="agent_public",
        scrub_mechanism="enum_closed_set",
        note="closed deny-reason string vocabulary",
    ),
    BoundaryField(
        module="yomotsusaka.policy",
        qualname="RestorationPolicyRow.profile_name",
        exposure="restricted",
        scrub_mechanism="never_emitted",
        note="operator-supplied profile id; private",
    ),
    BoundaryField(
        module="yomotsusaka.policy",
        qualname="RestorationPolicyRow.production_scopes",
        exposure="restricted",
        scrub_mechanism="never_emitted",
        note="operator-supplied scope list; private",
    ),
    BoundaryField(
        module="yomotsusaka.policy",
        qualname="RestorationPolicyRow.require_authorization_decision",
        exposure="restricted",
        scrub_mechanism="never_emitted",
        note="operator-supplied policy flag; private",
    ),
    BoundaryField(
        module="yomotsusaka.policy",
        qualname="RestorationPolicyRow.approval_ticket_pattern",
        exposure="restricted",
        scrub_mechanism="never_emitted",
        note="operator-supplied regex; never echoed",
    ),
    BoundaryField(
        module="yomotsusaka.policy",
        qualname="RestorationPolicyRow.default",
        exposure="restricted",
        scrub_mechanism="never_emitted",
        note="operator-supplied default-row flag; private",
    ),
    # -- yomotsusaka.runpod_lifecycle --------------------------------------
    BoundaryField(
        module="yomotsusaka.runpod_lifecycle",
        qualname="PodHandle.pod_id",
        exposure="never_expose",
        scrub_mechanism="never_emitted",
        note="vault-side identifier; never crosses",
    ),
    BoundaryField(
        module="yomotsusaka.runpod_lifecycle",
        qualname="PodHandle.endpoint",
        exposure="never_expose",
        scrub_mechanism="never_emitted",
        note="vault-side endpoint URL; never crosses",
    ),
    BoundaryField(
        module="yomotsusaka.runpod_lifecycle",
        qualname="PodConfig.gpu_type",
        exposure="private",
        scrub_mechanism="never_emitted",
        note="operator-supplied config; vault-side",
    ),
    BoundaryField(
        module="yomotsusaka.runpod_lifecycle",
        qualname="PodConfig.image",
        exposure="private",
        scrub_mechanism="never_emitted",
        note="operator-supplied config; vault-side",
    ),
    BoundaryField(
        module="yomotsusaka.runpod_lifecycle",
        qualname="PodConfig.model_id",
        exposure="private",
        scrub_mechanism="never_emitted",
        note="operator-supplied config; vault-side",
    ),
    BoundaryField(
        module="yomotsusaka.runpod_lifecycle",
        qualname="PodConfig.disk_gb",
        exposure="private",
        scrub_mechanism="never_emitted",
        note="operator-supplied config; vault-side",
    ),
    BoundaryField(
        module="yomotsusaka.runpod_lifecycle",
        qualname="PodConfig.extra",
        exposure="private",
        scrub_mechanism="never_emitted",
        note="operator-supplied config; vault-side",
    ),
    # -- yomotsusaka.tenant -------------------------------------------------
    BoundaryField(
        module="yomotsusaka.tenant",
        qualname="TenantScope.tenant_id",
        exposure="agent_redacted",
        scrub_mechanism="hash_only",
        note="tenant identity hashed for cross-line correlation",
    ),
    BoundaryField(
        module="yomotsusaka.tenant",
        qualname="TenantScope.vault_root",
        exposure="never_expose",
        scrub_mechanism="stripped_at_PublicHandle",
        note="absolute path; never reaches public response",
    ),
    # -- yomotsusaka.operational_report (child 03 / #92) -------------------
    BoundaryField(
        module="yomotsusaka.operational_report",
        qualname="PhaseRecord.phase_name",
        exposure="agent_public",
        scrub_mechanism="category_literal_only",
        note="caller-controlled phase token; public-safe by contract",
    ),
    BoundaryField(
        module="yomotsusaka.operational_report",
        qualname="PhaseRecord.status",
        exposure="agent_public",
        scrub_mechanism="enum_closed_set",
        note="PhaseStatus Literal vocabulary",
    ),
    BoundaryField(
        module="yomotsusaka.operational_report",
        qualname="PhaseRecord.category",
        exposure="agent_public",
        scrub_mechanism="category_literal_only",
        note="stable category token; public-safe by contract",
    ),
    BoundaryField(
        module="yomotsusaka.operational_report",
        qualname="ScenarioResult.phases",
        exposure="agent_public",
        scrub_mechanism="enum_closed_set",
        note="tuple of PhaseRecord",
    ),
    BoundaryField(
        module="yomotsusaka.operational_report",
        qualname="ScenarioResult.counters",
        exposure="agent_public",
        scrub_mechanism="category_literal_only",
        note="public-safe int/bool/short-string counters",
    ),
    # -- scripts.manage_runpod (public-safe category literals; #95) -------
    # The PUBLIC_SAFE_CATEGORIES frozen set in scripts/manage_runpod.py is
    # the canonical roster of stdout lifecycle categories. It is iterated
    # here at module load so a rename of any single _CATEGORY_* constant
    # does not silently fall out of the registry.
    BoundaryField(
        module="scripts.manage_runpod",
        qualname="PUBLIC_SAFE_CATEGORIES",
        exposure="agent_public",
        scrub_mechanism="category_literal_only",
        note="frozenset of public-safe lifecycle category literals",
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def iter_registry() -> Iterable[BoundaryField]:
    """Iterate :data:`REGISTRY` in declaration order.

    Provided as a stable helper so callers do not import the tuple
    directly (and so a future change to the underlying container does
    not break consumers).
    """
    return iter(REGISTRY)


def render_markdown() -> str:
    """Render :data:`REGISTRY` as a markdown table.

    The table has four columns: ``Module``, ``Qualname``, ``Exposure``,
    ``Scrub mechanism``. The optional ``note`` is rendered as a trailing
    column. Output is deterministic across calls (registry tuple order).
    """
    lines: list[str] = [
        "| Module | Qualname | Exposure | Scrub mechanism | Note |",
        "| --- | --- | --- | --- | --- |",
    ]
    for entry in REGISTRY:
        lines.append(
            f"| `{entry.module}` | `{entry.qualname}` |"
            f" `{entry.exposure}` | `{entry.scrub_mechanism}` |"
            f" {entry.note} |"
        )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Module-load invariants
# ---------------------------------------------------------------------------


# Pin the exposure-class vocabulary by reuse: a typo on the Literal type
# above is caught by mypy, and a runtime mismatch with boundary.* is caught
# here. This is belt-and-suspenders — the Literal already enforces the
# closed set at construction.
_REGISTRY_EXPOSURE_VALUES: frozenset[str] = frozenset(
    entry.exposure for entry in REGISTRY
)
assert _REGISTRY_EXPOSURE_VALUES <= EXPOSURE_CLASSES, (
    "boundary_registry.REGISTRY uses an exposure value not in"
    f" boundary.EXPOSURE_CLASSES: extras="
    f"{sorted(_REGISTRY_EXPOSURE_VALUES - EXPOSURE_CLASSES)!r}"
)


# ---------------------------------------------------------------------------
# CLI entry point — `python -m yomotsusaka.boundary_registry --render-markdown`
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    """Render the registry as markdown to stdout when ``--render-markdown``
    is passed. Exit code 0 on success, 2 on argument error.
    """
    parser = argparse.ArgumentParser(
        prog="python -m yomotsusaka.boundary_registry",
        description="Boundary-field registry helper (issue #95).",
    )
    parser.add_argument(
        "--render-markdown",
        action="store_true",
        help="Print the registry as a markdown table to stdout.",
    )
    args = parser.parse_args(argv)
    if args.render_markdown:
        sys.stdout.write(render_markdown())
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover - trivial CLI shim
    raise SystemExit(_main())
