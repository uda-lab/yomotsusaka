"""
Operational report — public-safe markdown summary of an operational scenario.

This module is **agent-facing**. It consumes a structured
:class:`ScenarioResult` (one record per phase, plus counters) and renders a
GitHub-paste-safe markdown summary on stdout for an ordinary agent.

Privacy invariants (binding per ``docs/architecture.md`` §§4–7, §11)
------------------------------------------------------------------
The rendered markdown is at most ``agent_redacted``. It MUST NOT contain:

* vault filesystem paths,
* Pod identifiers or endpoint URLs,
* credentials (API keys, bearer tokens, secrets),
* backend response bodies,
* raw private dictionary values,
* manifest handles that resolve to private content.

Categories are the only failure evidence printed. Detail belongs in vault-side
logs only. A fail-closed redaction sweep (:func:`_scrub_report`) re-checks the
output against a curated set of sensitive shapes (vault paths, RunPod Pod IDs,
hex tokens, bearer-style headers, common URL schemes) before returning.

Coordination with sibling MVP-5 children
----------------------------------------
The :class:`ScenarioResult` dataclass is the shared contract with child 02
(#91). Child 02 produces a :class:`ScenarioResult` from a live scenario run;
child 03 (this module) renders it. Field names are stable so that child 06
(#95) can later annotate them with exposure-class metadata without renaming.

Coordination with child 04 (#93) — failure-taxonomy consolidation — is by
field name only: the ``category`` field on :class:`PhaseRecord` and the
``runpod_lifecycle_category`` counter both hold stable token strings that
child 04 may later canonicalise across modules. This module does not own the
vocabulary.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = [
    "PhaseStatus",
    "ResultState",
    "PhaseRecord",
    "ScenarioResult",
    "RedactionError",
    "classify_result_state",
    "render_report",
]


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


PhaseStatus = Literal["ok", "warn", "fail", "skipped"]
"""Status token for a single phase. Stable vocabulary; do not extend without
co-updating the classifier and child 04's consolidated taxonomy (#93)."""


ResultState = Literal[
    "completed",
    "completed_with_warnings",
    "failed_cleaned",
    "failed_owner_action",
]
"""Top-level scenario state. Exactly four values; see ``classify_result_state``
for the binding classification rule."""


@dataclass(frozen=True)
class PhaseRecord:
    """One phase of an operational scenario.

    Parameters
    ----------
    phase_name:
        Stable token identifying the phase (e.g. ``"batch"``,
        ``"index_snapshot"``, ``"restoration_request"``). Caller-controlled;
        public-safe — must not encode vault paths or Pod identifiers.
    status:
        One of ``"ok"``, ``"warn"``, ``"fail"``, ``"skipped"``.
    category:
        Stable category token (e.g. ``"batch_ok"``, ``"vault_unwritable"``,
        ``"wait_timeout"``). Public-safe; categories are the only failure
        evidence printed. Empty string for "no category applies" is allowed.
    """

    phase_name: str
    status: PhaseStatus
    category: str = ""


@dataclass(frozen=True)
class ScenarioResult:
    """Structured result of an operational scenario.

    This is the shared contract with child 02 (#91): one record per phase
    plus the public-safe counter set defined in the umbrella spec.

    Counters
    --------
    The ``counters`` mapping carries integer or short-string counts. The
    keys defined by the umbrella spec are:

    * ``processed_documents`` (int)
    * ``failed_documents`` (int)
    * ``index_snapshot_ok`` (bool)
    * ``index_loadable`` (bool)
    * ``search_smoke_ok`` (bool)
    * ``restoration_outcome`` (str — stable token, e.g. ``"ok"``, ``"denied"``)
    * ``audit_row_count`` (int)
    * ``runpod_lifecycle_category`` (str, optional — present when the RunPod
      phase ran)

    Additional caller keys are tolerated; the renderer prints them in
    insertion order. All values are coerced to ``str`` at render time and
    pass through the same redaction sweep as the rest of the report.

    ``runpod_cleanup_confirmed`` (bool, optional) — used by
    :func:`classify_result_state` to discriminate ``failed_cleaned`` from
    ``failed_owner_action``. When omitted on a failing scenario, the
    classifier defaults to ``failed_owner_action`` (fail-closed).
    """

    phases: tuple[PhaseRecord, ...]
    counters: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# State classification
