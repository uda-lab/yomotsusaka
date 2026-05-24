"""Gate-keeper: AGENTS.md hygiene checks.

This script enforces that ``AGENTS.md`` stays a minimal, stale-resistant
control surface for coding agents. It complements
``check_docs_commands.py`` (docs-to-source drift) with three deterministic
checks scoped specifically to ``AGENTS.md``:

Rules
    R1 ``agents_md.visible_line_cap`` — count of "visible" lines in
       ``AGENTS.md`` must be at most ``--max-lines`` (default 15).
       "Visible" means non-blank, non-HTML-comment lines. HTML comments
       (``<!-- ... -->``) and blank lines do not count.
    R2 ``agents_md.no_issue_pr_mvp_provenance`` — ``AGENTS.md`` must not
       contain issue/PR/MVP provenance tokens. The regex set covers
       ``#<digits>``, ``MVP-<digits>``, ``PR <digits>``, and
       ``PR #<digits>``. Plain Markdown anchors (``#section``) and
       hash-prefixed shell comments are excluded by requiring a digit
       immediately after the ``#``.
    R3 ``agents_md.docs_references_resolve`` — every ``docs/<file>``
       reference inside ``AGENTS.md`` must resolve to an existing path
       on disk. (Family A3 in ``check_docs_commands.py`` covers the
       same family across all docs; we mirror it here so AGENTS.md
       hygiene stays self-contained and the rule reports under the
       ``agents_md.*`` namespace.)

Exit codes:
    0 — clean
    1 — at least one violation
    2 — internal error (file-read failure, unexpected exception)

Invocation::

    uv run python scripts/gatekeeper/check_agents_md.py [--json]
        [--json-out PATH] [--root PATH] [--target PATH]
        [--max-lines N]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator, Sequence


DEFAULT_MAX_VISIBLE_LINES = 15


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    """One drift detection."""

    rule: str
    severity: str
    path: str
    line: int
    evidence: str
    detail: str


@dataclass
class Report:
    files_scanned: int = 0
    findings: list[Finding] = field(default_factory=list)

    def to_json(self) -> dict[str, object]:
        return {
            "version": 1,
            "summary": {
                "files_scanned": self.files_scanned,
                "violations": len(self.findings),
            },
            "findings": [asdict(f) for f in self.findings],
        }


# ---------------------------------------------------------------------------
# Visibility classification
# ---------------------------------------------------------------------------

# A line is an HTML comment iff its trimmed content begins with ``<!--`` and
# ends with ``-->`` on the same line. Multi-line HTML comments are uncommon
# in AGENTS.md and intentionally not excluded — keeping the rule single-line
# avoids parser complexity and makes the count easy to audit by eye.
_HTML_COMMENT_RE = re.compile(r"^\s*<!--.*-->\s*$")


def _is_visible(line: str) -> bool:
    if not line.strip():
        return False
    if _HTML_COMMENT_RE.match(line):
        return False
    return True


def count_visible_lines(text: str) -> int:
    return sum(1 for line in text.splitlines() if _is_visible(line))


# ---------------------------------------------------------------------------
# Provenance token detection
# ---------------------------------------------------------------------------

# ``#<digits>`` — issue/PR numbers. We require at least one digit after the
# hash to exclude Markdown anchor links like ``#section`` and shell
# comments. We also require the ``#`` not be preceded by an alphanumeric
# (so ``foo#123`` is not flagged but ``Closes #123`` is).
_ISSUE_PR_HASH_RE = re.compile(r"(?<![A-Za-z0-9])#(\d+)")

# ``MVP-<digits>`` — MVP slice references.
_MVP_RE = re.compile(r"\bMVP-(\d+)\b")

# ``PR <digits>`` or ``PR #<digits>`` — pull-request provenance written
# out long-form. The regex is anchored on the literal ``PR`` token.
_PR_LONGFORM_RE = re.compile(r"\bPR\s+#?(\d+)\b")


_PROVENANCE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("issue_pr_hash", _ISSUE_PR_HASH_RE),
    ("mvp_slice", _MVP_RE),
    ("pr_longform", _PR_LONGFORM_RE),
]


# ---------------------------------------------------------------------------
# docs/ path reference detection
# ---------------------------------------------------------------------------

# Match ``docs/<path>`` references in AGENTS.md. We accept the path inside
# backticks, parentheses, brackets, or plain prose. The capture ends at the
# first whitespace, backtick, closing bracket/paren, comma, or semicolon.
_DOCS_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_/])(?P<path>docs/[A-Za-z0-9_./\-]+)"
)


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_visible_line_cap(
    text: str, target_rel: str, max_lines: int
) -> Iterator[Finding]:
    count = count_visible_lines(text)
    if count <= max_lines:
        return
    yield Finding(
        rule="agents_md.visible_line_cap",
        severity="error",
        path=target_rel,
        line=0,
        evidence=f"visible_lines={count}",
        detail=(
            f"AGENTS.md has {count} visible lines (max {max_lines}); "
            "trim or move detail to docs/"
        ),
    )


def check_no_provenance(text: str, target_rel: str) -> Iterator[Finding]:
    seen: set[tuple[str, str, int]] = set()
    for idx, raw in enumerate(text.splitlines(), start=1):
        # Skip HTML-comment lines so an intentional permanent directive
        # comment that references an issue cannot be added later by
        # accident — but our checks should not flag pure structural
        # markers. We intentionally still scan them; the absence of any
        # legitimate use of ``#<digits>`` in AGENTS.md is the whole point.
        for kind, pattern in _PROVENANCE_PATTERNS:
            for m in pattern.finditer(raw):
                token = m.group(0)
                key = (kind, token, idx)
                if key in seen:
                    continue
                seen.add(key)
                yield Finding(
                    rule="agents_md.no_issue_pr_mvp_provenance",
                    severity="error",
                    path=target_rel,
                    line=idx,
                    evidence=token,
                    detail=(
                        f"AGENTS.md must not contain issue/PR/MVP "
                        f"provenance tokens; found {token!r}"
                    ),
                )


def check_docs_references_resolve(
    text: str, target_rel: str, repo_root: Path
) -> Iterator[Finding]:
    seen: set[str] = set()
    for idx, raw in enumerate(text.splitlines(), start=1):
        for m in _DOCS_PATH_RE.finditer(raw):
            raw_path = m.group("path").rstrip(".,);:\"'`")
            file_part = raw_path.split("#", 1)[0]
            if not file_part or file_part in seen:
                continue
            if any(ch in file_part for ch in ("*", "?", "$")):
                continue
            target = repo_root / file_part
            if target.exists():
                continue
            seen.add(file_part)
            yield Finding(
                rule="agents_md.docs_references_resolve",
                severity="error",
                path=target_rel,
                line=idx,
                evidence=file_part,
                detail=f"docs/ reference does not resolve: {file_part}",
            )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_checks(
    target: Path, repo_root: Path, max_lines: int
) -> Report:
    report = Report()
    text = target.read_text(encoding="utf-8")
    report.files_scanned = 1
    try:
        target_rel = str(target.resolve().relative_to(repo_root.resolve()))
    except ValueError:
        target_rel = str(target)

    report.findings.extend(
        check_visible_line_cap(text, target_rel, max_lines)
    )
    report.findings.extend(check_no_provenance(text, target_rel))
    report.findings.extend(
        check_docs_references_resolve(text, target_rel, repo_root)
    )
    return report


def _human_report(report: Report, max_lines: int) -> str:
    lines: list[str] = []
    lines.append(
        f"scanned {report.files_scanned} file(s); "
        f"max-visible-lines={max_lines}; "
        f"{len(report.findings)} violation(s)"
    )
    for f in report.findings:
        loc = f"{f.path}:{f.line}" if f.line else f.path
        lines.append(f"  [{f.severity}] {f.rule} @ {loc}")
        lines.append(f"    evidence: {f.evidence}")
        lines.append(f"    detail:   {f.detail}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Gate-keeper: AGENTS.md hygiene checks. Enforces a "
            "visible-line cap, forbids issue/PR/MVP provenance tokens, "
            "and verifies docs/ references resolve."
        )
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Repository root (default: parent of scripts/gatekeeper/).",
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=None,
        help="Path to AGENTS.md (default: <root>/AGENTS.md).",
    )
    parser.add_argument(
        "--max-lines",
        type=int,
        default=DEFAULT_MAX_VISIBLE_LINES,
        help=(
            "Maximum visible (non-blank, non-HTML-comment) lines "
            f"(default: {DEFAULT_MAX_VISIBLE_LINES})."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON to stdout (suppresses human report).",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="Also write JSON report to this path.",
    )
    args = parser.parse_args(argv)

    repo_root = (
        args.root.resolve()
        if args.root
        else Path(__file__).resolve().parents[2]
    )
    target = (
        args.target.resolve()
        if args.target
        else (repo_root / "AGENTS.md").resolve()
    )

    if not target.exists():
        print(f"internal-error: target does not exist: {target}", file=sys.stderr)
        return 2

    try:
        report = run_checks(target, repo_root, args.max_lines)
    except OSError as exc:
        print(f"internal-error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover - defensive
        print(f"internal-error: {exc!r}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report.to_json(), indent=2, sort_keys=True))
    else:
        print(_human_report(report, args.max_lines))

    if args.json_out:
        args.json_out.write_text(
            json.dumps(report.to_json(), indent=2, sort_keys=True),
            encoding="utf-8",
        )

    return 1 if report.findings else 0


if __name__ == "__main__":  # pragma: no cover - thin CLI shim
    raise SystemExit(main())
