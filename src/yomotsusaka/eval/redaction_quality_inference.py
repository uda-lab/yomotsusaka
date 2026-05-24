"""Inference-backed comparison scaffold for redaction quality (issue #94).

Companion to :mod:`yomotsusaka.eval.redaction_quality`. Exercises
:class:`yomotsusaka.span_proposer.InferenceBackedSpanProposer` against
the same fixture corpus and returns a public-safe
:class:`yomotsusaka.eval.redaction_quality.RedactionQualityReport` so
the two proposer families can be compared side-by-side.

**Default backend is mock.** A live RunPod / vLLM evaluation requires
the explicit ``--live`` opt-in and a caller-supplied
:class:`~yomotsusaka.inference_backend.InferenceBackend` instance. Live
mode is **owner-gated** and **never** runs in CI; the
:func:`evaluate_corpus_inference` helper raises
:class:`InferenceEvaluationError` if invoked with ``live=True`` but no
backend is provided.

Privacy discipline mirrors :mod:`yomotsusaka.eval.redaction_quality`:
no raw corpus value crosses the report boundary regardless of which
backend is used.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from yomotsusaka.eval.redaction_quality import (
    RedactionQualityReport,
    evaluate_corpus,
    load_corpus,
)
from yomotsusaka.inference_backend import InferenceBackend
from yomotsusaka.span_proposer import InferenceBackedSpanProposer

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass

logger = logging.getLogger(__name__)


__all__ = [
    "InferenceEvaluationError",
    "MockOracleBackend",
    "evaluate_corpus_inference",
]


class InferenceEvaluationError(Exception):
    """Raised when the inference comparison scaffold is misconfigured.

    Currently used for the ``--live`` opt-in path when no backend is
    supplied. The message is fixed and carries no raw corpus contents.
    """


class MockOracleBackend(InferenceBackend):
    """Deterministic test backend that returns the corpus's
    ground-truth spans as if they had been emitted by a perfect LLM.

    Built by reading the corpus expectations at construction time and
    answering each ``generate`` call with the JSON-serialised spans of
    whichever document text the prompt contains.

    Prompt-to-document routing (unambiguous-match guarantee)
    --------------------------------------------------------

    The default :class:`yomotsusaka.span_proposer.InferenceBackedSpanProposer`
    template ends with ``"Document:\\n{document}"`` so the document body
    occupies the prompt's trailing slice. Routing keys on that
    invariant in two layers:

    1. **Exact suffix match.** If exactly one registered body is a
       suffix of *prompt*, the corresponding payload is returned. This
       is the common path under the default template and is the only
       routing path that is guaranteed to be unambiguous regardless of
       whether one fixture text happens to appear inside another.
    2. **Longest-suffix tie-break.** When more than one registered
       body suffix-matches the same prompt (only possible when one
       body is itself a suffix of another), the longest body wins.
       This is deterministic and corresponds to the strongest
       observable evidence in the prompt.

    If neither layer locates a body the call raises
    :class:`InferenceEvaluationError` — silently returning ``"[]"`` was
    rejected during review (codex on PR #102) because it can mask
    routing bugs as innocuous false negatives in the harness metrics.

    This backend is for **harness self-tests only**. It is not a
    substitute for a real LLM evaluation; the live-mode opt-in below
    refuses to accept it as a "real" backend.
    """

    def __init__(self, corpus_dir: Path) -> None:
        # Materialise per-document expected span payloads keyed by the
        # full document body. We read the body via the same path as
        # :func:`load_corpus` so the mock answers exactly what the
        # ground truth says.
        self._payloads: dict[str, str] = {}
        for txt_path in sorted(corpus_dir.glob("*.txt")):
            json_path = txt_path.with_suffix(".expected.json")
            if not json_path.exists():  # pragma: no cover - defensive
                continue
            body = txt_path.read_text(encoding="utf-8")
            if not body:  # pragma: no cover - defensive
                continue
            meta = json.loads(json_path.read_text(encoding="utf-8"))
            spans = meta.get("spans", []) or []
            self._payloads[body] = json.dumps(
                [
                    {"start": s["start"], "end": s["end"], "kind": s["kind"]}
                    for s in spans
                ]
            )

    def generate(self, prompt: str, *, max_tokens: int = 512) -> str:  # noqa: ARG002
        # Privacy note: we never log the prompt body — it carries the
        # raw corpus text by construction.
        #
        # Routing uses suffix matching (not arbitrary substring
        # containment) to avoid the ambiguity codex flagged on PR
        # #102: if one fixture body is a substring of another (or of
        # any other prompt scaffolding), substring routing could
        # silently return spans for the wrong document. With
        # ``endswith``-based matching, ambiguity is only possible when
        # one registered body is itself a suffix of another; we
        # deterministically pick the longest such body so the answer
        # corresponds to the strongest evidence present in the prompt.
        matches = [body for body in self._payloads if prompt.endswith(body)]
        if not matches:
            # Hard fail rather than masking a routing miss as a quiet
            # false negative in the harness metrics. The error message
            # does not echo the prompt or any body — both contain raw
            # corpus text.
            raise InferenceEvaluationError(
                "MockOracleBackend could not route prompt to any registered"
                " corpus document (prompt does not end with any known body);"
                " ensure the proposer's prompt template places {document} at"
                " the trailing position"
            )
        # Longest body wins on ties — see class docstring.
        winner = max(matches, key=len)
        return self._payloads[winner]

    def health_check(self) -> bool:
        return True


def evaluate_corpus_inference(
    corpus_dir: Path,
    *,
    backend: InferenceBackend | None = None,
    live: bool = False,
) -> RedactionQualityReport:
    """Evaluate the inference-backed proposer against the corpus.

    Parameters
    ----------
    corpus_dir:
        Same layout as :func:`yomotsusaka.eval.redaction_quality.evaluate_corpus`.
    backend:
        Optional :class:`InferenceBackend`. When omitted, a
        :class:`MockOracleBackend` keyed on the corpus is used —
        intended for harness self-test parity with the deterministic
        path.
    live:
        Owner-gated opt-in for real backends. When ``True``, *backend*
        MUST be supplied (a real backend, never the mock); otherwise
        :class:`InferenceEvaluationError` is raised. CI must leave this
        at the default ``False``.

    Raises
    ------
    InferenceEvaluationError
        If ``live=True`` and no backend was supplied.
    yomotsusaka.eval.redaction_quality.CorpusLoadError
        If the corpus is missing or malformed.

    The returned report is the same public-safe shape returned by the
    deterministic harness, so the two are directly comparable
    (subtract / diff at the report level).
    """
    # Validate the corpus path up front so a misconfigured ``--live``
    # call still surfaces a corpus error in preference to the live-mode
    # guard — fail-fast on the cheapest condition.
    load_corpus(corpus_dir)

    if live:
        if backend is None:
            raise InferenceEvaluationError(
                "live=True requires an explicit backend argument; the"
                " mock oracle backend is not a real LLM and must not be"
                " advertised as one"
            )
        # Reject the in-process mock even when supplied explicitly.
        # Codex flagged on PR #102 that the previous guard ("backend
        # is None") let callers smuggle MockOracleBackend through the
        # live path and surface mocked metrics as if they were real,
        # which would poison any experiment-reporting or
        # release-gating decision keyed on the ``live`` flag.
        if isinstance(backend, MockOracleBackend):
            raise InferenceEvaluationError(
                "live=True must not be invoked with MockOracleBackend;"
                " it is a self-test fixture, not a real inference"
                " backend, and routing live experiment metrics through"
                " it would misreport mocked spans as live model output"
            )
        proposer_backend = backend
    else:
        proposer_backend = backend if backend is not None else MockOracleBackend(corpus_dir)

    proposer = InferenceBackedSpanProposer(proposer_backend)
    logger.debug(
        "redaction_quality_inference: evaluating corpus_dir=%s with live=%s",
        corpus_dir,
        live,
    )
    return evaluate_corpus(corpus_dir, proposer=proposer)
