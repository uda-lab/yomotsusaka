# Chikaeshi — Private Execution Gateway Specification

This document specifies the private execution gateway ("Chikaeshi";
`src/yomotsusaka/execution_gateway.py`) defined in
[`docs/architecture.md` §13](architecture.md#13-private-execution-gateway).
It satisfies the preconditions in
[`docs/backend-promotion.md` §4](backend-promotion.md#4-promotion-procedure)
("Chikaeshi-specific additions") on paper, so the sibling dispatcher PR
(issue #43) can land without renegotiating shape.

Source-of-truth precedence is defined in
[`docs/architecture.md`](architecture.md#source-of-truth-precedence).
When this document and the architecture conflict on a privacy-boundary
decision, the architecture wins and this document is the bug.

## Status

**Specification only; not yet enforced.** Every section below describes a
contract that the dispatcher in #43 will implement. The module today
declares the request/response/failure Pydantic models and the
`ExecutionScope` enum, exports them via `__all__`, and leaves
`ExecutionGateway.execute()` returning the legacy
`{"status": "stub", ...}` dict. The classification in
[`docs/scaffold-status.md`](scaffold-status.md) stays `deferred`.

The four new symbols declared by #42:

- `ExecutionScope` — new `str` enum, `PRIVATE_BOUNDARY` and
  `ORDINARY_AGENT`. Deliberately NOT
  `yomotsusaka.boundary.ResolverScope` (see §2 below).
- `ExecutionRequest` — frozen Pydantic v2 model.
- `ExecutionResponse` — frozen Pydantic v2 model.
- `ExecutionFailure` — `Exception` subclass reserved for #43's
  dispatcher.

No new agent-facing entry point lands in #42. `boundary.execute_request`
and any `LocalFacade.execute` method are owned by #43 (and may be
deferred further).

## 1. Template-job registry

> Specification only; not yet enforced.

The dispatcher in #43 carries a registry of template jobs. Each entry is
shaped:

```
job_name -> {
    purpose_tag:      str,            # human-readable label for audit
    allowed_scopes:   set[ExecutionScope],
    input_schema:     pydantic.BaseModel subclass,
    output_schema:    pydantic.BaseModel subclass,
}
```

The MVP candidates listed in
[`docs/architecture.md` §13.2](architecture.md#132-initial-implementation-principle)
are:

- `generate_letter_from_private_template`
- `summarise_private_minutes`
- `fill_private_form`
- `render_private_pdf`
- `export_private_table_view`

#43 ships at least two concrete examples; the rest stay reserved names.

**Registry invariants** (to be enforced by #43):

- `job_name` is a fixed string from the registry. Arbitrary
  agent-submitted strings outside the registry are refused.
- `input_schema` and `output_schema` are `pydantic.BaseModel`
  subclasses with `ConfigDict(extra="forbid")` and `frozen=True`.
- `allowed_scopes` is non-empty and is a subset of `{ExecutionScope.PRIVATE_BOUNDARY,
  ExecutionScope.ORDINARY_AGENT}`.

## 2. Scope and purpose gate

> Specification only; not yet enforced.

Every `ExecutionRequest` carries two gate inputs:

- `scope: ExecutionScope` — caller scope.
- `purpose: str` — free-form, required, non-empty after `.strip()`
  (validated at construction time by `ExecutionRequest`).

The dispatcher in #43 will:

1. Refuse the request when `request.scope` is not in the template's
   `allowed_scopes`. Failure mode: `ExecutionFailure` with a stable
   error code (to be enumerated by #43).
2. Forward `purpose` into the audit record (§4 below). The purpose
   value is never re-emitted into the response body other than via the
   audit-record id.

The gate deliberately uses a **new** `ExecutionScope` enum rather than
reusing `yomotsusaka.boundary.ResolverScope`:

- `ResolverScope.AUDIT_REVIEWER` has no operational meaning for
  execution (a reviewer does not dispatch template jobs); reusing the
  enum would force every dispatcher test to expand whenever resolver
  scope grows a new value, and vice versa.
- The two enums evolve independently. `ResolverScope` governs locator
  resolution (`boundary.resolve(...)`); `ExecutionScope` governs
  template-job dispatch. Coupling them would entangle locator-resolution
  test coverage with execution policy.

This split was settled by the #42 reconciliation. Any future PR that
wants to merge the two enums must replace this section in the same PR.

## 3. Scrubbed I/O contract

> Specification only; not yet enforced.

Per [`docs/architecture.md` §13.4](architecture.md#134-returned-execution-information),
the gateway returns only opaque handles and scrubbed text fragments. The
declared `ExecutionResponse` shape encodes this:

- `artifacts: list[PublicHandle]` — every produced artifact is wrapped
  in a `PublicHandle` whose `locator` parses via
  `yomotsusaka.boundary.parse_locator`. The internal
  `ArtifactHandle.vault_path` is discarded at the boundary; the
  dispatcher MUST NOT include it.
- `scrubbed_stdout: str`, `scrubbed_stderr: str` — text fragments
  after the scrubber has redacted raw private values, vault paths, and
  any string matching the patterns enumerated in the exposure-contract
  scan (`tests/test_exposure_contract.py`). Empty string is the safe
  default.

**Scrubber failure policy** (to be enforced by #43):

- If the scrubber detects a raw private value in stdout/stderr that it
  cannot redact (e.g. an entity the manifest does not know about), the
  dispatcher fails the entire job: response carries `status="failed"`
  and `scrubbed_stdout`/`scrubbed_stderr` are emitted only after a
  second pass that drops the unredactable line entirely.
- The scrubber is a separate module owned by #43; its interface is not
  pinned by #42.

**What never appears in `ExecutionResponse`** (privacy invariants):

- Raw private values from any source (template inputs, private
  dictionary entries, vault file contents).
- Absolute filesystem paths (vault root, staging path, private
  dictionary path).
- Non-opaque job/output identifiers (every artifact reference is a
  `PublicHandle` whose locator parses via `parse_locator`).
- Generated private document contents (the agent gets only the handle;
  the operator retrieves the artifact out-of-band per §13.4).

## 4. Audit-record contract

> Specification only; not yet enforced.

Per [`docs/backend-promotion.md` §4](backend-promotion.md#4-promotion-procedure)
Chikaeshi-specific additions, every gateway-mediated restoration of
private values produces an audit record at
`<vault_root>/audit/restoration.jsonl`. This is the same file the
restoration-request boundary already writes (see
[`docs/architecture.md` §6.1](architecture.md#61-private-vault)).

The dispatcher in #43 appends one or more JSONL records per call. The
exact field set is to be reconciled with the existing
restoration-request audit shape (issue #27); the binding invariants are:

- Schema-invalid, scope-denied, and accept paths each write at least
  one record before returning.
- The accept path writes an *intent* record before invoking the
  template body, and a *result* record after. Consumers reconstruct
  the final outcome by taking the last record per `audit_record_id`.
- Records never carry `PrivateDictEntry.original_value`, absolute
  filesystem paths, or the vault root.
- The `audit_record_id` echoed on `ExecutionResponse.audit_record_id`
  is the same id used in the JSONL line.

The exact JSONL field list is owned by #43 and will be added to this
section in the same PR. #42 only pins that the file path is
`<vault_root>/audit/restoration.jsonl` and that the audit-record id
threads through `ExecutionResponse`.

### 4.1 Audit-write failure is explicit (#59)

The dispatcher promises one durable audit row per call. When the
underlying `audit.write_record` raises (either `AuditError` from the
pre-write scrubber re-check, or `OSError` from the filesystem),
`boundary.execute_request` MUST NOT claim the call succeeded or echo
the original failure classification as if the audit row had landed.

The dedicated failure reason
`ExecutionFailureReason.AuditWriteFailed` (`"audit_write_failed"`)
exists for exactly this case. The behaviour is:

- Every path that reaches a required audit write — including the
  success path after template output is available, and every
  `_emit_failure` denial path (schema-invalid, scope-denied,
  template-not-found, template-raised, scrub-failed,
  artifact-missing, purpose-not-permitted) — returns
  `ExecutionResponse(status="failed", reason=AuditWriteFailed, ...)`
  if its required audit row cannot be persisted. No path returns
  `status="accepted"` after the success audit write fails.
- The response carries `artifacts=[]`, empty scrubbed stdout/stderr,
  and a public-safe `detail` string that names only the original
  outcome category (an enum value, never raw caller input). The
  detail field never echoes a filesystem path, vault root, raw
  private value, endpoint URL, pod id, or tenant identifier.
- A failure-log line at `logger.error` records the request id,
  original outcome category, and reason for forensic correlation;
  the log line is similarly stripped of raw values and paths.
- No audit row lands for the request when the write itself fails —
  the append-only file may not even exist yet for first-call
  failures. Tests that assert "one row per call" therefore exclude
  `AuditWriteFailed` responses (the contract for that response is
  zero durable rows, not one).

## 5. Container profile (§13.3 invariants)

> Specification only; not yet enforced.

[`docs/architecture.md` §13.3](architecture.md#133-container-constraints)
lists the default execution profile for any container the gateway
launches. This section translates each item into a testable invariant
that the #43 dispatcher (and any later real-container backend) must
satisfy.

Each invariant is paired with the test category that will cover it; the
test names are placeholders and will be pinned by #43.

1. **No network unless explicitly required.** Default container profile
   denies all network egress. Templates that need network must declare
   it on their registry entry, and the dispatcher MUST refuse to launch
   if a non-declaring template attempts to bind a socket. Test category:
   "container-egress-default-deny".
2. **Non-root user.** Container runs as a non-root UID. Test category:
   "container-uid-non-zero".
3. **Read-only root filesystem.** The container's root mount is
   read-only; writes go only to the staging path below. Test category:
   "container-rootfs-readonly".
4. **Private input mounted read-only.** Per-document private inputs are
   mounted read-only at a known path inside the container. Test
   category: "container-input-mount-ro".
5. **Output staging path.** Outputs are written only to
   `<vault_root>/staging/<job_id>/`. The dispatcher rejects any output
   that resolves outside this directory (path-traversal guard, mirroring
   `restoration_api`'s `relative_to()` check). Test category:
   "container-output-staging-confined".
6. **Resource limits.** CPU, memory, process count, and wall-time
   limits are set per template; default values come from a profile
   registry. Test category: "container-resource-limits-enforced".
7. **Dropped Linux capabilities.** Container drops every Linux
   capability not explicitly granted. Test category:
   "container-capabilities-dropped".
8. **No full private-vault mount.** Per
   [`docs/architecture.md` §11.3](architecture.md#113-operational-mitigations),
   the container MUST NOT mount the full `<vault_root>/private/`
   directory. Only the per-document inputs above are made available.
   Test category: "container-no-full-vault-mount".
9. **Explicit cleanup.** After the container exits (success or
   failure), the dispatcher removes `<vault_root>/staging/<job_id>/`
   and any temporary mount points. Test category:
   "container-staging-cleanup".

These invariants are **not yet enforced**. #42 only pins them as the
testable profile #43 (and any later real-container backend) must satisfy
before `execution_gateway.py` can be promoted past `deferred`.

## 6. Response status vocabulary

> Specification only; not yet enforced.

`ExecutionResponse.status` is a free-form `str` field in #42 (the
dispatcher in #43 will narrow it). The closed set the dispatcher will
emit:

- `"stub"` — reserved for the legacy
  `ExecutionGateway.execute()` return shape preserved by #42.
- `"accepted"` — template ran to completion; `artifacts` carries the
  produced handles.
- `"failed"` — template was refused (scope/purpose, schema-invalid,
  scrubber failure, container exit non-zero, etc.) OR the required
  audit write itself failed (see §4.1). `artifacts` is empty;
  `scrubbed_stderr` may carry a short diagnostic. The `reason` field
  carries an :class:`ExecutionFailureReason`; the dedicated
  `AuditWriteFailed` value distinguishes "audit pipeline failed" from
  the seven classifications that imply a durable audit row landed.

The dispatcher in #43 may narrow `status` to a `Literal[...]` or
`Enum`; that narrowing is a backwards-compatible refinement (every
value above is already a valid `str`).

## 7. Out of scope for #42

The following are deliberately not in this PR:

- **Real dispatcher.** `ExecutionGateway.execute()` continues to
  return the legacy stub dict. Owned by #43.
- **Real audit-record writer for gateway-mediated calls.** Owned by
  #43; will reuse the writer the restoration-request boundary already
  has.
- **Real container runtime, sandbox selection, or resource
  enforcement.** Owned by #43 and (likely) a follow-up issue.
- **Promotion past `deferred` in `scaffold-status.md`.** Stays
  `deferred`. Promotion criteria are gated on
  [`docs/backend-promotion.md`](backend-promotion.md) §4 and on the
  Chikaeshi-specific additions.
- **`boundary.execute_request` or `LocalFacade.execute`.** No new
  agent-facing entry point lands in #42. The reconciliation explicitly
  excludes modifying `facade.py`.
- **`docs/backend-promotion.md` §4 edits.** This document satisfies
  the §4 preconditions; it does not change the gate itself.
