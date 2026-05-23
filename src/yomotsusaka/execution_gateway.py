"""
Execution gateway — Chikaeshi private execution boundary (deferred).

This module reserves the interface for the private execution gateway defined
in :doc:`architecture <../docs/architecture>` §13 and specified in
:doc:`chikaeshi <../docs/chikaeshi>`. Real dispatch is owned by the sibling
issue #43; this file is interface-only.

What this module declares (per the #42 reconciliation):

* :class:`ExecutionScope` — new ``str`` enum with values
  ``PRIVATE_BOUNDARY`` and ``ORDINARY_AGENT``. Does NOT reuse
  :class:`yomotsusaka.boundary.ResolverScope`; execution scope and locator
  resolution scope evolve independently and must not be coupled.
* :class:`ExecutionRequest` — frozen Pydantic v2 model carrying the
  template-job name, purpose tag, caller scope, and opaque inputs.
* :class:`ExecutionResponse` — frozen Pydantic v2 model carrying only
  opaque public handles, scrubbed text fragments, and the audit record id.
  Never carries raw private values, vault paths, or non-opaque identifiers.
* :class:`ExecutionFailure` — exception subclass of :class:`Exception`
  raised on policy/dispatch errors. Reserved for #43's dispatcher; this
  module does not raise it.

What this module deliberately does NOT do:

* :meth:`ExecutionGateway.execute` still returns the legacy
  ``{"status": "stub", "handle_id": ..., "operation": ...}`` dict. The new
  request/response models are *declared* and exported via ``__all__``, but
  are NOT plumbed into ``execute()``. Plumbing is owned by #43.
* No new agent-facing entry point is added to
  :mod:`yomotsusaka.boundary` or :mod:`yomotsusaka.facade`. The Chikaeshi
  request surface (``boundary.execute_request``, ``LocalFacade.execute``,
  etc.) lands in #43 (or later).
* Real policy enforcement, real template-job dispatch, real container
  execution, and real audit-record emission remain stubs. The classification
  in :doc:`scaffold-status <../docs/scaffold-status>` stays ``deferred``.

See ``docs/scaffold-status.md`` for module status and
``docs/backend-promotion.md`` §4 for the promotion gate.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from yomotsusaka.boundary import PublicHandle
from yomotsusaka.schemas import ArtifactHandle

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Execution scope (new enum; do NOT reuse ResolverScope)
# ---------------------------------------------------------------------------


class ExecutionScope(str, Enum):
    """Caller scope for the Chikaeshi private execution gateway.

    Deliberately distinct from :class:`yomotsusaka.boundary.ResolverScope`:

    * ``ResolverScope.AUDIT_REVIEWER`` has no operational meaning for
      execution (a reviewer does not dispatch template jobs).
    * Coupling the two enums would force locator-resolution test coverage
      to expand whenever execution policy evolves, and vice versa.

    Specification only; not yet enforced. The two values mirror the
    coarse split the dispatcher in #43 will need:

    * :data:`PRIVATE_BOUNDARY` — caller is the private-boundary service or
      a delegate trusted to invoke template jobs that touch private data.
    * :data:`ORDINARY_AGENT` — caller is an ordinary agent. The gate in
      #43 will refuse template jobs whose ``allowed_scopes`` do not include
      this value.
    """

    PRIVATE_BOUNDARY = "private_boundary"
    ORDINARY_AGENT = "ordinary_agent"


# ---------------------------------------------------------------------------
# Request / response / failure models
# ---------------------------------------------------------------------------


class ExecutionRequest(BaseModel):
    """Public-side request to dispatch a template job through the gateway.

    Specification only; not yet enforced. The fields below are the shape
    #43's dispatcher will validate. This MVP-3 spec PR pins the shape so
    that PR can land without renegotiating it.

    Privacy invariant
    -----------------
    No field may carry a raw private value, an absolute filesystem path,
    or a non-opaque identifier. ``inputs`` is a free-form dict only for
    template-supplied opaque payloads (e.g. ``{"target_handle":
    "private://..."}``); the dispatcher in #43 will type-check each
    template's expected schema.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_name: str = Field(
        description="Name of a registered template job (see "
        "``docs/chikaeshi.md`` §1 for the registry shape).",
    )
    purpose: str = Field(
        description="Free-form, required, non-empty after ``.strip()``. "
        "Recorded on the audit record for the gateway-mediated restoration. "
        "Empty/whitespace ⇒ the dispatcher returns "
        ":class:`ExecutionFailure` (specification only; not yet enforced).",
    )
    scope: ExecutionScope = Field(
        description="Caller scope. The dispatcher in #43 checks this "
        "against the template's ``allowed_scopes``.",
    )
    inputs: dict[str, Any] = Field(
        default_factory=dict,
        description="Template-specific opaque inputs. Each value must be a "
        "public-safe primitive or an opaque locator string; raw private "
        "values are forbidden.",
    )

    @field_validator("job_name")
    @classmethod
    def _job_name_non_empty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("job_name must be non-empty after strip")
        return v

    @field_validator("purpose")
    @classmethod
    def _purpose_non_empty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("purpose must be non-empty after strip")
        return v


