"""
Local boundary facade — ordinary-agent entry point over :mod:`yomotsusaka.boundary`.

This module provides :class:`LocalFacade`, a thin operational wrapper around
the five MVP-2 boundary entry points. It exists to spare in-process callers
from threading ``vault_root`` and a :class:`SearchGateway` through every call
site; it does not introduce a new public surface, a new type vocabulary, or
new policy.

Privacy invariant
-----------------
The facade adds **no code path** that returns a value the underlying
``boundary.*_request`` function would not have returned. Concretely:

* No path constructs the private-boundary resolver scope. The facade is
  hard-wired to ordinary-agent semantics, which is also the default scope
  inside :func:`boundary.inspect_request`. Callers needing private-boundary
  semantics (e.g. the future #27 restoration flow) must call
  :func:`boundary.resolve` directly with the appropriate scope value.
* No path imports or invokes the private restoration kernel module. The
  shape-only :func:`boundary.restoration_request` is the only restoration
  surface this facade exposes, and it returns
  :class:`RestorationResponse(outcome="deferred")` in MVP-2.
* No path reads from ``<vault_root>/private/`` or exposes the internal
  ``vault_path`` field. The boundary already discards ``vault_path`` before
  returning a :class:`PublicHandle`; the facade is a pure delegator over
  that already-public surface.

These invariants are intentionally checkable by literal substring scan of
this file (see ``tests/test_facade.py``). Names that would violate the
invariant — including the private-scope enum member and the kernel
restoration module name — must not appear anywhere in this source file,
including in this docstring.

Out of scope
------------
* ``audit_log`` — the original issue body listed it as a candidate command,
  but no matching entry point exists in :mod:`boundary` for MVP-2 and the
  real audit-log surface is owned by #27. Adding it here would create a
  facade method without a backing boundary primitive.
* Any CLI wrapper — explicitly listed as a non-goal on #30. A future
  ``argparse`` wrapper can be layered on top of :class:`LocalFacade` without
  changing this module.
"""

from __future__ import annotations

from pathlib import Path

from yomotsusaka.boundary import (
    InspectRequest,
    InspectResponse,
    ProcessRequest,
    ProcessResponse,
    ResolverFailure,
    RestorationRequest,
    RestorationResponse,
    SearchRequest,
    SearchResponse,
    StatusReportRequest,
    StatusReportResponse,
    inspect_request,
    process_document_request,
    restoration_request,
    search_request,
    status_report_request,
)
from yomotsusaka.search_gateway import SearchGateway


class LocalFacade:
    """Ordinary-agent entry point over the local boundary.

    Holds ``vault_root`` and a :class:`SearchGateway` so callers do not need
    to thread either through every operation. Construction is filesystem-free;
    the vault directory is only touched when an operation that needs it is
    invoked.

    Parameters
    ----------
    vault_root:
        Local vault root. Forwarded as-is to every boundary entry point that
        accepts ``vault_root``.
    gateway:
        Optional :class:`SearchGateway`. When ``None`` (the default) a fresh
        empty gateway is constructed lazily so callers that only need
        :meth:`process` / :meth:`inspect` / :meth:`status_report` /
        :meth:`request_restore` need not import :class:`SearchGateway`
        themselves.
    """

    def __init__(
        self,
        vault_root: Path,
        *,
        gateway: SearchGateway | None = None,
    ) -> None:
        self._vault_root = vault_root
        self._gateway: SearchGateway | None = gateway

    @property
    def vault_root(self) -> Path:
        """The vault root passed at construction time."""
        return self._vault_root

    @property
    def gateway(self) -> SearchGateway:
        """Return the underlying gateway, constructing a fresh one on first use."""
        if self._gateway is None:
            self._gateway = SearchGateway()
        return self._gateway

    # ------------------------------------------------------------------
    # 1:1 delegations to boundary.*_request
    # ------------------------------------------------------------------

    def process(self, request: ProcessRequest) -> ProcessResponse:
        """Delegate to :func:`boundary.process_document_request`."""
        return process_document_request(request, vault_root=self._vault_root)

    def inspect(
        self, request: InspectRequest
    ) -> InspectResponse | ResolverFailure:
        """Delegate to :func:`boundary.inspect_request`."""
        return inspect_request(request, vault_root=self._vault_root)

    def search(self, request: SearchRequest) -> SearchResponse:
        """Delegate to :func:`boundary.search_request` using the held gateway."""
        return search_request(request, gateway=self.gateway)

    def request_restore(
        self, request: RestorationRequest
    ) -> RestorationResponse | ResolverFailure:
        """Delegate to :func:`boundary.restoration_request`.

        Always shape-only in MVP-2: success returns
        :class:`RestorationResponse(outcome="deferred")`. The real
        restoration flow is scoped to #27.
        """
        return restoration_request(request, vault_root=self._vault_root)

    def status_report(
        self, request: StatusReportRequest
    ) -> StatusReportResponse:
        """Delegate to :func:`boundary.status_report_request`."""
        return status_report_request(request, vault_root=self._vault_root)


__all__ = ["LocalFacade"]
