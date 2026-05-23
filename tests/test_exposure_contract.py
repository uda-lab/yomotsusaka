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
from yomotsusaka.redactor import Span, redact
from yomotsusaka.schemas import DocumentManifest, EntityKind
from yomotsusaka.search_gateway import QueryResolver, SearchGateway

from tests._exposure_denylist import (
    CANONICAL_SPANS,
    CANONICAL_TEXT,
    PATH_LEAK_PATTERNS,
    RAW_VALUES,
)


# ---------------------------------------------------------------------------
# Canonical fixture (umbrella #4) — imported from :mod:`tests._exposure_denylist`
# so the MVP-2 scan and the MVP-3 widening share a single source of truth.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Drift guard against boundary.__all__
# ---------------------------------------------------------------------------

# Every public response type whose serialisation flows through this scan.
# If boundary.__all__ ever grows a new response, this set must grow to match
# (or the new response must be added to one of the per-surface tests below).
# Names this scan covers (responses + supporting types + entry points).
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
        "ResolverSuccess",
        "PrivateState",
        "ExecutionResponse",
        "parse_locator",
        "build_locator",
        "process_document_request",
        "inspect_request",
        "search_request",
        "restoration_request",
        "status_report_request",
        "execute_request",
        "resolve",
    }
)

# Names that are intentionally exported by ``boundary.__all__`` but are
# *not* themselves agent-facing responses (locator-grammar constants,
# request models, exception types). These do not need a per-surface scan
# but are listed here so the bidirectional drift guard recognises them.
_KNOWN_NON_RESPONSE_EXPORTS: frozenset[str] = frozenset(
    {
        "LOCATOR_SCHEME",
        "EXPOSURE_CLASSES",
        "ARTIFACT_KINDS",
        "ParsedLocator",
        "SpanSpec",
        "ResolverError",
        "ProcessRequest",
        "InspectRequest",
        "SearchRequest",
        "RestorationRequest",
        "RestorationFailureReason",
        "StatusReportRequest",
        # ExecutionFailure is the Chikaeshi exception type (#42) — reserved
        # for programmer-error paths, not raised by execute_request. Not
        # a response shape; classified here so the drift guard recognises
        # the boundary re-export added by #43.
        "ExecutionFailure",
    }
)


def test_expected_boundary_symbols_are_a_subset_of_module_all() -> None:
    """Direction 1: every name this scan claims to cover must still be
    exported by ``boundary.__all__``. Fires when a maintainer removes or
    renames a response without updating the scan."""
    module_all = set(boundary.__all__)
    missing = EXPECTED_BOUNDARY_SYMBOLS - module_all
    assert not missing, (
        f"EXPECTED_BOUNDARY_SYMBOLS contains names not in boundary.__all__: {missing!r}; "
        "either remove them from this test's expected set or restore them on the boundary."
    )


def test_no_new_unscanned_symbols_in_boundary_all() -> None:
    """Direction 2: every name in ``boundary.__all__`` must be either in
    ``EXPECTED_BOUNDARY_SYMBOLS`` (covered by the scan) or in
    ``_KNOWN_NON_RESPONSE_EXPORTS`` (explicitly classified as non-response).
    This is the §29-spec "force a maintainer to add new responses to the
    scan" direction: a freshly-added ``BatchResponse`` in ``boundary.__all__``
    fails this test until the maintainer either scans it or declares it a
    non-response export."""
    module_all = set(boundary.__all__)
    unclassified = module_all - EXPECTED_BOUNDARY_SYMBOLS - _KNOWN_NON_RESPONSE_EXPORTS
    assert not unclassified, (
        f"boundary.__all__ exports symbols this scan has not classified: "
        f"{sorted(unclassified)!r}. Either add the name to "
        "EXPECTED_BOUNDARY_SYMBOLS and write a per-surface test, or add it "
        "to _KNOWN_NON_RESPONSE_EXPORTS with a justification."
    )


