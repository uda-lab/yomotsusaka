"""Span proposer tests (issue #72).

These tests cover the deterministic and inference-backed proposers in
isolation, plus the new ``proposer=`` integration in
:func:`yomotsusaka.pipeline.process_document`.

Privacy discipline per ``AGENTS.md``: raw private values appear only
inside private-dictionary assertions; public-side assertions match on
shapes (placeholder prefixes, kinds, counts) so the test file does not
duplicate private-string literals outside the round-trip scope.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from yomotsusaka.inference_backend import (
    DummyBackend,
    InferenceBackend,
    PodUnavailableError,
    VLLMGenerationError,
)
from yomotsusaka.pipeline import process_document
from yomotsusaka.redactor import Span
from yomotsusaka.restoration_api import restore
from yomotsusaka.schemas import EntityKind
from yomotsusaka.span_proposer import (
    DeterministicSpanProposer,
    InferenceBackedSpanProposer,
    NoOpSpanProposer,
    SpanProposer,
    SpanProposerError,
)


# ---------------------------------------------------------------------------
# Canonical-fixture shape (matches tests/test_pipeline.py)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _SpanSpec:
    start: int
    end: int
    kind: EntityKind
    placeholder_prefix: str


_CANONICAL_SPAN_SPECS: tuple[_SpanSpec, ...] = (
    _SpanSpec(start=0, end=9, kind=EntityKind.PERSON, placeholder_prefix="<PERSON_"),
    _SpanSpec(start=19, end=28, kind=EntityKind.ORG, placeholder_prefix="<ORG_"),
    _SpanSpec(start=42, end=47, kind=EntityKind.ID_NUMBER, placeholder_prefix="<ID_NUMBER_"),
)


def _explicit_canonical_spans() -> list[Span]:
    return [Span(start=s.start, end=s.end, kind=s.kind) for s in _CANONICAL_SPAN_SPECS]


# ---------------------------------------------------------------------------
# Test backends
# ---------------------------------------------------------------------------


class _ScriptedBackend(InferenceBackend):
    """Test backend that returns a fixed response payload (no GPU/API)."""

    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[str] = []

    def generate(self, prompt: str, *, max_tokens: int = 512) -> str:  # noqa: ARG002
        self.calls.append(prompt)
        return self.response

    def health_check(self) -> bool:
        return True


class _RaisingBackend(InferenceBackend):
    """Test backend that raises the configured ``InferenceBackendError``."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.calls: list[str] = []

    def generate(self, prompt: str, *, max_tokens: int = 512) -> str:  # noqa: ARG002
        self.calls.append(prompt)
        raise self._exc

    def health_check(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# DeterministicSpanProposer
# ---------------------------------------------------------------------------


def test_deterministic_proposer_extracts_canonical_fixture_spans() -> None:
    proposer = DeterministicSpanProposer()
    raw_text = "Alice Tan works at Acme Corp. Patient ID: 12345."
    spans = proposer.propose(raw_text)

    expected = {(s.start, s.end, s.kind) for s in _explicit_canonical_spans()}
    actual = {(s.start, s.end, s.kind) for s in spans}
    assert actual == expected


def test_deterministic_proposer_is_a_span_proposer() -> None:
    """ABC subtype check — guards against accidental contract drift."""
    assert isinstance(DeterministicSpanProposer(), SpanProposer)


def test_deterministic_proposer_returns_empty_for_clean_text() -> None:
    proposer = DeterministicSpanProposer()
    spans = proposer.propose("The quick brown fox jumps over a lazy dog.")
    assert spans == []


def test_deterministic_proposer_accepts_custom_rules() -> None:
    rules = [(re.compile(r"\bcat\b"), EntityKind.CUSTOM)]
    proposer = DeterministicSpanProposer(rules=rules)
    spans = proposer.propose("the cat sat on the cat")
    assert [s.kind for s in spans] == [EntityKind.CUSTOM, EntityKind.CUSTOM]
    assert all(s.end - s.start == 3 for s in spans)


def test_deterministic_proposer_does_not_log_raw_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Privacy invariant: raw text MUST NOT appear in log records."""
    raw_text = "Alice Tan works at Acme Corp. Patient ID: 12345."
    private_strings = ("Alice Tan", "Acme Corp", "12345")

    proposer = DeterministicSpanProposer()
    with caplog.at_level("DEBUG", logger="yomotsusaka.span_proposer"):
        proposer.propose(raw_text)

    log_blob = "\n".join(rec.getMessage() for rec in caplog.records)
    for s in private_strings:
        assert s not in log_blob


# ---------------------------------------------------------------------------
# NoOpSpanProposer
# ---------------------------------------------------------------------------


def test_noop_proposer_returns_empty_list() -> None:
    """NoOp sentinel ALWAYS returns ``[]`` regardless of input."""
    proposer = NoOpSpanProposer()
    assert proposer.propose("") == []
    assert proposer.propose("Alice Tan works at Acme Corp.") == []
    assert isinstance(proposer, SpanProposer)


def test_process_document_with_noop_proposer_commits_unredacted_text(
    tmp_path: Path,
) -> None:
    """NoOpSpanProposer lets callers commit already-redacted text without
    violating the XOR invariant. This is the path templates.py uses."""
    raw_text = "An <PERSON_aabbccdd> writes to <ORG_11223344>"
    handle = process_document(
        doc_id="already-redacted-001",
        raw_text=raw_text,
        spans=[],
        vault_root=tmp_path / "vault",
        proposer=NoOpSpanProposer(),
    )
    manifest = json.loads(
        (tmp_path / "vault" / "manifests" / "already-redacted-001.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["redacted_text"] == raw_text
    assert manifest["entities"] == []
    restored = restore(handle, vault_root=tmp_path / "vault")
    assert restored == []


# ---------------------------------------------------------------------------
# InferenceBackedSpanProposer — happy path
# ---------------------------------------------------------------------------


def test_inference_backed_proposer_parses_well_formed_json_response() -> None:
    payload = json.dumps(
        [
            {"start": 0, "end": 9, "kind": "PERSON"},
            {"start": 19, "end": 28, "kind": "ORG"},
            {"start": 42, "end": 47, "kind": "ID_NUMBER"},
        ]
    )
    backend = _ScriptedBackend(payload)
    proposer = InferenceBackedSpanProposer(backend)

    spans = proposer.propose("Alice Tan works at Acme Corp. Patient ID: 12345.")

    assert spans == [
        Span(start=0, end=9, kind=EntityKind.PERSON),
        Span(start=19, end=28, kind=EntityKind.ORG),
        Span(start=42, end=47, kind=EntityKind.ID_NUMBER),
    ]
    # The backend was called exactly once with a prompt carrying the raw
    # text — that is by design (private-boundary computation).
    assert len(backend.calls) == 1
    assert "Alice Tan works at Acme Corp" in backend.calls[0]


def test_inference_backed_proposer_handles_empty_array() -> None:
    backend = _ScriptedBackend("[]")
    proposer = InferenceBackedSpanProposer(backend)
    assert proposer.propose("the quick brown fox") == []


def test_inference_backed_proposer_accepts_custom_prompt_template() -> None:
    template = "EXTRACT: {document}\nReturn JSON."
    backend = _ScriptedBackend("[]")
    proposer = InferenceBackedSpanProposer(backend, schema_prompt_template=template)
    proposer.propose("hello world")
    assert backend.calls == ["EXTRACT: hello world\nReturn JSON."]


def test_inference_backed_proposer_rejects_template_without_document_placeholder() -> None:
    with pytest.raises(ValueError, match="document"):
        InferenceBackedSpanProposer(
            DummyBackend(), schema_prompt_template="no placeholder here"
        )


# ---------------------------------------------------------------------------
# InferenceBackedSpanProposer — error paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_response",
    [
        "this is not JSON",
        "{}",  # not a list
        "null",
        "42",
        '[{"start": 0, "end": 9}]',  # missing kind
        '[{"start": 0, "end": 9, "kind": "NOT_A_KIND"}]',
        '[{"start": "0", "end": 9, "kind": "PERSON"}]',  # start not int
        '[{"start": 0, "end": "9", "kind": "PERSON"}]',  # end not int
        '[{"start": 0, "end": 9, "kind": 1}]',  # kind not str
        "[42]",  # element not dict
        # Offset semantics (review feedback): negative offsets and
        # zero-length / inverted ranges must be rejected at parse time
        # because the redactor's slice semantics would otherwise accept
        # them silently and commit a misleading manifest.
        '[{"start": -1, "end": 9, "kind": "PERSON"}]',
        '[{"start": 5, "end": 5, "kind": "PERSON"}]',
        '[{"start": 9, "end": 0, "kind": "PERSON"}]',
    ],
)
def test_inference_backed_proposer_unparseable_response_raises_generic_error(
    bad_response: str,
) -> None:
    backend = _ScriptedBackend(bad_response)
    proposer = InferenceBackedSpanProposer(backend)
    with pytest.raises(SpanProposerError) as excinfo:
        proposer.propose("Alice Tan works at Acme Corp. Patient ID: 12345.")

    # Privacy invariant: the error MUST carry a fixed message and MUST
    # NOT echo the backend response body (which could include the raw
    # text the model was asked to extract).
    assert str(excinfo.value) == "backend returned unparseable response"
    assert bad_response not in str(excinfo.value)
    assert bad_response not in repr(excinfo.value)


def test_inference_backed_proposer_rejects_offset_past_document_end() -> None:
    """A backend returning ``end > len(raw_text)`` is a parse failure.

    The ``raw_text`` argument is needed to make this check; the parse
    helper alone cannot know the document length. Surfacing it here
    prevents a misbehaving model from committing a manifest whose spans
    reference offsets outside the document body.
    """
    raw_text = "short doc"  # len == 9
    backend = _ScriptedBackend(
        '[{"start": 0, "end": 100, "kind": "PERSON"}]'
    )
    proposer = InferenceBackedSpanProposer(backend)
    with pytest.raises(SpanProposerError) as excinfo:
        proposer.propose(raw_text)
    assert str(excinfo.value) == "backend returned unparseable response"


def test_inference_backed_proposer_suppresses_exception_chain_on_parse_failure() -> None:
    """Privacy invariant: parse failures MUST NOT chain the underlying
    exception (which could carry attacker-controlled response fragments
    in its ``args``). The fixed-message ``SpanProposerError`` is the only
    privacy-bearing surface; ``__cause__`` must be ``None``.
    """
    # JSON decode failure path: ``json.loads`` raises ValueError whose
    # ``args[0]`` typically includes the offending character. We need to
    # be sure that does not propagate via ``raise ... from exc``.
    backend = _ScriptedBackend("not json at all")
    proposer = InferenceBackedSpanProposer(backend)
    with pytest.raises(SpanProposerError) as excinfo:
        proposer.propose("Alice Tan works at Acme Corp. Patient ID: 12345.")
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True

    # EntityKind coercion path: ``EntityKind('NOT_A_KIND')`` raises
    # ValueError whose ``args[0]`` echoes the (untrusted) input value.
    backend2 = _ScriptedBackend(
        '[{"start": 0, "end": 9, "kind": "NOT_A_KIND"}]'
    )
    proposer2 = InferenceBackedSpanProposer(backend2)
    with pytest.raises(SpanProposerError) as excinfo2:
        proposer2.propose("Alice Tan works at Acme Corp.")
    assert excinfo2.value.__cause__ is None
    assert excinfo2.value.__suppress_context__ is True


def test_inference_backed_proposer_propagates_vllm_generation_error() -> None:
    """InferenceBackendError MUST propagate unwrapped — no silent fallback."""
    exc = VLLMGenerationError("upstream timeout", reason="vllm_timeout")
    backend = _RaisingBackend(exc)
    proposer = InferenceBackedSpanProposer(backend)

    with pytest.raises(VLLMGenerationError) as excinfo:
        proposer.propose("Alice Tan works at Acme Corp. Patient ID: 12345.")
    # Same instance, same reason — not wrapped in SpanProposerError.
    assert excinfo.value is exc
    assert excinfo.value.reason == "vllm_timeout"


def test_inference_backed_proposer_propagates_pod_unavailable_error() -> None:
    exc = PodUnavailableError("pod down")
    backend = _RaisingBackend(exc)
    proposer = InferenceBackedSpanProposer(backend)

    with pytest.raises(PodUnavailableError) as excinfo:
        proposer.propose("some text")
    assert excinfo.value is exc
    assert excinfo.value.reason == "pod_unavailable"


def test_inference_backed_proposer_error_does_not_leak_raw_text() -> None:
    """Even when the backend raises, the proposer MUST NOT add raw text to
    error messages it constructs itself. The backend's own exception
    propagates unmodified (its message content is the backend's
    responsibility); see ``yomotsusaka.vllm_backend`` for the canonical
    redaction discipline applied to backend-side messages.
    """
    raw_text = "Alice Tan works at Acme Corp. Patient ID: 12345."
    backend = _ScriptedBackend("not json at all")
    proposer = InferenceBackedSpanProposer(backend)
    with pytest.raises(SpanProposerError) as excinfo:
        proposer.propose(raw_text)
    for private in ("Alice Tan", "Acme Corp", "12345"):
        assert private not in str(excinfo.value)
        assert private not in repr(excinfo.value)


# ---------------------------------------------------------------------------
# process_document integration
# ---------------------------------------------------------------------------


def test_process_document_with_deterministic_proposer_matches_explicit_spans(
    tmp_path: Path,
) -> None:
    """The proposer path produces the same manifest + private dict as the
    explicit-spans path on the canonical fixture."""
    raw_text = "Alice Tan works at Acme Corp. Patient ID: 12345."
    expected_private = {
        EntityKind.PERSON: "Alice Tan",
        EntityKind.ORG: "Acme Corp",
        EntityKind.ID_NUMBER: "12345",
    }

    # Explicit-spans baseline
    vault_explicit = tmp_path / "vault-explicit"
    handle_explicit = process_document(
        doc_id="explicit-fixture-001",
        raw_text=raw_text,
        spans=_explicit_canonical_spans(),
        vault_root=vault_explicit,
    )

    # Proposer-driven run
    vault_proposed = tmp_path / "vault-proposed"
    handle_proposed = process_document(
        doc_id="proposed-fixture-001",
        raw_text=raw_text,
        spans=[],
        vault_root=vault_proposed,
        proposer=DeterministicSpanProposer(),
    )

    manifest_explicit = json.loads(
        (vault_explicit / "manifests" / "explicit-fixture-001.json").read_text(
            encoding="utf-8"
        )
    )
    manifest_proposed = json.loads(
        (vault_proposed / "manifests" / "proposed-fixture-001.json").read_text(
            encoding="utf-8"
        )
    )

    # The opaque doc_id and source_ref differ by construction; everything
    # else about the redaction shape must match.
    assert manifest_explicit["redacted_text"] == manifest_proposed["redacted_text"]
    assert len(manifest_explicit["entities"]) == len(manifest_proposed["entities"])
    # Private dictionaries must recover the same private values.
    restored_explicit = {
        e.kind: e.original_value
        for e in restore(handle_explicit, vault_root=vault_explicit)
    }
    restored_proposed = {
        e.kind: e.original_value
        for e in restore(handle_proposed, vault_root=vault_proposed)
    }
    assert restored_explicit == expected_private
    assert restored_proposed == expected_private


def test_process_document_with_inference_backed_proposer(tmp_path: Path) -> None:
    """End-to-end with a mock InferenceBackend returning a known JSON
    payload — exercises the same code path RunPod-real callers would
    take, without touching a real backend."""
    payload = json.dumps(
        [
            {"start": 0, "end": 9, "kind": "PERSON"},
            {"start": 19, "end": 28, "kind": "ORG"},
            {"start": 42, "end": 47, "kind": "ID_NUMBER"},
        ]
    )
    backend = _ScriptedBackend(payload)
    proposer = InferenceBackedSpanProposer(backend)

    handle = process_document(
        doc_id="inference-fixture-001",
        raw_text="Alice Tan works at Acme Corp. Patient ID: 12345.",
        spans=[],
        vault_root=tmp_path / "vault",
        proposer=proposer,
    )
    restored = {
        e.kind: e.original_value
        for e in restore(handle, vault_root=tmp_path / "vault")
    }
    assert restored == {
        EntityKind.PERSON: "Alice Tan",
        EntityKind.ORG: "Acme Corp",
        EntityKind.ID_NUMBER: "12345",
    }


def test_process_document_raises_when_both_spans_and_proposer_supplied(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        process_document(
            doc_id="invalid-001",
            raw_text="Alice Tan works at Acme Corp. Patient ID: 12345.",
            spans=_explicit_canonical_spans(),
            vault_root=tmp_path / "vault",
            proposer=DeterministicSpanProposer(),
        )
    # Vault MUST be untouched on validation failure.
    assert not (tmp_path / "vault" / "manifests").exists()
    assert not (tmp_path / "vault" / "private").exists()


def test_process_document_raises_when_neither_spans_nor_proposer_supplied(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        process_document(
            doc_id="invalid-002",
            raw_text="placeholder text",
            spans=[],
            vault_root=tmp_path / "vault",
            proposer=None,
        )
    assert not (tmp_path / "vault" / "manifests").exists()
    assert not (tmp_path / "vault" / "private").exists()


def test_process_document_proposer_error_leaves_vault_untouched(
    tmp_path: Path,
) -> None:
    """An InferenceBackendError from the proposer must propagate unwrapped
    AND MUST leave the vault untouched (proposer runs before redact/commit)."""
    backend = _RaisingBackend(
        VLLMGenerationError("upstream timeout", reason="vllm_timeout")
    )
    proposer = InferenceBackedSpanProposer(backend)

    with pytest.raises(VLLMGenerationError):
        process_document(
            doc_id="will-not-commit-001",
            raw_text="Alice Tan works at Acme Corp. Patient ID: 12345.",
            spans=[],
            vault_root=tmp_path / "vault",
            proposer=proposer,
        )
    assert not (tmp_path / "vault" / "manifests").exists()
    assert not (tmp_path / "vault" / "private").exists()
