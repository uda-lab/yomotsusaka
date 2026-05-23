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
shape — for example `<PERSON_xxxxxxxx>` for a redacted person key, or
`private://<opaque-id>` for an opaque manifest locator. These shapes intentionally
contain no decodable raw value. The same discipline `docs/runpod-agent-smoke.md`
§7 imposes on the smoke script applies to every agent action this doc suggests.

---

## ResolverFailureReason

Returned by `yomotsusaka.boundary.resolve()` inside a `ResolverFailure`. Wire
identifiers are stable; do not infer behaviour from the symbolic name alone.

| Reason                    | Surface                          | Trigger                                                                                   | Owner action / agent response                                                                                                       |
| ------------------------- | -------------------------------- | ----------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `malformed_locator`       | `resolve()`                      | Locator string does not parse as a `private://<opaque-id>[#fragment]` URI.                | Report category `malformed_locator`. Agent re-derives the locator from the redacted manifest; never paste the raw input into a PR.  |
| `unknown_artifact`        | `resolve()`                      | Locator parses but its `artifact_kind` is not committed (e.g. unknown manifest kind).     | Report `unknown_artifact`. Agent treats the locator as not-yet-committed; do not retry until commit lands.                          |
| `artifact_missing`        | `resolve()`                      | Locator and kind are known, but the manifest or private dict file is absent on disk.      | Report `artifact_missing`. Owner inspects the vault directory listing (vault-side, not via the agent) to confirm commit state.      |
| `scope_denied`            | `resolve()`                      | Caller scope is not authorised for the artifact's exposure class.                         | Report `scope_denied`. Agent does not retry with a different scope; scope-elevation is owner-only.                                  |
| `purpose_not_permitted`   | `resolve()`                      | Caller's declared purpose is not in the artifact's `allowed_purposes`.                    | Report `purpose_not_permitted`. Agent reports the redacted purpose label; owner updates the artifact's policy if appropriate.       |

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
| `scope_denied`          | `execute_request()`           | Caller's `ExecutionScope` is below the template's `min_scope`.                                         | Report `scope_denied`. Agent does not retry with elevated scope; escalation is owner-only.                                                                     |
| `purpose_not_permitted` | `execute_request()`           | Caller purpose is not in the template's `allowed_purposes`.                                            | Report `purpose_not_permitted`. Agent reports the redacted purpose label; owner updates the template registry if appropriate.                                  |
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

---

## Cross-reference: symptom → reason

When the agent observes a symptom but does not yet have a `*FailureReason`
value (for example, a comment from the owner or a smoke output), use this
table to recover the precise reason value.

| Symptom                                                                  | Reason value                  | Surface                       |
| ------------------------------------------------------------------------ | ----------------------------- | ----------------------------- |
| Audit row missing on a failed call.                                      | `audit_write_failed`          | `restoration_request()` / `execute_request()` |
| Locator does not parse as `private://<opaque-id>`.                       | `malformed_locator`           | `resolve()`                   |
| No manifest at the addressed locator.                                    | `artifact_missing`            | `resolve()` / `restoration_request()` / `execute_request()` |
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
| Caller scope below required for the artifact / template / restoration.  | `scope_denied`                | `resolve()` / `restoration_request()` / `execute_request()` |
| Purpose not in the artifact/template `allowed_purposes`.                 | `purpose_not_permitted`       | `resolve()` / `execute_request()` |

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
   table denies the request and the agent cannot identify a profile whose
   `allowed_purposes` covers the call, the policy table itself needs an
   owner-side update. The agent reports the redacted policy name and stops.

For all three, the agent's escalation message is the category literal plus the
suggested action text from the table above — **never** the underlying exception
detail or vault-side path.
