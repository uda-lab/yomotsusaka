"""Interface tests for :mod:`yomotsusaka.execution_gateway`.

Per the #42 reconciliation, this PR is interface-only:

* :class:`ExecutionRequest` / :class:`ExecutionResponse` /
  :class:`ExecutionFailure` and :class:`ExecutionScope` are *declared*
  and exported via :data:`yomotsusaka.execution_gateway.__all__`.
* :meth:`ExecutionGateway.execute` continues to return the legacy
  ``{"status": "stub", ...}`` dict; the new models are NOT plumbed into
  it. The dispatcher that consumes them is owned by #43.

These tests pin the declared shape via direct construction. They do not
exercise a dispatcher because none exists yet.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from yomotsusaka import execution_gateway as eg
from yomotsusaka.boundary import PublicHandle, build_locator
from yomotsusaka.execution_gateway import (
    ExecutionFailure,
    ExecutionGateway,
    ExecutionRequest,
    ExecutionResponse,
    ExecutionScope,
)
from yomotsusaka.schemas import ArtifactHandle


# ---------------------------------------------------------------------------
# Module surface: __all__ exports the four new symbols + the legacy class
# ---------------------------------------------------------------------------


def test_module_all_exports_new_chikaeshi_symbols() -> None:
    """The four new symbols required by the #42 reconciliation must be
    reachable via the module's public ``__all__``."""
    expected = {
        "ExecutionScope",
        "ExecutionRequest",
        "ExecutionResponse",
        "ExecutionFailure",
        "ExecutionGateway",
    }
    assert expected.issubset(set(eg.__all__)), (
        f"execution_gateway.__all__ missing required symbols: "
        f"{expected - set(eg.__all__)!r}"
    )


# ---------------------------------------------------------------------------
# ExecutionScope: distinct from ResolverScope; the two documented values
# ---------------------------------------------------------------------------


def test_execution_scope_values_are_pinned() -> None:
    """The enum must carry exactly the two values the reconciliation pins;
    no ``AUDIT_REVIEWER`` (that belongs to :class:`ResolverScope`)."""
    assert ExecutionScope.PRIVATE_BOUNDARY.value == "private_boundary"
    assert ExecutionScope.ORDINARY_AGENT.value == "ordinary_agent"
    assert {m.name for m in ExecutionScope} == {"PRIVATE_BOUNDARY", "ORDINARY_AGENT"}


def test_execution_scope_is_distinct_from_resolver_scope() -> None:
    """The reconciliation forbids reusing :class:`ResolverScope`. The two
    enums must be different classes — even when both happen to carry a
    ``PRIVATE_BOUNDARY`` member, those members are different objects."""
    from yomotsusaka.boundary import ResolverScope

    assert ExecutionScope is not ResolverScope
    # The PRIVATE_BOUNDARY members coexist but are different enum members.
    assert ExecutionScope.PRIVATE_BOUNDARY is not ResolverScope.PRIVATE_BOUNDARY


def test_execution_scope_is_str_subclass() -> None:
    """The enum is a ``str`` subclass (matches the project convention of
    serialising as wire-stable strings)."""
    assert isinstance(ExecutionScope.PRIVATE_BOUNDARY, str)
    assert isinstance(ExecutionScope.ORDINARY_AGENT, str)


# ---------------------------------------------------------------------------
# ExecutionRequest: extra="forbid", frozen=True, required-field validation
# ---------------------------------------------------------------------------


def _valid_request_kwargs() -> dict[str, object]:
    return {
        "job_name": "summarise_private_minutes",
        "purpose": "weekly-review",
        "scope": ExecutionScope.PRIVATE_BOUNDARY,
        "inputs": {"target_handle": "private://agent_redacted/manifest/doc-001"},
    }


def test_execution_request_constructs_with_valid_kwargs() -> None:
    req = ExecutionRequest(**_valid_request_kwargs())
    assert req.job_name == "summarise_private_minutes"
    assert req.purpose == "weekly-review"
    assert req.scope is ExecutionScope.PRIVATE_BOUNDARY
    assert req.inputs == {
        "target_handle": "private://agent_redacted/manifest/doc-001"
    }


def test_execution_request_rejects_extra_fields() -> None:
    """``ConfigDict(extra="forbid")`` must reject unknown fields."""
    kwargs = _valid_request_kwargs()
    kwargs["unknown_field"] = "anything"
    with pytest.raises(ValidationError):
        ExecutionRequest(**kwargs)


def test_execution_request_is_frozen() -> None:
    """``frozen=True`` must prevent post-construction mutation."""
    req = ExecutionRequest(**_valid_request_kwargs())
    with pytest.raises(ValidationError):
        req.job_name = "another-job"  # type: ignore[misc]


def test_execution_request_inputs_defaults_to_empty_dict() -> None:
    kwargs = _valid_request_kwargs()
    del kwargs["inputs"]
    req = ExecutionRequest(**kwargs)
    assert req.inputs == {}


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_execution_request_rejects_blank_job_name(blank: str) -> None:
    kwargs = _valid_request_kwargs()
    kwargs["job_name"] = blank
    with pytest.raises(ValidationError):
        ExecutionRequest(**kwargs)


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_execution_request_rejects_blank_purpose(blank: str) -> None:
    kwargs = _valid_request_kwargs()
    kwargs["purpose"] = blank
    with pytest.raises(ValidationError):
        ExecutionRequest(**kwargs)


