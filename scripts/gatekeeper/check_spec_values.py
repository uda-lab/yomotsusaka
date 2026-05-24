"""Gate-keeper G2: Doc numeric spec vs code constants.

Parses ``<!-- spec-values target=X.y -->`` annotation blocks in
``docs/*.md`` files and asserts that the named Python attribute's
runtime value falls within the declared ``[min, max]`` range.

Format::

    <!-- spec-values target=PodConfig.disk_gb -->
    - min: 30
    - max: 50
    - practical: 40
    <!-- /spec-values -->

Rules
-----
G2.1 ``spec_values.block_parseable``
    Every ``<!-- spec-values ... -->`` block must be well-formed (has
    both ``min:`` and ``max:`` entries that are integers).
G2.2 ``spec_values.target_resolvable``
    The ``target=<Module>.<attr>`` reference must resolve to an
    *importable* attribute in the ``yomotsusaka`` package or in
    ``scripts/``.  An unresolvable target is itself drift evidence.
G2.3 ``spec_values.value_in_range``
    The resolved attribute value must satisfy ``min <= value <= max``.

Exit codes
----------
0 — no violations.
1 — at least one error-severity violation.
2 — internal error (import failure, parse error).

Invocation::

    uv run python scripts/gatekeeper/check_spec_values.py [--json]
        [--root PATH]
"""

from __future__ import annotations

import argparse
import importlib
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    """One spec-values drift detection."""

    rule: str
    severity: str  # "error" | "warning"
    file: str
    line: int
    target: str
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
# Block parsing
# ---------------------------------------------------------------------------

_OPEN_RE = re.compile(
    r"<!--\s*spec-values\s+target=(?P<target>[\w.]+)\s*-->",
    re.IGNORECASE,
)
_CLOSE_RE = re.compile(r"<!--\s*/spec-values\s*-->", re.IGNORECASE)
_FIELD_RE = re.compile(r"^\s*-\s*(?P<key>\w+)\s*:\s*(?P<value>.+?)\s*$")


@dataclass
class SpecBlock:
    """Parsed spec-values block."""

    target: str
    file: str
    open_line: int  # 1-indexed line where <!-- spec-values ... --> appears
    fields: dict[str, str]  # raw key→value from the block body


def _parse_blocks(text: str, rel_path: str) -> list[SpecBlock]:
    """Extract all spec-values blocks from *text*."""
    blocks: list[SpecBlock] = []
    lines = text.splitlines()
    in_block: SpecBlock | None = None
    for i, raw in enumerate(lines, start=1):
        if in_block is None:
            m = _OPEN_RE.search(raw)
            if m:
                in_block = SpecBlock(
                    target=m.group("target"),
                    file=rel_path,
                    open_line=i,
                    fields={},
                )
        else:
            if _CLOSE_RE.search(raw):
                blocks.append(in_block)
                in_block = None
            else:
                m2 = _FIELD_RE.match(raw)
                if m2:
                    in_block.fields[m2.group("key")] = m2.group("value")
    return blocks


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------

def _resolve_target(target: str, repo_root: Path) -> Any:
    """Attempt to import the named attribute and return its value.

    Supports two shapes:
    * ``Module.attr`` — resolved as ``yomotsusaka.<Module>.attr`` first,
      then bare ``<Module>.attr`` via module-level access.
    * ``ClassName.attr`` — instantiates ``ClassName()`` and reads the attr.

    Raises ``ImportError`` / ``AttributeError`` on failure.
    """
    parts = target.rsplit(".", 1)
    if len(parts) != 2:
        raise ValueError(f"target {target!r} must be of the form Module.attr")
    module_part, attr_part = parts

    # Try yomotsusaka.<module_part> first.
    # Insert repo_root/src into sys.path so the package is importable
    # when the script is run from the repo root.
    src_dir = str(repo_root / "src")
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)

    # Try to import from yomotsusaka package
    for candidate in (
        f"yomotsusaka.{module_part.lower()}",
        f"yomotsusaka.{module_part}",
        module_part,
    ):
        try:
            mod = importlib.import_module(candidate)
        except (ImportError, ModuleNotFoundError):
            continue
        # Look for the attr directly (module-level constant)
        if hasattr(mod, attr_part):
            return getattr(mod, attr_part)
        # Look for a class named module_part inside the module
        cls = getattr(mod, module_part, None)
        if cls is not None and isinstance(cls, type):
            try:
                instance = cls()
                return getattr(instance, attr_part)
            except Exception:
                pass
        # Look for the attr as a module-level name
        # (e.g. target=runpod_lifecycle.PodConfig.disk_gb handled as
        #  module=runpod_lifecycle, class=PodConfig, attr=disk_gb)

    raise ImportError(f"Cannot resolve target {target!r}")


