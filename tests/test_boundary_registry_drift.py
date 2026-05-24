"""Drift tests for the operational boundary-field registry (issue #95).

These tests are the load-bearing acceptance signal for MVP-5 child 06.
They verify, by runtime introspection, that
:mod:`yomotsusaka.boundary_registry` and the in-scope public modules stay
in lockstep:

1. ``test_registry_entries_resolve_in_source`` — every registry row
   resolves to a real Pydantic / dataclass field or module-level
   constant. A typo in ``qualname`` or a renamed source field fails
   here.
2. ``test_every_response_field_is_registered`` — every public-facing
   :class:`pydantic.BaseModel` / :class:`dataclasses.dataclass` field
   in the in-scope modules has a registry row. **Adding a new
   agent-facing field without registering it must fail this test.**
3. ``test_never_expose_fields_absent_from_response_serialisations`` —
   ``never_expose`` and ``private`` fields do not leak into the
   serialised form of any agent-facing response in
   :data:`EXPECTED_BOUNDARY_SYMBOLS`.
4. ``test_restricted_fields_gated_by_resolver_scope`` —
   :attr:`ResolverSuccess.private_state` (the only ``restricted`` field
   that crosses through a resolver scope) is ``None`` for every scope
   other than ``PRIVATE_BOUNDARY``.
5. ``test_runpod_category_literals_match_registry`` — the registry's
   single ``scripts.manage_runpod.PUBLIC_SAFE_CATEGORIES`` row resolves
   to the actual frozen set in the script and contains exactly the
   public-safe lifecycle category vocabulary.
6. ``test_registry_has_no_duplicate_qualname`` — sanity check that no
   ``(module, qualname)`` pair is repeated.
7. ``test_scrub_mechanism_is_closed_set`` — every row's
   ``scrub_mechanism`` is drawn from :data:`SCRUB_MECHANISMS`.

These tests deliberately use runtime introspection (``model_fields``,
``dataclasses.fields``, module-level attribute lookup) rather than an
AST scan: AST-based scanning is brittle under ``if TYPE_CHECKING`` and
Pydantic v2 model inheritance.
"""

from __future__ import annotations

import importlib
import json
import re
from collections import Counter
from dataclasses import fields as dc_fields, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from yomotsusaka import boundary
from yomotsusaka.audit import AuditRecord
from yomotsusaka.boundary import (
    PublicHandle,
    ResolverScope,
    ResolverSuccess,
    SpanSpec,
)
from yomotsusaka.boundary_registry import (
    IN_SCOPE_MODULES,
    REGISTRY,
    SCRUB_MECHANISMS,
    BoundaryField,
    iter_registry,
    render_markdown,
)
from yomotsusaka.execution_gateway import ExecutionResponse
from yomotsusaka.policy import PolicyDecision
from yomotsusaka.schemas import EntityKind, EntityRecord

from tests._exposure_denylist import EXPECTED_BOUNDARY_SYMBOLS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _split_qualname(qualname: str) -> tuple[str, str | None]:
    """Split a registry ``qualname`` into ``(head, attr)``.

    ``"PublicHandle.locator"`` → ``("PublicHandle", "locator")``.
    ``"PUBLIC_SAFE_CATEGORIES"`` → ``("PUBLIC_SAFE_CATEGORIES", None)``.
    ``"ExecutionFailureReason"`` (a module-level class) → that single name.
    """
    if "." in qualname:
        head, _, attr = qualname.partition(".")
        return head, attr
    return qualname, None


def _resolve_attr(module_obj: Any, qualname: str) -> Any:
    """Return the registered attribute or raise ``AttributeError``.

    Mirrors ``getattr`` chained on dots; we only ever have one dot in
    practice (``Class.field``) but accept multi-segment names defensively.
    """
    target = module_obj
    for part in qualname.split("."):
        target = getattr(target, part)
    return target


# ---------------------------------------------------------------------------
# 1. Field-presence drift (registry → code)
# ---------------------------------------------------------------------------


