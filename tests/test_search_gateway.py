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

import pytest

from yomotsusaka.pipeline import process_document
from yomotsusaka.redactor import Span, redact
from yomotsusaka.schemas import DocumentManifest, EntityKind, PrivateDictEntry
from yomotsusaka.search_gateway import QueryResolver, ResolvedQuery, SearchGateway


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


# ---------------------------------------------------------------------------
# QueryResolver — raw → key translation at the private boundary (#48)
# ---------------------------------------------------------------------------


def _canonical_private_entries(raw_text: str) -> tuple[DocumentManifest, list[PrivateDictEntry]]:
    """Reproduce the canonical redaction → return (redacted manifest, private dict).

    The redactor is deterministic, so the keys produced here are identical
    to the ones the pipeline persists to ``<vault_root>/private/...``.
    """
    redacted_text, entities, private_dict = redact(raw_text, _canonical_spans())
    manifest = DocumentManifest(
        doc_id="resolver-test-001",
        source_ref="resolver-test-001",
        redacted_text=redacted_text,
        entities=entities,
    )
    return manifest, private_dict


def test_query_resolver_translates_raw_to_key_and_returns_hit() -> None:
    """A raw private term known to the resolver must produce a hit via the
    translated key, exactly as the equivalent placeholder query would."""
    raw_text = "Alice Tan works at Acme Corp. Patient ID: 12345."
    manifest, private_dict = _canonical_private_entries(raw_text)

    resolver = QueryResolver()
    gateway = SearchGateway(query_resolver=resolver)
    gateway.index(manifest, private_entries=private_dict)

    # The raw value "Alice Tan" was a registered raw value; the gateway
    # must translate it and return the indexed manifest.
    hits = gateway.search("Alice Tan")
    assert hits == [manifest]

    # The corresponding redacted-key query must return the same manifest.
    person_entry = next(e for e in private_dict if e.kind is EntityKind.PERSON)
    placeholder_hits = gateway.search(person_entry.key)
    assert placeholder_hits == [manifest]

    # Privacy-boundary sweep: the returned manifest is the same redacted
    # object the existing tests sweep. No raw fixture value may appear in
    # any string-shaped field of the returned manifest.
    (indexed,) = hits
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


def test_query_resolver_unknown_raw_value_still_zero_hits() -> None:
    """Even with a resolver attached, a raw value that was NEVER registered
    must produce zero hits — the privacy invariant from the resolver-less
    case is preserved."""
    raw_text = "Alice Tan works at Acme Corp. Patient ID: 12345."
    manifest, private_dict = _canonical_private_entries(raw_text)

    resolver = QueryResolver()
    gateway = SearchGateway(query_resolver=resolver)
    gateway.index(manifest, private_entries=private_dict)

    # "Bob Smith" was never indexed, never registered; it must not match.
    assert gateway.search("Bob Smith") == []


def test_query_resolver_without_private_entries_preserves_zero_hits() -> None:
    """A gateway with a resolver but indexed WITHOUT ``private_entries``
    must behave identically to a gateway with no resolver: raw private
    values yield zero hits because nothing was registered for translation."""
    raw_text = "Alice Tan works at Acme Corp. Patient ID: 12345."
    manifest, _ = _canonical_private_entries(raw_text)

    resolver = QueryResolver()
    gateway = SearchGateway(query_resolver=resolver)
    gateway.index(manifest)  # no private_entries kwarg → nothing to translate

    for needle in ("Alice Tan", "Acme Corp", "12345"):
        assert gateway.search(needle) == []


def test_query_resolver_collision_raises() -> None:
    """Re-registering the same raw value with a different key is a hard
    error; the resolver MUST surface it loudly rather than silently picking
    one mapping over the other."""
    resolver = QueryResolver()
    resolver.register("Alice Tan", "<PERSON_aaaaaaaa>")
    # Idempotent re-register with the same key is fine.
    resolver.register("Alice Tan", "<PERSON_aaaaaaaa>")
    # Same raw value, different key → ValueError.
    with pytest.raises(ValueError):
        resolver.register("Alice Tan", "<PERSON_bbbbbbbb>")


