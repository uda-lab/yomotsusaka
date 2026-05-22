"""Request/response tests for the five MVP-2 boundary operations.

Pins the agent-facing contract for:

* :func:`process_document_request` — drives raw_text through the kernel and
  returns only a public handle (no vault_path).
* :func:`inspect_request` — returns a public manifest view stripped of
  ``source_ref``.
* :func:`search_request` — returns public ``SearchHit``s built from already-
  redacted manifest text.
* :func:`restoration_request` — shape-only stub, always ``outcome="deferred"``.
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

from yomotsusaka.boundary import (
    InspectRequest,
    InspectResponse,
    ProcessRequest,
    ProcessResponse,
    PublicHandle,
    PublicManifestView,
    ResolverFailure,
    ResolverFailureReason,
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
    process_document_request,
    restoration_request,
    search_request,
    status_report_request,
)
from yomotsusaka.schemas import DocumentManifest, EntityKind
from yomotsusaka.search_gateway import SearchGateway


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
# restoration_request (shape-only stub)
# ---------------------------------------------------------------------------


def test_restoration_request_always_returns_deferred(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    doc_id = "restore-doc"

    _process_canonical(vault_root, doc_id=doc_id)
    response = restoration_request(
        RestorationRequest(
            locator=_expected_locator(doc_id),
            purpose="legitimate-restore",
        ),
        vault_root=vault_root,
    )
    assert isinstance(response, RestorationResponse)
    assert response.outcome == "deferred"
    assert response.locator == _expected_locator(doc_id)

    # No raw private value reaches the deferred response.
    blob = response.model_dump_json()
    for needle in _RAW_NEEDLES:
        assert needle not in blob


def test_restoration_request_rejects_malformed_locator(tmp_path: Path) -> None:
    response = restoration_request(
        RestorationRequest(locator="bogus", purpose="t"),
        vault_root=tmp_path,
    )
    assert isinstance(response, ResolverFailure)
    assert response.reason is ResolverFailureReason.MalformedLocator


def test_restoration_request_rejects_empty_purpose(tmp_path: Path) -> None:
    response = restoration_request(
        RestorationRequest(locator=_expected_locator("x"), purpose="   "),
        vault_root=tmp_path,
    )
    assert isinstance(response, ResolverFailure)
    assert response.reason is ResolverFailureReason.PurposeNotPermitted


def test_restoration_request_does_not_invoke_restoration_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MVP-2 stub must not call restoration_api.restore; #27 wires the real flow."""
    vault_root = tmp_path / "vault"
    doc_id = "restore-no-call"
    _process_canonical(vault_root, doc_id=doc_id)

    calls: list[str] = []

    import yomotsusaka.restoration_api as restoration_api

    def boom(*args: object, **kwargs: object) -> None:
        calls.append("restore")
        raise AssertionError("restoration_api.restore must not be called in MVP-2")

    monkeypatch.setattr(restoration_api, "restore", boom)

    response = restoration_request(
        RestorationRequest(locator=_expected_locator(doc_id), purpose="t"),
        vault_root=vault_root,
    )
    assert isinstance(response, RestorationResponse)
    assert calls == []


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
