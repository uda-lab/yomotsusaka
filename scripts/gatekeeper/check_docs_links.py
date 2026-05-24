#!/usr/bin/env python3
"""
gate-keeper check family C: docs-to-docs link health.

Verifies internal Markdown links (``[text](target)``) resolve across
``README.md``, ``AGENTS.md``, and ``docs/*.md``; detects stale
umbrella-issue references; and flags any inversion of the
``docs/architecture.md`` source-of-truth precedence claim in
README / AGENTS.

Invoke:

```sh
uv run python scripts/gatekeeper/check_docs_links.py
uv run python scripts/gatekeeper/check_docs_links.py --no-gh
uv run python scripts/gatekeeper/check_docs_links.py --json /tmp/links.json
```

Exit codes (per issue #115 spec):

* ``0`` — all checks pass.
* ``1`` — at least one ``error`` finding (link unresolvable,
  precedence contradiction).
* ``2`` — only ``warning`` findings (stale umbrella, ``gh`` cache miss
  / offline mode).

The stale-umbrella sub-check is **advisory** (warning, exit 2 if it is
the only finding type) so transient closure latency or offline runs
do not gate PR merges. To skip the ``gh`` lookup entirely, pass
``--no-gh`` (the script then exits 0/1 only).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# Make scripts/gatekeeper/_common.py importable when invoked directly.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402  (path manipulation above)
    Finding,
    collect_docs,
    emit_human_readable,
    exit_code_from,
    iter_headings,
    write_json_report,
)

__all__ = [
    "LINK_RE",
    "ISSUE_REF_RE",
    "extract_links",
    "resolve_link",
    "check_stale_umbrellas",
    "check_precedence_consistency",
    "main",
]


# ---------------------------------------------------------------------------
# Regexes
# ---------------------------------------------------------------------------

# Captures inline `[text](target)` and `[text](target "title")` forms.
# `target` extraction stops at whitespace, so trailing `"title"` is not
# captured. Reference-style `[text][ref]` is not used in the current
# docs (verified by grep at script ship time); a follow-up check should
# be added if that changes.
LINK_RE = re.compile(
    r"\[(?P<text>[^\]]+)\]\((?P<target>[^)\s]+?)(?:\s+\"[^\"]*\")?\)"
)

# Issue references in human prose. We match `#NNN`, optionally
# preceded by `umbrella`, `MVP-N umbrella`, etc. The script normalizes
# everything down to the numeric issue id; the prose preceding it is
# only used to choose the surrounding-context window for the
# stale-claim regex below.
ISSUE_REF_RE = re.compile(r"#(?P<num>\d{1,6})\b")

# Cross-repo PR/issue markers that should NOT be looked up against
# uda-lab/yomotsusaka. We strip these spans from the line before
# running ISSUE_REF_RE.
_CROSS_REPO_RE = re.compile(
    r"(hermes-engineering(?:\s+(?:PR|issue))?\s+#\d+|t-uda/[\w.-]+#\d+|"
    r"[\w.-]+/[\w.-]+#\d+)",
    re.IGNORECASE,
)

# Stripped from each line before issue-ref scanning so anchors like
# ``architecture.md#132-initial-...`` (a section anchor, not a real
# issue #132) do not falsely trigger the gh lookup.
_MD_LINK_TARGET_RE = re.compile(r"\]\([^)\s]+(?:\s+\"[^\"]*\")?\)")

# Phrases that suggest a still-active claim about an umbrella. When
# combined with a CLOSED upstream state, we emit a stale-umbrella
# warning so the doc author can re-word.
STALE_CLAIM_TOKENS = (
    "in progress",
    "currently",
    "operationalizes",
    "is the active",
    "tracks",
    "lives in",
)

# Sentences that would INVERT the documented precedence
# (README/AGENTS > architecture.md). The check is bounded to these
# three docs.
INVERTING_PHRASES = (
    re.compile(r"README\.md\s+(overrides|supersedes|takes\s+precedence\s+over)\s+architecture\.md", re.IGNORECASE),
    re.compile(r"AGENTS\.md\s+(overrides|supersedes|takes\s+precedence\s+over)\s+architecture\.md", re.IGNORECASE),
    re.compile(r"README\.md\s+is\s+(authoritative|the\s+source\s+of\s+truth)\s+(over|for|when\s+conflicting\s+with)\s+architecture\.md", re.IGNORECASE),
    re.compile(r"AGENTS\.md\s+is\s+(authoritative|the\s+source\s+of\s+truth)\s+(over|for|when\s+conflicting\s+with)\s+architecture\.md", re.IGNORECASE),
)


# ---------------------------------------------------------------------------
# Link extraction & resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Link:
    """One Markdown link recovered from a doc."""

    source: Path
    line: int
    col: int
    text: str
    target: str


def extract_links(doc: Path) -> list[Link]:
    """Return all inline Markdown links in *doc*.

    Skips links that appear inside fenced code blocks (``` or ~~~)
    because those are typically literal command samples or shell
    snippets where ``[text](target)`` shapes are accidental.
    """

    out: list[Link] = []
    text = doc.read_text()
    in_fence = False
    for i, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for m in LINK_RE.finditer(raw_line):
            out.append(
                Link(
                    source=doc,
                    line=i,
                    col=m.start() + 1,
                    text=m.group("text"),
                    target=m.group("target"),
                )
            )
    return out


def _is_external(target: str) -> bool:
    return target.startswith(("http://", "https://", "mailto:"))


def _split_anchor(target: str) -> tuple[str, str | None]:
    if "#" in target:
        path_part, anchor = target.split("#", 1)
        return path_part, anchor or None
    return target, None


def resolve_link(link: Link, repo_root: Path) -> list[Finding]:
    """Resolve *link* and return any error findings.

    External links and bare ``mailto:`` targets are skipped. For
    anchor-only links the anchor is resolved against the same file.
    For relative paths the resolution happens against the source
    file's directory; absolute-style paths (``/...``) and
    ``src/...`` paths resolve against the repo root.
    """

    target = link.target
    if _is_external(target):
        return []

    path_part, anchor = _split_anchor(target)

    # Same-file anchor.
    if path_part == "":
        if anchor is None:
            return []  # empty target — nothing to resolve
        return _check_anchor_in_file(link.source, anchor, link, repo_root)

    # Absolute-style paths (leading ``/``) are the only repo-rooted
    # form. Every other relative path resolves against the source
    # file's directory — matching GitHub-flavored Markdown semantics.
    #
    # An earlier draft of this script treated any ``docs/...`` /
    # ``src/...`` prefix as repo-rooted, which silently accepted
    # ``[x](docs/architecture.md)`` written from inside
    # ``docs/foo.md`` (the real GFM target is
    # ``docs/docs/architecture.md``, which is broken). Resolving
    # uniformly against the source file's directory makes that bug
    # detectable — flagged by codex on PR #118.
    if path_part.startswith("/"):
        resolved = (repo_root / path_part.lstrip("/")).resolve()
    else:
        resolved = (link.source.parent / path_part).resolve()

    rel = _rel_to_root(resolved, repo_root)

    if not resolved.exists():
        return [
            Finding(
                severity="error",
                code="LINK_BROKEN",
                file=str(link.source.relative_to(repo_root)),
                line=link.line,
                message=f"link target does not exist: {rel}",
            )
        ]

    if anchor is None or not resolved.is_file() or resolved.suffix.lower() != ".md":
        return []

    return _check_anchor_in_file(resolved, anchor, link, repo_root)


def _rel_to_root(path: Path, repo_root: Path) -> str:
    try:
        return str(path.relative_to(repo_root))
    except ValueError:
        return str(path)


def _check_anchor_in_file(
    target_file: Path, anchor: str, link: Link, repo_root: Path
) -> list[Finding]:
    text = target_file.read_text()
    slugs = {slug for _, _, slug in iter_headings(text)}
    if anchor in slugs:
        return []
    return [
        Finding(
            severity="error",
            code="LINK_ANCHOR_MISSING",
            file=str(link.source.relative_to(repo_root)),
            line=link.line,
            message=(
                f"anchor #{anchor} not found in "
                f"{_rel_to_root(target_file, repo_root)}"
            ),
        )
    ]


# ---------------------------------------------------------------------------
# Stale-umbrella check (uses `gh issue view`, cached)
# ---------------------------------------------------------------------------


DEFAULT_CACHE_PATH = Path("/tmp/gatekeeper-issue-cache.json")
DEFAULT_TTL_S = 3600

GhCallable = Callable[[int], tuple[str, str]]
"""Test-only seam: takes issue number, returns ``(state, title)``."""


def _real_gh_query(issue_num: int) -> tuple[str, str]:
    """Live ``gh issue view`` query. Returns ``("", "")`` if ``gh`` fails."""

    try:
        proc = subprocess.run(
            [
                "gh",
                "issue",
                "view",
                str(issue_num),
                "--repo",
                "uda-lab/yomotsusaka",
                "--json",
                "state,title",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ("", "")
    if proc.returncode != 0:
        return ("", "")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return ("", "")
    return (data.get("state", ""), data.get("title", ""))


def _load_cache(cache_path: Path, ttl_s: int) -> dict[str, dict[str, object]]:
    if not cache_path.is_file():
        return {}
    try:
        data = json.loads(cache_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    now = time.time()
    return {
        k: v
        for k, v in data.items()
        if isinstance(v, dict) and now - float(v.get("fetched_at", 0)) < ttl_s
    }


def _save_cache(cache_path: Path, cache: dict[str, dict[str, object]]) -> None:
    try:
        cache_path.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n")
    except OSError:
        # Cache write is best-effort; never error the run on a /tmp
        # permission issue. The next invocation will refetch.
        pass


def check_stale_umbrellas(
    repo_root: Path,
    *,
    cache_path: Path = DEFAULT_CACHE_PATH,
    ttl_s: int = DEFAULT_TTL_S,
    gh: GhCallable | None = None,
) -> list[Finding]:
    """Warn when a CLOSED umbrella is still described in present-tense prose.

    The check scans the doc set for ``#NNN`` references, queries
    ``gh issue view`` (with a /tmp file cache, 1h TTL), and emits a
    ``STALE_UMBRELLA`` warning when the upstream state is ``CLOSED``
    AND the surrounding 60-character window contains a
    :data:`STALE_CLAIM_TOKENS` phrase.

    Errors are warning-class; this never fails a CI run on its own.
    A ``gh`` lookup failure for a specific issue surfaces as a single
    ``STALE_UMBRELLA_GH_MISS`` warning, not as an error.
    """

    gh = gh or _real_gh_query
    cache = _load_cache(cache_path, ttl_s)

    # Collect issue references with their (file, line, context). We
    # strip Markdown link-target spans and cross-repo PR markers
    # before matching so that anchors like
    # ``architecture.md#132-...`` and out-of-repo refs like
    # ``hermes-engineering PR #289`` do not trigger gh lookups
    # against the wrong repo. Cross-repo markers can span a line
    # break (``hermes-engineering PR\n#289``), so we scrub on the
    # joined-text level first to bridge the wrap, then map matches
    # back to line numbers.
    refs: dict[int, list[tuple[Path, int, str]]] = {}
    for doc in collect_docs(repo_root):
        text = doc.read_text()
        # Bridge soft wraps: replace newlines with spaces inside the
        # cross-repo regex's evaluation, then re-split for line-wise
        # scrubbing of remaining markers and per-line issue refs.
        joined = text.replace("\n", " ")
        joined_scrubbed = _CROSS_REPO_RE.sub(
            lambda m: " " * len(m.group(0)), joined
        )
        # Recover per-line scrubbed text so column / line numbers
        # remain stable.
        scrubbed_lines: list[str] = []
        cursor = 0
        for raw_line in text.splitlines():
            ln = joined_scrubbed[cursor : cursor + len(raw_line)]
            cursor += len(raw_line) + 1  # +1 for the joined-replacement space
            ln = _MD_LINK_TARGET_RE.sub(
                lambda m: " " * len(m.group(0)), ln
            )
            ln = _CROSS_REPO_RE.sub(lambda m: " " * len(m.group(0)), ln)
            scrubbed_lines.append(ln)
        for i, ln in enumerate(scrubbed_lines, start=1):
            for m in ISSUE_REF_RE.finditer(ln):
                num = int(m.group("num"))
                # ±30 char window around the match for stale-claim
                # phrase detection.
                start = max(0, m.start() - 30)
                end = min(len(ln), m.end() + 30)
                refs.setdefault(num, []).append((doc, i, ln[start:end]))

    findings: list[Finding] = []
    for num in sorted(refs):
        key = str(num)
        if key in cache:
            state = str(cache[key].get("state", ""))
        else:
            state, title = gh(num)
            cache[key] = {
                "state": state,
                "title": title,
                "fetched_at": time.time(),
            }

        if not state:
            # gh call failed and nothing was cached — record an
            # advisory miss for the FIRST referencing location only,
            # to avoid spamming.
            doc, line, _ctx = refs[num][0]
            findings.append(
                Finding(
                    severity="warning",
                    code="STALE_UMBRELLA_GH_MISS",
                    file=str(doc.relative_to(repo_root)),
                    line=line,
                    message=(
                        f"could not resolve issue #{num} state via gh "
                        "(offline or rate-limited); skipping stale check"
                    ),
                )
            )
            continue

        if state.upper() != "CLOSED":
            continue

        for doc, line, ctx in refs[num]:
            lowered = ctx.lower()
            if any(tok in lowered for tok in STALE_CLAIM_TOKENS):
                findings.append(
                    Finding(
                        severity="warning",
                        code="STALE_UMBRELLA",
                        file=str(doc.relative_to(repo_root)),
                        line=line,
                        message=(
                            f"closed issue #{num} referenced with "
                            "present-tense / active-claim language"
                        ),
                    )
                )

    _save_cache(cache_path, cache)
    return findings


# ---------------------------------------------------------------------------
# Precedence-contradiction check
# ---------------------------------------------------------------------------


def check_precedence_consistency(repo_root: Path) -> list[Finding]:
    """Flag any sentence in README/AGENTS that inverts architecture.md precedence.

    Bounded to README.md and AGENTS.md — the check intentionally does
    not generalize across all docs. The detection is direct: any
    sentence matching one of :data:`INVERTING_PHRASES` is an error,
    regardless of whether it also carries a precedence-class token
    (an inversion phrasing like ``README.md overrides architecture.md``
    is itself a precedence claim, so requiring an additional
    ``precedence``/``governs``/etc. keyword would let the most
    obvious phrasings slip through).
    """

    findings: list[Finding] = []
    for name in ("README.md", "AGENTS.md"):
        p = repo_root / name
        if not p.is_file():
            continue
        text = p.read_text()
        for i, raw_line in enumerate(text.splitlines(), start=1):
            for pattern in INVERTING_PHRASES:
                if pattern.search(raw_line):
                    findings.append(
                        Finding(
                            severity="error",
                            code="PRECEDENCE_CONTRADICTION",
                            file=name,
                            line=i,
                            message=(
                                "README/AGENTS asserts precedence inversion "
                                "over docs/architecture.md"
                            ),
                        )
                    )
                    break
    return findings


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _find_repo_root(start: Path) -> Path:
    """Walk upward from *start* until we find a directory containing
    ``pyproject.toml`` AND ``README.md``. Return that directory."""

    cur = start.resolve()
    for candidate in (cur, *cur.parents):
        if (candidate / "pyproject.toml").is_file() and (candidate / "README.md").is_file():
            return candidate
    raise SystemExit(
        f"check_docs_links.py: could not locate repo root from {start}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="docs-to-docs link + precedence + stale-umbrella checks",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Repo root (default: walk up from this script).",
    )
    parser.add_argument(
        "--no-gh",
        action="store_true",
        help="Skip the stale-umbrella check (offline mode). Exits 0/1 only.",
    )
    parser.add_argument(
        "--json",
        type=Path,
        default=None,
        help="Also write findings as JSON to this path.",
    )
    parser.add_argument(
        "--cache-path",
        type=Path,
        default=DEFAULT_CACHE_PATH,
        help="Path for the gh-issue cache JSON (default /tmp/...).",
    )
    args = parser.parse_args(argv)

    repo_root = args.repo_root or _find_repo_root(Path(__file__).resolve().parent)
    docs = collect_docs(repo_root)

    findings: list[Finding] = []

    for doc in docs:
        for link in extract_links(doc):
            findings.extend(resolve_link(link, repo_root))

    findings.extend(check_precedence_consistency(repo_root))

    if not args.no_gh:
        findings.extend(
            check_stale_umbrellas(
                repo_root,
                cache_path=args.cache_path,
            )
        )

    sys.stdout.write(emit_human_readable(findings))
    if args.json:
        write_json_report(args.json, findings)

    exit_code = exit_code_from(findings)
    # In --no-gh mode, suppress exit 2 (warnings-only) per the spec:
    # offline mode is 0/1 only.
    if args.no_gh and exit_code == 2:
        exit_code = 0
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
