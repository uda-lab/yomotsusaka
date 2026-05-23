"""Validator and constructor tests for :class:`yomotsusaka.tenant.TenantScope`.

These tests pin the field validators (charset, reserved prefix, traversal
exclusion) called out in the §C Fork 2 spec on issue #45. The contract is:

* ``tenant_id`` matches ``[A-Za-z0-9][A-Za-z0-9._-]{0,63}``.
* Leading ``_`` is reserved (so ``_local`` cannot be forged by a public
  caller; only :meth:`TenantScope.local` produces it).
* ``.`` and ``..`` are rejected (path traversal segments).
* ``vault_root`` must be a :class:`pathlib.Path` (not ``str`` / ``None``).

No assertions touch the on-disk vault — these are pure model-construction
tests. The boundary-side behavioural coverage lives in
``tests/test_tenant_isolation.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from yomotsusaka.tenant import TenantScope


# ---------------------------------------------------------------------------
# Happy-path construction
# ---------------------------------------------------------------------------


def test_tenant_scope_constructible_with_valid_id(tmp_path: Path) -> None:
    """A simple alphanumeric tenant_id with a real Path constructs cleanly."""
    scope = TenantScope(tenant_id="alpha", vault_root=tmp_path)
    assert scope.tenant_id == "alpha"
    assert scope.vault_root == tmp_path
    assert scope.is_local is False


def test_tenant_scope_local_classmethod(tmp_path: Path) -> None:
    """``TenantScope.local`` produces the reserved ``_local`` tenant id."""
    scope = TenantScope.local(tmp_path)
    assert scope.tenant_id == "_local"
    assert scope.vault_root == tmp_path
    assert scope.is_local is True


def test_tenant_scope_is_frozen(tmp_path: Path) -> None:
    """Pydantic ``frozen=True`` rejects attribute mutation post-construction."""
    scope = TenantScope(tenant_id="alpha", vault_root=tmp_path)
    with pytest.raises((TypeError, ValueError, AttributeError)):
        scope.tenant_id = "beta"  # type: ignore[misc]


def test_tenant_scope_charset_allows_punctuation(tmp_path: Path) -> None:
    """The permitted charset includes ``.``, ``-`` and ``_`` (after the
    leading alphanumeric)."""
    scope = TenantScope(
        tenant_id="team.alpha-2026_q1",
        vault_root=tmp_path,
    )
    assert scope.tenant_id == "team.alpha-2026_q1"


def test_tenant_scope_max_length(tmp_path: Path) -> None:
    """64 chars is permitted; 65 is rejected."""
    sixty_four = "a" + "b" * 63
    assert len(sixty_four) == 64
    TenantScope(tenant_id=sixty_four, vault_root=tmp_path)
    with pytest.raises(Exception):  # Pydantic ValidationError or ValueError
        TenantScope(tenant_id=sixty_four + "c", vault_root=tmp_path)


# ---------------------------------------------------------------------------
# Reserved-prefix / reserved-id rejection
# ---------------------------------------------------------------------------


def test_tenant_scope_rejects_reserved_local_id(tmp_path: Path) -> None:
    """Public construction with the reserved ``_local`` tenant id is rejected.

    Only :meth:`TenantScope.local` produces a ``_local`` scope; this guard
    prevents a caller from forging one through the public constructor and
    masquerading as a back-compat scope.
    """
    with pytest.raises(Exception):
        TenantScope(tenant_id="_local", vault_root=tmp_path)


def test_tenant_scope_rejects_leading_underscore(tmp_path: Path) -> None:
    """Leading ``_`` is reserved for kernel internal use; public ids must
    start with an alphanumeric character."""
    with pytest.raises(Exception):
        TenantScope(tenant_id="_anything", vault_root=tmp_path)


# ---------------------------------------------------------------------------
# Traversal-segment rejection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_id", [".", ".."])
def test_tenant_scope_rejects_traversal_segments(tmp_path: Path, bad_id: str) -> None:
    """``.`` and ``..`` are path-traversal segments and must be rejected.

    Mirrors the pre-existing ``_validate_doc_id`` and locator ``opaque_id``
    exclusion rules.
    """
    with pytest.raises(Exception):
        TenantScope(tenant_id=bad_id, vault_root=tmp_path)


# ---------------------------------------------------------------------------
# Charset rejection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_id",
    [
        "",  # empty
        "alpha/beta",  # path separator
        "alpha\\beta",  # Windows path separator
        "alpha beta",  # whitespace
        "alpha\nbeta",  # newline
        "alpha\x00beta",  # NUL
        "café",  # non-ASCII
    ],
)
def test_tenant_scope_rejects_invalid_charset(
    tmp_path: Path, bad_id: str
) -> None:
    """Anything outside ``[A-Za-z0-9._-]`` is a validation error."""
    with pytest.raises(Exception):
        TenantScope(tenant_id=bad_id, vault_root=tmp_path)


# ---------------------------------------------------------------------------
# vault_root type guard
# ---------------------------------------------------------------------------


def test_tenant_scope_rejects_non_path_vault_root() -> None:
    """``vault_root=str(...)`` is rejected — the rest of the kernel does
    ``isinstance(... , Path)`` guards on the unwrapped vault_root, so
    accepting a string here would defeat them."""
    with pytest.raises(Exception):
        TenantScope(tenant_id="alpha", vault_root="/tmp/some/path")  # type: ignore[arg-type]


def test_tenant_scope_local_rejects_non_path() -> None:
    """``TenantScope.local`` also rejects non-Path arguments."""
    with pytest.raises(Exception):
        TenantScope.local("/tmp/some/path")  # type: ignore[arg-type]
