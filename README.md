# yomotsusaka

Best-effort private data firewall for agent workflows.

Yomotsusaka preprocesses private documents into redacted, agent-readable
manifests, keys, labels, and summaries — keeping raw data and private
dictionaries behind a controlled boundary while allowing agents to work
safely with de-identified content.

- Raw private data stays in the vault.
- Agent-facing outputs contain only redacted documents and opaque handles.
- Private values are restorable, but only through project-provided APIs.
- Open-weight / self-hosted model assumptions; no hosted proprietary LLM APIs
  in the core path.

---

## Quickstart

### Prerequisites

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/) installed

`runpodctl` is **not** a happy-path prerequisite. The agent-managed
RunPod lifecycle is REST end-to-end (issue #90 / MVP-5 umbrella #89);
`runpodctl` is optional owner break-glass tooling only — see
[`docs/runpod-agent-lifecycle.md`](docs/runpod-agent-lifecycle.md) §10.5.

### Install

```bash
# Clone the repo
git clone https://github.com/uda-lab/yomotsusaka.git
cd yomotsusaka

# Create a virtual environment and install dependencies
uv venv
uv pip install -e ".[dev]"
```

### Agent-runnable operational smoke

For an agent picking the project up in a fresh Hermes-managed
devcontainer, the shortest path to operational smoke is:

```bash
# 1. Bootstrap the project environment (broker-mediated).
eval "$(/workspaces/hermes-engineering/scripts/project-env.sh yomotsusaka)"

# 2. Install dependencies (one-shot per worktree).
uv venv && uv pip install -e ".[dev]"

# 3. Run the canonical end-to-end operational scenario.
uv run python -m yomotsusaka.cli.operational_smoke ./inbox \
    --vault-root ./vault
```

The smoke CLI exercises the full batch → index → reload → search →
restoration-request → audit backbone in a single command and emits one
stable `phase=<name> status=<...> category=<...>` line per phase plus a
final `result=<...>` token. By default it makes **no** outbound network
calls; pass `--live-runpod` only when the owner has injected
`RUNPOD_API_KEY` and the agent is explicitly authorised to spend on a
GPU Pod. See
[`src/yomotsusaka/cli/operational_smoke.py`](src/yomotsusaka/cli/operational_smoke.py)
for the full phase / category vocabulary and exit-code map.

Owner bootstrap is intentionally narrow: preconfigure the RunPod account
API key (and optionally tune `PodConfig` defaults) — nothing else is
required for the agent to reach operational smoke.

For a public-safe markdown report over a recorded scenario run, pipe a
`ScenarioResult` JSON into:

```bash
uv run python -m yomotsusaka.cli.operational_report < scenario.json
```

(MVP-5 child 03 / #92; the renderer is a fail-closed redacted sweep over
the structured input — no live execution side-effects.)

### Run tests

```bash
uv run pytest
```

### CLI usage

Drain an inbox directory of raw documents through the local facade pipeline.
Each document is redacted, validated, committed to the vault, and indexed in
the facade's search gateway.

```bash
uv run python -m yomotsusaka.cli.run_batch ./inbox \
    --vault-root ./vault \
    [--tenant-id <your-tenant-id>] \
    [--fail-on-error | --no-fail-on-error]
```

`uv run` resolves the interpreter against the `.venv` created above, so the
`yomotsusaka` package is importable without a separate activation step.

On success the CLI prints exactly one redacted-only summary line of the shape
`batch <batch_id> committed=<N> failed=<M>`. Exit codes: `0` on success, `1`
on infrastructure failure (missing inbox, unwritable vault root, runner-level
exception), `2` when at least one document failed under `--fail-on-error`.

### Nightly batch script

```bash
./scripts/run_nightly_batch.sh ./inbox
```

---

## Configuration

Copy the example config files and adjust:

```bash
cp config/policy.example.yaml config/policy.yaml
cp config/model.example.yaml config/model.yaml
```

---

## Documentation

- [Architecture](docs/architecture.md) — MVP philosophy and module map.
- [Scaffold status](docs/scaffold-status.md) — per-module classification (functional / functional stub / deferred), current behavior, and MVP role.
- Source-of-truth precedence — see [`docs/architecture.md#source-of-truth-precedence`](docs/architecture.md#source-of-truth-precedence).
- [Error taxonomy](docs/error-taxonomy.md) — agent-facing failure-reason decision table; closed `OperationalCategory` enum for the agent-runnable flow lives in `yomotsusaka.operational_taxonomy` (MVP-5 child 04 / #93).
- [Boundary-field registry](docs/architecture.md#boundary-field-registry) — `yomotsusaka.boundary_registry` is the canonical field-level classification of every public-facing operational surface; drift tests in `tests/test_boundary_registry_drift.py` fail CI on unregistered exposure (MVP-5 child 06 / #95).
- [RunPod notes](docs/runpod.md) — GPU setup, real costs, and vLLM startup args for self-hosted inference.
- [RunPod agent-managed lifecycle](docs/runpod-agent-lifecycle.md) — owner/agent split and cost-controlled create → wait → smoke → delete runbook for `manage` mode (REST-first; `runpodctl` is optional break-glass per issue #90).
- [Naming](docs/naming.md) — mythological component codenames (Ifuya, Kukuri, Chikaeshi, Chibiki, Kamuzumi).
- [gate-keeper integration](docs/gate-keeper.md) — repository/process-guard policy separation; see [`policy/repo-rules.md`](policy/repo-rules.md) for the first rule catalog.
- Redaction-quality evaluation — `src/yomotsusaka/eval/redaction_quality.py` harness over `tests/fixtures/redaction_corpus/` reports false-negative / false-positive rates and per-tenant placeholder-consistency violations as hard failures (MVP-5 child 05 / #94).

### Governance cross-reference

The MVP-5 umbrella (#89) operationalizes the agent-runnable, public-safe
operational surface; the sister governance umbrella #63 (operational
governance) is **superseded in spirit** by MVP-5 (child 06 / #95 ships
the operational trust-boundary registry, child 04 / #93 closes the
operational failure taxonomy, child 05 / #94 ships the redaction-quality
harness). Closure of #63 is an owner decision and intentionally left
open.

---

## Project structure

```
src/yomotsusaka/
  __init__.py            package entry point
  schemas.py             Pydantic data models
  redactor.py            deterministic span-based redactor
  validator.py           post-redaction PII checks (plugin boundary)
  commit.py              persist manifest + private dict; return handle
  pipeline.py            local redact → validate → commit orchestrator
  span_proposer.py       deterministic + inference-backed span proposers
  batch_queue.py         batch lifecycle management
  batch_runner.py        inbox → facade.process → search index driver
  cli/
    run_batch.py         python -m yomotsusaka.cli.run_batch CLI entry
    operational_smoke.py python -m yomotsusaka.cli.operational_smoke (MVP-5 child 02)
    operational_report.py python -m yomotsusaka.cli.operational_report (MVP-5 child 03)
  operational_taxonomy.py closed OperationalCategory enum (MVP-5 child 04)
  boundary_registry.py    public-field exposure-class registry (MVP-5 child 06)
  eval/
    redaction_quality.py  false-negative / FP harness (MVP-5 child 05)
  inference_backend.py   LLM inference interface + DummyBackend
  vllm_backend.py        opt-in vLLM backend over RunPod-served Pods
  restoration_api.py     controlled re-hydration of private values
  search_gateway.py      agent-facing search over redacted manifests
  execution_gateway.py   mediated agent-triggered operations
  runpod_lifecycle.py    ephemeral GPU pod management (mock/attach/manage)
  transfer.py            artifact transfer (stub)
tests/                   # see the `tests/` directory for the authoritative tree
docs/
  architecture.md
  runpod.md
  naming.md
config/
  policy.example.yaml
  model.example.yaml
scripts/
  run_nightly_batch.sh
```
