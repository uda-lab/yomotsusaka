# Scaffold status

Canonical classification of every module under `src/yomotsusaka/`. Downstream
agents should consult this table before deciding whether a module is safe to
build on, safe to extend behind its stub interface, or out of scope for the
local MVP.

## Classification vocabulary

- **functional** — the module implements its MVP responsibility end-to-end on
  observable inputs. Behavior is exercised by tests or directly usable in the
  local pipeline. Extensions should preserve current behavior.
- **functional stub** — the module exposes a real interface and produces
  usable, deterministic output, but the underlying algorithm is a placeholder
  (e.g. substring search, dummy LLM). The interface is the contract; the
  internals are expected to be swapped for a real backend later.
- **deferred** — the module is a no-op or fake-result placeholder explicitly
  out of scope for the first local-only MVP slice. Treat it as an interface
  reservation; do not rely on its return values, and do not expand it without
  a child issue that scopes the real integration.

Source-of-truth precedence is defined in
[`docs/architecture.md`](architecture.md#source-of-truth-precedence). When
this table and the code disagree, the code wins and this table is the bug.

The role and exposure classification that governs which modules ordinary
agents may invoke versus which stay private-boundary-only is defined in
[`docs/architecture.md#capability-and-exposure-model`](architecture.md#capability-and-exposure-model);
this table refines that model down to per-module classification.

## Module table

| Module | Classification | Current behavior | MVP role |
| --- | --- | --- | --- |
| `src/yomotsusaka/boundary.py` | functional | Opaque public surface. Defines the `private://<exposure_class>/<artifact_kind>/<opaque_id>[#<fragment>]` locator grammar (`build_locator`, `parse_locator`, `ParsedLocator`), `PublicHandle`, `SpanSpec`; the fail-closed `resolve(...)` contract with `ResolverScope`, `ResolverFailureReason`, `ResolverSuccess`/`ResolverFailure`, `PrivateState`, `ResolverError`; and the five MVP-2 request/response models (process/inspect/search/restoration/status_report) with matching entry points. `restoration_request(req, *, scope, vault_root)` is audit-logged and scope-gated: only `ResolverScope.PRIVATE_BOUNDARY` reaches `restoration_api.restore`; every observable path writes to `<vault_root>/audit/restoration.jsonl` before returning. The internal `ArtifactHandle.vault_path` is discarded at the boundary; only the opaque locator survives. | Public boundary (architecture §5.7.1, §5.7.2, §6.1) — the only surface ordinary agents are intended to import; #27 wires the real audit-logged restoration request flow. |
| `src/yomotsusaka/schemas.py` | functional | Pydantic v2 models (`EntityKind`, `EntityRecord`, `PrivateDictEntry`, `DocumentManifest`, `ArtifactHandle`, `BatchState`, `BatchStatus`) with frozen defaults and UUID factories. `ArtifactHandle` carries `vault_path` and is reclassified as private-side internal state (see "Kernel reclassification" below). | Shared schema layer that every other module imports; the public/private boundary is encoded here. |
| `src/yomotsusaka/redactor.py` | functional | `redact(text, spans)` replaces each non-overlapping span with `<KIND_sha256[:8]>`, returning redacted text, `EntityRecord`s, and `PrivateDictEntry`s; silently drops overlapping or out-of-range spans. | Redaction and keying boundary (architecture §5.4) — deterministic span-to-key substitution. |
| `src/yomotsusaka/commit.py` | functional | Writes the manifest JSON to `<vault_root>/manifests/<doc_id>.json` and the private dictionary to `<vault_root>/private/<doc_id>.json`; returns an `ArtifactHandle` pointing at the private file. | Commit boundary (architecture §5.6) — atomic local persistence of manifest + private dictionary. |
| `src/yomotsusaka/restoration_api.py` | functional | `restore(handle)` resolves the handle's `vault_path`, enforces that it stays inside `<vault_root>/private/`, and reads the JSON back into `PrivateDictEntry` objects; raises `RestorationError` on boundary violation or missing file. | Restoration model (architecture §9) — sole sanctioned re-hydration path for private values. |
| `src/yomotsusaka/batch_queue.py` | functional | In-process `BatchQueue` storing `BatchState` objects in a dict; `submit`/`start`/`complete`/`fail`/`get` drive PENDING → RUNNING → DONE/FAILED transitions with UTC timestamps. | Batch lifecycle (architecture §5, §10) — local-only queue; durable backends can replace it behind the same interface. |
| `src/yomotsusaka/search_gateway.py` | functional stub | `SearchGateway.index`/`search` keeps manifests in a list and does a case-insensitive substring scan over `manifest.redacted_text`, capped at `top_k`. | Redacted search gateway scaffold (architecture §12) — real backend (vector store / FTS) plugs in behind this interface. |
| `src/yomotsusaka/inference_backend.py` | functional stub | `InferenceBackend` ABC with abstract `generate`/`health_check`; `DummyBackend` returns `"[DummyBackend] Echo: <prompt[:80]>"` and `health_check() == True`; `get_default_backend()` returns `DummyBackend`. | Model inference boundary (architecture §5.3, §7.2) — vLLM / Qwen3-8B / etc. implement the ABC. |
| `src/yomotsusaka/validator.py` | functional | `Validator.validate(manifest, private_dict)` enforces MVP privacy invariants and raises `ValidationError` on any breach: (1) raw `original_value` leakage into `manifest.redacted_text`; (2a/2b) every entity placeholder and every private-dictionary key appears in `redacted_text`; (3) entity-key set equals private-dictionary-key set; (4) every key matches the canonical `^<(PERSON|ORG|LOCATION|DATE|ID_NUMBER|FINANCIAL|HEALTH|CUSTOM)_[0-9a-f]{8}>$` shape; (5) each `kind` agrees with the prefix embedded in its key (entities and private-dict entries). Failure messages never echo the raw value. | Validation boundary (architecture §5.5) — pluggable seat for Presidio / LLM Guard / custom rules; the MVP implementation guards the redaction/keying contract behind a stable interface. |
| `src/yomotsusaka/runpod_lifecycle.py` | deferred | `RunPodLifecycle.start_pod` returns a hard-coded `PodHandle(pod_id="stub-pod-id", endpoint="http://localhost:8000")`; `stop_pod` is a no-op; `is_ready` always returns `False`; every method logs a "stub" warning. | RunPod lifecycle (architecture §7.1) — local MVP runs CPU-only with `DummyBackend`; real RunPod SDK calls are out of scope until a child issue scopes them. |
| `src/yomotsusaka/execution_gateway.py` | deferred | `ExecutionGateway.execute` logs the call and returns `{"status": "stub", "handle_id": ..., "operation": ...}`; no policy enforcement, no real operation dispatch. | Private execution gateway boundary (architecture §13) — interface reservation only; per §13 it is explicitly post-MVP. |
| `src/yomotsusaka/transfer.py` | deferred | `TransferBackend.upload` logs a warning and returns `"stub://<destination>/<doc_id>"`; `download` raises `TransferError`; `TransferError` exception type is defined. | Transfer boundary (architecture §5.2) — S3 / GCS / SFTP backends slot in here; local MVP needs no remote transfer. |

`src/yomotsusaka/__init__.py` is excluded by design (package entry point, no behavior to classify).

## Kernel reclassification

MVP-2 (#26 + #28) introduces `yomotsusaka.boundary` as the opaque public
surface. The MVP-1 modules `pipeline.py`, `commit.py`, `restoration_api.py`,
and `search_gateway.py` keep their existing import paths and behavior — no
deprecation warning, no rename, no move — but are reclassified as
**private-side internal kernel**: each carries a single-line docstring
banner pointing ordinary agents to `yomotsusaka.boundary` instead. The
`ArtifactHandle.vault_path` field similarly remains in `schemas.py` as
private-side internal state; the public boundary never returns it (the
opaque locator is the only handle ordinary agents see). Test fixtures
that already depend on the kernel APIs continue to import them directly.

## Maintenance

- When a module changes classification, update this table in the same PR.
- When a deferred module gains real behavior under a child issue, promote it
  to `functional stub` or `functional` here and add the corresponding tests
  before merging. The gate criteria for promoting a `deferred` backend (the
  boundary-contract tests that must pass, the non-weakening clause, and the
  Chikaeshi-specific additions for `execution_gateway.py`) are documented in
  [`docs/backend-promotion.md`](backend-promotion.md).
- If the issue body that originally specified a classification disagrees with
  this table, the code is authoritative and this table follows the code.
