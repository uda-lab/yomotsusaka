# RunPod Development Notes

## Recommended setup for initial development

| Parameter | Value |
|-----------|-------|
| Pod type | RunPod GPU Pod (on-demand) |
| GPU | NVIDIA RTX A5000 (24 GB VRAM) |
| Inference server | vLLM (`vllm/vllm-openai:latest`) |
| Model | `Qwen/Qwen3-8B` (fits comfortably in 24 GB) |
| Container disk | 20 GB (model weights cached here) |
| Network volume | Small persistent volume for vault/config (optional) |

---

## Lifecycle

1. **Start** a pod before a nightly batch job (`runpod_lifecycle.start_pod`).
2. **Poll** until the vLLM `/health` endpoint responds.
3. **Run** the batch (redaction, summarisation, labelling).
4. **Stop** the pod immediately after the batch completes
   (`runpod_lifecycle.stop_pod`).

Stopping pods after use is critical for cost control.  RunPod charges
per-second while the pod is running.

---

## Environment variables expected by the vLLM container

```
MODEL_ID=Qwen/Qwen3-8B
TENSOR_PARALLEL_SIZE=1
MAX_MODEL_LEN=8192
```

---

## Notes

- RunPod is **ephemeral compute only**, not durable private storage.
- Do not write private data to the container disk; use the vault on a
  separate persistent volume or local secure storage.
- The `runpod_lifecycle` module is a stub.  Implement using the RunPod
  Python SDK or REST API when ready.
- Model weights may be pre-cached in a RunPod network volume to avoid
  re-downloading on every pod start.
