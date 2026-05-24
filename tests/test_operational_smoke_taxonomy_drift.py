"""Drift test: every category token emitted by ``operational_smoke``
must be an ``OperationalCategory`` member value.

Without this gate the smoke-CLI's stable wire vocabulary and the canonical
taxonomy enum would silently re-diverge (see umbrella issue #105 finding
F3 + issue #111). When this test fails, either:

* the smoke CLI added a fresh ``_CAT_*`` literal without a corresponding
  ``OperationalCategory`` enum member (the usual fix is to add the enum
  member with a ``RecoveryInstruction`` row); OR
* a literal was misspelt at the smoke side (the fix is at the smoke side).

The test introspects ``operational_smoke``'s module-level ``_CAT_*``
constants by attribute scan so it has zero hand-maintained mapping —
any new constant added at the smoke side participates automatically.
"""

from __future__ import annotations

from yomotsusaka.cli import operational_smoke
from yomotsusaka.operational_taxonomy import OperationalCategory


def test_every_smoke_category_is_canonical() -> None:
    """Every ``_CAT_*`` literal in ``operational_smoke`` is an
    ``OperationalCategory.value``.
    """
    canonical = {c.value for c in OperationalCategory}
    smoke_tokens = {
        getattr(operational_smoke, name)
        for name in dir(operational_smoke)
        if name.startswith(("_CAT_OK_", "_CAT_FAIL_", "_CAT_SKIPPED_", "_CAT_KEPT_"))
        and isinstance(getattr(operational_smoke, name), str)
    }
    # Empty token-set is a regression risk — guards against a refactor
    # that removes the _CAT_* prefix and makes the assertion vacuously
    # true.
    assert smoke_tokens, (
        "operational_smoke exposes no _CAT_* constants; the drift test "
        "would vacuously pass. Restore the canonical token literals."
    )
    missing = smoke_tokens - canonical
    assert not missing, (
        f"operational_smoke emits tokens not in OperationalCategory: "
        f"{sorted(missing)}. Either add the enum member with a "
        "RecoveryInstruction row or correct the smoke-side literal."
    )
