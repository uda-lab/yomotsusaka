"""Tests for scripts/gatekeeper/check_documented_env_vars.py (Gate-keeper G3).

Covers:
- G3.1: every documented env-var must be wired via os.environ.get/os.getenv

Validates:
- Drift fixture: RUNPOD_TEMPLATE_ID in docs but not in source → fires
- Drift-free fixture: RUNPOD_TEMPLATE_ID in docs AND wired in source → passes
- Operator-only annotation exempts a var
- Tip-of-main: RUNPOD_TEMPLATE_ID is wired (post-#126) → passes
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Import the check module under test
# ---------------------------------------------------------------------------

_GATEKEEPER_DIR = Path(__file__).resolve().parents[1] / "scripts" / "gatekeeper"
_MODULE_PATH = _GATEKEEPER_DIR / "check_documented_env_vars.py"


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("check_documented_env_vars", _MODULE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("check_documented_env_vars", mod)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


# ---------------------------------------------------------------------------
# Table parsing
# ---------------------------------------------------------------------------


def test_parse_env_var_tables_finds_backtick_vars() -> None:
    text = """\
| Variable             | Required | Source |
| -------------------- | -------- | ------ |
| `RUNPOD_API_KEY`     | yes      | Owner  |
| `RUNPOD_TEMPLATE_ID` | optional | Owner-pinned template |
"""
    entries = _mod._parse_env_var_tables(text, "docs/runpod-agent-lifecycle.md")
    names = [e.var_name for e in entries]
    assert "RUNPOD_API_KEY" in names
    assert "RUNPOD_TEMPLATE_ID" in names


def test_parse_env_var_tables_skips_header_row() -> None:
    text = """\
| Variable             | Required | Source |
| -------------------- | -------- | ------ |
| `RUNPOD_API_KEY`     | yes      | Owner  |
"""
    entries = _mod._parse_env_var_tables(text, "docs/test.md")
    # Only RUNPOD_API_KEY, not "Variable" header
    assert all(e.var_name != "VARIABLE" for e in entries)


def test_parse_env_var_tables_operator_only() -> None:
    text = """\
| `OWNER_ONLY_VAR` | optional | Owner only (operator-only) |
"""
    entries = _mod._parse_env_var_tables(text, "docs/test.md")
    assert len(entries) == 1
    assert entries[0].operator_only is True


def test_parse_env_var_tables_empty_doc() -> None:
    entries = _mod._parse_env_var_tables("No tables here.", "docs/test.md")
    assert entries == []


# ---------------------------------------------------------------------------
# Source scanning
# ---------------------------------------------------------------------------


def test_build_source_env_lookup_finds_environ_get(tmp_path: Path) -> None:
    py = tmp_path / "mod.py"
    py.write_text('value = os.environ.get("RUNPOD_TEMPLATE_ID")\n')
    result = _mod._build_source_env_lookup([py])
    assert "RUNPOD_TEMPLATE_ID" in result


def test_build_source_env_lookup_finds_getenv(tmp_path: Path) -> None:
    py = tmp_path / "mod.py"
    py.write_text('value = os.getenv("RUNPOD_API_KEY")\n')
    result = _mod._build_source_env_lookup([py])
    assert "RUNPOD_API_KEY" in result


def test_build_source_env_lookup_finds_environ_bracket(tmp_path: Path) -> None:
    py = tmp_path / "mod.py"
    py.write_text('value = os.environ["RUNPOD_API_KEY"]\n')
    result = _mod._build_source_env_lookup([py])
    assert "RUNPOD_API_KEY" in result


def test_build_source_env_lookup_excludes_unwired_var(tmp_path: Path) -> None:
    py = tmp_path / "mod.py"
    py.write_text('x = 1\n')
    result = _mod._build_source_env_lookup([py])
    assert "RUNPOD_TEMPLATE_ID" not in result


# ---------------------------------------------------------------------------
# G3.1: check_env_vars
# ---------------------------------------------------------------------------


def _make_entry(var_name: str, operator_only: bool = False) -> Any:
    return _mod.EnvVarEntry(
        var_name=var_name,
        doc_file="docs/test.md",
        line=1,
        operator_only=operator_only,
    )


def test_check_env_vars_wired_var_passes() -> None:
    """Wired var does not fire."""
    findings = list(_mod.check_env_vars([_make_entry("RUNPOD_API_KEY")], {"RUNPOD_API_KEY"}))
    assert findings == []


def test_check_env_vars_unwired_var_fires() -> None:
    """Unwired var fires G3.1."""
    findings = list(_mod.check_env_vars([_make_entry("RUNPOD_TEMPLATE_ID")], set()))
    assert len(findings) == 1
    assert findings[0].rule == "documented_env_vars.wired_in_source"
    assert findings[0].var_name == "RUNPOD_TEMPLATE_ID"


def test_check_env_vars_operator_only_exempted() -> None:
    """Operator-only vars are exempt from G3.1."""
    findings = list(
        _mod.check_env_vars([_make_entry("OWNER_SECRET", operator_only=True)], set())
    )
    assert findings == []


# ---------------------------------------------------------------------------
# Drift and drift-free fixture integration
# ---------------------------------------------------------------------------


def _make_repo(tmp_path: Path, doc_content: str, src_content: str) -> Path:
    """Create a minimal repo structure with doc + source."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "runpod-agent-lifecycle.md").write_text(doc_content)
    src = tmp_path / "src" / "yomotsusaka"
    src.mkdir(parents=True)
    (src / "runpod_lifecycle.py").write_text(src_content)
    return tmp_path


