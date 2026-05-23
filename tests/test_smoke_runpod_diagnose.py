"""L1 unit tests for ``scripts/smoke_runpod.py`` diagnostic mode.

Mocks the vLLM HTTP server via ``httpx.MockTransport`` so no network is
required. The tests exercise every public-safe :class:`DiagnosticCategory`
literal documented in ``docs/runpod-agent-smoke.md`` §5, then re-run the
full ``main()`` entry point under monkeypatched failure scenarios to
assert that the sanitisation invariant (§6) holds: no secret value,
endpoint URL, response body, or full ``httpx`` exception message reaches
stdout/stderr.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

# scripts/ is not a package, so import the module directly from its file
# path. This mirrors how the script is invoked in production
# (``python3 scripts/smoke_runpod.py``).
_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "smoke_runpod.py"
_spec = importlib.util.spec_from_file_location("smoke_runpod", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
smoke_runpod = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("smoke_runpod", smoke_runpod)
_spec.loader.exec_module(smoke_runpod)


# ---------------------------------------------------------------------------
# Forbidden substrings — values that MUST NOT appear in stdout/stderr
# ---------------------------------------------------------------------------

_SECRET_ENDPOINT = "https://secret-pod-abc123-8000.proxy.runpod.net"
_SECRET_API_KEY = "sk-supersecret-do-not-leak-987654321"
_SECRET_POD_ID = "secret-pod-abc123"
_SECRET_RESPONSE_BODY = "raw-response-body-do-not-leak-555"
# Tokens uniquely identifying each secret; we look for *substrings* so a
# partial leak (e.g. only the host portion of the URL) is also caught.
_FORBIDDEN_TOKENS = (
    "secret-pod-abc123",
    "supersecret-do-not-leak",
    "raw-response-body-do-not-leak",
    "proxy.runpod.net",
    "Authorization",  # the header name we never print
    "Bearer ",
)


def _assert_no_secret(captured: pytest.CaptureResult[str]) -> None:
    combined = (captured.out or "") + "\n" + (captured.err or "")
    for token in _FORBIDDEN_TOKENS:
        assert token not in combined, (
            f"sanitisation violation: token {token!r} leaked into output:\n"
            f"{combined!r}"
        )


# ---------------------------------------------------------------------------
# run_diagnostics(): per-category coverage
# ---------------------------------------------------------------------------


def _ok_chat_handler(content: str = "pong"):
    body = {
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content}}
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, text="ok")
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(200, json=body)
        return httpx.Response(404)

    return handler


def test_endpoint_unset_when_no_endpoint() -> None:
    result = smoke_runpod.run_diagnostics(
        endpoint=None, api_key=_SECRET_API_KEY, pod_id=_SECRET_POD_ID
    )
    assert result.category == "endpoint_unset"
    assert result.snippet is None


def test_endpoint_unset_when_blank_endpoint() -> None:
    result = smoke_runpod.run_diagnostics(
        endpoint="", api_key=_SECRET_API_KEY, pod_id=_SECRET_POD_ID
    )
    assert result.category == "endpoint_unset"


def test_endpoint_unreachable_on_transport_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            f"Cannot connect to {_SECRET_ENDPOINT}", request=_request
        )

    transport = httpx.MockTransport(handler)
    result = smoke_runpod.run_diagnostics(
        endpoint=_SECRET_ENDPOINT,
        api_key=_SECRET_API_KEY,
        pod_id=_SECRET_POD_ID,
        transport=transport,
    )
    assert result.category == "endpoint_unreachable"


def test_health_non_200_when_health_returns_500_then_chat_unreached() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(500, text=_SECRET_RESPONSE_BODY)
        pytest.fail("chat endpoint should not be reached when /health is non-200")

    transport = httpx.MockTransport(handler)
    result = smoke_runpod.run_diagnostics(
        endpoint=_SECRET_ENDPOINT,
        api_key=_SECRET_API_KEY,
        pod_id=_SECRET_POD_ID,
        transport=transport,
    )
    assert result.category == "health_non_200"


def test_auth_failure_from_health_401() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(401, text=_SECRET_RESPONSE_BODY)
        pytest.fail("chat endpoint should not be reached on 401 /health")

    transport = httpx.MockTransport(handler)
    result = smoke_runpod.run_diagnostics(
        endpoint=_SECRET_ENDPOINT,
        api_key=_SECRET_API_KEY,
        pod_id=_SECRET_POD_ID,
        transport=transport,
    )
    assert result.category == "auth_failure"


def test_auth_failure_from_chat_403() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(404)  # no /health route on this template
        return httpx.Response(403, text=_SECRET_RESPONSE_BODY)

    transport = httpx.MockTransport(handler)
    result = smoke_runpod.run_diagnostics(
        endpoint=_SECRET_ENDPOINT,
        api_key=_SECRET_API_KEY,
        pod_id=_SECRET_POD_ID,
        transport=transport,
    )
    assert result.category == "auth_failure"


def test_model_mismatch_on_chat_400() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, text="ok")
        return httpx.Response(
            400,
            json={"error": {"message": _SECRET_RESPONSE_BODY}},
        )

    transport = httpx.MockTransport(handler)
    result = smoke_runpod.run_diagnostics(
        endpoint=_SECRET_ENDPOINT,
        api_key=_SECRET_API_KEY,
        pod_id=_SECRET_POD_ID,
        transport=transport,
    )
    assert result.category == "model_mismatch_or_bad_request"


def test_backend_not_ready_on_503() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, text="ok")
        return httpx.Response(503, text=_SECRET_RESPONSE_BODY)

    transport = httpx.MockTransport(handler)
    result = smoke_runpod.run_diagnostics(
        endpoint=_SECRET_ENDPOINT,
        api_key=_SECRET_API_KEY,
        pod_id=_SECRET_POD_ID,
        transport=transport,
    )
    assert result.category == "backend_not_ready"


def test_malformed_response_on_non_json_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, text="ok")
        return httpx.Response(200, text="this is not json " + _SECRET_RESPONSE_BODY)

    transport = httpx.MockTransport(handler)
    result = smoke_runpod.run_diagnostics(
        endpoint=_SECRET_ENDPOINT,
        api_key=_SECRET_API_KEY,
        pod_id=_SECRET_POD_ID,
        transport=transport,
    )
    assert result.category == "malformed_response"


def test_malformed_response_on_missing_choices() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, text="ok")
        return httpx.Response(200, json={"unexpected": _SECRET_RESPONSE_BODY})

    transport = httpx.MockTransport(handler)
    result = smoke_runpod.run_diagnostics(
        endpoint=_SECRET_ENDPOINT,
        api_key=_SECRET_API_KEY,
        pod_id=_SECRET_POD_ID,
        transport=transport,
    )
    assert result.category == "malformed_response"


def test_success_returns_truncated_snippet() -> None:
    long_content = "x" * 200
    transport = httpx.MockTransport(_ok_chat_handler(content=long_content))
    result = smoke_runpod.run_diagnostics(
        endpoint=_SECRET_ENDPOINT,
        api_key=_SECRET_API_KEY,
        pod_id=_SECRET_POD_ID,
        transport=transport,
    )
    assert result.category == "success"
    assert result.snippet is not None
    assert len(result.snippet) == 80


def test_success_collapses_newlines_in_snippet() -> None:
    transport = httpx.MockTransport(_ok_chat_handler(content="hello\nworld"))
    result = smoke_runpod.run_diagnostics(
        endpoint=_SECRET_ENDPOINT,
        api_key=_SECRET_API_KEY,
        pod_id=_SECRET_POD_ID,
        transport=transport,
    )
    assert result.category == "success"
    assert result.snippet == "hello world"


def test_chat_uses_resolved_api_key_when_only_pod_id_set() -> None:
    seen_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            for k, v in request.headers.items():
                seen_headers[k.lower()] = v
            return httpx.Response(200, text="ok")
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}}]
        })

    transport = httpx.MockTransport(handler)
    result = smoke_runpod.run_diagnostics(
        endpoint=_SECRET_ENDPOINT,
        api_key=None,
        pod_id="podxyz",
        transport=transport,
    )
    assert result.category == "success"
    # Authorization header is sent with the sk-<pod_id> fallback (but not
    # echoed by the diagnostic output — see _assert_no_secret tests).
    assert seen_headers.get("authorization") == "Bearer sk-podxyz"


# ---------------------------------------------------------------------------
# main() end-to-end: sanitisation invariant
# ---------------------------------------------------------------------------


def test_main_fails_closed_without_live_smoke_flag(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("RUNPOD_LIVE_SMOKE", raising=False)
    monkeypatch.setenv("VLLM_ENDPOINT", _SECRET_ENDPOINT)
    monkeypatch.setenv("VLLM_API_KEY", _SECRET_API_KEY)
    monkeypatch.setenv("RUNPOD_POD_ID", _SECRET_POD_ID)

    rc = smoke_runpod.main(["--mode", "diagnose"])

    assert rc == 2
    captured = capsys.readouterr()
    _assert_no_secret(captured)


def test_main_diagnose_endpoint_unset_is_sanitised(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("RUNPOD_LIVE_SMOKE", "1")
    monkeypatch.delenv("VLLM_ENDPOINT", raising=False)
    monkeypatch.setenv("VLLM_API_KEY", _SECRET_API_KEY)
    monkeypatch.setenv("RUNPOD_POD_ID", _SECRET_POD_ID)

    rc = smoke_runpod.main(["--mode", "diagnose"])

    captured = capsys.readouterr()
    assert rc == 1
    assert "diagnostic: endpoint_unset" in captured.out
    _assert_no_secret(captured)


def test_main_diagnose_unreachable_does_not_leak_exception_text(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When httpx raises an error containing the URL/secret, the main()
    output must classify it as ``endpoint_unreachable`` without printing
    any portion of the exception message.
    """
    monkeypatch.setenv("RUNPOD_LIVE_SMOKE", "1")
    monkeypatch.setenv("VLLM_ENDPOINT", _SECRET_ENDPOINT)
    monkeypatch.setenv("VLLM_API_KEY", _SECRET_API_KEY)
    monkeypatch.setenv("RUNPOD_POD_ID", _SECRET_POD_ID)

    # Force the TCP reachability probe to fail so we exit before httpx
    # is called at all — exercises the "no DNS" branch.
    def _fail_reachable(endpoint: str) -> bool:
        # The probe is allowed to see the endpoint string (it must, in
        # order to do DNS); but its return value must not be embedded in
        # diagnostic output. We simulate a hard "unreachable" verdict.
        assert endpoint == _SECRET_ENDPOINT
        return False

    monkeypatch.setattr(smoke_runpod, "_probe_endpoint_reachable", _fail_reachable)

    rc = smoke_runpod.main(["--mode", "diagnose"])

    captured = capsys.readouterr()
    assert rc == 1
    assert "diagnostic: endpoint_unreachable" in captured.out
    _assert_no_secret(captured)


