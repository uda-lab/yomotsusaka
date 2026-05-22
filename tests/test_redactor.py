"""Tests for the deterministic redactor."""

from yomotsusaka.redactor import Span, redact
from yomotsusaka.schemas import EntityKind


def test_single_span_replaced():
    text = "Hello, Alice. How are you?"
    spans = [Span(start=7, end=12, kind=EntityKind.PERSON)]
    redacted, records, private = redact(text, spans)

    assert "Alice" not in redacted
    assert len(records) == 1
    assert len(private) == 1

    rec = records[0]
    assert rec.kind == EntityKind.PERSON
    assert rec.redacted_key.startswith("<PERSON_")
    assert rec.redacted_key in redacted


def test_multiple_non_overlapping_spans():
    text = "Bob Smith works at Acme Corp."
    spans = [
        Span(start=0, end=9, kind=EntityKind.PERSON),   # Bob Smith
        Span(start=19, end=28, kind=EntityKind.ORG),    # Acme Corp
    ]
    redacted, records, private = redact(text, spans)

    assert "Bob Smith" not in redacted
    assert "Acme Corp" not in redacted
    assert len(records) == 2
    assert len(private) == 2


def test_no_spans_returns_original():
    text = "Nothing to redact here."
    redacted, records, private = redact(text, [])

    assert redacted == text
    assert records == []
    assert private == []


def test_overlapping_spans_second_skipped():
    text = "John Doe"
    spans = [
        Span(start=0, end=8, kind=EntityKind.PERSON),  # John Doe
        Span(start=5, end=8, kind=EntityKind.PERSON),  # Doe (overlaps)
    ]
    redacted, records, _ = redact(text, spans)
    # Only the first span should be replaced
    assert len(records) == 1


def test_key_is_deterministic():
    text = "Meet Alice tomorrow."
    spans = [Span(start=5, end=10, kind=EntityKind.PERSON)]
    _, records1, _ = redact(text, spans)
    _, records2, _ = redact(text, spans)
    assert records1[0].redacted_key == records2[0].redacted_key


def test_private_dict_maps_key_to_original():
    text = "Patient ID: 12345."
    spans = [Span(start=12, end=17, kind=EntityKind.ID_NUMBER)]
    _, records, private = redact(text, spans)
    assert private[0].key == records[0].redacted_key
    assert private[0].original_value == "12345"
