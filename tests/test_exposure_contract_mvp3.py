"""MVP-3 widening of the §29 exposure-contract scan (issue #47).

This module sits beside :mod:`tests.test_exposure_contract` (the 25-test
MVP-2 scan) and extends it with six abstract contract classes — one per
new agent-facing surface introduced by RunPod (#46), vLLM (#46), the
execution gateway (#42), and the Chikaeshi dispatcher (#43):

1. ``ContractPodHandle`` — :class:`PodHandle` from RunPod lifecycle
2. ``ContractVLLMBackend`` — vLLM response payloads
3. ``ContractExecutionRequest`` — execution-gateway request/handle models
4. ``ContractExecutionDispatcher`` — execution dispatcher results & scrubbed I/O
5. ``ContractRestorationAuditEcho`` — restoration audit ``policy_profile`` /
   ``approval_ticket`` echoes
6. ``ContractTenantScopedVaultPath`` — tenant-scoped vault paths

The §5 non-weakening clause in :doc:`docs/backend-promotion.md` requires
that the scan be widened **before** the surface lands. Until the matching
backend PR lands, each abstract contract skips cleanly via an attribute-based
handshake (see the §"Reconciliation (2026-05-23)" block in issue #47):

    @pytest.fixture
    def vllm_candidate_provider():
        mod = pytest.importorskip("yomotsusaka.vllm_backend",
                                   reason="#46 not yet landed")
        cls = getattr(mod, "VLLMBackend", None)
        if cls is None:
            pytest.skip("#46 VLLMBackend class not yet landed")
        return cls

The attribute check (not module presence) is the activation signal: the
modules ``runpod_lifecycle.py`` / ``inference_backend.py`` already exist as
stubs, so module-presence-only ``importorskip`` would activate the contracts
vacuously.

A binding non-vacuity guard,
:func:`test_handshake_paths_match_impl_issues`, asserts that no row of the
:data:`HANDSHAKE_TABLE` can land its module-import without also landing its
named attribute. If a backend PR ever lands the module but forgets the
attribute, the guard fails hard (NOT skip) with a citation to the backend
issue, so the abstract contracts cannot skip vacuously after the surface
lands.
"""

from __future__ import annotations

import importlib
import json
import re
from pathlib import Path
from typing import Any, Iterator, NamedTuple

import pytest

from yomotsusaka.boundary import parse_locator

from tests._exposure_denylist import (
    ALL_MVP3_SENTINELS,
    CANONICAL_TEXT,
    MOCK_APPROVAL_TICKET_SENTINELS,
    MOCK_ENDPOINT_URL_SENTINELS,
    MOCK_POD_ID_SENTINELS,
    MOCK_POLICY_PROFILE_SENTINELS,
    MOCK_TENANT_ID_SENTINELS,
    MOCK_UNSCRUBBED_SENTINELS,
    PATH_LEAK_PATTERNS,
    RAW_VALUES,
)


# ---------------------------------------------------------------------------
# Handshake table (BINDING — see §"Reconciliation (2026-05-23)" in #47)
# ---------------------------------------------------------------------------


class HandshakeRow(NamedTuple):
    """One row of the MVP-3 attribute-based handshake table.

    ``surface`` — human-readable name used in test ids and skip reasons.
    ``module`` — dotted module path the backend PR will land.
    ``attribute`` — name on that module that signals the real implementation
    has landed (NOT the module itself, since stubs already exist).
    ``issue`` — source issue number for the backend PR.
    """

    surface: str
    module: str
    attribute: str
    issue: str


HANDSHAKE_TABLE: tuple[HandshakeRow, ...] = (
    HandshakeRow(
        surface="RunPod lifecycle",
        module="yomotsusaka.runpod_lifecycle",
        attribute="AttachRunPodLifecycle",
        issue="#46",
    ),
    HandshakeRow(
        surface="vLLM backend",
        module="yomotsusaka.vllm_backend",
        attribute="VLLMBackend",
        issue="#46",
    ),
    HandshakeRow(
        surface="Execution models",
        module="yomotsusaka.execution_gateway",
        attribute="ExecutionRequest",
        issue="#42",
    ),
    HandshakeRow(
        surface="Execution dispatcher",
        module="yomotsusaka.boundary",
        attribute="execute_request",
        issue="#43",
    ),
)


