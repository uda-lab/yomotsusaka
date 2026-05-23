"""L1 unit tests for :class:`yomotsusaka.vllm_backend.VLLMBackend`.

Mocks the vLLM HTTP server via ``pytest-httpx`` so no network is required.
Covers the failure-mode → ``reason`` mapping pinned in metaplan Fork 7 of
issue #46.
"""

from __future__ import annotations

import json

import httpx
import pytest

from yomotsusaka.inference_backend import (
    InferenceBackendError,
    VLLMGenerationError,
)
from yomotsusaka.vllm_backend import VLLMBackend


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


CANONICAL_RESPONSE_BODY = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "hello from mocked vLLM",
            },
            "finish_reason": "stop",
        }
    ],
}


def _make_backend(handler) -> VLLMBackend:
    return VLLMBackend(
        endpoint="http://test.invalid:8000",
        model_id="Qwen/Qwen3-8B",
        api_key="sk-fixture-key",
        transport=httpx.MockTransport(handler),
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_generate_returns_choice_content_on_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/chat/completions"
        payload = json.loads(request.content)
        assert payload["model"] == "Qwen/Qwen3-8B"
        assert payload["messages"] == [
            {"role": "user", "content": "ping"}
        ]
        assert payload["max_tokens"] == 64
        assert request.headers["Authorization"] == "Bearer sk-fixture-key"
        return httpx.Response(200, json=CANONICAL_RESPONSE_BODY)

    backend = _make_backend(handler)
    out = backend.generate("ping", max_tokens=64)
    assert out == "hello from mocked vLLM"


def test_generate_omits_authorization_header_when_no_key() -> None:
    seen_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        for k, v in request.headers.items():
            seen_headers[k.lower()] = v
        return httpx.Response(200, json=CANONICAL_RESPONSE_BODY)

    backend = VLLMBackend(
        endpoint="http://test.invalid:8000",
        model_id="Qwen/Qwen3-8B",
        api_key=None,
        pod_id=None,
        transport=httpx.MockTransport(handler),
    )
    backend.generate("hi")
    assert "authorization" not in seen_headers


def test_generate_uses_pod_id_fallback_for_authorization() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("Authorization", "")
        return httpx.Response(200, json=CANONICAL_RESPONSE_BODY)

    backend = VLLMBackend(
        endpoint="http://test.invalid:8000",
        model_id="Qwen/Qwen3-8B",
        api_key=None,
        pod_id="abc123",
        transport=httpx.MockTransport(handler),
    )
    backend.generate("hi")
    assert seen["authorization"] == "Bearer sk-abc123"


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "   ", None, 42])
def test_constructor_rejects_invalid_endpoint(bad) -> None:
    with pytest.raises(ValueError, match="endpoint"):
        VLLMBackend(endpoint=bad, model_id="Qwen/Qwen3-8B")


@pytest.mark.parametrize("bad", ["", "   ", None, 42])
def test_constructor_rejects_invalid_model_id(bad) -> None:
    with pytest.raises(ValueError, match="model_id"):
        VLLMBackend(endpoint="http://x", model_id=bad)


# ---------------------------------------------------------------------------
# Failure-mode → reason mapping (metaplan Fork 7)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,expected_reason",
    [
        (401, "vllm_http_error"),
        (403, "vllm_http_error"),
        (500, "vllm_http_error"),
        (502, "vllm_http_error"),
    ],
)
def test_http_error_status_maps_to_vllm_http_error(status, expected_reason) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="server boom")

    backend = _make_backend(handler)
    with pytest.raises(VLLMGenerationError) as excinfo:
        backend.generate("hi")
    assert excinfo.value.reason == expected_reason


def test_http_429_maps_to_vllm_rate_limited() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="slow down")

    backend = _make_backend(handler)
    with pytest.raises(VLLMGenerationError) as excinfo:
        backend.generate("hi")
    assert excinfo.value.reason == "vllm_rate_limited"


def test_timeout_maps_to_vllm_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated read timeout", request=request)

    backend = _make_backend(handler)
    with pytest.raises(VLLMGenerationError) as excinfo:
        backend.generate("hi")
    assert excinfo.value.reason == "vllm_timeout"


def test_invalid_json_maps_to_vllm_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not a json body at all")

    backend = _make_backend(handler)
    with pytest.raises(VLLMGenerationError) as excinfo:
        backend.generate("hi")
    assert excinfo.value.reason == "vllm_http_error"


def test_missing_choices_maps_to_vllm_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "no-choices"})

    backend = _make_backend(handler)
    with pytest.raises(VLLMGenerationError) as excinfo:
        backend.generate("hi")
    assert excinfo.value.reason == "vllm_http_error"


def test_oom_in_5xx_body_maps_to_vllm_oom() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="CUDA out of memory: tried to allocate")

    backend = _make_backend(handler)
    with pytest.raises(VLLMGenerationError) as excinfo:
        backend.generate("hi")
    assert excinfo.value.reason == "vllm_oom"


def test_oom_in_200_content_maps_to_vllm_oom() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "OutOfMemoryError: GPU 0 ran out",
                        }
                    }
                ]
            },
        )

    backend = _make_backend(handler)
    with pytest.raises(VLLMGenerationError) as excinfo:
        backend.generate("hi")
    assert excinfo.value.reason == "vllm_oom"


def test_transport_error_maps_to_vllm_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    backend = _make_backend(handler)
    with pytest.raises(VLLMGenerationError) as excinfo:
        backend.generate("hi")
    assert excinfo.value.reason == "vllm_http_error"


# ---------------------------------------------------------------------------
# Exception-hierarchy contract
# ---------------------------------------------------------------------------


def test_vllm_error_subclasses_inference_backend_error() -> None:
    err = VLLMGenerationError("test", reason="vllm_timeout")
    assert isinstance(err, InferenceBackendError)
    assert err.reason == "vllm_timeout"


def test_exception_message_does_not_echo_endpoint_or_key() -> None:
    """Metaplan Fork 3 + Fork 7: exception messages must not contain the
    endpoint URL, the bearer token, or the raw response body."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            text="error body contains http://leak-sentinel.runpod.example/api",
        )

    backend = VLLMBackend(
        endpoint="http://test.invalid:8000",
        model_id="Qwen/Qwen3-8B",
        api_key="sk-LEAKY-KEY-SENTINEL",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(VLLMGenerationError) as excinfo:
        backend.generate("hi")
    msg = str(excinfo.value)
    assert "leak-sentinel" not in msg
    assert "LEAKY-KEY-SENTINEL" not in msg
    assert "test.invalid" not in msg


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


def test_health_check_returns_true_on_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health"
        return httpx.Response(200, text="ok")

    backend = _make_backend(handler)
    assert backend.health_check() is True


def test_health_check_returns_false_on_non_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="not ready")

    backend = _make_backend(handler)
    assert backend.health_check() is False


def test_health_check_returns_false_on_connect_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    backend = _make_backend(handler)
    assert backend.health_check() is False
