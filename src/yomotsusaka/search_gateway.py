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
import os
from dataclasses import dataclass, field
from pathlib import Path

from yomotsusaka.schemas import DocumentManifest, PrivateDictEntry

logger = logging.getLogger(__name__)


# Relative location, under the vault root, of the redacted-only JSONL snapshot.
# Per ``docs/architecture.md`` §6.2 the index is part of the agent-facing
# tree; sibling-of ``manifests/`` and ``private/`` but containing redacted
# projections only.
_INDEX_SUBDIR = "index"
_INDEX_FILENAME = "manifests.jsonl"


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

    # ------------------------------------------------------------------
    # JSONL snapshot / load (issue #78)
    # ------------------------------------------------------------------

    def snapshot(self, vault_root: Path) -> Path:
        """Persist every indexed manifest to ``<vault_root>/index/manifests.jsonl``.

        Writes one ``DocumentManifest.model_dump_json()`` line per indexed
        manifest. The write is atomic: contents are flushed and ``fsync``'d
        on a sibling ``manifests.jsonl.tmp`` and then ``os.replace``'d into
        place, so a partial write never leaves a corrupted final file.

        The on-disk artifact is **redacted-only**. The :class:`QueryResolver`
        state, if any, is private-side and is NEVER serialised here.

        Single-writer assumption
        ------------------------
        The batch runner is the only intended caller. There is no locking;
        concurrent writers from multiple processes are unsupported and may
        race on the temp file. Use one writer at a time.

        Parameters
        ----------
        vault_root:
            Vault root directory. The index subdirectory is created if
            absent. ``OSError`` from the underlying file operations
            propagates to the caller (the runner records it on
            :class:`BatchSummary` rather than aborting the whole batch).

        Returns
        -------
        Path
            Absolute path to the final ``manifests.jsonl`` file.
        """
        if not isinstance(vault_root, Path):
            raise TypeError("vault_root must be a pathlib.Path")

        index_dir = vault_root / _INDEX_SUBDIR
        index_dir.mkdir(parents=True, exist_ok=True)
        final_path = index_dir / _INDEX_FILENAME
        tmp_path = index_dir / f"{_INDEX_FILENAME}.tmp"

        try:
            # Open with ``"w"`` (truncate) so a prior snapshot is replaced
            # cleanly. JSONL is append-shaped on the wire but the file is
            # rewritten in full each snapshot — this keeps the read path a
            # one-shot ``readlines`` and avoids any need for compaction.
            with tmp_path.open("w", encoding="utf-8", newline="\n") as fh:
                for manifest in self._manifests:
                    line = manifest.model_dump_json()
                    fh.write(line)
                    fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, final_path)
        except OSError:
            # Best-effort cleanup of the temp file on failure. Suppress any
            # secondary OSError from the cleanup so the original (more
            # informative) exception propagates unmodified.
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:  # pragma: no cover - defensive
                pass
            raise

        # Count-only log line per privacy discipline (no content echo).
        logger.debug(
            "SearchGateway.snapshot wrote %d manifest(s)", len(self._manifests)
        )
        return final_path

    def load(self, vault_root: Path) -> int:
        """Append manifests from ``<vault_root>/index/manifests.jsonl`` to the index.

        Returns the number of manifests loaded. If the file does not exist
        this is a no-op and returns ``0``.

        Best-effort partial load
        ------------------------
        Lines that fail JSON parsing or :class:`DocumentManifest` schema
        validation are skipped; a count-only warning is logged. The
        remaining valid lines are still appended to the in-memory list.

        Idempotency
        -----------
        ``load`` appends — it does not reset internal state. Calling it
        twice on the same file double-indexes the manifests; callers that
        need a fresh view should construct a fresh :class:`SearchGateway`.

        The :class:`QueryResolver`, if attached, is NOT touched here. The
        resolver carries ``(raw_value, key)`` pairs that are private-side
        only and must be repopulated by the caller from private-side data
        they already hold.
        """
        if not isinstance(vault_root, Path):
            raise TypeError("vault_root must be a pathlib.Path")

        final_path = vault_root / _INDEX_SUBDIR / _INDEX_FILENAME
        if not final_path.exists():
            return 0

        loaded = 0
        skipped = 0
        with final_path.open("r", encoding="utf-8") as fh:
            for line_no, raw_line in enumerate(fh, start=1):
                line = raw_line.strip()
                if not line:
                    # Blank line — silently skip; not a parse failure worth
                    # logging.
                    continue
                try:
                    manifest = DocumentManifest.model_validate_json(line)
                except ValueError:
                    # ``ValueError`` covers ``ValidationError`` (Pydantic v2
                    # raises a ``ValueError`` subclass) and any JSON decode
                    # error. Log COUNT ONLY — never the line content.
                    logger.warning(
                        "SearchGateway.load skipped malformed index line %d",
                        line_no,
                    )
                    skipped += 1
                    continue
                self._manifests.append(manifest)
                loaded += 1

        if skipped:
            logger.warning(
                "SearchGateway.load completed with %d skipped malformed line(s)",
                skipped,
            )
        logger.debug("SearchGateway.load loaded %d manifest(s)", loaded)
        return loaded


__all__ = ["SearchGateway", "QueryResolver", "ResolvedQuery"]
