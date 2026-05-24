# Vocab drift — category fixture

This doc references a non-canonical OperationalCategory token.

The runner emits `batch_kaput` when all inbox files commit,
but `batch_kaput` is not a member of the canonical enum (and is
intentionally not in the documented synonym allowlist either —
it is a deliberately nonsense fixture token).
