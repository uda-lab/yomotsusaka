# RunPod/vLLM Agent-Runnable Smoke

Status: live/manual smoke path; **not** a CI merge gate. The L1 (mocked
HTTP) and L2 (mocked lifecycle) unit tests under
`tests/test_vllm_backend.py` and `tests/test_runpod_lifecycle.py` are the
merge gate; this document covers the post-merge, owner-provisioned smoke
that an agent runs from the dev container.

This runbook formalises the owner-to-agent handoff first described in
issue #57 and extended in issue #68: once the owner has provisioned a
development Pod and injected a temporary vLLM bearer key into the dev
container, the agent classifies failures and reports a sanitised result
without seeing or recording long-lived credentials.

## 1. Owner vs agent responsibilities

The split is intentionally narrow and must be preserved.

### Owner responsibilities

The owner is the only party who handles paid Pod lifecycle and secret
material:

- Provision the development Pod (RTX A5000 default per `docs/runpod.md`
  §3) and, when finished, **stop or delete it** — running time is the
  cost driver.
- Choose or rotate the temporary vLLM bearer key. The default key
  derived from the Pod ID (`sk-<pod-id>` per `docs/runpod.md` §6) is
  acceptable for short tests; for anything longer, override it with a
  random per-Pod key as described in §3 below.
- Inject the endpoint and key into the dev container via the agreed
  mechanism (§2). The owner is responsible for ensuring the key the dev
  container sees matches the key the Pod accepts.
- Verify the Pod is stopped after the smoke finishes (RunPod console
  check is the source of truth).

### Agent responsibilities

The agent runs inside the dev container after the owner has completed
the handoff. The agent:

- Reads `VLLM_ENDPOINT`, `VLLM_API_KEY`, and (optionally) `RUNPOD_POD_ID`
  from the environment **only**. The agent never asks the user to paste
  these values into a chat surface, never writes them to a file in the
  repo, and never includes them in a PR/issue comment.
- Invokes `scripts/smoke_runpod.py --mode diagnose` (or `--mode generate`
  for the legacy single chat completion). The diagnostic mode prints
  exactly one of the public-safe categories enumerated in §5.
- Echoes the category in a PR/issue comment if useful, but **never** the
  raw response body, the endpoint URL, the bearer header, the Pod ID,
  or full exception messages from `httpx`.
- Does not attempt to provision, stop, or autoscale Pods. Lifecycle
  decisions are owner-only.

## 2. Injection mechanism (env vars set in the dev container)

The agreed injection mechanism is **environment variables set by the
owner in the dev container shell session**. The agent reads:

| Variable           | Required | Source                                                   |
| ------------------ | -------- | -------------------------------------------------------- |
| `RUNPOD_LIVE_SMOKE`| yes      | Owner sets to `1` to opt in to a live call.              |
| `VLLM_ENDPOINT`    | yes      | Owner-provisioned Pod endpoint, e.g. `https://<pod-id>-8000.proxy.runpod.net`. |
| `VLLM_API_KEY`     | yes (recommended) | Owner-chosen temporary key (see §3); falls back to `sk-<RUNPOD_POD_ID>` when unset. |
| `RUNPOD_POD_ID`    | optional | Required only when `VLLM_API_KEY` is unset and the agent should derive the default key. |

The owner sets these in their dev-container shell (e.g. by
`export`-ing them before launching the agent, or by writing them into a
`.envrc`/`direnv` file that is **gitignored**). The agent never persists
these values; they live only in the running process's environment.

**Do not** commit `.env`, `.envrc`, or any other file containing these
values to the repository. The repo's `.gitignore` already excludes
common `.env` patterns; if a new injection convention is added later,
update `.gitignore` to match.

## 3. Temporary-key workflow (recommended)

The vLLM template authenticates via a single bearer token. Two patterns
are supported; the owner picks one per smoke session:

### 3a. Default `sk-<pod-id>` key

Acceptable for short development tests. The owner provides
`RUNPOD_POD_ID` and leaves `VLLM_API_KEY` unset; the script derives the
expected key. The Pod must be stopped immediately after the smoke.

### 3b. Owner-chosen random key (recommended for repeated runs)

The owner generates a random per-session key (e.g.
`openssl rand -hex 24` prefixed with `sk-`) and:

1. Sets that key as the vLLM bearer key on the Pod (RunPod template env
   variable or vLLM CLI flag — out of scope for this doc).
2. `export VLLM_API_KEY=<same-value>` in the dev container shell before
   running the agent.

