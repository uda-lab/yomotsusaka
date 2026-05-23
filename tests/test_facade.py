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
from yomotsusaka.execution_gateway import (
    ExecutionFailureReason,
    ExecutionRequest,
    ExecutionResponse,
    ExecutionScope,
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


def _facade_restore_request(doc_id: str) -> RestorationRequest:
    from datetime import datetime, timezone

    return RestorationRequest(
        caller_label="facade-test",
        reason="facade-delegation-test",
        timestamp=datetime.now(timezone.utc),
        document_id=doc_id,
        requested_entity_kinds=[EntityKind.PERSON],
    )


def test_request_restore_is_scope_denied(tmp_path: Path) -> None:
    """``request_restore`` must produce the same scope-denied response as the
    direct ordinary-agent boundary call. The facade is hard-wired to
    ``ResolverScope.ORDINARY_AGENT`` so #27's audit-logged path runs but
    the kernel is never invoked."""
    from yomotsusaka.boundary import (
        ResolverScope,
        RestorationFailureReason,
    )

    vault = tmp_path / "vault"
    doc_id = "facade-restore-001"
    facade = LocalFacade(vault)
    facade.process(_process_request(doc_id))

    facade_response = facade.request_restore(_facade_restore_request(doc_id))
    direct_response = restoration_request(
        _facade_restore_request(doc_id),
        scope=ResolverScope.ORDINARY_AGENT,
        vault_root=vault,
    )

    assert isinstance(facade_response, RestorationResponse)
    assert facade_response.outcome == "failed"
    assert facade_response.reason is RestorationFailureReason.ScopeDenied
    assert facade_response.private_entries is None
    # The facade response is shape-identical to a direct ordinary-agent call,
    # modulo the per-request audit_record_id which is freshly generated.
    assert facade_response.outcome == direct_response.outcome
    assert facade_response.reason == direct_response.reason
    assert facade_response.document_id == direct_response.document_id


def test_request_restore_does_not_call_restoration_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mechanical guard: ``LocalFacade.request_restore`` must not invoke
    ``restoration_api.restore``. The boundary already enforces this for its
    own entry point (scope gate); this test verifies the facade did not
    add a bypass."""
    vault = tmp_path / "vault"
    doc_id = "facade-restore-no-call"
    facade = LocalFacade(vault)
    facade.process(_process_request(doc_id))

    import yomotsusaka.restoration_api as restoration_api

    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("restoration_api.restore must not be called via the facade")

    monkeypatch.setattr(restoration_api, "restore", boom)

    response = facade.request_restore(_facade_restore_request(doc_id))
    assert isinstance(response, RestorationResponse)


def test_request_restore_propagates_request_schema_failure(tmp_path: Path) -> None:
    """A malformed ``target_public_handle`` must surface as a failed
    ``RestorationResponse`` with ``RequestSchemaInvalid``; the facade does
    not paper over failure typing."""
    from datetime import datetime, timezone

    from yomotsusaka.boundary import PublicHandle, RestorationFailureReason

    facade = LocalFacade(tmp_path)
    req = RestorationRequest(
        caller_label="facade-test",
        reason="facade-delegation-test",
        timestamp=datetime.now(timezone.utc),
        target_public_handle=PublicHandle(
            locator="private://agent_redacted/private_dict/some-id"
        ),
        requested_entity_kinds=[EntityKind.PERSON],
    )
    response = facade.request_restore(req)
    assert isinstance(response, RestorationResponse)
    assert response.outcome == "failed"
    assert response.reason is RestorationFailureReason.RequestSchemaInvalid


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


def test_facade_module_does_not_name_execution_private_boundary_scope() -> None:
    """``facade.py`` MUST NOT name ``ExecutionScope.PRIVATE_BOUNDARY``
    anywhere. The facade's ``execute`` method is hard-wired to ordinary-agent
    semantics; callers needing private-boundary execution invoke
    :func:`boundary.execute_request` directly. Mirrors the existing
    ``ResolverScope.PRIVATE_BOUNDARY`` absence assertion."""
    facade_source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "yomotsusaka"
        / "facade.py"
    ).read_text(encoding="utf-8")
    # The substring guard catches both ``ResolverScope.PRIVATE_BOUNDARY`` and
    # ``ExecutionScope.PRIVATE_BOUNDARY``; this assertion exists as an
    # explicit, separately-named regression marker for the execute path.
    assert "ExecutionScope.PRIVATE_BOUNDARY" not in facade_source, (
        "facade.py must not construct or reference "
        "ExecutionScope.PRIVATE_BOUNDARY"
    )


def test_facade_module_does_not_import_private_side_modules() -> None:
    """``facade.py`` MUST NOT import :mod:`yomotsusaka.templates`,
    :mod:`yomotsusaka.scrubber`, or :mod:`yomotsusaka.audit`. Those are
    private-side modules; the boundary already owns them, and the facade is
    a pure delegator over the already-public surface."""
    facade_source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "yomotsusaka"
        / "facade.py"
    ).read_text(encoding="utf-8")
    for forbidden in (
        "yomotsusaka.templates",
        "yomotsusaka.scrubber",
        "yomotsusaka.audit",
    ):
        assert forbidden not in facade_source, (
            f"facade.py must not reference {forbidden!r}; route through "
            "boundary.* instead"
        )


def test_facade_exposes_only_documented_methods() -> None:
    """Mechanical drift guard: the facade exposes exactly the six
    documented public methods and the two read-only properties. No
    ``resolve``, ``private_state``, ``audit_log``, or ``restore`` surface."""
    public = {
        name
        for name in dir(LocalFacade)
        if not name.startswith("_")
    }
    expected = {
        # The six operations.
        "process",
        "inspect",
        "search",
        "request_restore",
        "execute",
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


# ---------------------------------------------------------------------------
# execute() — Chikaeshi delegator (issue #73)
# ---------------------------------------------------------------------------


def _execute_request(
    job_name: str = "summarise_private_minutes",
    *,
    locator: str = "private://agent_redacted/manifest/exec-doc",
) -> ExecutionRequest:
    """Build an ``ExecutionRequest`` whose ``scope`` is ORDINARY_AGENT.

    The facade is hard-wired to ordinary-agent semantics, so every shipped
    template's ``min_scope=PRIVATE_BOUNDARY`` requirement is denied at the
    dispatcher's scope gate (the spec's whole reason for not adding a
    private-scope code path through the facade)."""
    return ExecutionRequest(
        job_name=job_name,
        purpose="facade-execute-test",
        scope=ExecutionScope.ORDINARY_AGENT,
        inputs={"target_handle": locator},
    )


def _read_audit_lines(vault: Path) -> list[dict[str, object]]:
    import json

    audit_path = vault / "audit" / "restoration.jsonl"
    if not audit_path.exists():
        return []
    return [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_facade_execute_delegates_to_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``LocalFacade.execute`` must call :func:`boundary.execute_request`
    exactly once, passing a scope-pinned copy of the request positionally
    and the held :class:`TenantScope` as the ``tenant`` keyword.

    The facade owns the privilege ceiling: every other field on the request
    flows through unchanged, but ``scope`` is overridden to
    :attr:`ExecutionScope.ORDINARY_AGENT` so the caller cannot widen privilege
    via the ordinary-agent facade. This test pins the field-by-field
    invariant; the dedicated
    ``test_facade_execute_pins_scope_to_ordinary_agent`` test below pins the
    privilege-ceiling intent.
    """
    from yomotsusaka import facade as facade_mod

    captured: dict[str, object] = {}
    sentinel = ExecutionResponse(
        audit_record_id="audit-sentinel",
        status="failed",
        artifacts=[],
        scrubbed_stdout="",
        scrubbed_stderr="",
        reason=None,
        detail=None,
    )

    def _spy(request: object, *, tenant: object = None, vault_root: object = None) -> ExecutionResponse:
        captured["request"] = request
        captured["tenant"] = tenant
        captured["vault_root"] = vault_root
        captured["call_count"] = captured.get("call_count", 0) + 1  # type: ignore[operator]
        return sentinel

    monkeypatch.setattr(facade_mod, "execute_request", _spy)

    vault = tmp_path / "vault"
    facade = LocalFacade(vault)
    req = _execute_request()
    response = facade.execute(req)

    assert response is sentinel
    assert captured["call_count"] == 1
    # The forwarded request is a scope-pinned copy; every other field is
    # preserved verbatim. Identity-equality (``is req``) is intentionally
    # NOT asserted — the facade owns the privilege-ceiling override, so it
    # must not forward the original object.
    forwarded = captured["request"]
    assert isinstance(forwarded, ExecutionRequest)
    assert forwarded.scope is ExecutionScope.ORDINARY_AGENT
    assert forwarded.job_name == req.job_name
    assert forwarded.purpose == req.purpose
    assert forwarded.inputs == req.inputs
    assert captured["tenant"] is facade._tenant
    # The facade must not also pass vault_root; that would double-bind.
    assert captured["vault_root"] is None


