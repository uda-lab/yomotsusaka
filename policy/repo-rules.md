# Yomotsusaka repository policy

```text
This document is the first repository-owned rule file for gate-keeper
(https://github.com/t-uda/gate-keeper). Background and intent are
recorded in docs/gate-keeper.md.

Each bullet below is a single rule. Bullets carry an inline
[severity: error] or [severity: advisory] annotation that documents the
author's intent; gate-keeper's parser routes effective behaviour from
the bullet phrasing, not from the annotation. The annotation discipline
is recorded in docs/gate-keeper.md.

Narrative prose in this file is intentionally written without the
normative verbs that gate-keeper's parser uses to extract rules so
that only the bullets below are interpreted as rules. Background
context goes in fenced code blocks, which the parser skips.
```

## Documentation and implementation drift

```text
Catch the failure mode that issue #32 surfaced: implementation state
and documentation state drift after MVP child issues land. The first
rules are minimal file-presence checks; later rules under the same
heading can extend coverage as the policy matures.
```

- `docs/scaffold-status.md` must exist at the documented path. [severity: error]
- `docs/architecture.md` must exist at the documented path. [severity: error]
- `docs/gate-keeper.md` must exist at the documented path. [severity: error]
- `README.md` must exist in the repository root. [severity: error]

## Stale path references

```text
Catch references to deleted or renamed documentation paths. The
canonical RunPod notes file is docs/runpod.md; the older
docs/runpod-notes.md filename is the deleted predecessor.
```

- `docs/runpod-notes.md` must not exist in the repository. [severity: error]

## AGENTS.md hygiene

```text
AGENTS.md is the single short, durable control surface for coding
agents. It becomes harmful when it duplicates README/docs content,
records issue/PR/MVP provenance, or carries stale operational claims.
The deterministic rules below pin AGENTS.md to a minimal, stale-resistant
shape; the script scripts/gatekeeper/check_agents_md.py evaluates each
rule against the live AGENTS.md at the repository root and exits
non-zero on any violation.
```

- `AGENTS.md` visible-line count (non-blank, non-HTML-comment lines) must be at most 15. [severity: error]
- `AGENTS.md` must not contain issue, PR, or MVP provenance tokens (`#<digits>`, `MVP-<digits>`, `PR <digits>`, `PR #<digits>`). [severity: error]
- Every `docs/<file>` reference in `AGENTS.md` must resolve to an existing path on disk. [severity: error]

## Boundary repository hygiene

```text
Prevent repository changes that weaken the documented private/public
split. The private vault (architecture document section 6.1) lives
outside the repository; vault-shaped paths belong outside Git history.
```

- `.vault/` must not exist in the repository. [severity: error]
- `private/` must not exist in the repository root. [severity: error]

## PR governance

```text
Mechanical merge-readiness checks routed to gate-keeper's GitHub
backend. These rules are evaluated against a PR target
(--target uda-lab/yomotsusaka#<PR> --backend github); they report as
unsupported against the local filesystem target and the validation
report records that explicitly.
```

- The PR must not be in draft state before merging. [severity: error]
- All review threads must be resolved before merge. [severity: error]
- PR tasks and checkboxes must all be complete before merging. [severity: error]

## README audience and provenance hygiene

