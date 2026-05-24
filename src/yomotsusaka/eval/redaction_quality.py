"""Redaction quality / false-negative evaluation harness (issue #94).

Pure-Python harness — **not** test-framework dependent at runtime — that
exercises a :class:`yomotsusaka.span_proposer.SpanProposer` against a
curated fixture corpus and surfaces:

* **false negatives** (an expected span the proposer did not recover),
* **false positives** (a proposed span not present in the expected
  ground truth),
* **placeholder-consistency** violations (a single source token mapping
  to more than one redacted key within a tenant scope).

The companion tests in :mod:`tests.eval.test_redaction_quality` invoke
this harness on the in-repo corpus at
``tests/fixtures/redaction_corpus`` and assert thresholds (zero misses
on the canonical corpus, zero placeholder inconsistencies). A
self-test exercises the harness on a synthetic "known miss" corpus to
verify the harness reports the failure rather than silently swallowing
it.

Agent-runnable CLI (issue #105 finding F1)
==========================================

The module also exposes a thin ``python -m`` entry point that wraps
:func:`evaluate_corpus` against the in-repo fixture corpus (or a caller-
supplied directory) and emits a public-safe one-line pass/fail summary
plus a per-document phase block. Exit codes follow the same convention
as the other ``yomotsusaka.cli.*`` shims: ``0`` on a fully clean run,
non-zero when any finding (false negative, false positive, or
placeholder inconsistency) is present, and a distinct code for input
errors so callers can branch on the failure shape.

Only public-safe values appear on stdout: counts, rates, document
stems, :class:`yomotsusaka.schemas.EntityKind` enum tokens, and
``(start, end, kind)`` triples. The raw text of the corpus is never
echoed by this CLI.

Privacy discipline (binding, see ``docs/architecture.md`` §Capability
and exposure model and issue #94 §Public-safe discipline)
=========================================================

* **No raw private value from the corpus appears in any return value,
  log record, exception message, or report serialisation produced by
  this module.** Reports key on (a) document identifiers (the corpus
  file stem, caller-supplied), (b) integer counts and rates, (c)
  :class:`yomotsusaka.schemas.EntityKind` enum members, and (d)
  redactor placeholders (``<KIND_hex>``) which are public-safe by
  construction.
* When the harness emits a span-level diagnostic (false negative or
  false positive), it emits the structural triple ``(start, end,
  kind)`` — never the source text or any substring of it. Operators
  that need to inspect the raw text per offset must do so vault-side;
  the report is agent-facing.
* The fixture corpus is **synthetic** but is treated as if its contents
  were private. The harness does not echo file contents through
  ``logger.debug`` even at finer log levels.

Module exports
--------------

* :class:`SpanCoord` — public-safe ``(start, end, kind)`` triple.
* :class:`DocumentExpectation` — the parsed contents of an
  ``<name>.expected.json`` file (no raw text).
* :class:`DocumentReport` — per-document evaluation result.
* :class:`PlaceholderInconsistency` — one tenant/source-token conflict
  surfaced by the cross-document consistency check (records keys only;
  the source token's value is never serialised).
* :class:`RedactionQualityReport` — corpus-wide result with aggregate
  rates and the list of per-document reports.
* :func:`load_corpus` — load and parse a corpus directory.
* :func:`evaluate_corpus` — run a proposer over a parsed corpus and
  return a :class:`RedactionQualityReport`.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from pydantic import BaseModel, ConfigDict, Field

from yomotsusaka.redactor import Span, redact
from yomotsusaka.schemas import EntityKind, PrivateDictEntry
from yomotsusaka.span_proposer import DeterministicSpanProposer, SpanProposer

logger = logging.getLogger(__name__)


__all__ = [
    "SpanCoord",
    "DocumentExpectation",
    "DocumentReport",
    "PlaceholderInconsistency",
    "RedactionQualityReport",
    "load_corpus",
    "evaluate_corpus",
    "CorpusLoadError",
    "main",
]


# ---------------------------------------------------------------------------
# CLI exit codes
# ---------------------------------------------------------------------------
#
# Stable codes the agent-runnable CLI returns. Other shims in
# ``yomotsusaka.cli`` use the same 0 / 1 / 2 split for clean run /
# findings present / input error so the wrapping orchestration scripts
# can pattern-match consistently.
_CLI_EXIT_CLEAN = 0
_CLI_EXIT_FINDINGS = 1
_CLI_EXIT_INPUT_ERROR = 2


# Default corpus directory — the in-repo synthetic fixture set used by
# both ``tests/eval/test_redaction_quality.py`` and (by default) the
# agent-runnable CLI below. Resolved off the package source tree at
# import time so the CLI works regardless of the caller's cwd.
_DEFAULT_CORPUS = (
    Path(__file__).resolve().parents[3]
    / "tests"
    / "fixtures"
    / "redaction_corpus"
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CorpusLoadError(Exception):
    """Raised when a corpus directory or fixture file is malformed.

    The error message references the offending file path (which is a
    test-fixture path — public-safe by construction). It MUST NOT echo
    raw document contents into the message, since the corpus is treated
    as if private.
    """


# ---------------------------------------------------------------------------
# Public-safe value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpanCoord:
    """A ``(start, end, kind)`` triple — the public-safe shape of a span.

    Equivalent to :class:`yomotsusaka.redactor.Span` but stripped to its
    structural members so two :class:`SpanCoord` instances compare equal
    by value (not by identity). Used throughout the harness so set
    differences (false negative / false positive computation) work
    without depending on private dictionary state.
    """

    start: int
    end: int
    kind: EntityKind

    @classmethod
    def from_span(cls, span: Span) -> "SpanCoord":
        return cls(start=span.start, end=span.end, kind=span.kind)


class DocumentExpectation(BaseModel):
    """Parsed ``<name>.expected.json`` contents.

    Frozen + ``extra="forbid"`` so a typo on a fixture key fails fast
    rather than silently weakening the harness. Mirrors the schema
    documented in ``tests/fixtures/redaction_corpus/README.md``.

    Note: the raw document text is **not** stored on this object — the
    harness reads it transiently from disk inside
    :func:`evaluate_corpus` so the public-safe report cannot accidentally
    carry it through pydantic serialisation.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    doc_name: str = Field(
        description="File stem of the corpus document (e.g. "
        "``canonical_employee`` for ``canonical_employee.txt``)."
    )
    tenant_id: str = Field(
        description="Placeholder-consistency grouping. Documents sharing"
        " a ``tenant_id`` must satisfy: same source token → same key."
    )
    spans: tuple[SpanCoord, ...] = Field(
        description="Expected span triples the proposer must recover."
    )
    expected_keys: frozenset[str] = Field(
        description="Expected redactor-produced placeholder set. The"
        " redactor de-duplicates by source token, so this set may be"
        " smaller than ``len(spans)`` when a token repeats within one"
        " document."
    )


