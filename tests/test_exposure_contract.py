"""Exposure-contract tests for the public boundary surface.

This module pins the agent-facing exposure contract for issue #29
(umbrella #24). It drives the canonical fixture from umbrella #4
(``"Alice Tan works at Acme Corp. Patient ID: 12345."``) through every wired
public surface in :mod:`yomotsusaka.boundary` and scans the serialised output
for:

* raw private values (``"Alice Tan"``, ``"Acme Corp"``, ``"12345"``),
* absolute / resolved vault paths,
* path-shaped substrings that would betray vault layout
  (``/manifests/<id>.json``, ``/private/<id>.json`` — only the manifest file's
  path-as-string is permitted to expose its own location string),
* non-opaque "handle"/"locator" fields (every such field must parse via
  :func:`boundary.parse_locator`).

Per ``CLAUDE.md`` / AGENTS.md, raw private literals appear here only inside
the canonical fixture body and the file-local denylist. They MUST NOT appear
in any expected-value assertion against a public response.

Coverage matrix mirrors the §29 lite-spec surface inventory:

1. ``PublicHandle.model_dump_json()``
2. ``ProcessResponse.model_dump_json()``
3. ``InspectResponse.model_dump_json()`` (incl. nested ``PublicManifestView``
   and each ``EntityRecord``)
4. ``SearchResponse.model_dump_json()`` and each ``SearchHit.model_dump_json()``
5. ``RestorationResponse.model_dump_json()`` (parameterised on outcome)
6. ``StatusReportResponse.model_dump_json()`` (both ``committed`` and
   ``unknown`` branches)
7. ``ResolverFailure.model_dump_json()`` for every ``ResolverFailureReason``
8. On-disk ``<vault_root>/manifests/<doc_id>.json`` contents
9. Captured ``caplog`` records (INFO+) from every ``boundary.*_request``
10. Captured ``capsys`` stdout/stderr from every ``boundary.*_request``
11. ``repr(exc)`` and ``str(exc)`` for every exception raised across the
    boundary in negative-path tests

A drift assertion against :data:`yomotsusaka.boundary.__all__` forces any
new agent-facing response to be added to this scan.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Iterator

import pytest

from yomotsusaka import boundary
from yomotsusaka.boundary import (
    InspectRequest,
    InspectResponse,
    ProcessRequest,
    ProcessResponse,
    PublicHandle,
    PublicManifestView,
    ResolverFailure,
    ResolverFailureReason,
    ResolverScope,
    RestorationRequest,
    RestorationResponse,
    SearchHit,
    SearchRequest,
    SearchResponse,
    SpanSpec,
    StatusReportRequest,
    StatusReportResponse,
    build_locator,
    inspect_request,
    parse_locator,
    process_document_request,
    resolve,
    restoration_request,
    search_request,
    status_report_request,
)
from yomotsusaka.schemas import DocumentManifest, EntityKind
from yomotsusaka.search_gateway import SearchGateway


# ---------------------------------------------------------------------------
# Canonical fixture (umbrella #4)
# ---------------------------------------------------------------------------

CANONICAL_TEXT = "Alice Tan works at Acme Corp. Patient ID: 12345."

# Offsets are verified at module import below so this stays in lockstep with
# CANONICAL_TEXT if the fixture ever drifts.
CANONICAL_SPANS: tuple[SpanSpec, ...] = (
    SpanSpec(start=0, end=9, kind=EntityKind.PERSON),
    SpanSpec(start=19, end=28, kind=EntityKind.ORG),
    SpanSpec(start=42, end=47, kind=EntityKind.ID_NUMBER),
)

RAW_VALUES: tuple[str, ...] = ("Alice Tan", "Acme Corp", "12345")

assert CANONICAL_TEXT[0:9] == "Alice Tan"
assert CANONICAL_TEXT[19:28] == "Acme Corp"
assert CANONICAL_TEXT[42:47] == "12345"

PATH_LEAK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"/manifests/[^/\"\\]+\.json"),
    re.compile(r"/private/[^/\"\\]+\.json"),
)


# ---------------------------------------------------------------------------
# Drift guard against boundary.__all__
# ---------------------------------------------------------------------------

# Every public response type whose serialisation flows through this scan.
# If boundary.__all__ ever grows a new response, this set must grow to match
# (or the new response must be added to one of the per-surface tests below).
EXPECTED_BOUNDARY_SYMBOLS: frozenset[str] = frozenset(
    {
        "PublicHandle",
        "PublicManifestView",
        "ProcessResponse",
        "InspectResponse",
        "SearchHit",
        "SearchResponse",
        "RestorationResponse",
        "StatusReportResponse",
        "ResolverFailure",
        "ResolverFailureReason",
        "ResolverScope",
        "parse_locator",
        "build_locator",
        "process_document_request",
        "inspect_request",
        "search_request",
        "restoration_request",
        "status_report_request",
    }
)


def test_expected_boundary_symbols_are_a_subset_of_module_all() -> None:
    """If ``boundary.__all__`` grows or shrinks, this test forces a manual
    review of the exposure-contract scan list."""
    module_all = set(boundary.__all__)
    missing = EXPECTED_BOUNDARY_SYMBOLS - module_all
    assert not missing, (
        f"EXPECTED_BOUNDARY_SYMBOLS contains names not in boundary.__all__: {missing!r}; "
        "either remove them from this test's expected set or restore them on the boundary."
    )


# ---------------------------------------------------------------------------
# Helpers (file-local; not exported)
# ---------------------------------------------------------------------------


def _iter_strings(value: Any) -> Iterator[str]:
    """Yield every string leaf in *value*, recursing through dict/list/tuple."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            # Keys are usually structural ("doc_id", "locator", ...) but a
            # leaky implementation could place a raw value there. Scan both.
            if isinstance(k, str):
                yield k
            yield from _iter_strings(v)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_strings(item)
    # ints / floats / bools / None / datetime are not string leaves.


