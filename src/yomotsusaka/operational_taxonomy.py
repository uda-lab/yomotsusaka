"""
Operational failure taxonomy and recovery instructions.

This module is the **single closed enum + recovery-instruction registry**
covering the operational surfaces that an ordinary agent may encounter when
driving the MVP-5 / MVP-6 agent-runnable flow (batch, index snapshot/load,
search smoke, restoration/audit, RunPod lifecycle, RunPod smoke, inference-
backed span proposer). It does NOT redefine the four MVP-4 wire enums
(:class:`~yomotsusaka.boundary.ResolverFailureReason`,
:class:`~yomotsusaka.boundary.RestorationFailureReason`,
:class:`~yomotsusaka.execution_gateway.ExecutionFailureReason`,
:class:`~yomotsusaka.inference_backend.InferenceBackendReason`). Those
vocabularies remain wire-stable and are consumed by reference; this module
wraps the operational surface only.

Mirror-not-redefine contract for ``scripts/manage_runpod.py`` literals
----------------------------------------------------------------------
The script-local category literals in ``scripts/manage_runpod.py`` (e.g.
``create_failed``, ``wait_timeout``, ``api_key_missing``, ``cleanup_failed``)
remain wire-authoritative on the script's own stdout — they are not
redefined here. Where the **operational layer** (``operational_smoke``)
forwards those literals on its own stdout phase ledger, the enum mirrors
them by string equality so the per-phase category token is canonical at the
operational layer too. This is the same pattern already used for
``runpod_lifecycle_failed_cleaned`` / ``runpod_lifecycle_failed_owner_action``
(operational-layer enum values that mirror script-local result tokens). The
mirror is one-way: a wire-vocabulary change in the script side requires a
matching enum-side change here.

Two products live here:

1. :class:`OperationalCategory` — closed ``str`` enum of operational
   categories the agent may report.
2. :func:`recovery_for` — resolver returning a frozen
   :class:`RecoveryInstruction` for every category value. The instruction
   record is what child 02 (#91) and child 03 (#92) consume when they want
   to surface a recovery hint alongside the category token.

Privacy invariants (binding, per ``docs/architecture.md`` precedence,
``docs/error-taxonomy.md`` §"Sanitisation discipline", and
``docs/runpod-agent-smoke.md`` §7)
-----------------------------------------------------------------------------
Every :class:`RecoveryInstruction` field that an agent may echo onto a
public surface (``agent_action``, ``safe_retry_condition``,
``owner_escalate_when``, ``safe_evidence``, ``forbidden_evidence``) MUST be
**public-safe**: it MUST NOT contain raw private dictionary values, vault
root substrings, absolute filesystem paths, Pod identifiers, endpoint URLs,
bearer tokens, RunPod API key fragments, tenant identifiers, backend
response bodies, raw ``httpx`` exception text, or vLLM stack traces. The
``forbidden_evidence`` tuple on each instruction is the wire statement of
this discipline; ``tests/test_operational_taxonomy.py::
test_forbidden_evidence_contains_baseline_set`` asserts the baseline so a
future contributor cannot quietly remove an entry.

Hard-stop semantics
-------------------
Only :data:`OperationalCategory.AuditInspectFailed` carries
``hard_stop=True`` in MVP-5. This mirrors the audit-write contract
(:mod:`yomotsusaka.audit` + the Chikaeshi audit invariants in
:mod:`yomotsusaka.boundary`): when the audit row cannot be inspected as
required, the agent never reports ``status="accepted"`` — the owner must
inspect the vault-side audit log. Other categories may degrade or retry; the
audit category does not.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping

__all__ = [
    "OperationalCategory",
    "RecoveryInstruction",
    "recovery_for",
    "BASELINE_FORBIDDEN_EVIDENCE",
]


# ---------------------------------------------------------------------------
# Baseline forbidden-evidence tokens
# ---------------------------------------------------------------------------

BASELINE_FORBIDDEN_EVIDENCE: tuple[str, ...] = (
    "vault_root",
    "pod_id",
    "endpoint_url",
    "raw_private_value",
    "exception_text",
    "response_body",
)
"""Public-safe **category labels** of the leak surfaces that no
:class:`RecoveryInstruction` may permit in agent-facing evidence. Each entry
is a *label* the agent recognises ("don't echo a vault_root substring"), not
a literal value. Every category's :attr:`RecoveryInstruction.forbidden_evidence`
tuple MUST be a superset of this baseline (asserted by test #4 in
``tests/test_operational_taxonomy.py``)."""


# ---------------------------------------------------------------------------
# Closed operational category enum
# ---------------------------------------------------------------------------


class OperationalCategory(str, Enum):
    """Closed set of operational categories an ordinary agent may report.

    Wire identifiers use ``snake_case`` (matches the MVP-4 enum shape and
    the child 02 / #91 per-phase emission shape ``phase=<name>
    status=<ok|warn|fail> category=<token>``).

    Categories cluster by surface:

    * ``batch_*`` — batch runner (``yomotsusaka.batch_runner``).
    * ``index_snapshot_*`` / ``index_reload_*`` — search-gateway JSONL
      snapshot and child-process load.
    * ``search_smoke_*`` — search-gateway smoke probe.
    * ``restoration_*`` — restoration API surface (wraps
      :class:`~yomotsusaka.boundary.RestorationFailureReason` except the
      hard-stop ``audit_write_failed`` clause, which surfaces here as
      :data:`AuditInspectFailed`).
    * ``audit_inspect_*`` — audit-row inspection (hard-stop on failure).
    * ``runpod_lifecycle_*`` — RunPod create/wait/smoke/delete in
      ``manage`` mode. Mirrors the child 02 result tokens
      (``failed_cleaned`` / ``failed_owner_action``) so the report
      renderer can pass through unchanged.
    * ``inference_span_*`` — inference-backed span proposer outcomes.
    """

    BatchOk = "batch_ok"
    BatchPartial = "batch_partial"
    BatchFailed = "batch_failed"
    # Fine-grained batch fail-causes mirrored from operational_smoke so the
    # per-phase ledger preserves the recovery hint (issue #111). The coarse
    # ``BatchFailed`` is retained for upstream callers (e.g. the report
    # renderer's classifier) that key on the broader bucket.
    BatchNoDocuments = "batch_no_documents"
    BatchAllFailed = "batch_all_failed"
    BatchPartialCommit = "batch_partial_commit"
    BatchInfrastructureError = "batch_infrastructure_error"

    IndexSnapshotOk = "index_snapshot_ok"
    IndexSnapshotFailed = "index_snapshot_failed"
    # Fine-grained snapshot fail-causes (issue #111). ``snapshot_write_failed``
    # is the IO-raised path; ``snapshot_not_persisted`` is the post-write
    # absent-file defensive branch.
    SnapshotWriteFailed = "snapshot_write_failed"
    SnapshotNotPersisted = "snapshot_not_persisted"

    IndexReloadOk = "index_reload_ok"
    IndexReloadFailed = "index_reload_failed"

    SearchSmokeOk = "search_smoke_ok"
    SearchSmokeFailed = "search_smoke_failed"
    # Fine-grained smoke fail-cause (issue #111): the probe returned zero
    # hits. Distinguishes a healthy gateway that failed the probe from a
    # gateway that errored during the probe.
    SearchNoHits = "search_no_hits"

    RestorationOk = "restoration_ok"
    RestorationFailed = "restoration_failed"
    # Operational-layer fail-cause for ``operational_smoke``'s phase-5 check
    # when the facade returns a not-``ScopeDenied`` failure (issue #111).
    # The wire-level RestorationFailureReason is still preserved in the
    # audit row; this token only describes the operational surface.
    RestorationRequestUnexpectedOutcome = "restoration_request_unexpected_outcome"

    AuditInspectOk = "audit_inspect_ok"
    AuditInspectFailed = "audit_inspect_failed"
    # Fine-grained audit fail-causes (issue #111). ``audit_file_missing``
    # = audit JSONL absent or unreadable; ``audit_record_not_found`` =
    # JSONL present but the correlation row for this run is absent.
    AuditFileMissing = "audit_file_missing"
    AuditRecordNotFound = "audit_record_not_found"

    RunpodLifecycleOk = "runpod_lifecycle_ok"
    RunpodLifecycleFailedCleaned = "runpod_lifecycle_failed_cleaned"
    RunpodLifecycleFailedOwnerAction = "runpod_lifecycle_failed_owner_action"
    # Skipped / kept dispositions (issue #111). ``runpod_lifecycle_disabled``
    # is the no-flag default; ``runpod_lifecycle_kept`` is the
    # ``--keep-pod`` warn-case where the agent successfully drove create→
    # wait→smoke but the caller opted out of delete.
    RunpodLifecycleDisabled = "runpod_lifecycle_disabled"
    RunpodLifecycleKept = "runpod_lifecycle_kept"
    # Wire-mirrored RunPod fail-cause literals (issue #111).
    # These mirror the ``scripts/manage_runpod.py`` PUBLIC_SAFE_CATEGORIES
    # tokens by string equality so the operational layer's phase ledger
    # carries the same vocabulary as the script's own stdout. See the
    # module docstring's "Mirror-not-redefine" clause.
    CreateFailed = "create_failed"
    WaitTimeout = "wait_timeout"
    # Pod was created and became unhealthy (wait_timeout) AND the subsequent
    # best-effort cleanup attempt also failed — the Pod may still be running
    # and billing. Callers should treat this as requiring owner action.
    WaitTimeoutCleanupFailed = "wait_timeout_cleanup_failed"
    ApiKeyMissing = "api_key_missing"
    CleanupFailed = "cleanup_failed"

    InferenceSpanDegraded = "inference_span_degraded"
    InferenceSpanUnavailable = "inference_span_unavailable"


# ---------------------------------------------------------------------------
# Recovery instruction record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecoveryInstruction:
    """Per-category recovery hint consumed by the operational scenario CLI
    (#91) and the report renderer (#92).

    The record is frozen and built once at module import. All string
    fields are public-safe per the module docstring's privacy invariants;
    the ``forbidden_evidence`` tuple is the wire statement of what the
    agent MUST NOT echo on a public surface when reporting this category.

    Fields
    ------
    category:
        The enum value this instruction is bound to.
    agent_action:
        Imperative, public-safe one-liner: what the agent should do next
        when it observes this category. No paths, no IDs, no raw values.
    safe_retry_condition:
        Stable token describing the retry policy. One of:

        * ``"never"`` — terminal category; do not retry.
        * ``"owner-only"`` — only the owner may re-attempt (e.g. after
          inspecting vault-side state).
        * ``"<= N retries"`` — bounded automated retry budget.
    owner_escalate_when:
        Stable token describing when the agent should hand off to the
        owner. One of ``"always"``, ``"after retries exhausted"``,
        ``"never"``.
    safe_evidence:
        Tuple of public-safe evidence labels the agent MAY include in a
        report (e.g. ``"counter:processed_documents"``). Labels, not
        values.
    forbidden_evidence:
        Tuple of public-safe leak labels the agent MUST NOT include in
        any report (e.g. ``"vault_root"``, ``"pod_id"``). MUST be a
        superset of :data:`BASELINE_FORBIDDEN_EVIDENCE`.
    hard_stop:
        ``True`` only for :data:`OperationalCategory.AuditInspectFailed`
        in MVP-5. When ``True`` the agent never reports
        ``status="accepted"`` and the owner must inspect the vault-side
        audit log per the Chikaeshi audit contract.
    """

    category: OperationalCategory
    agent_action: str
    safe_retry_condition: str
    owner_escalate_when: str
    safe_evidence: tuple[str, ...]
    forbidden_evidence: tuple[str, ...]
    hard_stop: bool


# Convenience: every instruction inherits the baseline forbidden-evidence
# set. Category-specific additions are appended in the table below.
_BASELINE = BASELINE_FORBIDDEN_EVIDENCE


_RECOVERY_TABLE_DATA: tuple[RecoveryInstruction, ...] = (
    RecoveryInstruction(
        category=OperationalCategory.BatchOk,
        agent_action=(
            "Report counts only (processed/failed); no per-file paths or "
            "raw text on the public surface."
        ),
        safe_retry_condition="never",
        owner_escalate_when="never",
        safe_evidence=("counter:processed_documents", "counter:failed_documents"),
        forbidden_evidence=_BASELINE + ("inbox_path", "document_text"),
        hard_stop=False,
    ),
    RecoveryInstruction(
        category=OperationalCategory.BatchPartial,
        agent_action=(
            "Report processed and failed counts; do not retry the batch. "
            "Owner inspects vault-side batch log for per-file detail."
        ),
        safe_retry_condition="owner-only",
        owner_escalate_when="never",
        safe_evidence=("counter:processed_documents", "counter:failed_documents"),
        forbidden_evidence=_BASELINE + ("inbox_path", "document_text"),
        hard_stop=False,
    ),
    RecoveryInstruction(
        category=OperationalCategory.BatchFailed,
        agent_action=(
            "Report batch_failed; do not retry. Owner verifies the inbox "
            "directory exists and is readable, then re-runs the batch."
        ),
        safe_retry_condition="owner-only",
        owner_escalate_when="always",
        safe_evidence=("counter:failed_documents",),
        forbidden_evidence=_BASELINE + ("inbox_path", "document_text"),
        hard_stop=False,
    ),
    RecoveryInstruction(
        category=OperationalCategory.BatchNoDocuments,
        agent_action=(
            "Report batch_no_documents; the inbox contained no admissible "
            "files. Owner verifies the inbox directory holds the expected "
            "corpus before re-running."
        ),
        safe_retry_condition="owner-only",
        owner_escalate_when="always",
        safe_evidence=("counter:processed_documents",),
        forbidden_evidence=_BASELINE + ("inbox_path", "document_text"),
        hard_stop=False,
    ),
    RecoveryInstruction(
        category=OperationalCategory.BatchAllFailed,
        agent_action=(
            "Report batch_all_failed; every submitted document failed to "
            "commit. Owner inspects vault-side batch log for the per-file "
            "cause before retrying."
        ),
        safe_retry_condition="owner-only",
        owner_escalate_when="always",
        safe_evidence=(
            "counter:processed_documents",
            "counter:failed_documents",
        ),
        forbidden_evidence=_BASELINE + ("inbox_path", "document_text"),
        hard_stop=False,
    ),
    RecoveryInstruction(
        category=OperationalCategory.BatchPartialCommit,
        agent_action=(
            "Report batch_partial_commit (warn status); some documents "
            "committed and some failed. Do not retry the batch — re-running "
            "would re-process the already-committed files. Owner inspects "
            "vault-side batch log for the failed subset."
        ),
        safe_retry_condition="owner-only",
        owner_escalate_when="never",
        safe_evidence=(
            "counter:processed_documents",
            "counter:failed_documents",
        ),
        forbidden_evidence=_BASELINE + ("inbox_path", "document_text"),
        hard_stop=False,
    ),
    RecoveryInstruction(
        category=OperationalCategory.BatchInfrastructureError,
        agent_action=(
            "Report batch_infrastructure_error; the runner could not enter "
            "the inbox at all (missing directory, IO fault, or constructor "
            "error). Owner verifies the inbox path and filesystem before "
            "re-running."
        ),
        safe_retry_condition="owner-only",
        owner_escalate_when="always",
        safe_evidence=("counter:failed_documents",),
        forbidden_evidence=_BASELINE + ("inbox_path", "document_text"),
        hard_stop=False,
    ),
    RecoveryInstruction(
        category=OperationalCategory.IndexSnapshotOk,
        agent_action=(
            "Report snapshot success; emit only the manifest count, never "
            "the snapshot file path or per-doc identifiers."
        ),
        safe_retry_condition="never",
        owner_escalate_when="never",
        safe_evidence=("counter:index_snapshot_ok",),
        forbidden_evidence=_BASELINE + ("snapshot_path", "manifest_doc_id"),
        hard_stop=False,
    ),
    RecoveryInstruction(
        category=OperationalCategory.IndexSnapshotFailed,
        agent_action=(
            "Report index_snapshot_failed; the snapshot helper already "
            "performed a single bounded retry. Do not retry further. "
            "Owner inspects vault-side index directory."
        ),
        safe_retry_condition="<= 1 retries",
        owner_escalate_when="after retries exhausted",
        safe_evidence=("counter:index_snapshot_ok",),
        forbidden_evidence=_BASELINE + ("snapshot_path", "manifest_doc_id"),
        hard_stop=False,
    ),
    RecoveryInstruction(
        category=OperationalCategory.SnapshotWriteFailed,
        agent_action=(
            "Report snapshot_write_failed; an OSError was raised while "
            "writing the index snapshot. The helper already performed a "
            "bounded retry. Owner inspects vault-side index directory."
        ),
        safe_retry_condition="<= 1 retries",
        owner_escalate_when="after retries exhausted",
        safe_evidence=("counter:index_snapshot_ok",),
        forbidden_evidence=_BASELINE + ("snapshot_path", "manifest_doc_id"),
        hard_stop=False,
    ),
    RecoveryInstruction(
        category=OperationalCategory.SnapshotNotPersisted,
        agent_action=(
            "Report snapshot_not_persisted; the snapshot helper returned "
            "without raising but the snapshot file is absent on disk. Owner "
            "inspects vault-side index directory for write-through faults."
        ),
        safe_retry_condition="owner-only",
        owner_escalate_when="always",
        safe_evidence=("counter:index_snapshot_ok",),
        forbidden_evidence=_BASELINE + ("snapshot_path", "manifest_doc_id"),
        hard_stop=False,
    ),
    RecoveryInstruction(
        category=OperationalCategory.IndexReloadOk,
        agent_action=(
            "Report reload success; emit the loaded count only. The child "
            "process performed no in-process state carry-over."
        ),
        safe_retry_condition="never",
        owner_escalate_when="never",
        safe_evidence=("counter:index_loadable",),
        forbidden_evidence=_BASELINE + ("snapshot_path", "manifest_doc_id"),
        hard_stop=False,
    ),
    RecoveryInstruction(
        category=OperationalCategory.IndexReloadFailed,
        agent_action=(
            "Report index_reload_failed; do not retry. Owner inspects "
            "vault-side snapshot for corruption or schema drift."
        ),
        safe_retry_condition="owner-only",
        owner_escalate_when="always",
        safe_evidence=("counter:index_loadable",),
        forbidden_evidence=_BASELINE + ("snapshot_path", "manifest_doc_id"),
        hard_stop=False,
    ),
    RecoveryInstruction(
        category=OperationalCategory.SearchSmokeOk,
        agent_action=(
            "Report smoke success; emit the match count only, not the "
            "probe query text or matched handles."
        ),
        safe_retry_condition="never",
        owner_escalate_when="never",
        safe_evidence=("counter:search_smoke_ok",),
        forbidden_evidence=_BASELINE + ("query_text", "manifest_doc_id"),
        hard_stop=False,
    ),
    RecoveryInstruction(
        category=OperationalCategory.SearchSmokeFailed,
        agent_action=(
            "Report search_smoke_failed (category only). Do not echo the "
            "probe query text or any candidate handles."
        ),
        safe_retry_condition="owner-only",
        owner_escalate_when="after retries exhausted",
        safe_evidence=("counter:search_smoke_ok",),
        forbidden_evidence=_BASELINE + ("query_text", "manifest_doc_id"),
        hard_stop=False,
    ),
    RecoveryInstruction(
        category=OperationalCategory.SearchNoHits,
        agent_action=(
            "Report search_no_hits; the smoke probe returned zero matches "
            "against a corpus that should always carry redaction keys. "
            "Owner inspects vault-side index for schema drift or empty "
            "manifests."
        ),
        safe_retry_condition="owner-only",
        owner_escalate_when="after retries exhausted",
        safe_evidence=("counter:search_smoke_ok",),
        forbidden_evidence=_BASELINE + ("query_text", "manifest_doc_id"),
        hard_stop=False,
    ),
    RecoveryInstruction(
        category=OperationalCategory.RestorationOk,
        agent_action=(
            "Report restoration success; the raw private value is "
            "asserted in-process only and never echoed onto a public "
            "surface."
        ),
        safe_retry_condition="never",
        owner_escalate_when="never",
        safe_evidence=("counter:restoration_outcome",),
        forbidden_evidence=_BASELINE + ("locator", "approval_ticket"),
        hard_stop=False,
    ),
    RecoveryInstruction(
        category=OperationalCategory.RestorationFailed,
        agent_action=(
            "Report restoration_failed and the wrapped "
            "RestorationFailureReason token (except audit_write_failed, "
            "which surfaces as audit_inspect_failed). Do not retry "
            "without owner review."
        ),
        safe_retry_condition="owner-only",
        owner_escalate_when="after retries exhausted",
        safe_evidence=(
            "counter:restoration_outcome",
            "wrapped:restoration_failure_reason",
        ),
        forbidden_evidence=_BASELINE + ("locator", "approval_ticket"),
        hard_stop=False,
    ),
    RecoveryInstruction(
        category=OperationalCategory.RestorationRequestUnexpectedOutcome,
        agent_action=(
            "Report restoration_request_unexpected_outcome; the facade "
            "returned a response that did not match the expected "
            "scope-denied contract. Owner inspects vault-side audit log "
            "to recover."
        ),
        safe_retry_condition="owner-only",
        owner_escalate_when="always",
        safe_evidence=(
            "counter:restoration_outcome",
            "wrapped:restoration_failure_reason",
        ),
        forbidden_evidence=_BASELINE + ("locator", "approval_ticket"),
        hard_stop=False,
    ),
    RecoveryInstruction(
        category=OperationalCategory.AuditInspectOk,
        agent_action=(
            "Report audit_inspect_ok; emit the audit row count only, "
            "never the audit row contents."
        ),
        safe_retry_condition="never",
        owner_escalate_when="never",
        safe_evidence=("counter:audit_row_count",),
        forbidden_evidence=_BASELINE + ("audit_row_body", "locator"),
        hard_stop=False,
    ),
    RecoveryInstruction(
        category=OperationalCategory.AuditInspectFailed,
        agent_action=(
            "Report audit_inspect_failed and stop. Never report "
            "status=accepted when this fires. Owner inspects vault-side "
            "audit log to recover (Chikaeshi audit contract)."
        ),
        safe_retry_condition="never",
        owner_escalate_when="always",
        safe_evidence=("counter:audit_row_count",),
        forbidden_evidence=_BASELINE + ("audit_row_body", "locator"),
        hard_stop=True,
    ),
    RecoveryInstruction(
        category=OperationalCategory.AuditFileMissing,
        agent_action=(
            "Report audit_file_missing; the audit JSONL is absent or "
            "unreadable. Owner inspects vault-side audit directory "
            "(Chikaeshi audit contract). Never report status=accepted."
        ),
        safe_retry_condition="never",
        owner_escalate_when="always",
        safe_evidence=("counter:audit_row_count",),
        forbidden_evidence=_BASELINE + ("audit_row_body", "locator"),
        hard_stop=False,
    ),
    RecoveryInstruction(
        category=OperationalCategory.AuditRecordNotFound,
        agent_action=(
            "Report audit_record_not_found; the audit JSONL exists but the "
            "correlation row for this run is absent. Owner inspects "
            "vault-side audit log to recover."
        ),
        safe_retry_condition="never",
        owner_escalate_when="always",
        safe_evidence=("counter:audit_row_count",),
        forbidden_evidence=_BASELINE + ("audit_row_body", "locator"),
        hard_stop=False,
    ),
    RecoveryInstruction(
        category=OperationalCategory.RunpodLifecycleOk,
        agent_action=(
            "Report runpod_lifecycle_ok; create-wait-smoke-delete "
            "completed and no billing tail remains."
        ),
        safe_retry_condition="never",
        owner_escalate_when="never",
        safe_evidence=("counter:runpod_lifecycle_category",),
        forbidden_evidence=_BASELINE + ("bearer_token", "api_key"),
        hard_stop=False,
    ),
    RecoveryInstruction(
        category=OperationalCategory.RunpodLifecycleFailedCleaned,
        agent_action=(
            "Report runpod_lifecycle_failed_cleaned; a phase failed but "
            "the bounded REST delete retry succeeded and no Pod is left "
            "running. Owner may inspect logs but no cleanup action is "
            "required."
        ),
        safe_retry_condition="<= 1 retries",
        owner_escalate_when="never",
        safe_evidence=("counter:runpod_lifecycle_category",),
        forbidden_evidence=_BASELINE + ("bearer_token", "api_key"),
        hard_stop=False,
    ),
    RecoveryInstruction(
        category=OperationalCategory.RunpodLifecycleFailedOwnerAction,
        agent_action=(
            "Report runpod_lifecycle_failed_owner_action; bounded "
            "cleanup retries exhausted and a Pod may still be running. "
            "Owner may use runpodctl as a break-glass tool to inspect or "
            "force-delete; the agent does not invoke runpodctl."
        ),
        safe_retry_condition="<= 1 retries",
        owner_escalate_when="always",
        safe_evidence=("counter:runpod_lifecycle_category",),
        forbidden_evidence=_BASELINE + ("bearer_token", "api_key"),
        hard_stop=False,
    ),
    RecoveryInstruction(
        category=OperationalCategory.RunpodLifecycleDisabled,
        agent_action=(
            "Report runpod_lifecycle_disabled; the RunPod phase was "
            "skipped because the agent did not pass --live-runpod. No "
            "owner action; this is the no-network default."
        ),
        safe_retry_condition="never",
        owner_escalate_when="never",
        safe_evidence=("counter:runpod_lifecycle_category",),
        forbidden_evidence=_BASELINE + ("bearer_token", "api_key"),
        hard_stop=False,
    ),
    RecoveryInstruction(
        category=OperationalCategory.RunpodLifecycleKept,
        agent_action=(
            "Report runpod_lifecycle_kept (warn status); the agent drove "
            "create-wait-smoke but skipped delete because the caller "
            "passed --keep-pod. Owner is responsible for deleting the "
            "Pod when finished (cost control)."
        ),
        safe_retry_condition="never",
        owner_escalate_when="always",
        safe_evidence=("counter:runpod_lifecycle_category",),
        forbidden_evidence=_BASELINE + ("bearer_token", "api_key"),
        hard_stop=False,
    ),
    RecoveryInstruction(
        category=OperationalCategory.CreateFailed,
        agent_action=(
            "Report create_failed; the RunPod create call returned an "
            "error before the Pod became schedulable. Owner inspects "
            "RunPod console; agent does not retry without owner review."
        ),
        safe_retry_condition="owner-only",
        owner_escalate_when="always",
        safe_evidence=("counter:runpod_lifecycle_category",),
        forbidden_evidence=_BASELINE + ("bearer_token", "api_key"),
        hard_stop=False,
    ),
    RecoveryInstruction(
        category=OperationalCategory.WaitTimeout,
        agent_action=(
            "Report wait_timeout; the Pod was created but did not become "
            "healthy within the configured wait budget. The library "
            "performed best-effort cleanup (stop_pod) before re-raising; "
            "the Pod was successfully deleted. Owner may inspect RunPod "
            "console; no further agent action is required."
        ),
        safe_retry_condition="<= 1 retries",
        owner_escalate_when="after retries exhausted",
        safe_evidence=("counter:runpod_lifecycle_category",),
        forbidden_evidence=_BASELINE + ("bearer_token", "api_key"),
        hard_stop=False,
    ),
    RecoveryInstruction(
        category=OperationalCategory.WaitTimeoutCleanupFailed,
        agent_action=(
            "Report wait_timeout_cleanup_failed; the Pod was created but "
            "did not become healthy AND the subsequent best-effort cleanup "
            "also failed — the Pod may still be running and billing. "
            "Owner must manually delete the Pod via the RunPod console or "
            "runpodctl; agent does not invoke runpodctl."
        ),
        safe_retry_condition="owner-only",
        owner_escalate_when="always",
        safe_evidence=("counter:runpod_lifecycle_category",),
        forbidden_evidence=_BASELINE + ("bearer_token", "api_key"),
        hard_stop=False,
    ),
    RecoveryInstruction(
        category=OperationalCategory.ApiKeyMissing,
        agent_action=(
            "Report api_key_missing; the RunPod phase was requested but "
            "no API key is configured. Owner provisions the credential "
            "before re-running with --live-runpod."
        ),
        safe_retry_condition="owner-only",
        owner_escalate_when="always",
        safe_evidence=("counter:runpod_lifecycle_category",),
        forbidden_evidence=_BASELINE + ("bearer_token", "api_key"),
        hard_stop=False,
    ),
    RecoveryInstruction(
        category=OperationalCategory.CleanupFailed,
        agent_action=(
            "Report cleanup_failed; the Pod exists but the bounded REST "
            "DELETE retries were exhausted. Owner uses runpodctl as a "
            "break-glass tool to force-delete; agent does not invoke "
            "runpodctl."
        ),
        safe_retry_condition="<= 1 retries",
        owner_escalate_when="always",
        safe_evidence=("counter:runpod_lifecycle_category",),
        forbidden_evidence=_BASELINE + ("bearer_token", "api_key"),
        hard_stop=False,
    ),
    RecoveryInstruction(
        category=OperationalCategory.InferenceSpanDegraded,
        agent_action=(
            "Report inference_span_degraded; the inference backend "
            "raised a recoverable error and the pipeline fell back to "
            "the deterministic proposer. Do not echo backend response "
            "bodies or exception text."
        ),
        safe_retry_condition="owner-only",
        owner_escalate_when="after retries exhausted",
        safe_evidence=("counter:processed_documents", "counter:failed_documents"),
        forbidden_evidence=_BASELINE + ("backend_response", "model_id"),
        hard_stop=False,
    ),
    RecoveryInstruction(
        category=OperationalCategory.InferenceSpanUnavailable,
        agent_action=(
            "Report inference_span_unavailable; the inference path was "
            "requested but the backend is not configured. The agent "
            "uses the deterministic proposer and reports this category "
            "so the owner can decide whether to provision a backend."
        ),
        safe_retry_condition="owner-only",
        owner_escalate_when="after retries exhausted",
        safe_evidence=("counter:processed_documents", "counter:failed_documents"),
        forbidden_evidence=_BASELINE + ("backend_response", "model_id"),
        hard_stop=False,
    ),
)


_RECOVERY_TABLE: Mapping[OperationalCategory, RecoveryInstruction] = MappingProxyType(
    {instruction.category: instruction for instruction in _RECOVERY_TABLE_DATA}
)
"""Read-only mapping from category to recovery instruction. Built once at
module import; the :class:`MappingProxyType` wrapper makes accidental
mutation a runtime ``TypeError`` rather than a silent privacy regression."""


def recovery_for(category: OperationalCategory) -> RecoveryInstruction:
    """Return the :class:`RecoveryInstruction` for *category*.

    Raises
    ------
    KeyError
        If *category* is not a known :class:`OperationalCategory` member.
        This is a programmer error — the enum is closed and the table is
        built at import time, so the missing-key path is unreachable
        unless a new enum value lands without an accompanying instruction
        (which ``tests/test_operational_taxonomy.py::
        test_every_category_has_recovery_instruction`` catches first).
    """
    return _RECOVERY_TABLE[category]


# ---------------------------------------------------------------------------
# Doc rendering — single source of truth for the markdown table in
# docs/error-taxonomy.md (asserted by tests/test_error_taxonomy_doc.py).
# ---------------------------------------------------------------------------


def render_recovery_table_markdown() -> str:
    """Render :data:`_RECOVERY_TABLE` as a GitHub-flavoured markdown table.

    Used by ``docs/error-taxonomy.md`` (the "OperationalCategory" section
    is generated from this output) and asserted against the on-disk doc
    by ``tests/test_error_taxonomy_doc.py``. The function does not write
    to disk; it returns the text so callers can compose it into a larger
    document.
    """
    header = (
        "| Category | Agent action | Safe retry | Owner escalate | Hard stop |\n"
        "| -------- | ------------ | ---------- | -------------- | --------- |\n"
    )
    rows = []
    for category in OperationalCategory:
        instruction = _RECOVERY_TABLE[category]
        rows.append(
            f"| `{category.value}` | {instruction.agent_action} | "
            f"`{instruction.safe_retry_condition}` | "
            f"`{instruction.owner_escalate_when}` | "
            f"{'yes' if instruction.hard_stop else 'no'} |"
        )
    return header + "\n".join(rows) + "\n"
