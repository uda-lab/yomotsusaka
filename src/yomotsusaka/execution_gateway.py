"""
Execution gateway — stub — deferred for the local MVP; see `docs/runpod.md`.

Mediates agent-triggered operations.  Only agents that have been granted
access to an :class:`~yomotsusaka.schemas.ArtifactHandle` may request
execution of operations that touch private data.

Plugin boundary: real implementations add authentication, rate limiting, and
audit logging.  The local MVP exercises only the stub return value; real
policy enforcement and operation dispatch remain out of scope until a child
issue scopes them.  See ``docs/scaffold-status.md`` for module status.
"""

from __future__ import annotations

import logging
from typing import Any

from yomotsusaka.schemas import ArtifactHandle

logger = logging.getLogger(__name__)


class ExecutionGateway:
    """
    Stub execution gateway.

    Subclass and override :meth:`execute` to implement real policy enforcement.
    """

    def execute(
        self,
        handle: ArtifactHandle,
        operation: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Execute *operation* on the document identified by *handle*.

        Parameters
        ----------
        handle:
            Artifact handle authorising access.
        operation:
            Named operation (e.g. ``"summarise"``, ``"translate"``).
        params:
            Optional operation-specific parameters.

        Returns
        -------
        dict
            Operation result.  Structure is operation-specific.
        """
        logger.info(
            "ExecutionGateway stub: handle=%s op=%s params=%s",
            handle.handle_id,
            operation,
            params,
        )
        return {"status": "stub", "handle_id": handle.handle_id, "operation": operation}


# ---------------------------------------------------------------------------
# MVP-3 handshake stub (#47 / #42)
# ---------------------------------------------------------------------------
#
# ``ExecutionRequest`` is the activation symbol named in the issue #47
# MVP-3 exposure-contract handshake table. It is added here as a NAMED STUB
# so that the non-vacuity guard
# (``tests.test_exposure_contract_mvp3.test_handshake_paths_match_impl_issues``)
# can verify "module importable AND attribute present" without requiring
# the real #42 implementation to have landed.
#
# Backend PR #42 replaces this stub with the real Pydantic request model.
# Activation of the abstract ``ContractExecutionRequest`` is gated on
# ``__is_stub__`` being false: while the marker is True, the
# ``execution_request_candidate_provider`` fixture skips with a citation;
# the moment #42 lands the real class (flipping or removing the marker),
# the contract activates.
#
# Intentionally NOT exported from any agent-facing surface.


class ExecutionRequest:
    """Stub marker class for the issue #42 execution-gateway request model.

    Replace with the real Pydantic implementation in #42; flip
    ``__is_stub__`` to ``False`` (or remove the attribute) to activate the
    abstract exposure contract in :mod:`tests.test_exposure_contract_mvp3`.
    """

    __is_stub__: bool = True
