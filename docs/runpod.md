# RunPod Usage Notes for the Private LLM MVP

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

- a development API key;
- `runpodctl`;
- an existing Pod ID;
- SSH access if needed.

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
