"""
vLLM OpenAI-compatible inference backend.

Implements :class:`yomotsusaka.inference_backend.InferenceBackend` against
the vLLM ``POST /v1/chat/completions`` endpoint exposed by the RunPod vLLM
template documented in ``docs/runpod.md`` §4 (default model
``Qwen/Qwen3-8B``).

This module is **private-side only** (see ``docs/architecture.md`` §7.2 and
metaplan Fork 6 of issue #46). It is NEVER imported by
:mod:`yomotsusaka.boundary`. The endpoint URL, API key, and pod id passed
to :class:`VLLMBackend` are classified ``never_expose`` (see
``docs/architecture.md`` capability matrix); they are not logged, not
included in exception messages, not stored in any agent-facing surface, and
not echoed in any response.

The constructor takes an explicit ``model_id`` (no default) so test
fixtures must declare it — pinning the first-model decision (``Qwen/Qwen3-8B``)
at the call site rather than at the module level.
"""

from __future__ import annotations

import json
import logging
import os

import httpx

from yomotsusaka.inference_backend import (
    InferenceBackend,
    VLLMGenerationError,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunable HTTP timeouts (metaplan Fork 4)
# ---------------------------------------------------------------------------

_DEFAULT_REQUEST_TIMEOUT_SECONDS = 60
_DEFAULT_HEALTH_TIMEOUT_SECONDS = 5


def _env_int(name: str, default: int) -> int:
    """Return ``int(os.environ[name])`` if set and valid; otherwise *default*.

    Never raises — invalid values fall back to the default so a bad
    environment cannot break the import path of this module. The env-var
    name itself is logged but its value is NOT (it could be a credential
    in a misconfigured deployment).
    """
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "ignoring non-integer value for env var %s; using default %d",
            name,
            default,
        )
        return default


VLLM_REQUEST_TIMEOUT_SECONDS: int = _env_int(
    "YOMOTSUSAKA_VLLM_REQUEST_TIMEOUT", _DEFAULT_REQUEST_TIMEOUT_SECONDS
)
"""HTTP timeout for ``POST /v1/chat/completions`` calls, in seconds.

Overridable via the ``YOMOTSUSAKA_VLLM_REQUEST_TIMEOUT`` environment
variable. Prevents hung HTTP from accumulating wall time against an
owner-paid Pod (no auto-stop, no idle reaper in this PR — see metaplan
Fork 4).
"""


VLLM_HEALTH_TIMEOUT_SECONDS: int = _env_int(
    "YOMOTSUSAKA_VLLM_HEALTH_TIMEOUT", _DEFAULT_HEALTH_TIMEOUT_SECONDS
)
"""HTTP timeout for ``/health`` probe calls, in seconds.

Overridable via the ``YOMOTSUSAKA_VLLM_HEALTH_TIMEOUT`` environment
variable.
"""


# ---------------------------------------------------------------------------
# VLLMBackend
# ---------------------------------------------------------------------------


def _oom_marker_in_body(body: str) -> bool:
    """Heuristically detect an out-of-memory marker in the response body.

    vLLM does not expose a standard OOM status; the response body typically
    carries phrases like ``"CUDA out of memory"`` or ``"OutOfMemoryError"``
    when the model server hits the GPU memory limit. We match a small,
    case-insensitive set of markers.
    """
    lowered = body.lower()
    return any(
        marker in lowered
        for marker in (
            "out of memory",
            "outofmemoryerror",
            "cuda oom",
        )
    )


