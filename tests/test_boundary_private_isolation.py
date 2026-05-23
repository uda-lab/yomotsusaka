"""Boundary import-isolation snapshot test (issue #46 metaplan Fork 6).

Asserts that importing :mod:`yomotsusaka.boundary` does NOT pull in
``httpx``, ``yomotsusaka.runpod_lifecycle``, ``yomotsusaka.inference_backend``,
or ``yomotsusaka.vllm_backend``. Per metaplan Fork 6, those modules are
private-side internal kernel; any transitive import path from
``yomotsusaka.boundary`` would defeat the privacy-boundary separation.

The test runs ``importlib.import_module`` against ``yomotsusaka.boundary``
inside a child Python subprocess so the parent test runner's already-loaded
modules cannot pollute the snapshot. Running in a subprocess also means
the assertion is robust to pytest plugins (``pytest-httpx``) that import
``httpx`` at collection time.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap


_FORBIDDEN_MODULES: tuple[str, ...] = (
    "httpx",
    "yomotsusaka.runpod_lifecycle",
    "yomotsusaka.inference_backend",
    "yomotsusaka.vllm_backend",
    # Issue #72: SpanProposer is a private-side kernel module that wires
    # InferenceBackend into the redaction pipeline; importing it from the
    # boundary would transitively pull yomotsusaka.inference_backend back
    # into the boundary's import graph.
    "yomotsusaka.span_proposer",
)


def _snapshot_after_boundary_import() -> set[str]:
    """Import :mod:`yomotsusaka.boundary` in a subprocess and return the
    resulting ``sys.modules`` keyset.

    Run in a subprocess so the parent test process's existing imports
    (notably ``httpx`` brought in by ``pytest-httpx``) do not pollute the
    snapshot.
    """
    script = textwrap.dedent(
        """
        import importlib
        import json
        import sys

        importlib.import_module("yomotsusaka.boundary")
        json.dump(sorted(sys.modules.keys()), sys.stdout)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return set(json.loads(result.stdout))


def test_boundary_does_not_import_private_side_modules() -> None:
    """Boundary import MUST NOT pull in private-side network/lifecycle modules."""
    loaded = _snapshot_after_boundary_import()
    leaked = [m for m in _FORBIDDEN_MODULES if m in loaded]
    assert not leaked, (
        "yomotsusaka.boundary transitively imported private-side module(s) "
        f"{leaked!r}; metaplan Fork 6 of issue #46 requires the boundary to "
        "stay free of httpx and the private-side lifecycle/backend modules"
    )


def test_boundary_imports_at_all() -> None:
    """Sanity: boundary import succeeds in the subprocess."""
    loaded = _snapshot_after_boundary_import()
    assert "yomotsusaka.boundary" in loaded
