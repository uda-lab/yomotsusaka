# Error Taxonomy

Status: agent-facing decision table consolidating the four failure-reason
surfaces an ordinary agent may encounter. **Source of truth for privacy-boundary
decisions remains `docs/architecture.md`** (see §"Source of truth precedence"
there); this document only consolidates already-documented reasons and the
suggested-next-action text for each.

This document is consumed in two ways:

- An agent that receives a `*FailureReason` value reads the matching row to
  decide its next action *without* grepping source.
- A reviewer adding a new enum value sees `tests/test_error_taxonomy_doc.py`
  fail until they extend the table below.

The four reason surfaces are:

| Enum                          | Module                                  | Surface (who returns it)                                                          |
| ----------------------------- | --------------------------------------- | --------------------------------------------------------------------------------- |
| `ResolverFailureReason`       | `yomotsusaka.boundary`                  | `resolve()` — locator resolution against a tenant vault.                          |
| `RestorationFailureReason`    | `yomotsusaka.boundary`                  | `restoration_request()` — restoring raw private values for a committed artifact.  |
| `ExecutionFailureReason`      | `yomotsusaka.execution_gateway`         | `execute_request()` — Chikaeshi private execution gateway dispatch.               |
| `InferenceBackendReason`      | `yomotsusaka.inference_backend`         | RunPod/vLLM inference backend (stable wire literals; see §1 below).               |

## Sanitisation discipline

Every "Owner action / agent response" cell below is **sanitised**: it tells the
agent which category to report and which vault-side artifact (if any) the owner
should inspect. The agent **must not** echo the underlying exception message,
absolute filesystem paths, vault root substrings, raw private values, endpoint
URLs, pod ids, or tenant identifiers into PR/issue comments. When an example
locator or key appears in this doc, it uses the canonical-fixture placeholder
shape — for example `<PERSON_xxxxxxxx>` for a redacted person key, or the full
public locator grammar
`private://<exposure_class>/<artifact_kind>/<opaque_id>[#<fragment>]` (see
`build_locator` / `parse_locator` in `yomotsusaka.boundary`). These shapes
intentionally contain no decodable raw value. The same discipline
`docs/runpod-agent-smoke.md` §7 imposes on the smoke script applies to every
agent action this doc suggests.

---

## ResolverFailureReason

Returned by `yomotsusaka.boundary.resolve()` inside a `ResolverFailure`. Wire
identifiers are stable; do not infer behaviour from the symbolic name alone.
The MVP-2 emission map below reflects the **current** `resolve()` contract; the
two reserved values (`scope_denied`, and the future-policy expansion of
`purpose_not_permitted`) are documented here so callers that exhaustively match
the enum behave correctly when they start being emitted.

