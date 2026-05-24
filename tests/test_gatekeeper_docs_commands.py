"""Tests for ``scripts/gatekeeper/check_docs_commands.py`` (issue #114).

The script lives outside the importable package tree, so we load it via
``importlib.util.spec_from_file_location`` and exercise it both as a
library (``run_checks``) and as a CLI (``main``).

The fixture corpus lives under
``tests/fixtures/gatekeeper_docs_commands/`` with one sample per rule.
Each parametrised case asserts the rule fires on its sample and that
``main`` exits non-zero. ``test_tip_of_main_passes`` runs the live check
against the real ``README.md``, ``AGENTS.md``, and ``docs/*.md``.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "gatekeeper" / "check_docs_commands.py"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "gatekeeper_docs_commands"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "check_docs_commands", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register in sys.modules before exec so @dataclass can introspect
    # `cls.__module__` correctly (Python 3.11 dataclasses look the module
    # up via sys.modules.get; if it returns None, dataclass creation
    # crashes with AttributeError on '__dict__').
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CHECK_MODULE = _load_module()


# ---------------------------------------------------------------------------
# Tip-of-main + clean fixture
# ---------------------------------------------------------------------------


def test_tip_of_main_passes(capsys):
    """The live README / AGENTS.md / docs/*.md must pass every check."""

    exit_code = CHECK_MODULE.main(["--root", str(REPO_ROOT)])
    captured = capsys.readouterr()
    assert exit_code == 0, (
        f"tip-of-main is dirty: exit={exit_code}\n{captured.out}"
    )


def test_clean_fixture_has_no_findings():
    paths = [FIXTURES / "clean" / "README.md"]
    report = CHECK_MODULE.run_checks(paths, REPO_ROOT)
    assert report.findings == [], (
        f"expected clean fixture to pass; got {report.findings}"
    )


# ---------------------------------------------------------------------------
# Per-rule fixture cases
# ---------------------------------------------------------------------------

RULE_CASES = [
    ("cli_module_importable", "docs_to_source.cli_module_importable"),
    ("module_path_imports", "docs_to_source.module_path_imports"),
    ("documented_paths_exist", "docs_to_source.documented_paths_exist"),
    ("enum_names_in_source", "docs_to_source.enum_names_in_source"),
    (
        "env_var_names_grep_detectable",
        "docs_to_source.env_var_names_grep_detectable",
    ),
    (
        "python_invocation_has_main",
        "command_validity.python_invocation_has_main",
    ),
    ("tee_pipefail_guard", "command_validity.tee_pipefail_guard"),
    ("fixture_path_seeded", "command_validity.fixture_path_seeded"),
]


@pytest.mark.parametrize("subdir,rule_id", RULE_CASES)
def test_rule_fires_on_fixture(subdir, rule_id):
    fixture = FIXTURES / subdir / "README.md"
    assert fixture.exists(), f"missing fixture: {fixture}"
    report = CHECK_MODULE.run_checks([fixture], REPO_ROOT)
    rule_ids = [f.rule for f in report.findings]
    assert rule_id in rule_ids, (
        f"rule {rule_id} did not fire on fixture {fixture}; "
        f"got {rule_ids}"
    )


@pytest.mark.parametrize("subdir,rule_id", RULE_CASES)
def test_main_exits_one_on_fixture(subdir, rule_id, capsys, monkeypatch):
    """``main`` returns exit code 1 when at least one rule fires."""

    fixture = FIXTURES / subdir / "README.md"
    rel = fixture.relative_to(REPO_ROOT).as_posix()
    exit_code = CHECK_MODULE.main(
        ["--root", str(REPO_ROOT), "--paths", rel]
    )
    captured = capsys.readouterr()
    assert exit_code == 1, (
        f"fixture {subdir} did not produce exit=1; got {exit_code}\n"
        f"{captured.out}"
    )
    assert rule_id in captured.out, (
        f"rule {rule_id} not surfaced in human report:\n{captured.out}"
    )


# ---------------------------------------------------------------------------
# Internal-error path
# ---------------------------------------------------------------------------


def test_malformed_fence_exits_two(capsys):
    fixture = FIXTURES / "malformed_fence" / "README.md"
    rel = fixture.relative_to(REPO_ROOT).as_posix()
    exit_code = CHECK_MODULE.main(
        ["--root", str(REPO_ROOT), "--paths", rel]
    )
    captured = capsys.readouterr()
    assert exit_code == 2, (
        f"malformed fence should exit 2; got {exit_code}\n"
        f"out={captured.out!r}\nerr={captured.err!r}"
    )
    assert "internal-error" in captured.err.lower()


# ---------------------------------------------------------------------------
# JSON output schema
# ---------------------------------------------------------------------------


def test_json_output_schema_on_violation(capsys):
    fixture = FIXTURES / "cli_module_importable" / "README.md"
    rel = fixture.relative_to(REPO_ROOT).as_posix()
    exit_code = CHECK_MODULE.main(
        ["--root", str(REPO_ROOT), "--paths", rel, "--json"]
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    payload = json.loads(captured.out)
    assert payload["version"] == 1
    assert set(payload["summary"]) == {
        "files_scanned",
        "blocks_scanned",
        "violations",
    }
    assert payload["summary"]["violations"] >= 1
    finding = payload["findings"][0]
    assert set(finding) == {
        "rule",
        "severity",
        "path",
        "line",
        "block_tag",
        "evidence",
        "detail",
    }
    assert finding["rule"] == "docs_to_source.cli_module_importable"


def test_json_output_schema_clean(capsys):
    fixture = FIXTURES / "clean" / "README.md"
    rel = fixture.relative_to(REPO_ROOT).as_posix()
    exit_code = CHECK_MODULE.main(
        ["--root", str(REPO_ROOT), "--paths", rel, "--json"]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    payload = json.loads(captured.out)
    assert payload["summary"]["violations"] == 0
    assert payload["findings"] == []


def test_json_out_file_written(tmp_path):
    fixture = FIXTURES / "cli_module_importable" / "README.md"
    rel = fixture.relative_to(REPO_ROOT).as_posix()
    out = tmp_path / "report.json"
    # capture stdout to avoid polluting the test log
    buf = io.StringIO()
    with redirect_stdout(buf):
        exit_code = CHECK_MODULE.main(
            [
                "--root",
                str(REPO_ROOT),
                "--paths",
                rel,
                "--json-out",
                str(out),
            ]
        )
    assert exit_code == 1
    assert out.exists()
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["summary"]["violations"] >= 1


# ---------------------------------------------------------------------------
# Fence-extraction regression tests
# ---------------------------------------------------------------------------


def test_tilde_fences_are_recognized(tmp_path):
    sample = tmp_path / "tilde.md"
    sample.write_text(
        "~~~sh\nuv run python -m yomotsusaka.cli.does_not_exist_xyz\n~~~\n",
        encoding="utf-8",
    )
    report = CHECK_MODULE.run_checks([sample], REPO_ROOT)
    rule_ids = [f.rule for f in report.findings]
    assert "docs_to_source.cli_module_importable" in rule_ids


def test_untagged_fence_is_scanned(tmp_path):
    sample = tmp_path / "untagged.md"
    sample.write_text(
        "```\nuv run python -m yomotsusaka.cli.does_not_exist_xyz\n```\n",
        encoding="utf-8",
    )
    report = CHECK_MODULE.run_checks([sample], REPO_ROOT)
    rule_ids = [f.rule for f in report.findings]
    assert "docs_to_source.cli_module_importable" in rule_ids


def test_unrelated_tag_is_skipped(tmp_path):
    sample = tmp_path / "json.md"
    sample.write_text(
        '```json\n{"python -m yomotsusaka.does_not_exist": true}\n```\n',
        encoding="utf-8",
    )
    report = CHECK_MODULE.run_checks([sample], REPO_ROOT)
    # The block is skipped, so no cli_module_importable finding.
    rule_ids = [f.rule for f in report.findings]
    assert "docs_to_source.cli_module_importable" not in rule_ids


# ---------------------------------------------------------------------------
# Negative checks for narrow rules
# ---------------------------------------------------------------------------


def test_tee_without_dollar_question_does_not_fire(tmp_path):
    sample = tmp_path / "tee_log.md"
    sample.write_text(
        "```sh\necho 'launching' | tee run.log\n```\n",
        encoding="utf-8",
    )
    report = CHECK_MODULE.run_checks([sample], REPO_ROOT)
    rule_ids = [f.rule for f in report.findings]
    assert "command_validity.tee_pipefail_guard" not in rule_ids


def test_tee_with_pipefail_guard_does_not_fire(tmp_path):
    sample = tmp_path / "tee_guarded.md"
    sample.write_text(
        "```sh\nset -o pipefail\nuv run pytest 2>&1 | tee pytest.log\n"
        "if [ $? -ne 0 ]; then echo fail; fi\n```\n",
        encoding="utf-8",
    )
    report = CHECK_MODULE.run_checks([sample], REPO_ROOT)
    rule_ids = [f.rule for f in report.findings]
    assert "command_validity.tee_pipefail_guard" not in rule_ids


def test_tee_with_pipestatus_guard_does_not_fire(tmp_path):
    sample = tmp_path / "tee_pipestatus.md"
    sample.write_text(
        "```sh\nuv run pytest 2>&1 | tee pytest.log\n"
        'if [ "${PIPESTATUS[0]}" -ne 0 ]; then echo fail; fi\n```\n',
        encoding="utf-8",
    )
    report = CHECK_MODULE.run_checks([sample], REPO_ROOT)
    rule_ids = [f.rule for f in report.findings]
    assert "command_validity.tee_pipefail_guard" not in rule_ids


def test_inbox_with_mkdir_seeding_does_not_fire(tmp_path):
    sample = tmp_path / "seeded.md"
    sample.write_text(
        "```sh\nmkdir -p ./inbox\n"
        "uv run python -m yomotsusaka.cli.run_batch ./inbox --vault-root ./vault\n"
        "```\n",
        encoding="utf-8",
    )
    report = CHECK_MODULE.run_checks([sample], REPO_ROOT)
    rule_ids = [f.rule for f in report.findings]
    assert "command_validity.fixture_path_seeded" not in rule_ids
