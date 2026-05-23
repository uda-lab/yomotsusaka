"""Request/response tests for the five MVP-2 boundary operations.

Pins the agent-facing contract for:

* :func:`process_document_request` — drives raw_text through the kernel and
  returns only a public handle (no vault_path).
* :func:`inspect_request` — returns a public manifest view stripped of
  ``source_ref``.
* :func:`search_request` — returns public ``SearchHit``s built from already-
  redacted manifest text.
* :func:`status_report_request` — shape-only stub mapping to
  ``"committed"`` / ``"unknown"``.

Per AGENTS.md, raw private literals appear here only inside the canonical
fixture body and the documented private-dictionary-style absence
assertions against public outputs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from yomotsusaka import restoration_api
from yomotsusaka.boundary import (
    InspectRequest,
    InspectResponse,
    ProcessRequest,
    ProcessResponse,
    PublicHandle,
    PublicManifestView,
    ResolverFailure,
    ResolverFailureReason,
    SearchHit,
    SearchRequest,
    SearchResponse,
    SpanSpec,
    StatusReportRequest,
    StatusReportResponse,
    build_locator,
    inspect_request,
    process_document_request,
    search_request,
    status_report_request,
)
from yomotsusaka.redactor import Span, redact
from yomotsusaka.schemas import ArtifactHandle, DocumentManifest, EntityKind
from yomotsusaka.search_gateway import QueryResolver, SearchGateway


_RAW_TEXT = "Alice Tan works at Acme Corp. Patient ID: 12345."
_RAW_NEEDLES = ("Alice Tan", "Acme Corp", "12345")


@dataclass(frozen=True)
class _SpanSpec:
    start: int
    end: int
    kind: EntityKind


_CANONICAL_SPAN_SPECS: tuple[_SpanSpec, ...] = (
    _SpanSpec(start=0, end=9, kind=EntityKind.PERSON),
    _SpanSpec(start=19, end=28, kind=EntityKind.ORG),
    _SpanSpec(start=42, end=47, kind=EntityKind.ID_NUMBER),
)


def _public_spans() -> list[SpanSpec]:
    return [SpanSpec(start=s.start, end=s.end, kind=s.kind) for s in _CANONICAL_SPAN_SPECS]


def _process_canonical(vault_root: Path, doc_id: str = "canonical-fixture-001") -> ProcessResponse:
    return process_document_request(
        ProcessRequest(doc_id=doc_id, raw_text=_RAW_TEXT, spans=_public_spans()),
        vault_root=vault_root,
    )


def _expected_locator(doc_id: str) -> str:
    return build_locator(
        exposure_class="agent_redacted",
        artifact_kind="manifest",
        opaque_id=doc_id,
    )


# ---------------------------------------------------------------------------
# process_document_request
# ---------------------------------------------------------------------------


def test_process_document_request_returns_only_public_handle(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    doc_id = "canonical-fixture-001"

    response = _process_canonical(vault_root, doc_id=doc_id)

    assert isinstance(response, ProcessResponse)
    assert isinstance(response.handle, PublicHandle)
    assert response.handle.locator == _expected_locator(doc_id)

    # Serialised response carries no internal vault_path, no ArtifactHandle
    # fields, and no raw private value.
    as_json = response.model_dump_json()
    assert "vault_path" not in as_json
    assert "handle_id" not in as_json
    for needle in _RAW_NEEDLES:
        assert needle not in as_json


def test_process_document_request_writes_kernel_artifacts(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    doc_id = "canonical-fixture-002"

    _process_canonical(vault_root, doc_id=doc_id)

    # Kernel still wrote both sides of the vault layout.
    assert (vault_root / "manifests" / f"{doc_id}.json").exists()
    assert (vault_root / "private" / f"{doc_id}.json").exists()


@pytest.mark.parametrize("bad_doc_id", [".", "..", "has space", "has/slash", ""])
def test_process_document_request_rejects_unsafe_doc_id(
    tmp_path: Path, bad_doc_id: str
) -> None:
    """A doc_id that violates the locator grammar must be rejected at the
    boundary before any vault write — no orphaned manifest, no orphaned
    private dict."""
    vault_root = tmp_path / "vault"
    with pytest.raises(ValueError):
        process_document_request(
            ProcessRequest(doc_id=bad_doc_id, raw_text="x", spans=[]),
            vault_root=vault_root,
        )
    # Nothing must have been written for a rejected doc_id.
    assert not (vault_root / "manifests").exists()
    assert not (vault_root / "private").exists()


# ---------------------------------------------------------------------------
# inspect_request
# ---------------------------------------------------------------------------


def test_inspect_request_returns_public_view_without_source_ref(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "vault"
    doc_id = "inspect-doc-001"

    process_response = _process_canonical(vault_root, doc_id=doc_id)
    response = inspect_request(
        InspectRequest(locator=process_response.handle.locator),
        vault_root=vault_root,
    )

    assert isinstance(response, InspectResponse)
    manifest_view = response.manifest
    assert isinstance(manifest_view, PublicManifestView)

    # Acceptance criterion (metaplan §Done): field set is exactly the public
    # subset of DocumentManifest, with no source_ref.
    expected_fields = {
        "doc_id",
        "redacted_text",
        "entities",
        "labels",
        "summary",
        "created_at",
        "metadata",
    }
    assert set(PublicManifestView.model_fields.keys()) == expected_fields
    assert "source_ref" not in PublicManifestView.model_fields

    # No raw private value reaches the serialised public view.
    blob = manifest_view.model_dump_json()
    for needle in _RAW_NEEDLES:
        assert needle not in blob


def test_inspect_request_returns_resolver_failure_for_unknown_artifact(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "vault"
    response = inspect_request(
        InspectRequest(locator=_expected_locator("never-committed")),
        vault_root=vault_root,
    )
    assert isinstance(response, ResolverFailure)
    assert response.reason is ResolverFailureReason.UnknownArtifact


def test_inspect_request_returns_resolver_failure_for_malformed_locator(
    tmp_path: Path,
) -> None:
    response = inspect_request(
        InspectRequest(locator="not-a-locator"),
        vault_root=tmp_path,
    )
    assert isinstance(response, ResolverFailure)
    assert response.reason is ResolverFailureReason.MalformedLocator


# ---------------------------------------------------------------------------
# search_request
# ---------------------------------------------------------------------------


def test_search_request_returns_only_public_fields(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    doc_id = "search-doc-001"

    _process_canonical(vault_root, doc_id=doc_id)

    # Build a gateway indexed from the committed redacted manifest on disk.
    manifest = DocumentManifest.model_validate_json(
        (vault_root / "manifests" / f"{doc_id}.json").read_text(encoding="utf-8")
    )
    gateway = SearchGateway()
    gateway.index(manifest)

    # Searching for a placeholder prefix finds the manifest.
    response = search_request(SearchRequest(query="<PERSON_"), gateway=gateway)
    assert isinstance(response, SearchResponse)
    assert len(response.hits) == 1
    hit = response.hits[0]
    assert isinstance(hit, SearchHit)

    # SearchHit field set is exactly {handle, redacted_snippet, labels}.
    assert set(SearchHit.model_fields.keys()) == {"handle", "redacted_snippet", "labels"}
    assert hit.handle.locator == _expected_locator(doc_id)
    assert "<PERSON_" in hit.redacted_snippet
    # Snippet sweep: no raw private values.
    blob = response.model_dump_json()
    for needle in _RAW_NEEDLES:
        assert needle not in blob, f"search response leaked {needle!r}"


def test_search_request_raw_value_queries_return_zero_hits(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    doc_id = "search-doc-zero"

    _process_canonical(vault_root, doc_id=doc_id)

    manifest = DocumentManifest.model_validate_json(
        (vault_root / "manifests" / f"{doc_id}.json").read_text(encoding="utf-8")
    )
    gateway = SearchGateway()
    gateway.index(manifest)

    for needle in _RAW_NEEDLES:
        response = search_request(SearchRequest(query=needle), gateway=gateway)
        # Privacy invariant: querying with a raw private value finds nothing,
        # because the gateway only sees redacted manifests.
        assert response.hits == [], (
            f"search returned hits for raw private value {needle!r}; "
            "private values must not be findable through the public boundary"
        )


# ---------------------------------------------------------------------------
# status_report_request (shape-only stub)
# ---------------------------------------------------------------------------


def test_status_report_after_process_is_committed(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    doc_id = "status-doc"
    _process_canonical(vault_root, doc_id=doc_id)

    response = status_report_request(
        StatusReportRequest(locator=_expected_locator(doc_id)),
        vault_root=vault_root,
    )
    assert isinstance(response, StatusReportResponse)
    assert response.status == "committed"
    assert response.locator == _expected_locator(doc_id)


def test_status_report_for_uncommitted_locator_is_unknown(tmp_path: Path) -> None:
    response = status_report_request(
        StatusReportRequest(locator=_expected_locator("never-committed")),
        vault_root=tmp_path,
    )
    assert response.status == "unknown"


def test_status_report_for_malformed_locator_is_unknown(tmp_path: Path) -> None:
    response = status_report_request(
        StatusReportRequest(locator="bogus"),
        vault_root=tmp_path,
    )
    assert response.status == "unknown"


# ---------------------------------------------------------------------------
# SpanSpec validation
# ---------------------------------------------------------------------------


def test_span_spec_rejects_end_before_start() -> None:
    """A SpanSpec with end < start is silently dropped by the kernel
    redactor; the public boundary should reject it up front with a clear
    validation error rather than producing no-op redaction."""
    from pydantic import ValidationError as PydanticValidationError

    with pytest.raises(PydanticValidationError, match="SpanSpec.end"):
        SpanSpec(start=9, end=0, kind=EntityKind.PERSON)


def test_span_spec_allows_zero_length_span() -> None:
    """end == start is an empty span; allowed (the redactor drops it harmlessly)."""
    spec = SpanSpec(start=5, end=5, kind=EntityKind.PERSON)
    assert spec.start == spec.end == 5


# ---------------------------------------------------------------------------
# inspect_request fail-closed on corrupt vault state
# ---------------------------------------------------------------------------


def test_inspect_request_returns_failure_for_corrupt_manifest(tmp_path: Path) -> None:
    """A manifest file that exists but is invalid JSON must produce a
    ResolverFailure, not propagate an exception out of inspect_request."""
    vault_root = tmp_path / "vault"
    doc_id = "corrupt-manifest"
    _process_canonical(vault_root, doc_id=doc_id)

    manifest_path = vault_root / "manifests" / f"{doc_id}.json"
    manifest_path.write_text("{not valid json", encoding="utf-8")

    response = inspect_request(
        InspectRequest(locator=_expected_locator(doc_id)),
        vault_root=vault_root,
    )
    assert isinstance(response, ResolverFailure)
    assert response.reason is ResolverFailureReason.ArtifactMissing


def test_inspect_request_forwards_caller_purpose(tmp_path: Path) -> None:
    """Caller-supplied purpose must reach resolve() so the audit field
    captures intent rather than a hard-coded label."""
    vault_root = tmp_path / "vault"
    doc_id = "purpose-doc"
    _process_canonical(vault_root, doc_id=doc_id)

    # Default purpose still works.
    default_resp = inspect_request(
        InspectRequest(locator=_expected_locator(doc_id)),
        vault_root=vault_root,
    )
    assert isinstance(default_resp, InspectResponse)

    # Explicit purpose is accepted and does not change the response shape.
    explicit_resp = inspect_request(
        InspectRequest(
            locator=_expected_locator(doc_id),
            purpose="ticket-1234:reading-redacted-doc",
        ),
        vault_root=vault_root,
    )
    assert isinstance(explicit_resp, InspectResponse)


def test_inspect_request_rejects_empty_purpose(tmp_path: Path) -> None:
    """Empty purpose flows into resolve() and surfaces as a typed failure."""
    vault_root = tmp_path / "vault"
    doc_id = "empty-purpose-doc"
    _process_canonical(vault_root, doc_id=doc_id)

    response = inspect_request(
        InspectRequest(locator=_expected_locator(doc_id), purpose="   "),
        vault_root=vault_root,
    )
    assert isinstance(response, ResolverFailure)
    assert response.reason is ResolverFailureReason.PurposeNotPermitted


# ---------------------------------------------------------------------------
# search_request — QueryResolver round-trip (#48)
# ---------------------------------------------------------------------------


def _canonical_kernel_spans() -> list[Span]:
    return [Span(start=s.start, end=s.end, kind=s.kind) for s in _CANONICAL_SPAN_SPECS]


def test_search_request_with_resolver_translates_raw_query_to_hit(
    tmp_path: Path,
) -> None:
    """A raw private value submitted as the query through the public boundary
    must surface as a hit when the gateway carries a :class:`QueryResolver`
    populated from the document's private dictionary — and the resulting
    :class:`SearchResponse` must not echo the raw value anywhere."""
    vault_root = tmp_path / "vault"
    doc_id = "resolver-search-001"
    _process_canonical(vault_root, doc_id=doc_id)

    # Re-derive the manifest + private dictionary the same way the kernel
    # did, so the resolver registration mirrors what an in-process caller
    # would have registered at index time.
    _, _, private_dict = redact(_RAW_TEXT, _canonical_kernel_spans())
    manifest = DocumentManifest.model_validate_json(
        (vault_root / "manifests" / f"{doc_id}.json").read_text(encoding="utf-8")
    )

    resolver = QueryResolver()
    gateway = SearchGateway(query_resolver=resolver)
    gateway.index(manifest, private_entries=private_dict)

    # Submit a RAW private value as the query — the canonical privacy
    # case from architecture.md §12.3.
    response = search_request(SearchRequest(query="Alice Tan"), gateway=gateway)
    assert isinstance(response, SearchResponse)
    assert len(response.hits) == 1
    hit = response.hits[0]
    assert isinstance(hit, SearchHit)
    assert hit.handle.locator == _expected_locator(doc_id)

    # Privacy invariant: the serialised response carries NO raw private
    # value anywhere — not in the snippet, not in the handle, not in
    # labels.
    blob = response.model_dump_json()
    for needle in _RAW_NEEDLES:
        assert needle not in blob, (
            f"search response leaked raw private value {needle!r} after "
            "resolver-mediated translation"
        )


def test_search_request_with_resolver_round_trips_to_restoration(
    tmp_path: Path,
) -> None:
    """The full §12.3 round-trip: raw private value → translated key → hit
    → restoration_api.restore — which is the SOLE sanctioned path through
    which a raw ``original_value`` becomes observable."""
    vault_root = tmp_path / "vault"
    doc_id = "resolver-restore-001"
    _process_canonical(vault_root, doc_id=doc_id)

    _, _, private_dict = redact(_RAW_TEXT, _canonical_kernel_spans())
    manifest = DocumentManifest.model_validate_json(
        (vault_root / "manifests" / f"{doc_id}.json").read_text(encoding="utf-8")
    )

    resolver = QueryResolver()
    gateway = SearchGateway(query_resolver=resolver)
    gateway.index(manifest, private_entries=private_dict)

    # Step 1: agent submits a raw private value as the query.
    response = search_request(SearchRequest(query="Alice Tan"), gateway=gateway)
    assert len(response.hits) == 1
    hit_locator = response.hits[0].handle.locator
    # The hit locator round-trips to the same doc_id we just committed.
    expected = _expected_locator(doc_id)
    assert hit_locator == expected

    # Step 2: re-hydrate via restoration_api.restore (the only sanctioned
    # path). Construct the ArtifactHandle from the vault layout the
    # kernel already wrote.
    private_path = (vault_root / "private" / f"{doc_id}.json").resolve()
    handle = ArtifactHandle(doc_id=doc_id, vault_path=str(private_path))
    restored = restoration_api.restore(handle, vault_root=vault_root)
    restored_values = {e.original_value for e in restored}
    assert "Alice Tan" in restored_values
    assert "Acme Corp" in restored_values
    assert "12345" in restored_values


def test_search_request_without_resolver_preserves_zero_hits_for_raw_query(
    tmp_path: Path,
) -> None:
    """The §Done criterion: a gateway constructed without ``query_resolver``
    and indexed without ``private_entries`` must produce identical
    ``search_request`` results to the pre-change implementation, including
    the zero-hit guarantee for raw private values."""
    vault_root = tmp_path / "vault"
    doc_id = "resolver-zero-hit"
    _process_canonical(vault_root, doc_id=doc_id)

    manifest = DocumentManifest.model_validate_json(
        (vault_root / "manifests" / f"{doc_id}.json").read_text(encoding="utf-8")
    )
    gateway = SearchGateway()
    gateway.index(manifest)

    for needle in _RAW_NEEDLES:
        response = search_request(SearchRequest(query=needle), gateway=gateway)
        assert response.hits == [], (
            "without a QueryResolver attached, raw private values must "
            f"continue to produce zero hits; got hits for {needle!r}"
        )