class DocumentReport(BaseModel):
    """Per-document evaluation result.

    All fields are public-safe by construction.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    doc_name: str
    tenant_id: str
    expected_count: int = Field(ge=0)
    proposed_count: int = Field(ge=0)
    false_negative_count: int = Field(ge=0)
    false_positive_count: int = Field(ge=0)
    false_negative_spans: tuple[SpanCoord, ...] = Field(
        description="Expected spans the proposer did not recover."
        " Structural triples only — never the raw text."
    )
    false_positive_spans: tuple[SpanCoord, ...] = Field(
        description="Proposed spans not present in the expected ground"
        " truth. Structural triples only — never the raw text."
    )
    expected_keys: frozenset[str]
    actual_keys: frozenset[str]
    missing_keys: frozenset[str] = Field(
        description="Expected redactor placeholders not present in"
        " ``actual_keys``."
    )
    extra_keys: frozenset[str] = Field(
        description="Redactor placeholders produced by the proposer but"
        " absent from the expected set."
    )
    key_match: bool = Field(
        description="``True`` iff ``actual_keys == expected_keys``."
    )


class PlaceholderInconsistency(BaseModel):
    """One placeholder-consistency violation.

    Records the tenant and the conflicting key set — **not** the source
    token that produced them. The raw token is private; the keys
    themselves are public-safe (they are deterministic hash projections
    by design, see :func:`yomotsusaka.redactor._make_key`).

    Operators that need to identify the offending token can use the
    list of ``observed_docs`` to locate the original document in the
    vault.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str
    kind: EntityKind = Field(
        description="The entity kind under which the conflict was"
        " observed. Helps operators narrow the search vault-side."
    )
    conflicting_keys: frozenset[str] = Field(
        description="The set of distinct keys the same source token"
        " mapped to. Always has cardinality ≥ 2."
    )
    observed_docs: tuple[str, ...] = Field(
        description="Document stems where the conflict was observed,"
        " in iteration order."
    )


