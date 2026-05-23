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

- `docs/scaffold-status.md` must exist in the repository root. [severity: error]
- `docs/architecture.md` must exist in the repository root. [severity: error]
- `docs/gate-keeper.md` must exist in the repository root. [severity: error]
- `README.md` must exist in the repository root. [severity: error]

## Stale path references

```text
Catch references to deleted or renamed documentation paths. The
canonical RunPod notes file is docs/runpod.md; the older
docs/runpod-notes.md filename is the deleted predecessor.
```

- `docs/runpod-notes.md` must not exist in the repository. [severity: error]

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

## Validation modes

```text
This rule file is multi-backend by design. A single invocation of
gate-keeper validate cannot exercise every rule because gate-keeper
binds the validation target at invocation time, not from the bullet
text. See docs/gate-keeper.md (Per-rule target binding) for the full
explanation.

For local preflight, the intended pattern is one invocation per
target — for example:

    gate-keeper validate policy/repo-rules.md \
        --target docs/scaffold-status.md \
        --backend filesystem --format text
    gate-keeper validate policy/repo-rules.md \
        --target docs/runpod-notes.md \
        --backend filesystem --format text
    gate-keeper validate policy/repo-rules.md \
        --target .vault \
        --backend filesystem --format text

Each invocation evaluates every filesystem rule against the supplied
target; rules that match the target's expected state report pass, and
rules that target a different path report fail or pass incidentally.
The PR body for the issue that introduced this file records the
combined per-target run.

GitHub rules (the PR governance bullets) pass when validated against
a PR reference (owner/repo#<N>) with the GitHub backend.

Advisory rules use the llm-rubric backend with a configured provider;
when the provider is unconfigured, they fail closed as unavailable
and never silently approve.
```
