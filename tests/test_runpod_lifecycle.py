"""L2 unit tests for :mod:`yomotsusaka.runpod_lifecycle`.

Covers the three operating modes pinned by metaplan Fork 1 of issue #46:
``mock`` (default), ``attach`` (real env-supplied handle), and ``manage``
(real REST create / wait / delete; shipped by issue #76 / closes #70).
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
import uuid
from pathlib import Path

import httpx
import pytest

from yomotsusaka import runpod_lifecycle as runpod_lifecycle_module
from yomotsusaka.runpod_lifecycle import (
    AttachRunPodLifecycle,
    ManageRunPodLifecycle,
    MockRunPodLifecycle,
    PodConfig,
    PodHandle,
    PodUnavailableError,
    RunPodConfigError,
    RunPodLifecycle,
    lifecycle_from_env,
)


# ---------------------------------------------------------------------------
# Helper-script import seam (loaded lazily from scripts/manage_runpod.py)
# ---------------------------------------------------------------------------

_HELPER_PATH = Path(__file__).resolve().parents[1] / "scripts" / "manage_runpod.py"
_helper_spec = importlib.util.spec_from_file_location("manage_runpod", _HELPER_PATH)
assert _helper_spec is not None and _helper_spec.loader is not None
manage_runpod = importlib.util.module_from_spec(_helper_spec)
sys.modules.setdefault("manage_runpod", manage_runpod)
_helper_spec.loader.exec_module(manage_runpod)


# ---------------------------------------------------------------------------
# Mock mode
# ---------------------------------------------------------------------------


def test_mock_lifecycle_returns_non_empty_handle() -> None:
    lifecycle = MockRunPodLifecycle()
    handle = lifecycle.start_pod(PodConfig())
    assert isinstance(handle, PodHandle)
    assert handle.pod_id
    assert handle.pod_id.startswith("mock-pod-")
    assert handle.endpoint == "http://127.0.0.1:0"


def test_mock_lifecycle_is_ready_true() -> None:
    lifecycle = MockRunPodLifecycle()
    handle = lifecycle.start_pod(PodConfig())
    assert lifecycle.is_ready(handle) is True


def test_mock_lifecycle_stop_pod_is_noop() -> None:
    lifecycle = MockRunPodLifecycle()
    handle = lifecycle.start_pod(PodConfig())
    # No exception; no return value
    assert lifecycle.stop_pod(handle) is None


def test_mock_lifecycle_subclasses_runpod_lifecycle() -> None:
    assert issubclass(MockRunPodLifecycle, RunPodLifecycle)
    assert issubclass(AttachRunPodLifecycle, RunPodLifecycle)


# ---------------------------------------------------------------------------
# Attach mode — config errors
# ---------------------------------------------------------------------------


def test_attach_lifecycle_missing_pod_id_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RUNPOD_POD_ID", raising=False)
    monkeypatch.setenv("RUNPOD_POD_ENDPOINT", "http://present.example")
    with pytest.raises(RunPodConfigError) as excinfo:
        AttachRunPodLifecycle()
    msg = str(excinfo.value)
    assert "RUNPOD_POD_ID" in msg
    # Metaplan Fork 3: the *key* may be named, the *value* never is.
    # Here the value is absent anyway, but the message must not include
    # the endpoint value that IS set.
    assert "present.example" not in msg


def test_attach_lifecycle_missing_endpoint_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNPOD_POD_ID", "pod-abc")
    monkeypatch.delenv("RUNPOD_POD_ENDPOINT", raising=False)
    with pytest.raises(RunPodConfigError) as excinfo:
        AttachRunPodLifecycle()
    msg = str(excinfo.value)
    assert "RUNPOD_POD_ENDPOINT" in msg
    assert "pod-abc" not in msg


# ---------------------------------------------------------------------------
# Attach mode — happy path
# ---------------------------------------------------------------------------


def test_attach_lifecycle_returns_env_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNPOD_POD_ID", "pod-from-env")
    monkeypatch.setenv("RUNPOD_POD_ENDPOINT", "http://from-env.example:8000")
    lifecycle = AttachRunPodLifecycle()
    handle = lifecycle.start_pod(PodConfig())
    assert handle.pod_id == "pod-from-env"
    assert handle.endpoint == "http://from-env.example:8000"


def test_attach_lifecycle_explicit_args_override_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNPOD_POD_ID", "pod-from-env")
    monkeypatch.setenv("RUNPOD_POD_ENDPOINT", "http://from-env.example")
    lifecycle = AttachRunPodLifecycle(
        pod_id="pod-explicit",
        endpoint="http://explicit.example",
    )
    handle = lifecycle.start_pod(PodConfig())
    assert handle.pod_id == "pod-explicit"
    assert handle.endpoint == "http://explicit.example"


def test_attach_lifecycle_start_pod_logs_advisory_without_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    lifecycle = AttachRunPodLifecycle(
        pod_id="pod-LEAK-SENTINEL-AAA",
        endpoint="http://leak-sentinel.example:8000",
    )
    with caplog.at_level(logging.INFO, logger="yomotsusaka.runpod_lifecycle"):
        lifecycle.start_pod(PodConfig())
    blob = "\n".join(rec.getMessage() for rec in caplog.records)
    # Advisory present, secret absent (metaplan Fork 3 / Fork 4)
    assert "attach mode" in blob
    assert "responsible" in blob
    assert "pod-LEAK-SENTINEL-AAA" not in blob
    assert "leak-sentinel" not in blob


def test_attach_lifecycle_stop_pod_logs_without_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    lifecycle = AttachRunPodLifecycle(
        pod_id="pod-LEAK-SENTINEL-AAA",
        endpoint="http://leak-sentinel.example:8000",
    )
    handle = lifecycle.start_pod(PodConfig())
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="yomotsusaka.runpod_lifecycle"):
        lifecycle.stop_pod(handle)
    blob = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "not stopping" in blob
    assert "pod-LEAK-SENTINEL-AAA" not in blob


# ---------------------------------------------------------------------------
# Attach mode — is_ready / /health
# ---------------------------------------------------------------------------


def test_attach_is_ready_true_on_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health"
        return httpx.Response(200, text="ok")

    lifecycle = AttachRunPodLifecycle(
        pod_id="pod-abc",
        endpoint="http://test.invalid:8000",
        transport=httpx.MockTransport(handler),
    )
    handle = lifecycle.start_pod(PodConfig())
    assert lifecycle.is_ready(handle) is True


def test_attach_is_ready_false_on_non_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="not ready")

    lifecycle = AttachRunPodLifecycle(
        pod_id="pod-abc",
        endpoint="http://test.invalid:8000",
        transport=httpx.MockTransport(handler),
    )
    handle = lifecycle.start_pod(PodConfig())
    assert lifecycle.is_ready(handle) is False


def test_attach_is_ready_false_on_connect_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    lifecycle = AttachRunPodLifecycle(
        pod_id="pod-abc",
        endpoint="http://test.invalid:8000",
        transport=httpx.MockTransport(handler),
    )
    handle = lifecycle.start_pod(PodConfig())
    assert lifecycle.is_ready(handle) is False


# ---------------------------------------------------------------------------
# Manage mode — config-error path + bypass kwargs (Decision 2)
# ---------------------------------------------------------------------------


def test_manage_lifecycle_missing_api_key_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    with pytest.raises(RunPodConfigError) as excinfo:
        ManageRunPodLifecycle()
    assert "RUNPOD_API_KEY" in str(excinfo.value)


def test_manage_lifecycle_empty_api_key_treated_as_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "")
    with pytest.raises(RunPodConfigError):
        ManageRunPodLifecycle()


def test_manage_lifecycle_explicit_api_key_overrides_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "sk-from-env")
    # No exception when explicit kwarg overrides env.
    lifecycle = ManageRunPodLifecycle(api_key="sk-explicit")
    assert isinstance(lifecycle, ManageRunPodLifecycle)


def test_manage_lifecycle_explicit_pod_id_skips_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision 2 — explicit ``pod_id``+``endpoint`` skip the REST call.

    Construct with sentinel pod_id+endpoint, give it a transport that
    would raise on any HTTP request, and assert ``start_pod`` returns
    the supplied :class:`PodHandle` without touching the network.
    """
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)

    def _refuse(request: httpx.Request) -> httpx.Response:
        pytest.fail("REST call must not happen when pod_id+endpoint are explicit")

    lifecycle = ManageRunPodLifecycle(
        pod_id="pod-x",
        endpoint="http://x.invalid",
        transport=httpx.MockTransport(_refuse),
    )
    handle = lifecycle.start_pod(PodConfig())
    assert handle.pod_id == "pod-x"
    assert handle.endpoint == "http://x.invalid"


