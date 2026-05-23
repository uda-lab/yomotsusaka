"""Cross-tenant isolation contract tests for issue #45.

These pin the three Fork 3 invariants on the §C metaplan:

(a) Two facades bound to ``tenant_a`` and ``tenant_b`` (distinct vault_roots
    under a shared ``tmp_path``) produce byte-identical locators for the same
    ``doc_id``, but each facade only successfully inspects the artifact under
    its own tenant — the other returns ``ResolverFailure(UnknownArtifact)``.

(b) ``tenant_b``'s facade given a forged locator that addresses a
    ``tenant_a`` artifact returns ``ResolverFailure(UnknownArtifact)``, not
    the manifest. (Automatic given disjoint vault_roots, but pinned here so
    a future implementation that conflated the two tenants regresses
    visibly.)

(c) Audit records written for ``tenant_a`` requests do not appear in
    ``tenant_b``'s audit file. Per Fork 4 the audit log is per-tenant
    (``<tenant.vault_root>/audit/restoration.jsonl``); no global file
    aggregates across tenants.

Failure-mode taxonomy (Fork 9): no new ``ResolverFailureReason`` /
``RestorationFailureReason`` values are introduced — cross-tenant misses
return the existing ``UnknownArtifact`` reason so a forged locator cannot
distinguish "exists under another tenant" from "never existed".
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from yomotsusaka import LocalFacade
from yomotsusaka.boundary import (
    InspectRequest,
    ProcessRequest,
    PublicHandle,
    ResolverFailure,
    ResolverFailureReason,
    ResolverScope,
    RestorationFailureReason,
    RestorationRequest,
    SpanSpec,
    build_locator,
    inspect_request,
    parse_locator,
    process_document_request,
    restoration_request,
    status_report_request,
    StatusReportRequest,
)
from yomotsusaka.schemas import EntityKind
from yomotsusaka.tenant import TenantScope


_RAW_TEXT = "Alice Tan works at Acme Corp. Patient ID: 12345."
_SPANS: tuple[SpanSpec, ...] = (
    SpanSpec(start=0, end=9, kind=EntityKind.PERSON),
    SpanSpec(start=19, end=28, kind=EntityKind.ORG),
    SpanSpec(start=42, end=47, kind=EntityKind.ID_NUMBER),
)


def _process(doc_id: str) -> ProcessRequest:
    return ProcessRequest(doc_id=doc_id, raw_text=_RAW_TEXT, spans=list(_SPANS))


def _expected_locator(doc_id: str) -> str:
    return build_locator(
        exposure_class="agent_redacted",
        artifact_kind="manifest",
        opaque_id=doc_id,
    )


def _make_tenant(tmp_path: Path, tenant_id: str) -> TenantScope:
    """Disjoint vault_roots per tenant under a shared tmp_path."""
    root = tmp_path / tenant_id
    return TenantScope(tenant_id=tenant_id, vault_root=root)


# ---------------------------------------------------------------------------
# Fork 3 (a): cross-tenant locator opacity
# ---------------------------------------------------------------------------


def test_two_tenants_same_doc_id_locators_are_byte_identical(
    tmp_path: Path,
) -> None:
    """Locator carries no tenant identity; same ``doc_id`` under two
    different tenants yields the same locator string (per Fork 1, the
    locator is "safe to log" and cannot leak the tenant).
    """
    tenant_a = _make_tenant(tmp_path, "alpha")
    tenant_b = _make_tenant(tmp_path, "beta")
    facade_a = LocalFacade(tenant=tenant_a)
    facade_b = LocalFacade(tenant=tenant_b)
    doc_id = "shared-doc-id"

    resp_a = facade_a.process(_process(doc_id))
    resp_b = facade_b.process(_process(doc_id))

    assert resp_a.handle.locator == resp_b.handle.locator
    # And the parsed view confirms there is no tenant token in the URI.
    parsed = parse_locator(resp_a.handle.locator)
    assert parsed is not None
    assert parsed.opaque_id == doc_id
    # Sanity: tenant ids do not appear anywhere in the locator string.
    assert "alpha" not in resp_a.handle.locator
    assert "beta" not in resp_a.handle.locator


def test_inspect_only_succeeds_against_originating_tenant(
    tmp_path: Path,
) -> None:
    """``tenant_b`` inspecting a locator committed by ``tenant_a`` must
    return ``ResolverFailure(UnknownArtifact)`` — the artifact is not
    committed under ``tenant_b``'s vault_root, and the failure must not
    leak the existence of ``tenant_a``'s copy."""
    tenant_a = _make_tenant(tmp_path, "alpha")
    tenant_b = _make_tenant(tmp_path, "beta")
    facade_a = LocalFacade(tenant=tenant_a)
    facade_b = LocalFacade(tenant=tenant_b)
    doc_id = "doc-only-in-tenant-a"

    resp = facade_a.process(_process(doc_id))
    locator = resp.handle.locator

    # tenant_a sees the artifact.
    own = facade_a.inspect(InspectRequest(locator=locator))
    assert not isinstance(own, ResolverFailure), (
        f"tenant_a's own artifact must be inspectable, got {own!r}"
    )

    # tenant_b sees UnknownArtifact for the same locator.
    cross = facade_b.inspect(InspectRequest(locator=locator))
    assert isinstance(cross, ResolverFailure)
    assert cross.reason is ResolverFailureReason.UnknownArtifact
    # Fork 9: the failure detail must not name the other tenant. Cross-check
    # against both tenant ids and against the absolute vault_root path.
    assert "alpha" not in (cross.detail or "")
    assert str(tenant_a.vault_root) not in (cross.detail or "")


