"""
``python -m yomotsusaka.cli.operational_report`` — render the public-safe
markdown report for an operational scenario.

The CLI reads a structured :class:`ScenarioResult` from one of:

* ``stdin`` as JSON (default) — typically piped from the child 02 (#91)
  scenario runner, or
* ``--input <path>`` — a JSON file produced earlier.

It writes the rendered markdown to ``stdout`` and exits non-zero only on
input / redaction failures. The state token printed in the ``## Result``
section is the agent-facing signal; this CLI does NOT translate the state
into the process exit code (callers compose that on top).

Privacy invariants (binding)
----------------------------
* No raw private value, vault path, Pod identifier, endpoint URL, or
  bearer token may appear in stdout. The renderer enforces this through
  the :class:`yomotsusaka.operational_report.RedactionError` fail-closed
  sweep; on that error the CLI emits a category-only diagnostic to stderr
  and exits non-zero.
* The CLI does NOT execute a scenario itself — it is a thin renderer over
  a structured input. Live scenario execution (with associated network /
  RunPod side-effects) lives in child 02 (#91); this keeps the report
  command safe to run in a review-only / read-only context.

JSON input shape
----------------
The input JSON mirrors :class:`ScenarioResult`::

    {
      "phases": [
        {"phase_name": "batch", "status": "ok", "category": "batch_ok"},
        ...
      ],
      "counters": {
        "processed_documents": 3,
        "failed_documents": 0,
        ...
      }
    }

``category`` defaults to ``""`` when omitted on a phase entry.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from yomotsusaka.operational_report import (
    PhaseRecord,
    RedactionError,
    ScenarioResult,
    render_report,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m yomotsusaka.cli.operational_report",
        description=(
            "Render a public-safe markdown report from a structured "
            "operational scenario result. Reads JSON from stdin by "
            "default, or from --input <path>."
        ),
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=None,
        help=(
            "Path to a JSON file containing the ScenarioResult. When "
            "omitted, the CLI reads JSON from stdin."
        ),
    )
    return parser


def _load_input(args: argparse.Namespace) -> Any:
    if args.input is not None:
        path: Path = args.input
        if not path.exists():
            raise FileNotFoundError(f"input file does not exist: {path}")
        return json.loads(path.read_text(encoding="utf-8"))
    raw = sys.stdin.read()
    if not raw.strip():
        raise ValueError("no JSON received on stdin")
    return json.loads(raw)


def _parse_scenario_result(data: Any) -> ScenarioResult:
    """Convert a decoded JSON object into a :class:`ScenarioResult`.

    Validation is intentionally lightweight — the renderer's redaction
    sweep is the load-bearing safety net. We only check that the shape
    matches well enough to construct the dataclasses.
    """
    if not isinstance(data, dict):
        raise ValueError("input JSON must be an object")
    raw_phases = data.get("phases", [])
    if not isinstance(raw_phases, list):
        raise ValueError("'phases' must be a list")
    phases: list[PhaseRecord] = []
    for idx, raw in enumerate(raw_phases):
        if not isinstance(raw, dict):
            raise ValueError(f"phases[{idx}] must be an object")
        try:
            phase_name = raw["phase_name"]
            status = raw["status"]
        except KeyError as exc:
            raise ValueError(
                f"phases[{idx}] missing required key {exc}"
            ) from None
        if not isinstance(phase_name, str) or not isinstance(status, str):
            raise ValueError(
                f"phases[{idx}] phase_name/status must be strings"
            )
        category = raw.get("category", "")
        if not isinstance(category, str):
            raise ValueError(f"phases[{idx}] category must be a string")
        phases.append(
            PhaseRecord(
                phase_name=phase_name,
                status=status,  # type: ignore[arg-type]
                category=category,
            )
        )
    counters = data.get("counters", {})
    if not isinstance(counters, dict):
        raise ValueError("'counters' must be an object")
    return ScenarioResult(phases=tuple(phases), counters=dict(counters))


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit code.

    Exit codes
    ----------
    0
        Report rendered and emitted to stdout.
    1
        Input error (missing file, malformed JSON, schema mismatch).
    2
        Redaction sweep failed — the renderer produced output containing a
        sensitive shape. Treated as a hard failure; no partial output is
        emitted.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        raw = _load_input(args)
    except FileNotFoundError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1
    except (json.JSONDecodeError, ValueError) as exc:
        # ValueError covers empty-stdin sentinel; JSONDecodeError covers
        # malformed JSON. Neither message echoes secret content because
        # the input itself is caller-supplied.
        sys.stderr.write(f"error: invalid input ({type(exc).__name__})\n")
        return 1

    try:
        result = _parse_scenario_result(raw)
    except ValueError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1

    try:
        rendered = render_report(result)
    except RedactionError:
        # Do NOT echo the exception message verbatim — the renderer's own
        # message is category-only, but the defensive posture is to also
        # avoid surfacing the rendered draft anywhere.
        sys.stderr.write(
            "error: rendered report failed the public-safe redaction "
            "sweep; refusing to emit\n"
        )
        return 2

    sys.stdout.write(rendered)
    sys.stdout.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
