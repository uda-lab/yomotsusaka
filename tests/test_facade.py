"""Delegation-contract tests for :class:`yomotsusaka.facade.LocalFacade`.

These tests pin the *delegation* invariant — that :class:`LocalFacade` is a
1:1 wrapper over the five MVP-2 ``boundary.*_request`` entry points — without
duplicating the boundary's own contract tests (``test_boundary_operations.py``
already covers `source_ref` redaction, raw-value absence, and resolver-failure
typing across the boundary surface).

Concretely we cover:

* Each of the five methods returns the same response shape as the underlying
  boundary call on the same vault state.
* Failure-mode typing (``ResolverFailure``) propagates through the facade
  unchanged.
* The privacy invariant stays mechanical: ``facade.py`` does not import
  ``restoration_api`` and does not name ``PRIVATE_BOUNDARY`` anywhere.
* The facade exposes no private-side affordances beyond the five documented
  methods.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from yomotsusaka import LocalFacade
from yomotsusaka.boundary import (
    InspectRequest,
    InspectResponse,
    ProcessRequest,
    ProcessResponse,
    PublicHandle,
    ResolverFailure,
    ResolverFailureReason,
    RestorationRequest,
    RestorationResponse,
    SearchRequest,
    SearchResponse,
    SpanSpec,
    StatusReportRequest,
    StatusReportResponse,
    build_locator,
    inspect_request,
    restoration_request,
)
from yomotsusaka.facade import LocalFacade as DirectLocalFacade
from yomotsusaka.schemas import DocumentManifest, EntityKind
from yomotsusaka.search_gateway import SearchGateway

_RAW_TEXT = "Alice Tan works at Acme Corp. Patient ID: 12345."
_CANONICAL_SPANS: tuple[SpanSpec, ...] = (
    SpanSpec(start=0, end=9, kind=EntityKind.PERSON),
    SpanSpec(start=19, end=28, kind=EntityKind.ORG),
    SpanSpec(start=42, end=47, kind=EntityKind.ID_NUMBER),
)


def _expected_locator(doc_id: str) -> str:
    return build_locator(
        exposure_class="agent_redacted",
        artifact_kind="manifest",
        opaque_id=doc_id,
    )


def _process_request(doc_id: str) -> ProcessRequest:
    return ProcessRequest(doc_id=doc_id, raw_text=_RAW_TEXT, spans=list(_CANONICAL_SPANS))


# ---------------------------------------------------------------------------
# Sanity: facade is importable both ways
# ---------------------------------------------------------------------------


def test_localfacade_reexport_matches_direct_import() -> None:
    """``from yomotsusaka import LocalFacade`` and the direct import resolve
    to the same class object — the re-export is not a stale alias."""
    assert LocalFacade is DirectLocalFacade


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_construction_does_not_touch_filesystem(tmp_path: Path) -> None:
    """The facade must be constructible against a non-existent vault root;
    the boundary entry points decide when (and whether) to materialise it."""
    missing = tmp_path / "does-not-exist"
    facade = LocalFacade(missing)
    assert facade.vault_root == missing
    assert not missing.exists()


def test_constructor_accepts_explicit_gateway(tmp_path: Path) -> None:
    """An explicitly-supplied gateway must round-trip via the property."""
    gateway = SearchGateway()
    facade = LocalFacade(tmp_path, gateway=gateway)
    assert facade.gateway is gateway


def test_lazy_gateway_is_constructed_on_first_access(tmp_path: Path) -> None:
    """If no gateway is supplied, the first access must materialise one and
    return the same instance on subsequent access."""
    facade = LocalFacade(tmp_path)
    first = facade.gateway
    second = facade.gateway
    assert isinstance(first, SearchGateway)
    assert first is second


# ---------------------------------------------------------------------------
# Method-by-method delegation
# ---------------------------------------------------------------------------


def test_process_delegates_to_boundary(tmp_path: Path) -> None:
    """``LocalFacade.process`` returns the same locator the direct boundary
    call would produce on the same vault state."""
    vault = tmp_path / "vault"
    doc_id = "facade-process-001"
    facade = LocalFacade(vault)

    response = facade.process(_process_request(doc_id))

    assert isinstance(response, ProcessResponse)
    assert isinstance(response.handle, PublicHandle)
    assert response.handle.locator == _expected_locator(doc_id)
    # The boundary discards vault_path; verify the serialised facade output
    # carries no internal kernel fields either.
    blob = response.model_dump_json()
    assert "vault_path" not in blob
    assert "handle_id" not in blob


def test_inspect_delegates_to_boundary(tmp_path: Path) -> None:
    """A doc committed via the facade is then inspectable via the facade and
    yields the same response a direct boundary call would."""
    vault = tmp_path / "vault"
    doc_id = "facade-inspect-001"
    facade = LocalFacade(vault)
    facade.process(_process_request(doc_id))

    facade_response = facade.inspect(
        InspectRequest(locator=_expected_locator(doc_id))
    )
    direct_response = inspect_request(
        InspectRequest(locator=_expected_locator(doc_id)),
        vault_root=vault,
    )

    assert isinstance(facade_response, InspectResponse)
    assert isinstance(direct_response, InspectResponse)
    # Same manifest body, same field set; the facade adds no projection.
    assert facade_response.manifest.model_dump() == direct_response.manifest.model_dump()


def test_inspect_propagates_resolver_failure(tmp_path: Path) -> None:
    """A malformed locator must surface as ``ResolverFailure`` through the
    facade; failure typing is not swallowed or remapped."""
    facade = LocalFacade(tmp_path)
    response = facade.inspect(InspectRequest(locator="not-a-locator"))
    assert isinstance(response, ResolverFailure)
    assert response.reason is ResolverFailureReason.MalformedLocator


def test_search_uses_held_gateway(tmp_path: Path) -> None:
    """Search hits returned by the facade match the gateway it was
    constructed with — the facade does not silently substitute another
    indexer."""
    vault = tmp_path / "vault"
    doc_id = "facade-search-001"
    facade_for_process = LocalFacade(vault)
    facade_for_process.process(_process_request(doc_id))

    manifest = DocumentManifest.model_validate_json(
        (vault / "manifests" / f"{doc_id}.json").read_text(encoding="utf-8")
    )
    gateway = SearchGateway()
    gateway.index(manifest)

    facade = LocalFacade(vault, gateway=gateway)
    response = facade.search(SearchRequest(query="<PERSON_"))

    assert isinstance(response, SearchResponse)
    assert len(response.hits) == 1
    assert response.hits[0].handle.locator == _expected_locator(doc_id)


def test_search_with_lazy_gateway_returns_empty(tmp_path: Path) -> None:
    """A fresh facade with no explicit gateway has an empty index; searches
    must still return a well-typed ``SearchResponse`` rather than raise."""
    facade = LocalFacade(tmp_path)
    response = facade.search(SearchRequest(query="<PERSON_"))
    assert isinstance(response, SearchResponse)
    assert response.hits == []


def test_request_restore_is_deferred(tmp_path: Path) -> None:
    """``request_restore`` must produce the same shape-only deferred response
    as the direct boundary call. This pins that the facade did NOT bypass
    :func:`boundary.restoration_request` (e.g. by calling
    ``restoration_api.restore`` directly)."""
    vault = tmp_path / "vault"
    doc_id = "facade-restore-001"
    facade = LocalFacade(vault)
    facade.process(_process_request(doc_id))

    facade_response = facade.request_restore(
        RestorationRequest(locator=_expected_locator(doc_id), purpose="t")
    )
    direct_response = restoration_request(
        RestorationRequest(locator=_expected_locator(doc_id), purpose="t"),
        vault_root=vault,
    )

    assert isinstance(facade_response, RestorationResponse)
    assert facade_response.outcome == "deferred"
    assert facade_response.model_dump() == direct_response.model_dump()


def test_request_restore_does_not_call_restoration_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mechanical guard: ``LocalFacade.request_restore`` must not invoke
    ``restoration_api.restore``. The boundary already enforces this for its
    own entry point; this test verifies the facade did not add a bypass."""
    vault = tmp_path / "vault"
    doc_id = "facade-restore-no-call"
    facade = LocalFacade(vault)
    facade.process(_process_request(doc_id))

    import yomotsusaka.restoration_api as restoration_api

    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("restoration_api.restore must not be called via the facade")

    monkeypatch.setattr(restoration_api, "restore", boom)

    response = facade.request_restore(
        RestorationRequest(locator=_expected_locator(doc_id), purpose="t")
    )
    assert isinstance(response, RestorationResponse)


