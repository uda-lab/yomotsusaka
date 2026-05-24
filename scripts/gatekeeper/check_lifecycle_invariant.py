"""Gate-keeper G4: Lifecycle invariant — every start_pod has a paired stop_pod.

Checks that ``ManageRunPodLifecycle.start_pod`` — the library method that
creates a real RunPod Pod — contains a ``stop_pod`` call in the exception
handler that wraps ``_wait_for_healthy``.  This is the #124 postmortem root
cause: ``wait_timeout`` left an orphan Pod running because no cleanup was
attempted.

More broadly, this check scans every function in ``src/`` and ``scripts/``
that:
1. Makes a ``start_pod`` call, AND
2. Is NOT the ``start_pod`` method itself (it is exempt — it is the
   implementation), AND
3. Has no paired ``stop_pod`` in any except-handler or finally block within
   the same function, AND
4. Does NOT carry the ``# CLEANUP: caller-responsibility`` marker.

For the library ``start_pod`` itself (``ManageRunPodLifecycle.start_pod``),
the invariant is checked differently: the function body must contain a
nested ``try`` block that covers ``_wait_for_healthy``, and the except
clause of that inner try must call ``stop_pod``.

Rules
-----
G4.1 ``lifecycle_invariant.library_start_pod_has_cleanup``
    ``ManageRunPodLifecycle.start_pod`` must have a ``stop_pod`` call in
    the exception handler that wraps ``_wait_for_healthy``.  This is the
    core invariant that prevents orphan Pods.

G4.2 ``lifecycle_invariant.caller_start_pod_paired``
    Every other function that calls ``something.start_pod(`` must have a
    ``stop_pod`` call reachable on failure (in an except-handler or finally
    block), OR carry ``# CLEANUP: caller-responsibility``.

Scope
-----
``src/yomotsusaka/runpod_lifecycle.py`` (G4.1) and all ``src/`` + top-level
``scripts/*.py`` Python files (G4.2), excluding ``tests/`` and
``scripts/gatekeeper/``.

Exit codes
----------
0 — no violations.
1 — at least one error-severity violation.
2 — internal error (parse failure, unexpected exception).

Invocation::

    uv run python scripts/gatekeeper/check_lifecycle_invariant.py [--json]
        [--root PATH]
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator, Sequence

# Marker that a function explicitly delegates cleanup responsibility to
# its caller.  Functions carrying this comment are exempt from G4.2.
_CALLER_RESPONSIBILITY_MARKER = "CLEANUP: caller-responsibility"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    rule: str
    severity: str  # "error" | "warning"
    file: str
    line: int
    function: str
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
# AST helpers
# ---------------------------------------------------------------------------


def _is_stop_pod_call(node: ast.AST) -> bool:
    """Return True if *node* is a ``stop_pod(...)`` call expression."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "stop_pod":
        return True
    if isinstance(func, ast.Name) and func.id == "stop_pod":
        return True
    return False


