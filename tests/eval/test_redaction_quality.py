"""Redaction-quality harness tests (issue #94, MVP-5 child 05).

Two responsibilities:

1. Run the harness against the in-repo fixture corpus and assert the
   binding thresholds from issue #94: zero false negatives, zero
   placeholder inconsistencies, and full key-set agreement.
2. Self-test the harness on a synthetic "known miss" corpus to
   guarantee a missed expected span is reported as a hard failure
   rather than silently swallowed.

Privacy discipline (binding, mirrors ``AGENTS.md`` + issue #94):

* Tests do **not** echo raw corpus values into assertion messages or
  parametrize-id strings. The corpus is treated as if private.
* Public-side assertions key on counts, kinds, doc names, and
  placeholder identifiers — all public-safe by construction.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from yomotsusaka.eval.redaction_quality import (
    CorpusLoadError,
    DocumentReport,
    RedactionQualityReport,
    evaluate_corpus,
    load_corpus,
)
from yomotsusaka.eval.redaction_quality_inference import (
    InferenceEvaluationError,
    evaluate_corpus_inference,
)
from yomotsusaka.inference_backend import DummyBackend
from yomotsusaka.schemas import EntityKind
from yomotsusaka.span_proposer import DeterministicSpanProposer


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


_REPO_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "redaction_corpus"


# ---------------------------------------------------------------------------
# In-repo corpus thresholds
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def in_repo_report() -> RedactionQualityReport:
    """Run the deterministic harness once and reuse across assertions."""
    return evaluate_corpus(_REPO_FIXTURES)


def test_in_repo_corpus_has_zero_false_negatives(
    in_repo_report: RedactionQualityReport,
) -> None:
    """Binding threshold from issue #94: zero misses on the in-repo corpus.

    Tightenable — if the corpus grows to include intentionally-hard
    fixtures, callers should update this threshold deliberately rather
    than relax it ambiently.
    """
    assert in_repo_report.false_negative_total == 0, (
        f"deterministic proposer missed {in_repo_report.false_negative_total}"
        f" expected span(s) across {len(in_repo_report.documents)} fixture(s);"
        f" inspect report.documents[i].false_negative_spans"
    )
    assert in_repo_report.false_negative_rate == 0.0


def test_in_repo_corpus_has_zero_placeholder_inconsistencies(
    in_repo_report: RedactionQualityReport,
) -> None:
    """Binding threshold from issue #94: no source token may map to two
    distinct placeholders within a single tenant scope.
    """
    assert in_repo_report.placeholder_inconsistencies == ()
    assert in_repo_report.placeholder_consistency == 1.0


def test_in_repo_corpus_key_match_per_document(
    in_repo_report: RedactionQualityReport,
) -> None:
    """Every fixture's ``expected_keys`` must agree with the redactor's
    actual output. A mismatch here usually means the fixture was edited
    without re-running the redactor — the harness's job is to make that
    drift loud.
    """
    mismatches = [
        d.doc_name for d in in_repo_report.documents if not d.key_match
    ]
    assert mismatches == [], (
        f"expected_keys / actual_keys mismatch on fixtures: {mismatches};"
        f" re-derive expected_keys via redactor for the listed docs"
    )


def test_in_repo_corpus_zero_false_positives(
    in_repo_report: RedactionQualityReport,
) -> None:
    """On the in-repo corpus every proposed span must correspond to an
    expected one. (If the corpus grows to include adversarial-leak
    candidates that the proposer *should* catch, this assertion stays
    valid — adversarial cases belong in ``spans`` of the expected.json.)
    """
    assert in_repo_report.false_positive_total == 0
    assert in_repo_report.false_positive_rate == 0.0


def test_in_repo_corpus_has_failures_is_false(
    in_repo_report: RedactionQualityReport,
) -> None:
    """Single-boolean convenience for downstream callers."""
    assert in_repo_report.has_failures is False


def test_in_repo_corpus_has_at_least_one_fixture(
    in_repo_report: RedactionQualityReport,
) -> None:
    """Defence against an empty corpus accidentally passing the suite."""
    assert len(in_repo_report.documents) >= 1
    assert in_repo_report.expected_spans_total >= 1


def test_in_repo_corpus_load_returns_expectations_only() -> None:
    """:func:`load_corpus` MUST NOT return raw document bodies."""
    expectations = load_corpus(_REPO_FIXTURES)
    assert len(expectations) >= 1
    # The frozen pydantic model has no ``raw_text`` field; check by
    # attribute introspection so a future regression that adds one fails
    # this test.
    for exp in expectations:
        assert not hasattr(exp, "raw_text"), (
            "DocumentExpectation must not carry the raw document body"
        )


# ---------------------------------------------------------------------------
# Self-test: harness reports a planted miss (hard fail, not silent log)
# ---------------------------------------------------------------------------


def test_harness_surfaces_planted_false_negative_as_hard_failure(
    tmp_path: Path,
) -> None:
    """Build a synthetic corpus with an expected span the default
    proposer cannot find, and confirm:

    * the harness records the miss as a structural span triple,
    * the rate metric reflects it,
    * :attr:`RedactionQualityReport.has_failures` flips to True so
      downstream tests see a hard signal (not a silent log).
    """
    body = "Just a name: Alice Tan."
    expected = {
        "tenant_id": "_local",
        "spans": [
            {"start": 13, "end": 22, "kind": "PERSON"},
            # PERSON span the default rules will not match — synthetic
            # "leak" the harness must surface as a miss.
            {"start": 0, "end": 4, "kind": "PERSON"},
        ],
        "expected_keys": [
            "<PERSON_a5f4ff58>",
            "<PERSON_aabbccdd>",  # bogus; intentional mismatch
        ],
    }
    (tmp_path / "planted_miss.txt").write_text(body, encoding="utf-8")
    (tmp_path / "planted_miss.expected.json").write_text(
        json.dumps(expected), encoding="utf-8"
    )

    report = evaluate_corpus(tmp_path)
    assert report.has_failures is True
    assert report.false_negative_total >= 1
    assert report.false_negative_rate > 0.0
    # The planted miss must appear in the per-doc miss list as a
    # structural triple — not as raw text.
    doc = report.documents[0]
    miss_kinds = {(c.start, c.end, c.kind) for c in doc.false_negative_spans}
    assert (0, 4, EntityKind.PERSON) in miss_kinds
    # Key-match must also be False because the bogus expected_keys does
    # not match what redact() actually produces.
    assert doc.key_match is False


def test_harness_surfaces_planted_false_positive(tmp_path: Path) -> None:
    """A proposed span absent from ``expected_keys`` / ``spans`` must
    appear as a structural false-positive triple in the report.
    """
    body = "Alice Tan visited."
    # Declare NO expected spans even though the proposer will detect
    # "Alice Tan" — the harness must surface the proposer's PERSON span
    # as a false positive.
    expected = {"tenant_id": "_local", "spans": [], "expected_keys": []}
    (tmp_path / "leaky.txt").write_text(body, encoding="utf-8")
    (tmp_path / "leaky.expected.json").write_text(
        json.dumps(expected), encoding="utf-8"
    )
    report = evaluate_corpus(tmp_path)
    assert report.has_failures is True
    assert report.false_positive_total >= 1
    doc = report.documents[0]
    fp_kinds = {(c.start, c.end, c.kind) for c in doc.false_positive_spans}
    assert (0, 9, EntityKind.PERSON) in fp_kinds


def test_harness_surfaces_planted_placeholder_inconsistency(
    tmp_path: Path,
) -> None:
    """A custom proposer that emits the same source token under two
    different ``EntityKind`` values produces two distinct redaction
    keys for one token. The harness must flag that as an inconsistency
    rather than silently treating each kind as independent.
    """
    # Document body: "Acme Corp shows up twice as different kinds."
    # We force a custom-rule proposer that catches "Acme Corp" as ORG in
    # the first doc and as CUSTOM in the second; the source token is
    # the same, so the redactor produces two keys (different KIND
    # prefix) and the harness must surface the inconsistency.
    import re

    body_a = "Acme Corp once."
    body_b = "Acme Corp again."
    (tmp_path / "kind_a.txt").write_text(body_a, encoding="utf-8")
    (tmp_path / "kind_b.txt").write_text(body_b, encoding="utf-8")
    # Empty expected sets so we don't drive the false-negative path.
    # We set tenant_id to the same value so the consistency check is
    # in-scope.
    for stem in ("kind_a", "kind_b"):
        (tmp_path / f"{stem}.expected.json").write_text(
            json.dumps(
                {"tenant_id": "_local", "spans": [], "expected_keys": []}
            ),
            encoding="utf-8",
        )

    # The harness API takes a single proposer. To simulate per-doc
    # kind drift (the failure mode the consistency metric is meant to
    # catch), build an inline proposer that alternates between ORG and
    # CUSTOM as ``propose`` is called. Sorted glob order means
    # ``kind_a.txt`` is evaluated first (→ ORG), ``kind_b.txt`` second
    # (→ CUSTOM), so the same source token ``"Acme Corp"`` maps to
    # ``<ORG_a73cb456>`` and ``<CUSTOM_a73cb456>`` respectively.
    from yomotsusaka.redactor import Span
    from yomotsusaka.span_proposer import SpanProposer

    class _AlternatingProposer(SpanProposer):
        def __init__(self) -> None:
            self._calls = 0

        def propose(self, raw_text: str) -> list[Span]:
            self._calls += 1
            # First call: ORG. Second call: CUSTOM. (sorted glob order
            # → kind_a then kind_b.)
            kind = EntityKind.ORG if self._calls == 1 else EntityKind.CUSTOM
            match = re.search(r"\bAcme Corp\b", raw_text)
            if not match:
                return []
            return [Span(start=match.start(), end=match.end(), kind=kind)]

    report = evaluate_corpus(tmp_path, proposer=_AlternatingProposer())
    assert report.has_failures is True
    assert report.placeholder_consistency < 1.0
    assert len(report.placeholder_inconsistencies) == 1
    violation = report.placeholder_inconsistencies[0]
    assert violation.tenant_id == "_local"
    assert len(violation.conflicting_keys) == 2
    assert set(violation.observed_docs) == {"kind_a", "kind_b"}
    # Privacy invariant: the violation MUST NOT carry the raw source
    # token. Validate by checking the model dump string does not
    # contain it.
    dump = violation.model_dump_json()
    assert "Acme Corp" not in dump
    # And the kind is correctly recorded (the *first* kind observed
    # under the conflict — implementation detail, but a useful spot
    # check that the field is populated).
    assert violation.kind in (EntityKind.ORG, EntityKind.CUSTOM)


# ---------------------------------------------------------------------------
# Privacy discipline
# ---------------------------------------------------------------------------


def test_report_serialisation_does_not_contain_raw_corpus_values() -> None:
    """The harness's public report must not echo any raw corpus value
    when serialised through ``model_dump_json``. This is the
    public-safe boundary the issue spec pins.
    """
    report = evaluate_corpus(_REPO_FIXTURES)
    blob = report.model_dump_json()
    # A representative slice of raw private values from the in-repo
    # corpus. (Listed here, in the test file, only because the test's
    # job is to confirm they do NOT leak — this is the documented
    # exception to the "no raw values in tests" rule, mirroring how
    # ``tests/test_span_proposer.py`` enumerates private strings for
    # the no-log assertion.)
    private_strings = (
        "Alice Tan",
        "Acme Corp",
        "Bob Smith",
        "Globex Inc",
        "Carol Jones",
        "Dave Wong",
        "Initech Co",
        "Eve Black",
    )
    for value in private_strings:
        assert value not in blob, (
            f"public report serialisation contains the private corpus"
            f" substring {value!r}; check the harness's report-building path"
        )


def test_harness_does_not_log_raw_corpus_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Privacy invariant: even at DEBUG level the harness's own log
    records must not contain raw document text.
    """
    with caplog.at_level("DEBUG", logger="yomotsusaka.eval.redaction_quality"):
        evaluate_corpus(_REPO_FIXTURES)
    log_blob = "\n".join(rec.getMessage() for rec in caplog.records)
    for value in ("Alice Tan", "Acme Corp", "12345", "Dave Wong", "Initech Co"):
        assert value not in log_blob