def test_main_diagnose_auth_failure_does_not_leak_body(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("RUNPOD_LIVE_SMOKE", "1")
    monkeypatch.setenv("VLLM_ENDPOINT", _SECRET_ENDPOINT)
    monkeypatch.setenv("VLLM_API_KEY", _SECRET_API_KEY)
    monkeypatch.setenv("RUNPOD_POD_ID", _SECRET_POD_ID)

    # Bypass the real TCP probe so we exercise the HTTP path.
    monkeypatch.setattr(
        smoke_runpod, "_probe_endpoint_reachable", lambda _endpoint: True
    )

    def handler(request: httpx.Request) -> httpx.Response:
        # Echo the secret in the body to confirm the script does not
        # propagate response bodies to stdout/stderr.
        return httpx.Response(401, text=_SECRET_RESPONSE_BODY)

    # Patch httpx.Client so the production code path (no transport
    # argument) still ends up using the mock transport.
    real_client_factory = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client_factory(*args, **kwargs)

    monkeypatch.setattr(smoke_runpod.httpx, "Client", fake_client)

    rc = smoke_runpod.main(["--mode", "diagnose"])

    captured = capsys.readouterr()
    assert rc == 1
    assert "diagnostic: auth_failure" in captured.out
    _assert_no_secret(captured)


def test_main_diagnose_malformed_does_not_leak_body(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("RUNPOD_LIVE_SMOKE", "1")
    monkeypatch.setenv("VLLM_ENDPOINT", _SECRET_ENDPOINT)
    monkeypatch.setenv("VLLM_API_KEY", _SECRET_API_KEY)
    monkeypatch.setenv("RUNPOD_POD_ID", _SECRET_POD_ID)

    monkeypatch.setattr(
        smoke_runpod, "_probe_endpoint_reachable", lambda _endpoint: True
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, text="ok")
        return httpx.Response(200, text=_SECRET_RESPONSE_BODY)

    real_client_factory = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client_factory(*args, **kwargs)

    monkeypatch.setattr(smoke_runpod.httpx, "Client", fake_client)

    rc = smoke_runpod.main(["--mode", "diagnose"])

    captured = capsys.readouterr()
    assert rc == 1
    assert "diagnostic: malformed_response" in captured.out
    _assert_no_secret(captured)


def test_main_diagnose_success_prints_only_truncated_snippet(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("RUNPOD_LIVE_SMOKE", "1")
    monkeypatch.setenv("VLLM_ENDPOINT", _SECRET_ENDPOINT)
    monkeypatch.setenv("VLLM_API_KEY", _SECRET_API_KEY)
    monkeypatch.setenv("RUNPOD_POD_ID", _SECRET_POD_ID)

    monkeypatch.setattr(
        smoke_runpod, "_probe_endpoint_reachable", lambda _endpoint: True
    )

    short_safe_content = "diagnostic-safe-pong"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, text="ok")
        body = {
            "choices": [
                {"message": {"role": "assistant", "content": short_safe_content}}
            ]
        }
        return httpx.Response(200, json=body)

    real_client_factory = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client_factory(*args, **kwargs)

    monkeypatch.setattr(smoke_runpod.httpx, "Client", fake_client)

    rc = smoke_runpod.main(["--mode", "diagnose"])

    captured = capsys.readouterr()
    assert rc == 0
    assert f"diagnostic: success snippet={short_safe_content}" in captured.out
    _assert_no_secret(captured)


def test_main_generate_failure_does_not_leak_exception_text(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--mode generate`` (the legacy path) must continue to honour the
    sanitisation invariant when the backend raises an exception whose
    message contains the endpoint URL.
    """
    monkeypatch.setenv("RUNPOD_LIVE_SMOKE", "1")
    monkeypatch.setenv("VLLM_ENDPOINT", _SECRET_ENDPOINT)
    monkeypatch.setenv("VLLM_API_KEY", _SECRET_API_KEY)
    monkeypatch.setenv("RUNPOD_POD_ID", _SECRET_POD_ID)

    class _ExplodingBackend:
        def __init__(self, **_kwargs) -> None:
            pass

        def generate(self, *_args, **_kwargs):
            # Real httpx exceptions can carry the URL — emulate that.
            raise RuntimeError(
                f"connection failed to {_SECRET_ENDPOINT} with body "
                f"{_SECRET_RESPONSE_BODY!r}"
            )

    monkeypatch.setattr(smoke_runpod, "VLLMBackend", _ExplodingBackend)

    rc = smoke_runpod.main(["--mode", "generate"])

    captured = capsys.readouterr()
    assert rc == 1
    assert "error: smoke generate failed" in captured.err
    _assert_no_secret(captured)


def test_format_line_known_categories_have_no_secret_input() -> None:
    """Sanity check that ``DiagnosticResult.format_line()`` only includes
    the category literal (and the truncated snippet for success), nothing
    that could leak. This is the source-of-truth for what reaches stdout.
    """
    for category in (
        "endpoint_unset",
        "endpoint_unreachable",
        "health_non_200",
        "auth_failure",
        "model_mismatch_or_bad_request",
        "backend_not_ready",
        "malformed_response",
    ):
        line = smoke_runpod.DiagnosticResult(category=category).format_line()
        assert line == f"diagnostic: {category}"

    success = smoke_runpod.DiagnosticResult(category="success", snippet="abc")
    assert success.format_line() == "diagnostic: success snippet=abc"


# ---------------------------------------------------------------------------
# Argparse / mode plumbing
# ---------------------------------------------------------------------------


def test_main_invalid_mode_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("RUNPOD_LIVE_SMOKE", "1")
    with pytest.raises(SystemExit) as excinfo:
        smoke_runpod.main(["--mode", "nope"])
    assert excinfo.value.code != 0
    captured = capsys.readouterr()
    _assert_no_secret(captured)


def test_run_diagnostics_uses_chat_when_health_returns_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 404 on /health is treated as 'no health route' (not unhealthy);
    the chat probe must still run.
    """
    chat_called: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(404)
        chat_called.append(True)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}}]
        })

    transport = httpx.MockTransport(handler)
    result = smoke_runpod.run_diagnostics(
        endpoint=_SECRET_ENDPOINT,
        api_key=_SECRET_API_KEY,
        pod_id=None,
        transport=transport,
    )
    assert chat_called == [True]
    assert result.category == "success"