def test_execution_request_requires_execution_scope_enum() -> None:
    """The ``scope`` field must reject arbitrary strings that do not match
    an :class:`ExecutionScope` member."""
    kwargs = _valid_request_kwargs()
    kwargs["scope"] = "not-a-scope"
    with pytest.raises(ValidationError):
        ExecutionRequest(**kwargs)


# ---------------------------------------------------------------------------
# ExecutionResponse: extra="forbid", frozen=True, opaque artifacts
# ---------------------------------------------------------------------------


def _valid_response_kwargs() -> dict[str, object]:
    return {
        "audit_record_id": "audit-001",
        "status": "stub",
        "artifacts": [],
        "scrubbed_stdout": "",
        "scrubbed_stderr": "",
    }


def test_execution_response_constructs_with_valid_kwargs() -> None:
    resp = ExecutionResponse(**_valid_response_kwargs())
    assert resp.audit_record_id == "audit-001"
    assert resp.status == "stub"
    assert resp.artifacts == []
    assert resp.scrubbed_stdout == ""
    assert resp.scrubbed_stderr == ""


def test_execution_response_rejects_extra_fields() -> None:
    kwargs = _valid_response_kwargs()
    kwargs["raw_value"] = "leaked"
    with pytest.raises(ValidationError):
        ExecutionResponse(**kwargs)


def test_execution_response_is_frozen() -> None:
    resp = ExecutionResponse(**_valid_response_kwargs())
    with pytest.raises(ValidationError):
        resp.status = "accepted"  # type: ignore[misc]


def test_execution_response_carries_only_public_handles() -> None:
    """``artifacts`` must be a list of :class:`PublicHandle`; the field
    type is what enforces the "no raw private artifact" invariant in this
    interface-only PR."""
    locator = build_locator(
        exposure_class="agent_redacted",
        artifact_kind="manifest",
        opaque_id="exec-output-001",
    )
    resp = ExecutionResponse(
        audit_record_id="audit-002",
        status="accepted",
        artifacts=[PublicHandle(locator=locator)],
    )
    assert len(resp.artifacts) == 1
    assert isinstance(resp.artifacts[0], PublicHandle)
    assert resp.artifacts[0].locator == locator


def test_execution_response_rejects_artifact_handle_in_artifacts() -> None:
    """A bare :class:`ArtifactHandle` (the private-side kernel type that
    carries ``vault_path``) must not pass validation for the public
    ``artifacts`` field."""
    bad = ArtifactHandle(doc_id="x", vault_path="/private/x.json")
    with pytest.raises(ValidationError):
        ExecutionResponse(
            audit_record_id="audit-003",
            status="accepted",
            artifacts=[bad],  # type: ignore[list-item]
        )


def test_execution_response_serialisation_omits_vault_paths() -> None:
    """Sanity: the declared response serialisation never carries a
    ``vault_path`` field name — :class:`PublicHandle` is the only artifact
    type and it deliberately strips it."""
    locator = build_locator(
        exposure_class="agent_redacted",
        artifact_kind="manifest",
        opaque_id="exec-output-002",
    )
    resp = ExecutionResponse(
        audit_record_id="audit-004",
        status="accepted",
        artifacts=[PublicHandle(locator=locator)],
    )
    blob = resp.model_dump_json()
    assert "vault_path" not in blob
    assert "/private/" not in blob


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_execution_response_rejects_blank_audit_record_id(blank: str) -> None:
    kwargs = _valid_response_kwargs()
    kwargs["audit_record_id"] = blank
    with pytest.raises(ValidationError):
        ExecutionResponse(**kwargs)


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_execution_response_rejects_blank_status(blank: str) -> None:
    kwargs = _valid_response_kwargs()
    kwargs["status"] = blank
    with pytest.raises(ValidationError):
        ExecutionResponse(**kwargs)


# ---------------------------------------------------------------------------
# ExecutionFailure: Exception subclass, not yet raised by this module
# ---------------------------------------------------------------------------


def test_execution_failure_is_exception_subclass() -> None:
    assert issubclass(ExecutionFailure, Exception)
    # And NOT a subclass of unrelated exception types so callers can
    # ``except ExecutionFailure`` narrowly.
    assert not issubclass(ExecutionFailure, ValueError)
    assert not issubclass(ExecutionFailure, RuntimeError)


def test_execution_failure_is_raisable() -> None:
    with pytest.raises(ExecutionFailure) as excinfo:
        raise ExecutionFailure("policy-denied: unknown template job")
    assert "policy-denied" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Legacy execute() — behaviour preserved by #42
# ---------------------------------------------------------------------------


def test_execute_returns_legacy_stub_dict() -> None:
    """Per the #42 reconciliation, :meth:`execute` continues to return the
    pre-#42 ``{"status": "stub", "handle_id": ..., "operation": ...}``
    dict. The new models are NOT plumbed into this method; #43 owns that."""
    gateway = ExecutionGateway()
    handle = ArtifactHandle(doc_id="doc-x", vault_path="/tmp/private/doc-x.json")
    result = gateway.execute(handle, "summarise", {"length": 100})

    assert isinstance(result, dict)
    assert result["status"] == "stub"
    assert result["handle_id"] == handle.handle_id
    assert result["operation"] == "summarise"


def test_execute_does_not_return_execution_response() -> None:
    """Negative pin: the legacy stub must NOT yet return an
    :class:`ExecutionResponse`. If a future refactor changes the return
    type, that change belongs to #43 (which also rewrites this test)."""
    gateway = ExecutionGateway()
    handle = ArtifactHandle(doc_id="doc-y", vault_path="/tmp/private/doc-y.json")
    result = gateway.execute(handle, "translate")
    assert not isinstance(result, ExecutionResponse)