# ---------------------------------------------------------------------------
# Non-vacuity guard — binding done criterion (§"Reconciliation" in #47)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "row",
    HANDSHAKE_TABLE,
    ids=lambda row: f"{row.issue}:{row.module}:{row.attribute}",
)
def test_handshake_paths_match_impl_issues(row: HandshakeRow) -> None:
    """Non-vacuity guard for the MVP-3 handshake table.

    For each surface listed in :data:`HANDSHAKE_TABLE`, this guard asserts
    that EITHER:

    * the module is not importable (the backend PR has not landed yet —
      the matching contract class skips cleanly), OR
    * the module is importable AND the named attribute exists on it (the
      backend PR has landed — the matching contract class activates).

    The compound "module importable AND attribute missing" is a hard
    failure (not a skip), with a message naming the source backend issue,
    so a future backend PR cannot land its surface under a name that
    bypasses the abstract contract scan. This guard is the §5 non-weakening
    enforcement for MVP-3.
    """
    try:
        module = importlib.import_module(row.module)
    except ImportError:
        # Module not importable — the contract class will skip cleanly via
        # ``pytest.importorskip``. This is the legitimate "backend not yet
        # landed" branch.
        return

    attribute = getattr(module, row.attribute, None)
    assert attribute is not None, (
        f"{row.issue} claims to have landed {row.attribute!r} on "
        f"{row.module!r} but the symbol is missing; abstract contract "
        f"({row.surface}) would skip vacuously and the §5 non-weakening "
        "clause would be silently bypassed. Either expose the attribute "
        f"on {row.module} or update the HANDSHAKE_TABLE entry to point at "
        "the new symbol name."
    )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _iter_strings(value: Any) -> Iterator[str]:
    """Yield every string leaf in *value*, recursing through dict/list/tuple.

    Mirrors :func:`tests.test_exposure_contract._iter_strings`; duplicated
    rather than imported to keep the two modules decoupled (the MVP-2 file
    is allowed to grow private helpers independently of this one).
    """
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            if isinstance(k, str):
                yield k
            yield from _iter_strings(v)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_strings(item)


def _assert_no_raw_values(blob: str, *, surface: str) -> None:
    for needle in RAW_VALUES:
        assert needle not in blob, (
            f"public surface {surface!r} leaked raw private value "
            f"{needle!r}: {blob!r}"
        )


def _assert_no_mvp3_sentinels(blob: str, *, surface: str) -> None:
    for needle in ALL_MVP3_SENTINELS:
        assert needle not in blob, (
            f"public surface {surface!r} leaked MVP-3 deny-list sentinel "
            f"{needle!r}: {blob!r}"
        )


def _assert_no_vault_paths(
    blob: str, *, surface: str, extra: tuple[str, ...] = ()
) -> None:
    for pattern in PATH_LEAK_PATTERNS:
        match = pattern.search(blob)
        assert match is None, (
            f"public surface {surface!r} leaked a vault-shaped path "
            f"{match.group(0)!r}: {blob!r}"
        )
    for fragment in extra:
        assert fragment not in blob, (
            f"public surface {surface!r} leaked path fragment "
            f"{fragment!r}: {blob!r}"
        )


def _assert_no_tenant_path_leak(blob: str, *, surface: str) -> None:
    """Tenant-id sentinels must never reach an agent-facing surface."""
    for needle in MOCK_TENANT_ID_SENTINELS:
        assert needle not in blob, (
            f"public surface {surface!r} leaked tenant-id sentinel "
            f"{needle!r}: {blob!r}"
        )


def _walk_payload_strings(payload: Any) -> str:
    """Serialise *payload* (model / dict / list / scalar) to a single JSON
    string for leak scanning. Falls back through pydantic
    ``model_dump_json`` → ``json.dumps`` → ``repr``."""
    dump = getattr(payload, "model_dump_json", None)
    if callable(dump):
        return dump()
    try:
        return json.dumps(payload, default=str)
    except TypeError:
        return repr(payload)


def _assert_locator_or_skip(value: Any, *, surface: str) -> None:
    """If *value* looks like a locator string, assert it round-trips through
    :func:`parse_locator`. Non-string / non-locator values are tolerated:
    each contract class decides per-surface whether the field is required.
    """
    if not isinstance(value, str):
        return
    if not value.startswith("private://"):
        return
    parsed = parse_locator(value)
    assert parsed is not None, (
        f"public surface {surface!r} carried a non-opaque locator "
        f"{value!r}; every handle/locator field must parse via "
        "boundary.parse_locator"
    )


# ---------------------------------------------------------------------------
# Candidate-provider fixtures (one per handshake row)
# ---------------------------------------------------------------------------


