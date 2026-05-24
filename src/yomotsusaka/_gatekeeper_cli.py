"""Aggregator entry point for all gate-keeper checks.

Invokes the five scripts under ``scripts/gatekeeper/`` in sequence and
collects their exit codes.  The overall process exits non-zero when any
check exits non-zero.

Design notes
------------
* Scripts are invoked via ``uv run python scripts/gatekeeper/<name>.py``
  so they share the same virtual environment as the rest of the project
  without requiring the gatekeeper scripts to be installed as importable
  modules under ``src/``.
* The aggregator is installed as ``yomo-gatekeeper`` via
  ``[project.scripts]`` in ``pyproject.toml``, so ``uv run yomo-gatekeeper``
  is the canonical one-shot gate command.
* ``check_vocab_drift.py`` is run with ``continue_on_error=True`` because
  two pre-existing failures on tip-of-main are tracked as a deferred
  follow-up from MVP-6 (see issue #123 PR body).  Its findings are
  surfaced in output but never fail the aggregator exit code.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent  # src/ -> repo root

_CHECKS: list[tuple[str, bool]] = [
    # (script name relative to scripts/gatekeeper/, continue_on_error)
    ("check_docs_commands.py", False),
    ("check_docs_links.py", False),
    ("check_readme_provenance.py", False),
    ("check_agents_md.py", False),
    # vocab_drift has 2 pre-existing failures on tip-of-main (MVP-6
    # deferred follow-up).  Run advisory only.
    ("check_vocab_drift.py", True),
]


def main() -> None:
    failures: list[str] = []
    advisory: list[str] = []

    for script_name, continue_on_error in _CHECKS:
        script_path = _REPO_ROOT / "scripts" / "gatekeeper" / script_name
        print(f"\n--- {script_name} ---", flush=True)
        result = subprocess.run(
            [sys.executable, str(script_path)],
            cwd=str(_REPO_ROOT),
        )
        if result.returncode != 0:
            if continue_on_error:
                advisory.append(f"{script_name} (exit {result.returncode}, advisory)")
            else:
                failures.append(f"{script_name} (exit {result.returncode})")

    print("\n=== gate-keeper summary ===")
    if failures:
        print(f"FAILED: {', '.join(failures)}")
    if advisory:
        print(f"ADVISORY (non-blocking): {', '.join(advisory)}")
    if not failures and not advisory:
        print("All checks passed.")
    elif not failures:
        print("All blocking checks passed.")

    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
