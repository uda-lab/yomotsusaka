"""Tests for the restoration policy table (issue #44).

Pins the contract delivered by issue #44:

* ``RestorationPolicyTable.default_local()`` preserves the MVP-2
  contract — ``tests/test_restoration_request.py`` keeps passing
  unmodified, and the explicit ``policy_table=None`` path is identical
  to the ``default_local()`` path.
* Profile selection rules: explicit name selects that row;
  ``policy_profile=None`` selects the row marked ``default: true``; an
  unknown name on a *loaded* table denies with ``PolicyDenied``.
* Row-level requirements: ``production_scopes`` membership,
  ``require_authorization_decision``, and ``approval_ticket_pattern`` —
  each producing ``PolicyDenied`` when violated, ``permit`` when satisfied.
* Audit-first contract on the deny path: one ``outcome="failed"`` audit
  record with ``failure_reason="policy_denied"`` is written **before**
  the response is returned; the kernel ``restoration_api.restore`` is
  NOT called; the response carries ``audit_record_id`` and no
  ``private_entries``.
* Table-load invariants: zero default rows, two default rows, duplicate
  profile names, malformed regex, and missing top-level keys in the YAML
  file all fail loud at load time.
* Privacy: the ``PolicyDenied`` response and the corresponding audit
  line never leak raw private values, absolute paths, or the vault root.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
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
    SpanSpec,
    build_locator,
    process_document_request,
    restoration_request,
)
from yomotsusaka.policy import (
    PolicyDecision,
    RestorationPolicyRow,
    RestorationPolicyTable,
)
from yomotsusaka.schemas import EntityKind


# ---------------------------------------------------------------------------
# Canonical fixtures — kept local rather than imported from
# tests/test_restoration_request.py to honour the "canonical fixture" rule
# in CLAUDE.md (raw private values appear only inside the canonical block).
# ---------------------------------------------------------------------------

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
    return {
        "caller_label": "test-agent",
        "reason": "unit-test",
        "timestamp": _now_utc(),
    }


def _strict_table() -> RestorationPolicyTable:
    """A representative strict table with a permissive default and a
    strict row that requires both an authorization decision and a
    ticket pattern, scoped to internal/staging."""
    return RestorationPolicyTable(
        [
            RestorationPolicyRow(
                profile_name="_default_local",
                production_scopes=["*"],
                require_authorization_decision=False,
                approval_ticket_pattern=None,
                default=True,
            ),
            RestorationPolicyRow(
                profile_name="strict",
                production_scopes=["internal", "staging"],
                require_authorization_decision=True,
                approval_ticket_pattern=r"^TKT-[0-9]{4,}$",
                default=False,
            ),
            RestorationPolicyRow(
                profile_name="production",
                production_scopes=["production"],
                require_authorization_decision=True,
                approval_ticket_pattern=r"^TKT-[0-9]{4,}$",
                default=False,
            ),
        ]
    )


# ---------------------------------------------------------------------------
# Table-load invariants
# ---------------------------------------------------------------------------


def test_table_requires_exactly_one_default_row() -> None:
    with pytest.raises(ValueError, match="got 0"):
        RestorationPolicyTable(
            [
                RestorationPolicyRow(
                    profile_name="a",
                    production_scopes=["*"],
                    default=False,
                )
            ]
        )
    with pytest.raises(ValueError, match="got 2"):
        RestorationPolicyTable(
            [
                RestorationPolicyRow(
                    profile_name="a", production_scopes=["*"], default=True
                ),
                RestorationPolicyRow(
                    profile_name="b", production_scopes=["*"], default=True
                ),
            ]
        )


def test_table_rejects_duplicate_profile_names() -> None:
    with pytest.raises(ValueError, match="duplicate profile_name"):
        RestorationPolicyTable(
            [
                RestorationPolicyRow(
                    profile_name="a", production_scopes=["*"], default=True
                ),
                RestorationPolicyRow(
                    profile_name="a", production_scopes=["*"], default=False
                ),
            ]
        )


def test_row_rejects_malformed_regex_at_load_time() -> None:
    with pytest.raises((ValueError, PydanticValidationError)):
        RestorationPolicyRow(
            profile_name="bad",
            production_scopes=["*"],
            approval_ticket_pattern="(unclosed",
            default=False,
        )


def test_row_rejects_unknown_keys() -> None:
    """``extra='forbid'`` on the row model fails loud on schema drift."""
    with pytest.raises(PydanticValidationError):
        RestorationPolicyRow(
            profile_name="a",
            production_scopes=["*"],
            default=True,
            sneaky_extra_field="x",  # type: ignore[call-arg]
        )


# ---------------------------------------------------------------------------
# load_from_path — uses the bundled config/policy.example.yaml
# ---------------------------------------------------------------------------


def test_load_from_path_parses_bundled_example() -> None:
    path = Path(__file__).resolve().parent.parent / "config" / "policy.example.yaml"
    table = RestorationPolicyTable.load_from_path(path)
    names = set(table.profile_names())
    assert {"_default_local", "strict", "production"}.issubset(names)
    # Default row is _default_local.
    assert table.default_profile_name == "_default_local"


def test_load_from_path_preserves_redaction_section(tmp_path: Path) -> None:
    """A reader of ``restoration:`` does not break when ``redaction:`` is
    also present — and vice-versa."""
    yml = tmp_path / "policy.yaml"
    yml.write_text(
        """
