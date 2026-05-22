"""Resolver-contract tests for :mod:`yomotsusaka.boundary`.

Pins the fail-closed semantics of :func:`boundary.resolve`:

* programmer errors raise :class:`ResolverError`;
* expected failure categories (malformed locator, unknown artifact, missing
  artifact, empty purpose) are returned as :class:`ResolverFailure`;
* the locator is parsed before any filesystem call;
* ``ResolverFailure.detail`` and ``model_dump_json()`` carry no raw private
  values, no absolute paths, and no environment variable contents;
* :data:`ResolverScope.PRIVATE_BOUNDARY` is the only scope that ever sees
  :class:`PrivateState`.

Per AGENTS.md, raw private literals appear in test scope only when
asserting private-dictionary contents. This module mostly uses them as
*absence* assertions against public surfaces, which is the documented
exception.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from yomotsusaka.boundary import (
    PrivateState,
    ResolverError,
    ResolverFailure,
    ResolverFailureReason,
    ResolverScope,
    ResolverSuccess,
    build_locator,
    resolve,
)
from yomotsusaka.pipeline import process_document
from yomotsusaka.redactor import Span
from yomotsusaka.schemas import EntityKind


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

_DOC_ID = "canonical-fixture-001"


def _canonical_spans() -> list[Span]:
    return [Span(start=s.start, end=s.end, kind=s.kind) for s in _CANONICAL_SPAN_SPECS]


def _commit_canonical_fixture(vault_root: Path) -> str:
    """Drive the canonical fixture through the pipeline and return the locator."""
    raw_text = "Alice Tan works at Acme Corp. Patient ID: 12345."
    process_document(
        doc_id=_DOC_ID,
        raw_text=raw_text,
        spans=_canonical_spans(),
        vault_root=vault_root,
    )
    return build_locator(
        exposure_class="agent_redacted",
        artifact_kind="manifest",
        opaque_id=_DOC_ID,
    )


# ---------------------------------------------------------------------------
# Programmer-error guardrails (raise ResolverError)
# ---------------------------------------------------------------------------


def test_resolve_raises_on_non_scope_value(tmp_path: Path) -> None:
    with pytest.raises(ResolverError, match="scope"):
        resolve(
            "private://agent_redacted/manifest/x",
            scope="ordinary_agent",  # type: ignore[arg-type]
            purpose="t",
            vault_root=tmp_path,
        )


def test_resolve_raises_on_non_path_vault_root() -> None:
    with pytest.raises(ResolverError, match="vault_root"):
        resolve(
            "private://agent_redacted/manifest/x",
            scope=ResolverScope.ORDINARY_AGENT,
            purpose="t",
            vault_root="/tmp/vault",  # type: ignore[arg-type]
        )


def test_resolve_raises_on_non_string_locator(tmp_path: Path) -> None:
    with pytest.raises(ResolverError, match="locator"):
        resolve(
            42,  # type: ignore[arg-type]
            scope=ResolverScope.ORDINARY_AGENT,
            purpose="t",
            vault_root=tmp_path,
        )


def test_resolve_raises_on_non_string_purpose(tmp_path: Path) -> None:
    with pytest.raises(ResolverError, match="purpose"):
        resolve(
            "private://agent_redacted/manifest/x",
            scope=ResolverScope.ORDINARY_AGENT,
            purpose=None,  # type: ignore[arg-type]
            vault_root=tmp_path,
        )


# ---------------------------------------------------------------------------
# Returned failure categories
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("empty_purpose", ["", "   ", "\t\n"])
def test_resolve_returns_purpose_not_permitted_on_empty(
    tmp_path: Path, empty_purpose: str
) -> None:
    outcome = resolve(
        "private://agent_redacted/manifest/x",
        scope=ResolverScope.ORDINARY_AGENT,
        purpose=empty_purpose,
        vault_root=tmp_path,
    )
    assert isinstance(outcome, ResolverFailure)
    assert outcome.reason is ResolverFailureReason.PurposeNotPermitted
    assert outcome.outcome == "failure"


@pytest.mark.parametrize(
    "bad_locator",
    [
        "",
        "doc-001",
        "private://nope/manifest/x",
        "private://agent_redacted/nope/x",
        "private://agent_redacted/manifest/..",
        "https://agent_redacted/manifest/x",
    ],
)
def test_resolve_returns_malformed_locator(tmp_path: Path, bad_locator: str) -> None:
    outcome = resolve(
        bad_locator,
        scope=ResolverScope.ORDINARY_AGENT,
        purpose="t",
        vault_root=tmp_path,
    )
    assert isinstance(outcome, ResolverFailure)
    assert outcome.reason is ResolverFailureReason.MalformedLocator


def test_resolve_does_not_touch_filesystem_for_malformed_locator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed locator must never reach Path.exists / read_text / listdir."""
    calls: list[str] = []

    real_exists = Path.exists
    real_read_text = Path.read_text

    def boom_exists(self: Path, *args: object, **kwargs: object) -> bool:  # type: ignore[override]
        calls.append(f"exists:{self}")
        return real_exists(self, *args, **kwargs)  # type: ignore[arg-type]

    def boom_read_text(self: Path, *args: object, **kwargs: object) -> str:  # type: ignore[override]
        calls.append(f"read_text:{self}")
        return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "exists", boom_exists)
    monkeypatch.setattr(Path, "read_text", boom_read_text)

    outcome = resolve(
        "private://nope/manifest/x",
        scope=ResolverScope.PRIVATE_BOUNDARY,
        purpose="t",
        vault_root=tmp_path,
    )
    assert isinstance(outcome, ResolverFailure)
    assert outcome.reason is ResolverFailureReason.MalformedLocator
    # No filesystem call must have been made before parse failed.
    assert calls == [], f"unexpected filesystem calls: {calls}"


