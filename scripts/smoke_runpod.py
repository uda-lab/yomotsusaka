#!/usr/bin/env python3
"""Owner-only L3 smoke script — single-shot vLLM health check.

Per issue #46 metaplan Fork 8, this script is explicitly NOT a merge gate.
It is owner-only, post-merge, off-critical-path. The L1 (mocked HTTP) and
L2 (mocked lifecycle) unit tests are the CI merge gate.

Run from the repository root with:

    RUNPOD_LIVE_SMOKE=1 \\
        VLLM_ENDPOINT=https://<pod-id>-8000.proxy.runpod.net \\
        VLLM_API_KEY=sk-<pod-id-or-override> \\
        python3 scripts/smoke_runpod.py

The script refuses to run unless ``RUNPOD_LIVE_SMOKE=1`` so an accidental
invocation in CI cannot hit a real pod. On success it prints the first
80 characters of ``choices[0].message.content`` from a single chat
completion against ``Qwen/Qwen3-8B``. On failure it exits non-zero with a
short diagnostic; no credentials are echoed.
"""

from __future__ import annotations

import os
import sys

# Import locally so the script never imports yomotsusaka.boundary (which
# would defeat the private-side import boundary).
from yomotsusaka.vllm_backend import VLLMBackend


def _require_env(name: str) -> str:
    """Return ``os.environ[name]`` or exit non-zero with a clear message.

    The env var name (key) is printed; the env var value is NEVER printed.
    """
    value = os.environ.get(name)
    if not value:
        print(f"error: {name} env var is required", file=sys.stderr)
        sys.exit(2)
    return value


def main() -> int:
    if os.environ.get("RUNPOD_LIVE_SMOKE") != "1":
        print(
            "refusing to run: set RUNPOD_LIVE_SMOKE=1 to enable the live "
            "L3 smoke check (cost-controlled; not a CI gate)",
            file=sys.stderr,
        )
        return 2

    endpoint = _require_env("VLLM_ENDPOINT")
    # VLLM_API_KEY is optional — if absent and RUNPOD_POD_ID is set, the
    # backend falls back to sk-<pod_id> per docs/runpod.md §6.
    pod_id = os.environ.get("RUNPOD_POD_ID")
    api_key = os.environ.get("VLLM_API_KEY")

    backend = VLLMBackend(
        endpoint=endpoint,
        model_id="Qwen/Qwen3-8B",
        api_key=api_key,
        pod_id=pod_id,
    )

    try:
        content = backend.generate(
            "日本語で短く自己紹介してください。",
            max_tokens=128,
        )
    except Exception as exc:  # noqa: BLE001 — top-level smoke script
        # Print only the exception type and reason literal (if any), never
        # the raw message — vLLM error bodies may include the endpoint URL.
        reason = getattr(exc, "reason", None)
        if reason is not None:
            print(
                f"error: smoke generate failed; type={type(exc).__name__} "
                f"reason={reason}",
                file=sys.stderr,
            )
        else:
            print(
                f"error: smoke generate failed; type={type(exc).__name__}",
                file=sys.stderr,
            )
        return 1

    snippet = content[:80].replace("\n", " ")
    print(f"ok: {snippet}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
