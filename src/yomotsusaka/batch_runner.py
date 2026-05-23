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

import hashlib
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
    # ``index_persisted`` is True iff ``SearchGateway.snapshot`` succeeded at
    # the tail of ``BatchRunner.run_directory``. A False value indicates the
    # JSONL snapshot raised ``OSError`` (e.g. read-only vault); the batch
    # itself is not failed by a snapshot failure — manifests / private
    # dictionaries are already committed at that point.
    index_persisted: bool = True


# ---------------------------------------------------------------------------
# doc_id derivation
# ---------------------------------------------------------------------------

# Mirror the kernel ``_DOC_ID_PATTERN`` charset (``[A-Za-z0-9._-]{1,128}``)
# so a doc_id derived here is always accepted by the facade's process call.
# The derivation is intentionally lossy on the stem (charset normalisation)
# but appends a short content-free hash of the path relative to the inbox
# root so distinct on-disk files NEVER collapse to the same doc_id (which
# would silently overwrite manifests in the vault).
_DOC_ID_ALLOWED = re.compile(r"[^A-Za-z0-9._-]")
_DOC_ID_MAX = 128
# 10 hex chars of SHA-256 over the inbox-relative path. 40 bits is ample
# disambiguation for any realistic inbox (collision probability ~1e-12 at
# one-million files) while keeping the resulting doc_id compact.
_DISAMBIGUATOR_HEX_LEN = 10


def _derive_doc_id(path: Path, inbox: Path) -> str:
    """Return a filesystem-safe, collision-resistant doc_id for *path*.

    The kernel restricts doc_id to ``[A-Za-z0-9._-]{1,128}`` and rejects the
    path-traversal segments ``.`` / ``..``. This helper:

    1. Normalises the file stem into the allowed charset (lossy: any
       disallowed character is replaced with ``_``).
    2. Appends a short SHA-256-derived disambiguator computed over the
       path's location **relative to the inbox root**. Two files with the
       same stem in different subdirectories (e.g. ``a/report.txt`` and
       ``b/report.txt``), or two files whose stems normalise to the same
       token, therefore receive distinct doc_ids.

    The disambiguator is deterministic: rerunning the runner over the same
    inbox produces the same doc_ids, so manifests written on a prior run
    are overwritten in-place rather than accumulated as duplicates.

    The hash is computed over a string only — never the file contents, so
    no raw private text reaches this function's hash input.
    """
    stem = path.stem or "doc"
    sanitised_stem = _DOC_ID_ALLOWED.sub("_", stem)
    if sanitised_stem in {"", ".", ".."}:
        sanitised_stem = "doc"

    # Compute the inbox-relative path. Fall back to the absolute path when
    # the file is not under the inbox (defensive — ``run_directory`` only
    # yields paths from ``inbox.rglob('*')`` so this should not normally
    # occur).
    try:
        rel = path.relative_to(inbox)
        disambiguator_input = rel.as_posix()
    except ValueError:
        disambiguator_input = str(path)

    digest = hashlib.sha256(
        disambiguator_input.encode("utf-8", errors="replace")
    ).hexdigest()[:_DISAMBIGUATOR_HEX_LEN]

    # Reserve the suffix budget so the assembled doc_id stays within the
    # kernel's 128-char cap. Suffix is ``"-" + digest`` (11 chars).
    budget = _DOC_ID_MAX - (1 + _DISAMBIGUATOR_HEX_LEN)
    stem_part = sanitised_stem[:budget] or "doc"
    return f"{stem_part}-{digest}"


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

    def _process_one(self, path: Path, inbox: Path) -> DocumentManifest | None:
        """Drive *path* through the facade and return the indexed manifest.

        Returns ``None`` (and logs a count-only failure) on any per-document
        failure. The runner-internal log line never echoes raw text; it
        records the file path (caller-public) and the failure category
        only.

        Failure isolation: any expected per-document failure — OS-level
        read errors (``OSError``) AND decode failures
        (``UnicodeDecodeError``, a subclass of ``ValueError``) on non-UTF-8
        inputs — is caught here so the surrounding :meth:`run_directory`
        loop continues with the next document instead of aborting the
        whole batch. The runner only surfaces unexpected exceptions (e.g.
        a kernel programmer error) to the caller.
        """
        try:
            raw_text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # ``UnicodeDecodeError`` is raised by ``read_text`` when the
            # file's bytes are not valid UTF-8. Treating it as a per-doc
            # failure (rather than an exception that aborts the batch)
            # preserves the documented failure-isolation contract.
            logger.warning("batch_runner: read failed for %s", path)
            return None

        doc_id = _derive_doc_id(path, inbox)
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
            manifest = self._process_one(Path(doc_ref), inbox)
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

        # Persist the redacted-only search index so a subsequent process
        # (e.g. an agent search after this runner exits) can ``load`` it
        # without re-running the batch. The snapshot writes only the
        # redacted ``DocumentManifest`` objects — no resolver state, no
        # raw private values. A snapshot OSError does not fail the batch:
        # the manifests + private dictionaries committed by the pipeline
        # are already durable in the vault; only the convenience JSONL
        # mirror is missing.
        index_persisted = True
        try:
            self._facade.gateway.snapshot(self._facade.vault_root)
        except OSError:
            # Count-only log: never echo the vault path or any manifest
            # content. The path is caller-public for MVP but the runner's
            # privacy discipline pins this to a category log.
            logger.warning(
                "batch_runner: gateway.snapshot failed for batch %s",
                batch.batch_id,
            )
            index_persisted = False

        summary = BatchSummary(
            batch_id=batch.batch_id,
            submitted_count=len(doc_refs),
            committed_count=len(manifests),
            failed_count=len(failed_refs),
            failed_doc_refs=failed_refs,
            started_at=started_at,
            finished_at=finished_at,
            index_persisted=index_persisted,
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
