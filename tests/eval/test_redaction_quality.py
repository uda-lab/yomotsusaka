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
    MockOracleBackend,
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


def test_inference_live_mode_rejects_mock_oracle_backend(tmp_path: Path) -> None:
    """``live=True`` MUST refuse :class:`MockOracleBackend` even when
    supplied explicitly. (Regression guard for codex review on PR #102:
    a caller could otherwise smuggle mocked metrics through the live
    path and have them surface as if they were real model output.)
    """
    (tmp_path / "x.txt").write_text("hello world", encoding="utf-8")
    (tmp_path / "x.expected.json").write_text(
        json.dumps({"tenant_id": "_local", "spans": [], "expected_keys": []}),
        encoding="utf-8",
    )
    mock = MockOracleBackend(tmp_path)
    with pytest.raises(InferenceEvaluationError, match="must not be invoked with MockOracleBackend"):
        evaluate_corpus_inference(tmp_path, backend=mock, live=True)


# ---------------------------------------------------------------------------
# MockOracleBackend prompt routing — unambiguous-match guarantee
# (regression guard for codex review on PR #102)
# ---------------------------------------------------------------------------


def test_mock_oracle_routes_by_exact_suffix(tmp_path: Path) -> None:
    """Default :class:`InferenceBackedSpanProposer` template places the
    document body at the prompt's trailing position. The mock routes by
    suffix match, so a prompt assembled by the default template
    resolves unambiguously to its source document.
    """
    # Two fixtures where one body is a substring (but NOT a suffix) of
    # the prompt-formatted other. Substring routing would mis-fire on
    # the shorter; suffix routing must not.
    (tmp_path / "short.txt").write_text("Alice Tan.", encoding="utf-8")
    (tmp_path / "long.txt").write_text(
        "Alice Tan reported. Bob Smith confirmed.", encoding="utf-8"
    )
    for stem in ("short", "long"):
        (tmp_path / f"{stem}.expected.json").write_text(
            json.dumps({"tenant_id": "_local", "spans": [], "expected_keys": []}),
            encoding="utf-8",
        )

    mock = MockOracleBackend(tmp_path)
    # Build a prompt that ends with the short body. Even though the
    # short body's text is contained inside the long body's text, the
    # mock must route to the short doc because the suffix matches it.
    prompt_short = "Document:\nAlice Tan."
    out = mock.generate(prompt_short)
    assert out == "[]"  # short doc's expected spans

    # Now route to the long doc — also expected to land on its own
    # payload, not silently mis-fire.
    prompt_long = "Document:\nAlice Tan reported. Bob Smith confirmed."
    assert mock.generate(prompt_long) == "[]"


def test_mock_oracle_picks_longest_suffix_on_tie(tmp_path: Path) -> None:
    """If two registered bodies both suffix-match a prompt (only
    possible when one body is itself a suffix of another), the longer
    body wins. This is deterministic and corresponds to the strongest
    evidence present in the prompt.
    """
    (tmp_path / "tail.txt").write_text("ends here.", encoding="utf-8")
    (tmp_path / "with_prefix.txt").write_text(
        "Story ends here.", encoding="utf-8"
    )
    for stem, kind in (("tail", "PERSON"), ("with_prefix", "ORG")):
        (tmp_path / f"{stem}.expected.json").write_text(
            json.dumps(
                {
                    "tenant_id": "_local",
                    "spans": [{"start": 0, "end": 5, "kind": kind}],
                    "expected_keys": [],
                }
            ),
            encoding="utf-8",
        )
    mock = MockOracleBackend(tmp_path)
    prompt = "Document:\nStory ends here."
    payload = mock.generate(prompt)
    # The longer body's kind is ORG; the shorter body's kind is
    # PERSON. The longer body must win.
    assert '"ORG"' in payload
    assert '"PERSON"' not in payload


def test_mock_oracle_raises_when_prompt_routes_to_no_known_body(
    tmp_path: Path,
) -> None:
    """A prompt that does not end with any registered body must raise
    rather than silently returning ``"[]"`` (which would mask routing
    bugs as innocuous false negatives in harness metrics).
    """
    (tmp_path / "known.txt").write_text("known body", encoding="utf-8")
    (tmp_path / "known.expected.json").write_text(
        json.dumps({"tenant_id": "_local", "spans": [], "expected_keys": []}),
        encoding="utf-8",
    )
    mock = MockOracleBackend(tmp_path)
    with pytest.raises(InferenceEvaluationError, match="could not route prompt"):
        mock.generate("Document:\nsomething else entirely")


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


