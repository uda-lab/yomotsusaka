# Agent Guidelines

<!-- Minimal, stale-resistant guardrails. Expand docs/ rather than this file. -->

## Source of truth

- `docs/architecture.md` governs privacy-boundary decisions.
- `policy/repo-rules.md` governs repository hygiene rules.

## Privacy guardrail

- Keep raw private values out of agent-facing returns, logs, manifests, search results, and tests except private-dictionary assertions.

## Validation

~~~sh
uv run pytest
uv run ruff check src tests
~~~

## Maintenance

- Prefer adding detail under `docs/` over expanding this file.
