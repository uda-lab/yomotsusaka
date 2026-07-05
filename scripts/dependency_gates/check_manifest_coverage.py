#!/usr/bin/env python3

# ---------------------------------------------------------------------------
# VENDORED — do not edit locally.
# Source: t-uda/gate-keeper @ a5123016e4a337b69928c5b9729695d25b1450ab
#         scripts/dependency_gates/check_manifest_coverage.py
# Vendored for the gate-keeper manifest-coverage trial (uda-lab/yomotsusaka#158).
# The validator lives outside the installed gate-keeper package, so it is
# copied verbatim here. Re-sync from the source repo rather than editing.
# ---------------------------------------------------------------------------
"""Mode-A validator for manifest coverage (umbrella #159, issue #280, slice 2).

Reads a JSON ``{rule, target}`` payload on stdin per the ``command`` external
adapter contract (docs/backend-external.md § "command").  For the single
``target`` file, checks whether it has either (a) at least one manifest edge
in ``.gate-keeper/dependency-manifest.yml`` whose ``from`` or ``to`` node
resolves to that path, or (b) an explicit entry in
``.gate-keeper/ref-exemptions.yml``.

A changed file without either is reported as ``uncovered_file`` (FAIL).
Files not in the changed-file set are silently passed as
``dependent_artifact_unaffected`` (PASS).

Failure vocabulary:

- ``uncovered_file`` — target is in the changed-file set but has neither a
  manifest edge nor an exemption. Status: ``fail``.
- ``covering_edge`` — target participates in at least one manifest edge (as
  ``from`` or ``to``). Status: ``pass``.
- ``exemption_applied`` — target has an entry in the exemption file. Status:
  ``pass``.
- ``dependent_artifact_unaffected`` — target not in the changed-file set;
  nothing to check. Status: ``pass``.
- ``manifest_invalid`` — manifest or exemption file parse/load failure.
  Status: ``unavailable`` (fail-closed).
- ``changed_file_source_unresolved`` — git diff unavailable. Status:
  ``unavailable`` (fail-closed).

Always exits 0; pass/fail is carried in the emitted ``Diagnostic.status``.

Schema and contract: docs/design/dependency-gates.md §6.2.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from dependency_gates.manifest import (  # noqa: E402
        Manifest,
        ManifestError,
        load_manifest,
    )
else:
    from .manifest import (
        Manifest,
        ManifestError,
        load_manifest,
    )

# ---------------------------------------------------------------------------
# Minimal git-diff helpers — inlined to keep this script self-contained.
# The same logic lives in scripts/dependency_gates/_changed_files.py; the
# two copies are intentionally kept in sync. If the shim there is updated
# to import from gate_keeper.changed, this copy provides the fallback so the
# validator can run without the full gate_keeper package installed.
# ---------------------------------------------------------------------------

_DEFAULT_BASE_REF_ENV = "GATE_KEEPER_BASE_REF"
_DEFAULT_BASE_REF = "origin/main"


class ChangedFilesError(RuntimeError):
    """Raised when the changed-file set cannot be computed."""


def resolve_base_ref(env: dict[str, str] | None = None) -> str:
    """Return the configured base ref (env var with fallback)."""
    source = env if env is not None else os.environ
    value = source.get(_DEFAULT_BASE_REF_ENV)
    if value:
        return value
    return _DEFAULT_BASE_REF


def compute_changed_files(repo_root: Path, base_ref: str) -> frozenset[str]:
    """Return repo-relative POSIX paths changed since *base_ref*."""
    if not repo_root.is_dir():
        raise ChangedFilesError(f"repo root does not exist: {repo_root}")
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "diff", "--name-only", f"{base_ref}...HEAD"],
            cwd=str(repo_root),
            check=False,
            capture_output=True,
            text=True,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise ChangedFilesError("git executable not found on PATH") from exc
    except OSError as exc:
        raise ChangedFilesError(f"git invocation failed: {exc}") from exc

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise ChangedFilesError(f"git diff exited {result.returncode}: {stderr or '(no stderr)'}")
    changed = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    return frozenset(changed)


DEFAULT_MANIFEST_PATH = ".gate-keeper/dependency-manifest.yml"
DEFAULT_EXEMPTIONS_PATH = ".gate-keeper/ref-exemptions.yml"

# Recognised exemption categories in this slice. ``llm_required`` is reserved
# for Layer 2 (issue #281) and is not exercised here.
_VALID_EXEMPTION_CATEGORIES = frozenset({"manual", "llm_required"})


@dataclass(frozen=True)
class Outcome:
    status: str
    message: str
    evidence: list[dict[str, Any]]
    remediation: str | None = None


def main() -> int:
    raw = sys.stdin.read()
    payload = _parse_payload(raw)
    target_str = payload.get("target", "") or ""
    rule = payload.get("rule") or {}
    params = rule.get("params") or {}

    repo_root = _resolve_repo_root(Path.cwd())
    manifest_path = _resolve_path(params.get("manifest"), DEFAULT_MANIFEST_PATH, repo_root)
    exemptions_path = _resolve_path(params.get("exemptions"), DEFAULT_EXEMPTIONS_PATH, repo_root)

    outcome = _run(
        repo_root=repo_root,
        manifest_path=manifest_path,
        exemptions_path=exemptions_path,
        target=target_str,
    )
    _emit(outcome)
    return 0


def _run(
    *,
    repo_root: Path,
    manifest_path: Path,
    exemptions_path: Path,
    target: str,
) -> Outcome:
    if not target:
        return Outcome(
            status="unavailable",
            message="target path is empty",
            evidence=[
                {
                    "kind": "params_error",
                    "data": {"field": "target", "reason": "empty"},
                }
            ],
            remediation="Invoke the validator with a non-empty --target path.",
        )

    target_rel = _to_repo_relative(target, repo_root)

    # Manifest is required — fail-closed if missing or malformed.
    if not manifest_path.is_file():
        return Outcome(
            status="unavailable",
            message=f"manifest not found at {manifest_path}",
            evidence=[
                {
                    "kind": "manifest_invalid",
                    "data": {
                        "manifest_path": str(manifest_path),
                        "error": "file not found",
                    },
                }
            ],
            remediation=(
                f"Create {manifest_path} per docs/design/dependency-gates.md §3, "
                "or configure 'params.manifest' in the rule."
            ),
        )
    try:
        manifest = load_manifest(manifest_path)
    except ManifestError as exc:
        return Outcome(
            status="unavailable",
            message=f"manifest invalid: {exc}",
            evidence=[
                {
                    "kind": "manifest_invalid",
                    "data": {"manifest_path": str(manifest_path), "error": str(exc)},
                }
            ],
            remediation=(f"Fix the manifest at {manifest_path} to match docs/design/dependency-gates.md §3."),
        )

    # Exemption file: fail-closed if present but unreadable/malformed;
    # absent ⇒ no exemptions (not an error).
    exemptions: dict[str, dict[str, str]] = {}
    if exemptions_path.is_file():
        exemption_load_outcome = _load_exemptions(exemptions_path)
        if isinstance(exemption_load_outcome, Outcome):
            return exemption_load_outcome
        exemptions = exemption_load_outcome

    # Changed-file set — fail-closed when unavailable.
    base_ref = resolve_base_ref()
    try:
        changed = compute_changed_files(repo_root, base_ref)
    except ChangedFilesError as exc:
        return Outcome(
            status="unavailable",
            message=f"changed-file set unavailable: {exc}",
            evidence=[
                {
                    "kind": "changed_file_source_unresolved",
                    "data": {
                        "base_ref": base_ref,
                        "error": str(exc),
                    },
                }
            ],
            remediation=(
                "Ensure git is available and GATE_KEEPER_BASE_REF resolves. "
                "Run inside a git worktree with 'origin/main' reachable."
            ),
        )

    # Target not in changed-file set → nothing to enforce.
    if target_rel not in changed:
        return Outcome(
            status="pass",
            message=f"{target_rel!r} not in the changed-file set; no coverage check required",
            evidence=[
                {
                    "kind": "dependent_artifact_unaffected",
                    "data": {
                        "target": target_rel,
                        "base_ref": base_ref,
                    },
                }
            ],
        )

    # Build the set of paths that participate in at least one manifest edge.
    covered_paths = _manifest_covered_paths(manifest)

    # Check manifest coverage first.
    if target_rel in covered_paths:
        covering = _covering_edge_summary(target_rel, manifest)
        return Outcome(
            status="pass",
            message=f"{target_rel!r} is covered by {len(covering)} manifest edge(s)",
            evidence=[
                {
                    "kind": "covering_edge",
                    "data": {
                        "target": target_rel,
                        "manifest_path": str(manifest_path),
                        "edges": covering,
                    },
                }
            ],
        )

    # Check exemption coverage.
    if target_rel in exemptions:
        entry = exemptions[target_rel]
        return Outcome(
            status="pass",
            message=(
                f"{target_rel!r} is exempted"
                + (f" ({entry.get('reason', '')})" if entry.get("reason") else "")
            ),
            evidence=[
                {
                    "kind": "exemption_applied",
                    "data": {
                        "target": target_rel,
                        "exemptions_path": str(exemptions_path),
                        "category": entry["category"],
                        "reason": entry.get("reason", ""),
                    },
                }
            ],
        )

    # Neither covered nor exempted.
    return Outcome(
        status="fail",
        message=(
            f"{target_rel!r} is in the changed-file set but has no manifest edge and no exemption entry"
        ),
        evidence=[
            {
                "kind": "uncovered_file",
                "data": {
                    "target": target_rel,
                    "manifest_path": str(manifest_path),
                    "exemptions_path": str(exemptions_path),
                },
            }
        ],
        remediation=(
            f"Either add {target_rel!r} as a node with at least one edge in "
            f"{manifest_path} (docs/design/dependency-gates.md §3), or add an "
            f"exemption entry in {exemptions_path} "
            "(docs/design/dependency-gates.md §6.2)."
        ),
    )


def _load_exemptions(
    path: Path,
) -> dict[str, dict[str, str]] | Outcome:
    """Load the exemption file. Returns a path→entry dict or an Outcome on failure."""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return Outcome(
            status="unavailable",
            message=f"exemption file {path} could not be read: {exc}",
            evidence=[
                {
                    "kind": "manifest_invalid",
                    "data": {
                        "manifest_path": str(path),
                        "error": f"read error: {exc}",
                    },
                }
            ],
            remediation=f"Fix the exemption file at {path} (UTF-8 expected) or restore it.",
        )

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        return Outcome(
            status="unavailable",
            message=f"exemption file {path} is not valid YAML: {exc}",
            evidence=[
                {
                    "kind": "manifest_invalid",
                    "data": {
                        "manifest_path": str(path),
                        "error": f"YAML parse error: {exc}",
                    },
                }
            ],
            remediation=(
                f"Fix the YAML syntax in {path}. See docs/design/dependency-gates.md §6.2 for the schema."
            ),
        )

    if data is None:
        data = {}
    if not isinstance(data, dict):
        return Outcome(
            status="unavailable",
            message=f"exemption file {path} top-level must be a mapping",
            evidence=[
                {
                    "kind": "manifest_invalid",
                    "data": {
                        "manifest_path": str(path),
                        "error": f"expected mapping, got {type(data).__name__}",
                    },
                }
            ],
            remediation=(
                f"Fix {path}: the top-level key must be 'exemptions:'. "
                "See docs/design/dependency-gates.md §6.2."
            ),
        )

    raw_list = data.get("exemptions") or []
    if not isinstance(raw_list, list):
        return Outcome(
            status="unavailable",
            message=f"exemption file {path}: 'exemptions' must be a list",
            evidence=[
                {
                    "kind": "manifest_invalid",
                    "data": {
                        "manifest_path": str(path),
                        "error": "'exemptions' is not a list",
                    },
                }
            ],
            remediation=(
                f"Fix {path}: 'exemptions' must be a YAML list. See docs/design/dependency-gates.md §6.2."
            ),
        )

    result: dict[str, dict[str, str]] = {}
    for i, item in enumerate(raw_list):
        ctx = f"exemptions[{i}]"
        if not isinstance(item, dict):
            return Outcome(
                status="unavailable",
                message=f"exemption file {path}: {ctx} must be a mapping",
                evidence=[
                    {
                        "kind": "manifest_invalid",
                        "data": {
                            "manifest_path": str(path),
                            "error": f"{ctx}: expected mapping, got {type(item).__name__}",
                        },
                    }
                ],
                remediation=(
                    f"Fix {ctx} in {path}: each entry must have at least 'path'. "
                    "See docs/design/dependency-gates.md §6.2."
                ),
            )
        entry_path = item.get("path")
        if not isinstance(entry_path, str) or not entry_path:
            return Outcome(
                status="unavailable",
                message=f"exemption file {path}: {ctx}.path must be a non-empty string",
                evidence=[
                    {
                        "kind": "manifest_invalid",
                        "data": {
                            "manifest_path": str(path),
                            "error": f"{ctx}.path missing or empty",
                        },
                    }
                ],
                remediation=(
                    f"Fix {ctx} in {path}: 'path' must be a non-empty string. "
                    "See docs/design/dependency-gates.md §6.2."
                ),
            )
        # ``category`` is required — a missing field must not silently become
        # ``manual`` and exempt an uncovered file without an explicit decision.
        if "category" not in item:
            return Outcome(
                status="unavailable",
                message=f"exemption file {path}: {ctx}.category is required",
                evidence=[
                    {
                        "kind": "manifest_invalid",
                        "data": {
                            "manifest_path": str(path),
                            "error": f"{ctx}.category missing",
                        },
                    }
                ],
                remediation=(
                    f"Fix {ctx} in {path}: add 'category: manual' (or 'category: llm_required'). "
                    "See docs/design/dependency-gates.md §6.2."
                ),
            )
        category = item["category"]
        if category not in _VALID_EXEMPTION_CATEGORIES:
            return Outcome(
                status="unavailable",
                message=(
                    f"exemption file {path}: {ctx}.category {category!r} is not valid; "
                    f"expected one of {sorted(_VALID_EXEMPTION_CATEGORIES)}"
                ),
                evidence=[
                    {
                        "kind": "manifest_invalid",
                        "data": {
                            "manifest_path": str(path),
                            "error": f"{ctx}.category unknown: {category!r}",
                        },
                    }
                ],
                remediation=(
                    f"Fix {ctx} in {path}: use category 'manual' (or 'llm_required' "
                    "once Layer 2 is available). See docs/design/dependency-gates.md §6.2."
                ),
            )
        result[entry_path] = {
            "category": category,
            "reason": item.get("reason", ""),
        }
    return result


def _manifest_covered_paths(manifest: Manifest) -> frozenset[str]:
    """Return the set of repo-relative paths that participate in any manifest edge."""
    out: set[str] = set()
    for edge in manifest.edges:
        from_node = manifest.nodes_by_id.get(edge.from_id)
        to_node = manifest.nodes_by_id.get(edge.to_id)
        if from_node is not None:
            out.add(from_node.path)
        if to_node is not None:
            out.add(to_node.path)
    return frozenset(out)


def _covering_edge_summary(target_rel: str, manifest: Manifest) -> list[dict[str, str]]:
    """Return a compact summary of all edges that involve *target_rel*."""
    out: list[dict[str, str]] = []
    for edge in manifest.edges:
        from_node = manifest.nodes_by_id.get(edge.from_id)
        to_node = manifest.nodes_by_id.get(edge.to_id)
        from_path = from_node.path if from_node is not None else ""
        to_path = to_node.path if to_node is not None else ""
        if from_path == target_rel or to_path == target_rel:
            out.append(
                {
                    "from": edge.from_id,
                    "to": edge.to_id,
                    "relation": edge.relation,
                    "from_path": from_path,
                    "to_path": to_path,
                }
            )
    return out


def _parse_payload(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _resolve_path(raw: Any, default: str, repo_root: Path) -> Path:
    if isinstance(raw, str) and raw:
        candidate = Path(raw)
    else:
        candidate = Path(default)
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    return candidate


def _resolve_repo_root(start: Path) -> Path:
    cur = start.resolve()
    for parent in [cur, *cur.parents]:
        if (parent / ".git").exists():
            return parent
    return start


def _to_repo_relative(target: str, repo_root: Path) -> str:
    if not target:
        return ""
    p = Path(target)
    try:
        if p.is_absolute():
            rel = p.resolve().relative_to(repo_root.resolve())
        else:
            rel = (repo_root / p).resolve().relative_to(repo_root.resolve())
    except ValueError:
        return target.replace("\\", "/")
    return rel.as_posix()


def _emit(outcome: Outcome) -> None:
    diagnostic: dict[str, Any] = {
        "status": outcome.status,
        "message": outcome.message,
        "evidence": list(outcome.evidence),
    }
    if outcome.remediation is not None:
        diagnostic["remediation"] = outcome.remediation
    sys.stdout.write(json.dumps(diagnostic))


if __name__ == "__main__":
    raise SystemExit(main())