| Reason                    | Surface                          | Trigger (MVP-2 contract)                                                                                                                                                                | Owner action / agent response                                                                                                                                  |
| ------------------------- | -------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `malformed_locator`       | `resolve()`                      | Locator string does not parse against the public URI grammar `private://<exposure_class>/<artifact_kind>/<opaque_id>[#<fragment>]`.                                                     | Report `malformed_locator`. Agent re-derives the locator from the redacted manifest; never paste the raw input into a PR.                                      |
| `unknown_artifact`        | `resolve()`                      | Locator parses but addresses an artifact that is not committed under the caller's tenant — covers (a) `artifact_kind` other than `manifest`, (b) no manifest file on disk for the given `opaque_id`, and (c) cross-tenant misses (Fork 9 fail-closed). | Report `unknown_artifact`. Agent treats the locator as not-yet-committed (or belonging to another tenant) and does not retry until commit lands.               |
| `artifact_missing`        | `resolve()`                      | The manifest exists but the **private-dict** file is missing or unparseable under `scope=PRIVATE_BOUNDARY`. Ordinary-agent / audit-reviewer scopes never reach this code path.          | Report `artifact_missing`. Owner inspects the vault-side `private/<opaque_id>.json` (vault-side, not via the agent) to confirm commit state.                   |
| `scope_denied`            | `resolve()` (reserved)           | **Reserved for #27 policy gating; not emitted by MVP-2 `resolve()`.** Exhaustive-match callers must still handle it as an inert future value.                                            | Treat as an inert future value; if it ever fires, report `scope_denied` without retry — scope-elevation is owner-only.                                         |
| `purpose_not_permitted`   | `resolve()`                      | The required `purpose` argument is empty or whitespace-only after `strip()`. (Per-artifact `allowed_purposes` policy is **not** in MVP-2; that path is reserved for #27 / #44.)          | Report `purpose_not_permitted`. Agent supplies a non-empty redacted purpose label on the retry; do not paste the original raw `purpose` value into a PR.       |

## RestorationFailureReason

Returned by `yomotsusaka.boundary.restoration_request()` inside a failed
`RestorationResponse`. Wire identifiers are stable; do not rename without
coordinating with the umbrella #29 contract tests.

`unknown_artifact` is grammar-reserved for future wiring (artifact kinds beyond
`manifest`); MVP-2 never emits it. Callers that exhaustively match must handle
it as an inert future value.

| Reason                     | Surface                       | Trigger                                                                                              | Owner action / agent response                                                                                                                                                |
| -------------------------- | ----------------------------- | ---------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `request_schema_invalid`   | `restoration_request()`       | Request fails Pydantic validation (missing fields, both `target_public_handle` and `document_id`, etc.). | Report `request_schema_invalid`. Agent reconstructs the request from the redacted handle; never paste the raw `ValidationError` text.                                       |
| `scope_denied`             | `restoration_request()`       | Caller scope is not `PRIVATE_BOUNDARY`. Audit row is written; kernel is NOT called.                  | Report `scope_denied`. Agent reports the redacted scope label; do not retry with an elevated scope.                                                                          |
| `unknown_artifact`         | `restoration_request()`       | Reserved; not emitted in MVP-2.                                                                      | Treat as an inert future value; agent reports `unknown_artifact` and stops without retry.                                                                                    |
| `artifact_missing`         | `restoration_request()`       | Kernel reported "No private data found" for the addressed artifact.                                  | Report `artifact_missing`. Owner inspects vault-side listing for the opaque id; do not paste the locator into the PR.                                                        |
| `audit_write_failed`       | `restoration_request()`       | The required audit row could not be durably written (pre-write `AuditError` or filesystem `OSError`). | Report `audit_write_failed`. **Escalate to the owner.** Per Chikaeshi audit contract: agent never reports `status="accepted"` when this fires. Inspect vault-side audit log. |
| `kernel_error`             | `restoration_request()`       | Kernel raised an unclassified error during restoration. `detail` strips `vault_root` substrings.     | Report `kernel_error`. Owner inspects vault-side kernel log; agent does not echo `detail` if it contains anything beyond the generic class label.                            |
| `policy_denied`            | `restoration_request()`       | Policy table (#44) denied the request before the kernel was called. Audit row written first.        | Report `policy_denied`. Agent reports the redacted policy name (already stripped of `vault_root`); owner updates the policy table profile if appropriate.                    |

## ExecutionFailureReason

Returned by `yomotsusaka.boundary.execute_request()` inside a structured
`ExecutionFailure` (Fork 5). Modelled on `ResolverFailureReason` but evolves
independently — the resolver enum is owned by the `resolve()` contract.

When the execution dispatcher internally calls `boundary.resolve()`, the
mapping is:

- `malformed_locator` / `unknown_artifact` / `artifact_missing` → `artifact_missing`
- `scope_denied` → `scope_denied`
- `purpose_not_permitted` → `purpose_not_permitted`

The original `ResolverFailureReason` is preserved in the audit record's
`resolver_reason` field for forensic correlation; the agent does not see it.

| Reason                  | Surface                       | Trigger                                                                                                | Owner action / agent response                                                                                                                                  |
| ----------------------- | ----------------------------- | ------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `scope_denied`          | `execute_request()`           | Caller's `ExecutionScope` is below the template's `min_scope` (MVP: any non-`PRIVATE_BOUNDARY` caller of a `min_scope=PRIVATE_BOUNDARY` template).      | Report `scope_denied`. Agent does not retry with elevated scope; escalation is owner-only.                                                                     |
| `purpose_not_permitted` | `execute_request()`           | Mapped from `ResolverFailureReason.PurposeNotPermitted` when the internal `resolve()` call rejects an empty/whitespace `purpose`. **Per-template `allowed_purposes` is not enforced in MVP-2** — the dispatcher's Step 4 is a no-op reserved for #44. | Report `purpose_not_permitted`. Agent supplies a non-empty redacted purpose label on the retry; never paste the original raw value into a PR.                  |
| `template_not_found`    | `execute_request()`           | `job_name` does not match any registered Chikaeshi template.                                           | Report `template_not_found`. Agent re-checks the redacted job name against the template registry; never paste the raw `inputs` payload into a PR.              |
| `schema_invalid`        | `execute_request()`           | `inputs` does not satisfy the template's declared input schema.                                        | Report `schema_invalid`. Agent reconstructs the input shape from the template registry; never echo `inputs` values into a PR.                                  |
| `scrub_failed`          | `execute_request()`           | The pre-dispatch scrubber refused the payload (private-value leak risk).                               | Report `scrub_failed`. Agent reports the redacted category and stops; do not retry with a modified payload before owner review.                                |
| `template_raised`       | `execute_request()`           | The template's job body raised an unhandled exception.                                                 | Report `template_raised`. Owner inspects vault-side template logs; agent does not echo the exception text.                                                     |
| `artifact_missing`      | `execute_request()`           | Internal `resolve()` returned `malformed_locator` / `unknown_artifact` / `artifact_missing`.           | Report `artifact_missing`. Owner inspects vault-side listing; do not paste the locator into the PR.                                                            |
| `audit_write_failed`    | `execute_request()`           | Audit row could not be durably written (pre-write `AuditError` or filesystem `OSError`).               | Report `audit_write_failed`. **Escalate to the owner.** Per Chikaeshi audit contract: agent never reports `status="accepted"` when this fires.                |

## InferenceBackendReason

Stable wire identifiers (a `typing.Literal`, not an `Enum`) for inference-backend
failures. The boundary facade maps these to `agent_redacted` failure envelopes
without echoing `InferenceBackendError.args[0]` (which may contain raw endpoint
URLs, model identifiers, or remote stack traces).

| Reason               | Surface                   | Trigger                                                                                                              | Owner action / agent response                                                                                                                              |
| -------------------- | ------------------------- | -------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `pod_unavailable`    | `PodUnavailableError`     | Connect-refused, DNS failure, non-200 `/health`, socket timeout on health probe, or RunPod "Pod-unavailable" reply.  | Report `pod_unavailable`. **Escalate to the owner after one or two retries** — Pod lifecycle (start/stop, replace) is owner-only per `docs/runpod.md`.    |
| `vllm_timeout`       | `VLLMGenerationError`     | `/v1/chat/completions` exceeded the configured request timeout.                                                      | Report `vllm_timeout`. Agent retries once with a shorter prompt; if it recurs, owner inspects Pod-side vLLM logs.                                          |
| `vllm_oom`           | `VLLMGenerationError`     | Response body or HTTP status indicated an out-of-memory marker from vLLM.                                            | Report `vllm_oom`. Agent reduces `max_tokens` or batch size for the next attempt; owner inspects Pod GPU memory if it persists.                            |
| `vllm_http_error`    | `VLLMGenerationError`     | Non-200 HTTP response or malformed JSON body that does not match the auth, OOM, or rate-limit shapes above.          | Report `vllm_http_error`. Agent does NOT echo the response body or `httpx` exception text; owner inspects Pod-side logs.                                   |
| `vllm_rate_limited`  | `VLLMGenerationError`     | HTTP 429 from vLLM.                                                                                                  | Report `vllm_rate_limited`. Agent back-off-retries with exponential wait; if it persists, owner inspects the Pod's concurrency configuration.              |

## OperationalCategory

Closed `str`-valued `Enum` defined in
`yomotsusaka.operational_taxonomy.OperationalCategory`. Categories cover the
**operational surfaces** of the MVP-5 agent-runnable flow (batch runner,
search-gateway snapshot/load, search smoke, restoration/audit, RunPod
lifecycle, RunPod smoke, inference-backed span proposer) — they do NOT
redefine the four MVP-4 `*FailureReason` enums above; instead they wrap or
sit alongside them at the operational layer.

Each value maps to a typed `RecoveryInstruction` returned by
`recovery_for()` and surfaced by child 02's operational-scenario CLI (#91)
and child 03's report renderer (#92). The same module exposes
`render_recovery_table_markdown()` for callers that need to embed the table
inline; the row set below is the **wire** vocabulary (asserted by
`tests/test_error_taxonomy_doc.py`).

`audit_inspect_failed` is the only category in MVP-5 that carries
`hard_stop=True`: per the Chikaeshi audit contract, when the required audit
row cannot be inspected the agent never reports `status="accepted"` and the
owner inspects the vault-side audit log.

| Category                                | Surface                          | Trigger                                                                                                                                                                          | Owner action / agent response                                                                                                                                |
| --------------------------------------- | -------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `batch_ok`                              | `batch_runner`                   | All inbox files committed.                                                                                                                                                       | Report counts only; no per-file paths or raw text on the public surface.                                                                                     |
| `batch_partial`                         | `batch_runner`                   | Per-file `ValueError` / decode failures.                                                                                                                                         | Report processed/failed counts; do not retry the batch. Owner inspects vault-side batch log for per-file detail.                                             |
| `batch_failed`                          | `batch_runner`                   | Inbox missing / not-a-directory / IO fault (`FileNotFoundError`, `NotADirectoryError`, `OSError`).                                                                               | Report `batch_failed`; do not retry. Owner verifies the inbox directory exists and is readable, then re-runs the batch.                                      |
| `batch_no_documents`                    | `operational_smoke` batch phase  | Inbox exists but contained no admissible files.                                                                                                                                  | Report `batch_no_documents`. Owner verifies the inbox holds the expected corpus before re-running.                                                            |
| `batch_all_failed`                      | `operational_smoke` batch phase  | Every submitted document failed to commit (zero `committed_count`).                                                                                                              | Report `batch_all_failed`. Owner inspects vault-side batch log for the per-file cause before retrying.                                                       |
| `batch_partial_commit`                  | `operational_smoke` batch phase  | Some documents committed and some failed (warn status).                                                                                                                          | Report `batch_partial_commit`. Do not retry — re-running would re-process already-committed files. Owner inspects vault-side batch log for the failed subset.|
| `batch_infrastructure_error`            | `operational_smoke` batch phase  | Runner could not enter the inbox at all (missing directory, IO fault, or constructor error).                                                                                     | Report `batch_infrastructure_error`. Owner verifies the inbox path and filesystem before re-running.                                                          |
| `index_snapshot_ok`                     | `search_gateway.snapshot`        | JSONL snapshot written.                                                                                                                                                          | Report success; emit only the manifest count.                                                                                                                |
| `index_snapshot_failed`                 | `search_gateway.snapshot`        | `OSError` writing snapshot; helper attempts one bounded retry.                                                                                                                   | Report `index_snapshot_failed`; do not retry further. Owner inspects vault-side index directory.                                                             |
| `snapshot_write_failed`                 | `operational_smoke` index_snapshot phase | `OSError` raised while writing the index snapshot; helper performed a bounded retry.                                                                                     | Report `snapshot_write_failed`. Owner inspects vault-side index directory.                                                                                    |
| `snapshot_not_persisted`                | `operational_smoke` index_snapshot phase | Snapshot helper returned without raising but the snapshot file is absent on disk (defensive branch).                                                                       | Report `snapshot_not_persisted`. Owner inspects vault-side index directory for write-through faults.                                                          |
| `index_reload_ok`                       | child-process JSONL load         | Subprocess loaded snapshot without state carry-over.                                                                                                                             | Report reload success; emit the loaded count only.                                                                                                           |
| `index_reload_failed`                   | child-process JSONL load         | Subprocess could not parse on-disk snapshot — corruption or schema drift.                                                                                                        | Report `index_reload_failed`; do not retry. Owner inspects vault-side snapshot.                                                                              |
| `search_smoke_ok`                       | `search_gateway.search`          | Fixed probe returned ≥1 expected handle.                                                                                                                                         | Report smoke success; emit the match count only.                                                                                                             |
| `search_smoke_failed`                   | `search_gateway.search`          | Probe returned 0 matches.                                                                                                                                                        | Report `search_smoke_failed` (category only). Do not echo the probe query text or any candidate handles.                                                     |
| `search_no_hits`                        | `operational_smoke` search_smoke phase | Smoke probe returned zero matches against a corpus that should always carry redaction keys.                                                                                | Report `search_no_hits`. Owner inspects vault-side index for schema drift or empty manifests.                                                                 |
| `restoration_ok`                        | `restoration_api`                | Restoration succeeded.                                                                                                                                                           | Report restoration success; the raw value is asserted in-process only.                                                                                       |
| `restoration_failed`                    | `restoration_api`                | Wraps any `RestorationFailureReason` EXCEPT `audit_write_failed` (which surfaces as `audit_inspect_failed`).                                                                     | Report `restoration_failed` and the wrapped reason token. Do not retry without owner review.                                                                 |
| `restoration_request_unexpected_outcome`| `operational_smoke` restoration phase | Facade returned a response that did not match the expected `ScopeDenied` contract (e.g. unexpected `AuditWriteFailed`).                                                     | Report `restoration_request_unexpected_outcome`. Owner inspects vault-side audit log to recover.                                                              |
| `audit_inspect_ok`                      | `audit`                          | Required audit row(s) present with required fields.                                                                                                                              | Report `audit_inspect_ok`; emit the audit row count only.                                                                                                    |
| `audit_inspect_failed`                  | `audit`                          | Audit row missing or malformed.                                                                                                                                                  | **Hard-stop.** Report `audit_inspect_failed` and stop. Never report `status=accepted`. Owner inspects vault-side audit log.                                  |
| `audit_file_missing`                    | `operational_smoke` audit phase  | Audit JSONL absent or unreadable.                                                                                                                                                | Report `audit_file_missing`. Owner inspects vault-side audit directory. Never report `status=accepted`.                                                       |
| `audit_record_not_found`                | `operational_smoke` audit phase  | Audit JSONL present but the correlation row for this run is absent.                                                                                                              | Report `audit_record_not_found`. Owner inspects vault-side audit log to recover.                                                                              |
| `runpod_lifecycle_ok`                   | `runpod_lifecycle` (manage)      | Create → wait → smoke → delete completed; no billing tail.                                                                                                                       | Report `runpod_lifecycle_ok`.                                                                                                                                |
| `runpod_lifecycle_failed_cleaned`       | `runpod_lifecycle`               | Phase failed but bounded REST `DELETE /v1/pods/{id}` retry succeeded; no Pod left running.                                                                                       | Report `runpod_lifecycle_failed_cleaned`; owner may inspect logs but no cleanup action is required.                                                          |
| `runpod_lifecycle_failed_owner_action`  | `runpod_lifecycle`               | Cleanup retries exhausted; Pod may still exist.                                                                                                                                  | Report `runpod_lifecycle_failed_owner_action`. Owner may use `runpodctl` as a break-glass tool to inspect or force-delete; agent does not invoke `runpodctl`.|
| `runpod_lifecycle_disabled`             | `operational_smoke` runpod phase | RunPod phase skipped because `--live-runpod` was not passed (no-network default).                                                                                                | No owner action; this is the no-network default.                                                                                                              |
| `runpod_lifecycle_kept`                 | `operational_smoke` runpod phase | Agent drove create-wait-smoke but skipped delete because the caller passed `--keep-pod` (warn status).                                                                           | Report `runpod_lifecycle_kept`. Owner is responsible for deleting the Pod when finished (cost control).                                                       |
| `create_failed`                         | `operational_smoke` runpod phase / `scripts/manage_runpod.py` (mirrored) | RunPod create call returned an error before the Pod became schedulable.                                                                                                          | Report `create_failed`. Owner inspects RunPod console; agent does not retry without owner review.                                                             |
| `wait_timeout`                          | `operational_smoke` runpod phase / `scripts/manage_runpod.py` (mirrored) | Pod created but did not become healthy within the configured wait budget; bounded cleanup helper deletes the Pod.                                                               | Report `wait_timeout`. Owner may inspect RunPod console; no further agent action required.                                                                    |
| `api_key_missing`                       | `operational_smoke` runpod phase / `scripts/manage_runpod.py` (mirrored) | RunPod phase requested but no API key configured.                                                                                                                                | Report `api_key_missing`. Owner provisions the credential before re-running with `--live-runpod`.                                                             |
| `cleanup_failed`                        | `operational_smoke` runpod phase / `scripts/manage_runpod.py` (mirrored) | Pod exists but bounded REST DELETE retries were exhausted.                                                                                                                       | Report `cleanup_failed`. Owner uses `runpodctl` as a break-glass tool to force-delete; agent does not invoke `runpodctl`.                                     |
| `inference_span_degraded`               | `span_proposer` (inference path) | Inference backend raised `SpanProposerError` / `InferenceBackendError`; per-document soft-degrade falls back to deterministic proposer.                                          | Report `inference_span_degraded`; do not echo backend response bodies or exception text.                                                                     |
| `inference_span_unavailable`            | `span_proposer`                  | Inference path requested but backend not configured.                                                                                                                             | Report `inference_span_unavailable`; agent uses the deterministic proposer.                                                                                  |

---

## Cross-reference: symptom → reason

When the agent observes a symptom but does not yet have a `*FailureReason`
value (for example, a comment from the owner or a smoke output), use this
table to recover the precise reason value.

| Symptom                                                                  | Reason value                  | Surface                       |
| ------------------------------------------------------------------------ | ----------------------------- | ----------------------------- |
| Audit row missing on a failed call.                                      | `audit_write_failed`          | `restoration_request()` / `execute_request()` |
| Locator does not parse against `private://<exposure_class>/<artifact_kind>/<opaque_id>[#<fragment>]`. | `malformed_locator`           | `resolve()`                   |
| No manifest at the addressed locator (uncommitted, or cross-tenant miss). | `unknown_artifact`           | `resolve()`                   |
| Internal `resolve()` from the dispatcher returned `malformed_locator` / `unknown_artifact` / `artifact_missing`. | `artifact_missing`            | `execute_request()`           |
| Private-dict file missing or unparseable under `PRIVATE_BOUNDARY` scope. | `artifact_missing`            | `resolve()`                   |
| Kernel reported "No private data found" for the addressed artifact.      | `artifact_missing`            | `restoration_request()`       |
| `RestorationRequest` rejected with both handle and `document_id` set.    | `request_schema_invalid`      | `restoration_request()`       |
| Restoration policy explicitly denied the request.                        | `policy_denied`               | `restoration_request()`       |
| Execution scrubber refused the payload (private-value leak risk).        | `scrub_failed`                | `execute_request()`           |
| Job name not in the Chikaeshi template registry.                         | `template_not_found`          | `execute_request()`           |
| Template body raised an unhandled exception.                             | `template_raised`             | `execute_request()`           |
| Pod did not respond (connect-refused / DNS / non-200 `/health`).         | `pod_unavailable`             | RunPod/vLLM backend           |
| vLLM completion exceeded the timeout.                                    | `vllm_timeout`                | RunPod/vLLM backend           |
| vLLM OOM marker in the response.                                         | `vllm_oom`                    | RunPod/vLLM backend           |
| vLLM returned HTTP 429.                                                  | `vllm_rate_limited`           | RunPod/vLLM backend           |
| Any other non-200 vLLM HTTP response or malformed body.                  | `vllm_http_error`             | RunPod/vLLM backend           |
| Restoration caller scope is not `PRIVATE_BOUNDARY`.                      | `scope_denied`                | `restoration_request()`       |
| Execution caller scope below the template's `min_scope`.                 | `scope_denied`                | `execute_request()`           |
| Empty/whitespace `purpose` on `resolve()` (or its dispatcher-internal call). | `purpose_not_permitted`   | `resolve()` / `execute_request()` (via resolver mapping) |

## When to escalate to the owner

Most reasons are recoverable: the agent reports the category, the calling
loop adjusts inputs or reconstructs the request from the redacted manifest,
and forward progress continues. **Three categories genuinely require human
intervention** and the agent should escalate rather than retry indefinitely:

1. **`audit_write_failed`** (both `restoration_request()` and `execute_request()`)
   — the durable audit row could not be written. Per the Chikaeshi audit
   contract, the agent never reports `status="accepted"` when this fires;
   owner inspects the vault-side audit log to recover.

2. **Repeated `pod_unavailable`** — one or two retries with backoff are
   appropriate, but persistent `pod_unavailable` after that indicates a Pod
   lifecycle problem (stopped, deleted, network outage). Pod start/stop is
   owner-only per `docs/runpod.md` §3.

3. **`policy_denied` with no matching profile** — if the restoration policy
   table denies the request and the agent cannot identify a `RestorationPolicyRow`
   whose `caller_label` / `target` matchers cover the call (or the named
   `policy_profile` is absent from the table altogether), the policy table
   itself needs an owner-side update. The agent reports the redacted policy
   name and the `deny_reason` category (already stripped of `vault_root`) and
   stops.

For all three, the agent's escalation message is the category literal plus the
suggested action text from the table above — **never** the underlying exception
detail or vault-side path.
