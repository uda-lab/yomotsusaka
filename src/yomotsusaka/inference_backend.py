"""
Inference backend interface and dummy implementation.

Real backends (vLLM + Qwen3-8B on RunPod, etc.) implement
:class:`InferenceBackend` and are wired in via configuration.

No hosted proprietary LLM APIs are used in the core path.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


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