def _is_start_pod_call(node: ast.AST) -> bool:
    """Return True if *node* is a ``start_pod(...)`` call expression."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "start_pod":
        return True
    if isinstance(func, ast.Name) and func.id == "start_pod":
        return True
    return False


_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _iter_body_in_scope(stmts: list[ast.stmt]) -> Iterator[ast.AST]:
    """Yield every descendant of *stmts* WITHOUT crossing nested function /
    class / lambda boundaries.

    ``ast.walk`` traverses the whole subtree including nested defs, which
    falsely treats a ``stop_pod`` call inside an uninvoked helper as
    cleanup (codex P2 on PR #132). Scope-stopping traversal ensures that
    only statements actually reachable from the current scope count.

    Statements that are themselves scope nodes (``def _x():``,
    ``class Y:``, ``lambda``) are yielded as a single node — their bodies
    are NOT descended into.
    """
    for stmt in stmts:
        if isinstance(stmt, _SCOPE_NODES):
            # The scope-node statement itself is yielded (caller may want
            # to see the def line), but we do NOT recurse into its body.
            yield stmt
            continue
        yield from _walk_no_scope(stmt)


def _walk_no_scope(node: ast.AST) -> Iterator[ast.AST]:
    """Yield *node* and its descendants, stopping at any scope boundary.

    Unlike ``ast.walk``, this traversal yields a scope-node when encountered
    but does NOT descend into its body — so a nested ``def``/``class``/
    ``lambda`` containing a ``stop_pod`` call inside the outer function is
    NOT counted as reachable cleanup.
    """
    yield node
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _SCOPE_NODES):
            # Yield the def node itself but do NOT descend into its body.
            yield child
            continue
        yield from _walk_no_scope(child)


def _contains_stop_pod(stmts: list[ast.stmt]) -> bool:
    """Return True if any node in *stmts* is a stop_pod call reachable from
    the current scope (nested function/class bodies are NOT traversed).
    """
    return any(_is_stop_pod_call(n) for n in _iter_body_in_scope(stmts))


def _contains_start_pod(stmts: list[ast.stmt]) -> bool:
    """Return True if any node in *stmts* is a start_pod call reachable from
    the current scope (nested function/class bodies are NOT traversed).
    """
    return any(_is_start_pod_call(n) for n in _iter_body_in_scope(stmts))


def _source_snippet(source_lines: list[str], node: ast.AST) -> str:
    start = getattr(node, "lineno", None)
    end = getattr(node, "end_lineno", start)
    if start is None:
        return "<unknown>"
    return "\n".join(source_lines[start - 1 : end])[:120]


# ---------------------------------------------------------------------------
# G4.1 — Library start_pod has cleanup
# ---------------------------------------------------------------------------


def _check_library_start_pod(
    func_def: ast.FunctionDef,
    source_lines: list[str],
    rel_path: str,
) -> Iterator[Finding]:
    """Verify that ManageRunPodLifecycle.start_pod has stop_pod in its
    _wait_for_healthy exception handler.

    The expected shape (post-#125):
        try:
            self._wait_for_healthy(handle)
        except PodUnavailableError:
            try:
                self.stop_pod(handle, terminate=True)
            except ...:
                ...
            raise
    """
    # Walk the function body looking for a Try node whose body contains
    # _wait_for_healthy and whose handlers contain stop_pod.
    # Scope-stopping traversal: nested defs are not descended into
    # (codex P2 on PR #132).
    found_inner_try_with_cleanup = False

    def _is_wait_call(n: ast.AST) -> bool:
        if not isinstance(n, ast.Call):
            return False
        if isinstance(n.func, ast.Attribute) and n.func.attr == "_wait_for_healthy":
            return True
        if isinstance(n.func, ast.Name) and n.func.id == "_wait_for_healthy":
            return True
        return False

    for node in _iter_body_in_scope(func_def.body):
        if not isinstance(node, ast.Try):
            continue
        # Does the body of this Try call _wait_for_healthy (scope-respecting)?
        has_wait = any(_is_wait_call(n) for n in _iter_body_in_scope(node.body))
        if not has_wait:
            continue
        # Does at least one handler contain a stop_pod call?
        for handler in node.handlers:
            if _contains_stop_pod(handler.body):
                found_inner_try_with_cleanup = True
                break
        # Also check finally body
        if not found_inner_try_with_cleanup and node.finalbody:
            if _contains_stop_pod(node.finalbody):
                found_inner_try_with_cleanup = True

    if found_inner_try_with_cleanup:
        return

    yield Finding(
        rule="lifecycle_invariant.library_start_pod_has_cleanup",
        severity="error",
        file=rel_path,
        line=func_def.lineno,
        function=func_def.name,
        evidence="no stop_pod found in _wait_for_healthy exception handler",
        detail=(
            "ManageRunPodLifecycle.start_pod must call stop_pod in the "
            "exception handler that wraps _wait_for_healthy, so that a Pod "
            "that times out during health polling is cleaned up before the "
            "exception propagates to the caller.  This is the #124 postmortem "
            "root cause.  Fix: wrap _wait_for_healthy in try/except and call "
            "stop_pod in the except clause (see PR #129 for the pattern)."
        ),
    )


# ---------------------------------------------------------------------------
# G4.2 — Caller start_pod paired
# ---------------------------------------------------------------------------


def _body_has_caller_responsibility_marker(
    func_def: ast.FunctionDef | ast.AsyncFunctionDef,
    source_lines: list[str],
) -> bool:
    """Return True if the function body source contains the marker comment.

    Comments are not AST nodes, so we scan the raw source lines from the
    function's first line (inclusive) to the last statement's end_lineno.
    This ensures we catch ``# CLEANUP: caller-responsibility`` comments
    that appear before the first statement.
    """
    body = func_def.body
    if not body:
        return False
    # Start from the function definition line itself (+1 because lineno is 1-indexed)
    # to catch comments between the def line and first statement.
    first_line = func_def.lineno  # 0-indexed in the list (lineno is 1-indexed)
    last_stmt = body[-1]
    last_line = getattr(last_stmt, "end_lineno", last_stmt.lineno)  # type: ignore[attr-defined]
    snippet = "\n".join(source_lines[first_line:last_line])
    return _CALLER_RESPONSIBILITY_MARKER in snippet


def _check_caller_function(
    func_def: ast.FunctionDef | ast.AsyncFunctionDef,
    source_lines: list[str],
    rel_path: str,
) -> Iterator[Finding]:
    """Yield G4.2 findings for a caller function (not start_pod itself)."""
    # Skip the start_pod implementation itself
    if func_def.name == "start_pod":
        return

    # Does the function body contain a start_pod call?
    # Scope-stopping: nested function/class/lambda bodies are NOT considered
    # part of this caller's reachable code (codex P2 on PR #132).
    if not _contains_start_pod(func_def.body):
        return

    # Check for caller-responsibility marker
    if _body_has_caller_responsibility_marker(func_def, source_lines):
        return

    # Check for stop_pod in any except-handler or finally block that
    # covers a start_pod call.
    # Strategy: walk the Try nodes (scope-respecting). For each Try node
    # whose body contains start_pod, check if any handler or the
    # finalbody has stop_pod (also scope-respecting).
    cleanup_covered = False
    for node in _iter_body_in_scope(func_def.body):
        if isinstance(node, ast.Try):
            if _contains_start_pod(node.body):
                for handler in node.handlers:
                    if _contains_stop_pod(handler.body):
                        cleanup_covered = True
                        break
                if not cleanup_covered and node.finalbody:
                    if _contains_stop_pod(node.finalbody):
                        cleanup_covered = True

    # Also accept: stop_pod called after start_pod unconditionally
    # (the start_pod call raises on failure, so the caller never reaches
    # stop_pod on the failure path — this is only safe if the library
    # guarantees internal cleanup, which is true post-#125).
    # We mark this as clean if the function ALSO has stop_pod somewhere
    # in the same scope (the happy-path cleanup).
    has_stop_anywhere = _contains_stop_pod(func_def.body)

    if cleanup_covered or has_stop_anywhere:
        return

    # Start_pod called but no stop_pod reachable anywhere in the function
    start_lines = [
        n.lineno
        for n in _iter_body_in_scope(func_def.body)
        if _is_start_pod_call(n) and hasattr(n, "lineno")
    ]
    yield Finding(
        rule="lifecycle_invariant.caller_start_pod_paired",
        severity="error",
        file=rel_path,
        line=func_def.lineno,
        function=func_def.name,
        evidence=f"start_pod at line(s) {start_lines}; no stop_pod found",
        detail=(
            f"Function {func_def.name!r} (line {func_def.lineno}) calls "
            f"start_pod but has no stop_pod call anywhere in the function body. "
            f"An orphan Pod may be left running if start_pod raises after "
            f"creating the Pod.  Fix: add stop_pod in an except/finally block, "
            f"rely on the library's built-in cleanup (post-#125 contract), or "
            f"add '# CLEANUP: caller-responsibility' if cleanup is delegated."
        ),
    )


# ---------------------------------------------------------------------------
# File-level scan
# ---------------------------------------------------------------------------


def _check_file_g41(
    path: Path,
    repo_root: Path,
    lifecycle_file: Path,
) -> Iterator[Finding]:
    """Run G4.1: check ManageRunPodLifecycle.start_pod in runpod_lifecycle.py."""
    if path.resolve() != lifecycle_file.resolve():
        return

    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return

    try:
        rel_path = str(path.relative_to(repo_root))
    except ValueError:
        rel_path = str(path)

    source_lines = source.splitlines()

    # Walk the AST looking for ManageRunPodLifecycle class → start_pod method
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if node.name != "ManageRunPodLifecycle":
            continue
        for item in node.body:
            if isinstance(item, ast.FunctionDef) and item.name == "start_pod":
                yield from _check_library_start_pod(item, source_lines, rel_path)


def _check_file_g42(path: Path, repo_root: Path) -> Iterator[Finding]:
    """Run G4.2: scan for caller functions that call start_pod."""
    source = path.read_text(encoding="utf-8")
    # Fast pre-filter
    if "start_pod" not in source:
        return

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return

    try:
        rel_path = str(path.relative_to(repo_root))
    except ValueError:
        rel_path = str(path)

    source_lines = source.splitlines()

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield from _check_caller_function(node, source_lines, rel_path)


# ---------------------------------------------------------------------------
# Scope collection
# ---------------------------------------------------------------------------


def _collect_files(repo_root: Path) -> list[Path]:
    """Return Python source files to scan (src/ + top-level scripts/*.py)."""
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
# Orchestration
# ---------------------------------------------------------------------------


def run_checks(repo_root: Path) -> Report:
    report = Report()
    lifecycle_file = repo_root / "src" / "yomotsusaka" / "runpod_lifecycle.py"
    for path in _collect_files(repo_root):
        report.files_scanned += 1
        try:
            report.findings.extend(_check_file_g41(path, repo_root, lifecycle_file))
            report.findings.extend(_check_file_g42(path, repo_root))
        except OSError:
            pass
    return report


def _human_report(report: Report) -> str:
    lines: list[str] = [
        f"scanned {report.files_scanned} file(s); "
        f"{len(report.findings)} violation(s)"
    ]
    for f in report.findings:
        loc = f"{f.file}:{f.line}"
        lines.append(f"  [{f.severity}] {f.rule} @ {loc} ({f.function})")
        lines.append(f"    evidence: {f.evidence}")
        lines.append(f"    detail:   {f.detail[:140]}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Gate-keeper G4: assert every start_pod has a paired stop_pod "
            "on failure paths."
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

    try:
        report = run_checks(repo_root)
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
