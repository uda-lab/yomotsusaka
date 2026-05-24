#!/usr/bin/env python3
"""
gate-keeper check family D: canonical-vocabulary drift.

Two independent checks:

* **D1** (``check_operational_category_drift``) — every backtick-quoted
  token in the doc / CLI scan set that *looks like* a category token
  (``<prefix>_<suffix>`` where ``prefix`` is one of
  ``{batch, index, search, restoration, audit, runpod, inference}``)
  must be a member of :class:`yomotsusaka.operational_taxonomy.OperationalCategory`
  or appear in the documented synonym allowlist.

* **D2** (``check_exposure_class_drift``) — exposure-class names
  (``agent_public`` / ``agent_redacted`` / ``private`` / ``restricted`` /
  ``never_expose``) in the doc scan set must be members of
  :data:`yomotsusaka.boundary.EXPOSURE_CLASSES`; conversely, every
  member of :data:`EXPOSURE_CLASSES` should appear at least once in
  the docs (undocumented members emit a warning).

Invoke:

```sh
uv run python scripts/gatekeeper/check_vocab_drift.py
uv run python scripts/gatekeeper/check_vocab_drift.py --json /tmp/vocab.json
```

Exit codes:

* ``0`` — all checks pass.
* ``1`` — at least one ``error`` finding (D1 mismatch, D2 mismatch).
* ``2`` — only ``warning`` findings (D2 undocumented member).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Make scripts/gatekeeper/_common.py importable when invoked directly.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402  (path manipulation above)
    Finding,
    collect_docs,
    emit_human_readable,
    exit_code_from,
    write_json_report,
)

__all__ = [
    "CATEGORY_TOKEN_RE",
    "EXPOSURE_TOKEN_RE",
    "CATEGORY_PREFIXES",
    "CATEGORY_SYNONYM_ALLOWLIST",
    "check_operational_category_drift",
    "check_exposure_class_drift",
    "main",
]


# ---------------------------------------------------------------------------
# Regexes & static configuration
# ---------------------------------------------------------------------------

# Matches a backtick-quoted lowercase identifier. The regex
# deliberately matches single-backtick spans; runs of double
# backticks (RST style, used in Python docstrings) are matched on
# the *inner* identifier — for example ```` ``index_snapshot`` ````
# will match ``index_snapshot`` once. That is intentional: both
# Markdown and RST docs participate in the canonical-token surface.
CATEGORY_TOKEN_RE = re.compile(r"`(?P<tok>[a-z][a-z0-9_]*)`")

# Matches a quoted Python string literal carrying a lowercase
# snake_case identifier — e.g. ``"batch_ok"`` or ``'batch_ok'``. The
# CLI modules emit category values as bare string literals
# (``_CAT_OK_BATCH = "batch_ok"``), and the issue #115 spec
# explicitly names the CLI files as part of the D1 scan set:
# *every category token name appearing in CLI output, JSON schemas,
# or docs is a member of OperationalCategory*. Without this regex,
# drift in runtime literals would slip past D1 (flagged by codex on
# PR #118).
PY_STRING_TOKEN_RE = re.compile(r"['\"](?P<tok>[a-z][a-z0-9_]*)['\"]")

# Closed enumeration: only these five tokens are interesting.
EXPOSURE_TOKEN_RE = re.compile(
    r"`(?P<tok>agent_public|agent_redacted|private|restricted|never_expose)`"
)

# Category-token prefix set. Tokens with these prefixes are
# *candidates* for D1 drift; tokens with any other prefix are out of
# scope (they belong to other vocabularies — module names, field
# names, etc.).
CATEGORY_PREFIXES: frozenset[str] = frozenset(
    {"batch", "index", "search", "restoration", "audit", "runpod", "inference"}
)

# Documented synonyms / sibling vocabulary that *share* the
# CATEGORY_PREFIXES alphabet but are NOT category tokens. These are
# enumerated explicitly so D1 stays strict — adding a synonym is a
# code change that lands alongside the doc change.
#
# Buckets, with rationale per cluster:
#
# * **Phase names** — the operational scenario CLI (#91) names each
#   phase ``batch``/``index_snapshot``/``index_reload``/``search_smoke``/
#   ``restoration_request``/``audit_inspect``/``runpod_lifecycle``. The
#   category tokens then suffix each phase with ``_ok``/``_failed``/
#   etc. These phase names are legitimate non-category vocabulary.
#
# * **Module names** — ``restoration_api``, ``search_gateway``,
#   ``batch_runner``, ``audit_log`` appear in source-of-truth docs as
#   bare module references.
#
# * **Field names** — ``audit_record_id``, ``audit_file_missing``,
#   ``audit_write_failed`` (a wire failure-reason on a different
#   enum, ``RestorationFailureReason``) appear in the operational
#   smoke and the error taxonomy.
#
# When #111 canonicalizes additional synonyms, append them here.
CATEGORY_SYNONYM_ALLOWLIST: frozenset[str] = frozenset(
    {
        # Phase names (subsets of category tokens).
        "index_snapshot",
        "index_reload",
        "search_smoke",
        "restoration_request",
        "audit_inspect",
        "runpod_lifecycle",
        # Module names.
        "restoration_api",
        "search_gateway",
        "batch_runner",
        "audit_log",
        # Field names / wire failure-reason tokens on sibling enums.
        "audit_record_id",
        "audit_file_missing",
        "audit_write_failed",
        # ---------------------------------------------------------------
        # Pre-#111 baseline: the operational_smoke CLI emits its own
        # local category vocabulary that runs parallel to (and is
        # documented to be canonicalized against)
        # OperationalCategory under issue #111. Listing them here
        # keeps the check passing on tip-of-main per #115 acceptance
        # criteria while still catching any NEW drift introduced
        # post-#115. #111 trims this bucket as it canonicalizes each
        # token in src/yomotsusaka/cli/operational_smoke.py.
        # ---------------------------------------------------------------
        "batch_committed",
        "batch_no_documents",
        "batch_all_failed",
        "batch_partial_commit",
        "batch_infrastructure_error",
        "index_reloaded",
        "index_reload_failed",
        "restoration_request_recorded",
        "restoration_request_unexpected_outcome",
        "audit_present",
        "audit_record_not_found",
        "runpod_cycle_complete",
        "runpod_disabled",
        "runpod_kept",
        "search_no_hits",
    }
)


# ---------------------------------------------------------------------------
# Scan set
# ---------------------------------------------------------------------------


def _d1_scan_set(repo_root: Path) -> list[Path]:
    """D1 scans docs + the two operational CLI modules.

    The CLI files are included because category tokens surface there
    as string literals (``"batch_ok"`` etc.) and also as backtick-quoted
    references inside docstrings. Both shapes participate in the
    canonical-token surface that #115's vocab-drift check defends.
    """

    out = collect_docs(repo_root)
    for rel in (
        "src/yomotsusaka/cli/operational_smoke.py",
        "src/yomotsusaka/cli/operational_report.py",
    ):
        p = repo_root / rel
        if p.is_file():
            out.append(p)
    return out


def _d2_scan_set(repo_root: Path) -> list[Path]:
    return collect_docs(repo_root)


# ---------------------------------------------------------------------------
# D1 — OperationalCategory drift
# ---------------------------------------------------------------------------


def _load_canonical_categories() -> frozenset[str]:
    """Read :class:`OperationalCategory` members live at runtime.

    This is the design lever that makes #115 independent of #111: the
    check adapts to whatever canonical set ``yomotsusaka.operational_taxonomy``
    settles on, without any code change here.
    """

    import yomotsusaka.operational_taxonomy as ot  # local import

    return frozenset(c.value for c in ot.OperationalCategory)


def check_operational_category_drift(repo_root: Path) -> list[Finding]:
    """D1: flag non-canonical category tokens in the scan set.

    A token ``tok`` is flagged when:

    1. ``tok`` has shape ``<prefix>_<suffix>`` with ``prefix in CATEGORY_PREFIXES``;
    2. ``tok not in CANONICAL_CATEGORIES``;
    3. ``tok not in CATEGORY_SYNONYM_ALLOWLIST``.
    """

    canonical = _load_canonical_categories()
    findings: list[Finding] = []
    for p in _d1_scan_set(repo_root):
        rel = str(p.relative_to(repo_root))
        # For .py files we scan BOTH backtick-quoted docstring tokens
        # AND single-/double-quoted Python string literals. The
        # docstring shape catches RST ``index_snapshot`` references;
        # the string-literal shape catches the actual runtime values
        # the CLI emits (``"batch_ok"`` etc.). For .md files only the
        # backtick form is meaningful.
        regexes = (
            (CATEGORY_TOKEN_RE, PY_STRING_TOKEN_RE)
            if p.suffix == ".py"
            else (CATEGORY_TOKEN_RE,)
        )
        for i, raw_line in enumerate(p.read_text().splitlines(), start=1):
            matched_on_line: set[str] = set()
            for regex in regexes:
                for m in regex.finditer(raw_line):
                    tok = m.group("tok")
                    if tok in matched_on_line:
                        continue
                    matched_on_line.add(tok)
                    if "_" not in tok:
                        continue
                    prefix = tok.split("_", 1)[0]
                    if prefix not in CATEGORY_PREFIXES:
                        continue
                    if tok in canonical:
                        continue
                    if tok in CATEGORY_SYNONYM_ALLOWLIST:
                        continue
                    findings.append(
                        Finding(
                            severity="error",
                            code="VOCAB_DRIFT_OP_CATEGORY",
                            file=rel,
                            line=i,
                            message=(
                                f"`{tok}` is not a canonical OperationalCategory "
                                "member nor a documented synonym"
                            ),
                        )
                    )
    return findings


# ---------------------------------------------------------------------------
# D2 — boundary exposure-class drift
# ---------------------------------------------------------------------------


def _load_exposure_classes() -> frozenset[str]:
    """Read :data:`yomotsusaka.boundary.EXPOSURE_CLASSES` live."""

    import yomotsusaka.boundary as b  # local import

    classes = b.EXPOSURE_CLASSES
    if not isinstance(classes, frozenset):
        # Defensive cast — keep this script working even if the
        # boundary module switches to plain set/tuple later.
        return frozenset(classes)
    return classes


def check_exposure_class_drift(
    repo_root: Path, *, exposure_classes: frozenset[str] | None = None
) -> list[Finding]:
    """D2: enforce two-way consistency for boundary exposure classes.

    * Every backtick-quoted exposure-class token in the doc scan set
      must be a current member of :data:`EXPOSURE_CLASSES`
      (``VOCAB_DRIFT_EXPOSURE_CLASS`` error). For the current
      five-token enumeration this is tautological in the forward
      direction, but if ``EXPOSURE_CLASSES`` ever drops a member
      while the docs still reference it, the regex-level
      enumeration in :data:`EXPOSURE_TOKEN_RE` will need to be
      widened — which is precisely what the failure prompts.
    * Every member of :data:`EXPOSURE_CLASSES` must appear at least
      once in the doc scan set (``VOCAB_DRIFT_EXPOSURE_UNDOCUMENTED``
      warning). Code classes with no doc footprint are stale-proofing
      risks.

    The ``exposure_classes`` keyword is a test-only seam: production
    callers leave it ``None`` and the live :data:`EXPOSURE_CLASSES`
    is loaded; tests can pass a restricted set to exercise the
    drift-detection path (``code removed a class, docs still say it``).
    """

    if exposure_classes is None:
        exposure_classes = _load_exposure_classes()
    seen: dict[str, list[tuple[str, int]]] = {tok: [] for tok in exposure_classes}
    findings: list[Finding] = []

    for p in _d2_scan_set(repo_root):
        rel = str(p.relative_to(repo_root))
        for i, raw_line in enumerate(p.read_text().splitlines(), start=1):
            for m in EXPOSURE_TOKEN_RE.finditer(raw_line):
                tok = m.group("tok")
                if tok not in exposure_classes:
                    findings.append(
                        Finding(
                            severity="error",
                            code="VOCAB_DRIFT_EXPOSURE_CLASS",
                            file=rel,
                            line=i,
                            message=(
                                f"`{tok}` is not a current member of "
                                "boundary.EXPOSURE_CLASSES"
                            ),
                        )
                    )
                    continue
                seen.setdefault(tok, []).append((rel, i))

    for tok in sorted(exposure_classes):
        if not seen.get(tok):
            findings.append(
                Finding(
                    severity="warning",
                    code="VOCAB_DRIFT_EXPOSURE_UNDOCUMENTED",
                    file="docs/",
                    line=0,
                    message=(
                        f"exposure class `{tok}` is defined in boundary.py "
                        "but never referenced in the doc scan set"
                    ),
                )
            )

    return findings


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _find_repo_root(start: Path) -> Path:
    cur = start.resolve()
    for candidate in (cur, *cur.parents):
        if (candidate / "pyproject.toml").is_file() and (candidate / "README.md").is_file():
            return candidate
    raise SystemExit(
        f"check_vocab_drift.py: could not locate repo root from {start}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="canonical-vocabulary drift checks (operational categories + exposure classes)",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Repo root (default: walk up from this script).",
    )
    parser.add_argument(
        "--json",
        type=Path,
        default=None,
        help="Also write findings as JSON to this path.",
    )
    args = parser.parse_args(argv)

    repo_root = args.repo_root or _find_repo_root(Path(__file__).resolve().parent)

    findings: list[Finding] = []
    findings.extend(check_operational_category_drift(repo_root))
    findings.extend(check_exposure_class_drift(repo_root))

    sys.stdout.write(emit_human_readable(findings))
    if args.json:
        write_json_report(args.json, findings)

    return exit_code_from(findings)


if __name__ == "__main__":
    raise SystemExit(main())
