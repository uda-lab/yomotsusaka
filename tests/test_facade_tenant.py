"""Tenant-aware construction tests for :class:`yomotsusaka.facade.LocalFacade`.

These tests pin the §C Fork 5(c) contract on issue #45:

* ``LocalFacade(vault_root=...)`` (legacy) and
  ``LocalFacade(tenant=TenantScope.local(vault_root))`` (explicit) produce
  byte-identical observable behaviour over a single-tenant workflow. This is
  the "no behaviour change for existing callers" pin that backs the
  back-compat CI gate.
* ``LocalFacade(vault_root=..., tenant=...)`` raises ``ValueError`` — exactly
  one scope must be supplied.
* ``LocalFacade()`` (no args) raises ``ValueError`` — there is no implicit
  default vault_root.

These tests live in a new file so the pre-existing ``tests/test_facade.py``
remains untouched (Fork 5 back-compat CI gate).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from yomotsusaka import LocalFacade
from yomotsusaka.boundary import (
    InspectRequest,
    ProcessRequest,
    SpanSpec,
    StatusReportRequest,
    build_locator,
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


# ---------------------------------------------------------------------------
# Construction contracts
# ---------------------------------------------------------------------------


def test_vault_root_and_tenant_local_equivalent(tmp_path: Path) -> None:
    """``LocalFacade(vault_root=X)`` and
    ``LocalFacade(tenant=TenantScope.local(X))`` must drive the same
    single-tenant workflow to the same observable end state."""
    vault_a = tmp_path / "via-vault-root"
    vault_b = tmp_path / "via-tenant-local"

    facade_legacy = LocalFacade(vault_a)
    facade_explicit = LocalFacade(tenant=TenantScope.local(vault_b))

    doc_id = "shared-equivalence-doc"
    legacy_response = facade_legacy.process(_process(doc_id))
    explicit_response = facade_explicit.process(_process(doc_id))

    # Locator carries no tenant identity → byte-identical for the same doc_id.
    assert legacy_response.handle.locator == explicit_response.handle.locator

    # Status-report agrees on the canonical locator under each vault root.
    locator = _expected_locator(doc_id)
    assert (
        facade_legacy.status_report(StatusReportRequest(locator=locator)).status
        == "committed"
    )
    assert (
        facade_explicit.status_report(StatusReportRequest(locator=locator)).status
        == "committed"
    )

    # ``vault_root`` property continues to work for both constructions
    # (back-compat shape).
    assert facade_legacy.vault_root == vault_a
    assert facade_explicit.vault_root == vault_b


def test_construct_with_explicit_tenant(tmp_path: Path) -> None:
    """An explicit non-local :class:`TenantScope` constructs cleanly and the
    facade routes operations into that tenant's vault_root."""
    tenant = TenantScope(tenant_id="explicit-tenant-1", vault_root=tmp_path / "t1")
    facade = LocalFacade(tenant=tenant)
    assert facade.vault_root == tenant.vault_root

    doc_id = "tenant-explicit-doc"
    facade.process(_process(doc_id))
    response = facade.inspect(InspectRequest(locator=_expected_locator(doc_id)))
    # Round-trip: the manifest is readable via the same facade.
    from yomotsusaka.boundary import InspectResponse

    assert isinstance(response, InspectResponse)


# ---------------------------------------------------------------------------
# Mutually-exclusive argument contract
# ---------------------------------------------------------------------------


def test_constructing_with_both_arguments_raises(tmp_path: Path) -> None:
    """Per Fork 5(c), passing both ``vault_root`` and ``tenant`` is a
    programmer error and raises :class:`ValueError`."""
    tenant = TenantScope(tenant_id="dual", vault_root=tmp_path / "dual")
    with pytest.raises(ValueError):
        LocalFacade(tmp_path, tenant=tenant)


def test_constructing_with_no_arguments_raises() -> None:
    """No implicit defaults: omitting both ``vault_root`` and ``tenant``
    raises :class:`ValueError`. (Matches the boundary's "explicit dependency
    injection, no environment defaults" contract from architecture §5.7.2.)
    """
    with pytest.raises(ValueError):
        LocalFacade()  # type: ignore[call-arg]


def test_constructing_with_non_path_vault_root_raises() -> None:
    """A string vault_root would defeat downstream ``isinstance(_, Path)``
    guards in the resolver / restoration_request entry points; reject it
    at construction time."""
    with pytest.raises(ValueError):
        LocalFacade("/tmp/vault")  # type: ignore[arg-type]


def test_constructing_with_non_tenantscope_tenant_raises(tmp_path: Path) -> None:
    """Symmetric guard: ``tenant`` must be a :class:`TenantScope`, not a
    bare dict or Path."""
    with pytest.raises(ValueError):
        LocalFacade(tenant={"tenant_id": "x", "vault_root": tmp_path})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Public surface stability — facade does not widen ``dir(...)``
# ---------------------------------------------------------------------------


def test_facade_public_surface_unchanged_under_tenant_construction(
    tmp_path: Path,
) -> None:
    """Constructing through ``tenant=...`` must not widen the facade's
    public attribute surface. The pre-existing
    ``test_facade_exposes_only_documented_methods`` test pins the closed
    set; this is the symmetric check from the tenant side."""
    facade = LocalFacade(tenant=TenantScope.local(tmp_path))
    public = {name for name in dir(facade) if not name.startswith("_")}
    expected = {
        "process",
        "inspect",
        "search",
        "request_restore",
        "execute",
        "status_report",
        "vault_root",
        "gateway",
    }
    assert public == expected, (
        f"facade.dir grew under tenant construction: extras={public - expected}, "
        f"missing={expected - public}"
    )