def test_query_resolver_longest_match_first() -> None:
    """When two registered raw values overlap (one is a prefix of the
    other), the longer match must win to avoid mis-translating
    ``"Alice Tan"`` as just ``"Alice"``."""
    resolver = QueryResolver()
    resolver.register("Alice", "<PERSON_aaaaaaaa>")
    resolver.register("Alice Tan", "<PERSON_bbbbbbbb>")

    resolved = resolver.translate("Alice Tan is here")
    # The longer registered value must win.
    assert resolved.translated_terms == ("<PERSON_bbbbbbbb>",)
    # "Alice" must NOT appear in the residual since it was consumed as
    # part of the longer match.
    assert "Alice" not in resolved.residual
    assert "Tan" not in resolved.residual


def test_query_resolver_passes_through_unregistered_text_to_residual() -> None:
    """Unregistered text in the query must survive verbatim in the residual;
    registered raw values must NOT appear in the residual after translation."""
    resolver = QueryResolver()
    resolver.register("Alice Tan", "<PERSON_xxxxxxxx>")

    resolved = resolver.translate("hello Alice Tan world")
    assert isinstance(resolved, ResolvedQuery)
    assert resolved.translated_terms == ("<PERSON_xxxxxxxx>",)
    # Raw value MUST be absent from the residual (privacy contract).
    assert "Alice Tan" not in resolved.residual
    # Unregistered framing text passes through.
    assert "hello" in resolved.residual
    assert "world" in resolved.residual


def test_query_resolver_empty_query_returns_empty_resolved_query() -> None:
    resolver = QueryResolver()
    resolver.register("Alice Tan", "<PERSON_x>")
    resolved = resolver.translate("")
    assert resolved.translated_terms == ()
    assert resolved.residual == ""


def test_search_gateway_without_resolver_preserves_legacy_behaviour() -> None:
    """The §Done criterion: a gateway constructed without ``query_resolver``
    and indexed without ``private_entries`` must produce identical results
    to the pre-change implementation — including the zero-hit guarantee
    for raw private values."""
    raw_text = "Alice Tan works at Acme Corp. Patient ID: 12345."
    manifest, _ = _canonical_private_entries(raw_text)

    gateway = SearchGateway()
    gateway.index(manifest)

    # Zero-hit invariant (pre-existing).
    assert gateway.search("Alice Tan") == []
    assert gateway.search("Acme Corp") == []
    assert gateway.search("12345") == []

    # Placeholder substring still hits.
    assert gateway.search("<PERSON_") == [manifest]


def test_search_gateway_resolver_dedupes_across_translated_and_residual() -> None:
    """If both the translated key and the residual would match the same
    manifest, the manifest must appear only once in the result list."""
    raw_text = "Alice Tan works at Acme Corp. Patient ID: 12345."
    manifest, private_dict = _canonical_private_entries(raw_text)

    resolver = QueryResolver()
    gateway = SearchGateway(query_resolver=resolver)
    gateway.index(manifest, private_entries=private_dict)

    # Query combines a raw private value AND a generic literal that
    # appears in the redacted text. Both would independently match the
    # same manifest; dedupe must keep it to a single entry.
    hits = gateway.search("Alice Tan works at")
    assert hits == [manifest]


def test_search_gateway_index_ignores_private_entries_without_resolver() -> None:
    """Indexing with ``private_entries`` but no resolver attached must not
    raise — the entries are silently dropped (no place to put them)."""
    raw_text = "Alice Tan works at Acme Corp. Patient ID: 12345."
    manifest, private_dict = _canonical_private_entries(raw_text)

    gateway = SearchGateway()  # no resolver
    gateway.index(manifest, private_entries=private_dict)

    # Raw queries still produce zero hits (no resolver to translate them).
    assert gateway.search("Alice Tan") == []


# ---------------------------------------------------------------------------
# JSONL snapshot / load (issue #78)
# ---------------------------------------------------------------------------


def _make_manifest(doc_id: str, redacted_text: str) -> DocumentManifest:
    """Build a minimally valid :class:`DocumentManifest` for snapshot tests."""
    return DocumentManifest(
        doc_id=doc_id,
        source_ref=doc_id,
        redacted_text=redacted_text,
    )


