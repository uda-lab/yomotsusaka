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
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from yomotsusaka.batch_runner import BatchRunner
from yomotsusaka.boundary import (
    RestorationRequest,
    RestorationResponse,
)
from yomotsusaka.facade import LocalFacade
from yomotsusaka.schemas import EntityKind
from yomotsusaka.tenant import TenantScope


# ---------------------------------------------------------------------------
# Stable category vocabulary
# ---------------------------------------------------------------------------
#
# These tokens are the ONLY values that may appear in ``category=<...>``.
# Stable across releases; downstream agent consumers may pattern-match on
# them. Coordinate any addition with child 04 (#93) which owns the cross-
# script taxonomy consolidation, but do NOT block this child on that.

# Phase-level OK categories.
_CAT_OK_BATCH = "batch_committed"
_CAT_OK_INDEX_SNAPSHOT = "snapshot_written"
_CAT_OK_INDEX_RELOAD = "index_reloaded"
_CAT_OK_SEARCH_SMOKE = "hits_found"
_CAT_OK_RESTORATION = "restoration_request_recorded"
_CAT_OK_AUDIT = "audit_present"
_CAT_OK_RUNPOD = "runpod_cycle_complete"

# Skipped category — RunPod phase only.
_CAT_SKIPPED_RUNPOD = "runpod_disabled"
_CAT_KEPT_RUNPOD = "runpod_kept"

