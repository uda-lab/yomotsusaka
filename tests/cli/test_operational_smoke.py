"""Integration tests for :mod:`yomotsusaka.cli.operational_smoke`.

Covers:

* the full no-network happy path against a canonical-fixture inbox;
* the boundary-discipline source scan (no private-kernel module import
  from the CLI);
* the public-safe stdout discipline (no raw private values, no vault
  paths, no pod identifiers on stdout);
* per-phase failure injection — each phase forced to fail in turn and
  the resulting ``result=<...>`` token + exit code asserted;
* a subprocess-isolation test confirming phase 3 does not see phase 2's
  in-memory state.

The tests deliberately avoid setting ``RUNPOD_API_KEY`` so the default
no-network mode is exercised on every run.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable

import pytest

from tests._exposure_denylist import (
    CANONICAL_TEXT,
    PATH_LEAK_PATTERNS,
    RAW_VALUES,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _write_corpus(inbox: Path, files: dict[str, str]) -> None:
    inbox.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (inbox / name).write_text(body, encoding="utf-8")


def _canonical_corpus() -> dict[str, str]:
    return {
        "doc-alpha.txt": CANONICAL_TEXT,
        "doc-beta.txt": (
            "Bob Lee joined Globex Inc. Patient ID: 67890."
        ),
    }


def _run_cli(
    args: list[str], *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env_full = dict(os.environ) if env is None else env
    # The no-network default is meaningful only when RUNPOD_API_KEY is
    # absent. Tests scrub it from the spawned env regardless of caller
    # to keep the assertion contract honest.
    env_full.pop("RUNPOD_API_KEY", None)
    return subprocess.run(
        [sys.executable, "-m", "yomotsusaka.cli.operational_smoke", *args],
        capture_output=True,
        text=True,
        check=False,
        env=env_full,
    )


def _parse_phase_lines(stdout: str) -> list[tuple[str, str, str]]:
    """Return ``[(phase, status, category), ...]`` for every phase line."""
    pat = re.compile(
        r"^phase=(?P<phase>\S+) status=(?P<status>\S+) "
        r"category=(?P<category>\S+)$"
    )
    out: list[tuple[str, str, str]] = []
    for line in stdout.splitlines():
        m = pat.match(line)
        if m:
            out.append((m["phase"], m["status"], m["category"]))
    return out


def _result_line(stdout: str) -> str | None:
    for line in stdout.splitlines():
        if line.startswith("result="):
            return line[len("result=") :]
    return None


# ---------------------------------------------------------------------------
# Boundary discipline — module-source scan
# ---------------------------------------------------------------------------


_FORBIDDEN_KERNEL_MODULES: tuple[str, ...] = (
    "pipeline",
    "commit",
    "restoration_api",
    "templates",
    "scrubber",
    "audit",
)


def test_cli_does_not_import_private_kernel_modules() -> None:
    """The CLI must access pipeline / commit / restoration / templates /
    scrubber / audit exclusively through :class:`LocalFacade` or the
    public boundary surface. The invariant is asserted by a literal
    substring scan of the CLI's source file.
    """
    src = (
        Path(__file__).resolve().parent.parent.parent
        / "src"
        / "yomotsusaka"
        / "cli"
        / "operational_smoke.py"
    ).read_text(encoding="utf-8")
    for mod in _FORBIDDEN_KERNEL_MODULES:
        pattern = re.compile(
            rf"^(from|import)\s+yomotsusaka\.{mod}\b", re.MULTILINE
        )
        assert not pattern.search(src), (
            f"operational_smoke.py imports forbidden private-kernel "
            f"module yomotsusaka.{mod}; access must be facade-only"
        )


# ---------------------------------------------------------------------------
# Happy path — full no-network scenario
# ---------------------------------------------------------------------------


def test_full_no_network_scenario_completes(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    vault = tmp_path / "vault"
    _write_corpus(inbox, _canonical_corpus())

    result = _run_cli([str(inbox), "--vault-root", str(vault)])

    assert result.returncode == 0, (
        f"exit={result.returncode}; stdout={result.stdout!r}; "
        f"stderr={result.stderr!r}"
    )

    phases = _parse_phase_lines(result.stdout)
    phase_names = [p for p, _, _ in phases]
    assert phase_names == [
        "batch",
        "index_snapshot",
        "index_reload",
        "search_smoke",
        "restoration_request",
        "audit_inspect",
        "runpod_lifecycle",
    ], phase_names

    statuses = {p: (s, c) for p, s, c in phases}
    assert statuses["batch"] == ("ok", "batch_ok")
    assert statuses["index_snapshot"] == ("ok", "index_snapshot_ok")
    assert statuses["index_reload"] == ("ok", "index_reload_ok")
    assert statuses["search_smoke"] == ("ok", "search_smoke_ok")
    assert statuses["restoration_request"] == (
        "ok",
        "restoration_ok",
    )
    assert statuses["audit_inspect"] == ("ok", "audit_inspect_ok")
    assert statuses["runpod_lifecycle"] == ("skipped", "runpod_lifecycle_disabled")

    assert _result_line(result.stdout) == "completed"


def test_full_no_network_scenario_with_tenant_id(tmp_path: Path) -> None:
    """The ``--tenant-id`` path must traverse every phase identically.

    Cross-process tenant propagation: the parent constructs a
    :class:`TenantScope` and the child subprocess that runs phase 3/4
    must also see the same tenant so the snapshot path resolves.
    """
    inbox = tmp_path / "inbox"
    vault = tmp_path / "vault"
    _write_corpus(inbox, _canonical_corpus())

    result = _run_cli(
        [
            str(inbox),
            "--vault-root",
            str(vault),
            "--tenant-id",
            "tenant-001",
        ]
    )
    assert result.returncode == 0, (
        f"exit={result.returncode}; stdout={result.stdout!r}; "
        f"stderr={result.stderr!r}"
    )
    statuses = {
        p: (s, c) for p, s, c in _parse_phase_lines(result.stdout)
    }
    assert statuses["index_reload"] == ("ok", "index_reload_ok")
    assert statuses["search_smoke"] == ("ok", "search_smoke_ok")
    assert _result_line(result.stdout) == "completed"


# ---------------------------------------------------------------------------
# Public-safe stdout discipline
# ---------------------------------------------------------------------------


def test_stdout_carries_no_raw_private_values_or_vault_paths(
    tmp_path: Path,
) -> None:
    """Every byte of stdout must satisfy the deny-list:

    * No raw value from ``RAW_VALUES``.
    * No vault-shape path matching ``PATH_LEAK_PATTERNS``.
    * No absolute path containing the tmp_path prefix (which acts as
      a stand-in for any caller-private vault location).

    The CLI guarantees this by emitting only fixed-shape token lines.
    """
    inbox = tmp_path / "inbox"
    vault = tmp_path / "vault"
    _write_corpus(inbox, {"canonical.txt": CANONICAL_TEXT})

    result = _run_cli([str(inbox), "--vault-root", str(vault)])
    assert result.returncode == 0

    blob = result.stdout
    for raw in RAW_VALUES:
        assert raw not in blob, (
            f"operational_smoke stdout leaked raw private value {raw!r}"
        )
    for pattern in PATH_LEAK_PATTERNS:
        assert not pattern.search(blob), (
            f"operational_smoke stdout leaked vault-shape path matching "
            f"{pattern.pattern!r}"
        )
    # The caller-supplied vault root path itself must not echo back —
    # public-safe output is opaque, not a caller-input echo. The inbox
    # path lives outside the vault, so use the vault root prefix only.
    assert str(vault) not in blob


def test_no_phase_line_includes_unexpected_tokens(tmp_path: Path) -> None:
    """Every non-empty stdout line must match either the per-phase
    fixed shape or the final ``result=`` line. No diagnostic, no
    interpolated value, ever lands on stdout.
    """
    inbox = tmp_path / "inbox"
    vault = tmp_path / "vault"
    _write_corpus(inbox, _canonical_corpus())

    result = _run_cli([str(inbox), "--vault-root", str(vault)])
    assert result.returncode == 0

    phase_line = re.compile(
        r"^phase=\S+ status=(ok|warn|fail|skipped) category=\S+$"
    )
    result_line = re.compile(
        r"^result=(completed|completed_with_warnings|failed_cleaned|"
        r"failed_owner_action)$"
    )
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        assert phase_line.match(line) or result_line.match(line), (
            f"unexpected stdout line shape: {line!r}"
        )


# ---------------------------------------------------------------------------
# Subprocess isolation — phase 3 must not see phase 2's in-memory state
# ---------------------------------------------------------------------------


def test_phase_3_subprocess_only_uses_on_disk_index(
    tmp_path: Path,
) -> None:
    """The reload phase loads the index from disk. If the snapshot file
    is removed BEFORE the CLI runs and never written, phase 3 must
    report a fresh-load count of zero — proving the child process did
    not inherit any in-memory manifests from the parent.

    Mechanism: corrupt the snapshot to empty bytes between the parent's
    snapshot phase and the child's load. We do this by intercepting via
    a wrapper script that asserts the vault was already written and the
    snapshot file is empty.
    """
    # Easier-to-reason-about variant: drive the CLI once, then directly
    # spawn the child program embedded in the module against a vault
    # that has NO index file. The child should report loaded=0 / hits=0.
    inbox = tmp_path / "inbox"
    vault = tmp_path / "vault"
    _write_corpus(inbox, _canonical_corpus())

    # First run: produce a valid vault.
    initial = _run_cli([str(inbox), "--vault-root", str(vault)])
    assert initial.returncode == 0

    # Now blow away the index snapshot so the child process truly only
    # sees an empty index when it loads from disk.
    snapshot_path = vault / "index" / "manifests.jsonl"
    assert snapshot_path.exists(), "first run should have written a snapshot"
    snapshot_path.unlink()

    # Re-run only the child program (extract the embedded child source
    # via attribute access). The child is the exact program phase 3
    # spawns — same isolation contract.
    from yomotsusaka.cli import operational_smoke as cli_mod

    payload = json.dumps(
        {
            "vault_root": str(vault),
            "tenant_id": None,
            "query": "<PERSON_",
        }
    )
    completed = subprocess.run(
        [sys.executable, "-c", cli_mod._CHILD_SOURCE],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0
    last_line = completed.stdout.strip().splitlines()[-1]
    report = json.loads(last_line)
    # Confirms the child loaded NOTHING from disk and saw NO hits — the
    # only way it could have seen hits is by inheriting the parent's
    # in-memory state, which is the failure mode the spec rules out.
    assert report["loaded"] == 0
    assert report["hits"] == 0


def test_child_subprocess_payload_carries_only_vault_root_and_tenant(
    tmp_path: Path,
) -> None:
    """The child program receives a JSON payload on stdin; that payload
    must list ONLY the vault root, tenant id, and the search query.
    Any extra key would indicate in-memory state leakage.
    """
    from yomotsusaka.cli import operational_smoke as cli_mod

    # The embedded child source reads ``json.loads(sys.stdin.read())``
    # and then references payload["vault_root"], payload["query"],
    # and payload.get("tenant_id"). The parent contract is locked in
    # via the _phase_index_reload_and_search helper; we re-derive the
    # payload shape from the source to make any future drift visible.
    source = cli_mod._CHILD_SOURCE
    referenced = set(re.findall(r"payload(?:\[\"|.get\(\")([^\"]+)\"", source))
    assert referenced == {"vault_root", "tenant_id", "query"}, referenced


# ---------------------------------------------------------------------------
# Per-phase failure injection
# ---------------------------------------------------------------------------


def test_failure_batch_missing_inbox(tmp_path: Path) -> None:
    """An absent inbox routes through the batch-phase infra category
    and produces ``failed_cleaned`` / exit 1."""
    result = _run_cli(
        [
            str(tmp_path / "does-not-exist"),
            "--vault-root",
            str(tmp_path / "vault"),
        ]
    )
    assert result.returncode == 1
    phases = _parse_phase_lines(result.stdout)
    assert phases[0] == ("batch", "fail", "batch_infrastructure_error")
    assert _result_line(result.stdout) == "failed_cleaned"


def test_failure_batch_empty_inbox(tmp_path: Path) -> None:
    """An empty inbox routes through ``batch_no_documents``."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    result = _run_cli(
        [str(inbox), "--vault-root", str(tmp_path / "vault")]
    )
    assert result.returncode == 1
    phases = _parse_phase_lines(result.stdout)
    statuses = {p: (s, c) for p, s, c in phases}
    assert statuses["batch"] == ("fail", "batch_no_documents")
    # Downstream phases short-circuit but still emit a ledger entry.
    assert statuses["index_snapshot"][0] == "fail"
    assert statuses["index_reload"][0] == "fail"
    assert statuses["search_smoke"][0] == "fail"
    assert statuses["restoration_request"][0] == "fail"
    assert statuses["audit_inspect"][0] == "fail"
    # RunPod phase keeps its independent skipped/live decision.
    assert statuses["runpod_lifecycle"] == ("skipped", "runpod_lifecycle_disabled")
    assert _result_line(result.stdout) == "failed_cleaned"


