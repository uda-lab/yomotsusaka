"""Tests for scripts/gatekeeper/check_lifecycle_invariant.py (Gate-keeper G4).

Covers:
- G4.1: ManageRunPodLifecycle.start_pod has stop_pod in _wait_for_healthy handler
- G4.2: caller functions that call start_pod have paired stop_pod

Validates:
- Drift fixture (pre-#125): start_pod without cleanup fires G4.1
- Drift-free fixture (post-#125): start_pod with cleanup passes G4.1
- Tip-of-main: real runpod_lifecycle.py passes G4.1
- Caller drift fixture: caller calls start_pod but no stop_pod fires G4.2
- Caller drift-free: caller has stop_pod somewhere passes G4.2
- # CLEANUP: caller-responsibility marker exempts from G4.2
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
_MODULE_PATH = _GATEKEEPER_DIR / "check_lifecycle_invariant.py"


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("check_lifecycle_invariant", _MODULE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("check_lifecycle_invariant", mod)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


# ---------------------------------------------------------------------------
# G4.1: Library start_pod — drift fixture (pre-#125)
# ---------------------------------------------------------------------------

_PRE_125_DRIFT = """\
class ManageRunPodLifecycle:
    def start_pod(self, config=None):
        handle = self._create_pod()
        try:
            self._wait_for_healthy(handle)
        except PodUnavailableError:
            raise  # pre-#125: no stop_pod call — orphan Pod!
        return handle
"""

_POST_125_CLEAN = """\
class ManageRunPodLifecycle:
    def start_pod(self, config=None):
        handle = self._create_pod()
        try:
            self._wait_for_healthy(handle)
        except PodUnavailableError:
            try:
                self.stop_pod(handle, terminate=True)
            except PodUnavailableError:
                raise PodUnavailableError("wait_timeout_cleanup_failed") from None
            raise
        return handle
"""


def _parse_and_check_g41(source: str, tmp_path: Path) -> list[Any]:
    """Write source to a temp file and run _check_file_g41 against it."""
    p = tmp_path / "runpod_lifecycle.py"
    p.write_text(source)
    repo_root = tmp_path
    return list(_mod._check_file_g41(p, repo_root, p))


def test_g41_drift_fixture_fires(tmp_path: Path) -> None:
    """Pre-#125 start_pod (no stop_pod in handler) must fire G4.1."""
    findings = _parse_and_check_g41(_PRE_125_DRIFT, tmp_path)
    assert len(findings) == 1
    assert findings[0].rule == "lifecycle_invariant.library_start_pod_has_cleanup"
    assert findings[0].function == "start_pod"
    assert "#124" in findings[0].detail or "orphan" in findings[0].detail.lower()


def test_g41_clean_fixture_passes(tmp_path: Path) -> None:
    """Post-#125 start_pod (stop_pod in handler) must NOT fire G4.1."""
    findings = _parse_and_check_g41(_POST_125_CLEAN, tmp_path)
    assert findings == []


def test_g41_finally_block_accepted(tmp_path: Path) -> None:
    """stop_pod in a finally block (instead of except) is also acceptable."""
    source = """\
class ManageRunPodLifecycle:
    def start_pod(self, config=None):
        handle = self._create_pod()
        try:
            self._wait_for_healthy(handle)
        finally:
            self.stop_pod(handle, terminate=True)
        return handle
"""
    findings = _parse_and_check_g41(source, tmp_path)
    assert findings == []


def test_g41_skips_non_manage_class(tmp_path: Path) -> None:
    """G4.1 only checks ManageRunPodLifecycle, not other classes."""
    source = """\
class MockRunPodLifecycle:
    def start_pod(self, config=None):
        try:
            self._wait_for_healthy(handle)
        except Exception:
            raise  # no stop_pod — but this is not ManageRunPodLifecycle
        return handle
"""
    findings = _parse_and_check_g41(source, tmp_path)
    # G4.1 does not fire for MockRunPodLifecycle
    assert all(f.rule != "lifecycle_invariant.library_start_pod_has_cleanup" for f in findings)


# ---------------------------------------------------------------------------
# G4.1: real tip-of-main runpod_lifecycle.py
# ---------------------------------------------------------------------------


def test_g41_tip_of_main_passes() -> None:
    """The real ManageRunPodLifecycle.start_pod must pass G4.1 (post-#125)."""
    repo_root = Path(__file__).resolve().parents[1]
    lifecycle_file = repo_root / "src" / "yomotsusaka" / "runpod_lifecycle.py"
    assert lifecycle_file.exists(), "runpod_lifecycle.py must exist"
    findings = list(_mod._check_file_g41(lifecycle_file, repo_root, lifecycle_file))
    g41_findings = [f for f in findings if f.rule == "lifecycle_invariant.library_start_pod_has_cleanup"]
    assert g41_findings == [], (
        f"ManageRunPodLifecycle.start_pod fails G4.1 on tip-of-main: {g41_findings}"
    )


