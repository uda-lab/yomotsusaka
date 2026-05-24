"""
Tests for ``scripts/gatekeeper/check_docs_links.py``.

Two flavours of test:

1. **Tip-of-main green test** — the production scan set
   (``README.md``, ``AGENTS.md``, ``docs/*.md``) resolves cleanly
   under ``--no-gh`` (offline mode). The ``gh``-based stale-umbrella
   check is intentionally not exercised in unit tests; that path is
   covered by the test-only ``gh`` seam in the failing-fixture cases.

2. **Failing-fixture tests** — for each seeded drift case under
   ``tests/fixtures/gatekeeper_drift/``, build a synthetic
   single-doc repo, invoke the targeted ``check_*`` function, and
   assert exactly the expected ``Finding`` shape.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "gatekeeper_drift"


def _load_check_docs_links():
    """Import ``scripts/gatekeeper/check_docs_links`` as a module.

    ``scripts/`` is not a Python package, so we use importlib.util to
    side-load it. Both the script and its ``_common`` helper are
    registered in :data:`sys.modules` so the dataclass machinery in
    ``_common.Finding`` can resolve forward references at import
    time.
    """

    gatekeeper_dir = REPO_ROOT / "scripts" / "gatekeeper"
    if str(gatekeeper_dir) not in sys.path:
        sys.path.insert(0, str(gatekeeper_dir))

    # Load _common first under its public name so Finding.__module__
    # resolves to a module that survives in sys.modules.
    if "_common" not in sys.modules:
        common_spec = importlib.util.spec_from_file_location(
            "_common", gatekeeper_dir / "_common.py"
        )
        assert common_spec and common_spec.loader
        common = importlib.util.module_from_spec(common_spec)
        sys.modules["_common"] = common
        common_spec.loader.exec_module(common)

    if "check_docs_links" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "check_docs_links", gatekeeper_dir / "check_docs_links.py"
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules["check_docs_links"] = module
        spec.loader.exec_module(module)
    return sys.modules["check_docs_links"]


@pytest.fixture(scope="module")
def cdl():
    return _load_check_docs_links()


# ---------------------------------------------------------------------------
# Tip-of-main green test
# ---------------------------------------------------------------------------


def test_tip_of_main_passes_offline(cdl, tmp_path):
    """Repo doc set resolves cleanly under --no-gh.

    Specifically:
    * No ``LINK_BROKEN`` / ``LINK_ANCHOR_MISSING`` findings.
    * No ``PRECEDENCE_CONTRADICTION`` findings.
    * The exit code (computed identically to ``main()``) is 0.
    """

    cache_path = tmp_path / "issue-cache.json"
    rc = cdl.main(
        [
            "--repo-root",
            str(REPO_ROOT),
            "--no-gh",
            "--cache-path",
            str(cache_path),
        ]
    )
    assert rc == 0


def test_tip_of_main_no_link_or_precedence_findings(cdl):
    """Lower-level assertion that link / precedence checks find nothing."""

    findings: list = []
    for doc in cdl.collect_docs(REPO_ROOT):
        for link in cdl.extract_links(doc):
            findings.extend(cdl.resolve_link(link, REPO_ROOT))
    findings.extend(cdl.check_precedence_consistency(REPO_ROOT))
    assert findings == [], f"unexpected findings: {findings}"


# ---------------------------------------------------------------------------
# Failing-fixture tests
# ---------------------------------------------------------------------------


def _make_fake_repo(tmp_path: Path, doc_name: str, fixture_name: str) -> Path:
    """Create a minimal repo skeleton with the chosen fixture.

    Layout:
        <tmp>/pyproject.toml   (empty; just satisfies `_find_repo_root`)
        <tmp>/README.md
        <tmp>/docs/<doc_name>
    """

    (tmp_path / "pyproject.toml").write_text('[project]\nname="fake"\n')
    (tmp_path / "README.md").write_text("# fake\n")
    (tmp_path / "docs").mkdir()
    fixture_text = (FIXTURES_DIR / fixture_name).read_text()
    (tmp_path / "docs" / doc_name).write_text(fixture_text)
    return tmp_path


def test_bad_link_fixture_flagged(cdl, tmp_path):
    repo = _make_fake_repo(tmp_path, "bad_link.md", "bad_link.md")
    findings: list = []
    for doc in cdl.collect_docs(repo):
        for link in cdl.extract_links(doc):
            findings.extend(cdl.resolve_link(link, repo))
    codes = {f.code for f in findings}
    assert "LINK_BROKEN" in codes
    # Targeting the right line is part of the contract.
    broken = [f for f in findings if f.code == "LINK_BROKEN"]
    assert any("does-not-exist.md" in f.message for f in broken)


def test_bad_anchor_fixture_flagged(cdl, tmp_path):
    # We need a target architecture.md in the synthetic repo so the
    # link's path-half resolves (and we exercise the anchor branch).
    repo = _make_fake_repo(tmp_path, "bad_anchor.md", "bad_anchor.md")
    (repo / "docs" / "architecture.md").write_text(
        "# Architecture\n\n## Some other heading\n"
    )
    findings: list = []
    for doc in cdl.collect_docs(repo):
        for link in cdl.extract_links(doc):
            findings.extend(cdl.resolve_link(link, repo))
    codes = {f.code for f in findings}
    assert "LINK_ANCHOR_MISSING" in codes


def test_precedence_contradiction_fixture_flagged(cdl, tmp_path):
    """Verify the precedence check on a README that asserts the inverse."""

    repo = tmp_path
    (repo / "pyproject.toml").write_text('[project]\nname="fake"\n')
    fixture_text = (FIXTURES_DIR / "precedence_contradiction.md").read_text()
    (repo / "README.md").write_text(fixture_text)
    (repo / "docs").mkdir()
    findings = cdl.check_precedence_consistency(repo)
    codes = {f.code for f in findings}
    assert "PRECEDENCE_CONTRADICTION" in codes


# ---------------------------------------------------------------------------
# Stale-umbrella check with injected gh seam
# ---------------------------------------------------------------------------


def test_stale_umbrella_fixture_flagged(cdl, tmp_path):
    """Inject a fake ``gh`` callable that reports issue #999 as CLOSED.

    The synthetic README references ``#999`` with present-tense
    'in progress' language; the check must emit
    ``STALE_UMBRELLA`` against that line.
    """

    repo = tmp_path
    (repo / "pyproject.toml").write_text('[project]\nname="fake"\n')
    (repo / "README.md").write_text(
        "# fake\n\nThe stale umbrella #999 is currently in progress.\n"
    )
    (repo / "docs").mkdir()
    cache_path = tmp_path / "issue-cache.json"

    def fake_gh(num: int) -> tuple[str, str]:
        if num == 999:
            return ("CLOSED", "fake umbrella")
        return ("", "")

    findings = cdl.check_stale_umbrellas(
        repo, cache_path=cache_path, gh=fake_gh
    )
    codes = {f.code for f in findings}
    assert "STALE_UMBRELLA" in codes
    stale = [f for f in findings if f.code == "STALE_UMBRELLA"]
    assert any("#999" in f.message for f in stale)


def test_stale_umbrella_open_does_not_flag(cdl, tmp_path):
    """An OPEN issue is never stale, even with active-claim language."""

    repo = tmp_path
    (repo / "pyproject.toml").write_text('[project]\nname="fake"\n')
    (repo / "README.md").write_text(
        "# fake\n\nThe umbrella #800 is currently in progress.\n"
    )
    (repo / "docs").mkdir()
    cache_path = tmp_path / "issue-cache.json"

    def fake_gh(num: int) -> tuple[str, str]:
        return ("OPEN", "still going")

    findings = cdl.check_stale_umbrellas(
        repo, cache_path=cache_path, gh=fake_gh
    )
    assert all(f.code != "STALE_UMBRELLA" for f in findings)


def test_cache_is_used_within_ttl(cdl, tmp_path):
    """Second invocation reads from the cache, gh not called again."""

    repo = tmp_path
    (repo / "pyproject.toml").write_text('[project]\nname="fake"\n')
    (repo / "README.md").write_text(
        "# fake\n\nThe stale umbrella #777 is currently active.\n"
    )
    (repo / "docs").mkdir()
    cache_path = tmp_path / "issue-cache.json"

    calls: list[int] = []

    def fake_gh(num: int) -> tuple[str, str]:
        calls.append(num)
        return ("CLOSED", "fake")

    cdl.check_stale_umbrellas(repo, cache_path=cache_path, gh=fake_gh)
    cdl.check_stale_umbrellas(repo, cache_path=cache_path, gh=fake_gh)
    assert calls == [777], "second run should hit the cache, not call gh"


def test_anchor_in_same_file(cdl, tmp_path):
    """Same-file ``#anchor`` resolution honors heading slugs."""

    repo = tmp_path
    (repo / "pyproject.toml").write_text('[project]\nname="fake"\n')
    (repo / "README.md").write_text("# fake\n")
    (repo / "docs").mkdir()
    (repo / "docs" / "self.md").write_text(
        "# Self\n\n## Some Heading\n\nSee [back](#some-heading).\n"
    )
    findings: list = []
    for doc in cdl.collect_docs(repo):
        for link in cdl.extract_links(doc):
            findings.extend(cdl.resolve_link(link, repo))
    assert findings == []


