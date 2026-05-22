"""Unit tests for the MVP :class:`~yomotsusaka.validator.Validator`.

Per AGENTS.md, raw private values are confined to private-dictionary
assertions and to the synthesis of ``PrivateDictEntry.original_value``
fields, since the validator's leakage check requires real raw literals to
prove the rule fires.  Module-level fixtures expose offsets, kinds, and
placeholder prefixes only — never raw values.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from yomotsusaka.redactor import Span, redact
from yomotsusaka.schemas import (
    DocumentManifest,
    EntityKind,
    EntityRecord,
    PrivateDictEntry,
)
from yomotsusaka.validator import ValidationError, Validator


@dataclass(frozen=True)
class _SpanSpec:
    start: int
    end: int
    kind: EntityKind
    placeholder_prefix: str


# Offsets and kinds for the canonical fixture; no raw values here so the
# module-level constants do not carry private literals.
_CANONICAL_SPAN_SPECS: tuple[_SpanSpec, ...] = (
    _SpanSpec(start=0, end=9, kind=EntityKind.PERSON, placeholder_prefix="<PERSON_"),
    _SpanSpec(start=19, end=28, kind=EntityKind.ORG, placeholder_prefix="<ORG_"),
    _SpanSpec(start=42, end=47, kind=EntityKind.ID_NUMBER, placeholder_prefix="<ID_NUMBER_"),
)


def _canonical_spans() -> list[Span]:
    return [Span(start=s.start, end=s.end, kind=s.kind) for s in _CANONICAL_SPAN_SPECS]


def _canonical_redacted_artifacts() -> tuple[
    DocumentManifest, list[PrivateDictEntry], str
]:
    """Drive the canonical fixture through the redactor and wrap the
    output in a ``DocumentManifest``.

    Returns the manifest, the matching private dictionary, and the raw
    text — the latter is consumed only inside private-dict assertions
    within an individual test.
    """
    raw_text = "Alice Tan works at Acme Corp. Patient ID: 12345."
    redacted_text, entities, private_dict = redact(raw_text, _canonical_spans())
    manifest = DocumentManifest(
        doc_id="canonical-fixture-001",
        source_ref="canonical-fixture-001",
        redacted_text=redacted_text,
        entities=entities,
    )
    return manifest, list(private_dict), raw_text


# ---------------------------------------------------------------------------
# Passing case
# ---------------------------------------------------------------------------


def test_canonical_fixture_passes() -> None:
    manifest, private_dict, raw_text = _canonical_redacted_artifacts()

    # Validator returns None on success and must not raise.
    assert Validator().validate(manifest, private_dict) is None

    # private-dictionary assertion: every raw canonical value is absent
    # from the redacted text once the validator approves the artifacts.
    raw_values = {
        EntityKind.PERSON: "Alice Tan",
        EntityKind.ORG: "Acme Corp",
        EntityKind.ID_NUMBER: "12345",
    }
    by_kind = {entry.kind: entry.original_value for entry in private_dict}
    assert by_kind == raw_values
    for raw in raw_values.values():
        assert raw in raw_text  # sanity: fixture really did contain the literal
        assert raw not in manifest.redacted_text


# ---------------------------------------------------------------------------
# Failure cases — one per rule
# ---------------------------------------------------------------------------


def test_raw_value_substring_leak_raises() -> None:
    """Rule 1: a raw original_value substring inside redacted_text fails."""
    manifest, private_dict, _ = _canonical_redacted_artifacts()
    # Splice the PERSON raw value back into the redacted text.  This relies
    # on the private dict to know which literal to splice, which is the
    # canonical private-dictionary-assertion shape.
    person_entry = next(p for p in private_dict if p.kind == EntityKind.PERSON)
    leaked_text = manifest.redacted_text + " (leak: " + person_entry.original_value + ")"
    leaked = manifest.model_copy(update={"redacted_text": leaked_text})

    with pytest.raises(ValidationError, match="leaked"):
        Validator().validate(leaked, private_dict)


def test_entity_key_missing_from_redacted_text_raises() -> None:
    """Rule 2a: an entity placeholder absent from redacted_text fails."""
    manifest, private_dict, _ = _canonical_redacted_artifacts()
    # Strip the first entity's placeholder from the redacted text so the
    # entity record points at a key that no longer occurs there.
    missing_key = manifest.entities[0].redacted_key
    stripped = manifest.redacted_text.replace(missing_key, "")
    broken = manifest.model_copy(update={"redacted_text": stripped})

    with pytest.raises(ValidationError, match="absent from redacted_text"):
        Validator().validate(broken, private_dict)


def test_private_dict_key_missing_from_redacted_text_raises() -> None:
    """Rule 2b: a private_dict key absent from redacted_text fails.

    Add an extra private_dict entry whose key does not appear anywhere in
    the manifest, then also add a matching entity record so the set-equality
    rule does not fire first.
    """
    manifest, private_dict, _ = _canonical_redacted_artifacts()
    bogus_key = "<PERSON_deadbeef>"
    bogus_entry = PrivateDictEntry(
        key=bogus_key,
        original_value="never-in-text",
        kind=EntityKind.PERSON,
    )
    bogus_entity = EntityRecord(
        kind=EntityKind.PERSON,
        redacted_key=bogus_key,
        start_char=0,
        end_char=0,
    )
    extended_manifest = manifest.model_copy(
        update={"entities": list(manifest.entities) + [bogus_entity]}
    )
    extended_dict = private_dict + [bogus_entry]

    with pytest.raises(ValidationError, match="absent from redacted_text"):
        Validator().validate(extended_manifest, extended_dict)


def test_key_set_mismatch_raises() -> None:
    """Rule 3: entity-key set != private-dict-key set fails."""
    manifest, private_dict, _ = _canonical_redacted_artifacts()
    # Drop one private-dict entry; entity set still contains its key.
    truncated_dict = private_dict[:-1]

    with pytest.raises(ValidationError, match="disagree"):
        Validator().validate(manifest, truncated_dict)


@pytest.mark.parametrize(
    "bad_key",
    [
        "PERSON_12345678",          # missing angle brackets
        "<PERSON_1234567>",         # 7 hex digits, not 8
        "<PERSON_123456789>",       # 9 hex digits, not 8
        "<PERSON_XYZWXYZW>",        # non-hex digest
        "<UNKNOWN_12345678>",       # kind not in the enum
        "<PERSON-12345678>",        # wrong separator
    ],
)
def test_key_shape_violation_raises(bad_key: str) -> None:
    """Rule 4: any key not matching the canonical shape fails."""
    manifest = DocumentManifest(
        doc_id="shape-violation",
        source_ref="shape-violation",
        redacted_text=f"prefix {bad_key} suffix",
        entities=[
            EntityRecord(
                kind=EntityKind.PERSON,
                redacted_key=bad_key,
                start_char=7,
                end_char=7 + len(bad_key),
            )
        ],
    )
    private_dict = [
        PrivateDictEntry(
            key=bad_key,
            original_value="value",
            kind=EntityKind.PERSON,
        )
    ]

    with pytest.raises(ValidationError, match="canonical"):
        Validator().validate(manifest, private_dict)


def test_entity_kind_prefix_mismatch_raises() -> None:
    """Rule 5a: an entity declaring PERSON but using an <ORG_...> key fails."""
    manifest, private_dict, _ = _canonical_redacted_artifacts()
    # Pick the ORG entity and replace its declared kind with PERSON; the
    # key itself still starts with <ORG_, so prefix vs. kind disagree.
    orig_entities = list(manifest.entities)
    org_index = next(
        i for i, e in enumerate(orig_entities) if e.kind == EntityKind.ORG
    )
    org_entity = orig_entities[org_index]
    mismatched = EntityRecord(
        entity_id=org_entity.entity_id,
        kind=EntityKind.PERSON,
        redacted_key=org_entity.redacted_key,
        start_char=org_entity.start_char,
        end_char=org_entity.end_char,
        confidence=org_entity.confidence,
    )
    orig_entities[org_index] = mismatched
    broken_manifest = manifest.model_copy(update={"entities": orig_entities})

    with pytest.raises(ValidationError, match="prefix kind"):
        Validator().validate(broken_manifest, private_dict)


def test_private_dict_kind_prefix_mismatch_raises() -> None:
    """Rule 5b: same mismatch but on the private_dict side."""
    manifest, private_dict, _ = _canonical_redacted_artifacts()
    org_index = next(
        i for i, entry in enumerate(private_dict) if entry.kind == EntityKind.ORG
    )
    org_entry = private_dict[org_index]
    mismatched = PrivateDictEntry(
        key=org_entry.key,
        original_value=org_entry.original_value,
        kind=EntityKind.PERSON,
        created_at=org_entry.created_at,
    )
    broken_dict = list(private_dict)
    broken_dict[org_index] = mismatched
    # The mirrored entity record still declares ORG, so set-equality holds
    # but the private-dict-side prefix/kind check fires.

    with pytest.raises(ValidationError, match="prefix kind"):
        Validator().validate(manifest, broken_dict)


# ---------------------------------------------------------------------------
# Pipeline integration: ValidationError must propagate and the vault must
# remain untouched.
# ---------------------------------------------------------------------------


def test_pipeline_propagates_validation_error_without_writing_artifacts(
    tmp_path, monkeypatch
) -> None:
    from pathlib import Path

    from yomotsusaka import pipeline as pipeline_module

    class _AlwaysFailingValidator:
        def validate(self, manifest, private_dict):  # noqa: D401
            raise ValidationError("synthetic failure")

    monkeypatch.setattr(pipeline_module, "Validator", _AlwaysFailingValidator)

    vault_root: Path = tmp_path / "vault"

    with pytest.raises(ValidationError, match="synthetic"):
        pipeline_module.process_document(
            doc_id="will-not-commit",
            raw_text="Alice Tan works at Acme Corp. Patient ID: 12345.",
            spans=_canonical_spans(),
            vault_root=vault_root,
        )

    # No manifest or private artifact must exist for the failed document.
    assert not (vault_root / "manifests" / "will-not-commit.json").exists()
    assert not (vault_root / "private" / "will-not-commit.json").exists()
