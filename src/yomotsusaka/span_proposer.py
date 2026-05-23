"""Private-side internal kernel. Ordinary agents should use ``yomotsusaka.boundary`` instead.

Span proposer — produces candidate :class:`~yomotsusaka.redactor.Span` lists
for a given raw text so the kernel pipeline can drive redaction end-to-end
without requiring the caller to pre-detect spans.

Two implementations live here:

* :class:`DeterministicSpanProposer` — pure-Python rule-based detector
  (regex-driven). LLM-free; default for the local-only MVP slice.
* :class:`InferenceBackedSpanProposer` — calls
  :meth:`yomotsusaka.inference_backend.InferenceBackend.generate` with a
  structured-extraction prompt and parses the JSON response into spans.
  Opt-in only.

This module is **private-side only**. It MUST NOT be imported by
:mod:`yomotsusaka.boundary` (see ``docs/architecture.md`` §7.2 / metaplan
Fork 6 of issue #46). Boundary isolation is enforced by
``tests/test_boundary_private_isolation.py``.

Privacy invariants (binding, see child_01 spec):

* Raw text is permitted to enter the proposer (private-boundary
  computation), but MUST NOT be persisted, logged, or echoed into error
  messages. On parse failure, :class:`InferenceBackedSpanProposer` raises
  the generic :class:`SpanProposerError` carrying a fixed message — never
  the backend's response body.
* :class:`yomotsusaka.inference_backend.InferenceBackendError` raised by
  the backend propagates unwrapped. The proposer does NOT silently fall
  back to a deterministic ruleset on backend outage, which would mask the
  failure from operators.
"""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass

from yomotsusaka.inference_backend import InferenceBackend
from yomotsusaka.redactor import Span
from yomotsusaka.schemas import EntityKind

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SpanProposerError(Exception):
    """Raised by :class:`InferenceBackedSpanProposer` when the backend's
    response cannot be parsed into a span list.

    The error message is intentionally fixed; it does NOT include the
    backend response body, which may echo or summarize the raw text that
    was sent as a prompt. Operators inspecting logs see the failure mode
    without leaking private values.
    """


# ---------------------------------------------------------------------------
# ABC
# ---------------------------------------------------------------------------


class SpanProposer(ABC):
    """Abstract candidate-span generator.

    Implementations consume the raw document text and return a list of
    :class:`~yomotsusaka.redactor.Span` objects in any order. The kernel
    redactor sorts and de-overlaps before applying redactions, so
    proposers are not required to do so.
    """

    @abstractmethod
    def propose(self, raw_text: str) -> list[Span]:
        """Return candidate spans for *raw_text*.

        Raises
        ------
        SpanProposerError
            For implementation-defined parse failures.
        yomotsusaka.inference_backend.InferenceBackendError
            Propagated unwrapped from the backend layer (LLM-backed
            implementations only).
        """


# ---------------------------------------------------------------------------
# NoOp proposer — sentinel for "this text has nothing to redact"
# ---------------------------------------------------------------------------


class NoOpSpanProposer(SpanProposer):
    """Proposer that always returns ``[]``.

    Used by callers that need to commit text known to be already-redacted
    (or otherwise free of private spans) without violating
    :func:`yomotsusaka.pipeline.process_document`'s single-source-of-spans
    invariant. Supplying ``NoOpSpanProposer()`` is an explicit declaration
    that the caller has verified the text needs no further redaction; it
    is preferred over passing ``spans=[]`` with ``proposer=None``, which
    is ambiguous between "no spans" and "forgot to specify".
    """

    def propose(self, raw_text: str) -> list[Span]:  # noqa: ARG002
        return []


# ---------------------------------------------------------------------------
# Deterministic rule-based proposer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Rule:
    pattern: re.Pattern[str]
    kind: EntityKind