# ---------------------------------------------------------------------------
# Agent-runnable CLI (#105 finding F1)
# ---------------------------------------------------------------------------
#
# These tests cover the ``python -m yomotsusaka.eval.redaction_quality``
# entry point added to fix F1 (umbrella #105) — prior to that fix the
# documented invocation exited 0 with zero bytes on stdout/stderr,
# making the harness silently no-op for any agent following the docs.


import io  # noqa: E402  - placed near CLI-only tests to keep top short


def test_cli_passes_on_clean_corpus() -> None:
    """The CLI must emit a non-empty PASS summary and exit 0 on the
    in-repo synthetic corpus (which is the binding "clean" reference).
    """
    from yomotsusaka.eval.redaction_quality import main

    out = io.StringIO()
    err = io.StringIO()
    rc = main(
        ["--corpus", str(_REPO_FIXTURES)],
        stdout=out,
        stderr=err,
    )
    assert rc == 0
    text = out.getvalue()
    assert text, "CLI must not produce a silent no-op"
    # First line must carry the stable PASS/FAIL verdict so grep-based
    # callers can pattern-match without parsing the table.
    first_line = text.splitlines()[0]
    assert first_line == "redaction_quality: PASS"
    # The summary must surface the binding totals so downstream callers
    # have a public-safe signal even in the PASS path.
    assert "false_negative_total=0" in text
    assert "false_positive_total=0" in text
    assert "placeholder_consistency=1.0" in text
    assert err.getvalue() == ""


def test_cli_fails_on_planted_miss(tmp_path: Path) -> None:
    """A planted false negative must surface as exit=1 and a FAIL
    summary so a docile agent gets a loud, parseable signal.
    """
    from yomotsusaka.eval.redaction_quality import main

    body = "A document with no proposable spans."
    expected = {
        "tenant_id": "_local",
        "spans": [{"start": 0, "end": 1, "kind": "PERSON"}],
        "expected_keys": ["<PERSON_xyz>"],
    }
    (tmp_path / "planted.txt").write_text(body, encoding="utf-8")
    (tmp_path / "planted.expected.json").write_text(
        json.dumps(expected), encoding="utf-8"
    )

    out = io.StringIO()
    err = io.StringIO()
    rc = main(
        ["--corpus", str(tmp_path)],
        stdout=out,
        stderr=err,
    )
    assert rc == 1
    text = out.getvalue()
    assert text.startswith("redaction_quality: FAIL\n")
    # The structural triple must appear public-safe (no raw text).
    assert "false_negative_spans: 0:1:PERSON" in text
    # No raw fixture body should leak into either stream.
    assert "proposable spans" not in text
    assert "proposable spans" not in err.getvalue()


def test_cli_input_error_returns_2(tmp_path: Path) -> None:
    """A missing corpus directory must yield exit=2 (input error) with
    an empty stdout and a single-line stderr diagnostic.
    """
    from yomotsusaka.eval.redaction_quality import main

    missing = tmp_path / "no-such-dir"
    out = io.StringIO()
    err = io.StringIO()
    rc = main(
        ["--corpus", str(missing)],
        stdout=out,
        stderr=err,
    )
    assert rc == 2
    assert out.getvalue() == ""
    assert err.getvalue().startswith("error:")
    assert str(missing) in err.getvalue()


def test_cli_json_mode_emits_structured_payload() -> None:
    """``--json`` must emit a single-line JSON envelope containing the
    verdict and the pydantic-public-safe report dump.
    """
    from yomotsusaka.eval.redaction_quality import main

    out = io.StringIO()
    err = io.StringIO()
    rc = main(
        ["--corpus", str(_REPO_FIXTURES), "--json"],
        stdout=out,
        stderr=err,
    )
    assert rc == 0
    payload = json.loads(out.getvalue())
    assert payload["verdict"] == "PASS"
    assert payload["corpus_dir"] == str(_REPO_FIXTURES)
    assert payload["report"]["false_negative_total"] == 0
    assert payload["report"]["placeholder_consistency"] == 1.0