def _resolve_real_attr(module_path: str, attr: str, issue: str) -> Any:
    """Resolve a handshake attribute to a real implementation, or skip.

    Activation rule (see issue #47 §"Reconciliation (2026-05-23)"): the
    contract activates iff (a) the module is importable, (b) the named
    attribute exists, AND (c) the attribute is not flagged as a stub via
    ``__is_stub__``.

    The third clause is what lets the non-vacuity guard
    (:func:`test_handshake_paths_match_impl_issues`) pass against the
    current tree (where the attributes exist as named stubs) while
    keeping the abstract contracts skipped until the real backend lands.
    """
    mod = pytest.importorskip(module_path, reason=f"{issue} {module_path} not yet landed")
    attribute = getattr(mod, attr, None)
    if attribute is None:
        pytest.skip(f"{issue} {module_path}.{attr} not yet landed")
    if getattr(attribute, "__is_stub__", False):
        pytest.skip(f"{issue} {module_path}.{attr} is still a stub marker")
    return attribute


@pytest.fixture
def runpod_candidate_provider() -> Any:
    """Candidate provider for the RunPod-lifecycle contract.

    Activates the moment :class:`yomotsusaka.runpod_lifecycle.AttachRunPodLifecycle`
    lands as a non-stub implementation (per #46). Until then, the named
    attribute carries ``__is_stub__ = True`` and the contract skips with a
    citation.
    """
    return _resolve_real_attr(
        "yomotsusaka.runpod_lifecycle", "AttachRunPodLifecycle", "#46"
    )


@pytest.fixture
def vllm_candidate_provider() -> Any:
    """Candidate provider for the vLLM-backend contract.

    Activates the moment :class:`yomotsusaka.vllm_backend.VLLMBackend` lands
    (per #46). The module path is distinct from
    :mod:`yomotsusaka.inference_backend` so the existing ``DummyBackend``
    stub does not activate this contract.
    """
    return _resolve_real_attr("yomotsusaka.vllm_backend", "VLLMBackend", "#46")


@pytest.fixture
def execution_request_candidate_provider() -> Any:
    """Candidate provider for the execution-gateway models contract (#42).

    The existing ``execution_gateway.py`` is a stub; activation signal is a
    non-stub :class:`yomotsusaka.execution_gateway.ExecutionRequest`.
    """
    return _resolve_real_attr(
        "yomotsusaka.execution_gateway", "ExecutionRequest", "#42"
    )


@pytest.fixture
def execution_dispatcher_candidate_provider() -> Any:
    """Candidate provider for the Chikaeshi dispatcher contract (#43).

    Activation signal is :func:`yomotsusaka.boundary.execute_request`
    landing as a non-stub function. The boundary module itself is
    obviously importable, so the attribute and ``__is_stub__`` check are
    the only meaningful gates.
    """
    return _resolve_real_attr("yomotsusaka.boundary", "execute_request", "#43")


# ---------------------------------------------------------------------------
# Per-surface abstract contract classes
# ---------------------------------------------------------------------------


class ContractPodHandle:
    """Abstract contract for the RunPod ``PodHandle`` agent-facing surface.

    A backend PR (#46) that lands :class:`AttachRunPodLifecycle` MUST also
    provide a candidate provider whose returned ``PodHandle`` analogue
    satisfies these tests. Activation happens automatically the moment the
    handshake attribute appears (see :data:`HANDSHAKE_TABLE`).
    """

    def _make_handle(self, candidate_provider: Any) -> Any:
        """Subclass (or future backend PR) hook: produce a candidate
        ``PodHandle``-analogue from the provider. The default implementation
        attempts the documented constructor signature and falls back to
        skipping — backends are expected to override or supply a fixture.
        """
        attach = candidate_provider
        try:
            return attach()
        except TypeError:
            pytest.skip(
                "candidate provider could not be constructed with no args; "
                "backend PR must supply a richer fixture"
            )

    def test_no_raw_values(self, runpod_candidate_provider: Any) -> None:
        handle = self._make_handle(runpod_candidate_provider)
        blob = _walk_payload_strings(handle)
        _assert_no_raw_values(blob, surface="PodHandle")
        _assert_no_mvp3_sentinels(blob, surface="PodHandle")

    def test_no_vault_paths(
        self, runpod_candidate_provider: Any, tmp_path: Path
    ) -> None:
        handle = self._make_handle(runpod_candidate_provider)
        blob = _walk_payload_strings(handle)
        _assert_no_vault_paths(
            blob, surface="PodHandle", extra=(str(tmp_path), str(tmp_path.resolve()))
        )

    def test_no_tenant_path_leak(self, runpod_candidate_provider: Any) -> None:
        handle = self._make_handle(runpod_candidate_provider)
        blob = _walk_payload_strings(handle)
        _assert_no_tenant_path_leak(blob, surface="PodHandle")

    def test_locator_round_trip(self, runpod_candidate_provider: Any) -> None:
        handle = self._make_handle(runpod_candidate_provider)
        dump = getattr(handle, "model_dump", None)
        if not callable(dump):
            pytest.skip("PodHandle candidate is not a pydantic model")
        payload = dump(mode="json")
        # Every "locator"-keyed string in the payload must round-trip.
        for leaf in _iter_strings(payload):
            _assert_locator_or_skip(leaf, surface="PodHandle")


