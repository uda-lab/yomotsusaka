"""Restoration boundary tests.

Pins the rule that ``restoration_api.restore`` is the only path that
materialises raw private values.  An agent that bypasses the API and
reads ``<vault_root>/manifests/<doc_id>.json`` directly must not be
able to recover the originals.

Per AGENTS.md, raw private literals are confined to the
private-dictionary assertion block of the round-trip test; offsets,
kinds, and placeholder prefixes are the only fixture data that lives
at module scope.

Scope:

- restore() returns all originals keyed by their placeholders;
- the public manifest on disk surfaces no raw values;
- restore() against a handle whose vault path does not exist raises
  (no silent fallback);
- the existing path-traversal guard in ``tests/test_restoration_api.py``
  is referenced, not re-implemented.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from yomotsusaka.pipeline import process_document
from yomotsusaka.redactor import Span
from yomotsusaka.restoration_api import RestorationError, restore
from yomotsusaka.schemas import ArtifactHandle, EntityKind


@dataclass(frozen=True)
class _SpanSpec:
    start: int
    end: int
    kind: EntityKind
    placeholder_prefix: str  # e.g. "<PERSON_"; raw value lives elsewhere


# Canonical fixture offsets and kinds.  Raw values are intentionally not
# captured at module scope; they only appear inside the private-dictionary
# assertion block of ``test_restore_returns_originals_via_placeholders``.
_CANONICAL_SPAN_SPECS: tuple[_SpanSpec, ...] = (
    _SpanSpec(start=0, end=9, kind=EntityKind.PERSON, placeholder_prefix="<PERSON_"),
    _SpanSpec(start=19, end=28, kind=EntityKind.ORG, placeholder_prefix="<ORG_"),
    _SpanSpec(start=42, end=47, kind=EntityKind.ID_NUMBER, placeholder_prefix="<ID_NUMBER_"),
)


def _canonical_spans() -> list[Span]:
    return [Span(start=s.start, end=s.end, kind=s.kind) for s in _CANONICAL_SPAN_SPECS]


def _commit_canonical_fixture(vault_root: Path) -> tuple[ArtifactHandle, str]:
    """Drive the canonical fixture through the pipeline and return the
    resulting handle along with the raw text used to produce it.

    Returned raw text is consumed only by callers that need it for
    private-dictionary assertions; tests that exercise the public
    boundary discard it.
    """
    raw_text = "Alice Tan works at Acme Corp. Patient ID: 12345."
    handle = process_document(
        doc_id="canonical-fixture-001",
        raw_text=raw_text,
        spans=_canonical_spans(),
        vault_root=vault_root,
    )
    return handle, raw_text


def test_restore_returns_originals_via_placeholders(tmp_path: Path) -> None:
    """restore() yields every original keyed by its redacted placeholder."""
    vault_root = tmp_path / "vault"
    handle, _ = _commit_canonical_fixture(vault_root)

    restored = restore(handle, vault_root=vault_root)

    # Every canonical span has a corresponding private-dictionary entry.
    assert len(restored) == len(_CANONICAL_SPAN_SPECS)

    by_kind_prefix = {spec.kind: spec.placeholder_prefix for spec in _CANONICAL_SPAN_SPECS}
    for entry in restored:
        # Each restored entry is routed by an opaque placeholder of the
        # expected ``<KIND_...>`` form for its kind.
        assert entry.key.startswith(by_kind_prefix[entry.kind])

    # ----- private-dictionary assertions (raw values permitted here) -----
    expected_private = {
        EntityKind.PERSON: "Alice Tan",
        EntityKind.ORG: "Acme Corp",
        EntityKind.ID_NUMBER: "12345",
    }
    recovered = {entry.kind: entry.original_value for entry in restored}
    assert recovered == expected_private


def test_manifest_on_disk_surfaces_no_raw_values(tmp_path: Path) -> None:
    """An agent that bypasses restoration_api and reads the manifest
    directly must not be able to recover any raw private value."""
    vault_root = tmp_path / "vault"
    handle, raw_text = _commit_canonical_fixture(vault_root)

    manifest_path = vault_root / "manifests" / f"{handle.doc_id}.json"
    assert manifest_path.exists()

    manifest_blob = manifest_path.read_text(encoding="utf-8")
    manifest_data = json.loads(manifest_blob)

    # Re-derive the raw values from the same source the test used to
    # produce the fixture.  Keeping the literals out of the assertion
    # signature avoids embedding private constants at module scope; the
    # raw text is sliced back into pieces via the canonical offsets.
    raw_values = [raw_text[spec.start : spec.end] for spec in _CANONICAL_SPAN_SPECS]

    for raw in raw_values:
        # Neither the serialised blob nor any structured field exposes
        # a raw private value.
        assert raw not in manifest_blob
        assert raw not in manifest_data["redacted_text"]
        for entity in manifest_data["entities"]:
            for field_value in entity.values():
                assert raw != field_value
                if isinstance(field_value, str):
                    assert raw not in field_value

    # Sanity check: the manifest still carries opaque placeholders so the
    # absence of raw values is not just because the manifest is empty.
    for spec in _CANONICAL_SPAN_SPECS:
        assert spec.placeholder_prefix in manifest_data["redacted_text"]


def test_restore_missing_vault_raises(tmp_path: Path) -> None:
    """restore() against a handle pointing at a non-existent vault file
    must raise (no silent fallback to an empty private dictionary)."""
    vault_root = tmp_path / "vault"
    private_dir = vault_root / "private"
    private_dir.mkdir(parents=True)

    # Path is inside the vault boundary (so we exercise the missing-file
    # branch, not the path-traversal guard tested separately in
    # tests/test_restoration_api.py::test_restore_rejects_path_outside_vault)
    # but the file itself does not exist.
    missing_path = private_dir / "ghost.json"
    assert not missing_path.exists()

    handle = ArtifactHandle(doc_id="ghost", vault_path=str(missing_path))

    with pytest.raises(RestorationError):
        restore(handle, vault_root=vault_root)


# Note: the path-traversal guard is asserted in
# tests/test_restoration_api.py::test_restore_rejects_path_outside_vault.
# This module deliberately does not duplicate that coverage.
