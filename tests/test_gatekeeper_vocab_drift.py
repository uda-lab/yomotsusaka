"""
Tests for ``scripts/gatekeeper/check_vocab_drift.py``.

Two flavours of test:

1. **Tip-of-main green test** — the production scan set passes both
   D1 (OperationalCategory) and D2 (boundary exposure classes).
2. **Failing-fixture tests** — synthetic seeded drift cases assert
   each check fires its expected ``Finding``.

The D2 ``exposure_classes`` keyword is exercised as a test-only seam
to simulate the realistic failure case ("code dropped a class, docs
still reference it"); on the live module set this assertion is
tautological.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "gatekeeper_drift"


def _load_check_vocab_drift():
    gatekeeper_dir = REPO_ROOT / "scripts" / "gatekeeper"
    if str(gatekeeper_dir) not in sys.path:
        sys.path.insert(0, str(gatekeeper_dir))

    if "_common" not in sys.modules:
        common_spec = importlib.util.spec_from_file_location(
            "_common", gatekeeper_dir / "_common.py"
        )
        assert common_spec and common_spec.loader
        common = importlib.util.module_from_spec(common_spec)
        sys.modules["_common"] = common
        common_spec.loader.exec_module(common)

    if "check_vocab_drift" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "check_vocab_drift", gatekeeper_dir / "check_vocab_drift.py"
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules["check_vocab_drift"] = module
        spec.loader.exec_module(module)
    return sys.modules["check_vocab_drift"]


@pytest.fixture(scope="module")
def cvd():
    return _load_check_vocab_drift()


# ---------------------------------------------------------------------------
# Tip-of-main green test
# ---------------------------------------------------------------------------


def test_tip_of_main_passes(cvd):
    """Production scan set has no D1 or D2 findings."""

    findings: list = []
    findings.extend(cvd.check_operational_category_drift(REPO_ROOT))
    findings.extend(cvd.check_exposure_class_drift(REPO_ROOT))
    assert findings == [], f"unexpected findings: {findings}"


def test_main_returns_zero_on_tip_of_main(cvd, tmp_path):
    """``main()`` returns the canonical exit code 0 on tip-of-main."""

    rc = cvd.main(["--repo-root", str(REPO_ROOT)])
    assert rc == 0


# ---------------------------------------------------------------------------
# D1 — OperationalCategory drift
# ---------------------------------------------------------------------------


def _make_fake_repo(tmp_path: Path, fixture_name: str, doc_name: str) -> Path:
    (tmp_path / "pyproject.toml").write_text('[project]\nname="fake"\n')
    (tmp_path / "README.md").write_text("# fake\n")
    (tmp_path / "docs").mkdir()
    fixture_text = (FIXTURES_DIR / fixture_name).read_text()
    (tmp_path / "docs" / doc_name).write_text(fixture_text)
    return tmp_path


def test_d1_flags_non_canonical_category_token(cvd, tmp_path):
    """A doc claiming ``batch_kaput`` triggers ``VOCAB_DRIFT_OP_CATEGORY``.

    Uses a deliberately nonsense token outside both
    :class:`OperationalCategory` and the documented synonym
    allowlist so the test stays valid as #111 expands the
    allowlist over time.
    """

    repo = _make_fake_repo(tmp_path, "vocab_drift_category.md", "fake.md")
    findings = cvd.check_operational_category_drift(repo)
    codes = {f.code for f in findings}
    assert "VOCAB_DRIFT_OP_CATEGORY" in codes
    drifted = [f for f in findings if f.code == "VOCAB_DRIFT_OP_CATEGORY"]
    assert any("batch_kaput" in f.message for f in drifted)


def test_d1_accepts_synonym_allowlist(cvd, tmp_path):
    """Tokens explicitly listed in the allowlist do not trigger drift."""

    repo = tmp_path
    (repo / "pyproject.toml").write_text('[project]\nname="fake"\n')
    (repo / "README.md").write_text("# fake\n")
    (repo / "docs").mkdir()
    # Every token below is in CATEGORY_SYNONYM_ALLOWLIST.
    (repo / "docs" / "synonyms.md").write_text(
        "Phase names like `index_snapshot`, `index_reload`, "
        "`search_smoke`, `restoration_request`, `audit_inspect`, "
        "and `runpod_lifecycle` are non-category vocabulary. "
        "Module names like `restoration_api`, `search_gateway`, "
        "`batch_runner`, `audit_log` likewise. Field names: "
        "`audit_record_id`, `audit_file_missing`, `audit_write_failed`.\n"
    )
    findings = cvd.check_operational_category_drift(repo)
    assert findings == []


def test_d1_canonical_tokens_pass(cvd, tmp_path):
    """Tokens that ARE canonical category members do not trigger."""

    repo = tmp_path
    (repo / "pyproject.toml").write_text('[project]\nname="fake"\n')
    (repo / "README.md").write_text("# fake\n")
    (repo / "docs").mkdir()
    (repo / "docs" / "canon.md").write_text(
        "Examples: `batch_ok`, `batch_failed`, `audit_inspect_ok`, "
        "`runpod_lifecycle_failed_cleaned`, `inference_span_unavailable`.\n"
    )
    findings = cvd.check_operational_category_drift(repo)
    assert findings == []


def test_d1_scans_python_string_literals(cvd, tmp_path):
    """A ``.py`` file with a non-canonical string-literal token is flagged.

    Codex on PR #118 pointed out that the original
    backtick-only regex left runtime literals (``"batch_ok"`` etc.)
    out of D1's reach. The check now scans Python string literals
    in .py files too — this test guards that path against
    regression.
    """

    repo = tmp_path
    (repo / "pyproject.toml").write_text('[project]\nname="fake"\n')
    (repo / "README.md").write_text("# fake\n")
    (repo / "docs").mkdir()
    src_dir = repo / "src" / "yomotsusaka" / "cli"
    src_dir.mkdir(parents=True)
    (src_dir / "operational_smoke.py").write_text(
        '"""Fake module."""\n'
        '_CAT_BAD = "batch_kaput"\n'
        '_CAT_FINE = "batch_ok"\n'
    )
    findings = cvd.check_operational_category_drift(repo)
    codes = {f.code for f in findings}
    assert "VOCAB_DRIFT_OP_CATEGORY" in codes
    drifted = [f for f in findings if f.code == "VOCAB_DRIFT_OP_CATEGORY"]
    assert any("batch_kaput" in f.message for f in drifted)
    # And the canonical literal is NOT flagged.
    assert not any("batch_ok" in f.message for f in drifted)


def test_d1_unrelated_prefix_ignored(cvd, tmp_path):
    """Tokens with prefixes outside CATEGORY_PREFIXES are out of scope."""

    repo = tmp_path
    (repo / "pyproject.toml").write_text('[project]\nname="fake"\n')
    (repo / "README.md").write_text("# fake\n")
    (repo / "docs").mkdir()
    (repo / "docs" / "off.md").write_text(
        "Out-of-scope tokens: `pipeline_failure`, `commit_phase_done`, "
        "`render_outcome`.\n"
    )
    findings = cvd.check_operational_category_drift(repo)
    assert findings == []


# ---------------------------------------------------------------------------
# D2 — exposure-class drift
# ---------------------------------------------------------------------------


def test_d2_flags_doc_token_not_in_exposure_set(cvd, tmp_path):
    """Simulate `EXPOSURE_CLASSES` dropping `agent_public`.

    The fixture references `agent_public`; we pass a restricted
    ``exposure_classes`` set that omits it. The check must emit
    ``VOCAB_DRIFT_EXPOSURE_CLASS``.
    """

    repo = _make_fake_repo(tmp_path, "vocab_drift_exposure.md", "exp.md")
    restricted = frozenset(
        {"agent_redacted", "private", "restricted", "never_expose"}
    )
    findings = cvd.check_exposure_class_drift(
        repo, exposure_classes=restricted
    )
    codes = {f.code for f in findings}
    assert "VOCAB_DRIFT_EXPOSURE_CLASS" in codes


def test_d2_warns_when_class_undocumented(cvd, tmp_path):
    """A class in EXPOSURE_CLASSES with no doc footprint emits a warning."""

    repo = tmp_path
    (repo / "pyproject.toml").write_text('[project]\nname="fake"\n')
    (repo / "README.md").write_text(
        "# fake\n\nMentions `agent_public` once.\n"
    )
    (repo / "docs").mkdir()
    # Pass a synthetic exposure class that the docs never mention.
    synthetic = frozenset({"agent_public", "ghost_class"})
    findings = cvd.check_exposure_class_drift(
        repo, exposure_classes=synthetic
    )
    codes = {f.code for f in findings}
    assert "VOCAB_DRIFT_EXPOSURE_UNDOCUMENTED" in codes
    undoc = [f for f in findings if f.code == "VOCAB_DRIFT_EXPOSURE_UNDOCUMENTED"]
    assert any("ghost_class" in f.message for f in undoc)


def test_d2_live_classes_documented(cvd):
    """Smoke: every live EXPOSURE_CLASS appears in the doc scan set.

    Guards against a future change that adds a class without
    updating any of README / AGENTS / docs/.
    """

    from yomotsusaka.boundary import EXPOSURE_CLASSES

    findings = cvd.check_exposure_class_drift(REPO_ROOT)
    # No undocumented-warning means every member is referenced
    # somewhere in the scan set.
    undoc = [
        f for f in findings if f.code == "VOCAB_DRIFT_EXPOSURE_UNDOCUMENTED"
    ]
    assert undoc == [], (
        f"some EXPOSURE_CLASSES members lack doc references: "
        f"{[f.message for f in undoc]} (members={sorted(EXPOSURE_CLASSES)})"
    )


def test_canonical_set_loaded_from_runtime(cvd):
    """D1 reads OperationalCategory live — adapts to any canonical set."""

    canonical = cvd._load_canonical_categories()
    # Sanity: the current canonical set is non-empty and contains
    # the BatchOk / BatchFailed shapes the scenario CLI emits.
    assert "batch_ok" in canonical
    assert "batch_failed" in canonical
