"""Shared deny-list and canonical-fixture constants for exposure-contract tests.

This module centralises the fixture-only sentinel strings used by both the
MVP-2 leakage scan (:mod:`tests.test_exposure_contract`, issue #29) and the
MVP-3 widening (:mod:`tests.test_exposure_contract_mvp3`, issue #47), and
the boundary-symbol roster reused by the MVP-5 boundary-registry drift
tests (:mod:`tests.test_boundary_registry_drift`, issue #95).

All strings declared here are **fixture-only sentinels**. They MUST NEVER
appear in any agent-facing return, log line, manifest, search result, or
audit echo regardless of which backend (DummyBackend, vLLM, RunPod-attached,
etc.) is in use. Per the §5 non-weakening clause in
``docs/backend-promotion.md`` and per the project ``CLAUDE.md`` rule, raw
private literals live only here and in the canonical fixture body — never in
expected-value assertions against a public response.

The deny-list is intentionally narrow: it pins the specific sentinel values
used by the umbrella #4 canonical fixture plus MVP-3 mock sentinels for
RunPod / vLLM / Chikaeshi-dispatcher surfaces. Backend implementers seeding
their own mocks SHOULD reuse these sentinels (rather than inventing their
own) so the exposure scan recognises them.
"""

from __future__ import annotations

import re

from yomotsusaka.schemas import EntityKind

# Forward-declared so :mod:`yomotsusaka.boundary` symbols imported below
# do not trigger a circular re-export.
from yomotsusaka.boundary import SpanSpec


# ---------------------------------------------------------------------------
# Canonical fixture (umbrella #4)
# ---------------------------------------------------------------------------

CANONICAL_TEXT: str = "Alice Tan works at Acme Corp. Patient ID: 12345."
"""The umbrella #4 canonical fixture text. Single source of truth."""

# Offsets are verified at module import so this stays in lockstep with
# CANONICAL_TEXT if the fixture ever drifts.
CANONICAL_SPANS: tuple[SpanSpec, ...] = (
    SpanSpec(start=0, end=9, kind=EntityKind.PERSON),
    SpanSpec(start=19, end=28, kind=EntityKind.ORG),
    SpanSpec(start=42, end=47, kind=EntityKind.ID_NUMBER),
)

RAW_VALUES: tuple[str, ...] = ("Alice Tan", "Acme Corp", "12345")
"""Raw private values from the canonical fixture; never permitted on any
agent-facing surface."""

assert CANONICAL_TEXT[0:9] == "Alice Tan"
assert CANONICAL_TEXT[19:28] == "Acme Corp"
assert CANONICAL_TEXT[42:47] == "12345"


# ---------------------------------------------------------------------------
# Vault-shape path-leak detectors
# ---------------------------------------------------------------------------

PATH_LEAK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"/manifests/[^/\"\\]+\.json"),
    re.compile(r"/private/[^/\"\\]+\.json"),
)


# ---------------------------------------------------------------------------
# MVP-3 mock sentinels
# ---------------------------------------------------------------------------
#
# Each tuple holds a small set of fixture-only sentinel strings that any
# MVP-3 candidate-provider mock SHOULD inject into its raw / pre-scrub bytes,
# so the exposure-contract scan can verify the scrubber strips them. None of
# these strings may appear in an agent-facing return — leaking any of them
# is a privacy failure regardless of backend.

MOCK_UNSCRUBBED_SENTINELS: tuple[str, ...] = (
    "<<UNSCRUBBED>>",
    "<<RAW_STDOUT_SENTINEL>>",
    "<<RAW_STDERR_SENTINEL>>",
)
"""Sentinels a candidate provider injects into pre-scrub subprocess bytes.

A scrubber that fails to strip these from agent-facing I/O fragments fails
the abstract scrubbed-I/O contract."""

MOCK_POD_ID_SENTINELS: tuple[str, ...] = (
    "pod-LEAK-SENTINEL-AAA",
    "pod-LEAK-SENTINEL-BBB",
)
"""RunPod pod-id sentinels. The opaque ``PodHandle`` must never echo these
back through any agent-facing surface; the locator string is the only
permitted identity."""