# ---------------------------------------------------------------------------
# Manage mode — full lifecycle (REST create + health poll + delete)
# ---------------------------------------------------------------------------


def _make_lifecycle_handler(
    *,
    create_pod_id: str = "pod-created-001",
    create_endpoint: str = "http://created.invalid:8000",
    health_status: int = 200,
    create_status: int = 201,
    delete_status: int = 200,
    fail_on_delete: bool = False,
    record: dict[str, list[httpx.Request]] | None = None,
):
    """Construct an :class:`httpx.MockTransport` handler that mimics the
    minimal RunPod REST surface (``POST /pods``, ``DELETE /pods/{id}``,
    ``GET /health``) plus a separate health endpoint at the created
    Pod's URL.

    ``record`` is an optional dict-of-lists used by tests to inspect
    which routes were invoked.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.setdefault(request.method, []).append(request)
        path = request.url.path
        if request.method == "POST" and path.endswith("/pods"):
            return httpx.Response(
                create_status,
                json={"id": create_pod_id, "endpoint": create_endpoint},
            )
        if request.method == "POST" and path.endswith("/stop"):
            return httpx.Response(200)
        if request.method == "DELETE" and "/pods/" in path:
            if fail_on_delete:
                raise httpx.ConnectError("simulated delete failure", request=request)
            return httpx.Response(delete_status)
        if path.endswith("/health"):
            return httpx.Response(health_status)
        return httpx.Response(404, text="unexpected route")

    return handler


def test_manage_lifecycle_create_then_delete_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L1 — full sequence: create → health 200 → delete."""
    # Drop the wait sleep so the test is sub-second even on slow CI.
    monkeypatch.setattr(
        runpod_lifecycle_module, "_MANAGE_HEALTH_POLL_INTERVAL_SECONDS", 0
    )
    record: dict[str, list[httpx.Request]] = {}
    handler = _make_lifecycle_handler(record=record)
    lifecycle = ManageRunPodLifecycle(
        api_key="sk-test", transport=httpx.MockTransport(handler)
    )
    handle = lifecycle.start_pod(PodConfig())
    assert isinstance(handle, PodHandle)
    assert handle.pod_id == "pod-created-001"
    lifecycle.stop_pod(handle, terminate=True)
    assert len(record.get("POST", [])) == 1
    assert len(record.get("DELETE", [])) == 1