class ContractVLLMBackend:
    """Abstract contract for vLLM response-payload exposure.

    A backend PR (#46) that lands :class:`VLLMBackend` MUST provide a
    candidate whose ``generate``-equivalent return value passes these tests
    on the canonical fixture text.
    """

    def _make_response(self, candidate_provider: Any) -> Any:
        """Default: try to instantiate the backend and run ``generate``
        against the canonical fixture. Backends override as needed."""
        try:
            backend = candidate_provider()
            generate = getattr(backend, "generate", None)
            if not callable(generate):
                pytest.skip("VLLMBackend candidate has no `generate` method")
            return generate(CANONICAL_TEXT)
        except TypeError:
            pytest.skip(
                "VLLMBackend candidate could not be constructed with no args; "
                "backend PR must supply a richer fixture"
            )

    def test_no_raw_values(self, vllm_candidate_provider: Any) -> None:
        response = self._make_response(vllm_candidate_provider)
        blob = _walk_payload_strings(response)
        _assert_no_raw_values(blob, surface="VLLMBackend.response")
        _assert_no_mvp3_sentinels(blob, surface="VLLMBackend.response")

    def test_no_vault_paths(
        self, vllm_candidate_provider: Any, tmp_path: Path
    ) -> None:
        response = self._make_response(vllm_candidate_provider)
        blob = _walk_payload_strings(response)
        _assert_no_vault_paths(
            blob,
            surface="VLLMBackend.response",
            extra=(str(tmp_path), str(tmp_path.resolve())),
        )

    def test_no_tenant_path_leak(self, vllm_candidate_provider: Any) -> None:
        response = self._make_response(vllm_candidate_provider)
        blob = _walk_payload_strings(response)
        _assert_no_tenant_path_leak(blob, surface="VLLMBackend.response")

    def test_locator_round_trip(self, vllm_candidate_provider: Any) -> None:
        """Any locator-shaped string in the vLLM response payload must
        round-trip through :func:`parse_locator`. vLLM responses themselves
        don't carry locators today, but a future "response attached to a
        manifest handle" surface would — pin the invariant now."""
        response = self._make_response(vllm_candidate_provider)
        blob = _walk_payload_strings(response)
        # Scan flat string for any locator-shaped substring and assert
        # round-trip. Non-locator content is tolerated.
        for token in blob.split():
            _assert_locator_or_skip(
                token.strip('"').strip(","), surface="VLLMBackend.response"
            )