# Kernel-symbol prefixes and exact names that must never be re-exported from
# ``boundary.__all__``. The prefix check catches anything a future MVP-3
# backend PR might tempt-import for convenience (``_kernel_*``, ``_private_*``);
# the exact-name check catches private-kernel datatypes whose names do not
# carry an underscore prefix (e.g. ``PodHandle``, ``VLLMResponse``,
# ``PodConfig``) — these belong vault-side and must surface only as opaque
# locators on the agent-facing boundary.
_PRIVATE_KERNEL_PREFIX_DENYLIST: tuple[str, ...] = (
    "_kernel_",
    "_private_",
)

_PRIVATE_KERNEL_EXACT_DENYLIST: frozenset[str] = frozenset(
    {
        # RunPod lifecycle internals — vault-side; #46 must not lift these.
        "PodHandle",
        "PodConfig",
        "RunPodLifecycle",
        # #47 handshake symbol — the attach-style class lives in
        # ``runpod_lifecycle`` (vault-side) and must surface only via an
        # opaque locator. Re-exporting through ``boundary.__all__`` would
        # widen the agent-facing surface without going through the
        # ``EXPECTED_BOUNDARY_SYMBOLS`` review path.
        "AttachRunPodLifecycle",
        # vLLM backend internals — vault-side; #46 must not lift these.
        "VLLMBackend",
        "VLLMResponse",
        # Execution-gateway internals — vault-side; #42/#43 must not lift these.
        "ExecutionGateway",
        # #47 handshake symbol — landed for real by #42 in
        # ``execution_gateway.__all__``; that module is its legitimate
        # home. ``boundary.__all__`` must not also re-export it, since
        # the agent-facing execution surface ships through #43's
        # ``execute_request`` entry point, not through a bare model name.
        "ExecutionRequest",
        # Schemas that intentionally carry private-side bytes.
        "PrivateDictEntry",
        "ArtifactHandle",
    }
)


