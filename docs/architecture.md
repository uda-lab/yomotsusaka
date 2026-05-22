# Private Data Firewall MVP Architecture

## 1. Purpose

This document defines the MVP architecture for a private-data preprocessing framework. The system is intended to reduce accidental exposure of private or institutionally sensitive documents to general-purpose coding agents and LLM agents while preserving practical utility.

The core idea is to interpose a controlled preprocessing layer between raw private data and ordinary agent-facing workspaces. Raw data remains in a private storage boundary. Agents operate primarily on redacted manifests, labels, keys, and summaries. When direct private information is operationally necessary, access is mediated through project-provided APIs rather than by mounting or exposing the raw data store.

This is not a perfect confidentiality system. It is a best-effort private data firewall for practical research and engineering workflows.

## Source of truth precedence

When guidance disagrees, follow this order:

1. Explicit user instruction on the active task.
2. The open umbrella issue and its linked child issue.
3. `docs/architecture.md` (this document).
4. `README.md`.
5. Module docstrings and existing tests.

If these sources conflict on privacy-boundary decisions, follow `docs/architecture.md` and record the conflict in the active child issue or PR.

## Capability and exposure model

This section is the canonical decision surface for "may this role do this thing
to an artifact of this exposure class?". Downstream MVP-2 work — request/response
schemas, restoration request shape, resolver, contract tests, and the local
boundary facade — must cite this section rather than re-deriving the split.

The model adapts the publication-class discipline of `uda-lab/spread-applicant-ai`
to the Yomotsusaka local firewall; it is not a direct copy. Identifiers below
are documentation tokens. They are deliberately not introduced as Python
identifiers in this section's PR; later issues may choose canonical Python names.

### Roles

- **ordinary agent** — a general-purpose coding or LLM agent (Claude Code,
  similar) operating on the agent-facing workspace. It interacts with the
  firewall only through boundary interfaces (public manifests, opaque handles,
  redacted search, restoration requests). It does not read the private vault,
  does not read private dictionary files, and does not import private-side
  modules to bypass the boundary.
