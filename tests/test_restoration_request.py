"""Tests for the audit-logged restoration request boundary (#27).

Pins the full contract delivered by issue #27:

* ``RestorationRequest`` two-part field split (required vs reserved).
* ``RestorationResponse`` outcome invariants — no ``"deferred"`` anywhere.
* ``ResolverScope.PRIVATE_BOUNDARY`` is the only scope that reaches the
  kernel; all other scopes return ``ScopeDenied`` *after* an audit record
  has been written.
* ``<vault_root>/audit/restoration.jsonl`` is appended to on every
  observable path (schema-invalid, scope-denied, accepted, audit-write-
  failed-but-best-effort, kernel error).
* Raw private values never leak into failure responses or audit records.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError as PydanticValidationError

from yomotsusaka import restoration_api
from yomotsusaka.boundary import (
    ProcessRequest,
    PublicHandle,
    ResolverScope,
    RestorationFailureReason,
    RestorationRequest,
    RestorationResponse,
    SpanSpec,
    build_locator,
    process_document_request,
    restoration_request,
)
from yomotsusaka.schemas import EntityKind, PrivateDictEntry


# Canonical raw fixture; copied from test_boundary_operations.py so the leak
# scans in this file can reuse the same needles without cross-importing.
_RAW_TEXT = "Alice Tan works at Acme Corp. Patient ID: 12345."
_RAW_NEEDLES = ("Alice", "Acme", "12345")


def _canonical_spans() -> list[SpanSpec]:
    return [
        SpanSpec(start=0, end=9, kind=EntityKind.PERSON),
        SpanSpec(start=19, end=28, kind=EntityKind.ORG),
        SpanSpec(start=42, end=47, kind=EntityKind.ID_NUMBER),
    ]


def _process_canonical(vault_root: Path, doc_id: str) -> None:
    process_document_request(
        ProcessRequest(doc_id=doc_id, raw_text=_RAW_TEXT, spans=_canonical_spans()),
        vault_root=vault_root,
    )


def _public_handle(doc_id: str) -> PublicHandle:
    return PublicHandle(
        locator=build_locator(
            exposure_class="agent_redacted",
            artifact_kind="manifest",
            opaque_id=doc_id,
        )
    )


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _audit_lines(vault_root: Path) -> list[dict]:
    path = vault_root / "audit" / "restoration.jsonl"
    if not path.exists():
        return []
    out: list[dict] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        out.append(json.loads(raw_line))
    return out


def _base_kwargs() -> dict:
    """Common required fields, override per test as needed."""
    return {
        "caller_label": "test-agent",
        "reason": "unit-test",
        "timestamp": _now_utc(),
    }


# ---------------------------------------------------------------------------
# RestorationRequest validation — required field split
# ---------------------------------------------------------------------------


def test_request_requires_exactly_one_target() -> None:
    with pytest.raises(PydanticValidationError):
        RestorationRequest(
            **_base_kwargs(),
            requested_keys=["k"],
        )  # no target at all
    with pytest.raises(PydanticValidationError):
        RestorationRequest(
            **_base_kwargs(),
            target_public_handle=_public_handle("a"),
            document_id="a",
            requested_keys=["k"],
        )  # both targets


def test_request_requires_some_filter() -> None:
    with pytest.raises(PydanticValidationError):
        RestorationRequest(
            **_base_kwargs(),
            document_id="d",
            requested_keys=[],
            requested_entity_kinds=[],
        )


def test_request_rejects_empty_caller_label() -> None:
    with pytest.raises(PydanticValidationError):
        RestorationRequest(
            caller_label="   ",
            reason="r",
            timestamp=_now_utc(),
            document_id="d",
            requested_keys=["k"],
        )


def test_request_rejects_empty_reason() -> None:
    with pytest.raises(PydanticValidationError):
        RestorationRequest(
            caller_label="c",
            reason="",
            timestamp=_now_utc(),
            document_id="d",
            requested_keys=["k"],
        )


def test_request_rejects_naive_timestamp() -> None:
    naive = datetime(2026, 5, 23, 12, 0, 0)  # no tzinfo
    with pytest.raises(PydanticValidationError):
        RestorationRequest(
            caller_label="c",
            reason="r",
            timestamp=naive,
            document_id="d",
            requested_keys=["k"],
        )


def test_request_is_frozen() -> None:
    req = RestorationRequest(
        **_base_kwargs(),
        document_id="d",
        requested_keys=["k"],
    )
    with pytest.raises((PydanticValidationError, TypeError)):
        req.caller_label = "other"  # type: ignore[misc]


def test_request_rejects_unknown_field() -> None:
    with pytest.raises(PydanticValidationError):
        RestorationRequest(
            **_base_kwargs(),
            document_id="d",
            requested_keys=["k"],
            unexpected="x",  # type: ignore[call-arg]
        )


# ---------------------------------------------------------------------------
# Happy path — PRIVATE_BOUNDARY scope, document_id target, key filter
# ---------------------------------------------------------------------------


def test_accepted_via_document_id_returns_raw_entries(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    doc_id = "doc-doc-id"
    _process_canonical(vault_root, doc_id=doc_id)

    # Read what the kernel actually stored so the key filter is realistic.
    raw = json.loads(
        (vault_root / "private" / f"{doc_id}.json").read_text(encoding="utf-8")
    )
    target_key = raw[0]["key"]

    req = RestorationRequest(
        **_base_kwargs(),
        document_id=doc_id,
        requested_keys=[target_key],
    )
    resp = restoration_request(req, scope=ResolverScope.PRIVATE_BOUNDARY, vault_root=vault_root)

    assert isinstance(resp, RestorationResponse)
    assert resp.outcome == "accepted"
    assert resp.reason is None
    assert resp.document_id == doc_id
    assert resp.private_entries is not None
    assert len(resp.private_entries) == 1
    assert resp.private_entries[0].key == target_key
    # The raw value DOES appear in the private_entries on the accepted path;
    # that is the entire point of the scope=PRIVATE_BOUNDARY contract.
    assert resp.private_entries[0].original_value in {"Alice Tan", "Acme Corp", "12345"}


def test_accepted_via_target_public_handle(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    doc_id = "doc-via-handle"
    _process_canonical(vault_root, doc_id=doc_id)

    req = RestorationRequest(
        **_base_kwargs(),
        target_public_handle=_public_handle(doc_id),
        requested_entity_kinds=[EntityKind.PERSON],
    )
    resp = restoration_request(req, scope=ResolverScope.PRIVATE_BOUNDARY, vault_root=vault_root)

    assert resp.outcome == "accepted"
    assert resp.document_id == doc_id
    assert resp.private_entries is not None
    assert all(e.kind is EntityKind.PERSON for e in resp.private_entries)


# ---------------------------------------------------------------------------
# AND-filter on keys × entity_kinds
# ---------------------------------------------------------------------------


def test_and_filter_keys_and_entity_kinds(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    doc_id = "doc-and-filter"
    _process_canonical(vault_root, doc_id=doc_id)

    raw = json.loads(
        (vault_root / "private" / f"{doc_id}.json").read_text(encoding="utf-8")
    )
    person_entry = next(e for e in raw if e["kind"] == EntityKind.PERSON.value)
    org_entry = next(e for e in raw if e["kind"] == EntityKind.ORG.value)

    # Key filter alone returns just the PERSON entry.
    resp_key_only = restoration_request(
        RestorationRequest(
            **_base_kwargs(),
            document_id=doc_id,
            requested_keys=[person_entry["key"]],
        ),
        scope=ResolverScope.PRIVATE_BOUNDARY,
        vault_root=vault_root,
    )
    assert resp_key_only.outcome == "accepted"
    assert resp_key_only.private_entries is not None
    assert [e.key for e in resp_key_only.private_entries] == [person_entry["key"]]

    # Kind filter alone returns just the ORG entry.
    resp_kind_only = restoration_request(
        RestorationRequest(
            **_base_kwargs(),
            document_id=doc_id,
            requested_entity_kinds=[EntityKind.ORG],
        ),
        scope=ResolverScope.PRIVATE_BOUNDARY,
        vault_root=vault_root,
    )
    assert resp_kind_only.outcome == "accepted"
    assert resp_kind_only.private_entries is not None
    assert [e.key for e in resp_kind_only.private_entries] == [org_entry["key"]]

    # Both together (PERSON key + ORG kind) AND-filter yields nothing.
    resp_and = restoration_request(
        RestorationRequest(
            **_base_kwargs(),
            document_id=doc_id,
            requested_keys=[person_entry["key"]],
            requested_entity_kinds=[EntityKind.ORG],
        ),
        scope=ResolverScope.PRIVATE_BOUNDARY,
        vault_root=vault_root,
    )
    assert resp_and.outcome == "accepted"
    assert resp_and.private_entries == []


# ---------------------------------------------------------------------------
# Scope gate — non-PRIVATE_BOUNDARY scopes never reach the kernel
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scope",
    [ResolverScope.ORDINARY_AGENT, ResolverScope.AUDIT_REVIEWER],
)
def test_non_private_boundary_scope_is_denied_and_skips_kernel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: ResolverScope,
) -> None:
    vault_root = tmp_path / "vault"
    doc_id = "doc-scope"
    _process_canonical(vault_root, doc_id=doc_id)

    calls: list[str] = []

    def boom(*args: object, **kwargs: object) -> None:
        calls.append("restore")
        raise AssertionError("kernel must not be called for non-PRIVATE_BOUNDARY scope")

    monkeypatch.setattr(restoration_api, "restore", boom)

    req = RestorationRequest(
        **_base_kwargs(),
        document_id=doc_id,
        requested_keys=["any"],
    )
    resp = restoration_request(req, scope=scope, vault_root=vault_root)

    assert resp.outcome == "failed"
    assert resp.reason is RestorationFailureReason.ScopeDenied
    assert resp.private_entries is None
    assert calls == []
    # Denial is still audit-logged.
    lines = _audit_lines(vault_root)
    assert len(lines) == 1
    assert lines[0]["outcome"] == "failed"
    assert lines[0]["failure_reason"] == "scope_denied"
    assert lines[0]["scope"] == scope.name


# ---------------------------------------------------------------------------
# Audit write failure → AuditWriteFailed, kernel not called, no partial line
# ---------------------------------------------------------------------------


def test_audit_write_failure_blocks_kernel_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault_root = tmp_path / "vault"
    doc_id = "doc-audit-fail"
    _process_canonical(vault_root, doc_id=doc_id)

    calls: list[str] = []

    def boom(*args: object, **kwargs: object) -> None:
        calls.append("restore")
        raise AssertionError("kernel must not be called when audit write fails")

    monkeypatch.setattr(restoration_api, "restore", boom)

    real_open = Path.open

    def deny_audit_open(self: Path, *args: object, **kwargs: object):
        if self.name == "restoration.jsonl":
            raise OSError("simulated disk full")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny_audit_open)

    req = RestorationRequest(
        **_base_kwargs(),
        document_id=doc_id,
        requested_keys=["any"],
    )
    resp = restoration_request(req, scope=ResolverScope.PRIVATE_BOUNDARY, vault_root=vault_root)

    assert resp.outcome == "failed"
    assert resp.reason is RestorationFailureReason.AuditWriteFailed
    assert resp.private_entries is None
    assert calls == []
    # No partial JSONL line was written.
    audit_file = vault_root / "audit" / "restoration.jsonl"
    if audit_file.exists():
        assert audit_file.read_text(encoding="utf-8") == ""


# ---------------------------------------------------------------------------
# Missing artifact — kernel raises "No private data found"
# ---------------------------------------------------------------------------


def test_missing_artifact_maps_to_artifact_missing(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    (vault_root / "private").mkdir()  # exists but empty — no committed doc

    req = RestorationRequest(
        **_base_kwargs(),
        document_id="never-committed",
        requested_keys=["any"],
    )
    resp = restoration_request(req, scope=ResolverScope.PRIVATE_BOUNDARY, vault_root=vault_root)

    assert resp.outcome == "failed"
    assert resp.reason is RestorationFailureReason.ArtifactMissing
    assert resp.private_entries is None
    # detail does not contain vault_root or any abs path.
    blob = resp.model_dump_json()
    assert str(vault_root) not in blob
    assert str(vault_root.resolve()) not in blob


# ---------------------------------------------------------------------------
# Kernel error path — non-"No private data found" RestorationError
# ---------------------------------------------------------------------------


def test_kernel_error_strips_vault_root_from_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault_root = tmp_path / "vault"
    doc_id = "doc-kernel-err"
    _process_canonical(vault_root, doc_id=doc_id)

    leaky_message = f"kernel exploded while reading {vault_root}/private/{doc_id}.json"

    def kernel_raises(*args: object, **kwargs: object) -> None:
        raise restoration_api.RestorationError(leaky_message)

    monkeypatch.setattr(restoration_api, "restore", kernel_raises)

    req = RestorationRequest(
        **_base_kwargs(),
        document_id=doc_id,
        requested_keys=["any"],
    )
    resp = restoration_request(req, scope=ResolverScope.PRIVATE_BOUNDARY, vault_root=vault_root)

    assert resp.outcome == "failed"
    assert resp.reason is RestorationFailureReason.KernelError
    assert resp.detail is not None
    assert str(vault_root) not in resp.detail
    # The placeholder should appear instead.
    assert "<vault_root>" in resp.detail

    # Kernel-error path also writes a corrective audit record sharing the
    # intent's audit_record_id. Consumers must reconcile by taking the last
    # record per audit_record_id (architecture §6.1).
    lines = _audit_lines(vault_root)
    assert len(lines) == 2
    assert all(ln["audit_record_id"] == resp.audit_record_id for ln in lines)
    assert lines[0]["outcome"] == "accepted"  # intent record
    assert lines[0]["returned_entry_count"] is None
    assert lines[1]["outcome"] == "failed"  # corrective record (the truth)
    assert lines[1]["failure_reason"] == "kernel_error"


def test_non_restoration_error_kernel_exception_is_classified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kernel may raise non-RestorationError exceptions (corrupt JSON,
    schema drift, OSError mid-read). The boundary must classify these as
    KernelError, not let them escape as raw exceptions — otherwise callers
    lose both the structured response and the audit-record-id pairing."""
    vault_root = tmp_path / "vault"
    doc_id = "doc-kernel-non-rest-err"
    _process_canonical(vault_root, doc_id=doc_id)

    leaky_raw_bytes = "RAW-PRIVATE-BYTES-Alice-from-corrupt-file"

    def kernel_raises_value_error(*args: object, **kwargs: object) -> None:
        # Simulate json.JSONDecodeError (a ValueError subclass) or a
        # PydanticValidationError leaking through restoration_api.restore.
        raise ValueError(leaky_raw_bytes)

    monkeypatch.setattr(restoration_api, "restore", kernel_raises_value_error)

    req = RestorationRequest(
        **_base_kwargs(),
        document_id=doc_id,
        requested_keys=["any"],
    )
    # Must NOT raise — the boundary catches the exception.
    resp = restoration_request(req, scope=ResolverScope.PRIVATE_BOUNDARY, vault_root=vault_root)

    assert resp.outcome == "failed"
    assert resp.reason is RestorationFailureReason.KernelError
    assert resp.private_entries is None
    # The raw bytes from the kernel exception message must NOT appear in the
    # public detail — that would leak private data through the error path.
    assert resp.detail is not None
    assert leaky_raw_bytes not in resp.detail
    blob = resp.model_dump_json()
    assert leaky_raw_bytes not in blob

    # And the corrective audit record was still written.
    lines = _audit_lines(vault_root)
    assert any(
        ln["audit_record_id"] == resp.audit_record_id and ln["outcome"] == "failed"
        for ln in lines
    )
    # The audit log itself does not contain the raw bytes either.
    audit_text = (vault_root / "audit" / "restoration.jsonl").read_text(encoding="utf-8")
    assert leaky_raw_bytes not in audit_text