# ---------------------------------------------------------------------------
# Loader error paths
# ---------------------------------------------------------------------------


def test_load_corpus_raises_when_directory_is_missing(tmp_path: Path) -> None:
    with pytest.raises(CorpusLoadError, match="does not exist"):
        evaluate_corpus(tmp_path / "no-such-dir")


def test_load_corpus_raises_when_no_fixtures_present(tmp_path: Path) -> None:
    with pytest.raises(CorpusLoadError, match="contains no"):
        evaluate_corpus(tmp_path)


def test_load_corpus_raises_when_expected_json_missing(tmp_path: Path) -> None:
    (tmp_path / "lonely.txt").write_text("body", encoding="utf-8")
    with pytest.raises(CorpusLoadError, match="missing expected metadata"):
        evaluate_corpus(tmp_path)


def test_load_corpus_raises_on_bad_offsets(tmp_path: Path) -> None:
    (tmp_path / "bad.txt").write_text("short", encoding="utf-8")
    (tmp_path / "bad.expected.json").write_text(
        json.dumps(
            {
                "tenant_id": "_local",
                "spans": [{"start": 0, "end": 100, "kind": "PERSON"}],
                "expected_keys": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(CorpusLoadError, match="exceeds document length"):
        evaluate_corpus(tmp_path)


def test_load_corpus_raises_on_invalid_entity_kind(tmp_path: Path) -> None:
    (tmp_path / "bad.txt").write_text("body", encoding="utf-8")
    (tmp_path / "bad.expected.json").write_text(
        json.dumps(
            {
                "tenant_id": "_local",
                "spans": [{"start": 0, "end": 4, "kind": "NOT_A_KIND"}],
                "expected_keys": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(CorpusLoadError, match="not a member of EntityKind"):
        evaluate_corpus(tmp_path)


# ---------------------------------------------------------------------------
# Inference comparison scaffold — mock parity
# ---------------------------------------------------------------------------


def test_inference_mock_oracle_matches_deterministic_on_corpus() -> None:
    """The mock oracle backend answers each prompt with the corpus's
    ground-truth spans, so the inference-backed evaluation MUST match
    the deterministic evaluation on the same corpus.
    """
    detr = evaluate_corpus(_REPO_FIXTURES)
    infer = evaluate_corpus_inference(_REPO_FIXTURES)
    assert infer.false_negative_total == detr.false_negative_total
    assert infer.placeholder_inconsistencies == ()
    assert infer.has_failures is False


def test_inference_live_mode_requires_explicit_backend(tmp_path: Path) -> None:
    """``--live`` must refuse to run without a caller-supplied backend.

    The mock oracle is documented as a self-test tool only; advertising
    it as a real LLM would be a category error.
    """
    # Minimal valid corpus so the loader's own checks pass.
    (tmp_path / "x.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "x.expected.json").write_text(
        json.dumps({"tenant_id": "_local", "spans": [], "expected_keys": []}),
        encoding="utf-8",
    )
    with pytest.raises(InferenceEvaluationError, match="requires an explicit backend"):
        evaluate_corpus_inference(tmp_path, live=True)


def test_inference_live_mode_accepts_caller_backend(tmp_path: Path) -> None:
    """When ``live=True`` and a backend is supplied, the scaffold runs.

    Uses :class:`DummyBackend` (which returns a non-JSON response) and
    expects the proposer to surface that as the documented
    :class:`SpanProposerError` — proving the live path is exercised
    rather than short-circuited.
    """
    from yomotsusaka.span_proposer import SpanProposerError

    (tmp_path / "x.txt").write_text("hello world", encoding="utf-8")
    (tmp_path / "x.expected.json").write_text(
        json.dumps({"tenant_id": "_local", "spans": [], "expected_keys": []}),
        encoding="utf-8",
    )
    with pytest.raises(SpanProposerError):
        evaluate_corpus_inference(tmp_path, backend=DummyBackend(), live=True)


# ---------------------------------------------------------------------------
# Per-document report shape spot checks
# ---------------------------------------------------------------------------


def test_document_report_fields_are_structurally_consistent(
    in_repo_report: RedactionQualityReport,
) -> None:
    """For every per-doc report: ``false_negative_count ==
    len(false_negative_spans)``, key-set arithmetic agrees, etc.
    """
    for doc in in_repo_report.documents:
        assert isinstance(doc, DocumentReport)
        assert doc.false_negative_count == len(doc.false_negative_spans)
        assert doc.false_positive_count == len(doc.false_positive_spans)
        assert doc.missing_keys == (doc.expected_keys - doc.actual_keys)
        assert doc.extra_keys == (doc.actual_keys - doc.expected_keys)
        assert doc.key_match is (doc.actual_keys == doc.expected_keys)


def test_default_proposer_is_used_when_none_supplied(
    in_repo_report: RedactionQualityReport,
) -> None:
    """Confirm parity between an explicit-default and an omitted
    proposer argument so future callers know the documented default is
    real.
    """
    explicit = evaluate_corpus(
        _REPO_FIXTURES, proposer=DeterministicSpanProposer()
    )
    # Equality on the pydantic model checks every field.
    assert explicit == in_repo_report