```text
README.md must serve general readers — describing project purpose,
installation, usage, configuration, and links to stable documentation.
It must not act as an agent work log, implementation diary, or
issue cross-reference index. Agent-workflow history, MVP/child issue
provenance, PR/issue-number breadcrumbs, and implementation-dispatch
notes belong in GitHub issues, GitHub PRs, AGENTS.md, or dedicated
internal docs under docs/ — not in README.md.

The deterministic patterns below are evaluated against the prose body
of README.md only. Edge-case carve-outs documented inline as part of
each rule:

  * URL anchors (link targets containing `#fragment`) are exempt;
    Markdown anchor refs of the form `[text](#section-slug)` are
    intra-document navigation, not issue references.
  * The literal token "agent" remains a legitimate product-audience
    term when it describes the actual user audience (e.g. "agent
    workflows", "agent-facing outputs"). The rules below target
    workflow-provenance phrasing, not the product-level term.
  * A "Changelog" or "Release notes" subsection, if introduced
    later, may legitimately cite issue and PR numbers; such a
    subsection is the documented carve-out and is exempt from the
    deterministic checks.

The check is implemented at scripts/gatekeeper/check_readme_provenance.py
and runs in the same family as the other gate-keeper sibling scripts.
```

- `README.md` must not contain explicit GitHub issue or PR breadcrumbs of the form `issue #N`, `PR #N`, or a bare `#N` outside URL anchors and Markdown anchor refs. [severity: error]
- `README.md` must not contain MVP child references of the form `MVP-N child` or `MVP-N umbrella`. [severity: error]
- `README.md` must not contain the workflow-provenance term `umbrella` outside a documented changelog carve-out. [severity: error]
- `README.md` must not contain the workflow-provenance phrase `owner decision`. [severity: error]
- `README.md` must not contain the workflow-provenance phrase `agent-runnable dispatch` or `agent runnable dispatch`. [severity: error]
- `README.md` must not contain the workflow-provenance phrases `introduced by`, `child issue`, or `post hermes`. [severity: error]
- `README.md` should describe behaviour as the current product contract rather than as development provenance, so that statements such as "is REST end-to-end" appear without an accompanying issue or PR citation. [severity: advisory]

## Advisory semantic checks

```text
Advisory rules route to gate-keeper's llm-rubric backend. The LLM
backend is opt-in via host dotenv and fails closed when unconfigured,
so these rules are intentionally severity-advisory and never block
merges. They state the boundary expectation in human language so a
reviewer (human or LLM) can flag drift.
```

- The README should describe the private/public boundary in plain language so a new contributor understands the firewall stance before reading `docs/architecture.md`. [severity: advisory]
- The scaffold-status table should classify every module under `src/yomotsusaka/` (excluding `__init__.py`) consistently with the module's current behavior, so a `deferred` classification never appears next to a module that already implements its MVP responsibility. [severity: advisory]
- Sources under `src/yomotsusaka/` should never contain a top-level `import gate_keeper` or `from gate_keeper` statement, so the runtime guards stay independent of the repository-process guards. [severity: advisory]

## Layer 2-5 runtime invariants (issue #128)

```text
The three rules below were validated empirically before adoption per the
validation-first discipline in issue #128.  Each rule was tested against a
deliberately-drifted fixture (true-positive), a legitimate-state fixture
(true-negative), and the tip-of-main state (which must pass as a hard gate).
Rejected candidates (G1, G5, G6) are documented in the PR #<128-pr> body.

G2 (spec_values) — catches the #124-class precondition drift: disk_gb=20 was
below the documented [30,50] range.  Fixed in the same PR by setting
disk_gb=40.

G3 (documented_env_vars) — catches the wrong-direction drift resolution that
PR #117 applied: it deleted the RUNPOD_TEMPLATE_ID doc row instead of wiring
the env-var in code.  After PR #126 restored the row and wired the variable,
this gate passes.

G4 (lifecycle_invariant) — catches the #124 root cause: ManageRunPodLifecycle.
start_pod lacked a stop_pod call in its _wait_for_healthy exception handler,
leaving orphan Pods on wait_timeout.  Fixed in PR #125; this gate verifies the
fix is preserved.
```

- `docs/*.md` files must not carry a `<!-- spec-values target=X.y -->` block whose resolved attribute value falls outside the declared `[min, max]` range. [severity: error]
- Every env-var documented in a `docs/*.md` pipe-table (as `` `VAR_NAME` ``) must have a corresponding `os.environ.get("VAR_NAME")` or `os.getenv("VAR_NAME")` call in `src/` or `scripts/`, unless the row carries an `(operator-only)` annotation. [severity: error]
- `ManageRunPodLifecycle.start_pod` must call `stop_pod` in the exception handler that wraps `_wait_for_healthy`, so that a Pod that times out is cleaned up before the exception propagates. [severity: error]

## Validation modes

```text
This rule file is multi-backend by design. A single invocation of
gate-keeper validate cannot exercise every rule because gate-keeper
binds the validation target at invocation time, not from the bullet
text. See docs/gate-keeper.md (Per-rule target binding) for the full
explanation.

For local preflight, the intended pattern is one invocation per
target. Caveat: each invocation evaluates every filesystem rule
against the supplied target, so rules whose intent matches the
target's expected state report pass, and rules that target a
different path report fail or pass incidentally. The examples below
therefore each pass a subset of rules and fail others by design.

    # exercises must-exist rules (passes 4 of them); must-not-exist
    # rules report incidental fail against this present target.
    gate-keeper validate policy/repo-rules.md \
        --target docs/scaffold-status.md \
        --backend filesystem --format text

    # exercises the docs/runpod-notes.md must-not-exist rule (passes);
    # must-exist rules report fail because the target is absent.
    gate-keeper validate policy/repo-rules.md \
        --target docs/runpod-notes.md \
        --backend filesystem --format text

    # exercises the .vault/ must-not-exist rule (passes); must-exist
    # rules report fail because the target is absent.
    gate-keeper validate policy/repo-rules.md \
        --target .vault \
        --backend filesystem --format text

The PR body for the issue that introduced this file records the
combined per-target run.

GitHub rules (the PR governance bullets) pass when validated against
a PR reference (owner/repo#<N>) with the GitHub backend.

Advisory rules use the llm-rubric backend with a configured provider;
when the provider is unconfigured, they fail closed as unavailable
and never silently approve.
```
