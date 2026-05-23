#!/usr/bin/env python3
"""Agent-runnable RunPod lifecycle helper (issue #76, closes #70).

Default policy — create → wait for ``/health`` → run sanitised smoke →
**delete**. ``--keep-pod`` opts out of the cleanup step. The script wraps
:class:`yomotsusaka.runpod_lifecycle.ManageRunPodLifecycle` and the
:mod:`scripts.smoke_runpod` diagnostic mode, and adds:

* A preflight ``runpodctl --version`` presence check that produces a
  friendlier owner-facing install hint without making a network call.
* Sanitised category-only stdout (``lifecycle: <category>``) per phase.
* A urgent stderr line + per-run lifecycle JSONL row on cleanup failure
  so the owner can identify the orphaned Pod manually.

See ``docs/runpod-agent-lifecycle.md`` for the owner/agent split and the
sanitisation invariant. Decision sources: source spec at
``.claude/plans/mvp4-20260523/child_05_agent-managed-runpod-lifecycle-with-cost-control.md``
and the tightened plan ``/tmp/mvp4_tightened_76.md``.

The script NEVER echoes Pod IDs, endpoint URLs, the bearer token, or raw
``httpx`` exception messages — see ``_FORBIDDEN`` in the sanitisation
test under ``tests/test_runpod_lifecycle.py``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Local imports — never imports yomotsusaka.boundary (private-side rule).
from yomotsusaka.runpod_lifecycle import (
    ManageRunPodLifecycle,
    PodConfig,
    PodHandle,
    PodUnavailableError,
)


# ---------------------------------------------------------------------------
# Constants — exit-code / category table (Decision 4 in /tmp/mvp4_tightened_76.md)
# ---------------------------------------------------------------------------


# Exit codes are precedence-ordered in `_select_exit_code`:
# 2 = preflight failure (no Pod created)
# 1 = phase failure (wait_timeout / smoke_failed) after a Pod was created
# 3 = cleanup failure (requires manual owner action)
EXIT_OK = 0
EXIT_PHASE_FAILED = 1
EXIT_PREFLIGHT_FAILED = 2
EXIT_CLEANUP_FAILED = 3


# Category literals — the ONLY values that may appear after the
# ``lifecycle:`` prefix on stdout. Mirrors the table in
# ``docs/runpod-agent-lifecycle.md`` §5.
_CATEGORY_PREFLIGHT_RUNPODCTL = "runpodctl_missing"
_CATEGORY_PREFLIGHT_APIKEY = "api_key_missing"
_CATEGORY_CREATED = "created"
_CATEGORY_CREATE_FAILED = "create_failed"
_CATEGORY_HEALTHY = "healthy"
_CATEGORY_WAIT_TIMEOUT = "wait_timeout"
_CATEGORY_SMOKE_PASSED = "smoke_passed"
_CATEGORY_SMOKE_FAILED = "smoke_failed"
_CATEGORY_DELETED = "deleted"
_CATEGORY_KEPT = "kept"
_CATEGORY_CLEANUP_FAILED = "cleanup_failed"

# The runpodctl docs URL the preflight points the owner at. Single
# constant so a future URL change is a one-line edit (low-impact;
# Decision-3 / Improvement-Findings note in the tightened plan).
_RUNPODCTL_INSTALL_URL = "https://docs.runpod.io/cli/install-runpodctl"

# JSONL file under the user's local cache (NOT in repo). The path is
# gitignored at the repo root (`lifecycle.jsonl`); the per-user path
# lives under ``~/.cache/yomotsusaka/lifecycle.jsonl``.
_DEFAULT_LIFECYCLE_LOG = Path.home() / ".cache" / "yomotsusaka" / "lifecycle.jsonl"


logger = logging.getLogger("yomotsusaka.manage_runpod")


# ---------------------------------------------------------------------------
# Sanitised producers (Seams S3 / S4 / S5 in the tightened plan)
# ---------------------------------------------------------------------------


def _emit_category(category: str, *, stream: Any = None) -> None:
    """S3 — print exactly ``lifecycle: <category>`` to stdout.

    No interpolation of Pod IDs, endpoint URLs, response bodies, or any
    other content. The only producer of public stdout content.
    """
    out = stream if stream is not None else sys.stdout
    print(f"lifecycle: {category}", file=out)


def _emit_urgent(
    request_id: str,
    *,
    log_path: Path | None = None,
    stream: Any = None,
) -> None:
    """S4 — emit ONE urgent stderr line on cleanup failure.

    The ``request_id`` is the per-run UUID4 correlation token; it carries
    no information about the Pod itself (no Pod ID, endpoint, or API
    key). The owner uses it to locate the matching ``lifecycle.jsonl``
    row and run ``runpodctl pod list`` themselves.

    ``log_path`` is the effective JSONL path the helper wrote to — by
    default ``~/.cache/yomotsusaka/lifecycle.jsonl``, but tests pass a
    ``tmp_path`` override; the urgent message must always point at the
    file actually written so the request_id correlation is reliable
    (per copilot review on PR #84).
    """
    err = stream if stream is not None else sys.stderr
    effective = log_path or _DEFAULT_LIFECYCLE_LOG
    # Render ``~/.cache/...`` style for the default to keep the existing
    # owner-facing wording; for non-default paths (tests / explicit
    # overrides), use the literal path string.
    if effective == _DEFAULT_LIFECYCLE_LOG:
        rendered = "~/.cache/yomotsusaka/lifecycle.jsonl"
    else:
        rendered = str(effective)
    print(
        "URGENT: manual Pod cleanup required; "
        f"see {rendered} for request_id={request_id}",
        file=err,
    )


def _append_lifecycle_row(
    *, request_id: str, category: str, log_path: Path | None = None
) -> None:
    """S5 — append a JSONL row with EXACTLY three keys.

    Schema (binding per the tightened plan):

    .. code-block:: json

        {"timestamp": "<iso8601>", "request_id": "<uuid>", "category": "<cat>"}

    No Pod ID, endpoint, API key, model id, gpu_type, or response body
    is ever written here. If the directory does not exist, it is
    created with default permissions; failures are swallowed (the
    stdout/stderr surface is the primary correlation channel).
    """
    target = log_path or _DEFAULT_LIFECYCLE_LOG
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "request_id": request_id,
            "category": category,
        }
        with target.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except OSError:
        # The stdout/stderr surface remains the primary correlation
        # channel; a write failure here is non-fatal.
        pass


# ---------------------------------------------------------------------------
# Preflight (Decision 4 — categories 1 + 2)
# ---------------------------------------------------------------------------


def _runpodctl_available() -> bool:
    """Return True iff ``runpodctl --version`` is on the PATH.

    Uses ``shutil.which`` followed by ``subprocess.run(['runpodctl',
    '--version'])`` so a stale entry in PATH that no longer maps to a
    real binary is also caught. The subprocess invocation discards both
    stdout and stderr; only the exit code is consulted. No exception
    text is ever propagated to the agent-facing surface.
    """
    if shutil.which("runpodctl") is None:
        return False
    try:
        result = subprocess.run(
            ["runpodctl", "--version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


def _api_key_available(env: dict[str, str] | None = None) -> bool:
    """Return True iff ``RUNPOD_API_KEY`` is set to a non-empty value.

    Presence check only — the value is NEVER read into a local variable
    that could leak into a log record. The script invokes
    :class:`ManageRunPodLifecycle` without passing ``api_key`` so the
    constructor reads the env var itself.
    """
    e = env if env is not None else os.environ
    return bool(e.get("RUNPOD_API_KEY"))


# ---------------------------------------------------------------------------
# Smoke subprocess (Seams S6 + S7)
# ---------------------------------------------------------------------------


_SMOKE_SCRIPT_REL_PATH = Path("scripts") / "smoke_runpod.py"


def _run_smoke_subprocess(
    handle: PodHandle,
    *,
    vllm_api_key: str | None,
    repo_root: Path,
    runner: Any = None,
) -> tuple[bool, str | None]:
    """Invoke ``scripts/smoke_runpod.py --mode diagnose`` in a child process.

    Returns ``(passed, smoke_category)`` where ``smoke_category`` is the
    bare category literal extracted from the smoke's ``diagnostic: <cat>``
    stdout line (without the snippet, even on success). Stderr from the
    child is discarded — it may carry ``httpx`` text from the smoke's
    own error path and is not safe to propagate.

    ``runner`` is the injection seam for L2 tests; it must be call-
    compatible with :func:`subprocess.run` (default).

    The smoke is invoked with ``env`` carrying ONLY the variables it
    needs (``RUNPOD_LIVE_SMOKE=1``, ``VLLM_ENDPOINT``, optionally
    ``VLLM_API_KEY``, ``RUNPOD_POD_ID``). ``VLLM_ENDPOINT`` /
    ``RUNPOD_POD_ID`` come from the freshly-created Pod's handle
    (Seam S6).

    Per ``docs/runpod-agent-smoke.md`` §9 and ``docs/runpod.md`` §6, the
    **vLLM bearer key** (``VLLM_API_KEY``) is a different secret from
    the **RunPod account API key** (``RUNPOD_API_KEY``). The owner
    injects ``VLLM_API_KEY`` separately when they want to override the
    smoke's default; if absent, the smoke synthesises
    ``sk-<RUNPOD_POD_ID>`` itself. The helper therefore passes
    ``VLLM_API_KEY`` through ONLY when the parent process already has
    it; otherwise it omits the variable and lets the smoke take the
    documented fallback. The helper NEVER passes ``RUNPOD_API_KEY`` as
    a vLLM bearer.
    """
    if runner is None:
        runner = subprocess.run

    smoke_path = repo_root / _SMOKE_SCRIPT_REL_PATH

    child_env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "RUNPOD_LIVE_SMOKE": "1",
        "VLLM_ENDPOINT": handle.endpoint,
        "RUNPOD_POD_ID": handle.pod_id,
    }
    if vllm_api_key:
        child_env["VLLM_API_KEY"] = vllm_api_key
    # Propagate PYTHONPATH so the smoke can import yomotsusaka.vllm_backend
    # when invoked from an editable install.
    if "PYTHONPATH" in os.environ:
        child_env["PYTHONPATH"] = os.environ["PYTHONPATH"]

    try:
        result = runner(
            [sys.executable, str(smoke_path), "--mode", "diagnose"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=child_env,
            timeout=120,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False, None

    raw_stdout = result.stdout if isinstance(result.stdout, str) else (
        result.stdout.decode("utf-8", errors="replace")
        if result.stdout is not None
        else ""
    )

    # Extract the smoke's category literal — only the category, never
    # the snippet (Seam S7).
    smoke_category: str | None = None
    for line in raw_stdout.splitlines():
        if line.startswith("diagnostic:"):
            # ``diagnostic: <category> [snippet=...]``
            payload = line[len("diagnostic:"):].strip()
            # Strip optional ``snippet=...`` portion.
            smoke_category = payload.split(" ", 1)[0]
            break

    passed = result.returncode == 0 and smoke_category == "success"
    return passed, smoke_category


# ---------------------------------------------------------------------------
# Lifecycle driver
# ---------------------------------------------------------------------------


def _select_exit_code(exit_codes: list[int]) -> int:
    """Apply the exit-code precedence rule.

    Per Decision 4: exit code 2 (preflight) is the only return path
    when no Pod was ever created. Exit code 3 (cleanup_failed) trumps
    every other non-zero code because manual owner action is required.
    Otherwise the LAST non-zero code observed wins (e.g. ``wait_timeout``
    then ``deleted`` → exit code 1).
    """
    if EXIT_PREFLIGHT_FAILED in exit_codes:
        return EXIT_PREFLIGHT_FAILED
    if EXIT_CLEANUP_FAILED in exit_codes:
        return EXIT_CLEANUP_FAILED
    non_zero = [c for c in exit_codes if c != EXIT_OK]
    if non_zero:
        return non_zero[-1]
    return EXIT_OK


def run_lifecycle(
    *,
    keep_pod: bool,
    pod_config: PodConfig,
    lifecycle_factory: Any | None = None,
    smoke_runner: Any | None = None,
    repo_root: Path | None = None,
    lifecycle_log: Path | None = None,
    env: dict[str, str] | None = None,
    runpodctl_check: Any | None = None,
) -> int:
    """Execute the full create → wait → smoke → cleanup sequence.

    Pure-function shape so the L2 test can drive it without spawning
    the helper script as a subprocess. ``lifecycle_factory`` returns
    a :class:`ManageRunPodLifecycle` (or compatible); ``smoke_runner``
    is the :func:`subprocess.run` substitute the L2 test injects.

    ``env`` is the environment dict consulted for ``RUNPOD_API_KEY``
    (allows the L2 test to assert the preflight without touching the
    process-level env).

    Returns the script's exit code. Per Decision 4's precedence rule.
    """
    env_dict = env if env is not None else dict(os.environ)
    runpodctl_present = (
        runpodctl_check() if runpodctl_check is not None else _runpodctl_available()
    )

    # Per-run correlation token — used only on the cleanup-failed path.
    # Generated upfront so the same id covers stdout, stderr, and JSONL.
    request_id = str(uuid.uuid4())

    if not runpodctl_present:
        _emit_category(_CATEGORY_PREFLIGHT_RUNPODCTL)
        print(
            f"hint: install runpodctl from {_RUNPODCTL_INSTALL_URL}",
            file=sys.stderr,
        )
        _append_lifecycle_row(
            request_id=request_id,
            category=_CATEGORY_PREFLIGHT_RUNPODCTL,
            log_path=lifecycle_log,
        )
        return EXIT_PREFLIGHT_FAILED

    if not _api_key_available(env_dict):
        _emit_category(_CATEGORY_PREFLIGHT_APIKEY)
        _append_lifecycle_row(
            request_id=request_id,
            category=_CATEGORY_PREFLIGHT_APIKEY,
            log_path=lifecycle_log,
        )
        return EXIT_PREFLIGHT_FAILED

    # Construct lifecycle — the constructor reads RUNPOD_API_KEY itself,
    # so the helper never touches the value directly.
    factory = lifecycle_factory or (lambda: ManageRunPodLifecycle(pod_config=pod_config))
    lifecycle = factory()
    # The smoke subprocess wants the vLLM bearer (VLLM_API_KEY), NOT the
    # RunPod account API key (RUNPOD_API_KEY). They are different
    # secrets (docs/runpod-agent-smoke.md §9). If the owner has not
    # injected VLLM_API_KEY, the smoke synthesises ``sk-<pod_id>``
    # itself — so we omit the variable and let the documented fallback
    # take over.
    vllm_api_key_for_smoke = env_dict.get("VLLM_API_KEY") or None

    # ---- create / wait ----
    try:
        handle = lifecycle.start_pod(pod_config)
    except PodUnavailableError as exc:
        # The exception's ``args[0]`` carries the category literal that
        # the library raised — Seam S2 in the tightened plan. Only
        # ``create_failed`` and ``wait_timeout`` are produced by
        # ``ManageRunPodLifecycle.start_pod``; treat anything else as
        # ``create_failed`` defensively.
        raw = exc.args[0] if exc.args else _CATEGORY_CREATE_FAILED
        if raw in (_CATEGORY_CREATE_FAILED, _CATEGORY_WAIT_TIMEOUT):
            category = raw
        else:
            category = _CATEGORY_CREATE_FAILED
        _emit_category(category)
        _append_lifecycle_row(
            request_id=request_id, category=category, log_path=lifecycle_log
        )
        return EXIT_PHASE_FAILED

    _emit_category(_CATEGORY_CREATED)
    _append_lifecycle_row(
        request_id=request_id, category=_CATEGORY_CREATED, log_path=lifecycle_log
    )

    # If start_pod returned, the wait phase already succeeded (it logs
    # ``healthy`` internally on success, ``wait_timeout`` on failure
    # which would have raised above).
    _emit_category(_CATEGORY_HEALTHY)
    _append_lifecycle_row(
        request_id=request_id, category=_CATEGORY_HEALTHY, log_path=lifecycle_log
    )

    exit_codes: list[int] = []

    # ---- smoke ----
    smoke_passed, smoke_category = _run_smoke_subprocess(
        handle,
        vllm_api_key=vllm_api_key_for_smoke,
        repo_root=repo_root or Path(__file__).resolve().parent.parent,
        runner=smoke_runner,
    )
    if smoke_passed:
        _emit_category(_CATEGORY_SMOKE_PASSED)
        _append_lifecycle_row(
            request_id=request_id,
            category=_CATEGORY_SMOKE_PASSED,
            log_path=lifecycle_log,
        )
    else:
        _emit_category(_CATEGORY_SMOKE_FAILED)
        _append_lifecycle_row(
            request_id=request_id,
            category=_CATEGORY_SMOKE_FAILED,
            log_path=lifecycle_log,
        )
        exit_codes.append(EXIT_PHASE_FAILED)

    # ---- cleanup ----
    if keep_pod:
        _emit_category(_CATEGORY_KEPT)
        _append_lifecycle_row(
            request_id=request_id,
            category=_CATEGORY_KEPT,
            log_path=lifecycle_log,
        )
        exit_codes.append(EXIT_OK)
    else:
        try:
            lifecycle.stop_pod(handle, terminate=True)
        except PodUnavailableError:
            _emit_category(_CATEGORY_CLEANUP_FAILED)
            _emit_urgent(request_id, log_path=lifecycle_log)
            _append_lifecycle_row(
                request_id=request_id,
                category=_CATEGORY_CLEANUP_FAILED,
                log_path=lifecycle_log,
            )
            exit_codes.append(EXIT_CLEANUP_FAILED)
        else:
            _emit_category(_CATEGORY_DELETED)
            _append_lifecycle_row(
                request_id=request_id,
                category=_CATEGORY_DELETED,
                log_path=lifecycle_log,
            )

    return _select_exit_code(exit_codes)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _build_pod_config(args: argparse.Namespace) -> PodConfig:
    """Construct a :class:`PodConfig` from CLI flags.

    Each flag defaults to ``None`` so an unprovided flag falls back to
    the :class:`PodConfig` default; we only override fields the owner
    explicitly named. This keeps the GPU-type / disk-gb defaults
    centralised in :class:`PodConfig` rather than embedded in argparse
    help text.
    """
    overrides: dict[str, Any] = {}
    if args.gpu_type is not None:
        overrides["gpu_type"] = args.gpu_type
    if args.disk_gb is not None:
        overrides["disk_gb"] = args.disk_gb
    return PodConfig(**overrides) if overrides else PodConfig()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="manage_runpod",
        description=(
            "Agent-runnable RunPod lifecycle helper. Default policy: "
            "create → wait until healthy → run sanitised smoke → DELETE. "
            "Use --keep-pod to opt out of the delete step (rare; owner "
            "must remember to clean up manually). See "
            "docs/runpod-agent-lifecycle.md for the owner/agent split."
        ),
    )
    parser.add_argument(
        "--keep-pod",
        action="store_true",
        help=(
            "Do NOT delete the Pod after the smoke completes. Default is "
            "to delete; running Pods (and stopped Pods with retained "
            "storage) continue to bill — see docs/runpod.md §9."
        ),
    )
    parser.add_argument(
        "--gpu-type",
        default=None,
        help=(
            "Override the GPU type. Defaults to PodConfig.gpu_type when "
            "unset."
        ),
    )
    parser.add_argument(
        "--disk-gb",
        type=int,
        default=None,
        help="Override the container disk size in GB.",
    )
    args = parser.parse_args(argv)

    pod_config = _build_pod_config(args)
    return run_lifecycle(keep_pod=args.keep_pod, pod_config=pod_config)


__all__ = [
    "EXIT_OK",
    "EXIT_PHASE_FAILED",
    "EXIT_PREFLIGHT_FAILED",
    "EXIT_CLEANUP_FAILED",
    "main",
    "run_lifecycle",
]


if __name__ == "__main__":
    raise SystemExit(main())
