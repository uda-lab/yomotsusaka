#!/usr/bin/env python3
"""Owner-provisioned, agent-runnable L3 smoke for the RunPod/vLLM endpoint.

Per issue #46 metaplan Fork 8, this script is explicitly NOT a merge gate.
It is post-merge, off-critical-path. The L1 (mocked HTTP) and L2 (mocked
lifecycle) unit tests are the CI merge gate.

Owner/agent split (see ``docs/runpod-agent-smoke.md`` for the full handoff):

* The **owner** provisions/stops the Pod, chooses or rotates the temporary
  vLLM bearer key, and injects ``VLLM_ENDPOINT``/``VLLM_API_KEY``
  (optionally ``RUNPOD_POD_ID``) into the dev container.
* The **agent** runs this script (default ``--mode generate`` for the legacy
  one-shot generate, or ``--mode diagnose`` for sanitised classification
  probes) and reports the public-safe classification.

Run from the repository root, after the owner has injected the secrets:

    RUNPOD_LIVE_SMOKE=1 python3 scripts/smoke_runpod.py --mode diagnose

The script refuses to run unless ``RUNPOD_LIVE_SMOKE=1`` so an accidental
invocation in CI cannot hit a real Pod. ``--mode generate`` retains the
historical behaviour: a single chat completion against ``Qwen/Qwen3-8B``,
printing the first 80 characters of ``choices[0].message.content`` on
success. ``--mode diagnose`` reports a stable public-safe failure category
suitable for echoing in a GitHub comment (see the category list below).

Sanitisation invariant
----------------------

Neither mode prints ``VLLM_ENDPOINT``, ``VLLM_API_KEY``, ``RUNPOD_POD_ID``,
the raw response body, the bearer header, or full exception messages from
``httpx``. Diagnostics report only the category literal and, for the
``success`` category, at most 80 characters of the generated content
(never the request URL or response headers).
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

import httpx

# Import locally so the script never imports yomotsusaka.boundary (which
# would defeat the private-side import boundary).
from yomotsusaka.vllm_backend import VLLMBackend

# ---------------------------------------------------------------------------
# Public-safe diagnostic categories
# ---------------------------------------------------------------------------

DiagnosticCategory = Literal[
    "endpoint_unset",
    "endpoint_unreachable",
    "health_non_200",
    "auth_failure",
    "model_mismatch_or_bad_request",
    "backend_not_ready",
    "malformed_response",
    "success",
]
"""Stable wire identifiers for the agent-facing diagnostic output.