# Default rules cover the canonical-fixture surface
# ("Alice Tan works at Acme Corp. Patient ID: 12345.") and a small number
# of common shapes. The defaults are intentionally narrow — the kernel
# stays predictable and CPU-only. Real workloads should construct a
# proposer with an explicit rule set tuned to the corpus.
# Rule order matters: the proposer applies rules in order and drops any
# match that overlaps a range already claimed by an earlier rule. ORG is
# listed BEFORE PERSON so an "Acme Corp"-shaped match wins against the
# generic two-TitleCase-tokens PERSON pattern. Callers needing different
# precedence can supply their own ordered ruleset.
_DEFAULT_RULES: tuple[_Rule, ...] = (
    # ORG: TitleCase token(s) followed by a common org suffix.
    _Rule(
        pattern=re.compile(
            r"\b[A-Z][A-Za-z]+(?:\s[A-Z][A-Za-z]+)*\s(?:Corp|Inc|LLC|Ltd|Co|GmbH)\b"
        ),
        kind=EntityKind.ORG,
    ),
    # PERSON: two TitleCase tokens. Avoids greedy multi-word matches.
    _Rule(
        pattern=re.compile(r"\b[A-Z][a-z]+\s[A-Z][a-z]+\b"),
        kind=EntityKind.PERSON,
    ),
    # ID_NUMBER: a bare numeric run, 4-12 digits, not adjacent to
    # alphanumeric/underscore word characters. Tuned for the canonical
    # fixture's "ID: 12345". Note that the lookarounds reject only word
    # characters; whitespace-adjacent digits (e.g. "Suite 12345") still
    # match. Real corpora typically need a tighter, corpus-specific rule
    # set supplied via the constructor; the default is the canonical
    # fixture floor.
    _Rule(
        pattern=re.compile(r"(?<!\w)\d{4,12}(?!\w)"),
        kind=EntityKind.ID_NUMBER,
    ),
)


class DeterministicSpanProposer(SpanProposer):
    """Regex-driven, LLM-free span proposer.

    Parameters
    ----------
    rules:
        Optional list of ``(pattern, kind)`` pairs. When omitted, the
        module's default rule set is used — sufficient to detect the
        canonical fixture but intentionally narrow.

    The proposer is deterministic and idempotent; calling
    :meth:`propose` twice on the same input returns equivalent spans.

    The proposer does NOT log raw text. It logs match counts only
    (consistent with :class:`yomotsusaka.search_gateway.QueryResolver`
    discipline elsewhere in the kernel).
    """

    def __init__(
        self,
        rules: list[tuple[re.Pattern[str], EntityKind]] | None = None,
    ) -> None:
        if rules is None:
            self._rules: tuple[_Rule, ...] = _DEFAULT_RULES
        else:
            self._rules = tuple(_Rule(pattern=p, kind=k) for p, k in rules)

    def propose(self, raw_text: str) -> list[Span]:
        spans: list[Span] = []
        claimed: list[tuple[int, int]] = []
        for rule in self._rules:
            for match in rule.pattern.finditer(raw_text):
                start, end = match.start(), match.end()
                # Drop matches overlapping a range already claimed by an
                # earlier (higher-priority) rule.
                if any(start < cend and end > cstart for cstart, cend in claimed):
                    continue
                spans.append(Span(start=start, end=end, kind=rule.kind))
                claimed.append((start, end))
        # Sort by start so callers see deterministic output. The redactor
        # also sorts, so this is a defence-in-depth nicety, not a
        # correctness requirement.
        spans.sort(key=lambda s: s.start)
        # Count-only logging — never the raw text or match values.
        logger.debug(
            "DeterministicSpanProposer produced %d candidate span(s)", len(spans)
        )
        return spans


# ---------------------------------------------------------------------------
# Inference-backed proposer
# ---------------------------------------------------------------------------


_DEFAULT_SCHEMA_PROMPT_TEMPLATE = (
    "You are a privacy-aware named-entity extractor. "
    "Identify private entities in the document below and return ONLY a "
    "JSON array. Each element MUST be an object with keys "
    '"start" (int char offset, inclusive), "end" (int char offset, '
    'exclusive), and "kind" (one of '
    f"{', '.join(repr(k.value) for k in EntityKind)}). "
    "Return [] if no entities are present. Do NOT include explanatory "
    "text outside the JSON array.\n\n"
    "Document:\n{document}"
)


def _build_default_schema_prompt(raw_text: str) -> str:
    return _DEFAULT_SCHEMA_PROMPT_TEMPLATE.format(document=raw_text)


