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
| `src/yomotsusaka/facade.py` | functional | `LocalFacade(vault_root, gateway=None)` binds construction-time defaults and dispatches to the five `boundary.*_request` functions (`process` / `inspect` / `search` / `request_restore` / `status_report`). ResolverScope is fixed at `ORDINARY_AGENT` (the boundary's default for ordinary-agent entry points); no path crosses into the private boundary, no path imports `restoration_api`, no path constructs `ResolverScope.PRIVATE_BOUNDARY`. Lazy `SearchGateway()` is materialised on first `gateway` access when none was supplied. Construction is filesystem-free. | Ordinary-agent integration sugar over `yomotsusaka.boundary`; the recommended import for in-process callers. `audit_log` is intentionally absent (owned by #27); CLI is explicitly out of scope. |
| `src/yomotsusaka/schemas.py` | functional | Pydantic v2 models (`EntityKind`, `EntityRecord`, `PrivateDictEntry`, `DocumentManifest`, `ArtifactHandle`, `BatchState`, `BatchStatus`) with frozen defaults and UUID factories. `ArtifactHandle` carries `vault_path` and is reclassified as private-side internal state (see "Kernel reclassification" below). | Shared schema layer that every other module imports; the public/private boundary is encoded here. |
| `src/yomotsusaka/redactor.py` | functional | `redact(text, spans)` replaces each non-overlapping span with `<KIND_sha256[:8]>`, returning redacted text, `EntityRecord`s, and `PrivateDictEntry`s; silently drops overlapping or out-of-range spans. | Redaction and keying boundary (architecture §5.4) — deterministic span-to-key substitution. |
| `src/yomotsusaka/commit.py` | functional | Writes the manifest JSON to `<vault_root>/manifests/<doc_id>.json` and the private dictionary to `<vault_root>/private/<doc_id>.json`; returns an `ArtifactHandle` pointing at the private file. | Commit boundary (architecture §5.6) — atomic local persistence of manifest + private dictionary. |
| `src/yomotsusaka/restoration_api.py` | functional | `restore(handle)` resolves the handle's `vault_path`, enforces that it stays inside `<vault_root>/private/`, and reads the JSON back into `PrivateDictEntry` objects; raises `RestorationError` on boundary violation or missing file. | Restoration model (architecture §9) — sole sanctioned re-hydration path for private values. |
| `src/yomotsusaka/batch_queue.py` | functional | In-process `BatchQueue` storing `BatchState` objects in a dict; `submit`/`start`/`complete`/`fail`/`get` drive PENDING → RUNNING → DONE/FAILED transitions with UTC timestamps. | Batch lifecycle (architecture §5, §10) — local-only queue; durable backends can replace it behind the same interface. |
| `src/yomotsusaka/search_gateway.py` | functional stub | `SearchGateway.index`/`search` keeps manifests in a list and does a case-insensitive substring scan over `manifest.redacted_text`, capped at `top_k`. Optional in-memory `QueryResolver` (populated by the caller from `PrivateDictEntry` objects via `SearchGateway(query_resolver=...)` + `index(manifest, private_entries=...)`) translates raw private terms in agent-submitted queries to redacted entity keys before the substring scan runs (architecture §12.3); the resolver is private-side only, has no agent-facing reverse-lookup surface, and is not re-exported through `yomotsusaka.boundary`. | Redacted search gateway scaffold (architecture §12) — real backend (vector store / FTS) plugs in behind this interface. |
| `src/yomotsusaka/inference_backend.py` | functional stub | `InferenceBackend` ABC unchanged. `DummyBackend` remains the default returned by `get_default_backend()`. `VLLMBackend` (new, in `vllm_backend.py`) is an opt-in subclass calling `POST {endpoint}/v1/chat/completions` on a RunPod-served Qwen3-8B Pod, selected explicitly by callers. The module additionally hosts the private-side `InferenceBackendError` hierarchy (`PodUnavailableError`, `VLLMGenerationError`) with stable `reason` literals (`pod_unavailable`, `vllm_timeout`, `vllm_oom`, `vllm_http_error`, `vllm_rate_limited`) for boundary-facade error mapping per metaplan Fork 7 of issue #46. | Model inference boundary (architecture §5.3, §7.2); `DummyBackend` keeps CPU-only tests deterministic, `VLLMBackend` enables real GPU inference when the owner has provisioned a Pod. |
| `src/yomotsusaka/vllm_backend.py` | functional stub | `VLLMBackend(endpoint, model_id, api_key=None, pod_id=None, ...)` implements `InferenceBackend` by calling `POST {endpoint}/v1/chat/completions` via `httpx` with OpenAI-compatible payload `{"model": model_id, "messages": [{"role":"user","content": prompt}], "temperature": 0.2, "max_tokens": ...}`. `health_check()` issues `GET {endpoint}/health` (5 s timeout). Failure modes (timeout / 429 / 5xx / malformed JSON / OOM-in-body) map to `VLLMGenerationError` with a stable `reason` literal; the exception message never echoes the endpoint, API key, or raw response body. Module is private-side only; NEVER imported by `yomotsusaka.boundary`. | Real vLLM inference backend; opt-in subclass of `InferenceBackend` per `docs/runpod.md` §4–§7 and metaplan Fork 2/Fork 5 of issue #46. |
| `src/yomotsusaka/validator.py` | functional | `Validator.validate(manifest, private_dict)` enforces MVP privacy invariants and raises `ValidationError` on any breach: (1) raw `original_value` leakage into `manifest.redacted_text`; (2a/2b) every entity placeholder and every private-dictionary key appears in `redacted_text`; (3) entity-key set equals private-dictionary-key set; (4) every key matches the canonical `^<(PERSON|ORG|LOCATION|DATE|ID_NUMBER|FINANCIAL|HEALTH|CUSTOM)_[0-9a-f]{8}>$` shape; (5) each `kind` agrees with the prefix embedded in its key (entities and private-dict entries). Failure messages never echo the raw value. | Validation boundary (architecture §5.5) — pluggable seat for Presidio / LLM Guard / custom rules; the MVP implementation guards the redaction/keying contract behind a stable interface. |
| `src/yomotsusaka/runpod_lifecycle.py` | functional stub | `RunPodLifecycle` is an abstract base; `MockRunPodLifecycle` (default, no network) returns a synthetic `PodHandle(pod_id="mock-pod-<uuid4>", endpoint="http://127.0.0.1:0")`; `AttachRunPodLifecycle` reads `RUNPOD_POD_ID` / `RUNPOD_POD_ENDPOINT` and exposes `is_ready` via `GET {endpoint}/health` (5 s timeout); full `manage` mode raises `NotImplementedError` pending a follow-up issue. Mode is selected by `YOMOTSUSAKA_RUNPOD_MODE` via `lifecycle_from_env()`. The module is private-side only; NEVER imported by `yomotsusaka.boundary`. `PodHandle.pod_id` / `PodHandle.endpoint` are classified `never_expose` and MUST NOT be copied into any agent-facing surface. | RunPod lifecycle (architecture §7.1); enables owner-provisioned Pod attach without committing the MVP to Pod-creation cost control. |
| `src/yomotsusaka/execution_gateway.py` | deferred | `ExecutionGateway.execute` logs the call and returns `{"status": "stub", "handle_id": ..., "operation": ...}`; no policy enforcement, no real operation dispatch. The module additionally declares the Chikaeshi spec surface — `ExecutionScope` (new enum: `PRIVATE_BOUNDARY`, `ORDINARY_AGENT`; distinct from `ResolverScope`), `ExecutionRequest`, `ExecutionResponse`, `ExecutionFailure` — all exported via `__all__`. These Pydantic v2 models (`extra="forbid"`, `frozen=True`) and the exception type are declared and tested via direct construction, but are NOT plumbed into `execute()`; the dispatcher that consumes them ships in #43. See `docs/chikaeshi.md` for the §13 / `docs/backend-promotion.md` §4 specification. | Private execution gateway boundary (architecture §13) — interface reservation only; per §13 it is explicitly post-MVP. `docs/chikaeshi.md` pins the dispatcher contract for #43. |
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
