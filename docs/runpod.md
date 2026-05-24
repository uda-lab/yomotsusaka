# RunPod Usage Notes for the Private LLM MVP

## Status

`src/yomotsusaka/runpod_lifecycle.py` and `src/yomotsusaka/vllm_backend.py`
ship a real **attach-mode** path as of issue #46: the owner provisions a
RunPod Pod manually, exports `RUNPOD_POD_ID` / `RUNPOD_POD_ENDPOINT`, and
`AttachRunPodLifecycle` + `VLLMBackend` drive `/health` and
`POST /v1/chat/completions` against that Pod. The default `mock` mode
remains no-network. The `manage` mode (full Pod creation/destruction) is
now implemented by `ManageRunPodLifecycle` (issue #76, closes #70): it
issues the create → wait-for-health → delete lifecycle against the
RunPod REST API using `RUNPOD_API_KEY` from env, with cost-controlled
delete-by-default behaviour. The L1/L2 mocked tests under
`tests/test_runpod_lifecycle.py` are the merge gate; the L3
owner-runnable live test gated by `RUNPOD_MANAGE_LIVE=1` is explicitly
NOT a CI merge gate. See
[`docs/runpod-agent-lifecycle.md`](runpod-agent-lifecycle.md) for the
agent-runnable runbook and owner/agent responsibility split.
`src/yomotsusaka/execution_gateway.py` and
`src/yomotsusaka/transfer.py` remain stubs. The local MVP still runs
CPU-only with `DummyBackend` by default; `VLLMBackend` is opt-in. See
[`docs/scaffold-status.md`](scaffold-status.md) for the canonical module
classification table.

## 1. Purpose

This note records the current practical RunPod setup for the private-data firewall / private LLM MVP. It is intentionally lightweight and operational. The goal is to make future agent-assisted development reproducible without over-designing the RunPod layer.

RunPod is used here as an ephemeral GPU development and batch-processing environment. It is not treated as durable private storage.

## 2. Current Working Assumption

Use RunPod **Pods**, not Serverless, Public Endpoints, or Clusters.

Reason:

- Pods behave like temporary GPU machines.
- They are easy to access via shell.
- They can run vLLM directly.
- They are suitable for development and one-shot batch jobs.
- Serverless may become useful later, but it adds handler/container design overhead.

## 3. Development GPU Choice

For cheap development, **RTX A5000 24GB** is acceptable.

Current observed spend rate:

```text
$0.394 / hr
```

This is suitable for:

- RunPod/vLLM connectivity tests;
- small model serving;
- Qwen/Qwen3-8B experiments;
- private-data firewall pipeline development;
- structured extraction MVP work.

It is not suitable for full-size high-quality validation of large models such as Gemma 4 26B/31B in BF16. For larger model validation, use A100 80GB or H100 80GB.

## 4. Initial vLLM Template

The RunPod vLLM latest template currently serves:

```text
Qwen/Qwen3-8B
```

Example startup arguments observed from the template:

```text
Qwen/Qwen3-8B --host 0.0.0.0 --port 8000 --dtype auto --enforce-eager --gpu-memory-utilization 0.95 --max-model-len 8128
```

For development on A5000, this is reasonable. If out-of-memory errors occur, reduce:

```text
--gpu-memory-utilization 0.90
```

or lower.

## 5. Storage Guidance

For cheap development:

```text
Container disk: 30–50GB
Persistent / Volume disk: 0–20GB
Network volume: none
```

A practical first choice:

```text
Container disk: 40GB
Volume disk: 20GB
```

If model cache reuse is important, increase volume disk to 30–50GB. Otherwise, keep persistent storage small.

Principle:

> RunPod may cache open-weight model files, but it should not become durable storage for private documents or private dictionaries.

## 6. API Key Behaviour

The vLLM template enables API key authentication.

By default, the key is derived from the Pod ID:

```text
sk-<pod-id>
```

For serious use, override this with:

```text
VLLM_API_KEY=<random-long-secret>
```

For short development tests, the default key is acceptable, but the Pod should be stopped after use.

## 7. Localhost Test Command

Inside the Pod, use the following one-liner:

```bash
VLLM_API_KEY="<your-vllm-api-key>"; curl -s "http://127.0.0.1:8000/v1/chat/completions" -H "Authorization: Bearer ${VLLM_API_KEY}" -H "Content-Type: application/json" -d '{"model":"Qwen/Qwen3-8B","messages":[{"role":"user","content":"日本語で短く自己紹介してください。"}],"temperature":0.2,"max_tokens":128}' | jq -r '.choices[0].message.content'
```

If using the default key:

```bash
POD_ID="<your-pod-id>"; curl -s "http://127.0.0.1:8000/v1/chat/completions" -H "Authorization: Bearer sk-${POD_ID}" -H "Content-Type: application/json" -d '{"model":"Qwen/Qwen3-8B","messages":[{"role":"user","content":"日本語で短く自己紹介してください。"}],"temperature":0.2,"max_tokens":128}' | jq -r '.choices[0].message.content'
```

### Smoke script

For testing from the local machine against a Pod accessed via the RunPod
proxy, `scripts/smoke_runpod.py` issues the same single chat-completion
through `VLLMBackend`. The script refuses to run unless
`RUNPOD_LIVE_SMOKE=1` is set, and on success prints the first 80 chars of
the response. It is **owner-only and explicitly NOT a CI merge gate** —
see issue #46 metaplan Fork 8 for the rationale (cost control: the L1 +
L2 mocked tests under `tests/test_vllm_backend.py` and
`tests/test_runpod_lifecycle.py` are the merge gate).