- **private-boundary service** — code running inside the private trust
  boundary (`restoration_api.py`, `redactor.py`'s key-issuing path, the search
  gateway's query-resolution stage, the resolver in #28). It may read and write
  the private vault, may read private dictionaries, and may handle raw private
  values in memory. It is responsible for emitting only public-safe artifacts
  back across the boundary.
- **human/operator** — the project owner or another authorised human. The
  operator may inspect the private vault directly out-of-band, configure
  policy, approve sensitive restoration requests, and review audit logs. The
  operator is not an automated agent; capability cells assume direct shell or
  filesystem access rather than mediated request flows.
- **restricted reviewer or task scope** — a reviewer agent or a task-scoped
  subagent whose operating scope is narrower than the ordinary agent role
  (e.g. a code-review subagent inspecting a PR without read access to private
  vault contents, or a delegate restricted to a specific document subset). It
  is a strict subset of the ordinary agent role and inherits ordinary-agent
  denies; additional `scoped` cells encode the narrower envelope.
- **test harness** — the `pytest` test runner and supporting fixtures. It
  splits along the exposure boundary: tests under the private-boundary contract
  (`test_redactor.py`, `test_restoration_api.py`, future resolver tests) may
  assert against private dictionary contents using the canonical local fixture;
  public-output contract tests (the #29 scan) must not contain raw private
  values outside that fixture and must not assert that public artifacts equal
  canonical raw values.

### Exposure classes

Each artifact in the system carries an exposure class. The class is operational,
not a free-form label: it constrains where the artifact may live, which
surfaces may emit it, and which fixtures may contain it.

- **`agent_public`** — content that is intrinsically safe for ordinary agents.
  - Vault locations: agent-facing index only (e.g. `<vault_root>/manifests/`
    public fields, search index files).
  - API/CLI surfaces: all ordinary agent-facing endpoints, ordinary log
    streams, status responses, error messages returned to ordinary agents.
  - Test fixtures: any test may contain `agent_public` values literally.
- **`agent_redacted`** — derived material in which private spans have been
  replaced by stable keys and any raw values have been removed.
  - Vault locations: agent-facing index (redacted documents, redacted
    summaries, redacted search snippets, public manifests' redacted-text
    fields).
  - API/CLI surfaces: the same surfaces as `agent_public`; ordinary agents
    receive `agent_redacted` artifacts by default rather than `private` ones.
  - Test fixtures: any test may contain `agent_redacted` values; public-output
    contract tests assert that ordinary outputs are at most `agent_redacted`.
- **`private`** — material that may be resolved only inside the private
  boundary.
  - Vault locations: private dictionary files (`<vault_root>/private/`), raw
    documents under the private vault, restoration audit records.
  - API/CLI surfaces: private-boundary service code only; never returned to
    ordinary agents, never written to ordinary log streams, never embedded in
    public manifests, search results, or batch state payloads.
  - Test fixtures: private-boundary tests only (e.g. `test_redactor.py`,
    `test_restoration_api.py`), and only against the canonical local fixture.
- **`restricted`** — `private` plus a narrower scope/profile requirement. The
  artifact is still private-boundary-only; additionally, even inside the
  private boundary it requires a matching scope or profile to be resolved
  (e.g. a sub-corpus that the operator has marked as additionally scoped, or a
  restoration purpose that must satisfy a stricter policy than ordinary
  `private` restoration).
  - Vault locations: same as `private`, plus a scope/profile marker recorded
    alongside the artifact.
  - API/CLI surfaces: private-boundary services that present and check the
    matching scope/profile; the resolver in #28 must fail-closed when the
    caller's scope does not satisfy the artifact's requirement.
  - Test fixtures: private-boundary tests that explicitly exercise the scope
    check; public-output contract tests do not contain `restricted` artifacts.
- **`never_expose`** — material that must not enter tracked, agent-facing, or
  private-boundary outputs at all. This class is the deny-by-default sink for
  values the system never legitimately needs to re-emit (e.g. cleared raw
  buffers after keying, credential material loaded from environment).
  - Vault locations: not stored; if present transiently in memory, must be
    dropped after use.
  - API/CLI surfaces: none. No surface may emit a `never_expose` artifact, and
    error messages must not echo `never_expose` content back to the caller.
  - Test fixtures: not permitted; tests assert absence rather than presence.

### Capability decision matrix

Rows are roles. Columns are capabilities. Cell values are exactly one of
`permit`, `deny`, `scoped`, or `N/A`:

- `permit` — the role may perform the capability without further restriction
  beyond ordinary boundary checks.
- `deny` — the role must not perform the capability; boundary interfaces and
  tests should enforce this.
- `scoped` — the role may perform the capability only within a stated
  envelope; the envelope is given in the corresponding footnote.
- `N/A` — the capability is not meaningful for this role in MVP-2; treat as
  `deny` for safety, but the role does not have a real use case for it here.

Capability semantics (neutral verbs, chosen so #26 schemas and #30 facade can
adopt them without renaming):

- `read` — observe an artifact's contents.
- `write` — create or modify an artifact.
- `request` — submit a structured request across a boundary (process, search,
  restore, inspect, audit-log retrieval).
- `restore` — obtain raw private values for a given key or handle.
- `log` — emit a record into the audit/log stream.
- `receive_output` — be a legitimate recipient of an operation's return value
  or rendered output.

The matrix encodes the default envelope per role; per-artifact policy refines
it through the exposure class. The matrix does not authorise reading `private`
artifacts when the role's `read` cell is `permit` — the exposure class is an
independent gate.

| Role                                | read           | write          | request        | restore        | log            | receive_output |
| ----------------------------------- | -------------- | -------------- | -------------- | -------------- | -------------- | -------------- |
| ordinary agent                      | scoped [^oa-r] | scoped [^oa-w] | permit         | deny           | scoped [^oa-l] | scoped [^oa-o] |
| private-boundary service            | permit         | permit         | permit         | permit         | permit         | permit         |
| human/operator                      | permit         | permit         | permit         | permit         | permit         | permit         |
| restricted reviewer or task scope   | scoped [^rr-r] | deny           | scoped [^rr-q] | deny           | scoped [^rr-l] | scoped [^rr-o] |
| test harness                        | scoped [^th-r] | scoped [^th-w] | permit         | scoped [^th-x] | permit         | scoped [^th-o] |

[^oa-r]: Ordinary agents may read artifacts of exposure class `agent_public` or
    `agent_redacted` only. Reading `private`, `restricted`, or `never_expose`
    is denied; the boundary interfaces must refuse to surface such artifacts to
    ordinary agents.
[^oa-w]: Ordinary agents may write only to the agent-facing index through
    boundary operations (e.g. submitting a process request that ultimately
    produces `agent_public` / `agent_redacted` artifacts). Direct writes to
    `<vault_root>/private/` or to private dictionary files are denied.
[^oa-l]: Ordinary agents may cause log entries through their requests, but the
    emitted log records themselves must be at most `agent_redacted`; raw
    private values must not appear in any log stream observable by ordinary
    agents.
[^oa-o]: Ordinary agents may receive operation outputs whose exposure class is
    `agent_public` or `agent_redacted`. Outputs of class `private`,
    `restricted`, or `never_expose` are denied; restoration responses must be
    delivered through the explicit restoration request path defined in #27,
    not as ordinary operation outputs.
[^rr-r]: Restricted reviewers inherit ordinary-agent read scoping and add the
    reviewer's narrower task scope (e.g. only the documents named in the
    current review). Cells default to the ordinary-agent envelope intersected
    with the reviewer's scope.
[^rr-q]: Restricted reviewers may submit requests only within their declared
    task scope. Requests that escape the scope must be refused by the boundary
    rather than silently broadened.
[^rr-l]: Restricted reviewers may cause log entries within their scope;
    cross-scope log access is denied even when the underlying records would be
    `agent_redacted`.
[^rr-o]: Restricted reviewers receive outputs at most `agent_redacted` and
    only for artifacts within their declared scope.
[^th-r]: The test harness may read artifacts of any exposure class up to
    `private` when the test file is itself a private-boundary contract test
    (per #29). Public-output contract tests must not read `private` or
    `restricted` artifacts. `never_expose` is denied for all tests.
[^th-w]: The test harness writes against temporary vault roots only; it must
    not write into a production vault. Tests may construct `private` fixtures
    only when those fixtures match the canonical local fixture and the test
    is a private-boundary contract test.
[^th-x]: The test harness may exercise `restore` only in private-boundary
    contract tests, and only to assert the restoration contract; public-output
    contract tests must not call `restore`.
[^th-o]: The test harness receives outputs commensurate with the test's
    classification: public-output tests receive only `agent_public` /
    `agent_redacted`; private-boundary tests may receive `private` outputs for
    assertion against the canonical fixture.

### Boundary statements

The capability matrix is bounded by the umbrella #24 settled boundaries.
These are restated here because they bind the matrix even when a cell would
otherwise read `permit`:

1. **Ordinary agents use boundary interfaces only.** Ordinary agents do not
   read or write the private vault directly, do not open private dictionary
   files, and do not import private-side modules to bypass the boundary. The
   `permit` and `scoped` cells in the ordinary-agent row are realised through
   `agent_public` / `agent_redacted` surfaces, not through filesystem access
   to `<vault_root>/private/`.
2. **Local-first.** MVP-2 stays local. Remote transfer, hosted GPU
   orchestration, and remote restoration backends remain out of scope. No
   capability cell authorises crossing the local-first line.
3. **No hosted proprietary LLM in the core private-data path.** The matrix's
   `restore` and `read` cells for private-boundary services do not authorise
   sending raw private values to a hosted proprietary LLM API. Open-weight
   local backends remain the only sanctioned model substrate for private-data
   processing.
4. **Private vault is not mounted into ordinary agent workflows.** No cell —
   including the operator row — authorises mounting `<vault_root>/private/`
   into an ordinary agent container or into a workflow that an ordinary agent
   shares. Operator access is direct and out-of-band; it is not a route to
   widen ordinary-agent capability.

## 2. Design Philosophy

### 2.1 Best-effort privacy, not absolute isolation

The system is designed under a realistic assumption: useful agents sometimes need to reason about or retrieve private information. A design that makes all private data strictly inaccessible may be cleaner in theory but too weak in practice.

Therefore, the MVP does not attempt to make private data cryptographically unreachable from every execution path. Instead, it reduces the default exposure surface:

- agents do not receive raw private documents by default;
- private dictionaries are not mounted inside ordinary agent containers;
- raw values are replaced by stable reference keys;
- restoration is possible only through controlled project APIs;
- accesses can be logged, scoped, rate-limited, and reviewed.

The goal is to prevent careless, unnecessary, or ambient access. Protection against actively malicious agents is a later and harder problem.

### 2.2 Separate knowledge of existence from access to contents

The agent-facing system should know that certain information exists, what kind of information it is, and how it may be referenced. It should not automatically receive the private values themselves.

This produces a layered representation:

- raw document: private, stored in the private vault;
- redacted document: agent-readable;
- metadata manifest: agent-readable;
- private dictionary: not directly agent-readable;
- controlled restoration API: available only through explicit project-defined interfaces.

This distinction is central. The system is not merely a redactor; it is a boundary between data awareness and data possession.

### 2.3 Short-lived compute, durable private storage

RunPod or another GPU provider is treated as an ephemeral compute substrate. It is not the system of record.

The existing VPS remains the durable storage side. RunPod is used only when GPU-backed preprocessing is needed. Except for cached open-weight model files, RunPod should not retain large or sensitive data after a job completes.

Principles:

- raw inputs originate from the VPS-side private vault;
- RunPod receives temporary working copies only;
- output artifacts are returned to the VPS;
- temporary files on RunPod are deleted after processing;
- failed jobs must not make RunPod the only location of any important state.

### 2.4 Provider-agnostic, but open-weight by default

The architecture must not depend on a specific model provider. The backend model and GPU provider are replaceable.

However, the MVP assumption is explicit:

> The privacy preprocessing model should be an open-weight model running on an ephemeral or self-hosted GPU runtime.

Hosted proprietary APIs such as Anthropic, OpenAI, Google, or similar services must not be silently substituted for the open-weight local model backend. Such substitution would violate the purpose of the architecture unless explicitly approved for a specific non-private workload.

The architecture should refer to model capabilities rather than vendor identity:

- structured extraction;
- semantic labelling;
- conservative candidate identification;
- multilingual handling;
- batch inference efficiency;
- replaceability.

### 2.5 LLM as assistant, not authority

The LLM is used to propose structured labels, entity candidates, summaries, and sensitivity classifications. It does not define the security boundary by itself.

The authoritative parts of the pipeline are conventional software components:

- queue state transitions;
- key issuance;
- dictionary storage;
- redaction application;
- artifact validation;
- access control;
- audit logging.

The LLM should be treated as a semantic worker inside a larger deterministic workflow.

## 3. Non-goals

The MVP explicitly does not aim to provide:

- perfect prevention of private-data access by all agents;
- protection against a fully malicious privileged operator;
- formal privacy guarantees;
- irreversible anonymisation;
- enterprise-grade DLP coverage;
- automatic legal or institutional compliance;
- a general-purpose hosted LLM service;
- permanent GPU-side data storage;
- dependence on a proprietary hosted LLM API.

These may be studied later, but they should not block the MVP.

## 4. High-level Architecture

```text
Existing VPS
  ├─ Private Vault
  │   ├─ raw documents
  │   ├─ private dictionary database
  │   └─ restoration API backend
  │
  ├─ Public/Agent-facing Index
  │   ├─ document manifests
  │   ├─ redacted summaries
  │   ├─ redacted documents
  │   └─ retrieval keys
  │
  ├─ Batch Scheduler
  │   ├─ job queue
  │   ├─ RunPod lifecycle controller
  │   └─ result validator/committer
  │
  └─ Agent Containers
      ├─ no direct private vault mount
      ├─ read access to public index
      └─ controlled access through project API

Ephemeral RunPod Job
  ├─ open-weight model runtime
  ├─ structured extraction worker
  ├─ redaction/keying worker
  ├─ temporary input workspace
  └─ temporary output staging
```

The VPS is the durable trust boundary. RunPod is a disposable accelerator.

## 5. Pipeline Boundaries

The MVP pipeline is defined by explicit boundaries so that future recognisers, validators, or anonymisation components can be added without rewriting the system.

### 5.1 Input boundary

Responsible for selecting raw documents and preparing a batch.

Inputs:

- raw files from the private vault;
- file metadata;
- project-specific policy hints;
- optional document-type expectations.

Outputs:

- immutable batch package;
- batch manifest;
- transfer instructions for RunPod.

Boundary rule:

> The input boundary may read raw data, but ordinary agent containers must not.

### 5.2 Transfer boundary

Responsible for moving temporary data to RunPod and returning results.

Principles:

- transfer only queued files;
- avoid syncing the entire private vault;
- avoid persistent RunPod-side storage for private data;
- include checksums for input and output artifacts;
- make retries idempotent.

### 5.3 Model inference boundary

Responsible for semantic extraction using an open-weight model.

Expected outputs:

- document type;
- sensitivity class;
- semantic labels;
- candidate private spans;
- proposed entity types;
- redacted summary;
- public index terms;
- uncertainty flags.

This boundary should expose a stable schema. The backend model may be replaced without changing downstream storage or access-control logic.

### 5.4 Redaction and keying boundary

Responsible for converting candidate spans into stable private references.

Duties:

- assign document IDs;
- assign entity keys;
- replace private spans with placeholders;
- build private dictionary entries;
- build public redacted artifacts;
- preserve enough information for restoration when authorised.

This component should be deterministic where possible.

### 5.5 Validation boundary

Responsible for checking that generated public artifacts are structurally valid and do not obviously contain raw private data.

MVP validation may be lightweight:

- JSON schema validation;
- checksum verification;
- simple regex scans;
- placeholder consistency checks;
- dictionary/public-index separation checks.

More advanced validators can be added later.

### 5.6 Commit boundary

Responsible for atomically moving results from staging to durable VPS storage.

The commit must not partially expose a failed batch. A practical state model is:

```text
queued → transferred → processing → staged → validated → committed
                                      ↘ failed
```

Only validated outputs should become visible to agent-facing systems.

### 5.7 Access boundary

Responsible for agent interaction after preprocessing.

Agents may access:

- redacted documents;
- manifests;
- labels;
- summaries;
- keys;
- controlled restoration APIs.

Agents must not access:

- raw documents by filesystem mount;
- private dictionary database files;
- batch input workspaces;
- model-side temporary files.

## 6. Storage Model

### 6.1 Private vault

The private vault is the authoritative store for raw private material. It lives on the existing VPS or another durable private storage system controlled by the project.

Contents:

- raw documents;
- private dictionary database;
- restoration metadata;
- batch queue source records;
- audit records for restoration requests.

The private vault is not mounted into ordinary agent containers.

### 6.2 Public/agent-facing index

The public index is the ordinary workspace for agents.

Contents:

- redacted documents;
- document manifests;
- sensitivity labels;
- entity labels;
- reference keys;
- redacted summaries;
- retrieval hints.

The public index must be useful enough for agents to plan, search, classify, and request more information without seeing raw private values by default.

### 6.3 Private dictionary

The private dictionary maps reference keys to private values and contextual restoration metadata.

Example conceptual entry:

```json
{
  "entity_key": "PERSON_2026_000123",
  "entity_type": "PERSON",
  "raw_value": "...",
  "source_document_id": "doc_...",
  "span_offsets": [120, 128],
  "access_policy": "project_api_only"
}
```

The dictionary is recoverable and reversible by design. The system chooses practicality over strict irreversibility.

### 6.4 RunPod-side storage

RunPod-side storage is not authoritative.

Allowed persistent data:

- open-weight model cache;
- container image layers;
- non-sensitive runtime dependencies.

Disallowed persistent data:

- raw private document corpus;
- private dictionary database;
- long-lived private batch outputs;
- agent-facing durable indexes.

Temporary private files may exist during a job, but they must be cleaned up after successful or failed execution.

## 7. Compute Model

### 7.1 RunPod lifecycle

RunPod is started only when a batch requires GPU processing.

The VPS-side scheduler should:

1. create or resume a RunPod instance;
2. wait for readiness;
3. transfer the batch package;
4. run the preprocessing job;
5. retrieve staged outputs;
6. validate and commit on the VPS;
7. stop or terminate the RunPod instance.

The system should assume that RunPod may fail or disappear. The VPS-side queue remains the source of truth.

### 7.2 Open-weight model backend

The model backend should run locally on the RunPod instance. Candidate runtimes include vLLM or equivalent open-weight inference systems.

The architecture does not require a specific model family. The model must support the extraction task sufficiently well and should provide structured output or be wrapped by a parser/validator.

The model is replaceable. The pipeline depends on the schema, not on the model brand.

### 7.3 Offline batch preference

For the MVP, offline batch execution is preferred over exposing a long-running API server.

Rationale:

- fewer network surfaces;
- simpler lifecycle;
- easier cleanup;
- direct fit for nightly or scheduled processing;
- no need to keep GPU compute idle.

A server mode may be introduced later if interactive preprocessing or service-style integration becomes necessary.

## 8. Data Products

The batch job should produce at least three artifact classes.

### 8.1 Public manifest

Agent-readable metadata for each processed document.

Possible fields:

- document ID;
- source file fingerprint;
- document type;
- sensitivity level;
- semantic labels;
- entity inventory;
- public summary;
- public index terms;
- uncertainty flags;
- processing timestamp;
- model/runtime metadata.

### 8.2 Redacted document

A version of the document where private spans are replaced by stable keys.

Example:

```text
<PERSON:PERSON_0001> submitted a report concerning <PROJECT:PROJECT_0003>.
```

This artifact is intended for ordinary agent use.

### 8.3 Private dictionary update

A private mapping from keys to raw values.

This artifact is committed only to the private vault and is never published into the agent-facing index.

## 9. Restoration Model

Restoration is allowed, but mediated.

An agent may request private values through a project-provided API. The API should require structured requests rather than arbitrary database access.

A restoration request should include:

- requesting agent or process identity;
- target key;
- purpose or reason;
- requested scope;
- optional task or issue reference.

The MVP can begin with permissive behaviour, but the boundary should exist from the start. Later controls may include:

- per-agent scopes;
- rate limits;
- allow/deny policies by entity type;
- human approval for sensitive classes;
- audit review;
- automatic redaction of restoration responses.

The key principle is not absolute denial. It is controlled, observable access.

## 10. Failure and Atomicity

A failed batch must not corrupt the public index or private dictionary.

The system should prefer append-only or staged writes:

- write RunPod outputs to staging;
- validate staging artifacts;
- commit dictionary and public artifacts together;
- mark batch as committed only after both sides are durable;
- retain enough logs to retry failed documents.

Idempotency is important. Re-running the same batch should not create uncontrolled duplicate keys unless the design explicitly permits versioned keys.

## 11. Security Posture

The MVP security posture is pragmatic.

### 11.1 What it reduces

- accidental prompt stuffing of raw documents;
- ambient exposure of private data inside agent containers;
- unnecessary replication of private corpora;
- uncontrolled long-running GPU-side data retention;
- casual browsing of private values by ordinary tools.

### 11.2 What it does not solve

- malicious privileged agents;
- compromised VPS host;
- compromised RunPod runtime;
- false negatives in private-span detection;
- policy correctness;
- legal compliance by itself.

### 11.3 Operational mitigations

Even in the MVP, the following should be considered:

- no direct private vault mount in agent containers;
- minimal RunPod retention;
- filesystem permission separation;
- API-level access logging;
- batch checksums;
- explicit cleanup step;
- small validation pass before public commit.

## 12. Redacted Search Gateway

A redacted search gateway should be treated as an early post-MVP extension, and its boundary should be recognised from the beginning.

The purpose is to let agents search for private-document knowledge without directly touching private values. An agent should be able to ask operational questions such as:

```text
Where is the file for the meeting with <PERSON_KEY> on a certain date?
```

or, when mediated by the private boundary:

```text
Where is the file for the meeting with a private person name supplied by the user?
```

and receive only redacted search results.

### 12.1 Search boundary

The agent must not directly query the private vault or the private dictionary database. Instead, search requests pass through a gateway.

```text
agent or user query
  ↓
redacted search gateway
  ↓
query normalisation / key resolution
  ↓
redacted index search
  ↓
redacted hit list
```

The gateway may operate inside the private boundary. It may temporarily inspect private values when resolving a user query, but it must return only redacted results to the agent-facing side.

### 12.2 Returned search results

Search results should be useful but non-revealing by default.

Allowed result fields:

- document ID;
- file or artifact handle;
- redacted title;
- redacted snippet;
- document type;
- sensitivity labels;
- date or time metadata;
- entity keys;
- confidence score;
- restoration eligibility metadata.

Disallowed result fields:

- raw private spans;
- raw private titles;
- private dictionary rows;
- unredacted document excerpts;
- arbitrary file paths inside the private vault.

### 12.3 Query privacy

Queries themselves may contain private information. For example, a user may ask about a named person, internal meeting, grant, student, or unpublished project.

Therefore, the search gateway should treat the query as private input. It may translate raw query terms into internal keys before searching the redacted index. This keeps the agent-facing search interaction centred on keys and labels rather than raw values.

### 12.4 Implementation posture

The MVP does not need a sophisticated search engine. The initial implementation may use a simple full-text index over redacted documents and manifests. The architecture should only require that the search backend can be replaced later.

Possible backend families:

- SQLite FTS or similar local full-text search;
- Tantivy or another embedded search engine;
- Meilisearch or an equivalent lightweight search service;
- OpenSearch or a heavier search/governance stack later if needed.

The key architectural point is not the search engine itself. It is the gateway boundary between private query resolution and redacted result exposure.

## 13. Private Execution Gateway

A private execution gateway is a powerful future extension. It should not be part of the first MVP, but the architecture should reserve a clean boundary for it.

The purpose is to let agents request private-data computations without giving the agent direct filesystem access to private data.

Example uses:

- generate a document containing private values;
- fill a private form;
- create a PDF from private records;
- process meeting minutes containing private names;
- transform private tabular data into a deliverable file.

The agent submits a job specification or script. The system executes it inside an isolated environment with controlled private-data access. The agent receives only status information and artifact handles, not the generated private content itself.

### 13.1 Execution boundary

```text
agent job request
  ↓
private execution gateway
  ↓
job validation / policy check
  ↓
isolated container execution
  ↓
private output staging
  ↓
status + artifact handles returned to agent
```

The isolated job may access private data according to its policy. The submitting agent does not receive direct access to the private mount or generated private artifacts.

### 13.2 Initial implementation principle

The first implementation should prefer predefined or template-based jobs rather than arbitrary agent-submitted programs.

Examples:

- `generate_letter_from_private_template`;
- `summarise_private_minutes`;
- `fill_private_form`;
- `render_private_pdf`;
- `export_private_table_view`.

Only after this is stable should the system consider arbitrary scripts or custom containers.

### 13.3 Container constraints

If container execution is introduced, the default execution profile should be restrictive:

- no network unless explicitly required;
- non-root user;
- read-only root filesystem;
- private input mounted read-only;
- output written only to a controlled staging directory;
- CPU, memory, process, and time limits;
- dropped Linux capabilities where possible;
- no direct mount of the full private vault;
- explicit cleanup after execution.

The goal is not perfect sandboxing. The goal is to keep private-data use localised, intentional, observable, and separated from ordinary agent context.

### 13.4 Returned execution information

The gateway may return:

- job ID;
- status;
- exit code;
- scrubbed stdout/stderr;
- validation warnings;
- artifact handles;
- review instructions for the user.

The gateway should not return:

- generated private document contents;
- unsanitised stdout/stderr;
- raw private records;
- direct private file paths;
- private dictionary entries.

This remains a best-effort boundary. Some stdout/stderr leakage risk exists, especially if agents submit code. That risk should be reduced by scrubbing, conventions, templates, and audit logs, but not treated as fully solved in the MVP architecture.

## 14. Plugin Strategy

The MVP should keep plugin boundaries clear but avoid over-engineering.

Potential future plugin types:

- detector plugins;
- validator plugins;
- anonymizer plugins;
- model backend plugins;
- storage backend plugins;
- restoration policy plugins.

Examples of low-priority future integrations:

- Microsoft Presidio for rule-based and NLP-based PII detection/anonymisation;
- OpenAI Privacy Filter for local PII span detection;
- LLM Guard or similar tools for prompt/output scanning;
- custom university recognisers for student IDs, internal committees, grants, and local naming conventions.

These are intentionally not required for the MVP. Their main architectural role at this stage is to motivate clean pipeline boundaries.

## 15. MVP Scope

A reasonable first MVP should implement only:

1. VPS-side batch queue;
2. RunPod lifecycle script;
3. temporary transfer of batch files;
4. one open-weight model backend;
5. structured extraction schema;
6. Python redactor/key issuer;
7. private dictionary storage;
8. public manifest/redacted document output;
9. lightweight validation;
10. controlled restoration API scaffold;
11. redacted search gateway scaffold;
12. private execution gateway boundary definition.

Everything else is future work.

## 16. Repository Layout

```text
yomotsusaka/
  README.md
  pyproject.toml
  docs/
    architecture.md
    runpod.md
    naming.md
    scaffold-status.md
  src/yomotsusaka/
    __init__.py
    schemas.py
    redactor.py
    validator.py
    commit.py
    batch_queue.py
    inference_backend.py
    restoration_api.py
    search_gateway.py
    execution_gateway.py
    runpod_lifecycle.py
    transfer.py
  tests/
    test_redactor.py
    test_restoration_api.py
    test_schemas.py
  config/
    policy.example.yaml
    model.example.yaml
  scripts/
    run_nightly_batch.sh
```

This structure keeps the conceptual boundaries visible without requiring a large framework.

For the canonical per-module classification (functional / functional stub / deferred), current behavior, and MVP role, see [`docs/scaffold-status.md`](scaffold-status.md). Downstream agents should consult that table before extending any module.

Before promoting any `deferred` module to `functional stub` or `functional`, see [`docs/backend-promotion.md`](backend-promotion.md) for the gate criteria (boundary-contract tests that must pass, non-weakening clause, Chikaeshi-specific additions).

## 17. Guiding Principle

The system should make the safe path the ordinary path.

Agents should naturally see redacted, labelled, structured representations. Raw private data should require an explicit, mediated action. The system should not pretend that private data is impossible to access; instead, it should make access intentional, localised, inspectable, and avoidable in the common case.

The MVP succeeds if it changes the default from:

```text
Agents receive raw private documents because that is convenient.
```

to:

```text
Agents receive structured redacted knowledge by default, and private values are restored only when explicitly needed.
```
