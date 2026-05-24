"""
Tests for :mod:`yomotsusaka.operational_report` (MVP-5 child 03, issue #92).

Coverage targets (per issue spec):

1. State classification — render each of the four states
   (``completed`` / ``completed_with_warnings`` / ``failed_cleaned`` /
   ``failed_owner_action``) and snapshot-style assertions on the markdown
   shape.
2. Public-safe redaction smoke — construct a ``ScenarioResult`` whose
   phase categories and counter values include sensitive-looking
   sentinels (vault paths, RunPod Pod IDs, https URLs, bearer tokens,
   long hex strings) and assert that ``render_report`` raises
   :class:`RedactionError` rather than emitting them.
3. CLI smoke — JSON-in, markdown-out happy path via the
   ``yomotsusaka.cli.operational_report`` module.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout

import pytest

from yomotsusaka.cli import operational_report as cli_module
from yomotsusaka.operational_report import (
    PhaseRecord,
    RedactionError,
    ScenarioResult,
    classify_result_state,
    render_report,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _completed_phases() -> tuple[PhaseRecord, ...]:
    return (
        PhaseRecord("batch", "ok", "batch_ok"),
        PhaseRecord("index_snapshot", "ok", "snapshot_ok"),
        PhaseRecord("index_reload", "ok", "reload_ok"),
        PhaseRecord("search_smoke", "ok", "search_ok"),
        PhaseRecord("restoration_request", "ok", "restoration_ok"),
        PhaseRecord("audit_inspect", "ok", "audit_ok"),
    )


def _baseline_counters() -> dict[str, object]:
    return {
        "processed_documents": 3,
        "failed_documents": 0,
        "index_snapshot_ok": True,
        "index_loadable": True,
        "search_smoke_ok": True,
        "restoration_outcome": "ok",
        "audit_row_count": 6,
    }


# ---------------------------------------------------------------------------
# State classification
# ---------------------------------------------------------------------------


class TestClassifyResultState:
    def test_completed_when_all_ok(self) -> None:
        result = ScenarioResult(phases=_completed_phases(), counters=_baseline_counters())
        assert classify_result_state(result) == "completed"

    def test_completed_with_warnings_when_any_warn_no_fail(self) -> None:
        phases = (
            PhaseRecord("batch", "ok", "batch_ok"),
            PhaseRecord("index_snapshot", "warn", "snapshot_partial"),
            PhaseRecord("audit_inspect", "ok", "audit_ok"),
        )
        result = ScenarioResult(phases=phases, counters=_baseline_counters())
        assert classify_result_state(result) == "completed_with_warnings"

    def test_failed_cleaned_when_runpod_untouched(self) -> None:
        phases = (
            PhaseRecord("batch", "fail", "vault_unwritable"),
            PhaseRecord("index_snapshot", "skipped", ""),
        )
        # No runpod_lifecycle_category counter -> RunPod was never touched.
        counters = {
            "processed_documents": 0,
            "failed_documents": 1,
            "index_snapshot_ok": False,
            "index_loadable": False,
            "search_smoke_ok": False,
            "restoration_outcome": "not_attempted",
            "audit_row_count": 1,
        }
        result = ScenarioResult(phases=phases, counters=counters)
        assert classify_result_state(result) == "failed_cleaned"

    def test_failed_cleaned_when_runpod_cleanup_confirmed(self) -> None:
        phases = (
            PhaseRecord("batch", "ok", "batch_ok"),
            PhaseRecord("runpod_lifecycle", "fail", "wait_timeout"),
        )
        counters = {
            **_baseline_counters(),
            "runpod_lifecycle_category": "wait_timeout",
            "runpod_cleanup_confirmed": True,
        }
        result = ScenarioResult(phases=phases, counters=counters)
        assert classify_result_state(result) == "failed_cleaned"

    def test_failed_owner_action_when_runpod_cleanup_not_confirmed(self) -> None:
        phases = (
            PhaseRecord("batch", "ok", "batch_ok"),
            PhaseRecord("runpod_lifecycle", "fail", "delete_failed"),
        )
        counters = {
            **_baseline_counters(),
            "runpod_lifecycle_category": "delete_failed",
            "runpod_cleanup_confirmed": False,
        }
        result = ScenarioResult(phases=phases, counters=counters)
        assert classify_result_state(result) == "failed_owner_action"

    def test_failed_owner_action_when_runpod_cleanup_field_absent(self) -> None:
        # Fail-closed default: failing scenario that touched RunPod but
        # didn't record cleanup state -> owner action.
        phases = (
            PhaseRecord("runpod_lifecycle", "fail", "delete_failed"),
        )
        counters = {
            **_baseline_counters(),
            "runpod_lifecycle_category": "delete_failed",
        }
        result = ScenarioResult(phases=phases, counters=counters)
        assert classify_result_state(result) == "failed_owner_action"

    @pytest.mark.parametrize(
        "truthy_non_bool",
        [
            "true",
            "false",
            "yes",
            "no",
            "1",
            "0",
            1,
            0,
            "ok",
            [True],
        ],
    )
    def test_failed_owner_action_when_cleanup_flag_is_non_bool(
        self, truthy_non_bool: object
    ) -> None:
        # Strict-bool check (PR #98 codex review id 4351827310): only the
        # literal Python ``True`` (decoded from JSON ``true``) counts as
        # confirmed cleanup. Any non-bool value — including the string
        # ``"true"``, integers ``0`` / ``1``, and other truthy-looking
        # shapes — must fall back to ``failed_owner_action``.
        phases = (
            PhaseRecord("runpod_lifecycle", "fail", "delete_failed"),
        )
        counters = {
            **_baseline_counters(),
            "runpod_lifecycle_category": "delete_failed",
            "runpod_cleanup_confirmed": truthy_non_bool,
        }
        result = ScenarioResult(phases=phases, counters=counters)
        assert classify_result_state(result) == "failed_owner_action"

    def test_skipped_phases_do_not_block_completed(self) -> None:
        phases = (
            PhaseRecord("batch", "ok", "batch_ok"),
            PhaseRecord("runpod_lifecycle", "skipped", ""),
        )
        result = ScenarioResult(phases=phases, counters=_baseline_counters())
        assert classify_result_state(result) == "completed"


# ---------------------------------------------------------------------------
# Markdown rendering — per-state shape assertions
# ---------------------------------------------------------------------------


class TestRenderReportShape:
    def test_completed_shape(self) -> None:
        result = ScenarioResult(phases=_completed_phases(), counters=_baseline_counters())
        out = render_report(result)
        assert "## Result\n\ncompleted\n" in out
        assert "## Phases" in out
        assert "| phase | status | category |" in out
        assert "| batch | ok | batch_ok |" in out
        assert "## Counters" in out
        assert "- processed_documents: 3" in out
        assert "- index_snapshot_ok: true" in out
        # No owner-action section on the happy path.
        assert "## Owner action required" not in out

    def test_completed_with_warnings_shape(self) -> None:
        phases = (
            PhaseRecord("batch", "ok", "batch_ok"),
            PhaseRecord("index_snapshot", "warn", "snapshot_partial"),
        )
        result = ScenarioResult(phases=phases, counters=_baseline_counters())
        out = render_report(result)
        assert "## Result\n\ncompleted_with_warnings\n" in out
        assert "| index_snapshot | warn | snapshot_partial |" in out
        assert "## Owner action required" not in out

    def test_failed_cleaned_shape(self) -> None:
        phases = (
            PhaseRecord("batch", "fail", "vault_unwritable"),
        )
        counters = {
            "processed_documents": 0,
            "failed_documents": 1,
            "index_snapshot_ok": False,
            "index_loadable": False,
            "search_smoke_ok": False,
            "restoration_outcome": "not_attempted",
            "audit_row_count": 1,
        }
        result = ScenarioResult(phases=phases, counters=counters)
        out = render_report(result)
        assert "## Result\n\nfailed_cleaned\n" in out
        assert "| batch | fail | vault_unwritable |" in out
        # Owner-action section is only for failed_owner_action.
        assert "## Owner action required" not in out

    def test_failed_owner_action_shape(self) -> None:
        phases = (
            PhaseRecord("runpod_lifecycle", "fail", "delete_failed"),
        )
        counters = {
            **_baseline_counters(),
            "runpod_lifecycle_category": "delete_failed",
            "runpod_cleanup_confirmed": False,
        }
        result = ScenarioResult(phases=phases, counters=counters)
        out = render_report(result)
        assert "## Result\n\nfailed_owner_action\n" in out
        assert "## Owner action required" in out
        # Owner-action section names the failing phase and category, but
        # nothing else. No vault path, no Pod id.
        assert "runpod_lifecycle" in out
        assert "delete_failed" in out
        # Internal classifier counter is NOT printed in the Counters list.
        assert "runpod_cleanup_confirmed" not in out

    def test_canonical_counter_order(self) -> None:
        # Build counters in non-canonical insertion order.
        counters: dict[str, object] = {
            "audit_row_count": 2,
            "processed_documents": 1,
            "failed_documents": 0,
            "index_snapshot_ok": True,
            "index_loadable": True,
            "search_smoke_ok": True,
            "restoration_outcome": "ok",
            "custom_extra": "extra_value",
        }
        result = ScenarioResult(phases=_completed_phases(), counters=counters)
        out = render_report(result)
        # processed_documents must appear before audit_row_count in the
        # rendered output despite the insertion order.
        proc_idx = out.index("- processed_documents:")
        audit_idx = out.index("- audit_row_count:")
        extra_idx = out.index("- custom_extra:")
        assert proc_idx < audit_idx < extra_idx


# ---------------------------------------------------------------------------
# Public-safe redaction smoke
# ---------------------------------------------------------------------------


SENSITIVE_TOKENS: tuple[tuple[str, str], ...] = (
    ("vault_path", "/manifests/doc_abcdef.json"),
    ("vault_private_path", "/private/doc_abcdef.json"),
    ("vault_audit_path", "/audit/restoration.jsonl"),
    ("https_endpoint", "https://api.runpod.io/v1/pods/runpod-abcdef123"),
    ("http_endpoint", "http://10.0.0.1:8000/v1/chat/completions"),
    ("pod_id", "runpod-deadbeefcafe"),
    ("bearer_token", "Bearer abcdef0123456789xyz"),
    (
        "long_hex_api_key",
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    ),
)


class TestRedactionSweep:
    @pytest.mark.parametrize("name,token", SENSITIVE_TOKENS)
    def test_sensitive_token_in_phase_category_is_rejected(
        self, name: str, token: str
    ) -> None:
        phases = (PhaseRecord("phase_x", "fail", token),)
        counters = dict(_baseline_counters())
        counters["failed_documents"] = 1
        result = ScenarioResult(phases=phases, counters=counters)
        with pytest.raises(RedactionError):
            render_report(result)

    @pytest.mark.parametrize("name,token", SENSITIVE_TOKENS)
    def test_sensitive_token_in_counter_value_is_rejected(
        self, name: str, token: str
    ) -> None:
        counters = dict(_baseline_counters())
        counters["restoration_outcome"] = token
        result = ScenarioResult(phases=_completed_phases(), counters=counters)
        with pytest.raises(RedactionError):
            render_report(result)

    @pytest.mark.parametrize("name,token", SENSITIVE_TOKENS)
    def test_sensitive_token_in_extra_counter_is_rejected(
        self, name: str, token: str
    ) -> None:
        counters = dict(_baseline_counters())
        counters["extra_field"] = token
        result = ScenarioResult(phases=_completed_phases(), counters=counters)
        with pytest.raises(RedactionError):
            render_report(result)

    def test_clean_report_has_no_sensitive_shape(self) -> None:
        # The happy-path report must NOT contain any of the sensitive
        # tokens we just probed. This is a positive assertion that
        # categories alone (without secrets in them) survive the sweep.
        result = ScenarioResult(phases=_completed_phases(), counters=_baseline_counters())
        out = render_report(result)
        for _name, token in SENSITIVE_TOKENS:
            assert token not in out


# ---------------------------------------------------------------------------
# CLI smoke
# ---------------------------------------------------------------------------


def _scenario_to_json(result: ScenarioResult) -> str:
    return json.dumps(
        {
            "phases": [
                {
                    "phase_name": p.phase_name,
                    "status": p.status,
                    "category": p.category,
                }
                for p in result.phases
            ],
            "counters": result.counters,
        }
    )


class TestCliEntry:
    def test_stdin_json_to_markdown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = _scenario_to_json(
            ScenarioResult(phases=_completed_phases(), counters=_baseline_counters())
        )
        monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            rc = cli_module.main([])
        assert rc == 0, stderr.getvalue()
        out = stdout.getvalue()
        assert "## Result\n\ncompleted\n" in out
        assert "| batch | ok | batch_ok |" in out

    def test_input_file_argument(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        result = ScenarioResult(
            phases=(PhaseRecord("batch", "ok", "batch_ok"),),
            counters=_baseline_counters(),
        )
        input_path = tmp_path / "scenario.json"
        input_path.write_text(_scenario_to_json(result), encoding="utf-8")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            rc = cli_module.main(["--input", str(input_path)])
        assert rc == 0, stderr.getvalue()
        assert "## Result\n\ncompleted\n" in stdout.getvalue()

    def test_empty_stdin_returns_input_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            rc = cli_module.main([])
        assert rc == 1
        assert "error:" in stderr.getvalue()
        assert stdout.getvalue() == ""

    def test_malformed_json_returns_input_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "stdin", io.StringIO("{not valid json"))
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            rc = cli_module.main([])
        assert rc == 1
        assert stdout.getvalue() == ""

    def test_missing_phase_key_returns_input_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bad_payload = json.dumps(
            {"phases": [{"status": "ok"}], "counters": {}}
        )
        monkeypatch.setattr(sys, "stdin", io.StringIO(bad_payload))
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            rc = cli_module.main([])
        assert rc == 1
        assert stdout.getvalue() == ""

    @pytest.mark.parametrize(
        "bad_status", ["error", "failed", "FAIL", "success", "OK", ""]
    )
    def test_unknown_phase_status_rejected_by_cli(
        self, bad_status: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Status-vocabulary enforcement (PR #98 codex review id 4351827310).
        # Without this guard, an unknown status would silently fall through
        # ``classify_result_state`` to ``completed`` — turning a failed
        # scenario into a successful report. The CLI must reject the input.
        bad_payload = json.dumps(
            {
                "phases": [
                    {"phase_name": "batch", "status": bad_status},
                ],
                "counters": {},
            }
        )
        monkeypatch.setattr(sys, "stdin", io.StringIO(bad_payload))
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            rc = cli_module.main([])
        assert rc == 1
        # No partial report emitted; rejection should be category-only.
        assert stdout.getvalue() == ""
        # Diagnostic names the offending field but does NOT need to echo
        # the bad token. We only assert the failure happened on the
        # status field.
        assert "status" in stderr.getvalue()

    def test_redaction_failure_returns_exit_two(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Plant a vault path in a phase category — renderer's sweep must
        # reject it, CLI must surface a category-only diagnostic.
        bad = _scenario_to_json(
            ScenarioResult(
                phases=(
                    PhaseRecord(
                        "leaky_phase", "fail", "/manifests/leaked.json"
                    ),
                ),
                counters={**_baseline_counters(), "failed_documents": 1},
            )
        )
        monkeypatch.setattr(sys, "stdin", io.StringIO(bad))
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            rc = cli_module.main([])
        assert rc == 2
        assert stdout.getvalue() == ""
        assert "redaction" in stderr.getvalue().lower()


# ---------------------------------------------------------------------------
# Subprocess smoke — full `python -m yomotsusaka.cli.operational_report`
# round-trip. Guards against import-time regressions that the in-process
# CLI test does not catch (e.g. accidental package-init side-effects).
# ---------------------------------------------------------------------------


class TestCliSubprocess:
    def test_module_invocation_renders_completed_report(self) -> None:
        payload = _scenario_to_json(
            ScenarioResult(
                phases=_completed_phases(), counters=_baseline_counters()
            )
        )
        proc = subprocess.run(
            [sys.executable, "-m", "yomotsusaka.cli.operational_report"],
            input=payload,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        assert "## Result\n\ncompleted\n" in proc.stdout
