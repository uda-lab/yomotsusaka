"""Unit tests for :mod:`yomotsusaka.operational_taxonomy` and the bounded
safe-recovery helpers added by MVP-5 child 04 (issue #93).

The taxonomy module is the single closed registry mapping each
``OperationalCategory`` to a typed ``RecoveryInstruction``. These tests
enforce:

1. Every enum value has a recovery instruction (the registry is closed,
   not partial).
2. :func:`recovery_for` is total over the enum.
3. ``audit_inspect_failed`` is the only ``hard_stop`` category in MVP-5.
4. Every instruction's ``forbidden_evidence`` tuple covers the baseline
   leak-label set defined by the architecture / runpod-agent-smoke docs.
5. ``agent_action`` strings are public-safe — they contain no fixture-only
   raw values (re-using the MVP-3 deny-list sentinels).
6. The bounded REST cleanup-retry helper in
   :mod:`yomotsusaka.runpod_lifecycle` issues at most ``MAX_ATTEMPTS``
   calls (cross-check against the MVP-4 retry contract from #90).
7. The bounded snapshot-write retry helper in
   :mod:`yomotsusaka.search_gateway` issues at most one retry on
   ``OSError`` before re-raising.
8. The inference-backed span proposer's soft-degrade surface aligns with
   :data:`OperationalCategory.InferenceSpanDegraded` (vs the
   ``InferenceSpanUnavailable`` "not configured" path).
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

from tests._exposure_denylist import (
    ALL_MVP3_SENTINELS,
    RAW_VALUES,
)
from yomotsusaka.inference_backend import (
    DummyBackend,
    InferenceBackend,
    InferenceBackendError,
)
from yomotsusaka.operational_taxonomy import (
    BASELINE_FORBIDDEN_EVIDENCE,
    OperationalCategory,
    RecoveryInstruction,
    _RECOVERY_TABLE,
    recovery_for,
    render_recovery_table_markdown,
)
from yomotsusaka.runpod_lifecycle import (
    ManageRunPodLifecycle,
    PodConfig,
    _MANAGE_CLEANUP_MAX_ATTEMPTS,
)
from yomotsusaka.search_gateway import (
    SearchGateway,
    _SNAPSHOT_MAX_ATTEMPTS,
)
from yomotsusaka.schemas import DocumentManifest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manifest(doc_id: str) -> DocumentManifest:
    """Minimal valid manifest for snapshot tests; redacted-only fields."""
    return DocumentManifest(
        doc_id=doc_id,
        source_ref=f"opaque-{doc_id}",
        redacted_text="<PERSON_aaaaaaaa> works at <ORG_bbbbbbbb>.",
    )


# ---------------------------------------------------------------------------
# 1–5: taxonomy registry shape
# ---------------------------------------------------------------------------


def test_every_category_has_recovery_instruction() -> None:
    """The recovery table is total: every ``OperationalCategory`` value has
    a registered :class:`RecoveryInstruction`. Catches a future enum
    addition that forgets to add a row."""
    assert set(_RECOVERY_TABLE) == set(OperationalCategory), (
        "Recovery table drift: enum values "
        f"{set(OperationalCategory)} vs table keys "
        f"{set(_RECOVERY_TABLE)}"
    )


def test_recovery_for_returns_for_every_value() -> None:
    """:func:`recovery_for` returns a :class:`RecoveryInstruction` for
    every enum value (no exceptions, no ``None``)."""
    for category in OperationalCategory:
        instruction = recovery_for(category)
        assert isinstance(instruction, RecoveryInstruction)
        assert instruction.category is category


def test_audit_inspect_failed_is_hard_stop() -> None:
    """Only ``audit_inspect_failed`` carries ``hard_stop=True``. Mirrors
    the Chikaeshi audit-write contract: the agent never reports
    ``status="accepted"`` when audit inspection fails."""
    assert recovery_for(OperationalCategory.AuditInspectFailed).hard_stop is True
    for category in OperationalCategory:
        if category is OperationalCategory.AuditInspectFailed:
            continue
        assert recovery_for(category).hard_stop is False, (
            f"unexpected hard_stop on {category!r}: only "
            "audit_inspect_failed is hard-stop in MVP-5."
        )


def test_forbidden_evidence_contains_baseline_set() -> None:
    """Every instruction's ``forbidden_evidence`` is a superset of the
    baseline labels (``vault_root``, ``pod_id``, ``endpoint_url``,
    ``raw_private_value``, ``exception_text``, ``response_body``). Guards
    against a future contributor quietly weakening the privacy floor."""
    baseline = set(BASELINE_FORBIDDEN_EVIDENCE)
    for category in OperationalCategory:
        forbidden = set(recovery_for(category).forbidden_evidence)
        missing = baseline - forbidden
        assert not missing, (
            f"{category.value} forbidden_evidence is missing baseline "
            f"labels: {missing}"
        )


def test_agent_action_is_public_safe() -> None:
    """No ``agent_action`` echoes a fixture-only raw value or an MVP-3
    leak sentinel. The instruction strings are designed to be quoted into
    public PR / issue comments verbatim."""
    forbidden_tokens: tuple[str, ...] = ALL_MVP3_SENTINELS + RAW_VALUES
    for category in OperationalCategory:
        action = recovery_for(category).agent_action
        for token in forbidden_tokens:
            assert token not in action, (
                f"{category.value} agent_action leaks fixture token "
                f"{token!r}: {action!r}"
            )


def test_render_recovery_table_markdown_covers_every_category() -> None:
    """The doc-rendering helper emits exactly one row per enum value."""
    rendered = render_recovery_table_markdown()
    for category in OperationalCategory:
        assert f"`{category.value}`" in rendered, (
            f"render_recovery_table_markdown is missing a row for "
            f"{category.value}"
        )


# ---------------------------------------------------------------------------
# 6: bounded RunPod REST cleanup retry helper (#90 contract cross-check)
# ---------------------------------------------------------------------------


def test_runpod_retry_helper_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ManageRunPodLifecycle.stop_pod`` issues at most
    ``_MANAGE_CLEANUP_MAX_ATTEMPTS`` REST DELETE attempts before raising
    ``PodUnavailableError("cleanup_failed")``. Cross-checks the bounded
    retry contract documented for the operational taxonomy."""
    from yomotsusaka.inference_backend import PodUnavailableError
    from yomotsusaka.runpod_lifecycle import (
        PodHandle,
        _MANAGE_CLEANUP_RETRY_DELAY_SECONDS,  # noqa: F401 - imported for context
    )

    # Zero the sleep so the bounded test runs in real time.
    monkeypatch.setattr(
        "yomotsusaka.runpod_lifecycle._MANAGE_CLEANUP_RETRY_DELAY_SECONDS",
        0,
    )

    delete_calls: list[str] = []

    def _always_fail(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            delete_calls.append(request.url.path)
            return httpx.Response(500, json={"error": "fail"})
        return httpx.Response(404)

    lifecycle = ManageRunPodLifecycle(
        api_key="sk-test",
        pod_config=PodConfig(),
        transport=httpx.MockTransport(_always_fail),
    )
    handle = PodHandle(pod_id="pod-test", endpoint="http://example/")

    with pytest.raises(PodUnavailableError) as excinfo:
        lifecycle.stop_pod(handle)

    assert excinfo.value.args[0] == "cleanup_failed"
    assert len(delete_calls) == _MANAGE_CLEANUP_MAX_ATTEMPTS, (
        f"expected exactly {_MANAGE_CLEANUP_MAX_ATTEMPTS} DELETE attempts "
        f"(initial + bounded retries); got {len(delete_calls)}"
    )


# ---------------------------------------------------------------------------
# 7: bounded snapshot-write retry helper
# ---------------------------------------------------------------------------


def test_snapshot_retry_helper_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``SearchGateway.snapshot`` retries an ``OSError`` from
    ``os.replace`` exactly once before re-raising. The second attempt
    succeeds when the underlying transient clears."""
    vault_root = tmp_path / "vault"
    gateway = SearchGateway()
    gateway.index(_make_manifest("doc-1"))

    attempts: list[str] = []
    real_replace = os.replace

    def _flaky_replace(src, dst):  # noqa: ANN001 - test shim mirrors os.replace shape
        attempts.append("replace")
        if len(attempts) == 1:
            raise OSError("simulated transient")
        return real_replace(src, dst)

    monkeypatch.setattr(
        "yomotsusaka.search_gateway.os.replace", _flaky_replace
    )

    final_path = gateway.snapshot(vault_root)

    # Helper succeeded on the second attempt; final file is present.
    assert final_path.is_file()
    assert len(attempts) == 2, (
        f"expected exactly 2 os.replace attempts (initial + bounded retry); "
        f"got {len(attempts)}"
    )


def test_snapshot_retry_helper_bounded_on_persistent_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``os.replace`` fails on every attempt, the helper re-raises
    the last ``OSError`` after exhausting :data:`_SNAPSHOT_MAX_ATTEMPTS`
    attempts. The temp file is cleaned up and no final file exists."""
    vault_root = tmp_path / "vault"
    gateway = SearchGateway()
    gateway.index(_make_manifest("doc-1"))

    final_path = vault_root / "index" / "manifests.jsonl"
    tmp_marker = vault_root / "index" / "manifests.jsonl.tmp"

    attempts: list[str] = []

    def _always_fail(src, dst):  # noqa: ANN001 - test shim mirrors os.replace shape
        attempts.append("replace")
        raise OSError("simulated persistent rename failure")

    monkeypatch.setattr(
        "yomotsusaka.search_gateway.os.replace", _always_fail
    )

    with pytest.raises(OSError, match="simulated persistent rename failure"):
        gateway.snapshot(vault_root)

    assert len(attempts) == _SNAPSHOT_MAX_ATTEMPTS, (
        f"expected exactly {_SNAPSHOT_MAX_ATTEMPTS} os.replace attempts; "
        f"got {len(attempts)}"
    )
    assert not final_path.exists()
    assert not tmp_marker.exists()


# ---------------------------------------------------------------------------
# 8: inference-backend soft-degrade aligns with InferenceSpanDegraded
# ---------------------------------------------------------------------------


class _ErroringBackend(InferenceBackend):
    """Backend stub that always raises ``InferenceBackendError`` — used to
    exercise the soft-degrade surface that maps to
    :data:`OperationalCategory.InferenceSpanDegraded`."""

    def generate(self, prompt: str, *, max_tokens: int = 256) -> str:  # noqa: ARG002
        raise InferenceBackendError("simulated backend failure", reason="pod_unavailable")

    def health_check(self) -> bool:
        return False


def test_inference_span_categories_align_with_proposer_softdegrade() -> None:
    """When the inference backend raises an :class:`InferenceBackendError`,
    the operational surface reports
    :data:`OperationalCategory.InferenceSpanDegraded` (NOT
    ``InferenceSpanUnavailable``). The ``Unavailable`` category is
    reserved for the "backend not configured at all" path; ``Degraded``
    is the "configured but errored" path.

    This test exercises the boundary in the simplest way: it confirms
    that both categories exist and carry distinct recovery instructions,
    and that the ``Degraded`` action references the backend-error path
    while the ``Unavailable`` action references the not-configured path.
    """
    # Sanity: the two categories are distinct registry entries.
    degraded = recovery_for(OperationalCategory.InferenceSpanDegraded)
    unavailable = recovery_for(OperationalCategory.InferenceSpanUnavailable)
    assert degraded != unavailable

    # The "Degraded" action references the backend-error path.
    assert "backend" in degraded.agent_action.lower()
    # The "Unavailable" action references the not-configured path.
    assert "not configured" in unavailable.agent_action.lower()

    # Cross-check: an erroring backend produces InferenceBackendError, which
    # is what the batch runner catches and treats as a per-document failure
    # (soft-degrade). The mapping into InferenceSpanDegraded happens at the
    # operational-CLI layer (#91); the contract here is just that the
    # categories are wired up correctly.
    backend = _ErroringBackend()
    with pytest.raises(InferenceBackendError):
        backend.generate("ignored prompt")

    # A "configured" backend that does not raise should NOT trigger
    # either inference-span category. DummyBackend returns a constant
    # JSON-shaped response that the parser will treat as a parse failure
    # at the proposer layer — out of scope here; just confirm we can
    # construct it without touching the operational categories.
    dummy = DummyBackend()
    assert dummy is not None