# Phase-level FAIL categories.
_CAT_FAIL_BATCH_EMPTY = "batch_no_documents"
_CAT_FAIL_BATCH_ALL_FAILED = "batch_all_failed"
_CAT_FAIL_BATCH_PARTIAL = "batch_partial_commit"
_CAT_FAIL_BATCH_INFRA = "batch_infrastructure_error"
_CAT_FAIL_INDEX_SNAPSHOT_WRITE = "snapshot_write_failed"
_CAT_FAIL_INDEX_SNAPSHOT_NOT_PERSISTED = "snapshot_not_persisted"
_CAT_FAIL_INDEX_RELOAD = "index_reload_failed"
_CAT_FAIL_SEARCH_SMOKE = "search_no_hits"
_CAT_FAIL_RESTORATION = "restoration_request_unexpected_outcome"
_CAT_FAIL_AUDIT_MISSING = "audit_file_missing"
_CAT_FAIL_AUDIT_NO_MATCH = "audit_record_not_found"
_CAT_FAIL_RUNPOD_CREATE = "create_failed"
_CAT_FAIL_RUNPOD_WAIT = "wait_timeout"
_CAT_FAIL_RUNPOD_PREFLIGHT_APIKEY = "api_key_missing"
_CAT_FAIL_RUNPOD_CLEANUP = "cleanup_failed"

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
) -> tuple[str, str, str | None]:
    """Run the batch runner against *inbox*.

    Returns ``(status, category, doc_id_for_restoration_phase)``. The
    third element is a doc_id from a successfully committed document the
    later phases can target; ``None`` on failure.
    """
    runner = BatchRunner(facade=facade)
    try:
        summary = runner.run_directory(inbox)
    except (FileNotFoundError, NotADirectoryError):
        return _STATUS_FAIL, _CAT_FAIL_BATCH_INFRA, None
    except Exception:  # pragma: no cover - defensive
        return _STATUS_FAIL, _CAT_FAIL_BATCH_INFRA, None

    if summary.submitted_count == 0:
        return _STATUS_FAIL, _CAT_FAIL_BATCH_EMPTY, None
    if summary.committed_count == 0:
        return _STATUS_FAIL, _CAT_FAIL_BATCH_ALL_FAILED, None

    # Pick a stable doc_id from the committed set so phases 5/6 can
    # target a known artifact. Reading the manifests directory is a
    # public-side read (the manifests are the redacted projection); no
    # private dictionary is touched here.
    manifests_dir = facade.vault_root / "manifests"
    try:
        candidates = sorted(manifests_dir.glob("*.json"))
    except OSError:
        return _STATUS_FAIL, _CAT_FAIL_BATCH_INFRA, None
    if not candidates:
        return _STATUS_FAIL, _CAT_FAIL_BATCH_ALL_FAILED, None
    doc_id = candidates[0].stem

    if summary.failed_count > 0:
        # A partial-commit batch is a warning — work proceeds, but the
        # synthesis result will downgrade to ``completed_with_warnings``.
        return _STATUS_WARN, _CAT_FAIL_BATCH_PARTIAL, doc_id
    return _STATUS_OK, _CAT_OK_BATCH, doc_id


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
) -> tuple[str, str]:
    """Submit a public restoration request via the facade.

    The facade is hard-wired to ordinary-agent scope; the response is a
    ``scope_denied`` failure, but the boundary writes an audit row
    regardless. The phase succeeds when the response is a
    well-formed :class:`RestorationResponse` carrying a non-empty
    ``audit_record_id``; the next phase (``audit_inspect``) verifies the
    row actually landed on disk.
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
        return _STATUS_FAIL, _CAT_FAIL_RESTORATION

    if not isinstance(response, RestorationResponse):  # pragma: no cover
        return _STATUS_FAIL, _CAT_FAIL_RESTORATION
    if not response.audit_record_id:
        return _STATUS_FAIL, _CAT_FAIL_RESTORATION
    return _STATUS_OK, _CAT_OK_RESTORATION


def _phase_audit_inspect(
    vault_root: Path, doc_id: str
) -> tuple[str, str]:
    """Read the audit JSONL and assert the restoration row landed.

    The audit file is read as plain text — no kernel-side parser is
    imported, so the privacy-boundary import scan stays green. Each line
    is parsed as JSON and matched on ``caller_label`` and a target field
    that names ``doc_id``.
    """
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
        if record.get("caller_label") != _RESTORATION_CALLER_LABEL:
            continue
        target = record.get("target") or {}
        if isinstance(target, dict) and target.get("document_id") == doc_id:
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
            return _STATUS_FAIL, _CAT_FAIL_RUNPOD_WAIT
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

    1. Any phase with status ``fail`` and category ``cleanup_failed`` →
       ``failed_owner_action`` (exit 3).
    2. Any phase with status ``fail`` → ``failed_cleaned`` (exit 1).
    3. Any phase with status ``warn`` → ``completed_with_warnings``
       (exit 0).
    4. All phases ``ok`` or ``skipped`` → ``completed`` (exit 0).
    """
    if any(
        s == _STATUS_FAIL and c == _CAT_FAIL_RUNPOD_CLEANUP
        for _, s, c in statuses
    ):
        return _RESULT_FAILED_OWNER_ACTION, _EXIT_FAILED_OWNER_ACTION
    if any(s == _STATUS_FAIL for _, s, _ in statuses):
        return _RESULT_FAILED_CLEANED, _EXIT_FAILED_CLEANED
    if any(s == _STATUS_WARN for _, s, _ in statuses):
        return _RESULT_COMPLETED_WITH_WARNINGS, _EXIT_OK
    return _RESULT_COMPLETED, _EXIT_OK


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
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit code per the table in the
    module docstring."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    inbox: Path = args.inbox_dir
    vault_root: Path = args.vault_root

    # Probe inbox / vault before kicking off any phase so a startup
    # failure does not emit a confusing partial-phase log.
    if not inbox.exists() or not inbox.is_dir():
        # Map startup misuse onto the batch phase's infra category so the
        # caller still sees a phase=batch line and the result=failed_cleaned
        # synthesis behaves uniformly.
        _emit_phase(_PHASE_BATCH, _STATUS_FAIL, _CAT_FAIL_BATCH_INFRA)
        _emit_result(_RESULT_FAILED_CLEANED)
        return _EXIT_FAILED_CLEANED

    try:
        vault_root.mkdir(parents=True, exist_ok=True)
    except OSError:
        _emit_phase(_PHASE_BATCH, _STATUS_FAIL, _CAT_FAIL_BATCH_INFRA)
        _emit_result(_RESULT_FAILED_CLEANED)
        return _EXIT_FAILED_CLEANED

    try:
        facade = _construct_facade(vault_root, args.tenant_id)
    except (ValueError, TypeError):
        _emit_phase(_PHASE_BATCH, _STATUS_FAIL, _CAT_FAIL_BATCH_INFRA)
        _emit_result(_RESULT_FAILED_CLEANED)
        return _EXIT_FAILED_CLEANED

    statuses: list[tuple[str, str, str]] = []

    # ---- phase 1: batch ----
    batch_status, batch_category, doc_id = _phase_batch(facade, inbox)
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
    rest_status, rest_category = _phase_restoration(facade, doc_id)
    statuses.append((_PHASE_RESTORATION, rest_status, rest_category))
    _emit_phase(_PHASE_RESTORATION, rest_status, rest_category)

    # ---- phase 6: audit_inspect ----
    audit_status, audit_category = _phase_audit_inspect(vault_root, doc_id)
    statuses.append((_PHASE_AUDIT, audit_status, audit_category))
    _emit_phase(_PHASE_AUDIT, audit_status, audit_category)

    # ---- phase 7: runpod_lifecycle (skipped by default) ----
    runpod_status, runpod_category = _runpod_phase_outcome(args)
    statuses.append((_PHASE_RUNPOD, runpod_status, runpod_category))
    _emit_phase(_PHASE_RUNPOD, runpod_status, runpod_category)

    result, exit_code = _synthesise_result(statuses)
    _emit_result(result)
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
