"""Tests for ``scripts/gatekeeper/check_agents_md.py`` (issue #109).

The script lives outside the importable package tree, so we load it via
``importlib.util.spec_from_file_location`` and exercise it both as a
library (``run_checks``) and as a CLI (``main``).

The fixture corpus lives under
``tests/fixtures/gatekeeper_agents_md/`` with one sample per rule.
``test_tip_of_main_passes`` runs the live check against the real
``AGENTS.md`` at the repo root and asserts a clean exit so the rule set
never falls behind the file it gates.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "gatekeeper" / "check_agents_md.py"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "gatekeeper_agents_md"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "check_agents_md", SCRIPT_PATH
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
    """The live AGENTS.md must pass every hygiene check."""

    exit_code = CHECK_MODULE.main(["--root", str(REPO_ROOT)])
    captured = capsys.readouterr()
    assert exit_code == 0, (
        f"tip-of-main AGENTS.md is dirty: exit={exit_code}\n"
        f"{captured.out}"
    )


def test_clean_fixture_has_no_findings():
    target = FIXTURES / "clean" / "AGENTS.md"
    report = CHECK_MODULE.run_checks(
        target, REPO_ROOT, CHECK_MODULE.DEFAULT_MAX_VISIBLE_LINES
    )
    assert report.findings == [], (
        f"expected clean fixture to pass; got {report.findings}"
    )


# ---------------------------------------------------------------------------
# Per-rule fixture cases
# ---------------------------------------------------------------------------

RULE_CASES = [
    ("visible_line_cap", "agents_md.visible_line_cap"),
    (
        "no_issue_pr_mvp_provenance",
        "agents_md.no_issue_pr_mvp_provenance",
    ),
    (
        "docs_references_resolve",
        "agents_md.docs_references_resolve",
    ),
]


@pytest.mark.parametrize("subdir,rule_id", RULE_CASES)
def test_rule_fires_on_fixture(subdir, rule_id):
    fixture = FIXTURES / subdir / "AGENTS.md"
    assert fixture.exists(), f"missing fixture: {fixture}"
    report = CHECK_MODULE.run_checks(
        fixture, REPO_ROOT, CHECK_MODULE.DEFAULT_MAX_VISIBLE_LINES
    )
    rule_ids = [f.rule for f in report.findings]
    assert rule_id in rule_ids, (
        f"rule {rule_id} did not fire on fixture {fixture}; "
        f"got {rule_ids}"
    )


@pytest.mark.parametrize("subdir,rule_id", RULE_CASES)
def test_main_exits_one_on_fixture(subdir, rule_id, capsys):
    """``main`` returns exit code 1 when at least one rule fires."""

    fixture = FIXTURES / subdir / "AGENTS.md"
    exit_code = CHECK_MODULE.main(
        ["--root", str(REPO_ROOT), "--target", str(fixture)]
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
# Visible-line classifier semantics
# ---------------------------------------------------------------------------


def test_blank_lines_and_html_comments_do_not_count():
    """Blank lines and single-line ``<!-- ... -->`` comments are invisible."""

    sample = (
        "# Title\n"
        "\n"
        "<!-- a directive comment -->\n"
        "- visible bullet\n"
        "\n"
        "<!-- another comment -->\n"
    )
    assert CHECK_MODULE.count_visible_lines(sample) == 2


def test_visible_line_cap_can_be_overridden(capsys):
    """``--max-lines`` can tighten or relax the cap."""

    fixture = FIXTURES / "clean" / "AGENTS.md"
    # The clean fixture has < 15 visible lines; tighten to 1 to force a
    # violation, verifying the override path.
    exit_code = CHECK_MODULE.main(
        [
            "--root",
            str(REPO_ROOT),
            "--target",
            str(fixture),
            "--max-lines",
            "1",
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "agents_md.visible_line_cap" in captured.out


# ---------------------------------------------------------------------------
# Provenance-token detection edge cases
# ---------------------------------------------------------------------------


def test_provenance_check_skips_markdown_anchors_and_shell_comments(tmp_path):
    """``#section`` Markdown anchors and shell ``#`` comments do not fire."""

    target = tmp_path / "AGENTS.md"
    target.write_text(
        "# Heading\n"
        "\n"
        "- See `docs/architecture.md#source-of-truth-precedence`.\n"
        "- Shell comments like `# noqa` are fine.\n"
        "- Phrase 'PR review' (no digits) is fine.\n",
        encoding="utf-8",
    )
    report = CHECK_MODULE.run_checks(target, tmp_path, 99)
    provenance = [
        f
        for f in report.findings
        if f.rule == "agents_md.no_issue_pr_mvp_provenance"
    ]
    assert provenance == [], f"unexpected provenance findings: {provenance}"


@pytest.mark.parametrize(
    "snippet,expected_kind",
    [
        ("Closes #109.", "#109"),
        ("Per MVP-4 ...", "MVP-4"),
        ("Landed via PR 88.", "PR 88"),
        ("Landed via PR #88.", "PR #88"),
    ],
)
def test_provenance_check_fires_on_each_token_form(
    snippet, expected_kind, tmp_path
):
    target = tmp_path / "AGENTS.md"
    target.write_text(f"# A\n\n- {snippet}\n", encoding="utf-8")
    report = CHECK_MODULE.run_checks(target, tmp_path, 99)
    matches = [
        f
        for f in report.findings
        if f.rule == "agents_md.no_issue_pr_mvp_provenance"
    ]
    assert matches, (
        f"expected provenance rule to fire on {snippet!r}; got {report.findings}"
    )
    assert any(expected_kind in f.evidence for f in matches), (
        f"expected evidence containing {expected_kind!r} for {snippet!r}; "
        f"got {[f.evidence for f in matches]}"
    )


# ---------------------------------------------------------------------------
# Internal-error path
# ---------------------------------------------------------------------------


def test_missing_target_exits_two(tmp_path, capsys):
    exit_code = CHECK_MODULE.main(
        [
            "--root",
            str(REPO_ROOT),
            "--target",
            str(tmp_path / "does-not-exist.md"),
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "internal-error" in captured.err.lower()


# ---------------------------------------------------------------------------
# JSON output schema
# ---------------------------------------------------------------------------


def test_json_output_schema_on_violation(capsys):
    fixture = FIXTURES / "visible_line_cap" / "AGENTS.md"
    exit_code = CHECK_MODULE.main(
        [
            "--root",
            str(REPO_ROOT),
            "--target",
            str(fixture),
            "--json",
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    payload = json.loads(captured.out)
    assert payload["version"] == 1
    assert set(payload["summary"]) == {"files_scanned", "violations"}
    assert payload["summary"]["violations"] >= 1
    finding = payload["findings"][0]
    assert set(finding) == {
        "rule",
        "severity",
        "path",
        "line",
        "evidence",
        "detail",
    }


def test_json_output_schema_clean(capsys):
    fixture = FIXTURES / "clean" / "AGENTS.md"
    exit_code = CHECK_MODULE.main(
        [
            "--root",
            str(REPO_ROOT),
            "--target",
            str(fixture),
            "--json",
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    payload = json.loads(captured.out)
    assert payload["summary"]["violations"] == 0
    assert payload["findings"] == []
