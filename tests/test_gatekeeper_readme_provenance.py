"""
Tests for ``scripts/gatekeeper/check_readme_provenance.py``.

Two flavours:

1. **Tip-of-main green test** — the production ``README.md`` resolves
   cleanly. This is the durable contract from issue #108: README must
   stay clean of agent-workflow provenance.

2. **Failing-fixture tests** — for each forbidden pattern, write a
   minimal synthetic README, invoke :func:`scan_readme`, and assert
   the expected ``Finding.code`` is present. Carve-outs (URL anchors,
   Markdown anchor refs, the ``Changelog`` heading) are exercised in a
   companion set of negative tests.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_check_readme_provenance():
    """Side-load ``scripts/gatekeeper/check_readme_provenance`` as a module.

    Mirrors the loader used by ``test_gatekeeper_docs_links.py`` so the
    helper script stays runnable both as a standalone CLI and as a
    pytest-imported module.
    """

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

    if "check_readme_provenance" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "check_readme_provenance",
            gatekeeper_dir / "check_readme_provenance.py",
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules["check_readme_provenance"] = module
        spec.loader.exec_module(module)
    return sys.modules["check_readme_provenance"]


@pytest.fixture(scope="module")
def crp():
    return _load_check_readme_provenance()


# ---------------------------------------------------------------------------
# Tip-of-main green test
# ---------------------------------------------------------------------------


def test_tip_of_main_readme_passes(crp):
    """The shipped ``README.md`` is clean.

    This is the durable acceptance criterion from issue #108: future
    PRs must not regress the README into an issue-log surface.
    """

    findings = crp.scan_readme(REPO_ROOT / "README.md")
    assert findings == [], f"unexpected findings: {findings}"


def test_main_returns_zero_on_tip(crp, capsys):
    """``main()`` exits 0 against the live tree."""

    rc = crp.main(["--repo-root", str(REPO_ROOT)])
    out = capsys.readouterr().out
    assert rc == 0, f"unexpected exit; stdout was:\n{out}"


# ---------------------------------------------------------------------------
# Failing-fixture tests — one per forbidden pattern
# ---------------------------------------------------------------------------


def _write_readme(tmp_path: Path, body: str) -> Path:
    """Write a synthetic README body and return its path."""

    readme = tmp_path / "README.md"
    readme.write_text(body, encoding="utf-8")
    return readme


@pytest.mark.parametrize(
    "body, expected_code",
    [
        # issue/PR breadcrumb
        (
            "# Title\n\nThis is shipped per issue #90.\n",
            "README_PROVENANCE_ISSUE_PR_REF",
        ),
        (
            "# Title\n\nLanded in PR #289.\n",
            "README_PROVENANCE_ISSUE_PR_REF",
        ),
        # MVP child / umbrella
        (
            "# Title\n\nThis came from MVP-5 child 03.\n",
            "README_PROVENANCE_MVP_CHILD",
        ),
        (
            "# Title\n\nDelivered under MVP-5 umbrella.\n",
            "README_PROVENANCE_MVP_CHILD",
        ),
        # Workflow-provenance phrases
        (
            "# Title\n\nClosure is an owner decision.\n",
            "README_PROVENANCE_OWNER_DECISION",
        ),
        (
            "# Title\n\nSee the agent-runnable dispatch flow.\n",
            "README_PROVENANCE_AGENT_DISPATCH",
        ),
        (
            "# Title\n\nThis API was introduced by the recent change.\n",
            "README_PROVENANCE_INTRODUCED_BY",
        ),
        (
            "# Title\n\nFiled as a child issue.\n",
            "README_PROVENANCE_CHILD_ISSUE",
        ),
        (
            "# Title\n\nDocumented post hermes refactor.\n",
            "README_PROVENANCE_POST_HERMES",
        ),
        (
            "# Title\n\nThis is the umbrella for the next slice.\n",
            "README_PROVENANCE_UMBRELLA",
        ),
        # Bare #N outside an anchor
        (
            "# Title\n\nResolved against #123.\n",
            "README_PROVENANCE_BARE_ISSUE_NUMBER",
        ),
    ],
)
def test_forbidden_patterns_flagged(crp, tmp_path, body, expected_code):
    """Each canonical forbidden pattern produces the expected finding."""

    readme = _write_readme(tmp_path, body)
    findings = crp.scan_readme(readme)
    codes = {f.code for f in findings}
    assert expected_code in codes, f"missing {expected_code}; got {codes}"


# ---------------------------------------------------------------------------
# Edge-case carve-outs — negative tests
# ---------------------------------------------------------------------------


def test_url_anchor_fragment_does_not_trigger(crp, tmp_path):
    """A ``#fragment`` inside a Markdown link target is exempt.

    Links such as ``[Section](https://example.com/page#L123)`` carry a
    numeric fragment that must not be interpreted as an issue
    reference.
    """

    body = (
        "# Title\n\n"
        "See [the spec](https://example.com/page#L123) for details.\n"
    )
    readme = _write_readme(tmp_path, body)
    findings = crp.scan_readme(readme)
    assert findings == [], f"unexpected findings on URL anchor: {findings}"


def test_markdown_anchor_ref_does_not_trigger(crp, tmp_path):
    """Intra-document anchor refs ``[text](#section-slug)`` are exempt."""

    body = (
        "# Title\n\n"
        "Jump to [Quickstart](#quickstart) below.\n"
    )
    readme = _write_readme(tmp_path, body)
    findings = crp.scan_readme(readme)
    assert findings == [], f"unexpected findings on anchor ref: {findings}"


def test_agent_as_product_term_does_not_trigger(crp, tmp_path):
    """Bare ``agent`` as a product-audience term is allowed.

    The only ``agent``-prefixed phrase that should flag is
    ``agent-runnable dispatch`` / ``agent runnable dispatch``. Plain
    product copy like ``agent workflows`` or ``agent-facing outputs``
    must pass.
    """

    body = (
        "# Title\n\n"
        "Yomotsusaka supports agent workflows and agent-facing outputs.\n"
    )
    readme = _write_readme(tmp_path, body)
    findings = crp.scan_readme(readme)
    assert findings == [], f"unexpected findings on product term: {findings}"


def test_changelog_section_is_exempt(crp, tmp_path):
    """A ``## Changelog`` (or ``## Release notes``) section is exempt.

    Citing issue and PR numbers in a release-notes block is the
    documented carve-out.
    """

    body = (
        "# Title\n\n"
        "Stable usage docs go here.\n\n"
        "## Changelog\n\n"
        "- 2026-05-24: shipped issue #108 cleanup and PR #999 integration.\n"
        "- 2026-05-23: closed MVP-5 umbrella.\n"
    )
    readme = _write_readme(tmp_path, body)
    findings = crp.scan_readme(readme)
    assert findings == [], f"unexpected findings under Changelog: {findings}"


def test_changelog_carveout_closes_at_next_heading(crp, tmp_path):
    """A new H1/H2/H3 outside the Changelog re-enables the checks."""

    body = (
        "# Title\n\n"
        "## Changelog\n\n"
        "- 2026-05-24: shipped issue #108.\n\n"
        "## Configuration\n\n"
        "Closure is an owner decision.\n"
    )
    readme = _write_readme(tmp_path, body)
    findings = crp.scan_readme(readme)
    codes = {f.code for f in findings}
    assert "README_PROVENANCE_OWNER_DECISION" in codes, (
        f"expected owner-decision finding after changelog close; got {codes}"
    )
    # The Changelog body itself remains exempt — no findings for
    # ``issue #108`` recorded against it.
    for f in findings:
        assert f.line != 4, (
            f"finding {f} should not have fired inside Changelog body"
        )


def test_fenced_block_is_exempt(crp, tmp_path):
    """Fenced code blocks are not scanned (example commands are free).

    Example shell or JSON inside a fence may legitimately reference
    numeric ids (port numbers, exit codes, sample payloads). The rules
    only fire on prose.
    """

    body = (
        "# Title\n\n"
        "Example:\n\n"
        "```\n"
        "# This example mentions issue #999 but it's inside a fence.\n"
        "```\n"
    )
    readme = _write_readme(tmp_path, body)
    findings = crp.scan_readme(readme)
    assert findings == [], f"unexpected findings inside fence: {findings}"


# ---------------------------------------------------------------------------
# Exit-code contract
# ---------------------------------------------------------------------------


def test_main_returns_one_on_failing_synthetic(crp, tmp_path):
    """``main()`` returns 1 against a README containing a forbidden pattern."""

    body = "# Title\n\nClosure is an owner decision.\n"
    readme = _write_readme(tmp_path, body)
    rc = crp.main(["--readme", str(readme), "--repo-root", str(REPO_ROOT)])
    assert rc == 1


def test_no_double_report_for_issue_pr_breadcrumb(crp, tmp_path):
    """``issue #N`` matches once, not also under the bare-#N rule.

    The pattern dispatch claims byte spans so the more specific rule
    wins and the bare-#N rule does not double-report.
    """

    body = "# Title\n\nShipped under issue #90.\n"
    readme = _write_readme(tmp_path, body)
    findings = crp.scan_readme(readme)
    codes = [f.code for f in findings]
    assert codes.count("README_PROVENANCE_ISSUE_PR_REF") == 1
    assert "README_PROVENANCE_BARE_ISSUE_NUMBER" not in codes