def test_external_links_skipped(cdl, tmp_path):
    repo = tmp_path
    (repo / "pyproject.toml").write_text('[project]\nname="fake"\n')
    (repo / "README.md").write_text(
        "# fake\n\nSee [uv](https://docs.astral.sh/uv/) and "
        "[mail](mailto:nobody@example.com).\n"
    )
    (repo / "docs").mkdir()
    findings: list = []
    for doc in cdl.collect_docs(repo):
        for link in cdl.extract_links(doc):
            findings.extend(cdl.resolve_link(link, repo))
    assert findings == []


def test_heading_slug_handles_numeric_prefix(cdl):
    assert cdl.iter_headings  # ensure import worked
    from _common import heading_slug  # type: ignore[import-not-found]

    assert heading_slug("13. Private Execution Gateway") == "13-private-execution-gateway"
    assert heading_slug("Source of truth precedence") == "source-of-truth-precedence"
    assert heading_slug("13.2 Initial implementation principle") == "132-initial-implementation-principle"


def test_main_json_emission(cdl, tmp_path):
    """--json writes a {findings: [...]} report."""

    import json as _json

    repo = tmp_path
    (repo / "pyproject.toml").write_text('[project]\nname="fake"\n')
    (repo / "README.md").write_text("# fake\n")
    (repo / "docs").mkdir()
    out = tmp_path / "report.json"
    cache_path = tmp_path / "issue-cache.json"
    rc = cdl.main(
        [
            "--repo-root",
            str(repo),
            "--no-gh",
            "--json",
            str(out),
            "--cache-path",
            str(cache_path),
        ]
    )
    assert rc == 0
    payload = _json.loads(out.read_text())
    assert payload == {"findings": []}