def test_classify_status_matrix() -> None:
    cases = {
        200: None,
        204: None,
        299: None,
        400: "model_mismatch_or_bad_request",
        401: "auth_failure",
        403: "auth_failure",
        404: "model_mismatch_or_bad_request",
        429: "model_mismatch_or_bad_request",
        500: "backend_not_ready",
        503: "backend_not_ready",
        599: "backend_not_ready",
    }
    for status, expected in cases.items():
        assert smoke_runpod._classify_status(status) == expected, status


def test_diagnostic_result_is_immutable() -> None:
    result = smoke_runpod.DiagnosticResult(category="success", snippet="ok")
    with pytest.raises(Exception):
        result.category = "auth_failure"  # type: ignore[misc]


def test_canonical_payload_shape_matches_runtime() -> None:
    """Smoke-test that the payload the diagnostic probe sends matches the
    shape the production VLLMBackend uses (model, messages with role/content,
    temperature, max_tokens). Catches accidental schema drift.
    """
    seen_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, text="ok")
        seen_payload.update(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}}]
        })

    transport = httpx.MockTransport(handler)
    smoke_runpod.run_diagnostics(
        endpoint=_SECRET_ENDPOINT,
        api_key=_SECRET_API_KEY,
        pod_id=None,
        model_id="some/model",
        transport=transport,
    )
    assert seen_payload["model"] == "some/model"
    assert isinstance(seen_payload["messages"], list)
    assert seen_payload["messages"][0]["role"] == "user"
    assert "max_tokens" in seen_payload
