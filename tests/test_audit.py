"""Tests for :mod:`yomotsusaka.audit` (Fork 3 of #43).

Pins the audit-record schema invariants from §D-5 of the metaplan:

1. Append-only JSONL at ``<vault_root>/audit/restoration.jsonl``.
2. Raw-value leakage detection via pre-write scrubber re-check.
3. ``"deferred"`` outcome is rejected by the Literal type.
4. Reserved ``policy_profile`` / ``approval_ticket`` always ``None``
   in #43 records.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from yomotsusaka.audit import (
    AuditError,
    AuditRecord,
    read_records,
    write_record,
)
from yomotsusaka.schemas import EntityKind, PrivateDictEntry

from tests._exposure_denylist import RAW_VALUES


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_record(**overrides: object) -> AuditRecord:
    defaults: dict[str, object] = dict(
        ts=datetime.now(timezone.utc),
        request_id="req-001",
        template_name="summarise_private_minutes",
        caller_scope="private_boundary",
        purpose="audit-test",
        locator="private://agent_redacted/manifest/doc-001",
        outcome="success",
        artifact_locators=[],
        resolver_reason=None,
        detail=None,
        policy_profile=None,
        approval_ticket=None,
    )
    defaults.update(overrides)
    return AuditRecord(**defaults)  # type: ignore[arg-type]


def _make_canonical_entries() -> list[PrivateDictEntry]:
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
# Schema invariants
# ---------------------------------------------------------------------------


def test_audit_record_constructs_with_valid_kwargs() -> None:
    record = _make_record()
    assert record.outcome == "success"
    assert record.policy_profile is None
    assert record.approval_ticket is None


def test_audit_record_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        _make_record(unknown_field="leak")  # type: ignore[arg-type]


def test_audit_record_is_frozen() -> None:
    record = _make_record()
    with pytest.raises(ValidationError):
        record.outcome = "scope_denied"  # type: ignore[misc]


def test_audit_record_rejects_deferred_outcome() -> None:
    """``"deferred"`` is reserved for the legacy ``RestorationResponse``
    path; the execution-gateway audit must never use it."""
    with pytest.raises(ValidationError):
        _make_record(outcome="deferred")


@pytest.mark.parametrize(
    "outcome",
    [
        "success",
        "scope_denied",
        "purpose_not_permitted",
        "template_not_found",
        "schema_invalid",
        "scrub_failed",
        "template_raised",
        "artifact_missing",
    ],
)
def test_audit_record_accepts_every_valid_outcome(outcome: str) -> None:
    record = _make_record(outcome=outcome)
    assert record.outcome == outcome


def test_audit_record_rejects_blank_request_id() -> None:
    with pytest.raises(ValidationError):
        _make_record(request_id="   ")


def test_audit_record_rejects_blank_template_name() -> None:
    with pytest.raises(ValidationError):
        _make_record(template_name="")


# ---------------------------------------------------------------------------
# write_record: append + read round-trip
# ---------------------------------------------------------------------------


def test_write_record_appends_jsonl(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    record1 = _make_record(request_id="req-001", outcome="success")
    record2 = _make_record(request_id="req-002", outcome="scope_denied")
    write_record(record1, vault)
    write_record(record2, vault)

    audit_path = vault / "audit" / "restoration.jsonl"
    lines = audit_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    assert parsed[0]["request_id"] == "req-001"
    assert parsed[1]["request_id"] == "req-002"


def test_write_record_creates_audit_dir(tmp_path: Path) -> None:
    """A vault root that does not yet have an ``audit/`` subdirectory
    must be auto-created by ``write_record``."""
    vault = tmp_path / "fresh-vault"
    record = _make_record()
    write_record(record, vault)
    assert (vault / "audit" / "restoration.jsonl").exists()


def test_read_records_round_trips(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    originals = [
        _make_record(request_id=f"req-{i:03d}", outcome="success")
        for i in range(3)
    ]
    for r in originals:
        write_record(r, vault)
    recovered = read_records(vault)
    assert len(recovered) == 3
    assert [r.request_id for r in recovered] == [
        "req-000",
        "req-001",
        "req-002",
    ]


def test_read_records_returns_empty_when_file_missing(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    assert read_records(vault) == []


# ---------------------------------------------------------------------------
# Raw-value leakage detection
# ---------------------------------------------------------------------------


def test_write_record_rejects_raw_value_in_detail(tmp_path: Path) -> None:
    """Construct a record whose ``detail`` field embeds a raw value
    against a non-empty ``private_dict``. The pre-write scrubber re-check
    must catch it and refuse to write."""
    vault = tmp_path / "vault"
    entries = _make_canonical_entries()
    # Embed the raw value "Alice Tan" directly. After scrubber pass 1
    # this would normally be masked, but the re-check ensures it's gone
    # AFTER scrubbing. Construct a case where the value reappears: use
    # the pathological AA/A overlap from test_scrubber.
    bad_entries = [
        PrivateDictEntry(
            key="A",
            original_value="AA",
            kind=EntityKind.CUSTOM,
        )
    ]
    record = _make_record(detail="AAAA - leak")
    with pytest.raises(AuditError):
        write_record(record, vault, private_dict=bad_entries)

    # File MUST NOT have been created (no partial write).
    audit_path = vault / "audit" / "restoration.jsonl"
    assert not audit_path.exists()
    _ = entries  # noqa: F841 — keeps the canonical-fixture reference in scope


def test_write_record_succeeds_when_no_raw_value_in_detail(
    tmp_path: Path,
) -> None:
    """Detail that contains only KEYS (no raw values) passes the
    pre-write re-check."""
    vault = tmp_path / "vault"
    entries = _make_canonical_entries()
    record = _make_record(
        detail="template referenced key <PERSON_a5f4ff58>"
    )
    write_record(record, vault, private_dict=entries)
    audit_path = vault / "audit" / "restoration.jsonl"
    assert audit_path.exists()
    # The persisted line must contain NO raw value.
    contents = audit_path.read_text(encoding="utf-8")
    for raw in RAW_VALUES:
        assert raw not in contents


# ---------------------------------------------------------------------------
# Reserved fields (#44)
# ---------------------------------------------------------------------------


def test_audit_record_reserves_policy_fields_as_none(tmp_path: Path) -> None:
    """Per the reconciliation, this PR's records always set
    ``policy_profile`` and ``approval_ticket`` to ``None``. Test the
    invariant at the schema level (the field defaults are ``None``)."""
    record = _make_record()
    assert record.policy_profile is None
    assert record.approval_ticket is None

    # Round-trip through the file: persisted fields stay None.
    vault = tmp_path / "vault"
    write_record(record, vault)
    recovered = read_records(vault)
    assert recovered[0].policy_profile is None
    assert recovered[0].approval_ticket is None


def test_audit_record_accepts_explicit_policy_fields_for_future_44(
    tmp_path: Path,
) -> None:
    """The schema MUST accept non-None policy_profile / approval_ticket
    (so #44 can populate them later without a migration). This PR's
    dispatcher always passes ``None``; the assertion of None-ness for
    PR-43 records lives in :mod:`tests.test_execution_gateway`."""
    record = _make_record(
        policy_profile="some-profile",
        approval_ticket="some-ticket",
    )
    assert record.policy_profile == "some-profile"
    assert record.approval_ticket == "some-ticket"


# ---------------------------------------------------------------------------
# OSError propagation (issue #67 / #75)
# ---------------------------------------------------------------------------


def test_write_record_propagates_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #67 (absorbed by #75): pin :func:`write_record`'s OSError
    propagation contract.

    Per the :func:`write_record` docstring: "OSError on filesystem
    failure (parent unwritable, disk full, etc.). The caller is expected
    to map this to a structured failure response." The MVP-2 callers
    (notably :func:`boundary.restoration_request` and
    :func:`boundary.execute_request`) DEPEND on this contract — they
    catch ``OSError`` explicitly to translate into
    :data:`RestorationFailureReason.AuditWriteFailed` /
    :data:`ExecutionFailureReason.AuditWriteFailed` outcomes (see
    `boundary.py` steps (c)/(d) and `execution_gateway` audit-first
    branches). Pinning the contract here ensures a future refactor that
    swallows or re-raises a different exception type is caught loudly.

    Strategy: monkeypatch :meth:`pathlib.Path.open` to raise ``OSError``
    when invoked in append-mode on the audit file, then assert:

    1. The exception propagates (no swallowing).
    2. The audit file is NOT created (no partial state — the scrubber
       re-check completes first, then the ``open`` failure stops the
       call before any byte is written).
    """
    vault = tmp_path / "vault"
    record = _make_record()

    # Pre-create the audit directory so the directory-creation step
    # succeeds; the failure surface we want to pin is the ``open`` call
    # in append mode, NOT the ``mkdir`` step (which has its own
    # ``exist_ok=True`` contract).
    audit_dir = vault / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit_path = audit_dir / "restoration.jsonl"

    real_open = Path.open

    def _fail_open(self: Path, *args: object, **kwargs: object) -> object:
        # Fail only when opening the specific audit file for writing.
        # Other ``Path.open`` calls (e.g. for read in tests, or for the
        # ``mkdir``-internal probe) keep their original behaviour.
        mode = ""
        if args:
            mode_arg = args[0]
            if isinstance(mode_arg, str):
                mode = mode_arg
        mode = str(kwargs.get("mode", mode))
        if self == audit_path and "a" in mode:
            raise OSError("simulated audit-file write failure")
        return real_open(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "open", _fail_open)

    with pytest.raises(OSError, match="simulated audit-file write failure"):
        write_record(record, vault)

    # The append target must not exist (no partial-line state). If the
    # test setup pre-created the directory only, the file itself must
    # remain absent.
    assert not audit_path.exists(), (
        "OSError on append must not leave a partial restoration.jsonl on "
        "disk; the writer is expected to fail before the first byte is "
        "appended"
    )


@pytest.mark.skip(
    reason=(
        "Issue #67 (absorbed by #75): single-process invariant per "
        "architecture §11.3; the multi-process concurrent-write contract "
        "is future work (no file lock today). Placeholder stub kept so a "
        "future locking PR has an existing test slot to populate."
    )
)
def test_write_record_concurrent_writes_future_work() -> None:
    """Placeholder for the concurrent-writes contract.

    The current writer takes no file lock; ``docs/architecture.md``
    §11.3 documents a single-process invariant. A future child that adds
    a lock (or migrates to an SQLite-backed audit store) should populate
    this slot with a real concurrent-writer test asserting that two
    interleaved :func:`write_record` calls produce two distinct lines
    with no torn writes.

    This stub is intentionally a hard ``skip``, not an ``xfail`` —
    there is no implementation today and no agreed semantics to assert
    against, so even an ``xfail`` would be ambiguous.
    """
    raise NotImplementedError(
        "concurrent-write contract pending; see docstring for re-introduction plan"
    )
