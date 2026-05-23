"""
RunPod lifecycle — stub — deferred for the local MVP; see `docs/runpod.md`.

RunPod (and equivalent GPU cloud providers) are ephemeral compute backends,
not durable private storage.  Pods should be started before a batch job and
stopped immediately after to minimise cost.

NOTE: No real RunPod API calls are made here.  The local MVP runs CPU-only
with ``DummyBackend``; real RunPod SDK / REST calls remain out of scope
until a child issue scopes them.  See ``docs/runpod.md`` for hardware and
image recommendations and ``docs/scaffold-status.md`` for module status.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class PodConfig:
    """Configuration for a RunPod pod."""
    gpu_type: str = "NVIDIA RTX A5000"
    image: str = "vllm/vllm-openai:latest"
    model_id: str = "Qwen/Qwen3-8B"
    disk_gb: int = 20
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class PodHandle:
    """Opaque reference to a running pod."""
    pod_id: str
    endpoint: str  # inference endpoint URL


class RunPodLifecycle:
    """
    Manages the start/stop lifecycle of RunPod GPU pods.

    STUB — replace method bodies with real RunPod SDK calls.
    """

    def start_pod(self, config: PodConfig) -> PodHandle:
        """
        Start a new pod and return a handle.

        STUB: always returns a fake handle.
        """
        logger.warning("RunPodLifecycle.start_pod is a stub — no real pod started")
        return PodHandle(pod_id="stub-pod-id", endpoint="http://localhost:8000")

    def stop_pod(self, handle: PodHandle) -> None:
        """
        Stop and terminate the pod identified by *handle*.

        STUB: no-op.
        """
        logger.warning(
            "RunPodLifecycle.stop_pod is a stub — pod %s not actually stopped",
            handle.pod_id,
        )

    def is_ready(self, handle: PodHandle) -> bool:
        """
        Return ``True`` if the pod is ready to serve inference requests.

        STUB: always returns ``False``.
        """
        logger.warning("RunPodLifecycle.is_ready is a stub — returning False")
        return False


# ---------------------------------------------------------------------------
# MVP-3 handshake stub (#47 / #46)
# ---------------------------------------------------------------------------
#
# ``AttachRunPodLifecycle`` is the activation symbol named in the issue #47
# MVP-3 exposure-contract handshake table. It is added here as a NAMED STUB
# so that the non-vacuity guard
# (``tests.test_exposure_contract_mvp3.test_handshake_paths_match_impl_issues``)
# can verify "module importable AND attribute present" without requiring
# the real #46 implementation to have landed.
#
# Backend PR #46 replaces this stub with the real attach-style lifecycle
# manager. Activation of the abstract ``ContractPodHandle`` is gated on
# ``__is_stub__`` being false: while the marker is True, the
# ``runpod_candidate_provider`` fixture skips with a citation; the moment
# #46 sets ``__is_stub__ = False`` (or removes the marker) on its real
# class, the contract activates.
#
# Intentionally NOT exported from any agent-facing surface.


class AttachRunPodLifecycle:
    """Stub marker class for the issue #46 attach-style RunPod lifecycle.

    Replace with the real implementation in #46; flip ``__is_stub__`` to
    ``False`` (or remove the attribute) to activate the abstract exposure
    contract in :mod:`tests.test_exposure_contract_mvp3`.
    """

    __is_stub__: bool = True