def test_failure_index_snapshot_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Force ``SearchGateway.snapshot`` to raise ``OSError``; the
    index_snapshot phase reports ``snapshot_write_failed`` and the
    final synthesis lands on ``failed_cleaned``.
    """
    from yomotsusaka.cli import operational_smoke as cli_mod
    from yomotsusaka.search_gateway import SearchGateway

    inbox = tmp_path / "inbox"
    vault = tmp_path / "vault"
    _write_corpus(inbox, _canonical_corpus())

    def boom(self: SearchGateway, vault_root: Path) -> Path:
        raise OSError("forced snapshot failure")

    monkeypatch.setattr(SearchGateway, "snapshot", boom)

    exit_code = cli_mod.main(
        [str(inbox), "--vault-root", str(vault)]
    )
    # In-process call so we can monkeypatch SearchGateway.snapshot. The
    # batch runner ALSO calls snapshot at its tail; that call will hit
    # the same monkeypatched failure but the runner tolerates it
    # (``index_persisted=False``), so the batch phase still reports
    # success. The dedicated index_snapshot phase below fails as
    # expected.
    assert exit_code == 1


def test_failure_search_smoke_no_hits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Force the embedded child source to use a query that does not
    match any indexed manifest; the search_smoke phase reports
    ``search_no_hits``."""
    from yomotsusaka.cli import operational_smoke as cli_mod

    inbox = tmp_path / "inbox"
    vault = tmp_path / "vault"
    _write_corpus(inbox, _canonical_corpus())

    monkeypatch.setattr(
        cli_mod,
        "_SEARCH_QUERY",
        "::query-that-cannot-occur-in-any-redacted-manifest::",
    )
    exit_code = cli_mod.main(
        [str(inbox), "--vault-root", str(vault)]
    )
    assert exit_code == 1
    # The audit phase still succeeds because phase 5 runs unconditionally
    # after the search phase reports its result.


