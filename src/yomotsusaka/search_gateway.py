"""Private-side internal kernel. Ordinary agents should use ``yomotsusaka.boundary`` instead.

Search gateway — agent-facing search over redacted manifests.

Plugin boundary: a real implementation would back this with a vector store
(e.g. Qdrant, ChromaDB) or a keyword index.  The stub performs a naive
substring search over in-memory manifests.

Query privacy (``architecture.md`` §12.3)
-----------------------------------------
Agent-submitted queries may themselves contain raw private values. The
optional :class:`QueryResolver` translates such raw values into their
redacted entity keys **before** the substring scan touches the redacted
index, so the index only ever sees redacted-side text. The resolver is
populated in-memory by the caller (typically the same code path that just
committed a document) from already-materialised
:class:`~yomotsusaka.schemas.PrivateDictEntry` objects; it has **no**
agent-facing reverse-lookup surface (``key → raw value``) and is not
re-exported from :mod:`yomotsusaka.boundary`.
"""
# MVP: substring-only; see docs/scaffold-status.md

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from yomotsusaka.schemas import DocumentManifest, PrivateDictEntry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedQuery:
    """Result of :meth:`QueryResolver.translate`.

    Attributes
    ----------
    translated_terms:
        Tuple of redacted entity keys produced by substituting registered
        raw values in the query, in left-to-right order of first appearance.
        Never contains raw private values.
    residual:
        The query text with every registered raw value stripped to whatever
        remains after translation. May be the empty string. Never contains
        a raw value that was registered with the resolver; unregistered
        text passes through unchanged.
    """

    translated_terms: tuple[str, ...] = ()
    residual: str = ""


@dataclass
class QueryResolver:
    """In-memory raw→key translator for the search gateway.

    Populated by the private-boundary caller from already-materialised
    :class:`~yomotsusaka.schemas.PrivateDictEntry` objects (typically right
    after :func:`yomotsusaka.pipeline.process_document` returns). This
    component is **private-side only**: it MUST NOT be re-exported through
    :mod:`yomotsusaka.boundary` and MUST NOT expose any reverse lookup
    (``key → raw value``).

    Translation semantics
    ---------------------
    :meth:`translate` performs **longest-match-first**, case-sensitive
    substring substitution: if ``"Alice"`` and ``"Alice Tan"`` are both
    registered, the longer value wins for any occurrence that matches
    both. Unregistered text passes through into the residual. The same
    raw value MAY be registered more than once with the *same* redacted
    key (idempotent re-register at index time); registering it twice with
    *different* keys raises :class:`ValueError`.
    """

    # Map raw value → redacted key. Kept private; no agent-facing accessor.
    _map: dict[str, str] = field(default_factory=dict)

    def register(self, raw_value: str, key: str) -> None:
        """Register a single ``(raw_value, key)`` pair.

        Re-registering the same raw value with the same key is a no-op
        (idempotent — supports the common case of re-indexing the same
        manifest). Re-registering with a *different* key raises
        :class:`ValueError`; that would indicate two documents disagree
        on the redaction of the same raw value, which currently cannot
        happen with :func:`yomotsusaka.redactor._make_key` (deterministic
        SHA-8 on the value) but is enforced here so a future kind change
        that broke determinism would surface loudly instead of silently
        diverging.
        """
        if not isinstance(raw_value, str) or raw_value == "":
            raise ValueError("raw_value must be a non-empty string")
        if not isinstance(key, str) or key == "":
            raise ValueError("key must be a non-empty string")
        existing = self._map.get(raw_value)
        if existing is not None and existing != key:
            raise ValueError(
                "QueryResolver.register: raw value already registered with a "
                "different redacted key"
            )
        self._map[raw_value] = key

    def register_entries(self, entries: list[PrivateDictEntry]) -> None:
        """Register every ``(original_value, key)`` pair from *entries*."""
        for entry in entries:
            self.register(entry.original_value, entry.key)

    def translate(self, query: str) -> ResolvedQuery:
        """Translate *query* into ``(translated_terms, residual)``.

        Longest-match-first, case-sensitive substring replacement. The
        residual is the query text with every registered raw value
        removed; unregistered text survives verbatim. Empty / non-string
        input yields an empty :class:`ResolvedQuery`.
        """
        if not isinstance(query, str) or query == "":
            return ResolvedQuery()
        if not self._map:
            return ResolvedQuery(translated_terms=(), residual=query)

        # Longest-match-first: sort raw values by descending length so
        # "Alice Tan" wins over "Alice" when both are registered.
        raw_values_sorted = sorted(self._map.keys(), key=len, reverse=True)

        # Walk the query left-to-right. At each position try the longest
        # registered raw value first; on a hit emit its key as a
        # translated term and skip past it. On miss copy one character
        # into the residual and advance.
        translated: list[str] = []
        seen_keys: set[str] = set()
        residual_parts: list[str] = []
        i = 0
        n = len(query)
        while i < n:
            matched = False
            for raw in raw_values_sorted:
                if not raw:
                    continue
                if query.startswith(raw, i):
                    key = self._map[raw]
                    if key not in seen_keys:
                        translated.append(key)
                        seen_keys.add(key)
                    i += len(raw)
                    matched = True
                    break
            if not matched:
                residual_parts.append(query[i])
                i += 1

        residual = "".join(residual_parts)
        if translated:
            # Privacy-discipline log line per CLAUDE.md: count only; never
            # the terms or the raw query string.
            logger.debug(
                "QueryResolver.translate produced %d translated term(s)",
                len(translated),
            )
        return ResolvedQuery(translated_terms=tuple(translated), residual=residual)


