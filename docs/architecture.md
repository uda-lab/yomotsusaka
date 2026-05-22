# Architecture

## Purpose

Yomotsusaka is a **best-effort private data firewall** for agent workflows.
It preprocesses private documents into redacted, agent-readable manifests,
keys, labels, and summaries while keeping raw data and private dictionaries
behind a controlled boundary.

---

## Boundary model

```
┌───────────────────────────────────────────────────────┐
│                      PRIVATE VAULT                    │
│  • raw documents                                      │
│  • private dictionary (key → original value)          │
│  • encryption keys                                    │
└────────────────────┬──────────────────────────────────┘
                     │  project-provided APIs only
                     ▼
┌───────────────────────────────────────────────────────┐
│                  PROCESSING PIPELINE                  │
│  redactor → validator → commit                        │
│  (runs inside the boundary, using ephemeral GPU pods) │
└────────────────────┬──────────────────────────────────┘
                     │  agent-safe outputs only
                     ▼
┌───────────────────────────────────────────────────────┐
│                  AGENT-FACING LAYER                   │
│  • DocumentManifest  (redacted text, labels, summary) │
│  • ArtifactHandle    (opaque reference)               │
│  • SearchGateway     (search over manifests)          │
│  • ExecutionGateway  (mediated operations)            │
│  • RestorationAPI    (controlled re-hydration)        │
└───────────────────────────────────────────────────────┘
```

---

## Key principles

1. **Raw private data stays in the vault.**  No raw values leave the vault
   except through the restoration API under explicit authorisation.

2. **Agent outputs are redacted.**  Manifests contain only redacted text,
   entity keys, labels, and summaries.

3. **Open-weight / self-hosted models only** in the core path.  No
   Anthropic / OpenAI / Google hosted APIs are wired into the pipeline.
   Plugin boundaries exist for future integration.

4. **Ephemeral GPU compute.**  RunPod Pods (or equivalent) are started
   before a batch job and stopped immediately after.  They are not durable
   storage.

5. **Backends are replaceable.**  The inference backend, batch queue,
   search gateway, vault storage, and transfer layer are all swappable
   behind defined interfaces.

---

## Module map

| Module | Responsibility |
|--------|---------------|
| `schemas` | Pydantic data models shared across the pipeline |
| `redactor` | Deterministic span-based redaction |
| `validator` | Post-redaction residual-PII checks (plugin boundary) |
| `commit` | Persist manifest + private dict; return artifact handle |
| `batch_queue` | Batch lifecycle management |
| `inference_backend` | LLM inference interface + DummyBackend |
| `restoration_api` | Controlled re-hydration of private values |
| `search_gateway` | Agent-facing search over redacted manifests |
| `execution_gateway` | Mediated agent-triggered operations |
| `runpod_lifecycle` | Start/stop ephemeral GPU pods (stub) |
| `transfer` | Move artifacts to/from external destinations (stub) |

---

## MVP limitations

- Redaction is span-based; entity detection is not yet automated.
- Validation is a no-op stub.
- RunPod and transfer integrations are clearly marked stubs.
- The vault is a local directory; encrypted remote storage is not yet
  implemented.
- Authentication and access control are not yet enforced.