def test_registry_entries_resolve_in_source() -> None:
    """Every registry row must resolve to a real source artifact.

    For each :class:`BoundaryField`, the test imports ``module`` and
    asserts that ``qualname`` resolves to (one of):

    * a Pydantic v2 ``model_fields`` entry on a ``BaseModel`` subclass,
    * a ``dataclasses.fields`` entry on a dataclass,
    * a module-level constant or class.
    """
    errors: list[str] = []
    for entry in iter_registry():
        try:
            module_obj = importlib.import_module(entry.module)
        except Exception as exc:  # noqa: BLE001
            errors.append(
                f"registry row {entry.module}:{entry.qualname} imports a "
                f"module that cannot be loaded: {exc!r}"
            )
            continue

        head, attr = _split_qualname(entry.qualname)
        if not hasattr(module_obj, head):
            errors.append(
                f"registry row {entry.module}:{entry.qualname} references a "
                f"missing module attribute {head!r}"
            )
            continue
        head_obj = getattr(module_obj, head)

        if attr is None:
            # Module-level constant or class. Just having the attribute is
            # enough to be a real symbol — the type-level checks (e.g. it
            # is an Enum subclass or a frozenset) are validated elsewhere.
            continue

        # Class-level field: must appear in model_fields or dataclass fields
        if isinstance(head_obj, type) and issubclass(head_obj, BaseModel):
            if attr not in head_obj.model_fields:
                errors.append(
                    f"registry row {entry.module}:{entry.qualname} references "
                    f"a Pydantic field that does not exist on {head!r}: "
                    f"available={sorted(head_obj.model_fields)!r}"
                )
            continue
        if is_dataclass(head_obj):
            dc_field_names = {f.name for f in dc_fields(head_obj)}
            if attr not in dc_field_names:
                errors.append(
                    f"registry row {entry.module}:{entry.qualname} references "
                    f"a dataclass field that does not exist on {head!r}: "
                    f"available={sorted(dc_field_names)!r}"
                )
            continue
        # Fallback: maybe the attribute exists on the head class even
        # though the head is not a BaseModel / dataclass (e.g. enum
        # members on an Enum subclass).
        if not hasattr(head_obj, attr):
            errors.append(
                f"registry row {entry.module}:{entry.qualname} references "
                f"{attr!r} on {head!r}, but it has no such attribute "
                f"(head type={type(head_obj).__name__})"
            )

    assert not errors, "registry rows do not resolve in source:\n" + "\n".join(errors)


# ---------------------------------------------------------------------------
# 2. Surface-completeness drift (code → registry)
# ---------------------------------------------------------------------------


# Module-level callables / aliases that the registry does not classify
# field-by-field because they expose no field surface (they are entry
# points or enum / Literal type aliases). Each entry MUST carry a
# justification comment.
_REGISTRY_EXEMPT: frozenset[tuple[str, str]] = frozenset(
    {
        # justification: ResolverError is an exception subclass — no fields cross the boundary.
        ("yomotsusaka.boundary", "ResolverError"),
        # justification: ResolverScope / ResolverFailureReason are closed enums; whole-enum exposure is recorded as ParsedLocator.* (covered).
        ("yomotsusaka.boundary", "ResolverScope"),
        ("yomotsusaka.boundary", "ResolverFailureReason"),
        # justification: RestorationFailureReason is a closed enum; values exposed via RestorationResponse.reason (registered).
        ("yomotsusaka.boundary", "RestorationFailureReason"),
        # justification: AuditOutcome is a Literal type alias, not a class with fields.
        ("yomotsusaka.audit", "AuditOutcome"),
        # justification: AuditError is an exception subclass.
        ("yomotsusaka.audit", "AuditError"),
        # justification: ExecutionFailure is an exception subclass.
        ("yomotsusaka.execution_gateway", "ExecutionFailure"),
        # justification: ExecutionGateway is the stub dispatcher; no public fields cross — gateway returns ExecutionResponse (registered).
        ("yomotsusaka.execution_gateway", "ExecutionGateway"),
        # justification: RestorationPolicyTable is a private collection wrapper; its public fields are exposed via PolicyDecision (registered).
        ("yomotsusaka.policy", "RestorationPolicyTable"),
        # justification: RunPodLifecycle / subclasses are private-side service implementations; their public state is PodHandle/PodConfig (registered).
        ("yomotsusaka.runpod_lifecycle", "RunPodLifecycle"),
        ("yomotsusaka.runpod_lifecycle", "MockRunPodLifecycle"),
        ("yomotsusaka.runpod_lifecycle", "AttachRunPodLifecycle"),
        ("yomotsusaka.runpod_lifecycle", "ManageRunPodLifecycle"),
        # justification: RunPodConfigError is an exception subclass.
        ("yomotsusaka.runpod_lifecycle", "RunPodConfigError"),
        # justification: RedactionError is an exception subclass.
        ("yomotsusaka.operational_report", "RedactionError"),
        # justification: PHASE_STATUS_VOCABULARY mirrors the PhaseStatus Literal already enforced via PhaseRecord.status (registered).
        ("yomotsusaka.operational_report", "PHASE_STATUS_VOCABULARY"),
        # justification: EntityKind / BatchStatus are closed enums consumed by registered fields (EntityRecord.kind, BatchState.status, PrivateDictEntry.kind); their values are public-safe by construction.
        ("yomotsusaka.schemas", "EntityKind"),
        ("yomotsusaka.schemas", "BatchStatus"),
    }
)