The two values must match exactly. The owner rotates the key by setting
a new value on the Pod and re-exporting it in the dev container.

### RunPod account API keys vs vLLM bearer keys

These are **not** the same thing.

- A RunPod **account API key** authenticates the owner's RunPod account
  for Pod lifecycle operations (`runpodctl`, REST API). The agent never
  needs this and the owner never injects it into the dev container.
- The **vLLM bearer key** authenticates HTTPS calls to the Pod's vLLM
  server. This is the value the agent reads from `VLLM_API_KEY`.

Mixing them up has been a recurring source of smoke-failure confusion;
see issue #57's review history.

## 4. Running the smoke

After the owner has injected the env vars per §2, the agent runs:

```bash
RUNPOD_LIVE_SMOKE=1 python3 scripts/smoke_runpod.py --mode diagnose
```

The script fails closed without `RUNPOD_LIVE_SMOKE=1` (exit code 2,
message on stderr; no network call). With `--mode diagnose`, exit code
0 means the `success` category, non-zero means one of the failure
categories below; the category literal is always printed on a single
stdout line.

`--mode generate` is the legacy behaviour preserved from MVP-3 #46: a
single chat completion against `Qwen/Qwen3-8B` that prints the first 80
characters of the response on success.

## 5. Public-safe diagnostic categories

`scripts/smoke_runpod.py --mode diagnose` always classifies the outcome
into exactly one of the following stable literals (see
`DiagnosticCategory` in the script for the source of truth):

| Category                          | When                                                                 | Suggested next owner action                                                                 |
| --------------------------------- | -------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| `endpoint_unset`                  | `VLLM_ENDPOINT` env var missing.                                     | Export `VLLM_ENDPOINT` in the dev container shell.                                          |
| `endpoint_unreachable`            | DNS or TCP probe to the endpoint host failed.                        | Check Pod is running; check RunPod proxy URL is correct.                                    |
| `health_non_200`                  | `/health` returned non-200 (and not 404, 401, or 403).               | Pod is starting or vLLM crashed; check logs in the RunPod console.                          |
| `auth_failure`                    | `/health` or `/v1/chat/completions` returned 401 / 403.              | `VLLM_API_KEY` does not match the Pod's bearer key — re-check the temporary-key workflow.   |
| `model_mismatch_or_bad_request`   | `/v1/chat/completions` returned 400 (or another non-auth 4xx).       | Pod is up but rejected the probe — usually a model-name mismatch.                           |
| `backend_not_ready`               | `/v1/chat/completions` returned 5xx/503.                             | Model still loading or vLLM is overloaded; retry after a short wait.                        |
| `malformed_response`              | Response was not JSON or lacked `choices[0].message.content`.        | Unusual template response shape; capture logs Pod-side, do not paste them into a PR.        |
| `success`                         | Probe completed and returned the first 80 chars of the model output. | Smoke passes; stop the Pod when done.                                                       |

These literals are the *only* values the script prints alongside the
`diagnostic:` prefix. Specifically, the script never echoes
`VLLM_ENDPOINT`, `VLLM_API_KEY`, `RUNPOD_POD_ID`, the raw response body,
the `Authorization` header, or full exception messages from `httpx` —
those may include URLs, headers, or partial bodies and are wrapped at
the boundary.

## 6. Sanitisation invariant

The script enforces the following invariants. A test asserts them by
monkeypatching probe failures and scanning stdout/stderr for forbidden
substrings.

- No secret value (`VLLM_ENDPOINT`, `VLLM_API_KEY`, `RUNPOD_POD_ID`,
  bearer header, response body) is ever printed.
- Exception messages from `httpx` (which may include the URL and
  headers) are caught and replaced with a category literal.
- For the `success` category, the snippet is the **first 80 chars** of
  `choices[0].message.content`, newlines collapsed. The full response
  body is never printed.

If you extend the diagnostic surface, you **must** extend the
sanitisation test to cover the new code path.

## 7. Reporting back

When the agent comments the smoke result on a PR or issue, the comment
should contain:

- The category literal from the script's `diagnostic:` line (and the
  snippet for `success`, since it is already truncated and safe).
- The suggested-next-action text from §5, if useful.

The comment **must not** contain:

- The endpoint URL, Pod ID, or bearer key.
- The raw `httpx` exception text.
- The full vLLM response body.
- Logs scraped from the Pod that contain any of the above.

If you are unsure whether a piece of context is safe to paste, omit it
and ask the owner to inspect Pod-side logs instead.