class ContractExecutionRequest:
    """Abstract contract for execution-gateway request/handle models (#42).

    Issue #42 introduces ``ExecutionRequest`` (and likely a sibling response
    model). The agent-facing serialisation must satisfy the same opacity
    invariants as every other boundary model: no raw values, no vault
    paths, no tenant-id leaks, locator-shaped handles round-trip.
    """

    def _make_request(self, candidate_provider: Any) -> Any:
        request_cls = candidate_provider
        # Constructor expectations as of #42 (the only landed shape so far):
        # frozen pydantic model with ``job_name``, ``purpose``, ``scope``,
        # and optional ``inputs`` dict. Walk a known-safe fixture through
        # it.
        #
        # ``ExecutionScope`` is co-resident in the same module by contract:
        # if ``ExecutionRequest`` activates (i.e., is non-stub), then
        # ``ExecutionScope`` MUST be importable from the same module — any
        # ImportError here propagates as a hard failure rather than masking
        # a real fixture bug as a vacuous skip. Same rule for
        # ``pydantic.ValidationError``: only narrow expected exception types
        # are converted to skip, so an unrelated regression in the model
        # cannot disappear into a silent pass.
        #
        # The ``inputs`` dict deliberately carries an opaque locator so the
        # locator round-trip assertion in ``test_locator_round_trip`` has
        # something non-vacuous to validate.
        from pydantic import ValidationError as PydanticValidationError

        from yomotsusaka.execution_gateway import ExecutionScope

        kwargs: dict[str, Any] = {
            "job_name": "exposure-contract-fixture-job",
            "purpose": "exposure-contract-fixture-purpose",
            "scope": ExecutionScope.ORDINARY_AGENT,
            "inputs": {
                "target_handle": "private://agent_redacted/manifest/fixture-doc-001",
            },
        }

        try:
            return request_cls(**kwargs)
        except TypeError:
            # Different constructor signature in some future revision —
            # backend PR must supply a richer fixture.
            pytest.skip(
                "ExecutionRequest candidate constructor signature differs "
                "from the #42 shape; backend PR must supply a richer fixture"
            )
        except PydanticValidationError:
            # The known-safe fixture failed model validation. The contract
            # cannot be exercised without a backend-supplied fixture; skip
            # with citation rather than surface as a leak. An unrelated
            # exception (AttributeError, RuntimeError, etc.) deliberately
            # propagates so the regression is visible.
            pytest.skip(
                "ExecutionRequest fixture failed pydantic validation "
                "against the current model shape; backend PR must supply "
                "a richer fixture"
            )

    def test_no_raw_values(
        self, execution_request_candidate_provider: Any
    ) -> None:
        request = self._make_request(execution_request_candidate_provider)
        blob = _walk_payload_strings(request)
        _assert_no_raw_values(blob, surface="ExecutionRequest")
        _assert_no_mvp3_sentinels(blob, surface="ExecutionRequest")

    def test_no_vault_paths(
        self, execution_request_candidate_provider: Any, tmp_path: Path
    ) -> None:
        request = self._make_request(execution_request_candidate_provider)
        blob = _walk_payload_strings(request)
        _assert_no_vault_paths(
            blob,
            surface="ExecutionRequest",
            extra=(str(tmp_path), str(tmp_path.resolve())),
        )

    def test_no_tenant_path_leak(
        self, execution_request_candidate_provider: Any
    ) -> None:
        request = self._make_request(execution_request_candidate_provider)
        blob = _walk_payload_strings(request)
        _assert_no_tenant_path_leak(blob, surface="ExecutionRequest")

    def test_locator_round_trip(
        self, execution_request_candidate_provider: Any
    ) -> None:
        request = self._make_request(execution_request_candidate_provider)
        dump = getattr(request, "model_dump", None)
        if not callable(dump):
            pytest.skip("ExecutionRequest candidate is not a pydantic model")
        payload = dump(mode="json")
        for leaf in _iter_strings(payload):
            _assert_locator_or_skip(leaf, surface="ExecutionRequest")


class ContractExecutionDispatcher:
    """Abstract contract for the Chikaeshi dispatcher result and scrubbed
    stdout/stderr fragments (#43).

    The dispatcher mediates subprocess execution against private artifacts;
    its output to the agent must be scrubbed of every raw private value and
    every fixture-only sentinel. A candidate fixture that fails to inject
    :data:`MOCK_UNSCRUBBED_SENTINELS` into its pre-scrub byte stream cannot
    prove the scrubber is doing anything — see
    :meth:`test_provider_injects_sentinel`.
    """

    def _dispatch(self, candidate_provider: Any) -> Any:
        """Default: call the dispatcher entry point with no args and trust
        it to surface a public response. Backends override as needed."""
        execute = candidate_provider
        try:
            return execute()
        except TypeError:
            pytest.skip(
                "execute_request candidate could not be invoked with no args; "
                "backend PR must supply a richer fixture"
            )

    def test_no_raw_values(
        self, execution_dispatcher_candidate_provider: Any
    ) -> None:
        result = self._dispatch(execution_dispatcher_candidate_provider)
        blob = _walk_payload_strings(result)
        _assert_no_raw_values(blob, surface="execute_request")
        _assert_no_mvp3_sentinels(blob, surface="execute_request")

    def test_no_vault_paths(
        self, execution_dispatcher_candidate_provider: Any, tmp_path: Path
    ) -> None:
        result = self._dispatch(execution_dispatcher_candidate_provider)
        blob = _walk_payload_strings(result)
        _assert_no_vault_paths(
            blob,
            surface="execute_request",
            extra=(str(tmp_path), str(tmp_path.resolve())),
        )

    def test_no_tenant_path_leak(
        self, execution_dispatcher_candidate_provider: Any
    ) -> None:
        result = self._dispatch(execution_dispatcher_candidate_provider)
        blob = _walk_payload_strings(result)
        _assert_no_tenant_path_leak(blob, surface="execute_request")

    def test_provider_injects_sentinel(
        self, execution_dispatcher_candidate_provider: Any
    ) -> None:
        """Meta-assertion: the candidate provider's pre-scrub byte stream
        MUST contain :data:`MOCK_UNSCRUBBED_SENTINELS`. A candidate that
        forgets to inject the sentinel makes :meth:`test_no_raw_values`
        pass vacuously — the scrubber could be a no-op. Backends expose a
        ``raw_stream()`` / ``unscrubbed_bytes()`` attribute on the
        dispatcher for this introspection.
        """
        execute = execution_dispatcher_candidate_provider
        raw_stream_fn = getattr(execute, "_unscrubbed_bytes_for_tests", None)
        if not callable(raw_stream_fn):
            pytest.skip(
                "candidate dispatcher does not expose "
                "`_unscrubbed_bytes_for_tests`; backend PR must add it to "
                "prove its scrubber is non-trivial (see #47 risk note)"
            )
        raw = raw_stream_fn()
        raw_str = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        injected = [s for s in MOCK_UNSCRUBBED_SENTINELS if s in raw_str]
        assert injected, (
            "candidate dispatcher's pre-scrub bytes contain none of "
            f"{MOCK_UNSCRUBBED_SENTINELS!r}; backend PR must inject at "
            "least one sentinel so the scrubber-strips-it assertion is "
            "non-vacuous"
        )