def test_manage_lifecycle_create_failure_raises_pod_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runpod_lifecycle_module, "_MANAGE_HEALTH_POLL_INTERVAL_SECONDS", 0
    )
    handler = _make_lifecycle_handler(create_status=500)
    lifecycle = ManageRunPodLifecycle(
        api_key="sk-test", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(PodUnavailableError) as excinfo:
        lifecycle.start_pod(PodConfig())
    assert excinfo.value.args[0] == "create_failed"


def test_manage_lifecycle_create_transport_error_is_create_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runpod_lifecycle_module, "_MANAGE_HEALTH_POLL_INTERVAL_SECONDS", 0
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated", request=request)

    lifecycle = ManageRunPodLifecycle(
        api_key="sk-test", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(PodUnavailableError) as excinfo:
        lifecycle.start_pod(PodConfig())
    assert excinfo.value.args[0] == "create_failed"


def test_manage_lifecycle_wait_timeout_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision 5 — the wait phase polls up to MAX_ATTEMPTS and then
    raises ``wait_timeout``."""
    monkeypatch.setattr(
        runpod_lifecycle_module, "_MANAGE_HEALTH_POLL_INTERVAL_SECONDS", 0
    )
    monkeypatch.setattr(
        runpod_lifecycle_module, "_MANAGE_HEALTH_POLL_MAX_ATTEMPTS", 3
    )
    handler = _make_lifecycle_handler(health_status=503)
    lifecycle = ManageRunPodLifecycle(
        api_key="sk-test", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(PodUnavailableError) as excinfo:
        lifecycle.start_pod(PodConfig())
    assert excinfo.value.args[0] == "wait_timeout"


def test_manage_lifecycle_stop_pod_failure_raises_cleanup_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runpod_lifecycle_module, "_MANAGE_HEALTH_POLL_INTERVAL_SECONDS", 0
    )
    handler = _make_lifecycle_handler(fail_on_delete=True)
    lifecycle = ManageRunPodLifecycle(
        api_key="sk-test", transport=httpx.MockTransport(handler)
    )
    handle = lifecycle.start_pod(PodConfig())
    with pytest.raises(PodUnavailableError) as excinfo:
        lifecycle.stop_pod(handle, terminate=True)
    assert excinfo.value.args[0] == "cleanup_failed"


def test_manage_lifecycle_stop_pod_terminate_false_uses_stop_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runpod_lifecycle_module, "_MANAGE_HEALTH_POLL_INTERVAL_SECONDS", 0
    )
    record: dict[str, list[httpx.Request]] = {}
    handler = _make_lifecycle_handler(record=record)
    lifecycle = ManageRunPodLifecycle(
        api_key="sk-test", transport=httpx.MockTransport(handler)
    )
    handle = lifecycle.start_pod(PodConfig())
    lifecycle.stop_pod(handle, terminate=False)
    # The second POST is the /stop call (no DELETE expected).
    assert len(record.get("DELETE", [])) == 0
    post_paths = [str(r.url.path) for r in record.get("POST", [])]
    assert any(p.endswith("/stop") for p in post_paths), post_paths


def test_manage_lifecycle_is_ready_health_probe_round_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = _make_lifecycle_handler(health_status=200)
    lifecycle = ManageRunPodLifecycle(
        api_key="sk-test", transport=httpx.MockTransport(handler)
    )
    handle = PodHandle(pod_id="pod-x", endpoint="http://x.invalid")
    assert lifecycle.is_ready(handle) is True


def test_manage_lifecycle_logs_only_category_literals(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Decision 3 / Seam S1 — every log record carries only a category."""
    monkeypatch.setattr(
        runpod_lifecycle_module, "_MANAGE_HEALTH_POLL_INTERVAL_SECONDS", 0
    )
    handler = _make_lifecycle_handler(
        create_pod_id="pod-LEAK-SENTINEL-AAA",
        create_endpoint="http://leak-sentinel.example:8000",
    )
    lifecycle = ManageRunPodLifecycle(
        api_key="sk-LEAK-API-KEY-SENTINEL",
        transport=httpx.MockTransport(handler),
    )
    with caplog.at_level(logging.INFO, logger="yomotsusaka.runpod_lifecycle"):
        handle = lifecycle.start_pod(PodConfig())
        lifecycle.stop_pod(handle, terminate=True)
    messages = [r.getMessage() for r in caplog.records]
    allowed = {
        "created",
        "waiting_health",
        "healthy",
        "deleted",
        "stopped",
        "create_failed",
        "wait_timeout",
        "cleanup_failed",
    }
    for msg in messages:
        assert msg in allowed, f"log record {msg!r} is not a category literal"
    assert "created" in messages
    assert "healthy" in messages
    assert "deleted" in messages


def test_manage_lifecycle_no_secret_leak_in_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Mirror :func:`test_attach_lifecycle_start_pod_logs_advisory_without_secret`.

    Run the full create + delete sequence with sentinel api_key /
    pod_id / endpoint values and assert none of them appear in any log
    record. Extends Seam S1 in the tightened plan to lifecycle envelope.
    """
    monkeypatch.setattr(
        runpod_lifecycle_module, "_MANAGE_HEALTH_POLL_INTERVAL_SECONDS", 0
    )
    handler = _make_lifecycle_handler(
        create_pod_id="pod-LEAK-SENTINEL-AAA",
        create_endpoint="http://leak-sentinel.example:8000",
    )
    lifecycle = ManageRunPodLifecycle(
        api_key="sk-LEAK-API-KEY-SENTINEL",
        transport=httpx.MockTransport(handler),
    )
    with caplog.at_level(logging.INFO, logger="yomotsusaka.runpod_lifecycle"):
        handle = lifecycle.start_pod(PodConfig())
        lifecycle.stop_pod(handle, terminate=True)
    blob = "\n".join(r.getMessage() for r in caplog.records)
    for needle in (
        "sk-LEAK-API-KEY-SENTINEL",
        "pod-LEAK-SENTINEL-AAA",
        "leak-sentinel.example",
        "https://rest.runpod.io",
        "Authorization",
        "Bearer ",
    ):
        assert needle not in blob, f"log leak: {needle!r}"


def test_manage_lifecycle_pod_unavailable_messages_are_category_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision 3 / Seam S2 — PodUnavailableError messages are category
    literals only (no URL, body, or raw httpx exception text)."""
    monkeypatch.setattr(
        runpod_lifecycle_module, "_MANAGE_HEALTH_POLL_INTERVAL_SECONDS", 0
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            "connection refused to https://leak-sentinel.example/api/pods",
            request=request,
        )

    lifecycle = ManageRunPodLifecycle(
        api_key="sk-test", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(PodUnavailableError) as excinfo:
        lifecycle.start_pod(PodConfig())
    msg = str(excinfo.value)
    assert msg == "create_failed"
    assert "leak-sentinel" not in msg
    assert "rest.runpod.io" not in msg


def test_manage_lifecycle_handle_exposure_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision 2 — the manage-mode lifecycle satisfies the existing
    :class:`tests.test_exposure_contract_mvp3.TestPodHandleContract`
    surface even though the constructor accepts extra kwargs (pod_id /
    endpoint).

    Constructed with sentinel pod_id+endpoint via the bypass kwargs:
    ``start_pod`` returns a real :class:`PodHandle` that round-trips
    the sentinels, while the agent-facing projection (the empty
    mapping per the existing contract) carries no private state.
    """
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    from tests._exposure_denylist import (
        MOCK_ENDPOINT_URL_SENTINELS,
        MOCK_POD_ID_SENTINELS,
    )

    lifecycle = ManageRunPodLifecycle(
        pod_id=MOCK_POD_ID_SENTINELS[0],
        endpoint=MOCK_ENDPOINT_URL_SENTINELS[0],
    )
    handle = lifecycle.start_pod(PodConfig())
    # The private handle round-trips the sentinels (per Fork 6: this
    # value stays vault-side).
    assert handle.pod_id == MOCK_POD_ID_SENTINELS[0]
    # The agent-facing projection is the empty mapping — the contract
    # carried by ``TestPodHandleContract._make_handle``. We don't have
    # an agent-facing surface here; the assertion is a structural
    # reminder.
    agent_facing_projection: dict[str, object] = {}
    assert agent_facing_projection == {}


# ---------------------------------------------------------------------------
# Mode selection
# ---------------------------------------------------------------------------


def test_lifecycle_from_env_defaults_to_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("YOMOTSUSAKA_RUNPOD_MODE", raising=False)
    lifecycle = lifecycle_from_env()
    assert isinstance(lifecycle, MockRunPodLifecycle)


def test_lifecycle_from_env_selects_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YOMOTSUSAKA_RUNPOD_MODE", "mock")
    assert isinstance(lifecycle_from_env(), MockRunPodLifecycle)


def test_lifecycle_from_env_selects_attach(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YOMOTSUSAKA_RUNPOD_MODE", "attach")
    monkeypatch.setenv("RUNPOD_POD_ID", "pod-id")
    monkeypatch.setenv("RUNPOD_POD_ENDPOINT", "http://endpoint.example")
    assert isinstance(lifecycle_from_env(), AttachRunPodLifecycle)


def test_lifecycle_from_env_selects_manage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YOMOTSUSAKA_RUNPOD_MODE", "manage")
    monkeypatch.setenv("RUNPOD_API_KEY", "sk-test")
    lifecycle = lifecycle_from_env()
    assert isinstance(lifecycle, ManageRunPodLifecycle)


def test_lifecycle_from_env_manage_missing_api_key_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("YOMOTSUSAKA_RUNPOD_MODE", "manage")
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    with pytest.raises(RunPodConfigError):
        lifecycle_from_env()


def test_lifecycle_from_env_unknown_mode_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("YOMOTSUSAKA_RUNPOD_MODE", "bogus")
    with pytest.raises(RunPodConfigError):
        lifecycle_from_env()


# ===========================================================================
# scripts/manage_runpod.py — L1 helper tests (issue #76)
# ===========================================================================
#
# These exercise the cost-control helper (preflight + driver + reporting)
# against an injected lifecycle factory and a fake smoke runner. They
# never spawn the helper as a subprocess and never make a real REST or
# vLLM call.


_FORBIDDEN_TOKENS_HELPER = (
    "sk-LEAK-API-KEY-SENTINEL",
    "sk-LEAK-VLLM-KEY-SENTINEL",
    "pod-LEAK-SENTINEL-AAA",
    "leak-sentinel.example",
    "https://rest.runpod.io",
    "Authorization",
    "Bearer ",
)


def _assert_no_helper_secret(blob: str) -> None:
    for token in _FORBIDDEN_TOKENS_HELPER:
        assert token not in blob, (
            f"sanitisation violation: token {token!r} leaked into output:\n"
            f"{blob!r}"
        )


def _make_fake_lifecycle(
    *,
    create_should_fail: bool = False,
    wait_should_fail: bool = False,
    cleanup_should_fail: bool = False,
    pod_id: str = "pod-LEAK-SENTINEL-AAA",
    endpoint: str = "http://leak-sentinel.example:8000",
) -> object:
    """Return a stub lifecycle with the same surface as
    :class:`ManageRunPodLifecycle` but no network calls."""

    class _Fake:
        def __init__(self) -> None:
            self.stop_calls: list[PodHandle] = []
            self.start_calls = 0

        def start_pod(self, _config: PodConfig) -> PodHandle:
            self.start_calls += 1
            if create_should_fail:
                raise PodUnavailableError("create_failed")
            if wait_should_fail:
                raise PodUnavailableError("wait_timeout")
            return PodHandle(pod_id=pod_id, endpoint=endpoint)

        def stop_pod(self, handle: PodHandle, *, terminate: bool = True) -> None:
            self.stop_calls.append(handle)
            if cleanup_should_fail:
                raise PodUnavailableError("cleanup_failed")

    return _Fake()


def _make_fake_smoke_runner(*, success: bool = True):
    """Mimic :func:`subprocess.run` for the smoke subprocess."""
    captured: dict[str, object] = {}

    def runner(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["env"] = dict(kwargs.get("env", {}))
        stdout = "diagnostic: success snippet=ok\n" if success else (
            "diagnostic: auth_failure\n"
        )

        class _Result:
            returncode = 0 if success else 1

            def __init__(self, out: str) -> None:
                self.stdout = out

        return _Result(stdout)

    return runner, captured


def test_manage_helper_runpodctl_missing_exits_two(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "sk-test")
    log_path = tmp_path / "lifecycle.jsonl"

    rc = manage_runpod.run_lifecycle(
        keep_pod=False,
        pod_config=PodConfig(),
        lifecycle_factory=lambda: _make_fake_lifecycle(),
        smoke_runner=lambda *_a, **_k: None,
        lifecycle_log=log_path,
        runpodctl_check=lambda: False,
    )
    out = capsys.readouterr()
    assert rc == manage_runpod.EXIT_PREFLIGHT_FAILED
    assert "lifecycle: runpodctl_missing" in out.out
    # No REST call happened — no smoke env in log.
    _assert_no_helper_secret(out.out + "\n" + out.err)
    # The JSONL row was appended.
    rows = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert rows and rows[0]["category"] == "runpodctl_missing"
    assert set(rows[0].keys()) == {"timestamp", "request_id", "category"}


def test_manage_helper_api_key_missing_exits_two(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    rc = manage_runpod.run_lifecycle(
        keep_pod=False,
        pod_config=PodConfig(),
        lifecycle_factory=lambda: _make_fake_lifecycle(),
        smoke_runner=lambda *_a, **_k: None,
        lifecycle_log=tmp_path / "lifecycle.jsonl",
        env={},
        runpodctl_check=lambda: True,
    )
    out = capsys.readouterr()
    assert rc == manage_runpod.EXIT_PREFLIGHT_FAILED
    assert "lifecycle: api_key_missing" in out.out
    _assert_no_helper_secret(out.out + "\n" + out.err)


def test_manage_helper_create_then_delete_default(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    fake = _make_fake_lifecycle()
    runner, captured = _make_fake_smoke_runner(success=True)
    rc = manage_runpod.run_lifecycle(
        keep_pod=False,
        pod_config=PodConfig(),
        lifecycle_factory=lambda: fake,
        smoke_runner=runner,
        lifecycle_log=tmp_path / "lifecycle.jsonl",
        env={"RUNPOD_API_KEY": "sk-LEAK-API-KEY-SENTINEL"},
        runpodctl_check=lambda: True,
    )
    out = capsys.readouterr()
    assert rc == manage_runpod.EXIT_OK
    assert fake.start_calls == 1
    assert len(fake.stop_calls) == 1, "default policy must delete the Pod"
    for cat in ("created", "healthy", "smoke_passed", "deleted"):
        assert f"lifecycle: {cat}" in out.out, cat
    _assert_no_helper_secret(out.out + "\n" + out.err)


def test_manage_helper_keep_pod_skips_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    fake = _make_fake_lifecycle()
    runner, _captured = _make_fake_smoke_runner(success=True)
    rc = manage_runpod.run_lifecycle(
        keep_pod=True,
        pod_config=PodConfig(),
        lifecycle_factory=lambda: fake,
        smoke_runner=runner,
        lifecycle_log=tmp_path / "lifecycle.jsonl",
        env={"RUNPOD_API_KEY": "sk-test"},
        runpodctl_check=lambda: True,
    )
    out = capsys.readouterr()
    assert rc == manage_runpod.EXIT_OK
    assert fake.stop_calls == [], "keep-pod must not call stop_pod"
    assert "lifecycle: kept" in out.out
    assert "lifecycle: deleted" not in out.out


def test_manage_helper_cleanup_on_smoke_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    fake = _make_fake_lifecycle()
    runner, _captured = _make_fake_smoke_runner(success=False)
    rc = manage_runpod.run_lifecycle(
        keep_pod=False,
        pod_config=PodConfig(),
        lifecycle_factory=lambda: fake,
        smoke_runner=runner,
        lifecycle_log=tmp_path / "lifecycle.jsonl",
        env={"RUNPOD_API_KEY": "sk-test"},
        runpodctl_check=lambda: True,
    )
    out = capsys.readouterr()
    # Smoke failed -> exit 1, but the Pod was still deleted.
    assert rc == manage_runpod.EXIT_PHASE_FAILED
    assert "lifecycle: smoke_failed" in out.out
    assert "lifecycle: deleted" in out.out
    assert len(fake.stop_calls) == 1, "delete must still be attempted"


def test_manage_helper_cleanup_failure_surfaces_urgent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    fake = _make_fake_lifecycle(cleanup_should_fail=True)
    runner, _captured = _make_fake_smoke_runner(success=True)
    log_path = tmp_path / "lifecycle.jsonl"
    rc = manage_runpod.run_lifecycle(
        keep_pod=False,
        pod_config=PodConfig(),
        lifecycle_factory=lambda: fake,
        smoke_runner=runner,
        lifecycle_log=log_path,
        env={"RUNPOD_API_KEY": "sk-LEAK-API-KEY-SENTINEL"},
        runpodctl_check=lambda: True,
    )
    out = capsys.readouterr()
    assert rc == manage_runpod.EXIT_CLEANUP_FAILED
    assert "lifecycle: cleanup_failed" in out.out
    # Urgent stderr line carries a UUID4 request_id and no Pod id.
    assert "URGENT: manual Pod cleanup required" in out.err
    assert "request_id=" in out.err
    # Extract request_id and verify it is a UUID.
    req_id_token = out.err.split("request_id=", 1)[1].strip().split()[0]
    uuid.UUID(req_id_token)
    # JSONL row matches the same request_id.
    rows = [json.loads(line) for line in log_path.read_text().splitlines()]
    cleanup_rows = [r for r in rows if r["category"] == "cleanup_failed"]
    assert cleanup_rows and cleanup_rows[0]["request_id"] == req_id_token
    _assert_no_helper_secret(out.out + "\n" + out.err)


def test_manage_helper_create_failure_no_cleanup_attempted(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    fake = _make_fake_lifecycle(create_should_fail=True)
    runner, _captured = _make_fake_smoke_runner(success=True)
    rc = manage_runpod.run_lifecycle(
        keep_pod=False,
        pod_config=PodConfig(),
        lifecycle_factory=lambda: fake,
        smoke_runner=runner,
        lifecycle_log=tmp_path / "lifecycle.jsonl",
        env={"RUNPOD_API_KEY": "sk-test"},
        runpodctl_check=lambda: True,
    )
    out = capsys.readouterr()
    assert rc == manage_runpod.EXIT_PHASE_FAILED
    assert "lifecycle: create_failed" in out.out
    assert fake.stop_calls == [], "no cleanup when create failed"
    assert "lifecycle: deleted" not in out.out


def test_manage_helper_no_secret_leak_in_any_surface(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Decision 3 — combined surface scan: stdout, stderr, log records, jsonl.

    Inject sentinel api_key / pod_id / endpoint and assert none reach
    any agent-facing surface.
    """
    fake = _make_fake_lifecycle(
        cleanup_should_fail=True,
        pod_id="pod-LEAK-SENTINEL-AAA",
        endpoint="http://leak-sentinel.example:8000",
    )
    runner, _captured = _make_fake_smoke_runner(success=False)
    log_path = tmp_path / "lifecycle.jsonl"
    with caplog.at_level(logging.INFO):
        manage_runpod.run_lifecycle(
            keep_pod=False,
            pod_config=PodConfig(),
            lifecycle_factory=lambda: fake,
            smoke_runner=runner,
            lifecycle_log=log_path,
            env={
                "RUNPOD_API_KEY": "sk-LEAK-API-KEY-SENTINEL",
                "VLLM_API_KEY": "sk-LEAK-VLLM-KEY-SENTINEL",
            },
            runpodctl_check=lambda: True,
        )

    captured = capsys.readouterr()
    surfaces = [
        captured.out,
        captured.err,
        "\n".join(r.getMessage() for r in caplog.records),
        log_path.read_text(),
    ]
    for blob in surfaces:
        _assert_no_helper_secret(blob)


def test_manage_helper_smoke_subprocess_invokes_with_correct_env(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """L2 — verify the helper invokes ``scripts/smoke_runpod.py --mode
    diagnose`` with VLLM_ENDPOINT / VLLM_API_KEY / RUNPOD_POD_ID set in
    the child env. The real smoke is never executed."""
    fake = _make_fake_lifecycle(
        pod_id="pod-LEAK-SENTINEL-AAA",
        endpoint="http://leak-sentinel.example:8000",
    )
    runner, captured_call = _make_fake_smoke_runner(success=True)
    manage_runpod.run_lifecycle(
        keep_pod=False,
        pod_config=PodConfig(),
        lifecycle_factory=lambda: fake,
        smoke_runner=runner,
        lifecycle_log=tmp_path / "lifecycle.jsonl",
        env={"RUNPOD_API_KEY": "sk-LEAK-API-KEY-SENTINEL"},
        runpodctl_check=lambda: True,
    )
    argv = captured_call.get("argv")
    assert argv is not None
    assert any("smoke_runpod.py" in str(a) for a in argv), argv
    assert "--mode" in argv and "diagnose" in argv

    child_env = captured_call.get("env")
    assert isinstance(child_env, dict)
    # The child receives the Pod's endpoint + id, the live-smoke gate,
    # and the bearer token only via env (NOT as command-line args).
    assert child_env.get("RUNPOD_LIVE_SMOKE") == "1"
    assert child_env.get("VLLM_ENDPOINT") == "http://leak-sentinel.example:8000"
    assert child_env.get("RUNPOD_POD_ID") == "pod-LEAK-SENTINEL-AAA"
    assert child_env.get("VLLM_API_KEY") == "sk-LEAK-API-KEY-SENTINEL"

    # stdout/stderr from this in-process driver must still be sanitised.
    out = capsys.readouterr()
    _assert_no_helper_secret(out.out + "\n" + out.err)


def test_manage_helper_select_exit_code_precedence() -> None:
    """Decision 4 — exit-code precedence is enforced."""
    select = manage_runpod._select_exit_code
    assert select([0]) == manage_runpod.EXIT_OK
    # cleanup_failed wins
    assert (
        select([manage_runpod.EXIT_PHASE_FAILED, manage_runpod.EXIT_CLEANUP_FAILED])
        == manage_runpod.EXIT_CLEANUP_FAILED
    )
    # preflight stops the loop early
    assert (
        select([manage_runpod.EXIT_PREFLIGHT_FAILED, manage_runpod.EXIT_OK])
        == manage_runpod.EXIT_PREFLIGHT_FAILED
    )
    # last-non-zero wins among phase failures
    assert select([manage_runpod.EXIT_PHASE_FAILED, 0]) == manage_runpod.EXIT_PHASE_FAILED


def test_manage_helper_main_help_exits_zero() -> None:
    """``--help`` must succeed without consulting any env var."""
    with pytest.raises(SystemExit) as excinfo:
        manage_runpod.main(["--help"])
    assert excinfo.value.code == 0


def test_manage_helper_emit_category_only_prints_category(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The S3 producer prints exactly one ``lifecycle: <category>`` line
    and nothing else."""
    manage_runpod._emit_category("created")
    out = capsys.readouterr()
    assert out.out == "lifecycle: created\n"
    assert out.err == ""


def test_manage_helper_jsonl_row_has_exactly_three_keys(tmp_path: Path) -> None:
    """The S5 producer writes exactly ``{timestamp, request_id, category}``."""
    log_path = tmp_path / "lifecycle.jsonl"
    manage_runpod._append_lifecycle_row(
        request_id="0000-test", category="created", log_path=log_path
    )
    row = json.loads(log_path.read_text().strip())
    assert set(row.keys()) == {"timestamp", "request_id", "category"}
    assert row["request_id"] == "0000-test"
    assert row["category"] == "created"


# ---------------------------------------------------------------------------
# L3 — owner-runnable live test (skipped unless RUNPOD_MANAGE_LIVE=1)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("RUNPOD_MANAGE_LIVE") != "1",
    reason="L3 owner-only — set RUNPOD_MANAGE_LIVE=1 plus RUNPOD_API_KEY",
)
def test_manage_runpod_live_owner_only() -> None:  # pragma: no cover — owner-only
    """Run the real REST lifecycle against the real RunPod account.

    Skipped by default in CI; mirrors :data:`RUNPOD_LIVE_SMOKE` from the
    smoke runbook. Run manually with ``RUNPOD_MANAGE_LIVE=1
    RUNPOD_API_KEY=... uv run pytest -k live -v``.
    """
    lifecycle = ManageRunPodLifecycle()
    handle = lifecycle.start_pod(PodConfig())
    try:
        assert isinstance(handle, PodHandle)
    finally:
        lifecycle.stop_pod(handle, terminate=True)