# ---------------------------------------------------------------------------


def classify_result_state(result: ScenarioResult) -> ResultState:
    """Classify the four-state result token per the umbrella spec.

    Rules (binding):

    * ``completed`` iff every phase has ``status="ok"`` (``skipped`` phases
      are ignored — they neither succeed nor fail).
    * ``completed_with_warnings`` iff at least one phase is ``status="warn"``
      and no phase is ``status="fail"``.
    * ``failed_cleaned`` iff at least one phase is ``status="fail"`` AND
      RunPod resources were successfully deleted (``runpod_cleanup_confirmed``
      is True) or were never created (no ``runpod_lifecycle_category``
      counter present).
    * ``failed_owner_action`` iff at least one phase is ``status="fail"`` AND
      the agent could not confirm clean teardown (fail-closed default when
      ``runpod_cleanup_confirmed`` is absent or False on a failing scenario
      that did touch RunPod).
    """
    statuses = {phase.status for phase in result.phases}

    if "fail" in statuses:
        runpod_touched = "runpod_lifecycle_category" in result.counters
        cleanup_confirmed = bool(result.counters.get("runpod_cleanup_confirmed", False))
        if not runpod_touched or cleanup_confirmed:
            return "failed_cleaned"
        return "failed_owner_action"

    if "warn" in statuses:
        return "completed_with_warnings"

    # No fails, no warns. "ok" or "skipped" only.
    return "completed"


# ---------------------------------------------------------------------------
# Redaction sweep
# ---------------------------------------------------------------------------


class RedactionError(Exception):
    """Raised by :func:`_scrub_report` when the fail-closed re-check finds
    a sensitive shape in the rendered report.

    The exception message identifies the *category* of the leak only and
    NEVER echoes the offending substring (which by construction is the
    private value we are trying not to leak).
    """


# Shape detectors. Each pattern matches a class of sensitive token that must
# not appear in a public-safe report. Patterns are intentionally permissive
# on the "looks like X" side and conservative on what counts as a fixture
# value (handled by the caller's tests).
#
# The list mirrors the boundary surfaces already audited elsewhere in the
# repo:
#
# * ``/manifests/...`` / ``/private/...`` / ``/audit/...`` — vault layout
#   (matches ``scrubber._VAULT_PATH_PATTERN`` plus generic vault prefixes).
# * ``http(s)://`` URLs — endpoint leaks (RunPod, vLLM, etc.).
# * RunPod-style Pod IDs (``runpod-...``, ``pod-...``).
# * Bearer tokens (``Bearer <token>``).
# * Long hex strings (>= 32 chars) — API keys, sha256, etc.
# * Absolute filesystem paths under common vault roots.
_LEAK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "vault_path",
        re.compile(
            r"/(?:manifests|private|audit|search-index)/[A-Za-z0-9._-]{1,128}"
        ),
    ),
    (
        "url",
        re.compile(r"\bhttps?://[^\s<>]+"),
    ),
    (
        "pod_id",
        re.compile(r"\b(?:runpod|pod)-[A-Za-z0-9]{6,}\b"),
    ),
    (
        "bearer_token",
        re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{8,}"),
    ),
    (
        "long_hex",
        re.compile(r"\b[0-9a-fA-F]{32,}\b"),
    ),
)


def _scrub_report(rendered: str) -> str:
    """Fail-closed re-scan: raise :class:`RedactionError` if the rendered
    report contains any sensitive shape.

    The renderer is responsible for not producing these shapes in the first
    place; this sweep is the safety net that turns a programmer mistake into
    a hard failure instead of a silent leak.
    """
    for category, pattern in _LEAK_PATTERNS:
        if pattern.search(rendered):
            raise RedactionError(
                f"rendered report contains a sensitive shape ({category}); "
                "refusing to emit"
            )
    return rendered


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------


