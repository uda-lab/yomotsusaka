"""Tests for scripts/gatekeeper/check_spec_values.py (Gate-keeper G2).

Covers the three validation rules:
- G2.1: spec-values block is parseable (has min/max as integers)
- G2.2: target attribute is resolvable
- G2.3: resolved value falls in [min, max]

Also validates:
- Drift fixture: PodConfig.disk_gb=20 fires when docs say [30,50]
- Drift-free fixture: PodConfig.disk_gb=40 passes
- No spec-values blocks → zero violations (clean scan)
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Import the check module under test
# ---------------------------------------------------------------------------

_GATEKEEPER_DIR = Path(__file__).resolve().parents[1] / "scripts" / "gatekeeper"
_MODULE_PATH = _GATEKEEPER_DIR / "check_spec_values.py"


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("check_spec_values", _MODULE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # Register under a unique name to avoid cache pollution
    sys.modules.setdefault("check_spec_values", mod)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


# ---------------------------------------------------------------------------
# Block parsing tests
# ---------------------------------------------------------------------------


def test_parse_blocks_finds_simple_block() -> None:
    text = """\
## §5 Storage

<!-- spec-values target=PodConfig.disk_gb -->
- min: 30
- max: 50
- practical: 40
<!-- /spec-values -->
"""
    blocks = _mod._parse_blocks(text, "docs/runpod.md")
    assert len(blocks) == 1
    b = blocks[0]
    assert b.target == "PodConfig.disk_gb"
    assert b.fields["min"] == "30"
    assert b.fields["max"] == "50"
    assert b.fields["practical"] == "40"


def test_parse_blocks_multiple_blocks() -> None:
    text = """\
<!-- spec-values target=PodConfig.disk_gb -->
- min: 30
- max: 50
<!-- /spec-values -->
<!-- spec-values target=PodConfig.other -->
- min: 1
- max: 8
<!-- /spec-values -->
"""
    blocks = _mod._parse_blocks(text, "docs/runpod.md")
    assert len(blocks) == 2
    assert blocks[0].target == "PodConfig.disk_gb"
    assert blocks[1].target == "PodConfig.other"


def test_parse_blocks_empty_doc() -> None:
    blocks = _mod._parse_blocks("No blocks here", "docs/runpod.md")
    assert blocks == []


# ---------------------------------------------------------------------------
# G2.1: Block parseable
# ---------------------------------------------------------------------------


def test_check_block_missing_min_max(tmp_path: Path) -> None:
    repo_root = tmp_path
    block = _mod.SpecBlock(
        target="PodConfig.disk_gb",
        file="docs/runpod.md",
        open_line=5,
        fields={"practical": "40"},  # missing min and max
    )
    findings = list(_mod.check_block(block, repo_root))
    assert len(findings) == 1
    assert findings[0].rule == "spec_values.block_parseable"
    assert "min" in findings[0].detail or "max" in findings[0].detail


def test_check_block_non_integer_min(tmp_path: Path) -> None:
    repo_root = tmp_path
    block = _mod.SpecBlock(
        target="PodConfig.disk_gb",
        file="docs/runpod.md",
        open_line=5,
        fields={"min": "30GB", "max": "50"},  # min has unit
    )
    findings = list(_mod.check_block(block, repo_root))
    assert len(findings) == 1
    assert findings[0].rule == "spec_values.block_parseable"


# ---------------------------------------------------------------------------
# G2.3: Value in range — drift and drift-free fixtures
# ---------------------------------------------------------------------------


class _DriftPodConfig:
    """Drift fixture: disk_gb=20 (below the [30,50] spec range)."""
    disk_gb: int = 20


class _CleanPodConfig:
    """Drift-free fixture: disk_gb=40 (within [30,50])."""
    disk_gb: int = 40


def test_value_in_range_drift_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Drift fixture: disk_gb=20 is outside [30,50] — gate must fire."""
    repo_root = tmp_path
    # Patch _resolve_class_attr to return 20
    monkeypatch.setattr(_mod, "_resolve_class_attr", lambda target, root: 20)
    block = _mod.SpecBlock(
        target="PodConfig.disk_gb",
        file="docs/runpod.md",
        open_line=5,
        fields={"min": "30", "max": "50"},
    )
    findings = list(_mod.check_block(block, repo_root))
    assert len(findings) == 1
    assert findings[0].rule == "spec_values.value_in_range"
    assert "20" in findings[0].evidence
    assert "outside" in findings[0].detail


