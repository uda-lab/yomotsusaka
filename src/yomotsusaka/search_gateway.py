"""
Search gateway — agent-facing search over redacted manifests.

Plugin boundary: a real implementation would back this with a vector store
(e.g. Qdrant, ChromaDB) or a keyword index.  The stub performs a naive
substring search over in-memory manifests.
"""
# MVP: substring-only; see docs/scaffold-status.md

from __future__ import annotations

import logging

from yomotsusaka.schemas import DocumentManifest

logger = logging.getLogger(__name__)


class SearchGateway:
    """
    Searches redacted :class:`~yomotsusaka.schemas.DocumentManifest` objects.

    Only redacted text and metadata are exposed; private values are never
    surfaced here.
    """

    def __init__(self, manifests: list[DocumentManifest] | None = None) -> None:
        self._manifests: list[DocumentManifest] = manifests or []

    def index(self, manifest: DocumentManifest) -> None:
        """Add *manifest* to the search index."""
        self._manifests.append(manifest)
        logger.debug("Indexed manifest %s", manifest.doc_id)

    def search(self, query: str, *, top_k: int = 10) -> list[DocumentManifest]:
        """
        Return up to *top_k* manifests whose redacted text contains *query*.

        This is a naive stub.  Replace with a real retrieval backend.
        """
        results = [m for m in self._manifests if query.lower() in m.redacted_text.lower()]
        return results[:top_k]