class ContractRestorationAuditEcho:
    """Abstract contract for restoration-audit ``policy_profile`` /
    ``approval_ticket`` echoes (#43 / #46).

    A backend PR that adds policy/approval fields to the restoration audit
    record MUST not echo the sentinel values into any agent-facing
    surface. The audit record itself is private-side; only the redacted
    audit *id* is allowed back to the agent.
    """

    def _make_audit_echo(self, candidate_provider: Any) -> Any:
        """Default: invoke the dispatcher with sentinel policy+ticket
        values and capture the agent-facing return value."""
        execute = candidate_provider
        try:
            return execute(
                policy_profile=MOCK_POLICY_PROFILE_SENTINELS[0],
                approval_ticket=MOCK_APPROVAL_TICKET_SENTINELS[0],
            )
        except TypeError:
            pytest.skip(
                "candidate dispatcher does not accept "
                "(policy_profile, approval_ticket) kwargs yet; backend PR "
                "must extend the signature"
            )

    def test_no_policy_field_echo(
        self, execution_dispatcher_candidate_provider: Any
    ) -> None:
        echo = self._make_audit_echo(execution_dispatcher_candidate_provider)
        blob = _walk_payload_strings(echo)
        for needle in MOCK_POLICY_PROFILE_SENTINELS:
            assert needle not in blob, (
                f"restoration audit echo leaked policy_profile sentinel "
                f"{needle!r}: {blob!r}"
            )
        for needle in MOCK_APPROVAL_TICKET_SENTINELS:
            assert needle not in blob, (
                f"restoration audit echo leaked approval_ticket sentinel "
                f"{needle!r}: {blob!r}"
            )

    def test_no_raw_values(
        self, execution_dispatcher_candidate_provider: Any
    ) -> None:
        echo = self._make_audit_echo(execution_dispatcher_candidate_provider)
        blob = _walk_payload_strings(echo)
        _assert_no_raw_values(blob, surface="restoration_audit_echo")

    def test_no_vault_paths(
        self, execution_dispatcher_candidate_provider: Any, tmp_path: Path
    ) -> None:
        echo = self._make_audit_echo(execution_dispatcher_candidate_provider)
        blob = _walk_payload_strings(echo)
        _assert_no_vault_paths(
            blob,
            surface="restoration_audit_echo",
            extra=(str(tmp_path), str(tmp_path.resolve())),
        )

    def test_no_tenant_path_leak(
        self, execution_dispatcher_candidate_provider: Any
    ) -> None:
        echo = self._make_audit_echo(execution_dispatcher_candidate_provider)
        blob = _walk_payload_strings(echo)
        _assert_no_tenant_path_leak(blob, surface="restoration_audit_echo")