_DOC_WITH_TEMPLATE_ID = """\
## §2 Injection

| Variable             | Required | Source |
| -------------------- | -------- | ------ |
| `RUNPOD_API_KEY`     | yes      | Owner  |
| `RUNPOD_TEMPLATE_ID` | optional | Owner-pinned template |
"""

_SRC_WITHOUT_TEMPLATE_ID = """\
import os
api_key = os.environ.get("RUNPOD_API_KEY")
# RUNPOD_TEMPLATE_ID is NOT wired — pre-#126 state
"""

_SRC_WITH_TEMPLATE_ID = """\
import os
api_key = os.environ.get("RUNPOD_API_KEY")
template_id = os.environ.get("RUNPOD_TEMPLATE_ID")
"""


def test_drift_fixture_template_id_unwired(tmp_path: Path) -> None:
    """Drift fixture: RUNPOD_TEMPLATE_ID in docs but not in source → fire."""
    repo = _make_repo(tmp_path, _DOC_WITH_TEMPLATE_ID, _SRC_WITHOUT_TEMPLATE_ID)
    report = _mod.run_checks(repo / "docs", repo)
    template_findings = [
        f for f in report.findings if f.var_name == "RUNPOD_TEMPLATE_ID"
    ]
    assert len(template_findings) == 1
    assert template_findings[0].rule == "documented_env_vars.wired_in_source"


def test_drift_free_fixture_template_id_wired(tmp_path: Path) -> None:
    """Drift-free fixture: RUNPOD_TEMPLATE_ID in docs AND wired → pass."""
    repo = _make_repo(tmp_path, _DOC_WITH_TEMPLATE_ID, _SRC_WITH_TEMPLATE_ID)
    report = _mod.run_checks(repo / "docs", repo)
    template_findings = [
        f for f in report.findings if f.var_name == "RUNPOD_TEMPLATE_ID"
    ]
    assert template_findings == []


# ---------------------------------------------------------------------------
# Tip-of-main integration
# ---------------------------------------------------------------------------


def test_tip_of_main_passes() -> None:
    """Tip-of-main: all documented env-vars are wired in source (post-#126)."""
    repo_root = Path(__file__).resolve().parents[1]
    docs_dir = repo_root / "docs"
    report = _mod.run_checks(docs_dir, repo_root)
    assert report.findings == [], (
        "G3 violations on tip-of-main:\n"
        + "\n".join(
            f"  {f.rule} @ {f.file}:{f.line} var={f.var_name}"
            for f in report.findings
        )
    )


# ---------------------------------------------------------------------------
# CLI smoke tests
# ---------------------------------------------------------------------------


def test_main_exit_0_when_clean(tmp_path: Path) -> None:
    """CLI exits 0 when no env-var docs or all wired."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "runpod.md").write_text("No env-var tables here.\n")
    result = _mod.main(["--root", str(tmp_path)])
    assert result == 0


def test_main_exit_1_on_violation(tmp_path: Path) -> None:
    """CLI exits 1 when an unwired documented env-var is found."""
    repo = _make_repo(tmp_path, _DOC_WITH_TEMPLATE_ID, _SRC_WITHOUT_TEMPLATE_ID)
    result = _mod.main(["--root", str(repo)])
    assert result == 1