# ---------------------------------------------------------------------------
# Reserved fields round-trip into audit
# ---------------------------------------------------------------------------


def test_reserved_fields_round_trip_into_audit(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    doc_id = "doc-reserved"
    _process_canonical(vault_root, doc_id=doc_id)

    req = RestorationRequest(
        **_base_kwargs(),
        document_id=doc_id,
        requested_entity_kinds=[EntityKind.PERSON],
        authorization_decision="accept",
        policy_profile="default",
        approval_ticket="TICKET-123",
        production_scope="research",
    )
    resp = restoration_request(req, scope=ResolverScope.PRIVATE_BOUNDARY, vault_root=vault_root)
    assert resp.outcome == "accepted"

    lines = _audit_lines(vault_root)
    # accepted path writes 2 records sharing the same audit_record_id.
    assert len(lines) == 2
    assert all(line["audit_record_id"] == resp.audit_record_id for line in lines)
    for line in lines:
        assert line["authorization_decision"] == "accept"
        assert line["policy_profile"] == "default"
        assert line["approval_ticket"] == "TICKET-123"
        assert line["production_scope"] == "research"
    # First (intent) record has returned_entry_count=None; second has the int.
    assert lines[0]["returned_entry_count"] is None
    assert isinstance(lines[1]["returned_entry_count"], int)


# ---------------------------------------------------------------------------
# Audit JSONL parseability — every line has the frozen-shape fields
# ---------------------------------------------------------------------------


def test_audit_jsonl_is_well_formed(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    doc_id = "doc-jsonl"
    _process_canonical(vault_root, doc_id=doc_id)

    # One accepted (writes 2 records) + one denied (writes 1 record).
    restoration_request(
        RestorationRequest(
            **_base_kwargs(),
            document_id=doc_id,
            requested_entity_kinds=[EntityKind.PERSON],
        ),
        scope=ResolverScope.PRIVATE_BOUNDARY,
        vault_root=vault_root,
    )
    restoration_request(
        RestorationRequest(
            **_base_kwargs(),
            document_id=doc_id,
            requested_entity_kinds=[EntityKind.PERSON],
        ),
        scope=ResolverScope.ORDINARY_AGENT,
        vault_root=vault_root,
    )

    audit_path = vault_root / "audit" / "restoration.jsonl"
    raw = audit_path.read_text(encoding="utf-8")
    lines = [ln for ln in raw.split("\n") if ln]
    assert len(lines) == 3

    required_keys = {
        "audit_record_id",
        "recorded_at",
        "caller_label",
        "scope",
        "target",
        "requested_keys",
        "requested_entity_kinds",
        "reason",
        "request_timestamp",
        "authorization_decision",
        "policy_profile",
        "approval_ticket",
        "production_scope",
        "outcome",
        "failure_reason",
        "returned_entry_count",
    }
    for ln in lines:
        rec = json.loads(ln)
        assert required_keys.issubset(rec.keys())


# ---------------------------------------------------------------------------
# Privacy invariants — no raw values, no abs paths, no vault_root in failures
# ---------------------------------------------------------------------------


def test_failure_response_does_not_leak_raw_values_or_vault_root(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "vault-with-canary-Alice-Acme"
    vault_root.mkdir()
    (vault_root / "private").mkdir()

    req = RestorationRequest(
        **_base_kwargs(),
        document_id="never-committed",
        requested_keys=["any"],
    )
    resp = restoration_request(req, scope=ResolverScope.PRIVATE_BOUNDARY, vault_root=vault_root)
    assert resp.outcome == "failed"

    blob = resp.model_dump_json()
    for needle in _RAW_NEEDLES:
        assert needle not in blob, f"raw needle {needle!r} leaked into failure response"
    assert str(vault_root) not in blob
    assert str(vault_root.resolve()) not in blob


def test_audit_records_never_contain_raw_original_value(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    doc_id = "doc-audit-no-raw"
    _process_canonical(vault_root, doc_id=doc_id)

    restoration_request(
        RestorationRequest(
            **_base_kwargs(),
            document_id=doc_id,
            requested_entity_kinds=[EntityKind.PERSON],
        ),
        scope=ResolverScope.PRIVATE_BOUNDARY,
        vault_root=vault_root,
    )

    audit_text = (vault_root / "audit" / "restoration.jsonl").read_text(
        encoding="utf-8"
    )
    for needle in _RAW_NEEDLES:
        assert needle not in audit_text, (
            f"raw needle {needle!r} leaked into audit JSONL"
        )


# ---------------------------------------------------------------------------
# Existing restoration_api.restore behaviour stays usable through the new flow
# ---------------------------------------------------------------------------


def test_kernel_restoration_api_still_works_directly(tmp_path: Path) -> None:
    """Acceptance criterion: existing restoration_api.restore behavior remains
    usable. We exercise the kernel directly here to pin that contract."""
    from yomotsusaka.commit import commit
    from yomotsusaka.schemas import DocumentManifest

    vault_root = tmp_path / "vault"
    manifest = DocumentManifest(
        source_ref="sha256:test",
        redacted_text="Hello <PERSON_x>.",
    )
    private_dict = [
        PrivateDictEntry(
            key="<PERSON_x>",
            original_value="Alice",
            kind=EntityKind.PERSON,
        )
    ]
    handle = commit(manifest, private_dict, vault_root=vault_root)
    restored = restoration_api.restore(handle, vault_root=vault_root)
    assert restored[0].original_value == "Alice"


# ---------------------------------------------------------------------------
# Outcome contract — no observable response ever carries outcome="deferred"
# ---------------------------------------------------------------------------


def test_no_observable_response_uses_deferred_outcome(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    doc_id = "doc-no-deferred"
    _process_canonical(vault_root, doc_id=doc_id)

    inputs = [
        # accepted path
        (
            RestorationRequest(
                **_base_kwargs(),
                document_id=doc_id,
                requested_entity_kinds=[EntityKind.PERSON],
            ),
            ResolverScope.PRIVATE_BOUNDARY,
        ),
        # scope-denied path
        (
            RestorationRequest(
                **_base_kwargs(),
                document_id=doc_id,
                requested_entity_kinds=[EntityKind.PERSON],
            ),
            ResolverScope.ORDINARY_AGENT,
        ),
        # missing-artifact path
        (
            RestorationRequest(
                **_base_kwargs(),
                document_id="missing",
                requested_keys=["k"],
            ),
            ResolverScope.PRIVATE_BOUNDARY,
        ),
    ]
    for req, scope in inputs:
        resp = restoration_request(req, scope=scope, vault_root=vault_root)
        assert resp.outcome != "deferred"
        assert resp.outcome in {"accepted", "failed", "accepted_but_redacted"}


# ---------------------------------------------------------------------------
# Outcome invariants — frozen response shape
# ---------------------------------------------------------------------------


def test_response_outcome_invariants_block_inconsistent_shapes() -> None:
    """RestorationResponse model_validator must reject inconsistent shapes."""
    audit_id = "a" * 32
    # accepted without private_entries
    with pytest.raises(PydanticValidationError):
        RestorationResponse(outcome="accepted", audit_record_id=audit_id)
    # accepted with a failure reason
    with pytest.raises(PydanticValidationError):
        RestorationResponse(
            outcome="accepted",
            audit_record_id=audit_id,
            private_entries=[],
            reason=RestorationFailureReason.KernelError,
        )
    # failed without a reason
    with pytest.raises(PydanticValidationError):
        RestorationResponse(outcome="failed", audit_record_id=audit_id)
    # failed with private_entries
    with pytest.raises(PydanticValidationError):
        RestorationResponse(
            outcome="failed",
            audit_record_id=audit_id,
            reason=RestorationFailureReason.KernelError,
            private_entries=[],
        )


# ---------------------------------------------------------------------------
# Timestamp passthrough — non-UTC tz is normalised in audit
# ---------------------------------------------------------------------------


def test_non_utc_tz_is_recorded_in_utc(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    doc_id = "doc-tz"
    _process_canonical(vault_root, doc_id=doc_id)

    plus_nine = timezone(timedelta(hours=9))
    ts = datetime(2026, 5, 23, 21, 0, 0, tzinfo=plus_nine)
    req = RestorationRequest(
        caller_label="c",
        reason="r",
        timestamp=ts,
        document_id=doc_id,
        requested_entity_kinds=[EntityKind.PERSON],
    )
    resp = restoration_request(req, scope=ResolverScope.PRIVATE_BOUNDARY, vault_root=vault_root)
    assert resp.outcome == "accepted"

    lines = _audit_lines(vault_root)
    assert lines
    # Recorded request_timestamp is in UTC (12:00 UTC corresponds to 21:00 +09:00).
    assert lines[0]["request_timestamp"] == "2026-05-23T12:00:00+00:00"