def test_status_report_is_per_tenant(tmp_path: Path) -> None:
    """``status_report_request`` must also be per-tenant: ``committed`` under
    ``tenant_a``, ``unknown`` under ``tenant_b``."""
    tenant_a = _make_tenant(tmp_path, "alpha")
    tenant_b = _make_tenant(tmp_path, "beta")
    doc_id = "doc-x"
    process_document_request(_process(doc_id), tenant=tenant_a)

    locator = _expected_locator(doc_id)
    resp_a = status_report_request(StatusReportRequest(locator=locator), tenant=tenant_a)
    resp_b = status_report_request(StatusReportRequest(locator=locator), tenant=tenant_b)
    assert resp_a.status == "committed"
    assert resp_b.status == "unknown"


# ---------------------------------------------------------------------------
# Fork 3 (b): forged-locator denial
# ---------------------------------------------------------------------------


def test_forged_locator_targeting_other_tenant_is_unknown(
    tmp_path: Path,
) -> None:
    """A locator hand-crafted to address a ``tenant_a`` artifact must yield
    ``UnknownArtifact`` when resolved against ``tenant_b``. This is the
    explicit fail-closed contract on the public surface: a malicious or
    confused caller cannot read across tenants via locator forgery."""
    tenant_a = _make_tenant(tmp_path, "alpha")
    tenant_b = _make_tenant(tmp_path, "beta")
    doc_id = "private-doc-001"

    # tenant_a commits the document.
    process_document_request(_process(doc_id), tenant=tenant_a)

    # Caller manually builds the locator (no boundary call needed — the
    # locator grammar is public).
    forged = build_locator(
        exposure_class="agent_redacted",
        artifact_kind="manifest",
        opaque_id=doc_id,
    )

    # The forged locator resolves under tenant_b to UnknownArtifact.
    cross = inspect_request(
        InspectRequest(locator=forged),
        tenant=tenant_b,
    )
    assert isinstance(cross, ResolverFailure)
    assert cross.reason is ResolverFailureReason.UnknownArtifact


def test_restoration_request_is_per_tenant_artifact_missing(
    tmp_path: Path,
) -> None:
    """Cross-tenant restoration (private boundary scope) against a forged
    locator must surface as ``ArtifactMissing`` — the existing kernel
    failure reason. No new reason is introduced (Fork 9)."""
    tenant_a = _make_tenant(tmp_path, "alpha")
    tenant_b = _make_tenant(tmp_path, "beta")
    doc_id = "private-restore-001"

    process_document_request(_process(doc_id), tenant=tenant_a)

    req = RestorationRequest(
        caller_label="tenant-iso-test",
        reason="cross-tenant smoke",
        timestamp=datetime.now(timezone.utc),
        target_public_handle=PublicHandle(locator=_expected_locator(doc_id)),
        requested_entity_kinds=[EntityKind.PERSON],
    )
    resp = restoration_request(
        req,
        scope=ResolverScope.PRIVATE_BOUNDARY,
        tenant=tenant_b,
    )
    assert resp.outcome == "failed"
    assert resp.reason is RestorationFailureReason.ArtifactMissing


# ---------------------------------------------------------------------------
# Fork 3 (c): per-tenant audit log
# ---------------------------------------------------------------------------


def _audit_path(tenant: TenantScope) -> Path:
    return tenant.vault_root / "audit" / "restoration.jsonl"


def _read_audit_records(tenant: TenantScope) -> list[dict[str, object]]:
    path = _audit_path(tenant)
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_audit_log_lives_under_each_tenant_vault_root(tmp_path: Path) -> None:
    """A restoration request against ``tenant_a`` writes its audit line to
    ``<tenant_a.vault_root>/audit/restoration.jsonl`` and never touches
    ``tenant_b``'s audit directory."""
    tenant_a = _make_tenant(tmp_path, "alpha")
    tenant_b = _make_tenant(tmp_path, "beta")
    doc_id = "audit-iso-doc"

    process_document_request(_process(doc_id), tenant=tenant_a)

    req = RestorationRequest(
        caller_label="tenant-iso-audit",
        reason="audit isolation smoke",
        timestamp=datetime.now(timezone.utc),
        target_public_handle=PublicHandle(locator=_expected_locator(doc_id)),
        requested_entity_kinds=[EntityKind.PERSON],
    )
    # ORDINARY_AGENT → scope-denied path writes one audit line, kernel NOT
    # called. That is sufficient to prove file-path isolation.
    restoration_request(
        req,
        scope=ResolverScope.ORDINARY_AGENT,
        tenant=tenant_a,
    )

    records_a = _read_audit_records(tenant_a)
    records_b = _read_audit_records(tenant_b)
    assert len(records_a) == 1, (
        f"expected exactly one audit line in tenant_a, got {records_a!r}"
    )
    assert records_b == [], (
        f"tenant_b's audit log must be empty, got {records_b!r}"
    )
    # The audit record itself does NOT carry a tenant_id field (Fork 4: the
    # file path is the tenant scope). Defensive: confirm we did not silently
    # introduce one.
    assert "tenant_id" not in records_a[0]


