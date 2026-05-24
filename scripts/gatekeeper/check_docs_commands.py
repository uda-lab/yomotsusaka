"""Gate-keeper: docs-to-source + command-validity drift checks.

This script walks fenced code blocks in ``README.md``, ``AGENTS.md``, and
``docs/*.md`` and applies eight deterministic rules covering two families:

Family A — docs-to-source
    A1 ``docs_to_source.cli_module_importable`` — documented
       ``python -m yomotsusaka.<X>`` invocations resolve to importable
       modules under the live ``src/yomotsusaka`` tree (no runtime
       execution; resolution via ``importlib.util.find_spec``).
    A2 ``docs_to_source.module_path_imports`` — documented module paths
       (``src/yomotsusaka/<x>.py`` or backticked dotted names) resolve to
       importable modules.
    A3 ``docs_to_source.documented_paths_exist`` — file/dir paths
       referenced under ``docs/``, ``policy/``, ``tests/``, and
       ``scripts/`` exist on disk.
    A4 ``docs_to_source.enum_names_in_source`` — documented enum /
       category / constant names (e.g. ``OperationalCategory``,
       ``EXPOSURE_CLASSES``) exist as top-level names in the live
       package tree.
    A5 ``docs_to_source.env_var_names_grep_detectable`` — documented
       env-var names (e.g. ``RUNPOD_API_KEY``, ``VLLM_API_KEY``) appear
       somewhere in the codebase (``src/``, ``scripts/``, ``tests/``,
       ``config/``).

Family B — command validity
    B1 ``command_validity.python_invocation_has_main`` — every
       documented ``python -m yomotsusaka.<X>`` invocation maps to a
       module that exposes either a top-level ``main`` function or an
       ``if __name__ == "__main__":`` block.
    B2 ``command_validity.tee_pipefail_guard`` — any shell block that
       pipes through ``tee`` and inspects ``$?`` must either set
       ``-o pipefail`` (or ``-eo`` / ``-euo``) earlier in the block or
       inspect ``${PIPESTATUS[0]}`` instead.
    B3 ``command_validity.fixture_path_seeded`` — shell blocks that
       pass a fixture path (``./inbox`` is the canonical example) as
       *input* must include a seeding step (``mkdir`` / ``cp`` /
       ``--demo-corpus`` / similar in-block creation). This rule is
       intentionally narrow to avoid false positives on the canonical
       quickstart invocations.

Design knobs (resolved per the triage augmentation on issue #114):
    * The CLI-module resolver accepts any ``python -m yomotsusaka.*``
      target — not just ``yomotsusaka.cli.*`` — because
      ``docs/architecture.md:244`` documents
      ``python -m yomotsusaka.boundary_registry --render-markdown`` and
      that target has a real ``__main__`` block.
    * B3 fires *only* when an in-block reference to ``./inbox`` appears
      AND no seeding step is present anywhere in the same block. The
      README / AGENTS.md quickstart blocks pass because they are the
      documented contract surface; tighter scoping is documented in
      ``docs/gate-keeper.md`` follow-ups.

Exit codes:
    0 — clean
    1 — at least one violation
    2 — internal error (malformed fence, file-read failure,
        unexpected exception)

Invocation::

    uv run python scripts/gatekeeper/check_docs_commands.py [--json]
        [--json-out PATH] [--root PATH] [--paths GLOB ...]
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator, Sequence

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Block:
    """A fenced code block extracted from a Markdown source file."""

    path: Path
    start_line: int  # 1-based line number of the opening fence
    tag: str  # "sh" / "bash" / "python" / "" (untagged)
    body: str  # block body (no fence lines, no leading newline trimming)


@dataclass
class Finding:
    """One drift detection."""

    rule: str
    severity: str
    path: str
    line: int
    block_tag: str
    evidence: str
    detail: str


@dataclass
class Report:
    files_scanned: int = 0
    blocks_scanned: int = 0
    findings: list[Finding] = field(default_factory=list)

    def to_json(self) -> dict[str, object]:
        return {
            "version": 1,
            "summary": {
                "files_scanned": self.files_scanned,
                "blocks_scanned": self.blocks_scanned,
                "violations": len(self.findings),
            },
            "findings": [asdict(f) for f in self.findings],
        }


# ---------------------------------------------------------------------------
# Fence extraction
# ---------------------------------------------------------------------------

# Accepts both backtick and tilde fences. The optional info string captures
# the language tag; anything after the tag on the same line is ignored.
_FENCE_RE = re.compile(r"^(?P<fence>```|~~~)\s*(?P<tag>[A-Za-z0-9_+\-]*)\s*$")

_ACCEPTED_TAGS: frozenset[str] = frozenset({"sh", "bash", "python", ""})


class MalformedFenceError(RuntimeError):
    """Raised when a fenced block opens but never closes."""


def extract_fenced_blocks(text: str, path: Path) -> list[Block]:
    """Walk ``text`` and yield fenced code blocks whose tag we care about.

    The matcher is tolerant of mixed fence characters (``` vs ~~~), tabs vs
    spaces in indentation, and CRLF line endings. Nested fences using a
    different fence character (e.g. ``~~~`` inside a ```` ``` ```` block) are
    treated as literal content of the outer block.

    Raises :class:`MalformedFenceError` if an opening fence never closes —
    callers convert this into an exit-2 internal error.
    """

    blocks: list[Block] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = _FENCE_RE.match(lines[i].rstrip("\r"))
        if not m:
            i += 1
            continue
        fence = m.group("fence")
        tag = m.group("tag").lower()
        start = i + 1  # 1-based line of opening fence
        body_lines: list[str] = []
        j = i + 1
        closed = False
        while j < len(lines):
            stripped = lines[j].rstrip("\r")
            inner = _FENCE_RE.match(stripped)
            if inner and inner.group("fence") == fence and not inner.group("tag"):
                closed = True
                break
            body_lines.append(lines[j])
            j += 1
        if not closed:
            raise MalformedFenceError(
                f"unclosed fence at {path}:{start} (opened with {fence!r})"
            )
        if tag in _ACCEPTED_TAGS:
            blocks.append(
                Block(
                    path=path,
                    start_line=start,
                    tag=tag,
                    body="\n".join(body_lines),
                )
            )
        i = j + 1
    return blocks


# ---------------------------------------------------------------------------
# Source-resolution helpers (no execution)
# ---------------------------------------------------------------------------


def _rel(path: Path, repo_root: Path) -> str:
    """Return path relative to repo_root, falling back to absolute string.

    Tests and external callers can pass paths outside the repo (e.g.
    ``tmp_path``); we never want that to crash the report.
    """

    try:
        return str(path.relative_to(repo_root))
    except ValueError:
        return str(path)


def _ensure_src_on_path(repo_root: Path) -> None:
    """Make sure ``<repo>/src`` is the first entry on sys.path.

    The script is designed to run under ``uv run`` (editable install), but
    we add this defensively so the resolver works in tooling that hasn't
    pre-installed the package.
    """

    src = str((repo_root / "src").resolve())
    if src not in sys.path:
        sys.path.insert(0, src)


def _find_spec_safe(qualified_name: str) -> importlib.machinery.ModuleSpec | None:
    """``importlib.util.find_spec`` that swallows ``ImportError``.

    ``find_spec`` itself can raise when an intermediate package cannot be
    imported (e.g. side-effecting top-level imports). We treat any error
    as "not importable" for drift-detection purposes.
    """

    try:
        return importlib.util.find_spec(qualified_name)
    except (ImportError, ValueError, ModuleNotFoundError):
        return None


def _module_file_for(spec: importlib.machinery.ModuleSpec) -> Path | None:
    if spec.origin and spec.origin != "built-in":
        return Path(spec.origin)
    return None


def _has_main_entry(module_file: Path) -> bool:
    """Return True if the module exposes ``main()`` or ``__main__`` block.

    Parsing is done via ``ast`` so we never execute module-level code.
    """

    try:
        tree = ast.parse(module_file.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return False
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            return True
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "main":
            return True
        if isinstance(node, ast.If) and _is_dunder_main_compare(node.test):
            return True
    return False


def _is_dunder_main_compare(node: ast.expr) -> bool:
    if not isinstance(node, ast.Compare):
        return False
    if not isinstance(node.left, ast.Name) or node.left.id != "__name__":
        return False
    if len(node.ops) != 1 or not isinstance(node.ops[0], ast.Eq):
        return False
    if len(node.comparators) != 1:
        return False
    comp = node.comparators[0]
    if isinstance(comp, ast.Constant) and comp.value == "__main__":
        return True
    return False


def _module_top_level_names(module_file: Path) -> set[str]:
    """Top-level names defined in a single module file (AST-only)."""

    names: set[str] = set()
    try:
        tree = ast.parse(module_file.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return names
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    names.add(tgt.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".", 1)[0])
    return names


def _resolves_as_module_or_attribute(dotted: str) -> bool:
    """Return True if ``dotted`` is a module spec or a top-level attribute
    of a resolvable parent module (AST-only attribute check)."""

    spec = _find_spec_safe(dotted)
    if spec is not None:
        return True
    if "." not in dotted:
        return False
    parent, leaf = dotted.rsplit(".", 1)
    parent_spec = _find_spec_safe(parent)
    if parent_spec is None:
        return False
    module_file = _module_file_for(parent_spec)
    if module_file is None or not module_file.exists():
        return False
    return leaf in _module_top_level_names(module_file)


def _top_level_names_in_package(package_root: Path) -> set[str]:
    """Collect top-level ``def``/``class``/assignment names in ``package_root``.

    Used for the enum-name check. We only look at module top level (not
    nested classes / functions) to keep the rule deterministic and cheap.
    """

    names: set[str] = set()
    for py in package_root.rglob("*.py"):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        names.add(tgt.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
    return names


# ---------------------------------------------------------------------------
# Doc-content regexes (reused across multiple checks)
# ---------------------------------------------------------------------------

# Match `python -m yomotsusaka.<dotted>` invocations (with or without an
# explicit interpreter prefix like `uv run python`). The dotted module name
# is captured up to the first non-identifier character.
_PYTHON_M_RE = re.compile(
    r"python(?:3)?\s+-m\s+(?P<module>yomotsusaka(?:\.[A-Za-z_][A-Za-z0-9_]*)+)"
)

# Match `src/yomotsusaka/<x>.py` paths or dotted yomotsusaka references.
_MODULE_PATH_RE = re.compile(
    r"src/yomotsusaka/(?P<rel>[A-Za-z0-9_./]+?)\.py"
)
_DOTTED_REF_RE = re.compile(
    r"`(?P<dotted>yomotsusaka(?:\.[A-Za-z_][A-Za-z0-9_]*)+)`"
)

# Match referenced docs/, policy/, tests/, scripts/ paths. Anchored loosely
# to avoid matching mid-word; the regex captures the path up to the first
# whitespace, closing backtick, paren, comma, or end-of-line.
_DOC_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_/])(?P<path>(?:docs|policy|tests|scripts)/[A-Za-z0-9_./\-]+)"
)

# Conservatively pick env-var-looking identifiers: ALL_CAPS with at least
# one underscore, length ≥ 5. The check then filters by known prefixes
# (RUNPOD_, VLLM_, YOMO*, HERMES_, etc.) to keep noise low.
_ENV_VAR_RE = re.compile(r"\b([A-Z][A-Z0-9_]{4,})\b")
_ENV_VAR_PREFIXES = (
    "RUNPOD_",
    "VLLM_",
    "YOMO",
    "HERMES_",
)
_ENV_VAR_EXCLUDE = {
    # Common false positives (acronyms, doc anchors, headings).
    "TODO",
    "FIXME",
    "NOTE",
    "WARNING",
    "INFO",
    "MVP",
    "API",
    "CLI",
    "JSON",
    "YAML",
    "CI",
    "PR",
    "GPU",
    "LLM",
    "URL",
    "PATH",
    "REST",
    "HTTP",
    "HTTPS",
    "ID",
}

# Match enum / constant names backticked or appearing in headings. We
# restrict to capitalised CamelCase tokens or ALL_CAPS tokens to keep the
# scan deterministic, then filter by the prefixes used in this repo.
_NAME_TOKEN_RE = re.compile(r"`(?P<name>[A-Z][A-Za-z0-9_]{3,})`")
_NAME_TOKEN_PREFIXES = (
    "Operational",
    "Exposure",
    "EXPOSURE",
    "Boundary",
)

# `tee` and `$?` detection in shell blocks.
_TEE_LINE_RE = re.compile(r"\btee\b")
_DOLLAR_QUESTION_RE = re.compile(r"\$\?")
# Match all common pipefail-enabling forms:
#   set -o pipefail          (canonical)
#   set -eo pipefail         (short-flag combos + `-o pipefail`)
#   set -euo pipefail
#   set -e -o pipefail       (separate flags)
# Each must include the literal `pipefail` token.
_PIPEFAIL_RE = re.compile(r"set\s+-[A-Za-z\s\-]*\bpipefail\b")
_PIPESTATUS_RE = re.compile(r"\$\{PIPESTATUS\[")

# Fixture-path seeding detection.
_FIXTURE_PATH_RE = re.compile(r"(?<![A-Za-z0-9_./])\./inbox(?![A-Za-z0-9_/])")
_SEEDING_RE = re.compile(
    r"\b(mkdir|cp\b|rsync|--demo-corpus|seed_inbox|touch\s+\./inbox)"
)


# ---------------------------------------------------------------------------
# Family A — docs-to-source
# ---------------------------------------------------------------------------


def check_cli_module_importable(
    blocks: Sequence[Block], repo_root: Path
) -> Iterator[Finding]:
    """A1: ``python -m yomotsusaka.<X>`` resolves via ``find_spec``."""

    for block in blocks:
        if block.tag not in {"sh", "bash", ""}:
            continue
        for match in _PYTHON_M_RE.finditer(block.body):
            module = match.group("module")
            spec = _find_spec_safe(module)
            if spec is None:
                yield Finding(
                    rule="docs_to_source.cli_module_importable",
                    severity="error",
                    path=_rel(block.path, repo_root),
                    line=block.start_line,
                    block_tag=block.tag,
                    evidence=f"python -m {module}",
                    detail=f"module {module} not importable via find_spec",
                )


def check_module_path_imports(
    blocks: Sequence[Block],
    repo_root: Path,
    prose: dict[Path, str],
) -> Iterator[Finding]:
    """A2: documented module paths resolve.

    We scan both fenced-block bodies AND prose, since the path form
    (``src/yomotsusaka/<x>.py``) and dotted form
    (``\\`yomotsusaka.foo\\```) most often appear in prose.
    """

    seen: set[tuple[str, str]] = set()
    sources: list[tuple[Path, int, str, str]] = []
    for block in blocks:
        sources.append((block.path, block.start_line, block.tag, block.body))
    for path, text in prose.items():
        sources.append((path, 0, "prose", text))

    for path, line, tag, text in sources:
        # Path form: src/yomotsusaka/<x>.py
        for match in _MODULE_PATH_RE.finditer(text):
            rel = match.group("rel")
            target = repo_root / "src" / "yomotsusaka" / f"{rel}.py"
            if target.exists():
                continue
            key = (str(path), f"path:{rel}")
            if key in seen:
                continue
            seen.add(key)
            yield Finding(
                rule="docs_to_source.module_path_imports",
                severity="error",
                path=_rel(path, repo_root),
                line=line,
                block_tag=tag,
                evidence=f"src/yomotsusaka/{rel}.py",
                detail=f"file does not exist: src/yomotsusaka/{rel}.py",
            )
        # Dotted form: `yomotsusaka.foo.bar` — accept either a module
        # whose spec resolves, or a leaf attribute defined at the
        # top level of a resolvable parent module. We never execute
        # the module; attribute checks are AST-based.
        for match in _DOTTED_REF_RE.finditer(text):
            dotted = match.group("dotted")
            if _resolves_as_module_or_attribute(dotted):
                continue
            key = (str(path), f"dotted:{dotted}")
            if key in seen:
                continue
            seen.add(key)
            yield Finding(
                rule="docs_to_source.module_path_imports",
                severity="error",
                path=_rel(path, repo_root),
                line=line,
                block_tag=tag,
                evidence=dotted,
                detail=f"dotted name {dotted} not importable",
            )


def check_documented_paths_exist(
    blocks: Sequence[Block],
    repo_root: Path,
    prose: dict[Path, str],
) -> Iterator[Finding]:
    """A3: ``docs/``, ``policy/``, ``tests/``, ``scripts/`` paths exist.

    We scan both fenced-block bodies AND the prose (lines outside any
    fenced block) of each source file, since most path references live in
    prose. Anchor-only references like ``docs/architecture.md#anchor`` are
    accepted if the file part exists.
    """

    seen: set[tuple[str, str]] = set()
    sources: list[tuple[Path, int, str, str]] = []
    for block in blocks:
        sources.append((block.path, block.start_line, block.tag, block.body))
    for path, text in prose.items():
        sources.append((path, 0, "prose", text))

    for path, line, tag, text in sources:
        for match in _DOC_PATH_RE.finditer(text):
            raw = match.group("path")
            # Strip trailing punctuation that the regex may have grabbed.
            clean = raw.rstrip(".,);:\"'`")
            # Drop anchor/fragment.
            file_part = clean.split("#", 1)[0]
            # Skip glob patterns and shell expansions.
            if any(ch in file_part for ch in ("*", "?", "$")):
                continue
            target = repo_root / file_part
            if target.exists():
                continue
            # Some paths refer to subtree roots that may be packages —
            # tolerate trailing slash-less directory names.
            if (repo_root / file_part).is_dir():
                continue
            key = (str(path), file_part)
            if key in seen:
                continue
            seen.add(key)
            yield Finding(
                rule="docs_to_source.documented_paths_exist",
                severity="error",
                path=_rel(path, repo_root),
                line=line,
                block_tag=tag,
                evidence=file_part,
                detail=f"path does not exist on disk: {file_part}",
            )


def check_enum_names_in_source(
    blocks: Sequence[Block],
    repo_root: Path,
    prose: dict[Path, str],
) -> Iterator[Finding]:
    """A4: documented enum / constant names exist in the live package."""

    package_root = repo_root / "src" / "yomotsusaka"
    if not package_root.exists():
        return
    live_names = _top_level_names_in_package(package_root)

    seen: set[tuple[str, str]] = set()
    sources: list[tuple[Path, int, str, str]] = []
    for block in blocks:
        sources.append((block.path, block.start_line, block.tag, block.body))
    for path, text in prose.items():
        sources.append((path, 0, "prose", text))

    for path, line, tag, text in sources:
        for match in _NAME_TOKEN_RE.finditer(text):
            name = match.group("name")
            if not name.startswith(_NAME_TOKEN_PREFIXES):
                continue
            if name in live_names:
                continue
            key = (str(path), name)
            if key in seen:
                continue
            seen.add(key)
            yield Finding(
                rule="docs_to_source.enum_names_in_source",
                severity="error",
                path=_rel(path, repo_root),
                line=line,
                block_tag=tag,
                evidence=name,
                detail=(
                    f"name {name} not found at module top level under "
                    "src/yomotsusaka/"
                ),
            )


def check_env_var_names_grep_detectable(
    blocks: Sequence[Block],
    repo_root: Path,
    prose: dict[Path, str],
) -> Iterator[Finding]:
    """A5: documented env-var names appear somewhere in the codebase."""

    # Build the haystack once: src/ + scripts/ + tests/ (excluding the
    # fixture tree, which would self-reference) + config/ + .env.example.
    haystack_parts: list[str] = []
    fixtures_root = (repo_root / "tests" / "fixtures").resolve()
    for sub in ("src", "scripts", "tests", "config"):
        root = repo_root / sub
        if not root.exists():
            continue
        for f in root.rglob("*"):
            if not f.is_file():
                continue
            # Skip large binaries / non-text files.
            if f.suffix in {".pyc", ".so", ".bin", ".lock"}:
                continue
            # Don't read the fixture corpus — fixtures intentionally
            # contain names that should be flagged as drift, and reading
            # them here would defeat the check on its own fixtures.
            try:
                if fixtures_root in f.resolve().parents:
                    continue
            except OSError:
                pass
            try:
                haystack_parts.append(f.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                continue
    env_example = repo_root / ".env.example"
    if env_example.exists():
        try:
            haystack_parts.append(
                env_example.read_text(encoding="utf-8", errors="ignore")
            )
        except OSError:
            pass
    haystack = "\n".join(haystack_parts)

    seen: set[tuple[str, str]] = set()
    sources: list[tuple[Path, int, str, str]] = []
    for block in blocks:
        sources.append((block.path, block.start_line, block.tag, block.body))
    for path, text in prose.items():
        sources.append((path, 0, "prose", text))

    for path, line, tag, text in sources:
        for match in _ENV_VAR_RE.finditer(text):
            name = match.group(1)
            if name in _ENV_VAR_EXCLUDE:
                continue
            if not name.startswith(_ENV_VAR_PREFIXES):
                continue
            if name in haystack:
                continue
            key = (str(path), name)
            if key in seen:
                continue
            seen.add(key)
            yield Finding(
                rule="docs_to_source.env_var_names_grep_detectable",
                severity="error",
                path=_rel(path, repo_root),
                line=line,
                block_tag=tag,
                evidence=name,
                detail=f"env var {name} not referenced anywhere under src/ scripts/ tests/ config/",
            )


# ---------------------------------------------------------------------------
# Family B — command validity
# ---------------------------------------------------------------------------


def check_python_invocation_has_main(
    blocks: Sequence[Block], repo_root: Path
) -> Iterator[Finding]:
    """B1: every documented ``python -m yomotsusaka.<X>`` has main()/__main__."""

    seen: set[tuple[str, int, str]] = set()
    for block in blocks:
        if block.tag not in {"sh", "bash", ""}:
            continue
        for match in _PYTHON_M_RE.finditer(block.body):
            module = match.group("module")
            spec = _find_spec_safe(module)
            if spec is None:
                # A1 already flagged this; skip to avoid double-reporting.
                continue
            module_file = _module_file_for(spec)
            if module_file is None or not module_file.exists():
                continue
            if _has_main_entry(module_file):
                continue
            key = (str(block.path), block.start_line, module)
            if key in seen:
                continue
            seen.add(key)
            yield Finding(
                rule="command_validity.python_invocation_has_main",
                severity="error",
                path=_rel(block.path, repo_root),
                line=block.start_line,
                block_tag=block.tag,
                evidence=f"python -m {module}",
                detail=(
                    f"module {module} has neither a top-level main() nor "
                    'an if __name__ == "__main__": block'
                ),
            )


def check_tee_pipefail_guard(
    blocks: Sequence[Block], repo_root: Path
) -> Iterator[Finding]:
    """B2: tee + $? without pipefail or PIPESTATUS guard.

    Order matters. The guard (``set -o pipefail`` or any of its short-flag
    variants like ``set -euo pipefail``) must appear *before* the ``tee``
    pipeline — a pipefail set after the pipeline has already exited
    cannot influence ``$?`` for that pipeline. ``${PIPESTATUS[0]}``,
    being an explicit per-line read, is accepted on the same line as the
    ``$?`` check or earlier (it replaces ``$?``, it doesn't precede it).
    """

    for block in blocks:
        if block.tag not in {"sh", "bash", ""}:
            continue
        lines = block.body.splitlines()
        tee_seen_at: int | None = None
        pipefail_before_tee = False
        # Track whether the line that inspects $? also uses PIPESTATUS
        # (PIPESTATUS is a per-line guard, not a session-wide one).
        for idx, raw in enumerate(lines):
            if _TEE_LINE_RE.search(raw):
                if tee_seen_at is None:
                    tee_seen_at = idx
            elif tee_seen_at is None and _PIPEFAIL_RE.search(raw):
                # pipefail must precede the tee pipeline to count
                pipefail_before_tee = True
            if tee_seen_at is not None and _DOLLAR_QUESTION_RE.search(raw):
                # Accept PIPESTATUS as a per-line replacement for $?.
                pipestatus_here = bool(_PIPESTATUS_RE.search(raw))
                if not (pipefail_before_tee or pipestatus_here):
                    yield Finding(
                        rule="command_validity.tee_pipefail_guard",
                        severity="error",
                        path=_rel(block.path, repo_root),
                        line=block.start_line + idx,
                        block_tag=block.tag,
                        evidence=raw.strip(),
                        detail=(
                            "shell block pipes through tee and inspects "
                            "$? without a preceding `set -o pipefail` "
                            "(or short-flag variant) and no "
                            "${PIPESTATUS[0]} guard on the inspection line"
                        ),
                    )
                # Only fire once per block.
                break


def check_fixture_path_seeded(
    blocks: Sequence[Block], repo_root: Path
) -> Iterator[Finding]:
    """B3: ``./inbox`` referenced as input must have a seeding step.

    Narrow scoping: we fire only when ``./inbox`` appears in the block AND
    no seeding indicator (``mkdir``, ``cp``, ``--demo-corpus``, etc.)
    appears anywhere in the block. The README and AGENTS.md quickstart
    blocks intentionally treat ``./inbox`` as the user-supplied fixture
    path and document the contract; this rule will flag fresh docs that
    add an ``./inbox`` reference without the seeding contract.
    """

    for block in blocks:
        if block.tag not in {"sh", "bash", ""}:
            continue
        if not _FIXTURE_PATH_RE.search(block.body):
            continue
        if _SEEDING_RE.search(block.body):
            continue
        # Whitelist the canonical quickstart/operational-smoke contract
        # blocks — they document the fixture-path contract that downstream
        # blocks should follow, not violate. Match by repo-relative path
        # so fixture corpora named ``README.md`` are NOT whitelisted.
        rel = _rel(block.path, repo_root)
        if rel in {"AGENTS.md", "README.md"}:
            continue
        yield Finding(
            rule="command_validity.fixture_path_seeded",
            severity="error",
            path=_rel(block.path, repo_root),
            line=block.start_line,
            block_tag=block.tag,
            evidence="./inbox",
            detail=(
                "shell block references ./inbox as input without a "
                "seeding step (mkdir / cp / --demo-corpus / similar)"
            ),
        )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _default_paths(repo_root: Path) -> list[Path]:
    paths: list[Path] = []
    for candidate in (repo_root / "README.md", repo_root / "AGENTS.md"):
        if candidate.exists():
            paths.append(candidate)
    docs_dir = repo_root / "docs"
    if docs_dir.exists():
        paths.extend(sorted(docs_dir.glob("*.md")))
    return paths


def _expand_globs(repo_root: Path, globs: Sequence[str]) -> list[Path]:
    paths: list[Path] = []
    for pattern in globs:
        if any(ch in pattern for ch in ("*", "?", "[")):
            paths.extend(sorted(repo_root.glob(pattern)))
        else:
            paths.append(repo_root / pattern)
    return [p for p in paths if p.exists() and p.is_file()]


def _strip_fenced_blocks(text: str) -> str:
    """Return ``text`` with fenced-block bodies removed.

    Used to build the prose stream so A3/A4/A5 don't double-count
    references that already live inside a fenced block.
    """

    out: list[str] = []
    lines = text.splitlines()
    in_fence = False
    fence_char: str | None = None
    for raw in lines:
        m = _FENCE_RE.match(raw.rstrip("\r"))
        if m:
            if not in_fence:
                in_fence = True
                fence_char = m.group("fence")
                out.append("")  # preserve line count for offsets
                continue
            if fence_char and m.group("fence") == fence_char and not m.group("tag"):
                in_fence = False
                fence_char = None
                out.append("")
                continue
        if in_fence:
            out.append("")
        else:
            out.append(raw)
    return "\n".join(out)


def run_checks(
    paths: Sequence[Path], repo_root: Path
) -> Report:
    report = Report()
    blocks: list[Block] = []
    prose: dict[Path, str] = {}
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise MalformedFenceError(f"cannot read {path}: {exc}") from exc
        report.files_scanned += 1
        path_blocks = extract_fenced_blocks(text, path)
        blocks.extend(path_blocks)
        prose[path] = _strip_fenced_blocks(text)
    report.blocks_scanned = len(blocks)

    # Family A
    report.findings.extend(check_cli_module_importable(blocks, repo_root))
    report.findings.extend(check_module_path_imports(blocks, repo_root, prose))
    report.findings.extend(check_documented_paths_exist(blocks, repo_root, prose))
    report.findings.extend(check_enum_names_in_source(blocks, repo_root, prose))
    report.findings.extend(
        check_env_var_names_grep_detectable(blocks, repo_root, prose)
    )

    # Family B
    report.findings.extend(check_python_invocation_has_main(blocks, repo_root))
    report.findings.extend(check_tee_pipefail_guard(blocks, repo_root))
    report.findings.extend(check_fixture_path_seeded(blocks, repo_root))

    return report


def _human_report(report: Report) -> str:
    lines: list[str] = []
    lines.append(
        f"scanned {report.files_scanned} files / "
        f"{report.blocks_scanned} fenced blocks; "
        f"{len(report.findings)} violation(s)"
    )
    for f in report.findings:
        loc = f"{f.path}:{f.line}" if f.line else f.path
        lines.append(f"  [{f.severity}] {f.rule} @ {loc}")
        lines.append(f"    evidence: {f.evidence}")
        lines.append(f"    detail:   {f.detail}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Gate-keeper: docs-to-source + command-validity drift checks. "
            "Walks fenced code blocks in README.md, AGENTS.md, and "
            "docs/*.md and emits a structured drift report."
        )
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Repository root (default: parent of scripts/gatekeeper/).",
    )
    parser.add_argument(
        "--paths",
        nargs="*",
        default=None,
        help=(
            "Override the default doc set (README.md AGENTS.md docs/*.md). "
            "Globs are resolved relative to --root."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON to stdout (suppresses human report).",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="Also write JSON report to this path.",
    )
    args = parser.parse_args(argv)

    repo_root = (
        args.root.resolve()
        if args.root
        else Path(__file__).resolve().parents[2]
    )
    _ensure_src_on_path(repo_root)

    if args.paths:
        paths = _expand_globs(repo_root, args.paths)
    else:
        paths = _default_paths(repo_root)

    try:
        report = run_checks(paths, repo_root)
    except MalformedFenceError as exc:
        print(f"internal-error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover - defensive
        print(f"internal-error: {exc!r}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report.to_json(), indent=2, sort_keys=True))
    else:
        print(_human_report(report))

    if args.json_out:
        args.json_out.write_text(
            json.dumps(report.to_json(), indent=2, sort_keys=True),
            encoding="utf-8",
        )

    return 1 if report.findings else 0


if __name__ == "__main__":  # pragma: no cover - thin CLI shim
    sys.exit(main())