class ExecutionResponse(BaseModel):
    """Public-side response from the gateway.

    Specification only; not yet enforced. The response carries only
    opaque handles and scrubbed text fragments. It NEVER carries:

    * raw private values from a template's output,
    * :attr:`ArtifactHandle.vault_path` or any absolute path,
    * non-opaque job/output identifiers (every artifact reference must be
      a :class:`PublicHandle` whose locator parses via
      :func:`yomotsusaka.boundary.parse_locator`).

    The ``status`` field is a closed string set. The ``"stub"`` value is
    reserved for the legacy :meth:`ExecutionGateway.execute` return shape
    described in :doc:`chikaeshi <../docs/chikaeshi>` §6; #43's dispatcher
    will return ``"accepted"`` / ``"failed"`` instead.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    audit_record_id: str = Field(
        description="Opaque correlation id for the gateway-mediated audit "
        "record; ties the response back to ``<vault_root>/audit/"
        "restoration.jsonl`` (see ``docs/chikaeshi.md`` §4).",
    )
    status: str = Field(
        description="One of the closed status values defined in "
        "``docs/chikaeshi.md`` §6. The legacy stub returns ``\"stub\"``; "
        "#43's dispatcher returns ``\"accepted\"`` / ``\"failed\"``.",
    )
    artifacts: list[PublicHandle] = Field(
        default_factory=list,
        description="Opaque public handles for any artifacts the template "
        "produced. The dispatcher MUST NOT include raw bytes or "
        "private-side paths here.",
    )
    scrubbed_stdout: str = Field(
        default="",
        description="Scrubbed stdout fragment; the scrubber contract is "
        "defined in ``docs/chikaeshi.md`` §3. Empty in the legacy stub.",
    )
    scrubbed_stderr: str = Field(
        default="",
        description="Scrubbed stderr fragment; same contract as "
        "``scrubbed_stdout``.",
    )

    @field_validator("audit_record_id")
    @classmethod
    def _audit_record_id_non_empty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("audit_record_id must be non-empty after strip")
        return v

    @field_validator("status")
    @classmethod
    def _status_non_empty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("status must be non-empty after strip")
        return v


class ExecutionFailure(Exception):
    """Raised by the future #43 dispatcher on policy or dispatch errors.

    Specification only; not yet raised. Reserved as the single exception
    type the Chikaeshi dispatcher will throw for:

    * unknown template job (``job_name`` not in the registry),
    * scope/purpose gate denial,
    * scrubbed I/O policy violation detected during dispatch,
    * any non-recoverable dispatcher-side error.

    Subclass of :class:`Exception` (not :class:`ValueError` or
    :class:`RuntimeError`) so callers can ``except ExecutionFailure``
    without catching unrelated kernel errors.
    """


# ---------------------------------------------------------------------------
# Legacy execution gateway (deferred stub; behaviour preserved by #42)
# ---------------------------------------------------------------------------


class ExecutionGateway:
    """
    Stub execution gateway.

    Per the #42 reconciliation, :meth:`execute` continues to return the
    legacy ``{"status": "stub", ...}`` dict. The new
    :class:`ExecutionRequest` / :class:`ExecutionResponse` /
    :class:`ExecutionFailure` symbols are declared and exported via
    :data:`__all__` but are NOT plumbed into :meth:`execute`. The
    dispatcher that consumes them ships in #43.

    Subclass and override :meth:`execute` to implement real policy
    enforcement (deferred).
    """

    def execute(
        self,
        handle: ArtifactHandle,
        operation: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Execute *operation* on the document identified by *handle*.

        Parameters
        ----------
        handle:
            Artifact handle authorising access.
        operation:
            Named operation (e.g. ``"summarise"``, ``"translate"``).
        params:
            Optional operation-specific parameters.

        Returns
        -------
        dict
            Operation result.  Structure is operation-specific; the
            deferred stub returns ``{"status": "stub", "handle_id": ...,
            "operation": ...}``.
        """
        logger.info(
            "ExecutionGateway stub: handle=%s op=%s params=%s",
            handle.handle_id,
            operation,
            params,
        )
        return {"status": "stub", "handle_id": handle.handle_id, "operation": operation}


__all__ = [
    # New Chikaeshi spec symbols (declared, not plumbed; see #43).
    "ExecutionScope",
    "ExecutionRequest",
    "ExecutionResponse",
    "ExecutionFailure",
    # Legacy stub (behaviour preserved).
    "ExecutionGateway",
]