def test_request_restore_propagates_resolver_failure(tmp_path: Path) -> None:
    """Malformed locator must surface as ``ResolverFailure``; the facade
    does not paper over failure typing."""
    facade = LocalFacade(tmp_path)
    response = facade.request_restore(
        RestorationRequest(locator="bogus", purpose="t")
    )
    assert isinstance(response, ResolverFailure)
    assert response.reason is ResolverFailureReason.MalformedLocator


def test_status_report_committed_after_process(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    doc_id = "facade-status-committed"
    facade = LocalFacade(vault)
    facade.process(_process_request(doc_id))

    response = facade.status_report(
        StatusReportRequest(locator=_expected_locator(doc_id))
    )
    assert isinstance(response, StatusReportResponse)
    assert response.status == "committed"
    assert response.locator == _expected_locator(doc_id)


def test_status_report_unknown_for_never_committed(tmp_path: Path) -> None:
    facade = LocalFacade(tmp_path)
    response = facade.status_report(
        StatusReportRequest(locator=_expected_locator("never-committed"))
    )
    assert response.status == "unknown"


def test_status_report_unknown_for_malformed_locator(tmp_path: Path) -> None:
    facade = LocalFacade(tmp_path)
    response = facade.status_report(StatusReportRequest(locator="bogus"))
    assert response.status == "unknown"


# ---------------------------------------------------------------------------
# Privacy invariant guards
# ---------------------------------------------------------------------------


def test_facade_module_does_not_import_restoration_api() -> None:
    """``facade.py`` MUST NOT import :mod:`restoration_api` directly. The
    only legitimate restoration surface is :func:`boundary.restoration_request`
    (deferred in MVP-2)."""
    facade_source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "yomotsusaka"
        / "facade.py"
    ).read_text(encoding="utf-8")
    # The substring guard catches both `import restoration_api` and
    # `from yomotsusaka.restoration_api import ...`.
    assert "restoration_api" not in facade_source, (
        "facade.py must not reference restoration_api; route through "
        "boundary.restoration_request instead"
    )