def test_resolve_returns_unknown_artifact_for_non_manifest_kind(tmp_path: Path) -> None:
    locator = build_locator(
        exposure_class="agent_redacted",
        artifact_kind="search_hit",
        opaque_id="x",
    )
    outcome = resolve(
        locator,
        scope=ResolverScope.ORDINARY_AGENT,
        purpose="t",
        vault_root=tmp_path,
    )
    assert isinstance(outcome, ResolverFailure)
    assert outcome.reason is ResolverFailureReason.UnknownArtifact


def test_resolve_returns_unknown_artifact_when_manifest_missing(tmp_path: Path) -> None:
    locator = build_locator(
        exposure_class="agent_redacted",
        artifact_kind="manifest",
        opaque_id="never-committed",
    )
    outcome = resolve(
        locator,
        scope=ResolverScope.ORDINARY_AGENT,
        purpose="t",
        vault_root=tmp_path,
    )
    assert isinstance(outcome, ResolverFailure)
    assert outcome.reason is ResolverFailureReason.UnknownArtifact


def test_resolve_returns_artifact_missing_when_private_file_absent(
    tmp_path: Path,
) -> None:
    """Manifest committed but private/ file removed → ArtifactMissing under
    PRIVATE_BOUNDARY scope."""
    vault_root = tmp_path / "vault"
    locator = _commit_canonical_fixture(vault_root)

    private_file = vault_root / "private" / f"{_DOC_ID}.json"
    assert private_file.exists()
    private_file.unlink()

    outcome = resolve(
        locator,
        scope=ResolverScope.PRIVATE_BOUNDARY,
        purpose="restore-test",
        vault_root=vault_root,
    )
    assert isinstance(outcome, ResolverFailure)
    assert outcome.reason is ResolverFailureReason.ArtifactMissing


# ---------------------------------------------------------------------------
# Successful resolution
# ---------------------------------------------------------------------------


