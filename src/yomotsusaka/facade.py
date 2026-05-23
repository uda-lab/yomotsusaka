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
  facade calls :func:`boundary.restoration_request` with
  ``scope=ResolverScope.ORDINARY_AGENT`` only; the boundary's scope gate
  guarantees that ordinary-agent restoration requests are denied with
  ``failure_reason="scope_denied"`` **before** the kernel module is
  imported or invoked. Callers needing private-boundary semantics must
  invoke :func:`boundary.restoration_request` directly with the
  appropriate scope.
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
    ResolverScope,
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
from yomotsusaka.tenant import TenantScope


class LocalFacade:
    """Ordinary-agent entry point over the local boundary.

    Holds a :class:`TenantScope` (or a back-compat ``vault_root``) and a
    :class:`SearchGateway` so callers do not need to thread either through
    every operation. Construction is filesystem-free; the vault directory is
    only touched when an operation that needs it is invoked.

    Parameters
    ----------
    vault_root:
        Legacy back-compat alias for ``tenant=TenantScope.local(vault_root)``.
        Accepted positionally for callers written before tenant scoping
        landed (issue #45, Fork 5). Exactly one of ``vault_root`` or
        ``tenant`` must be supplied; passing both raises ``ValueError``.
    tenant:
        :class:`~yomotsusaka.tenant.TenantScope` for a multi-tenant caller.
        Cross-tenant isolation is enforced at the boundary by ``vault_root``
        disjointness; per Fork 4 the audit log is per-tenant
        (``<tenant.vault_root>/audit/restoration.jsonl``) so audit lines
        cannot leak across tenants.
    gateway:
        Optional :class:`SearchGateway`. When ``None`` (the default) a fresh
        empty gateway is constructed lazily so callers that only need
        :meth:`process` / :meth:`inspect` / :meth:`status_report` /
        :meth:`request_restore` need not import :class:`SearchGateway`
        themselves. Fork 8: gateways are per-tenant by construction (callers
        must not share a single gateway instance across distinct tenants).
    """

    def __init__(
        self,
        vault_root: Path | None = None,
        *,
        tenant: TenantScope | None = None,
        gateway: SearchGateway | None = None,
    ) -> None:
        if vault_root is not None and tenant is not None:
            raise ValueError(
                "pass either vault_root=Path(...) or tenant=TenantScope(...), "
                "not both"
            )
        if vault_root is None and tenant is None:
            raise ValueError(
                "LocalFacade requires either vault_root=Path(...) or "
                "tenant=TenantScope(...)"
            )
        if tenant is not None:
            if not isinstance(tenant, TenantScope):
                raise ValueError(
                    f"tenant must be a TenantScope; got {type(tenant).__name__}"
                )
            self._tenant: TenantScope = tenant
        else:
            assert vault_root is not None  # narrow for type-checkers
            if not isinstance(vault_root, Path):
                raise ValueError(
                    f"vault_root must be a pathlib.Path; got {type(vault_root).__name__}"
                )
            self._tenant = TenantScope.local(vault_root)
        self._gateway: SearchGateway | None = gateway

    @property
    def vault_root(self) -> Path:
        """The vault root passed at construction time.

        For a :class:`TenantScope`-constructed facade this returns
        ``tenant.vault_root``; the back-compat shape is preserved so
        callers written before #45 see no behavioural change.
        """
        return self._tenant.vault_root

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
        return process_document_request(request, tenant=self._tenant)

    def inspect(
        self, request: InspectRequest
    ) -> InspectResponse | ResolverFailure:
        """Delegate to :func:`boundary.inspect_request`."""
        return inspect_request(request, tenant=self._tenant)

    def search(self, request: SearchRequest) -> SearchResponse:
        """Delegate to :func:`boundary.search_request` using the held gateway."""
        return search_request(request, gateway=self.gateway)

    def request_restore(
        self, request: RestorationRequest
    ) -> RestorationResponse:
        """Delegate to :func:`boundary.restoration_request` with ordinary scope.

        The facade is hard-wired to ``ResolverScope.ORDINARY_AGENT`` so an
        ordinary-agent caller can submit a restoration request through the
        sanctioned audit-logged path while remaining unable to obtain raw
        private values: the boundary's scope gate returns
        ``failure_reason="scope_denied"`` for every non-ordinary scope,
        after writing the denial to the audit log. Callers that actually
        need raw values must invoke
        :func:`yomotsusaka.boundary.restoration_request` directly with the
        narrower scope value, not via this facade.
        """
        return restoration_request(
            request,
            scope=ResolverScope.ORDINARY_AGENT,
            tenant=self._tenant,
        )

    def status_report(
        self, request: StatusReportRequest
    ) -> StatusReportResponse:
        """Delegate to :func:`boundary.status_report_request`."""
        return status_report_request(request, tenant=self._tenant)


__all__ = ["LocalFacade"]