def _resolve_class_attr(target: str, repo_root: Path) -> Any:
    """Resolve ``ClassName.attr`` by finding the class in yomotsusaka.*.

    This function tries a broader search: imports
    ``yomotsusaka.runpod_lifecycle`` (and similar candidate modules) and
    looks up the class by name.
    """
    parts = target.rsplit(".", 1)
    if len(parts) != 2:
        raise ValueError(f"target {target!r} must be of the form ClassName.attr")
    class_name, attr_name = parts

    src_dir = str(repo_root / "src")
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)

    # Walk yomotsusaka submodules listed via directory scan
    src_yomo = repo_root / "src" / "yomotsusaka"
    candidate_modules: list[str] = []
    if src_yomo.is_dir():
        for py in src_yomo.rglob("*.py"):
            rel = py.relative_to(repo_root / "src")
            mod_name = ".".join(rel.with_suffix("").parts)
            candidate_modules.append(mod_name)

    for mod_name in candidate_modules:
        try:
            mod = importlib.import_module(mod_name)
        except Exception:
            continue
        cls = getattr(mod, class_name, None)
        if cls is None or not isinstance(cls, type):
            continue
        try:
            instance = cls()
            val = getattr(instance, attr_name, None)
            if val is not None:
                return val
        except Exception:
            pass
        # Try class attribute directly
        val = getattr(cls, attr_name, None)
        if val is not None:
            return val

    raise ImportError(
        f"Cannot find class {class_name!r} with attr {attr_name!r} "
        f"in any yomotsusaka submodule"
    )


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_block(block: SpecBlock, repo_root: Path) -> Iterator[Finding]:
    """Validate one spec-values block."""
    # G2.1 — parseable: must have min and max as integers
    raw_min = block.fields.get("min")
    raw_max = block.fields.get("max")
    if raw_min is None or raw_max is None:
        yield Finding(
            rule="spec_values.block_parseable",
            severity="error",
            file=block.file,
            line=block.open_line,
            target=block.target,
            evidence=repr(block.fields),
            detail=(
                f"spec-values block for {block.target!r} is missing "
                f"'min' or 'max' field; found keys: {sorted(block.fields)}"
            ),
        )
        return

    try:
        spec_min = int(raw_min)
        spec_max = int(raw_max)
    except ValueError:
        yield Finding(
            rule="spec_values.block_parseable",
            severity="error",
            file=block.file,
            line=block.open_line,
            target=block.target,
            evidence=f"min={raw_min!r} max={raw_max!r}",
            detail=(
                f"spec-values block for {block.target!r}: "
                f"'min' and 'max' must be plain integers, "
                f"got min={raw_min!r} max={raw_max!r}"
            ),
        )
        return

    # G2.2 — target resolvable
    try:
        actual_value = _resolve_class_attr(block.target, repo_root)
    except (ImportError, AttributeError, ValueError) as exc:
        yield Finding(
            rule="spec_values.target_resolvable",
            severity="error",
            file=block.file,
            line=block.open_line,
            target=block.target,
            evidence=str(exc),
            detail=(
                f"spec-values target {block.target!r} cannot be resolved: {exc}"
            ),
        )
        return

    # G2.3 — value in range
    try:
        numeric_value = int(actual_value)
    except (TypeError, ValueError):
        yield Finding(
            rule="spec_values.value_in_range",
            severity="error",
            file=block.file,
            line=block.open_line,
            target=block.target,
            evidence=f"actual={actual_value!r}",
            detail=(
                f"spec-values target {block.target!r} resolved to "
                f"non-integer value {actual_value!r}; cannot compare to range"
            ),
        )
        return

    if not (spec_min <= numeric_value <= spec_max):
        yield Finding(
            rule="spec_values.value_in_range",
            severity="error",
            file=block.file,
            line=block.open_line,
            target=block.target,
            evidence=f"actual={numeric_value}, min={spec_min}, max={spec_max}",
            detail=(
                f"spec-values drift: {block.target!r} = {numeric_value} "
                f"is outside the documented range [{spec_min}, {spec_max}]; "
                f"update the code default or adjust the spec annotation"
            ),
        )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_checks(docs_dir: Path, repo_root: Path) -> Report:
    report = Report()
    if not docs_dir.is_dir():
        return report

    for md_file in sorted(docs_dir.glob("*.md")):
        text = md_file.read_text(encoding="utf-8")
        try:
            rel_path = str(md_file.relative_to(repo_root))
        except ValueError:
            rel_path = str(md_file)
        blocks = _parse_blocks(text, rel_path)
        report.files_scanned += 1
        for block in blocks:
            report.findings.extend(check_block(block, repo_root))

    return report


def _human_report(report: Report) -> str:
    lines: list[str] = [
        f"scanned {report.files_scanned} docs/*.md file(s); "
        f"{len(report.findings)} violation(s)"
    ]
    for f in report.findings:
        loc = f"{f.file}:{f.line}"
        lines.append(f"  [{f.severity}] {f.rule} @ {loc}")
        lines.append(f"    target:   {f.target}")
        lines.append(f"    evidence: {f.evidence}")
        lines.append(f"    detail:   {f.detail}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Gate-keeper G2: assert doc spec-values annotations match "
            "code constants."
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