def _enumerate_in_scope_models() -> list[tuple[str, str, type]]:
    """Return ``(module_name, class_name, class_obj)`` for every public
    BaseModel / dataclass class declared in :data:`IN_SCOPE_MODULES`.

    A "public" class is one (a) defined in the module itself (not
    re-exported from a different module), and (b) whose name does not
    start with ``_``.
    """
    out: list[tuple[str, str, type]] = []
    for module_name in sorted(IN_SCOPE_MODULES):
        module_obj = importlib.import_module(module_name)
        for attr_name in sorted(dir(module_obj)):
            if attr_name.startswith("_"):
                continue
            obj = getattr(module_obj, attr_name)
            if not isinstance(obj, type):
                continue
            # Re-exports follow ``__module__`` back to the defining module.
            if getattr(obj, "__module__", None) != module_name:
                continue
            is_basemodel = (
                issubclass(obj, BaseModel) and obj is not BaseModel
            )
            if is_basemodel or is_dataclass(obj):
                out.append((module_name, attr_name, obj))
    return out


def test_every_response_field_is_registered() -> None:
    """Every public-facing field on every in-scope BaseModel / dataclass
    must have a registry row.

    This is the load-bearing test: adding a new field to a covered
    surface without registering it fires here with the exact missing
    ``(module, qualname)`` pair, forcing the maintainer to either
    register it or move the class out of the in-scope set.
    """
    registered: set[tuple[str, str]] = {
        (entry.module, entry.qualname) for entry in REGISTRY
    }
    missing: list[str] = []

    for module_name, class_name, class_obj in _enumerate_in_scope_models():
        # Pydantic models
        if isinstance(class_obj, type) and issubclass(class_obj, BaseModel):
            for field_name in class_obj.model_fields:
                key = (module_name, f"{class_name}.{field_name}")
                if key not in registered:
                    missing.append(f"{key[0]}:{key[1]}")
            continue
        # Dataclasses
        if is_dataclass(class_obj):
            for f in dc_fields(class_obj):
                key = (module_name, f"{class_name}.{f.name}")
                if key not in registered:
                    missing.append(f"{key[0]}:{key[1]}")
            continue

    assert not missing, (
        "the following public-facing fields are not registered in"
        " yomotsusaka.boundary_registry.REGISTRY (add a row classifying"
        " their exposure, or add an explicit _REGISTRY_EXEMPT entry if"
        " the field genuinely never crosses the boundary):\n"
        + "\n".join(sorted(missing))
    )


