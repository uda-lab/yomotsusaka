"""
yomotsusaka — best-effort private data firewall for agent workflows.

Raw private data stays in the vault.  Agent-facing outputs contain only
redacted documents, manifests, labels, keys, summaries, and artifact handles.

Ordinary agents should import :mod:`yomotsusaka.boundary` — the opaque public
surface that exposes locator-keyed request/response models and the
fail-closed local resolver — or use :class:`yomotsusaka.facade.LocalFacade`,
the in-process ordinary-agent entry point that holds ``vault_root`` and a
:class:`~yomotsusaka.search_gateway.SearchGateway` at construction time and
delegates 1:1 to the boundary entry points. Kernel modules (``pipeline``,
``commit``, ``restoration_api``, ``search_gateway``) remain importable from
their original paths but are classified as private-side internal kernel;
they are intentionally not re-exported here.
"""

from yomotsusaka import boundary  # noqa: F401 — public boundary surface re-export
from yomotsusaka.facade import LocalFacade

__all__ = ["LocalFacade", "boundary"]
__version__ = "0.1.0"
