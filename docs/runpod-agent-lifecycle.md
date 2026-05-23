# RunPod Agent-Managed Lifecycle

Status: agent-runnable cost-controlled lifecycle helper; **not** a CI
merge gate. The L1 (mocked HTTP) and L2 (mocked lifecycle module) unit
tests under `tests/test_runpod_lifecycle.py` are the merge gate; the L3
owner-runnable live test is gated by `RUNPOD_MANAGE_LIVE=1` and stays
opt-in (mirrors `RUNPOD_LIVE_SMOKE`).

This runbook covers issue #76 (closes #70): an agent-runnable helper
that creates a Pod, waits for vLLM to become healthy, runs the sanitised
diagnostic smoke, and **deletes** the Pod by default — so an agent run
cannot accidentally leave a paid Pod running.

For the smoke-only flow that attaches to an already-provisioned Pod, see
`docs/runpod-agent-smoke.md` (issue #68).

## 1. Owner vs agent responsibilities

The split is intentionally narrow and must be preserved.

### Owner responsibilities

- Create a scoped RunPod **account API key** (NOT a vLLM bearer key —
  see §9), set it as `RUNPOD_API_KEY` in the dev container shell, and
  rotate / disable it after the session (§10).
- Choose the GPU type, disk size, and image (or accept the
  `PodConfig` defaults pinned in `docs/runpod.md`).
- Decide policy: the default is create → wait → smoke → **delete**.
  Owner explicitly opts into `--keep-pod` when they need to retain
  `/workspace` after the run.
- Spot-check the RunPod console after the agent reports `lifecycle:
  deleted`; the console is the source of truth for "Pod is gone".

### Agent responsibilities

The agent runs inside the dev container after the owner has injected
`RUNPOD_API_KEY`. The agent:

- Reads `RUNPOD_API_KEY` from the environment **only**. Never asks the
  user to paste the value into a chat surface, never writes it to a
  file in the repo, and never includes it in a PR/issue comment.
- Invokes `python3 scripts/manage_runpod.py` (optionally with
  `--keep-pod`, `--gpu-type`, `--disk-gb`). The helper prints exactly
  one of the public-safe categories enumerated in §5 per phase.
- Echoes the category line(s) in a PR/issue comment if useful, but
  **never** the Pod ID, endpoint URL, bearer token, response body, or
  full exception messages from `httpx`.
- Does not attempt to provision long-lived storage on RunPod, set up
  warm pools, or autoscale. Those are out of scope for #70.

## 2. Injection mechanism (env vars set in the dev container)

The agreed injection mechanism is **environment variables set by the
owner in the dev container shell session**. The agent reads:

| Variable             | Required | Source                                                                                       |
| -------------------- | -------- | -------------------------------------------------------------------------------------------- |
| `RUNPOD_API_KEY`     | yes      | Owner-issued scoped RunPod account API key (§3). Presence-checked only; value never logged.  |
| `RUNPOD_TEMPLATE_ID` | optional | Owner-pinned RunPod template; defaults are baked into `PodConfig` when unset.                |

The owner sets these in their dev-container shell (e.g. by `export`-ing
them before launching the agent, or by writing them into a
`.envrc`/`direnv` file that is **gitignored**). The agent never persists
these values; they live only in the running process's environment.

**Do not** commit `.env`, `.envrc`, or any other file containing these
values to the repository. The repo's `.gitignore` already excludes
common `.env` patterns plus the local lifecycle log
(`lifecycle.jsonl`).

## 3. Temporary-API-key workflow

The agent's RunPod API key should be short-lived. Recommended flow:

1. Owner logs into the RunPod console and creates a new **account API
   key** scoped to the minimum permissions needed for Pod lifecycle
   (typically "Read/Write Pods"). A read-only key cannot create or
   delete Pods.
2. Owner exports the key in the dev container shell:
   ```bash
   export RUNPOD_API_KEY=<the-new-key>
   ```
3. Agent runs `python3 scripts/manage_runpod.py` once per smoke
   session.
4. After the session, owner **disables or deletes the key** in the
   RunPod console (§10). Do not reuse the key across long gaps.

The key is never written to disk by the agent; the only file the helper
writes is the lifecycle JSONL under `~/.cache/yomotsusaka/lifecycle.jsonl`
which records `{timestamp, request_id, category}` and nothing else.

## 4. Default policy: create → wait → smoke → delete

The helper runs the following sequence:

1. **Preflight** — `runpodctl --version` and `RUNPOD_API_KEY` presence
   checks. Either failure → exit code 2; no network call attempted.
2. **Create** — REST `POST https://rest.runpod.io/v1/pods` with the
   resolved `PodConfig`. The library raises
   `PodUnavailableError("create_failed")` on any failure, which the
   helper reports as `lifecycle: create_failed` and exit code 1. No
   cleanup needed (the Pod was never created).
3. **Wait** — poll `GET {endpoint}/health` every 5 s up to 60 times
   (5 minute cap). Timeout raises
   `PodUnavailableError("wait_timeout")`; the helper then attempts
   cleanup (unless `--keep-pod` is set).
4. **Smoke** — invoke `scripts/smoke_runpod.py --mode diagnose` as a
   subprocess with `VLLM_ENDPOINT` / `VLLM_API_KEY` / `RUNPOD_POD_ID`
   set in the child environment (NOT in any log record). The smoke's
   `diagnostic: <category>` stdout line is propagated as
   `lifecycle: smoke_passed` or `lifecycle: smoke_failed`. The smoke's
   stderr is discarded (it may carry `httpx` text).
5. **Cleanup** — by default `DELETE /v1/pods/{podId}`. The cost-control
   rationale matters: per `docs/runpod.md` §9, **stopped Pods continue
   to bill for retained volume storage**. Delete is the only way to
   stop billing entirely. `--keep-pod` skips this step and exits with
   `lifecycle: kept`; the owner is then responsible for cleanup.

The default flow guarantees that an agent run with no failures leaves no
running Pod and no billing tail.

## 5. Lifecycle categories table

`scripts/manage_runpod.py` always classifies each phase into exactly
one of the following stable literals (the source of truth is the
constants block at the top of the script):

| Category             | Phase         | When                                                                  | Owner action                                                                       |
| -------------------- | ------------- | --------------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| `runpodctl_missing`  | preflight     | `runpodctl --version` not on PATH.                                    | Install via the link printed on stderr; rerun.                                     |
| `api_key_missing`    | preflight     | `RUNPOD_API_KEY` env var not set or empty.                            | Export the key per §3; rerun.                                                      |
| `created`            | create        | Pod created successfully.                                             | (continues automatically)                                                          |
| `create_failed`      | create        | REST `POST /v1/pods` returned non-2xx or the response body was unparseable. | Inspect the RunPod console; check the account API key still has Pods permission.   |
| `healthy`            | wait          | `/health` returned 200 within the 5 minute cap.                       | (continues automatically)                                                          |
| `wait_timeout`       | wait          | `/health` did not return 200 within 60 × 5 s probes.                  | Inspect Pod logs in the RunPod console; the helper still attempts cleanup.        |
| `smoke_passed`       | smoke         | `scripts/smoke_runpod.py --mode diagnose` reported `success`.         | (continues automatically)                                                          |
| `smoke_failed`       | smoke         | Smoke reported any non-`success` category.                            | The helper still attempts cleanup; rerun after addressing the smoke's category.   |
| `deleted`            | cleanup       | `DELETE /v1/pods/{podId}` returned 2xx.                               | None; Pod is gone.                                                                 |
| `kept`               | cleanup (opt) | `--keep-pod` requested; cleanup skipped.                              | Manually delete the Pod in the RunPod console when finished.                       |
| `cleanup_failed`     | cleanup       | The DELETE call failed (network error or non-2xx).                    | See §7 — urgent owner action required.                                             |

Exit codes:

| Code | Meaning                                                            |
| ---- | ------------------------------------------------------------------ |
| 0    | Full success (`deleted` or `kept`).                                |
| 1    | Phase failure (`wait_timeout` / `smoke_failed`) after Pod created. |
| 2    | Preflight failure (no Pod created; nothing to clean up).           |
| 3    | Cleanup failed — manual owner action required (§7).                |

Exit-code precedence: when multiple non-zero categories occur in one
run, `cleanup_failed` (3) trumps a phase failure (1), which trumps
success (0). Preflight (2) is reserved for the "no Pod ever created"
case.

## 6. Sanitisation invariant

Stronger than the smoke runbook's invariant: the lifecycle envelope
(create, wait, smoke, cleanup) ALSO must not echo private values.

Forbidden in any stdout line, stderr line, log record, exception
message, or `lifecycle.jsonl` row:

- The Pod ID returned by `POST /v1/pods`.
- The endpoint URL returned by `POST /v1/pods` (or synthesised from
  the Pod ID via the RunPod proxy pattern).
- `RUNPOD_API_KEY`, `VLLM_API_KEY`, or any bearer value.
- Raw response bodies from RunPod or vLLM.
- Full `httpx` exception messages (which may include the URL).
- SSH endpoints, proxy URLs, model identifiers tied to a specific Pod.

The helper enforces this by:

- Routing every stdout line through `_emit_category()`, which prints
  exactly `lifecycle: <category>` and nothing else.
- Catching `PodUnavailableError` at every site and reading **only**
  the category literal off `args[0]`; the underlying `httpx` exception
  text never reaches stdout/stderr.
- Discarding the smoke subprocess's stderr (its stdout is already
  pre-sanitised by `scripts/smoke_runpod.py`).