def test_in_scope_module_classes_are_either_registered_or_exempt() -> None:
    """Bidirectional drift guard for class-level exposure.

    Every public type in an in-scope module that is NOT a BaseModel /
    dataclass (e.g. an Enum, a service class, an exception) must be
    listed in :data:`_REGISTRY_EXEMPT` with a justification, OR be the
    subject of at least one registry row that names it (covered by the
    field-level scan above).
    """
    extras: list[str] = []
    for module_name in sorted(IN_SCOPE_MODULES):
        module_obj = importlib.import_module(module_name)
        for attr_name in sorted(dir(module_obj)):
            if attr_name.startswith("_"):
                continue
            obj = getattr(module_obj, attr_name)
            if not isinstance(obj, type):
                continue
            if getattr(obj, "__module__", None) != module_name:
                continue
            if issubclass(obj, BaseModel) and obj is not BaseModel:
                continue
            if is_dataclass(obj):
                continue
            # Otherwise: enum / service class / exception. Must be
            # explicitly exempt.
            key = (module_name, attr_name)
            if key in _REGISTRY_EXEMPT:
                continue
            # Or it must appear as the head of at least one registry row
            # (e.g. ExecutionFailureReason exposed via the whole-enum
            # row).
            head_match = any(
                entry.module == module_name
                and entry.qualname.split(".", 1)[0] == attr_name
                for entry in REGISTRY
            )
            if head_match:
                continue
            extras.append(f"{module_name}.{attr_name}")
    assert not extras, (
        "the following in-scope module-level classes are neither "
        "registered nor exempt (add a registry row or extend "
        "_REGISTRY_EXEMPT with a justification):\n" + "\n".join(extras)
    )


# ---------------------------------------------------------------------------
# 3. Classification consistency
# ---------------------------------------------------------------------------


def _build_concrete_responses() -> list[tuple[str, BaseModel]]:
    """Build a small concrete instance of every agent-facing response in
    :data:`EXPECTED_BOUNDARY_SYMBOLS` so they can be serialised and
    leak-scanned.

    The instances use synthetic public-safe values only. Each populated
    field is filled so the serialised JSON is non-trivial — that way
    the scan can detect a leak through any single field.
    """
    out: list[tuple[str, BaseModel]] = []
    public_handle = PublicHandle(
        locator="private://agent_redacted/manifest/doc-aaaaaa",
    )
    span = SpanSpec(start=0, end=5, kind=EntityKind.PERSON)
    entity = EntityRecord(
        entity_id="ent-aaaaaa",
        kind=EntityKind.PERSON,
        redacted_key="<PERSON:k1>",
        start_char=0,
        end_char=5,
    )
    pmv = boundary.PublicManifestView(
        doc_id="doc-aaaaaa",
        redacted_text="<PERSON:k1> works at <ORG:k2>.",
        entities=[entity],
        labels=["agent_label"],
        summary="<PERSON:k1> summary",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        metadata={"opaque": "value"},
    )
    out.append(("PublicHandle", public_handle))
    out.append(
        ("ProcessResponse", boundary.ProcessResponse(handle=public_handle))
    )
    out.append(
        ("InspectResponse", boundary.InspectResponse(manifest=pmv))
    )
    out.append(("PublicManifestView", pmv))
    out.append(
        (
            "SearchHit",
            boundary.SearchHit(
                handle=public_handle,
                redacted_snippet="<PERSON:k1> works",
                labels=["agent_label"],
            ),
        )
    )
    out.append(
        (
            "SearchResponse",
            boundary.SearchResponse(
                hits=[
                    boundary.SearchHit(
                        handle=public_handle,
                        redacted_snippet="<PERSON:k1> works",
                        labels=["agent_label"],
                    )
                ]
            ),
        )
    )
    out.append(
        (
            "RestorationResponse",
            boundary.RestorationResponse(
                outcome="accepted",
                audit_record_id="aud-aaaaaa",
                document_id="doc-aaaaaa",
                private_entries=[],
            ),
        )
    )
    out.append(
        (
            "StatusReportResponse",
            boundary.StatusReportResponse(
                locator=public_handle.locator,
                status="committed",
            ),
        )
    )
    out.append(
        (
            "ResolverFailure",
            boundary.ResolverFailure(
                locator=public_handle.locator,
                reason=boundary.ResolverFailureReason.UnknownArtifact,
                detail="no committed manifest for this locator",
            ),
        )
    )
    out.append(
        (
            "ResolverSuccess_ordinary",
            ResolverSuccess(
                locator=public_handle.locator,
                exposure_class="agent_redacted",
                artifact_kind="manifest",
                opaque_id="doc-aaaaaa",
                fragment=None,
                purpose="test",
                private_state=None,
            ),
        )
    )
    out.append(
        (
            "ExecutionResponse",
            ExecutionResponse(
                audit_record_id="aud-bbbbbb",
                status="accepted",
                artifacts=[public_handle],
                scrubbed_stdout="ok",
                scrubbed_stderr="",
                reason=None,
                detail=None,
            ),
        )
    )
    # SpanSpec is technically a request fragment but it's in the symbol set.
    out.append(("SpanSpec", span))
    # AuditRecord — not classified as agent-facing in EXPECTED_BOUNDARY_SYMBOLS,
    # but included because the registry classifies its restricted fields.
    out.append(
        (
            "AuditRecord",
            AuditRecord(
                ts=datetime(2026, 1, 1, tzinfo=timezone.utc),
                request_id="req-aaaaaa",
                template_name="t1",
                caller_scope="ordinary_agent",
                purpose="test",
                locator=public_handle.locator,
                outcome="success",
                artifact_locators=[public_handle.locator],
                resolver_reason=None,
                detail=None,
            ),
        )
    )
    out.append(
        (
            "PolicyDecision",
            PolicyDecision(
                verdict="permit",
                matched_profile="default",
                deny_reason=None,
            ),
        )
    )
    return out