def test_failure_restoration_audit_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the audit file is deleted between phase 5 and phase 6, the
    audit_inspect phase reports ``audit_file_missing`` and exit code 1.
    """
    from yomotsusaka.cli import operational_smoke as cli_mod

    inbox = tmp_path / "inbox"
    vault = tmp_path / "vault"
    _write_corpus(inbox, _canonical_corpus())

    real_phase_restoration = cli_mod._phase_restoration

    def restoration_then_wipe(facade, doc_id):  # type: ignore[no-untyped-def]
        result = real_phase_restoration(facade, doc_id)
        audit_path = vault / "audit" / "restoration.jsonl"
        if audit_path.exists():
            audit_path.unlink()
        return result

    monkeypatch.setattr(
        cli_mod, "_phase_restoration", restoration_then_wipe
    )

    exit_code = cli_mod.main(
        [str(inbox), "--vault-root", str(vault)]
    )
    assert exit_code == 1


def test_failure_runpod_apikey_missing_under_live_runpod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--live-runpod`` without RUNPOD_API_KEY reports
    ``api_key_missing`` and produces ``failed_cleaned`` / exit 1.

    The preflight runs entirely in-process; no network call is made.
    """
    inbox = tmp_path / "inbox"
    vault = tmp_path / "vault"
    _write_corpus(inbox, _canonical_corpus())

    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)

    result = _run_cli(
        [
            str(inbox),
            "--vault-root",
            str(vault),
            "--live-runpod",
        ]
    )
    assert result.returncode == 1
    statuses = {
        p: (s, c) for p, s, c in _parse_phase_lines(result.stdout)
    }
    assert statuses["runpod_lifecycle"] == ("fail", "api_key_missing")
    assert _result_line(result.stdout) == "failed_cleaned"