- Writing `lifecycle.jsonl` rows with **exactly three keys** —
  `timestamp`, `request_id`, `category` — and no others.

A scrub-scan unit test
(`tests/test_runpod_lifecycle.py::test_manage_lifecycle_no_secret_leak_in_any_surface`)
exercises every public-safe surface with sentinel values and asserts
none leak. If you extend the helper, you **must** extend that test.

## 7. Cleanup-failure surface

When the DELETE call fails (e.g. RunPod returned 5xx, the network
dropped), the helper:

1. Prints `lifecycle: cleanup_failed` to stdout (category-only).
2. Prints one urgent line to **stderr**:
   ```
   URGENT: manual Pod cleanup required; see ~/.cache/yomotsusaka/lifecycle.jsonl for request_id=<uuid>
   ```
3. Writes a row to `~/.cache/yomotsusaka/lifecycle.jsonl` with
   `{timestamp, request_id, category: "cleanup_failed"}`.
4. Exits with code 3.

The `request_id` is a fresh UUID4 generated at the start of the run; it
carries no information about the Pod itself. The owner uses it to
correlate the stderr line with the JSONL row (e.g. to confirm timing),
then runs `runpodctl pod list` in their own shell to identify which
Pod was left behind, and deletes it manually (via `runpodctl pod
remove <id>` or the RunPod console).