redaction:
  always_redact: [PERSON]
  min_confidence: 0.8
restoration:
  profiles:
    - profile_name: only
      production_scopes: ["*"]
      default: true
""".strip(),
        encoding="utf-8",
    )
    table = RestorationPolicyTable.load_from_path(yml)
    assert table.default_profile_name == "only"


def test_load_from_path_missing_restoration_section(tmp_path: Path) -> None:
    yml = tmp_path / "policy.yaml"
    yml.write_text("redaction:\n  always_redact: [PERSON]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="restoration"):
        RestorationPolicyTable.load_from_path(yml)


def test_load_from_path_empty_file(tmp_path: Path) -> None:
    yml = tmp_path / "policy.yaml"
    yml.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        RestorationPolicyTable.load_from_path(yml)


def test_load_from_path_malformed_profile_row(tmp_path: Path) -> None:
    yml = tmp_path / "policy.yaml"
    yml.write_text(
        """
restoration:
  profiles:
    - profile_name: bad
      production_scopes: [internal]
      approval_ticket_pattern: "(unclosed"
      default: true
""".strip(),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="profiles\\[0\\]"):
        RestorationPolicyTable.load_from_path(yml)


# ---------------------------------------------------------------------------
# evaluate — pure decision function (no I/O, no audit side-effects here)
# ---------------------------------------------------------------------------


def test_default_local_evaluate_permits_everything() -> None:
    table = RestorationPolicyTable.default_local()
    decision = table.evaluate(
        policy_profile=None,
        production_scope=None,
        authorization_decision=None,
        approval_ticket=None,
    )
    assert isinstance(decision, PolicyDecision)
    assert decision.verdict == "permit"
    assert decision.matched_profile == "_default_local"
    assert decision.deny_reason is None


def test_default_local_route_unknown_profile_to_default() -> None:
    """``default_local`` is the permissive built-in; unknown profile names
    silently route to the single default row (so existing MVP-2 callers
    that pass arbitrary policy_profile strings keep working)."""
    table = RestorationPolicyTable.default_local()
    decision = table.evaluate(
        policy_profile="arbitrary-unknown-profile",
        production_scope="anywhere",
        authorization_decision=None,
        approval_ticket=None,
    )
    assert decision.verdict == "permit"
    assert decision.matched_profile == "_default_local"


def test_loaded_table_unknown_profile_denies() -> None:
    """A loaded table is strict — an unknown profile name is a deny, not a
    silent fallback to the default."""
    table = _strict_table()
    decision = table.evaluate(
        policy_profile="not-a-real-profile",
        production_scope="internal",
        authorization_decision="accept",
        approval_ticket="TKT-1234",
    )
    assert decision.verdict == "deny"
    assert decision.deny_reason is not None
    assert "not-a-real-profile" in decision.deny_reason


def test_none_profile_selects_default_row() -> None:
    table = _strict_table()
    decision = table.evaluate(
        policy_profile=None,
        production_scope=None,
        authorization_decision=None,
        approval_ticket=None,
    )
    assert decision.verdict == "permit"
    assert decision.matched_profile == "_default_local"


def test_named_profile_selects_named_row() -> None:
    table = _strict_table()
    decision = table.evaluate(
        policy_profile="strict",
        production_scope="internal",
        authorization_decision="accept",
        approval_ticket="TKT-1234",
    )
    assert decision.verdict == "permit"
    assert decision.matched_profile == "strict"


def test_production_scope_not_in_row_denies() -> None:
    table = _strict_table()
    decision = table.evaluate(
        policy_profile="strict",
        production_scope="production",  # not in [internal, staging]
        authorization_decision="accept",
        approval_ticket="TKT-1234",
    )
    assert decision.verdict == "deny"
    assert decision.matched_profile == "strict"
    assert decision.deny_reason is not None
    assert "production_scope" in decision.deny_reason


def test_require_authorization_decision_absent_denies() -> None:
    table = _strict_table()
    decision = table.evaluate(
        policy_profile="strict",
        production_scope="internal",
        authorization_decision=None,
        approval_ticket="TKT-1234",
    )
    assert decision.verdict == "deny"
    assert decision.deny_reason is not None
    assert "authorization_decision" in decision.deny_reason


def test_require_authorization_decision_present_permits() -> None:
    table = _strict_table()
    decision = table.evaluate(
        policy_profile="strict",
        production_scope="internal",
        authorization_decision="accept",
        approval_ticket="TKT-1234",
    )
    assert decision.verdict == "permit"


def test_approval_ticket_pattern_missing_denies() -> None:
    table = _strict_table()
    decision = table.evaluate(
        policy_profile="strict",
        production_scope="internal",
        authorization_decision="accept",
        approval_ticket=None,
    )
    assert decision.verdict == "deny"
    assert decision.deny_reason is not None
    assert "approval_ticket" in decision.deny_reason


def test_approval_ticket_pattern_mismatch_denies() -> None:
    table = _strict_table()
    decision = table.evaluate(
        policy_profile="strict",
        production_scope="internal",
        authorization_decision="accept",
        approval_ticket="WRONG-FORMAT",
    )
    assert decision.verdict == "deny"
    assert decision.deny_reason is not None
    assert "approval_ticket" in decision.deny_reason


def test_approval_ticket_pattern_match_permits() -> None:
    table = _strict_table()
    decision = table.evaluate(
        policy_profile="strict",
        production_scope="staging",
        authorization_decision="accept",
        approval_ticket="TKT-9999",
    )
    assert decision.verdict == "permit"


def test_approval_ticket_length_cap_denies() -> None:
    """A pathological ``approval_ticket`` must be refused before
    ``re.fullmatch`` walks it, to avoid a regex-DoS attack surface."""
    table = _strict_table()
    decision = table.evaluate(
        policy_profile="strict",
        production_scope="internal",
        authorization_decision="accept",
        approval_ticket="TKT-" + "9" * 1000,  # > 256-char cap
    )
    assert decision.verdict == "deny"
    assert decision.deny_reason is not None
    assert "256" in decision.deny_reason


def test_policy_decision_invariants() -> None:
    """``PolicyDecision`` rejects inconsistent verdict/deny_reason combos."""
    with pytest.raises(PydanticValidationError):
        PolicyDecision(verdict="permit", matched_profile="x", deny_reason="nope")
    with pytest.raises(PydanticValidationError):
        PolicyDecision(verdict="deny", matched_profile="x", deny_reason=None)
    with pytest.raises(PydanticValidationError):
        PolicyDecision(verdict="deny", matched_profile="x", deny_reason="")


# ---------------------------------------------------------------------------
# Wiring through restoration_request — default behaviour preserved
# ---------------------------------------------------------------------------


def test_policy_table_none_matches_default_local_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``policy_table=None`` and ``policy_table=default_local()`` produce
    structurally identical audit lines (modulo audit_record_id and
    timestamps) and identical response outcomes."""
    vault_root_a = tmp_path / "vault-a"
    vault_root_b = tmp_path / "vault-b"
    _process_canonical(vault_root_a, doc_id="d")
    _process_canonical(vault_root_b, doc_id="d")

    req = RestorationRequest(
        **_base_kwargs(),
        document_id="d",
        requested_entity_kinds=[EntityKind.PERSON],
    )

    resp_default = restoration_request(
        req,
        scope=ResolverScope.PRIVATE_BOUNDARY,
        vault_root=vault_root_a,
    )
    resp_explicit = restoration_request(
        req,
        scope=ResolverScope.PRIVATE_BOUNDARY,
        vault_root=vault_root_b,
        policy_table=RestorationPolicyTable.default_local(),
    )
    assert resp_default.outcome == "accepted"
    assert resp_explicit.outcome == "accepted"

    lines_a = _audit_lines(vault_root_a)
    lines_b = _audit_lines(vault_root_b)
    # Both paths write two records (intent + result) on the accepted path.
    assert len(lines_a) == 2 == len(lines_b)
    for line in lines_a + lines_b:
        assert line["policy_verdict"] == "permit"
        assert line["policy_matched_profile"] == "_default_local"