MOCK_ENDPOINT_URL_SENTINELS: tuple[str, ...] = (
    "https://leak-sentinel.runpod.example/api",
    "http://192.0.2.1:8000",  # TEST-NET-1; safe to use as a sentinel
)
"""Endpoint URLs that the agent-facing surface must never echo. A pod
endpoint is private routing information; it stays vault-side."""

MOCK_TENANT_ID_SENTINELS: tuple[str, ...] = (
    "tenant-LEAK-SENTINEL",
)
"""Tenant-id sentinels for the (future) tenant-scoped vault. Any tenant-
scoped fixture that parameterises on this id must assert it never reaches
public surfaces."""

MOCK_APPROVAL_TICKET_SENTINELS: tuple[str, ...] = (
    "approval-LEAK-SENTINEL-0001",
    "approval-LEAK-SENTINEL-0002",
)
"""Restoration-audit ``approval_ticket`` sentinels."""

MOCK_POLICY_PROFILE_SENTINELS: tuple[str, ...] = (
    "policy-LEAK-SENTINEL-strict",
    "policy-LEAK-SENTINEL-lenient",
)
"""Restoration-audit ``policy_profile`` sentinels."""


# ---------------------------------------------------------------------------
# Aggregate convenience
# ---------------------------------------------------------------------------

ALL_MVP3_SENTINELS: tuple[str, ...] = (
    MOCK_UNSCRUBBED_SENTINELS
    + MOCK_POD_ID_SENTINELS
    + MOCK_ENDPOINT_URL_SENTINELS
    + MOCK_TENANT_ID_SENTINELS
    + MOCK_APPROVAL_TICKET_SENTINELS
    + MOCK_POLICY_PROFILE_SENTINELS
)
"""Union of every MVP-3 sentinel. Convenient for ``assert needle not in blob``
loops in the abstract contract classes."""


# ---------------------------------------------------------------------------
# Boundary symbol roster (shared with tests.test_boundary_registry_drift, #95)
# ---------------------------------------------------------------------------
#
# Every public response type whose serialisation flows through the
# :mod:`tests.test_exposure_contract` scan. The boundary-registry drift
# tests (issue #95, child 06 of MVP-5) also consume this constant so the
# two suites pin the same surface set. If ``boundary.__all__`` ever grows a
# new response, this set must grow to match (or the new response must be
# added to one of the per-surface tests in ``test_exposure_contract.py``).

EXPECTED_BOUNDARY_SYMBOLS: frozenset[str] = frozenset(
    {
        "PublicHandle",
        "PublicManifestView",
        "ProcessResponse",
        "InspectResponse",
        "SearchHit",
        "SearchResponse",
        "RestorationResponse",
        "StatusReportResponse",
        "ResolverFailure",
        "ResolverFailureReason",
        "ResolverScope",
        "ResolverSuccess",
        "PrivateState",
        "ExecutionResponse",
        "parse_locator",
        "build_locator",
        "process_document_request",
        "inspect_request",
        "search_request",
        "restoration_request",
        "status_report_request",
        "execute_request",
        "resolve",
    }
)
"""Boundary symbols (responses + entry points) covered by the public-surface
scan and the boundary-field registry. Single source of truth shared between
``test_exposure_contract.py`` and ``test_boundary_registry_drift.py``."""


__all__ = [
    "CANONICAL_TEXT",
    "CANONICAL_SPANS",
    "RAW_VALUES",
    "PATH_LEAK_PATTERNS",
    "MOCK_UNSCRUBBED_SENTINELS",
    "MOCK_POD_ID_SENTINELS",
    "MOCK_ENDPOINT_URL_SENTINELS",
    "MOCK_TENANT_ID_SENTINELS",
    "MOCK_APPROVAL_TICKET_SENTINELS",
    "MOCK_POLICY_PROFILE_SENTINELS",
    "ALL_MVP3_SENTINELS",
    "EXPECTED_BOUNDARY_SYMBOLS",
]
