"""
Batch runner — drain a directory of raw documents through :class:`LocalFacade`.

This module is **agent-facing**. It consumes only the public facade surface
plus the public schemas / search gateway types:

* :class:`yomotsusaka.facade.LocalFacade` (process / inspect / search)
* :class:`yomotsusaka.search_gateway.SearchGateway` (index the redacted view)
* :class:`yomotsusaka.boundary.ProcessRequest` / :class:`SpanSpec`
* :class:`yomotsusaka.schemas.DocumentManifest` (re-loaded from the vault to
  feed the gateway's index — the manifest is the public, redacted projection
  of the source document; no raw private values are read here)
* :class:`yomotsusaka.batch_queue.BatchQueue` (queue state transitions)
* :class:`yomotsusaka.span_proposer.DeterministicSpanProposer` (default
  proposer; LLM-free)

Privacy invariants (binding)
----------------------------
This module MUST NOT import private-kernel modules — concretely
``yomotsusaka.pipeline``, ``yomotsusaka.commit``, ``yomotsusaka.restoration_api``,
``yomotsusaka.templates``, ``yomotsusaka.scrubber``, or ``yomotsusaka.audit``.
Access to those primitives is routed exclusively through the
:class:`LocalFacade`. The invariant is verified by a literal substring scan of
this source file (see ``tests/test_batch_runner.py``).

No raw private value, raw input text, or vault filesystem path may appear in
:class:`BatchSummary`, in CLI stdout, or in any log line emitted from this
module. Per-document failures record the caller-supplied document path
(``doc_ref``) only; paths are caller-public for MVP per the child_06 spec.

The runner is single-threaded and processes documents sequentially.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from yomotsusaka.batch_queue import BatchQueue
from yomotsusaka.boundary import ProcessRequest, SpanSpec
from yomotsusaka.facade import LocalFacade
from yomotsusaka.schemas import DocumentManifest
from yomotsusaka.span_proposer import DeterministicSpanProposer, SpanProposer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public summary schema
# ---------------------------------------------------------------------------


class BatchSummary(BaseModel, frozen=True):
    """Public, redacted-only summary of a batch run.

    Carries opaque counts and the caller-supplied document references
    (paths) only. No raw input text, no manifest body, no vault path beyond
    what the caller already passed in on the command line.
    """

    model_config = ConfigDict(extra="forbid")

    batch_id: str
    submitted_count: int
    committed_count: int
    failed_count: int
    failed_doc_refs: list[str] = Field(default_factory=list)
    started_at: datetime
    finished_at: datetime


# ---------------------------------------------------------------------------
# doc_id derivation
# ---------------------------------------------------------------------------

# Mirror the kernel ``_DOC_ID_PATTERN`` charset (``[A-Za-z0-9._-]{1,128}``)
# so a doc_id derived here is always accepted by the facade's process call.
# The derivation is intentionally lossy: a path stem is normalised by
# replacing every disallowed character with ``_`` and truncating to 128.
_DOC_ID_ALLOWED = re.compile(r"[^A-Za-z0-9._-]")
_DOC_ID_MAX = 128


def _derive_doc_id(path: Path) -> str:
    """Return a filesystem-safe doc_id derived from *path*'s stem.

    The kernel restricts doc_id to ``[A-Za-z0-9._-]{1,128}`` and rejects the
    path-traversal segments ``.`` / ``..``. This helper normalises the file
    stem into that charset deterministically so callers do not need to
    pre-sanitise filenames.
    """
    stem = path.stem or "doc"
    sanitised = _DOC_ID_ALLOWED.sub("_", stem)[:_DOC_ID_MAX]
    if sanitised in {"", ".", ".."}:
        sanitised = "doc"
    return sanitised


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class BatchRunner:
    """Drain an inbox directory through the :class:`LocalFacade` pipeline.

    Parameters
    ----------
    facade:
        Agent-facing :class:`LocalFacade`. The runner uses
        :meth:`LocalFacade.process` to drive each document through the
        kernel pipeline and reads the resulting :class:`DocumentManifest`
        from the facade's vault to register it with the facade's
        :class:`~yomotsusaka.search_gateway.SearchGateway`. The runner does
        NOT import any private-kernel module directly.
    proposer:
        Optional :class:`~yomotsusaka.span_proposer.SpanProposer` used to
        derive candidate spans for each document. When omitted, a
        default :class:`~yomotsusaka.span_proposer.DeterministicSpanProposer`
        is constructed.

    Behaviour
    ---------
    * One :class:`BatchQueue` entry is submitted per :meth:`run_directory`
      call; state transitions PENDING → RUNNING → DONE (when at least one
      document commits) or FAILED (when zero documents commit and at
      least one was submitted).
    * Per-document failures (any ``ValueError`` from the pipeline, any
      ``yomotsusaka.span_proposer.SpanProposerError`` from a misbehaving
      LLM backend, etc.) are recorded into
      :attr:`BatchSummary.failed_doc_refs` and the runner continues with
      the next document. A single failing document never aborts the
      whole batch.
    """

    def __init__(
        self,
        facade: LocalFacade,
        proposer: SpanProposer | None = None,
    ) -> None:
        self._facade = facade
        self._proposer: SpanProposer = (
            proposer if proposer is not None else DeterministicSpanProposer()
        )
        self._queue = BatchQueue()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _process_one(self, path: Path) -> DocumentManifest | None:
        """Drive *path* through the facade and return the indexed manifest.

        Returns ``None`` (and logs a count-only failure) on any per-document
        failure. The runner-internal log line never echoes raw text; it
        records the file path (caller-public) and the failure category
        only.
        """
        try:
            raw_text = path.read_text(encoding="utf-8")
        except OSError:
            logger.warning("batch_runner: read failed for %s", path)
            return None

        doc_id = _derive_doc_id(path)
        try:
            spans = self._proposer.propose(raw_text)
        except Exception:
            # ``SpanProposerError`` and ``InferenceBackendError`` are
            # implementation-defined; any failure here is treated as a
            # per-document failure. Log the path only, never the text.
            logger.warning(
                "batch_runner: proposer failed for %s (doc_id=%s)", path, doc_id
            )
            return None

        span_specs = [
            SpanSpec(start=s.start, end=s.end, kind=s.kind) for s in spans
        ]
        request = ProcessRequest(
            doc_id=doc_id, raw_text=raw_text, spans=span_specs
        )
        try:
            self._facade.process(request)
        except Exception:
            logger.warning(
                "batch_runner: facade.process failed for %s (doc_id=%s)",
                path,
                doc_id,
            )
            return None

        # Re-load the committed manifest from the public manifests/
        # subdirectory of the facade's vault. The file contains only the
        # redacted projection (per the kernel commit contract); no raw
        # values pass through this read. The manifest is then registered
        # with the facade's gateway so subsequent ``facade.search`` calls
        # can find it.
        manifest_path = (
            self._facade.vault_root / "manifests" / f"{doc_id}.json"
        )
        try:
            manifest = DocumentManifest.model_validate_json(
                manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:  # pragma: no cover - defensive
            logger.warning(
                "batch_runner: manifest re-load failed for doc_id=%s (%s)",
                doc_id,
                type(exc).__name__,
            )
            return None

        try:
            self._facade.gateway.index(manifest)
        except Exception:  # pragma: no cover - defensive
            logger.warning(
                "batch_runner: gateway.index failed for doc_id=%s", doc_id
            )
            return None

        return manifest

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run_directory(self, inbox: Path) -> BatchSummary:
        """Walk *inbox*, process every regular file, return a summary.

        Recursive glob is used so a caller may organise documents under
        subdirectories. Non-regular entries (directories, symlinks to
        directories, special files) are skipped. The result order is
        deterministic (sorted by relative path) so failed_doc_refs is
        stable across reruns of the same corpus.
        """
        if not isinstance(inbox, Path):
            raise TypeError("inbox must be a pathlib.Path")
        if not inbox.exists():
            raise FileNotFoundError(f"inbox directory does not exist: {inbox}")
        if not inbox.is_dir():
            raise NotADirectoryError(f"inbox is not a directory: {inbox}")

        doc_refs = sorted(
            str(p) for p in inbox.rglob("*") if p.is_file()
        )

        batch = self._queue.submit(doc_refs)
        started_at = datetime.now(timezone.utc)
        if doc_refs:
            self._queue.start(batch.batch_id)

        manifests: list[DocumentManifest] = []
        failed_refs: list[str] = []
        for doc_ref in doc_refs:
            manifest = self._process_one(Path(doc_ref))
            if manifest is None:
                failed_refs.append(doc_ref)
            else:
                manifests.append(manifest)

        finished_at = datetime.now(timezone.utc)

        # Queue terminal transitions. The BatchQueue API operates per-batch
        # (not per-doc), so we map the runner's outcome onto a single
        # terminal call:
        #   - empty batch:           PENDING (no transition; submit() only)
        #   - any commit succeeded:  RUNNING → DONE (with surviving manifests)
        #   - every doc failed:      RUNNING → FAILED
        if doc_refs:
            if manifests or not failed_refs:
                self._queue.complete(batch.batch_id, manifests)
            else:
                self._queue.fail(
                    batch.batch_id,
                    f"all {len(failed_refs)} document(s) failed",
                )

        summary = BatchSummary(
            batch_id=batch.batch_id,
            submitted_count=len(doc_refs),
            committed_count=len(manifests),
            failed_count=len(failed_refs),
            failed_doc_refs=failed_refs,
            started_at=started_at,
            finished_at=finished_at,
        )
        logger.info(
            "batch_runner: batch %s submitted=%d committed=%d failed=%d",
            summary.batch_id,
            summary.submitted_count,
            summary.committed_count,
            summary.failed_count,
        )
        return summary


__all__ = ["BatchRunner", "BatchSummary"]
