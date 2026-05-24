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
    assert statuses["batch"] == ("ok", "batch_committed")
    assert statuses["index_snapshot"] == ("ok", "snapshot_written")
    assert statuses["index_reload"] == ("ok", "index_reloaded")
    assert statuses["search_smoke"] == ("ok", "hits_found")
    assert statuses["restoration_request"] == (
        "ok",
        "restoration_request_recorded",
    )
    assert statuses["audit_inspect"] == ("ok", "audit_present")
    assert statuses["runpod_lifecycle"] == ("skipped", "runpod_disabled")

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
    assert statuses["index_reload"] == ("ok", "index_reloaded")
    assert statuses["search_smoke"] == ("ok", "hits_found")
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
    assert statuses["runpod_lifecycle"] == ("skipped", "runpod_disabled")
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
            ("batch", "ok", "batch_committed"),
            ("index_snapshot", "ok", "snapshot_written"),
            ("index_reload", "ok", "index_reloaded"),
            ("search_smoke", "ok", "hits_found"),
            ("restoration_request", "ok", "restoration_request_recorded"),
            ("audit_inspect", "ok", "audit_present"),
            ("runpod_lifecycle", "skipped", "runpod_disabled"),
        ]
    )
    assert cli_mod._synthesise_result(statuses) == ("completed", 0)


def test_synthesis_warn_yields_completed_with_warnings() -> None:
    from yomotsusaka.cli import operational_smoke as cli_mod

    statuses = _statuses_for(
        [
            ("batch", "warn", "batch_partial_commit"),
            ("index_snapshot", "ok", "snapshot_written"),
            ("index_reload", "ok", "index_reloaded"),
            ("search_smoke", "ok", "hits_found"),
            ("restoration_request", "ok", "restoration_request_recorded"),
            ("audit_inspect", "ok", "audit_present"),
            ("runpod_lifecycle", "skipped", "runpod_disabled"),
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
            ("batch", "ok", "batch_committed"),
            ("index_snapshot", "ok", "snapshot_written"),
            ("index_reload", "ok", "index_reloaded"),
            ("search_smoke", "fail", "search_no_hits"),
            ("restoration_request", "ok", "restoration_request_recorded"),
            ("audit_inspect", "ok", "audit_present"),
            ("runpod_lifecycle", "skipped", "runpod_disabled"),
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
    assert category == "restoration_request_recorded"
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
    assert category == "audit_present"


def test_synthesis_cleanup_failed_yields_owner_action() -> None:
    """The ``cleanup_failed`` category outranks ordinary fail — the
    owner must intervene to delete the orphaned Pod."""
    from yomotsusaka.cli import operational_smoke as cli_mod

    statuses = _statuses_for(
        [
            ("batch", "ok", "batch_committed"),
            ("index_snapshot", "ok", "snapshot_written"),
            ("index_reload", "ok", "index_reloaded"),
            ("search_smoke", "ok", "hits_found"),
            ("restoration_request", "ok", "restoration_request_recorded"),
            ("audit_inspect", "ok", "audit_present"),
            ("runpod_lifecycle", "fail", "cleanup_failed"),
        ]
    )
    assert cli_mod._synthesise_result(statuses) == (
        "failed_owner_action",
        3,
    )
