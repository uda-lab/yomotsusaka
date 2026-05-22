"""
yomotsusaka — best-effort private data firewall for agent workflows.

Raw private data stays in the vault.  Agent-facing outputs contain only
redacted documents, manifests, labels, keys, summaries, and artifact handles.

Ordinary agents should import :mod:`yomotsusaka.boundary` — the opaque public
surface that exposes locator-keyed request/response models and the
fail-closed local resolver. Kernel modules (``pipeline``, ``commit``,
``restoration_api``, ``search_gateway``) remain importable from their
original paths but are classified as private-side internal kernel; they
are intentionally not re-exported here.
"""

from yomotsusaka import boundary  # noqa: F401 — public boundary surface re-export

__version__ = "0.1.0"