class VLLMBackend(InferenceBackend):
    """OpenAI-compatible vLLM chat-completions backend.

    The backend calls ``POST {endpoint}/v1/chat/completions`` with the
    payload documented in ``docs/runpod.md`` §7. Response content is read
    from ``choices[0].message.content``.

    Parameters
    ----------
    endpoint:
        Base URL of the vLLM server, e.g. ``https://pod-id-8000.proxy.runpod.net``.
        Classified ``never_expose``; not logged, not echoed.
    model_id:
        Exact model identifier sent to vLLM. The MVP-3 pin is
        ``"Qwen/Qwen3-8B"`` per ``docs/runpod.md`` §4 — see metaplan
        Fork 5; this argument has no default so the call site is forced to
        declare the choice.
    api_key:
        Optional bearer token. If ``None``, ``$VLLM_API_KEY`` is read; if
        that is also absent and ``pod_id`` is supplied, the
        ``sk-<pod_id>`` fallback documented in ``docs/runpod.md`` §6 is
        used. Absence of all three is permitted (vLLM templates can be
        configured without auth) — no header is sent in that case.
    pod_id:
        Optional pod identifier for the ``sk-<pod_id>`` fallback. Not
        required when ``api_key`` is supplied directly or via env.
    request_timeout_seconds / health_timeout_seconds:
        Overrides for the module-level defaults; lets the caller pin
        per-instance timeouts without touching the process env.
    transport:
        Optional ``httpx`` transport override. Used by tests via
        ``pytest-httpx``; never set in production code.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        model_id: str,
        api_key: str | None = None,
        pod_id: str | None = None,
        request_timeout_seconds: int | None = None,
        health_timeout_seconds: int | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not isinstance(endpoint, str) or not endpoint.strip():
            # Never echo the actual offending value — it would be a credential
            # in some misuse cases (e.g. a caller passing the API key in the
            # endpoint slot).
            raise ValueError("VLLMBackend endpoint must be a non-empty string")
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValueError("VLLMBackend model_id must be a non-empty string")

        self._endpoint = endpoint.rstrip("/")
        self._model_id = model_id

        resolved_api_key: str | None
        if api_key is not None:
            resolved_api_key = api_key
        else:
            env_key = os.environ.get("VLLM_API_KEY")
            if env_key:
                resolved_api_key = env_key
            elif pod_id:
                resolved_api_key = f"sk-{pod_id}"
            else:
                resolved_api_key = None
        self._api_key = resolved_api_key
        self._request_timeout = (
            int(request_timeout_seconds)
            if request_timeout_seconds is not None
            else VLLM_REQUEST_TIMEOUT_SECONDS
        )
        self._health_timeout = (
            int(health_timeout_seconds)
            if health_timeout_seconds is not None
            else VLLM_HEALTH_TIMEOUT_SECONDS
        )
        self._transport = transport

    # ------------------------------------------------------------------
    # Internal HTTP helpers
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _client(self, *, timeout: float) -> httpx.Client:
        kwargs: dict[str, object] = {"timeout": timeout}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.Client(**kwargs)  # type: ignore[arg-type]

    # ------------------------------------------------------------------
    # InferenceBackend interface
    # ------------------------------------------------------------------

    def generate(self, prompt: str, *, max_tokens: int = 512) -> str:
        """POST one chat-completion request and return the model output.

        Raises :class:`VLLMGenerationError` on every non-success path, with
        a stable ``reason`` literal. The exception message intentionally
        does NOT include the endpoint URL, the bearer token, or the raw
        response body — the boundary facade catches the exception and
        emits a structured ``agent_redacted`` failure carrying only the
        ``reason``.
        """
        url = f"{self._endpoint}/v1/chat/completions"
        payload = {
            "model": self._model_id,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }
        try:
            with self._client(timeout=self._request_timeout) as client:
                response = client.post(url, json=payload, headers=self._headers())
        except httpx.TimeoutException as exc:
            raise VLLMGenerationError(
                "vLLM request timed out", reason="vllm_timeout"
            ) from exc
        except httpx.HTTPError as exc:
            raise VLLMGenerationError(
                "vLLM transport error", reason="vllm_http_error"
            ) from exc

        status = response.status_code
        # Read the body once for OOM scanning and JSON parsing. The body is
        # NOT echoed in any exception message.
        body_text = response.text or ""
        if status == 429:
            raise VLLMGenerationError(
                "vLLM rate-limited", reason="vllm_rate_limited"
            )
        if status >= 400:
            if _oom_marker_in_body(body_text):
                raise VLLMGenerationError(
                    "vLLM out-of-memory", reason="vllm_oom"
                )
            raise VLLMGenerationError(
                f"vLLM returned HTTP {status}", reason="vllm_http_error"
            )

        try:
            data = json.loads(body_text)
        except (TypeError, ValueError) as exc:
            raise VLLMGenerationError(
                "vLLM response was not valid JSON", reason="vllm_http_error"
            ) from exc

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise VLLMGenerationError(
                "vLLM response missing choices[0].message.content",
                reason="vllm_http_error",
            ) from exc

        if not isinstance(content, str):
            raise VLLMGenerationError(
                "vLLM response content was not a string",
                reason="vllm_http_error",
            )

        # OOM markers can also appear in 200 responses (e.g. vLLM returns
        # 200 + an error string). Classify them here too.
        if _oom_marker_in_body(content):
            raise VLLMGenerationError(
                "vLLM out-of-memory", reason="vllm_oom"
            )

        return content

    def health_check(self) -> bool:
        """Return ``True`` iff ``GET {endpoint}/health`` returns HTTP 200.

        Uses the shorter :data:`VLLM_HEALTH_TIMEOUT_SECONDS` so a hung
        backend cannot starve health probes. Connection errors return
        ``False`` rather than raising — health checks are a "yes/no" surface.
        """
        url = f"{self._endpoint}/health"
        try:
            with self._client(timeout=self._health_timeout) as client:
                response = client.get(url, headers=self._headers())
        except httpx.HTTPError:
            return False
        return response.status_code == 200


__all__ = [
    "VLLMBackend",
    "VLLM_REQUEST_TIMEOUT_SECONDS",
    "VLLM_HEALTH_TIMEOUT_SECONDS",
]
