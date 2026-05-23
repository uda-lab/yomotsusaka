# Backend Promotion Criteria

This document defines the gate that every future backend implementation must
pass before the corresponding module in
[`docs/scaffold-status.md`](scaffold-status.md) can be promoted from
`deferred` to `functional stub` or `functional`.

It is a **gate document, not a backend roadmap**. Listing a backend here does
not schedule, prioritise, or commit the project to implementing it. Every row
applies *only if* a separate child issue is opened that explicitly scopes the
real integration, and the boundary contracts named in this document must keep
passing under that integration.

See also: source-of-truth precedence is defined in
[`docs/architecture.md`](architecture.md#source-of-truth-precedence). When
this document and the architecture conflict on a privacy-boundary decision,
the architecture wins and this document is the bug.

## 1. Purpose and non-mandate clause

The MVP-1 (#4) kernel and the MVP-2 (#24) boundary contracts establish a
local public/private split. Several modules under `src/yomotsusaka/` exist
today only as `deferred` stubs whose role is to reserve the boundary, not to
commit the project to a particular backend:

- `src/yomotsusaka/runpod_lifecycle.py`
- `src/yomotsusaka/execution_gateway.py`
- `src/yomotsusaka/transfer.py`
- `src/yomotsusaka/search_gateway.py` (currently `functional stub`)
- `src/yomotsusaka/validator.py` (currently `functional`, plugin slot for
  detectors such as Presidio is `deferred`)
- `src/yomotsusaka/inference_backend.py` (currently `functional stub` via
  `DummyBackend`; real open-weight inference is `deferred`)

The existence of these stubs is **not** a commitment to implement them in
MVP-2. In particular:

- [`docs/runpod.md`](runpod.md) remains operational notes only. Its
  `Status` block (added by #14) already records the deferred posture; nothing
  in this document overrides that framing.
- Chikaeshi (private execution; `execution_gateway.py`) remains deferred and
  must remain deferred until a future issue explicitly scopes both the
  execution-policy specification and the container constraints defined in
  [`docs/architecture.md` §13](architecture.md#13-private-execution-gateway).
  See §4 below.
- Per umbrella #24 settled boundaries: MVP-2 does not implement real RunPod,
  vLLM, remote transfer, advanced PII detection, vector/FTS search, or
  arbitrary private execution. Those are later umbrellas.

Role and exposure-class definitions are out of scope for this document. They
are owned by #25; this gate document is silent on capability/role policy.

## 2. Backend inventory

The six backends below are taken from umbrella #24's deferred-backend list.
Each entry names the module that currently reserves the interface and the
architecture section that governs it. Promotion of any one of these is a
separate decision, controlled by §3 below.

1. **Real RunPod lifecycle** — `src/yomotsusaka/runpod_lifecycle.py`
   (architecture §7.1). Today: stub returning a hard-coded `PodHandle`.
2. **vLLM / open-weight model inference** — real implementations of the
   `InferenceBackend` ABC in `src/yomotsusaka/inference_backend.py`
   (architecture §5.3, §7.2). Today: `DummyBackend` echoes prompts.
3. **Remote transfer** — `src/yomotsusaka/transfer.py` (architecture §5.2).
   Today: `upload` returns `"stub://..."`; `download` raises.
4. **Presidio / PII detector plugins** — real validator backends behind
   `src/yomotsusaka/validator.py` (architecture §5.5, §14). Today: the
   MVP-1 validator enforces the redaction/keying contract; detector plugins
   are reserved.
5. **Vector / FTS search backend** — real backend behind
   `src/yomotsusaka/search_gateway.py` (architecture §12). Today: substring
   scan over redacted manifests.
6. **Private execution gateway behaviour** — real implementation behind
   `src/yomotsusaka/execution_gateway.py` (architecture §13). Today:
   `execute` returns `{"status": "stub", ...}`. Chikaeshi is the most
   dangerous extension; §4 imposes additional preconditions.

## 3. Backend × required-contract-test correspondence

Rows list each backend. Columns list the boundary-contract issues that own
the relevant test categories. Each cell names the test **categories** the
backend must pass before promotion, citing the source issue. The cells do
not name specific test function names because those names belong to the
source issues and may evolve; the categories are the stable contract.

`N/A` means the source issue's scope does not cover the backend. `N/A —
requires new contract issue` means promotion cannot proceed until a new
contract issue is opened for that backend-axis pair.

| Backend | #29 — `[Kamuzumi]` public artifact and exposure contract | #27 — `[Kukuri]` restoration request and audit | #28 — `[Chibiki+Kukuri]` local resolver contract |
| --- | --- | --- | --- |
| Real RunPod lifecycle (`runpod_lifecycle.py`) | Public-output recursive leakage scan still passes when batch lifecycle drives a real Pod (no Pod IDs, endpoints, credentials, or vault paths in agent-facing manifests, handles, logs, errors, batch state). | N/A | N/A |
| vLLM / open-weight model inference (`inference_backend.py`) | Public-output recursive leakage scan and leakage-through-error-messages scan still pass when the inference backend is real (no raw prompt fragments, no private spans, no model-side temporary paths in agent-facing outputs). | N/A | N/A |
| Remote transfer (`transfer.py`) | Public handles remain opaque after transfer; transfer-side identifiers and remote paths do not appear in agent-facing manifests, logs, or errors. | N/A | Resolver fail-closed report shape is preserved when the backing object lives behind a remote transfer; missing-remote-object → structured `ArtifactMissing` (or new equivalent reason) with no remote path, credential, or profile in `detail`. |
| Presidio / PII detector plugins (`validator.py`) | Validator does not emit raw `original_value`, private dictionary paths, or detector-internal feature strings in error messages or logs; placeholder/key invariants from the MVP-1 validator remain enforced. | N/A | N/A |
| Vector / FTS search backend (`search_gateway.py`) | Search results contain only redacted snippets and opaque keys; raw query terms supplied by callers do not reappear in result objects, scores, ranking explanations, or logs. | N/A | Resolver may be invoked for query-term resolution and must stay fail-closed; malformed or unknown query-derived locators do not fall back to filesystem or index probing, and resolver failure reports remain non-leaky. |
| Private execution gateway (`execution_gateway.py`) — Chikaeshi | Scrubbed stdout/stderr only; artifact handles remain opaque; no private path, no generated-private-content, no private dictionary entry, and no job-side temporary path leaks into agent-facing responses, logs, or errors. | Every gateway-mediated restoration of private values produces an audit record at `<vault_root>/audit/restoration.jsonl` (or the contract-equivalent location at the time of promotion); `outcome="deferred"` never appears; denials and schema-invalid requests are still audited. The additional `policy_verdict` / `policy_matched_profile` columns added by issue #44 are non-leaking (verdict is an enumerated permit/deny string; matched profile is a caller-supplied label, never a private value). | Resolver fail-closed for missing job artifacts and missing output artifacts; resolver never falls back to filesystem probing for unknown job/output handles. |

Re-check the cells against each source issue's acceptance criteria at the
time of promotion. If #27 or #29 has redefined a category, follow the source
issue. If a future PR proposes a backend that does not match any row above,
add a new row in the same PR — do not promote the backend without an
explicit cell.

## 4. Promotion procedure

The following ordered steps gate every backend promotion from `deferred` to
`functional stub` or `functional`:

1. **Child issue exists.** A separate child issue, distinct from the umbrella
   and from any boundary-contract issue, scopes the real integration. The
   issue body names the backend, the module being promoted, and the target
   classification (`functional stub` or `functional`).
2. **Listed contract tests pass with the real backend wired in.** Every
   non-`N/A` cell in §3 corresponding to the promoted backend is exercised
   by the existing tests under `tests/`, and those tests pass on `uv run
   pytest`. If a row is `N/A — requires new contract issue`, that contract
   issue must be opened and merged **before** the backend PR can merge.
3. **`docs/scaffold-status.md` updated in the same PR.** The module's row in
   the scaffold-status table is updated from `deferred` to the new
   classification, with the new "Current behavior" cell describing the real
   backend, and the "MVP role" cell adjusted if necessary. The classification
   vocabulary in `docs/scaffold-status.md` is the canonical promotion
   vocabulary.
4. **No boundary-contract test is weakened, skipped, `xfail`ed, or replaced
   with a softer assertion to make the backend pass.** See §5. If a test
   genuinely needs to change to accommodate a new backend, that change is
   its own separate issue and PR, reviewed independently from the backend
   PR.

### Chikaeshi-specific additions

Private execution (Chikaeshi; `execution_gateway.py`) carries the highest
risk of weakening the public/private boundary, and the umbrella #24 design
philosophy treats it as a powerful future extension that should not be part
of the first MVP. Promotion of `execution_gateway.py` therefore requires,
**in addition to** steps 1–4 above:

- a separate child issue dedicated to the private execution backend (it may
  not piggy-back on a different backend's promotion PR);
- an explicit execution-policy specification in the child issue, citing
  [`docs/architecture.md` §13.1–§13.4](architecture.md#13-private-execution-gateway);
- the container constraints from §13.3 (no network by default; non-root
  user; read-only root filesystem; controlled output staging; resource
  limits; dropped capabilities; no full private-vault mount; explicit
  cleanup) translated into a concrete, testable profile in the child issue;
- the `#27` audit-record contract extended to cover every gateway-mediated
  restoration of private values, with audit records present on denial and
  schema-invalid requests as well as on accepted requests.

Chikaeshi remains `deferred` until all of the above are explicitly scoped
and merged.

## 5. Non-weakening clause

No backend integration may weaken the public/private artifact contract.
Specifically:

- The boundary-contract tests owned by #29 (public-output leakage scans),
  #27 (restoration audit shape), and #28 (resolver fail-closed report
  shape) MAY NOT be relaxed, skipped, `xfail`ed, deleted, parametrised
  away, or replaced with softer assertions inside a backend-promotion PR.
- If a contract test genuinely needs to change to accommodate a new
  backend, the test change must be proposed in its own separate issue and
  PR, reviewed independently from the backend implementation, and approved
  on the merits of the contract change itself — not on the merits of any
  pending backend work.
- Raw private values, vault paths, private dictionary paths, and
  non-opaque handles must not appear in any agent-facing surface after a
  backend is promoted. The surfaces are the ones enumerated by #29
  (manifests, handles, search results, batch states, ordinary errors,
  ordinary logs, CLI/API output intended for agents) plus any new surface
  the backend introduces. Any new surface the backend introduces must be
  added to the #29 scan in the same PR or in a precondition PR.
- Backend PRs that need to widen the public surface (for example, by adding
  a new agent-facing field) must update the relevant contract tests to
  cover the new surface **before** widening it, not after.

## 6. Maintenance

- When this document changes, cross-check
  [`docs/architecture.md`](architecture.md) for contradictions and update
  the architecture in the same PR if needed.
- When a deferred module is promoted under a child issue, update both
  [`docs/scaffold-status.md`](scaffold-status.md) and this document in the
  same PR so that the row in §3 reflects the current contract-test scope.
- When #27, #28, or #29 changes its acceptance criteria, re-check that the
  cells in §3 still describe categories that those issues actually cover;
  adjust cell text and source-issue references accordingly.