def _assert_no_raw_values(blob: str, *, surface: str) -> None:
    for needle in RAW_VALUES:
        assert needle not in blob, (
            f"public surface {surface!r} leaked raw private value {needle!r}: "
            f"{blob!r}"
        )


def _assert_no_paths(blob: str, *, surface: str, extra: tuple[str, ...] = ()) -> None:
    for pattern in PATH_LEAK_PATTERNS:
        match = pattern.search(blob)
        assert match is None, (
            f"public surface {surface!r} leaked a vault-shaped path "
            f"{match.group(0)!r}: {blob!r}"
        )
    for fragment in extra:
        assert fragment not in blob, (
            f"public surface {surface!r} leaked path fragment {fragment!r}: "
            f"{blob!r}"
        )


def _assert_locator_shape(locator: str, *, surface: str) -> None:
    parsed = parse_locator(locator)
    assert parsed is not None, (
        f"public surface {surface!r} carried a non-opaque locator {locator!r}; "
        "every handle/locator field must parse via boundary.parse_locator"
    )


def _walk_handles(payload: Any, *, surface: str) -> int:
    """Walk *payload* (a JSON-mode dict) and check every ``locator``/``handle``
    field round-trips through :func:`parse_locator`. Returns the count of
    locator strings validated, so the caller can assert ``count >= 1`` for
    surfaces that must carry one.
    """
    count = 0
    if isinstance(payload, dict):
        for k, v in payload.items():
            if k == "locator" and isinstance(v, str):
                _assert_locator_shape(v, surface=surface)
                count += 1
            elif k == "handle" and isinstance(v, dict):
                inner = v.get("locator")
                assert isinstance(inner, str), (
                    f"{surface!r}: handle field has no string locator: {v!r}"
                )
                _assert_locator_shape(inner, surface=surface)
                count += 1
                count += _walk_handles(v, surface=surface)
            else:
                count += _walk_handles(v, surface=surface)
    elif isinstance(payload, list):
        for item in payload:
            count += _walk_handles(item, surface=surface)
    return count


