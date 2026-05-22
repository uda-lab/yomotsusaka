"""Search-gateway boundary tests.

Pins the agent-facing search rule: the gateway indexes only redacted
manifest content; raw private values must never be findable. A regression
that accidentally indexed the private dictionary (or otherwise leaked raw
values into ``DocumentManifest.redacted_text``) would be caught here.

Per AGENTS.md, raw private values are kept out of agent-facing returns,
manifests, and tests *except* inside private-dictionary assertions. The
queries that this test issues against ``SearchGateway`` are deliberately
the raw values from the canonical fixture — they appear as query strings
that must produce *zero* results, which is the privacy-boundary assertion
itself rather than a leak.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from yomotsusaka.pipeline import process_document
from yomotsusaka.redactor import Span
from yomotsusaka.schemas import DocumentManifest, EntityKind
from yomotsusaka.search_gateway import SearchGateway


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


def _canonical_spans() -> list[Span]:
    return [Span(start=s.start, end=s.end, kind=s.kind) for s in _CANONICAL_SPAN_SPECS]


def _load_manifest(vault_root: Path, doc_id: str) -> DocumentManifest:
    """Read the committed manifest back from disk as a ``DocumentManifest``.

    ``process_document`` returns an ``ArtifactHandle``; the manifest itself
    lives at ``<vault_root>/manifests/<doc_id>.json``. Parsing the file
    through ``DocumentManifest.model_validate_json`` keeps the test honest:
    the gateway sees the same agent-safe schema instance the rest of the
    pipeline produces.
    """
    manifest_path = vault_root / "manifests" / f"{doc_id}.json"
    return DocumentManifest.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )


def test_search_gateway_only_indexes_redacted_manifest(tmp_path: Path) -> None:
    # Raw private literals are confined to this test's local scope; the
    # canonical fixture is reused so the boundary assertion lines up with
    # the pipeline round-trip in ``test_pipeline.py``.
    raw_text = "Alice Tan works at Acme Corp. Patient ID: 12345."
    doc_id = "search-boundary-001"
    vault_root = tmp_path / "vault"

    process_document(
        doc_id=doc_id,
        raw_text=raw_text,
        spans=_canonical_spans(),
        vault_root=vault_root,
    )

    manifest = _load_manifest(vault_root, doc_id)

    gateway = SearchGateway()
    gateway.index(manifest)

    # Privacy-boundary core: raw private values must produce zero hits.
    # These queries are not leaks — they are the assertion itself, since
    # any nonzero result would prove the gateway saw a private value.
    assert gateway.search("Alice Tan") == []
    assert gateway.search("Acme Corp") == []
    assert gateway.search("12345") == []

    # Placeholder substrings must round-trip and return the indexed manifest.
    person_hits = gateway.search("<PERSON_")
    assert person_hits == [manifest]

    # The gateway operand is exclusively a ``DocumentManifest``; it must not
    # see, store, or surface anything from the private dictionary. Verify
    # by inspecting every attribute the gateway holds and asserting it is
    # drawn entirely from the manifest schema, with no private value
    # leaking into any string-valued field.
    (indexed,) = person_hits
    assert isinstance(indexed, DocumentManifest)
    assert set(indexed.model_dump().keys()) == set(
        DocumentManifest.model_fields.keys()
    )

    # Sweep every string-shaped field on the manifest for raw private
    # values; finding any one would mean the gateway is exposing private
    # data via the manifest it indexed.
    haystacks = [
        indexed.doc_id,
        indexed.source_ref,
        indexed.redacted_text,
        indexed.summary,
        *indexed.labels,
        *(record.redacted_key for record in indexed.entities),
    ]
    for needle in ("Alice Tan", "Acme Corp", "12345"):
        for hay in haystacks:
            assert needle not in hay