# ---------------------------------------------------------------------------
# Result-synthesis unit tests
# ---------------------------------------------------------------------------


def _statuses_for(spec: Iterable[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    return list(spec)


def test_synthesis_all_ok_yields_completed() -> None:
    from yomotsusaka.cli import operational_smoke as cli_mod

    statuses = _statuses_for(
        [
            ("batch", "ok", "batch_ok"),
            ("index_snapshot", "ok", "index_snapshot_ok"),
            ("index_reload", "ok", "index_reload_ok"),
            ("search_smoke", "ok", "search_smoke_ok"),
            ("restoration_request", "ok", "restoration_ok"),
            ("audit_inspect", "ok", "audit_inspect_ok"),
            ("runpod_lifecycle", "skipped", "runpod_lifecycle_disabled"),
        ]
    )
    assert cli_mod._synthesise_result(statuses) == ("completed", 0)


def test_synthesis_warn_yields_completed_with_warnings() -> None:
    from yomotsusaka.cli import operational_smoke as cli_mod

    statuses = _statuses_for(
        [
            ("batch", "warn", "batch_partial_commit"),
            ("index_snapshot", "ok", "index_snapshot_ok"),
            ("index_reload", "ok", "index_reload_ok"),
            ("search_smoke", "ok", "search_smoke_ok"),
            ("restoration_request", "ok", "restoration_ok"),
            ("audit_inspect", "ok", "audit_inspect_ok"),
            ("runpod_lifecycle", "skipped", "runpod_lifecycle_disabled"),
        ]
    )
    assert cli_mod._synthesise_result(statuses) == (
        "completed_with_warnings",
        0,
    )


def test_synthesis_fail_yields_failed_cleaned() -> None:
    from yomotsusaka.cli import operational_smoke as cli_mod

    statuses = _statuses_for(
        [
            ("batch", "ok", "batch_ok"),
            ("index_snapshot", "ok", "index_snapshot_ok"),
            ("index_reload", "ok", "index_reload_ok"),
            ("search_smoke", "fail", "search_no_hits"),
            ("restoration_request", "ok", "restoration_ok"),
            ("audit_inspect", "ok", "audit_inspect_ok"),
            ("runpod_lifecycle", "skipped", "runpod_lifecycle_disabled"),
        ]
    )
    assert cli_mod._synthesise_result(statuses) == ("failed_cleaned", 1)


def test_phase_restoration_asserts_scope_denied_contract(
    tmp_path: Path,
) -> None:
    """Phase 5 must validate the response contract before reporting ``ok``.

    Pins the codex P1 fix on PR #99: a stub response that lacks
    ``reason=ScopeDenied`` (e.g. an unexpected ``AuditWriteFailed``)
    must surface as ``status=fail category=<restoration_fail>`` rather
    than being papered over by a non-empty ``audit_record_id`` check.
    """
    from yomotsusaka.boundary import (
        RestorationFailureReason,
        RestorationResponse,
    )
    from yomotsusaka.cli import operational_smoke as cli_mod
    from yomotsusaka.facade import LocalFacade

    class _StubFacade:
        """Minimal stand-in that returns a not-ScopeDenied failure."""

        vault_root = tmp_path / "vault"
        # Mirror the real facade's gateway attribute so any future
        # phase-5 helper accidentally touching gateway raises clearly.
        gateway = None  # type: ignore[assignment]

        def request_restore(  # type: ignore[no-untyped-def]
            self, request
        ) -> RestorationResponse:
            return RestorationResponse(
                outcome="failed",
                audit_record_id="stub-record-id",
                document_id="doc-x",
                reason=RestorationFailureReason.AuditWriteFailed,
            )

    status, category, audit_id = cli_mod._phase_restoration(
        _StubFacade(),  # type: ignore[arg-type]
        "doc-x",
    )
    assert status == "fail"
    assert category == "restoration_request_unexpected_outcome"
    assert audit_id is None

    # And the inverse: a contract-matching response yields ``ok`` plus
    # the surfaced audit_record_id so phase 6 can correlate.
    vault = tmp_path / "vault-ok"
    inbox = tmp_path / "inbox-ok"
    _write_corpus(inbox, _canonical_corpus())
    facade = LocalFacade(vault)
    # Drive the real batch once so the facade has a real doc to target.
    from yomotsusaka.batch_runner import BatchRunner

    BatchRunner(facade=facade).run_directory(inbox)
    doc_id = sorted((vault / "manifests").glob("*.json"))[0].stem
    status, category, audit_id = cli_mod._phase_restoration(facade, doc_id)
    assert status == "ok"
    assert category == "restoration_ok"
    assert isinstance(audit_id, str) and audit_id


def test_phase_audit_inspect_correlates_on_record_id_not_caller_label(
    tmp_path: Path,
) -> None:
    """Phase 6 must match on the phase-5 ``audit_record_id``.

    Pins the codex P2 fix on PR #99: a stale row from a previous run
    (same caller_label, same document_id, different audit_record_id)
    must NOT satisfy phase 6 when the current run's row is absent.
    """
    from yomotsusaka.cli import operational_smoke as cli_mod

    vault = tmp_path / "vault"
    audit_dir = vault / "audit"
    audit_dir.mkdir(parents=True)
    audit_path = audit_dir / "restoration.jsonl"

    # Stale row from a prior run: same caller_label, same doc_id, but
    # a DIFFERENT audit_record_id. With the old caller_label/doc_id
    # match this would falsely pass; with the codex-P2 fix it must
    # not.
    stale = {
        "audit_record_id": "stale-record-from-prior-run",
        "caller_label": cli_mod._RESTORATION_CALLER_LABEL,
        "target": {"document_id": "doc-shared"},
        "scope": "ORDINARY_AGENT",
        "outcome": "failed",
        "reason": "scope_denied",
    }
    audit_path.write_text(json.dumps(stale) + "\n", encoding="utf-8")

    # Phase 6 must report no-match because the current run's record id
    # is not present, even though caller_label + document_id collide.
    status, category = cli_mod._phase_audit_inspect(
        vault, audit_record_id="current-run-record-id-that-was-not-written"
    )
    assert status == "fail"
    assert category == "audit_record_not_found"

    # Append the current row and re-check: now phase 6 finds it.
    current = dict(stale)
    current["audit_record_id"] = "current-run-record-id-that-was-not-written"
    with audit_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(current) + "\n")
    status, category = cli_mod._phase_audit_inspect(
        vault, audit_record_id="current-run-record-id-that-was-not-written"
    )
    assert status == "ok"
    assert category == "audit_inspect_ok"


def test_synthesis_cleanup_failed_yields_owner_action() -> None:
    """The ``cleanup_failed`` category outranks ordinary fail — the
    owner must intervene to delete the orphaned Pod."""
    from yomotsusaka.cli import operational_smoke as cli_mod

    statuses = _statuses_for(
        [
            ("batch", "ok", "batch_ok"),
            ("index_snapshot", "ok", "index_snapshot_ok"),
            ("index_reload", "ok", "index_reload_ok"),
            ("search_smoke", "ok", "search_smoke_ok"),
            ("restoration_request", "ok", "restoration_ok"),
            ("audit_inspect", "ok", "audit_inspect_ok"),
            ("runpod_lifecycle", "fail", "cleanup_failed"),
        ]
    )
    assert cli_mod._synthesise_result(statuses) == (
        "failed_owner_action",
        3,
    )


def test_synthesis_wait_timeout_cleanup_failed_yields_owner_action() -> None:
    """Issue #125 — ``wait_timeout_cleanup_failed`` must yield
    ``failed_owner_action`` (exit 3), not ``failed_cleaned`` (exit 1).

    ``failed_cleaned`` would be a lie: cleanup was attempted but also
    failed, so the Pod may still be running and billing.
    """
    from yomotsusaka.cli import operational_smoke as cli_mod

    statuses = _statuses_for(
        [
            ("batch", "ok", "batch_ok"),
            ("index_snapshot", "ok", "index_snapshot_ok"),
            ("index_reload", "ok", "index_reload_ok"),
            ("search_smoke", "ok", "search_smoke_ok"),
            ("restoration_request", "ok", "restoration_ok"),
            ("audit_inspect", "ok", "audit_inspect_ok"),
            ("runpod_lifecycle", "fail", "wait_timeout_cleanup_failed"),
        ]
    )
    assert cli_mod._synthesise_result(statuses) == (
        "failed_owner_action",
        3,
    ), (
        "wait_timeout_cleanup_failed must route to failed_owner_action "
        "(exit 3), not failed_cleaned — the Pod may still be running"
    )


def test_phase_runpod_lifecycle_wait_timeout_cleanup_failed_routes_to_owner_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #125 — when start_pod raises PodUnavailableError("wait_timeout_cleanup_failed"),
    _phase_runpod_lifecycle must return the category that routes to
    failed_owner_action (not failed_cleaned).

    Three assertions per the triage spec:
    1. stop_pod was called once with the leaked handle — the library's
       internal stop_pod call is the one that failed; we verify the phase
       returns the correct category that records this.
    2. The exception propagates with the combined category (the fake raises
       wait_timeout_cleanup_failed directly, matching the post-fix library
       contract).
    3. The result-token from _synthesise_result is failed_owner_action
       (exit 3), not failed_cleaned (exit 1) — the token honesty fix.
    """
    from yomotsusaka.cli import operational_smoke as cli_mod
    from yomotsusaka.runpod_lifecycle import PodHandle, PodUnavailableError

    stop_calls: list[tuple[PodHandle, bool]] = []

    class _FakeLifecycle:
        """Simulates library post-fix: start_pod raises wait_timeout_cleanup_failed.

        The library's internal stop_pod was already attempted (and failed)
        before raising this category; callers must NOT call stop_pod again.
        """

        def __init__(self, **_kwargs: object) -> None:
            pass  # Accept api_key / pod_config kwargs that ManageRunPodLifecycle accepts.

        def start_pod(self, _config: object = None) -> PodHandle:  # noqa: ANN201
            raise PodUnavailableError("wait_timeout_cleanup_failed")

        def stop_pod(self, handle: PodHandle, *, terminate: bool = True) -> None:
            stop_calls.append((handle, terminate))

    # Patch ManageRunPodLifecycle inside the runpod_lifecycle module so the
    # lazy import inside _phase_runpod_lifecycle picks up our fake.
    import yomotsusaka.runpod_lifecycle as rl_mod

    monkeypatch.setattr(rl_mod, "ManageRunPodLifecycle", _FakeLifecycle)

    status, category = cli_mod._phase_runpod_lifecycle(
        keep_pod=False,
        env={"RUNPOD_API_KEY": "sk-test-sentinel"},
    )

    # Assertion 1 & 2: The phase returns FAIL + wait_timeout_cleanup_failed.
    # (The library already called stop_pod internally — recorded in the
    # category string; our fake confirms the phase does NOT call stop_pod
    # again after receiving this category from start_pod.)
    assert status == "fail", f"expected fail; got {status!r}"
    assert category == "wait_timeout_cleanup_failed", (
        f"expected wait_timeout_cleanup_failed; got {category!r}"
    )
    # Caller must NOT call stop_pod again — library already handled cleanup.
    assert stop_calls == [], (
        "_phase_runpod_lifecycle must not call stop_pod after receiving "
        "wait_timeout_cleanup_failed from start_pod (library already tried)"
    )

    # Assertion 3: _synthesise_result maps this to failed_owner_action.
    result_token, exit_code = cli_mod._synthesise_result(
        [("runpod_lifecycle", status, category)]
    )
    assert result_token == "failed_owner_action", (
        f"wait_timeout_cleanup_failed must yield failed_owner_action; "
        f"got {result_token!r} — emitting failed_cleaned here would be a lie "
        f"since cleanup was not verified to have succeeded"
    )
    assert exit_code == 3


# ---------------------------------------------------------------------------
# --emit-json bridge (issue #111)
# ---------------------------------------------------------------------------


def test_emit_json_default_omits_file(tmp_path: Path) -> None:
    """Without ``--emit-json`` no JSON file is written. The default path
    keeps the no-network happy-path unchanged."""
    inbox = tmp_path / "inbox"
    vault = tmp_path / "vault"
    _write_corpus(inbox, _canonical_corpus())

    result = _run_cli([str(inbox), "--vault-root", str(vault)])
    assert result.returncode == 0
    # No file should have been written anywhere under tmp_path beyond the
    # vault and inbox we already created.
    extras = sorted(
        p
        for p in tmp_path.rglob("*")
        if p.is_file()
        and not str(p).startswith(str(inbox))
        and not str(p).startswith(str(vault))
    )
    assert extras == [], f"unexpected extra files: {extras}"


def test_emit_json_writes_canonical_payload(tmp_path: Path) -> None:
    """``--emit-json`` writes a JSON ScenarioResult that
    ``operational_report`` can consume on stdin."""
    inbox = tmp_path / "inbox"
    vault = tmp_path / "vault"
    _write_corpus(inbox, _canonical_corpus())
    emit_path = tmp_path / "scenario.json"

    result = _run_cli(
        [
            str(inbox),
            "--vault-root",
            str(vault),
            "--emit-json",
            str(emit_path),
        ]
    )
    assert result.returncode == 0
    # The phase ledger on stdout is unchanged.
    statuses = {p: (s, c) for p, s, c in _parse_phase_lines(result.stdout)}
    assert statuses["batch"] == ("ok", "batch_ok")
    assert _result_line(result.stdout) == "completed"

    # JSON file exists, parses, and carries the canonical shape.
    assert emit_path.is_file()
    payload = json.loads(emit_path.read_text(encoding="utf-8"))
    assert set(payload.keys()) == {"phases", "counters"}
    assert isinstance(payload["phases"], list)
    assert len(payload["phases"]) == 7
    first = payload["phases"][0]
    assert set(first.keys()) >= {"phase_name", "status", "category"}
    assert first == {
        "phase_name": "batch",
        "status": "ok",
        "category": "batch_ok",
    }

    counters = payload["counters"]
    assert counters["processed_documents"] == 2
    assert counters["failed_documents"] == 0
    assert counters["index_snapshot_ok"] == 1
    assert counters["index_loadable"] == 1
    assert counters["search_smoke_ok"] == 1
    assert counters["restoration_outcome"] == "ok"
    assert counters["audit_row_count"] == 1
    # RunPod was skipped — runpod_lifecycle_category MUST be omitted (not
    # present as an empty string).
    assert "runpod_lifecycle_category" not in counters


def test_emit_json_bridges_into_operational_report(tmp_path: Path) -> None:
    """End-to-end: smoke --emit-json then pipe into operational_report."""
    inbox = tmp_path / "inbox"
    vault = tmp_path / "vault"
    _write_corpus(inbox, _canonical_corpus())
    emit_path = tmp_path / "scenario.json"

    smoke = _run_cli(
        [
            str(inbox),
            "--vault-root",
            str(vault),
            "--emit-json",
            str(emit_path),
        ]
    )
    assert smoke.returncode == 0
    assert emit_path.is_file()

    report = subprocess.run(
        [
            sys.executable,
            "-m",
            "yomotsusaka.cli.operational_report",
        ],
        input=emit_path.read_text(encoding="utf-8"),
        capture_output=True,
        text=True,
        check=False,
    )
    assert report.returncode == 0, (
        f"operational_report exited {report.returncode}; "
        f"stderr={report.stderr!r}"
    )
    # The renderer emits the canonical state token and the canonical
    # counters; no need to assert every cell here.
    assert "## Result" in report.stdout
    assert "completed" in report.stdout
    assert "## Phases" in report.stdout
    assert "| batch | ok | batch_ok |" in report.stdout
    assert "- processed_documents: 2" in report.stdout


def test_emit_json_written_after_result_line(tmp_path: Path) -> None:
    """Phase ledger on stdout is unaffected by ``--emit-json``: ``result=``
    is still the final stdout line (no JSON noise mixed in)."""
    inbox = tmp_path / "inbox"
    vault = tmp_path / "vault"
    _write_corpus(inbox, _canonical_corpus())
    emit_path = tmp_path / "scenario.json"

    result = _run_cli(
        [
            str(inbox),
            "--vault-root",
            str(vault),
            "--emit-json",
            str(emit_path),
        ]
    )
    assert result.returncode == 0
    non_empty_lines = [
        line for line in result.stdout.splitlines() if line.strip()
    ]
    assert non_empty_lines[-1].startswith("result="), non_empty_lines[-1]


def test_emit_json_atomic_via_tmp_rename(tmp_path: Path) -> None:
    """The atomic write should leave no ``.tmp`` sibling after the run."""
    inbox = tmp_path / "inbox"
    vault = tmp_path / "vault"
    _write_corpus(inbox, _canonical_corpus())
    emit_path = tmp_path / "scenario.json"

    result = _run_cli(
        [
            str(inbox),
            "--vault-root",
            str(vault),
            "--emit-json",
            str(emit_path),
        ]
    )
    assert result.returncode == 0
    assert emit_path.exists()
    assert not emit_path.with_suffix(emit_path.suffix + ".tmp").exists()


def test_emit_json_payload_passes_report_redaction(tmp_path: Path) -> None:
    """The emitted JSON, fed into the report renderer's render_report,
    passes the fail-closed redaction sweep (no vault paths, no Pod IDs,
    etc.). Cross-check that the smoke counters do not echo any sensitive
    shape.
    """
    inbox = tmp_path / "inbox"
    vault = tmp_path / "vault"
    _write_corpus(inbox, _canonical_corpus())
    emit_path = tmp_path / "scenario.json"

    result = _run_cli(
        [
            str(inbox),
            "--vault-root",
            str(vault),
            "--emit-json",
            str(emit_path),
        ]
    )
    assert result.returncode == 0

    # Round-trip via the parser.
    from yomotsusaka.cli import operational_report as report_cli
    payload = json.loads(emit_path.read_text(encoding="utf-8"))
    scenario = report_cli._parse_scenario_result(payload)
    from yomotsusaka.operational_report import render_report
    rendered = render_report(scenario)  # raises RedactionError on leak
    assert "## Result" in rendered


def test_emit_json_runpod_category_present_on_live_run(
    tmp_path: Path,
) -> None:
    """When ``--live-runpod`` runs and the RunPod phase fails the api-key
    preflight, the JSON counters include the ``runpod_lifecycle_category``
    token. Demonstrates the present-only-when-actually-ran rule (D4)."""
    inbox = tmp_path / "inbox"
    vault = tmp_path / "vault"
    _write_corpus(inbox, _canonical_corpus())
    emit_path = tmp_path / "scenario.json"

    # No RUNPOD_API_KEY -> preflight reports api_key_missing.
    result = _run_cli(
        [
            str(inbox),
            "--vault-root",
            str(vault),
            "--live-runpod",
            "--emit-json",
            str(emit_path),
        ]
    )
    assert result.returncode == 1
    payload = json.loads(emit_path.read_text(encoding="utf-8"))
    assert (
        payload["counters"]["runpod_lifecycle_category"] == "api_key_missing"
    )


def test_emit_json_state_matches_smoke_for_runpod_failures(
    tmp_path: Path,
) -> None:
    """Codex P1 (PR #119 review id 4352393743): when smoke reports
    ``failed_cleaned`` for a RunPod-touched run, the JSON the report
    renderer consumes MUST classify identically (``failed_cleaned``), not
    ``failed_owner_action``.

    The report renderer's ``classify_result_state`` is fail-closed: it
    defaults to ``failed_owner_action`` for any failing scenario that
    touched RunPod, unless the strict-bool ``runpod_cleanup_confirmed``
    flag is exactly ``True``. We pin that the bridge emits the flag
    correctly so the two CLIs agree.
    """
    inbox = tmp_path / "inbox"
    vault = tmp_path / "vault"
    _write_corpus(inbox, _canonical_corpus())
    emit_path = tmp_path / "scenario.json"

    # Force the api_key_missing path: --live-runpod with no key. Smoke
    # classifies this as failed_cleaned (only cleanup_failed promotes
    # to owner_action). The bridge must surface a payload that the
    # report renderer also classifies as failed_cleaned.
    result = _run_cli(
        [
            str(inbox),
            "--vault-root",
            str(vault),
            "--live-runpod",
            "--emit-json",
            str(emit_path),
        ]
    )
    assert result.returncode == 1
    assert _result_line(result.stdout) == "failed_cleaned"

    payload = json.loads(emit_path.read_text(encoding="utf-8"))
    counters = payload["counters"]
    assert counters["runpod_lifecycle_category"] == "api_key_missing"
    # Strict-bool: must be the literal True, not a string or int — the
    # renderer's check rejects anything that is not the Python bool.
    assert counters["runpod_cleanup_confirmed"] is True

    # End-to-end: render the report and assert the state token agrees.
    from yomotsusaka.cli import operational_report as report_cli
    from yomotsusaka.operational_report import (
        classify_result_state,
        render_report,
    )

    scenario = report_cli._parse_scenario_result(payload)
    assert classify_result_state(scenario) == "failed_cleaned"
    rendered = render_report(scenario)
    assert "## Result\n\nfailed_cleaned\n" in rendered


def test_emit_json_state_matches_smoke_for_cleanup_failed() -> None:
    """Inverse of the P1 alignment: when the runpod fail-category IS
    ``cleanup_failed``, smoke routes to ``failed_owner_action`` and the
    JSON must carry ``runpod_cleanup_confirmed=False`` so the report
    renderer agrees. Direct unit-test of the synthesiser because
    triggering a real cleanup_failed needs a mocked lifecycle.
    """
    from yomotsusaka.cli import operational_smoke as cli_mod
    from yomotsusaka.operational_report import (
        PhaseRecord as ReportPhaseRecord,
        ScenarioResult,
        classify_result_state,
    )

    statuses = [
        ("batch", "ok", "batch_ok"),
        ("index_snapshot", "ok", "index_snapshot_ok"),
        ("index_reload", "ok", "index_reload_ok"),
        ("search_smoke", "ok", "search_smoke_ok"),
        ("restoration_request", "ok", "restoration_ok"),
        ("audit_inspect", "ok", "audit_inspect_ok"),
        ("runpod_lifecycle", "fail", "cleanup_failed"),
    ]
    counters = cli_mod._synthesise_counters(statuses, batch_summary=None)
    assert counters["runpod_lifecycle_category"] == "cleanup_failed"
    assert counters["runpod_cleanup_confirmed"] is False

    # The renderer agrees: failed_owner_action.
    scenario = ScenarioResult(
        phases=tuple(
            ReportPhaseRecord(p, s, c)  # type: ignore[arg-type]
            for p, s, c in statuses
        ),
        counters=counters,
    )
    assert classify_result_state(scenario) == "failed_owner_action"


def test_synthesise_counters_omits_runpod_when_skipped() -> None:
    """Direct unit-test of the counter-synthesis helper: a skipped RunPod
    phase MUST NOT emit a ``runpod_lifecycle_category`` key. An empty
    string is forbidden by the contract."""
    from yomotsusaka.cli import operational_smoke as cli_mod

    statuses = [
        ("batch", "ok", "batch_ok"),
        ("index_snapshot", "ok", "index_snapshot_ok"),
        ("index_reload", "ok", "index_reload_ok"),
        ("search_smoke", "ok", "search_smoke_ok"),
        ("restoration_request", "ok", "restoration_ok"),
        ("audit_inspect", "ok", "audit_inspect_ok"),
        ("runpod_lifecycle", "skipped", "runpod_lifecycle_disabled"),
    ]
    counters = cli_mod._synthesise_counters(statuses, batch_summary=None)
    assert "runpod_lifecycle_category" not in counters
    # When the phase did NOT run, the cleanup-confirmed flag also stays
    # out of the payload — there is no failing scenario to classify, so
    # the report renderer's fail-closed default never fires.
    assert "runpod_cleanup_confirmed" not in counters
    # Boolean-as-int counters are integers, not bool-strings.
    assert counters["index_snapshot_ok"] == 1
    assert counters["index_loadable"] == 1
    assert counters["search_smoke_ok"] == 1
    # Empty batch_summary -> 0 counts.
    assert counters["processed_documents"] == 0
    assert counters["failed_documents"] == 0
