"""
RunPod lifecycle — three operating modes (``mock`` | ``attach`` | ``manage``).

This module is **private-side only** (see ``docs/architecture.md`` §7.1 and
metaplan Fork 6 of issue #46). It is NEVER imported by
:mod:`yomotsusaka.boundary`. ``PodHandle.pod_id`` and ``PodHandle.endpoint``
are classified ``never_expose``; they MUST NOT be copied into any
``ResolverSuccess`` / ``ResolverFailure`` / ``DocumentManifest`` /
``PublicHandle`` / ``BatchState`` / restoration audit record.

Mode semantics (metaplan Fork 1):

* ``mock`` — :class:`MockRunPodLifecycle`. Synthetic ``PodHandle``; no
  network. Default for unit tests and any CI run.
* ``attach`` — :class:`AttachRunPodLifecycle`. Reads ``RUNPOD_POD_ID`` and
  ``RUNPOD_POD_ENDPOINT`` from env; never creates or destroys a Pod.
  ``start_pod`` is a no-op that returns the env-provided handle;
  ``stop_pod`` is a logged no-op (owner is responsible per
  ``docs/runpod.md`` §9); ``is_ready`` issues ``GET {endpoint}/health``
  with a 5 s timeout. This is the only mode that MUST work for issue #46.
* ``manage`` — full RunPod GraphQL lifecycle. Stubbed
  :class:`NotImplementedError` in this PR; reserved for a follow-up issue
  scoping Pod-creation cost control.

Credentials are env-var-only (metaplan Fork 3). No config file, no secret
manager, no keyring integration. ``RUNPOD_API_KEY`` is read only in
``manage`` mode (currently NotImplementedError); ``RUNPOD_POD_ID`` and
``RUNPOD_POD_ENDPOINT`` are required in ``attach`` mode. None of these
values are logged, included in exception messages, returned in any
``PodHandle`` field, or serialised in any ``ResolverFailure.detail``.
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from yomotsusaka.inference_backend import PodUnavailableError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config + handle
# ---------------------------------------------------------------------------


@dataclass
class PodConfig:
    """Configuration for a RunPod pod.

    ``model_id`` defaults to the MVP-3 pin ``"Qwen/Qwen3-8B"`` per
    ``docs/runpod.md`` §4 and metaplan Fork 5.
    """

    gpu_type: str = "NVIDIA RTX A5000"
    image: str = "vllm/vllm-openai:latest"
    model_id: str = "Qwen/Qwen3-8B"
    disk_gb: int = 20
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class PodHandle:
    """Opaque reference to a running pod.

    Both fields are classified ``never_expose`` and MUST NOT be copied
    into any agent-facing surface. The boundary facade interacts with
    handles only by reference; the values themselves stay vault-side.
    """

    pod_id: str
    endpoint: str  # inference endpoint URL


# ---------------------------------------------------------------------------
# Configuration error
# ---------------------------------------------------------------------------


class RunPodConfigError(Exception):
    """Raised when the lifecycle mode is misconfigured.

    Examples: ``attach`` mode with ``RUNPOD_POD_ID`` unset, ``manage``
    mode without ``RUNPOD_API_KEY``, an unknown ``YOMOTSUSAKA_RUNPOD_MODE``
    value. The exception message names the missing env var (the key, NOT
    the value) so the caller can diagnose without leaking credentials.
    """


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class RunPodLifecycle:
    """Abstract base for RunPod lifecycle managers.

    Concrete subclasses are :class:`MockRunPodLifecycle` (no network),
    :class:`AttachRunPodLifecycle` (real ``attach`` mode), and a future
    ``ManageRunPodLifecycle`` (gated behind a follow-up issue;
    currently raises :class:`NotImplementedError` on instantiation).
    """

    def start_pod(self, config: PodConfig) -> PodHandle:
        raise NotImplementedError

    def stop_pod(self, handle: PodHandle) -> None:
        raise NotImplementedError

    def is_ready(self, handle: PodHandle) -> bool:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Mode 1: mock — synthetic handle, no network
# ---------------------------------------------------------------------------


class MockRunPodLifecycle(RunPodLifecycle):
    """No-network mock lifecycle. Default for unit tests and any CI run."""

    def start_pod(self, config: PodConfig) -> PodHandle:  # noqa: ARG002
        return PodHandle(
            pod_id=f"mock-pod-{uuid.uuid4().hex}",
            # 127.0.0.1:0 is intentionally unreachable; any caller that
            # tries to use this endpoint will fail fast rather than
            # accidentally hitting a real network service.
            endpoint="http://127.0.0.1:0",
        )

    def stop_pod(self, handle: PodHandle) -> None:  # noqa: ARG002
        return None

    def is_ready(self, handle: PodHandle) -> bool:  # noqa: ARG002
        # Mock pods are always "ready" — they have no service to probe.
        return True


# ---------------------------------------------------------------------------
# Mode 2: attach — env-supplied handle, real /health probe
# ---------------------------------------------------------------------------


_ATTACH_HEALTH_TIMEOUT_SECONDS = 5


class AttachRunPodLifecycle(RunPodLifecycle):
    """Attach to a Pod the owner has already provisioned.

    Reads ``RUNPOD_POD_ID`` and ``RUNPOD_POD_ENDPOINT`` from env.
    ``start_pod`` is a no-op that returns the env-provided handle and
    emits a single ``logger.info`` advisory reminding the owner to stop
    the Pod after use (``docs/runpod.md`` §9). ``stop_pod`` is a logged
    no-op. ``is_ready`` issues ``GET {endpoint}/health`` with a 5 s
    timeout and returns ``True`` iff the response is HTTP 200.

    No raw env-var value (pod id, endpoint, API key) is logged. The
    advisory log line contains only a fixed string; no credential
    interpolation.
    """

    def __init__(
        self,
        *,
        pod_id: str | None = None,
        endpoint: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        # Allow explicit construction (used by the smoke script / tests) as
        # well as env-driven construction (the default).
        resolved_pod_id = pod_id if pod_id is not None else os.environ.get("RUNPOD_POD_ID")
        resolved_endpoint = (
            endpoint if endpoint is not None else os.environ.get("RUNPOD_POD_ENDPOINT")
        )
        if not resolved_pod_id:
            raise RunPodConfigError("RUNPOD_POD_ID required in attach mode")
        if not resolved_endpoint:
            raise RunPodConfigError("RUNPOD_POD_ENDPOINT required in attach mode")
        self._pod_id = resolved_pod_id
        self._endpoint = resolved_endpoint.rstrip("/")
        self._transport = transport

    def _client(self) -> httpx.Client:
        kwargs: dict[str, object] = {"timeout": _ATTACH_HEALTH_TIMEOUT_SECONDS}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.Client(**kwargs)  # type: ignore[arg-type]

    def start_pod(self, config: PodConfig) -> PodHandle:  # noqa: ARG002
        # Fixed-string advisory; no credential interpolation. The metaplan
        # explicitly forbids echoing the env-var value here.
        logger.info(
            "attach mode: caller is responsible for stopping the owner-managed pod"
        )
        return PodHandle(pod_id=self._pod_id, endpoint=self._endpoint)

    def stop_pod(self, handle: PodHandle) -> None:  # noqa: ARG002
        logger.info(
            "attach mode: not stopping owner-managed pod (caller responsibility)"
        )
        return None

    def is_ready(self, handle: PodHandle) -> bool:
        url = f"{handle.endpoint.rstrip('/')}/health"
        try:
            with self._client() as client:
                response = client.get(url)
        except httpx.HTTPError:
            return False
        return response.status_code == 200


# ---------------------------------------------------------------------------
# Mode 3: manage — reserved for follow-up issue
# ---------------------------------------------------------------------------


class ManageRunPodLifecycle(RunPodLifecycle):
    """Reserved manage-mode lifecycle.

    Full RunPod GraphQL Pod-creation / destruction is out of scope for
    issue #46. Instantiation raises :class:`NotImplementedError` with a
    pointer to the follow-up issue. Metaplan Fork 1 + Fork 4 spelled out
    why: shipping Pod-creation without cost control (autoscaling guard,
    Pod-runtime cap, idle reaper) would be a billing footgun.
    """

    def __init__(self) -> None:
        raise NotImplementedError(
            "manage mode requires Pod-creation cost-control wiring; "
            "see the follow-up issue tracked in #46 metaplan Fork 1/Fork 4"
        )


# ---------------------------------------------------------------------------
# Mode selection
# ---------------------------------------------------------------------------


_MODE_ENV_VAR = "YOMOTSUSAKA_RUNPOD_MODE"


def lifecycle_from_env() -> RunPodLifecycle:
    """Return a lifecycle manager selected by ``$YOMOTSUSAKA_RUNPOD_MODE``.

    Defaults to ``"mock"``. Unknown values raise :class:`RunPodConfigError`.
    """
    mode = os.environ.get(_MODE_ENV_VAR, "mock").strip().lower()
    if mode == "mock":
        return MockRunPodLifecycle()
    if mode == "attach":
        return AttachRunPodLifecycle()
    if mode == "manage":
        return ManageRunPodLifecycle()
    raise RunPodConfigError(
        f"{_MODE_ENV_VAR} must be one of 'mock' | 'attach' | 'manage'"
    )


# ---------------------------------------------------------------------------
# Handshake table activation marker
# ---------------------------------------------------------------------------
#
# The MVP-3 exposure-contract handshake (issue #47) gates the abstract
# ``ContractPodHandle`` activation on ``__is_stub__`` being false. The
# real implementation lands in this PR, so remove the stub marker that
# the named-stub class previously carried — the contract activates the
# moment this module is importable AND ``AttachRunPodLifecycle`` is
# present without the marker.


__all__ = [
    "PodConfig",
    "PodHandle",
    "RunPodConfigError",
    "RunPodLifecycle",
    "MockRunPodLifecycle",
    "AttachRunPodLifecycle",
    "ManageRunPodLifecycle",
    "PodUnavailableError",
    "lifecycle_from_env",
]
