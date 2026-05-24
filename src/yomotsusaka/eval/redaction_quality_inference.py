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
    whichever document text the prompt contains. The mapping key is the
    raw text the proposer interpolates into the prompt — this lets the
    mock backend operate without any cooperation from the proposer
    template (it inspects the full prompt, locates the document body
    by substring match, and returns the canned span set).

    This backend is for **harness self-tests only**. It is not a
    substitute for a real LLM evaluation; the live-mode opt-in below
    requires a caller-supplied real backend.
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
            meta = json.loads(json_path.read_text(encoding="utf-8"))
            spans = meta.get("spans", []) or []
            self._payloads[body] = json.dumps(
                [
                    {"start": s["start"], "end": s["end"], "kind": s["kind"]}
                    for s in spans
                ]
            )

    def generate(self, prompt: str, *, max_tokens: int = 512) -> str:  # noqa: ARG002
        # Locate the document body inside the prompt by substring
        # containment. Privacy note: we never log the prompt body — it
        # carries the raw corpus text by construction.
        for body, payload in self._payloads.items():
            if body and body in prompt:
                return payload
        # Unknown document — return an empty span list rather than
        # raising, so the harness reports a proposer-side miss rather
        # than a backend error.
        return "[]"

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
