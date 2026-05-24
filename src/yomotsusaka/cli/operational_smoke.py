"""
``python -m yomotsusaka.cli.operational_smoke`` — agent-runnable operational scenario.

Runs the canonical end-to-end operational backbone in a single command,
producing a structured, public-safe run log suitable for agent
consumption. Each phase is sequential and emits exactly one stdout line:

    phase=<name> status=<ok|warn|fail|skipped> category=<stable_token>

Followed by a final line:

    result=<completed|completed_with_warnings|failed_cleaned|failed_owner_action>

CLI surface
-----------

    python -m yomotsusaka.cli.operational_smoke <inbox_dir>
        --vault-root <vault_root>
        [--tenant-id <id>]
        [--live-runpod]
        [--keep-pod]   # only meaningful with --live-runpod; default is delete
        [--emit-json <path>]  # optional ScenarioResult JSON for operational_report

Phases (in order)
-----------------

1. ``batch`` — run the batch runner against ``<inbox_dir>``, committing to
   the vault via :class:`~yomotsusaka.facade.LocalFacade`.
2. ``index_snapshot`` — persist the in-memory search index to
   ``<vault_root>/index/manifests.jsonl``.
3. ``index_reload`` — spawn a subprocess that loads the index snapshot from
   disk only (no in-memory carry-over from phase 2). The subprocess only
   receives the vault root and tenant id; no in-memory dictionaries cross
   the process boundary.
4. ``search_smoke`` — run a fixed redacted-key search query inside the
   reloaded child process and assert at least one expected handle returns.
5. ``restoration_request`` — exercise the public restoration request path
   (``LocalFacade.request_restore``) using a document id from phase 1. The
   facade is hard-wired to ordinary-agent scope, so the response is a
   ``scope_denied`` failure — but the audit row is written, which is the
   whole point of exercising this seam from agent-runnable code.
6. ``audit_inspect`` — read the audit JSONL written under
   ``<vault_root>/audit/restoration.jsonl`` and assert it contains rows
   bearing the phase-5 ``caller_label`` for the targeted document id.
7. ``runpod_lifecycle`` — only when ``--live-runpod`` is set. Create →
   wait → smoke → delete a Pod via
   :class:`~yomotsusaka.runpod_lifecycle.ManageRunPodLifecycle`.
   Delete-by-default; ``--keep-pod`` opts out. Otherwise skipped.

Privacy invariants (binding)
----------------------------
The CLI MUST NOT import any private-kernel module
(``yomotsusaka.pipeline``, ``yomotsusaka.commit``,
``yomotsusaka.restoration_api``, ``yomotsusaka.templates``,
``yomotsusaka.scrubber``, ``yomotsusaka.audit``). All access goes via
:class:`yomotsusaka.facade.LocalFacade` or the public audit-read helper
:func:`yomotsusaka.boundary.<...>`. The audit JSONL is read directly as a
text file (no parser import from the kernel audit module) so the privacy
discipline is also a hard import boundary; a sibling test asserts this.

Public-safe output only: every stdout line is a stable token of the form
above. The CLI NEVER prints vault paths, pod identifiers, endpoint URLs,
credentials, response bodies, or raw private dictionary values. The final
summary line is the sole synthesis surface.

Exit codes
----------
``0``
    ``completed`` (every required phase ``ok``) or
    ``completed_with_warnings`` (every required phase ``ok`` or ``warn``,
    no ``fail``).
``1``
    ``failed_cleaned`` — at least one required phase failed; any RunPod
    resource created in phase 7 was successfully deleted (or no Pod was
    created in the first place).
``3``
    ``failed_owner_action`` — phase 7 created a Pod that could not be
    cleaned up; the urgent stderr line and lifecycle JSONL row carry the
    correlation token the owner needs to delete it manually.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any

from yomotsusaka.batch_runner import BatchRunner, BatchSummary
from yomotsusaka.boundary import (
    RestorationFailureReason,
    RestorationRequest,
    RestorationResponse,
)
from yomotsusaka.facade import LocalFacade
from yomotsusaka.operational_taxonomy import OperationalCategory
from yomotsusaka.schemas import EntityKind
from yomotsusaka.tenant import TenantScope


# ---------------------------------------------------------------------------
# Stable category vocabulary
# ---------------------------------------------------------------------------
#
# These tokens are the ONLY values that may appear in ``category=<...>``.
# Stable across releases; downstream agent consumers may pattern-match on
# them. Every token is an ``OperationalCategory.value`` — the closed enum
# in :mod:`yomotsusaka.operational_taxonomy` is the canonical source of
# truth (issue #111). The drift test
# ``tests/test_operational_smoke_taxonomy_drift.py`` enforces this by
# asserting every ``_CAT_*`` literal here is a member value.

# Phase-level OK categories.
_CAT_OK_BATCH = OperationalCategory.BatchOk.value
_CAT_OK_INDEX_SNAPSHOT = OperationalCategory.IndexSnapshotOk.value
_CAT_OK_INDEX_RELOAD = OperationalCategory.IndexReloadOk.value
_CAT_OK_SEARCH_SMOKE = OperationalCategory.SearchSmokeOk.value
_CAT_OK_RESTORATION = OperationalCategory.RestorationOk.value
_CAT_OK_AUDIT = OperationalCategory.AuditInspectOk.value
_CAT_OK_RUNPOD = OperationalCategory.RunpodLifecycleOk.value

# Skipped / kept dispositions — RunPod phase only.
_CAT_SKIPPED_RUNPOD = OperationalCategory.RunpodLifecycleDisabled.value
_CAT_KEPT_RUNPOD = OperationalCategory.RunpodLifecycleKept.value

# Phase-level FAIL categories.
_CAT_FAIL_BATCH_EMPTY = OperationalCategory.BatchNoDocuments.value
_CAT_FAIL_BATCH_ALL_FAILED = OperationalCategory.BatchAllFailed.value
_CAT_FAIL_BATCH_PARTIAL = OperationalCategory.BatchPartialCommit.value
_CAT_FAIL_BATCH_INFRA = OperationalCategory.BatchInfrastructureError.value
_CAT_FAIL_INDEX_SNAPSHOT_WRITE = OperationalCategory.SnapshotWriteFailed.value
_CAT_FAIL_INDEX_SNAPSHOT_NOT_PERSISTED = (
    OperationalCategory.SnapshotNotPersisted.value
)
_CAT_FAIL_INDEX_RELOAD = OperationalCategory.IndexReloadFailed.value
_CAT_FAIL_SEARCH_SMOKE = OperationalCategory.SearchNoHits.value
_CAT_FAIL_RESTORATION = (
    OperationalCategory.RestorationRequestUnexpectedOutcome.value
)
_CAT_FAIL_AUDIT_MISSING = OperationalCategory.AuditFileMissing.value
_CAT_FAIL_AUDIT_NO_MATCH = OperationalCategory.AuditRecordNotFound.value
_CAT_FAIL_RUNPOD_CREATE = OperationalCategory.CreateFailed.value
_CAT_FAIL_RUNPOD_WAIT = OperationalCategory.WaitTimeout.value
# Pod created, health-check timed out, cleanup also failed — Pod may still
# exist and bill. The library raises PodUnavailableError("wait_timeout_cleanup_failed")
# in this case (issue #125). Maps to failed_owner_action via _synthesise_result.
_CAT_FAIL_RUNPOD_WAIT_TIMEOUT_CLEANUP_FAILED = OperationalCategory.WaitTimeoutCleanupFailed.value
_CAT_FAIL_RUNPOD_PREFLIGHT_APIKEY = OperationalCategory.ApiKeyMissing.value
_CAT_FAIL_RUNPOD_CLEANUP = OperationalCategory.CleanupFailed.value

# Final result tokens.
_RESULT_COMPLETED = "completed"
_RESULT_COMPLETED_WITH_WARNINGS = "completed_with_warnings"
_RESULT_FAILED_CLEANED = "failed_cleaned"
_RESULT_FAILED_OWNER_ACTION = "failed_owner_action"

# Exit-code map.
_EXIT_OK = 0
_EXIT_FAILED_CLEANED = 1
_EXIT_FAILED_OWNER_ACTION = 3

# Phase names — stable wire tokens used in stdout.
_PHASE_BATCH = "batch"
_PHASE_INDEX_SNAPSHOT = "index_snapshot"
_PHASE_INDEX_RELOAD = "index_reload"
_PHASE_SEARCH_SMOKE = "search_smoke"
_PHASE_RESTORATION = "restoration_request"
_PHASE_AUDIT = "audit_inspect"
_PHASE_RUNPOD = "runpod_lifecycle"

# Status literals.
_STATUS_OK = "ok"
_STATUS_WARN = "warn"
_STATUS_FAIL = "fail"
_STATUS_SKIPPED = "skipped"

# The caller_label embedded in the public RestorationRequest issued by
# phase 5. The audit_inspect phase scans for this exact label so the
# row written by phase 5 can be located unambiguously even when prior
# unrelated rows are present in the same vault.
_RESTORATION_CALLER_LABEL = "operational-smoke"

# The query the search-smoke child process runs. Any document committed
# through ``DeterministicSpanProposer`` will carry a ``<PERSON_*>``
# redaction key — a robust hit pattern across canonical fixtures.
_SEARCH_QUERY = "<PERSON_"

# ---------------------------------------------------------------------------
# Demo-corpus support
# ---------------------------------------------------------------------------
#
# ``--demo-corpus`` materialises a small fixture inbox under a fresh temp
# directory so a quickstart caller does not need to pre-stage an inbox or
# copy fixtures by hand. The shipped files are a deliberate, frozen mirror
# of two canonical fixtures from ``tests/fixtures/redaction_corpus/``;
# byte-equality is enforced by a unit test so the mirror cannot silently
# drift. The temp dir is created via :func:`tempfile.mkdtemp` (NOT under
# the user-supplied positional ``inbox_dir``) so the positional path is
# never mutated.
_DEMO_CORPUS_PACKAGE = "yomotsusaka.cli._demo_corpus"
_DEMO_CORPUS_FILES: tuple[str, ...] = (
    "canonical_employee.txt",
    "multi_mention.txt",
)
_DEMO_CORPUS_TEMP_PREFIX = "yomotsusaka-demo-corpus-"

# Fixed stderr advisory tokens. Stable across releases for agent callers
# that scrape stderr; do NOT interpolate paths into the override notice.
_NOTICE_DEMO_CORPUS_OVERRIDE = "notice=demo_corpus_override"
_NOTICE_DEMO_CORPUS_KEPT = "notice=demo_corpus_kept"


def _materialise_demo_corpus() -> Path:
    """Create a fresh temp directory and copy the demo fixtures into it.

    Returns the temp directory path. Caller is responsible for cleanup
    (typically via the ``main`` ``try/finally`` and ``--keep-demo-corpus``
    semantics). Fixture bytes are loaded via :mod:`importlib.resources`
    so the path works from an installed wheel as well as a source
    checkout.
    """
    demo_root = Path(tempfile.mkdtemp(prefix=_DEMO_CORPUS_TEMP_PREFIX))
    package = resources.files(_DEMO_CORPUS_PACKAGE)
    for name in _DEMO_CORPUS_FILES:
        # ``read_bytes`` over ``copy`` because the resource may live
        # inside a zipfile when distributed as a wheel and ``shutil.copy``
        # would need an on-disk source.
        data = (package / name).read_bytes()
        (demo_root / name).write_bytes(data)
    return demo_root


# ---------------------------------------------------------------------------
# Output helpers (sole stdout producers)
# ---------------------------------------------------------------------------


def _emit_phase(
    phase: str,
    status: str,
    category: str,
    *,
    stream: Any = None,
) -> None:
    """Print exactly ``phase=<phase> status=<status> category=<category>``.

    The ONLY producer of per-phase stdout. No interpolation of vault
    paths, raw text, pod identifiers, endpoint URLs, or any other content
    is permitted through this helper.
    """
    out = stream if stream is not None else sys.stdout
    out.write(f"phase={phase} status={status} category={category}\n")
    out.flush()


def _emit_result(result: str, *, stream: Any = None) -> None:
    """Print exactly ``result=<result>`` — the single final summary line."""
    out = stream if stream is not None else sys.stdout
    out.write(f"result={result}\n")
    out.flush()


# ---------------------------------------------------------------------------
# Facade construction (shared with run_batch idiom)
# ---------------------------------------------------------------------------


def _construct_facade(vault_root: Path, tenant_id: str | None) -> LocalFacade:
    if tenant_id is not None:
        tenant = TenantScope(tenant_id=tenant_id, vault_root=vault_root)
        return LocalFacade(tenant=tenant)
    return LocalFacade(vault_root)


# ---------------------------------------------------------------------------
# Phase implementations
# ---------------------------------------------------------------------------


def _phase_batch(
    facade: LocalFacade, inbox: Path
) -> tuple[str, str, str | None, BatchSummary | None]:
    """Run the batch runner against *inbox*.

    Returns ``(status, category, doc_id_for_restoration_phase, summary)``.
    The third element is a doc_id from a successfully committed document
    the later phases can target; ``None`` on failure. The fourth element
    is the raw :class:`BatchSummary` (or ``None`` when the runner errored
    before producing one) so the calling layer can populate
    ``processed_documents`` / ``failed_documents`` counters for the
    ``--emit-json`` payload (issue #111).
    """
    runner = BatchRunner(facade=facade)
    try:
        summary = runner.run_directory(inbox)
    except (FileNotFoundError, NotADirectoryError):
        return _STATUS_FAIL, _CAT_FAIL_BATCH_INFRA, None, None
    except Exception:  # pragma: no cover - defensive
        return _STATUS_FAIL, _CAT_FAIL_BATCH_INFRA, None, None

    if summary.submitted_count == 0:
        return _STATUS_FAIL, _CAT_FAIL_BATCH_EMPTY, None, summary
    if summary.committed_count == 0:
        return _STATUS_FAIL, _CAT_FAIL_BATCH_ALL_FAILED, None, summary

    # Pick a stable doc_id from the committed set so phases 5/6 can
    # target a known artifact. Reading the manifests directory is a
    # public-side read (the manifests are the redacted projection); no
    # private dictionary is touched here.
    manifests_dir = facade.vault_root / "manifests"
    try:
        candidates = sorted(manifests_dir.glob("*.json"))
    except OSError:
        return _STATUS_FAIL, _CAT_FAIL_BATCH_INFRA, None, summary
    if not candidates:
        return _STATUS_FAIL, _CAT_FAIL_BATCH_ALL_FAILED, None, summary
    doc_id = candidates[0].stem

    if summary.failed_count > 0:
        # A partial-commit batch is a warning — work proceeds, but the
        # synthesis result will downgrade to ``completed_with_warnings``.
        return _STATUS_WARN, _CAT_FAIL_BATCH_PARTIAL, doc_id, summary
    return _STATUS_OK, _CAT_OK_BATCH, doc_id, summary


def _phase_index_snapshot(facade: LocalFacade) -> tuple[str, str]:
    """Force a fresh index snapshot.

    The batch runner already invokes :meth:`SearchGateway.snapshot` at
    the tail of every run, but doing it again here both (a) exercises
    the snapshot seam explicitly and (b) tolerates a future runner that
    might disable the tail snapshot.
    """
    try:
        facade.gateway.snapshot(facade.vault_root)
    except OSError:
        return _STATUS_FAIL, _CAT_FAIL_INDEX_SNAPSHOT_WRITE

    snapshot_path = facade.vault_root / "index" / "manifests.jsonl"
    if not snapshot_path.exists():  # pragma: no cover - defensive
        return _STATUS_FAIL, _CAT_FAIL_INDEX_SNAPSHOT_NOT_PERSISTED
    return _STATUS_OK, _CAT_OK_INDEX_SNAPSHOT


def _phase_index_reload_and_search(
    vault_root: Path,
    tenant_id: str | None,
) -> tuple[tuple[str, str], tuple[str, str]]:
    """Spawn a child process that loads the index and runs the smoke query.

    The child process is given ONLY the vault root and the tenant id (as
    process-arguments via stdin JSON). It does NOT inherit any in-memory
    state from the parent; the search index is loaded fresh from
    ``<vault_root>/index/manifests.jsonl``. This pins the spec's
    "subprocess-isolation" requirement: phase 3 cannot accidentally
    short-circuit through Python-level shared state.

    Returns ``((reload_status, reload_category), (search_status,
    search_category))`` — one tuple per phase the child spans.
    """
    payload = {
        "vault_root": str(vault_root),
        "tenant_id": tenant_id,
        "query": _SEARCH_QUERY,
    }
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                _CHILD_SOURCE,
            ],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return (
            (_STATUS_FAIL, _CAT_FAIL_INDEX_RELOAD),
            (_STATUS_FAIL, _CAT_FAIL_SEARCH_SMOKE),
        )

    if completed.returncode != 0:
        return (
            (_STATUS_FAIL, _CAT_FAIL_INDEX_RELOAD),
            (_STATUS_FAIL, _CAT_FAIL_SEARCH_SMOKE),
        )

    try:
        report = json.loads(completed.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return (
            (_STATUS_FAIL, _CAT_FAIL_INDEX_RELOAD),
            (_STATUS_FAIL, _CAT_FAIL_SEARCH_SMOKE),
        )

    loaded = int(report.get("loaded", 0))
    hits = int(report.get("hits", 0))

    reload_status = (
        (_STATUS_OK, _CAT_OK_INDEX_RELOAD)
        if loaded > 0
        else (_STATUS_FAIL, _CAT_FAIL_INDEX_RELOAD)
    )
    search_status = (
        (_STATUS_OK, _CAT_OK_SEARCH_SMOKE)
        if hits > 0
        else (_STATUS_FAIL, _CAT_FAIL_SEARCH_SMOKE)
    )
    return reload_status, search_status


# The child program source. Embedded as a module-level constant so the
# spawned interpreter receives a self-contained literal — no on-disk
# helper file is created, and the child cannot accidentally import
# state from the parent process. The child writes ONE JSON line to
# stdout (counts only); it never echoes manifest bodies or raw text.
_CHILD_SOURCE = r"""
import json
import sys
from pathlib import Path

payload = json.loads(sys.stdin.read())
vault_root = Path(payload["vault_root"])
tenant_id = payload.get("tenant_id")
query = payload["query"]

from yomotsusaka.facade import LocalFacade
from yomotsusaka.tenant import TenantScope
from yomotsusaka.search_gateway import SearchGateway
from yomotsusaka.boundary import SearchRequest

if tenant_id is not None:
    tenant = TenantScope(tenant_id=tenant_id, vault_root=vault_root)
    facade = LocalFacade(tenant=tenant, gateway=SearchGateway())
else:
    facade = LocalFacade(vault_root, gateway=SearchGateway())

loaded = facade.gateway.load(vault_root)
response = facade.search(SearchRequest(query=query))
hits = len(response.hits)

# Public-safe: counts only, never the snippet text or any handle locator.
print(json.dumps({"loaded": loaded, "hits": hits}))
"""


def _phase_restoration(
    facade: LocalFacade, doc_id: str
) -> tuple[str, str, str | None]:
    """Submit a public restoration request via the facade.

    The facade is hard-wired to ordinary-agent scope; the boundary's
    scope gate denies the request and writes one audit row before
    returning. The phase succeeds **only** when the response matches the
    expected contract:

    * ``outcome == "failed"`` (any other outcome would mean the kernel
      actually returned private entries — a privilege escalation the
      facade is supposed to prevent and the smoke must catch);
    * ``reason == RestorationFailureReason.ScopeDenied`` (any other
      ``RestorationFailureReason`` means a different failure path ran —
      e.g. ``AuditWriteFailed`` would mean the row this phase relies on
      was never appended, which the next phase would mis-classify);
    * ``audit_record_id`` is non-empty so phase 6 can correlate on it.

    Returns ``(status, category, audit_record_id_or_none)``. The audit
    record id is forwarded to :func:`_phase_audit_inspect`, which uses
    it as the sole correlation key — relying on ``caller_label`` and
    ``document_id`` alone is insufficient because both are stable across
    reruns and a stale row from a previous invocation would mask a
    missing write from the current run (codex review on PR #99, P2).
    """
    request = RestorationRequest(
        caller_label=_RESTORATION_CALLER_LABEL,
        reason="agent-runnable operational scenario",
        timestamp=datetime.now(timezone.utc),
        document_id=doc_id,
        requested_entity_kinds=[EntityKind.PERSON],
    )
    try:
        response = facade.request_restore(request)
    except Exception:  # pragma: no cover - defensive
        return _STATUS_FAIL, _CAT_FAIL_RESTORATION, None

    if not isinstance(response, RestorationResponse):  # pragma: no cover
        return _STATUS_FAIL, _CAT_FAIL_RESTORATION, None
    # Contract check (codex review on PR #99, P1). The facade pins the
    # scope to ordinary-agent, so the boundary's scope gate is the only
    # outcome that should ever surface here. Any other outcome / reason
    # indicates either a regression in the facade's privilege pinning
    # or a different failure mode on the audit-write path — either way,
    # the operational backbone is NOT healthy and the phase must fail
    # loudly rather than report ``ok`` while restoration semantics are
    # broken.
    if response.outcome != "failed":
        return _STATUS_FAIL, _CAT_FAIL_RESTORATION, None
    if response.reason is not RestorationFailureReason.ScopeDenied:
        return _STATUS_FAIL, _CAT_FAIL_RESTORATION, None
    if not response.audit_record_id:
        return _STATUS_FAIL, _CAT_FAIL_RESTORATION, None
    return _STATUS_OK, _CAT_OK_RESTORATION, response.audit_record_id


def _phase_audit_inspect(
    vault_root: Path, audit_record_id: str | None
) -> tuple[str, str]:
    """Read the audit JSONL and assert the phase-5 row landed.

    Correlation key is the ``audit_record_id`` returned by phase 5 —
    not ``caller_label`` + ``document_id``, which are both stable
    across reruns and would let a stale row from a previous invocation
    mask a missing write from the current run (codex review on PR #99,
    P2).

    The audit file is read as plain text — no kernel-side parser is
    imported, so the privacy-boundary import scan stays green.
    """
    if audit_record_id is None:
        # Phase 5 short-circuited; phase 6 cannot synthesise a positive
        # answer without a correlation key, so report the no-match
        # category. ``audit_file_missing`` would mislead — the file may
        # well exist and contain unrelated rows.
        return _STATUS_FAIL, _CAT_FAIL_AUDIT_NO_MATCH

    audit_path = vault_root / "audit" / "restoration.jsonl"
    if not audit_path.exists():
        return _STATUS_FAIL, _CAT_FAIL_AUDIT_MISSING
    try:
        lines = audit_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return _STATUS_FAIL, _CAT_FAIL_AUDIT_MISSING

    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except ValueError:
            continue
        if record.get("audit_record_id") == audit_record_id:
            return _STATUS_OK, _CAT_OK_AUDIT
    return _STATUS_FAIL, _CAT_FAIL_AUDIT_NO_MATCH


def _phase_runpod_lifecycle(
    *, keep_pod: bool, env: dict[str, str]
) -> tuple[str, str]:
    """Drive create → wait → smoke → delete via ``ManageRunPodLifecycle``.

    The lifecycle import is lazy so the no-network default never touches
    the runpod surface. Bypasses :mod:`scripts.manage_runpod` because the
    spec wants the agent-side scenario to exercise the in-tree library
    directly (no shell-out); however the same category vocabulary is
    reused so downstream readers can pattern-match identically.
    """
    api_key = (env.get("RUNPOD_API_KEY") or "").strip()
    if not api_key:
        return _STATUS_FAIL, _CAT_FAIL_RUNPOD_PREFLIGHT_APIKEY

    # Local import: keeps the no-network default free of httpx setup.
    from yomotsusaka.runpod_lifecycle import (  # noqa: PLC0415
        ManageRunPodLifecycle,
        PodConfig,
        PodUnavailableError,
    )

    lifecycle = ManageRunPodLifecycle(
        api_key=api_key, pod_config=PodConfig()
    )

    try:
        handle = lifecycle.start_pod()
    except PodUnavailableError as exc:
        raw = exc.args[0] if exc.args else _CAT_FAIL_RUNPOD_CREATE
        if raw == "wait_timeout":
            # Library cleaned up the Pod before re-raising — honest token.
            return _STATUS_FAIL, _CAT_FAIL_RUNPOD_WAIT
        if raw == "wait_timeout_cleanup_failed":
            # Library attempted cleanup but it also failed — Pod may still
            # exist and bill. Route to _CAT_FAIL_RUNPOD_CLEANUP so
            # _synthesise_result maps this to failed_owner_action (exit 3).
            return _STATUS_FAIL, _CAT_FAIL_RUNPOD_WAIT_TIMEOUT_CLEANUP_FAILED
        return _STATUS_FAIL, _CAT_FAIL_RUNPOD_CREATE

    if keep_pod:
        # The Pod was created but the caller asked to keep it; this is
        # ``ok`` for the scenario (the agent successfully drove the
        # lifecycle through the wait phase) but downgrades the final
        # result to ``completed_with_warnings`` so the owner knows the
        # Pod is still running and bills.
        return _STATUS_WARN, _CAT_KEPT_RUNPOD

    try:
        lifecycle.stop_pod(handle, terminate=True)
    except PodUnavailableError:
        # Cleanup failed: the Pod exists but we could not delete it.
        # The result-synthesis function below maps this to
        # ``failed_owner_action`` so the agent caller knows manual
        # cleanup is required.
        return _STATUS_FAIL, _CAT_FAIL_RUNPOD_CLEANUP

    return _STATUS_OK, _CAT_OK_RUNPOD


# ---------------------------------------------------------------------------
# Result synthesis
# ---------------------------------------------------------------------------


def _synthesise_result(
    statuses: list[tuple[str, str, str]],
) -> tuple[str, int]:
    """Map the per-phase ``(phase, status, category)`` list onto the final
    ``result=<...>`` token and exit code.

    Precedence (highest first):

    1. Any phase with status ``fail`` and category ``cleanup_failed`` OR
       ``wait_timeout_cleanup_failed`` → ``failed_owner_action`` (exit 3).
       Both categories indicate a Pod that may still be running and billing.
       ``failed_cleaned`` MUST NOT be emitted for these categories: doing so
       would falsely claim cleanup succeeded when it did not (issue #125).
    2. Any phase with status ``fail`` → ``failed_cleaned`` (exit 1). This
       path is only reached when NO Pod exists that requires owner cleanup.
       When the library raises ``wait_timeout``, it has already deleted the
       Pod before re-raising — so ``failed_cleaned`` is accurate here.
    3. Any phase with status ``warn`` → ``completed_with_warnings``
       (exit 0).
    4. All phases ``ok`` or ``skipped`` → ``completed`` (exit 0).
    """
    _owner_action_categories = {
        _CAT_FAIL_RUNPOD_CLEANUP,
        _CAT_FAIL_RUNPOD_WAIT_TIMEOUT_CLEANUP_FAILED,
    }
    if any(
        s == _STATUS_FAIL and c in _owner_action_categories
        for _, s, c in statuses
    ):
        return _RESULT_FAILED_OWNER_ACTION, _EXIT_FAILED_OWNER_ACTION
    if any(s == _STATUS_FAIL for _, s, _ in statuses):
        return _RESULT_FAILED_CLEANED, _EXIT_FAILED_CLEANED
    if any(s == _STATUS_WARN for _, s, _ in statuses):
        return _RESULT_COMPLETED_WITH_WARNINGS, _EXIT_OK
    return _RESULT_COMPLETED, _EXIT_OK


# ---------------------------------------------------------------------------
# Smoke -> report JSON bridge (issue #111)
# ---------------------------------------------------------------------------


# Phases whose ``status == "ok"`` should map to a ``1`` boolean-as-int
# counter in the synthesised ``ScenarioResult`` consumed by
# ``operational_report``. These names align with the ``_CANONICAL_COUNTERS``
# tuple in :mod:`yomotsusaka.operational_report`. A phase that did NOT
# run ``ok`` synthesises a ``0`` counter so the report renderer still sees
# the canonical key (matches the existing ``_baseline_counters`` shape used
# by ``operational_report``'s own test fixtures).
_PHASE_TO_BOOL_COUNTER: dict[str, str] = {
    _PHASE_INDEX_SNAPSHOT: "index_snapshot_ok",
    _PHASE_INDEX_RELOAD: "index_loadable",
    _PHASE_SEARCH_SMOKE: "search_smoke_ok",
}


def _synthesise_counters(
    statuses: list[tuple[str, str, str]],
    batch_summary: BatchSummary | None,
) -> dict[str, Any]:
    """Build the ``counters`` dict for the ``--emit-json`` payload.

    Public-safe by construction: only integer counts, boolean-as-int
    flags, and the public-safe ``runpod_lifecycle_category`` token. No
    vault paths, no doc identifiers, no raw text.

    The shape matches the keys ``yomotsusaka.operational_report``
    iterates over in ``_CANONICAL_COUNTERS``, plus the additional
    ``restoration_outcome`` and ``audit_row_count`` keys the renderer
    surfaces. Per the issue #111 augmentation:

    * ``processed_documents`` / ``failed_documents`` from
      ``BatchSummary.committed_count`` / ``failed_count`` (0 when the
      runner never produced a summary).
    * ``index_snapshot_ok`` / ``index_loadable`` / ``search_smoke_ok``
      synthesised from the corresponding phase status (1 iff ``ok``).
    * ``restoration_outcome`` derived from the restoration phase status
      (``"ok"`` on success, otherwise the phase status token).
    * ``audit_row_count`` is 1 iff the audit phase reported ``ok``
      (smoke does not count rows itself; the report renderer prints the
      integer verbatim).
    * ``runpod_lifecycle_category`` is included ONLY when the RunPod
      phase actually ran (``--live-runpod``). An empty string is
      forbidden by the contract — the key is OMITTED instead.
    """
    by_phase: dict[str, tuple[str, str]] = {
        phase: (status, category) for phase, status, category in statuses
    }
    counters: dict[str, Any] = {}

    if batch_summary is not None:
        counters["processed_documents"] = int(batch_summary.committed_count)
        counters["failed_documents"] = int(batch_summary.failed_count)
    else:
        counters["processed_documents"] = 0
        counters["failed_documents"] = 0

    for phase_name, counter_key in _PHASE_TO_BOOL_COUNTER.items():
        status_tuple = by_phase.get(phase_name)
        counters[counter_key] = (
            1 if status_tuple and status_tuple[0] == _STATUS_OK else 0
        )

    restoration = by_phase.get(_PHASE_RESTORATION)
    if restoration is None:
        counters["restoration_outcome"] = "not_attempted"
    elif restoration[0] == _STATUS_OK:
        counters["restoration_outcome"] = "ok"
    else:
        counters["restoration_outcome"] = restoration[0]

    audit = by_phase.get(_PHASE_AUDIT)
    counters["audit_row_count"] = 1 if audit and audit[0] == _STATUS_OK else 0

    runpod = by_phase.get(_PHASE_RUNPOD)
    # Per D4: include the runpod category ONLY when --live-runpod actually
    # ran (i.e. status is not "skipped"). An empty string is forbidden;
    # omit the key instead. The skipped-disposition is recorded in the
    # phase ledger itself, not as a counter.
    if runpod is not None and runpod[0] != _STATUS_SKIPPED:
        counters["runpod_lifecycle_category"] = runpod[1]
        # Cross-CLI state alignment (codex review on PR #119, P1).
        # ``operational_report.classify_result_state`` is fail-closed: a
        # failing scenario that touched RunPod defaults to
        # ``failed_owner_action`` UNLESS the strict-bool flag
        # ``runpod_cleanup_confirmed`` is exactly ``True``. Smoke's own
        # classifier (:func:`_synthesise_result`) only routes to
        # ``failed_owner_action`` when the runpod fail-category is
        # ``cleanup_failed``; every other runpod fail
        # (``api_key_missing``, ``wait_timeout``, ``create_failed``)
        # routes to ``failed_cleaned``. Without an explicit flag the
        # report renderer would render those latter cases as
        # owner-action-required while smoke's stdout said
        # ``failed_cleaned`` — a state mismatch that misleads operators.
        # Mirror smoke's classifier here so both CLIs agree.
        counters["runpod_cleanup_confirmed"] = (
            runpod[1] != _CAT_FAIL_RUNPOD_CLEANUP
        )

    return counters


def _build_scenario_payload(
    statuses: list[tuple[str, str, str]],
    batch_summary: BatchSummary | None,
) -> dict[str, Any]:
    """Build the JSON payload (``ScenarioResult`` shape) for ``--emit-json``.

    Mirrors :class:`yomotsusaka.operational_report.ScenarioResult`. The
    keys / shape are the same contract ``operational_report`` already
    consumes on stdin.
    """
    phases_payload = [
        {
            "phase_name": phase,
            "status": status,
            "category": category,
        }
        for phase, status, category in statuses
    ]
    return {
        "phases": phases_payload,
        "counters": _synthesise_counters(statuses, batch_summary),
    }


def _maybe_emit_json(
    args: argparse.Namespace,
    statuses: list[tuple[str, str, str]],
    batch_summary: BatchSummary | None,
) -> None:
    """Write the JSON ScenarioResult to ``args.emit_json`` (if set).

    Atomic write: serialise to ``<path>.tmp`` then ``os.replace`` onto the
    final path so a concurrent reader cannot observe a partial file. The
    parent directory is created on demand. On filesystem error the helper
    writes a single advisory line to stderr (stable token shape so agent
    callers can pattern-match) and returns; the run's exit code is NOT
    altered — the JSON bridge is best-effort, the phase ledger on stdout
    remains the load-bearing surface.

    Called AFTER ``result=<...>`` has already been emitted on stdout so a
    write failure cannot truncate the human-facing log.
    """
    emit_path: Path | None = args.emit_json
    if emit_path is None:
        return
    payload = _build_scenario_payload(statuses, batch_summary)
    try:
        emit_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = emit_path.with_suffix(emit_path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(payload, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp_path, emit_path)
    except OSError:
        # Stable stderr advisory; never echoes the path back (the path is
        # caller-supplied and may be private). Public-safe category token
        # only.
        sys.stderr.write("notice=emit_json_write_failed\n")
        sys.stderr.flush()


# ---------------------------------------------------------------------------
# Argument parsing + entry point
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m yomotsusaka.cli.operational_smoke",
        description=(
            "Agent-runnable operational scenario: run the canonical "
            "batch → index → reload → search → restoration → audit "
            "backbone (and optionally a live RunPod create/delete) as a "
            "single command. Output is one stable token per phase plus "
            "a final result line; nothing else lands on stdout."
        ),
    )
    parser.add_argument(
        "inbox_dir",
        type=Path,
        help="Directory containing raw text documents (recursively walked).",
    )
    parser.add_argument(
        "--vault-root",
        type=Path,
        required=True,
        help="Vault root for committed manifests and private dictionaries.",
    )
    parser.add_argument(
        "--tenant-id",
        type=str,
        default=None,
        help=(
            "Optional tenant id. When supplied the facade and the child "
            "subprocess both construct TenantScope(tenant_id=..., "
            "vault_root=...); otherwise the back-compat local scope is "
            "used."
        ),
    )
    parser.add_argument(
        "--live-runpod",
        action="store_true",
        default=False,
        help=(
            "Enable phase 7 (RunPod lifecycle). Without this flag the "
            "phase is skipped — every other phase completes with NO "
            "outbound network."
        ),
    )
    parser.add_argument(
        "--keep-pod",
        action="store_true",
        default=False,
        help=(
            "Only meaningful with --live-runpod. Skip the delete step "
            "after the Pod becomes healthy. Default is delete (per the "
            "agent-managed lifecycle's cost-control rule)."
        ),
    )
    parser.add_argument(
        "--demo-corpus",
        action="store_true",
        default=False,
        help=(
            "Materialise a small canonical fixture inbox under a fresh "
            "temp directory and use that inbox for the run. When set, "
            "the positional inbox_dir is ignored (a stderr advisory is "
            "emitted). Intended for the agent quickstart from a fresh "
            "checkout. The temp directory is removed on exit unless "
            "--keep-demo-corpus is also set."
        ),
    )
    parser.add_argument(
        "--keep-demo-corpus",
        action="store_true",
        default=False,
        help=(
            "Only meaningful with --demo-corpus. Skip the temp-dir "
            "cleanup after the run completes. The retained path is "
            "echoed to stderr (never stdout)."
        ),
    )
    parser.add_argument(
        "--emit-json",
        type=Path,
        default=None,
        help=(
            "Optional path. When supplied, write the structured "
            "ScenarioResult (matching the operational_report stdin "
            "contract) to this path AFTER the final result= line on "
            "stdout. Per-phase stdout lines remain unchanged. The file "
            "is written atomically (tmp + rename) so partial JSON "
            "cannot be read by a concurrent reader."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit code per the table in the
    module docstring."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    # --keep-demo-corpus is only meaningful with --demo-corpus. Reject
    # the lone-flag case at parse time so the user sees a usage error
    # rather than a silent no-op.
    if args.keep_demo_corpus and not args.demo_corpus:
        parser.error("--keep-demo-corpus requires --demo-corpus")

    # When --demo-corpus is set, materialise a transient inbox BEFORE
    # the existing inbox probe and use it for the run. The positional
    # arg is preserved on ``args.inbox_dir`` but ignored for routing;
    # a single stable stderr advisory records the override so the agent
    # caller has a trail. Cleanup is wired through ``try/finally`` so
    # an unhandled exception still removes the temp dir (unless the
    # caller opted into --keep-demo-corpus).
    demo_root: Path | None = None
    if args.demo_corpus:
        demo_root = _materialise_demo_corpus()
        sys.stderr.write(f"{_NOTICE_DEMO_CORPUS_OVERRIDE}\n")
        sys.stderr.flush()
        inbox: Path = demo_root
    else:
        inbox = args.inbox_dir
    vault_root: Path = args.vault_root

    try:
        return _run_main(args, inbox, vault_root)
    finally:
        if demo_root is not None:
            if args.keep_demo_corpus:
                # Path on stderr only — the explicit owner opt-in to
                # retain the temp dir means the caller wants to find
                # it, but stdout discipline still forbids any new
                # token there.
                sys.stderr.write(
                    f"{_NOTICE_DEMO_CORPUS_KEPT} path={demo_root}\n"
                )
                sys.stderr.flush()
            else:
                shutil.rmtree(demo_root, ignore_errors=False)


def _run_main(
    args: argparse.Namespace, inbox: Path, vault_root: Path
) -> int:
    """Body of ``main`` factored out so the demo-corpus ``try/finally``
    wrapper can guarantee cleanup regardless of the exit path.
    """
    # Probe inbox / vault before kicking off any phase so a startup
    # failure does not emit a confusing partial-phase log.
    if not inbox.exists() or not inbox.is_dir():
        # Map startup misuse onto the batch phase's infra category so the
        # caller still sees a phase=batch line and the result=failed_cleaned
        # synthesis behaves uniformly.
        _emit_phase(_PHASE_BATCH, _STATUS_FAIL, _CAT_FAIL_BATCH_INFRA)
        _emit_result(_RESULT_FAILED_CLEANED)
        _maybe_emit_json(
            args,
            [(_PHASE_BATCH, _STATUS_FAIL, _CAT_FAIL_BATCH_INFRA)],
            None,
        )
        return _EXIT_FAILED_CLEANED

    try:
        vault_root.mkdir(parents=True, exist_ok=True)
    except OSError:
        _emit_phase(_PHASE_BATCH, _STATUS_FAIL, _CAT_FAIL_BATCH_INFRA)
        _emit_result(_RESULT_FAILED_CLEANED)
        _maybe_emit_json(
            args,
            [(_PHASE_BATCH, _STATUS_FAIL, _CAT_FAIL_BATCH_INFRA)],
            None,
        )
        return _EXIT_FAILED_CLEANED

    try:
        facade = _construct_facade(vault_root, args.tenant_id)
    except (ValueError, TypeError):
        _emit_phase(_PHASE_BATCH, _STATUS_FAIL, _CAT_FAIL_BATCH_INFRA)
        _emit_result(_RESULT_FAILED_CLEANED)
        _maybe_emit_json(
            args,
            [(_PHASE_BATCH, _STATUS_FAIL, _CAT_FAIL_BATCH_INFRA)],
            None,
        )
        return _EXIT_FAILED_CLEANED

    statuses: list[tuple[str, str, str]] = []

    # ---- phase 1: batch ----
    batch_status, batch_category, doc_id, batch_summary = _phase_batch(
        facade, inbox
    )
    statuses.append((_PHASE_BATCH, batch_status, batch_category))
    _emit_phase(_PHASE_BATCH, batch_status, batch_category)

    # If batch failed outright (no doc_id), we still emit the remaining
    # phases as ``fail`` with cause-category so the agent caller sees a
    # complete phase ledger (rather than a truncated stream). Each
    # downstream phase's category reflects the actual short-circuit
    # reason, not a bogus "we didn't run".
    if doc_id is None:
        for phase, cat in (
            (_PHASE_INDEX_SNAPSHOT, _CAT_FAIL_INDEX_SNAPSHOT_WRITE),
            (_PHASE_INDEX_RELOAD, _CAT_FAIL_INDEX_RELOAD),
            (_PHASE_SEARCH_SMOKE, _CAT_FAIL_SEARCH_SMOKE),
            (_PHASE_RESTORATION, _CAT_FAIL_RESTORATION),
            (_PHASE_AUDIT, _CAT_FAIL_AUDIT_MISSING),
        ):
            statuses.append((phase, _STATUS_FAIL, cat))
            _emit_phase(phase, _STATUS_FAIL, cat)
        # RunPod phase still runs its own decision (skipped vs live).
        runpod_status, runpod_category = _runpod_phase_outcome(args)
        statuses.append((_PHASE_RUNPOD, runpod_status, runpod_category))
        _emit_phase(_PHASE_RUNPOD, runpod_status, runpod_category)
        result, exit_code = _synthesise_result(statuses)
        _emit_result(result)
        _maybe_emit_json(args, statuses, batch_summary)
        return exit_code

    # ---- phase 2: index_snapshot ----
    snap_status, snap_category = _phase_index_snapshot(facade)
    statuses.append((_PHASE_INDEX_SNAPSHOT, snap_status, snap_category))
    _emit_phase(_PHASE_INDEX_SNAPSHOT, snap_status, snap_category)

    # ---- phase 3 + 4: subprocess loads index + runs smoke query ----
    (reload_status, reload_category), (search_status, search_category) = (
        _phase_index_reload_and_search(vault_root, args.tenant_id)
    )
    statuses.append((_PHASE_INDEX_RELOAD, reload_status, reload_category))
    _emit_phase(_PHASE_INDEX_RELOAD, reload_status, reload_category)
    statuses.append((_PHASE_SEARCH_SMOKE, search_status, search_category))
    _emit_phase(_PHASE_SEARCH_SMOKE, search_status, search_category)

    # ---- phase 5: restoration_request ----
    # Phase 5 returns the audit_record_id so phase 6 can correlate
    # exactly (codex review on PR #99, P2). Without that key, a stale
    # row from a previous invocation against the same vault would let
    # phase 6 report ok even when the current phase 5 wrote nothing.
    rest_status, rest_category, audit_record_id = _phase_restoration(
        facade, doc_id
    )
    statuses.append((_PHASE_RESTORATION, rest_status, rest_category))
    _emit_phase(_PHASE_RESTORATION, rest_status, rest_category)

    # ---- phase 6: audit_inspect ----
    audit_status, audit_category = _phase_audit_inspect(
        vault_root, audit_record_id
    )
    statuses.append((_PHASE_AUDIT, audit_status, audit_category))
    _emit_phase(_PHASE_AUDIT, audit_status, audit_category)

    # ---- phase 7: runpod_lifecycle (skipped by default) ----
    runpod_status, runpod_category = _runpod_phase_outcome(args)
    statuses.append((_PHASE_RUNPOD, runpod_status, runpod_category))
    _emit_phase(_PHASE_RUNPOD, runpod_status, runpod_category)

    result, exit_code = _synthesise_result(statuses)
    _emit_result(result)
    _maybe_emit_json(args, statuses, batch_summary)
    return exit_code


def _runpod_phase_outcome(args: argparse.Namespace) -> tuple[str, str]:
    """Return the runpod phase ``(status, category)`` for *args*.

    Factored out so the no-doc-id early-exit branch and the happy path
    share the same RunPod decision tree.
    """
    if not args.live_runpod:
        return _STATUS_SKIPPED, _CAT_SKIPPED_RUNPOD
    return _phase_runpod_lifecycle(
        keep_pod=args.keep_pod, env=dict(os.environ)
    )


# Re-exported imports used only for the test suite's deny-list scan;
# pinning them to ``__all__`` keeps the public surface explicit.
__all__ = ["main"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