def test_facade_module_does_not_name_private_boundary_scope() -> None:
    """``facade.py`` MUST NOT name ``PRIVATE_BOUNDARY`` anywhere. The facade
    is ordinary-agent only; callers needing private-boundary semantics call
    :func:`boundary.resolve` directly."""
    facade_source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "yomotsusaka"
        / "facade.py"
    ).read_text(encoding="utf-8")
    assert "PRIVATE_BOUNDARY" not in facade_source, (
        "facade.py must not construct or reference ResolverScope.PRIVATE_BOUNDARY"
    )


def test_facade_exposes_only_documented_methods() -> None:
    """Mechanical drift guard: the facade exposes exactly the five
    documented public methods and the two read-only properties. No
    ``resolve``, ``private_state``, ``audit_log``, or ``restore`` surface."""
    public = {
        name
        for name in dir(LocalFacade)
        if not name.startswith("_")
    }
    expected = {
        # The five operations.
        "process",
        "inspect",
        "search",
        "request_restore",
        "status_report",
        # Read-only properties.
        "vault_root",
        "gateway",
    }
    assert public == expected, (
        f"unexpected facade surface: extras={public - expected}, "
        f"missing={expected - public}"
    )
    # Defensive: the names the privacy invariant explicitly forbids.
    for forbidden in ("resolve", "private_state", "audit_log", "restore"):
        assert forbidden not in public, (
            f"facade exposes a forbidden surface: {forbidden!r}"
        )