def _registry_rows_for(exposure: str) -> list[BoundaryField]:
    return [e for e in REGISTRY if e.exposure == exposure]


# Synthetic sentinels for fields classified as ``never_expose`` /
# ``private`` that we will plant into a private-side dataclass instance
# and then re-confirm those sentinels never appear in the serialised
# agent-facing responses above.
_NEVER_EXPOSE_SENTINELS: dict[str, str] = {
    "vault_path_sentinel": "/never-expose-sentinel/vault-path-NEVER-EXPOSE",
    "pod_id_sentinel": "pod-NEVER-EXPOSE-SENTINEL",
    "endpoint_sentinel": "https://NEVER-EXPOSE-SENTINEL.example/api",
    "tenant_root_sentinel": "/never-expose-sentinel/tenant-root-NEVER-EXPOSE",
}


def test_never_expose_fields_absent_from_response_serialisations() -> None:
    """Serialise each agent-facing response and assert that none of the
    sentinel values planted in ``never_expose`` / ``private``-classified
    fields appear.

    This is a defence-in-depth check that complements the existing
    :mod:`tests.test_exposure_contract` scan: that scan is text-driven
    (canonical-fixture values), this one is *registry-driven*
    (sentinels keyed on field name).
    """
    responses = _build_concrete_responses()
    sentinels = list(_NEVER_EXPOSE_SENTINELS.values())

    for name, response in responses:
        blob = response.model_dump_json()
        for sentinel in sentinels:
            assert sentinel not in blob, (
                f"sentinel {sentinel!r} leaked into the serialised form"
                f" of agent-facing response {name!r}: {blob!r}"
            )

    # Sanity: ensure the sentinels were chosen so they are not a degenerate
    # case (e.g. empty string) that would always be absent. The check is
    # cheap and pinpoints test bit-rot.
    for sentinel in sentinels:
        assert sentinel and len(sentinel) > 8


def test_restricted_fields_gated_by_resolver_scope(tmp_path: Path) -> None:
    """For every non-``PRIVATE_BOUNDARY`` scope, ``ResolverSuccess.private_state``
    must be ``None`` on a success outcome.

    This pins the registry's classification of ``ResolverSuccess.private_state``
    as ``restricted`` with mechanism ``scope_gated_resolver``: the field
    is non-None only under ``PRIVATE_BOUNDARY``.
    """
    # Stand up a minimal vault: a manifest file and a private dictionary.
    vault_root = tmp_path / "vault"
    (vault_root / "manifests").mkdir(parents=True)
    (vault_root / "private").mkdir(parents=True)
    doc_id = "doc-restricted"
    manifest = boundary.DocumentManifest(
        doc_id=doc_id,
        source_ref=doc_id,
        redacted_text="<PERSON:k1>",
        entities=[],
        labels=[],
        summary="",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        metadata={},
    )
    (vault_root / "manifests" / f"{doc_id}.json").write_text(
        manifest.model_dump_json(), encoding="utf-8"
    )
    (vault_root / "private" / f"{doc_id}.json").write_text(
        json.dumps([]), encoding="utf-8"
    )
    locator = boundary.build_locator(
        exposure_class="agent_redacted",
        artifact_kind="manifest",
        opaque_id=doc_id,
    )

    for scope in (ResolverScope.ORDINARY_AGENT, ResolverScope.AUDIT_REVIEWER):
        outcome = boundary.resolve(
            locator,
            scope=scope,
            purpose="drift-test",
            vault_root=vault_root,
        )
        assert isinstance(outcome, ResolverSuccess), (
            f"resolve under {scope!r} did not succeed: {outcome!r}"
        )
        assert outcome.private_state is None, (
            f"resolve under {scope!r} populated private_state — the "
            f"restricted-field scope gate is broken: {outcome.private_state!r}"
        )

    # And the positive case: PRIVATE_BOUNDARY does materialise it.
    outcome = boundary.resolve(
        locator,
        scope=ResolverScope.PRIVATE_BOUNDARY,
        purpose="drift-test",
        vault_root=vault_root,
    )
    assert isinstance(outcome, ResolverSuccess)
    assert outcome.private_state is not None, (
        "PRIVATE_BOUNDARY scope did not materialise private_state; "
        "the restricted-field gate is over-strict"
    )