def test_boundary_all_does_not_export_private_kernel_symbols() -> None:
    """Second drift guard (issue #47): ``boundary.__all__`` must not grow
    re-exports of private-kernel symbols introduced by MVP-3 backends.

    The §5 non-weakening clause forbids backend PRs from "lifting" a
    private-kernel datatype (e.g. ``PodHandle``, ``VLLMResponse``,
    ``ExecutionGateway``) onto the agent-facing surface as a shortcut. Such
    a re-export would bypass the per-surface leakage scans by sneaking a
    raw-bearing dataclass into the public namespace.

    This guard fires the moment any MVP-3 PR (or any later PR) adds a name
    to ``boundary.__all__`` that either:

    1. starts with a ``_kernel_`` / ``_private_`` prefix, or
    2. matches a known-private-kernel exact name from
       :data:`_PRIVATE_KERNEL_EXACT_DENYLIST`.

    If a future refactor legitimately needs to expose one of the listed
    names through ``boundary``, the deny-list itself must be edited in a
    separate, independently-reviewed PR — not in the same backend
    integration PR (per §5 of ``docs/backend-promotion.md``).
    """
    module_all = set(boundary.__all__)

    prefix_violations = sorted(
        name
        for name in module_all
        if any(name.startswith(prefix) for prefix in _PRIVATE_KERNEL_PREFIX_DENYLIST)
    )
    assert not prefix_violations, (
        "boundary.__all__ re-exports private-kernel symbol(s) matching a "
        f"deny-list prefix: {prefix_violations!r}. Per §5 non-weakening "
        "clause in docs/backend-promotion.md, kernel-prefixed names must "
        "stay vault-side; remove the re-export and surface the value via an "
        "opaque locator instead."
    )

    exact_violations = sorted(module_all & _PRIVATE_KERNEL_EXACT_DENYLIST)
    assert not exact_violations, (
        "boundary.__all__ re-exports private-kernel datatype(s): "
        f"{exact_violations!r}. These names carry raw private values or "
        "vault paths by design; per §5 non-weakening clause in "
        "docs/backend-promotion.md, the boundary must expose only an opaque "
        "locator. If this exposure is genuinely needed, propose the "
        "deny-list edit in a separate, independently-reviewed PR."
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
    """Walk *payload* (a JSON-mode dict) and check every ``locator`` string
    parses via :func:`parse_locator`. Returns the count of locator strings
    validated, so the caller can assert ``count >= 1`` for surfaces that
    must carry one.

    The walker counts each locator exactly once: a ``handle`` sub-dict's
    locator is recognised through the generic ``"locator"`` key during
    recursion, so we do not need a special-case branch for ``"handle"``.
    """
    count = 0
    if isinstance(payload, dict):
        for k, v in payload.items():
            if k == "locator" and isinstance(v, str):
                _assert_locator_shape(v, surface=surface)
                count += 1
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


def _make_resolver_indexed_gateway(
    vault_root: Path, doc_id: str
) -> SearchGateway:
    """Build a :class:`SearchGateway` with a populated :class:`QueryResolver`.

    Reproduces the canonical pipeline's redaction to recover the same
    :class:`PrivateDictEntry` objects an in-process caller would have
    handed the resolver at index time. This is the resolver-attached
    twin of :func:`_make_indexed_gateway`.
    """
    kernel_spans = [Span(start=s.start, end=s.end, kind=s.kind) for s in CANONICAL_SPANS]
    _, _, private_dict = redact(CANONICAL_TEXT, kernel_spans)
    manifest = DocumentManifest.model_validate_json(
        (vault_root / "manifests" / f"{doc_id}.json").read_text(encoding="utf-8")
    )
    resolver = QueryResolver()
    gateway = SearchGateway(query_resolver=resolver)
    gateway.index(manifest, private_entries=private_dict)
    return gateway


def test_search_response_with_resolver_translates_raw_query_without_leak(
    canonical_vault: tuple[Path, ProcessResponse, PublicHandle],
) -> None:
    """Resolver-attached path (#48 / architecture.md §12.3): when a raw
    private value matches a registered entry, the gateway returns a hit
    via the *translated* key — but the serialised response body still
    must NOT echo the raw value anywhere (snippet, handle, label, or
    any nested field). This is the resolver-side counterpart to the
    pre-resolver zero-hit leakage check above."""
    vault_root, _response, _handle = canonical_vault
    gateway = _make_resolver_indexed_gateway(vault_root, "exposure-doc-001")

    for needle in RAW_VALUES:
        response = search_request(SearchRequest(query=needle), gateway=gateway)
        # With the resolver attached, the raw value DOES match (via the
        # translated key); the privacy invariant is that the response
        # never echoes the raw value, not that hits are absent.
        assert isinstance(response, SearchResponse)
        for blob in _both_json_renders(response):
            _assert_no_raw_values(
                blob,
                surface=f"SearchResponse(resolver,query={needle!r})",
            )
            _assert_no_paths(
                blob,
                surface=f"SearchResponse(resolver,query={needle!r})",
                extra=_scrub_for_path_assertion(vault_root),
            )
        # Recursively sweep every string leaf of the JSON-mode payload
        # for raw values too — this would catch a leak via a key name
        # (in case a future field used a raw value as a dict key).
        for leaf in _iter_strings(response.model_dump(mode="json")):
            _assert_no_raw_values(
                leaf,
                surface=f"SearchResponse.leaf(resolver,query={needle!r})",
            )


# ---------------------------------------------------------------------------
# 5. RestorationResponse (deferred stub; parameterised on outcome)
# ---------------------------------------------------------------------------


def _exposure_restoration_request(handle: PublicHandle) -> RestorationRequest:
    from datetime import datetime, timezone

    return RestorationRequest(
        caller_label="exposure-contract-test",
        reason="exposure-contract-test",
        timestamp=datetime.now(timezone.utc),
        target_public_handle=handle,
        requested_entity_kinds=[EntityKind.PERSON],
    )


def test_restoration_response_serialisation_is_opaque(
    canonical_vault: tuple[Path, ProcessResponse, PublicHandle],
) -> None:
    vault_root, _response, handle = canonical_vault
    response = restoration_request(
        _exposure_restoration_request(handle),
        scope=ResolverScope.ORDINARY_AGENT,
        vault_root=vault_root,
    )
    # MVP-2 with #27 always returns one of the well-typed outcomes
    # (``accepted`` / ``accepted_but_redacted`` / ``failed``); the
    # ordinary-agent scope here yields ``failed`` with ``scope_denied``.
    # The serialised-blob scans below are the real privacy invariant; the
    # outcome value itself is whatever the boundary chose to emit.
    assert isinstance(response, RestorationResponse)

    for blob in _both_json_renders(response):
        _assert_no_raw_values(blob, surface="RestorationResponse")
        _assert_no_paths(
            blob,
            surface="RestorationResponse",
            extra=_scrub_for_path_assertion(vault_root),
        )

    # The new RestorationResponse intentionally does not carry a public
    # locator field (it carries `document_id` and the redacted handle is
    # already in the audit log). No `count >= 1` assertion here; the
    # locator round-trip test below excludes RestorationResponse for the
    # same reason.


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


def test_resolver_failure_scope_denied_no_leak(
    canonical_vault: tuple[Path, ProcessResponse, PublicHandle],
) -> None:
    """Issue #66 (absorbed by #75): pin the leak-scan invariants on the
    two restoration-policy denial paths.

    Two complementary paths land the caller in a structured
    ``RestorationResponse(outcome="failed", ...)`` denial:

    1. **Scope-gate denial.** A non-``PRIVATE_BOUNDARY`` scope is rejected
       by the boundary's scope gate (step (c) in
       :func:`restoration_request`) before the policy table runs;
       ``reason`` is :data:`RestorationFailureReason.ScopeDenied`. The
       canonical fixture's
       ``test_resolver_failure_purpose_not_permitted_is_opaque`` already
       exercises this path implicitly, but issue #66 calls for an
       explicit standalone assertion so the test name encodes the
       contract.
    2. **Policy-table denial.** A ``PRIVATE_BOUNDARY``-scoped request
       carrying an unknown ``policy_profile`` is rejected by the policy
       table (deny-by-default per :class:`RestorationPolicyTable`
       semantics); ``reason`` is
       :data:`RestorationFailureReason.PolicyDenied`. The detail string
       contains the unknown profile name verbatim (audit-visible) but
       must not echo raw private values or vault-shaped paths.

    Both paths share the same leak-scan invariant: the serialised
    response must carry no raw private value and no vault-shaped path.
    The previous skip in this slot (``"MVP-2 accepts all scopes; see #27
    for policy-gated ScopeDenied"``) was stale — #27 has landed and
    :class:`RestorationPolicyTable` is the policy table referenced
    there.
    """
    from yomotsusaka.boundary import RestorationFailureReason
    from yomotsusaka.policy import (
        RestorationPolicyRow,
        RestorationPolicyTable,
    )

    vault_root, _process_response, handle = canonical_vault

    # ---- Path 1: scope gate denial under ORDINARY_AGENT.
    # A strict policy table is constructed locally so the test pins the
    # gate ordering: the scope gate fires BEFORE the policy table is
    # consulted, so the policy table's `require_authorization_decision`
    # is irrelevant to this branch. (If the gate ordering ever flips, the
    # reason changes to PolicyDenied and the assertion below catches it.)
    strict_table = RestorationPolicyTable(
        [
            RestorationPolicyRow(
                profile_name="strict-production",
                production_scopes=["production"],
                require_authorization_decision=True,
                approval_ticket_pattern=None,
                default=True,
            )
        ]
    )
    scope_failure = restoration_request(
        _exposure_restoration_request(handle),
        scope=ResolverScope.ORDINARY_AGENT,
        vault_root=vault_root,
        policy_table=strict_table,
    )
    assert isinstance(scope_failure, RestorationResponse)
    assert scope_failure.outcome == "failed"
    assert scope_failure.reason is RestorationFailureReason.ScopeDenied
    for blob in _both_json_renders(scope_failure):
        _assert_no_raw_values(
            blob, surface="RestorationResponse.ScopeDenied"
        )
        _assert_no_paths(
            blob,
            surface="RestorationResponse.ScopeDenied",
            extra=_scrub_for_path_assertion(vault_root),
        )

    # ---- Path 2: policy-table denial under PRIVATE_BOUNDARY.
    # ``PRIVATE_BOUNDARY`` clears the scope gate; the request then names
    # an unknown ``policy_profile``, which the table denies by default
    # (``route_unknown_profile_to_default`` is ``False`` on user-loaded
    # tables — see :meth:`RestorationPolicyTable.__init__`). The
    # resulting ``RestorationResponse`` carries ``PolicyDenied`` and a
    # detail string that names the unknown profile — privacy-discipline
    # requires that the detail nevertheless carries no raw value and no
    # vault-shaped path.
    base_request = _exposure_restoration_request(handle)
    policy_denied_request = RestorationRequest(
        caller_label=base_request.caller_label,
        reason=base_request.reason,
        timestamp=base_request.timestamp,
        target_public_handle=base_request.target_public_handle,
        requested_entity_kinds=list(base_request.requested_entity_kinds),
        policy_profile="profile-does-not-exist",
    )
    policy_failure = restoration_request(
        policy_denied_request,
        scope=ResolverScope.PRIVATE_BOUNDARY,
        vault_root=vault_root,
        policy_table=strict_table,
    )
    assert isinstance(policy_failure, RestorationResponse)
    assert policy_failure.outcome == "failed"
    assert policy_failure.reason is RestorationFailureReason.PolicyDenied
    for blob in _both_json_renders(policy_failure):
        _assert_no_raw_values(
            blob, surface="RestorationResponse.PolicyDenied"
        )
        _assert_no_paths(
            blob,
            surface="RestorationResponse.PolicyDenied",
            extra=_scrub_for_path_assertion(vault_root),
        )


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

    # Also exercise the restoration_request scope-denied path, whose
    # response is a different failure variant but must satisfy the same
    # leak-scan invariants.
    from yomotsusaka.boundary import RestorationFailureReason

    restore_failure = restoration_request(
        _exposure_restoration_request(
            PublicHandle(locator=_expected_locator("any-id"))
        ),
        scope=ResolverScope.ORDINARY_AGENT,
        vault_root=vault_root,
    )
    assert isinstance(restore_failure, RestorationResponse)
    assert restore_failure.outcome == "failed"
    assert restore_failure.reason is RestorationFailureReason.ScopeDenied
    for blob in _both_json_renders(restore_failure):
        _assert_no_raw_values(
            blob, surface="restoration_request.ScopeDenied"
        )
        _assert_no_paths(
            blob,
            surface="restoration_request.ScopeDenied",
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
# ResolverSuccess: ordinary-agent scope vs. PRIVATE_BOUNDARY scope
# ---------------------------------------------------------------------------


def test_resolver_success_under_ordinary_scope_omits_private_state(
    canonical_vault: tuple[Path, ProcessResponse, PublicHandle],
) -> None:
    """:func:`resolve` under :data:`ResolverScope.ORDINARY_AGENT` must return
    a :class:`ResolverSuccess` whose ``private_state`` is ``None`` and whose
    serialisation carries no raw private values or vault paths."""
    from yomotsusaka.boundary import ResolverSuccess

    vault_root, _process_response, handle = canonical_vault
    outcome = resolve(
        handle.locator,
        scope=ResolverScope.ORDINARY_AGENT,
        purpose="exposure-contract",
        vault_root=vault_root,
    )
    assert isinstance(outcome, ResolverSuccess)
    assert outcome.private_state is None, (
        "ORDINARY_AGENT scope must never populate PrivateState"
    )

    for blob in _both_json_renders(outcome):
        _assert_no_raw_values(blob, surface="ResolverSuccess(ORDINARY_AGENT)")
        _assert_no_paths(
            blob,
            surface="ResolverSuccess(ORDINARY_AGENT)",
            extra=_scrub_for_path_assertion(vault_root),
        )

    # AUDIT_REVIEWER is also a non-private scope; the same invariant holds.
    audit_outcome = resolve(
        handle.locator,
        scope=ResolverScope.AUDIT_REVIEWER,
        purpose="exposure-contract",
        vault_root=vault_root,
    )
    assert isinstance(audit_outcome, ResolverSuccess)
    assert audit_outcome.private_state is None, (
        "AUDIT_REVIEWER scope must never populate PrivateState"
    )
    for blob in _both_json_renders(audit_outcome):
        _assert_no_raw_values(blob, surface="ResolverSuccess(AUDIT_REVIEWER)")
        _assert_no_paths(
            blob,
            surface="ResolverSuccess(AUDIT_REVIEWER)",
            extra=_scrub_for_path_assertion(vault_root),
        )


def test_resolver_success_under_private_boundary_scope_carries_private_state(
    canonical_vault: tuple[Path, ProcessResponse, PublicHandle],
) -> None:
    """:func:`resolve` under :data:`ResolverScope.PRIVATE_BOUNDARY` is the
    one entry point that materialises raw private values, by design (see
    architecture.md §5.7.2). This test documents that fact and pins the
    invariant that *no other code in this file* serialises a
    ``ResolverSuccess`` carrying ``PrivateState``: such a serialisation
    contains raw private values and must never reach an agent-facing
    surface. This is the inverse of every other test in the file — it
    expects the leak, because the leak is the contract."""
    from yomotsusaka.boundary import PrivateState, ResolverSuccess

    vault_root, _process_response, handle = canonical_vault
    outcome = resolve(
        handle.locator,
        scope=ResolverScope.PRIVATE_BOUNDARY,
        purpose="exposure-contract",
        vault_root=vault_root,
    )
    assert isinstance(outcome, ResolverSuccess)
    assert isinstance(outcome.private_state, PrivateState)
    # Sanity: the materialised PrivateState DOES carry raw values (because
    # that is the point of PRIVATE_BOUNDARY scope). If this assertion ever
    # fails the kernel has stopped passing raw values through resolve(),
    # which would make every "no raw value" assertion below trivially true.
    blob = outcome.model_dump_json()
    leaked_values = [v for v in RAW_VALUES if v in blob]
    assert leaked_values, (
        "ResolverSuccess(PRIVATE_BOUNDARY) is expected to carry raw private "
        f"values in its serialisation; saw none of {RAW_VALUES!r}"
    )

    # And confirm that the public-facing helper we use elsewhere (resolve
    # under ORDINARY_AGENT) does NOT leak these same values. This is the
    # contract: scope is the gate.
    public_outcome = resolve(
        handle.locator,
        scope=ResolverScope.ORDINARY_AGENT,
        purpose="exposure-contract",
        vault_root=vault_root,
    )
    assert isinstance(public_outcome, ResolverSuccess)
    for blob in _both_json_renders(public_outcome):
        _assert_no_raw_values(
            blob, surface="ResolverSuccess(ORDINARY_AGENT vs PRIVATE_BOUNDARY)"
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
            _exposure_restoration_request(proc.handle),
            scope=ResolverScope.ORDINARY_AGENT,
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

    # Scan the formatted message + any exception traceback text. A
    # ``logger.exception()`` call lands the traceback in ``record.exc_text``
    # (not in ``getMessage()``); ValidationError reprs and OSError messages
    # can carry private input fragments or paths.
    for record in caplog.records:
        candidates: list[str] = [record.getMessage()]
        if record.exc_text:
            candidates.append(record.exc_text)
        for blob in candidates:
            for leaf in _iter_strings(blob):
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
        _exposure_restoration_request(proc.handle),
        scope=ResolverScope.ORDINARY_AGENT,
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

    # RestorationResponse (scope-denied via ordinary-agent path)
    restore_resp = restoration_request(
        _exposure_restoration_request(handle),
        scope=ResolverScope.ORDINARY_AGENT,
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
        # RestorationResponse intentionally does NOT carry a public locator
        # field under the #27 audit-logged contract — the response carries
        # `document_id`, `audit_record_id`, and (for `accepted`)
        # `private_entries`; the public handle is in the audit log, not the
        # response body. Walking it would assert ``count >= 1`` for a
        # surface that legitimately has no locator string, so we omit it.
        ("StatusReportResponse(committed)", status_committed),
        ("StatusReportResponse(unknown)", status_unknown),
    ):
        total_locators += _walk_handles(
            model.model_dump(mode="json"), surface=surface_name
        )

    # Conservative lower bound: ProcessResponse(1) + handle(1) +
    # SearchResponse(>=1) + Status(2) = 5.
    assert total_locators >= 5, (
        f"expected >=5 locator-bearing fields across successful surfaces, "
        f"got {total_locators}"
    )
    # Sanity: the omitted RestorationResponse still must not leak raw
    # values or vault paths (asserted elsewhere); the omission is purely
    # structural.
    assert isinstance(restore_resp, RestorationResponse)