def test_audit_files_are_disjoint_under_concurrent_tenants(
    tmp_path: Path,
) -> None:
    """Each tenant writes only to its own audit file; the two files'
    record counts evolve independently."""
    tenant_a = _make_tenant(tmp_path, "alpha")
    tenant_b = _make_tenant(tmp_path, "beta")
    doc_id_a = "doc-a"
    doc_id_b = "doc-b"

    process_document_request(_process(doc_id_a), tenant=tenant_a)
    process_document_request(_process(doc_id_b), tenant=tenant_b)

    req_a = RestorationRequest(
        caller_label="A",
        reason="A",
        timestamp=datetime.now(timezone.utc),
        target_public_handle=PublicHandle(locator=_expected_locator(doc_id_a)),
        requested_entity_kinds=[EntityKind.PERSON],
    )
    req_b = RestorationRequest(
        caller_label="B",
        reason="B",
        timestamp=datetime.now(timezone.utc),
        target_public_handle=PublicHandle(locator=_expected_locator(doc_id_b)),
        requested_entity_kinds=[EntityKind.PERSON],
    )
    # Two A-requests, one B-request: counts should diverge.
    restoration_request(req_a, scope=ResolverScope.ORDINARY_AGENT, tenant=tenant_a)
    restoration_request(req_a, scope=ResolverScope.ORDINARY_AGENT, tenant=tenant_a)
    restoration_request(req_b, scope=ResolverScope.ORDINARY_AGENT, tenant=tenant_b)

    records_a = _read_audit_records(tenant_a)
    records_b = _read_audit_records(tenant_b)
    assert len(records_a) == 2
    assert len(records_b) == 1
    # Defensive: caller_label correlation rules out cross-tenant bleed.
    for rec in records_a:
        assert rec["caller_label"] == "A"
    for rec in records_b:
        assert rec["caller_label"] == "B"


# ---------------------------------------------------------------------------
# Programmer-error guardrails on the new tenant kwarg
# ---------------------------------------------------------------------------


def test_passing_both_tenant_and_vault_root_raises(tmp_path: Path) -> None:
    """Every boundary entry point must reject double-binding. Exhaustively
    enumerate them so a future entry point that forgets the
    ``_resolve_tenant`` call regresses visibly."""
    tenant = _make_tenant(tmp_path, "alpha")
    from yomotsusaka.boundary import ResolverError

    # process_document_request
    with pytest.raises(ResolverError):
        process_document_request(
            _process("xx"), vault_root=tmp_path, tenant=tenant
        )

    # inspect_request
    with pytest.raises(ResolverError):
        inspect_request(
            InspectRequest(locator=_expected_locator("xx")),
            vault_root=tmp_path,
            tenant=tenant,
        )

    # status_report_request
    with pytest.raises(ResolverError):
        status_report_request(
            StatusReportRequest(locator=_expected_locator("xx")),
            vault_root=tmp_path,
            tenant=tenant,
        )

    # restoration_request
    req = RestorationRequest(
        caller_label="x",
        reason="x",
        timestamp=datetime.now(timezone.utc),
        target_public_handle=PublicHandle(locator=_expected_locator("xx")),
        requested_entity_kinds=[EntityKind.PERSON],
    )
    with pytest.raises(ResolverError):
        restoration_request(
            req,
            scope=ResolverScope.ORDINARY_AGENT,
            vault_root=tmp_path,
            tenant=tenant,
        )


def test_passing_neither_tenant_nor_vault_root_raises(tmp_path: Path) -> None:
    """Every boundary entry point must reject missing scope (no implicit
    default vault_root from env or cwd)."""
    from yomotsusaka.boundary import ResolverError

    with pytest.raises(ResolverError):
        process_document_request(_process("xx"))

    with pytest.raises(ResolverError):
        inspect_request(InspectRequest(locator=_expected_locator("xx")))

    with pytest.raises(ResolverError):
        status_report_request(
            StatusReportRequest(locator=_expected_locator("xx"))
        )

    req = RestorationRequest(
        caller_label="x",
        reason="x",
        timestamp=datetime.now(timezone.utc),
        target_public_handle=PublicHandle(locator=_expected_locator("xx")),
        requested_entity_kinds=[EntityKind.PERSON],
    )
    with pytest.raises(ResolverError):
        restoration_request(req, scope=ResolverScope.ORDINARY_AGENT)
