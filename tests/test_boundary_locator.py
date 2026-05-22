"""Locator-grammar tests for :mod:`yomotsusaka.boundary`.

Pins the frozen URI shape

    private://<exposure_class>/<artifact_kind>/<opaque_id>[#<fragment>]

as the on-the-wire form for every public artifact reference.

The umbrella's #29 contract tests will be written against this grammar; if
this file's invariants drift, #29 will fail in a way that points back here.
"""

from __future__ import annotations

import pytest

from yomotsusaka.boundary import (
    ARTIFACT_KINDS,
    EXPOSURE_CLASSES,
    LOCATOR_SCHEME,
    ParsedLocator,
    PublicHandle,
    SpanSpec,
    build_locator,
    parse_locator,
)
from yomotsusaka.schemas import EntityKind


# ---------------------------------------------------------------------------
# build_locator / parse_locator round-trip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exposure_class,artifact_kind,opaque_id,fragment",
    [
        ("agent_redacted", "manifest", "doc-001", None),
        ("agent_public", "search_hit", "abc.123", None),
        ("private", "private_dict", "X" * 128, None),  # max length
        ("restricted", "restoration_request", "x", "frag1"),
        ("never_expose", "status_report", "y_z.0", "F" * 64),  # max fragment length
    ],
)
def test_build_and_parse_round_trip(
    exposure_class: str, artifact_kind: str, opaque_id: str, fragment: str | None
) -> None:
    locator = build_locator(
        exposure_class=exposure_class,
        artifact_kind=artifact_kind,
        opaque_id=opaque_id,
        fragment=fragment,
    )
    # On-the-wire shape sanity check.
    assert locator.startswith(f"{LOCATOR_SCHEME}://{exposure_class}/{artifact_kind}/")
    if fragment is None:
        assert "#" not in locator
    else:
        assert locator.endswith(f"#{fragment}")

    parsed = parse_locator(locator)
    assert isinstance(parsed, ParsedLocator)
    assert parsed.exposure_class == exposure_class
    assert parsed.artifact_kind == artifact_kind
    assert parsed.opaque_id == opaque_id
    assert parsed.fragment == fragment


def test_locator_constants_match_metaplan() -> None:
    assert LOCATOR_SCHEME == "private"
    assert EXPOSURE_CLASSES == frozenset(
        {"agent_public", "agent_redacted", "private", "restricted", "never_expose"}
    )
    assert ARTIFACT_KINDS == frozenset(
        {"manifest", "private_dict", "search_hit", "restoration_request", "status_report"}
    )


@pytest.mark.parametrize(
    "bad_locator",
    [
        "",
        "doc-001",
        "public://agent_redacted/manifest/doc-001",  # wrong scheme
        "private:/agent_redacted/manifest/doc-001",  # missing slash
        "private:///manifest/doc-001",  # empty class
        "private://made_up_class/manifest/doc-001",  # unknown class
        "private://agent_redacted/made_up_kind/doc-001",  # unknown kind
        "private://agent_redacted/manifest/",  # empty opaque_id
        "private://agent_redacted/manifest/has space",  # invalid charset
        "private://agent_redacted/manifest/has/slash",  # path inside opaque_id
        "private://agent_redacted/manifest/" + "x" * 129,  # opaque_id too long
        "private://agent_redacted/manifest/doc-001#",  # empty fragment
        "private://agent_redacted/manifest/doc-001#frag/with/slash",
        "private://agent_redacted/manifest/doc-001#" + "f" * 65,  # fragment too long
        "private://agent_redacted/manifest/doc-001?query=1",  # query string not allowed
        "private://agent_redacted/manifest/.",  # path-traversal segment
        "private://agent_redacted/manifest/..",  # path-traversal segment
    ],
)
def test_parse_locator_rejects_invalid(bad_locator: str) -> None:
    assert parse_locator(bad_locator) is None


def test_parse_locator_handles_non_string() -> None:
    # parse_locator never raises; non-strings come back as None.
    # Cast through Any so the type-checker does not flag the deliberate misuse.
    from typing import Any

    bad: Any = 42
    assert parse_locator(bad) is None
    bad = None
    assert parse_locator(bad) is None


# ---------------------------------------------------------------------------
# build_locator input validation
# ---------------------------------------------------------------------------


def test_build_locator_rejects_unknown_exposure_class() -> None:
    with pytest.raises(ValueError, match="exposure_class"):
        build_locator(
            exposure_class="not_a_class",
            artifact_kind="manifest",
            opaque_id="doc-001",
        )


def test_build_locator_rejects_unknown_artifact_kind() -> None:
    with pytest.raises(ValueError, match="artifact_kind"):
        build_locator(
            exposure_class="agent_redacted",
            artifact_kind="not_a_kind",
            opaque_id="doc-001",
        )


@pytest.mark.parametrize(
    "bad_opaque_id",
    ["", "has space", "has/slash", "..", ".", "x" * 129, "has\x00null"],
)
def test_build_locator_rejects_bad_opaque_id(bad_opaque_id: str) -> None:
    with pytest.raises(ValueError, match="opaque_id"):
        build_locator(
            exposure_class="agent_redacted",
            artifact_kind="manifest",
            opaque_id=bad_opaque_id,
        )


@pytest.mark.parametrize("bad_fragment", ["", "has space", "has/slash", "x" * 65])
def test_build_locator_rejects_bad_fragment(bad_fragment: str) -> None:
    with pytest.raises(ValueError, match="fragment"):
        build_locator(
            exposure_class="agent_redacted",
            artifact_kind="manifest",
            opaque_id="doc-001",
            fragment=bad_fragment,
        )


# ---------------------------------------------------------------------------
# PublicHandle / SpanSpec
# ---------------------------------------------------------------------------


def test_public_handle_carries_only_locator() -> None:
    handle = PublicHandle(
        locator=build_locator(
            exposure_class="agent_redacted",
            artifact_kind="manifest",
            opaque_id="doc-001",
        )
    )
    # No vault path, no doc_id field, no internal state.
    assert set(handle.model_dump().keys()) == {"locator"}
    assert handle.locator == "private://agent_redacted/manifest/doc-001"


def test_public_handle_is_frozen_and_extra_forbid() -> None:
    handle = PublicHandle(
        locator="private://agent_redacted/manifest/doc-001",
    )
    # frozen
    with pytest.raises(Exception):  # noqa: BLE001 — pydantic raises ValidationError
        handle.locator = "private://agent_redacted/manifest/doc-002"  # type: ignore[misc]
    # extra=forbid
    with pytest.raises(Exception):  # noqa: BLE001
        PublicHandle(
            locator="private://agent_redacted/manifest/doc-001",
            vault_path="/tmp/leak",  # type: ignore[call-arg]
        )


def test_span_spec_projects_to_internal_span() -> None:
    spec = SpanSpec(start=0, end=9, kind=EntityKind.PERSON)
    span = spec.to_internal()
    assert (span.start, span.end, span.kind) == (0, 9, EntityKind.PERSON)
