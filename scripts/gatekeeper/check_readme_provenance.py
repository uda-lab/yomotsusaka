#!/usr/bin/env python3
"""
gate-keeper check family E: README audience and provenance hygiene.

README.md must serve general readers. The patterns this script flags
are agent-workflow / implementation-diary residue that the issue #108
gate-keeper rule forbids:

* explicit GitHub issue or PR breadcrumbs (``issue #N``, ``PR #N``);
* bare ``#N`` outside URL anchors and Markdown anchor refs;
* MVP child / MVP umbrella references (``MVP-N child``, ``MVP-N umbrella``);
* the workflow-provenance term ``umbrella`` outside a documented
  changelog carve-out;
* workflow-provenance phrases ``owner decision``,
  ``agent-runnable dispatch``, ``introduced by``, ``child issue``,
  ``post hermes``.

The check operates exclusively on ``README.md`` because that is the
audience boundary defined by ``policy/repo-rules.md`` under the
``## README audience and provenance hygiene`` heading.

Edge-case carve-outs the regex layer implements before flagging a
match:

* URL anchors — link targets of shape ``](https://…#fragment)`` are
  stripped before scanning so the fragment cannot trigger ``#N``.
* Markdown intra-document anchor refs — ``[text](#section-slug)`` is
  stripped before scanning.
* The literal token ``agent`` remains a legitimate product-audience
  term; the rules target workflow-provenance phrasing, not the
  product-level term. None of the rules below match a bare
  ``agent`` token; the only ``agent``-prefixed phrase that is flagged
  is the very specific ``agent-runnable dispatch`` / ``agent runnable
  dispatch`` form.
* A ``Changelog`` or ``Release notes`` heading section, if present,
  is exempt — citing issue and PR numbers is the documented purpose
  of such a section.

Invoke:

```sh
uv run python scripts/gatekeeper/check_readme_provenance.py
uv run python scripts/gatekeeper/check_readme_provenance.py --json /tmp/readme.json
```

Exit codes follow the family convention:

* ``0`` — no findings.
* ``1`` — at least one ``error`` finding.
* ``2`` — only ``warning`` findings.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Make scripts/gatekeeper/_common.py importable when invoked directly.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402  (path manipulation above)
    Finding,
    emit_human_readable,
    exit_code_from,
    write_json_report,
)

__all__ = [
    "PATTERNS",
    "scan_readme",
    "main",
]


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------
#
# Each pattern is paired with a stable code (used in test assertions
# and machine consumers) and a short human-readable message template.
# The pattern is applied to a line AFTER URL anchors and Markdown
# anchor refs have been stripped (see ``_strip_anchors``).
#
# The order is:
#
#   1. ``issue #N`` / ``PR #N`` — the most specific, fires first so a
#      bare ``#N`` rule does not double-report.
#   2. ``MVP-N child`` / ``MVP-N umbrella`` — specific phrasing that
#      precedes the bare ``umbrella`` rule.
#   3. workflow-provenance phrases (``owner decision``,
#      ``agent-runnable dispatch``, ``introduced by``,
#      ``child issue``, ``post hermes``, bare ``umbrella``).
#   4. bare ``#N`` — last, so the more specific rules above claim
#      their matches first.


_ISSUE_PR_REF_RE = re.compile(r"(?i)\b(?:issue|PR)\s*#\d+\b")
_MVP_CHILD_RE = re.compile(r"(?i)\bMVP-\d+\s+(?:child|umbrella)\b")
_OWNER_DECISION_RE = re.compile(r"(?i)\bowner\s+decision\b")
_AGENT_RUNNABLE_DISPATCH_RE = re.compile(r"(?i)\bagent[- ]runnable\s+dispatch\b")
_INTRODUCED_BY_RE = re.compile(r"(?i)\bintroduced\s+by\b")
_CHILD_ISSUE_RE = re.compile(r"(?i)\bchild\s+issue\b")
_POST_HERMES_RE = re.compile(r"(?i)\bpost\s+hermes\b")
_UMBRELLA_RE = re.compile(r"(?i)\bumbrella\b")
_BARE_ISSUE_RE = re.compile(r"(?<![A-Za-z0-9_/])#\d+\b")


PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "README_PROVENANCE_ISSUE_PR_REF",
        _ISSUE_PR_REF_RE,
        "issue/PR breadcrumb is workflow provenance; remove or rephrase as current behaviour",
    ),
    (
        "README_PROVENANCE_MVP_CHILD",
        _MVP_CHILD_RE,
        "MVP child / umbrella reference is workflow provenance; remove or rephrase as current behaviour",
    ),
    (
        "README_PROVENANCE_OWNER_DECISION",
        _OWNER_DECISION_RE,
        "phrase `owner decision` is workflow provenance; move to AGENTS.md or an internal doc",
    ),
    (
        "README_PROVENANCE_AGENT_DISPATCH",
        _AGENT_RUNNABLE_DISPATCH_RE,
        "phrase `agent-runnable dispatch` is workflow provenance; remove or rephrase",
    ),
    (
        "README_PROVENANCE_INTRODUCED_BY",
        _INTRODUCED_BY_RE,
        "phrase `introduced by` is workflow provenance; describe current behaviour instead",
    ),
    (
        "README_PROVENANCE_CHILD_ISSUE",
        _CHILD_ISSUE_RE,
        "phrase `child issue` is workflow provenance; remove or rephrase",
    ),
    (
        "README_PROVENANCE_POST_HERMES",
        _POST_HERMES_RE,
        "phrase `post hermes` is workflow provenance; remove or rephrase",
    ),
    (
        "README_PROVENANCE_UMBRELLA",
        _UMBRELLA_RE,
        "term `umbrella` is workflow provenance; remove or move to a changelog section",
    ),
    (
        "README_PROVENANCE_BARE_ISSUE_NUMBER",
        _BARE_ISSUE_RE,
        "bare `#N` reads as an issue/PR breadcrumb; remove or rephrase as current behaviour",
    ),
)


# ---------------------------------------------------------------------------
# Anchor + carve-out stripping
# ---------------------------------------------------------------------------


# Inline Markdown links of the form `[text](target)` — `target` may
# contain `#fragment`. We strip the whole `(...)` segment so neither the
# bare-`#N` rule nor the issue-PR rule can match against the fragment.
_MD_LINK_TARGET_RE = re.compile(r"\]\([^)\s]+(?:\s+\"[^\"]*\")?\)")


def _strip_anchors(line: str) -> str:
    """Remove URL anchors and Markdown link targets from ``line``.

    After this step the residue still contains the link *text* (so
    workflow-provenance phrases inside link text are still flagged) but
    no longer contains the link target — meaning a URL like
    ``https://github.com/foo/bar/issues/89`` no longer trips the
    bare-``#N`` rule via its fragment, and a Markdown anchor ref like
    ``[Section](#section-slug)`` no longer trips it either.
    """

    return _MD_LINK_TARGET_RE.sub("](X)", line)


# Heading lines that open a documented carve-out section. Inside such
# a section the deterministic rules are silenced so a future
# ``## Changelog`` block can legitimately cite issue / PR numbers.
_CARVE_OUT_HEADINGS_RE = re.compile(
    r"^#{1,6}\s+(changelog|release notes)\b",
    re.IGNORECASE,
)

# Any heading at H1/H2/H3 level closes a previously-open carve-out
# section. (Nothing else qualifies as a section break in plain GFM.)
_ANY_HEADING_RE = re.compile(r"^#{1,6}\s+\S")

# Fenced code blocks are excluded from scanning. The README's prose is
# the audience surface; example commands and JSON payloads inside
# fences are allowed to reference numbers freely.
#
# Per CommonMark, a fence may be indented up to three spaces — common
# inside list-item continuation lines such as:
#
#     1. Run this:
#
#        ```sh
#        # example payload references issue #99 — allowed inside fence
#        ```
#
# The regex therefore accepts a leading ``{0,3}`` whitespace prefix
# before the fence characters. The captured ``fence`` group is the
# fence string itself (``` ``` ``` or ``~~~``) so the closing-fence
# matcher can compare against it directly.
_FENCE_RE = re.compile(r"^[ \t]{0,3}(?P<fence>```|~~~)")


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------


def scan_readme(readme_path: Path) -> list[Finding]:
    """Scan ``readme_path`` (typically ``README.md``) and return findings.

    The function is pure: it reads the file and returns a list of
    :class:`Finding` records. Callers compose the exit code via
    :func:`_common.exit_code_from`.

    The scan walks the file line-by-line, tracks whether the current
    line is inside a fenced code block or a carve-out heading section,
    and applies :data:`PATTERNS` against the anchor-stripped residue
    of each in-scope line.
    """

    rel = readme_path.name  # README.md is at repo root; keep the path short.
    findings: list[Finding] = []

    in_fence = False
    fence_char: str | None = None
    in_carve_out = False

    for line_no, raw in enumerate(readme_path.read_text().splitlines(), start=1):
        fence_match = _FENCE_RE.match(raw)
        if fence_match:
            matched_fence = fence_match.group("fence")
            if not in_fence:
                in_fence = True
                fence_char = matched_fence
                continue
            if fence_char and matched_fence == fence_char:
                # Close on a same-character fence. CommonMark allows the
                # closing fence to use a different leading indent than
                # the opener; the regex already permits 0–3 spaces, so
                # comparing the captured fence string suffices.
                in_fence = False
                fence_char = None
                continue
            # Mismatched fence inside a fence — treat as content.
        if in_fence:
            continue

        if _CARVE_OUT_HEADINGS_RE.match(raw):
            in_carve_out = True
            continue
        if in_carve_out and _ANY_HEADING_RE.match(raw):
            # A new heading closes the carve-out unless it's another
            # changelog-style heading (in which case the carve-out
            # continues seamlessly).
            in_carve_out = bool(_CARVE_OUT_HEADINGS_RE.match(raw))
            continue
        if in_carve_out:
            continue

        residue = _strip_anchors(raw)

        # Track ``(start, end)`` spans already claimed by a more
        # specific rule so the bare-``#N`` rule does not double-report
        # the same byte range.
        claimed: list[tuple[int, int]] = []

        for code, pattern, message in PATTERNS:
            for match in pattern.finditer(residue):
                span = match.span()
                if any(
                    span[0] >= c[0] and span[1] <= c[1] for c in claimed
                ):
                    continue
                claimed.append(span)
                findings.append(
                    Finding(
                        severity="error",
                        code=code,
                        file=rel,
                        line=line_no,
                        message=f"{message} (matched: {match.group(0)!r})",
                    )
                )

    return findings


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _find_repo_root(start: Path) -> Path:
    cur = start.resolve()
    for candidate in (cur, *cur.parents):
        if (candidate / "pyproject.toml").is_file() and (candidate / "README.md").is_file():
            return candidate
    raise SystemExit(
        f"check_readme_provenance.py: could not locate repo root from {start}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "README audience and provenance hygiene check — flags "
            "agent-workflow history, MVP/child issue provenance, "
            "PR/issue-number breadcrumbs, and similar implementation-"
            "diary residue in README.md."
        ),
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Repo root (default: walk up from this script).",
    )
    parser.add_argument(
        "--readme",
        type=Path,
        default=None,
        help="Override README path (default: <repo-root>/README.md).",
    )
    parser.add_argument(
        "--json",
        type=Path,
        default=None,
        help="Also write findings as JSON to this path.",
    )
    args = parser.parse_args(argv)

    repo_root = args.repo_root or _find_repo_root(Path(__file__).resolve().parent)
    readme = args.readme or (repo_root / "README.md")
    if not readme.is_file():
        sys.stderr.write(f"check_readme_provenance.py: README not found at {readme}\n")
        return 1

    findings = scan_readme(readme)
    sys.stdout.write(emit_human_readable(findings))
    if args.json:
        write_json_report(args.json, findings)

    return exit_code_from(findings)


if __name__ == "__main__":
    raise SystemExit(main())