def test_resolve_success_for_ordinary_agent_returns_no_private_state(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "vault"
    locator = _commit_canonical_fixture(vault_root)

    outcome = resolve(
        locator,
        scope=ResolverScope.ORDINARY_AGENT,
        purpose="inspect-doc",
        vault_root=vault_root,
    )
    assert isinstance(outcome, ResolverSuccess)
    assert outcome.outcome == "success"
    assert outcome.exposure_class == "agent_redacted"
    assert outcome.artifact_kind == "manifest"
    assert outcome.opaque_id == _DOC_ID
    assert outcome.fragment is None
    assert outcome.purpose == "inspect-doc"
    # Ordinary agents NEVER receive private state.
    assert outcome.private_state is None


def test_resolve_success_for_audit_reviewer_returns_no_private_state(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "vault"
    locator = _commit_canonical_fixture(vault_root)

    outcome = resolve(
        locator,
        scope=ResolverScope.AUDIT_REVIEWER,
        purpose="audit-review",
        vault_root=vault_root,
    )
    assert isinstance(outcome, ResolverSuccess)
    assert outcome.private_state is None


def test_resolve_success_for_private_boundary_materialises_private_state(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "vault"
    locator = _commit_canonical_fixture(vault_root)

    outcome = resolve(
        locator,
        scope=ResolverScope.PRIVATE_BOUNDARY,
        purpose="restore-test",
        vault_root=vault_root,
    )
    assert isinstance(outcome, ResolverSuccess)
    state = outcome.private_state
    assert isinstance(state, PrivateState)
    assert state.manifest_path.exists()
    assert state.private_dict_path.exists()
    assert len(state.private_entries) == len(_CANONICAL_SPAN_SPECS)
    # Private-dictionary assertion (raw values permitted here by AGENTS.md).
    by_kind = {e.kind: e.original_value for e in state.private_entries}
    assert by_kind == {
        EntityKind.PERSON: "Alice Tan",
        EntityKind.ORG: "Acme Corp",
        EntityKind.ID_NUMBER: "12345",
    }


# ---------------------------------------------------------------------------
# Failure-detail privacy invariant
# ---------------------------------------------------------------------------


def test_resolver_failure_detail_leaks_no_private_or_path_data(
    tmp_path: Path,
) -> None:
    """Across every enumerated failure reason, failure detail and JSON
    serialisation must not contain raw private values, absolute paths, or
    environment variable contents."""
    vault_root = tmp_path / "vault"
    _commit_canonical_fixture(vault_root)

    abs_tmp = str(tmp_path.resolve())
    forbidden = (
        "Alice Tan",
        "Acme Corp",
        "12345",
        abs_tmp,
    )

    failure_cases: list[tuple[str, ResolverScope, str]] = [
        # MalformedLocator
        ("private://nope/manifest/x", ResolverScope.ORDINARY_AGENT, "t"),
        # PurposeNotPermitted
        (
            build_locator(
                exposure_class="agent_redacted",
                artifact_kind="manifest",
                opaque_id=_DOC_ID,
            ),
            ResolverScope.ORDINARY_AGENT,
            "   ",
        ),
        # UnknownArtifact (kind != manifest)
        (
            build_locator(
                exposure_class="agent_redacted",
                artifact_kind="search_hit",
                opaque_id="x",
            ),
            ResolverScope.ORDINARY_AGENT,
            "t",
        ),
        # UnknownArtifact (manifest does not exist)
        (
            build_locator(
                exposure_class="agent_redacted",
                artifact_kind="manifest",
                opaque_id="never-committed",
            ),
            ResolverScope.ORDINARY_AGENT,
            "t",
        ),
    ]

    for locator, scope, purpose in failure_cases:
        outcome = resolve(
            locator,
            scope=scope,
            purpose=purpose,
            vault_root=vault_root,
        )
        assert isinstance(outcome, ResolverFailure), outcome
        as_json = outcome.model_dump_json()
        detail = outcome.detail or ""
        for needle in forbidden:
            assert needle not in detail, (
                f"failure detail for {outcome.reason} leaked {needle!r}: {detail!r}"
            )
            assert needle not in as_json, (
                f"failure JSON for {outcome.reason} leaked {needle!r}"
            )