def test_snapshot_writes_jsonl(tmp_path: Path) -> None:
    """``snapshot`` writes one JSON-decodable line per indexed manifest at
    ``<vault_root>/index/manifests.jsonl``."""
    vault_root = tmp_path / "vault"
    gateway = SearchGateway()
    m1 = _make_manifest("doc-1", "<PERSON_aaaaaaaa> works at <ORG_bbbbbbbb>.")
    m2 = _make_manifest("doc-2", "<PERSON_cccccccc> joined <ORG_dddddddd>.")
    gateway.index(m1)
    gateway.index(m2)

    final_path = gateway.snapshot(vault_root)

    assert final_path == vault_root / "index" / "manifests.jsonl"
    assert final_path.is_file()
    lines = final_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    parsed = [DocumentManifest.model_validate_json(line) for line in lines]
    assert {p.doc_id for p in parsed} == {"doc-1", "doc-2"}


def test_snapshot_atomic_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On rename failure the temp file is cleaned up and the final file is
    not partially written."""
    import os as _os

    vault_root = tmp_path / "vault"
    gateway = SearchGateway()
    gateway.index(_make_manifest("doc-x", "<PERSON_x>"))

    final_path = vault_root / "index" / "manifests.jsonl"
    tmp_marker = vault_root / "index" / "manifests.jsonl.tmp"

    def _boom(src, dst):  # noqa: ANN001 - test shim mirrors os.replace shape
        raise OSError("simulated rename failure")

    monkeypatch.setattr(_os, "replace", _boom)

    with pytest.raises(OSError, match="simulated rename failure"):
        gateway.snapshot(vault_root)

    # Final file must NOT exist — the rename was the commit step and it
    # failed; the on-disk state is the pre-snapshot state.
    assert not final_path.exists()
    # Temp file must be cleaned up so a retry does not see stale bytes.
    assert not tmp_marker.exists()


def test_load_repopulates_index(tmp_path: Path) -> None:
    """Snapshot from one gateway, ``load`` into a fresh one, then search."""
    vault_root = tmp_path / "vault"
    src = SearchGateway()
    src.index(_make_manifest("doc-α", "<PERSON_aaaaaaaa> story line"))
    src.index(_make_manifest("doc-β", "<ORG_bbbbbbbb> joined the team"))
    src.snapshot(vault_root)

    dst = SearchGateway()
    loaded = dst.load(vault_root)

    assert loaded == 2
    hits = dst.search("<PERSON_")
    assert len(hits) == 1
    assert hits[0].doc_id == "doc-α"


def test_load_skips_malformed_lines(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A malformed JSONL line is skipped; ``load`` continues with the rest
    and logs a count-only warning."""
    vault_root = tmp_path / "vault"
    index_dir = vault_root / "index"
    index_dir.mkdir(parents=True)
    final_path = index_dir / "manifests.jsonl"
    # One valid line, one malformed JSON line.
    valid = _make_manifest("doc-ok", "<PERSON_ok>").model_dump_json()
    final_path.write_text(
        valid + "\n" + "{this is not json}\n", encoding="utf-8"
    )

    gateway = SearchGateway()
    with caplog.at_level("WARNING", logger="yomotsusaka.search_gateway"):
        loaded = gateway.load(vault_root)

    assert loaded == 1
    # The valid manifest is in the index.
    assert len(gateway.search("<PERSON_")) == 1
    # Warning logged. Privacy invariant: no raw line content echoed.
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("skipped malformed index line" in r.getMessage() for r in warnings)
    for record in warnings:
        msg = record.getMessage()
        assert "{this is not json}" not in msg


def test_load_no_file_is_noop(tmp_path: Path) -> None:
    """``load`` on a vault with no index file returns 0 and leaves the
    gateway untouched."""
    vault_root = tmp_path / "vault"
    gateway = SearchGateway()
    pre = _make_manifest("doc-pre", "<PERSON_pre> existing")
    gateway.index(pre)

    loaded = gateway.load(vault_root)

    assert loaded == 0
    # Existing manifest still searchable; gateway state unchanged.
    assert gateway.search("<PERSON_") == [pre]


