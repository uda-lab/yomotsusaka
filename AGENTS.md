# Agent Guidelines

<!-- Do not restructure or delete sections. Update individual values in-place when they change. -->

## Core Principles

- Keep this file under 20-30 lines of visible guidance.
- Keep only repo-specific, non-obvious instructions here.

## Commands

~~~sh
uv venv
uv pip install -e ".[dev]"
uv run pytest
uv run ruff check src tests
uv run python -m yomotsusaka.cli.run_batch ./inbox --vault-root ./vault  # quick end-to-end check
~~~

## Architecture

- `docs/architecture.md` governs privacy-boundary decisions when docs, tests, or module comments conflict.
- Public artifacts are redacted manifests/handles; private dictionaries stay vault-side and restore only through `restoration_api.py`.
- Keep raw private values out of agent-facing returns, logs, manifests, search results, and tests except private-dictionary assertions.
- First MVP slice is local-only; RunPod/vLLM modules remain stubs unless a child issue explicitly scopes real integration.

## Maintenance Notes

<!-- This section is permanent. Do not delete. -->
- Delete stale or inferable guidance.
- Update commands and architecture when workflows change.
- Keep durable rules here; move detail to dedicated docs.