# ---------------------------------------------------------------------------
# G4.2: Caller functions
# ---------------------------------------------------------------------------


def _parse_and_check_g42(source: str, tmp_path: Path) -> list[Any]:
    p = tmp_path / "caller.py"
    p.write_text(source)
    repo_root = tmp_path
    return list(_mod._check_file_g42(p, repo_root))


def test_g42_caller_without_stop_pod_fires(tmp_path: Path) -> None:
    """Caller that calls start_pod but never calls stop_pod fires G4.2."""
    source = """\
def run_lifecycle(lifecycle, config):
    handle = lifecycle.start_pod(config)
    do_work(handle)
    # No stop_pod call anywhere — potential orphan Pod
"""
    findings = _parse_and_check_g42(source, tmp_path)
    assert len(findings) == 1
    assert findings[0].rule == "lifecycle_invariant.caller_start_pod_paired"
    assert findings[0].function == "run_lifecycle"


def test_g42_caller_with_stop_pod_passes(tmp_path: Path) -> None:
    """Caller that calls start_pod AND stop_pod passes G4.2."""
    source = """\
def run_lifecycle(lifecycle, config):
    handle = lifecycle.start_pod(config)
    try:
        do_work(handle)
    finally:
        lifecycle.stop_pod(handle, terminate=True)
"""
    findings = _parse_and_check_g42(source, tmp_path)
    assert findings == []


def test_g42_caller_responsibility_marker_exempts(tmp_path: Path) -> None:
    """Functions with # CLEANUP: caller-responsibility are exempt from G4.2."""
    source = """\
def run_lifecycle(lifecycle, config):
    # CLEANUP: caller-responsibility
    handle = lifecycle.start_pod(config)
    return handle
"""
    findings = _parse_and_check_g42(source, tmp_path)
    assert findings == []


def test_g42_start_pod_method_itself_skipped(tmp_path: Path) -> None:
    """The start_pod method itself is exempt from G4.2 (it IS the implementation)."""
    source = """\
class ManageRunPodLifecycle:
    def start_pod(self, config=None):
        handle = self._create()
        # No stop_pod here — the stop_pod is in the inner try
        return handle
"""
    findings = _parse_and_check_g42(source, tmp_path)
    # G4.2 does not flag the start_pod method itself
    g42_findings = [f for f in findings if f.rule == "lifecycle_invariant.caller_start_pod_paired"]
    assert g42_findings == []


def test_g42_stop_pod_in_except_block_passes(tmp_path: Path) -> None:
    """Caller with stop_pod in an except handler passes G4.2."""
    source = """\
def run_lifecycle(lifecycle, config):
    try:
        handle = lifecycle.start_pod(config)
    except Exception:
        lifecycle.stop_pod(None, terminate=True)
        raise
    return handle
"""
    findings = _parse_and_check_g42(source, tmp_path)
    assert findings == []


# ---------------------------------------------------------------------------
# G4.2: real tip-of-main source files
# ---------------------------------------------------------------------------


def test_g42_tip_of_main_src_passes() -> None:
    """All src/ and scripts/ caller functions pass G4.2 on tip-of-main."""
    repo_root = Path(__file__).resolve().parents[1]
    report = _mod.run_checks(repo_root)
    g42_findings = [
        f for f in report.findings
        if f.rule == "lifecycle_invariant.caller_start_pod_paired"
    ]
    assert g42_findings == [], (
        f"G4.2 violations on tip-of-main: {[(f.file, f.function) for f in g42_findings]}"
    )


# ---------------------------------------------------------------------------
# Full run_checks on tip-of-main
# ---------------------------------------------------------------------------


def test_run_checks_tip_of_main_zero_findings() -> None:
    """run_checks on the real repo root must report zero findings (post-#125)."""
    repo_root = Path(__file__).resolve().parents[1]
    report = _mod.run_checks(repo_root)
    assert report.findings == [], (
        "Unexpected G4 findings on tip-of-main:\n"
        + "\n".join(
            f"  {f.rule} @ {f.file}:{f.line} ({f.function})"
            for f in report.findings
        )
    )


# ---------------------------------------------------------------------------
# CLI smoke tests
# ---------------------------------------------------------------------------


def test_main_exit_0_on_clean_tree() -> None:
    """CLI exits 0 on a clean tree (tip-of-main)."""
    repo_root = Path(__file__).resolve().parents[1]
    result = _mod.main(["--root", str(repo_root)])
    assert result == 0


def test_main_exit_1_on_drift(tmp_path: Path) -> None:
    """CLI exits 1 when a G4.1 violation is found."""
    src = tmp_path / "src" / "yomotsusaka"
    src.mkdir(parents=True)
    lifecycle = src / "runpod_lifecycle.py"
    lifecycle.write_text(_PRE_125_DRIFT)
    result = _mod.main(["--root", str(tmp_path)])
    assert result == 1