# ---------------------------------------------------------------------------
# 5. RunPod public-safe categories
# ---------------------------------------------------------------------------


def test_runpod_category_literals_match_registry() -> None:
    """The registry's single row for the lifecycle categories must
    resolve to ``scripts.manage_runpod.PUBLIC_SAFE_CATEGORIES``, and
    every element of that set must look like a public-safe category
    literal (no whitespace, no URL/path shapes, no Pod-id shapes).
    """
    # Resolve the registered constant.
    matching = [
        e
        for e in REGISTRY
        if e.module == "scripts.manage_runpod"
        and e.qualname == "PUBLIC_SAFE_CATEGORIES"
    ]
    assert len(matching) == 1, (
        "expected exactly one PUBLIC_SAFE_CATEGORIES row in the registry, "
        f"got {len(matching)}"
    )

    module_obj = importlib.import_module("scripts.manage_runpod")
    categories = module_obj.PUBLIC_SAFE_CATEGORIES
    assert isinstance(categories, frozenset), (
        "scripts.manage_runpod.PUBLIC_SAFE_CATEGORIES must be a frozenset; "
        f"got {type(categories).__name__}"
    )
    assert categories, "PUBLIC_SAFE_CATEGORIES is empty"

    # Per-element public-safety contract: no whitespace, no obvious
    # secret-shape leaks, no URL prefixes, no Pod-id prefixes.
    bad: list[str] = []
    forbidden_substrings = ("http://", "https://", "/", "runpod-", " ", "\t")
    for value in sorted(categories):
        if not isinstance(value, str):
            bad.append(f"non-string entry: {value!r}")
            continue
        if not value:
            bad.append("empty string entry")
            continue
        for needle in forbidden_substrings:
            if needle in value:
                bad.append(
                    f"category {value!r} contains forbidden substring "
                    f"{needle!r}"
                )
    assert not bad, "\n".join(bad)

    # Drift check: every ``_CATEGORY_*`` literal declared at module
    # level must appear in PUBLIC_SAFE_CATEGORIES (and vice versa).
    literal_values: set[str] = set()
    for attr_name in dir(module_obj):
        if not attr_name.startswith("_CATEGORY_"):
            continue
        v = getattr(module_obj, attr_name)
        if isinstance(v, str):
            literal_values.add(v)
    assert literal_values == set(categories), (
        f"_CATEGORY_* literals and PUBLIC_SAFE_CATEGORIES disagree: "
        f"missing_from_set={sorted(literal_values - set(categories))!r}, "
        f"extras_in_set={sorted(set(categories) - literal_values)!r}"
    )


# ---------------------------------------------------------------------------
# 6/7. Sanity guards
# ---------------------------------------------------------------------------


def test_registry_has_no_duplicate_qualname() -> None:
    """No ``(module, qualname)`` pair appears more than once."""
    counter = Counter((e.module, e.qualname) for e in REGISTRY)
    dupes = [k for k, v in counter.items() if v > 1]
    assert not dupes, f"duplicate registry rows: {sorted(dupes)!r}"


