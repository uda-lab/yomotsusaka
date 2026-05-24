# Redaction-quality fixture corpus

Small, fully synthetic corpus used by
`src/yomotsusaka/eval/redaction_quality.py` and exercised by
`tests/eval/test_redaction_quality.py` (issue #94, MVP-5 child 05).

## Layout

Each document is two paired files:

- `<name>.txt` — the raw document body (UTF-8, no trailing newline
  stripped).
- `<name>.expected.json` — ground-truth metadata:

```json
{
  "tenant_id": "_local",
  "spans": [
    {"start": 0, "end": 9, "kind": "PERSON"},
    {"start": 19, "end": 28, "kind": "ORG"}
  ],
  "expected_keys": ["<PERSON_a5f4ff58>", "<ORG_a73cb456>"]
}
```

Fields:

- `tenant_id` — placeholder-consistency grouping. The harness asserts
  that for a given `tenant_id`, the same source token always maps to
  exactly one redacted key. Use `"_local"` for documents that share one
  vault.
- `spans` — every span the deterministic proposer is expected to find,
  as `(start, end, kind)`. `kind` must be a member of
  `yomotsusaka.schemas.EntityKind`.
- `expected_keys` — the redactor-produced placeholder set
  (`<KIND_hex>`). Derived from `yomotsusaka.redactor._make_key` so a
  span-set change MUST be re-derived; the harness recomputes the actual
  keys and asserts equality against this set.

## Privacy discipline

The corpus is synthetic, but the harness treats every value inside the
documents as if it were private. Tests, log records, and the harness's
public-facing report MUST NOT echo any raw value from the documents.
Assertions key on offsets, kinds, counts, and placeholder identifiers
only.

## Adding a fixture

1. Author the `.txt` document (synthetic content only — never paste
   from any real data source).
2. Run the deterministic proposer manually and confirm the spans you
   intend are recovered. If a span is missed, decide whether to (a)
   tighten the fixture text so the default rules cover it, (b) file a
   follow-up issue against the proposer (out of scope for this
   harness), or (c) leave it as a known miss in `.expected.json` — but
   only after raising the per-document miss threshold by intent. The
   default in-repo threshold is **zero misses**.
3. Recompute `expected_keys` via `redact()` and embed them verbatim.
