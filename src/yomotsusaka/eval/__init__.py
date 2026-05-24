"""Evaluation harnesses for the yomotsusaka kernel (issue #94, MVP-5 child 05).

This subpackage hosts deterministic, public-safe quality harnesses that
exercise kernel modules against curated fixture corpora and surface
regressions as hard failures rather than silent logs.

Modules
-------

* :mod:`yomotsusaka.eval.redaction_quality` — measures how well
  :class:`yomotsusaka.span_proposer.DeterministicSpanProposer` recovers
  expected sensitive spans on the in-repo fixture corpus, and reports
  false negatives, false positives, and placeholder-consistency
  violations.
* :mod:`yomotsusaka.eval.redaction_quality_inference` — comparison
  scaffold for inference-backed span proposers. Defaults to a mock
  backend; a ``--live`` opt-in is owner-gated and **never** runs in CI.

Privacy discipline (binding)
----------------------------

Every harness in this subpackage MUST satisfy the same public-safe
discipline as the rest of the kernel:

* No raw private value from the corpus ever appears in a harness log
  record, structured report, exception message, or test-failure
  message. Reports key on counts, kinds, document identifiers, and
  redaction placeholders (``<KIND_hex>``), all of which are
  public-safe by construction.
* The fixture corpus is synthetic but is treated as if its contents
  were private — never echoed back through any agent-facing surface.
"""

from __future__ import annotations

__all__: list[str] = []
