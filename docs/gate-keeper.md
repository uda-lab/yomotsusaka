# gate-keeper integration

Yomotsusaka and [`t-uda/gate-keeper`](https://github.com/t-uda/gate-keeper)
play different roles. This note records the intended separation so that
later changes do not accidentally entangle them.

## Two layers, two responsibilities

- **Yomotsusaka runtime guards** protect the **private/public boundary
  at runtime**: redaction, validation, the public/private artifact split,
  the restoration boundary, the search boundary, and the future
  audit-ready request model. These guards run inside the application
  process and are exercised by `tests/`.
- **gate-keeper repository/process guards** protect the **repository and
  development process that maintain that boundary**: documentation /
  implementation drift, stale path references, forbidden tracked paths,
  accidental weakening of the documented private/public split, and PR
  checklist / merge-readiness hygiene. These guards run outside the
  application process — typically before a merge or as a local preflight.

A useful single-sentence summary:

> Yomotsusaka protects the private/public boundary at runtime;
> gate-keeper protects the repository and development process that
> preserve that boundary.

## No runtime dependency

`gate-keeper` is **not** a runtime dependency of Yomotsusaka. Nothing
under `src/yomotsusaka/` may `import gate_keeper`. The check below must
exit non-zero (i.e. find no matches) on `origin/main` and on every
release branch:

```sh
! grep -rE '^(import gate_keeper|from gate_keeper)' src/yomotsusaka/
```

Repository rules live under `policy/` and are evaluated by a separately
installed `gate-keeper` CLI; the Yomotsusaka package itself does not
load, configure, or call into `gate-keeper`.

## Source-of-truth precedence

If a `gate-keeper` rule in `policy/repo-rules.md` ever conflicts with
`AGENTS.md` or `docs/architecture.md`, the conflict is resolved in favour
of `AGENTS.md` and `docs/architecture.md`. The rule must be amended or
removed; the architecture document is not edited to placate a rule. This
mirrors the source-of-truth precedence in
[`docs/architecture.md`](architecture.md#source-of-truth-precedence) —
gate-keeper rules sit below module docstrings and tests in the same
ordering.

## Rule severity convention

Rules in `policy/repo-rules.md` carry an inline `[severity: error]` or
`[severity: advisory]` annotation in their bullet text. The annotation
is human-readable documentation; gate-keeper's parser classifies the
effective backend from the bullet's phrasing (deterministic predicate
vs. semantic rubric) and reports its own severity from that route.

The annotation policy is:

- `[severity: error]` is used **only** for rules whose effective backend
  is fully deterministic (`file_exists`, `file_absent`, `text_required`,
  `text_forbidden`, `changed_file_policy`, or any `github_*` predicate),
  that pass on `origin/main` today, and that would not false-positive on
  the canonical fixture from issue #4.
- `[severity: advisory]` is used for any rule that depends on semantic
  or LLM-rubric evaluation, or that intentionally references content
  currently present in the repository for forward-looking review.

The LLM-rubric backend is opt-in via host dotenv and fails closed when
unconfigured. Semantic rules therefore stay advisory in the first MVP of
the policy.

## Local-first invocation

The intended local workflow uses the filesystem backend against the
current working tree. No network access, no credentials, no hosted LLM
API:

```sh
# One-time install of the CLI (outside the Yomotsusaka virtualenv)
uv tool install gate-keeper

# Discovery
gate-keeper --help
gate-keeper validate --help

# See how each rule routes to a backend (no target needed)
gate-keeper explain policy/repo-rules.md

# Validate a single deterministic rule against the file it targets
gate-keeper validate policy/repo-rules.md --target docs/scaffold-status.md \
    --backend filesystem --format text

# Compose multiple rule files when policy/ grows past a single doc
gate-keeper validate --include 'policy/**/*.md' --target . --format json
```

Exit codes: `0` = all rules passed, `1` = one or more failures or
errors, `2` = CLI usage error.

### Per-rule target binding

The Markdown rule format records the **intent** of each rule (the file
path or text pattern named in the bullet) but does not bind that intent
to gate-keeper's validation target — the target is whatever `--target`
specifies on the command line, and the rule's bullet text is treated as
human-readable narrative by the backend. `_file_exists` and
`_file_absent`, for example, check `target.exists()` directly; they do
not parse the bullet to recover a path. Likewise, `text_required` /
`text_forbidden` / `path_matches` / `changed_file_policy` require
`params.pattern` (or `params.manifest_path`) which the Markdown
classifier does not auto-fill.

The practical consequences:

1. A single `gate-keeper validate policy/repo-rules.md --target . ...`
   invocation cannot exercise every rule meaningfully — multi-target
   expansion runs each filesystem rule against every walked path, which
   produces spurious `fail` results for the `must-not-exist` rules (the
   repository root itself "exists but must be absent" for every walked
   file).
2. Wiring each rule to its intended target requires either (a) one
   `gate-keeper validate ... --target <specific-path>` invocation per
   rule, or (b) hand-authored IR JSON that the rule consumes via
   `--rules-format ir` so per-rule `params` (paths, patterns, manifest
   references) survive into the validator.
3. The MVP-1 policy file therefore documents intent in human-readable
   form and routes rules to the correct backend; production wire-up of
   each rule to its target lives in a follow-up.

When a PR target is needed, the same rule document can be re-evaluated
against a PR with the GitHub backend (`gh` CLI must already be
authenticated):

```sh
gate-keeper validate policy/repo-rules.md \
    --target uda-lab/yomotsusaka#<PR> --backend github
```

### `--allow-command-adapter` is disabled

`gate-keeper` exposes an `external` adapter that can execute an
arbitrary `argv` provided by a rule document. The CLI disables this
adapter by default and requires `--allow-command-adapter` to opt in.
Yomotsusaka **never** passes `--allow-command-adapter`; rule documents
must not assume it is available. Treat any future rule that requires
`tool: command` execution as out of scope for this repository.

## Scope of the first policy file

`policy/repo-rules.md` is intentionally small. Its goals are:

1. Establish the rule-file location so later issues can extend it.
2. Capture a few deterministic checks that are obviously safe to enforce
   today (forbidden stale paths, scaffold-status coverage, forbidden
   private-vault paths).
3. Capture a few PR governance rules so that merge-time mechanical
   checks can be delegated to `gate-keeper` instead of re-invented in
   ad-hoc scripts.
4. Capture a small number of semantic / advisory rules that articulate
   the boundary expectation in human terms, marked `advisory` so the
   LLM-rubric route does not block merges when its backend is
   unconfigured.

Expansion into multiple `policy/**/*.md` files, CI wiring, and a
canonical "blocking vs advisory" promotion ladder are out of scope here
and tracked separately.

## Non-goals

- `gate-keeper` does not validate runtime invariants. The Yomotsusaka
  runtime validator (`src/yomotsusaka/validator.py`) and the
  boundary/exposure tests under `tests/` remain the authoritative
  runtime guards.
- `gate-keeper` is not added to the runtime private-data path. Any
  proposal to call into `gate-keeper` from the redaction, restoration,
  search, or commit pipeline is rejected by this design note.
- The first policy file does not attempt to encode every rule the
  repository might eventually want. Coverage grows under follow-up
  issues, not by amending this file ahead of need.