class RedactionQualityReport(BaseModel):
    """Corpus-wide evaluation result.

    Aggregates per-document results plus cross-document
    placeholder-consistency findings.

    The three named rates use the binding definitions from issue #94:

    * ``false_negative_rate`` =
      ``expected_spans_missed / expected_spans_total`` (``0.0`` when
      the corpus has zero expected spans).
    * ``false_positive_rate`` =
      ``proposed_spans_not_in_expected / proposed_spans_total``
      (``0.0`` when the proposer produced zero spans).
    * ``placeholder_consistency`` = fraction of distinct source-token
      observations within a tenant that map to a single placeholder.
      ``1.0`` means no inconsistencies; values < 1.0 mean at least one
      source token mapped to ≥ 2 distinct keys under one tenant.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    documents: tuple[DocumentReport, ...]
    expected_spans_total: int = Field(ge=0)
    proposed_spans_total: int = Field(ge=0)
    false_negative_total: int = Field(ge=0)
    false_positive_total: int = Field(ge=0)
    false_negative_rate: float = Field(ge=0.0, le=1.0)
    false_positive_rate: float = Field(ge=0.0, le=1.0)
    placeholder_consistency: float = Field(ge=0.0, le=1.0)
    placeholder_inconsistencies: tuple[PlaceholderInconsistency, ...] = Field(
        default=(),
        description="Empty tuple when ``placeholder_consistency`` is"
        " 1.0. Otherwise one entry per (tenant, source-token) conflict.",
    )

    @property
    def has_failures(self) -> bool:
        """Convenience predicate for callers that want a single boolean.

        ``True`` when any of the three metric classes is non-clean:
        any false negative, any false positive, or any placeholder
        inconsistency. Tests typically assert ``not has_failures``.
        """
        return (
            self.false_negative_total > 0
            or self.false_positive_total > 0
            or bool(self.placeholder_inconsistencies)
        )


# ---------------------------------------------------------------------------
# Corpus loading
# ---------------------------------------------------------------------------


# In-memory representation of one loaded document. Held privately by
# :func:`load_corpus` / :func:`evaluate_corpus`; the raw text MUST NOT
# leak into the public-facing report or any return value crossing the
# harness boundary. We use a frozen dataclass (rather than a Pydantic
# model) so accidental serialisation via ``model_dump`` is impossible.
@dataclass(frozen=True, slots=True)
class _LoadedDocument:
    name: str
    raw_text: str
    expectation: DocumentExpectation


def _load_document(txt_path: Path) -> _LoadedDocument:
    """Load one ``.txt`` + ``.expected.json`` pair.

    Raises :class:`CorpusLoadError` (never echoing raw text) on any
    parse / shape failure. The fixture *path* is a public-safe
    identifier; the contents are not.
    """
    name = txt_path.stem
    json_path = txt_path.with_suffix(".expected.json")
    if not json_path.exists():
        raise CorpusLoadError(
            f"missing expected metadata for fixture {name!r}:"
            f" expected sibling {json_path.name!r} next to {txt_path.name!r}"
        )
    try:
        raw_text = txt_path.read_text(encoding="utf-8")
    except OSError as exc:
        # File I/O error — surface the *path* (public-safe), not the
        # exception ``args`` (which may quote contents on some platforms).
        raise CorpusLoadError(
            f"could not read fixture body {txt_path.name!r}: {type(exc).__name__}"
        ) from None
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CorpusLoadError(
            f"could not parse expected metadata {json_path.name!r}:"
            f" {type(exc).__name__}"
        ) from None
    if not isinstance(payload, dict):
        raise CorpusLoadError(
            f"expected metadata {json_path.name!r} must be a JSON object"
        )
    # Re-shape into the pydantic model. Tuple-of-SpanCoord coercion is
    # done explicitly so a malformed entry yields a CorpusLoadError, not
    # a raw pydantic ValidationError (which would chain through and
    # potentially echo the offending value).
    try:
        spans_raw = payload.get("spans", [])
        if not isinstance(spans_raw, list):
            raise CorpusLoadError(
                f"expected metadata {json_path.name!r}: ``spans`` must be a list"
            )
        coords: list[SpanCoord] = []
        for entry in spans_raw:
            if not isinstance(entry, dict):
                raise CorpusLoadError(
                    f"expected metadata {json_path.name!r}: each span must be an object"
                )
            try:
                start = entry["start"]
                end = entry["end"]
                kind_raw = entry["kind"]
            except KeyError:
                raise CorpusLoadError(
                    f"expected metadata {json_path.name!r}: each span must have"
                    f" start/end/kind keys"
                ) from None
            if not isinstance(start, int) or not isinstance(end, int):
                raise CorpusLoadError(
                    f"expected metadata {json_path.name!r}: span start/end must be ints"
                )
            if start < 0 or end <= start:
                raise CorpusLoadError(
                    f"expected metadata {json_path.name!r}: invalid offsets"
                    f" (start={start}, end={end})"
                )
            if end > len(raw_text):
                raise CorpusLoadError(
                    f"expected metadata {json_path.name!r}: span end {end} exceeds"
                    f" document length {len(raw_text)}"
                )
            if not isinstance(kind_raw, str):
                raise CorpusLoadError(
                    f"expected metadata {json_path.name!r}: span ``kind`` must be a string"
                )
            try:
                kind = EntityKind(kind_raw)
            except ValueError:
                # Echo only the bad kind token (closed enum vocabulary
                # is public-safe).
                raise CorpusLoadError(
                    f"expected metadata {json_path.name!r}: span ``kind`` {kind_raw!r}"
                    f" is not a member of EntityKind"
                ) from None
            coords.append(SpanCoord(start=start, end=end, kind=kind))
        tenant_id = payload.get("tenant_id", "_local")
        if not isinstance(tenant_id, str) or not tenant_id:
            raise CorpusLoadError(
                f"expected metadata {json_path.name!r}: ``tenant_id`` must be a non-empty string"
            )
        expected_keys_raw = payload.get("expected_keys", [])
        if not isinstance(expected_keys_raw, list) or not all(
            isinstance(k, str) for k in expected_keys_raw
        ):
            raise CorpusLoadError(
                f"expected metadata {json_path.name!r}: ``expected_keys`` must be a list of strings"
            )
        expectation = DocumentExpectation(
            doc_name=name,
            tenant_id=tenant_id,
            spans=tuple(coords),
            expected_keys=frozenset(expected_keys_raw),
        )
    except CorpusLoadError:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        raise CorpusLoadError(
            f"unexpected error parsing {json_path.name!r}: {type(exc).__name__}"
        ) from None
    return _LoadedDocument(name=name, raw_text=raw_text, expectation=expectation)


def load_corpus(corpus_dir: Path) -> tuple[DocumentExpectation, ...]:
    """Load every fixture in *corpus_dir* and return the expectations.

    The raw document bodies are intentionally **not** returned — they
    remain on disk until :func:`evaluate_corpus` reads them transiently
    inside the harness. Callers that want to drive a custom evaluation
    loop should call :func:`evaluate_corpus` instead.

    Raises :class:`CorpusLoadError` for any malformed fixture pair.
    """
    if not corpus_dir.is_dir():
        raise CorpusLoadError(
            f"corpus directory {corpus_dir!s} does not exist or is not a directory"
        )
    expectations: list[DocumentExpectation] = []
    for txt_path in sorted(corpus_dir.glob("*.txt")):
        loaded = _load_document(txt_path)
        expectations.append(loaded.expectation)
    if not expectations:
        raise CorpusLoadError(
            f"corpus directory {corpus_dir!s} contains no ``*.txt`` fixtures"
        )
    return tuple(expectations)


def _load_all(corpus_dir: Path) -> tuple[_LoadedDocument, ...]:
    """Internal counterpart of :func:`load_corpus` that retains the raw
    text. Restricted to module-internal use (note the underscore prefix)
    so callers cannot accidentally route raw text into agent-facing
    surfaces.
    """
    if not corpus_dir.is_dir():
        raise CorpusLoadError(
            f"corpus directory {corpus_dir!s} does not exist or is not a directory"
        )
    loaded: list[_LoadedDocument] = []
    for txt_path in sorted(corpus_dir.glob("*.txt")):
        loaded.append(_load_document(txt_path))
    if not loaded:
        raise CorpusLoadError(
            f"corpus directory {corpus_dir!s} contains no ``*.txt`` fixtures"
        )
    return tuple(loaded)


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _evaluate_one(
    document: _LoadedDocument,
    proposer: SpanProposer,
) -> tuple[DocumentReport, Sequence[PrivateDictEntry]]:
    """Evaluate one document. Returns the public-safe report and the
    private-dictionary entries for cross-document consistency checking.

    The returned private entries are intentionally **not** part of the
    public report — they are consumed only by
    :func:`_check_placeholder_consistency` inside this module and never
    cross the harness boundary.
    """
    expected_set: set[SpanCoord] = set(document.expectation.spans)
    proposed_spans = proposer.propose(document.raw_text)
    proposed_set: set[SpanCoord] = {SpanCoord.from_span(s) for s in proposed_spans}

    false_negatives = expected_set - proposed_set
    false_positives = proposed_set - expected_set

    _, _, private_entries = redact(document.raw_text, proposed_spans)
    actual_keys: frozenset[str] = frozenset(e.key for e in private_entries)
    expected_keys = document.expectation.expected_keys
    missing_keys = expected_keys - actual_keys
    extra_keys = actual_keys - expected_keys

    report = DocumentReport(
        doc_name=document.name,
        tenant_id=document.expectation.tenant_id,
        expected_count=len(expected_set),
        proposed_count=len(proposed_set),
        false_negative_count=len(false_negatives),
        false_positive_count=len(false_positives),
        false_negative_spans=tuple(
            sorted(false_negatives, key=lambda c: (c.start, c.end, c.kind.value))
        ),
        false_positive_spans=tuple(
            sorted(false_positives, key=lambda c: (c.start, c.end, c.kind.value))
        ),
        expected_keys=expected_keys,
        actual_keys=actual_keys,
        missing_keys=missing_keys,
        extra_keys=extra_keys,
        key_match=(actual_keys == expected_keys),
    )
    return report, private_entries


def _check_placeholder_consistency(
    per_doc_entries: Iterable[tuple[str, str, Sequence[PrivateDictEntry]]],
) -> tuple[float, tuple[PlaceholderInconsistency, ...]]:
    """Compute the placeholder-consistency metric and the list of
    violations.

    *per_doc_entries* is an iterable of ``(doc_name, tenant_id,
    private_entries)``. The tuple structure is intentional — we want to
    keep the doc name correlated with each entry so a violation report
    can list ``observed_docs``.

    Returns ``(rate, violations)`` where ``rate`` is ``1.0`` minus the
    fraction of tenant-scoped distinct source tokens with a non-unique
    key set, and ``violations`` enumerates the conflicts.

    The raw source-token strings are used **only** to key the in-memory
    aggregation map; they are never echoed into the returned
    :class:`PlaceholderInconsistency` objects (which carry the *keys*,
    not the tokens). The aggregation map is dropped at function exit,
    so the raw values never cross the harness boundary.
    """
    # (tenant_id, source_value) -> {key: [(doc_name, kind), ...]}
    # Keying is intentionally **kind-agnostic**: the issue spec defines
    # placeholder consistency as "same source token -> same placeholder
    # across the corpus, per tenant_id". A token that drifts across
    # kinds (e.g. ORG in one doc, CUSTOM in another) emits two distinct
    # placeholders by construction and is exactly the failure mode this
    # metric exists to surface.
    agg: dict[
        tuple[str, str], dict[str, list[tuple[str, EntityKind]]]
    ] = defaultdict(lambda: defaultdict(list))

    for doc_name, tenant_id, entries in per_doc_entries:
        for entry in entries:
            agg_key = (tenant_id, entry.original_value)
            agg[agg_key][entry.key].append((doc_name, entry.kind))

    distinct_tokens = len(agg)
    inconsistent_tokens = 0
    violations: list[PlaceholderInconsistency] = []
    for (tenant_id, _source_value), key_map in agg.items():
        if len(key_map) <= 1:
            continue
        inconsistent_tokens += 1
        # Collect doc-name observations and the first observed kind,
        # preserving first-seen order (Python dict ordering guarantees
        # this since 3.7). The first observed kind is reported as the
        # ``kind`` field; the full set of kinds is implicitly encoded
        # in ``conflicting_keys`` (each key carries its kind prefix).
        seen_docs: list[str] = []
        first_kind: EntityKind | None = None
        for observations in key_map.values():
            for doc_name, observed_kind in observations:
                if doc_name not in seen_docs:
                    seen_docs.append(doc_name)
                if first_kind is None:
                    first_kind = observed_kind
        assert first_kind is not None  # len(key_map) > 1 implies entries exist
        violations.append(
            PlaceholderInconsistency(
                tenant_id=tenant_id,
                kind=first_kind,
                conflicting_keys=frozenset(key_map.keys()),
                observed_docs=tuple(seen_docs),
            )
        )

    if distinct_tokens == 0:
        consistency = 1.0
    else:
        consistency = 1.0 - (inconsistent_tokens / distinct_tokens)
    # Round to a sane precision for stable comparisons in tests; the
    # underlying ratio is a fraction with a small integer denominator
    # in practice, but float arithmetic can introduce trailing-bit
    # noise.
    consistency = round(consistency, 12)
    return consistency, tuple(violations)


def evaluate_corpus(
    corpus_dir: Path,
    *,
    proposer: SpanProposer | None = None,
) -> RedactionQualityReport:
    """Run *proposer* over the corpus at *corpus_dir* and return a
    public-safe :class:`RedactionQualityReport`.

    Parameters
    ----------
    corpus_dir:
        Directory containing one or more ``<name>.txt`` /
        ``<name>.expected.json`` fixture pairs. See
        ``tests/fixtures/redaction_corpus/README.md`` for the layout.
    proposer:
        Span proposer to evaluate. Defaults to
        :class:`yomotsusaka.span_proposer.DeterministicSpanProposer`
        with the default rule set, which is the floor case for the
        kernel.

    Raises
    ------
    CorpusLoadError
        If the corpus directory or any fixture pair is missing or
        malformed.

    The returned report is fully public-safe: every field can be
    serialised through ``model_dump_json`` and surfaced to an
    ordinary-agent caller without leaking corpus contents.
    """
    if proposer is None:
        proposer = DeterministicSpanProposer()

    loaded = _load_all(corpus_dir)

    doc_reports: list[DocumentReport] = []
    consistency_input: list[tuple[str, str, Sequence[PrivateDictEntry]]] = []
    expected_total = 0
    proposed_total = 0
    fn_total = 0
    fp_total = 0
    for document in loaded:
        report, private_entries = _evaluate_one(document, proposer)
        doc_reports.append(report)
        consistency_input.append(
            (document.name, document.expectation.tenant_id, private_entries)
        )
        expected_total += report.expected_count
        proposed_total += report.proposed_count
        fn_total += report.false_negative_count
        fp_total += report.false_positive_count

    if expected_total == 0:
        fn_rate = 0.0
    else:
        fn_rate = round(fn_total / expected_total, 12)
    if proposed_total == 0:
        fp_rate = 0.0
    else:
        fp_rate = round(fp_total / proposed_total, 12)

    consistency, violations = _check_placeholder_consistency(consistency_input)

    # Count-only debug logging — never the raw text or per-doc bodies.
    logger.debug(
        "redaction_quality: evaluated %d documents (expected_total=%d,"
        " proposed_total=%d, fn=%d, fp=%d, inconsistencies=%d)",
        len(doc_reports),
        expected_total,
        proposed_total,
        fn_total,
        fp_total,
        len(violations),
    )

    return RedactionQualityReport(
        documents=tuple(doc_reports),
        expected_spans_total=expected_total,
        proposed_spans_total=proposed_total,
        false_negative_total=fn_total,
        false_positive_total=fp_total,
        false_negative_rate=fn_rate,
        false_positive_rate=fp_rate,
        placeholder_consistency=consistency,
        placeholder_inconsistencies=violations,
    )


# ---------------------------------------------------------------------------
# Agent-runnable CLI (issue #105 finding F1)
# ---------------------------------------------------------------------------
#
# Prior to this entry point, ``python -m yomotsusaka.eval.redaction_quality``
# exited 0 with zero bytes on stdout/stderr because the module had no
# ``__main__`` block — a docile agent following the docs got a silent
# pass with no signal. The CLI below wraps :func:`evaluate_corpus`,
# emits a public-safe summary, and maps findings to a non-zero exit
# code so the failure mode is loud, not silent.
#
# Privacy posture: every value the CLI prints is either a count, a rate,
# a document stem, an :class:`EntityKind` token, or a ``(start, end,
# kind)`` triple. Raw corpus text never appears. The renderer is
# defensive about the structural fields too: triples are formatted as
# ``start:end:kind`` so a future field-shape change cannot accidentally
# stringify a pydantic model that grows a raw-text member.


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m yomotsusaka.eval.redaction_quality",
        description=(
            "Run the redaction-quality harness against a fixture "
            "corpus and emit a public-safe pass/fail summary on "
            "stdout. Exits 0 on a fully clean run, 1 when any "
            "finding (FN / FP / placeholder inconsistency) is "
            "present, and 2 on input error."
        ),
    )
    parser.add_argument(
        "--corpus",
        type=Path,
        default=_DEFAULT_CORPUS,
        help=(
            "Corpus directory containing one or more "
            "``<name>.txt`` / ``<name>.expected.json`` fixture "
            "pairs. Defaults to the in-repo synthetic corpus at "
            "``tests/fixtures/redaction_corpus``."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help=(
            "Emit the public-safe report as a single JSON object "
            "instead of the default human-readable text summary. "
            "Useful when piping into another tool."
        ),
    )
    return parser


def _format_span_coord(coord: SpanCoord) -> str:
    """Render a :class:`SpanCoord` as ``start:end:kind``.

    Public-safe by construction — the triple carries no raw text. The
    explicit formatter (rather than ``repr`` or pydantic dump) is
    defensive against future shape drift.
    """
    return f"{coord.start}:{coord.end}:{coord.kind.value}"


def _render_text_summary(
    report: RedactionQualityReport,
    *,
    corpus_dir: Path,
) -> str:
    """Render the human-readable summary for a
    :class:`RedactionQualityReport`.

    The first line is a stable single-token verdict (``PASS`` or
    ``FAIL``) so callers grepping the output can pattern-match cleanly
    without parsing the table.
    """
    verdict = "FAIL" if report.has_failures else "PASS"
    lines: list[str] = []
    lines.append(f"redaction_quality: {verdict}")
    lines.append(f"corpus: {corpus_dir}")
    lines.append(f"documents: {len(report.documents)}")
    lines.append(
        f"expected_spans_total={report.expected_spans_total}"
        f" proposed_spans_total={report.proposed_spans_total}"
    )
    lines.append(
        f"false_negative_total={report.false_negative_total}"
        f" false_negative_rate={report.false_negative_rate}"
    )
    lines.append(
        f"false_positive_total={report.false_positive_total}"
        f" false_positive_rate={report.false_positive_rate}"
    )
    lines.append(
        f"placeholder_consistency={report.placeholder_consistency}"
        f" placeholder_inconsistencies={len(report.placeholder_inconsistencies)}"
    )
    if report.has_failures:
        lines.append("")
        lines.append("findings:")
        for doc in report.documents:
            if (
                doc.false_negative_count == 0
                and doc.false_positive_count == 0
                and doc.key_match
            ):
                continue
            lines.append(
                f"  doc={doc.doc_name} tenant={doc.tenant_id}"
                f" fn={doc.false_negative_count}"
                f" fp={doc.false_positive_count}"
                f" key_match={doc.key_match}"
            )
            if doc.false_negative_spans:
                joined = ",".join(
                    _format_span_coord(c) for c in doc.false_negative_spans
                )
                lines.append(f"    false_negative_spans: {joined}")
            if doc.false_positive_spans:
                joined = ",".join(
                    _format_span_coord(c) for c in doc.false_positive_spans
                )
                lines.append(f"    false_positive_spans: {joined}")
            if doc.missing_keys:
                joined = ",".join(sorted(doc.missing_keys))
                lines.append(f"    missing_keys: {joined}")
            if doc.extra_keys:
                joined = ",".join(sorted(doc.extra_keys))
                lines.append(f"    extra_keys: {joined}")
        for inc in report.placeholder_inconsistencies:
            joined_keys = ",".join(sorted(inc.conflicting_keys))
            joined_docs = ",".join(inc.observed_docs)
            lines.append(
                f"  placeholder_inconsistency tenant={inc.tenant_id}"
                f" kind={inc.kind.value}"
                f" keys=[{joined_keys}]"
                f" observed_docs=[{joined_docs}]"
            )
    return "\n".join(lines) + "\n"


def _render_json_summary(
    report: RedactionQualityReport,
    *,
    corpus_dir: Path,
) -> str:
    """Render the report as a single-line JSON object.

    Uses ``model_dump`` (pydantic-public-safe by construction) plus a
    couple of envelope fields so the CLI shape is self-describing.
    """
    payload = {
        "verdict": "FAIL" if report.has_failures else "PASS",
        "corpus_dir": str(corpus_dir),
        "report": report.model_dump(mode="json"),
    }
    return json.dumps(payload, sort_keys=True) + "\n"


def main(
    argv: list[str] | None = None,
    *,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
) -> int:
    """Entry point. Returns the process exit code.

    Exit codes
    ----------
    0
        Corpus evaluated cleanly — zero false negatives, zero false
        positives, no placeholder inconsistencies.
    1
        Evaluation completed but at least one finding is present.
        Stdout still carries the full summary; callers can grep it.
    2
        Input error (corpus directory missing or malformed). Stdout is
        empty; stderr carries a single-line diagnostic.
    """
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr

    parser = _build_parser()
    args = parser.parse_args(argv)
    corpus_dir: Path = args.corpus

    try:
        report = evaluate_corpus(corpus_dir)
    except CorpusLoadError as exc:
        # The CorpusLoadError message references the offending fixture
        # path and shape — public-safe by construction (see the class
        # docstring). No raw corpus body can reach this surface.
        err.write(f"error: {exc}\n")
        return _CLI_EXIT_INPUT_ERROR

    if args.json:
        out.write(_render_json_summary(report, corpus_dir=corpus_dir))
    else:
        out.write(_render_text_summary(report, corpus_dir=corpus_dir))
    out.flush()

    return _CLI_EXIT_FINDINGS if report.has_failures else _CLI_EXIT_CLEAN


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