class InferenceBackedSpanProposer(SpanProposer):
    """LLM-backed span proposer.

    Parameters
    ----------
    backend:
        An :class:`~yomotsusaka.inference_backend.InferenceBackend`
        instance. ``DummyBackend`` is acceptable for tests but does not
        produce real extractions.
    schema_prompt_template:
        Optional override for the structured-extraction prompt. The
        template MUST contain a literal ``{document}`` placeholder where
        the raw text is interpolated. When omitted, a built-in template
        is used.

    Privacy notes (binding):

    * The prompt sent to the backend includes the raw text by design —
      this is a private-boundary computation. The prompt MUST NOT be
      persisted by this module (no logging, no caching).
    * The backend response is expected to be a JSON array of
      ``{"start", "end", "kind"}`` objects. On any parse failure
      (malformed JSON, wrong shape, unknown entity kind, etc.) the
      proposer raises :class:`SpanProposerError` with a fixed message —
      it does NOT echo the response body, which could contain the raw
      values it was asked to extract.
    * :class:`~yomotsusaka.inference_backend.InferenceBackendError` from
      the backend propagates unwrapped. The proposer NEVER silently
      falls back to a deterministic ruleset; backend outages must be
      visible to operators.
    """

    def __init__(
        self,
        backend: InferenceBackend,
        schema_prompt_template: str | None = None,
        *,
        max_tokens: int = 1024,
    ) -> None:
        if schema_prompt_template is not None and "{document}" not in schema_prompt_template:
            raise ValueError(
                "schema_prompt_template must contain a literal '{document}' placeholder"
            )
        self._backend = backend
        self._schema_prompt_template = schema_prompt_template
        self._max_tokens = max_tokens

    def propose(self, raw_text: str) -> list[Span]:
        if self._schema_prompt_template is None:
            prompt = _build_default_schema_prompt(raw_text)
        else:
            prompt = self._schema_prompt_template.format(document=raw_text)
        # NOTE: prompt contains raw text and MUST NOT be logged or cached.
        # InferenceBackendError (or subclasses) intentionally propagates.
        response = self._backend.generate(prompt, max_tokens=self._max_tokens)
        spans = _parse_backend_response(response)
        # Out-of-range offset check (defence-in-depth). The redactor itself
        # silently drops spans with ``end > len(text)``, but a backend that
        # returns a span past the document end is a parse failure we want
        # to surface — committing a manifest with phantom entities would
        # leave a misleading audit trail.
        text_len = len(raw_text)
        for span in spans:
            if span.end > text_len:
                # Fixed message — never echo span values, which derive
                # from a response that may contain raw text fragments.
                raise SpanProposerError("backend returned unparseable response") from None
        logger.debug(
            "InferenceBackedSpanProposer parsed %d candidate span(s)", len(spans)
        )
        return spans


def _parse_backend_response(response: str) -> list[Span]:
    """Parse a backend response into :class:`Span` objects.

    On any parse failure raise :class:`SpanProposerError` with a fixed
    message. NEVER include *response* in the error — it may echo the
    raw text the model was asked to extract.

    Exception chaining is suppressed (``from None``) on every parse
    branch: the underlying exception's ``args`` may contain attacker-
    controlled response fragments (notably :class:`ValueError` from
    :class:`json.loads` and :class:`EntityKind`-from-str coercion), and a
    chained traceback could surface those fragments in operator logs.
    The fixed top-level message is the only privacy-bearing surface.
    """
    try:
        payload = json.loads(response)
    except (TypeError, ValueError):
        raise SpanProposerError("backend returned unparseable response") from None

    if not isinstance(payload, list):
        raise SpanProposerError("backend returned unparseable response") from None

    spans: list[Span] = []
    for element in payload:
        if not isinstance(element, dict):
            raise SpanProposerError("backend returned unparseable response") from None
        try:
            start = element["start"]
            end = element["end"]
            kind_raw = element["kind"]
        except KeyError:
            raise SpanProposerError("backend returned unparseable response") from None
        if not isinstance(start, int) or not isinstance(end, int):
            raise SpanProposerError("backend returned unparseable response") from None
        if not isinstance(kind_raw, str):
            raise SpanProposerError("backend returned unparseable response") from None
        try:
            kind = EntityKind(kind_raw)
        except ValueError:
            raise SpanProposerError("backend returned unparseable response") from None
        # Offset semantics. ``redactor.redact`` silently drops overlapping
        # or out-of-range spans, but accepts negative offsets and ``end
        # <= start`` (Python slice semantics may treat them as no-ops or,
        # for negative values, wrap around). A backend returning such
        # offsets is a parse failure — surface it here so a misbehaving
        # model cannot smuggle a phantom span into the manifest. (The
        # ``end <= len(raw_text)`` check is deferred to ``propose`` where
        # the raw text is in scope.)
        if start < 0 or end <= start:
            raise SpanProposerError("backend returned unparseable response") from None
        spans.append(Span(start=start, end=end, kind=kind))
    return spans


__all__ = [
    "SpanProposer",
    "DeterministicSpanProposer",
    "InferenceBackedSpanProposer",
    "NoOpSpanProposer",
    "SpanProposerError",
]
