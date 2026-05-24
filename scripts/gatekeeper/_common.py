"""
Shared utilities for the ``scripts/gatekeeper/`` doc-governance checks.

This module is intentionally small: it carries the :class:`Finding`
dataclass and a couple of helpers (heading slugging, doc collection,
JSON emission) that both ``check_docs_links.py`` and
``check_vocab_drift.py`` consume. It does NOT import any
``yomotsusaka.*`` runtime module — that responsibility stays with the
two leaf scripts so import failures stay localised to the surface they
affect.

Per issue #115 spec (the augmentation section, "Decision: split into
two scripts"), ``scripts/`` is **not** a package today. These files are
runnable scripts invoked via
``uv run python scripts/gatekeeper/<name>.py``; the shared helpers are
imported via path manipulation in the scripts themselves so we do not
accidentally turn ``scripts/`` into a package.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Literal

__all__ = [
    "Finding",
    "Severity",
    "collect_docs",
    "heading_slug",
    "iter_headings",
    "emit_human_readable",
    "write_json_report",
]


Severity = Literal["error", "warning"]


@dataclass(frozen=True)
class Finding:
    """One drift / link / vocabulary finding.

    Fields are deliberately public-safe: paths are repo-relative, line
    is 1-indexed, and ``message`` is a short human-readable summary
    that does not embed private values. Both gate-keeper checks emit
    only :class:`Finding` records — no raw doc bodies, no private
    dictionary content, no vault paths.
    """

    severity: Severity
    code: str
    file: str
    line: int
    message: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<title>.+?)\s*$")


def heading_slug(title: str) -> str:
    """Return the GitHub-style anchor slug for *title*.

    Mirrors the subset of GFM slugging used in this repository's
    architecture / scaffold-status / chikaeshi docs:

    * lowercase
    * drop punctuation except ``-`` and ``_``
    * collapse internal whitespace
    * whitespace → ``-``

    Numeric prefixes such as ``## 13. Private Execution Gateway``
    survive the punctuation strip ("13" stays) and the dot is removed,
    yielding ``13-private-execution-gateway`` — matching the on-disk
    anchor used in ``docs/chikaeshi.md`` / ``docs/backend-promotion.md``.
    """

    lowered = title.strip().lower()
    # Drop punctuation other than `-`, `_`, and whitespace. Whitespace
    # is collapsed below.
    stripped = "".join(
        ch if (ch.isalnum() or ch in "-_ \t") else "" for ch in lowered
    )
    # Collapse runs of whitespace into a single space, then map to `-`.
    collapsed = " ".join(stripped.split())
    return collapsed.replace(" ", "-")


def iter_headings(text: str) -> Iterable[tuple[int, str, str]]:
    """Yield ``(line_no, title, slug)`` for every Markdown heading in *text*.

    Skips ATX-style headings inside fenced code blocks (triple
    backticks). Reference-style and Setext headings (``===`` /
    ``---`` underlines) are not used in this repo's docs; if they
    appear later, this helper will silently miss them and an extra
    check should be added then.
    """

    in_fence = False
    for i, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.rstrip()
        if line.startswith("```") or line.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _HEADING_RE.match(line)
        if not m:
            continue
        title = m.group("title").strip()
        yield i, title, heading_slug(title)


def collect_docs(repo_root: Path) -> list[Path]:
    """Return the canonical doc scan set in deterministic order.

    The set is fixed per the issue #115 spec to avoid globbing
    surprises: ``README.md``, ``AGENTS.md``, and every
    ``docs/*.md`` (sorted by name). Returns absolute paths.
    """

    out: list[Path] = []
    for name in ("README.md", "AGENTS.md"):
        p = repo_root / name
        if p.is_file():
            out.append(p)
    docs_dir = repo_root / "docs"
    if docs_dir.is_dir():
        out.extend(sorted(p for p in docs_dir.glob("*.md") if p.is_file()))
    return out


def emit_human_readable(findings: list[Finding]) -> str:
    """Render *findings* as a human-readable summary table.

    The intent is operator-facing terminal output, not machine
    consumption — for that, use :func:`write_json_report`.
    """

    if not findings:
        return "OK — no findings.\n"
    lines = [
        f"{len(findings)} finding(s):",
        "",
        f"{'SEVERITY':<8} {'CODE':<30} {'LOCATION':<48} MESSAGE",
        f"{'-' * 8} {'-' * 30} {'-' * 48} {'-' * 7}",
    ]
    for f in findings:
        loc = f"{f.file}:{f.line}"
        lines.append(f"{f.severity:<8} {f.code:<30} {loc:<48} {f.message}")
    lines.append("")
    return "\n".join(lines)


def write_json_report(path: Path, findings: list[Finding]) -> None:
    """Write ``{"findings": [...]}`` JSON for downstream consumption.

    The JSON shape is deliberately minimal: a single ``findings`` key
    holding the list of finding dicts. Downstream tooling that wants
    counters or aggregates can compute them off this list.
    """

    payload = {"findings": [f.to_dict() for f in findings]}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def exit_code_from(findings: list[Finding]) -> int:
    """Compute the standard exit-code contract.

    * ``0`` — no findings.
    * ``1`` — at least one ``severity="error"`` finding.
    * ``2`` — only ``severity="warning"`` findings.
    """

    if not findings:
        return 0
    if any(f.severity == "error" for f in findings):
        return 1
    return 2