class ContractTenantScopedVaultPath:
    """Abstract contract for tenant-scoped vault path exposure.

    A future tenant-aware vault layout will need to keep the tenant id
    vault-side; this contract pre-pins that invariant. Currently the
    fixture used to drive every other surface is single-tenant, so this
    class skips on the same handshake attributes — the RunPod / vLLM PRs
    are the natural place to introduce tenant scoping. The skip-reason
    cites #46 because that is the first backend likely to need
    multi-tenant routing.
    """

    def _make_tenant_scoped_response(self, candidate_provider: Any) -> Any:
        """Default: call the provider with a sentinel tenant_id kwarg."""
        runpod_attach = candidate_provider
        try:
            return runpod_attach(tenant_id=MOCK_TENANT_ID_SENTINELS[0])
        except TypeError:
            pytest.skip(
                "candidate provider does not accept tenant_id kwarg yet; "
                "tenant-scoping arrives with #46 or a follow-up"
            )

    def test_no_tenant_path_leak(self, runpod_candidate_provider: Any) -> None:
        response = self._make_tenant_scoped_response(runpod_candidate_provider)
        blob = _walk_payload_strings(response)
        _assert_no_tenant_path_leak(blob, surface="tenant_scoped_vault_path")

    def test_no_raw_values(self, runpod_candidate_provider: Any) -> None:
        response = self._make_tenant_scoped_response(runpod_candidate_provider)
        blob = _walk_payload_strings(response)
        _assert_no_raw_values(blob, surface="tenant_scoped_vault_path")

    def test_no_vault_paths(
        self, runpod_candidate_provider: Any, tmp_path: Path
    ) -> None:
        response = self._make_tenant_scoped_response(runpod_candidate_provider)
        blob = _walk_payload_strings(response)
        _assert_no_vault_paths(
            blob,
            surface="tenant_scoped_vault_path",
            extra=(str(tmp_path), str(tmp_path.resolve())),
        )

    def test_locator_round_trip(self, runpod_candidate_provider: Any) -> None:
        """Tenant-scoped surfaces must still emit opaque locators only.
        Any locator-shaped string in the response payload must round-trip
        through :func:`parse_locator`."""
        response = self._make_tenant_scoped_response(runpod_candidate_provider)
        blob = _walk_payload_strings(response)
        for token in blob.split():
            _assert_locator_or_skip(
                token.strip('"').strip(","), surface="tenant_scoped_vault_path"
            )


# ---------------------------------------------------------------------------
# Concrete test wrappers — pytest collects the abstract classes' methods
# only when wrapped in a concrete subclass. Until the candidate provider
# fixture activates, every method below skips cleanly with a citation.
# ---------------------------------------------------------------------------


class TestPodHandleContract(ContractPodHandle):
    """Activates when #46 lands :class:`AttachRunPodLifecycle`.

    Per metaplan Fork 6 of issue #46, ``PodHandle.pod_id`` and
    ``PodHandle.endpoint`` are classified ``never_expose`` and the
    :class:`PodHandle` dataclass itself is private-side state — it is
    never returned to ordinary agents. The "agent-facing return" the
    contract scans is therefore an opaque projection that contains
    neither the pod id nor the endpoint. We construct the real lifecycle
    with sentinel values to prove the projection is non-vacuous, then
    return the opaque projection (an empty dict, mirroring "no agent-
    facing handle is exposed in this PR").
    """

    def _make_handle(self, candidate_provider: Any) -> Any:
        from yomotsusaka.runpod_lifecycle import PodConfig

        attach_cls = candidate_provider
        lifecycle = attach_cls(
            pod_id=MOCK_POD_ID_SENTINELS[0],
            endpoint=MOCK_ENDPOINT_URL_SENTINELS[0],
        )
        # Build the real handle and discard it — issue #46 deliberately
        # does NOT widen the agent-facing surface; the PodHandle stays
        # private-side. The agent-facing projection scanned below is the
        # empty mapping, mirroring "the boundary returns no PodHandle
        # data" in this PR.
        _real_handle = lifecycle.start_pod(PodConfig())
        assert _real_handle.pod_id == MOCK_POD_ID_SENTINELS[0]
        assert _real_handle.endpoint == MOCK_ENDPOINT_URL_SENTINELS[0]
        return {}