```bash
RUNPOD_LIVE_SMOKE=1 \
    VLLM_ENDPOINT=https://<pod-id>-8000.proxy.runpod.net \
    VLLM_API_KEY=sk-<pod-id-or-override> \
    python3 scripts/smoke_runpod.py
```

## 8. Cost Interpretation

A short test that produced only two responses consumed about:

```text
$0.09
```

At the observed rate:

```text
$0.394 / hr ≈ $0.0066 / min
$0.09 ≈ 13.7 min
```

The cost was mostly Pod startup, model loading, and interactive waiting time, not the token generation itself.

Expected costs at this rate:

```text
1 hour:  about $0.39
3 hours: about $1.18
8 hours: about $3.15
30 hours/month: about $11.82
```

This is acceptable for MVP development.

## 9. Operational Rule

For development:

```text
Start Pod → test/develop → stop Pod immediately after work
```

Do not leave a running GPU Pod idle. Running time is the main cost driver.

Stopped or deleted state should be checked explicitly in the RunPod console after each session.

## 10. Agent Access Policy for Development

For development only, it is acceptable to give an AI coding agent temporary RunPod access using:

- a development API key (**required** for the live managed lifecycle —
  see `docs/runpod-agent-lifecycle.md` §2);
- an existing Pod ID and endpoint URL (`attach` mode only);
- SSH access if needed.

`runpodctl` is **optional owner break-glass tooling** (issue #90 / MVP-5
umbrella #89). The agent-managed lifecycle is REST end-to-end; install
`runpodctl` only when you want a local CLI for manual recovery after a
terminal `cleanup_failed` (see `docs/runpod-agent-lifecycle.md` §10.5).

This should be treated separately from the production private-data firewall architecture.

Lightweight safeguards:

- use a dedicated temporary API key;
- delete or disable the key after development;
- do not give the agent billing/admin credentials unrelated to RunPod work;
- check active Pods after the agent finishes;
- instruct the agent to stop the Pod after use.

Avoid building a complex wrapper until the workflow proves stable.

## 11. MVP Relationship

For the private-data firewall MVP, RunPod should initially be used in two modes:

### Development mode

Manual or semi-manual Pod operation. Useful for testing vLLM, schemas, extraction prompts, redaction scripts, and batch scripts.

### Batch mode

Later, a scheduler can start a Pod, run a batch job, return outputs, and stop the Pod. This should remain a thin automation layer until real operational needs justify more complexity.

The `manage` mode landed in issue #76 is the first thin slice of this
automation: `ManageRunPodLifecycle` exposes a cost-controlled create →
wait → smoke → delete loop intended to be run from inside the dev
container by an agent, with the owner injecting `RUNPOD_API_KEY` and
spot-checking the RunPod console after the run reports `lifecycle:
deleted`. See [`docs/runpod-agent-lifecycle.md`](runpod-agent-lifecycle.md)
for the full runbook.

## 12. Current Recommendation

For immediate development:

```text
RunPod Pods
RTX A5000 24GB
vLLM latest template
Qwen/Qwen3-8B
Container disk 40GB
Volume disk 20GB
No network volume
Stop Pod after use
```

For serious model-quality testing:

```text
A100 80GB or H100 80GB
larger open-weight model
same pipeline interface
short controlled test window
```

The architecture should depend on the pipeline interface, not on the specific GPU or model selected for early development.