# Counter keys printed in this fixed order at the top of the Counters
# section. Extra keys (caller-supplied) follow in their insertion order.
_CANONICAL_COUNTERS: tuple[str, ...] = (
    "processed_documents",
    "failed_documents",
    "index_snapshot_ok",
    "index_loadable",
    "search_smoke_ok",
    "restoration_outcome",
    "audit_row_count",
    "runpod_lifecycle_category",
)


# Counters that exist purely to drive the classifier (``classify_result_state``)
# and are NOT rendered in the Counters list. Keeping them out of the output
# avoids exposing internal boolean state that is already encoded in the
# top-level Result token.
_INTERNAL_COUNTERS: frozenset[str] = frozenset({"runpod_cleanup_confirmed"})


def _format_counter_value(value: Any) -> str:
    """Render a counter value as a public-safe string.

    Booleans render as ``true`` / ``false`` (markdown-safe lower-case).
    Integers and strings render via ``str()``. Other types are coerced via
    ``str()`` for forward compatibility but should be avoided by callers.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _render_owner_action_section(result: ScenarioResult) -> str:
    """Render the ``## Owner action required`` section.

    Contains a minimal pointer only — no raw evidence, no vault paths, no
    Pod identifiers. The fixed copy intentionally directs the operator to
    out-of-band vault-side logs rather than echoing any detail through the
    public surface.
    """
    failing_phases = [p for p in result.phases if p.status == "fail"]
    failing_tokens = ", ".join(
        f"`{p.phase_name}` ({p.category or 'no_category'})" for p in failing_phases
    )
    if not failing_tokens:
        failing_tokens = "(no failing phase recorded)"
    return (
        "## Owner action required\n"
        "\n"
        "Operational scenario did not complete cleanly and the agent could "
        "not confirm safe teardown. Inspect vault-side audit logs and "
        "RunPod console for the failing phase below; no public-safe detail "
        "is included here by design.\n"
        "\n"
        f"- failing phase(s): {failing_tokens}\n"
    )


def render_report(result: ScenarioResult) -> str:
    """Render *result* as a public-safe markdown report.

    The report is GitHub-paste-safe: it is at most ``agent_redacted`` and
    passes a fail-closed redaction sweep (:func:`_scrub_report`) before
    being returned.

    Sections (in fixed order):

    1. ``## Result`` — single state token.
    2. ``## Phases`` — table of ``phase | status | category``.
    3. ``## Counters`` — bulleted list of the umbrella-spec counters.
    4. ``## Owner action required`` — included only when the state is
       ``failed_owner_action``.

    Raises
    ------
    RedactionError
        If the rendered markdown contains a sensitive shape. This is a
        fail-closed guard against renderer bugs; callers should treat it
        as a hard failure (e.g. drop the report on the floor and log a
        category-only error) rather than retrying.
    """
    state = classify_result_state(result)

    buf = io.StringIO()

    # 1. Result
    buf.write("## Result\n\n")
    buf.write(f"{state}\n\n")

    # 2. Phases table
    buf.write("## Phases\n\n")
    buf.write("| phase | status | category |\n")
    buf.write("| --- | --- | --- |\n")
    for phase in result.phases:
        category = phase.category if phase.category else "-"
        buf.write(f"| {phase.phase_name} | {phase.status} | {category} |\n")
    buf.write("\n")

    # 3. Counters list (canonical order, then any extras in insertion order)
    buf.write("## Counters\n\n")
    emitted: set[str] = set()
    for key in _CANONICAL_COUNTERS:
        if key in result.counters:
            value = _format_counter_value(result.counters[key])
            buf.write(f"- {key}: {value}\n")
            emitted.add(key)
    for key, value in result.counters.items():
        if key in emitted or key in _INTERNAL_COUNTERS:
            continue
        buf.write(f"- {key}: {_format_counter_value(value)}\n")
    buf.write("\n")

    # 4. Owner-action section (conditional)
    if state == "failed_owner_action":
        buf.write(_render_owner_action_section(result))

    rendered = buf.getvalue()
    return _scrub_report(rendered)
