"""Gate-keeper G3: Every documented env-var must be wired in source code.

Parses env-var table rows from ``docs/*.md`` files.  For every variable
name in the table, asserts that ``os.environ.get("<NAME>")`` or
``os.getenv("<NAME>")`` appears at least once in ``src/`` or
top-level ``scripts/``.

The env-var table format is the Markdown pipe-table shape used in
``docs/runpod-agent-lifecycle.md`` §2:

    | Variable             | Required | Source ... |
    | -------------------- | -------- | ---------- |
    | `RUNPOD_API_KEY`     | yes      | ...        |
    | `RUNPOD_TEMPLATE_ID` | optional | ...        |

Rules
-----
G3.1 ``documented_env_vars.wired_in_source``
    Every ``| `VAR_NAME` | ... |`` row in a docs env-var table must have
    a corresponding ``os.environ.get("VAR_NAME")`` or
    ``os.getenv("VAR_NAME")`` call in ``src/`` or ``scripts/``.
    Rows with ``(operator-only)`` annotation in the Source column are
    exempt (they are intentionally not consumed by agent code).

Scope
-----
Docs scanned: all ``docs/*.md`` files.
Source scanned: ``src/`` (recursive) + top-level ``scripts/*.py``.

Exit codes
----------
0 — no violations.
1 — at least one error-severity violation.
2 — internal error.

Invocation::

    uv run python scripts/gatekeeper/check_documented_env_vars.py [--json]
        [--root PATH]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator, Sequence


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    rule: str
    severity: str
    file: str
    line: int
    var_name: str
    evidence: str
    detail: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


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
            "findings": [f.to_dict() for f in self.findings],
        }


# ---------------------------------------------------------------------------
# Env-var table parsing
# ---------------------------------------------------------------------------

# Match a Markdown pipe-table row where the first cell is a backtick-quoted
# env-var name (ALL_CAPS_WITH_UNDERSCORES). Examples:
#   | `RUNPOD_API_KEY` | yes | ... |
#   | `RUNPOD_TEMPLATE_ID` | optional | ... |
_TABLE_ROW_RE = re.compile(
    r"^\s*\|\s*`(?P<var>[A-Z][A-Z0-9_]{2,})`\s*\|(?P<rest>[^|]+\|.*)?$"
)

# Annotation that marks a row as operator-only (not consumed by agent code).
_OPERATOR_ONLY_MARKER = "(operator-only)"


@dataclass
class EnvVarEntry:
    """One documented env-var."""

    var_name: str
    doc_file: str
    line: int
    operator_only: bool


def _parse_env_var_tables(text: str, rel_path: str) -> list[EnvVarEntry]:
    """Extract env-var table rows from *text*."""
    entries: list[EnvVarEntry] = []
    for i, raw in enumerate(text.splitlines(), start=1):
        m = _TABLE_ROW_RE.match(raw)
        if m:
            var_name = m.group("var")
            rest = m.group("rest") or ""
            # Skip separator rows (e.g. | --- | --- |)
            if re.match(r"^\s*-+\s*$", var_name):
                continue
            # Skip header rows (e.g. `Variable`)
            if var_name.lower() in ("variable", "name", "var"):
                continue
            # Only include ALL_CAPS env-var names
            if not re.match(r"^[A-Z][A-Z0-9_]+$", var_name):
                continue
            operator_only = _OPERATOR_ONLY_MARKER in rest
            entries.append(
                EnvVarEntry(
                    var_name=var_name,
                    doc_file=rel_path,
                    line=i,
                    operator_only=operator_only,
                )
            )
    return entries


# ---------------------------------------------------------------------------
# Source code scanning
# ---------------------------------------------------------------------------


def _build_source_env_lookup(src_paths: list[Path]) -> set[str]:
    """Return the set of env-var names referenced in the scanned source."""
    # Match os.environ.get("VAR") / os.getenv("VAR") / os.environ["VAR"]
    # We accept both single and double quotes.
    _ENV_GET_RE = re.compile(
        r"""os\.(?:environ\.get|getenv)\(\s*['"](?P<var>[A-Z][A-Z0-9_]+)['"]\s*[,)]"""
        r"""|os\.environ\[\s*['"](?P<var2>[A-Z][A-Z0-9_]+)['"]\s*\]"""
    )
    referenced: set[str] = set()
    for path in src_paths:
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for m in _ENV_GET_RE.finditer(source):
            var = m.group("var") or m.group("var2")
            if var:
                referenced.add(var)
    return referenced


def _collect_source_files(repo_root: Path) -> list[Path]:
    """Collect Python source files from src/ and top-level scripts/."""
    files: list[Path] = []
    src_dir = repo_root / "src"
    if src_dir.is_dir():
        files.extend(sorted(src_dir.rglob("*.py")))
    scripts_dir = repo_root / "scripts"
    if scripts_dir.is_dir():
        for py in sorted(scripts_dir.glob("*.py")):
            files.append(py)
    return files


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_env_vars(
    entries: list[EnvVarEntry],
    referenced_vars: set[str],
) -> Iterator[Finding]:
    """Yield G3.1 findings for undocumented env-vars."""
    for entry in entries:
        if entry.operator_only:
            continue
        if entry.var_name in referenced_vars:
            continue
        yield Finding(
            rule="documented_env_vars.wired_in_source",
            severity="error",
            file=entry.doc_file,
            line=entry.line,
            var_name=entry.var_name,
            evidence="not found in src/ or scripts/ as os.environ.get/os.getenv",
            detail=(
                f"Documented env-var {entry.var_name!r} (from {entry.doc_file}:{entry.line}) "
                f"has no os.environ.get or os.getenv usage in src/ or scripts/. "
                f"Either wire the variable in code or add '(operator-only)' to "
                f"the table row if it is exclusively consumed by the owner."
            ),
        )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_checks(docs_dir: Path, repo_root: Path) -> Report:
    report = Report()
    if not docs_dir.is_dir():
        return report

    # Collect all documented env-vars from docs/*.md
    all_entries: list[EnvVarEntry] = []
    for md_file in sorted(docs_dir.glob("*.md")):
        report.files_scanned += 1
        try:
            text = md_file.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            rel_path = str(md_file.relative_to(repo_root))
        except ValueError:
            rel_path = str(md_file)
        all_entries.extend(_parse_env_var_tables(text, rel_path))

    # Build the set of env-vars referenced in source
    source_files = _collect_source_files(repo_root)
    referenced = _build_source_env_lookup(source_files)

    # Deduplicate: only report each var_name once (across all docs)
    seen: set[str] = set()
    for entry in all_entries:
        if entry.var_name in seen:
            continue
        seen.add(entry.var_name)
        report.findings.extend(check_env_vars([entry], referenced))

    return report


def _human_report(report: Report) -> str:
    lines: list[str] = [
        f"scanned {report.files_scanned} docs/*.md file(s); "
        f"{len(report.findings)} violation(s)"
    ]
    for f in report.findings:
        loc = f"{f.file}:{f.line}"
        lines.append(f"  [{f.severity}] {f.rule} @ {loc}")
        lines.append(f"    var:      {f.var_name}")
        lines.append(f"    evidence: {f.evidence}")
        lines.append(f"    detail:   {f.detail[:140]}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Gate-keeper G3: assert every documented env-var is wired in source."
        )
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Repository root (default: two levels up from scripts/gatekeeper/).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON to stdout.",
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
    docs_dir = repo_root / "docs"

    try:
        report = run_checks(docs_dir, repo_root)
    except Exception as exc:
        print(f"internal-error: {exc!r}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report.to_json(), indent=2, sort_keys=True))
    else:
        print(_human_report(report))

    if args.json_out:
        args.json_out.write_text(
            json.dumps(report.to_json(), indent=2, sort_keys=True),
            encoding="utf-8",
        )

    return 1 if report.findings else 0


if __name__ == "__main__":  # pragma: no cover - thin CLI shim
    raise SystemExit(main())