class SearchGateway:
    """
    Searches redacted :class:`~yomotsusaka.schemas.DocumentManifest` objects.

    Only redacted text and metadata are exposed; private values are never
    surfaced here.

    Optional :class:`QueryResolver` integration
    -------------------------------------------
    When a ``query_resolver`` is supplied at construction time, callers may
    additionally hand :meth:`index` a list of :class:`PrivateDictEntry`
    objects via the ``private_entries`` kwarg; the gateway will register
    each entry's ``(original_value, key)`` pair on the resolver so that
    subsequent :meth:`search` calls translate raw private terms in the
    query into redacted keys **before** the substring scan runs. A gateway
    constructed *without* a ``query_resolver`` behaves identically to the
    pre-resolver implementation, including the existing zero-hit guarantee
    for queries containing raw private values.
    """

    def __init__(
        self,
        manifests: list[DocumentManifest] | None = None,
        *,
        query_resolver: QueryResolver | None = None,
    ) -> None:
        self._manifests: list[DocumentManifest] = manifests or []
        self._resolver: QueryResolver | None = query_resolver

    @property
    def query_resolver(self) -> QueryResolver | None:
        """Return the attached :class:`QueryResolver`, or ``None``.

        Read-only accessor used by the boundary to compute snippet needles
        from translated terms. The resolver itself is private-side state;
        this accessor never returns its internal map.
        """
        return self._resolver

    def index(
        self,
        manifest: DocumentManifest,
        *,
        private_entries: list[PrivateDictEntry] | None = None,
    ) -> None:
        """Add *manifest* to the search index.

        If *private_entries* is supplied **and** a :class:`QueryResolver`
        was attached at construction time, the entries are registered on
        the resolver so subsequent searches can translate raw private
        terms in the query into the corresponding redacted keys. When no
        resolver is attached the entries are ignored (the gateway has no
        place to put them, and silently dropping them keeps the index-time
        contract permissive for callers that index from multiple sources).
        """
        self._manifests.append(manifest)
        if private_entries and self._resolver is not None:
            self._resolver.register_entries(private_entries)
        logger.debug("Indexed manifest %s", manifest.doc_id)

    def search(self, query: str, *, top_k: int = 10) -> list[DocumentManifest]:
        """
        Return up to *top_k* manifests whose redacted text contains *query*.

        When a :class:`QueryResolver` is attached, the gateway first
        translates *query* via :meth:`QueryResolver.translate` and runs
        the substring scan once per translated term plus once on the
        residual (if non-empty), deduplicating manifests in first-seen
        order. When no resolver is attached, the original behaviour is
        preserved bit-for-bit: a single case-insensitive substring scan
        over the raw query.

        This is a naive stub.  Replace with a real retrieval backend.
        """
        if self._resolver is None:
            results = [
                m for m in self._manifests if query.lower() in m.redacted_text.lower()
            ]
            return results[:top_k]

        resolved = self._resolver.translate(query)
        # Build the list of needles: every translated term, then the
        # residual (skipping empty / whitespace-only residual so that an
        # all-translated query does not silently degrade into a "return
        # everything" scan).
        needles: list[str] = list(resolved.translated_terms)
        if resolved.residual and resolved.residual.strip():
            needles.append(resolved.residual)

        # If translation consumed everything and the residual is empty,
        # but no translated terms were produced (e.g. empty query),
        # preserve the legacy behaviour of an empty-needle short-circuit.
        if not needles:
            return []

        seen_ids: set[str] = set()
        ordered: list[DocumentManifest] = []
        for needle in needles:
            needle_lower = needle.lower()
            for m in self._manifests:
                if m.doc_id in seen_ids:
                    continue
                if needle_lower in m.redacted_text.lower():
                    ordered.append(m)
                    seen_ids.add(m.doc_id)
                    if len(ordered) >= top_k:
                        return ordered
        return ordered[:top_k]


__all__ = ["SearchGateway", "QueryResolver", "ResolvedQuery"]
