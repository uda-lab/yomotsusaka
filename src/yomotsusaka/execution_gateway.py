"""
Execution gateway — mediates agent-triggered operations.

Only agents that have been granted access to an
:class:`~yomotsusaka.schemas.ArtifactHandle` may request execution of
operations that touch private data.

Plugin boundary: real implementations add authentication, rate limiting, and
audit logging.
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