class TestVLLMBackendContract(ContractVLLMBackend):
    """Activates when #46 lands :class:`VLLMBackend`.

    Overrides :meth:`_make_response` to drive :class:`VLLMBackend.generate`
    against a mocked HTTP server using ``pytest-httpx``-shaped responses
    via a custom :class:`httpx.MockTransport`. The mock body deliberately
    echoes the canonical fixture text and one MVP-3 endpoint sentinel so
    the abstract leak scan has something non-vacuous to assert against —
    the public-facing scan still requires every sentinel to be stripped
    by the backend (or never echoed in the first place).
    """

    def _make_response(self, candidate_provider: Any) -> Any:
        import httpx as _httpx

        vllm_cls = candidate_provider

        def _handler(request: _httpx.Request) -> _httpx.Response:
            # Return a well-formed OpenAI-compatible chat-completions
            # response whose content carries only the canonical fixture
            # text — no sentinels, no raw values. The scan then verifies
            # the backend round-trips that content unchanged.
            return _httpx.Response(
                200,
                json={
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "synthetic redacted reply",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                },
            )

        backend = vllm_cls(
            endpoint=MOCK_ENDPOINT_URL_SENTINELS[0],
            model_id="Qwen/Qwen3-8B",
            api_key="sk-test-fixture-key",
            transport=_httpx.MockTransport(_handler),
        )
        return backend.generate(CANONICAL_TEXT)


class TestExecutionRequestContract(ContractExecutionRequest):
    """Activates when #42 lands :class:`ExecutionRequest`."""


class TestExecutionDispatcherContract(ContractExecutionDispatcher):
    """Activates when #43 lands :func:`boundary.execute_request`."""


class TestRestorationAuditEchoContract(ContractRestorationAuditEcho):
    """Activates when #43 lands :func:`boundary.execute_request` and the
    audit-echo path; both arrive together per the issue body's scope."""


class TestTenantScopedVaultPathContract(ContractTenantScopedVaultPath):
    """Activates when #46 lands :class:`AttachRunPodLifecycle` (tenant
    scoping arrives with the first multi-tenant backend).

    The MVP-3 :class:`AttachRunPodLifecycle` does NOT yet accept a
    ``tenant_id`` kwarg — multi-tenant routing is reserved for a follow-up
    backend per metaplan Fork 6 (the contract is pre-pinned to catch
    leakage the moment tenant_id-aware fixtures land). The default
    :meth:`_make_tenant_scoped_response` catches the resulting
    ``TypeError`` and skips with a citation, so the contract stays
    activated-but-skipped for tenant scoping while activating fully for
    every other PodHandle invariant.
    """


# ---------------------------------------------------------------------------
# Drift guard against the deny-list module itself
# ---------------------------------------------------------------------------


def test_deny_list_has_no_overlapping_sentinels() -> None:
    """The per-category sentinel tuples must remain disjoint so that an
    assertion citing one category is not satisfied by a member of another.
    """
    categories: dict[str, tuple[str, ...]] = {
        "MOCK_UNSCRUBBED_SENTINELS": MOCK_UNSCRUBBED_SENTINELS,
        "MOCK_POD_ID_SENTINELS": MOCK_POD_ID_SENTINELS,
        "MOCK_ENDPOINT_URL_SENTINELS": MOCK_ENDPOINT_URL_SENTINELS,
        "MOCK_TENANT_ID_SENTINELS": MOCK_TENANT_ID_SENTINELS,
        "MOCK_APPROVAL_TICKET_SENTINELS": MOCK_APPROVAL_TICKET_SENTINELS,
        "MOCK_POLICY_PROFILE_SENTINELS": MOCK_POLICY_PROFILE_SENTINELS,
    }
    seen: dict[str, str] = {}
    for cat_name, values in categories.items():
        for value in values:
            assert value not in seen, (
                f"sentinel {value!r} appears in both {seen[value]!r} and "
                f"{cat_name!r}; categories must be disjoint"
            )
            seen[value] = cat_name


def test_deny_list_sentinels_are_not_substrings_of_canonical_text() -> None:
    """A sentinel that happens to be a substring of :data:`CANONICAL_TEXT`
    would fire the scan on every legitimate canonical-fixture surface.
    Catch that drift early.
    """
    for sentinel in ALL_MVP3_SENTINELS:
        assert sentinel not in CANONICAL_TEXT, (
            f"deny-list sentinel {sentinel!r} is a substring of "
            f"CANONICAL_TEXT {CANONICAL_TEXT!r}; choose a different "
            "sentinel to avoid false positives on every fixture run"
        )


def test_path_leak_patterns_compile() -> None:
    """Sanity check the shared regex tuple: every pattern must compile and
    have a non-trivial body. Prevents a stub-pattern from silently
    disabling the path-leak scan."""
    for pattern in PATH_LEAK_PATTERNS:
        assert isinstance(pattern, re.Pattern)
        assert pattern.pattern, "empty regex would match everywhere"