def test_value_in_range_drift_free_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Drift-free fixture: disk_gb=40 is within [30,50] — gate must NOT fire."""
    repo_root = tmp_path
    monkeypatch.setattr(_mod, "_resolve_class_attr", lambda target, root: 40)
    block = _mod.SpecBlock(
        target="PodConfig.disk_gb",
        file="docs/runpod.md",
        open_line=5,
        fields={"min": "30", "max": "50"},
    )
    findings = list(_mod.check_block(block, repo_root))
    assert findings == []


def test_value_at_boundary_min_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Boundary value min=30 is inclusive — must pass."""
    repo_root = tmp_path
    monkeypatch.setattr(_mod, "_resolve_class_attr", lambda target, root: 30)
    block = _mod.SpecBlock(
        target="PodConfig.disk_gb",
        file="docs/runpod.md",
        open_line=5,
        fields={"min": "30", "max": "50"},
    )
    assert list(_mod.check_block(block, repo_root)) == []


def test_value_at_boundary_max_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Boundary value max=50 is inclusive — must pass."""
    repo_root = tmp_path
    monkeypatch.setattr(_mod, "_resolve_class_attr", lambda target, root: 50)
    block = _mod.SpecBlock(
        target="PodConfig.disk_gb",
        file="docs/runpod.md",
        open_line=5,
        fields={"min": "30", "max": "50"},
    )
    assert list(_mod.check_block(block, repo_root)) == []


# ---------------------------------------------------------------------------
# G2.2: Target resolvable — unresolvable target fires
# ---------------------------------------------------------------------------


def test_target_unresolvable_fires(tmp_path: Path) -> None:
    """An unresolvable target is itself drift evidence."""
    repo_root = tmp_path
    block = _mod.SpecBlock(
        target="NonExistentClass.nonexistent_attr",
        file="docs/runpod.md",
        open_line=5,
        fields={"min": "30", "max": "50"},
    )
    findings = list(_mod.check_block(block, repo_root))
    # Either target_resolvable or value_in_range fires (target not found)
    assert len(findings) == 1
    assert findings[0].rule in (
        "spec_values.target_resolvable",
        "spec_values.value_in_range",
    )


# ---------------------------------------------------------------------------
# Integration: run_checks on a temp doc directory
# ---------------------------------------------------------------------------


def test_run_checks_no_blocks_clean(tmp_path: Path) -> None:
    """A docs/ directory with no spec-values blocks → zero violations."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "runpod.md").write_text("# §5 Storage\n\nContainer disk: 30-50GB\n")
    report = _mod.run_checks(docs_dir, tmp_path)
    assert report.findings == []
    assert report.files_scanned == 1


def test_run_checks_drift_disk_gb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Integration: disk_gb=20 drifted below spec → gate fires."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "runpod.md").write_text(
        "## §5\n\n"
        "<!-- spec-values target=PodConfig.disk_gb -->\n"
        "- min: 30\n"
        "- max: 50\n"
        "<!-- /spec-values -->\n"
    )
    monkeypatch.setattr(_mod, "_resolve_class_attr", lambda target, root: 20)
    report = _mod.run_checks(docs_dir, tmp_path)
    assert len(report.findings) == 1
    assert report.findings[0].rule == "spec_values.value_in_range"


def test_run_checks_clean_disk_gb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Integration: disk_gb=40 within spec → no violations."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "runpod.md").write_text(
        "## §5\n\n"
        "<!-- spec-values target=PodConfig.disk_gb -->\n"
        "- min: 30\n"
        "- max: 50\n"
        "<!-- /spec-values -->\n"
    )
    monkeypatch.setattr(_mod, "_resolve_class_attr", lambda target, root: 40)
    report = _mod.run_checks(docs_dir, tmp_path)
    assert report.findings == []


# ---------------------------------------------------------------------------
# Integration: PodConfig.disk_gb resolved from the real module
# ---------------------------------------------------------------------------


def test_real_pod_config_disk_gb_in_range() -> None:
    """Tip-of-main: PodConfig.disk_gb must be in [30,50] after the fix."""
    repo_root = Path(__file__).resolve().parents[1]
    actual = _mod._resolve_class_attr("PodConfig.disk_gb", repo_root)
    assert 30 <= int(actual) <= 50, (
        f"PodConfig.disk_gb={actual} is outside the documented range [30,50]; "
        "update the code default or adjust the spec-values annotation in docs/runpod.md"
    )


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------


def test_main_exit_0_when_clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI exits 0 when no violations."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "runpod.md").write_text("# no blocks\n")
    result = _mod.main(["--root", str(tmp_path)])
    assert result == 0


def test_main_exit_1_on_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI exits 1 when a violation is found."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "runpod.md").write_text(
        "<!-- spec-values target=PodConfig.disk_gb -->\n"
        "- min: 30\n"
        "- max: 50\n"
        "<!-- /spec-values -->\n"
    )
    monkeypatch.setattr(_mod, "_resolve_class_attr", lambda target, root: 20)
    result = _mod.main(["--root", str(tmp_path)])
    assert result == 1
