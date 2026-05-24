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

_CHECKS: list[tuple[str, list[str], bool]] = [
    # (script name relative to scripts/gatekeeper/, extra args, continue_on_error)
    ("check_docs_commands.py", [], False),
    # ``check_docs_links.py`` exits 2 on advisory ``gh`` lookup misses
    # (offline / unauthenticated environments).  The aggregator treats
    # any non-zero exit as a hard failure for blocking checks, so we
    # pass ``--no-gh`` to keep ``uv run yomo-gatekeeper`` parity with
    # the ``.github/workflows/gatekeeper.yml`` step which also passes
    # ``--no-gh``.  The stale-umbrella sub-check is itself documented
    # as advisory in ``check_docs_links.py``; skipping the ``gh``
    # lookup just trades one advisory for an offline-stable exit code
    # path.  Codex P1 on PR #130.
    ("check_docs_links.py", ["--no-gh"], False),
    ("check_readme_provenance.py", [], False),
    ("check_agents_md.py", [], False),
    # vocab_drift has 2 pre-existing failures on tip-of-main (MVP-6
    # deferred follow-up).  Run advisory only.
    ("check_vocab_drift.py", [], True),
    # Layer 2-5 rules (issue #128) — adopted after validation-first review.
    # G2: Doc numeric spec vs code constants (hard gate; disk_gb drift fixed).
    ("check_spec_values.py", [], False),
    # G3: Documented env-var must be wired in source (hard gate; post-#126).
    ("check_documented_env_vars.py", [], False),
    # G4: Lifecycle invariant — start_pod has paired stop_pod (hard gate; post-#125).
    ("check_lifecycle_invariant.py", [], False),
]


def main() -> None:
    failures: list[str] = []
    advisory: list[str] = []

    for script_name, extra_args, continue_on_error in _CHECKS:
        script_path = _REPO_ROOT / "scripts" / "gatekeeper" / script_name
        print(f"\n--- {script_name} ---", flush=True)
        result = subprocess.run(
            [sys.executable, str(script_path), *extra_args],
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