# ---------------------------------------------------------------------------
# Wiring through restoration_request — deny path: audit-first, no kernel call
# ---------------------------------------------------------------------------


def test_policy_denied_audit_written_before_response_and_kernel_not_called(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault_root = tmp_path / "vault"
    doc_id = "doc-deny"
    _process_canonical(vault_root, doc_id=doc_id)

    calls: list[str] = []

    def boom(*args: object, **kwargs: object) -> None:
        calls.append("restore")
        raise AssertionError(
            "kernel must not be called when the policy table denies the request"
        )

    monkeypatch.setattr(restoration_api, "restore", boom)

    # ``strict`` requires authorization_decision and a TKT- ticket; supplying
    # neither must produce a PolicyDenied.
    req = RestorationRequest(
        **_base_kwargs(),
        document_id=doc_id,
        requested_entity_kinds=[EntityKind.PERSON],
        policy_profile="strict",
        production_scope="internal",
    )
    resp = restoration_request(
        req,
        scope=ResolverScope.PRIVATE_BOUNDARY,
        vault_root=vault_root,
        policy_table=_strict_table(),
    )

    assert resp.outcome == "failed"
    assert resp.reason is RestorationFailureReason.PolicyDenied
    assert resp.private_entries is None
    assert resp.audit_record_id  # non-empty
    assert calls == []  # kernel NOT called

    lines = _audit_lines(vault_root)
    assert len(lines) == 1
    rec = lines[0]
    assert rec["audit_record_id"] == resp.audit_record_id
    assert rec["outcome"] == "failed"
    assert rec["failure_reason"] == "policy_denied"
    assert rec["policy_verdict"] == "deny"
    assert rec["policy_matched_profile"] == "strict"


def test_policy_denied_response_and_audit_do_not_leak(
    tmp_path: Path,
) -> None:
    """The PolicyDenied response and the audit line must scrub vault_root
    and never carry raw private values."""
    vault_root = tmp_path / "vault-with-canary-Alice-Acme-12345"
    doc_id = "doc-leak-deny"
    _process_canonical(vault_root, doc_id=doc_id)

    req = RestorationRequest(
        **_base_kwargs(),
        document_id=doc_id,
        requested_entity_kinds=[EntityKind.PERSON],
        policy_profile="strict",
        production_scope="internal",
    )
    resp = restoration_request(
        req,
        scope=ResolverScope.PRIVATE_BOUNDARY,
        vault_root=vault_root,
        policy_table=_strict_table(),
    )
    assert resp.outcome == "failed"
    assert resp.reason is RestorationFailureReason.PolicyDenied

    blob = resp.model_dump_json()
    for needle in _RAW_NEEDLES:
        assert needle not in blob
    assert str(vault_root) not in blob
    assert str(vault_root.resolve()) not in blob

    audit_text = (vault_root / "audit" / "restoration.jsonl").read_text(
        encoding="utf-8"
    )
    for needle in _RAW_NEEDLES:
        assert needle not in audit_text
    # The audit line records the user-supplied production_scope/profile
    # verbatim, which is *not* private — but vault_root must not leak.
    assert str(vault_root) not in audit_text
    assert str(vault_root.resolve()) not in audit_text


def test_unknown_profile_on_loaded_table_denies_with_policy_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault_root = tmp_path / "vault"
    doc_id = "doc-unknown-profile"
    _process_canonical(vault_root, doc_id=doc_id)

    calls: list[str] = []

    def boom(*args: object, **kwargs: object) -> None:
        calls.append("restore")
        raise AssertionError("kernel must not be called")

    monkeypatch.setattr(restoration_api, "restore", boom)

    req = RestorationRequest(
        **_base_kwargs(),
        document_id=doc_id,
        requested_entity_kinds=[EntityKind.PERSON],
        policy_profile="ghost-profile",
    )
    resp = restoration_request(
        req,
        scope=ResolverScope.PRIVATE_BOUNDARY,
        vault_root=vault_root,
        policy_table=_strict_table(),
    )
    assert resp.outcome == "failed"
    assert resp.reason is RestorationFailureReason.PolicyDenied
    assert calls == []

    # The deny path must still emit one audit record with the policy
    # columns populated, even when the deny is the "unknown profile"
    # special case (which reports the default row's name on
    # ``policy_matched_profile`` — see ``RestorationPolicyTable.evaluate``
    # for the rationale). The requested-but-unknown profile name shows
    # up in ``response.detail`` rather than the audit column.
    lines = _audit_lines(vault_root)
    assert len(lines) == 1
    rec = lines[0]
    assert rec["audit_record_id"] == resp.audit_record_id
    assert rec["outcome"] == "failed"
    assert rec["failure_reason"] == "policy_denied"
    assert rec["policy_verdict"] == "deny"
    assert rec["policy_matched_profile"] == "_default_local"
    assert resp.detail is not None
    assert "ghost-profile" in resp.detail


def test_policy_denied_target_via_public_handle_still_audits_document_id(
    tmp_path: Path,
) -> None:
    """The deny response carries ``document_id`` derived from the public
    handle, so the caller can correlate the denial with the artifact even
    when they only passed a handle."""
    vault_root = tmp_path / "vault"
    doc_id = "doc-via-handle-deny"
    _process_canonical(vault_root, doc_id=doc_id)

    req = RestorationRequest(
        **_base_kwargs(),
        target_public_handle=_public_handle(doc_id),
        requested_entity_kinds=[EntityKind.PERSON],
        policy_profile="strict",
        production_scope="production",  # not in strict's [internal, staging]
        authorization_decision="accept",
        approval_ticket="TKT-1234",
    )
    resp = restoration_request(
        req,
        scope=ResolverScope.PRIVATE_BOUNDARY,
        vault_root=vault_root,
        policy_table=_strict_table(),
    )
    assert resp.outcome == "failed"
    assert resp.reason is RestorationFailureReason.PolicyDenied
    assert resp.document_id == doc_id


# ---------------------------------------------------------------------------
# Permit path with a loaded table — kernel still called, audit records carry
# the matched profile name.
# ---------------------------------------------------------------------------


def test_permit_path_carries_matched_profile_in_audit(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    doc_id = "doc-permit"
    _process_canonical(vault_root, doc_id=doc_id)

    req = RestorationRequest(
        **_base_kwargs(),
        document_id=doc_id,
        requested_entity_kinds=[EntityKind.PERSON],
        policy_profile="strict",
        production_scope="internal",
        authorization_decision="accept",
        approval_ticket="TKT-1234",
    )
    resp = restoration_request(
        req,
        scope=ResolverScope.PRIVATE_BOUNDARY,
        vault_root=vault_root,
        policy_table=_strict_table(),
    )
    assert resp.outcome == "accepted"

    lines = _audit_lines(vault_root)
    assert len(lines) == 2  # intent + result
    for rec in lines:
        assert rec["audit_record_id"] == resp.audit_record_id
        assert rec["policy_verdict"] == "permit"
        assert rec["policy_matched_profile"] == "strict"


# ---------------------------------------------------------------------------
# Scope-denied path: policy table is not consulted (and no policy columns
# materialise on the audit record).
# ---------------------------------------------------------------------------


def test_scope_denied_short_circuits_before_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ScopeDenied wins over PolicyDenied — the scope gate is the earlier
    guard, so a non-PRIVATE_BOUNDARY scope must produce ``scope_denied``
    regardless of whether the policy would also have denied."""
    vault_root = tmp_path / "vault"
    doc_id = "doc-scope-vs-policy"
    _process_canonical(vault_root, doc_id=doc_id)

    calls: list[str] = []

    def boom(*args: object, **kwargs: object) -> None:
        calls.append("restore")
        raise AssertionError("kernel must not be called")

    monkeypatch.setattr(restoration_api, "restore", boom)

    req = RestorationRequest(
        **_base_kwargs(),
        document_id=doc_id,
        requested_entity_kinds=[EntityKind.PERSON],
        policy_profile="strict",  # would also fail the policy gate
        production_scope="production",
    )
    resp = restoration_request(
        req,
        scope=ResolverScope.ORDINARY_AGENT,
        vault_root=vault_root,
        policy_table=_strict_table(),
    )
    assert resp.outcome == "failed"
    assert resp.reason is RestorationFailureReason.ScopeDenied
    assert calls == []

    lines = _audit_lines(vault_root)
    assert len(lines) == 1
    assert lines[0]["failure_reason"] == "scope_denied"
    # Scope gate runs before policy evaluation; the audit columns for the
    # policy verdict/matched profile are therefore unset.
    assert lines[0]["policy_verdict"] is None
    assert lines[0]["policy_matched_profile"] is None
