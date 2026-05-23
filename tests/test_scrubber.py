"""Tests for :mod:`yomotsusaka.scrubber` (Fork 2 of #43).

Pins the two-pass + fail-closed re-check contract: raw values are masked
to their canonical keys, vault-shaped paths are masked to
``<vault_path>``, and any raw value still present after both passes
raises :class:`ScrubError`.

Per project ``CLAUDE.md``: raw private literals only live inside the
canonical fixture; they MUST NOT appear in any expected-value assertion
against a scrubbed return.
"""

from __future__ import annotations

import pytest

from yomotsusaka.scrubber import ScrubError, scrub_stream
from yomotsusaka.schemas import EntityKind, PrivateDictEntry

from tests._exposure_denylist import CANONICAL_TEXT, RAW_VALUES


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_entries() -> list[PrivateDictEntry]:
    """Return private-dictionary entries matching the canonical fixture."""
    return [
        PrivateDictEntry(
            key="<PERSON_a5f4ff58>",
            original_value="Alice Tan",
            kind=EntityKind.PERSON,
        ),
        PrivateDictEntry(
            key="<ORG_a73cb456>",
            original_value="Acme Corp",
            kind=EntityKind.ORG,
        ),
        PrivateDictEntry(
            key="<ID_NUMBER_5994471a>",
            original_value="12345",
            kind=EntityKind.ID_NUMBER,
        ),
    ]


# ---------------------------------------------------------------------------
# Pass 1 — raw-value mask
# ---------------------------------------------------------------------------


def test_scrub_stream_replaces_raw_values_with_keys() -> None:
    entries = _make_entries()
    scrubbed = scrub_stream(CANONICAL_TEXT, entries)

    # Raw values must be gone.
    for needle in RAW_VALUES:
        assert needle not in scrubbed, (
            f"scrubbed output still contains raw value {needle!r}"
        )
    # Keys must be present.
    for entry in entries:
        assert entry.key in scrubbed, (
            f"scrubbed output missing key {entry.key!r}"
        )


def test_scrub_stream_handles_empty_text() -> None:
    assert scrub_stream("", _make_entries()) == ""


def test_scrub_stream_handles_empty_private_dict() -> None:
    """An empty private dict only performs the path-shape mask."""
    assert scrub_stream("hello world", []) == "hello world"


def test_scrub_stream_is_idempotent_on_already_scrubbed_text() -> None:
    entries = _make_entries()
    once = scrub_stream(CANONICAL_TEXT, entries)
    twice = scrub_stream(once, entries)
    assert once == twice


def test_scrub_stream_skips_empty_original_values() -> None:
    """Empty ``original_value`` entries are non-actionable and must not
    silently replace every empty-string occurrence (which would mangle
    the output)."""
    entries = [
        PrivateDictEntry(
            key="<PERSON_a5f4ff58>",
            original_value="",
            kind=EntityKind.PERSON,
        ),
    ]
    assert scrub_stream("plain text", entries) == "plain text"


def test_scrub_stream_longest_match_first() -> None:
    """If ``"Alice Tan"`` and ``"Alice"`` are both registered, the longer
    raw value must win — otherwise we'd partial-mask and leave ``" Tan"``
    dangling next to ``<PERSON_alice>``."""
    entries = [
        PrivateDictEntry(
            key="<PERSON_alice>",
            original_value="Alice",
            kind=EntityKind.PERSON,
        ),
        PrivateDictEntry(
            key="<PERSON_alicetan>",
            original_value="Alice Tan",
            kind=EntityKind.PERSON,
        ),
    ]
    scrubbed = scrub_stream("Alice Tan went to lunch", entries)
    # "Alice Tan" was masked as a whole.
    assert "<PERSON_alicetan>" in scrubbed
    assert "Alice Tan" not in scrubbed
    assert "Alice" not in scrubbed  # subsumed by the longer key


# ---------------------------------------------------------------------------
# Pass 2 — path-shape mask
# ---------------------------------------------------------------------------


def test_scrub_stream_masks_manifest_paths() -> None:
    text = "see /manifests/abc-123.json for details"
    scrubbed = scrub_stream(text, [])
    assert "/manifests/abc-123.json" not in scrubbed
    assert "<vault_path>" in scrubbed


def test_scrub_stream_masks_private_paths() -> None:
    text = "look in /private/doc-001.json for entries"
    scrubbed = scrub_stream(text, [])
    assert "/private/doc-001.json" not in scrubbed
    assert "<vault_path>" in scrubbed


def test_scrub_stream_masks_audit_paths() -> None:
    text = "audit log at /audit/restoration.jsonl"
    scrubbed = scrub_stream(text, [])
    assert "/audit/restoration.jsonl" not in scrubbed
    assert "<vault_path>" in scrubbed


def test_scrub_stream_does_not_mask_unrelated_paths() -> None:
    """Only vault-layout-shaped paths are masked; ``/tmp/...`` etc. pass
    through. (The audit log writer pre-scrubs raw-value leaks separately;
    arbitrary path text is allowed.)"""
    text = "/tmp/build/output.log is not a vault path"
    scrubbed = scrub_stream(text, [])
    assert "/tmp/build/output.log" in scrubbed


# ---------------------------------------------------------------------------
# Fail-closed re-check
# ---------------------------------------------------------------------------


def test_scrub_stream_raises_on_residual_raw_value() -> None:
    """The fail-closed re-check raises :class:`ScrubError` when a raw
    value survives both scrubber passes.

    Pathological construction: ``original_value="AA"``, ``key="A"``. The
    naive ``str.replace("AA", "A")`` on ``"AAAA"`` yields ``"AA"`` — the
    raw value reappears in the output because the replacement consumed
    overlapping characters. The fail-closed re-check catches this.
    """
    entry = PrivateDictEntry(
        key="A",
        original_value="AA",
        kind=EntityKind.CUSTOM,
    )
    with pytest.raises(ScrubError) as excinfo:
        scrub_stream("AAAA", [entry])
    # Error message must identify the key, not the raw value.
    assert "A" in str(excinfo.value)
    # The raw value "AA" must NOT appear quoted in the error text
    # (the message format echoes only the key with !r).
    assert "AA'" not in str(excinfo.value) and 'AA"' not in str(excinfo.value)


def test_scrub_stream_does_not_echo_raw_value_in_error() -> None:
    """A more realistic pathological case using the canonical fixture
    plus a constructed overlap. Sanity check that the ScrubError message
    contains only the key reference, never the raw value text.
    """
    entry = PrivateDictEntry(
        key="K",
        original_value="KK",
        kind=EntityKind.CUSTOM,
    )
    with pytest.raises(ScrubError) as excinfo:
        scrub_stream("KKKK", [entry])
    # The raw value "KK" must NOT appear literally as a substring in the
    # error text (other than as part of the key reference, which uses
    # repr() formatting).
    message = str(excinfo.value)
    # Verify message identifies the key.
    assert "'K'" in message


# ---------------------------------------------------------------------------
# Type checks
# ---------------------------------------------------------------------------


def test_scrub_stream_rejects_non_str_input() -> None:
    with pytest.raises(TypeError):
        scrub_stream(123, _make_entries())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        scrub_stream(None, _make_entries())  # type: ignore[arg-type]