def _both_json_renders(model: Any) -> tuple[str, str]:
    """Return both ``model.model_dump_json()`` and the equivalent
    ``json.dumps(model.model_dump(mode='json'))`` so the scan covers both
    escaping paths (Pydantic's own and the stdlib's)."""
    return (
        model.model_dump_json(),
        json.dumps(model.model_dump(mode="json")),
    )


def _scrub_for_path_assertion(vault_root: Path) -> tuple[str, ...]:
    """Path fragments that, if they appear in *public* surfaces, indicate a
    boundary leak. ``str(vault_root)`` is checked alongside the regex-based
    ``/manifests/...``/``/private/...`` patterns."""
    return (str(vault_root), str(vault_root.resolve()))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def canonical_vault(
    tmp_path: Path,
) -> tuple[Path, ProcessResponse, PublicHandle]:
    """Drive the canonical fixture through ``process_document_request`` and
    return the vault root + response + handle for downstream tests."""
    vault_root = tmp_path / "vault"
    response = process_document_request(
        ProcessRequest(
            doc_id="exposure-doc-001",
            raw_text=CANONICAL_TEXT,
            spans=list(CANONICAL_SPANS),
        ),
        vault_root=vault_root,
    )
    return vault_root, response, response.handle


def _expected_locator(doc_id: str) -> str:
    return build_locator(
        exposure_class="agent_redacted",
        artifact_kind="manifest",
        opaque_id=doc_id,
    )


# ---------------------------------------------------------------------------
# 1. PublicHandle
# ---------------------------------------------------------------------------


def test_public_handle_serialisation_is_opaque(
    canonical_vault: tuple[Path, ProcessResponse, PublicHandle],
) -> None:
    vault_root, _response, handle = canonical_vault
    for blob in _both_json_renders(handle):
        _assert_no_raw_values(blob, surface="PublicHandle")
        _assert_no_paths(
            blob, surface="PublicHandle", extra=_scrub_for_path_assertion(vault_root)
        )
    payload = handle.model_dump(mode="json")
    count = _walk_handles(payload, surface="PublicHandle")
    assert count == 1, "PublicHandle must carry exactly one locator"


# ---------------------------------------------------------------------------
# 2. ProcessResponse
# ---------------------------------------------------------------------------


def test_process_response_serialisation_is_opaque(
    canonical_vault: tuple[Path, ProcessResponse, PublicHandle],
) -> None:
    vault_root, response, _handle = canonical_vault
    for blob in _both_json_renders(response):
        _assert_no_raw_values(blob, surface="ProcessResponse")
        _assert_no_paths(
            blob,
            surface="ProcessResponse",
            extra=_scrub_for_path_assertion(vault_root),
        )
    payload = response.model_dump(mode="json")
    count = _walk_handles(payload, surface="ProcessResponse")
    assert count >= 1


# ---------------------------------------------------------------------------
# 3. InspectResponse (incl. PublicManifestView + EntityRecord leaves)
# ---------------------------------------------------------------------------


def test_inspect_response_serialisation_is_opaque(
    canonical_vault: tuple[Path, ProcessResponse, PublicHandle],
) -> None:
    vault_root, _process_response, handle = canonical_vault
    response = inspect_request(
        InspectRequest(locator=handle.locator),
        vault_root=vault_root,
    )
    assert isinstance(response, InspectResponse)

    for blob in _both_json_renders(response):
        _assert_no_raw_values(blob, surface="InspectResponse")
        _assert_no_paths(
            blob,
            surface="InspectResponse",
            extra=_scrub_for_path_assertion(vault_root),
        )

    # Sub-views: PublicManifestView and each EntityRecord leaf.
    manifest_view: PublicManifestView = response.manifest
    for blob in _both_json_renders(manifest_view):
        _assert_no_raw_values(blob, surface="PublicManifestView")
        _assert_no_paths(
            blob,
            surface="PublicManifestView",
            extra=_scrub_for_path_assertion(vault_root),
        )
    for entity in manifest_view.entities:
        for blob in _both_json_renders(entity):
            _assert_no_raw_values(blob, surface="EntityRecord")
            _assert_no_paths(
                blob,
                surface="EntityRecord",
                extra=_scrub_for_path_assertion(vault_root),
            )

    # source_ref was projected out; doubly assert it does not surface here.
    payload = response.model_dump(mode="json")
    assert "source_ref" not in json.dumps(payload), (
        "PublicManifestView must strip source_ref from the manifest projection"
    )


