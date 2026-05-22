"""
Validator — post-redaction quality checks.

Plugin boundary: real implementations can call Presidio, LLM Guard, or a
custom rule engine.  The stub always passes.
"""

from __future__ import annotations

import logging

from yomotsusaka.schemas import DocumentManifest

logger = logging.getLogger(__name__)


class ValidationError(Exception):
    """Raised when a manifest fails a validation rule."""


class Validator:
    """
    Checks that a :class:`~yomotsusaka.schemas.DocumentManifest` does not
    contain residual private data.

    Override :meth:`validate` in a subclass to integrate Presidio, LLM Guard,
    or a custom rule engine.
    """

    def validate(self, manifest: DocumentManifest) -> None:
        """
        Raise :class:`ValidationError` if *manifest* contains residual PII.

        The base implementation is a no-op stub.
        """
        logger.debug("Validator stub: manifest %s passed (no-op)", manifest.doc_id)
