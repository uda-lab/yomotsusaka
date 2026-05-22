"""Persistence boundary test for the canonical fixture.

Pins the on-disk vault layout produced by ``pipeline.process_document`` so
future refactors cannot accidentally collapse the public/private split.

Per AGENTS.md, raw private values must not appear in agent-facing returns,
manifests, or test artifacts *except* inside private-dictionary assertions.
The canonical raw strings are therefore confined to the local scope of the
test where they are used solely to (a) assert that they are absent from the
public manifest tree and (b) assert that they are present in the
private-dictionary file (the documented exception).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from yomotsusaka.pipeline import process_document
from yomotsusaka.redactor import Span
from yomotsusaka.schemas import EntityKind


@dataclass(frozen=True)
class _SpanSpec:
    start: int
    end: int
    kind: EntityKind


# Offsets and kinds for the canonical fixture; no raw values at module scope.
_CANONICAL_SPAN_SPECS: tuple[_SpanSpec, ...] = (
    _SpanSpec(start=0, end=9, kind=EntityKind.PERSON),
    _SpanSpec(start=19, end=28, kind=EntityKind.ORG),
    _SpanSpec(start=42, end=47, kind=EntityKind.ID_NUMBER),
)


def _canonical_spans() -> list[Span]:
    return [Span(start=s.start, end=s.end, kind=s.kind) for s in _CANONICAL_SPAN_SPECS]


def test_persistence_boundary_keeps_public_and_private_separated(
    tmp_path: Path,
) -> None:
    """Drive the canonical fixture through the pipeline and assert that the
    on-disk vault keeps every raw private value confined to ``private/``."""
    # Raw private literals are confined to this test's local scope.  They are
    # consumed only by the absence/presence assertions below, which is the
    # documented private-dictionary exception in AGENTS.md.
    raw_text = "Alice Tan works at Acme Corp. Patient ID: 12345."
    raw_values = ("Alice Tan", "Acme Corp", "12345")

    vault_root = tmp_path / "vault"
    doc_id = "canonical-fixture-001"

    process_document(
        doc_id=doc_id,
        raw_text=raw_text,
        spans=_canonical_spans(),
        vault_root=vault_root,
    )

    manifest_path = vault_root / "manifests" / f"{doc_id}.json"
    private_path = vault_root / "private" / f"{doc_id}.json"

    # ----- file layout -----
    assert manifest_path.exists(), "public manifest must be written"
    assert private_path.exists(), "private dictionary must be written"

    # ----- manifest is public-safe -----
    manifest_text = manifest_path.read_text(encoding="utf-8")
    for raw in raw_values:
        assert raw not in manifest_text, (
            f"manifest leaked raw value {raw!r}; "
            "public artifacts must not contain private literals"
        )

    # ----- private dictionary carries all raw values keyed by placeholders -----
    private_text = private_path.read_text(encoding="utf-8")
    for raw in raw_values:
        assert raw in private_text, (
            f"private dictionary missing raw value {raw!r}; "
            "restoration would be unable to recover the original document"
        )

    # Each raw value must coexist on the same private-dict entry as a
    # placeholder of the matching kind.  We do not parse JSON here because
    # the substring relationship is sufficient and keeps the test agnostic
    # to entry ordering.
    for spec, raw in zip(_CANONICAL_SPAN_SPECS, raw_values, strict=True):
        placeholder_prefix = f"<{spec.kind.value}_"
        assert placeholder_prefix in private_text, (
            f"private dictionary missing placeholder prefix {placeholder_prefix!r}"
        )
        assert raw in private_text, (
            f"private dictionary missing raw value {raw!r} for kind {spec.kind}"
        )

    # ----- recursive scan: manifests/ subtree carries no raw value -----
    manifest_files = [p for p in (vault_root / "manifests").rglob("*") if p.is_file()]
    assert manifest_files, "expected at least one file under manifests/"
    for path in manifest_files:
        contents = path.read_text(encoding="utf-8")
        for raw in raw_values:
            assert raw not in contents, (
                f"raw value {raw!r} leaked into public manifest tree at {path}"
            )

    # ----- recursive scan: every raw occurrence in the vault lives under private/ -----
    private_subtree = (vault_root / "private").resolve()
    for path in vault_root.rglob("*"):
        if not path.is_file():
            continue
        try:
            contents = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # Binary files cannot meaningfully be scanned for raw text; the
            # MVP commits only JSON, so flag this as a regression rather
            # than silently passing.
            raise AssertionError(
                f"unexpected non-UTF-8 file inside vault: {path}"
            )
        resolved = path.resolve()
        is_under_private = (
            resolved == private_subtree
            or private_subtree in resolved.parents
        )
        for raw in raw_values:
            if raw in contents:
                assert is_under_private, (
                    f"raw value {raw!r} found outside private/ at {path}"
                )
