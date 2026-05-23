"""L2 unit tests for :mod:`yomotsusaka.runpod_lifecycle`.

Covers the three operating modes pinned by metaplan Fork 1 of issue #46:
``mock`` (default), ``attach`` (the only real mode in this PR), and
``manage`` (NotImplementedError pending a follow-up issue).
"""

from __future__ import annotations

import logging

import httpx
import pytest

from yomotsusaka.runpod_lifecycle import (
    AttachRunPodLifecycle,
    ManageRunPodLifecycle,
    MockRunPodLifecycle,
    PodConfig,
    PodHandle,
    RunPodConfigError,
    RunPodLifecycle,
    lifecycle_from_env,
)


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
# Manage mode
# ---------------------------------------------------------------------------


def test_manage_lifecycle_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError) as excinfo:
        ManageRunPodLifecycle()
    assert "manage" in str(excinfo.value)


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


def test_lifecycle_from_env_manage_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YOMOTSUSAKA_RUNPOD_MODE", "manage")
    with pytest.raises(NotImplementedError):
        lifecycle_from_env()


def test_lifecycle_from_env_unknown_mode_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("YOMOTSUSAKA_RUNPOD_MODE", "bogus")
    with pytest.raises(RunPodConfigError):
        lifecycle_from_env()