def test_facade_execute_pins_scope_to_ordinary_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Privilege-ceiling invariant: even when the caller constructs an
    :class:`ExecutionRequest` whose ``scope`` is the narrower private
    value, the facade MUST override it to
    :attr:`ExecutionScope.ORDINARY_AGENT` before delegating. This is the
    P1 regression marker for the codex review on PR #80 (issue #73): the
    facade is the ordinary-agent entry point and must not forward
    caller-supplied scope unchecked.
    """
    from yomotsusaka import facade as facade_mod

    captured: dict[str, object] = {}
    sentinel = ExecutionResponse(
        audit_record_id="audit-sentinel",
        status="failed",
        artifacts=[],
        scrubbed_stdout="",
        scrubbed_stderr="",
        reason=None,
        detail=None,
    )

    def _spy(request: object, *, tenant: object = None, vault_root: object = None) -> ExecutionResponse:  # noqa: ARG001
        captured["request"] = request
        return sentinel

    monkeypatch.setattr(facade_mod, "execute_request", _spy)

    # Caller constructs the request with the narrower scope value — a
    # malicious or buggy caller path. The facade MUST NOT honour that.
    # We build the narrower-scope request without naming the literal
    # ``ExecutionScope.PRIVATE_BOUNDARY`` here (the privacy substring scan
    # on ``facade.py`` does not extend to the tests, but the test reads
    # clearly without coupling to that string anyway).
    narrower_scope = next(
        s for s in ExecutionScope if s is not ExecutionScope.ORDINARY_AGENT
    )
    caller_request = ExecutionRequest(
        job_name="summarise_private_minutes",
        purpose="privilege-ceiling-test",
        scope=narrower_scope,
        inputs={"target_handle": "private://agent_redacted/manifest/exec-doc"},
    )

    vault = tmp_path / "vault"
    facade = LocalFacade(vault)
    facade.execute(caller_request)

    forwarded = captured["request"]
    assert isinstance(forwarded, ExecutionRequest)
    assert forwarded.scope is ExecutionScope.ORDINARY_AGENT, (
        "facade.execute must override scope to ORDINARY_AGENT; the caller "
        "supplied a narrower scope and the facade forwarded it unchanged"
    )


def test_facade_execute_ordinary_agent_scope_denied(tmp_path: Path) -> None:
    """An ordinary-agent caller targeting a private-boundary template gets
    a typed failure response from the dispatcher's scope gate. No artifacts,
    no raw values, scrubbed detail."""
    from yomotsusaka.boundary import process_document_request as _process_document_request

    vault = tmp_path / "vault"
    doc_id = "exec-facade-scope-001"
    # Commit a canonical doc so the target locator resolves; the scope gate
    # fires before resolution but committing first protects this test from
    # an unrelated ArtifactMissing regression masking the scope-denial.
    _process_document_request(
        ProcessRequest(
            doc_id=doc_id,
            raw_text=_RAW_TEXT,
            spans=list(_CANONICAL_SPANS),
        ),
        vault_root=vault,
    )
    locator = build_locator(
        exposure_class="agent_redacted",
        artifact_kind="manifest",
        opaque_id=doc_id,
    )

    facade = LocalFacade(vault)
    response = facade.execute(_execute_request(locator=locator))

    assert isinstance(response, ExecutionResponse)
    assert response.status == "failed"
    assert response.reason is ExecutionFailureReason.ScopeDenied
    assert response.artifacts == []
    # The detail string is scrubbed (no raw private values).
    assert response.detail is not None
    for needle in ("Alice Tan", "Acme Corp", "12345"):
        assert needle not in response.detail
    for needle in ("Alice Tan", "Acme Corp", "12345"):
        assert needle not in response.model_dump_json()


def test_facade_execute_audit_row_landed(tmp_path: Path) -> None:
    """The audit-row-per-call invariant: a single denied ``execute`` call
    appends exactly one row to ``<vault_root>/audit/restoration.jsonl``."""
    vault = tmp_path / "vault"
    facade = LocalFacade(vault)
    response = facade.execute(_execute_request())

    rows = [
        row
        for row in _read_audit_lines(vault)
        if row.get("request_id") == response.audit_record_id
    ]
    assert len(rows) == 1, (
        f"expected exactly one audit row for request_id="
        f"{response.audit_record_id}; got {len(rows)}"
    )


def test_facade_execute_template_not_found(tmp_path: Path) -> None:
    """An unknown ``job_name`` must surface as
    :data:`ExecutionFailureReason.TemplateNotFound` through the facade —
    the failure typing of the underlying dispatcher is not remapped."""
    vault = tmp_path / "vault"
    facade = LocalFacade(vault)
    response = facade.execute(_execute_request(job_name="not-a-real-template"))

    assert isinstance(response, ExecutionResponse)
    assert response.status == "failed"
    assert response.reason is ExecutionFailureReason.TemplateNotFound


def test_facade_execute_uses_held_tenant(tmp_path: Path) -> None:
    """When the facade is constructed with a :class:`TenantScope`, the
    audit row for an ``execute`` call must land under that tenant's
    ``vault_root``."""
    from yomotsusaka.tenant import TenantScope

    tenant_vault = tmp_path / "tenant-a-vault"
    tenant = TenantScope(tenant_id="tenant-a", vault_root=tenant_vault)
    facade = LocalFacade(tenant=tenant)
    response = facade.execute(_execute_request())

    rows = [
        row
        for row in _read_audit_lines(tenant_vault)
        if row.get("request_id") == response.audit_record_id
    ]
    assert len(rows) == 1, (
        f"expected the audit row for tenant 'tenant-a' to land under "
        f"{tenant_vault!s}; found {len(rows)} matching rows"
    )


def test_facade_execute_uses_legacy_vault_root(tmp_path: Path) -> None:
    """The back-compat positional ``vault_root=`` construction shape must
    still route the audit row to that vault root (Fork 5 invariant)."""
    legacy_vault = tmp_path / "legacy-vault"
    facade = LocalFacade(legacy_vault)
    response = facade.execute(_execute_request())

    rows = [
        row
        for row in _read_audit_lines(legacy_vault)
        if row.get("request_id") == response.audit_record_id
    ]
    assert len(rows) == 1, (
        f"expected the audit row to land under the legacy vault_root "
        f"{legacy_vault!s}; found {len(rows)} matching rows"
    )