def test_snapshot_does_not_touch_query_resolver(tmp_path: Path) -> None:
    """``snapshot`` MUST NOT serialise the resolver's raw→key map.

    Index a manifest with private entries on a resolver-backed gateway,
    snapshot, then load into a fresh gateway with a fresh resolver. The
    new resolver must have an empty map: persistence is redacted-only.
    """
    raw_text = "Alice Tan works at Acme Corp. Patient ID: 12345."
    manifest, private_dict = _canonical_private_entries(raw_text)
    vault_root = tmp_path / "vault"

    src_resolver = QueryResolver()
    src = SearchGateway(query_resolver=src_resolver)
    src.index(manifest, private_entries=private_dict)
    assert src_resolver._map  # sanity: resolver populated

    src.snapshot(vault_root)

    # Fresh gateway with a fresh resolver — load only repopulates manifests.
    new_resolver = QueryResolver()
    dst = SearchGateway(query_resolver=new_resolver)
    dst.load(vault_root)

    # Resolver remained empty — no persistence of private-side state.
    assert new_resolver._map == {}
    # A raw private query produces zero hits because translation has no
    # registered mappings to consult.
    assert dst.search("Alice Tan") == []


def test_snapshot_dedupes_by_doc_id_keeping_latest(tmp_path: Path) -> None:
    """Codex review on PR #86 (comment 3293061028, P2): re-indexing the
    same ``doc_id`` must NOT cause stale rows to accumulate in
    ``manifests.jsonl``. The snapshot retains only the most recently
    indexed manifest per ``doc_id`` (last-write-wins, mirroring the
    vault commit semantics)."""
    vault_root = tmp_path / "vault"

    # Initial index + snapshot cycle: two distinct manifests.
    gateway = SearchGateway()
    v1 = _make_manifest("doc-alpha", "<PERSON_old> v1 body")
    v2 = _make_manifest("doc-beta", "<PERSON_beta> beta body")
    gateway.index(v1)
    gateway.index(v2)
    gateway.snapshot(vault_root)

    # Simulate the second-process recovery + re-index pattern: load the
    # snapshot back, then re-index ``doc-alpha`` with an UPDATED manifest.
    # Without dedupe the next snapshot would carry both the stale v1 AND
    # the updated row for the same doc_id, causing stale hits to
    # resurface in a third process.
    recovered = SearchGateway()
    recovered.load(vault_root)
    v1_updated = _make_manifest("doc-alpha", "<PERSON_new> v2 updated body")
    recovered.index(v1_updated)
    recovered.snapshot(vault_root)

    # On-disk: exactly two lines — one per unique doc_id — and doc-alpha
    # carries the UPDATED redacted_text, not the stale one.
    final_path = vault_root / "index" / "manifests.jsonl"
    lines = final_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    parsed = [DocumentManifest.model_validate_json(line) for line in lines]
    by_id = {m.doc_id: m for m in parsed}
    assert set(by_id.keys()) == {"doc-alpha", "doc-beta"}
    assert "v2 updated body" in by_id["doc-alpha"].redacted_text
    assert "v1 body" not in by_id["doc-alpha"].redacted_text

    # A third process loading the dedupe'd snapshot sees only one
    # manifest per doc_id; the stale v1 row never resurfaces in search.
    third = SearchGateway()
    loaded = third.load(vault_root)
    assert loaded == 2
    updated_hits = third.search("updated body")
    assert len(updated_hits) == 1
    assert updated_hits[0].doc_id == "doc-alpha"
    assert third.search("v1 body") == []


def test_snapshot_uses_redacted_text_only(tmp_path: Path) -> None:
    """Drive the pipeline end-to-end, snapshot, and assert no raw private
    value appears anywhere in the on-disk JSONL."""
    raw_text = "Alice Tan works at Acme Corp. Patient ID: 12345."
    doc_id = "snapshot-redacted-001"
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
    final_path = gateway.snapshot(vault_root)

    on_disk = final_path.read_text(encoding="utf-8")
    for needle in ("Alice Tan", "Acme Corp", "12345"):
        assert needle not in on_disk, (
            f"snapshot leaked raw private value {needle!r} into JSONL"
        )