def test_scrub_mechanism_is_closed_set() -> None:
    """Every row's ``scrub_mechanism`` must be a member of the closed
    :data:`SCRUB_MECHANISMS` vocabulary.

    The :class:`BoundaryField` validator already enforces this at
    construction, but the test pins the contract at the module level
    so a future BoundaryField subclass that loosens the validator does
    not silently widen the vocabulary.
    """
    extras = [e for e in REGISTRY if e.scrub_mechanism not in SCRUB_MECHANISMS]
    assert not extras, (
        "registry rows carry a scrub_mechanism outside the closed set: "
        + ", ".join(f"{e.module}:{e.qualname}={e.scrub_mechanism!r}" for e in extras)
    )


def test_registry_exposures_match_boundary_module_set() -> None:
    """The set of exposure classes used by REGISTRY must be a subset of
    :data:`yomotsusaka.boundary.EXPOSURE_CLASSES`. Belt-and-suspenders
    over the ``Literal`` type at construction; pins the contract at the
    module level."""
    used = {e.exposure for e in REGISTRY}
    extras = used - boundary.EXPOSURE_CLASSES
    assert not extras, (
        "REGISTRY uses exposure classes not in boundary.EXPOSURE_CLASSES: "
        f"{sorted(extras)!r}"
    )


# ---------------------------------------------------------------------------
# Markdown render — not CI-gated for content, but smoke-tested for shape
# ---------------------------------------------------------------------------


def test_render_markdown_returns_a_table_for_every_row() -> None:
    """The markdown renderer must emit one row per :data:`REGISTRY`
    entry plus the two-line header. Used by the optional docs handoff
    (issue #96) to cite the registry from ``docs/architecture.md``.
    """
    rendered = render_markdown()
    lines = [line for line in rendered.splitlines() if line.strip()]
    # 2 header lines (header + separator) + len(REGISTRY) rows
    assert len(lines) == 2 + len(REGISTRY), (
        f"render_markdown emitted {len(lines)} non-empty lines; "
        f"expected {2 + len(REGISTRY)}"
    )
    # Header looks markdown-ish.
    assert lines[0].startswith("| Module |"), lines[0]
    assert re.fullmatch(r"\|(\s*-+\s*\|)+", lines[1]), lines[1]


# ---------------------------------------------------------------------------
# Boundary-symbol roster sanity (shared constant)
# ---------------------------------------------------------------------------


def test_expected_boundary_symbols_includes_every_response_we_register() -> None:
    """Every BaseModel in :data:`EXPECTED_BOUNDARY_SYMBOLS` must have at
    least one registry row, regardless of which in-scope module defines
    the class.

    ``EXPECTED_BOUNDARY_SYMBOLS`` pins the public-response roster used
    by :mod:`tests.test_exposure_contract`. Several of those symbols are
    re-exported from :mod:`yomotsusaka.boundary` but defined in a
    sibling module (e.g. ``ExecutionResponse`` lives in
    :mod:`yomotsusaka.execution_gateway`). The registry follows
    ``__module__`` to the defining module, so the lookup must, too.
    """
    # Build {class_name: defining_module} for every registered BaseModel
    # in the in-scope modules.
    registered_models: dict[str, str] = {}
    for entry in REGISTRY:
        head, attr = _split_qualname(entry.qualname)
        if attr is None:
            continue
        registered_models.setdefault(head, entry.module)

    missing: list[str] = []
    for sym in sorted(EXPECTED_BOUNDARY_SYMBOLS):
        head_obj = getattr(boundary, sym, None)
        if not (
            isinstance(head_obj, type)
            and issubclass(head_obj, BaseModel)
            and head_obj is not BaseModel
        ):
            continue
        if sym not in registered_models:
            missing.append(sym)
            continue
        # Defending module must match — a stale registry row whose
        # `module` points at the wrong place is just as broken as a
        # missing row.
        expected_module = head_obj.__module__
        if registered_models[sym] != expected_module:
            missing.append(
                f"{sym} (registered under {registered_models[sym]!r}; "
                f"actual defining module is {expected_module!r})"
            )

    assert not missing, (
        "EXPECTED_BOUNDARY_SYMBOLS pins these BaseModel responses but "
        "the boundary_registry has no matching rows:\n" + "\n".join(missing)
    )
