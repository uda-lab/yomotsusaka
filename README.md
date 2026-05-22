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
- [RunPod notes](docs/runpod-notes.md) — recommended GPU setup for
  self-hosted inference.

---

## Project structure

```
src/yomotsusaka/
  __init__.py            package entry point
  schemas.py             Pydantic data models
  redactor.py            deterministic span-based redactor
  validator.py           post-redaction PII checks (plugin boundary)
  commit.py              persist manifest + private dict; return handle
  batch_queue.py         batch lifecycle management
  inference_backend.py   LLM inference interface + DummyBackend
  restoration_api.py     controlled re-hydration of private values
  search_gateway.py      agent-facing search over redacted manifests
  execution_gateway.py   mediated agent-triggered operations
  runpod_lifecycle.py    ephemeral GPU pod management (stub)
  transfer.py            artifact transfer (stub)
tests/
  test_redactor.py
  test_schemas.py
docs/
  architecture.md
  runpod-notes.md
config/
  policy.example.yaml
  model.example.yaml
scripts/
  run_nightly_batch.sh
```