These literals are intentionally narrow and stable so an agent can route on
the category without inspecting secrets. They are safe to echo in a GitHub
PR/issue comment; the raw endpoint URL, bearer key, Pod ID, response
body, or full exception text are NEVER included alongside them.
"""

_PROBE_TIMEOUT_SECONDS = 5
_GENERATE_TIMEOUT_SECONDS = 30
_DIAGNOSTIC_MODEL_ID = "Qwen/Qwen3-8B"
_DIAGNOSTIC_PROMPT = "ping"
_DIAGNOSTIC_MAX_TOKENS = 16


@dataclass(frozen=True)
class DiagnosticResult:
    """Result of one diagnostic probe.

    ``category`` is always set to a :data:`DiagnosticCategory` literal.
    ``snippet`` is populated only for the ``success`` category and is
    truncated to 80 characters with newlines collapsed. No other field
    derives from the response body, URL, or headers — so the dataclass is
    safe to format into agent-facing output.
    """

    category: DiagnosticCategory
    snippet: str | None = None

    def format_line(self) -> str:
        if self.category == "success" and self.snippet is not None:
            return f"diagnostic: success snippet={self.snippet}"
        return f"diagnostic: {self.category}"


# ---------------------------------------------------------------------------
# Diagnostic probe (mode=diagnose)
# ---------------------------------------------------------------------------


def _classify_status(status: int) -> DiagnosticCategory | None:
    """Map an HTTP status code from ``/v1/chat/completions`` to a category.

    Returns ``None`` when the status is 2xx (the caller must continue with
    response-body parsing). All non-2xx codes resolve to one of the stable
    diagnostic categories; the raw status integer is NOT echoed.
    """
    if 200 <= status < 300:
        return None
    if status in (401, 403):
        return "auth_failure"
    if status == 400:
        return "model_mismatch_or_bad_request"
    if status == 503 or 500 <= status < 600:
        return "backend_not_ready"
    # Other 4xx (404, 405, 409, 429, ...) treated as model/bad-request
    # rather than auth or backend. The wire-stable category set in the
    # issue body does not enumerate them separately.
    return "model_mismatch_or_bad_request"


def _probe_endpoint_reachable(endpoint: str) -> bool:
    """Best-effort DNS + TCP reachability check against the endpoint host.

    Returns ``True`` when the hostname resolves and a TCP connect attempt
    succeeds (or the kernel returns a non-network error such as
    "connection refused", which still indicates the host is routable).
    Returns ``False`` only when DNS fails or the kernel reports
    ``EHOSTUNREACH``/``ENETUNREACH``/timeout — the conditions we classify
    as ``endpoint_unreachable``.

    Any exception raised here is swallowed; only the boolean reaches the
    caller, so a stringified ``socket`` error cannot leak into the output.
    """
    try:
        parts = urlsplit(endpoint)
    except Exception:
        return False
    host = parts.hostname
    if not host:
        return False
    port = parts.port
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    except Exception:
        return False
    for family, socktype, proto, _canon, sockaddr in infos:
        sock = None
        try:
            sock = socket.socket(family, socktype, proto)
            sock.settimeout(_PROBE_TIMEOUT_SECONDS)
            sock.connect(sockaddr)
            return True
        except (socket.timeout, TimeoutError):
            continue
        except OSError as exc:
            # ECONNREFUSED / similar — host is reachable, port closed.
            # Treat as reachable so we surface the higher-layer error.
            errno = getattr(exc, "errno", None)
            if errno in (
                111,  # ECONNREFUSED
                104,  # ECONNRESET
                113,  # EHOSTUNREACH
                101,  # ENETUNREACH
            ):
                if errno in (111, 104):
                    return True
                continue
            return False
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
    return False


def _build_headers(api_key: str | None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _probe_health(
    endpoint: str,
    *,
    api_key: str | None,
    transport: httpx.BaseTransport | None = None,
) -> DiagnosticCategory | None:
    """Probe ``GET {endpoint}/health``.

    Returns ``None`` when the endpoint responds 200 or when no
    ``/health`` route exists (404 — vLLM templates without the route
    should not be classified as unhealthy). Returns the appropriate
    diagnostic category otherwise. Network-layer errors map to
    ``endpoint_unreachable``; any other non-2xx status maps to
    ``health_non_200``.
    """
    url = f"{endpoint.rstrip('/')}/health"
    try:
        client = (
            httpx.Client(timeout=_PROBE_TIMEOUT_SECONDS, transport=transport)
            if transport is not None
            else httpx.Client(timeout=_PROBE_TIMEOUT_SECONDS)
        )
        with client:
            response = client.get(url, headers=_build_headers(api_key))
    except httpx.HTTPError:
        return "endpoint_unreachable"
    if response.status_code == 200:
        return None
    if response.status_code == 404:
        # No /health route on this template; defer to the chat probe.
        return None
    if response.status_code in (401, 403):
        # Some templates auth-gate /health; treat as auth failure so the
        # agent doesn't waste a paid generate call.
        return "auth_failure"
    return "health_non_200"


def _probe_chat_completions(
    endpoint: str,
    *,
    api_key: str | None,
    model_id: str,
    transport: httpx.BaseTransport | None = None,
) -> DiagnosticResult:
    """Issue one minimal chat-completion request and classify the outcome.

    The request body is intentionally tiny (``"ping"``, 16 max_tokens) so
    a paid Pod is not exercised more than necessary. The response is
    classified into a :data:`DiagnosticCategory`; on success, the first
    80 characters of ``choices[0].message.content`` are returned.

    All ``httpx`` exception messages are caught and replaced with a
    category literal — they may include the URL, headers, or raw body and
    must never reach the agent-facing surface.
    """
    url = f"{endpoint.rstrip('/')}/v1/chat/completions"
    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": _DIAGNOSTIC_PROMPT}],
        "temperature": 0.0,
        "max_tokens": _DIAGNOSTIC_MAX_TOKENS,
    }
    try:
        client = (
            httpx.Client(timeout=_GENERATE_TIMEOUT_SECONDS, transport=transport)
            if transport is not None
            else httpx.Client(timeout=_GENERATE_TIMEOUT_SECONDS)
        )
        with client:
            response = client.post(
                url, json=payload, headers=_build_headers(api_key)
            )
    except httpx.HTTPError:
        return DiagnosticResult(category="endpoint_unreachable")

    status_category = _classify_status(response.status_code)
    if status_category is not None:
        return DiagnosticResult(category=status_category)

    try:
        body_text = response.text or ""
        data = json.loads(body_text)
        content = data["choices"][0]["message"]["content"]
    except (TypeError, ValueError, KeyError, IndexError):
        return DiagnosticResult(category="malformed_response")

    if not isinstance(content, str):
        return DiagnosticResult(category="malformed_response")

    snippet = content[:80].replace("\n", " ")
    return DiagnosticResult(category="success", snippet=snippet)


def run_diagnostics(
    *,
    endpoint: str | None,
    api_key: str | None,
    pod_id: str | None,
    model_id: str = _DIAGNOSTIC_MODEL_ID,
    transport: httpx.BaseTransport | None = None,
) -> DiagnosticResult:
    """Run the full diagnostic sequence and return the first classification.

    The sequence is:

    1. ``endpoint_unset`` if ``endpoint`` is falsy.
    2. ``endpoint_unreachable`` if DNS/TCP probe fails.
    3. ``/health`` probe — may return ``auth_failure`` /
       ``health_non_200`` / ``endpoint_unreachable``.
    4. ``/v1/chat/completions`` probe — produces one of the remaining
       categories including ``success``.

    The returned :class:`DiagnosticResult` is always safe to format into
    agent-facing output via :meth:`DiagnosticResult.format_line`.
    """
    if not endpoint:
        return DiagnosticResult(category="endpoint_unset")

    if transport is None and not _probe_endpoint_reachable(endpoint):
        return DiagnosticResult(category="endpoint_unreachable")

    # Resolve the effective API key the same way VLLMBackend does, so the
    # diagnostic probe authenticates identically to the real generate call.
    resolved_api_key: str | None
    if api_key:
        resolved_api_key = api_key
    elif pod_id:
        resolved_api_key = f"sk-{pod_id}"
    else:
        resolved_api_key = None

    health_category = _probe_health(
        endpoint, api_key=resolved_api_key, transport=transport
    )
    if health_category is not None:
        return DiagnosticResult(category=health_category)

    return _probe_chat_completions(
        endpoint,
        api_key=resolved_api_key,
        model_id=model_id,
        transport=transport,
    )


# ---------------------------------------------------------------------------
# Legacy ``generate`` mode
# ---------------------------------------------------------------------------


def _require_env(name: str) -> str:
    """Return ``os.environ[name]`` or exit non-zero with a clear message.

    The env var name (key) is printed; the env var value is NEVER printed.
    """
    value = os.environ.get(name)
    if not value:
        print(f"error: {name} env var is required", file=sys.stderr)
        sys.exit(2)
    return value


def _run_generate_mode() -> int:
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


def _run_diagnose_mode() -> int:
    # Read but do NOT print endpoint/key/pod-id values. Only the category
    # literal reaches stdout/stderr.
    endpoint = os.environ.get("VLLM_ENDPOINT")
    api_key = os.environ.get("VLLM_API_KEY")
    pod_id = os.environ.get("RUNPOD_POD_ID")

    result = run_diagnostics(endpoint=endpoint, api_key=api_key, pod_id=pod_id)
    print(result.format_line())
    return 0 if result.category == "success" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="smoke_runpod",
        description=(
            "Owner-provisioned, agent-runnable RunPod/vLLM smoke. Requires "
            "RUNPOD_LIVE_SMOKE=1; reads VLLM_ENDPOINT / VLLM_API_KEY / "
            "RUNPOD_POD_ID from the environment. See "
            "docs/runpod-agent-smoke.md for the owner-to-agent handoff."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=("generate", "diagnose"),
        default="generate",
        help=(
            "generate (default): legacy single chat completion; prints "
            "the first 80 chars of the response. diagnose: run sanitised "
            "reachability/health/chat probes and print one stable "
            "DiagnosticCategory literal."
        ),
    )
    args = parser.parse_args(argv)

    if os.environ.get("RUNPOD_LIVE_SMOKE") != "1":
        print(
            "refusing to run: set RUNPOD_LIVE_SMOKE=1 to enable the live "
            "L3 smoke check (cost-controlled; not a CI gate)",
            file=sys.stderr,
        )
        return 2

    if args.mode == "diagnose":
        return _run_diagnose_mode()
    return _run_generate_mode()


__all__ = [
    "DiagnosticCategory",
    "DiagnosticResult",
    "run_diagnostics",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
