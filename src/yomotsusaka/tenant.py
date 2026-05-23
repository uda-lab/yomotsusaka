"""
Tenant scoping for the local vault boundary.

This module introduces :class:`TenantScope`, the runtime carrier that binds
an opaque ``tenant_id`` to a resolved on-disk ``vault_root`` for every
boundary entry point. See ``docs/architecture.md`` §5.7.2 "Tenant scoping".

Design notes (binding metaplan on issue #45):

* The kernel never resolves ``tenant_id → vault_root`` itself. That mapping is
  caller-side ("operator code") and lives outside this package. The kernel only
  sees a :class:`TenantScope` whose ``vault_root`` has already been chosen.
* ``tenant_id`` is a *runtime* label. It is never persisted into a manifest,
  audit record, private dictionary, or public locator. The on-disk layout is
  identical to the pre-tenant single-vault layout; the path *is* the tenant
  scope (Fork 4 / Fork 6).
* :meth:`TenantScope.local` produces a back-compat scope with the reserved
  ``tenant_id="_local"`` value. The ``_`` prefix is reserved and rejected by
  the validator so no real tenant id can collide with it.
* Failure-mode taxonomy is unchanged (Fork 9): cross-tenant locator misses at
  the resolver return the existing ``UnknownArtifact`` reason; malformed
  ``tenant_id`` at construction time raises :class:`ValueError`.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, field_validator


# Charset mirrors ``pipeline._validate_doc_id`` / locator ``opaque_id`` so the
# same constraints (filesystem-safe, no traversal, no path separators) apply
# to a tenant id. Length is capped at 64; ``opaque_id`` is 128, but tenants
# are coarser-grained and a tighter cap keeps audit / log lines compact.
_TENANT_ID_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
"""Public tenant ids: must start with an alphanumeric and stay within
``[A-Za-z0-9._-]``. Leading ``_`` is reserved for the back-compat
``"_local"`` scope and is therefore not matchable by this pattern."""

_RESERVED_LOCAL_TENANT_ID = "_local"
"""Reserved tenant id used by :meth:`TenantScope.local` so the back-compat
wrapper never collides with a real caller-supplied tenant id."""


def _is_reserved_tenant_id(value: str) -> bool:
    """Return ``True`` if *value* is one of the kernel-reserved tenant ids.

    Currently a one-element set (``"_local"``), but extracted as a helper so
    a future second reserved id is a one-line change.
    """
    return value == _RESERVED_LOCAL_TENANT_ID


class TenantScope(BaseModel, frozen=True):
    """Per-tenant scope bound to a resolved vault root.

    Construct via either:

    * :meth:`TenantScope.local` — back-compat shortcut for the legacy
      single-vault layout. Sets ``tenant_id = "_local"``.
    * Direct construction with an explicit, validated ``tenant_id`` and the
      caller-resolved ``vault_root: Path`` for that tenant.

    The kernel reads ``tenant.vault_root`` exactly the way it used to read
    the bare ``vault_root`` argument; the tenant id is intentionally not
    interpolated into any on-disk path.
    """

    model_config = ConfigDict(extra="forbid")
    tenant_id: str
    vault_root: Path

    @field_validator("tenant_id")
    @classmethod
    def _validate_tenant_id(cls, value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("tenant_id must be a string")
        if value in {".", ".."}:
            raise ValueError(
                "tenant_id must not be a path traversal segment"
            )
        # The ``_local`` reserved id is constructed only through
        # ``TenantScope.local`` (which bypasses this validator). Anyone
        # attempting to construct it directly is rejected so the reserved-id
        # invariant cannot be forged by a public caller.
        if _is_reserved_tenant_id(value):
            raise ValueError(
                f"tenant_id {value!r} is reserved for internal back-compat use"
            )
        if not _TENANT_ID_PATTERN.fullmatch(value):
            raise ValueError(
                "tenant_id must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}; "
                "path separators, traversal segments, and leading '_' are not allowed"
            )
        return value

    @field_validator("vault_root", mode="before")
    @classmethod
    def _validate_vault_root(cls, value: object) -> Path:
        # ``mode="before"`` so this validator sees the raw input (e.g. a
        # ``str``) instead of Pydantic's coerced :class:`Path`. The rest
        # of the kernel does ``isinstance(_, Path)`` guards on the
        # unwrapped vault_root, so accepting a coerced string here would
        # silently re-introduce the type confusion this validator exists
        # to prevent.
        if not isinstance(value, Path):
            raise ValueError("vault_root must be a pathlib.Path")
        return value

    @classmethod
    def local(cls, vault_root: Path) -> "TenantScope":
        """Return a back-compat scope for the legacy single-vault layout.

        Used by every internal call site that previously took
        ``vault_root: Path`` directly. The ``tenant_id="_local"`` value is
        reserved and unreachable through public construction.
        """
        if not isinstance(vault_root, Path):
            raise ValueError("vault_root must be a pathlib.Path")
        # Bypass the public field validator by constructing through
        # ``model_construct``: validators reject ``_local`` for everyone
        # except this classmethod, which is the single legitimate producer.
        return cls.model_construct(
            tenant_id=_RESERVED_LOCAL_TENANT_ID,
            vault_root=vault_root,
        )

    @property
    def is_local(self) -> bool:
        """``True`` for back-compat scopes produced by :meth:`local`."""
        return self.tenant_id == _RESERVED_LOCAL_TENANT_ID


__all__ = ["TenantScope"]
