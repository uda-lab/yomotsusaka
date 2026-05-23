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

### Install

```bash
# Clone the repo
git clone https://github.com/uda-lab/yomotsusaka.git
cd yomotsusaka

# Create a virtual environment and install dependencies
uv venv
uv pip install -e ".[dev]"
```

### Run tests

```bash
uv run pytest
```

### CLI usage

Drain an inbox directory of raw documents through the local facade pipeline.
Each document is redacted, validated, committed to the vault, and indexed in
the facade's search gateway.

```bash
python -m yomotsusaka.cli.run_batch ./inbox \
    --vault-root ./vault \
    [--tenant-id <your-tenant-id>] \
    [--fail-on-error | --no-fail-on-error]
```

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
- [Error taxonomy](docs/error-taxonomy.md) — agent-facing failure-reason decision table across the four `*FailureReason` surfaces.
- [RunPod notes](docs/runpod.md) — GPU setup, real costs, and vLLM startup args for self-hosted inference.
- [RunPod agent-managed lifecycle](docs/runpod-agent-lifecycle.md) — owner/agent split and cost-controlled create → wait → smoke → delete runbook for `manage` mode.
- [Naming](docs/naming.md) — mythological component codenames (Ifuya, Kukuri, Chikaeshi, Chibiki, Kamuzumi).
- [gate-keeper integration](docs/gate-keeper.md) — repository/process-guard policy separation; see [`policy/repo-rules.md`](policy/repo-rules.md) for the first rule catalog.

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