Why the urgent line plus the JSONL row (both) — see the pinned
decision in the source spec
(`.claude/plans/mvp4-20260523/child_05_agent-managed-runpod-lifecycle-with-cost-control.md`):
the stderr line is for the immediate session; the JSONL row is for
forensic recovery if the transcript is replayed or stdout/stderr is
captured to a file that mixes the streams. The `request_id` is the
only non-secret correlation token.

## 8. Reporting back

When the agent comments the lifecycle result on a PR or issue, the
comment should contain:

- Each `lifecycle: <category>` line printed by the helper (these are
  the category literals from §5).
- The exit code, if useful.
- The `request_id` from the urgent stderr line, when relevant — but
  ONLY if the cleanup failed.

The comment **must not** contain:

- The Pod ID, endpoint URL, or any bearer token.
- The raw `httpx` exception text.
- The full vLLM or RunPod response body.
- The contents of `lifecycle.jsonl` beyond the three-key schema.
- Logs scraped from the Pod that contain any of the above.

If you are unsure whether a piece of context is safe to paste, omit it.

## 9. RunPod account API keys vs vLLM bearer keys

These are **not** the same thing. The disambiguation is identical to
`docs/runpod-agent-smoke.md` §3, restated here:

- A RunPod **account API key** (`RUNPOD_API_KEY`) authenticates the
  owner's RunPod account for Pod lifecycle operations (REST `POST /v1/pods`,
  `DELETE /v1/pods/{podId}`). This is the key the
  `manage_runpod.py` helper requires.
- The **vLLM bearer key** (`VLLM_API_KEY`) authenticates HTTPS calls
  to the Pod's vLLM server (`/v1/chat/completions`). This is the key
  the smoke (`scripts/smoke_runpod.py`) requires. The default falls
  back to `sk-<RUNPOD_POD_ID>` when `VLLM_API_KEY` is unset.

Mixing them up has been a recurring source of confusion; see issue
#57's review history.

## 10. Key rotation / disabling after the session

After the session ends, the owner should:

1. Disable or delete the scoped RunPod account API key in the RunPod
   console. Use the console's "revoke" action — not just deletion of
   the local `export` — so the key is unusable even if it leaked into
   shell history.
2. Verify in the RunPod console that no Pods remain billed beyond the
   intended session window.
3. If `cleanup_failed` was reported, follow §7's manual-cleanup steps
   immediately. Do not assume the next agent run will clean up the
   leftover Pod.

## 11. Live owner-runnable test (L3)

A single owner-runnable test under `tests/test_runpod_lifecycle.py` is
gated by `RUNPOD_MANAGE_LIVE=1`; absent that env var, it skips cleanly
(mirroring `RUNPOD_LIVE_SMOKE` in `tests/test_smoke_runpod_diagnose.py`).
The L3 test is **not** part of the CI merge gate; do not run it in
automated pipelines.

To run it manually, set both `RUNPOD_MANAGE_LIVE=1` and a working
`RUNPOD_API_KEY`, then:

```bash
RUNPOD_MANAGE_LIVE=1 uv run pytest tests/test_runpod_lifecycle.py -v -k live
```

The test creates a real Pod, runs the smoke, deletes the Pod, and
asserts the helper reported `deleted`. Expect the run to cost a few
cents of GPU time.