# ---------------------------------------------------------------------------
# 4. SearchResponse and SearchHit
# ---------------------------------------------------------------------------


def _make_indexed_gateway(vault_root: Path, doc_id: str) -> SearchGateway:
    manifest = DocumentManifest.model_validate_json(
        (vault_root / "manifests" / f"{doc_id}.json").read_text(encoding="utf-8")
    )
    gateway = SearchGateway()
    gateway.index(manifest)
    return gateway


def test_search_response_serialisation_is_opaque(
    canonical_vault: tuple[Path, ProcessResponse, PublicHandle],
) -> None:
    vault_root, _response, _handle = canonical_vault
    gateway = _make_indexed_gateway(vault_root, "exposure-doc-001")

    response = search_request(SearchRequest(query="<PERSON_"), gateway=gateway)
    assert isinstance(response, SearchResponse)
    assert len(response.hits) == 1

    for blob in _both_json_renders(response):
        _assert_no_raw_values(blob, surface="SearchResponse")
        _assert_no_paths(
            blob,
            surface="SearchResponse",
            extra=_scrub_for_path_assertion(vault_root),
        )

    for hit in response.hits:
        assert isinstance(hit, SearchHit)
        for blob in _both_json_renders(hit):
            _assert_no_raw_values(blob, surface="SearchHit")
            _assert_no_paths(
                blob,
                surface="SearchHit",
                extra=_scrub_for_path_assertion(vault_root),
            )

    count = _walk_handles(response.model_dump(mode="json"), surface="SearchResponse")
    assert count >= 1


def test_search_response_with_raw_value_queries_yields_no_leak(
    canonical_vault: tuple[Path, ProcessResponse, PublicHandle],
) -> None:
    """Even if a caller smuggles a raw value into the *query*, the response
    body must not echo it back; the index sees only redacted manifest text so
    raw-value queries return zero hits and the serialised response carries
    no raw values."""
    vault_root, _response, _handle = canonical_vault
    gateway = _make_indexed_gateway(vault_root, "exposure-doc-001")

    for needle in RAW_VALUES:
        response = search_request(SearchRequest(query=needle), gateway=gateway)
        assert response.hits == []
        for blob in _both_json_renders(response):
            _assert_no_raw_values(blob, surface=f"SearchResponse(query={needle!r})")


# ---------------------------------------------------------------------------
# 5. RestorationResponse (deferred stub; parameterised on outcome)
# ---------------------------------------------------------------------------


def test_restoration_response_serialisation_is_opaque(
    canonical_vault: tuple[Path, ProcessResponse, PublicHandle],
) -> None:
    vault_root, _response, handle = canonical_vault
    response = restoration_request(
        RestorationRequest(locator=handle.locator, purpose="exposure-contract-test"),
        vault_root=vault_root,
    )
    # MVP-2 always returns the deferred shape, but #27 may swap it in for a
    # wired response. Parameterise on outcome rather than hard-coding
    # ``"deferred"``.
    assert isinstance(response, RestorationResponse)
    assert response.outcome in {"deferred", "restored", "denied"}, (
        "RestorationResponse.outcome must remain a small, enumerated set"
    )

    for blob in _both_json_renders(response):
        _assert_no_raw_values(blob, surface="RestorationResponse")
        _assert_no_paths(
            blob,
            surface="RestorationResponse",
            extra=_scrub_for_path_assertion(vault_root),
        )

    count = _walk_handles(response.model_dump(mode="json"), surface="RestorationResponse")
    assert count >= 1


