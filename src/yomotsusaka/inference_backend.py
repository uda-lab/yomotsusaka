"""
Inference backend interface and dummy implementation.

Real backends (vLLM + Qwen3-8B on RunPod, etc.) implement
:class:`InferenceBackend` and are wired in via configuration.

No hosted proprietary LLM APIs are used in the core path.

This module is **private-side only**. It MUST NOT be imported by
``yomotsusaka.boundary``; the new :class:`InferenceBackendError` hierarchy
and its ``reason`` literals are caught and translated into structured
``agent_redacted`` failures by the boundary facade — the raw exception
``args[0]`` MUST NOT reach an ordinary-agent surface (see metaplan Fork 7
of issue #46).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal


# ---------------------------------------------------------------------------
# Private-side failure-mode hierarchy (#46 Fork 7)
# ---------------------------------------------------------------------------


InferenceBackendReason = Literal[
    "pod_unavailable",
    "vllm_timeout",
    "vllm_oom",
    "vllm_http_error",
    "vllm_rate_limited",
]
"""Stable wire identifiers for inference-backend failures.

The boundary facade maps these to ``agent_redacted`` failure envelopes
without echoing the underlying exception ``args[0]``. Wire identifiers are
intentionally stable so a future change does not silently bypass the
boundary's mapping table.
"""


class InferenceBackendError(Exception):
    """Base class for private-side inference-backend failures.

    Carries a stable ``reason`` literal so the boundary facade can map the
    failure to a structured ``agent_redacted`` response without re-parsing
    the exception message (which may contain private endpoint URLs, model
    identifiers, raw stack traces from the remote runtime, etc.).
    """

    reason: InferenceBackendReason

    def __init__(
        self,
        message: str,
        *,
        reason: InferenceBackendReason,
    ) -> None:
        super().__init__(message)
        self.reason = reason


class PodUnavailableError(InferenceBackendError):
    """Raised when the RunPod-side host cannot be reached.

    Covers connect-refused, DNS failure, ``/health`` returning non-200,
    socket timeout on the health probe, and RunPod-side "Pod-unavailable"
    / "rate-limit" responses on the lifecycle channel. The ``reason``
    literal defaults to ``"pod_unavailable"`` but may be overridden when a
    rate-limit response from RunPod can be distinguished from a generic
    unavailable response.
    """

    def __init__(
        self,
        message: str,
        *,
        reason: InferenceBackendReason = "pod_unavailable",
    ) -> None:
        super().__init__(message, reason=reason)


class VLLMGenerationError(InferenceBackendError):
    """Raised when the vLLM ``/v1/chat/completions`` call fails.

    Covers non-200 HTTP responses, malformed JSON bodies, request
    timeouts, OOM markers in the response body, and rate-limit (HTTP 429)
    responses. The ``reason`` literal narrows the failure class so the
    boundary facade's mapping table does not depend on string-matching
    exception messages.
    """

    def __init__(
        self,
        message: str,
        *,
        reason: InferenceBackendReason = "vllm_http_error",
    ) -> None:
        super().__init__(message, reason=reason)


# ---------------------------------------------------------------------------
# ABC + DummyBackend (existing surface; unchanged behaviour)
# ---------------------------------------------------------------------------


class InferenceBackend(ABC):
    """Abstract interface for open-weight LLM inference."""

    @abstractmethod
    def generate(self, prompt: str, *, max_tokens: int = 512) -> str:
        """
        Generate text from *prompt*.

        Parameters
        ----------
        prompt:
            The input prompt.
        max_tokens:
            Upper bound on generated tokens.

        Returns
        -------
        str
            Generated text.
        """

    @abstractmethod
    def health_check(self) -> bool:
        """Return ``True`` if the backend is reachable and ready."""


class DummyBackend(InferenceBackend):
    """
    Deterministic stub used during development and testing.

    Returns a fixed template response so no GPU or API key is required.
    """

    def generate(self, prompt: str, *, max_tokens: int = 512) -> str:  # noqa: ARG002
        return f"[DummyBackend] Echo: {prompt[:80]}"

    def health_check(self) -> bool:
        return True


def get_default_backend() -> InferenceBackend:
    """Return the default backend (DummyBackend until a real one is configured)."""
    return DummyBackend()


__all__ = [
    "InferenceBackend",
    "DummyBackend",
    "get_default_backend",
    "InferenceBackendError",
    "InferenceBackendReason",
    "PodUnavailableError",
    "VLLMGenerationError",
]
