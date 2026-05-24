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
* ``manage`` — :class:`ManageRunPodLifecycle`. Real RunPod REST lifecycle
  (create → wait → delete) issued via ``httpx`` against
  ``https://rest.runpod.io/v1``. ``RUNPOD_API_KEY`` is read from env (or
  passed explicitly) as a bearer token. All log lines are category-only
  literals; Pod IDs, endpoint URLs, and bearer tokens never appear in any
  log record, exception message, or :class:`PodHandle` field exposed
  outside the private kernel. See ``docs/runpod-agent-lifecycle.md`` for
  the owner/agent split and cost-control rationale (issue #70 / #76).

Credentials are env-var-only (metaplan Fork 3). No config file, no secret
manager, no keyring integration. ``RUNPOD_API_KEY`` is read only in
``manage`` mode; ``RUNPOD_POD_ID`` and ``RUNPOD_POD_ENDPOINT`` are
required in ``attach`` mode. None of these values are logged, included in
exception messages, returned in any ``PodHandle`` field, or serialised in
any ``ResolverFailure.detail``.
"""

from __future__ import annotations

import logging
import os
import time
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
    mode with neither ``api_key`` kwarg nor ``RUNPOD_API_KEY`` env var,
    an unknown ``YOMOTSUSAKA_RUNPOD_MODE`` value. The exception message
    names the missing env var (the key, NOT the value) so the caller can
    diagnose without leaking credentials.
    """


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class RunPodLifecycle:
    """Abstract base for RunPod lifecycle managers.

    Concrete subclasses are :class:`MockRunPodLifecycle` (no network),
    :class:`AttachRunPodLifecycle` (env-supplied handle, real ``attach``
    mode), and :class:`ManageRunPodLifecycle` (real REST create / wait /
    delete; the cost-controlled ``manage`` mode shipped by issue #76 /
    closes #70).
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
# Mode 3: manage — real REST lifecycle (create / wait / delete)
# ---------------------------------------------------------------------------


_RUNPOD_REST_BASE = "https://rest.runpod.io/v1"
"""RunPod REST API base URL.

Held as a module-level constant so a future endpoint change (or a test
substitution) does not require touching call sites. Decision 1 of
``/tmp/mvp4_tightened_76.md`` freezes the REST mechanism; the exact base
URL is the only seam left swappable.
"""

_MANAGE_CREATE_TIMEOUT_SECONDS = 30
"""Single ``POST /v1/pods`` request timeout (httpx Client timeout)."""

_MANAGE_HEALTH_POLL_INTERVAL_SECONDS = 5
"""Sleep between consecutive ``/health`` probes during the wait phase."""

_MANAGE_HEALTH_POLL_MAX_ATTEMPTS = 60
"""Maximum number of ``/health`` probes. 60 * 5 s = 5 minute wall-clock cap."""

_MANAGE_DELETE_TIMEOUT_SECONDS = 30
"""Single ``DELETE /v1/pods/{podId}`` request timeout."""

_MANAGE_CLEANUP_RETRY_DELAY_SECONDS = 2
"""Sleep between the first and second cleanup attempts (issue #90).

When the first ``DELETE /v1/pods/{podId}`` attempt fails (transient
network error or non-2xx response), the lifecycle waits this many
seconds and issues exactly ONE more REST-based attempt before raising
``PodUnavailableError("cleanup_failed")``. The retry is bounded
(single attempt) to keep the worst-case cleanup wall-clock predictable
for the agent-runnable MVP-5 flow (umbrella #89): two REST attempts
plus one short sleep cannot exceed ``2 * _MANAGE_DELETE_TIMEOUT_SECONDS
+ _MANAGE_CLEANUP_RETRY_DELAY_SECONDS`` (62 s by default).

Module-level so L1 tests can monkeypatch it to 0 without touching
``time.sleep`` mocks (mirrors the ``_MANAGE_HEALTH_POLL_*`` pattern).
"""

_MANAGE_CLEANUP_MAX_ATTEMPTS = 2
"""Total number of cleanup attempts (1 original + 1 bounded retry).

Issue #90: a bounded REST-based safe-retry on cleanup failure. The
retry uses the same REST mechanism — no fall-back to ``runpodctl`` or
any out-of-band channel — so the public-safe category vocabulary is
unchanged. Module-level so L1 tests can monkeypatch to 1 to assert
the no-retry baseline if needed.
"""


class ManageRunPodLifecycle(RunPodLifecycle):
    """Real RunPod ``manage`` mode — create → wait → delete via REST.

    Issued exclusively over the RunPod REST API (``rest.runpod.io/v1``)
    via :mod:`httpx`. Authenticates with ``Authorization: Bearer
    ${RUNPOD_API_KEY}`` (read from env unless an explicit ``api_key``
    kwarg overrides). No subprocess fall-back — Decision 1 of issue #76
    freezes the mechanism on REST because the repository already mocks
    every other HTTP seam via :class:`httpx.MockTransport`
    (``AttachRunPodLifecycle``, ``VLLMBackend``,
    ``smoke_runpod._probe_chat_completions``); adding a second mock
    idiom for one class would diverge the test surface.

    Privacy invariants (binding per ``docs/runpod-agent-lifecycle.md`` §6
    and ``/tmp/mvp4_tightened_76.md`` Decision 3):

    * Every log record contains ONLY one of the 10 category literals
      (``created``, ``waiting_health``, ``healthy``, ``deleted``, …).
    * No exception message echoes the Pod ID, endpoint URL, response
      body, raw ``httpx`` error text, or the bearer token.
    * The returned :class:`PodHandle` keeps ``pod_id`` / ``endpoint``
      as ``never_expose`` private state; the boundary never widens.

    Constructor contract (Decision 2):

    * ``api_key=None`` reads ``RUNPOD_API_KEY``; an explicit non-None
      string overrides; an explicit empty string is treated as missing.
    * ``pod_config`` is the default :class:`PodConfig` passed to
      :meth:`start_pod` when the caller does not supply one.
    * ``transport`` is the ``httpx.BaseTransport`` injection seam for
      the L1 mock tests.
    * ``pod_id`` / ``endpoint`` are present **only** so the exposure-
      contract test (`tests/test_exposure_contract_mvp3.py::TestPodHandleContract`)
      can construct the manage-mode lifecycle with sentinel values and
      receive the corresponding :class:`PodHandle` back without making
      any network call. When BOTH are provided, :meth:`start_pod` skips
      the REST create and returns the handle directly. When either is
      absent, ``RUNPOD_API_KEY`` must be available (the normal path).
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        pod_config: PodConfig | None = None,
        transport: httpx.BaseTransport | None = None,
        pod_id: str | None = None,
        endpoint: str | None = None,
    ) -> None:
        # Explicit pod_id+endpoint short-circuits the create call entirely
        # — this is the seam the exposure-contract test relies on. In that
        # mode we do not require an API key; the lifecycle becomes a thin
        # wrapper that returns the supplied handle and proxies is_ready /
        # stop_pod against it.
        bypass = pod_id is not None and endpoint is not None

        # api_key resolution mirrors AttachRunPodLifecycle's explicit-args-
        # override-env shape. An explicitly empty string is treated as
        # missing, matching VLLMBackend.__init__ semantics.
        resolved_api_key: str | None
        if api_key is not None:
            resolved_api_key = api_key
        else:
            resolved_api_key = os.environ.get("RUNPOD_API_KEY")
        if resolved_api_key == "":
            resolved_api_key = None

        if not bypass and not resolved_api_key:
            raise RunPodConfigError(
                "RUNPOD_API_KEY required in manage mode"
            )

        self._api_key = resolved_api_key
        self._pod_config = pod_config or PodConfig()
        self._transport = transport
        self._bypass_pod_id = pod_id
        self._bypass_endpoint = endpoint.rstrip("/") if endpoint else None

    # ------------------------------------------------------------------
    # httpx client construction (REST + health probe)
    # ------------------------------------------------------------------

    def _rest_client(self, *, timeout: float) -> httpx.Client:
        kwargs: dict[str, object] = {
            "timeout": timeout,
            "base_url": _RUNPOD_REST_BASE,
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.Client(**kwargs)  # type: ignore[arg-type]

    def _health_client(self) -> httpx.Client:
        kwargs: dict[str, object] = {"timeout": _ATTACH_HEALTH_TIMEOUT_SECONDS}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.Client(**kwargs)  # type: ignore[arg-type]

    def _auth_headers(self) -> dict[str, str]:
        # The bearer header is built only at call time and never logged.
        # The Authorization value never leaves this method.
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # Public lifecycle API
    # ------------------------------------------------------------------

    def start_pod(self, config: PodConfig | None = None) -> PodHandle:
        """Create a Pod and wait for its endpoint to become healthy.

        When the lifecycle was constructed with explicit ``pod_id`` and
        ``endpoint`` kwargs, the REST create is skipped entirely and the
        supplied handle is returned without any network activity. This is
        the seam the exposure-contract test relies on (see Decision 2 in
        ``/tmp/mvp4_tightened_76.md``).

        Otherwise: ``POST /v1/pods`` with the resolved :class:`PodConfig`,
        parse the created Pod's id+endpoint, then poll ``/health`` up to
        ``_MANAGE_HEALTH_POLL_MAX_ATTEMPTS`` times.
        """
        if self._bypass_pod_id is not None and self._bypass_endpoint is not None:
            # No-network branch — exposure-contract test path.
            return PodHandle(
                pod_id=self._bypass_pod_id, endpoint=self._bypass_endpoint
            )

        resolved_config = config or self._pod_config
        try:
            with self._rest_client(timeout=_MANAGE_CREATE_TIMEOUT_SECONDS) as client:
                response = client.post(
                    "/pods",
                    headers=self._auth_headers(),
                    json=self._create_payload(resolved_config),
                )
        except httpx.HTTPError:
            # Decision 3 / S2: PodUnavailableError carries only the
            # category literal. The underlying httpx error message
            # (which may include the URL) does NOT propagate.
            logger.info("create_failed")
            raise PodUnavailableError("create_failed")

        if not 200 <= response.status_code < 300:
            logger.info("create_failed")
            raise PodUnavailableError("create_failed")

        try:
            data = response.json()
            pod_id, endpoint = self._extract_pod_identity(data)
        except (ValueError, KeyError, TypeError):
            logger.info("create_failed")
            raise PodUnavailableError("create_failed")

        if not pod_id or not endpoint:
            logger.info("create_failed")
            raise PodUnavailableError("create_failed")

        handle = PodHandle(pod_id=pod_id, endpoint=endpoint.rstrip("/"))
        logger.info("created")

        # Wait phase — pure category-only logging.
        self._wait_for_healthy(handle)
        return handle

    def stop_pod(self, handle: PodHandle, *, terminate: bool = True) -> None:
        """Stop or delete the Pod.

        Default ``terminate=True`` issues ``DELETE /v1/pods/{podId}`` —
        this is the cost-control default per ``docs/runpod.md`` §9 and
        issue #70: stopped Pods continue to bill for retained volume
        storage, so the agent-managed flow deletes by default. Set
        ``terminate=False`` only when the caller explicitly wants to
        retain ``/workspace`` (rare; documented in the runbook).

        Bypass-mode (``pod_id`` / ``endpoint`` supplied at construction
        with no API key) is a no-op: the bypass seam exists only for
        the exposure-contract test (see Decision 2 in
        ``/tmp/mvp4_tightened_76.md``); there is no REST account on
        behalf of which to issue ``DELETE``. Without this short-circuit,
        ``_auth_headers`` would build ``Authorization: Bearer None`` and
        surface a misleading ``cleanup_failed`` (copilot review on PR
        #84).

        Bounded safe-retry (issue #90 / MVP-5 umbrella #89): when the
        first REST attempt fails (transient HTTPError or non-2xx
        response), wait ``_MANAGE_CLEANUP_RETRY_DELAY_SECONDS`` and
        issue ONE more REST attempt before raising
        ``PodUnavailableError("cleanup_failed")``. The retry stays on
        the REST mechanism — no out-of-band ``runpodctl`` fall-back —
        so the public-safe category vocabulary and the agent-facing
        surface contract are unchanged. The bound is intentionally
        narrow (single retry) to keep worst-case cleanup wall-clock
        predictable for the agent-runnable lifecycle.
        """
        if self._bypass_pod_id is not None and self._bypass_endpoint is not None:
            # Bypass mode — the test seam supplied the handle; no REST
            # account is configured, so nothing to delete on the wire.
            return
        if not self._api_key:
            # Defensive: the non-bypass path requires an API key.
            raise RunPodConfigError("RUNPOD_API_KEY required for stop_pod")

        path = (
            f"/pods/{handle.pod_id}"
            if terminate
            else f"/pods/{handle.pod_id}/stop"
        )

        # Bounded REST retry loop (issue #90). The loop runs at most
        # ``_MANAGE_CLEANUP_MAX_ATTEMPTS`` times; on each failure that is
        # not the final attempt, we log ``cleanup_retry`` (diagnostic
        # surface only) and sleep before retrying. On the final failure
        # we log ``cleanup_failed`` and raise. No public-safe category
        # change: the category vocabulary the helper script consumes
        # (``cleanup_failed`` / ``deleted`` / ``stopped``) is unchanged.
        # No reason string is propagated past the retry boundary — the
        # raw ``httpx`` exception text and the response body MUST NOT
        # reach the public-safe surface (per Decision 3 / §6).
        for attempt in range(_MANAGE_CLEANUP_MAX_ATTEMPTS):
            attempt_failed = False
            try:
                with self._rest_client(
                    timeout=_MANAGE_DELETE_TIMEOUT_SECONDS
                ) as client:
                    if terminate:
                        response = client.delete(
                            path, headers=self._auth_headers()
                        )
                    else:
                        response = client.post(
                            path, headers=self._auth_headers()
                        )
            except httpx.HTTPError:
                attempt_failed = True
            else:
                if 200 <= response.status_code < 300:
                    logger.info("deleted" if terminate else "stopped")
                    return
                attempt_failed = True

            # Failure on this attempt. If we have a retry budget left,
            # emit the diagnostic-only ``cleanup_retry`` marker and sleep.
            # ``cleanup_retry`` is a logger-only diagnostic literal
            # (carries no Pod id / URL / response body / bearer / exception
            # text) — it never reaches the public-safe ``lifecycle:``
            # channel emitted by ``scripts/manage_runpod.py``.
            if attempt_failed and attempt < _MANAGE_CLEANUP_MAX_ATTEMPTS - 1:
                logger.info("cleanup_retry")
                if _MANAGE_CLEANUP_RETRY_DELAY_SECONDS > 0:
                    time.sleep(_MANAGE_CLEANUP_RETRY_DELAY_SECONDS)

        # All attempts exhausted — surface the terminal category. The
        # public-safe ``cleanup_failed`` literal is the only thing the
        # helper script consumes.
        logger.info("cleanup_failed")
        raise PodUnavailableError("cleanup_failed")

    def is_ready(self, handle: PodHandle) -> bool:
        """Probe ``GET {endpoint}/health`` — identical to attach mode."""
        url = f"{handle.endpoint.rstrip('/')}/health"
        try:
            with self._health_client() as client:
                response = client.get(url)
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    # ------------------------------------------------------------------
    # Internal helpers (no logging — caller emits category literals only)
    # ------------------------------------------------------------------

    def _wait_for_healthy(self, handle: PodHandle) -> None:
        """Poll :meth:`is_ready` up to ``_MANAGE_HEALTH_POLL_MAX_ATTEMPTS``
        times, sleeping ``_MANAGE_HEALTH_POLL_INTERVAL_SECONDS`` between
        probes. Raises :class:`PodUnavailableError` with the
        ``wait_timeout`` category on exhaustion.

        Both constants are module-level so the L1 tests can monkeypatch
        them to small values without resorting to ``time.sleep`` mocks
        (see Decision 5 in ``/tmp/mvp4_tightened_76.md``).
        """
        for _ in range(_MANAGE_HEALTH_POLL_MAX_ATTEMPTS):
            if self.is_ready(handle):
                logger.info("healthy")
                return
            logger.info("waiting_health")
            time.sleep(_MANAGE_HEALTH_POLL_INTERVAL_SECONDS)
        logger.info("wait_timeout")
        raise PodUnavailableError("wait_timeout")

    @staticmethod
    def _create_payload(config: PodConfig) -> dict[str, Any]:
        """Build the ``POST /v1/pods`` JSON body from a :class:`PodConfig`.

        Field names follow the public RunPod REST schema. The payload is
        constructed here (not in :meth:`start_pod`) so the test seam can
        inspect it via a :class:`httpx.MockTransport` handler.
        """
        return {
            "gpuTypeIds": [config.gpu_type],
            "imageName": config.image,
            "containerDiskInGb": config.disk_gb,
            "env": {"MODEL_ID": config.model_id, **config.extra},
        }

    @staticmethod
    def _extract_pod_identity(data: Any) -> tuple[str | None, str | None]:
        """Read ``id`` and the inference endpoint URL out of the REST
        response body.

        RunPod's response shapes vary across template versions; this
        helper tolerates two common forms:

        1. ``{"id": "<pod-id>", "endpoint": "https://..."}`` — flat.
        2. ``{"id": "<pod-id>", "publicIp": "1.2.3.4", "ports": [...]}``
           — synthesise ``https://<pod-id>-8000.proxy.runpod.net`` from
           the documented runpod-proxy pattern.

        Returns ``(None, None)`` on unrecognised shapes; the caller then
        treats this as ``create_failed`` without echoing the raw body.
        """
        if not isinstance(data, dict):
            return None, None
        pod_id = data.get("id") or data.get("pod_id")
        if not isinstance(pod_id, str) or not pod_id:
            return None, None
        endpoint = data.get("endpoint") or data.get("publicEndpoint")
        if not isinstance(endpoint, str) or not endpoint:
            # Synthesise the RunPod proxy URL from the Pod ID. The proxy
            # pattern is documented in docs/runpod.md and used elsewhere
            # in the repo.
            endpoint = f"https://{pod_id}-8000.proxy.runpod.net"
        return pod_id, endpoint


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
        # API key resolution happens inside ManageRunPodLifecycle.__init__;
        # raises RunPodConfigError if unset.
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
