"""
RunPod lifecycle — stub for ephemeral GPU Pod management.

RunPod (and equivalent GPU cloud providers) are ephemeral compute backends,
not durable private storage.  Pods should be started before a batch job and
stopped immediately after to minimise cost.

NOTE: No real RunPod API calls are made here.  Fill in the RunPod SDK /
REST calls when ready.  See docs/runpod-notes.md for hardware and image
recommendations.
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