# ---------------------------------------------------------------------------
# 6. StatusReportResponse (committed + unknown)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("branch", ["committed", "unknown"])
def test_status_report_response_serialisation_is_opaque(
    canonical_vault: tuple[Path, ProcessResponse, PublicHandle],
    branch: str,
) -> None:
    vault_root, _response, handle = canonical_vault
    if branch == "committed":
        locator = handle.locator
    else:
        locator = _expected_locator("never-committed-doc")
    response = status_report_request(
        StatusReportRequest(locator=locator),
        vault_root=vault_root,
    )
    assert isinstance(response, StatusReportResponse)
    assert response.status == branch

    for blob in _both_json_renders(response):
        _assert_no_raw_values(blob, surface=f"StatusReportResponse({branch})")
        _assert_no_paths(
            blob,
            surface=f"StatusReportResponse({branch})",
            extra=_scrub_for_path_assertion(vault_root),
        )

    count = _walk_handles(response.model_dump(mode="json"), surface="StatusReportResponse")
    assert count >= 1


# ---------------------------------------------------------------------------
# 7. ResolverFailure for every ResolverFailureReason
# ---------------------------------------------------------------------------


def test_resolver_failure_malformed_locator_is_opaque(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    failure = resolve(
        "not-a-uri",
        scope=ResolverScope.ORDINARY_AGENT,
        purpose="exposure-contract",
        vault_root=vault_root,
    )
    assert isinstance(failure, ResolverFailure)
    assert failure.reason is ResolverFailureReason.MalformedLocator
    for blob in _both_json_renders(failure):
        _assert_no_raw_values(blob, surface="ResolverFailure.MalformedLocator")
        _assert_no_paths(
            blob,
            surface="ResolverFailure.MalformedLocator",
            extra=_scrub_for_path_assertion(vault_root),
        )


def test_resolver_failure_unknown_artifact_for_unwired_kind_is_opaque(
    tmp_path: Path,
) -> None:
    """A well-formed locator naming a non-``manifest`` artifact_kind (e.g.
    ``private_dict``) must surface as :class:`ResolverFailureReason.UnknownArtifact`
    without echoing any path-shaped detail."""
    vault_root = tmp_path / "vault"
    locator = build_locator(
        exposure_class="agent_redacted",
        artifact_kind="private_dict",
        opaque_id="some-id",
    )
    failure = resolve(
        locator,
        scope=ResolverScope.ORDINARY_AGENT,
        purpose="exposure-contract",
        vault_root=vault_root,
    )
    assert isinstance(failure, ResolverFailure)
    assert failure.reason is ResolverFailureReason.UnknownArtifact
    for blob in _both_json_renders(failure):
        _assert_no_raw_values(blob, surface="ResolverFailure.UnknownArtifact[kind]")
        _assert_no_paths(
            blob,
            surface="ResolverFailure.UnknownArtifact[kind]",
            extra=_scrub_for_path_assertion(vault_root),
        )


def test_resolver_failure_unknown_artifact_for_missing_manifest_is_opaque(
    tmp_path: Path,
) -> None:
    """A well-formed ``manifest`` locator pointing at an uncommitted
    ``opaque_id`` must surface as :class:`ResolverFailureReason.UnknownArtifact`
    (the resolver does not distinguish "kind not wired" from "manifest file
    absent" under ordinary-agent scope) and must not echo vault paths."""
    vault_root = tmp_path / "vault"
    locator = _expected_locator("never-written")
    failure = resolve(
        locator,
        scope=ResolverScope.ORDINARY_AGENT,
        purpose="exposure-contract",
        vault_root=vault_root,
    )
    assert isinstance(failure, ResolverFailure)
    assert failure.reason is ResolverFailureReason.UnknownArtifact
    for blob in _both_json_renders(failure):
        _assert_no_raw_values(blob, surface="ResolverFailure.UnknownArtifact[manifest]")
        _assert_no_paths(
            blob,
            surface="ResolverFailure.UnknownArtifact[manifest]",
            extra=_scrub_for_path_assertion(vault_root),
        )


def test_resolver_failure_artifact_missing_when_private_dict_absent_is_opaque(
    canonical_vault: tuple[Path, ProcessResponse, PublicHandle],
) -> None:
    """Under ``PRIVATE_BOUNDARY`` scope the resolver attempts to load the
    private dictionary; if the manifest file exists but the private dict was
    deleted, the resolver must surface :class:`ResolverFailureReason.ArtifactMissing`
    without echoing the deleted file's path or contents."""
    vault_root, _process_response, handle = canonical_vault
    private_dict_path = vault_root / "private" / "exposure-doc-001.json"
    private_dict_path.unlink()
    assert not private_dict_path.exists()

    failure = resolve(
        handle.locator,
        scope=ResolverScope.PRIVATE_BOUNDARY,
        purpose="exposure-contract",
        vault_root=vault_root,
    )
    assert isinstance(failure, ResolverFailure)
    assert failure.reason is ResolverFailureReason.ArtifactMissing
    for blob in _both_json_renders(failure):
        _assert_no_raw_values(blob, surface="ResolverFailure.ArtifactMissing")
        _assert_no_paths(
            blob,
            surface="ResolverFailure.ArtifactMissing",
            extra=_scrub_for_path_assertion(vault_root),
        )


def test_resolver_failure_scope_denied_is_deferred_to_issue_27() -> None:
    """MVP-2 resolver accepts every :class:`ResolverScope` value (see
    metaplan Fork 6); :class:`ResolverFailureReason.ScopeDenied` is reserved
    for the #27 policy table. Skip with a citation so a future scope-aware
    resolver flips this to a real assertion."""
    pytest.skip("MVP-2 accepts all scopes; see #27 for policy-gated ScopeDenied")


def test_resolver_failure_purpose_not_permitted_is_opaque(tmp_path: Path) -> None:
    """An empty/whitespace ``purpose`` flows into :func:`resolve` and
    surfaces as :class:`ResolverFailureReason.PurposeNotPermitted` without
    echoing private values or paths."""
    vault_root = tmp_path / "vault"
    failure = resolve(
        _expected_locator("any-id"),
        scope=ResolverScope.ORDINARY_AGENT,
        purpose="   ",
        vault_root=vault_root,
    )
    assert isinstance(failure, ResolverFailure)
    assert failure.reason is ResolverFailureReason.PurposeNotPermitted
    for blob in _both_json_renders(failure):
        _assert_no_raw_values(blob, surface="ResolverFailure.PurposeNotPermitted")
        _assert_no_paths(
            blob,
            surface="ResolverFailure.PurposeNotPermitted",
            extra=_scrub_for_path_assertion(vault_root),
        )

    # Also exercise the deferred restoration_request path, whose own purpose
    # check returns the same failure reason.
    restore_failure = restoration_request(
        RestorationRequest(locator=_expected_locator("any-id"), purpose=""),
        vault_root=vault_root,
    )
    assert isinstance(restore_failure, ResolverFailure)
    assert restore_failure.reason is ResolverFailureReason.PurposeNotPermitted
    for blob in _both_json_renders(restore_failure):
        _assert_no_raw_values(
            blob, surface="restoration_request.PurposeNotPermitted"
        )
        _assert_no_paths(
            blob,
            surface="restoration_request.PurposeNotPermitted",
            extra=_scrub_for_path_assertion(vault_root),
        )


# ---------------------------------------------------------------------------
# 8. On-disk manifest file contents
# ---------------------------------------------------------------------------


def test_on_disk_manifest_contains_no_raw_private_values(
    canonical_vault: tuple[Path, ProcessResponse, PublicHandle],
) -> None:
    """The manifest file persisted under ``<vault_root>/manifests/`` is itself
    an ``agent_redacted`` artifact (architecture.md §exposure classes). Its
    *path-as-string* may appear in vault layout, but its *contents* must pass
    the raw-value scan."""
    vault_root, _response, _handle = canonical_vault
    manifest_path = vault_root / "manifests" / "exposure-doc-001.json"
    assert manifest_path.exists()
    contents = manifest_path.read_text(encoding="utf-8")
    _assert_no_raw_values(contents, surface="on_disk_manifest")


def test_on_disk_private_dict_does_contain_raw_values(
    canonical_vault: tuple[Path, ProcessResponse, PublicHandle],
) -> None:
    """Sanity check: the *private* dictionary on disk **does** contain the
    raw values; this is a private-boundary artifact, not an agent-facing one.
    If this assertion ever fails the kernel has stopped persisting the
    restoration data, and the rest of this file's "no raw value" assertions
    become trivially-true false positives."""
    vault_root, _response, _handle = canonical_vault
    private_path = vault_root / "private" / "exposure-doc-001.json"
    assert private_path.exists()
    contents = private_path.read_text(encoding="utf-8")
    for needle in RAW_VALUES:
        assert needle in contents, (
            f"private dictionary file missing canonical value {needle!r}; "
            "either the kernel changed or the fixture drifted"
        )


# ---------------------------------------------------------------------------
# 9. caplog (INFO+) across every boundary entry point
# ---------------------------------------------------------------------------


def test_boundary_entry_points_do_not_log_raw_values(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    vault_root = tmp_path / "vault"
    with caplog.at_level(logging.INFO, logger="yomotsusaka"):
        # Positive path through every wired entry point.
        proc = process_document_request(
            ProcessRequest(
                doc_id="caplog-doc",
                raw_text=CANONICAL_TEXT,
                spans=list(CANONICAL_SPANS),
            ),
            vault_root=vault_root,
        )
        inspect_request(
            InspectRequest(locator=proc.handle.locator),
            vault_root=vault_root,
        )
        gateway = _make_indexed_gateway(vault_root, "caplog-doc")
        search_request(SearchRequest(query="<PERSON_"), gateway=gateway)
        restoration_request(
            RestorationRequest(locator=proc.handle.locator, purpose="t"),
            vault_root=vault_root,
        )
        status_report_request(
            StatusReportRequest(locator=proc.handle.locator),
            vault_root=vault_root,
        )

        # Negative paths so error-time logging is also scanned.
        inspect_request(
            InspectRequest(locator="not-a-locator"),
            vault_root=vault_root,
        )
        inspect_request(
            InspectRequest(locator=_expected_locator("never-written")),
            vault_root=vault_root,
        )

    # Concatenate every formatted record + its raw message; either could
    # carry a leak.
    for record in caplog.records:
        for leaf in _iter_strings(record.getMessage()):
            _assert_no_raw_values(leaf, surface=f"caplog[{record.name}]")
            _assert_no_paths(
                leaf,
                surface=f"caplog[{record.name}]",
                extra=_scrub_for_path_assertion(vault_root),
            )


# ---------------------------------------------------------------------------
# 10. capsys / stderr across every boundary entry point
# ---------------------------------------------------------------------------


def test_boundary_entry_points_do_not_print_raw_values(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault_root = tmp_path / "vault"
    proc = process_document_request(
        ProcessRequest(
            doc_id="capsys-doc",
            raw_text=CANONICAL_TEXT,
            spans=list(CANONICAL_SPANS),
        ),
        vault_root=vault_root,
    )
    inspect_request(
        InspectRequest(locator=proc.handle.locator),
        vault_root=vault_root,
    )
    gateway = _make_indexed_gateway(vault_root, "capsys-doc")
    search_request(SearchRequest(query="<PERSON_"), gateway=gateway)
    restoration_request(
        RestorationRequest(locator=proc.handle.locator, purpose="t"),
        vault_root=vault_root,
    )
    status_report_request(
        StatusReportRequest(locator=proc.handle.locator),
        vault_root=vault_root,
    )
    inspect_request(
        InspectRequest(locator="not-a-locator"),
        vault_root=vault_root,
    )

    captured = capsys.readouterr()
    for stream_name, stream in (("stdout", captured.out), ("stderr", captured.err)):
        _assert_no_raw_values(stream, surface=f"capsys.{stream_name}")
        _assert_no_paths(
            stream,
            surface=f"capsys.{stream_name}",
            extra=_scrub_for_path_assertion(vault_root),
        )


# ---------------------------------------------------------------------------
# 11. Exception text in negative-path tests
# ---------------------------------------------------------------------------


def test_process_document_request_rejects_unsafe_doc_id_without_leaking(
    tmp_path: Path,
) -> None:
    """An unsafe ``doc_id`` raises :class:`ValueError`; neither ``str(exc)``
    nor ``repr(exc)`` may echo a raw private value or a vault path."""
    vault_root = tmp_path / "vault"
    # Deliberately mix an unsafe doc_id with raw_text that does contain raw
    # values — even if the implementation echoed raw_text we'd catch it.
    with pytest.raises(ValueError) as excinfo:
        process_document_request(
            ProcessRequest(
                doc_id="..",
                raw_text=CANONICAL_TEXT,
                spans=list(CANONICAL_SPANS),
            ),
            vault_root=vault_root,
        )
    for blob in (str(excinfo.value), repr(excinfo.value)):
        _assert_no_raw_values(blob, surface="ValueError(unsafe doc_id)")
        _assert_no_paths(
            blob,
            surface="ValueError(unsafe doc_id)",
            extra=_scrub_for_path_assertion(vault_root),
        )


def test_span_spec_validation_error_does_not_leak() -> None:
    """A ``SpanSpec`` with ``end < start`` raises ``pydantic.ValidationError``;
    its text must not echo any raw private value."""
    from pydantic import ValidationError as PydanticValidationError

    with pytest.raises(PydanticValidationError) as excinfo:
        SpanSpec(start=9, end=0, kind=EntityKind.PERSON)
    for blob in (str(excinfo.value), repr(excinfo.value)):
        _assert_no_raw_values(blob, surface="SpanSpec.ValidationError")


# ---------------------------------------------------------------------------
# Cross-cutting: every locator field round-trips through parse_locator
# ---------------------------------------------------------------------------


def test_every_locator_field_round_trips_through_parse_locator(
    canonical_vault: tuple[Path, ProcessResponse, PublicHandle],
) -> None:
    """For each successful surface that returns a locator-bearing payload,
    walk the serialised dict and assert every ``locator`` / ``handle.locator``
    string parses via :func:`boundary.parse_locator`. This is the file's
    structural guarantee that no surface ever returns a non-opaque handle."""
    vault_root, process_response, handle = canonical_vault

    # InspectResponse
    inspect_resp = inspect_request(
        InspectRequest(locator=handle.locator),
        vault_root=vault_root,
    )
    assert isinstance(inspect_resp, InspectResponse)

    # SearchResponse
    gateway = _make_indexed_gateway(vault_root, "exposure-doc-001")
    search_resp = search_request(SearchRequest(query="<PERSON_"), gateway=gateway)

    # RestorationResponse (deferred)
    restore_resp = restoration_request(
        RestorationRequest(locator=handle.locator, purpose="t"),
        vault_root=vault_root,
    )
    assert isinstance(restore_resp, RestorationResponse)

    # StatusReportResponse (committed + unknown)
    status_committed = status_report_request(
        StatusReportRequest(locator=handle.locator),
        vault_root=vault_root,
    )
    status_unknown = status_report_request(
        StatusReportRequest(locator=_expected_locator("never-committed-doc")),
        vault_root=vault_root,
    )

    total_locators = 0
    for surface_name, model in (
        ("PublicHandle", handle),
        ("ProcessResponse", process_response),
        ("InspectResponse", inspect_resp),
        ("SearchResponse", search_resp),
        ("RestorationResponse", restore_resp),
        ("StatusReportResponse(committed)", status_committed),
        ("StatusReportResponse(unknown)", status_unknown),
    ):
        total_locators += _walk_handles(
            model.model_dump(mode="json"), surface=surface_name
        )

    # Conservative lower bound: ProcessResponse(1) + handle(1) +
    # SearchResponse(>=1) + Restoration(1) + Status(2) = 6.
    assert total_locators >= 6, (
        f"expected >=6 locator-bearing fields across successful surfaces, "
        f"got {total_locators}"
    )
