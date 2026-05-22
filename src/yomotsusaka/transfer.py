"""
Transfer — move artifacts between vault and external destinations.

Plugin boundary: real implementations support S3, GCS, SFTP, etc.
The stub only logs the intended transfer without moving any data.

Private data must never be sent to an uncontrolled external destination.
Implementations must enforce destination allow-lists.
"""

from __future__ import annotations

import logging
from typing import Any

from yomotsusaka.schemas import ArtifactHandle

logger = logging.getLogger(__name__)


class TransferError(Exception):
    """Raised when a transfer cannot be completed."""


class TransferBackend:
    """
    Abstract-style base class for transfer backends.

    Subclass and override :meth:`upload` / :meth:`download`.
    """

    def upload(
        self,
        handle: ArtifactHandle,
        destination: str,
        *,
        options: dict[str, Any] | None = None,
    ) -> str:
        """
        Upload the manifest (redacted, agent-safe) to *destination*.

        Returns the destination URI.

        STUB: logs and returns a fake URI.
        """
        logger.warning(
            "TransferBackend.upload is a stub — handle=%s dest=%s",
            handle.handle_id,
            destination,
        )
        return f"stub://{destination}/{handle.doc_id}"

    def download(
        self,
        source: str,
        *,
        options: dict[str, Any] | None = None,
    ) -> bytes:
        """
        Download content from *source*.

        STUB: raises :class:`TransferError`.
        """
        raise TransferError("TransferBackend.download is a stub")
